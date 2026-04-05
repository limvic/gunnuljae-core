"""
건눌재 Core Engine v5
KIS API → 실데이터 + 건눌재 점수 + 다중 타임프레임
"""

import os, time, asyncio, httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="건눌재 Core Engine", version="5.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

KIS_APP_KEY    = os.environ.get("KIS_APP_KEY", "")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
KIS_MODE       = os.environ.get("KIS_MODE", "mock")

BASE_URL   = "https://openapivts.koreainvestment.com:29443" if KIS_MODE == "mock" else "https://openapi.koreainvestment.com:9443"
TOKEN_PATH = "/oauth2/tokenP" if KIS_MODE == "mock" else "/oauth2/token"

_token_cache = {"token": None, "expires_at": 0}
_cache: dict = {}
CACHE_TTL = 10  # v5: 10초 캐시 (더 자주 갱신)


# ── 토큰 ──────────────────────────────────────────────
async def get_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    async with httpx.AsyncClient(timeout=10) as c:
        res = await c.post(f"{BASE_URL}{TOKEN_PATH}", json={
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        })
        data = res.json()
    if "access_token" not in data:
        raise HTTPException(status_code=503, detail="토큰 실패")
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + 86400
    return _token_cache["token"]


# ── 현재가 조회 ───────────────────────────────────────
async def fetch_price(code: str, token: str) -> dict:
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010100", "custtype": "P",
    }
    async with httpx.AsyncClient(timeout=10) as c:
        res = await c.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
            params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
            headers=headers,
        )
    data = res.json()
    if data.get("rt_cd") != "0":
        raise Exception("가격 조회 실패")
    o = data["output"]
    price      = int(o.get("stck_prpr", 0))
    high       = int(o.get("stck_hgpr", 0))
    low        = int(o.get("stck_lwpr", 0))
    open_      = int(o.get("stck_oprc", 0))
    prev_close = int(o.get("stck_sdpr", 0))
    volume     = int(o.get("acml_vol", 0))
    vol_ratio  = float(o.get("vol_tnrt", 0))  # 거래량 회전율
    return {
        "price": price, "high": high, "low": low,
        "open": open_, "prev_close": prev_close,
        "volume": volume, "vol_ratio": vol_ratio,
        "name": o.get("hts_kor_isnm", code),
        "change_pct": float(o.get("prdy_ctrt", 0)),
        "change_amt": int(o.get("prdy_vrss", 0)),
        "is_bullish": price >= open_,
        "pullback_pct": round((high - price) / high * 100, 2) if high > 0 else 0,
        "high_ratio": round(price / high * 100, 1) if high > 0 else 100,  # 고점대비 위치
    }


# ── 분봉 데이터 조회 (60분/5분) ────────────────────────
async def fetch_minute(code: str, token: str, min_type: str = "60") -> dict:
    """min_type: '60' or '5'"""
    tr_id = "FHKST03010200"  # 분봉 조회
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": tr_id, "custtype": "P",
    }
    async with httpx.AsyncClient(timeout=10) as c:
        res = await c.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            params={
                "fid_etc_cls_code": "", "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": code, "fid_input_hour_1": min_type,
                "fid_pw_data_incu_yn": "N",
            },
            headers=headers,
        )
    data = res.json()
    if data.get("rt_cd") != "0" or not data.get("output2"):
        return {}

    candles = data["output2"][:20]  # 최근 20개 봉
    if not candles:
        return {}

    prices  = [int(c.get("stck_prpr", 0)) for c in candles if c.get("stck_prpr")]
    volumes = [int(c.get("cntg_vol", 0))  for c in candles if c.get("cntg_vol")]

    cur_price = prices[0]  if prices  else 0
    avg_vol   = sum(volumes) / len(volumes) if volumes else 1
    cur_vol   = volumes[0] if volumes else 0

    # 이평선 계산
    ma5  = sum(prices[:5])  / min(5,  len(prices))
    ma20 = sum(prices[:20]) / min(20, len(prices))

    # VWAP 추정 (고가+저가+종가)/3 평균
    highs = [int(c.get("stck_hgpr", prices[i])) for i, c in enumerate(candles) if prices]
    lows  = [int(c.get("stck_lwpr", prices[i])) for i, c in enumerate(candles) if prices]
    vwap  = sum((h + l + p) / 3 for h, l, p in zip(highs, lows, prices)) / len(prices) if prices else cur_price

    return {
        "cur_price": cur_price,
        "ma5": round(ma5), "ma20": round(ma20),
        "vwap": round(vwap),
        "is_above_vwap": cur_price > vwap,
        "is_above_ma5": cur_price > ma5,
        "vwap_pct": round((cur_price - vwap) / vwap * 100, 2) if vwap > 0 else 0,
        "vol_ratio_min": round(cur_vol / avg_vol, 2) if avg_vol > 0 else 1,
        "trend_aligned": ma5 > ma20,
    }


# ── 건눌재 점수 계산 ──────────────────────────────────
def score_daily(p: dict) -> dict:
    """일봉 건눌재 점수"""
    gap      = round((p["open"] - p["prev_close"]) / p["prev_close"] * 100, 2) if p["prev_close"] > 0 else 0
    pullback = p["pullback_pct"]
    chg      = p["change_pct"]

    trend_s   = 30 if chg > 0 and gap > -1 else 10 if chg > 0 else 0
    pullback_s = 25 if 5 <= pullback <= 12 else 15 if 2 <= pullback < 5 else 5 if pullback < 2 else 0
    volume_s  = 20 if p["vol_ratio"] > 1.5 else 10 if p["vol_ratio"] > 1.0 else 5
    candle_s  = 15 if p["is_bullish"] else 0
    breakout_s = 10 if p["high_ratio"] >= 98 else 5 if p["high_ratio"] >= 95 else 0

    score = trend_s + pullback_s + volume_s + candle_s + breakout_s
    grade = "S" if score >= 85 else "A" if score >= 70 else "B" if score >= 55 else "C" if score >= 40 else "D"
    signal = "매수대기" if score >= 75 else "진입검토" if score >= 60 else "관망" if score >= 45 else "위험"
    return {
        "tf": "D", "score": score, "grade": grade, "signal": signal,
        "trend_score": trend_s, "pullback_score": pullback_s,
        "volume_score": volume_s, "candle_score": candle_s, "breakout_score": breakout_s,
        "pullback_pct": -pullback, "gap_pct": gap,
        "summary": f"변동률 {chg:+.1f}% · 눌림 {pullback:.1f}% · 고점대비 {p['high_ratio']:.0f}%",
    }


def score_60min(p: dict, m: dict) -> dict:
    """60분봉 건눌재 점수"""
    if not m:
        return {"tf": "60", "score": 0, "grade": "D", "signal": "데이터없음", "summary": "60분봉 조회 실패"}

    vwap_pct  = m.get("vwap_pct", 0)
    is_aligned = m.get("trend_aligned", False)
    is_above_vwap = m.get("is_above_vwap", False)
    vol_ratio = m.get("vol_ratio_min", 1)
    pullback  = abs(vwap_pct) if vwap_pct < 0 else 0

    trend_s   = 25 if is_aligned else 0
    align_s   = 20 if is_above_vwap else 0
    pullback_s = 25 if 2 <= pullback <= 7 else 10 if pullback < 2 else 0
    volume_s  = 15 if vol_ratio >= 1.5 else 8 if vol_ratio >= 1.0 else 3
    candle_s  = 10 if p["is_bullish"] else 0
    breakout_s = 5 if p["high_ratio"] >= 98 else 0

    score = trend_s + align_s + pullback_s + volume_s + candle_s + breakout_s
    grade = "S" if score >= 85 else "A" if score >= 70 else "B" if score >= 55 else "C" if score >= 40 else "D"
    signal = "매수대기" if score >= 75 else "진입검토" if score >= 60 else "관망" if score >= 45 else "위험"
    return {
        "tf": "60", "score": score, "grade": grade, "signal": signal,
        "trend_score": trend_s, "align_score": align_s,
        "pullback_score": pullback_s, "volume_score": volume_s,
        "candle_score": candle_s, "breakout_score": breakout_s,
        "pullback_pct": -pullback, "vwap_pct": vwap_pct,
        "is_above_vwap": is_above_vwap, "trend_aligned": is_aligned,
        "summary": f"{'정배열' if is_aligned else '역배열'} · VWAP {'위' if is_above_vwap else '아래'} · 거래량 {vol_ratio:.1f}배",
    }


def score_5min(p: dict, m: dict) -> dict:
    """5분봉 건눌재 점수"""
    if not m:
        return {"tf": "5", "score": 0, "grade": "D", "signal": "데이터없음", "summary": "5분봉 조회 실패"}

    is_above_vwap = m.get("is_above_vwap", False)
    is_above_ma5  = m.get("is_above_ma5", False)
    vwap_pct      = m.get("vwap_pct", 0)
    vol_ratio     = m.get("vol_ratio_min", 1)
    pullback      = p["pullback_pct"]

    vwap_s    = 30 if is_above_vwap else 0
    ma5_s     = 20 if is_above_ma5  else 0
    pullback_s = 25 if 0.5 <= pullback <= 3 else 10 if pullback < 0.5 else 0
    volume_s  = 15 if vol_ratio >= 1.5 else 8 if vol_ratio >= 1.0 else 3
    candle_s  = 5  if p["is_bullish"] else 0
    micro_s   = 5  if p["high_ratio"] >= 98 else 0

    score = vwap_s + ma5_s + pullback_s + volume_s + candle_s + micro_s
    grade = "S" if score >= 85 else "A" if score >= 70 else "B" if score >= 55 else "C" if score >= 40 else "D"
    signal = "매수대기" if score >= 75 else "진입검토" if score >= 60 else "관망" if score >= 45 else "위험"
    return {
        "tf": "5", "score": score, "grade": grade, "signal": signal,
        "vwap_score": vwap_s, "ma5_score": ma5_s,
        "pullback_score": pullback_s, "volume_score": volume_s,
        "candle_score": candle_s, "breakout_score": micro_s,
        "pullback_pct": -pullback, "vwap_pct": vwap_pct,
        "vol_ratio": vol_ratio,
        "is_above_vwap": is_above_vwap, "is_above_ma5": is_above_ma5,
        "vol_spike": vol_ratio >= 1.5,
        "summary": f"VWAP {'위' if is_above_vwap else '아래'} · 5선 {'위' if is_above_ma5 else '아래'} · 눌림 {pullback:.1f}%",
    }


# ── 패턴 분류 ─────────────────────────────────────────
def classify_pattern(p: dict) -> dict:
    gap = round((p["open"] - p["prev_close"]) / p["prev_close"] * 100, 2) if p["prev_close"] > 0 else 0
    if   gap >=  2.0: pat = "G"
    elif gap <= -2.0: pat = "GD"
    else:             pat = "A"
    return {"pattern": pat, "gap_pct": gap}


# ══ 엔드포인트 ══════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status": "ok", "version": "5.0.0", "mode": KIS_MODE}


@app.get("/stock/{code}")
async def get_stock(code: str):
    """현재가 + 패턴 (기존 호환 유지)"""
    now = time.time()
    cache_key = f"stock_{code}"
    if cache_key in _cache and now - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]
    try:
        token = await get_token()
        p     = await fetch_price(code, token)
        pat   = classify_pattern(p)
        result = {**p, **pat, "code": code, "status": "ok"}
        _cache[cache_key] = {"ts": now, "data": result}
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/score/{code}")
async def get_score(code: str, tf: str = "D"):
    """
    건눌재 점수 직접 반환
    tf: D(일봉) / 60(60분봉) / 5(5분봉)
    """
    now = time.time()
    cache_key = f"score_{code}_{tf}"
    if cache_key in _cache and now - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]
    try:
        token = await get_token()
        p     = await fetch_price(code, token)
        pat   = classify_pattern(p)

        if tf == "D":
            score_data = score_daily(p)
        elif tf == "60":
            m = await fetch_minute(code, token, "60")
            score_data = score_60min(p, m)
        elif tf == "5":
            m = await fetch_minute(code, token, "5")
            score_data = score_5min(p, m)
        else:
            raise HTTPException(status_code=400, detail="tf는 D/60/5 중 하나")

        result = {
            **p, **pat, **score_data,
            "code": code, "status": "ok",
        }
        _cache[cache_key] = {"ts": now, "data": result}
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/scan")
async def scan_multi(codes: str, tf: str = "D"):
    """
    복수 종목 일괄 스캔
    ?codes=005930,000660,042700&tf=5
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if len(code_list) > 10:
        raise HTTPException(status_code=400, detail="최대 10종목")
    try:
        token = await get_token()
        results = []
        for code in code_list:
            try:
                p   = await fetch_price(code, token)
                pat = classify_pattern(p)
                if tf == "D":
                    s = score_daily(p)
                elif tf == "60":
                    m = await fetch_minute(code, token, "60")
                    s = score_60min(p, m)
                else:
                    m = await fetch_minute(code, token, "5")
                    s = score_5min(p, m)
                results.append({**p, **pat, **s, "code": code, "status": "ok"})
                await asyncio.sleep(0.1)  # API 부하 방지
            except Exception as e:
                results.append({"code": code, "status": "error", "detail": str(e)})
        return {"status": "ok", "tf": tf, "count": len(results), "results": results}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

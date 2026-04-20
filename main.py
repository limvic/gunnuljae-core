"""
Trinity Core Engine v5.1
KIS API → 실데이터 + 건눌재 점수 + 다중 타임프레임
"""

import os, time, asyncio, httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(title="건눌재 Core Engine", version="5.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

KIS_APP_KEY    = os.environ.get("KIS_APP_KEY", "")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
KIS_MODE       = os.environ.get("KIS_MODE", "mock")

BASE_URL   = "https://openapivts.koreainvestment.com:29443" if KIS_MODE == "mock" else "https://openapi.koreainvestment.com:9443"
TOKEN_PATH = "/oauth2/tokenP"

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


async def _scan_logic(codes: str, tf: str = "D"):
    """공통 스캔 로직"""
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if len(code_list) > 10:
        raise HTTPException(status_code=400, detail="최대 10종목")
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
            await asyncio.sleep(0.3)  # KIS API 안정성 (채피 권장 0.3s)
        except Exception as e:
            results.append({"code": code, "status": "error", "detail": str(e)})
    return results


@app.get("/stocks")
async def stocks_multi(codes: str, tf: str = "D"):
    """
    채피 설계 — 복수 종목 일괄 스캔 (배열 직접 반환)
    ?codes=005930,000660,042700&tf=5
    응답: [{code, price, score, grade, signal, ...}, ...]
    """
    try:
        results = await _scan_logic(codes, tf)
        return results  # 배열 직접 반환 (채피 응답 구조)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/scan")
async def scan_multi(codes: str, tf: str = "D"):
    """
    기존 호환 유지 — 래퍼 형태로 반환
    ?codes=005930,000660,042700&tf=5
    """
    try:
        results = await _scan_logic(codes, tf)
        return {"status": "ok", "tf": tf, "count": len(results), "results": results}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
# ── 텔레그램 알람 ──────────────────────────────────────
_alerted: set = set()  # 중복 방지

async def send_telegram(token: str, chat_id: str, message: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

@app.get("/notify")
async def notify(codes: str, tf: str = "5",
                 tg_token: str = "", chat_id: str = "",
                 min_score: int = 85):
    """
    🔥 Trinity 텔레그램 알람
    index.html에서 주기적으로 호출
    """
    global _alerted
    if not tg_token or not chat_id:
        return {"sent": 0, "reason": "토큰/ChatID 없음"}

    results = await _scan_logic(codes, tf)
    sent = []

    for item in results:
        code  = item.get("code", "")
        score = item.get("score", 0)
        grade = item.get("grade", "")
        name  = item.get("name", code)
        signal = item.get("signal", "")

        alert_key = f"{code}_{grade}"

        if score >= min_score and alert_key not in _alerted:
            msg = (
                f"🔥 <b>Trinity 진입 신호</b>\n\n"
                f"종목: {name} ({code})\n"
                f"점수: {score}점 ({grade}급)\n"
                f"조건: {item.get('summary', '')}\n\n"
                f"👉 <a href='https://finance.naver.com/item/main.nhn?code={code}'>네이버 현재가 바로가기</a>"
            )
            await send_telegram(tg_token, chat_id, msg)
            _alerted.add(alert_key)
            sent.append(code)

    return {"sent": len(sent), "codes": sent}
@app.get("/")
async def root():
    """Trinity HTML 서빙"""
    import pathlib
    base = pathlib.Path(__file__).parent
    for fname in ["index.html", "Trinity-v1.0.html"]:
        fpath = base / fname
        if fpath.exists():
            return FileResponse(str(fpath))
    return {"status": "ok", "message": "Trinity API v5.1"}
# ── Trinity v1.1 자동주문 ──────────────────────────────
import json, hashlib, secrets
from datetime import datetime, date
from pathlib import Path

TG_TOKEN    = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID  = os.environ.get("TG_CHAT_ID", "")
KIS_ACCOUNT_NO = os.environ.get("KIS_ACCOUNT", "")
KIS_ACCOUNT_TYPE = os.environ.get("KIS_ACCOUNT_TYPE", "01")

ORDER_LOG_PATH = Path("/tmp/order_log.json")
MAX_DAILY_ORDERS = 3  # 1일 최대 주문 수
PRICE_GAP_LIMIT  = 1.0  # 신호가 대비 현재가 괴리 한도(%)
BUTTON_EXPIRE_SEC = 300  # 버튼 만료 5분

# 메모리 저장소
_pending_orders: dict = {}   # token → 주문 대기 정보
_daily_order_count: dict = {}  # date → count


# ── 유틸 ──────────────────────────────────────────────
def today_str() -> str:
    return date.today().isoformat()

def is_market_open() -> dict:
    """장 시간 체크 (KST 기준)"""
    now = datetime.now()
    weekday = now.weekday()  # 0=월 ~ 4=금
    hour, minute = now.hour, now.minute
    total_min = hour * 60 + minute

    if weekday >= 5:
        return {"open": False, "type": "휴장", "reason": "주말"}
    if 540 <= total_min < 930:   # 09:00~15:30
        return {"open": True,  "type": "정규장", "reason": ""}
    if 480 <= total_min < 540:   # 08:00~09:00
        return {"open": False, "type": "시간외", "reason": "장전 시간외"}
    if 930 <= total_min < 960:   # 15:30~16:00
        return {"open": False, "type": "시간외", "reason": "장후 시간외"}
    return {"open": False, "type": "휴장", "reason": "장외 시간"}

def get_daily_count() -> int:
    return _daily_order_count.get(today_str(), 0)

def increment_daily_count():
    key = today_str()
    _daily_order_count[key] = _daily_order_count.get(key, 0) + 1

def save_order_log(entry: dict):
    logs = []
    if ORDER_LOG_PATH.exists():
        try:
            logs = json.loads(ORDER_LOG_PATH.read_text())
        except:
            logs = []
    logs.append(entry)
    ORDER_LOG_PATH.write_text(json.dumps(logs, ensure_ascii=False, indent=2))

async def send_telegram_button(message: str, order_token: str):
    """확인 버튼 포함 텔레그램 메시지"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    callback_data = f"buy_{order_token}"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ 매수 확인", "callback_data": callback_data},
                {"text": "❌ 취소",      "callback_data": f"cancel_{order_token}"}
            ]]
        }
    }
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(url, json=payload)

async def send_telegram_simple(message: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(url, json={
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        })


# ── KIS 주문 ──────────────────────────────────────────
async def kis_order_market_buy(code: str, qty: int = 1) -> dict:
    """KIS mock 시장가 매수"""
    token = await get_token()
    tr_id = "VTTC0802U" if KIS_MODE == "mock" else "TTTC0802U"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }
    body = {
        "CANO": KIS_ACCOUNT_NO,
        "ACNT_PRDT_CD": KIS_ACCOUNT_TYPE,
        "PDNO": code,
        "ORD_DVSN": "01",   # 시장가
        "ORD_QTY": str(qty),
        "ORD_UNPR": "0",    # 시장가는 0
    }
    async with httpx.AsyncClient(timeout=10) as c:
        res = await c.post(
            f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
            headers=headers,
            json=body,
        )
    data = res.json()
    if data.get("rt_cd") != "0":
        raise Exception(data.get("msg1", "주문 실패"))
    return {
        "order_no": data.get("output", {}).get("ODNO", ""),
        "msg": data.get("msg1", ""),
    }

async def kis_get_balance() -> int:
    """매수 가능 금액 조회"""
    token = await get_token()
    tr_id = "VTTC8908R" if KIS_MODE == "mock" else "TTTC8908R"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }
    async with httpx.AsyncClient(timeout=10) as c:
        res = await c.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            params={
                "CANO": KIS_ACCOUNT_NO,
                "ACNT_PRDT_CD": KIS_ACCOUNT_TYPE,
                "PDNO": "005930",
                "ORD_UNPR": "0",
                "ORD_DVSN": "01",
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
            headers=headers,
        )
    data = res.json()
    if data.get("rt_cd") != "0":
        return 0
    return int(data.get("output", {}).get("ord_psbl_cash", 0))


# ══ 주문 엔드포인트 ════════════════════════════════════

@app.get("/signal")
async def signal_and_alert(code: str, score: int, signal_price: int, summary: str = ""):
    """
    Trinity 신호 발생 → 텔레그램 확인 버튼 전송
    index.html 또는 /notify에서 호출
    """
    if not TG_TOKEN or not TG_CHAT_ID:
        return {"ok": False, "reason": "TG 설정 없음"}

    # 장 시간 체크
    mkt = is_market_open()
    if not mkt["open"]:
        return {"ok": False, "reason": f"장외: {mkt['reason']}"}

    # 1일 한도
    if get_daily_count() >= MAX_DAILY_ORDERS:
        return {"ok": False, "reason": f"일일 한도 초과 ({MAX_DAILY_ORDERS}회)"}

    # 고유 토큰 생성
    order_token = secrets.token_hex(8)
    _pending_orders[order_token] = {
        "code": code,
        "score": score,
        "signal_price": signal_price,
        "summary": summary,
        "created_at": time.time(),
    }

    # 종목명 조회 시도
    try:
        token = await get_token()
        p = await fetch_price(code, token)
        name = p.get("name", code)
        cur_price = p.get("price", signal_price)
    except:
        name = code
        cur_price = signal_price

    msg = (
        f"🔥 <b>Trinity 진입 신호</b>\n\n"
        f"종목: <b>{name}</b> ({code})\n"
        f"점수: <b>{score}점</b>\n"
        f"신호가: {signal_price:,}원 | 현재가: {cur_price:,}원\n"
        f"조건: {summary}\n\n"
        f"⏰ <b>5분 내 확인하지 않으면 자동 만료</b>"
    )
    await send_telegram_button(msg, order_token)
    return {"ok": True, "token": order_token}


@app.post("/callback")
async def telegram_callback(update: dict):
    """
    텔레그램 버튼 콜백 처리
    → Webhook으로 수신 (별도 설정 필요)
    """
    try:
        cq = update.get("callback_query", {})
        callback_data = cq.get("data", "")
        callback_id   = cq.get("id", "")

        # 콜백 응답 (버튼 로딩 해제)
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery",
                json={"callback_query_id": callback_id}
            )

        if callback_data.startswith("cancel_"):
            order_token = callback_data.replace("cancel_", "")
            _pending_orders.pop(order_token, None)
            await send_telegram_simple("❌ 주문 취소되었습니다.")
            return {"ok": True}

        if not callback_data.startswith("buy_"):
            return {"ok": False}

        order_token = callback_data.replace("buy_", "")
        order_info  = _pending_orders.get(order_token)

        # ① 토큰 검증
        if not order_info:
            await send_telegram_simple("⚠️ 유효하지 않은 주문입니다. (이미 처리됨)")
            return {"ok": False, "reason": "토큰 없음"}

        # ② 만료 체크 (5분)
        if time.time() - order_info["created_at"] > BUTTON_EXPIRE_SEC:
            _pending_orders.pop(order_token, None)
            await send_telegram_simple("⏰ 주문 만료되었습니다. (5분 초과)")
            return {"ok": False, "reason": "만료"}

        # ③ 중복 주문 방지 (즉시 제거)
        _pending_orders.pop(order_token, None)

        code         = order_info["code"]
        signal_price = order_info["signal_price"]
        score        = order_info["score"]

        # ④ 1일 한도
        if get_daily_count() >= MAX_DAILY_ORDERS:
            await send_telegram_simple(f"🚫 오늘 주문 한도 초과 ({MAX_DAILY_ORDERS}회)")
            return {"ok": False, "reason": "한도 초과"}

        # ⑤ 장 시간
        mkt = is_market_open()
        if not mkt["open"]:
            await send_telegram_simple(f"🚫 장외 시간: {mkt['reason']}")
            return {"ok": False, "reason": "장외"}

        # ⑥ 잔고 확인
        token = await get_token()
        p = await fetch_price(code, token)
        cur_price = p.get("price", 0)
        balance   = await kis_get_balance()

        if balance < cur_price:
            await send_telegram_simple(
                f"💸 잔고 부족\n필요: {cur_price:,}원 | 가용: {balance:,}원"
            )
            return {"ok": False, "reason": "잔고 부족"}

        # ⑦ 가격 괴리 체크 (+1% 이탈 시 취소)
        if signal_price > 0:
            gap_pct = (cur_price - signal_price) / signal_price * 100
            if gap_pct > PRICE_GAP_LIMIT:
                await send_telegram_simple(
                    f"📈 가격 괴리 초과\n"
                    f"신호가: {signal_price:,}원 → 현재가: {cur_price:,}원 "
                    f"(+{gap_pct:.1f}%)\n기준: +{PRICE_GAP_LIMIT}%"
                )
                return {"ok": False, "reason": f"괴리 {gap_pct:.1f}%"}

        # ⑧ KIS 주문 실행
        click_time = datetime.now().isoformat()
        try:
            order_result = await kis_order_market_buy(code, qty=1)
            order_no = order_result.get("order_no", "")
            success  = True
            result_msg = f"✅ 체결 완료!\n주문번호: {order_no}"
        except Exception as e:
            success  = False
            order_no = ""
            result_msg = f"❌ 주문 실패: {str(e)}"

        # ⑨ 결과 텔레그램 회신
        name = p.get("name", code)
        final_msg = (
            f"{'✅' if success else '❌'} <b>Trinity 주문 결과</b>\n\n"
            f"종목: {name} ({code})\n"
            f"점수: {score}점\n"
            f"신호가: {signal_price:,}원\n"
            f"체결가: {cur_price:,}원\n"
            f"수량: 1주\n"
            f"{result_msg}\n"
            f"모드: {'모의' if KIS_MODE == 'mock' else '실전'}"
        )
        await send_telegram_simple(final_msg)
        increment_daily_count()

        # ⑩ 주문 로그 저장
        save_order_log({
            "date": today_str(),
            "click_time": click_time,
            "code": code,
            "name": name,
            "score": score,
            "signal_price": signal_price,
            "exec_price": cur_price,
            "order_no": order_no,
            "success": success,
            "mode": KIS_MODE,
            "summary": order_info.get("summary", ""),
        })

        return {"ok": success}

    except Exception as e:
        await send_telegram_simple(f"⚠️ 서버 오류: {str(e)}")
        return {"ok": False, "reason": str(e)}


@app.get("/order/logs")
async def get_order_logs():
    """주문 로그 조회"""
    if not ORDER_LOG_PATH.exists():
        return {"logs": [], "count": 0}
    try:
        logs = json.loads(ORDER_LOG_PATH.read_text())
        return {"logs": logs, "count": len(logs), "today": get_daily_count()}
    except:
        return {"logs": [], "count": 0}
# ── 거래량 TOP 30 ──────────────────────────────────────
@app.get("/top30")
async def get_top30():
    """
    KIS 거래량 순위 TOP 30
    TR: FHPST01710000
    """
    now = time.time()
    cache_key = "top30"
    if cache_key in _cache and now - _cache[cache_key]["ts"] < 60:  # 1분 캐시
        return _cache[cache_key]["data"]
    try:
        token = await get_token()
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "tr_id": "FHPST01710000",
            "custtype": "P",
        }
        async with httpx.AsyncClient(timeout=10) as c:
            res = await c.get(
                f"{BASE_URL}/uapi/domestic-stock/v1/ranking/volume",
                params={
                    "fid_cond_mrkt_div_code": "J",
                    "fid_cond_scr_div_code": "20171",
                    "fid_input_iscd": "0000",
                    "fid_div_cls_code": "0",
                    "fid_blng_cls_code": "0",
                    "fid_trgt_cls_code": "111111111",
                    "fid_trgt_exls_cls_code": "000000",
                    "fid_input_price_1": "0",
                    "fid_input_price_2": "0",
                    "fid_vol_cnt": "0",
                    "fid_input_date_1": "0",
                },
                headers=headers,
            )
        data = res.json()
        if data.get("rt_cd") != "0" or not data.get("output"):
            raise Exception(data.get("msg1", "TOP30 조회 실패"))

        result = []
        for item in data["output"][:30]:
            code = item.get("mksc_shrn_iscd", "")
            name = item.get("hts_kor_isnm", "")
            price = int(item.get("stck_prpr", 0))
            change_pct = float(item.get("prdy_ctrt", 0))
            volume = int(item.get("acml_vol", 0))
            high = int(item.get("stck_hgpr", 0))
            if code and name:
                result.append({
                    "rank": len(result) + 1,
                    "code": code,
                    "name": name,
                    "price": price,
                    "change_pct": change_pct,
                    "volume": volume,
                    "high": high,
                })

        _cache[cache_key] = {"ts": now, "data": {"status": "ok", "items": result}}
        return {"status": "ok", "items": result}

    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
from sangjeonjo import create_router
app.include_router(create_router(get_token, BASE_URL))

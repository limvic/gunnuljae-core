"""
Trinity Core Engine v5.1
KIS API → 실데이터 + 건눌재 점수 + 다중 타임프레임
"""

import os, time, asyncio, httpx
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI(title="건눌재 Core Engine", version="5.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

KIS_APP_KEY    = os.environ.get("KIS_APP_KEY", "")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
KIS_MODE       = os.environ.get("KIS_MODE", "mock")

# ── IGNITION Telegram Alert (Trinity v2.2) ──────────────────────────────────
TG_IGNITION_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_IGNITION_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE_URL   = "https://openapivts.koreainvestment.com:29443" if KIS_MODE == "mock" else "https://openapi.koreainvestment.com:9443"
TOKEN_PATH = "/oauth2/tokenP"

_token_cache = {"token": None, "expires_at": 0}
_cache: dict = {}
CACHE_TTL = 10


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
    vol_ratio  = float(o.get("vol_tnrt", 0))
    return {
        "price": price, "high": high, "low": low,
        "open": open_, "prev_close": prev_close,
        "volume": volume, "vol_ratio": vol_ratio,
        "name": o.get("hts_kor_isnm", code),
        "change_pct": float(o.get("prdy_ctrt", 0)),
        "change_amt": int(o.get("prdy_vrss", 0)),
        "is_bullish": price >= open_,
        "pullback_pct": round((high - price) / high * 100, 2) if high > 0 else 0,
        "high_ratio": round(price / high * 100, 1) if high > 0 else 100,
    }


def _estimate_minute_from_daily(p: dict) -> dict:
    """분봉 API 실패 시 일봉 데이터로 추정값 생성 (mock/장외 환경 대응)"""
    cur = p.get("price", 0)
    chg = p.get("change_pct", 0)
    gap = p.get("gap_pct", 0)
    vol = p.get("vol_ratio", 1.0)
    return {
        "cur_price": cur,
        "ma5":  round(cur * (0.998 if chg > 0 else 1.002)),
        "ma20": round(cur * (0.995 if chg > 0 else 1.005)),
        "vwap": round(cur * (1 - gap * 0.003 / 100)),
        "is_above_vwap": chg > 0,
        "is_above_ma5":  chg > 0,
        "vwap_pct":      round(chg * 0.3, 2),
        "vol_ratio_min": vol if vol > 0 else 1.0,
        "trend_aligned": chg > 0,
    }


async def fetch_minute(code: str, token: str, min_type: str = "60") -> dict:
    """min_type: '60' or '5'"""
    tr_id = "FHKST03010200"
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
        # mock 환경 또는 장외시간: 일봉 데이터로 추정값 반환
        try:
            p_fallback = await fetch_price(code, token)
            return _estimate_minute_from_daily(p_fallback)
        except Exception:
            return {}

    candles = data["output2"][:20]
    if not candles:
        try:
            p_fallback = await fetch_price(code, token)
            return _estimate_minute_from_daily(p_fallback)
        except Exception:
            return {}

    prices  = [int(c.get("stck_prpr", 0)) for c in candles if c.get("stck_prpr")]
    volumes = [int(c.get("cntg_vol", 0))  for c in candles if c.get("cntg_vol")]

    cur_price = prices[0]  if prices  else 0
    avg_vol   = sum(volumes) / len(volumes) if volumes else 1
    cur_vol   = volumes[0] if volumes else 0

    ma5  = sum(prices[:5])  / min(5,  len(prices))
    ma20 = sum(prices[:20]) / min(20, len(prices))

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


def score_daily(p: dict) -> dict:
    gap      = round((p["open"] - p["prev_close"]) / p["prev_close"] * 100, 2) if p["prev_close"] > 0 else 0
    pullback = p["pullback_pct"]
    chg      = p["change_pct"]

    trend_s    = 30 if chg > 0 and gap > -1 else 10 if chg > 0 else 0
    pullback_s = 25 if 5 <= pullback <= 12 else 15 if 2 <= pullback < 5 else 5 if pullback < 2 else 0
    volume_s   = 20 if p["vol_ratio"] > 1.5 else 10 if p["vol_ratio"] > 1.0 else 5
    candle_s   = 15 if p["is_bullish"] else 0
    breakout_s = 10 if p["high_ratio"] >= 98 else 5 if p["high_ratio"] >= 95 else 0

    score  = trend_s + pullback_s + volume_s + candle_s + breakout_s
    grade  = "S" if score >= 85 else "A" if score >= 70 else "B" if score >= 55 else "C" if score >= 40 else "D"
    signal = "매수대기" if score >= 75 else "진입검토" if score >= 60 else "관망" if score >= 45 else "위험"
    return {
        "tf": "D", "score": score, "grade": grade, "signal": signal,
        "trend_score": trend_s, "pullback_score": pullback_s,
        "volume_score": volume_s, "candle_score": candle_s, "breakout_score": breakout_s,
        "pullback_pct": -pullback, "gap_pct": gap,
        "summary": f"변동률 {chg:+.1f}% · 눌림 {pullback:.1f}% · 고점대비 {p['high_ratio']:.0f}%",
    }


def score_60min(p: dict, m: dict) -> dict:
    if not m or "cur_price" not in m:
        return {"tf": "60", "score": 0, "grade": "D", "signal": "데이터없음", "summary": "60분봉 조회 실패"}

    vwap_pct      = m.get("vwap_pct", 0)
    is_aligned    = m.get("trend_aligned", False)
    is_above_vwap = m.get("is_above_vwap", False)
    vol_ratio     = m.get("vol_ratio_min", 1)
    pullback      = abs(vwap_pct) if vwap_pct < 0 else 0

    trend_s    = 25 if is_aligned else 0
    align_s    = 20 if is_above_vwap else 0
    pullback_s = 25 if 2 <= pullback <= 7 else 10 if pullback < 2 else 0
    volume_s   = 15 if vol_ratio >= 1.5 else 8 if vol_ratio >= 1.0 else 3
    candle_s   = 10 if p["is_bullish"] else 0
    breakout_s =  5 if p["high_ratio"] >= 98 else 0

    score  = trend_s + align_s + pullback_s + volume_s + candle_s + breakout_s
    grade  = "S" if score >= 85 else "A" if score >= 70 else "B" if score >= 55 else "C" if score >= 40 else "D"
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
    if not m or "cur_price" not in m:
        return {"tf": "5", "score": 0, "grade": "D", "signal": "데이터없음", "summary": "5분봉 조회 실패"}

    is_above_vwap = m.get("is_above_vwap", False)
    is_above_ma5  = m.get("is_above_ma5", False)
    vwap_pct      = m.get("vwap_pct", 0)
    vol_ratio     = m.get("vol_ratio_min", 1)
    pullback      = p["pullback_pct"]

    vwap_s     = 30 if is_above_vwap else 0
    ma5_s      = 20 if is_above_ma5  else 0
    pullback_s = 25 if 0.5 <= pullback <= 3 else 10 if pullback < 0.5 else 0
    volume_s   = 15 if vol_ratio >= 1.5 else 8 if vol_ratio >= 1.0 else 3
    candle_s   =  5 if p["is_bullish"] else 0
    micro_s    =  5 if p["high_ratio"] >= 98 else 0

    score  = vwap_s + ma5_s + pullback_s + volume_s + candle_s + micro_s
    grade  = "S" if score >= 85 else "A" if score >= 70 else "B" if score >= 55 else "C" if score >= 40 else "D"
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


def classify_pattern(p: dict) -> dict:
    gap = round((p["open"] - p["prev_close"]) / p["prev_close"] * 100, 2) if p["prev_close"] > 0 else 0
    if   gap >=  2.0: pat = "G"
    elif gap <= -2.0: pat = "GD"
    else:             pat = "A"
    return {"pattern": pat, "gap_pct": gap}


@app.get("/health")
async def health():
    return {"status": "ok", "version": "5.1.0", "mode": KIS_MODE}


@app.get("/stock/{code}")
async def get_stock(code: str):
    now = time.time()
    cache_key = f"stock_{code}"
    if cache_key in _cache and now - _cache[cache_key]["ts"] < CACHE_TTL:
        return _cache[cache_key]["data"]
    try:
        token  = await get_token()
        p      = await fetch_price(code, token)
        pat    = classify_pattern(p)
        result = {**p, **pat, "code": code, "status": "ok"}
        _cache[cache_key] = {"ts": now, "data": result}
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/score/{code}")
async def get_score(code: str, tf: str = "D"):
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

        result = {**p, **pat, **score_data, "code": code, "status": "ok"}
        _cache[cache_key] = {"ts": now, "data": result}
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


async def _scan_logic(codes: str, tf: str = "D"):
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if len(code_list) > 10:
        raise HTTPException(status_code=400, detail="최대 10종목")
    token   = await get_token()
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
            await asyncio.sleep(0.3)
        except Exception as e:
            results.append({"code": code, "status": "error", "detail": str(e)})
    return results


@app.get("/stocks")
async def stocks_multi(codes: str, tf: str = "D"):
    try:
        return await _scan_logic(codes, tf)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/scan")
async def scan_multi(codes: str, tf: str = "D"):
    try:
        results = await _scan_logic(codes, tf)
        return {"status": "ok", "tf": tf, "count": len(results), "results": results}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

# ── 텔레그램 알람 ──────────────────────────────────────
_alerted: set = set()

async def send_telegram(token: str, chat_id: str, message: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(url, json={
            "chat_id": chat_id, "text": message,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        })

@app.get("/notify")
async def notify(codes: str, tf: str = "5",
                 tg_token: str = "", chat_id: str = "", min_score: int = 85):
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


# ── IGNITION → Telegram Alert (Trinity v2.2) ───────────────────────────────
# detect_ignition_event() 가 이미 5분 중복 방지를 하므로
# 여기서는 단순히 받아서 포맷 후 발송만 담당한다.

def _kst_now_str() -> str:
    """현재 KST 시각을 'YYYY-MM-DD HH:MM KST' 문자열로 반환"""
    from datetime import timezone, timedelta
    kst = timezone(timedelta(hours=9))
    return datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")


def _format_ignition_alert(ev: dict, item: dict) -> str:
    """채피 설계 메시지 포맷 그대로"""
    event_type = ev.get("event", "")
    name       = ev.get("name", ev.get("code", ""))
    code       = ev.get("code", "")
    score      = ev.get("score", 0)
    from_st    = ev.get("from", "-")
    to_st      = ev.get("to", "-")

    price      = item.get("price", 0)
    change_pct = item.get("change_pct", 0)
    volume     = item.get("volume", 0)
    vwap_above = item.get("vwapAbove", True)

    # 이벤트별 헤더/판단
    if event_type == "BREAK":
        header  = "🚨 IGNITION ALERT — BREAK"
        verdict = "장중 강한 돌파 감지.\n추격 금지, 눌림목 대기 구간."
    elif event_type == "CONFIRM":
        header  = "🔔 IGNITION ALERT — CONFIRM"
        verdict = "돌파 후 눌림 확인 구간.\n추격 금지, 첫 눌림만 관찰."
    else:  # READY
        header  = "👀 IGNITION ALERT — READY"
        verdict = "WATCH → READY 진입.\n스코어 흐름 모니터링."

    vol_str  = f"{volume:,}" if volume else "-"
    vwap_str = "위 ✅" if vwap_above else "아래 ⚠️"
    price_str = f"{price:,}원" if price else "-"
    pct_str  = f"{change_pct:+.2f}%" if change_pct else "-"

    return (
        f"<b>{header}</b>\n\n"
        f"종목: {name} ({code})\n"
        f"상태: {from_st} → <b>{to_st}</b>\n"
        f"점수: {score}\n"
        f"현재가: {price_str} ({pct_str})\n"
        f"거래량: {vol_str}\n"
        f"VWAP: {vwap_str}\n"
        f"시간: {_kst_now_str()}\n\n"
        f"판단:\n{verdict}\n\n"
        f"⚠️ 알림은 관찰 신호 — 실제 진입은 림빅 최종 판단"
    )


async def _send_ignition_telegram(ev: dict, item: dict):
    """IGNITION 이벤트 1건을 텔레그램으로 발송. 실패 시 로그만 남김."""
    if not TG_IGNITION_TOKEN or not TG_IGNITION_CHAT_ID:
        print("[IGNITION-TG] 환경변수 없음 — 발송 스킵")
        return
    try:
        msg = _format_ignition_alert(ev, item)
        url = f"https://api.telegram.org/bot{TG_IGNITION_TOKEN}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json={
                "chat_id": TG_IGNITION_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        if r.status_code == 200:
            print(f"[IGNITION-TG] ✅ 발송 완료: {ev.get('code')} {ev.get('event')}")
        else:
            print(f"[IGNITION-TG] ⚠️ 발송 실패 {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[IGNITION-TG] ❌ 예외: {e}")


@app.get("/ignition_notify_test")
async def ignition_notify_test():
    """
    텔레그램 연결 단독 테스트용.
    Railway 환경변수 세팅 확인 → 더미 CONFIRM 메시지 1건 발송.
    """
    if not TG_IGNITION_TOKEN or not TG_IGNITION_CHAT_ID:
        return {"ok": False, "reason": "TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 환경변수 없음"}
    dummy_ev   = {"event": "CONFIRM", "code": "TEST001", "name": "테스트종목",
                  "from": "WATCH", "to": "CONFIRM", "score": 87}
    dummy_item = {"price": 58600, "change_pct": 4.5, "volume": 1230000, "vwapAbove": True}
    await _send_ignition_telegram(dummy_ev, dummy_item)
    return {"ok": True, "message": "더미 CONFIRM 알림 발송 완료 — 텔레그램 확인하세요"}


@app.get("/")
async def root():
    import pathlib
    base = pathlib.Path(__file__).parent
    for fname in ["index.html", "Trinity-v1.0.html"]:
        fpath = base / fname
        if fpath.exists():
            return FileResponse(str(fpath))
    return {"status": "ok", "message": "Trinity API v5.1"}


# ── Trinity v1.1 자동주문 ──────────────────────────────
import json, hashlib, secrets
from datetime import datetime, date, timezone, timedelta
from pathlib import Path

TG_TOKEN         = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID       = os.environ.get("TG_CHAT_ID", "")
KIS_ACCOUNT_NO   = os.environ.get("KIS_ACCOUNT", "")
KIS_ACCOUNT_TYPE = os.environ.get("KIS_ACCOUNT_TYPE", "01")

ORDER_LOG_PATH   = Path("/tmp/order_log.json")
MAX_DAILY_ORDERS = 3
PRICE_GAP_LIMIT  = 1.0
BUTTON_EXPIRE_SEC = 300

_pending_orders: dict   = {}
_daily_order_count: dict = {}


def today_str() -> str:
    return date.today().isoformat()

def is_market_open() -> dict:
    now     = datetime.now()
    weekday = now.weekday()
    hour, minute = now.hour, now.minute
    total_min = hour * 60 + minute
    if weekday >= 5:
        return {"open": False, "type": "휴장", "reason": "주말"}
    if 540 <= total_min < 930:
        return {"open": True,  "type": "정규장", "reason": ""}
    if 480 <= total_min < 540:
        return {"open": False, "type": "시간외", "reason": "장전 시간외"}
    if 930 <= total_min < 960:
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
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [[
            {"text": "✅ 매수 확인", "callback_data": f"buy_{order_token}"},
            {"text": "❌ 취소",      "callback_data": f"cancel_{order_token}"}
        ]]}
    }
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(url, json=payload)

async def send_telegram_simple(message: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(url, json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"})


async def kis_order_market_buy(code: str, qty: int = 1) -> dict:
    token = await get_token()
    tr_id = "VTTC0802U" if KIS_MODE == "mock" else "TTTC0802U"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": tr_id, "custtype": "P",
    }
    body = {
        "CANO": KIS_ACCOUNT_NO, "ACNT_PRDT_CD": KIS_ACCOUNT_TYPE,
        "PDNO": code, "ORD_DVSN": "01", "ORD_QTY": str(qty), "ORD_UNPR": "0",
    }
    async with httpx.AsyncClient(timeout=10) as c:
        res = await c.post(
            f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
            headers=headers, json=body,
        )
    data = res.json()
    if data.get("rt_cd") != "0":
        raise Exception(data.get("msg1", "주문 실패"))
    return {"order_no": data.get("output", {}).get("ODNO", ""), "msg": data.get("msg1", "")}

async def kis_get_balance() -> int:
    token = await get_token()
    tr_id = "VTTC8908R" if KIS_MODE == "mock" else "TTTC8908R"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": tr_id, "custtype": "P",
    }
    async with httpx.AsyncClient(timeout=10) as c:
        res = await c.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            params={
                "CANO": KIS_ACCOUNT_NO, "ACNT_PRDT_CD": KIS_ACCOUNT_TYPE,
                "PDNO": "005930", "ORD_UNPR": "0", "ORD_DVSN": "01",
                "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N",
            },
            headers=headers,
        )
    data = res.json()
    if data.get("rt_cd") != "0":
        return 0
    return int(data.get("output", {}).get("ord_psbl_cash", 0))


@app.get("/signal")
async def signal_and_alert(code: str, score: int, signal_price: int, summary: str = ""):
    if not TG_TOKEN or not TG_CHAT_ID:
        return {"ok": False, "reason": "TG 설정 없음"}
    mkt = is_market_open()
    if not mkt["open"]:
        return {"ok": False, "reason": f"장외: {mkt['reason']}"}
    if get_daily_count() >= MAX_DAILY_ORDERS:
        return {"ok": False, "reason": f"일일 한도 초과 ({MAX_DAILY_ORDERS}회)"}
    order_token = secrets.token_hex(8)
    _pending_orders[order_token] = {
        "code": code, "score": score, "signal_price": signal_price,
        "summary": summary, "created_at": time.time(),
    }
    try:
        token = await get_token()
        p = await fetch_price(code, token)
        name      = p.get("name", code)
        cur_price = p.get("price", signal_price)
    except:
        name      = code
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
    try:
        cq            = update.get("callback_query", {})
        callback_data = cq.get("data", "")
        callback_id   = cq.get("id", "")
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery",
                json={"callback_query_id": callback_id}
            )
        if callback_data.startswith("cancel_"):
            _pending_orders.pop(callback_data.replace("cancel_", ""), None)
            await send_telegram_simple("❌ 주문 취소되었습니다.")
            return {"ok": True}
        if not callback_data.startswith("buy_"):
            return {"ok": False}
        order_token = callback_data.replace("buy_", "")
        order_info  = _pending_orders.get(order_token)
        if not order_info:
            await send_telegram_simple("⚠️ 유효하지 않은 주문입니다. (이미 처리됨)")
            return {"ok": False, "reason": "토큰 없음"}
        if time.time() - order_info["created_at"] > BUTTON_EXPIRE_SEC:
            _pending_orders.pop(order_token, None)
            await send_telegram_simple("⏰ 주문 만료되었습니다. (5분 초과)")
            return {"ok": False, "reason": "만료"}
        _pending_orders.pop(order_token, None)
        code         = order_info["code"]
        signal_price = order_info["signal_price"]
        score        = order_info["score"]
        if get_daily_count() >= MAX_DAILY_ORDERS:
            await send_telegram_simple(f"🚫 오늘 주문 한도 초과 ({MAX_DAILY_ORDERS}회)")
            return {"ok": False, "reason": "한도 초과"}
        mkt = is_market_open()
        if not mkt["open"]:
            await send_telegram_simple(f"🚫 장외 시간: {mkt['reason']}")
            return {"ok": False, "reason": "장외"}
        token     = await get_token()
        p         = await fetch_price(code, token)
        cur_price = p.get("price", 0)
        balance   = await kis_get_balance()
        if balance < cur_price:
            await send_telegram_simple(f"💸 잔고 부족\n필요: {cur_price:,}원 | 가용: {balance:,}원")
            return {"ok": False, "reason": "잔고 부족"}
        if signal_price > 0:
            gap_pct = (cur_price - signal_price) / signal_price * 100
            if gap_pct > PRICE_GAP_LIMIT:
                await send_telegram_simple(
                    f"📈 가격 괴리 초과\n신호가: {signal_price:,}원 → 현재가: {cur_price:,}원 "
                    f"(+{gap_pct:.1f}%)\n기준: +{PRICE_GAP_LIMIT}%"
                )
                return {"ok": False, "reason": f"괴리 {gap_pct:.1f}%"}
        click_time = datetime.now().isoformat()
        try:
            order_result = await kis_order_market_buy(code, qty=1)
            order_no = order_result.get("order_no", "")
            success  = True
            result_msg = f"✅ 체결 완료!\n주문번호: {order_no}"
        except Exception as e:
            success    = False
            order_no   = ""
            result_msg = f"❌ 주문 실패: {str(e)}"
        name = p.get("name", code)
        final_msg = (
            f"{'✅' if success else '❌'} <b>Trinity 주문 결과</b>\n\n"
            f"종목: {name} ({code})\n점수: {score}점\n"
            f"신호가: {signal_price:,}원\n체결가: {cur_price:,}원\n수량: 1주\n"
            f"{result_msg}\n모드: {'모의' if KIS_MODE == 'mock' else '실전'}"
        )
        await send_telegram_simple(final_msg)
        increment_daily_count()
        save_order_log({
            "date": today_str(), "click_time": click_time,
            "code": code, "name": name, "score": score,
            "signal_price": signal_price, "exec_price": cur_price,
            "order_no": order_no, "success": success,
            "mode": KIS_MODE, "summary": order_info.get("summary", ""),
        })
        return {"ok": success}
    except Exception as e:
        await send_telegram_simple(f"⚠️ 서버 오류: {str(e)}")
        return {"ok": False, "reason": str(e)}


@app.get("/order/logs")
async def get_order_logs():
    if not ORDER_LOG_PATH.exists():
        return {"logs": [], "count": 0}
    try:
        logs = json.loads(ORDER_LOG_PATH.read_text())
        return {"logs": logs, "count": len(logs), "today": get_daily_count()}
    except:
        return {"logs": [], "count": 0}


# ── 거래량 TOP 30 (KIS) ────────────────────────────────
@app.get("/top30")
async def get_top30():
    now = time.time()
    cache_key = "top30"
    if cache_key in _cache and now - _cache[cache_key]["ts"] < 60:
        return _cache[cache_key]["data"]
    try:
        token = await get_token()
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
            "tr_id": "FHPST01710000", "custtype": "P",
        }
        async with httpx.AsyncClient(timeout=10) as c:
            res = await c.get(
                f"{BASE_URL}/uapi/domestic-stock/v1/ranking/volume",
                params={
                    "fid_cond_mrkt_div_code": "J", "fid_cond_scr_div_code": "20171",
                    "fid_input_iscd": "0000", "fid_div_cls_code": "0",
                    "fid_blng_cls_code": "0", "fid_trgt_cls_code": "111111111",
                    "fid_trgt_exls_cls_code": "000000", "fid_input_price_1": "0",
                    "fid_input_price_2": "0", "fid_vol_cnt": "0", "fid_input_date_1": "0",
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
            if code and name:
                result.append({
                    "rank": len(result) + 1, "code": code, "name": name,
                    "price": int(item.get("stck_prpr", 0)),
                    "change_pct": float(item.get("prdy_ctrt", 0)),
                    "volume": int(item.get("acml_vol", 0)),
                    "high": int(item.get("stck_hgpr", 0)),
                })
        _cache[cache_key] = {"ts": now, "data": {"status": "ok", "items": result}}
        return {"status": "ok", "items": result}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


from sangjeonjo import create_router
app.include_router(create_router(get_token, BASE_URL))


# ── 🔱 Wave Target Engine v1.0 (채피 설계 × 써니 구현 × 림빅 운영) ──
# 익일/3일/7일 목표가 + 확률 + 무효가 + 재평가가
# 예: GET /target/000660?sector=strong&days=1,3,7
from target_engine import router as target_router
app.include_router(target_router)


# ── 거래량 TOP 30 v2 OLD (비활성화) ──────────────────────
import re as _re_top30

@app.get("/top30_v2_OLD")
async def get_top30_v2_old():
    return {"status": "deprecated", "message": "/top30_v2 를 사용하세요"}


# ── 거래량 TOP 30 v2 (BeautifulSoup) ─────────────────────
@app.get("/top30_v2")
async def get_top30_v3():
    """네이버 증권 거래량 순위 TOP 30 (코스피+코스닥)"""
    from bs4 import BeautifulSoup
    now = time.time()
    if "top30_v3" in _cache and now - _cache["top30_v3"]["ts"] < 60:
        return _cache["top30_v3"]["data"]
    try:
        all_items = []
        async with httpx.AsyncClient(timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Linux; Windows NT 10.0) AppleWebKit/537.36",
        }) as c:
            for mkt, sosok in [("kospi", "0"), ("kosdaq", "1")]:
                url = f"https://finance.naver.com/sise/sise_quant.naver?sosok={sosok}"
                res = await c.get(url)
                res.encoding = "euc-kr"
                soup  = BeautifulSoup(res.text, "html.parser")
                table = soup.find("table", class_="type_2")
                if not table:
                    continue
                for row in table.find_all("tr"):
                    tds  = row.find_all("td")
                    if len(tds) < 10:
                        continue
                    link = tds[1].find("a")
                    if not link:
                        continue
                    href       = link.get("href", "")
                    code_match = _re_top30.search(r"code=(\d{6})", href)
                    if not code_match:
                        continue
                    try:
                        name       = link.get_text(strip=True)
                        price      = int(tds[2].get_text(strip=True).replace(",", ""))
                        change_txt = tds[4].get_text(strip=True).replace("%", "").replace(",", "")
                        change_pct = float(change_txt)
                        volume     = int(tds[5].get_text(strip=True).replace(",", ""))
                        all_items.append({
                            "code": code_match.group(1), "name": name,
                            "price": price, "change_pct": change_pct,
                            "volume": volume, "high": 0, "market": mkt,
                        })
                    except:
                        continue
        all_items.sort(key=lambda x: x["volume"], reverse=True)
        result = [dict(item, rank=idx+1) for idx, item in enumerate(all_items[:30])]
        if not result:
            raise Exception(f"파싱 실패 - all_items={len(all_items)}")
        response = {"status": "ok", "items": result}
        _cache["top30_v3"] = {"ts": now, "data": response}
        return response
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"TOP30 실패: {str(e)}")
"""
╔══════════════════════════════════════════════════════════════════════╗
║  TRINITY STATE MACHINE  v1.0  —  Phase 1                            ║
║  채피(GPT) 설계  ×  써니(Claude) 구현  ×  림빅 최종결정              ║
║                                                                      ║
║  ⚡ 신경계 레이어 — 눈(👁)과 뇌(🧠) 사이의 상태 관리               ║
║                                                                      ║
║  원칙:                                                               ║
║  - 기존 Trinity 점수 로직 변경 금지                                  ║
║  - 기존 Candidate 점수 로직 변경 금지                                ║
║  - 이 파일은 "상태 관리 레이어"로만 동작                             ║
║                                                                      ║
║  상태 흐름:                                                          ║
║  IDLE → WATCH → ARMED → FIRE                                        ║
║                                                                      ║
║  핵심 원칙:                                                          ║
║  상태 없으면 트리거 무시                                              ║
║  트리거 없으면 진입 금지                                              ║
║                                                                      ║
║  📌 main.py 맨 아래에 통째로 붙여넣기                                ║
╚══════════════════════════════════════════════════════════════════════╝
"""

from collections import deque
from enum import Enum
from typing import Optional

# ═══════════════════════════════════════════════════════════════════════
# § 1.  상태 Enum 정의
# ═══════════════════════════════════════════════════════════════════════

class StockState(str, Enum):
    IDLE  = "IDLE"    # 기본 상태 / 관심 없음
    WATCH = "WATCH"   # 후보군 통과 / 차트 확인 필요
    ARMED = "ARMED"   # 진입 준비 완료 / 돌파 트리거만 기다리는 상태
    FIRE  = "FIRE"    # 돌파 트리거 발생 / 실제 진입 검토 상태


# ═══════════════════════════════════════════════════════════════════════
# § 2.  상태 저장소 (메모리 — 최대 200종목)
#        { code: StateEntry }
# ═══════════════════════════════════════════════════════════════════════

class StateEntry:
    def __init__(self, code: str, name: str):
        self.code       = code
        self.name       = name
        self.state      = StockState.IDLE
        self.prev_state = StockState.IDLE
        self.armed_at:  Optional[str] = None
        self.fire_at:   Optional[str] = None
        self.armed_reasons: list[str] = []
        self.fire_reasons:  list[str] = []
        self.updated_at: str = _now()

    def to_dict(self) -> dict:
        return {
            "code":          self.code,
            "name":          self.name,
            "state":         self.state.value,
            "prev_state":    self.prev_state.value,
            "armed_at":      self.armed_at,
            "fire_at":       self.fire_at,
            "armed_reasons": self.armed_reasons,
            "fire_reasons":  self.fire_reasons,
            "updated_at":    self.updated_at,
        }


# 전역 상태 저장소
_state_store: dict[str, StateEntry] = {}

# 상태 변경 로그 (최대 100건)
_state_log: deque = deque(maxlen=100)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get_or_create(code: str, name: str) -> StateEntry:
    if code not in _state_store:
        _state_store[code] = StateEntry(code, name)
    else:
        _state_store[code].name = name  # 이름 최신화
    return _state_store[code]


# ═══════════════════════════════════════════════════════════════════════
# § 3.  상태 전이 함수 — 점수 로직과 완전 분리
# ═══════════════════════════════════════════════════════════════════════

def _transition(entry: StateEntry, new_state: StockState, reasons: list[str]) -> bool:
    """
    상태 전이 실행. 변경이 있을 때만 True 반환 + 로그 기록.
    상위 상태로만 전이 (역행 없음, FIRE→IDLE 리셋 제외).
    """
    if entry.state == new_state:
        return False

    # 상태 순서: IDLE < WATCH < ARMED < FIRE
    order = {StockState.IDLE: 0, StockState.WATCH: 1,
             StockState.ARMED: 2, StockState.FIRE: 3}

    # 강제 리셋이 아니면 역행 금지
    if new_state != StockState.IDLE and order[new_state] < order[entry.state]:
        return False

    prev = entry.state
    entry.prev_state = prev
    entry.state      = new_state
    entry.updated_at = _now()

    if new_state == StockState.ARMED:
        entry.armed_at      = _now()
        entry.armed_reasons = reasons
    elif new_state == StockState.FIRE:
        entry.fire_at      = _now()
        entry.fire_reasons = reasons

    # 로그 기록
    log_entry = {
        "code":          entry.code,
        "name":          entry.name,
        "state_before":  prev.value,
        "state_after":   new_state.value,
        "reason":        reasons,
        "time":          _now(),
    }
    _state_log.append(log_entry)
    return True


# ═══════════════════════════════════════════════════════════════════════
# § 4.  WATCH 판정 — Candidate score 기반
#        Candidate Engine 점수를 받아 WATCH 여부 결정
# ═══════════════════════════════════════════════════════════════════════

def evaluate_watch(
    code: str,
    name: str,
    candidate_score: int,          # Candidate Engine 총점
    candidate_status: str,         # "돌파 직전" 등
) -> dict:
    """
    Candidate score >= 70 이면 WATCH 승격.
    (Candidate 필터 통과 = 이미 기준 충족)
    """
    entry = _get_or_create(code, name)
    reasons = []

    if candidate_score >= 70:
        reasons.append(f"Candidate {candidate_score}점 통과")
        if candidate_status in ("돌파 직전", "돌파 중"):
            reasons.append(f"상태: {candidate_status}")
        _transition(entry, StockState.WATCH, reasons)

    return entry.to_dict()


# ═══════════════════════════════════════════════════════════════════════
# § 5.  ARMED 판정 — Trinity 점수 기반 (채피 설계 그대로)
#        기존 Trinity 점수 로직에 손대지 않고
#        그 결과값만 받아서 상태 판정
# ═══════════════════════════════════════════════════════════════════════

def evaluate_armed(
    code: str,
    name: str,
    # Trinity 점수 (기존 calcKN / calcAN / finalDecision 결과를 그대로 받음)
    total_score: int,
    score_5m:    int,
    score_60m:   int,
    # 보조 조건
    price_above_vwap: bool,
    above_ma5:        bool,
    pullback_pct:     float,        # 당일 눌림 % (음수)
    # 우대 조건 (선택)
    candidate_score:  int  = 0,
    candidate_status: str  = "",
    volume_ratio:     float = 0.0,
    is_sector_leader: bool  = False,
) -> dict:
    """
    채피 설계 ARMED 조건 판정.
    Trinity 점수 결과를 받아서만 판단 — 점수 계산 자체는 건드리지 않음.

    [필수 조건]
    total_score >= 80
    score_5m    >= 85
    score_60m   >= 75
    price_above_vwap == True
    above_ma5 == True

    [우대 조건] (충족 시 reasons에 추가)
    candidate_score >= 90
    candidate_status == "돌파 직전"
    volume_ratio >= 1.5
    is_sector_leader == True
    """
    entry = _get_or_create(code, name)
    reasons = []
    bonus   = []

    # ── 필수 조건 체크 ──────────────────────────────────
    mandatory_ok = (
        total_score >= 80
        and score_5m    >= 85
        and score_60m   >= 75
        and price_above_vwap
        and above_ma5
    )

    if not mandatory_ok:
        # 필수 미충족 — 최소 WATCH는 유지
        if entry.state == StockState.IDLE:
            _transition(entry, StockState.WATCH, ["Trinity 평가 진행 중"])
        return {**entry.to_dict(), "armed": False, "missing": _armed_missing(
            total_score, score_5m, score_60m, price_above_vwap, above_ma5
        )}

    # ── 필수 조건 통과 ──────────────────────────────────
    reasons.append(f"5분 {score_5m}점")
    reasons.append(f"60분 {score_60m}점")
    if price_above_vwap:  reasons.append("VWAP 위")
    if above_ma5:         reasons.append("5선 위")
    if -4.0 <= pullback_pct <= -1.0:
        reasons.append(f"당일 눌림 {pullback_pct:.1f}% (적정)")

    # ── 우대 조건 ────────────────────────────────────────
    if candidate_score >= 90:         bonus.append(f"Candidate {candidate_score}pt")
    if candidate_status == "돌파 직전": bonus.append("돌파 직전")
    if volume_ratio >= 1.5:            bonus.append(f"거래량비 {volume_ratio:.1f}x")
    if is_sector_leader:               bonus.append("섹터 선도주")

    if bonus:
        reasons.append("우대: " + " · ".join(bonus))

    changed = _transition(entry, StockState.ARMED, reasons)
    return {**entry.to_dict(), "armed": True, "just_armed": changed}


def _armed_missing(total, s5m, s60m, vwap, ma5) -> list[str]:
    """ARMED 미충족 이유 반환."""
    m = []
    if total < 80:   m.append(f"총점 {total}/80 미달")
    if s5m   < 85:   m.append(f"5분 {s5m}/85 미달")
    if s60m  < 75:   m.append(f"60분 {s60m}/75 미달")
    if not vwap:     m.append("VWAP 하회")
    if not ma5:      m.append("5선 하회")
    return m


# ═══════════════════════════════════════════════════════════════════════
# § 6.  FIRE 트리거 — ARMED 종목에서만 발동
#        채피 핵심 원칙: "ARMED 아니면 FIRE 없음"
# ═══════════════════════════════════════════════════════════════════════

def evaluate_fire(
    code: str,
    name: str,
    # 돌파 레이더 입력값
    breakout_high:  bool,          # 직전 고점 돌파 여부
    volume_surge:   bool,          # 돌파 시 거래량 증가
    # 공격형/보수형 토글
    aggressive: bool = True,       # True=돌파 순간 FIRE / False=1봉 종가 확인 후 FIRE
    # 무효 조건
    price_above_vwap: bool = True, # VWAP 하회 시 FIRE 무효
) -> dict:
    """
    채피 설계 FIRE 조건.

    ⚡ 핵심: ARMED 상태가 아니면 절대 FIRE 불가.
    상태 없으면 트리거 무시.

    aggressive=True  → 돌파 순간 FIRE (공격형)
    aggressive=False → 1봉 종가 확인 후 FIRE (보수형)
    """
    entry = _get_or_create(code, name)

    # ── ARMED 아니면 완전 무시 ──────────────────────────
    if entry.state != StockState.ARMED:
        return {
            **entry.to_dict(),
            "fired": False,
            "blocked_reason": f"ARMED 아님 (현재: {entry.state.value})",
        }

    # ── VWAP 하회 즉시 무효 ─────────────────────────────
    if not price_above_vwap:
        return {
            **entry.to_dict(),
            "fired": False,
            "blocked_reason": "VWAP 하회 — FIRE 무효",
        }

    # ── 돌파 조건 체크 ───────────────────────────────────
    reasons = []

    if aggressive:
        # 공격형: 돌파 + 거래량 동시 확인
        if breakout_high:   reasons.append("전고 돌파")
        if volume_surge:    reasons.append("거래량 급증")
        fire_ok = breakout_high and volume_surge
    else:
        # 보수형: 돌파 확인 (1봉 종가 확인은 호출 시점에 이미 처리된 것으로 간주)
        if breakout_high:   reasons.append("전고 돌파 (종가 확인)")
        if volume_surge:    reasons.append("거래량 급증")
        fire_ok = breakout_high

    if not fire_ok:
        return {
            **entry.to_dict(),
            "fired": False,
            "blocked_reason": "돌파 조건 미충족",
        }

    reasons.append("공격형" if aggressive else "보수형")
    changed = _transition(entry, StockState.FIRE, reasons)

    return {
        **entry.to_dict(),
        "fired": True,
        "just_fired": changed,
        "mode": "공격형" if aggressive else "보수형",
    }


# ═══════════════════════════════════════════════════════════════════════
# § 7.  상태 리셋
# ═══════════════════════════════════════════════════════════════════════

def reset_state(code: str, name: str = "") -> dict:
    """종목 상태를 IDLE로 리셋 (장 마감 후 / 수동 리셋)."""
    if code in _state_store:
        entry = _state_store[code]
        _transition(entry, StockState.IDLE, ["수동 리셋"])
        entry.armed_at = None
        entry.fire_at  = None
        entry.armed_reasons = []
        entry.fire_reasons  = []
        return entry.to_dict()
    return {"code": code, "state": "IDLE", "message": "새 항목"}


# ═══════════════════════════════════════════════════════════════════════
# § 8.  조회 헬퍼
# ═══════════════════════════════════════════════════════════════════════

def get_state(code: str) -> Optional[dict]:
    if code in _state_store:
        return _state_store[code].to_dict()
    return None

def get_all_states() -> list[dict]:
    return [e.to_dict() for e in _state_store.values()]

def get_armed_list() -> list[dict]:
    return [e.to_dict() for e in _state_store.values()
            if e.state == StockState.ARMED]

def get_fire_list() -> list[dict]:
    return [e.to_dict() for e in _state_store.values()
            if e.state == StockState.FIRE]


# ═══════════════════════════════════════════════════════════════════════
# § 9.  FastAPI 엔드포인트
# ═══════════════════════════════════════════════════════════════════════


# ── 9-1. 상태 전체 조회 ──────────────────────────────────────────────
@app.get("/state")
async def api_get_all_states():
    """
    전체 종목 상태 조회.
    Response: { states: [...], armed_count: N, fire_count: N }
    """
    states = get_all_states()
    return {
        "total":       len(states),
        "armed_count": sum(1 for s in states if s["state"] == "ARMED"),
        "fire_count":  sum(1 for s in states if s["state"] == "FIRE"),
        "states":      states,
    }


# ── 9-2. ARMED 목록 ──────────────────────────────────────────────────
@app.get("/state/armed")
async def api_get_armed():
    """
    ARMED 상태 종목 목록.
    돌파 트리거 대기 중인 종목들.
    """
    armed = get_armed_list()
    return {
        "count":  len(armed),
        "armed":  armed,
        "note":   "이 종목들에서만 FIRE 트리거 발동 가능",
    }


# ── 9-3. FIRE 목록 ───────────────────────────────────────────────────
@app.get("/state/fire")
async def api_get_fire():
    """
    FIRE 상태 종목 목록.
    진입 검토 대상.
    """
    fire = get_fire_list()
    return {
        "count": len(fire),
        "fire":  fire,
        "note":  "진입 검토 구간 — 림빅 최종 결정",
    }


# ── 9-4. ARMED 평가 요청 ─────────────────────────────────────────────
@app.post("/state/evaluate/armed")
async def api_evaluate_armed(payload: dict = Body(...)):
    """
    Trinity 점수 결과를 받아 ARMED 판정.
    기존 점수 로직과 완전 분리 — 결과값만 받음.

    Body:
    {
      "code": "336260",
      "name": "이수페타시스",
      "total_score": 95,
      "score_5m": 95,
      "score_60m": 80,
      "price_above_vwap": true,
      "above_ma5": true,
      "pullback_pct": -2.6,
      "candidate_score": 100,
      "candidate_status": "돌파 직전",
      "volume_ratio": 2.0,
      "is_sector_leader": true
    }
    """
    result = evaluate_armed(
        code              = payload["code"],
        name              = payload.get("name", ""),
        total_score       = payload.get("total_score", 0),
        score_5m          = payload.get("score_5m", 0),
        score_60m         = payload.get("score_60m", 0),
        price_above_vwap  = payload.get("price_above_vwap", False),
        above_ma5         = payload.get("above_ma5", False),
        pullback_pct      = payload.get("pullback_pct", 0.0),
        candidate_score   = payload.get("candidate_score", 0),
        candidate_status  = payload.get("candidate_status", ""),
        volume_ratio      = payload.get("volume_ratio", 0.0),
        is_sector_leader  = payload.get("is_sector_leader", False),
    )
    return result


# ── 9-5. FIRE 트리거 요청 ────────────────────────────────────────────
@app.post("/state/evaluate/fire")
async def api_evaluate_fire(payload: dict = Body(...)):
    """
    돌파 레이더 결과를 받아 FIRE 판정.
    ARMED 상태 종목에서만 발동.

    Body:
    {
      "code": "336260",
      "name": "이수페타시스",
      "breakout_high": true,
      "volume_surge": true,
      "aggressive": true,
      "price_above_vwap": true
    }
    """
    result = evaluate_fire(
        code             = payload["code"],
        name             = payload.get("name", ""),
        breakout_high    = payload.get("breakout_high", False),
        volume_surge     = payload.get("volume_surge", False),
        aggressive       = payload.get("aggressive", True),
        price_above_vwap = payload.get("price_above_vwap", True),
    )
    return result


# ── 9-6. WATCH 평가 요청 ─────────────────────────────────────────────
@app.post("/state/evaluate/watch")
async def api_evaluate_watch(payload: dict = Body(...)):
    """
    Candidate 결과를 받아 WATCH 판정.

    Body:
    {
      "code": "336260",
      "name": "이수페타시스",
      "candidate_score": 100,
      "candidate_status": "돌파 직전"
    }
    """
    result = evaluate_watch(
        code             = payload["code"],
        name             = payload.get("name", ""),
        candidate_score  = payload.get("candidate_score", 0),
        candidate_status = payload.get("candidate_status", ""),
    )
    return result


# ── 9-7. 단일 종목 상태 조회 ─────────────────────────────────────────
@app.get("/state/{code}")
async def api_get_state(code: str):
    """단일 종목 상태 조회."""
    s = get_state(code)
    if s is None:
        return {"code": code, "state": "IDLE", "message": "등록된 상태 없음"}
    return s


# ── 9-8. 상태 리셋 ───────────────────────────────────────────────────
@app.post("/state/{code}/reset")
async def api_reset_state(code: str, payload: dict = Body(default={})):
    """종목 상태 IDLE 리셋."""
    name = payload.get("name", "")
    return reset_state(code, name)


# ── 9-9. 상태 변경 로그 ──────────────────────────────────────────────
@app.get("/state/log/all")
async def api_state_log(last: int = 20):
    """
    상태 변경 로그 조회.
    채피 설계 로그 포맷 그대로 반환.

    Query: last=N (최근 N건, 기본 20)
    """
    last = max(1, min(last, 100))
    logs = list(_state_log)[-last:]
    return {
        "count": len(logs),
        "logs":  list(reversed(logs)),   # 최신 순
    }

# — 9-10. 지수 스냅샷 -------------------------------------------------
@app.get("/index_snapshot")
async def index_snapshot():
    """
    실시간 지수 스냅샷 (v2 — 안정성 강화)
    - KOSPI / KOSDAQ : Naver 모바일 시세 페이지 (정규식 파싱)
    - NASDAQ / S&P500 : Yahoo Finance chart API
    캐시 TTL: 60초
    """
    import re
    from bs4 import BeautifulSoup

    cache_key = "index_snapshot"
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]["ts"] < 60:
        return _cache[cache_key]["data"]

    UA = "Mozilla/5.0 (Linux; Windows NT 10.0) AppleWebKit/537.36"

    result = {
        "ts": int(now),
        "kospi":  None,
        "kosdaq": None,
        "nasdaq": None,
        "sp500":  None,
        "_debug": {}
    }

    # ── 1. KOSPI / KOSDAQ : Naver 금융 sise_index 페이지
    async def fetch_naver_index(code, label):
        """code: KOSPI / KOSDAQ"""
        try:
            url = f"https://finance.naver.com/sise/sise_index.naver?code={code}"
            async with httpx.AsyncClient(timeout=8, headers={"User-Agent": UA}) as c:
                res = await c.get(url)
            res.encoding = "euc-kr"
            soup = BeautifulSoup(res.text, "html.parser")

            # 현재 지수
            now_val_el = soup.select_one("#now_value")
            if not now_val_el:
                result["_debug"][label] = "now_value not found"
                return None
            price = float(now_val_el.text.strip().replace(",", ""))

            # 전일 대비 + 등락률 (#change_value_and_rate 안에 텍스트로 들어있음)
            chg_box = soup.select_one("#change_value_and_rate")
            change = None
            change_pct = None
            if chg_box:
                txt = chg_box.get_text(" ", strip=True)
                # 예: "8.72 +0.32%" 또는 "2.15 -0.25%" 또는 "상승 8.72 +0.32%"
                nums = re.findall(r"[-+]?\d+\.?\d*", txt)
                if len(nums) >= 2:
                    change = float(nums[0])
                    change_pct = float(nums[1])
                # 부호 보정: "하락" 또는 "▼" 포함 시 음수
                if any(s in txt for s in ["하락", "▼"]):
                    change = -abs(change) if change is not None else None
                    change_pct = -abs(change_pct) if change_pct is not None else None
                elif any(s in txt for s in ["상승", "▲"]):
                    change = abs(change) if change is not None else None
                    change_pct = abs(change_pct) if change_pct is not None else None

            return {"price": price, "change": change, "change_pct": change_pct}
        except Exception as e:
            result["_debug"][label] = f"err: {type(e).__name__}: {str(e)[:80]}"
            return None

    result["kospi"]  = await fetch_naver_index("KOSPI",  "kospi")
    result["kosdaq"] = await fetch_naver_index("KOSDAQ", "kosdaq")

    # ── 2. NASDAQ / S&P500 : Yahoo Finance chart API
    async def fetch_yahoo(symbol, label):
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            async with httpx.AsyncClient(timeout=8, headers={
                "User-Agent": UA,
                "Accept": "application/json",
            }) as c:
                res = await c.get(url, params={"interval": "1d", "range": "5d"})
            data = res.json()
            chart = (data.get("chart") or {}).get("result") or []
            if not chart:
                result["_debug"][label] = "no chart result"
                return None
            meta = chart[0].get("meta", {})
            price = meta.get("regularMarketPrice")
            prev  = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price is None or prev is None:
                result["_debug"][label] = f"no price (price={price}, prev={prev})"
                return None
            change = round(price - prev, 2)
            change_pct = round((price - prev) / prev * 100, 2) if prev else None
            return {
                "price": round(float(price), 2),
                "change": change,
                "change_pct": change_pct,
            }
        except Exception as e:
            result["_debug"][label] = f"err: {type(e).__name__}: {str(e)[:80]}"
            return None

    result["nasdaq"] = await fetch_yahoo("%5EIXIC", "nasdaq")
    result["sp500"]  = await fetch_yahoo("%5EGSPC", "sp500")

    _cache[cache_key] = {"ts": now, "data": result}
    return result
# — 9-11. IGNITION 실데이터 스캔 ---------------------------------

def calc_ignition_score(item):
    price = float(item.get("price") or 0)
    change_pct = float(item.get("change_pct") or 0)
    volume = int(item.get("volume") or 0)

    score = 0

    # 거래량 에너지
    if volume >= 100_000_000:
        score += 40
    elif volume >= 50_000_000:
        score += 30
    elif volume >= 20_000_000:
        score += 20
    elif volume >= 10_000_000:
        score += 10

    # 상승률 에너지
    if 3 <= change_pct <= 12:
        score += 35
    elif 1 <= change_pct < 3:
        score += 20
    elif 12 < change_pct <= 20:
        score += 15

    # 과열 제외 보정
    if change_pct > 20:
        score -= 20

    # 현재가 유효성
    if price > 0:
        score += 10

    # 거래량 순위 보너스
    rank = int(item.get("rank") or 99)
    if rank <= 10:
        score += 15
    elif rank <= 20:
        score += 10
    elif rank <= 30:
        score += 5

    return max(0, min(score, 100))


def ignition_status(score):
    if score >= 80:
        return "BREAK"
    if score >= 60:
        return "READY"
    if score >= 40:
        return "WATCH"
    return "IGNORE"


# — 9-12. 공용 필터: ETF / ETN 제외 ---------------------------------

ETF_PREFIXES = [
    "KODEX", "TIGER", "KBSTAR", "ARIRANG", "ACE", "HANARO",
    "SOL", "KOSEF", "KINDEX", "SMART", "히어로즈", "WOORI", "RISE"
]

ETF_KEYWORDS = [
    "ETF", "ETN", "인버스", "레버리지", "선물", "TR", "합성"
]


def is_etf_etn(name: str) -> bool:
    if not name:
        return False

    upper = str(name).upper().strip()

    if any(upper.startswith(prefix) for prefix in ETF_PREFIXES):
        return True

    if any(keyword.upper() in upper for keyword in ETF_KEYWORDS):
        return True

    return False


def filter_tradeable_stocks(items, exclude_etf: bool = True):
    if not exclude_etf:
        return items

    return [
        item for item in items
        if not is_etf_etn(item.get("name", ""))
    ]


# — 9-14. IGNITION 이벤트 상태 추적 (Trinity v2.1) ---------------------------------
# 채피 설계, 써니 통합 (2026.04.29)
# 상태 변화를 감지해서 events 배열로 반환 → 프론트가 토스트 알림으로 표시

_ignition_last_states = {}    # 종목별 직전 상태 (in-memory)
_ignition_last_scores = {}    # 종목별 직전 점수 (in-memory)
_ignition_last_event_ts = {}  # 같은 이벤트 5분 중복 방지용 타임스탬프


def detect_ignition_event(item, new_status, new_score):
    """
    종목의 상태/점수 변화를 감지해 이벤트 객체를 반환.
    감지 대상:
      - WATCH → READY    (READY 이벤트)
      - * → BREAK        (BREAK 이벤트, BREAK가 아니었다가 BREAK가 된 경우)
      - 점수 <85 → ≥85   (CONFIRM 이벤트)

    가드:
      - 첫 관측(old_status=None)은 알림 없음 (재시작 직후 폭발 방지)
      - 같은 (code, from→to) 이벤트는 5분 내 중복 차단
    """
    code = item.get("code")
    name = item.get("name", code)

    if not code:
        return None

    old_status = _ignition_last_states.get(code)
    old_score = _ignition_last_scores.get(code, 0)

    # 새 상태/점수 저장 (다음 호출 비교용)
    _ignition_last_states[code] = new_status
    _ignition_last_scores[code] = new_score

    # 첫 관측은 알림 폭발 방지
    if old_status is None:
        return None

    # 이벤트 타입 판별
    event_type = None

    if old_status != "BREAK" and new_status == "BREAK":
        event_type = "BREAK"
    elif old_status == "WATCH" and new_status == "READY":
        event_type = "READY"
    elif old_score < 85 and new_score >= 85:
        event_type = "CONFIRM"

    if not event_type:
        return None

    # 5분 중복 방지
    now = time.time()
    key = f"{code}_{old_status}_{new_status}_{event_type}"
    if key in _ignition_last_event_ts and now - _ignition_last_event_ts[key] < 300:
        return None
    _ignition_last_event_ts[key] = now

    return {
        "event": event_type,
        "code": code,
        "name": name,
        "from": old_status,
        "to": new_status,
        "score": new_score,
        "timestamp": int(now)
    }


# — 9-15. IGNITION 실데이터 스캔 (Trinity v2.1 — events 추적 포함) -----------------

@app.get("/ignition_scan")
async def ignition_scan(exclude_etf: bool = True):
    """
    TOP30_v2 실데이터 기반 IGNITION 스캔.
    기본값: ETF/ETN 제외.
    사용 예:
    /ignition_scan
    /ignition_scan?exclude_etf=true
    /ignition_scan?exclude_etf=false

    Trinity v2.1: 응답에 events 배열 추가 — 상태 변화 감지 결과.
    첫 호출 시 events는 빈 배열 (정상). 이후 호출부터 변화 감지.
    """
    try:
        data = await get_top30_v3()
    except HTTPException as e:
        return {
            "status": "error",
            "message": f"top30_v2 호출 실패: {e.detail}",
            "exclude_etf": exclude_etf,
            "count": 0, "watch": 0, "ready": 0, "break": 0,
            "events": [],
            "items": []
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"예외: {str(e)}",
            "exclude_etf": exclude_etf,
            "count": 0, "watch": 0, "ready": 0, "break": 0,
            "events": [],
            "items": []
        }

    raw_items = data.get("items", [])
    items = filter_tradeable_stocks(raw_items, exclude_etf=exclude_etf)

    results = []
    events = []

    for item in items:
        score = calc_ignition_score(item)
        status = ignition_status(score)

        if status == "IGNORE":
            continue

        result_item = {
            "code": item.get("code"),
            "name": item.get("name"),
            "price": item.get("price"),
            "change_pct": item.get("change_pct"),
            "volume": item.get("volume"),
            "market": item.get("market"),
            "rank": item.get("rank"),
            "score": score,
            "status": status,
            "source": "top30_v2",
            "exclude_etf": exclude_etf
        }

        results.append(result_item)

        # Trinity v2.2 — 상태 변화 감지 + 텔레그램 자동 발송
        ev = detect_ignition_event(result_item, status, score)
        if ev:
            events.append(ev)
            await _send_ignition_telegram(ev, result_item)

    results = sorted(results, key=lambda x: x["score"], reverse=True)

    return {
        "status": "ok",
        "exclude_etf": exclude_etf,
        "raw_count": len(raw_items),
        "filtered_count": len(items),
        "count": len(results),
        "watch": len([x for x in results if x["status"] == "WATCH"]),
        "ready": len([x for x in results if x["status"] == "READY"]),
        "break": len([x for x in results if x["status"] == "BREAK"]),
        "events": events,
        "items": results
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Trinity v2.2 — 백엔드 자동 스캔 스케줄러
# 프론트 없이 Railway가 스스로 ignition_scan 실행 → Telegram 자동 발송
# 설계: 채피 🔮  |  구현: 써니 ☀️  |  2026-05-02
# ═══════════════════════════════════════════════════════════════════════════════

KST = timezone(timedelta(hours=9))
SCAN_INTERVAL_SEC = 90   # 스캔 주기 (초) — 필요 시 조정

# ── 스케줄러 ON/OFF 스위치 ──────────────────────────────────────────────────
_scheduler_enabled: bool = True   # 기본값 ON

@app.get("/scheduler/status")
async def scheduler_status():
    return {
        "enabled": _scheduler_enabled,
        "message": "🟢 자동 스캔 ON" if _scheduler_enabled else "🔴 자동 스캔 OFF"
    }

@app.get("/scheduler/pause")
async def scheduler_pause():
    global _scheduler_enabled
    _scheduler_enabled = False
    print("[AUTO-SCAN] 🔴 스케줄러 일시정지 (수동)")
    return {"enabled": False, "message": "🔴 자동 스캔 OFF"}

@app.get("/scheduler/resume")
async def scheduler_resume():
    global _scheduler_enabled
    _scheduler_enabled = True
    print("[AUTO-SCAN] 🟢 스케줄러 재개 (수동)")
    return {"enabled": True, "message": "🟢 자동 스캔 ON"}


def _scheduler_market_open() -> bool:
    """자동 스캔 스케줄러용 — 기존 is_market_open() 래핑"""
    return is_market_open().get("open", False)


async def _run_auto_scan():
    """
    ignition_scan 내부 로직을 직접 호출 (HTTP 없이 함수 호출).
    BREAK / CONFIRM 이벤트 발생 시 텔레그램 자동 발송.
    """
    try:
        data = await get_top30_v3()
    except Exception as e:
        print(f"[AUTO-SCAN] ❌ top30_v3 호출 실패: {e}")
        return

    raw_items = data.get("items", [])
    items = filter_tradeable_stocks(raw_items, exclude_etf=True)

    fired = 0
    for item in items:
        score  = calc_ignition_score(item)
        status = ignition_status(score)

        if status == "IGNORE":
            continue

        result_item = {
            "code":       item.get("code"),
            "name":       item.get("name"),
            "price":      item.get("price"),
            "change_pct": item.get("change_pct"),
            "volume":     item.get("volume"),
            "market":     item.get("market"),
            "rank":       item.get("rank"),
            "score":      score,
            "status":     status,
            "vwapAbove":  item.get("vwapAbove", True),
            "source":     "auto_scheduler",
            "exclude_etf": True,
        }

        ev = detect_ignition_event(result_item, status, score)
        if ev:
            await _send_ignition_telegram(ev, result_item)
            fired += 1

    now_str = datetime.now(KST).strftime("%H:%M:%S")
    print(f"[AUTO-SCAN] ✅ {now_str} | 종목 {len(items)}개 스캔 | 알림 {fired}건")


async def scan_loop():
    """
    백그라운드 루프.
    - 장 중: SCAN_INTERVAL_SEC 마다 스캔
    - 장 외: 60초 sleep 후 재확인 (로그 최소화)
    """
    print("[AUTO-SCAN] 🚀 스케줄러 시작 — Trinity v2.2")
    await asyncio.sleep(10)   # 서버 완전 기동 대기

    while True:
        try:
            if not _scheduler_enabled:
                await asyncio.sleep(30)
                continue
            if _scheduler_market_open():
                await _run_auto_scan()
                await asyncio.sleep(SCAN_INTERVAL_SEC)
            else:
                now_str = datetime.now(KST).strftime("%H:%M")
                print(f"[AUTO-SCAN] 💤 장외 대기 중 ({now_str}) — 60초 후 재확인")
                await asyncio.sleep(60)
        except Exception as e:
            print(f"[AUTO-SCAN] ⚠️ 루프 예외 (유지됨): {e}")
            await asyncio.sleep(30)


@app.on_event("startup")
async def start_scheduler():
    """FastAPI 시작 시 자동 스캔 루프 백그라운드 실행"""
    asyncio.create_task(scan_loop())

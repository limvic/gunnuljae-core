# redeploy 2026.06.11 — Day 2 MRI
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
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"])

# 📒 VAVLOG v1.0 — 매매 복기 로그 (독립 모듈). 실패해도 본체 무중단.
try:
    from vavlog import router as vavlog_router
    app.include_router(vavlog_router)
except Exception as _vav_e:
    print(f"[vavlog] 라우터 로드 실패 — 스킵(본체는 정상): {_vav_e}")

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


# ══════════════════════════════════════════════════════════════════════════════
# 🔱 Trinity Filter Mode v1.0
#    채피(GPT) 설계  ×  써니(Claude) 구현  ×  림빅 최종결정
#
#    원칙:
#    1. 기존 /scan, /stocks 기본 동작 절대 변경 금지 (기본값 전부 false)
#    2. v1 = 고정 Universe set 기반 (실시간 API 연동 v2 이후)
#    3. 수급모드 = 데이터 없으면 unknown 통과 (탈락 금지)
#    4. 탈락 사유 로그에 항상 기록
# ══════════════════════════════════════════════════════════════════════════════

# ── 시총 Universe (국장 시총 BIG 55 실제 기준) ──────────────────────────────
# 출처: 국장 시총 BIG 55 인포그래픽 (2026.05 기준)
MARKETCAP_TOP50_UNIVERSE: set = {
    "005930",  #  1위 삼성전자
    "000660",  #  2위 SK하이닉스
    "005935",  #  3위 삼성전자우
    "402340",  #  4위 SK스퀘어
    "005380",  #  5위 현대차
    "373220",  #  6위 LG에너지솔루션
    "009150",  #  7위 삼성전기
    "034020",  #  8위 두산에너빌리티
    "028260",  #  9위 삼성물산
    "329180",  # 10위 HD현대중공업
    "000270",  # 11위 기아
    "012450",  # 12위 한화에어로스페이스
    "207940",  # 13위 삼성바이오로직스
    "032830",  # 14위 삼성생명
    "012330",  # 15위 현대모비스
    "105560",  # 16위 KB금융
    "006400",  # 17위 삼성SDI
    "055550",  # 18위 신한지주
    "267260",  # 19위 HD현대일렉트릭
    "068270",  # 20위 셀트리온
    "010120",  # 21위 LS ELECTRIC
    "006800",  # 22위 미래에셋증권
    "034730",  # 23위 SK(주)
    "042700",  # 24위 한미반도체
    "005490",  # 25위 포스코홀딩스
    "298040",  # 26위 효성중공업
    "042660",  # 27위 한화오션
    "066570",  # 28위 LG전자
    "017670",  # 29위 SK텔레콤
    "035420",  # 30위 NAVER
    "010130",  # 31위 고려아연
    "009540",  # 32위 HD한국조선해양
    "051910",  # 33위 LG화학
    "000150",  # 34위 두산(주)
    "010140",  # 35위 삼성중공업
    "015760",  # 36위 한국전력공사
    "000810",  # 37위 삼성화재
    "316140",  # 38위 우리금융지주
    "064350",  # 39위 현대로템
    "003670",  # 40위 포스코퓨처엠
    "267250",  # 42위 HD현대(주)
    "272210",  # 43위 한화시스템
    "033780",  # 44위 KT&G
    "096770",  # 45위 SK이노베이션
    "196170",  # 46위 알테오젠
    "247540",  # 47위 에코프로비엠
    "035720",  # 48위 카카오
    "086280",  # 49위 현대글로비스
    "086520",  # 50위 에코프로
    "079550",  # 51위 LIG넥스원
    "011200",  # 52위 HMM
    "307950",  # 53위 현대오토에버
    "000720",  # 54위 현대건설
    "138040",  # 55위 메리츠금융지주
}

# ── 영업이익 지속 흑자 Universe (최근 3개년 흑자 기업 기준) ──────────────────
PROFIT_STABLE_UNIVERSE: set = {
    # 반도체/IT
    "005930",  # 삼성전자
    "000660",  # SK하이닉스
    "267260",  # HD현대일렉트릭
    "042700",  # 한미반도체
    "007660",  # 이수페타시스
    "036930",  # 주성엔지니어링
    "058470",  # 리노공업
    "039030",  # 이오테크닉스
    "095340",  # ISC
    "140860",  # 파크시스템스
    "357780",  # 솔브레인
    "009150",  # 삼성전기
    # 자동차
    "005380",  # 현대차
    "000270",  # 기아
    "012330",  # 현대모비스
    # 금융
    "105560",  # KB금융
    "055550",  # 신한지주
    "086790",  # 하나금융지주
    "024110",  # 기업은행
    "071050",  # 한국금융지주
    "316140",  # 우리금융지주
    "032830",  # 삼성생명
    "000810",  # 삼성화재
    # 통신
    "017670",  # SK텔레콤
    "030200",  # KT
    "033780",  # KT&G
    # 에너지/중공업
    "034020",  # 두산에너빌리티
    "010140",  # 삼성중공업
    "011200",  # HMM
    "047050",  # 포스코인터내셔널
    # 인터넷/플랫폼 (흑자 전환 확인 종목만)
    "035420",  # NAVER
    "003550",  # LG
    "066570",  # LG전자
    "096770",  # SK이노베이션
    "034730",  # SK
}

# ── 주도 섹터 Universe (현재 시장 주도 6개 섹터) ─────────────────────────────
LEADING_SECTOR_UNIVERSE: set = {
    # 반도체 / AI 인프라
    "005930",  # 삼성전자
    "000660",  # SK하이닉스
    "042700",  # 한미반도체
    "007660",  # 이수페타시스
    "036930",  # 주성엔지니어링
    "058470",  # 리노공업
    "039030",  # 이오테크닉스
    "095340",  # ISC
    "140860",  # 파크시스템스
    "357780",  # 솔브레인
    "009150",  # 삼성전기
    # 전력 인프라 / 데이터센터
    "267260",  # HD현대일렉트릭
    "034020",  # 두산에너빌리티
    "015760",  # 한국전력
    # 방산
    "012450",  # 한화에어로스페이스
    "047050",  # 포스코인터내셔널
    # 조선
    "010140",  # 삼성중공업
    "011200",  # HMM
    # 원전
    "034020",  # 두산에너빌리티 (중복 허용 — 원전+중공업 이중 해당)
    # 2차전지 (테마 약화 중이나 포함 유지)
    "373220",  # LG에너지솔루션
    "006400",  # 삼성SDI
    "003670",  # 포스코퓨처엠
}

# ── 필터 탈락 로그 (최근 100건) ──────────────────────────────────────────────
from collections import deque as _deque
_filter_reject_log: _deque = _deque(maxlen=100)


def _log_filter_reject(code: str, name: str, mode: str, reason: str):
    """필터 탈락 사유 로그 기록."""
    import datetime as _dt
    _filter_reject_log.append({
        "ts":     _dt.datetime.now().strftime("%H:%M:%S"),
        "code":   code,
        "name":   name,
        "filter": mode,
        "reason": reason,
    })
    print(f"[FILTER-OUT] {code} {name} | {mode} | {reason}")


def apply_filter_modes(
    items: list,
    marketcap_mode: bool = False,
    profit_mode:    bool = False,
    supply_mode:    bool = False,
    sector_mode:    bool = False,
) -> list:
    """
    Trinity Filter Mode v1.0 — 엔진 앞단 필터 레이어.

    원칙:
    - 기본값 전부 False → 기존 동작 100% 보존
    - 수급모드(supply_mode)는 데이터 없으면 unknown 통과 (탈락 금지)
    - 탈락 사유는 _filter_reject_log에 기록
    - 통과한 item에 filter_badges 필드 추가
    """
    filtered = []

    for item in items:
        code = item.get("code", "")
        name = item.get("name", code)
        badges = []
        rejected = False

        # ── 1. 시총모드 ────────────────────────────────────────────
        if marketcap_mode:
            if code in MARKETCAP_TOP50_UNIVERSE:
                badges.append("시총")
            else:
                _log_filter_reject(code, name, "시총모드", "시총 Universe 미포함")
                rejected = True

        if rejected:
            continue

        # ── 2. 영업이익모드 ────────────────────────────────────────
        if profit_mode:
            if code in PROFIT_STABLE_UNIVERSE:
                badges.append("흑자")
            else:
                _log_filter_reject(code, name, "영업이익모드", "영업이익 지속흑자 Universe 미포함")
                rejected = True

        if rejected:
            continue

        # ── 3. 섹터모드 ────────────────────────────────────────────
        if sector_mode:
            if code in LEADING_SECTOR_UNIVERSE:
                badges.append("섹터")
            else:
                _log_filter_reject(code, name, "섹터모드", "주도섹터 Universe 미포함")
                rejected = True

        if rejected:
            continue

        # ── 4. 수급모드 (탈락 없음 — 점수 가산 + 배지만) ──────────
        if supply_mode:
            # v1: 외부 수급 데이터 미연동 → unknown 통과 원칙
            # item에 supply_status 필드가 있으면 활용, 없으면 unknown
            supply_status = item.get("supply_status", "unknown")
            if supply_status in ("외인순매수", "기관순매수", "양방향순매수"):
                badges.append("수급")
                item = dict(item)  # 원본 변경 방지
                item["supply_bonus"] = 5
            else:
                # unknown 포함 전부 통과 — 탈락 없음
                item = dict(item)
                item["supply_status"] = "unknown"

        # ── 통과 처리 ──────────────────────────────────────────────
        item = dict(item)
        item["filter_badges"] = badges
        filtered.append(item)

    return filtered


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
            # UN(통합)=KRX+NXT 통합시세. 시간외(프리/애프터마켓 NXT)에도 live 가격 반영.
            # NXT 미거래 종목은 자동으로 KRX와 동일 → 부작용 없음. (/judge와 동일, 2026.06.10 J→UN)
            params={"fid_cond_mrkt_div_code": "UN", "fid_input_iscd": code},
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
        # ✅ 정정(2026.06.02): hts_kor_isnm(종목명) 우선.
        # ✅ 정정(2026.06.08): 빈 값일 때 bstp_kor_isnm(업종명)로 떨어지면 섹터가
        #    종목명 행세를 함("IT 서비스" 오표기). → 코드로 fallback (섹터는 sector 필드로만).
        "name": o.get("hts_kor_isnm") or code,
        "sector": o.get("bstp_kor_isnm", ""),   # 업종은 별도 필드로만 분리(judge와 일관)
        "change_pct": float(o.get("prdy_ctrt", 0)),
        "change_amt": int(o.get("prdy_vrss", 0)),
        "is_bullish": price >= open_,
        "pullback_pct": round((high - price) / high * 100, 2) if high > 0 else 0,
        "high_ratio": round(price / high * 100, 1) if high > 0 else 100,
        # 누적거래대금 (VWAP SSOT 원천) — KIS 정식 필드
        "acml_tr_pbmn": _to_int_safe(o.get("acml_tr_pbmn")),
    }


def _to_int_safe(v) -> int:
    try:
        return int(str(v).replace(",", "").strip())
    except Exception:
        return 0


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


@app.get("/supply/{code}")
async def supply_flow(code: str):
    """Judge MRI 수급축 v1 (2026.06.16) — 종목별 당일 외인·기관 순매수 수급 점수.
    소스: KIS inquire-investor(FHKST01010900) 당일 외인/기관 순매수대금 ÷ 당일 거래대금.
    장중(평일 09:00~15:30 KST)=잠정, 그 외=확정. 데이터 없으면 score=null('준비 중') — 가짜 점수 금지."""
    code = (code or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return {"ok": False, "code": code, "score": None, "reason": "invalid_code"}

    def _num(v):  # 음수(순매도) 안전 파싱
        try:
            return float(str(v).replace(",", "").strip())
        except Exception:
            return 0.0

    try:
        token = await get_token()
        # 1) 당일 외인·기관 순매수대금 (inquire-investor = FHKST01010900)
        headers = dict(_wave_build_headers(token)); headers["tr_id"] = "FHKST01010900"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
        async with httpx.AsyncClient(timeout=10) as c:
            res = await c.get(
                f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor",
                params=params, headers=headers,
            )
        d = res.json()
        if d.get("rt_cd") != "0":
            return {"ok": False, "code": code, "score": None, "reason": "kis_error", "msg": d.get("msg1", "")}
        rows = d.get("output") or []
        if not rows:
            return {"ok": False, "code": code, "score": None, "reason": "no_data"}
        t = rows[0]
        frgn = _num(t.get("frgn_ntby_tr_pbmn"))   # 외국인 순매수대금
        orgn = _num(t.get("orgn_ntby_tr_pbmn"))   # 기관계 순매수대금
        net = frgn + orgn

        # 2) 당일 거래대금 — KIS 연속호출 간격(rate limit 회피) 후 직접 호출(원본 에러 노출)
        await asyncio.sleep(0.35)
        ph = dict(_wave_build_headers(token))  # tr_id 기본 FHKST01010100(현재가)
        async with httpx.AsyncClient(timeout=10) as c:
            res2 = await c.get(
                f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
                params={"fid_cond_mrkt_div_code": "UN", "fid_input_iscd": code},
                headers=ph,
            )
        d2 = res2.json()
        if d2.get("rt_cd") != "0":
            return {"ok": False, "code": code, "score": None, "reason": "price_error", "msg": d2.get("msg1", "")}
        turnover = _num((d2.get("output") or {}).get("acml_tr_pbmn"))
        if turnover <= 0:
            return {"ok": False, "code": code, "score": None, "reason": "no_turnover"}

        # 3) v1 점수화: r = 순매수 / 거래대금 → 0~100 (50=중립, ±20%≈양 끝)
        # 단위 보정(실측 확인): inquire-investor 순매수대금=백만원, acml_tr_pbmn=원
        #   → net을 ×100만 해서 원 단위로 일치시킨 뒤 비율 계산.
        net_won = net * 1_000_000
        r = net_won / turnover
        score = max(0, min(100, round(50 + r * 250)))

        # 4) 잠정/확정 (평일 09:00~15:30 KST = 장중 추정)
        now = datetime.now(KST); mins = now.hour * 60 + now.minute
        stat = "잠정" if (now.weekday() < 5 and 540 <= mins <= 930) else "확정"

        return {
            "ok": True, "code": code, "score": score, "status": stat,
            "ratio_pct": round(r * 100, 2),
            "frgn_pbmn": frgn, "orgn_pbmn": orgn, "net_pbmn": net,
            "turnover_pbmn": turnover, "date": t.get("stck_bsop_date", ""),
            "note": "v1: (외인+기관 순매수)/거래대금 · net=백만원→원 보정완료 · 지속성은 v2",
        }
    except Exception as e:
        return {"ok": False, "code": code, "score": None, "reason": "exception", "msg": str(e)[:80]}


# ── Judge MRI v1.0 점수화 엔진 (2026.06.16, 채피 공식 확정) ──────────────────
def _mri_interp(x, pts):
    """구간 선형보간. pts=[(x0,y0)...] x 오름차순. 범위 밖은 끝값으로 클램프."""
    if x <= pts[0][0]:
        return float(pts[0][1])
    if x >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return float(pts[-1][1])

def _mri_vol_score(r):    # 거래량 배율(현재/20일평균)
    return round(_mri_interp(r, [(0.5, 20), (1.0, 50), (1.5, 65), (2.0, 75), (3.0, 90), (5.0, 100)]))
def _mri_mom_score(p):    # 모멘텀: 5일 수익률 %
    return round(_mri_interp(p, [(-10, 10), (-5, 30), (0, 50), (5, 70), (10, 85), (20, 100)]))
def _mri_safe_score(a):   # 안전성: ATR%(낮을수록 안전) — x 오름차순, y 감소
    return round(_mri_interp(a, [(3, 100), (5, 80), (7, 60), (10, 40), (15, 20)]))
def _mri_tech_score(ma5, ma20, ma60):  # 기술: 이동평균 배열 (보너스는 v2)
    if ma5 > ma20 > ma60:
        return 100
    if ma5 > ma20:
        return 80
    if ma20 > ma60:
        return 60
    return 20

def _mri_grade(s):
    return ("S" if s >= 90 else "A" if s >= 80 else "B" if s >= 70
            else "C" if s >= 60 else "D" if s >= 50 else "F")


# ── 🎯 S·A Hunter — 유니버스 자동 MRI 스캔 (2026.06.16, 채피 설계 × 써니 구현) ──
#    선택 유니버스를 순회하며 judge_mri(SSOT)를 돌려 min_score 이상만 반환.
#    중복계산 0(judge_mri 재사용) · 종목 결과 5분 캐시 · 동시 3개(KIS rate limit).
#    ⚠️ 반드시 /mri/{code} 보다 먼저 등록 — 아니면 "scan"이 종목코드로 매칭됨.
_mri_scan_cache: dict = {}   # code -> (epoch, judge_mri 결과)

def _resolve_scan_universe(universe: str):
    # MVP: wave 고정(67종목). 추후 MY universe(림빅 지정) 확장 지점.
    return [c for (_, c) in _WAVE_UNIVERSE]

@app.get("/mri/scan")
async def mri_scan(universe: str = "wave", min_score: int = 80, limit: int = 10):
    codes = _resolve_scan_universe(universe)
    name_map = {v: k for k, v in JUDGE_STOCK_MAP.items()}
    sem = asyncio.Semaphore(3)

    async def _one(code):
        async with sem:
            ent = _mri_scan_cache.get(code)
            if ent and (time.time() - ent[0]) < 300:   # 5분 캐시
                return ent[1]
            try:
                r = await judge_mri(code)
            except Exception as e:
                r = {"ok": False, "code": code, "reason": "exception", "msg": str(e)[:60]}
            _mri_scan_cache[code] = (time.time(), r)
            return r

    results = await asyncio.gather(*[_one(c) for c in codes], return_exceptions=True)
    items = []
    for r in results:
        if not isinstance(r, dict) or not r.get("ok"):
            continue
        sc = r.get("score")
        if sc is None or sc < min_score:
            continue
        a = r.get("axes") or {}
        code = r.get("code")
        items.append({
            "code": code,
            "name": name_map.get(code) or _sentinel_name(code) or code,
            "mri": sc, "grade": r.get("grade"),
            "supply": a.get("supply"), "volume": a.get("volume"),
            "momentum": a.get("momentum"), "safety": a.get("safety"), "tech": a.get("tech"),
            "supply_status": r.get("supply_status", ""),
        })
    items.sort(key=lambda x: x["mri"], reverse=True)
    return {
        "ok": True, "universe": universe, "count": len(codes),
        "passed": len(items), "min_score": min_score,
        "items": items[:limit],
        "note": "S·A Hunter v1 · Wave 유니버스 · judge_mri SSOT · 5분 캐시",
    }


@app.get("/mri/{code}")
async def judge_mri(code: str):
    """Judge MRI v1.0 — 1종목 5축 오각형 + 종합점수 + 등급 + 자동해석.
    축: 수급(/supply)·거래량·모멘텀·기술·안전. 종합 가중치 = 수급30·거래량25·모멘텀20·기술15·안전10.
    데이터 없는 축은 null('준비 중') — 가짜 점수 금지. 장중 수급은 잠정."""
    code = (code or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return {"ok": False, "code": code, "reason": "invalid_code"}
    try:
        token = await get_token()
        rows = await _wave_fetch_daily_rows(code, token)   # 일봉 60일 (최신이 [0])
        if not rows or len(rows) < 20:
            return {"ok": False, "code": code, "reason": "no_daily"}
        closes = [_to_int_safe(r.get("stck_clpr")) for r in rows]
        highs  = [_to_int_safe(r.get("stck_hgpr")) for r in rows]
        lows   = [_to_int_safe(r.get("stck_lwpr")) for r in rows]
        vols   = [_to_int_safe(r.get("acml_vol")) for r in rows]
        cur = closes[0] or 1
        ma5  = sum(closes[:5]) / 5
        ma20 = sum(closes[:20]) / 20
        ma60 = sum(closes[:60]) / min(60, len(closes))
        # 거래량비 = 당일 / 20일 평균
        avg_vol20 = (sum(vols[:20]) / 20) or 1
        vol_ratio = vols[0] / avg_vol20 if avg_vol20 > 0 else 1.0
        # 모멘텀 = 5일 수익률(%)
        mom_pct = (cur - closes[5]) / closes[5] * 100 if len(closes) > 5 and closes[5] > 0 else 0.0
        # 안전성 = ATR14 근사(고-저 평균) / 현재가 %
        trs = [highs[i] - lows[i] for i in range(min(14, len(rows)))]
        atr = (sum(trs) / len(trs)) if trs else 0
        atr_pct = atr / cur * 100 if cur > 0 else 0.0

        # 4축 점수 (실데이터)
        vol_s  = _mri_vol_score(vol_ratio)
        mom_s  = _mri_mom_score(mom_pct)
        safe_s = _mri_safe_score(atr_pct)
        tech_s = _mri_tech_score(ma5, ma20, ma60)

        # 수급축 — /supply 재사용 (없으면 null·가짜 금지)
        sup = await supply_flow(code)
        sup_s = sup.get("score") if sup.get("ok") else None
        sup_status = sup.get("status", "")

        # 종합 (채피 가중치). 수급 빠지면 나머지 4축을 70%로 재정규화.
        if sup_s is not None:
            comp = sup_s * 0.30 + vol_s * 0.25 + mom_s * 0.20 + tech_s * 0.15 + safe_s * 0.10
        else:
            comp = (vol_s * 0.25 + mom_s * 0.20 + tech_s * 0.15 + safe_s * 0.10) / 0.70
        comp = round(comp)
        grade = _mri_grade(comp)

        # "왜 이 등급인가" 자동 해석
        notes = []
        if sup_s is not None and sup_s >= 70:   notes.append("외인·기관 순매수 강함")
        elif sup_s is not None and sup_s < 40:   notes.append("수급 매도 우위")
        if vol_s >= 75:   notes.append("거래량 급증")
        elif vol_s < 40:  notes.append("거래량 부진")
        if mom_s >= 70:   notes.append("단기 상승 모멘텀")
        elif mom_s < 40:  notes.append("단기 하락 압력")
        if tech_s >= 80:  notes.append("정배열 유지")
        elif tech_s <= 20: notes.append("역배열")
        if safe_s < 40:   notes.append("변동성 큼")

        return {
            "ok": True, "code": code, "score": comp, "grade": grade,
            "axes": {"supply": sup_s, "volume": vol_s, "momentum": mom_s,
                     "tech": tech_s, "safety": safe_s},
            "supply_status": sup_status,
            "detail": {"vol_ratio": round(vol_ratio, 2), "mom_pct": round(mom_pct, 2),
                       "atr_pct": round(atr_pct, 2), "aligned": ma5 > ma20 > ma60},
            "interpretation": " · ".join(notes) if notes else "특이 신호 없음",
            "note": "Judge MRI v1.0 · 수급30·거래량25·모멘텀20·기술15·안전10 · 52주보너스/지속성은 v2",
        }
    except Exception as e:
        return {"ok": False, "code": code, "reason": "exception", "msg": str(e)[:80]}


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
        # ━━ Trinity VWAP SSOT v1.0 (채피 확정) ━━
        # fetch_price가 이미 받은 누적거래대금/누적거래량으로 계산.
        # KIS 재호출 없음. compute_vwap = 전 엔진 공유 단일 공식.
        from target_engine import compute_vwap
        vwap_data = compute_vwap(
            p.get("acml_tr_pbmn", 0), p.get("volume", 0), p.get("price", 0)
        )
        # ━━ 영업이익 필터 (지속흑자 Universe 참조 · SSOT) ━━
        # ignition/candidate와 동일한 PROFIT_STABLE_UNIVERSE 사용.
        # Universe 미포함 = 흑자 미확인(적자 가능) → AUTO 진입 제한 신호.
        profit_data = {"profit_stable": code in PROFIT_STABLE_UNIVERSE}
        result = {**p, **pat, **vwap_data, **profit_data, "code": code, "status": "ok"}
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
async def scan_multi(
    codes: str,
    tf: str = "D",
    # ── Trinity Filter Mode v1.0 파라미터 (기본값 전부 False = 기존 동작 보존) ──
    marketcap_mode: bool = False,
    profit_mode:    bool = False,
    supply_mode:    bool = False,
    sector_mode:    bool = False,
):
    """
    종목 스캔. 기본 동작은 기존과 동일.

    Filter Mode (선택):
      marketcap_mode=true  → 시총 상위 Universe만
      profit_mode=true     → 영업이익 지속흑자 Universe만
      supply_mode=true     → 수급 양호 우대 (unknown은 통과)
      sector_mode=true     → 주도섹터 Universe만

    예시: /scan?codes=005930,000660&tf=D&marketcap_mode=true&sector_mode=true
    """
    try:
        results = await _scan_logic(codes, tf)

        # 필터 모드가 하나라도 ON이면 적용
        any_filter = marketcap_mode or profit_mode or supply_mode or sector_mode
        if any_filter:
            before_count = len(results)
            results = apply_filter_modes(
                results,
                marketcap_mode=marketcap_mode,
                profit_mode=profit_mode,
                supply_mode=supply_mode,
                sector_mode=sector_mode,
            )
            filtered_count = before_count - len(results)
        else:
            filtered_count = 0

        return {
            "status":         "ok",
            "tf":             tf,
            "count":          len(results),
            "results":        results,
            # 필터 메타 (필터 OFF면 포함하지 않음)
            **({"filter_meta": {
                "marketcap_mode": marketcap_mode,
                "profit_mode":    profit_mode,
                "supply_mode":    supply_mode,
                "sector_mode":    sector_mode,
                "filtered_out":   filtered_count,
            }} if any_filter else {}),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/filter/log")
async def get_filter_log(last: int = 20):
    """Trinity Filter Mode 탈락 로그 조회. last=N 건."""
    last = max(1, min(last, 100))
    logs = list(_filter_reject_log)[-last:]
    return {
        "count": len(logs),
        "logs":  logs,
        "note":  "수급모드 unknown 통과 종목은 여기에 기록되지 않음",
    }

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

@app.get("/ignition")
async def ignition_page():
    """🔥 IGNITION AUTO 페이지 — same-origin 서빙으로 CORS 회피"""
    import pathlib
    base = pathlib.Path(__file__).parent
    fpath = base / "ignition.html"
    if fpath.exists():
        return FileResponse(str(fpath))
    return {"status": "error", "message": "ignition.html not found in repo root"}

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
    now     = datetime.now(KST)   # ✅ 정정(2026.06.08): 서버 UTC 문제 → 명시적 KST.
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
                    "fid_cond_mrkt_div_code": "UN", "fid_cond_scr_div_code": "20171",
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

from scan_confirm_router import router as wave_scan_router
app.include_router(wave_scan_router)

# ── 🌊 Wave Alert Engine v1.0 (림빅 운영 × 써니 구현) ────────────────────────
# 목적: Universe 종목 중 "거래량 늘면서 상승 시작한 종목"만 텔레그램 알람.
# 철학: "잘 잡는 것"보다 "왜 왔고 왜 안 왔는지 설명 가능".
# 검증된 기존 함수(get_token / fetch_price / _judge_adjusted_52w / send_telegram)
# 를 주입(inject)하여 재사용 — 중복 구현/가짜값/불일치 차단.
from wave_alert_router import (
    router as wave_alert_router,
    register_wave_alert,
    run_wave_alert,
)


def _wave_build_headers(token: str) -> dict:
    """KIS 공통 헤더 (현재가 tr_id 기준)."""
    return {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010100", "custtype": "P",
    }


async def _wave_fetch_daily_rows(code: str, token: str):
    """5일선/20일평균거래량용 수정주가 일봉 rows 반환 (FID_ORG_ADJ_PRC=0)."""
    headers = dict(_wave_build_headers(token))
    headers["tr_id"] = "FHKST03010100"
    end = datetime.now(KST)
    start = end - timedelta(days=60)
    params = {
        "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
    }
    async with httpx.AsyncClient(timeout=10) as c:
        res = await c.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            params=params, headers=headers,
        )
    d = res.json()
    if d.get("rt_cd") != "0":
        return []
    return d.get("output2") or []


# Universe 합집합 (세 Universe 합치고 set으로 중복 자동 제거)
# 이름은 비워두고 fetch_price가 hts_kor_isnm으로 채움 → 가짜 이름 방지.
_WAVE_UNIVERSE = [
    ("", _c) for _c in sorted(
        MARKETCAP_TOP50_UNIVERSE | PROFIT_STABLE_UNIVERSE | LEADING_SECTOR_UNIVERSE
    )
]

# 라우터는 지금 등록 (엔드포인트 노출).
# 단, register_wave_alert(의존성 주입)는 모든 함수 정의가 끝난 뒤
# startup 이벤트에서 호출한다 — _judge_adjusted_52w가 파일 하단(약 2800행대)에
# 정의되므로, 여기서 바로 주입하면 NameError 발생. (import 순서 함정 회피)
app.include_router(wave_alert_router)

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

# ── 종목명 → 코드 검색 (v1.1 · 2026.06.13) ─────────────────────────────────
# 실측 교정: 네이버 금융 검색은 query를 euc-kr 인코딩해야 결과를 반환
# (v1.0은 UTF-8 전송 → "no match in page" — 림빅 실측 스크린샷으로 확인)
# 2단 구조: ① euc-kr 검색 페이지 크롤 → ② 자동완성 API(ac.stock.naver.com) 예비
@app.get("/search_stock/{query}")
async def search_stock(query: str):
    import re as _re
    from urllib.parse import quote as _quote

    cache_key = f"search_stock:{query}"
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key]["ts"] < 3600:
        return _cache[cache_key]["data"]

    UA = "Mozilla/5.0 (Linux; Windows NT 10.0) AppleWebKit/537.36"
    result = {"query": query, "matches": [], "_debug": None, "_src": None}

    def _push(code, name):
        code = (code or "").strip(); name = (name or "").strip()
        if code and name and _re.fullmatch(r"\d{6}", code):
            if not any(m["code"] == code for m in result["matches"]):
                result["matches"].append({"code": code, "name": name})

    # ── ① 데스크톱 검색 페이지 (euc-kr 인코딩 필수) ──
    try:
        q_euc = _quote(query.encode("euc-kr"))
        url = f"https://finance.naver.com/search/search.naver?query={q_euc}"
        async with httpx.AsyncClient(timeout=8, headers={"User-Agent": UA}) as c:
            res = await c.get(url)
        res.encoding = "euc-kr"
        pairs = _re.findall(r'code=(\d{6})[^>]*>\s*([^<>]{1,40}?)\s*</a>', res.text)
        for code, name in pairs[:8]:
            _push(code, name)
        if result["matches"]:
            result["_src"] = "search_euckr"
    except Exception as e:
        result["_debug"] = f"L1 err: {type(e).__name__}: {str(e)[:60]}"

    # ── ② 예비: 네이버 증권 자동완성 (UTF-8 JSON) — 구조 가정 없이 방어적 추출 ──
    if not result["matches"]:
        try:
            url2 = "https://ac.stock.naver.com/ac"
            async with httpx.AsyncClient(timeout=8, headers={"User-Agent": UA}) as c:
                res2 = await c.get(url2, params={"q": query, "target": "stock,index"})
            data = res2.json()

            def walk(node):
                # 어떤 깊이든 [6자리코드, 한글이름, ...] 패턴을 찾는다 (포맷 가정 최소화)
                if isinstance(node, list):
                    strs = [x for x in node if isinstance(x, str)]
                    code = next((s for s in strs if _re.fullmatch(r"\d{6}", s)), None)
                    name = next((s for s in strs if s and not _re.fullmatch(r"[\d.,%+-]+", s) and s != code), None)
                    if code and name:
                        _push(code, name)
                    for x in node:
                        walk(x)
                elif isinstance(node, dict):
                    code = node.get("code") or node.get("cd")
                    name = node.get("name") or node.get("nm")
                    if code and name:
                        _push(str(code), str(name))
                    for v in node.values():
                        walk(v)
            walk(data)
            result["matches"] = result["matches"][:8]
            if result["matches"]:
                result["_src"] = "autocomplete"
            elif result["_debug"] is None:
                result["_debug"] = "both layers empty"
        except Exception as e:
            result["_debug"] = (result["_debug"] or "") + f" | L2 err: {type(e).__name__}: {str(e)[:60]}"

    _cache[cache_key] = {"ts": now, "data": result}
    return result

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
        "vix":    None,
        "usdkrw": None,
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
    # v2.1 (2026.06.13): VIX·원달러 환율 — 동일 검증 경로(fetch_yahoo) 재사용
    result["vix"]    = await fetch_yahoo("%5EVIX", "vix")
    result["usdkrw"] = await fetch_yahoo("KRW%3DX", "usdkrw")

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
async def ignition_scan(
    exclude_etf: bool = True,
    # 🔱 Trinity Filter Mode v1.0 (기본값 전부 False = 기존 동작 보존)
    marketcap_mode: bool = False,
    profit_mode:    bool = False,
    supply_mode:    bool = False,
    sector_mode:    bool = False,
):
    """
    TOP30_v2 실데이터 기반 IGNITION 스캔.
    기본값: ETF/ETN 제외.
    사용 예:
    /ignition_scan
    /ignition_scan?exclude_etf=true
    /ignition_scan?exclude_etf=false
    /ignition_scan?marketcap_mode=true&sector_mode=true

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

    # 🔱 Trinity Filter Mode 적용 (모두 False면 그대로 통과)
    any_filter = marketcap_mode or profit_mode or supply_mode or sector_mode
    if any_filter:
        before = len(results)
        results = apply_filter_modes(
            results,
            marketcap_mode=marketcap_mode,
            profit_mode=profit_mode,
            supply_mode=supply_mode,
            sector_mode=sector_mode,
        )
        print(f"[ignition_scan] Filter Mode: {before}→{len(results)}")

    resp = {
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
    if any_filter:
        resp["filter_meta"] = {
            "marketcap_mode": marketcap_mode,
            "profit_mode":    profit_mode,
            "supply_mode":    supply_mode,
            "sector_mode":    sector_mode,
        }
    return resp


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


async def wave_alert_loop():
    """
    🌊 Wave Alert 백그라운드 루프 — v1.1 (2026.06.08 KST/NXT 창 정정).
    - 활성: 평일 09:00~20:00 KST (정규장 + NXT 시간외). 5분(300초) 간격.
    - 20:00 이후 / 주말: 60초 sleep 후 재확인 (텔레그램 미발송).
    ※ is_market_open()(정규장 전용)와 분리 — Wave는 시간외 NXT까지 커버.
    """
    print("[WAVE-ALERT] 🌊 스케줄러 시작 — v1.1 (09:00~20:00 KST)")
    await asyncio.sleep(15)   # 서버 완전 기동 대기
    while True:
        try:
            now_k = datetime.now(KST)
            tmin  = now_k.hour * 60 + now_k.minute
            wave_open = (now_k.weekday() < 5) and (540 <= tmin < 1200)  # 09:00~20:00
            if wave_open:
                await run_wave_alert()
                await asyncio.sleep(300)   # 5분 간격
            else:
                print(f"[WAVE-ALERT] 💤 시간외/휴장 대기 ({now_k.strftime('%H:%M')} KST) — 60초 후 재확인")
                await asyncio.sleep(60)
        except Exception as e:
            print(f"[WAVE-ALERT] ⚠️ 루프 예외 (유지됨): {e}")
            await asyncio.sleep(30)


@app.on_event("startup")
async def start_scheduler():
    """FastAPI 시작 시 자동 스캔 루프 백그라운드 실행"""
    asyncio.create_task(scan_loop())

    # 🌊 Wave Alert 의존성 주입 (모든 함수 정의 완료 후 시점 → NameError 없음)
    # 유니버스 이름은 JUDGE_STOCK_MAP(이름→코드)을 뒤집어 큐레이트 이름으로 채움.
    # → KIS hts_kor_isnm가 빈 값이어도(예: 035420 NAVER) 코드 대신 이름 표시.
    _code_to_name = {v: k for k, v in JUDGE_STOCK_MAP.items()}
    _named_universe = [(_code_to_name.get(c, ""), c) for (_, c) in _WAVE_UNIVERSE]
    register_wave_alert(
        get_token=get_token,
        fetch_price=fetch_price,
        judge_52w=_judge_adjusted_52w,
        send_telegram=send_telegram,
        is_market_open=is_market_open,
        build_headers=_wave_build_headers,
        fetch_daily_rows=_wave_fetch_daily_rows,
        tg_token=TG_IGNITION_TOKEN,
        tg_chat_id=TG_IGNITION_CHAT_ID,
        kst=KST,
        universe=_named_universe,
    )
    asyncio.create_task(wave_alert_loop())

    # 🛰 Trinity Sentinel v0.1 — 장중 보초 (정의는 파일 하단, startup 시점엔 로드 완료)
    asyncio.create_task(sentinel_loop())

# ============================================
# 🔥 AUTO ORDER ROUTER — IGNITION AUTO v0.1
# AUTO 전용 계좌 (KIS_AUTO_ACCOUNT) 전용
# 실주문 라우터 — 기존 KIS 로직과 완전 분리
# ============================================
# v2.0 SAFE POSITION ARCHITECTURE (채피 설계 / 써니 구현)
#
# 핵심 철학:
#   KIS 계좌 = 창고 (건드리지 않음)
#   AUTO positions = 엔진 전용 장부 (이것만 관리)
#
# 안전 규칙:
#   1. 블랙리스트 — 앱 시작 시 기존 보유종목 자동 등록 → 매수/매도 금지
#   2. 포지션 장부 — AUTO가 직접 매수한 수량만 기록
#   3. 매도 검증 — MIN(AUTO수량, KIS실제수량) 만 매도
#   4. REJECT_OVERSELL — 초과 매도 시도 즉시 거절 + 로그
# ============================================

import os as _os

KIS_AUTO_ACCOUNT = _os.environ.get("KIS_AUTO_ACCOUNT", "")

# ── AUTO 전용 토큰 ──────────────────────────────────────────────────
_auto_access_token: str = ""
_auto_token_expires: float = 0.0

async def _get_auto_token() -> str:
    """AUTO 계좌 전용 액세스 토큰 발급/재사용"""
    import time, httpx
    global _auto_access_token, _auto_token_expires
    if _auto_access_token and time.time() < _auto_token_expires - 60:
        return _auto_access_token
    url = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey":    KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(url, json=body)
        data = res.json()
    _auto_access_token  = data["access_token"]
    _auto_token_expires = time.time() + int(data.get("expires_in", 86400))
    print(f"[AUTO-ORDER] 토큰 발급 완료 — 계좌: {KIS_AUTO_ACCOUNT}")
    return _auto_access_token


# ── 🛡️ SAFE POSITION 장부 ────────────────────────────────────────────
# AUTO가 직접 매수한 수량만 기록 — KIS 전체 잔고와 무관
# { "005930": {"qty": 3, "avg_price": 285000, "entry_time": "..."} }
_auto_positions: dict = {}

# ── 🚫 블랙리스트 — 기존 보유종목 자동 등록 ────────────────────────
# 앱 시작 시 /auto/blacklist/refresh 호출로 갱신
_auto_blacklist: set = set()


async def _fetch_kis_holdings(token: str) -> dict:
    """
    KIS 실제 보유수량 조회.
    반환: { "005930": 100, "066970": 12, ... }
    """
    import httpx
    url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {
        "content-type":  "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APP_KEY,
        "appsecret":     KIS_APP_SECRET,
        "tr_id":         "TTTC8434R",
        "custtype":      "P",
    }
    params = {
        "CANO":                  KIS_AUTO_ACCOUNT,
        "ACNT_PRDT_CD":          "01",
        "AFHR_FLPR_YN":          "N",
        "OFL_YN":                "N",
        "INQR_DVSN":             "02",
        "UNPR_DVSN":             "01",
        "FUND_STTL_ICLD_YN":     "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN":             "01",
        "CTX_AREA_FK100":        "",
        "CTX_AREA_NK100":        "",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res  = await client.get(url, headers=headers, params=params)
            data = res.json()
        holdings = {}
        for item in (data.get("output1") or []):
            code = item.get("pdno", "")
            qty  = int(item.get("hldg_qty", 0) or 0)
            if code and qty > 0:
                holdings[code] = qty
        return holdings
    except Exception as e:
        print(f"[AUTO-HOLDINGS] 조회 실패: {e}")
        return {}


# ── 🚫 블랙리스트 갱신 엔드포인트 ───────────────────────────────────

# ── 🚫 블랙리스트 갱신 엔드포인트 ───────────────────────────────────

@app.post("/auto/blacklist/refresh")
async def auto_blacklist_refresh():
    """KIS 현재 보유종목을 블랙리스트에 자동 등록 (POST)."""
    return await _do_blacklist_refresh()

@app.get("/auto/blacklist/refresh")
async def auto_blacklist_refresh_get():
    """
    KIS 현재 보유종목을 블랙리스트에 자동 등록 (GET — 브라우저/폰에서 직접 호출 가능).

    브라우저 주소창에 입력:
    https://fastapi-production-f631.up.railway.app/auto/blacklist/refresh
    """
    return await _do_blacklist_refresh()

async def _do_blacklist_refresh():
    """블랙리스트 갱신 공통 로직."""
    global _auto_blacklist
    if not KIS_AUTO_ACCOUNT:
        raise HTTPException(status_code=500, detail="KIS_AUTO_ACCOUNT 미설정")

    token    = await _get_auto_token()
    holdings = await _fetch_kis_holdings(token)

    # AUTO 장부에 없는 종목만 블랙리스트 등록
    # (AUTO가 직접 산 종목은 블랙리스트 제외)
    new_blacklist = set()
    for code, qty in holdings.items():
        if code not in _auto_positions:
            new_blacklist.add(code)

    _auto_blacklist = new_blacklist
    bl_list = sorted(list(_auto_blacklist))
    print(f"[BLACKLIST] 등록 완료: {bl_list}")

    return {
        "blacklist": bl_list,
        "count":     len(bl_list),
        "message":   "기존 보유종목 블랙리스트 등록 완료 — AUTO 매수/매도 금지",
    }


@app.get("/auto/blacklist")
async def auto_blacklist_get():
    """현재 블랙리스트 조회."""
    return {
        "blacklist": sorted(list(_auto_blacklist)),
        "count":     len(_auto_blacklist),
    }


# ── 📋 AUTO 포지션 장부 조회 ─────────────────────────────────────────

@app.get("/auto/positions")
async def auto_positions_get():
    """AUTO 엔진이 직접 매수한 포지션 장부 조회."""
    return {
        "positions": _auto_positions,
        "count":     len(_auto_positions),
        "note":      "AUTO 엔진 전용 장부. KIS 전체 잔고와 별개.",
    }


# ── ✅ AUTO 매수 주문 (안전 버전) ────────────────────────────────────

@app.post("/auto/order/buy")
async def auto_order_buy(body: dict):
    """
    AUTO 매수 주문 — SAFE POSITION ARCHITECTURE

    body: { code, qty, price, name? }

    안전 체크:
      1. 블랙리스트 종목 → REJECT_BLACKLIST
      2. 이미 AUTO 보유 중 → REJECT_DUPLICATE
      3. KIS 주문 성공 → positions 장부 등록
    """
    import httpx, datetime
    if not KIS_AUTO_ACCOUNT:
        raise HTTPException(status_code=500, detail="KIS_AUTO_ACCOUNT 미설정")

    code  = body.get("code", "")
    qty   = int(body.get("qty", 0))
    price = int(body.get("price", 0))
    name  = body.get("name", code)

    if not code or qty <= 0 or price <= 0:
        raise HTTPException(status_code=400, detail="code/qty/price 필수")

    # ── 🚫 블랙리스트 체크 ──────────────────────────────────────────
    if code in _auto_blacklist:
        print(f"[AUTO-BUY] REJECT_BLACKLIST — {code} {name} 기존 보유종목")
        raise HTTPException(
            status_code=400,
            detail=f"REJECT_BLACKLIST: {name}({code})는 기존 보유종목 — AUTO 매수 금지"
        )

    # ── 🚫 중복 진입 체크 ────────────────────────────────────────────
    if code in _auto_positions:
        print(f"[AUTO-BUY] REJECT_DUPLICATE — {code} 이미 AUTO 보유 중")
        raise HTTPException(
            status_code=400,
            detail=f"REJECT_DUPLICATE: {name}({code}) 이미 AUTO 진입 중"
        )

    # ── KIS 매수 주문 ────────────────────────────────────────────────
    token = await _get_auto_token()
    url   = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/order-cash"
    headers = {
        "content-type":  "application/json",
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APP_KEY,
        "appsecret":     KIS_APP_SECRET,
        "tr_id":         "TTTC0802U",
        "custtype":      "P",
    }
    payload = {
        "CANO":         KIS_AUTO_ACCOUNT,
        "ACNT_PRDT_CD": "01",
        "PDNO":         code,
        "ORD_DVSN":     "00",
        "ORD_QTY":      str(qty),
        "ORD_UNPR":     str(price),
    }
    async with httpx.AsyncClient(timeout=10) as client:
        res  = await client.post(url, json=payload, headers=headers)
        data = res.json()

    rt_cd = data.get("rt_cd", "")
    msg   = data.get("msg1", "")
    print(f"[AUTO-BUY] {code} {qty}주 @ {price}원 → rt_cd:{rt_cd} {msg}")

    if rt_cd != "0":
        raise HTTPException(status_code=400, detail=f"KIS 오류: {msg}")

    ord_no = data.get("output", {}).get("ODNO", "")

    # ── ✅ 포지션 장부 등록 ──────────────────────────────────────────
    _auto_positions[code] = {
        "name":       name,
        "qty":        qty,
        "avg_price":  price,
        "entry_time": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "ord_no":     ord_no,
        "source":     "CANDIDATE_AUTO",
    }
    print(f"[AUTO-POS] 장부 등록: {code} {name} {qty}주 @ {price}")

    return {
        "status":   "ok",
        "code":     code,
        "name":     name,
        "qty":      qty,
        "price":    price,
        "ord_no":   ord_no,
        "msg":      msg,
        "position": _auto_positions[code],
    }


# ── ✅ AUTO 매도 주문 (안전 버전) ────────────────────────────────────

@app.post("/auto/order/sell")
async def auto_order_sell(body: dict):
    """
    AUTO 매도 주문 — SAFE POSITION ARCHITECTURE

    body: { code, qty, price, name? }

    안전 체크:
      1. 블랙리스트 종목 → REJECT_BLACKLIST
      2. AUTO 장부에 없는 종목 → REJECT_NOT_IN_POSITIONS
      3. KIS 실제 보유수량 조회 → MIN(AUTO수량, 실제수량) 적용
      4. sell_qty <= 0 → REJECT_OVERSELL
    """
    import httpx, datetime
    if not KIS_AUTO_ACCOUNT:
        raise HTTPException(status_code=500, detail="KIS_AUTO_ACCOUNT 미설정")

    code  = body.get("code", "")
    qty   = int(body.get("qty", 0))
    price = int(body.get("price", 0))
    name  = body.get("name", code)

    if not code or qty <= 0 or price <= 0:
        raise HTTPException(status_code=400, detail="code/qty/price 필수")

    # ── 🚫 블랙리스트 체크 ──────────────────────────────────────────
    if code in _auto_blacklist:
        print(f"[AUTO-SELL] REJECT_BLACKLIST — {code} 기존 보유종목 매도 시도 차단")
        raise HTTPException(
            status_code=400,
            detail=f"REJECT_BLACKLIST: {name}({code})는 기존 보유종목 — AUTO 매도 절대 금지"
        )

    # ── 🚫 포지션 장부 체크 ─────────────────────────────────────────
    if code not in _auto_positions:
        print(f"[AUTO-SELL] REJECT_NOT_IN_POSITIONS — {code} AUTO 장부에 없음")
        raise HTTPException(
            status_code=400,
            detail=f"REJECT_NOT_IN_POSITIONS: {name}({code}) AUTO 진입 기록 없음 — 매도 금지"
        )

    auto_qty = _auto_positions[code]["qty"]

    # ── KIS 실제 보유수량 조회 ───────────────────────────────────────
    token    = await _get_auto_token()
    holdings = await _fetch_kis_holdings(token)
    kis_qty  = holdings.get(code, 0)

    # ── MIN(AUTO수량, 실제수량) 적용 ────────────────────────────────
    sell_qty = min(auto_qty, kis_qty, qty)

    if sell_qty <= 0:
        log_msg = f"REJECT_OVERSELL: AUTO={auto_qty} KIS={kis_qty} 요청={qty}"
        print(f"[AUTO-SELL] {log_msg}")
        raise HTTPException(status_code=400, detail=log_msg)

    if sell_qty < qty:
        print(f"[AUTO-SELL] 수량 조정: 요청{qty} → 실제{sell_qty} (AUTO:{auto_qty} KIS:{kis_qty})")

    # ── KIS 매도 주문 ────────────────────────────────────────────────
    url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/order-cash"
    headers = {
        "content-type":  "application/json",
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APP_KEY,
        "appsecret":     KIS_APP_SECRET,
        "tr_id":         "TTTC0801U",
        "custtype":      "P",
    }
    payload = {
        "CANO":         KIS_AUTO_ACCOUNT,
        "ACNT_PRDT_CD": "01",
        "PDNO":         code,
        "ORD_DVSN":     "00",
        "ORD_QTY":      str(sell_qty),
        "ORD_UNPR":     str(price),
    }
    async with httpx.AsyncClient(timeout=10) as client:
        res  = await client.post(url, json=payload, headers=headers)
        data = res.json()

    rt_cd = data.get("rt_cd", "")
    msg   = data.get("msg1", "")
    print(f"[AUTO-SELL] {code} {sell_qty}주 @ {price}원 → rt_cd:{rt_cd} {msg}")

    if rt_cd != "0":
        raise HTTPException(status_code=400, detail=f"KIS 오류: {msg}")

    ord_no = data.get("output", {}).get("ODNO", "")

    # ── ✅ 포지션 장부 청산 ──────────────────────────────────────────
    remaining = auto_qty - sell_qty
    if remaining <= 0:
        del _auto_positions[code]
        print(f"[AUTO-POS] 장부 청산 완료: {code}")
    else:
        _auto_positions[code]["qty"] = remaining
        print(f"[AUTO-POS] 부분 청산: {code} 잔량 {remaining}주")

    return {
        "status":        "ok",
        "code":          code,
        "name":          name,
        "sell_qty":      sell_qty,
        "price":         price,
        "ord_no":        ord_no,
        "msg":           msg,
        "auto_qty_orig": auto_qty,
        "kis_qty":       kis_qty,
        "remaining":     remaining if remaining > 0 else 0,
    }


@app.get("/auto/balance")
async def auto_balance():
    """AUTO 계좌 현금 잔고 조회"""
    import httpx
    if not KIS_AUTO_ACCOUNT:
        raise HTTPException(status_code=500, detail="KIS_AUTO_ACCOUNT 미설정")

    token = await _get_auto_token()
    url   = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APP_KEY,
        "appsecret":     KIS_APP_SECRET,
        "tr_id":         "TTTC8908R",
        "custtype":      "P",
    }
    params = {
        "CANO":                  KIS_AUTO_ACCOUNT,
        "ACNT_PRDT_CD":          "01",
        "PDNO":                  "005930",
        "ORD_UNPR":              "0",
        "ORD_DVSN":              "00",
        "CMA_EVLU_AMT_ICLD_YN":  "N",
        "OVRS_ICLD_YN":          "N",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        res  = await client.get(url, headers=headers, params=params)
        data = res.json()

    output = data.get("output", {})
    cash   = int(output.get("ord_psbl_cash", 0))
    print(f"[AUTO-BALANCE] 가용현금: {cash:,}원")

    return {
        "status":          "ok",
        "account":         KIS_AUTO_ACCOUNT,
        "avail_cash":      cash,
        "auto_positions":  len(_auto_positions),
        "blacklist_count": len(_auto_blacklist),
    }



# ══════════════════════════════════════════════════════════════════════════════
# ⚖ Judge Engine 1단계 — KIS 자동수집 (inquire-price)
#    채피 설계 × 써니 구현 × 림빅 결정
#    종목명/코드 → 현재가 · 시총 · PER · PBR · 52주 회복률
#    get_token() · _to_int_safe() 재사용 · 기존 함수 무수정
# ══════════════════════════════════════════════════════════════════════════════

JUDGE_STOCK_MAP = {
    # ── Quad-Core (림빅 핵심 포트폴리오) ──────────────────────────────────
    "삼성전자":           "005930",
    "SK하이닉스":         "000660",
    "한미반도체":         "042700",
    "HD현대일렉트릭":     "267260",   # ✅ 267260(전기장비). 267250=HD현대 지주
    "이수페타시스":       "007660",
    # ── 반도체 / AI 인프라 ──────────────────────────────────────────────
    "주성엔지니어링":     "036930",
    "리노공업":           "058470",
    "이오테크닉스":       "039030",
    "ISC":                "095340",
    "파크시스템스":       "140860",
    "솔브레인":           "357780",
    "삼성전기":           "009150",
    # ── 전력인프라 / 방산 / 조선 ────────────────────────────────────────
    "LG디스플레이":       "034220",
    "두산에너빌리티":     "034020",
    "한화에어로스페이스": "012450",
    "HD한국조선해양":     "009540",
    "삼성중공업":         "010140",
    "HD현대중공업":       "329180",
    "한화오션":           "042660",
    "효성중공업":         "298040",
    "LS ELECTRIC":        "010120",
    "한국전력":           "015760",
    # ── 대형주 ─────────────────────────────────────────────────────────
    "현대차":             "005380",
    "기아":               "000270",
    "현대모비스":         "012330",
    "NAVER":              "035420",
    "카카오":             "035720",
    "KB금융":             "105560",
    "신한지주":           "055550",
    "하나금융지주":       "086790",
    "삼성바이오로직스":   "207940",
    "셀트리온":           "068270",
    "LG에너지솔루션":     "373220",
    "삼성SDI":            "006400",
    "포스코퓨처엠":       "003670",
    "POSCO홀딩스":        "005490",
    "LG화학":             "051910",
    "LG전자":             "066570",
    "SK이노베이션":       "096770",
    "SK텔레콤":           "017670",
    "SK스퀘어":           "402340",
    "SK":                 "034730",
    "삼성물산":           "028260",
    "삼성생명":           "032830",
    "고려아연":           "010130",
    "포스코인터내셔널":   "047050",
    "HMM":                "011200",
    "두산":               "000150",
    "HD현대":             "267250",   # 지주 — 혼동 방지 별도 등록
    "미래에셋증권":       "006800",
    # … 추가 필요 시 코드 반드시 검증 후 등록
}


def _judge_resolve(q: str):
    q = q.strip()
    if q.isdigit() and len(q) == 6:
        return q
    if q in JUDGE_STOCK_MAP:
        return JUDGE_STOCK_MAP[q]
    key = q.replace(" ", "")
    # 정확 일치(공백 무시) 우선 → 부분 일치는 짧은 이름부터(오매칭 방지)
    for n, c in sorted(JUDGE_STOCK_MAP.items(), key=lambda kv: len(kv[0])):
        if key == n.replace(" ", ""):
            return c
    for n, c in sorted(JUDGE_STOCK_MAP.items(), key=lambda kv: len(kv[0])):
        if key in n.replace(" ", ""):
            return c
    return None


async def _judge_adjusted_52w(code: str, headers: dict):
    """수정주가 일봉으로 최근 52주 고/저 계산 (액면분할 왜곡 제거).
    반환: (w52_low, w52_high, source) — 실패 시 (None, None, 사유)"""
    from datetime import datetime, timedelta
    end = datetime.now()
    start = end - timedelta(days=370)
    chart_headers = dict(headers)
    chart_headers["tr_id"] = "FHKST03010100"
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            res = await c.get(
                f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                params=params, headers=chart_headers,
            )
        d = res.json()
        if d.get("rt_cd") != "0":
            return None, None, "chart_fail"
        rows = d.get("output2") or []
        highs, lows = [], []
        for r in rows:
            hi = _to_int_safe(r.get("stck_hgpr"))
            lo = _to_int_safe(r.get("stck_lwpr"))
            if hi > 0:
                highs.append(hi)
            if lo > 0:
                lows.append(lo)
        if not highs or not lows:
            return None, None, "chart_empty"
        return min(lows), max(highs), "adjusted_daily"
    except Exception as e:
        return None, None, "chart_err_" + type(e).__name__


@app.get("/judge/{query}")
async def judge_step1(query: str):
    """Judge 1단계: 종목명 -> 시세/밸류/52주. 52주는 수정주가 일봉 기준."""
    code = _judge_resolve(query)
    if not code:
        raise HTTPException(
            status_code=404,
            detail="'" + query + "' 코드 못 찾음 — 6자리 코드 입력 또는 JUDGE_STOCK_MAP에 추가",
        )

    token = await get_token()
    headers = {
        "content-type": "application/json",
        "authorization": "Bearer " + token,
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010100", "custtype": "P",
    }
    async with httpx.AsyncClient(timeout=10) as c:
        res = await c.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
            # UN(통합)=KRX+NXT 통합시세. 시간외(NXT 애프터마켓)에도 live 가격 반영.
            # NXT 미거래 종목은 자동으로 KRX와 동일 → 부작용 없음. (2026.06.05 J→UN)
            params={"fid_cond_mrkt_div_code": "UN", "fid_input_iscd": code},
            headers=headers,
        )
        data = res.json()
    if data.get("rt_cd") != "0":
        raise HTTPException(status_code=502, detail="KIS: " + str(data.get("msg1", "조회 실패")))
    o = data.get("output") or {}

    price = int(o.get("stck_prpr", 0) or 0)
    change_pct = float(o.get("prdy_ctrt", 0) or 0)      # 전일대비율(%)
    change_amt = int(o.get("prdy_vrss", 0) or 0)        # 전일대비(원)
    prev_close = int(o.get("stck_sdpr", 0) or 0)        # 전일 종가(기준가)

    w52l_raw = _to_int_safe(o.get("w52_lwpr"))
    w52h_raw = _to_int_safe(o.get("w52_hgpr"))
    w52l_adj, w52h_adj, w52_src = await _judge_adjusted_52w(code, headers)
    if w52l_adj and w52h_adj:
        w52l, w52h, w52_source = w52l_adj, w52h_adj, w52_src
    else:
        w52l, w52h, w52_source = w52l_raw, w52h_raw, "price_api_raw_" + str(w52_src)

    raw_name = (o.get("hts_kor_isnm") or "").strip()
    name_out = raw_name if raw_name else _sentinel_name(code)  # 빈 이름 → HUB 박제 이름/종목맵 폴백
    sector_raw = (o.get("bstp_kor_isnm") or "").strip()

    # ── v2.0 Day 2: MRI 3축 동승 (Grade · R/R · Mode) ─────────────────────
    # SSOT 원칙: 재계산 금지 — /support와 '같은 함수'를 내부 호출해 같은 숫자만 사용.
    # MRI가 실패해도 Judge 본 기능은 유지 (mri=null + 사유 노출, 조용히 안 넘김).
    mri, mri_error, spark = None, None, None
    try:
        sup = await support_zone(code, mode="balance")
        spark = sup.get("spark")
        mri = {
            "grade": sup["grade"], "grade_label": sup["grade_label"],
            "rr": sup["rr"], "rr_target": sup["rr_target"],
            "mode": sup["mode"], "recommended_mode": sup["recommended_mode"],
            "stop": sup["stop"], "risk_pct": sup["risk_pct"],
            "tradeable": sup["tradeable"], "data_pending": sup["data_pending"],
            "action_label": sup["action_label"],
            "verdict": sup["card"]["verdict"],
            "mode_rr": sup["mode_rr"],
            "source": "MRI SSOT (target_engine · /support 동일 계산)",
        }
    except Exception as e:
        mri_error = type(e).__name__ + ": " + str(e)

    return {
        "code":    code,
        "name":    name_out,
        "sector":  sector_raw,
        "price":   price,
        "change_pct": round(change_pct, 2),
        "change_amt": change_amt,
        "prev_close": prev_close,
        "mcap":    _to_int_safe(o.get("hts_avls")),
        "per":     float(o.get("per",  0) or 0),
        "pbr":     float(o.get("pbr",  0) or 0),
        "w52_low":  w52l,
        "w52_high": w52h,
        "recovery": round((price - w52l) / w52l * 100, 1) if w52l else 0,
        "w52_source": w52_source,
        "spark": spark,                   # 최근 30일 종가 — HUB 미니 차트용
        "mri": mri,                       # v2.0 — Grade·R/R·Mode 3축 (SSOT)
        "mri_error": mri_error,           # 실패 시 사유 노출 (실측 우선 원칙)
        "_debug_hts": o.get("hts_kor_isnm"),
        "_debug_bstp": o.get("bstp_kor_isnm"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 🔬 Judge 시간외 대응 — 1단계: RAW 디버그 엔드포인트 (2026.06.05)
#    채피 설계 × 써니 구현 × 림빅 결정
#    목적: KIS inquire-price 응답 output을 "가공·판단 없이 통째로" 노출.
#    림빅이 시간외(16:00~18:00) / 장전(08:00~09:00)에 직접 호출 →
#    어떤 필드가 시간외가인지 실측 확인 → 2단계에서 /judge에 정식 반영.
#    원칙: 추측으로 필드 박지 않음. 가짜값 0. 실패는 조용히 넘기지 않고 노출.
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/judge_raw/{query}")
async def judge_raw(query: str, mkt: str = "J"):
    """KIS inquire-price 원본 output 전체 노출 (시간외 필드 탐지용 디버그).
    /judge 와 동일한 검증된 호출을 쓰되, 가공 없이 raw 를 그대로 돌려줌.
    mkt: 시장코드 J=KRX(기본) · NX=NXT · UN=통합 (?mkt=UN 으로 호출)."""
    code = _judge_resolve(query)
    if not code:
        raise HTTPException(
            status_code=404,
            detail="'" + query + "' 코드 못 찾음 — 6자리 코드 입력 또는 JUDGE_STOCK_MAP에 추가",
        )
    mkt = (mkt or "J").upper()
    if mkt not in ("J", "NX", "UN"):
        mkt = "J"

    # 현재 시각(KST)으로 세션 힌트 — 어떤 시간대 스냅샷인지 표시용 (대략값)
    now_kst = datetime.now(KST)
    hhmm = now_kst.hour * 100 + now_kst.minute
    if   800 <= hhmm < 900:    session_hint = "PREP (장전 시간외)"
    elif 900 <= hhmm <= 1530:  session_hint = "REGULAR (정규장)"
    elif 1530 < hhmm < 1600:   session_hint = "AFTER-CLOSE (시간외 종가)"
    elif 1600 <= hhmm <= 1800: session_hint = "AFTER (시간외 단일가)"
    else:                      session_hint = "CLOSED (장 종료)"

    token = await get_token()
    headers = {
        "content-type": "application/json",
        "authorization": "Bearer " + token,
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010100", "custtype": "P",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            res = await c.get(
                f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
                params={"fid_cond_mrkt_div_code": mkt, "fid_input_iscd": code},
                headers=headers,
            )
            data = res.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail="KIS 호출 실패: " + type(e).__name__)

    output = data.get("output") or {}

    # 시간외로 의심되는 키만 따로 모아 보기 편하게 (키 이름 grep만, 값 가공 없음)
    # ※ 이건 '힌트'일 뿐 — 진짜 정답은 아래 output_raw 전체에서 림빅이 확인.
    SUSPECT = ("ovtm", "otms", "after")
    overtime_like = {k: v for k, v in output.items()
                     if any(s in k.lower() for s in SUSPECT)}

    return {
        "code": code,
        "mkt_used": mkt,                       # J=KRX · NX=NXT · UN=통합
        "name": (output.get("hts_kor_isnm") or "").strip() or code,
        "queried_at": now_kst.strftime("%Y-%m-%d %H:%M:%S KST"),
        "session_hint": session_hint,
        "rt_cd": data.get("rt_cd"),
        "msg1": data.get("msg1"),
        "field_count": len(output),
        "all_keys": sorted(output.keys()),
        "overtime_like_keys": overtime_like,   # 의심 필드 (없으면 {} — 그래도 정상)
        "output_raw": output,                  # ← 전체 원본. 가공 0. 여기가 핵심.
    }


# ══════════════════════════════════════════════════════════════════════════════
# 🔬 Judge 시간외 대응 — 검증: 시간외현재가 확인 엔드포인트 (2026.06.05)
#    KIS [국내주식-076] 국내주식 시간외현재가 / HTS [0230] 화면
#    inquire-price엔 시간외가가 없음(실측 확정) → 시간외 단일가는 이 API에서 옴.
#    시간외 단일가 현재가 = output.ovtm_untp_prpr
#    18시 전에 ovtm_untp_prpr 실값 확인 후 /judge 정식 반영(2단계) 예정.
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/judge_overtime/{query}")
async def judge_overtime(query: str):
    """KIS 시간외현재가(inquire-overtime-price · FHPST02300000) 실측 확인."""
    code = _judge_resolve(query)
    if not code:
        raise HTTPException(
            status_code=404,
            detail="'" + query + "' 코드 못 찾음 — 6자리 코드 입력 또는 JUDGE_STOCK_MAP에 추가",
        )

    now_kst = datetime.now(KST)
    token = await get_token()
    headers = {
        "content-type": "application/json",
        "authorization": "Bearer " + token,
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": "FHPST02300000", "custtype": "P",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            res = await c.get(
                f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-overtime-price",
                params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
                headers=headers,
            )
            data = res.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail="KIS 호출 실패: " + type(e).__name__)

    o = data.get("output") or {}
    summary = {
        "시간외_현재가": o.get("ovtm_untp_prpr"),       # ← 핵심
        "전일대비":     o.get("ovtm_untp_prdy_vrss"),
        "부호":         o.get("ovtm_untp_prdy_vrss_sign"),  # 2=상승,5=하락 등
        "전일대비율":   o.get("ovtm_untp_prdy_ctrt"),
        "거래량":       o.get("ovtm_untp_vol"),
        "거래대금":     o.get("ovtm_untp_tr_pbmn"),
        "시가":         o.get("ovtm_untp_oprc"),
        "최고가":       o.get("ovtm_untp_hgpr"),
        "최저가":       o.get("ovtm_untp_lwpr"),
        "예상체결가":   o.get("ovtm_untp_antc_cnpr"),
        "기준가":       o.get("ovtm_untp_sdpr"),
    }
    return {
        "code": code,
        "queried_at": now_kst.strftime("%Y-%m-%d %H:%M:%S KST"),
        "rt_cd": data.get("rt_cd"),
        "msg1": data.get("msg1"),
        "시간외요약": summary,
        "overtime_raw": o,                              # 전체 원본 (가공 0)
    }


# ══════════════════════════════════════════════════════════════════════════════
# 🛡️ Support Alert Engine v0.1 — 지지선 계산 + 손절 쉬운 자리 탐색
#    채피 설계 × 써니 구현 × 림빅 결정
#    철학: "좋은 종목보다 좋은 상태, 좋은 상태보다 손절 쉬운 자리.
#           손절 쉬운 자리만 모으면 결국 수익은 따라온다."
#
#    공통 엔진 1개 + 3모드 (군집·등급은 동일, 손절선만 모드별):
#      balance(Judge 기본) : 2차 지지 군집 직하 (균형 — 잘 안 털림)
#      loose  (Judge 널널) : 최종 구조저점(60일) 직하 (스윙답게 넉넉)
#      tight  (Strike)     : 1차 지지 군집 직하 (스캘핑 — 타이트)
#
#    데이터는 전부 기존 실측 경로 재사용 — 추측·가짜값 0:
#      fetch_price()            → 현재가·당일저점·시가·전일종가·거래대금·종목명
#      compute_vwap()           → VWAP (전 엔진 공유 SSOT)
#      _wave_fetch_daily_rows() → 수정주가 일봉 60일 (5/20일선·스윙저점·장대양봉)
#      _judge_adjusted_52w()    → 52주 고/저 (R/R 상승여력 소스)
#
#    ※ main_safe.py 맨 끝(judge_overtime 엔드포인트 다음)에 그대로 붙여넣기.
#       @app / fetch_price / _wave_fetch_daily_rows / _judge_adjusted_52w /
#       _judge_resolve / get_token / _to_int_safe / KIS_* / BASE_URL / KST 모두
#       같은 파일에 이미 정의돼 있으므로 추가 import 불필요.
# ══════════════════════════════════════════════════════════════════════════════

# ── v2.0 SSOT 통합 (Day 1) ──────────────────────────────────────────────
# MRI 계산 계층(상수·_sup_* 13개)은 target_engine.py로 이전.
# 아래 import가 기존 이름을 그대로 제공 → /support 엔드포인트 본체 무변경.
from target_engine import (
    SUPPORT_CLUSTER_TOL, SUPPORT_TOL_MAX, SUPPORT_TOL_K,
    SUPPORT_BODY_MIN, SUPPORT_VOL_MULT, SUPPORT_PAD,
    GRADE_LABEL, SUPPORT_RR_MIN, SUPPORT_MODE_LABEL,
    _sup_sma, _sup_build_candles, _sup_vwap, _sup_find_pungdae,
    _sup_cluster_tol, _sup_cluster, _sup_grade, _sup_stop,
    _sup_swing_high, _sup_rr, _sup_recommend,
)


@app.get("/support/{query}")
async def support_zone(query: str, mode: str = "balance"):
    """🛡️ Support Alert Engine v0.1 — 지지선 계산 + 손절 쉬운 자리.
    mode: balance(Judge 기본) · loose(Judge 널널) · tight(Strike 스캘핑)."""
    mode = (mode or "balance").lower()
    if mode not in ("balance", "loose", "tight"):
        mode = "balance"

    code = _judge_resolve(query)
    if not code:
        raise HTTPException(
            status_code=404,
            detail="'" + query + "' 코드 못 찾음 — 6자리 코드 입력 또는 JUDGE_STOCK_MAP에 추가",
        )

    token = await get_token()

    # ── 실측 현재가/당일/거래대금/종목명 (fetch_price = /stock과 동일 SSOT 경로) ──
    p = await fetch_price(code, token)
    price       = p.get("price", 0)
    prev_close  = p.get("prev_close", 0)
    today_low   = p.get("low", 0)
    today_open  = p.get("open", 0)
    name        = p.get("name", code)
    if name == code:   # hts_kor_isnm 빈 값(시간외 등) → JUDGE_STOCK_MAP 역매핑
        name = next((n for n, c in JUDGE_STOCK_MAP.items() if c == code), code)
    sector      = p.get("sector", "")
    acml_pbmn   = p.get("acml_tr_pbmn", 0)
    volume      = p.get("volume", 0)
    if price <= 0:
        raise HTTPException(status_code=502, detail="현재가 실측 실패 — 판단 보류")

    vwap, vwap_src = _sup_vwap(acml_pbmn, volume, price)

    # ── 52주 고/저 (수정주가 일봉) — R/R 상단 소스 ──
    _hdr = {
        "content-type": "application/json",
        "authorization": "Bearer " + token,
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010100", "custtype": "P",
    }
    w52l, w52h, _w52src = await _judge_adjusted_52w(code, _hdr)

    # ── 수정주가 일봉 60일 → 이평·스윙저점·장대양봉 ──
    rows    = await _wave_fetch_daily_rows(code, token)
    candles = _sup_build_candles(rows)
    closes  = [k["c"] for k in candles]
    vols    = [k["v"] for k in candles]
    ma5       = _sup_sma(closes, 5)
    ma20      = _sup_sma(closes, 20)
    avg_vol20 = _sup_sma(vols, 20)
    swing20   = min((k["l"] for k in candles[:20] if k["l"] > 0), default=None)
    swing60   = min((k["l"] for k in candles[:60] if k["l"] > 0), default=None)
    pd_open, pd_mid = _sup_find_pungdae(candles, avg_vol20)

    # ── 지지 후보 8종 → 군집 → 등급 → 손절선 → R/R ──
    levels = [
        ("스윙저점", swing20), ("구조저점", swing60),
        ("5일선", ma5), ("20일선", ma20), ("VWAP", vwap),
        ("전일종가", prev_close), ("당일저점", today_low),
        ("장대양봉시가", pd_open), ("장대양봉중간", pd_mid),
    ]
    cluster_tol = _sup_cluster_tol(candles)
    clusters = _sup_cluster(levels, price, cluster_tol)
    grade, grade_reason = _sup_grade(clusters, price)
    swing_high = _sup_swing_high(candles, price)

    # ── Mode = 손절폭 선택 축: 3모드 손절/R/R 전부 계산 ──
    mode_results = {}
    for _m in ("tight", "balance", "loose"):
        _s, _rk, _basis = _sup_stop(clusters, candles, _m, price)
        _r, _t, _src = _sup_rr(price, _s, swing_high, w52h)
        mode_results[_m] = {"stop": _s, "risk_pct": _rk, "rr": _r,
                            "basis": _basis, "target": _t, "target_source": _src}

    cur = mode_results[mode]                       # 요청된 모드 결과
    stop, risk, stop_basis = cur["stop"], cur["risk_pct"], cur["basis"]
    rr, target, target_src = cur["rr"], cur["target"], cur["target_source"]

    # ── 3축 종합: Grade(자리) → tradeable, R/R(효율) 가드 → recommended_mode ──
    tradeable = grade in ("A", "B")
    mode_rrs = {_m: mode_results[_m]["rr"] for _m in mode_results}
    rec_mode, action_label, guard_note = _sup_recommend(grade, mode_rrs)
    rr_gap = round((target - price) / price * 100, 1) if target else None

    # ── 장전·휴장·데이터 부족 가드 ──
    # VWAP 없고 당일 저점도 0 = 장 시작 전 or 휴장 → 당일값 미반영, 판단 보류.
    # (전일종가=현재가가 가짜 지지로 잡혀 tight R/R이 폭주하는 것을 차단)
    data_pending = (vwap is None and (not today_low or today_low <= 0))
    if data_pending:
        tradeable = False
        action_label = "데이터 대기"
        rec_mode = "wait"
        guard_note = "장전·휴장 — 당일 VWAP·저점 미반영, 장중(09:00~15:30) 재확인 필요"

    support1 = clusters[0]["price"] if clusters else None
    support2 = clusters[1]["price"] if len(clusters) >= 2 else None

    # ── 진단서 카드 (병원 차트형) ──
    grade_label = GRADE_LABEL.get(grade, grade)
    diagnosis = "{} ({})".format(grade_label, grade)   # 예: "Recovery 회복가능 (B)"
    if support1:
        rx = "관찰 구간 {:,} ~ {:,} · {} 등급".format(support1, price, grade)
    else:
        rx = "명확한 지지 군집 없음 — 진입 보류"
    contra = ("{:,} 이탈 시 즉시 철수 (Risk {}%)".format(stop, risk)
              if stop else "손절선 산출 불가 — 진입 비추")
    # verdict: Grade · action — R/R 가드 (3축 한 문장)
    if data_pending:
        verdict = "{} · 데이터 대기 — 장전·휴장이라 당일값 미반영, 09:00~15:30 재확인".format(
            grade_label)
    elif action_label == "진입 후보":
        if rec_mode == mode:
            verdict = "{} · 진입 후보 — {} R/R {} 통과".format(grade_label, mode, rr)
        else:
            rec_rr = mode_results[rec_mode]["rr"]
            verdict = "{} · 진입 후보 — {} R/R {} → {} 권장 (R/R {})".format(
                grade_label, mode, rr, rec_mode, rec_rr)
    elif action_label == "눌림 대기":
        verdict = "{} · 눌림 대기 — 전 모드 R/R {} 미만, 추가 눌림 대기".format(
            grade_label, SUPPORT_RR_MIN)
    else:  # 관찰 / 회피 (Grade 브레이크)
        verdict = "{} · 진입 보류 — R/R {} 여도 Grade 브레이크".format(
            grade_label, rr if rr is not None else "-")

    return {
        "engine": "Support Alert v0.4",
        "code": code, "name": name, "sector": sector,
        "mode": mode, "mode_label": SUPPORT_MODE_LABEL[mode],
        "price": price, "prev_close": prev_close,
        "spark": list(reversed(closes[:30])),   # 최근 30일 종가 (이미 받은 일봉 재활용 — KIS 추가 호출 0)
        "vwap": vwap, "vwap_source": vwap_src,
        "above_vwap": (vwap is not None and price >= vwap),
        # 핵심 결과 — 3축: Grade(자리) · R/R(효율) · Mode(손절폭)
        "tradeable": tradeable,
        "data_pending": data_pending,
        "action_label": action_label,
        "recommended_mode": rec_mode,
        "guard_note": guard_note,
        "grade": grade, "grade_label": grade_label, "grade_reason": grade_reason,
        "support1": support1, "support2": support2,
        "stop": stop, "stop_basis": stop_basis, "risk_pct": risk,
        "rr": rr, "rr_target": target, "rr_target_source": target_src,
        "rr_target_gap_pct": rr_gap,
        "rr_min": SUPPORT_RR_MIN,
        "mode_rr": {                               # Mode 축 한눈에 (손절폭별 손익비)
            _m: {"stop": mode_results[_m]["stop"],
                 "risk_pct": mode_results[_m]["risk_pct"],
                 "rr": mode_results[_m]["rr"]}
            for _m in ("tight", "balance", "loose")
        },
        "cluster_tol_pct": round(cluster_tol * 100, 2),
        # 진단서
        "card": {
            "diagnosis": diagnosis,
            "verdict": verdict,
            "prescription": rx,
            "contraindication": contra,
        },
        # 군집 상세 (프론트 표시용 — 가까운 순)
        "clusters": [
            {"price": cl["price"], "count": cl["count"],
             "members": cl["members"], "has_vol_node": cl["has_vol_node"]}
            for cl in clusters
        ],
        "w52_low": w52l, "w52_high": w52h,
        # 실측 근거 (디버그 — 추측 아님을 증명)
        "_facts": {
            "ma5": ma5, "ma20": ma20, "avg_vol20": avg_vol20,
            "swing20": swing20, "swing60": swing60,
            "today_low": today_low, "today_open": today_open,
            "pungdae_open": pd_open, "pungdae_mid": pd_mid,
            "swing_high": swing_high,
            "candles_used": len(candles),
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# 🛰 TRINITY SENTINEL v0.1 — "림빅이 종목을 찾지 않게 만든다." (2026.06.12)
#
# 역할: 후보 탐지기. 자동 매매기가 아님 — 주문 코드 0줄 (헌법).
# 구조: Sentinel → 텔레그램 → 림빅 → Judge 확인 → Strike 확인 → 주문(림빅 손)
#
# 채피 확정 스펙 (2026.06.12):
#  1. 진입 조건: Grade A/B + R/R≥1.5 + tradeable=true (IGNITION 미결합 — v0.1 단순 우선)
#  2. 워치리스트: 서버 저장 (POST/GET /sentinel/watch)
#  3. 중복: 동일 종목·동일 상태 당일 1회. 상태 변화 시 재알림.
#  4. 보유: POST /sentinel/holding (HUB v3 보유 섹션 → 서버 동기화)
#  5. 등급: 🎯후보 / 🟡주의(손절선 3% 이내) / 🔴계약(손절선 이탈)
#  6. 시간: 평일 09:00~15:30 KST만. 90초 주기.
#  7. 제외: 자동주문·LLM·브리핑·차트·음성 — 전부 없음.
#
# 저장소: v0.1 = JSON 임시 (Railway 재배포 시 증발 허용 — 채피 승인).
#         단 load/save 추상화로 감쌈 → v0.2에서 Volume/Supabase 교체 시 함수 내부만 변경.
# SSOT: 모든 숫자는 support_zone() 내부 호출 — 재계산 금지, /judge·/support와 같은 숫자.
# ══════════════════════════════════════════════════════════════════════════════
import json as _sentinel_json

SENTINEL_FILE         = os.environ.get("SENTINEL_FILE", "sentinel_data.json")
SENTINEL_INTERVAL_SEC = 90          # 채피 스펙 6 — scan_loop와 동일 리듬
SENTINEL_WARN_PCT     = 3.0         # 🟡 주의: 손절선 위 3% 이내 (HUB v3 보유 감시와 동일 기준)
SENTINEL_RR_MIN       = 1.5         # 채피 스펙 1
SENTINEL_PAUSE_BETWEEN = 0.5        # 종목 간 호출 간격 (KIS 예의)

# 메모리 상주 상태 (JSON은 백업일 뿐 — 매 루프 파일 I/O 안 함)
_sentinel = {
    "watch":     [],     # ["005930", ...]
    "holdings":  [],     # [{"code": "042700", "buy_price": 123400}, ...]
    "names":     {},     # {"005930": "삼성전자"} — HUB가 전달한 이름 (KIS 빈 이름 대비)
    "alert_log": {},     # {"005930|CANDIDATE": "2026-06-12"} — 당일 중복 방지
    "last_scan": None,   # ISO 시각 (상태 확인용)
    "alerts_sent_today": 0,
    "alerts_date": "",
    "enabled": True,
}

# ── 저장소 추상화 (채피 지시 ② — v0.2 교체 지점은 아래 4개 함수 내부뿐) ──────
def _sentinel_disk_load():
    """파일 → 메모리. 파일 없으면 조용히 빈 상태 유지 (재배포 직후 정상 상황)."""
    try:
        with open(SENTINEL_FILE, "r", encoding="utf-8") as f:
            d = _sentinel_json.load(f)
        _sentinel["watch"]     = list(d.get("watch", []))
        _sentinel["holdings"]  = list(d.get("holdings", []))
        _sentinel["names"]     = dict(d.get("names", {}))
        _sentinel["alert_log"] = dict(d.get("alert_log", {}))
        print(f"[SENTINEL] 📂 저장소 복원 — watch {len(_sentinel['watch'])} · holdings {len(_sentinel['holdings'])}")
    except FileNotFoundError:
        print("[SENTINEL] 📂 저장 파일 없음 — 새 출발 (재배포 후 HUB에서 재등록)")
    except Exception as e:
        print(f"[SENTINEL] ⚠️ 저장소 복원 실패 (빈 상태로 계속): {e}")

def _sentinel_disk_save():
    try:
        with open(SENTINEL_FILE, "w", encoding="utf-8") as f:
            _sentinel_json.dump({
                "watch": _sentinel["watch"],
                "holdings": _sentinel["holdings"],
                "names": _sentinel["names"],
                "alert_log": _sentinel["alert_log"],
            }, f, ensure_ascii=False)
    except Exception as e:
        print(f"[SENTINEL] ⚠️ 저장 실패 (메모리는 유지): {e}")

def load_watchlist() -> list:
    return list(_sentinel["watch"])

def save_watchlist(codes: list):
    _sentinel["watch"] = codes
    _sentinel_disk_save()

def load_holdings() -> list:
    return list(_sentinel["holdings"])

def save_holdings(holds: list):
    _sentinel["holdings"] = holds
    _sentinel_disk_save()

def _sentinel_name(code: str, raw_name: str = "") -> str:
    """이름 3단 폴백: KIS 실측 → HUB가 전달한 이름 → JUDGE_STOCK_MAP → 코드.
    KIS가 hts_kor_isnm을 빈 값으로 주는 경우(NAVER 전례)에도 알림에 이름이 나오게."""
    if raw_name and raw_name != code:
        return raw_name
    n = _sentinel["names"].get(code, "")
    if n:
        return n
    for nm, c in JUDGE_STOCK_MAP.items():
        if c == code:
            return nm
    return code

async def _resolve_name_via_search(code: str) -> str:
    """검색기 연동(2026.06.16) — 이름 캐시가 비었을 때 /search_stock(=네이버 소스)로
    코드→이름을 1회 해소해 _sentinel["names"]에 박제한다.
    · 네이버 소스라 사명변경 종목도 정확 → JUDGE_STOCK_MAP 수동등록 불필요.
    · 담기/동기 시점(async)에서만 호출 → 1회 박제 후엔 캐시로 즉시(재호출 X).
    · 실패해도 조용히 "" 반환 → 기존 폴백(맵→코드)이 그대로 받친다(무중단)."""
    cached = _sentinel["names"].get(code, "")
    if cached:
        return cached
    try:
        res = await search_stock(code)
        for m in (res or {}).get("matches", []):
            if m.get("code") == code and m.get("name") and m["name"] != code:
                _sentinel["names"][code] = m["name"]
                return m["name"]
    except Exception as e:
        print(f"[SENTINEL] 이름 검색 보강 실패 {code}: {type(e).__name__}: {str(e)[:50]}")
    return ""

# ── 중복 알림 정책 (채피 스펙 3) ─────────────────────────────────────────────
def _sentinel_today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")

def _sentinel_should_alert(code: str, state: str) -> bool:
    """동일 종목·동일 상태 = 당일 1회. 상태가 바뀌면 키가 달라져 자연 재알림."""
    key = f"{code}|{state}"
    if _sentinel["alert_log"].get(key) == _sentinel_today():
        return False
    _sentinel["alert_log"][key] = _sentinel_today()
    # 로그 청소: 오늘 아닌 항목 제거 (무한 성장 방지)
    _sentinel["alert_log"] = {k: v for k, v in _sentinel["alert_log"].items() if v == _sentinel_today()}
    _sentinel_disk_save()
    return True

def _sentinel_market_open() -> bool:
    """채피 스펙 6 — 평일 09:00~15:30 KST만. 프리장/NXT/애프터 전부 제외."""
    now_k = datetime.now(KST)
    if now_k.weekday() >= 5:
        return False
    tmin = now_k.hour * 60 + now_k.minute
    return 540 <= tmin <= 930   # 09:00 ~ 15:30

async def _sentinel_send(msg: str):
    if not TG_IGNITION_TOKEN or not TG_IGNITION_CHAT_ID:
        print("[SENTINEL] ⚠️ 텔레그램 환경변수 없음 — 발송 생략")
        return
    await send_telegram(TG_IGNITION_TOKEN, TG_IGNITION_CHAT_ID, msg)
    today = _sentinel_today()
    if _sentinel["alerts_date"] != today:
        _sentinel["alerts_date"] = today
        _sentinel["alerts_sent_today"] = 0
    _sentinel["alerts_sent_today"] += 1

# ── 감시 1회전 ────────────────────────────────────────────────────────────────
async def _sentinel_check_watch(code: str):
    """🎯 후보 감지 — Grade A/B + R/R≥1.5 + tradeable=true (전부 SSOT 실측)."""
    sup = await support_zone(code, mode="balance")
    grade     = str(sup.get("grade") or "").upper()
    rr        = sup.get("rr")
    tradeable = bool(sup.get("tradeable"))
    if grade in ("A", "B") and rr is not None and rr >= SENTINEL_RR_MIN and tradeable:
        if _sentinel_should_alert(code, "CANDIDATE"):
            stop = sup.get("stop")
            risk = sup.get("risk_pct")
            disp = _sentinel_name(code, sup.get("name") or "")
            lines = [
                f"🎯 <b>진입 후보 — {disp}</b>",
                f"Grade <b>{grade}</b> · R/R <b>{rr}</b>",
                f"현재가 {sup.get('price', 0):,}원",
            ]
            if stop:
                risk_txt = f" (-{risk}%)" if risk is not None else ""
                lines.append(f"STOP {stop:,}원{risk_txt}")
            verdict = (sup.get("card") or {}).get("verdict", "")
            if verdict:
                lines.append(f"verdict: {verdict}")
            lines.append("— Judge 확인 후 결정하세요 🔱")
            await _sentinel_send("\n".join(lines))
            print(f"[SENTINEL] 🎯 후보 알림 — {code} (Grade {grade}, R/R {rr})")

async def _sentinel_check_holding(h: dict):
    """🔴 손절선 이탈 / 🟡 3% 이내 근접 — MRI stop 기준 (SSOT)."""
    code = h.get("code", "")
    if not code:
        return
    sup = await support_zone(code, mode="balance")
    price = sup.get("price", 0)
    stop  = sup.get("stop")
    name  = _sentinel_name(code, sup.get("name") or "")
    if not price or not stop:
        return   # 실측 불가 → 추측 알림 금지 (vwap=null 원칙과 동일)
    buy = h.get("buy_price", 0)
    pnl = f" · 평단 대비 {round((price - buy) / buy * 100, 1)}%" if buy else ""
    if price < stop:
        if _sentinel_should_alert(code, "BREACH"):
            await _sentinel_send(
                f"🔴 <b>{name} 손절선 이탈</b>\n"
                f"현재가 {price:,}원 &lt; STOP {stop:,}원{pnl}\n"
                f"— 계약 이행 시점입니다. 직접 확인하세요."
            )
            print(f"[SENTINEL] 🔴 이탈 알림 — {code}")
    elif price <= stop * (1 + SENTINEL_WARN_PCT / 100):
        if _sentinel_should_alert(code, "WARN"):
            await _sentinel_send(
                f"🟡 <b>{name} 손절선 근접</b>\n"
                f"현재가 {price:,}원 · STOP {stop:,}원 (여유 {round((price - stop) / stop * 100, 1)}%){pnl}\n"
                f"— 주의 깊게 지켜보세요."
            )
            print(f"[SENTINEL] 🟡 주의 알림 — {code}")

async def _sentinel_run_once():
    for code in load_watchlist():
        try:
            await _sentinel_check_watch(code)
        except Exception as e:
            print(f"[SENTINEL] ⚠️ watch {code} 실패 (계속): {e}")
        await asyncio.sleep(SENTINEL_PAUSE_BETWEEN)
    for h in load_holdings():
        try:
            await _sentinel_check_holding(h)
        except Exception as e:
            print(f"[SENTINEL] ⚠️ holding {h.get('code')} 실패 (계속): {e}")
        await asyncio.sleep(SENTINEL_PAUSE_BETWEEN)
    _sentinel["last_scan"] = datetime.now(KST).isoformat()

async def sentinel_loop():
    """🛰 장중 보초 — scan_loop·wave_alert_loop와 같은 생존 패턴 (예외 먹고 계속)."""
    print("[SENTINEL] 🛰 Trinity Sentinel v0.1 기동 — 평일 09:00~15:30 KST · 90초")
    _sentinel_disk_load()
    await asyncio.sleep(20)   # 서버 완전 기동 대기 (scan 10s·wave 15s 뒤)
    while True:
        try:
            if not _sentinel["enabled"]:
                await asyncio.sleep(30)
                continue
            if _sentinel_market_open():
                await _sentinel_run_once()
                await asyncio.sleep(SENTINEL_INTERVAL_SEC)
            else:
                await asyncio.sleep(60)
        except Exception as e:
            print(f"[SENTINEL] ⚠️ 루프 예외 (유지됨): {e}")
            await asyncio.sleep(30)

# ── API — HUB v3 동기화 + 운영 확인 ──────────────────────────────────────────
@app.get("/sentinel/watch")
async def sentinel_watch_get():
    w = load_watchlist()
    return {"watch": w, "names": {c: _sentinel_name(c) for c in w}, "count": len(w)}

@app.post("/sentinel/watch")
async def sentinel_watch_set(payload: dict = Body(...)):
    """전체 교체 방식 — HUB v3가 자기 리스트를 통째로 동기화. body: {"codes": ["005930", ...]}"""
    codes = payload.get("codes")
    if not isinstance(codes, list):
        raise HTTPException(status_code=422, detail='body는 {"codes": ["005930", ...]} 형식')
    clean = []
    for c in codes:
        c = str(c).strip()
        if len(c) == 6 and c.isdigit() and c not in clean:
            clean.append(c)
    names = payload.get("names") or {}
    if isinstance(names, dict):
        for c, n in names.items():
            c, n = str(c).strip(), str(n).strip()
            if len(c) == 6 and c.isdigit() and n and n != c:
                _sentinel["names"][c] = n
    # 검색기 연동: HUB가 이름을 안 준 코드는 네이버로 1회 보강 (관심은 최대 5종목)
    for c in clean:
        if not _sentinel["names"].get(c):
            await _resolve_name_via_search(c)
    save_watchlist(clean)   # names도 함께 디스크 저장됨
    return {"ok": True, "watch": clean, "count": len(clean)}

@app.get("/sentinel/holding")
async def sentinel_holding_get():
    hs = [{**h, "name": _sentinel_name(h.get("code", ""))} for h in load_holdings()]
    return {"holdings": hs, "count": len(hs)}

@app.post("/sentinel/holding")
async def sentinel_holding_set(payload: dict = Body(...)):
    """전체 교체 방식. body: {"holdings": [{"code": "042700", "buy_price": 123400}, ...]}"""
    holds = payload.get("holdings")
    if not isinstance(holds, list):
        raise HTTPException(status_code=422, detail='body는 {"holdings": [{"code","buy_price"}]} 형식')
    clean = []
    for h in holds:
        code = str(h.get("code", "")).strip()
        if len(code) == 6 and code.isdigit():
            try:
                bp = int(h.get("buy_price", 0) or 0)
            except (TypeError, ValueError):
                bp = 0
            nm = str(h.get("name", "")).strip()
            if nm and nm != code:
                _sentinel["names"][code] = nm
            elif not _sentinel["names"].get(code):
                # 검색기 연동: HUB가 이름을 못 줬고 캐시에도 없으면 → 네이버로 1회 보강
                await _resolve_name_via_search(code)
            clean.append({"code": code, "buy_price": bp})
    save_holdings(clean)
    return {"ok": True, "holdings": clean, "count": len(clean)}

@app.get("/sentinel/status")
async def sentinel_status():
    return {
        "engine": "Trinity Sentinel v0.1",
        "motto": "림빅이 종목을 찾지 않게 만든다.",
        "enabled": _sentinel["enabled"],
        "market_open_now": _sentinel_market_open(),
        "watch_count": len(_sentinel["watch"]),
        "holdings_count": len(_sentinel["holdings"]),
        "last_scan": _sentinel["last_scan"],
        "alerts_sent_today": _sentinel["alerts_sent_today"] if _sentinel["alerts_date"] == _sentinel_today() else 0,
        "rule": "Grade A/B + R/R≥1.5 + tradeable=true · 평일 09:00~15:30 KST · 90초",
        "storage": "JSON 임시 (v0.1 — 재배포 시 증발 허용, v0.2 Volume/Supabase)",
    }

@app.get("/sentinel/test")
async def sentinel_test():
    """텔레그램 연결 실측 — 더미 후보 알림 1건 발사 (조건 판정 없음)."""
    await _sentinel_send(
        "🛰 <b>Trinity Sentinel v0.1 — 연결 테스트</b>\n"
        "이 메시지가 보이면 보초 교대 완료.\n"
        "장중에는 시스템이 먼저 림빅을 부릅니다 🔱"
    )
    return {"ok": True, "sent": "test alert"}

@app.post("/sentinel/toggle")
async def sentinel_toggle():
    _sentinel["enabled"] = not _sentinel["enabled"]
    return {"ok": True, "enabled": _sentinel["enabled"]}


# ══════════════════════════════════════════════════════════════════════════════
# 📌 Trinity Memo Board v1.0  (HUB v3.9)
#    채피(GPT) 설계  ×  써니(Claude) 구현  ×  림빅 최종결정
#
#    용도: 그날의 발표·이슈·일정 핀보드.
#         림빅 수동 핀 + 채피/써니 자동 등록을 같은 저장소에서 공유.
#    저장: Sentinel과 동일한 JSON 디스크 백업 패턴.
#         (v1 — 재배포 시 증발 허용. v2에서 Volume/Supabase로 교체 지점은 아래 2함수뿐)
#    원칙: 자동 뉴스 수집 없음(채피 지시). 등록은 사람/AI가 명시적으로만.
# ══════════════════════════════════════════════════════════════════════════════
MEMO_FILE = os.environ.get("MEMO_FILE", "memo_data.json")
_memos: list = []   # [{id, date, text, tag, author, ts}]

def _memo_load():
    global _memos
    try:
        with open(MEMO_FILE, "r", encoding="utf-8") as f:
            _memos = _sentinel_json.load(f).get("memos", [])
        print(f"[MEMO] 📂 복원 — {len(_memos)}건")
    except FileNotFoundError:
        _memos = []
        print("[MEMO] 📂 파일 없음 — 새 출발 (재배포 후 HUB에서 재등록)")
    except Exception as e:
        _memos = []
        print(f"[MEMO] ⚠️ 복원 실패 (빈 상태로 계속): {e}")

def _memo_save():
    try:
        with open(MEMO_FILE, "w", encoding="utf-8") as f:
            _sentinel_json.dump({"memos": _memos}, f, ensure_ascii=False)
    except Exception as e:
        print(f"[MEMO] ⚠️ 저장 실패 (메모리는 유지): {e}")

_memo_load()

@app.get("/memo")
async def memo_list(date: str = ""):
    """date 지정 시 해당 날짜만, 없으면 전체. 최신순."""
    items = [m for m in _memos if (not date or m.get("date") == date)]
    items.sort(key=lambda m: m.get("ts", 0), reverse=True)
    return {"memos": items, "count": len(items)}

@app.post("/memo")
async def memo_add(payload: dict = Body(...)):
    """업서트(id 같으면 교체). body: {text, date?, tag?, author?, id?, ts?}
    author 예: 림빅 / 채피 / 써니 — AI 자동 등록도 이 경로 하나로."""
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=422, detail="text 필수")
    now = datetime.now(KST)
    mid = str(payload.get("id") or f"m{int(now.timestamp() * 1000)}")
    memo = {
        "id":     mid,
        "date":   str(payload.get("date") or now.strftime("%Y-%m-%d")),
        "text":   text[:300],
        "tag":    str(payload.get("tag") or "메모")[:10],
        "author": str(payload.get("author") or "림빅")[:10],
        "ts":     int(payload.get("ts") or now.timestamp() * 1000),
    }
    global _memos
    _memos = [m for m in _memos if m.get("id") != mid]   # 업서트
    _memos.append(memo)
    if len(_memos) > 200:                                 # 무한 성장 방지
        _memos = sorted(_memos, key=lambda m: m.get("ts", 0))[-200:]
    _memo_save()
    return {"ok": True, "memo": memo, "count": len(_memos)}

@app.delete("/memo/{mid}")
async def memo_delete(mid: str):
    global _memos
    before = len(_memos)
    _memos = [m for m in _memos if m.get("id") != mid]
    _memo_save()
    return {"ok": True, "removed": before - len(_memos), "count": len(_memos)}

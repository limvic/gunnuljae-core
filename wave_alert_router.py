# wave_alert_router.py
# ─────────────────────────────────────────────────────────────────────────────
# Wave Alert Engine v1.0
# 목적: Universe 종목 중 "거래량 늘면서 상승 시작한 종목"만 텔레그램 알람.
# 철학: "잘 잡는 것"보다 "왜 왔고 왜 안 왔는지 설명 가능".
#
# 역할 분리:
#   Wave Alert = 움직이기 시작한 종목 감지  ← (이 파일)
#   Judge      = 위치/RR/Future Wave 확인
#   Strike     = 최종 진입 타이밍 판단
#
# v1.0 조건 (단순):
#   1. Universe 포함 (MARKETCAP + PROFIT_STABLE + LEADING_SECTOR 합집합)
#   2. 등락률 +2% 이상
#   3. 현재 거래량 >= 20일 평균 거래량 x 2
#   4. 현재가 > 5일선
#   5. 52주 밴드 위치 <= 90%   (수정주가 일봉 기준)
#
# 설계 원칙:
#   - 자기완결형: main_safe import 최소화.
#   - 단, 검증된 기존 함수는 main_safe에서 "주입(inject)" 받아 재사용
#     -> 토큰/현재가/52주수정주가/텔레그램 중복 구현 금지 (가짜값/불일치 방지).
#   - 가짜값 금지. 데이터 없으면 skip + 사유 / 에러는 errors 배열.
#   - 실패 조용히 넘기지 말 것.
#   - 52주 고저는 반드시 수정주가 일봉 (w52_hgpr/w52_lwpr 직접 사용 금지).
#     -> LS ELECTRIC 액면분할 왜곡 재발 방지.
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter

logger = logging.getLogger("wave_alert")
logger.setLevel(logging.INFO)

router = APIRouter()

# ── v1.0 임계값 ──────────────────────────────────────────────────────────────
COND_CHANGE_PCT = 2.0    # 등락률 +2% 이상
COND_VOL_MULT   = 2.0    # 거래량 20일 평균 대비 2배 이상
COND_BAND_MAX   = 90.0   # 52주 밴드 위치 90% 이하
MA_DAYS         = 5      # 5일선
VOL_AVG_DAYS    = 20     # 20일 평균 거래량
REQUEST_DELAY   = 0.12   # 종목 간 delay (KIS 유량 보호). 70종목x2호출 대비.

# ── main_safe 주입 슬롯 ──────────────────────────────────────────────────────
# main_safe.py에서 register_wave_alert(...)로 검증된 함수/상수를 주입한다.
# 직접 import 하지 않는 이유: scan_confirm_router의 'main' import 깨짐 재발 방지.
_DEPS = {
    "get_token":        None,  # async () -> token
    "fetch_price":      None,  # async (code, token) -> {name, price, change_pct, volume, ...}
    "judge_52w":        None,  # async (code, headers) -> (low, high, source)
    "send_telegram":    None,  # async (token, chat_id, msg)
    "is_market_open":   None,  # () -> {"open": bool}
    "build_headers":    None,  # (token) -> dict  (KIS 공통 헤더)
    "fetch_daily_rows": None,  # async (code, token) -> [output2 rows]
    "tg_token":         "",
    "tg_chat_id":       "",
    "kst":              None,
    "universe":         [],    # [(name, code), ...] 합집합
}


def register_wave_alert(*, get_token, fetch_price, judge_52w, send_telegram,
                        is_market_open, build_headers, fetch_daily_rows,
                        tg_token, tg_chat_id, kst, universe):
    """main_safe.py 기동 시 1회 호출하여 검증된 의존성 주입."""
    _DEPS.update(
        get_token=get_token,
        fetch_price=fetch_price,
        judge_52w=judge_52w,
        send_telegram=send_telegram,
        is_market_open=is_market_open,
        build_headers=build_headers,
        fetch_daily_rows=fetch_daily_rows,
        tg_token=tg_token,
        tg_chat_id=tg_chat_id,
        kst=kst,
        universe=universe,
    )
    logger.info(f"[WAVE-ALERT] 의존성 주입 완료 — universe={len(universe)}종목")


def _ready() -> bool:
    """주입 완료 여부 (가짜값 방지: 미주입 시 실행 거부)."""
    required = ("get_token", "fetch_price", "judge_52w", "send_telegram",
                "build_headers", "fetch_daily_rows")
    return all(_DEPS.get(k) for k in required)


# ── 메시지 빌더 ──────────────────────────────────────────────────────────────
def _build_message(a: dict) -> str:
    rr = 100.0 - a["band_pos"]
    return (
        "🌊 Trinity Wave Alert\n\n"
        f"{a['name']} / {a['code']}\n"
        f"{a['price']:,}원 / +{a['change_pct']:.2f}%\n"
        f"거래량: 20일 평균 대비 {a['vol_mult']:.1f}배\n"
        "상태: 파도 발생 감지\n\n"
        "Judge 요약:\n"
        f"Zone: 52주 밴드 {a['band_pos']:.0f}%\n"
        f"R/R: 밴드 여유 {rr:.0f}%\n"
        "Future Wave: 거래량 급증 초기 / 지속성 미확인\n\n"
        "→ 차트 확인 후 Strike로 진입 판단"
    )


# ── 한 종목 검사 ──────────────────────────────────────────────────────────────
async def _check_one(name: str, code: str, token: str) -> tuple:
    fetch_price      = _DEPS["fetch_price"]
    judge_52w        = _DEPS["judge_52w"]
    build_headers    = _DEPS["build_headers"]
    fetch_daily_rows = _DEPS["fetch_daily_rows"]

    # (1) 현재가/등락률/현재거래량 — 검증된 fetch_price 재사용
    p = await fetch_price(code, token)
    if not p or not p.get("price"):
        raise RuntimeError("inquire-price 빈 응답")

    change_pct = float(p.get("change_pct", 0))
    cur_price  = int(p.get("price", 0))
    cur_vol    = int(p.get("volume", 0))
    disp_name  = p.get("name") or name

    # 조건 2: 등락률 +2% 이상
    if change_pct < COND_CHANGE_PCT:
        return ("skip", "below_2pct")

    # (2) 일봉 한 번 호출 -> 5일선 + 20일 평균거래량 (수정주가)
    headers = build_headers(token)
    rows = await fetch_daily_rows(code, token)
    if not rows:
        return ("skip", "daily_empty")

    closes, vols = [], []
    for r in rows:
        c = r.get("stck_clpr")
        v = r.get("acml_vol")
        try:
            c = int(str(c).replace(",", "").strip())
            v = int(str(v).replace(",", "").strip())
        except Exception:
            continue
        if c > 0:
            closes.append(c)
            vols.append(v)

    if len(closes) < MA_DAYS:
        return ("skip", "ma_data_short")
    if len(vols) < VOL_AVG_DAYS:
        return ("skip", "vol_data_short")

    # 조건 3: 거래량 20일 평균 대비 2배 이상
    avg_vol = sum(vols[:VOL_AVG_DAYS]) / VOL_AVG_DAYS
    if avg_vol <= 0:
        return ("skip", "vol_data_bad")
    vol_mult = cur_vol / avg_vol
    if vol_mult < COND_VOL_MULT:
        return ("skip", "low_volume")

    # 조건 4: 현재가 > 5일선
    ma5 = sum(closes[:MA_DAYS]) / MA_DAYS
    if cur_price <= ma5:
        return ("skip", "below_ma5")

    # 조건 5: 52주 밴드 위치 <= 90% — 수정주가 일봉 (검증된 judge_52w 재사용)
    w52_low, w52_high, w52_src = await judge_52w(code, headers)
    if not w52_low or not w52_high or w52_high <= w52_low:
        return ("skip", "band_data_bad")
    band_pos = (cur_price - w52_low) / (w52_high - w52_low) * 100
    if band_pos > COND_BAND_MAX:
        return ("skip", "band_too_high")

    alert = {
        "name":       disp_name,
        "code":       code,
        "price":      cur_price,
        "change_pct": round(change_pct, 2),
        "vol_mult":   round(vol_mult, 2),
        "band_pos":   round(band_pos, 1),
        "w52_source": w52_src,
    }
    return ("pass", alert)


# ── 메인 실행 (수동/스케줄러 공용) ────────────────────────────────────────────
async def run_wave_alert() -> dict:
    kst = _DEPS["kst"]
    started = datetime.now(kst) if kst else datetime.now()

    result = {
        "ts": started.isoformat(),
        "checked": 0, "passed": 0, "sent": 0,
        "skipped": {}, "alerts": [], "errors": [],
    }

    if not _ready():
        result["errors"].append({"stage": "init", "error": "의존성 미주입 (register_wave_alert 안 됨)"})
        logger.error("[WAVE-ALERT] 의존성 미주입 — 실행 거부")
        return result

    get_token     = _DEPS["get_token"]
    send_telegram = _DEPS["send_telegram"]
    universe      = _DEPS["universe"]
    tg_token      = _DEPS["tg_token"]
    tg_chat_id    = _DEPS["tg_chat_id"]

    try:
        token = await get_token()
    except Exception as e:
        result["errors"].append({"stage": "token", "error": str(e)})
        logger.error(f"[WAVE-ALERT] 토큰 실패 -> {e}")
        return result

    for name, code in universe:
        result["checked"] += 1
        try:
            verdict, payload = await _check_one(name, code, token)
            if verdict == "skip":
                result["skipped"][payload] = result["skipped"].get(payload, 0) + 1
                logger.info(f"[SKIP] {name}({code}) -> {payload}")
            else:  # pass
                result["passed"] += 1
                result["alerts"].append(payload)
                logger.info(f"[PASS] {name}({code}) volx{payload['vol_mult']} band{payload['band_pos']}%")
                if not tg_token or not tg_chat_id:
                    result["errors"].append({"code": code, "stage": "telegram", "error": "TG env 미설정"})
                    logger.error(f"[TELEGRAM-FAIL] {name}({code}) -> env 미설정")
                else:
                    try:
                        await send_telegram(tg_token, tg_chat_id, _build_message(payload))
                        result["sent"] += 1
                        logger.info(f"[SENT] {name}({code})")
                    except Exception as te:
                        result["errors"].append({"code": code, "stage": "telegram", "error": str(te)})
                        logger.error(f"[TELEGRAM-FAIL] {name}({code}) -> {te}")
        except Exception as e:
            result["errors"].append({"code": code, "stage": "check", "error": str(e)})
            logger.error(f"[CHECK-FAIL] {name}({code}) -> {e}")

        await asyncio.sleep(REQUEST_DELAY)  # KIS 유량 보호

    end = datetime.now(kst) if kst else datetime.now()
    result["elapsed_sec"] = round((end - started).total_seconds(), 2)
    logger.info(
        f"[WAVE-ALERT DONE] checked={result['checked']} passed={result['passed']} "
        f"sent={result['sent']} skipped={result['skipped']} errors={len(result['errors'])}"
    )
    return result


# ── 엔드포인트 ───────────────────────────────────────────────────────────────
@router.get("/wave-alert/run")
async def wave_alert_run():
    """수동 1회 실행. skipped 사유 분포로 '왜 안 왔는지' 설명 가능."""
    return await run_wave_alert()


@router.get("/wave-alert/ping")
async def wave_alert_ping():
    """주입/환경 상태만 빠르게 점검 (가짜값 없음)."""
    return {
        "ready":          _ready(),
        "universe_count": len(_DEPS.get("universe") or []),
        "telegram_set":   bool(_DEPS.get("tg_token") and _DEPS.get("tg_chat_id")),
    }

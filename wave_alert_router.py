# wave_alert_router.py
# ─────────────────────────────────────────────────────────────────────────────
# Wave Alert Engine v2.2  (이름 폴백 강화 + 배포 확인용 version 노출)
# 목적: Universe 종목을 5개 항목 가중점수(Wave Score 0~100)로 평가해
#       후보별 score + reason 을 통째로 내려준다. 컷(느슨70/보통80/엄격90)은
#       프론트 슬라이더에서 결정 → 백엔드는 점수만, 필터는 클라.
#
# v2.1 → v2.2 변경 (2026.06.12 저녁):
#   - 064350(현대로템) 실전 누락 확인(17:45·19:22 알림) → FALLBACK_NAMES 등록
#   - /wave-alert/ping 에 version·fallback_names·resolve_name_injected 노출
#     → 배포 적용 여부를 URL 한 번으로 실측 가능
#
# v2.0 → v2.1 변경 (2026.06.12):
#   - [버그] 텔레그램 알림에 "024110 / 024110"처럼 이름이 코드로 표시되는 증상.
#     원인: KIS hts_kor_isnm 빈 값 + universe 이름도 코드/빈값인 경우
#           기존 2단 폴백(KIS→universe)이 모두 코드로 떨어짐.
#   - [수정] 이름 4단 폴백으로 확장:
#       ① KIS 실측 이름 → ② universe 이름 → ③ main_safe 주입 resolve_name
#       → ④ 파일 내 FALLBACK_NAMES → ⑤ 최후 코드
#     · resolve_name 주입은 선택(기본 None) — main_safe 수정 없이도 기존과 동일 동작.
#     · 최종적으로 코드로 떨어지면 [NAME-MISS] 경고 로그 → 다음 누락 즉시 발견.
#
# v1.0 → v2.0 변경:
#   - (구) 게이트마다 통과/탈락 하드필터, 통과분만 alert
#   - (신) 5항목 가중점수로 환산, 데이터 있는 전 후보를 candidates[]로 반환
#          · 등락률 / 거래량 배수 / 5일선 위 / 52주 밴드 위치 / Future Wave
#
# 점수 가중치(합 100):
#   거래량 배수 30 · 등락률 20 · 52주 밴드 위치 20 · 5일선 위 15 · Future Wave 15
#
# 설계 원칙(유지):
#   - 자기완결형. 검증된 함수는 main_safe에서 주입 받아 재사용(가짜값/불일치 방지).
#   - 데이터 없으면 skip + 사유. 실패 조용히 넘기지 말 것.
#   - 52주 고저는 반드시 수정주가 일봉(judge_52w). (LS ELECTRIC 액면분할 왜곡 방지)
#   - KIS 유량 보호: +1% 미만은 일봉·밴드 호출 전에 가볍게 컷(어차피 점수 미달).
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import logging
from datetime import datetime
from fastapi import APIRouter

logger = logging.getLogger("wave_alert")
logger.setLevel(logging.INFO)

router = APIRouter()

# ── 점수 가중치 (합 100) ──────────────────────────────────────────────────────
W_VOL  = 30    # 거래량 배수 (핵심 신호)
W_CHG  = 20    # 등락률
W_BAND = 20    # 52주 밴드 위치 (낮을수록 고점 = 여유)
W_MA5  = 15    # 5일선 위
W_FW   = 15    # Future Wave (테마 강도)

FW_NEUTRAL    = 0.6    # Future Wave 미정 시 중립(가중치의 60% — 유니버스는 선도섹터 전제)
TG_SCORE_CUT  = 80     # 텔레그램 발송 점수 컷 (보통)
PRE_SCAN_FLOOR = 1.0   # 등락률 +1% 미만은 일봉 호출 전 컷 (KIS 부하 가드)

MA_DAYS      = 5       # 5일선
VOL_AVG_DAYS = 20      # 20일 평균 거래량
REQUEST_DELAY = 0.12   # 종목 간 delay (KIS 유량 보호)

# ── Future Wave 강도표 (채피 선정 TOP10 · code → 0~100) ───────────────────────
# ⚠️ 채피 확장 영역: 유니버스 전 종목 강도를 채우면 자동 반영됨.
#    미수록 종목은 가짜값 박지 않고 '중립(FW_NEUTRAL)'으로 처리 + reason 표기.
FUTURE_WAVE = {
    "000660": 100,  # SK하이닉스 (1위)
    "034220": 95,   # LG디스플레이 (2위)
    "009150": 90,   # 삼성전기 (3위)
    "042700": 85,   # 한미반도체 (4위)
    "007660": 80,   # 이수페타시스 (5위)
    "267260": 75,   # HD현대일렉트릭 (6위)
    "010120": 70,   # LS ELECTRIC (7위)
    "035420": 60,   # NAVER (9위)
    "307950": 55,   # 현대오토에버 (10위)
    # 두산로보틱스(8위) 등 코드 미확정분은 의도적으로 비움 → 중립 처리
}

# ── 이름 최후 폴백표 (KIS·universe·resolve_name 모두 실패 시) ─────────────────
# ⚠️ 실측 확인된 누락 종목만 등록. 추측 금지.
#    2026.06.12 실전 누락: 024110(16:30 알림) · 064350(17:45·19:22 알림)
FALLBACK_NAMES = {
    "024110": "기업은행",
    "064350": "현대로템",
}

ENGINE_VERSION = "2.2"   # /wave-alert/ping 에 노출 → 배포 확인용

# ── main_safe 주입 슬롯 ──────────────────────────────────────────────────────
_DEPS = {
    "get_token":        None,
    "fetch_price":      None,
    "judge_52w":        None,
    "send_telegram":    None,
    "is_market_open":   None,
    "build_headers":    None,
    "fetch_daily_rows": None,
    "resolve_name":     None,   # 선택 주입: main_safe 3단 폴백(HUB 박제→JUDGE_STOCK_MAP)
    "tg_token":         "",
    "tg_chat_id":       "",
    "kst":              None,
    "universe":         [],
}


def register_wave_alert(*, get_token, fetch_price, judge_52w, send_telegram,
                        is_market_open, build_headers, fetch_daily_rows,
                        tg_token, tg_chat_id, kst, universe,
                        resolve_name=None):
    """main_safe.py 기동 시 1회 호출하여 검증된 의존성 주입.
    resolve_name: code -> 이름 또는 None. 선택 인자 — 안 넘기면 기존과 동일 동작."""
    _DEPS.update(
        get_token=get_token,
        fetch_price=fetch_price,
        judge_52w=judge_52w,
        send_telegram=send_telegram,
        is_market_open=is_market_open,
        build_headers=build_headers,
        fetch_daily_rows=fetch_daily_rows,
        resolve_name=resolve_name,
        tg_token=tg_token,
        tg_chat_id=tg_chat_id,
        kst=kst,
        universe=universe,
    )
    logger.info(f"[WAVE-ALERT] 의존성 주입 완료 — universe={len(universe)}종목 "
                f"resolve_name={'주입됨' if resolve_name else '미주입(폴백표만 사용)'}")


def _ready() -> bool:
    required = ("get_token", "fetch_price", "judge_52w", "send_telegram",
                "build_headers", "fetch_daily_rows")
    return all(_DEPS.get(k) for k in required)


def _clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


# ── 이름 4단 폴백 ─────────────────────────────────────────────────────────────
def _resolve_display_name(kis_name: str, universe_name: str, code: str) -> str:
    """① KIS 실측 → ② universe 이름 → ③ 주입 resolve_name → ④ FALLBACK_NAMES → ⑤ 코드.
    '이름이 코드와 동일'인 값은 이름으로 취급하지 않는다."""
    def _valid(n):
        n = (n or "").strip()
        return n if (n and n != code) else ""

    name = _valid(kis_name) or _valid(universe_name)
    if name:
        return name

    resolver = _DEPS.get("resolve_name")
    if resolver:
        try:
            name = _valid(resolver(code))
            if name:
                return name
        except Exception as e:
            logger.warning(f"[NAME-RESOLVER-FAIL] {code} -> {e}")

    name = _valid(FALLBACK_NAMES.get(code))
    if name:
        return name

    logger.warning(f"[NAME-MISS] {code} — 모든 폴백 실패, 코드로 표시됨. "
                   f"FALLBACK_NAMES 또는 universe 이름 보강 필요.")
    return code


# ── Wave Score 계산 (5항목 가중) ──────────────────────────────────────────────
def _wave_score(change_pct, vol_mult, cur_price, ma5, band_pos, code):
    # 거래량: 1배→0, 2배→23, 2.3배+→만점 (2배가 핵심 신호)
    s_vol  = _clamp((vol_mult - 1) / 1.3) * W_VOL
    # 등락률: 0%→0, +2%→11.4, +3.5%+→만점 (과열 상한)
    s_chg  = _clamp(change_pct / 3.5) * W_CHG
    # 52주 밴드: 낮을수록↑ — 밴드25%↓→만점, 100%→0
    s_band = _clamp((100 - band_pos) / 75) * W_BAND
    # 5일선: 위면 기본 11 + 거리(+3%까지) 4 = 최대 15, 아래면 0 (크로스 보상)
    if ma5 > 0 and cur_price > ma5:
        s_ma5 = 11.0 + _clamp((cur_price / ma5 - 1) / 0.03) * 4.0
    else:
        s_ma5 = 0.0

    fw_raw = FUTURE_WAVE.get(code)
    if fw_raw is None:
        s_fw, fw_note = W_FW * FW_NEUTRAL, "중립(미정)"
    else:
        s_fw, fw_note = (fw_raw / 100.0) * W_FW, str(fw_raw)

    total = s_vol + s_chg + s_band + s_ma5 + s_fw
    parts = {
        "vol":     round(s_vol, 1),
        "change":  round(s_chg, 1),
        "band":    round(s_band, 1),
        "ma5":     round(s_ma5, 1),
        "fw":      round(s_fw, 1),
        "fw_note": fw_note,
    }
    reason = (
        f"거래량 {vol_mult:.1f}배 · {change_pct:+.1f}% · "
        f"밴드 {band_pos:.0f}% · 5일선 {'위' if cur_price > ma5 else '아래'} · "
        f"FW {fw_note}"
    )
    return round(total), parts, reason


# ── 메시지 빌더 ──────────────────────────────────────────────────────────────
def _build_message(a: dict) -> str:
    rr = 100.0 - a["band_pos"]
    return (
        "🌊 Trinity Wave Alert\n\n"
        f"{a['name']} / {a['code']}\n"
        f"{a['price']:,}원 / {a['change_pct']:+.2f}%\n"
        f"Wave Score: {a['score']}점\n"
        f"거래량: 20일 평균 대비 {a['vol_mult']:.1f}배\n"
        "상태: 파도 발생 감지\n\n"
        "Judge 요약:\n"
        f"Zone: 52주 밴드 {a['band_pos']:.0f}%\n"
        f"R/R: 밴드 여유 {rr:.0f}%\n"
        f"근거: {a['reason']}\n\n"
        "→ 차트 확인 후 Strike로 진입 판단"
    )


# ── 한 종목 검사 → 점수화 ─────────────────────────────────────────────────────
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
    # 이름 4단 폴백: KIS 실측 → universe → 주입 resolve_name → FALLBACK_NAMES → 코드
    # (KIS hts_kor_isnm 빈 값 대응: 035420 NAVER · 024110 기업은행 실측 사례)
    disp_name = _resolve_display_name(p.get("name"), name, code)

    # KIS 부하 가드: +1% 미만은 일봉 호출 전 컷 (점수 미달 확실)
    if change_pct < PRE_SCAN_FLOOR:
        return ("skip", "below_floor")

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

    avg_vol = sum(vols[:VOL_AVG_DAYS]) / VOL_AVG_DAYS
    if avg_vol <= 0:
        return ("skip", "vol_data_bad")
    vol_mult = cur_vol / avg_vol
    ma5 = sum(closes[:MA_DAYS]) / MA_DAYS

    # 52주 밴드 위치 — 수정주가 일봉 (검증된 judge_52w 재사용)
    w52_low, w52_high, w52_src = await judge_52w(code, headers)
    if not w52_low or not w52_high or w52_high <= w52_low:
        return ("skip", "band_data_bad")
    band_pos = (cur_price - w52_low) / (w52_high - w52_low) * 100

    score, parts, reason = _wave_score(change_pct, vol_mult, cur_price, ma5, band_pos, code)

    cand = {
        "name":       disp_name,
        "code":       code,
        "price":      cur_price,
        "change_pct": round(change_pct, 2),
        "vol_mult":   round(vol_mult, 2),
        "band_pos":   round(band_pos, 1),
        "above_ma5":  cur_price > ma5,
        "w52_source": w52_src,
        "score":      score,        # ← Wave Score 0~100
        "score_parts": parts,       # ← 항목별 배점 (디버그/표시용)
        "reason":     reason,       # ← 사람이 읽는 근거
    }
    return ("scored", cand)


# ── 메인 실행 (수동/스케줄러 공용) ────────────────────────────────────────────
async def run_wave_alert() -> dict:
    kst = _DEPS["kst"]
    started = datetime.now(kst) if kst else datetime.now()

    result = {
        "ts": started.isoformat(),
        "checked": 0, "scored": 0, "sent": 0,
        "tg_score_cut": TG_SCORE_CUT,
        "skipped": {}, "candidates": [], "alerts": [], "errors": [],
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
            else:  # scored
                result["scored"] += 1
                result["candidates"].append(payload)
                logger.info(f"[SCORED] {name}({code}) score={payload['score']} "
                            f"volx{payload['vol_mult']} band{payload['band_pos']}%")
        except Exception as e:
            result["errors"].append({"code": code, "stage": "check", "error": str(e)})
            logger.error(f"[CHECK-FAIL] {name}({code}) -> {e}")

        await asyncio.sleep(REQUEST_DELAY)  # KIS 유량 보호

    # 점수 내림차순 정렬
    result["candidates"].sort(key=lambda c: c["score"], reverse=True)

    # 텔레그램: 점수 컷(보통 80) 이상만 발송. (legacy 호환 위해 alerts에도 담음)
    result["alerts"] = [c for c in result["candidates"] if c["score"] >= TG_SCORE_CUT]
    if result["alerts"]:
        if not tg_token or not tg_chat_id:
            result["errors"].append({"stage": "telegram", "error": "TG env 미설정"})
            logger.error("[TELEGRAM-FAIL] env 미설정")
        else:
            for a in result["alerts"]:
                try:
                    await send_telegram(tg_token, tg_chat_id, _build_message(a))
                    result["sent"] += 1
                    logger.info(f"[SENT] {a['name']}({a['code']}) score={a['score']}")
                except Exception as te:
                    result["errors"].append({"code": a["code"], "stage": "telegram", "error": str(te)})
                    logger.error(f"[TELEGRAM-FAIL] {a['name']}({a['code']}) -> {te}")

    end = datetime.now(kst) if kst else datetime.now()
    result["elapsed_sec"] = round((end - started).total_seconds(), 2)
    logger.info(
        f"[WAVE-ALERT DONE] checked={result['checked']} scored={result['scored']} "
        f"sent={result['sent']} skipped={result['skipped']} errors={len(result['errors'])}"
    )
    return result


# ── 엔드포인트 ───────────────────────────────────────────────────────────────
@router.get("/wave-alert/run")
async def wave_alert_run():
    """수동 1회 실행. candidates[]에 후보별 score+reason, skipped로 '왜 안 왔는지' 설명."""
    return await run_wave_alert()


@router.get("/wave-alert/ping")
async def wave_alert_ping():
    """주입/환경 상태만 빠르게 점검 (가짜값 없음). version으로 배포 확인."""
    return {
        "version":        ENGINE_VERSION,
        "ready":          _ready(),
        "universe_count": len(_DEPS.get("universe") or []),
        "telegram_set":   bool(_DEPS.get("tg_token") and _DEPS.get("tg_chat_id")),
        "future_wave_mapped": len(FUTURE_WAVE),
        "fallback_names": FALLBACK_NAMES,
        "resolve_name_injected": bool(_DEPS.get("resolve_name")),
    }

"""
🎯 RISK JUDGE v1.1 — Trinity Trader Judge (표준 리포트 + 슬라이더 게이지)
================================================================================
설계: 림빅(결정) × 채피(스펙) × 써니(구현) · 2026.06.22
철학: "우리는 종목을 맞추는 팀이 아니다. 우리는 실수를 줄이는 팀이다."
      계좌 보존 최우선. 현금도 포지션. FOMO 배제. 종목보다 진입 위치.

v1.1 변경 (2026.06.22):
  - R/R 게이트 · 거래당 위험%를 슬라이더로 런타임 조절 (GET/POST /trader/settings)
  - 라우팅 충돌 수정: 정적 라우트(/mode·/settings)를 /{code}보다 먼저 등록
    (/mri/scan을 /mri/{code}보다 먼저 등록하는 원칙과 동일)

원칙:
  - SSOT 재사용: judge_mri / support_zone 내부 호출만. 재계산 0.
  - 실측 우선: 데이터 없는 칸은 null('준비 중'). 가짜값 금지.
  - 독립 모듈: 기존 엔진 미수정. main_safe.py는 register + include 3줄.
  - 실패 격리: 어떤 예외도 본 시스템에 전파 안 됨.
  - Rule A 불변: 판단 도구지 결정자가 아니다. 림빅 NO = 정지.

연결 (main_safe.py, app + judge_mri/support_zone/_judge_resolve 정의 이후):
    from risk_judge import router as trader_router, register_trader
    register_trader(judge_mri=judge_mri, support_zone=support_zone,
                    judge_resolve=_judge_resolve)   # 종목명↔코드 교차검색 재사용
    app.include_router(trader_router)

엔드포인트:
    GET  /trader/settings   현재 R/R게이트·위험% 조회
    POST /trader/settings   조절 (rr_gate 1.0~10.0 · risk_pct 1~10%)
    GET  /trader/mode        현재 작전 모드
    POST /trader/mode        모드 변경 (NORMAL / CASH)
    GET  /trader/{query}     표준 리포트 (종목명 또는 6자리 코드)
"""
from __future__ import annotations

import math
from typing import Optional, Callable, Awaitable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/trader", tags=["trader"])

# ── 고정 상수 ─────────────────────────────────────────────────────────────────
SEED_KRW        = 10_000_000      # 시드 = 계좌 전체
STRATEGY_ALLOC  = 0.60            # 전략매매 총노출 한도 = 60% = 6,000,000원
RR_BONUS        = 3.0             # R/R ≥ 3.0 = 'S급 효율' 보너스 플래그
TREND_TECH_MIN  = 80             # 추세 양호 = MRI 기술축 ≥ 80
VOL_AXIS_MIN    = 50             # 거래량 확인 = MRI 거래량축 ≥ 50
STRATEGY_BUDGET = round(SEED_KRW * STRATEGY_ALLOC)   # 6,000,000

# ── 런타임 조절 (슬라이더) — 기본 R/R 2.0, 위험 2% ──────────────────────────────
RR_GATE_MIN,  RR_GATE_MAX  = 1.0, 10.0
RISK_PCT_MIN, RISK_PCT_MAX = 1, 10          # %
_RR_GATE        = 2.0
_RISK_PCT       = 2                          # % (정수, 1~10)


def _risk_budget() -> int:
    return round(SEED_KRW * (_RISK_PCT / 100.0))


# ── 트레이더 유니버스 (헌법 잠금 — 여기서만 수정하면 됨) ─────────────────────────
TRADER_CORE  = ["005930", "000660", "036930", "267260", "007660"]  # 삼성전자·SK하이닉스·주성엔지니어링·HD현대일렉트릭·이수페타시스
TRADER_WATCH = ["034220"]                                          # LG디스플레이
SCAN_MAX     = 25                                                  # 1회 스캔 종목 상한(KIS 부하·타임아웃 방지)


# ── 의존성 주입 (순환 import 회피 — wave_alert_router 패턴) ──────────────────────
_judge_mri:        Optional[Callable[[str], Awaitable[dict]]] = None
_support_zone:     Optional[Callable[..., Awaitable[dict]]]   = None
_fetch_index_ret5: Optional[Callable[[], Awaitable[Optional[float]]]] = None
_judge_resolve:    Optional[Callable[[str], Optional[str]]]   = None   # 종목명↔코드 교차검색 (SSOT 재사용)
_scan_universe:    Optional[Callable[..., Awaitable[dict]]]   = None   # mri_scan — 67유니버스 1차 스코어링 (Stage1)


def register_trader(*, judge_mri, support_zone, fetch_index_ret5=None,
                    judge_resolve=None, scan_universe=None) -> None:
    global _judge_mri, _support_zone, _fetch_index_ret5, _judge_resolve, _scan_universe
    _judge_mri = judge_mri
    _support_zone = support_zone
    _fetch_index_ret5 = fetch_index_ret5
    _judge_resolve = judge_resolve     # main_safe의 _judge_resolve 주입 — 이름으로도 조회
    _scan_universe = scan_universe      # main_safe의 mri_scan 주입 — 전체 유니버스 2단계 스캔


def _ready() -> bool:
    return _judge_mri is not None and _support_zone is not None


# ── 작전 모드 (인메모리 · 재배포 시 NORMAL 초기화) ──────────────────────────────
_TRADER_MODE = "NORMAL"
MODE_LABEL = {"NORMAL": "평시 모드", "CASH": "현금대기 작전 모드"}


class ModeReq(BaseModel):
    mode: str


class SettingsReq(BaseModel):
    rr_gate:  Optional[float] = None    # 1.0 ~ 10.0
    risk_pct: Optional[int]   = None    # 1 ~ 10 (%)


# ── 보조 계산 (전부 실측 입력 기반) ─────────────────────────────────────────────
def _confidence(mri_score, sup_grade, rr, rr_gate) -> tuple:
    if mri_score is None:
        return ("하", "MRI 점수 미산출 — 판단 보류")
    if mri_score >= 80 and sup_grade == "A" and rr is not None and rr >= RR_BONUS:
        return ("상", "고점수 · A자리 · R/R 보너스 동시 충족")
    if sup_grade in ("A", "B") and rr is not None and rr >= rr_gate:
        return ("중", "자리·R/R 게이트 통과")
    return ("하", "자리 또는 R/R 게이트 미달")


def _hold_days(entry, target, atr_pct, price) -> Optional[int]:
    if not (entry and target and atr_pct and price) or target <= entry:
        return None
    atr_won = atr_pct / 100.0 * price
    if atr_won <= 0:
        return None
    return max(1, math.ceil((target - entry) / atr_won))


def _optimal_entry(price, support1):
    if support1 and 0 < support1 <= price:
        return support1, "지지군집(support1) — 눌림 자리"
    return price, "현재가 — 지지 자리 미형성/이미 자리"


def _size_position(entry, stop, risk_budget):
    if not (entry and stop) or entry <= stop:
        return {"shares": 0, "amount": 0, "risk_won_per_share": None,
                "max_loss_won": 0, "capped": False,
                "note": "손절가 ≥ 진입가 — 위험 산출 불가, 진입 불가"}
    risk_per_share = entry - stop
    shares = int(risk_budget // risk_per_share)
    capped = False
    if shares * entry > STRATEGY_BUDGET:
        shares = int(STRATEGY_BUDGET // entry)
        capped = True
    return {
        "shares": shares,
        "amount": shares * entry,
        "risk_won_per_share": round(risk_per_share),
        "max_loss_won": round(shares * risk_per_share),
        "capped": capped,
        "note": ("전략버킷 600만 한도로 수량 캡" if capped
                 else "거래당 위험 {}%({:,}원) 기준 산출".format(_RISK_PCT, risk_budget)),
    }


# ══════════════════════════════════════════════════════════════════════════════
# ⚠️ 정적 라우트 먼저 (/{code}보다 위 — 라우팅 충돌 방지)
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/settings")
async def get_settings():
    return {
        "rr_gate": _RR_GATE, "risk_pct": _RISK_PCT,
        "risk_budget_krw": _risk_budget(),
        "seed_krw": SEED_KRW, "strategy_budget_krw": STRATEGY_BUDGET,
        "bounds": {"rr_gate": [RR_GATE_MIN, RR_GATE_MAX],
                   "risk_pct": [RISK_PCT_MIN, RISK_PCT_MAX]},
        "note": "인메모리 — 재배포 시 기본(R/R 2.0·위험 2%)으로 초기화",
    }


@router.post("/settings")
async def set_settings(req: SettingsReq):
    global _RR_GATE, _RISK_PCT
    changed = {}
    if req.rr_gate is not None:
        if not (RR_GATE_MIN <= req.rr_gate <= RR_GATE_MAX):
            raise HTTPException(status_code=400,
                                detail="rr_gate는 {}~{}".format(RR_GATE_MIN, RR_GATE_MAX))
        _RR_GATE = round(float(req.rr_gate), 1)
        changed["rr_gate"] = _RR_GATE
    if req.risk_pct is not None:
        if not (RISK_PCT_MIN <= req.risk_pct <= RISK_PCT_MAX):
            raise HTTPException(status_code=400,
                                detail="risk_pct는 {}~{}%".format(RISK_PCT_MIN, RISK_PCT_MAX))
        _RISK_PCT = int(req.risk_pct)
        changed["risk_pct"] = _RISK_PCT
    if not changed:
        raise HTTPException(status_code=400, detail="rr_gate 또는 risk_pct 중 하나는 필요")
    return {"ok": True, "changed": changed,
            "rr_gate": _RR_GATE, "risk_pct": _RISK_PCT,
            "risk_budget_krw": _risk_budget(),
            "message": "적용됨 · R/R 게이트 {} · 거래당 위험 {}%({:,}원)".format(
                _RR_GATE, _RISK_PCT, _risk_budget())}


@router.get("/mode")
async def get_mode():
    return {"mode": _TRADER_MODE, "label": MODE_LABEL[_TRADER_MODE],
            "options": {"NORMAL": "평시 — 조건 충족 시 매수 후보 도출",
                        "CASH": "현금대기 — 신호 무관 신규 진입 동결"},
            "note": "인메모리 — 재배포 시 NORMAL 초기화"}


@router.post("/mode")
async def set_mode(req: ModeReq):
    global _TRADER_MODE
    m = (req.mode or "").strip().upper()
    if m not in ("NORMAL", "CASH"):
        raise HTTPException(status_code=400, detail="mode는 NORMAL 또는 CASH")
    _TRADER_MODE = m
    return {"ok": True, "mode": _TRADER_MODE, "label": MODE_LABEL[m],
            "message": ("현금대기 작전 — 신규 진입 동결" if m == "CASH"
                        else "평시 모드 — 정상 평가 재개")}


# ══════════════════════════════════════════════════════════════════════════════
# 표준 리포트 생성 (단일 조회 · 스캔 공용 헬퍼 — 코드 확정 후 호출)
# ══════════════════════════════════════════════════════════════════════════════
async def _report_for_code(code: str) -> dict:
    rr_gate     = _RR_GATE
    risk_pct    = _RISK_PCT
    risk_budget = _risk_budget()

    # ── 1) SSOT 내부 호출 ─────────────────────────────────────────────────────
    mri = await _judge_mri(code)
    try:
        sup = await _support_zone(code)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail="support_zone 실패: " + type(e).__name__)

    mri_ok    = bool(mri and mri.get("ok"))
    mri_score = mri.get("score") if mri_ok else None
    mri_grade = mri.get("grade") if mri_ok else None
    axes      = (mri.get("axes") or {}) if mri_ok else {}
    detail    = (mri.get("detail") or {}) if mri_ok else {}
    interp    = mri.get("interpretation") if mri_ok else None

    price        = sup.get("price")
    sup_grade    = sup.get("grade")
    rr           = sup.get("rr")
    stop         = sup.get("stop")
    target       = sup.get("rr_target")
    support1     = sup.get("support1")
    support2     = sup.get("support2")
    swing_high   = (sup.get("_facts") or {}).get("swing_high")
    w52h         = sup.get("w52_high")
    vwap         = sup.get("vwap")
    above_vwap   = sup.get("above_vwap")
    data_pending = sup.get("data_pending", False)
    sector       = sup.get("sector", "")
    name         = sup.get("name", code)

    tech_s   = axes.get("tech")
    vol_s    = axes.get("volume")
    supply_s = axes.get("supply")
    atr_pct  = detail.get("atr_pct")

    # ── 2) 추세 ───────────────────────────────────────────────────────────────
    if tech_s is None:
        trend = "준비 중"
    elif tech_s >= 100:
        trend = "상승(정배열)"
    elif tech_s >= 80:
        trend = "상승(ma5>ma20)"
    elif tech_s >= 60:
        trend = "횡보(회복중)"
    else:
        trend = "하락(역배열)"

    # ── 3) 상대강도 — 지수 피드 주입 시만 실측 ─────────────────────────────────
    rs = None
    rs_note = "준비 중 · KOSPI 지수 피드 v2 (가짜값 금지)"
    mom_pct = detail.get("mom_pct")
    if _fetch_index_ret5 is not None and mom_pct is not None:
        try:
            idx5 = await _fetch_index_ret5()
            if idx5 is not None:
                rs = round(mom_pct - idx5, 2)
                rs_note = "종목 5일 − KOSPI 5일 (실측)"
        except Exception:
            rs_note = "지수 조회 실패 — RS 보류"

    industry = {"sector": sector, "strength": None,
                "note": "섹터 강도 수치화는 v2 — 섹터명만 실측 노출"}

    # ── 4) 진입/손절/목표/수량 ────────────────────────────────────────────────
    entry, entry_basis = _optimal_entry(price, support1)
    sizing = _size_position(entry, stop, risk_budget)
    exp_return = (round((target - entry) / entry * 100, 2)
                  if (target and entry and target > entry) else None)
    hold_days = _hold_days(entry, target, atr_pct, price)
    conf_label, conf_reason = _confidence(mri_score, sup_grade, rr, rr_gate)
    rr_bonus = (rr is not None and rr >= RR_BONUS)

    # ── 5) 하드 게이트 ────────────────────────────────────────────────────────
    rr_key = "R/R≥{}".format(rr_gate)
    gate = {
        rr_key:      (rr is not None and rr >= rr_gate),
        "추세양호":   (tech_s is not None and tech_s >= TREND_TECH_MIN),
        "거래량확인":  (vol_s is not None and vol_s >= VOL_AXIS_MIN),
        "자리A/B":    (sup_grade in ("A", "B")),
    }
    gate_pass = all(gate.values())
    gate_fail = [k for k, v in gate.items() if not v]

    # ── 6) 모드 + 게이트 → 최종 포지션 ─────────────────────────────────────────
    mode = _TRADER_MODE
    trade = False
    if mode == "CASH":
        position = "관망"
        basis = "현금대기 작전 모드 — 신규 진입 동결 (기존 보유 관리만)"
    elif data_pending:
        position = "보류"
        basis = "데이터 대기 — 장전·휴장, 09:00~15:30 재확인 필요"
    elif gate_pass:
        position = "매수"
        trade = True
        basis = "4대 게이트 전부 통과 — 진입 후보"
        if rr_bonus:
            basis += " · R/R 보너스(≥3) S급 효율"
    elif sup_grade in ("A", "B"):
        position = "관망"
        basis = "자리는 양호하나 게이트 미달: " + ", ".join(gate_fail) + " — 눌림/조건 대기"
    else:
        position = "보류"
        basis = "거래하지 않음 — 게이트 미달: " + ", ".join(gate_fail)

    if interp:
        basis = basis + " | MRI: " + interp

    # ── 7) 표준 출력 ──────────────────────────────────────────────────────────
    return {
        "engine": "Risk Judge v1.2 · Trinity Trader",
        "code": code, "name": name,
        "operation_mode": {"mode": mode, "label": MODE_LABEL[mode]},
        "settings": {"rr_gate": rr_gate, "risk_pct": risk_pct,
                     "risk_budget_krw": risk_budget},

        "report": {
            "점수": mri_score,
            "등급": mri_grade,
            "포지션": position,
            "근거": basis,
            "진입가격": entry,
            "손절가격": stop,
            "목표가격": target,
            "기대수익률": (str(exp_return) + "%") if exp_return is not None else None,
            "확신도": conf_label,
        },

        "analysis": {
            "추세": trend,
            "산업강도": industry,
            "상대강도": {"rs": rs, "note": rs_note},
            "거래량구조": {"axis_score": vol_s, "vol_ratio": detail.get("vol_ratio")},
            "수급상태": {"axis_score": supply_s,
                       "status": mri.get("supply_status") if mri_ok else None},
            "RR": {"value": rr, "gate": rr_gate, "bonus_3": rr_bonus},
            "주요지지선": {"support1": support1, "support2": support2,
                        "vwap": vwap, "above_vwap": above_vwap},
            "주요저항선": {"swing_high": swing_high, "w52_high": w52h},
            "최적진입가": {"price": entry, "basis": entry_basis},
            "손절가": {"price": stop, "basis": sup.get("stop_basis"),
                     "risk_pct": sup.get("risk_pct")},
            "목표가": {"price": target, "source": sup.get("rr_target_source")},
            "보유예상": {"days": hold_days,
                      "basis": "ATR 기준 대략" if hold_days else "산출 불가"},
            "확신도사유": conf_reason,
        },

        "sizing": {
            "seed_krw": SEED_KRW,
            "risk_pct": risk_pct,
            "risk_budget_krw": risk_budget,
            "strategy_budget_krw": STRATEGY_BUDGET,
            "shares": sizing["shares"] if trade else 0,
            "amount_krw": sizing["amount"] if trade else 0,
            "max_loss_krw": sizing["max_loss_won"] if trade else 0,
            "risk_won_per_share": sizing["risk_won_per_share"],
            "executed": trade,
            "note": sizing["note"] if trade else "비진입 — 수량 미집행 (현금도 포지션)",
        },

        "gate": {"checks": gate, "pass": gate_pass, "fail": gate_fail,
                 "trade": trade,
                 "verdict": "진입 가능" if trade else "거래하지 않음"},

        "rule_a": "이 리포트는 판단 도구. 최종 결정은 림빅. NO = 즉시 정지.",
        "data": {"mri_ok": mri_ok, "support_data_pending": data_pending,
                 "mri_reason": None if mri_ok else mri.get("reason")},
    }


# ══════════════════════════════════════════════════════════════════════════════
# 유니버스 일괄 스캔 + 단일 조회 라우트  (⚠️ 둘 다 정적이거나 /{query} — 순서 주의)
#   라우트 등록 순서: /settings · /mode · /scan  →  /{query} (맨 마지막)
# ══════════════════════════════════════════════════════════════════════════════
def _scan_row(rep: dict) -> dict:
    r, a, g, s = rep["report"], rep["analysis"], rep["gate"], rep["sizing"]
    return {
        "code": rep["code"], "name": rep["name"],
        "포지션": r["포지션"], "점수": r["점수"], "등급": r["등급"],
        "rr": a["RR"]["value"], "rr_bonus": a["RR"]["bonus_3"],
        "진입가": r["진입가격"], "기대수익률": r["기대수익률"], "확신도": r["확신도"],
        "trade": g["trade"], "gate_pass": g["pass"], "gate_fail": g["fail"],
        "수량": s["shares"], "금액": s["amount_krw"],
    }


@router.get("/scan")
async def trader_scan(codes: str = "", group: str = "all", min_score: int = 0, top: int = 15):
    """유니버스 일괄 스캔 — 게이트 통과(매수) 후보를 한 번에.
    codes=005930,000660 (커스텀) · group=core|watch|all|universe.
    group=universe → 2단계: mri_scan(67종목 1차) → 상위 top개만 정밀 판단."""
    if not _ready():
        raise HTTPException(status_code=503, detail="register_trader 미호출 — SSOT 함수 미주입")

    stage1 = None
    if codes.strip():
        raw = [c.strip() for c in codes.split(",") if c.strip()]
        grp = "custom"
    elif group == "universe":
        if _scan_universe is None:
            raise HTTPException(status_code=503,
                                detail="scan_universe 미주입 — register_trader에 mri_scan 연결 필요")
        top = max(1, min(top, SCAN_MAX))
        s1 = await _scan_universe(universe="wave", min_score=min_score, limit=top)  # Stage 1
        items = s1.get("items", []) if isinstance(s1, dict) else []
        raw = [it.get("code") for it in items if it.get("code")]
        grp = "universe"
        stage1 = {"universe_count": (s1.get("count") if isinstance(s1, dict) else None),
                  "passed": (s1.get("passed") if isinstance(s1, dict) else None),
                  "shortlist": len(raw), "min_score": min_score, "top": top,
                  "note": "Stage1 mri_scan(judge_mri SSOT · 5분캐시 · 동시3) → Stage2 정밀판단"}
    elif group == "core":
        raw, grp = list(TRADER_CORE), "core"
    elif group == "watch":
        raw, grp = list(TRADER_WATCH), "watch"
    else:
        raw, grp = list(TRADER_CORE) + list(TRADER_WATCH), "all"
    raw = raw[:SCAN_MAX]

    rows, errors = [], []
    for item in raw:
        try:
            c = _judge_resolve(item) if _judge_resolve is not None else item
            if not c or not (len(c) == 6 and c.isdigit()):
                errors.append({"input": item, "error": "코드 확정 실패"})
                continue
            rep = await _report_for_code(c)      # SSOT 동일 로직 재사용
            rows.append(_scan_row(rep))
        except HTTPException as he:
            errors.append({"input": item, "error": "HTTP " + str(he.status_code)})
        except Exception as e:
            errors.append({"input": item, "error": type(e).__name__})

    # 정렬: 매수(진입 가능) 먼저 → 점수 내림차순(None 뒤로)
    rows.sort(key=lambda x: (0 if x["trade"] else 1,
                             -(x["점수"] if x["점수"] is not None else -1)))

    summary = {
        "scanned": len(rows),
        "매수": sum(1 for x in rows if x["포지션"] == "매수"),
        "관망": sum(1 for x in rows if x["포지션"] == "관망"),
        "보류": sum(1 for x in rows if x["포지션"] == "보류"),
        "error": len(errors),
    }
    return {
        "engine": "Risk Judge v1.2 · Universe Scan",
        "operation_mode": {"mode": _TRADER_MODE, "label": MODE_LABEL[_TRADER_MODE]},
        "settings": {"rr_gate": _RR_GATE, "risk_pct": _RISK_PCT, "risk_budget_krw": _risk_budget()},
        "group": grp, "stage1": stage1, "summary": summary, "results": rows, "errors": errors,
        "rule_a": "스캔은 후보 제시. 최종 결정은 림빅. NO = 즉시 정지.",
    }


@router.get("/{query}")
async def trader_judge(query: str):
    if not _ready():
        raise HTTPException(status_code=503, detail="register_trader 미호출 — SSOT 함수 미주입")
    q = (query or "").strip()
    if _judge_resolve is not None:                # 종목명↔코드 교차검색 재사용
        code = _judge_resolve(q)
        if not code:
            raise HTTPException(status_code=404,
                                detail="'" + q + "' 종목 못 찾음 — 종목명 또는 6자리 코드 확인")
    else:
        code = q
        if not (len(code) == 6 and code.isdigit()):
            raise HTTPException(status_code=400, detail="검색기 미연결 — 6자리 종목코드를 입력하세요")
    return await _report_for_code(code)

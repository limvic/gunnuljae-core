"""
📒 VAVLOG v1.0 — Wave Log (매매 복기 로그)
==========================================
설계: 림빅(결정) × 채피(스펙) × 써니(구현) · 2026.06.15
원칙:
  - 삭제 = 기록. 보유에서 빼는 순간 복기 데이터로 남는다.
  - 실측 우선: 매도가·수량·세후손익은 사용자 입력(토스 실거래).
    세전손익·손익률·보유기간·Rule위반은 서버에서 계산(추측 금지).
  - 영구 저장: Supabase REST (service_role 키, Railway 환경변수). httpx 사용 — 추가 의존성 0.
  - 독립 모듈: 기존 엔진/함수 미수정. main_safe.py는 include_router 2줄만 추가.

연결 (main_safe.py 어디든 app 정의 이후에 2줄):
    from vavlog import router as vavlog_router
    app.include_router(vavlog_router)

환경변수 (Railway):
    SUPABASE_URL, SUPABASE_KEY (service_role)

엔드포인트:
    POST /vavlog          청산 기록 저장
    GET  /vavlog          매매일지 (최신순)
    GET  /vavlog/stats    복기 통계 (?month=YYYY-MM 선택)
"""
from __future__ import annotations
import os
from datetime import date
from typing import Optional

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/vavlog", tags=["vavlog"])

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
TABLE = "vavlog"


def _ready() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "authorization": f"Bearer {SUPABASE_KEY}",
        "content-type": "application/json",
    }


class VavlogEntry(BaseModel):
    code: str
    name: Optional[str] = None
    entry_date: Optional[str] = None     # 'YYYY-MM-DD' (담기 시점)
    exit_date:  Optional[str] = None     # 'YYYY-MM-DD' (청산 시점)
    buy_price:  Optional[float] = None
    sell_price: Optional[float] = None
    qty:        Optional[float] = None
    pnl_net:    Optional[float] = None    # 세후 실현손익 (토스 영수증 실측, 선택)
    exit_reason:   Optional[str] = None   # 목표가도달/손절/Judge경고/시스템종료/수동익절/기타
    entry_reason:  Optional[str] = None
    planned_stop:  Optional[float] = None # 진입 시 계획 STOP (스냅샷)
    entry_grade:   Optional[str] = None   # 진입 스냅샷
    entry_rr:      Optional[float] = None
    entry_score:   Optional[float] = None
    entry_verdict: Optional[str] = None
    emotion:       Optional[str] = None   # 자신감/중립/불안/조급 (선택)
    source: str = "hub"


def _enrich(e: VavlogEntry) -> dict:
    """세전손익·손익률·보유기간·Rule위반 = 서버 실측 계산. 세후는 사용자 입력 그대로."""
    row = e.dict()
    pnl_gross = pnl_pct = None
    if e.buy_price and e.sell_price and e.qty:
        pnl_gross = round((e.sell_price - e.buy_price) * e.qty)
        pnl_pct = round((e.sell_price - e.buy_price) / e.buy_price * 100, 2)
    row["pnl_gross"] = pnl_gross
    row["pnl_pct"] = pnl_pct

    if e.entry_date and e.exit_date:
        try:
            row["hold_days"] = (date.fromisoformat(e.exit_date)
                                - date.fromisoformat(e.entry_date)).days
        except Exception:
            pass

    # Rule 위반 교차검증(실측, 자기기록 왜곡 방지)
    rv = False
    # ① 계획 STOP 이하로 떨어져 청산했는데 사유가 '손절'이 아니면 의심
    if (e.planned_stop and e.sell_price
            and e.sell_price <= e.planned_stop and (e.exit_reason or "") != "손절"):
        rv = True
    # ② '수동익절'이라 했는데 실제론 손실 = 라벨과 결과 불일치
    if (e.exit_reason or "") == "수동익절" and pnl_gross is not None and pnl_gross < 0:
        rv = True
    row["rule_violation"] = rv
    return row


@router.post("")
async def vavlog_save(e: VavlogEntry):
    """청산 기록 저장 (삭제=기록). 반환: 계산된 세전손익·손익률·Rule위반."""
    if not _ready():
        return {"ok": False, "reason": "Supabase 미설정 (SUPABASE_URL/KEY)"}
    row = _enrich(e)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{SUPABASE_URL}/rest/v1/{TABLE}",
                headers={**_headers(), "prefer": "return=representation"},
                json=row,
            )
            r.raise_for_status()
            saved = r.json()
        return {
            "ok": True,
            "saved": saved[0] if isinstance(saved, list) and saved else saved,
            "pnl_gross": row["pnl_gross"],
            "pnl_pct": row["pnl_pct"],
            "rule_violation": row["rule_violation"],
        }
    except Exception as ex:
        return {"ok": False, "reason": f"{type(ex).__name__}: {ex}"}


@router.get("")
async def vavlog_list(limit: int = 200):
    """매매일지 — 최신순 전체."""
    if not _ready():
        return {"ok": False, "reason": "Supabase 미설정", "rows": []}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/{TABLE}",
                headers=_headers(),
                params={
                    "select": "*",
                    "order": "exit_date.desc,created_at.desc",
                    "limit": str(limit),
                },
            )
            r.raise_for_status()
            return {"ok": True, "rows": r.json()}
    except Exception as ex:
        return {"ok": False, "reason": f"{type(ex).__name__}: {ex}", "rows": []}


@router.get("/stats")
async def vavlog_stats(month: Optional[str] = None):
    """복기 통계 — 승률·평균익절/손절·실측손익비·Rule준수율·최고/최악.
    month='YYYY-MM' 주면 그 달만, 없으면 전체."""
    if not _ready():
        return {"ok": False, "reason": "Supabase 미설정"}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/{TABLE}",
                headers=_headers(),
                params={"select": "*", "order": "exit_date.desc"},
            )
            r.raise_for_status()
            rows = r.json()
    except Exception as ex:
        return {"ok": False, "reason": f"{type(ex).__name__}: {ex}"}

    if month:
        rows = [x for x in rows if (x.get("exit_date") or "").startswith(month)]
    closed = [x for x in rows if x.get("pnl_gross") is not None]
    n = len(closed)
    if not n:
        return {"ok": True, "n": 0, "month": month}

    wins = [x for x in closed if x["pnl_gross"] > 0]
    losses = [x for x in closed if x["pnl_gross"] < 0]

    def _avg(xs, k):
        v = [x[k] for x in xs if x.get(k) is not None]
        return round(sum(v) / len(v), 2) if v else None

    avg_win, avg_loss = _avg(wins, "pnl_pct"), _avg(losses, "pnl_pct")
    best = max(closed, key=lambda x: x["pnl_gross"])
    worst = min(closed, key=lambda x: x["pnl_gross"])
    rule_ok = sum(1 for x in closed if not x.get("rule_violation"))

    return {
        "ok": True,
        "month": month,
        "n": n,
        "winrate": round(len(wins) / n * 100, 1),
        "realized_pnl": sum(x["pnl_gross"] for x in closed),
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "rr_realized": round(abs(avg_win / avg_loss), 2) if (avg_win and avg_loss) else None,
        "rule_adherence": round(rule_ok / n * 100, 1),   # Rule 준수율(%)
        "best":  {"name": best.get("name"),  "pnl": best["pnl_gross"]},
        "worst": {"name": worst.get("name"), "pnl": worst["pnl_gross"]},
    }

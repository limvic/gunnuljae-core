"""
📒 TRADER LOG v1.0 — Risk Judge 판단 로그 (복기 자산)
================================================================================
설계: 림빅(결정) × 채피(스펙) × 써니(구현) · 2026.06.22
원칙:
  - 가격이 아니라 '판단'을 남긴다. 관망·보류·매수, 그리고 왜(게이트 사유)까지.
    → "관찰만 하고 넘긴 종목이 그 뒤 어떻게 됐나"를 되짚기 위함.
  - 실측 우선: Risk Judge가 산출한 값 그대로 저장. 추측·가공 금지.
  - 하루 한 종목 1행(upsert): (log_date, code) 유니크 — judge_history와 동일 패턴.
  - 독립 모듈: 기존 엔진/함수 미수정. main_safe.py는 include_router 2줄만.
  - 실패 격리: Supabase 미설정/오류여도 본 시스템 영향 0 (ok:False 반환).

연결 (main_safe.py, app 정의 이후 어디든 2줄):
    from trader_log import router as trader_log_router
    app.include_router(trader_log_router)

환경변수 (Railway · vavlog/judge_history와 공유):
    SUPABASE_URL, SUPABASE_KEY

⚠️ 최초 1회 테이블 생성 필요 (Supabase apply_migration — execute_sql 말고):
    create table if not exists trader_log (
      id bigint generated always as identity primary key,
      log_date date not null,
      code text not null,
      name text,
      position text,           -- 매수 / 관망 / 보류
      score int,
      grade text,
      rr numeric,
      rr_gate numeric,         -- 기록 시점 R/R 게이트
      mode text,               -- NORMAL / CASH
      risk_pct int,
      entry numeric,
      stop numeric,
      target numeric,
      exp_return text,
      confidence text,         -- 상 / 중 / 하
      gate_pass boolean,
      gate_fail text,          -- '추세양호 · 거래량확인'
      trade boolean,
      source text default 'scan',
      note text,
      created_at timestamptz default now(),
      unique (log_date, code)
    );

엔드포인트:
    POST /trader_log              판단 일괄 기록 (스캔 결과 스냅샷)
    GET  /trader_log              로그 조회 (?code= ?date= ?position= ?limit=)
    GET  /trader_log/review/{code} 한 종목의 판단 타임라인 (오래된→최신)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/trader_log", tags=["trader_log"])

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
TABLE = "trader_log"
KST = timezone(timedelta(hours=9))


def _ready() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "authorization": f"Bearer {SUPABASE_KEY}",
        "content-type": "application/json",
    }


def _today_kst() -> str:
    return datetime.now(KST).date().isoformat()


class LogEntry(BaseModel):
    code: str
    name: Optional[str] = None
    position: Optional[str] = None
    score: Optional[int] = None
    grade: Optional[str] = None
    rr: Optional[float] = None
    rr_gate: Optional[float] = None
    mode: Optional[str] = None
    risk_pct: Optional[int] = None
    entry: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    exp_return: Optional[str] = None
    confidence: Optional[str] = None
    gate_pass: Optional[bool] = None
    gate_fail: Optional[str] = None
    trade: Optional[bool] = None
    note: Optional[str] = None


class LogBatch(BaseModel):
    entries: List[LogEntry]
    log_date: Optional[str] = None      # 'YYYY-MM-DD' (없으면 KST 오늘)
    source: str = "scan"                # 'scan' / 'single'


@router.post("")
async def trader_log_save(batch: LogBatch):
    """판단 일괄 기록. (log_date, code) upsert — 같은 날 같은 종목은 최신 판단으로 갱신."""
    if not _ready():
        return {"ok": False, "reason": "Supabase 미설정 (SUPABASE_URL/KEY)"}
    if not batch.entries:
        return {"ok": False, "reason": "entries 비어 있음"}
    log_date = (batch.log_date or "").strip() or _today_kst()
    rows = []
    for e in batch.entries:
        row = e.dict()
        row["log_date"] = log_date
        row["source"] = batch.source
        rows.append(row)
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post(
                f"{SUPABASE_URL}/rest/v1/{TABLE}",
                headers={**_headers(),
                         "prefer": "resolution=merge-duplicates,return=minimal"},
                params={"on_conflict": "log_date,code"},
                json=rows,
            )
            r.raise_for_status()
        return {"ok": True, "saved": len(rows), "log_date": log_date, "source": batch.source}
    except Exception as ex:
        return {"ok": False, "reason": f"{type(ex).__name__}: {ex}"}


@router.get("")
async def trader_log_list(limit: int = 200, code: Optional[str] = None,
                          date: Optional[str] = None, position: Optional[str] = None):
    """판단 로그 — 최신순. code/date(YYYY-MM-DD)/position 필터 선택."""
    if not _ready():
        return {"ok": False, "reason": "Supabase 미설정", "rows": []}
    params = {"select": "*", "order": "log_date.desc,created_at.desc", "limit": str(limit)}
    if code:
        params["code"] = f"eq.{code.strip()}"
    if date:
        params["log_date"] = f"eq.{date.strip()}"
    if position:
        params["position"] = f"eq.{position.strip()}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{SUPABASE_URL}/rest/v1/{TABLE}", headers=_headers(), params=params)
            r.raise_for_status()
            return {"ok": True, "rows": r.json()}
    except Exception as ex:
        return {"ok": False, "reason": f"{type(ex).__name__}: {ex}", "rows": []}


@router.get("/review/{code}")
async def trader_log_review(code: str):
    """한 종목의 판단 타임라인 (오래된→최신) — '그때 뭐라 했고 그 뒤 어떻게 바뀌었나'."""
    if not _ready():
        return {"ok": False, "reason": "Supabase 미설정", "rows": []}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{SUPABASE_URL}/rest/v1/{TABLE}",
                headers=_headers(),
                params={"select": "*", "code": f"eq.{code.strip()}", "order": "log_date.asc"},
            )
            r.raise_for_status()
            rows = r.json()
        # 판단 변화 지점 표시 (관망→매수 등)
        prev = None
        for x in rows:
            x["changed"] = (prev is not None and x.get("position") != prev)
            prev = x.get("position")
        return {"ok": True, "code": code, "count": len(rows), "rows": rows}
    except Exception as ex:
        return {"ok": False, "reason": f"{type(ex).__name__}: {ex}", "rows": []}

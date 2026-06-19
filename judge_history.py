# ══════════════════════════════════════════════════════════════════════════════
# 📈 Judge History v1.0 — Daily Judge Snapshot 적재기 (독립 모듈)
#    채피(GPT) 설계 × 써니(Claude) 구현 × 림빅 최종결정
#
#    철학:  Trinity 1.0 = "현재 상태를 본다"
#           Trinity 2.0 = "상태의 변화를 본다"  ← 이 모듈이 그 연료 탱크
#
#    원칙:
#    1. 본체 무중단 — 이 모듈이 죽어도 main_safe는 정상 (try/except 격리)
#    2. judge_mri(SSOT) 재사용 — 점수 로직 중복 0
#    3. 가짜 데이터 금지 — 적재 실패 시 INSERT 스킵, 로그만
#    4. 평일 16:00 KST 일 1회 — 장중 KIS 호출 부담 없음
#    5. KIS_MODE=real 전제 (mock이면 적재 안 함)
#
#    적재 대상: CORE 3 + WATCH + Wave 유니버스 (중복 제거)
#    테이블:    judge_history (Supabase, VAVLOG와 동일 프로젝트)
# ══════════════════════════════════════════════════════════════════════════════
"""
Supabase 테이블 DDL (한 번만 실행 — SQL Editor):

create table if not exists judge_history (
    id          bigint generated always as identity primary key,
    snap_date   date        not null,
    code        text        not null,
    name        text,
    score       int,
    grade       text,
    supply      int,
    volume      int,
    momentum    int,
    tech        int,
    safety      int,
    supply_status text,
    aligned     boolean,
    note        text,
    created_at  timestamptz default now(),
    unique (snap_date, code)          -- 하루 1종목 1행 (중복 적재 방지)
);
create index if not exists idx_judge_history_code on judge_history (code, snap_date);

필요 환경변수 (Railway):
    SUPABASE_URL  = https://xxxx.supabase.co
    SUPABASE_KEY  = (service_role 키 — VAVLOG와 동일 env. 이미 설정돼 있음)
"""

import os
import asyncio
import httpx
from datetime import datetime

# ── 환경변수 ────────────────────────────────────────────────────────────────
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")   # VAVLOG와 동일 env (service_role)
SNAPSHOT_HOUR = int(os.environ.get("JUDGE_SNAPSHOT_HOUR", "16"))   # 16:00 KST
JH_ENABLED    = os.environ.get("JUDGE_HISTORY_ENABLED", "1") == "1"

# 적재기 상태 (엔드포인트로 노출)
_jh_state = {
    "enabled": JH_ENABLED,
    "last_run": None,
    "last_result": None,
    "last_error": None,
    "configured": bool(SUPABASE_URL and SUPABASE_KEY),
}


def jh_status() -> dict:
    """적재기 현재 상태 — /judge_history/status 에서 사용."""
    return dict(_jh_state)


# ── Supabase INSERT (PostgREST 직접 호출, supabase 패키지 불필요) ─────────────
async def _supabase_upsert(rows: list[dict]) -> dict:
    """judge_history 테이블에 upsert. (snap_date, code) 충돌 시 갱신.
    실패해도 예외를 밖으로 던지지 않고 결과 dict 반환 — 본체 무중단."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return {"ok": False, "reason": "no_supabase_env"}
    if not rows:
        return {"ok": False, "reason": "no_rows"}

    url = f"{SUPABASE_URL}/rest/v1/judge_history"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        # 충돌 시 갱신(같은 날 재실행해도 안전) + 결과 최소화
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            res = await c.post(
                url,
                headers=headers,
                params={"on_conflict": "snap_date,code"},
                json=rows,
            )
        if res.status_code in (200, 201, 204):
            return {"ok": True, "inserted": len(rows)}
        return {"ok": False, "reason": "supabase_error",
                "status": res.status_code, "body": res.text[:200]}
    except Exception as e:
        return {"ok": False, "reason": "exception", "msg": str(e)[:120]}


# ── 적재 대상 종목 수집 ──────────────────────────────────────────────────────
def _collect_targets(code_to_name: dict, wave_universe: list,
                     core_codes: list, extra_codes: list) -> list[tuple]:
    """(name, code) 튜플 리스트를 중복 제거하여 반환.
    CORE → extra(WATCH 등) → Wave 유니버스 순. 코드 기준 dedup."""
    seen = set()
    out = []
    for c in core_codes + extra_codes:
        c = (c or "").strip()
        if len(c) == 6 and c.isdigit() and c not in seen:
            seen.add(c)
            out.append((code_to_name.get(c, ""), c))
    for (nm, c) in wave_universe:
        c = (c or "").strip()
        if len(c) == 6 and c.isdigit() and c not in seen:
            seen.add(c)
            out.append((nm or code_to_name.get(c, ""), c))
    return out


# ── 1회 스냅샷 실행 ──────────────────────────────────────────────────────────
async def run_snapshot(judge_mri, code_to_name: dict, wave_universe: list,
                       kst, core_codes: list, extra_codes: list,
                       kis_mode: str) -> dict:
    """대상 종목들을 judge_mri로 1회씩 돌려 그날치 한 줄씩 적재.
    동시 3개로 KIS rate limit 보호. 실패 종목은 스킵(가짜 데이터 금지)."""
    if kis_mode != "real":
        return {"ok": False, "reason": "kis_not_real", "mode": kis_mode}

    snap_date = datetime.now(kst).strftime("%Y-%m-%d")
    targets = _collect_targets(code_to_name, wave_universe, core_codes, extra_codes)

    sem = asyncio.Semaphore(3)   # KIS 동시 호출 3개 (judge_mri 스캐너와 동일 정책)

    async def _one(name, code):
        async with sem:
            try:
                r = await judge_mri(code)
            except Exception as e:
                return {"_skip": True, "code": code, "err": str(e)[:60]}
            if not r.get("ok"):
                return {"_skip": True, "code": code, "err": r.get("reason", "?")}
            ax = r.get("axes", {})
            det = r.get("detail", {})
            return {
                "snap_date": snap_date,
                "code": code,
                "name": name or r.get("code", code),
                "score": r.get("score"),
                "grade": r.get("grade"),
                "supply":   ax.get("supply"),
                "volume":   ax.get("volume"),
                "momentum": ax.get("momentum"),
                "tech":     ax.get("tech"),
                "safety":   ax.get("safety"),
                "supply_status": r.get("supply_status", ""),
                "aligned":  det.get("aligned"),
                "note":     (r.get("interpretation") or "")[:200],
            }

    results = await asyncio.gather(*[_one(n, c) for (n, c) in targets])
    rows    = [r for r in results if not r.get("_skip")]
    skipped = [r for r in results if r.get("_skip")]

    up = await _supabase_upsert(rows)

    summary = {
        "ok": up.get("ok", False),
        "snap_date": snap_date,
        "targets": len(targets),
        "judged": len(rows),
        "skipped": len(skipped),
        "supabase": up,
        "skipped_detail": skipped[:10],   # 디버깅용 일부만
    }
    return summary


# ── 백그라운드 스케줄러 루프 (평일 16:00 KST 1회) ────────────────────────────
async def judge_history_loop(judge_mri, code_to_name: dict, wave_universe: list,
                             kst, core_codes: list, extra_codes: list,
                             kis_mode: str):
    """평일 16:00 KST 정각 창(16:00~16:09)에 1회 적재. 하루 중복 실행 방지."""
    print(f"[JUDGE-HISTORY] 📈 적재기 기동 — 평일 {SNAPSHOT_HOUR:02d}:00 KST · 일 1회")
    if not _jh_state["configured"]:
        print("[JUDGE-HISTORY] ⚠️ SUPABASE_URL/SERVICE_KEY 미설정 — 적재 대기(상태만 노출)")

    last_run_date = None
    while True:
        try:
            if not _jh_state["enabled"]:
                await asyncio.sleep(60)
                continue

            now = datetime.now(kst)
            today = now.strftime("%Y-%m-%d")
            is_weekday = now.weekday() < 5            # 0=월 … 4=금
            in_window  = (now.hour == SNAPSHOT_HOUR and now.minute < 10)

            if is_weekday and in_window and last_run_date != today:
                print(f"[JUDGE-HISTORY] 🎯 {today} {now.strftime('%H:%M')} KST — 스냅샷 시작")
                res = await run_snapshot(
                    judge_mri, code_to_name, wave_universe,
                    kst, core_codes, extra_codes, kis_mode,
                )
                last_run_date = today
                _jh_state["last_run"] = now.isoformat()
                _jh_state["last_result"] = res
                _jh_state["last_error"] = None if res.get("ok") else res
                print(f"[JUDGE-HISTORY] ✅ 적재 완료 — judged={res.get('judged')} "
                      f"skipped={res.get('skipped')} ok={res.get('ok')}")
                await asyncio.sleep(600)   # 창 벗어날 때까지 대기(중복 방지 이중장치)
            else:
                await asyncio.sleep(60)

        except Exception as e:
            _jh_state["last_error"] = str(e)[:120]
            print(f"[JUDGE-HISTORY] ❌ 루프 예외(무중단): {e}")
            await asyncio.sleep(60)

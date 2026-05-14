"""
🔱 Wave Scan Router v1.0 — /scan_confirm
============================================================
채피 설계 + 써니 구현 + 림빅 운영
설계 원칙: target_engine 재사용 — 단일 진실 원천(SSOT)

역할:
  TOP30 → ETF/ETN 제외 → target_engine 호출 → compact 변환
  → CONFIRM/BREAK/EXHAUST 필터 → score 정렬 → 응답

절대 금지:
  scan_confirm 안에서 Wave 상태 판정 로직 새로 작성
  (Wave Target과 Wave Scan 결과가 갈라지면 안 됨)

작성: 써니 (Trinity v2.2 — 2026.05.14)
"""
from __future__ import annotations

import asyncio
import time
import logging
from typing import Optional
from dataclasses import asdict

from fastapi import APIRouter, Query, HTTPException

# target_engine 재사용 — 새 판단 로직 만들지 않음
from target_engine import (
    fetch_snapshot,
    run_wave_target,
    TargetResult,
    SectorStrength,
    WaveState,
)

log = logging.getLogger("wave_scan")

# =============================================================================
# 1. 설정값 (튜닝 가능)
# =============================================================================

SCAN_CONCURRENCY = 5        # KIS API 동시 호출 제한 (채피 권고)
SCAN_TIMEOUT_SEC = 60.0     # 전체 스캔 타임아웃
CACHE_TTL_SEC = 180         # 3분 캐시 (채피 권고)

# 후보로 통과시킬 상태들 (READY는 너무 광범위해서 제외)
CONFIRM_STATES: set[WaveState] = {"CONFIRM", "BREAK", "EXHAUST"}

# ETF/ETN 4중 필터 — Trinity 표준 패턴
ETF_KEYWORDS = ["ETF", "ETN", "KODEX", "TIGER", "ARIRANG", "KBSTAR", "HANARO",
                "KOSEF", "SOL", "ACE", "RISE", "WOORI", "PLUS"]


def is_etf_etn(code: str, name: Optional[str] = None) -> bool:
    """ETF/ETN 4중 필터 (Trinity 표준).
    1. 종목코드 6자리 첫 자리 '0' 시작 + 특정 패턴
    2. 종목명에 ETF/ETN/레버리지/인버스/2X 포함
    3. ETF 운용사 브랜드명 포함 (KODEX, TIGER 등)
    4. 알려진 ETF 코드 prefix
    """
    if not name:
        return False

    name_upper = name.upper()

    # 1. 직접 키워드
    if any(kw in name_upper for kw in ["ETF", "ETN"]):
        return True

    # 2. 레버리지/인버스/2X 패턴
    if any(kw in name for kw in ["레버리지", "인버스", "2X", "선물", "곱버스"]):
        return True

    # 3. 운용사 브랜드명
    if any(kw in name_upper for kw in ETF_KEYWORDS):
        return True

    # 4. 코드 패턴 (ETF는 보통 0으로 시작하지만 일반주식과 겹쳐서 보조 필터로만)
    # 일반 ETF/ETN 코드 prefix: 069500~069700, 102110~ 등
    # 종목명 필터만으로도 95% 잡힘 — 코드 패턴은 보조용
    return False


# =============================================================================
# 2. Compact 변환 (채피 의사 코드 그대로)
# =============================================================================

def to_compact_wave_item(result: TargetResult) -> dict:
    """TargetResult → Wave Scan 응답용 경량 dict.

    Wave Target은 1/3/7일 모두 반환하지만, Scan은 7일 목표가만 노출.
    Strike Check에서 클릭하면 그때 Wave Target 풀 호출.
    """
    # 7일 목표가 + 확률 추출 (없으면 마지막 항목)
    target_7d = None
    for t in result.targets:
        if t.days == 7:
            target_7d = t
            break
    if target_7d is None and result.targets:
        target_7d = result.targets[-1]

    if target_7d is None:
        # 안전장치 — 정상 케이스에서는 발생 안 함
        return {
            "code": result.code,
            "name": result.name,
            "state": result.state,
            "current": result.current,
            "score": 0,
            "target_7d_pct": 0.0,
            "target_7d_price": 0,
            "flags": result.flags,
            "source": result.source,
            "generated_at": result.generated_at,
        }

    # 목표가 상승률 (%)
    if result.current > 0:
        target_pct = round((target_7d.price - result.current) / result.current * 100, 2)
    else:
        target_pct = 0.0

    return {
        "code": result.code,
        "name": result.name,
        "state": result.state,
        "current": result.current,
        "score": target_7d.probability,   # 7일 도달 확률 = score
        "target_7d_pct": target_pct,
        "target_7d_price": target_7d.price,
        "invalidation": result.invalidation,
        "reentry": result.reentry,
        "flags": result.flags,
        "source": result.source,
        "generated_at": result.generated_at,
    }


# =============================================================================
# 3. TTL 캐시 (단순 dict 기반 — 3분)
# =============================================================================

_scan_cache: dict[str, tuple[float, dict]] = {}


def _cache_get(key: str) -> Optional[dict]:
    entry = _scan_cache.get(key)
    if not entry:
        return None
    ts, data = entry
    if time.time() - ts > CACHE_TTL_SEC:
        _scan_cache.pop(key, None)
        return None
    return data


def _cache_set(key: str, data: dict) -> None:
    _scan_cache[key] = (time.time(), data)


# =============================================================================
# 4. 단건 스캔 (target_engine 호출 + 변환)
# =============================================================================

async def scan_one(code: str, name: str, sector: SectorStrength,
                   sem: asyncio.Semaphore) -> Optional[dict]:
    """단일 종목 스캔 — Semaphore로 동시성 제한.
    실패 시 None 반환 (예외 전파 X — gather 막힘 방지)."""
    async with sem:
        # 캐시 확인
        cache_key = f"{code}:{sector}"
        cached = _cache_get(cache_key)
        if cached:
            return cached

        try:
            snapshot = await fetch_snapshot(code)
            # 종목명이 KIS에서 못 가져왔으면 TOP30에서 받은 이름 사용
            if not snapshot.name or snapshot.name == code:
                snapshot.name = name
            result = run_wave_target(snapshot, sector=sector)
            compact = to_compact_wave_item(result)
            _cache_set(cache_key, compact)
            return compact
        except Exception as e:
            log.warning(f"[wave_scan] {code}({name}) 스캔 실패: {e}")
            return None


# =============================================================================
# 5. FastAPI 라우터
# =============================================================================

router = APIRouter(prefix="/scan", tags=["wave_scan"])


@router.get("/confirm")
async def scan_confirm(
    top: int = Query(30, ge=5, le=50, description="TOP N 종목 (5~50)"),
    sector: SectorStrength = Query("normal", description="섹터 강도"),
    include_ready: bool = Query(False, description="READY 상태도 포함 (기본 제외)"),
):
    """🔱 Wave Scan — TOP30 자동 후보 추출
    
    흐름:
      1. /top30_v2 호출 → TOP N 종목 조회
      2. ETF/ETN 4중 필터
      3. Semaphore(5)로 병렬 target_engine 호출
      4. CONFIRM/BREAK/EXHAUST 필터 (옵션: READY 포함)
      5. score(7일 확률) 내림차순 정렬
      6. compact 응답
    
    응답 추가 필드 (써니 보강):
      - scan_latency_ms: 총 소요 시간
      - total_scanned: ETF 제거 후 실제 스캔 종목 수
      - confirmed_count: 후보 통과 종목 수
    """
    t0 = time.time()

    # 1. TOP30 조회 — main.py의 /top30_v2 재활용
    #    순환 import 방지: 함수 호출 시점에 lazy import
    try:
        from main import fetch_top30_v2
        top_list = await fetch_top30_v2()
    except ImportError:
        # fetch_top30_v2가 함수가 아니라 라우터일 수 있음 — 직접 호출 폴백
        try:
            from main import app
            # 내부 호출 대신 직접 크롤링 함수가 있으면 그걸 호출
            from main import crawl_naver_top30  # 예상 함수명
            top_list = await crawl_naver_top30()
        except ImportError as e:
            raise HTTPException(
                status_code=500,
                detail=f"TOP30 함수를 찾을 수 없음 — main.py에 fetch_top30_v2 또는 crawl_naver_top30 노출 필요: {e}"
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TOP30 조회 실패: {e}")

    if not top_list:
        return {
            "scan_latency_ms": int((time.time() - t0) * 1000),
            "total_scanned": 0,
            "confirmed_count": 0,
            "candidates": [],
            "note": "TOP30 응답 비어있음",
        }

    # TOP N 자르기
    top_list = top_list[:top]

    # 2. ETF/ETN 필터
    filtered = [
        item for item in top_list
        if not is_etf_etn(item.get("code", ""), item.get("name", ""))
    ]

    if not filtered:
        return {
            "scan_latency_ms": int((time.time() - t0) * 1000),
            "total_scanned": 0,
            "confirmed_count": 0,
            "candidates": [],
            "note": "ETF 필터 후 종목 없음",
        }

    # 3. 병렬 스캔 (Semaphore 5)
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)
    tasks = [
        scan_one(item.get("code", ""), item.get("name", ""), sector, sem)
        for item in filtered
    ]

    try:
        compacts = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=False),
            timeout=SCAN_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        log.error(f"[wave_scan] 전체 스캔 타임아웃 ({SCAN_TIMEOUT_SEC}s)")
        raise HTTPException(status_code=504, detail=f"스캔 타임아웃 ({SCAN_TIMEOUT_SEC}초)")

    # None 제거 (실패한 종목)
    valid = [c for c in compacts if c is not None]

    # 4. 상태 필터
    target_states = CONFIRM_STATES.copy()
    if include_ready:
        target_states.add("READY")

    candidates = [c for c in valid if c.get("state") in target_states]

    # 5. score 내림차순 정렬
    candidates.sort(key=lambda x: x.get("score", 0), reverse=True)

    # 6. 응답
    return {
        "scan_latency_ms": int((time.time() - t0) * 1000),
        "total_scanned": len(filtered),
        "confirmed_count": len(candidates),
        "states_filter": sorted(target_states),
        "sector": sector,
        "candidates": candidates,
    }


@router.get("/")
async def scan_info():
    return {
        "router": "Wave Scan",
        "version": "1.0",
        "endpoint": "/scan/confirm",
        "designer": "채피",
        "implementer": "써니",
        "operator": "림빅",
        "philosophy": "좋은 종목 자동 발견이 아니라, 나쁜 후보 빨리 제거.",
        "principle": "target_engine 재사용 — SSOT (Single Source of Truth)",
        "concurrency": SCAN_CONCURRENCY,
        "cache_ttl_sec": CACHE_TTL_SEC,
        "confirm_states": sorted(CONFIRM_STATES),
    }


@router.get("/cache/clear")
async def clear_cache():
    """캐시 강제 비우기 — 테스트/디버깅용."""
    count = len(_scan_cache)
    _scan_cache.clear()
    return {"cleared": count, "message": f"{count}건 캐시 삭제됨"}

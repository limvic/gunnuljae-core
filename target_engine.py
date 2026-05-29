"""
🔱 Wave Target Engine v1.0
==========================
채피 설계 규칙 기반 — 익일/3일/7일 목표가 산출 엔진

핵심 철학:
  목표가를 맞히는 도구가 아니라,
  림빅의 욕심과 공포를 조절하는 운영 계기판.

데이터 소스: KIS OpenAPI 우선 + 네이버 크롤링 폴백
섹터 강도:  림빅 수동 입력 토글 (strong/normal/weak)

작성: 써니 (Trinity v2.1 호환)
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Literal, Optional
from datetime import datetime, timezone, timedelta
import logging

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

log = logging.getLogger("wave_target")

# =============================================================================
# 1. 데이터 모델
# =============================================================================

SectorStrength = Literal["strong", "normal", "weak"]
WaveState = Literal["READY", "BREAK", "CONFIRM", "EXHAUST"]


@dataclass
class StockSnapshot:
    """일봉 기반 종목 스냅샷 — KIS 또는 네이버에서 채워짐."""
    code: str
    name: str
    current: float
    atr14: float
    ma5: float
    ma20: float
    recent_high_20: float
    recent_low_5: float
    today_volume: float
    avg_volume_20: float
    source: str  # "kis" or "naver"

    @property
    def volume_ratio(self) -> float:
        if self.avg_volume_20 <= 0:
            return 1.0
        return self.today_volume / self.avg_volume_20

    @property
    def distance_to_high_pct(self) -> float:
        """전고점까지 거리 (현재가 대비 %)."""
        if self.current <= 0:
            return 0.0
        return (self.recent_high_20 - self.current) / self.current * 100


@dataclass
class TargetPoint:
    days: int
    price: int       # 원 단위 반올림
    probability: int # 20~75
    judgment: str


@dataclass
class TargetResult:
    code: str
    name: str
    current: int
    state: WaveState
    sector: SectorStrength
    targets: list[TargetPoint]
    invalidation: int
    reentry: int
    flags: list[str]
    source: str
    generated_at: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["targets"] = [asdict(t) for t in self.targets]
        return d


# =============================================================================
# 2. 상태 판정 (채피 규칙 §4)
# =============================================================================

def classify_state(s: StockSnapshot) -> WaveState:
    """v1.0: READY / BREAK / CONFIRM / EXHAUST.
    채피 권고대로 우선 4개 다 구현 — 단순하지만 우선순위 명확히."""

    vr = s.volume_ratio
    near_high = s.distance_to_high_pct <= 2.0  # 전고점 2% 이내
    above_high = s.current >= s.recent_high_20

    # EXHAUST: 거래량 폭증 + 전고점 근접 (위꼬리 데이터 v1.0엔 없음, 추후 보강)
    if vr >= 3.0 and near_high:
        return "EXHAUST"

    # BREAK: 전고점 돌파 + 거래량 2배 이상
    if (above_high or near_high) and vr >= 2.0:
        return "BREAK"

    # CONFIRM: 정배열 + 5일선 위 + 거래량 살아있음
    if s.current > s.ma5 > s.ma20 and vr >= 1.2:
        return "CONFIRM"

    # READY: 20일선 위 + 거래량 1.2 이상
    if s.current > s.ma20 and vr >= 1.2:
        return "READY"

    # 그 외에도 v1.0은 READY로 처리 (보수적 기본값)
    return "READY"


# =============================================================================
# 3. ATR 기반 목표가 (채피 규칙 §1)
# =============================================================================

STATE_ATR_BONUS = {
    "READY":   0.0,
    "BREAK":   +0.3,
    "CONFIRM": +0.5,
    "EXHAUST": -0.4,
}

BASE_MULTIPLIERS = {
    1: 0.8,
    3: 1.5,
    7: 2.3,
}


def calc_target_price(current: float, atr: float, days: int, state: WaveState) -> int:
    base = BASE_MULTIPLIERS[days]
    bonus = STATE_ATR_BONUS[state]
    multiplier = base + bonus
    target = current + atr * multiplier
    return int(round(target))


# =============================================================================
# 4. 확률 보정 (채피 규칙 §2)
# =============================================================================

BASE_PROBABILITY = {1: 50, 3: 45, 7: 40}


def calc_probability(s: StockSnapshot, days: int, sector: SectorStrength,
                     flags: list[str]) -> int:
    prob = BASE_PROBABILITY[days]

    # --- 거래량 확장률 ---
    vr = s.volume_ratio
    if vr < 0.8:
        prob -= 8
    elif vr <= 1.2:
        prob += 0
    elif vr <= 2.0:
        prob += 5
    elif vr <= 3.0:
        prob += 10
    else:
        prob += 14
        if "OVERHEAT_RISK" not in flags:
            flags.append("OVERHEAT_RISK")

    # --- 5일선 / 20일선 위치 ---
    if s.current > s.ma5 > s.ma20:
        prob += 8
    elif s.current > s.ma20 and s.current < s.ma5:
        prob += 2
    elif s.current < s.ma20:
        prob -= 10

    if s.ma5 < s.ma20:
        prob -= 6

    # --- 전고점 거리 ---
    dist = s.distance_to_high_pct
    if dist < 3:
        prob -= 5
    elif dist <= 8:
        prob += 3
    elif dist <= 15:
        prob += 6
    else:
        prob += 2

    # --- 섹터 강도 (림빅 수동 입력) ---
    if sector == "strong":
        prob += 6
    elif sector == "weak":
        prob -= 6

    # --- 20~75% 제한 ---
    return max(20, min(75, prob))


def judgment_text(prob: int) -> str:
    if prob >= 60:
        return "단기 도달 가능성 높음"
    if prob >= 50:
        return "조건부 가능"
    if prob >= 40:
        return "관찰"
    return "보수적 접근"


# =============================================================================
# 5. 무효가 / 재평가가 (채피 규칙 §3)
# =============================================================================

def calc_invalidation(s: StockSnapshot, flags: list[str]) -> int:
    """가장 보수적인 값(=가장 가까운 지지선)을 손절선으로."""
    candidates = [
        s.recent_low_5,
        s.ma20,
        s.current - s.atr14 * 1.2,
    ]
    # "가장 보수적" = 현재가에서 가장 가까운 = 가장 높은 지지선
    inv = max(candidates)
    # 단, 현재가보다 위면 무의미 → 두 번째로 보수적인 값
    if inv >= s.current:
        below = [c for c in candidates if c < s.current]
        inv = max(below) if below else s.current - s.atr14

    # 손절폭 과대 플래그
    drop_pct = (s.current - inv) / s.current * 100
    if drop_pct >= 7.0:
        flags.append("WIDE_STOP_LOSS")

    return int(round(inv))


def calc_reentry(s: StockSnapshot) -> int:
    """무너진 종목 즉시 재진입 금지. 5일선 또는 ATR 0.6 눌림 회복 확인 후."""
    reentry = max(s.ma5, s.current - s.atr14 * 0.6)
    return int(round(reentry))


# =============================================================================
# 6. 메인 엔진
# =============================================================================

def run_wave_target(snapshot: StockSnapshot,
                    sector: SectorStrength = "normal",
                    days_list: Optional[list[int]] = None) -> TargetResult:
    """Wave Target Engine v1.0 메인 진입점."""
    if days_list is None:
        days_list = [1, 3, 7]

    flags: list[str] = []
    state = classify_state(snapshot)

    targets: list[TargetPoint] = []
    for d in days_list:
        if d not in BASE_MULTIPLIERS:
            continue
        price = calc_target_price(snapshot.current, snapshot.atr14, d, state)
        prob = calc_probability(snapshot, d, sector, flags)
        targets.append(TargetPoint(
            days=d,
            price=price,
            probability=prob,
            judgment=judgment_text(prob),
        ))

    invalidation = calc_invalidation(snapshot, flags)
    reentry = calc_reentry(snapshot)

    return TargetResult(
        code=snapshot.code,
        name=snapshot.name,
        current=int(round(snapshot.current)),
        state=state,
        sector=sector,
        targets=targets,
        invalidation=invalidation,
        reentry=reentry,
        flags=flags,
        source=snapshot.source,
        generated_at=datetime.now(timezone(timedelta(hours=9))).isoformat(),
    )


# =============================================================================
# 7. 데이터 수집 — KIS 우선 + 네이버 폴백
# =============================================================================

# --- KIS 일봉 파싱 헬퍼 (채피 설계 §7) ---

def _to_int(v) -> int:
    try:
        return int(str(v).replace(",", "").strip())
    except Exception:
        return 0


def _sma(values: list, n: int) -> float:
    if len(values) < n:
        return sum(values) / max(len(values), 1)
    return sum(values[:n]) / n


def _atr14(rows: list) -> float:
    """rows는 최신순. 직전 14거래일 True Range 평균."""
    if len(rows) < 15:
        return 0.0

    trs = []
    for i in range(14):
        today = rows[i]
        prev = rows[i + 1]

        high = _to_int(today.get("stck_hgpr"))
        low = _to_int(today.get("stck_lwpr"))
        prev_close = _to_int(prev.get("stck_clpr"))

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        trs.append(tr)

    return sum(trs) / 14


async def fetch_snapshot_kis(code: str) -> Optional[StockSnapshot]:
    """KIS OpenAPI 일봉 조회 → StockSnapshot 생성.
    채피 §7 + 써니 필드명 매핑 + 림빅 main.py 컨벤션.

    의존성 (main.py에서 import):
      - get_token: KIS 토큰 캐싱 함수
      - BASE_URL: KIS_MODE에 따라 mock/real 자동 분기
      - KIS_APP_KEY, KIS_APP_SECRET: 환경변수

    실패 케이스 → None 반환 → 네이버 폴백.
    """
    # main.py 순환 import 방지: 함수 호출 시점에 lazy import
    try:
        from main import get_token, BASE_URL, KIS_APP_KEY, KIS_APP_SECRET
    except ImportError as e:
        log.warning(f"[wave_target] main.py KIS 함수 import 실패 — 폴백: {e}")
        return None

    try:
        access_token = await get_token()
    except Exception as e:
        log.warning(f"[wave_target] KIS 토큰 획득 실패: {e}")
        return None

    end = datetime.now()
    start = end - timedelta(days=160)

    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"

    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {access_token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST03010100",
        "custtype": "P",
    }

    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(url, headers=headers, params=params)
            res.raise_for_status()
            data = res.json()
    except Exception as e:
        log.warning(f"[wave_target] KIS 호출 실패 ({code}): {e}")
        return None

    # 림빅 main.py 패턴: rt_cd 체크
    if data.get("rt_cd") != "0":
        log.warning(f"[wave_target] KIS rt_cd 오류 ({code}): {data.get('msg1', '')}")
        return None

    rows = data.get("output2", [])
    if not rows or len(rows) < 20:
        log.info(f"[wave_target] KIS 응답 부족 ({code}): {len(rows)}건")
        return None

    # 최신순 정렬 보장 (stck_bsop_date 내림차순)
    rows = sorted(rows, key=lambda x: x.get("stck_bsop_date", ""), reverse=True)

    closes = [_to_int(r.get("stck_clpr")) for r in rows]
    highs = [_to_int(r.get("stck_hgpr")) for r in rows]
    lows = [_to_int(r.get("stck_lwpr")) for r in rows]
    volumes = [_to_int(r.get("acml_vol")) for r in rows]

    if closes[0] <= 0:
        log.warning(f"[wave_target] KIS 현재가 0 ({code})")
        return None

    # output1에 종목명이 있으면 사용, 없으면 코드로
    name = (data.get("output1") or {}).get("hts_kor_isnm") or code

    return StockSnapshot(
        code=code,
        name=name,
        current=float(closes[0]),
        atr14=round(_atr14(rows), 2),
        ma5=round(_sma(closes, 5), 2),
        ma20=round(_sma(closes, 20), 2),
        recent_high_20=float(max(highs[:20])),
        recent_low_5=float(min(lows[:5])),
        today_volume=float(volumes[0]),
        avg_volume_20=round(sum(volumes[:20]) / 20, 2),
        source="kis",
    )


async def fetch_snapshot_naver(code: str) -> Optional[StockSnapshot]:
    """네이버 폴백 — /top30_v2 패턴 재활용.
    User-Agent 필수: Mozilla/5.0 (Linux; Windows NT 10.0) AppleWebKit/537.36"""
    # TODO: BeautifulSoup으로 finance.naver.com/item/sise_day 파싱
    #   주의: Android UA 쓰면 모바일 사이트로 리다이렉트되어 파싱 깨짐
    return None  # 구현은 main.py 통합 시


async def fetch_snapshot(code: str) -> StockSnapshot:
    """KIS 우선, 실패 시 네이버 폴백."""
    snap = await fetch_snapshot_kis(code)
    if snap is not None:
        return snap
    log.warning(f"[wave_target] KIS 실패 → 네이버 폴백: {code}")
    snap = await fetch_snapshot_naver(code)
    if snap is not None:
        return snap
    raise HTTPException(status_code=502, detail=f"종목 데이터 조회 실패: {code}")


# =============================================================================
# 7.5 Trinity VWAP SSOT v1.0 (채피 설계 확정)
# =============================================================================
#
# 원칙:
#   - VWAP = 당일 누적거래대금 / 당일 누적거래량 (KIS inquire-price 실측)
#   - 이것은 거래소 집계 실측값이며 추정이 아니다.
#   - 1분봉 페이지네이션 미사용 (호출량 ↓ · TOP30/Candidate/Ignition AUTO 대응)
#   - 계산 불가 시 vwap=None 반환. 절대 추정값으로 채우지 않는다.
#
# 이 함수는 Strike / Ignition / Candidate 공통 SSOT.
# 프론트 계산 금지. 모든 엔진은 이 값을 /stock 응답으로 받아 쓴다.

_VWAP_CACHE: dict = {}      # { code: {"data": {...}, "ts": float} }
_VWAP_CACHE_TTL = 30        # 채피 §7: 종목별 30초 캐시


def compute_vwap(acml_tr_pbmn, acml_vol, price) -> dict:
    """VWAP 계산 SSOT (단일 공식).

    당일 누적거래대금 / 누적거래량. 거래소 실측값, 추정 아님.
    /stock 핸들러와 get_intraday_vwap()이 공유하는 단 하나의 계산식.
    모든 엔진(Strike/Ignition/Candidate)은 이 공식만 사용한다.

    반환: {"vwap": int|None, "vwap_source": str, "vwap_gap": float|None}
    """
    pbmn = _to_int(acml_tr_pbmn)
    vol  = _to_int(acml_vol)
    px   = _to_int(price)
    if pbmn <= 0 or vol <= 0 or px <= 0:
        return {"vwap": None, "vwap_source": "UNAVAILABLE", "vwap_gap": None}
    vwap = round(pbmn / vol)
    if vwap <= 0:
        return {"vwap": None, "vwap_source": "UNAVAILABLE", "vwap_gap": None}
    return {
        "vwap": vwap,
        "vwap_source": "KIS_INQUIRE_PRICE",
        "vwap_gap": round((px - vwap) / vwap * 100, 2),
    }


async def get_intraday_vwap(code: str) -> dict:
    """당일 누적거래대금 / 누적거래량 기반 실측 VWAP.

    반환:
        {"vwap": 81200, "vwap_source": "KIS_INQUIRE_PRICE", "vwap_gap": 1.23}
    실패:
        {"vwap": None, "vwap_source": "UNAVAILABLE", "vwap_gap": None}

    vwap_gap = (price - vwap) / vwap * 100  (양수=VWAP 위, 음수=아래)
    """
    import time as _time

    # 30초 캐시
    now = _time.time()
    hit = _VWAP_CACHE.get(code)
    if hit and now - hit["ts"] < _VWAP_CACHE_TTL:
        return hit["data"]

    fail = {"vwap": None, "vwap_source": "UNAVAILABLE", "vwap_gap": None}

    # main.py KIS 함수 lazy import (순환 import 방지 — fetch_snapshot_kis와 동일 패턴)
    try:
        from main import get_token, BASE_URL, KIS_APP_KEY, KIS_APP_SECRET
    except ImportError as e:
        log.warning(f"[vwap] main.py KIS import 실패: {e}")
        return fail

    try:
        token = await get_token()
    except Exception as e:
        log.warning(f"[vwap] 토큰 획득 실패 ({code}): {e}")
        return fail

    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010100",
        "custtype": "P",
    }
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                url,
                params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
                headers=headers,
            )
            res.raise_for_status()
            data = res.json()
    except Exception as e:
        log.warning(f"[vwap] KIS 호출 실패 ({code}): {e}")
        return fail

    if data.get("rt_cd") != "0":
        log.warning(f"[vwap] rt_cd 오류 ({code}): {data.get('msg1', '')}")
        return fail

    o = data.get("output") or {}
    price        = o.get("stck_prpr")       # 현재가
    acml_vol     = o.get("acml_vol")         # 누적 거래량 (주)
    acml_pbmn    = o.get("acml_tr_pbmn")     # 누적 거래대금 (원)

    # SSOT 단일 공식 사용 (/stock과 동일)
    result = compute_vwap(acml_pbmn, acml_vol, price)
    if result["vwap"] is None:
        log.info(f"[vwap] 누적 데이터 없음 ({code})")
        return fail

    _VWAP_CACHE[code] = {"data": result, "ts": now}
    return result


# =============================================================================
# 8. FastAPI 라우터
# =============================================================================

router = APIRouter(prefix="/target", tags=["wave_target"])


class TargetRequest(BaseModel):
    code: str
    sector: SectorStrength = "normal"
    days: Optional[str] = "1,3,7"  # 쿼리스트링 호환


@router.get("/{code}")
async def get_target(
    code: str,
    sector: SectorStrength = Query("normal"),
    days: str = Query("1,3,7"),
):
    """🔱 Wave Target Engine v1.0
    예: GET /target/000660?sector=strong&days=1,3,7
    """
    try:
        days_list = [int(x.strip()) for x in days.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="days 파라미터 형식 오류")

    snapshot = await fetch_snapshot(code)
    result = run_wave_target(snapshot, sector=sector, days_list=days_list)
    return result.to_dict()


@router.get("/")
async def info():
    return {
        "engine": "Wave Target Engine",
        "version": "1.0",
        "designer": "채피",
        "implementer": "써니",
        "operator": "림빅",
        "philosophy": "목표가를 맞히는 도구가 아니라, 욕심과 공포를 조절하는 운영 계기판.",
        "supported_days": [1, 3, 7],
        "sectors": ["strong", "normal", "weak"],
        "states": ["READY", "BREAK", "CONFIRM", "EXHAUST"],
    }


# =============================================================================
# 9. 자체 테스트 (개발용)
# =============================================================================

if __name__ == "__main__":
    # 가상 SK하이닉스 스냅샷으로 동작 검증
    test_snap = StockSnapshot(
        code="000660",
        name="SK하이닉스",
        current=215000,
        atr14=5800,
        ma5=212000,
        ma20=205000,
        recent_high_20=220000,
        recent_low_5=210000,
        today_volume=4_500_000,
        avg_volume_20=2_800_000,
        source="test",
    )
    result = run_wave_target(test_snap, sector="strong")
    import json
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))

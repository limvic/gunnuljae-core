"""
🔱 상전조 (Pre-Limit Detector) v1.1-A
================================================
채피 설계 × 써니 구현 × 림빅 실전
"시장이 안 보는 곳에서 반복적으로 터지는 놈을 잡는다"

Version: 1.1-A (2026.04.17)
Author: 써니 (Claude)
Dependencies: main.py의 get_token(), BASE_URL, KIS_APP_KEY, KIS_APP_SECRET
"""

import os
import time
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Literal
from dataclasses import dataclass, asdict, field

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

# ═══════════════════════════════════════════════
# KST 타임존 & 상수
# ═══════════════════════════════════════════════
KST = timezone(timedelta(hours=9))

# 기본 점수 항목별 만점
MAX_VOLUME_SCORE = 30
MAX_PRICE_POSITION_SCORE = 20
MAX_VWAP_SCORE = 15
MAX_PULLBACK_SCORE = 20
MAX_MOMENTUM_SCORE = 15
MAX_BASE_SCORE = 100

# 보너스/가중치
MAX_DIFFUSION_BONUS = 10

# 티어 임계값 (채피 확정)
TIER_DETECTED = 80
TIER_STRONG = 90
TIER_IGNITION = 95  # ← 채피 추가 요청: "점화직전"

# 유니버스 (채피 확정: 시장별 각 30 제외)
UNIVERSE_TOP_N = 200
EXCLUDE_KOSPI_TOP = 30
EXCLUDE_KOSDAQ_TOP = 30

# 필터 (채피 보정 반영)
MIN_TRADE_VALUE_EARLY = 3_000_000_000   # 9:10 이전 30억
MIN_TRADE_VALUE_NORMAL = 5_000_000_000  # 9:10 이후 50억
MAX_FOREIGN_RATIO = 35.0  # 30% → 35%로 완화
DIFFUSION_ACTIVATION_MIN = 9 * 60 + 15  # 9:15 이전 비활성

# 알림 쿨다운
ALERT_COOLDOWN_SECONDS = 600  # 10분
MAX_ALERTS_PER_STOCK_DAILY = 3

# ═══════════════════════════════════════════════
# 🛡️ v1.1-A.1 안정성 레이어 (채피 지시사항)
# ═══════════════════════════════════════════════
DEBUG_MODE = os.environ.get("DEBUG", "false").lower() == "true"


class ErrorType:
    """에러 타입 분류 (채피 P4)"""
    TOKEN_ERROR = "token_error"
    API_ERROR = "api_error"
    DATA_ERROR = "data_error"
    UNKNOWN_ERROR = "unknown_error"


# Mock 데이터 템플릿 (채피 P2)
MOCK_STOCK_DATA = {
    "price": 0,
    "is_bullish": False,
    "pullback_pct": 0.0,
    "low": 0,
}

MOCK_TOP_TRADE_ROW = {
    "mksc_shrn_iscd": "000000",
    "hts_kor_isnm": "MOCK",
    "stck_prpr": "0",
    "prdy_ctrt": "0",
    "acml_vol": "0",
    "prdy_vol": "0",
    "acml_tr_pbmn": "0",
}


async def safe_get_token(get_token_fn) -> Optional[str]:
    """
    토큰 발급 안전 래퍼 (채피 P1)
    실패 시 예외 대신 None 반환 → 서버 죽지 않음
    """
    try:
        token = await get_token_fn()
        return token if token else None
    except HTTPException as e:
        if DEBUG_MODE:
            print(f"[TOKEN_ERROR] HTTPException: {e.detail}")
        return None
    except Exception as e:
        if DEBUG_MODE:
            print(f"[TOKEN_ERROR] {type(e).__name__}: {e}")
        return None


def build_error_response(
    error_type: str,
    message: str,
    hint: Optional[str] = None,
) -> dict:
    """채피 P4: 구조화된 에러 응답"""
    payload = {
        "status": "error",
        "error_type": error_type,
        "message": message,
        "timestamp": now_kst().isoformat() if _time_helpers_ready() else None,
    }
    if hint:
        payload["hint"] = hint
    if DEBUG_MODE:
        payload["debug"] = True
    return payload


def _time_helpers_ready() -> bool:
    """now_kst가 정의됐는지 체크 (순환 import 방지)"""
    return "now_kst" in globals()


def build_empty_scan_response(reason: str, error_type: str) -> dict:
    """
    채피 P2 + P5: scan 실패 시에도 '항상 응답하는 API'
    구조 유지 + 빈 결과 + 에러 메타
    """
    n = datetime.now(KST)
    return {
        "scanned_at": n.isoformat(),
        "total_scanned": 0,
        "excluded_count": 0,
        "diffusion": {
            "timestamp": n.isoformat(),
            "top10_trade_value": 0,
            "total_trade_value": 0,
            "concentration": 0.0,
            "diffusion": 0.0,
            "interpretation": f"[mock] {reason}",
            "bonus_applied": 0,
            "active": False,
        },
        "time_multiplier": time_multiplier(n),
        "candidates": [],
        "status": "fallback",
        "error_type": error_type,
        "message": reason,
    }

# ═══════════════════════════════════════════════
# 데이터 모델
# ═══════════════════════════════════════════════
class ScoreBreakdown(BaseModel):
    volume_score: int
    price_position_score: int
    vwap_score: int
    pullback_score: int
    momentum_score: int
    base_score: int
    diffusion_bonus: int
    time_multiplier: float
    final_score: int


class StockCandidate(BaseModel):
    code: str
    name: str
    current_price: int
    change_rate: float
    volume: int
    volume_ratio: float
    vwap: float
    trade_value: int
    market_cap: Optional[int] = None
    foreign_ratio: Optional[float] = None
    breakdown: ScoreBreakdown
    final_score: int
    tier: Literal["탐지", "강력후보", "점화직전", "제외"]
    excluded_reason: Optional[str] = None
    detected_at: str


class MarketDiffusion(BaseModel):
    timestamp: str
    top10_trade_value: int
    total_trade_value: int
    concentration: float
    diffusion: float
    interpretation: str
    bonus_applied: int
    active: bool  # 9:15 이전이면 False


class ScanResult(BaseModel):
    scanned_at: str
    total_scanned: int
    excluded_count: int
    diffusion: MarketDiffusion
    time_multiplier: float
    candidates: list[StockCandidate]


# ═══════════════════════════════════════════════
# 유틸리티
# ═══════════════════════════════════════════════
def now_kst() -> datetime:
    return datetime.now(KST)


def current_minute_of_day(now: Optional[datetime] = None) -> int:
    """오늘 자정 기준 몇 분째인지 (KST)"""
    n = now or now_kst()
    return n.hour * 60 + n.minute


def time_multiplier(now: Optional[datetime] = None) -> float:
    """시간대 가중치 (채피 확정)"""
    total_min = current_minute_of_day(now)
    if 540 <= total_min < 600:      # 09:00 ~ 10:00
        return 1.0
    elif 600 <= total_min < 660:    # 10:00 ~ 11:00
        return 0.7
    else:                            # 11:00 이후 또는 장전
        return 0.5


def min_trade_value_threshold(now: Optional[datetime] = None) -> int:
    """9:10 이전 30억, 이후 50억 (채피 확정)"""
    total_min = current_minute_of_day(now)
    if total_min < 9 * 60 + 10:
        return MIN_TRADE_VALUE_EARLY
    return MIN_TRADE_VALUE_NORMAL


# ═══════════════════════════════════════════════
# 주의력 분산도 계산
# ═══════════════════════════════════════════════
def calculate_diffusion_bonus(concentration: float) -> int:
    """집중도가 높을수록 저가주 보너스 ↑"""
    if concentration > 0.45:
        return 10
    elif concentration > 0.35:
        return 7
    elif concentration > 0.25:
        return 3
    else:
        return 0


def interpret_concentration(concentration: float) -> str:
    if concentration > 0.45:
        return "극심한 쏠림 — 저가주 기회 최대"
    elif concentration > 0.35:
        return "중간 쏠림 — 저가주 관찰 유효"
    elif concentration > 0.25:
        return "약한 쏠림 — 일반 관찰"
    else:
        return "분산 시장 — 특이사항 없음"


def compute_market_diffusion(
    top10_values: list[int],
    total_value: int,
    now: Optional[datetime] = None,
) -> MarketDiffusion:
    """시장 주의력 분산도 지수 계산"""
    n = now or now_kst()
    total_min = current_minute_of_day(n)
    active = total_min >= DIFFUSION_ACTIVATION_MIN  # 9:15 이전 비활성

    top10_sum = sum(top10_values) if top10_values else 0
    concentration = (top10_sum / total_value) if total_value > 0 else 0.0
    diffusion_score = 1.0 - concentration
    bonus = calculate_diffusion_bonus(concentration) if active else 0

    return MarketDiffusion(
        timestamp=n.isoformat(),
        top10_trade_value=top10_sum,
        total_trade_value=total_value,
        concentration=round(concentration, 4),
        diffusion=round(diffusion_score, 4),
        interpretation=interpret_concentration(concentration) if active else "9:15 이전 비활성",
        bonus_applied=bonus,
        active=active,
    )


# ═══════════════════════════════════════════════
# 점수 엔진 - 기본 5개 항목
# ═══════════════════════════════════════════════
def score_volume(volume_ratio: float) -> int:
    """전일 동시간대 대비 거래량 배수"""
    if volume_ratio >= 2.0:
        return 30
    elif volume_ratio >= 1.5:
        return 20
    elif volume_ratio >= 1.2:
        return 10
    return 0


def score_price_position(change_rate: float) -> int:
    """등락률 구간별 점수 (채피 세분화안 반영)"""
    if 3.0 <= change_rate < 7.0:
        return 20
    elif 7.0 <= change_rate < 12.0:
        return 15
    elif 12.0 <= change_rate < 15.0:
        return 10
    return 0


def score_vwap(current_price: float, vwap: float) -> int:
    """VWAP 상단/근접/하단"""
    if vwap <= 0:
        return 0
    diff_pct = (current_price - vwap) / vwap * 100
    if diff_pct >= 0.3:
        return 15
    elif -0.3 <= diff_pct < 0.3:
        return 5
    return 0


def score_pullback(min5_low_holding: bool, wiggle: bool) -> int:
    """5분봉 저점 유지 여부"""
    if min5_low_holding:
        return 20
    elif wiggle:
        return 10
    return 0


def score_momentum(
    recent_3d_surge: bool,
    healthy_sideways: bool,
) -> int:
    """최근 3일 급등 + 건전한 횡보"""
    if recent_3d_surge and healthy_sideways:
        return 15
    elif recent_3d_surge:
        return 5
    return 0


@dataclass
class StockRawData:
    """KIS에서 받아오는 원시 데이터 (점수 계산 입력)"""
    code: str
    name: str
    current_price: int
    change_rate: float
    volume: int
    volume_ratio: float          # 전일 동시간 대비
    vwap: float
    trade_value: int             # 거래대금
    market_cap: Optional[int] = None
    market_cap_rank: Optional[int] = None
    market_type: Literal["KOSPI", "KOSDAQ"] = "KOSPI"
    foreign_ratio: Optional[float] = None
    min5_low_holding: bool = False
    min5_wiggle: bool = False
    recent_3d_surge: bool = False
    healthy_sideways: bool = False
    volume_declining: bool = False
    upper_tail_count: int = 0
    vi_triggered: bool = False
    post_vi_drop: float = 0.0


def compute_base_score(raw: StockRawData) -> tuple[int, dict]:
    """기본 점수 100점 계산 + 세부 내역"""
    v = score_volume(raw.volume_ratio)
    p = score_price_position(raw.change_rate)
    w = score_vwap(float(raw.current_price), raw.vwap)
    pb = score_pullback(raw.min5_low_holding, raw.min5_wiggle)
    m = score_momentum(raw.recent_3d_surge, raw.healthy_sideways)
    total = v + p + w + pb + m
    breakdown = {
        "volume_score": v,
        "price_position_score": p,
        "vwap_score": w,
        "pullback_score": pb,
        "momentum_score": m,
        "base_score": total,
    }
    return total, breakdown


# ═══════════════════════════════════════════════
# 유니버스 필터 (대장주 제외)
# ═══════════════════════════════════════════════
def check_exclusion(
    raw: StockRawData,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """OR 조건 — 하나라도 걸리면 제외"""
    # 1) 시총 상위 제외 (시장별 각 30)
    if raw.market_cap_rank is not None:
        if raw.market_type == "KOSPI" and raw.market_cap_rank <= EXCLUDE_KOSPI_TOP:
            return True, f"KOSPI시총상위{EXCLUDE_KOSPI_TOP}"
        if raw.market_type == "KOSDAQ" and raw.market_cap_rank <= EXCLUDE_KOSDAQ_TOP:
            return True, f"KOSDAQ시총상위{EXCLUDE_KOSDAQ_TOP}"

    # 2) 외국인 비중 (35% 완화 반영)
    if raw.foreign_ratio is not None and raw.foreign_ratio >= MAX_FOREIGN_RATIO:
        return True, f"외국인비중{raw.foreign_ratio:.1f}%"

    # 3) +15% 이상 + 거래량 감소 + 윗꼬리 2회↑
    if (raw.change_rate >= 15.0
        and raw.volume_declining
        and raw.upper_tail_count >= 2):
        return True, "과열후약화"

    # 4) 거래대금 부족 (시간대별 기준)
    min_value = min_trade_value_threshold(now)
    if raw.trade_value < min_value:
        return True, f"거래대금부족({min_value/1e8:.0f}억↓)"

    # 5) VI 후 급락
    if raw.vi_triggered and raw.post_vi_drop > 3.0:
        return True, "VI후급락"

    return False, ""


# ═══════════════════════════════════════════════
# 티어 판정
# ═══════════════════════════════════════════════
def determine_tier(
    final_score: int,
    excluded: bool,
) -> Literal["탐지", "강력후보", "점화직전", "제외"]:
    if excluded:
        return "제외"
    if final_score >= TIER_IGNITION:
        return "점화직전"
    elif final_score >= TIER_STRONG:
        return "강력후보"
    elif final_score >= TIER_DETECTED:
        return "탐지"
    return "제외"  # 80점 미만은 후보 아님


# ═══════════════════════════════════════════════
# 종합 스코어링 (단일 종목)
# ═══════════════════════════════════════════════
def score_stock(
    raw: StockRawData,
    diffusion_bonus: int,
    now: Optional[datetime] = None,
) -> StockCandidate:
    n = now or now_kst()

    # 제외 필터 먼저
    excluded, reason = check_exclusion(raw, n)

    # 점수 계산
    base_score, breakdown = compute_base_score(raw)
    t_mult = time_multiplier(n)

    if excluded:
        final = 0
    else:
        raw_final = (base_score + diffusion_bonus) * t_mult
        final = min(int(raw_final), 100)

    tier = determine_tier(final, excluded)

    breakdown["diffusion_bonus"] = diffusion_bonus if not excluded else 0
    breakdown["time_multiplier"] = t_mult
    breakdown["final_score"] = final

    return StockCandidate(
        code=raw.code,
        name=raw.name,
        current_price=raw.current_price,
        change_rate=raw.change_rate,
        volume=raw.volume,
        volume_ratio=raw.volume_ratio,
        vwap=raw.vwap,
        trade_value=raw.trade_value,
        market_cap=raw.market_cap,
        foreign_ratio=raw.foreign_ratio,
        breakdown=ScoreBreakdown(**breakdown),
        final_score=final,
        tier=tier,
        excluded_reason=reason if excluded else None,
        detected_at=n.isoformat(),
    )


# ═══════════════════════════════════════════════
# KIS API 래퍼 (핵심 조회 함수)
# ═══════════════════════════════════════════════
async def fetch_top_trade_value(
    get_token_fn,
    base_url: str,
    market: Literal["KOSPI", "KOSDAQ", "ALL"] = "ALL",
    top_n: int = 200,
) -> list[dict]:
    """
    거래대금 상위 N 종목 조회
    KIS TR ID: FHPST01710000 (거래대금순위)

    Returns: [{code, name, trade_value, ...}, ...]
    v1.1-A.1: 토큰/API 실패 시 빈 리스트 반환 (죽지 않음)
    """
    token = await safe_get_token(get_token_fn)
    if not token:
        if DEBUG_MODE:
            print("[fetch_top_trade_value] token unavailable → [] fallback")
        return []

    headers = {
        "authorization": f"Bearer {token}",
        "appkey": os.environ.get("KIS_APP_KEY", ""),
        "appsecret": os.environ.get("KIS_APP_SECRET", ""),
        "tr_id": "FHPST01710000",
    }

    # 시장 구분: 0000=전체, 0001=코스피, 1001=코스닥
    fid_input_iscd = {"ALL": "0000", "KOSPI": "0001", "KOSDAQ": "1001"}[market]

    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_COND_SCR_DIV_CODE": "20171",
        "FID_INPUT_ISCD": fid_input_iscd,
        "FID_DIV_CLS_CODE": "0",
        "FID_BLNG_CLS_CODE": "0",
        "FID_TRGT_CLS_CODE": "111111111",
        "FID_TRGT_EXLS_CLS_CODE": "000000",
        "FID_INPUT_PRICE_1": "0",
        "FID_INPUT_PRICE_2": "0",
        "FID_VOL_CNT": "0",
        "FID_INPUT_DATE_1": "",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(
                f"{base_url}/uapi/domestic-stock/v1/quotations/volume-rank",
                headers=headers,
                params=params,
            )
            data = res.json()
    except Exception as e:
        if DEBUG_MODE:
            print(f"[fetch_top_trade_value] API exception: {e}")
        return []

    if data.get("rt_cd") != "0":
        if DEBUG_MODE:
            print(f"[fetch_top_trade_value] API rt_cd={data.get('rt_cd')}: {data.get('msg1','')}")
        return []

    output = data.get("output", [])[:top_n]
    return output


async def fetch_stock_detail(
    get_token_fn,
    base_url: str,
    code: str,
) -> dict:
    """
    종목 현재가/거래량/등락률 등 상세
    v1.1-A.1: 실패 시 빈 dict 반환
    """
    token = await safe_get_token(get_token_fn)
    if not token:
        return {}

    headers = {
        "authorization": f"Bearer {token}",
        "appkey": os.environ.get("KIS_APP_KEY", ""),
        "appsecret": os.environ.get("KIS_APP_SECRET", ""),
        "tr_id": "FHKST01010100",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                f"{base_url}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers=headers,
                params=params,
            )
            data = res.json()
    except Exception as e:
        if DEBUG_MODE:
            print(f"[fetch_stock_detail({code})] API exception: {e}")
        return {}

    if data.get("rt_cd") != "0":
        if DEBUG_MODE:
            print(f"[fetch_stock_detail({code})] rt_cd={data.get('rt_cd')}: {data.get('msg1','')}")
        return {}

    return data.get("output", {})


async def fetch_min5_candles(
    get_token_fn,
    base_url: str,
    code: str,
    count: int = 30,
) -> list[dict]:
    """
    5분봉 OHLC (VWAP/눌림 분석용)
    v1.1-A.1: 토큰/API 실패 시 빈 리스트 반환
    """
    token = await safe_get_token(get_token_fn)
    if not token:
        return []

    headers = {
        "authorization": f"Bearer {token}",
        "appkey": os.environ.get("KIS_APP_KEY", ""),
        "appsecret": os.environ.get("KIS_APP_SECRET", ""),
        "tr_id": "FHKST03010200",
    }
    now = now_kst()
    params = {
        "FID_ETC_CLS_CODE": "",
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_HOUR_1": now.strftime("%H%M%S"),
        "FID_PW_DATA_INCU_YN": "Y",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                f"{base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                headers=headers,
                params=params,
            )
            data = res.json()
    except Exception as e:
        if DEBUG_MODE:
            print(f"[fetch_min5_candles({code})] API exception: {e}")
        return []

    if data.get("rt_cd") != "0":
        return []

    return data.get("output2", [])[:count]


# ═══════════════════════════════════════════════
# 5분봉 → 파생 지표 계산
# ═══════════════════════════════════════════════
def derive_from_candles(candles: list[dict]) -> dict:
    """5분봉에서 VWAP/눌림/윗꼬리 등 파생"""
    if not candles:
        return {
            "vwap": 0.0,
            "min5_low_holding": False,
            "min5_wiggle": False,
            "upper_tail_count": 0,
            "volume_declining": False,
        }

    # VWAP = Σ(체결가 * 체결량) / Σ(체결량)
    total_pv = 0.0
    total_v = 0
    recent_lows = []
    upper_tails = 0
    volumes = []

    for c in candles:
        try:
            close = float(c.get("stck_prpr", 0))
            high = float(c.get("stck_hgpr", 0))
            low = float(c.get("stck_lwpr", 0))
            open_p = float(c.get("stck_oprc", 0))
            vol = int(c.get("cntg_vol", 0))
        except (ValueError, TypeError):
            continue

        typical = (high + low + close) / 3.0
        total_pv += typical * vol
        total_v += vol
        recent_lows.append(low)
        volumes.append(vol)

        # 윗꼬리 카운트: 고가 - max(시가, 종가) > (종가 - 저가) * 1.5
        body_top = max(open_p, close)
        upper_wick = high - body_top
        lower_range = max(close - low, 1)
        if upper_wick > lower_range * 1.5:
            upper_tails += 1

    vwap = (total_pv / total_v) if total_v > 0 else 0.0

    # 최근 3봉의 저점이 비슷하게 유지되면 눌림 유지
    min5_low_holding = False
    min5_wiggle = False
    if len(recent_lows) >= 3:
        last3 = recent_lows[:3]
        spread = (max(last3) - min(last3)) / max(min(last3), 1) * 100
        if spread < 1.5:
            min5_low_holding = True
        elif spread < 3.0:
            min5_wiggle = True

    # 거래량 감소 추세
    volume_declining = False
    if len(volumes) >= 3:
        if volumes[0] < volumes[1] < volumes[2]:
            volume_declining = True

    return {
        "vwap": round(vwap, 2),
        "min5_low_holding": min5_low_holding,
        "min5_wiggle": min5_wiggle,
        "upper_tail_count": upper_tails,
        "volume_declining": volume_declining,
    }


# ═══════════════════════════════════════════════
# 알림 쿨다운 관리 (메모리 기반)
# ═══════════════════════════════════════════════
_alert_history: dict[str, list[float]] = {}


def should_send_alert(code: str) -> bool:
    """중복 알림 차단 (10분 쿨다운 + 일 3회 제한)"""
    now = time.time()
    today_start = datetime.now(KST).replace(hour=0, minute=0, second=0).timestamp()

    history = _alert_history.get(code, [])
    # 오늘 것만 필터
    history = [t for t in history if t >= today_start]

    if len(history) >= MAX_ALERTS_PER_STOCK_DAILY:
        return False

    if history and (now - history[-1]) < ALERT_COOLDOWN_SECONDS:
        return False

    history.append(now)
    _alert_history[code] = history
    return True


# ═══════════════════════════════════════════════
# 스캔 캐시 (30초 TTL)
# ═══════════════════════════════════════════════
_scan_cache: dict = {"data": None, "ts": 0}
SCAN_CACHE_TTL = 30


# ═══════════════════════════════════════════════
# FastAPI Router
# ═══════════════════════════════════════════════
def create_router(get_token_fn, base_url: str) -> APIRouter:
    """
    main.py에서 호출:
        from sangjeonjo import create_router
        app.include_router(create_router(get_token, BASE_URL))
    """
    router = APIRouter(prefix="/api/sangjeonjo", tags=["상전조"])

    @router.get("/diffusion")
    async def get_diffusion():
        """
        시장 주의력 분산도 조회
        v1.1-A.1: 토큰/API 실패 시 fallback mock 응답
        """
        try:
            rows = await fetch_top_trade_value(get_token_fn, base_url, "ALL", 200)
        except Exception as e:
            if DEBUG_MODE:
                print(f"[/diffusion] unexpected: {e}")
            rows = []

        if not rows:
            # Fallback: 구조 유지된 mock 응답
            n = now_kst()
            return {
                "timestamp": n.isoformat(),
                "top10_trade_value": 0,
                "total_trade_value": 0,
                "concentration": 0.0,
                "diffusion": 0.0,
                "interpretation": "[fallback] 거래대금 데이터 없음 (토큰/API 점검 필요)",
                "bonus_applied": 0,
                "active": False,
                "status": "fallback",
                "error_type": ErrorType.TOKEN_ERROR,
            }

        # 거래대금 필드 파싱 (KIS 필드명: acml_tr_pbmn)
        def tv(r):
            try:
                return int(r.get("acml_tr_pbmn", 0))
            except:
                return 0

        top10 = [tv(r) for r in rows[:10]]
        total = sum(tv(r) for r in rows)
        return compute_market_diffusion(top10, total)

    @router.get("/scan")
    async def scan_market(
        min_score: int = Query(80, ge=0, le=100),
        force_refresh: bool = Query(False),
    ):
        """
        전체 유니버스 스캔
        v1.1-A.1: 어떤 실패에도 구조화된 응답 보장 (죽지 않는 API)
        """
        now = time.time()

        # 캐시 확인
        if (not force_refresh
            and _scan_cache["data"]
            and now - _scan_cache["ts"] < SCAN_CACHE_TTL):
            return _scan_cache["data"]

        # 1. 거래대금 상위 200 조회 (실패 시 빈 리스트 반환)
        try:
            rows = await fetch_top_trade_value(get_token_fn, base_url, "ALL", UNIVERSE_TOP_N)
        except Exception as e:
            if DEBUG_MODE:
                print(f"[/scan] fetch_top_trade_value unexpected: {e}")
            rows = []

        # 🛡️ Fallback: 거래대금 데이터가 없으면 구조만 유지한 응답
        if not rows:
            fallback = build_empty_scan_response(
                reason="거래대금 상위 조회 실패 (토큰/API 점검 필요)",
                error_type=ErrorType.TOKEN_ERROR,
            )
            return fallback

        # 2. 주의력 분산도 먼저 계산
        def tv(r):
            try:
                return int(r.get("acml_tr_pbmn", 0))
            except:
                return 0

        top10_values = [tv(r) for r in rows[:10]]
        total_value = sum(tv(r) for r in rows)
        diffusion = compute_market_diffusion(top10_values, total_value)

        # 3. 각 종목 스코어링 (동시성 제한)
        n = now_kst()
        t_mult = time_multiplier(n)

        candidates: list[StockCandidate] = []
        excluded_count = 0
        process_errors = 0  # 개별 종목 실패 카운트 (디버깅용)

        # API 부하 방지 → 세마포어로 동시 10개 제한
        sem = asyncio.Semaphore(10)

        async def process(row: dict, rank: int):
            """
            개별 종목 처리
            v1.1-A.1 P3: 어떤 실패에도 None 반환, 예외 전파 금지
            """
            nonlocal excluded_count, process_errors
            try:
                code = row.get("mksc_shrn_iscd", "")
                name = row.get("hts_kor_isnm", "")
                if not code:
                    return None

                # 5분봉 조회 (실패해도 기본값 사용)
                async with sem:
                    try:
                        candles = await fetch_min5_candles(get_token_fn, base_url, code, 10)
                        derived = derive_from_candles(candles)
                    except Exception as e:
                        if DEBUG_MODE:
                            print(f"[process({code})] candles failed: {e}")
                        derived = {
                            "vwap": 0.0, "min5_low_holding": False,
                            "min5_wiggle": False, "upper_tail_count": 0,
                            "volume_declining": False,
                        }

                # 숫자 파싱 (실패 시 종목 스킵)
                try:
                    current_price = int(row.get("stck_prpr", 0))
                    change_rate = float(row.get("prdy_ctrt", 0))
                    volume = int(row.get("acml_vol", 0))
                    trade_value = tv(row)
                    prev_vol = int(row.get("prdy_vol", 0) or 0)
                    volume_ratio = (volume / prev_vol) if prev_vol > 0 else 1.0
                except (ValueError, TypeError) as e:
                    if DEBUG_MODE:
                        print(f"[process({code})] parse failed: {e}")
                    return None

                raw = StockRawData(
                    code=code,
                    name=name,
                    current_price=current_price,
                    change_rate=change_rate,
                    volume=volume,
                    volume_ratio=volume_ratio,
                    vwap=derived["vwap"],
                    trade_value=trade_value,
                    market_cap_rank=rank + 1,
                    market_type="KOSPI",  # v1.1-A.1에서는 아직 KOSPI 고정
                    min5_low_holding=derived["min5_low_holding"],
                    min5_wiggle=derived["min5_wiggle"],
                    volume_declining=derived["volume_declining"],
                    upper_tail_count=derived["upper_tail_count"],
                )

                candidate = score_stock(raw, diffusion.bonus_applied, n)
                if candidate.tier == "제외":
                    excluded_count += 1
                    return None
                return candidate

            except Exception as e:
                # 🛡️ 최종 방어선: 어떤 에러든 None 반환
                process_errors += 1
                if DEBUG_MODE:
                    print(f"[process] unexpected exception: {type(e).__name__}: {e}")
                return None

        tasks = [process(row, i) for i, row in enumerate(rows)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, StockCandidate) and r.final_score >= min_score:
                candidates.append(r)

        # 점수 내림차순 정렬
        candidates.sort(key=lambda c: c.final_score, reverse=True)

        # ⚠️ dict로 반환 (response_model 제거 → 유연한 응답)
        result = {
            "scanned_at": n.isoformat(),
            "total_scanned": len(rows),
            "excluded_count": excluded_count,
            "diffusion": diffusion.dict() if hasattr(diffusion, 'dict') else diffusion.model_dump(),
            "time_multiplier": t_mult,
            "candidates": [
                c.dict() if hasattr(c, 'dict') else c.model_dump()
                for c in candidates[:50]
            ],
            "status": "ok",
        }

        if DEBUG_MODE and process_errors > 0:
            result["debug_process_errors"] = process_errors

        _scan_cache["data"] = result
        _scan_cache["ts"] = now
        return result

    @router.get("/score/{code}")
    async def get_single_score(code: str):
        """
        개별 종목 점수 상세
        v1.1-A.1: 토큰/파싱 실패 시 fallback 응답
        """
        try:
            detail = await fetch_stock_detail(get_token_fn, base_url, code)
            candles = await fetch_min5_candles(get_token_fn, base_url, code, 10)
        except Exception as e:
            if DEBUG_MODE:
                print(f"[/score/{code}] fetch failed: {e}")
            detail, candles = {}, []

        if not detail:
            # Fallback: token 실패 or 존재하지 않는 종목
            return {
                "code": code,
                "name": code,
                "status": "fallback",
                "error_type": ErrorType.TOKEN_ERROR,
                "message": "종목 데이터 조회 실패 (토큰/API 점검 필요)",
                "mock_data": MOCK_STOCK_DATA,
            }

        derived = derive_from_candles(candles)

        try:
            current_price = int(detail.get("stck_prpr", 0))
            change_rate = float(detail.get("prdy_ctrt", 0))
            volume = int(detail.get("acml_vol", 0))
            trade_value = int(detail.get("acml_tr_pbmn", 0))
            prev_vol = int(detail.get("prdy_vol", 0) or 0)
            volume_ratio = (volume / prev_vol) if prev_vol > 0 else 1.0
        except (ValueError, TypeError) as e:
            return {
                "code": code,
                "status": "fallback",
                "error_type": ErrorType.DATA_ERROR,
                "message": f"종목 데이터 파싱 실패: {str(e) if DEBUG_MODE else ''}",
                "mock_data": MOCK_STOCK_DATA,
            }

        raw = StockRawData(
            code=code,
            name=detail.get("bstp_kor_isnm", code),
            current_price=current_price,
            change_rate=change_rate,
            volume=volume,
            volume_ratio=volume_ratio,
            vwap=derived["vwap"],
            trade_value=trade_value,
            min5_low_holding=derived["min5_low_holding"],
            min5_wiggle=derived["min5_wiggle"],
            volume_declining=derived["volume_declining"],
            upper_tail_count=derived["upper_tail_count"],
        )

        candidate = score_stock(raw, 0, now_kst())
        result = candidate.dict() if hasattr(candidate, 'dict') else candidate.model_dump()
        result["status"] = "ok"
        return result

    @router.get("/health")
    async def health():
        return {
            "status": "ok",
            "module": "sangjeonjo",
            "version": "1.1-A.1",
            "debug_mode": DEBUG_MODE,
            "kis_mode": os.environ.get("KIS_MODE", "unknown"),
            "timestamp": now_kst().isoformat(),
        }

    @router.get("/diagnostic")
    async def diagnostic():
        """
        v1.1-A.1 신규: KIS 연결 진단
        토큰 발급 시도 → 어디서 막히는지 구체적으로 리턴
        """
        n = now_kst()
        result = {
            "timestamp": n.isoformat(),
            "version": "1.1-A.1",
            "checks": {},
        }

        # 1. 환경변수 체크
        result["checks"]["env"] = {
            "KIS_APP_KEY": bool(os.environ.get("KIS_APP_KEY")),
            "KIS_APP_SECRET": bool(os.environ.get("KIS_APP_SECRET")),
            "KIS_ACCOUNT": bool(os.environ.get("KIS_ACCOUNT")),
            "KIS_MODE": os.environ.get("KIS_MODE", "NOT_SET"),
        }

        # 2. 토큰 발급 시도
        token = await safe_get_token(get_token_fn)
        result["checks"]["token"] = {
            "obtained": bool(token),
            "base_url": base_url,
        }

        # 3. 토큰 있으면 최소 API 호출 1번 (삼성전자 현재가)
        if token:
            try:
                test_detail = await fetch_stock_detail(get_token_fn, base_url, "005930")
                result["checks"]["api_call"] = {
                    "success": bool(test_detail),
                    "sample_price": test_detail.get("stck_prpr", None) if test_detail else None,
                }
            except Exception as e:
                result["checks"]["api_call"] = {
                    "success": False,
                    "error": str(e) if DEBUG_MODE else "api_error",
                }
        else:
            result["checks"]["api_call"] = {"success": False, "skipped": "no_token"}

        # 종합 판정
        if not all(result["checks"]["env"].values()):
            result["overall"] = "환경변수 누락"
            result["error_type"] = ErrorType.TOKEN_ERROR
        elif not token:
            result["overall"] = "토큰 발급 실패 (KIS API 키/시크릿 확인 필요)"
            result["error_type"] = ErrorType.TOKEN_ERROR
        elif not result["checks"]["api_call"].get("success"):
            result["overall"] = "토큰 OK, API 호출 실패"
            result["error_type"] = ErrorType.API_ERROR
        else:
            result["overall"] = "전체 정상"
            result["error_type"] = None

        return result

    return router

# -*- coding: utf-8 -*-
"""
toss_client.py  —  Trinity × 토스증권 Open API (읽기 전용 / READ-ONLY)
=====================================================================
원칙 (림빅 지시 + 핸드오프 헌장):
  1. 1단계는 주문 금지. 이 모듈에는 매수/매도/정정/취소 함수가 "존재하지 않는다."
  2. 키는 코드에 절대 박지 않는다. Railway 환경변수에서만 읽는다.
       TOSS_CLIENT_ID, TOSS_CLIENT_SECRET   (필수)
       TOSS_ACCOUNT_SEQ                     (선택 — 없으면 /toss/accounts로 먼저 확인)
  3. 추측 금지. 계좌/보유 엔드포인트의 정확한 경로·필드명은 토스 OpenAPI 스펙과
     실제 raw 응답으로 확인한 뒤 매핑한다. 모르는 값은 "준비중"으로 둔다.

검증된 사실 (공식 문서 https://developers.tossinvest.com/docs 기준):
  - Base URL                : https://openapi.tossinvest.com
  - OpenAPI 스펙(경로 카탈로그): /openapi-docs/latest/openapi.json
  - 토큰 발급               : POST /oauth2/token  (OAuth2 Client Credentials Grant)
  - 인증 헤더               : Authorization: Bearer {access_token}
  - 계좌/자산/주문 카테고리   : 추가로 X-Tossinvest-Account: {accountSeq}

연결 방법 (main_safe.py 에 두 줄만 추가):
  from toss_client import toss_router
  app.include_router(toss_router)

제공 엔드포인트 (전부 GET, 읽기 전용):
  GET /toss/health     키 존재 + 토큰 발급 + 스펙 접근까지 확인 (실데이터 의존 X)
  GET /toss/spec       토스 OpenAPI 스펙의 전체 경로 목록 (실제 account/positions 경로 발견용)
  GET /toss/accounts   계좌 목록 raw (accountSeq 확인용)
  GET /toss/positions  보유 종목 raw + 정규화 시도 (Odin holding 매핑 소스)
  GET /toss/portfolio  Odin v0.3 Portfolio/Holding 카드용 (정규화된 형태, 미확정 시 "준비중")
  GET /toss/raw?path=… 발견한 경로를 직접 GET 패스스루 (GET-only = 주문 불가 안전장치)
"""

import os
import time
import logging

import requests
from fastapi import APIRouter, Query

log = logging.getLogger("toss")

# ─────────────────────────────────────────────────────────────────────────────
# 검증된 상수
# ─────────────────────────────────────────────────────────────────────────────
TOSS_BASE = "https://openapi.tossinvest.com"
TOKEN_PATH = "/oauth2/token"
SPEC_PATH = "/openapi-docs/latest/openapi.json"

# ─────────────────────────────────────────────────────────────────────────────
# 미확정 상수 — 토스 OpenAPI 스펙(/toss/spec)으로 실제 경로 확인 후 채운다.
#   확인 전까지 빈 문자열이면 해당 엔드포인트는 "준비중"을 반환한다 (가짜 숫자 금지).
#   예시 후보일 뿐, 실제 값은 /toss/spec 출력으로 검증할 것. 추측해서 쓰지 말 것.
# ─────────────────────────────────────────────────────────────────────────────
ACCOUNTS_PATH = ""    # 예: "/v1/accounts" — 스펙에서 '계좌 목록' 경로 확인 후 입력
POSITIONS_PATH = ""   # 예: "/v1/accounts/{accountSeq}/holdings" — 보유 조회 경로 확인 후 입력

# 정규화 매핑 — 첫 raw 응답(/toss/positions)을 본 뒤 실제 키 이름으로 채운다.
#   왼쪽 = Odin MOCK.holding 키, 오른쪽 = 토스 응답 키 (확인 전엔 None).
POSITION_FIELD_MAP = {
    "code": None,       # 종목코드 (예: 토스 응답의 어떤 필드?)
    "name": None,       # 종목명
    "qty": None,        # 보유수량
    "avgPrice": None,   # 평균단가
    "evalPnl": None,    # 평가손익(금액)
    "evalPct": None,    # 평가손익(%)
}

# ─────────────────────────────────────────────────────────────────────────────
# 토큰 캐시 (메모리). client credentials 토큰은 만료가 있으므로 재사용한다.
# ─────────────────────────────────────────────────────────────────────────────
_token_cache = {"access_token": None, "expires_at": 0.0}
_TOKEN_SAFETY_MARGIN = 60  # 만료 60초 전이면 미리 재발급


def _creds():
    cid = os.environ.get("TOSS_CLIENT_ID")
    csec = os.environ.get("TOSS_CLIENT_SECRET")
    if not cid or not csec:
        raise RuntimeError(
            "TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 환경변수가 없습니다. "
            "Railway Environment Variables에 등록하세요."
        )
    return cid, csec


def get_toss_access_token(force: bool = False) -> str:
    """OAuth2 Client Credentials 토큰 발급/재사용. 비밀키는 로깅하지 않는다."""
    now = time.time()
    if (
        not force
        and _token_cache["access_token"]
        and now < _token_cache["expires_at"] - _TOKEN_SAFETY_MARGIN
    ):
        return _token_cache["access_token"]

    cid, csec = _creds()
    url = TOSS_BASE + TOKEN_PATH

    # 표준 client_credentials: form-encoded body.
    # ⚠️ 만약 401/invalid_client 이 나오면 토스가 HTTP Basic 인증을 요구하는 것일 수 있다.
    #    그 경우 아래 data 에서 client_id/secret 을 빼고
    #    auth=(cid, csec) 를 requests.post 에 넘기는 방식으로 바꿀 것. (검증 후 확정)
    data = {
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": csec,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    resp = requests.post(url, data=data, headers=headers, timeout=10)
    if resp.status_code != 200:
        # 본문은 남기되 키는 절대 남기지 않는다.
        log.warning("toss token failed: %s %s", resp.status_code, resp.text[:300])
        raise RuntimeError(f"토큰 발급 실패: HTTP {resp.status_code}")

    body = resp.json()
    token = body.get("access_token")
    if not token:
        raise RuntimeError(f"토큰 응답에 access_token 없음: keys={list(body.keys())}")

    expires_in = int(body.get("expires_in", 1800))  # 기본 30분 가정
    _token_cache["access_token"] = token
    _token_cache["expires_at"] = now + expires_in
    return token


def _auth_headers(account_seq: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {get_toss_access_token()}"}
    seq = account_seq or os.environ.get("TOSS_ACCOUNT_SEQ")
    if seq:
        h["X-Tossinvest-Account"] = str(seq)
    return h


def _toss_get(path: str, account_seq: str | None = None, params: dict | None = None) -> dict:
    """인증된 GET. 읽기 전용. path 는 '/'로 시작하는 전체 경로."""
    url = TOSS_BASE + path
    resp = requests.get(url, headers=_auth_headers(account_seq), params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# 읽기 전용 함수
# ─────────────────────────────────────────────────────────────────────────────
def toss_get_spec_paths() -> list[str]:
    """토스 OpenAPI 스펙에서 전체 경로 목록을 추출 (인증 불필요)."""
    resp = requests.get(TOSS_BASE + SPEC_PATH, timeout=10)
    resp.raise_for_status()
    spec = resp.json()
    return sorted((spec.get("paths") or {}).keys())


def toss_get_accounts() -> dict:
    if not ACCOUNTS_PATH:
        return {"status": "준비중", "reason": "ACCOUNTS_PATH 미설정 — /toss/spec 로 경로 확인 필요"}
    return _toss_get(ACCOUNTS_PATH)


def toss_get_positions(account_seq: str | None = None) -> dict:
    if not POSITIONS_PATH:
        return {"status": "준비중", "reason": "POSITIONS_PATH 미설정 — /toss/spec 로 경로 확인 필요"}
    path = POSITIONS_PATH.replace("{accountSeq}", str(account_seq or os.environ.get("TOSS_ACCOUNT_SEQ", "")))
    return _toss_get(path, account_seq=account_seq)


def _normalize_positions(raw) -> list[dict] | None:
    """raw 토스 응답 → Odin holding 형태로 매핑.
    POSITION_FIELD_MAP 이 채워지기 전엔 None 을 반환(=준비중)."""
    if any(v is None for v in POSITION_FIELD_MAP.values()):
        return None
    # 응답에서 종목 배열의 위치를 모르므로, 첫 list 값을 종목 배열로 간주(확인 후 고정).
    items = raw if isinstance(raw, list) else None
    if items is None and isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, list):
                items = v
                break
    if items is None:
        return None
    out = []
    for it in items:
        out.append({k: it.get(src) for k, src in POSITION_FIELD_MAP.items()})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI 라우터 (전부 GET = 주문 불가)
# ─────────────────────────────────────────────────────────────────────────────
toss_router = APIRouter(prefix="/toss", tags=["toss"])


@toss_router.get("/health")
def toss_health():
    """키 존재 + 토큰 발급 + 스펙 접근까지만 확인. 실데이터(경로) 의존 없음."""
    out = {"keys_present": False, "token_acquired": False, "spec_reachable": False}
    try:
        _creds()
        out["keys_present"] = True
    except Exception as e:
        out["error"] = str(e)
        return out
    try:
        get_toss_access_token(force=True)
        out["token_acquired"] = True
        out["token_expires_at"] = int(_token_cache["expires_at"])
    except Exception as e:
        out["error"] = str(e)
        return out
    try:
        paths = toss_get_spec_paths()
        out["spec_reachable"] = True
        out["spec_path_count"] = len(paths)
    except Exception as e:
        out["spec_error"] = str(e)
    out["ok"] = out["token_acquired"]
    return out


@toss_router.get("/spec")
def toss_spec():
    """실제 account/positions 경로를 찾기 위한 전체 경로 목록."""
    try:
        return {"paths": toss_get_spec_paths()}
    except Exception as e:
        return {"status": "준비중", "error": str(e)}


@toss_router.get("/accounts")
def toss_accounts():
    try:
        return toss_get_accounts()
    except Exception as e:
        return {"status": "준비중", "error": str(e)}


@toss_router.get("/positions")
def toss_positions(account_seq: str | None = Query(default=None)):
    """보유 raw + 정규화 시도. 매핑 전엔 normalized=null(준비중)."""
    try:
        raw = toss_get_positions(account_seq)
        return {"raw": raw, "normalized": _normalize_positions(raw)}
    except Exception as e:
        return {"status": "준비중", "error": str(e)}


@toss_router.get("/portfolio")
def toss_portfolio(account_seq: str | None = Query(default=None)):
    """Odin v0.3 Portfolio/Holding 카드용. 정규화 가능할 때만 holding 반환."""
    try:
        raw = toss_get_positions(account_seq)
        norm = _normalize_positions(raw)
        if norm is None:
            return {"status": "준비중", "reason": "POSITION_FIELD_MAP 매핑 전 — raw 확인 필요"}
        return {"holding": norm, "ts": int(time.time())}
    except Exception as e:
        return {"status": "준비중", "error": str(e)}


@toss_router.get("/raw")
def toss_raw(path: str = Query(...), account_seq: str | None = Query(default=None)):
    """발견한 경로를 직접 GET. 읽기 전용 안전장치: '/'로 시작 + GET only."""
    if not path.startswith("/"):
        return {"status": "error", "reason": "path must start with '/'"}
    try:
        return {"path": path, "data": _toss_get(path, account_seq=account_seq)}
    except Exception as e:
        return {"status": "준비중", "path": path, "error": str(e)}

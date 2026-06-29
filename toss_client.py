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
  4. HTTP는 main_safe.py와 동일하게 httpx 사용 (requests 의존성 추가 없음).

검증된 사실 (공식 문서 https://developers.tossinvest.com/docs 기준):
  - Base URL                : https://openapi.tossinvest.com
  - OpenAPI 스펙(경로 카탈로그): /openapi-docs/latest/openapi.json
  - 토큰 발급               : POST /oauth2/token  (OAuth2 Client Credentials Grant)
  - 인증 헤더               : Authorization: Bearer {access_token}
  - 계좌/자산/주문 카테고리   : 추가로 X-Tossinvest-Account: {accountSeq}

연결 방법 (main_safe.py 에 이미 추가됨):
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
from __future__ import annotations

import os
import time
import logging

import httpx
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
# ✅ 토스 OpenAPI 스펙(v1.1.5)으로 확정. 추측 아님.
ACCOUNTS_PATH = "/api/v1/accounts"     # 계좌 목록 → result[].accountSeq
POSITIONS_PATH = "/api/v1/holdings"    # 보유 주식 → result.items[]  (X-Tossinvest-Account 헤더 필요)

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

    # 표준 client_credentials: form-encoded body (httpx 가 Content-Type 자동 설정).
    # ⚠️ 만약 401/invalid_client 이 나오면 토스가 HTTP Basic 인증을 요구하는 것일 수 있다.
    #    그 경우 data 에서 client_id/secret 을 빼고
    #    httpx.post(url, data={"grant_type": "client_credentials"}, auth=(cid, csec), ...)
    #    형태로 바꿀 것. (검증 후 확정)
    data = {
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": csec,
    }

    resp = httpx.post(url, data=data, timeout=10)
    if resp.status_code != 200:
        # 본문은 남기되 키는 절대 남기지 않는다.
        log.warning("toss token failed: %s %s", resp.status_code, resp.text[:300])
        raise RuntimeError(f"토큰 발급 실패: HTTP {resp.status_code} {resp.text[:200]}")

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
    resp = httpx.get(url, headers=_auth_headers(account_seq), params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# 읽기 전용 함수
# ─────────────────────────────────────────────────────────────────────────────
def toss_get_spec_paths() -> list:
    """토스 OpenAPI 스펙에서 전체 경로 목록을 추출 (인증 불필요)."""
    resp = httpx.get(TOSS_BASE + SPEC_PATH, timeout=10)
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
    return _toss_get(POSITIONS_PATH, account_seq=account_seq)


def _num(v):
    """토스 decimal 문자열 → float. None/빈값은 None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def _normalize_positions(raw):
    """raw 토스 /api/v1/holdings 응답 → Odin holding 형태로 매핑.
    스펙(v1.1.5) 기준: raw = {"result": {"items": [...], "marketValue": {...}, ...}}
    종목별 손익은 중첩 객체(profitLoss.amount / profitLoss.rate)."""
    result = raw.get("result") if isinstance(raw, dict) else None
    if not isinstance(result, dict):
        return None
    items = result.get("items")
    if not isinstance(items, list):
        return None
    out = []
    for it in items:
        pl = it.get("profitLoss") or {}
        mv = it.get("marketValue") or {}
        rate = _num(pl.get("rate"))
        out.append({
            "code": it.get("symbol"),
            "name": it.get("name"),
            "qty": _num(it.get("quantity")),
            "avgPrice": _num(it.get("averagePurchasePrice")),
            "lastPrice": _num(it.get("lastPrice")),
            "evalAmount": _num(mv.get("amount")),          # 평가금액
            "evalPnl": _num(pl.get("amount")),             # 평가손익(금액)
            "evalPct": round(rate * 100, 2) if isinstance(rate, float) else None,  # 손익률 %
            "currency": it.get("currency"),
            "market": it.get("marketCountry"),
        })
    return out


def _account_summary(raw):
    """계좌 전체 요약(총매입/평가/손익). Odin Portfolio 카드용."""
    result = raw.get("result") if isinstance(raw, dict) else None
    if not isinstance(result, dict):
        return None
    pl = result.get("profitLoss") or {}
    mv = result.get("marketValue") or {}
    rate = _num(pl.get("rate"))
    return {
        "purchaseKRW": _num((result.get("totalPurchaseAmount") or {}).get("krw")),
        "marketValueKRW": _num((mv.get("amount") or {}).get("krw")),
        "pnlKRW": _num((pl.get("amount") or {}).get("krw")),
        "pnlPct": round(rate * 100, 2) if isinstance(rate, float) else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI 라우터 (전부 GET = 주문 불가)
# ─────────────────────────────────────────────────────────────────────────────
toss_router = APIRouter(prefix="/toss", tags=["toss"])


@toss_router.get("/myip")
def toss_myip():
    """이 서버(Railway)가 실제로 어떤 외부 IP로 나가는지 확인.
    여기 나오는 IP(들)를 토스 '허용 IP 관리'에 등록하면 된다.
    여러 echo 서비스로 교차 확인한다."""
    seen = _detect_outbound_ips()
    if not seen:
        return {"status": "준비중", "reason": "IP echo 서비스 응답 없음 — 잠시 후 재시도"}
    return {
        "outbound_ips": seen,
        "hint": "이 IP(들)를 토스 '허용 IP 관리 → IP 추가'에 등록하세요. (PC IP가 아니라 이 IP)",
    }


@toss_router.get("/ip_check")
def toss_ip_check():
    """파수꾼 — 현재 Railway outbound IP가 토스 허용목록과 일치하는지 확인.
    TOSS_ALLOWED_IPS(쉼표구분 환경변수)와 비교. 다르면 텔레그램 경고(선택).
    ⚠️ 토스 허용 IP '자동 등록'은 불가(공개 API 없음). 등록은 림빅이 콘솔에서 수동."""
    current = _detect_outbound_ips()
    if not current:
        return {"status": "준비중", "reason": "IP 감지 실패 — 잠시 후 재시도"}
    allowed_raw = os.environ.get("TOSS_ALLOWED_IPS", "")
    allowed = [x.strip() for x in allowed_raw.split(",") if x.strip()]
    unregistered = [ip for ip in current if ip not in allowed]

    if not allowed:
        return {
            "status": "준비중",
            "current_ips": current,
            "reason": "TOSS_ALLOWED_IPS 환경변수 미설정 — 등록한 IP를 쉼표로 넣으세요",
        }
    if unregistered:
        # 변경 감지 → 텔레그램 경고(있으면). 등록은 사람이 수동으로.
        msg = (
            "🔱 [Trinity] 토스 허용 IP 불일치 감지\n"
            f"현재 Railway IP: {', '.join(current)}\n"
            f"미등록 IP: {', '.join(unregistered)}\n"
            "→ 토스 '허용 IP 관리'에 추가 + TOSS_ALLOWED_IPS 갱신 필요"
        )
        sent = _try_telegram(msg)
        return {
            "status": "changed",
            "current_ips": current,
            "unregistered_ips": unregistered,
            "allowed_ips": allowed,
            "telegram_sent": sent,
            "action": "토스 허용 IP 관리에 unregistered_ips 추가 + TOSS_ALLOWED_IPS 환경변수 갱신",
        }
    return {"status": "ok", "current_ips": current, "allowed_ips": allowed}


def _detect_outbound_ips() -> list:
    """이 서버의 실제 outbound IP(들)를 echo 서비스로 교차 확인."""
    services = [
        "https://api.ipify.org",
        "https://checkip.amazonaws.com",
        "https://ifconfig.me/ip",
    ]
    seen = []
    for url in services:
        try:
            r = httpx.get(url, timeout=8)
            if r.status_code == 200:
                ip = r.text.strip()
                if ip and ip not in seen:
                    seen.append(ip)
        except Exception:
            continue
    return seen


def _try_telegram(message: str) -> bool:
    """텔레그램 경고 전송(선택). main_safe와 동일 환경변수 재사용.
    토큰/챗ID 없으면 조용히 False. 절대 본체에 영향 주지 않는다."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=8,
        )
        return r.status_code == 200
    except Exception:
        return False


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
    """Odin v0.3 Portfolio/Holding 카드용. holding(종목별) + summary(계좌요약)."""
    try:
        raw = toss_get_positions(account_seq)
        norm = _normalize_positions(raw)
        if norm is None:
            return {"status": "준비중", "reason": "보유 응답 구조 예상과 다름 — /toss/positions raw 확인"}
        return {
            "holding": norm,
            "summary": _account_summary(raw),
            "count": len(norm),
            "ts": int(time.time()),
        }
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

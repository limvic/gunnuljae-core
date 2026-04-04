"""
건눌재 Core Engine v1.0
KIS API → 표준 데이터 반환
"""

import os
import time
import asyncio
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

app = FastAPI(title="건눌재 Core Engine", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

KIS_APP_KEY    = os.environ.get("KIS_APP_KEY", "")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
KIS_ACCOUNT    = os.environ.get("KIS_ACCOUNT", "")
KIS_MODE       = os.environ.get("KIS_MODE", "mock")

BASE_URL = (
    "https://openapivts.koreainvestment.com:29443"
    if KIS_MODE == "mock"
    else "https://openapi.koreainvestment.com:9443"
)

TOKEN_PATH = "/oauth2/tokenP" if KIS_MODE == "mock" else "/oauth2/token"

_token_cache = {"token": None, "expires_at": 0}
_cache: dict = {}
_last_good: dict = {}
CACHE_TTL = 2


async def get_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(
            f"{BASE_URL}{TOKEN_PATH}",
            json={"grant_type": "client_credentials", "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET},
        )
        data = res.json()
    if "access_token" not in data:
        raise HTTPException(status_code=503, detail={"status": "error", "error": f"토큰 발급 실패: {data.get('msg1','')}", "code": "token"})
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + 86400
    return _token_cache["token"]


async def fetch_price(code: str, token: str) -> dict:
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010100",
        "custtype": "P",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
            params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
            headers=headers,
        )
    data = res.json()
    if data.get("rt_cd") != "0":
        raise Exception(f"현재가 조회 실패: {data.get('msg1','')}")
    o = data["output"]
    price = int(o.get("stck_prpr", 0))
    high = int(o.get("stck_hgpr", 0))
    open_ = int(o.get("stck_oprc", 0))
    return {
        "price": price,
        "volume": int(o.get("acml_vol", 0)),
        "high": high,
        "low": int(o.get("stck_lwpr", 0)),
        "open": open_,
        "name": o.get("hts_kor_isnm", code),
        "change_pct": float(o.get("prdy_ctrt", 0)),
        "is_bullish": price > open_,
        "pullback_pct": round((high - price) / high * 100, 2) if high > 0 else 0,
    }

"""
건눌재 Core Engine v1.2
KIS API → 데이터 + A/G 패턴
"""

import os
import time
import asyncio
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="건눌재 Core Engine", version="1.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

KIS_APP_KEY = os.environ.get("KIS_APP_KEY", "")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
KIS_ACCOUNT = os.environ.get("KIS_ACCOUNT", "")
KIS_MODE = os.environ.get("KIS_MODE", "mock")

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
            json={
                "grant_type": "client_credentials",
                "appkey": KIS_APP_KEY,
                "appsecret": KIS_APP_SECRET,
            },
        )
        data = res.json()

    if "access_token" not in data:
        raise HTTPException(status_code=503, detail={"error": "토큰 실패"})

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
        raise Exception("가격 조회 실패")

    o = data["output"]

    price = int(o.get("stck_prpr", 0))
    high = int(o.get("stck_hgpr", 0))
    open_ = int(o.get("stck_oprc", 0))
    prev_close = int(o.get("stck_sdpr", 0))

    return {
        "price": price,
        "volume": int(o.get("acml_vol", 0)),
        "high": high,
        "low": int(o.get("stck_lwpr", 0)),
        "open": open_,
        "prev_close": prev_close,
        "name": o.get("hts_kor_isnm", code),
        "change_pct": float(o.get("prdy_ctrt", 0)),
        "is_bullish": price > open_,
        "pullback_pct": round((high - price) / high * 100, 2) if high > 0 else 0,
    }


def classify_pattern(price_data: dict) -> dict:
    open_ = price_data["open"]
    prev_close = price_data["prev_close"]
    high = price_data["high"]
    price = price_data["price"]
    pullback_pct = price_data["pullback_pct"]

    gap_pct = round((open_ - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0

    if gap_pct >= 2.0:
        return {"pattern": "G", "gap_pct": gap_pct}
    elif gap_pct <= -2.0:
        return {"pattern": "GD", "gap_pct": gap_pct}
    else:
        return {"pattern": "A", "gap_pct": gap_pct}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/stock/{code}")
async def get_stock(code: str):
    now = time.time()

    if code in _cache and now - _cache[code]["ts"] < CACHE_TTL:
        return _cache[code]["data"]

    try:
        token = await get_token()
        price_data = await fetch_price(code, token)
        pattern = classify_pattern(price_data)

        result = {
            **price_data,
            **pattern,
            "code": code,
            "status": "ok",
        }

        _cache[code] = {"ts": now, "data": result}
        _last_good[code] = result
        return result

    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


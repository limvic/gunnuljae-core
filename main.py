"""
건눌재 Core Engine v1.0
KIS API → 표준 데이터 반환
판단 로직 없음 — 순수 데이터만
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
        raise HTTPException(status_code=503, detail=f"토큰 발급 실패: {data.get('msg1','')}")
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
    return {
        "price":      int(o.get("stck_prpr", 0)),
        "volume":     int(o.get("acml_vol", 0)),
        "high":       int(o.get("stck_hgpr", 0)),
        "low":        int(o.get("stck_lwpr", 0)),
        "open":       int(o.get("stck_oprc", 0)),
        "name":       o.get("hts_kor_isnm", code),
        "change_pct": float(o.get("prdy_ctrt", 0)),
            "is_bullish":int(o.get("stck_prpr",0)) > int(o.get("stck_oprc", 0)),
            "pullback_pct": round(
            (int(o.get("stck_hgpr", 0)) - int(o.get("stck_prpr", 0)))
            / int(o.get("stck_hgpr", 1)) * 100, 2
        ),
  }



async def fetch_volume_avg(code: str, token: str) -> int:
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST01010400",
        "custtype": "P",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
            params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code, "fid_org_adj_prc": "0", "fid_period_div_code": "D"},
            headers=headers,
        )
    data = res.json()
    if data.get("rt_cd") != "0":
        return 0
    outputs = data.get("output", [])[:20]
    vols = [int(o.get("acml_vol", 0)) for o in outputs if int(o.get("acml_vol", 0)) > 0]
    return int(sum(vols) / len(vols)) if vols else 0


async def fetch_vwap(code: str, token: str) -> Optional[int]:
    """VWAP = Σ(체결가 × 체결량) / Σ체결량 — 실패 시 None"""
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": "FHKST03010200",
        "custtype": "P",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                params={"fid_etc_cls_code": "", "fid_cond_mrkt_div_code": "J", "fid_input_iscd": code, "fid_input_hour_1": "153000", "fid_pw_data_incu_yn": "Y"},
                headers=headers,
            )
        data = res.json()
        if data.get("rt_cd") != "0":
            return None
        candles = data.get("output2", [])
        if not candles:
            return None
        total_pv, total_v = 0, 0
        for c in candles:
            try:
                p = int(c.get("stck_prpr", 0) or c.get("stck_cntg_prpr", 0))
                v = int(c.get("cntg_vol", 0))
                if p > 0 and v > 0:
                    total_pv += p * v
                    total_v  += v
            except (ValueError, TypeError):
                continue
        return int(total_pv / total_v) if total_v > 0 else None
    except Exception:
        return None


def calc_tp(price: int, high: int, open_price: int) -> int:
    """tp = (시가+고가+현재가)/3 — VWAP 아닌 대표가격"""
    if open_price > 0 and high > 0:
        return int((open_price + high + price) / 3)
    return price


@app.get("/")
async def root():
    return {
        "service": "건눌재 Core Engine v1.0",
        "mode": KIS_MODE,
        "status": "running",
        "endpoints": {
            "/health": "서버 상태 확인",
            "/stock/{code}": "단일 종목 조회",
            "/stocks": "다종목 조회 (?codes=000660,042700)",
        },
    }


@app.get("/health")
async def health():
    token_ok = bool(_token_cache["token"]) and time.time() < _token_cache["expires_at"]
    return {
        "status": "ok",
        "token": "valid" if token_ok else "not_issued",
        "mode": KIS_MODE,
        "cache_size": len(_cache),
        "kis_key_set": bool(KIS_APP_KEY),
    }


@app.get("/stock/{code}")
async def get_stock(code: str):
    now = time.time()

    if code in _cache and now - _cache[code]["ts"] < CACHE_TTL:
        return _cache[code]["data"]

    if not KIS_APP_KEY:
        return {
            "code": code, "name": f"테스트({code})",
            "price": 85000, "volume": 230000, "volume_avg": 180000,
            "high": 87000, "vwap": None, "tp": 84666,
            "vwap_ok": False, "change_pct": 2.5,
            "mode": "dummy", "cached_at": int(now), "status": "dummy",
        }

    try:
        token = await get_token()
        await asyncio.sleep(0.3)
        price_data = await fetch_price(code, token)
        await asyncio.sleep(0.3)
        vol_avg = await fetch_volume_avg(code, token)
        await asyncio.sleep(0.3)
        vwap = await fetch_vwap(code, token)
        tp = calc_tp(price_data["price"], price_data["high"], price_data["open"])

        result = {
            "code": code, "name": price_data["name"],
            "price": price_data["price"], "volume": price_data["volume"],
            "volume_avg": vol_avg, "high": price_data["high"],
            "vwap": vwap, "tp": tp, "vwap_ok": vwap is not None,
            "change_pct": price_data["change_pct"],
            "mode": KIS_MODE, "cached_at": int(now), "status": "ok",
        }

        _cache[code] = {"ts": now, "data": result}
        _last_good[code] = result
        return result

    except Exception as e:
        if code in _last_good:
            fb = dict(_last_good[code])
            fb["status"] = "fallback"
            fb["error"] = str(e)
            return fb
        raise HTTPException(status_code=503, detail={"status": "error", "error": str(e), "code": code})


@app.get("/stocks")
async def get_stocks(codes: str):
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if len(code_list) > 10:
        raise HTTPException(status_code=400, detail="최대 10종목까지 조회 가능")
    results = []
    for code in code_list:
        try:
            data = await get_stock(code)
            results.append(data)
        except Exception as e:
            results.append({"code": code, "status": "error", "error": str(e)})
        await asyncio.sleep(0.5)
    return {"results": results, "count": len(results)}

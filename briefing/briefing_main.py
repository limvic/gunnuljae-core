"""
Trinity Briefing Service
Railway Cron 전용 — /brief/auto 호출만 담당
"""
import os
import httpx
from fastapi import FastAPI

app = FastAPI(title="Trinity Briefing", version="1.0.0")

FASTAPI_URL = os.environ.get("FASTAPI_URL", "https://fastapi-production-f631.up.railway.app")

@app.get("/")
async def root():
    return {"status": "Trinity Briefing Service"}

@app.post("/run")
async def run_brief():
    """Railway Cron이 호출 → FastAPI /brief/auto 로 전달"""
    async with httpx.AsyncClient(timeout=30) as c:
        res = await c.post(f"{FASTAPI_URL}/brief/auto")
        return res.json()

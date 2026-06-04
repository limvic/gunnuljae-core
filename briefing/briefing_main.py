"""
Trinity Briefing Cron Runner
Railway Cron이 직접 실행 → /brief/auto 호출 후 즉시 종료
웹서버(uvicorn) 아님 — 한 번 실행하고 끝나는 스크립트
"""
import os
import sys
import httpx

FASTAPI_URL = os.environ.get(
    "FASTAPI_URL",
    "https://fastapi-production-f631.up.railway.app"
)

def main():
    try:
        res = httpx.post(f"{FASTAPI_URL}/brief/auto", timeout=15)
        print(f"[BRIEF-CRON] 호출 완료: {res.status_code}")
        print(f"[BRIEF-CRON] 응답: {res.json()}")
    except Exception as e:
        print(f"[BRIEF-CRON] 오류: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()

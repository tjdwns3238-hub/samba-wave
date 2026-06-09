"""진단: 반품 목록 실제 HTTP 엔드포인트 latency 측정 (in-container)."""

import asyncio
import time
from datetime import datetime, timezone, timedelta

import jwt
import httpx
from sqlalchemy import text

from backend.core.config import settings
from backend.db.orm import get_read_session


async def main():
    # 실 사용자 + tenant 조회
    async with get_read_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT id, tenant_id FROM samba_user "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).first()
    uid, tid = row[0], row[1]
    payload = {
        "sub": uid,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        "type": "access",
    }
    if tid:
        payload["tid"] = tid
    tok = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    headers = {"Authorization": f"Bearer {tok}"}

    kst = timezone(timedelta(hours=9))
    today = str(datetime.now(kst).date())
    two_mon = str((datetime.now(kst) - timedelta(days=60)).date())

    base = "http://127.0.0.1:8080/api/v1/samba/returns"
    async with httpx.AsyncClient(timeout=120) as c:
        for label, qs in [
            ("오늘", f"?limit=500&start_date={today}&end_date={today}"),
            ("2달", f"?limit=500&start_date={two_mon}&end_date={today}"),
            ("오늘(2회차)", f"?limit=500&start_date={today}&end_date={today}"),
        ]:
            t = time.perf_counter()
            r = await c.get(base + qs, headers=headers)
            dt = time.perf_counter() - t
            n = len(r.json()) if r.status_code == 200 else -1
            print(f"[{label}] status={r.status_code} 건수={n} {dt:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())

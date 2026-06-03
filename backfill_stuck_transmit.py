"""stuck 상품(failed_at>sent_at) 강제 재전송 백필.

오토튠 자연회전이 느려(특정 상품 앞 4만개 대기) stuck 상품 마켓가격이 며칠 묵음.
이 스크립트는 stuck 상품을 찾아 start_update(skip_refresh=True)로 현재 DB가격을 직접
재전송 → 성공 시 sent_snapshot이 sent_at 갱신 → 치유. 소싱 fetch 없어 소싱 rate-limit
회피. 마켓 부하는 기존 account_sem/전송 세마포어가 자동 throttle.

사용: python backfill_stuck_transmit.py [LIMIT] [CONC] [SITE]
  LIMIT=처리 상품 수(기본 30), CONC=동시 전송(기본 3), SITE=소싱처(기본 MUSINSA)
"""

import asyncio
import json
import sys

import asyncpg

from backend.core.config import settings
from backend.db.orm import get_write_session
from backend.domain.samba.shipment.repository import SambaShipmentRepository
from backend.domain.samba.shipment.service import SambaShipmentService

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 30
CONC = int(sys.argv[2]) if len(sys.argv) > 2 else 3
SITE = sys.argv[3] if len(sys.argv) > 3 else "MUSINSA"


async def find_stuck(limit: int):
    c = await asyncpg.connect(
        host=settings.write_db_host,
        port=int(settings.write_db_port),
        user=settings.write_db_user,
        password=settings.write_db_password,
        database=settings.write_db_name,
        ssl=False,
    )
    # 오래 묵은 것부터(asc nullsfirst) — 우선 복구
    rows = await c.fetch(
        "SELECT id, registered_accounts, last_sent_data FROM samba_collected_product"
        " WHERE source_site=$1 AND status='registered'"
        " AND registered_accounts IS NOT NULL"
        " AND jsonb_array_length(CAST(registered_accounts AS jsonb))>0"
        " ORDER BY last_refreshed_at ASC NULLS FIRST LIMIT $2",
        SITE,
        limit * 6,
    )
    await c.close()
    stuck = []
    for r in rows:
        d = r["last_sent_data"]
        if isinstance(d, str):
            d = json.loads(d)
        is_stuck = False
        for _a, v in (d or {}).items():
            fa = v.get("failed_at")
            sa = v.get("sent_at")
            if fa and ((not sa) or str(fa) > str(sa)):
                is_stuck = True
                break
        if is_stuck:
            ra = r["registered_accounts"]
            if isinstance(ra, str):
                ra = json.loads(ra)
            stuck.append((r["id"], list(ra or [])))
        if len(stuck) >= limit:
            break
    return stuck


sem = asyncio.Semaphore(CONC)
healed = [0]
failed = [0]
done = [0]


async def transmit_one(pid: str, accs: list):
    async with sem:
        try:
            async with get_write_session() as s:
                svc = SambaShipmentService(SambaShipmentRepository(s), s)
                res = await svc.start_update(
                    [pid],
                    ["price", "stock"],
                    accs,
                    skip_unchanged=False,
                    skip_refresh=True,
                    skip_policy_account_filter=True,
                )
                await s.commit()
            ok = any(
                st == "success"
                for pr in (res.get("results") or [])
                for st in (pr.get("transmit_result") or {}).values()
            )
            if ok:
                healed[0] += 1
            else:
                failed[0] += 1
        except Exception as e:  # noqa: BLE001
            failed[0] += 1
            print("ERR", pid, str(e)[:120], flush=True)
        finally:
            done[0] += 1
            if done[0] % 10 == 0:
                print(
                    f"  진행 {done[0]} (성공 {healed[0]} 실패 {failed[0]})",
                    flush=True,
                )


async def main():
    stuck = await find_stuck(LIMIT)
    print(f"[백필] site={SITE} stuck발견={len(stuck)} conc={CONC}", flush=True)
    if not stuck:
        print("stuck 없음 — 종료")
        return
    await asyncio.gather(*[transmit_one(pid, accs) for pid, accs in stuck])
    print(
        f"[백필 완료] 처리={done[0]} 치유={healed[0]} 실패={failed[0]}",
        flush=True,
    )


asyncio.run(main())

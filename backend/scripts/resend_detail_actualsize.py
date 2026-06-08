# -*- coding: utf-8 -*-
"""실측표 보유 상품 마켓별 상세 재전송. skip_policy_account_filter + 3회실패만 스킵."""

import asyncio
import os
import logging
from backend.db.orm import get_write_session
from backend.domain.samba.shipment.service import SambaShipmentService
from backend.domain.samba.shipment.repository import SambaShipmentRepository
from sqlalchemy import text

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("resend")
ACCOUNT = os.getenv("RESEND_ACCOUNT", "")
MARKER = os.getenv("RESEND_MARKER", "")
MAX = int(os.getenv("RESEND_MAX", "0"))
INTERVAL = float(os.getenv("RESEND_INTERVAL", "2.0"))
assert ACCOUNT and MARKER
MK = f"detailResent_{MARKER}"
SEL = """SELECT id FROM samba_collected_product
 WHERE source_site='MUSINSA' AND jsonb_typeof((extra_data::jsonb)->'actualSize')='object'
 AND jsonb_typeof(registered_accounts::jsonb)='array'
 AND (registered_accounts::jsonb @> CAST(:arr AS jsonb)) AND (market_product_nos::jsonb ? :acc)
 AND NOT (extra_data::jsonb ? CAST(:marker AS text)) ORDER BY id LIMIT :lim"""
MARK = """UPDATE samba_collected_product
 SET extra_data=(COALESCE(extra_data::jsonb,'{}'::jsonb)||jsonb_build_object(CAST(:marker AS text),true))::json
 WHERE id=:id"""


async def mark(pid):
    async with get_write_session() as s2:
        await s2.execute(text(MARK), {"marker": MK, "id": pid})
        await s2.commit()


async def main():
    done = ok = fail = 0
    failc = {}
    # 프록시 캐시 명시 로드 — detached 워커는 startup 안 거쳐 캐시 비어있음(playauto 직접연결 방지)
    try:
        from backend.domain.samba.collector.refresher import refresh_db_proxy_cache

        await refresh_db_proxy_cache()
    except Exception as _pe:
        print(f"[proxy] 캐시 로드 실패(무시): {_pe!r}", flush=True)
    while True:
        lim = 50
        if MAX:
            rem = MAX - done
            if rem <= 0:
                break
            lim = min(50, rem)
        async with get_write_session() as s:
            rows = (
                await s.execute(
                    text(SEL),
                    {"arr": f'["{ACCOUNT}"]', "acc": ACCOUNT, "marker": MK, "lim": lim},
                )
            ).fetchall()
        if not rows:
            print(f"[{MARKER}] 완료 성공{ok} 실패{fail}", flush=True)
            break
        for row in rows:
            pid = row[0]
            st = None
            try:
                async with get_write_session() as session:
                    svc = SambaShipmentService(
                        SambaShipmentRepository(session), session
                    )
                    res = await svc.start_update(
                        product_ids=[pid],
                        update_items=["image"],
                        target_account_ids=[ACCOUNT],
                        skip_refresh=True,
                        skip_policy_account_filter=True,
                    )
                st = (res.get("results") or [{}])[0].get("status")
            except Exception as e:
                print(f"[{MARKER}] {pid} 예외:{e!r}", flush=True)
            if st == "completed":
                ok += 1
                try:
                    await mark(pid)
                except Exception as e:
                    print(f"[{MARKER}] {pid} 마커실패:{e!r}", flush=True)
            else:
                fail += 1
                failc[pid] = failc.get(pid, 0) + 1
                # 3회 연속 실패만 스킵마커(영구실패). 1-2회는 재시도(일시실패 보호)
                if failc[pid] >= 3:
                    print(f"[{MARKER}] {pid} 3회실패 스킵 status={st}", flush=True)
                    try:
                        await mark(pid)
                    except Exception:
                        pass
            done += 1
            if done % 20 == 0:
                print(f"[{MARKER}] 진행{done} 성공{ok} 실패{fail}", flush=True)
            await asyncio.sleep(INTERVAL)
    print(f"=== [{MARKER}] 종료 처리{done} 성공{ok} 실패{fail} ===", flush=True)


asyncio.run(main())

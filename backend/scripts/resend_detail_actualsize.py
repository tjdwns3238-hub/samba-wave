# -*- coding: utf-8 -*-
"""실측표 보유 상품 마켓별 상세페이지 재전송(실측표 반영).

- 대상: extra_data.actualSize(object) 보유 + 지정 마켓계정 등록분
- 앱 정상 경로 start_update 사용(세마포어/서킷브레이커/에러분류)
- 순차 1건씩 + 인터벌(마켓 rate limit) — write pool/오토튠 경합 최소화
- 재개 가능: 성공 시 extra_data 마커 박음 → 다음 실행 시 스킵
- skip_refresh=True: cost 재취득 안 함(전송가는 정책 재계산값)

환경변수:
  RESEND_ACCOUNT=ma_xxx   (필수, 마켓 계정 id)
  RESEND_MARKER=11st      (필수, 마커 접미사 — 마켓 구분)
  RESEND_MAX=0            (0=전량, >0=테스트 캡)
  RESEND_INTERVAL=2.0     (건당 인터벌초)

실행(컨테이너 detached):
  nohup /app/backend/.venv/bin/python3 /tmp/resend.py > /tmp/resend.log 2>&1 &
"""

import asyncio
import os
import logging

from backend.db.orm import get_write_session
from backend.domain.samba.shipment.service import SambaShipmentService
from backend.domain.samba.shipment.repository import SambaShipmentRepository
from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("resend")

ACCOUNT = os.getenv("RESEND_ACCOUNT", "")
MARKER = os.getenv("RESEND_MARKER", "")
MAX = int(os.getenv("RESEND_MAX", "0"))
INTERVAL = float(os.getenv("RESEND_INTERVAL", "2.0"))

assert ACCOUNT and MARKER, "RESEND_ACCOUNT, RESEND_MARKER 필수"
MARKER_KEY = f"detailResent_{MARKER}"

SELECT_SQL = """
    SELECT id FROM samba_collected_product
    WHERE source_site='MUSINSA'
      AND jsonb_typeof((extra_data::jsonb)->'actualSize')='object'
      AND jsonb_typeof(registered_accounts::jsonb)='array'
      AND (registered_accounts::jsonb @> CAST(:arr AS jsonb))
      AND (market_product_nos::jsonb ? :acc)
      AND NOT (extra_data::jsonb ? :marker)
    ORDER BY id
    LIMIT :lim
"""

MARK_SQL = """
    UPDATE samba_collected_product
    SET extra_data = (
        COALESCE(extra_data::jsonb,'{}'::jsonb)
        || jsonb_build_object(:marker, true)
    )::json
    WHERE id = :id
"""


async def main():
    done = 0
    ok = 0
    fail = 0
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
                    text(SELECT_SQL),
                    {
                        "arr": f'["{ACCOUNT}"]',
                        "acc": ACCOUNT,
                        "marker": MARKER_KEY,
                        "lim": lim,
                    },
                )
            ).fetchall()
        if not rows:
            log.info(f"[{MARKER}] 처리할 상품 없음 — 완료 (성공 {ok}, 실패 {fail})")
            break
        for row in rows:
            pid = row[0]
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
                    )
                st = (res.get("results") or [{}])[0].get("status")
                if st == "completed":
                    ok += 1
                    async with get_write_session() as s2:
                        await s2.execute(
                            text(MARK_SQL), {"marker": MARKER_KEY, "id": pid}
                        )
                        await s2.commit()
                else:
                    fail += 1
                    err = (res.get("results") or [{}])[0].get("transmit_error")
                    log.warning(f"[{MARKER}] {pid} 실패: {str(err)[:150]}")
            except Exception as e:
                fail += 1
                log.warning(f"[{MARKER}] {pid} 예외: {e!r}")
            done += 1
            if done % 20 == 0:
                log.info(f"[{MARKER}] 진행 {done}건 (성공 {ok}, 실패 {fail})")
            await asyncio.sleep(INTERVAL)
    log.info(f"=== [{MARKER}] 종료: 처리 {done}, 성공 {ok}, 실패 {fail} ===")


if __name__ == "__main__":
    asyncio.run(main())

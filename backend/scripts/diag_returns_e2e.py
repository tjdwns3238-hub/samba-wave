"""진단: 반품 목록 엔드포인트 end-to-end 실측 (write 세션, 오늘 날짜)."""

import asyncio
import time
from datetime import datetime, timezone, timedelta

from sqlalchemy import or_, select
from sqlmodel import col

from backend.db.orm import get_write_session
from backend.domain.samba.returns.repository import SambaReturnRepository
from backend.domain.samba.returns.service import SambaReturnService
from backend.domain.samba.returns.model import SambaReturn
from backend.domain.samba.order.model import SambaOrder


async def main():
    kst = timezone(timedelta(hours=9))
    today = str(datetime.now(kst).date())

    t0 = time.perf_counter()
    async with get_write_session() as s:
        print(f"[acquire] write 세션 획득: {time.perf_counter() - t0:.2f}s")

        # backfill 스캔(전체행) — 매 호출 발생
        t = time.perf_counter()
        claim_statuses = {
            "cancel_requested",
            "cancelling",
            "cancelled",
            "return_requested",
            "returning",
            "returned",
            "exchange_requested",
            "exchanging",
            "exchanged",
        }
        words = ["취소", "반품", "교환"]
        stmt = (
            select(SambaOrder)
            .where(
                or_(
                    col(SambaOrder.status).in_(claim_statuses),
                    *[SambaOrder.shipping_status.ilike(f"%{w}%") for w in words],
                )
            )
            .limit(5000)
        )
        orders = list((await s.execute(stmt)).scalars().all())
        oids = [o.id for o in orders]
        onums = [o.order_number for o in orders if o.order_number]
        ex = (
            await s.execute(
                select(SambaReturn.order_id, SambaReturn.order_number).where(
                    or_(
                        col(SambaReturn.order_id).in_(oids),
                        col(SambaReturn.order_number).in_(onums),
                    )
                )
            )
        ).all()
        print(
            f"[backfill] 주문{len(orders):,} 기존{len(ex):,}: "
            f"{time.perf_counter() - t:.2f}s"
        )

        # 실제 목록 (오늘)
        t = time.perf_counter()
        svc = SambaReturnService(SambaReturnRepository(s))
        returns = await svc.list_returns(
            skip=0, limit=500, start_date=today, end_date=today
        )
        print(
            f"[list_filtered] 오늘 {len(returns):,}건: {time.perf_counter() - t:.2f}s"
        )

    print(f"[total] {time.perf_counter() - t0:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())

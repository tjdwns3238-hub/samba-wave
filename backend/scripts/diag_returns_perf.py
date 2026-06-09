"""진단: 반품 목록 30초 로딩 — 어느 단계가 느린지 실측."""

import asyncio
import time

from sqlalchemy import or_, select
from sqlmodel import col

from backend.db.orm import get_read_session
from backend.domain.samba.order.model import SambaOrder
from backend.domain.samba.returns.model import SambaReturn


async def main():
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
    claim_words = ["취소", "반품", "교환"]

    async with get_read_session() as s:
        # 1) backfill claim 주문 스캔 (전체 행)
        t = time.perf_counter()
        stmt = (
            select(SambaOrder)
            .where(
                or_(
                    col(SambaOrder.status).in_(claim_statuses),
                    *[SambaOrder.shipping_status.ilike(f"%{w}%") for w in claim_words],
                )
            )
            .limit(5000)
        )
        orders = list((await s.execute(stmt)).scalars().all())
        print(
            f"[1] backfill 주문 스캔(전체행 {len(orders):,}건): {time.perf_counter() - t:.2f}s"
        )

        # 1b) 같은 조건 count만
        t = time.perf_counter()
        from sqlalchemy import func

        cnt = (
            await s.execute(
                select(func.count())
                .select_from(SambaOrder)
                .where(
                    or_(
                        col(SambaOrder.status).in_(claim_statuses),
                        *[
                            SambaOrder.shipping_status.ilike(f"%{w}%")
                            for w in claim_words
                        ],
                    )
                )
            )
        ).scalar()
        print(f"[1b] 같은 조건 COUNT({cnt:,}): {time.perf_counter() - t:.2f}s")

        # 2) existing 체크
        t = time.perf_counter()
        order_ids = [o.id for o in orders]
        order_numbers = [o.order_number for o in orders if o.order_number]
        existing_stmt = select(SambaReturn.order_id, SambaReturn.order_number).where(
            or_(
                col(SambaReturn.order_id).in_(order_ids),
                col(SambaReturn.order_number).in_(order_numbers),
            )
        )
        rows = (await s.execute(existing_stmt)).all()
        print(f"[2] existing 체크({len(rows):,}건): {time.perf_counter() - t:.2f}s")

        # 3) 실제 목록 쿼리 (오늘 하루 가정, 날짜 무관하게 최근 50)
        t = time.perf_counter()
        lst = (
            (
                await s.execute(
                    select(SambaReturn)
                    .order_by(SambaReturn.created_at.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        print(f"[3] 반품목록 50건: {time.perf_counter() - t:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())

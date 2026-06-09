"""백필: 반품 order_date(주문일) NULL 채움 — 날짜필터 정상화 + 목록 로딩 속도 개선.

원인: order_date NULL 반품(2,762건)이 날짜필터 `OR order_date IS NULL`로
      항상 끌려와 '오늘 하루' 조회도 500건 상한을 채워 프론트 렌더 30초.
교정: NULL을 주문 paid_at(없으면 주문 created_at, 그것도 없으면 반품 created_at)으로 채움.
"""

import asyncio

from sqlalchemy import text

from backend.db.orm import get_write_session


async def main():
    async with get_write_session() as s:
        before = (
            await s.execute(
                text(
                    "SELECT count(*) total, "
                    "count(*) FILTER (WHERE order_date IS NULL) nulls, "
                    "count(*) FILTER (WHERE order_date >= now()::date) today "
                    "FROM samba_return"
                )
            )
        ).first()
        print(f"[before] 전체={before[0]:,} NULL={before[1]:,} 오늘이후={before[2]:,}")

        # 1단계: 주문과 매칭되는 행 → 주문 paid_at(없으면 주문 created_at)
        r1 = await s.execute(
            text(
                "UPDATE samba_return rt "
                "SET order_date = COALESCE(o.paid_at, o.created_at) "
                "FROM samba_order o "
                "WHERE rt.order_id = o.id "
                "AND rt.order_date IS NULL "
                "AND COALESCE(o.paid_at, o.created_at) IS NOT NULL"
            )
        )
        print(f"[1] 주문일 기준 채움: {r1.rowcount:,}건")

        # 2단계: 그래도 NULL(주문 매칭 실패 등) → 반품 자체 created_at
        r2 = await s.execute(
            text(
                "UPDATE samba_return "
                "SET order_date = created_at "
                "WHERE order_date IS NULL AND created_at IS NOT NULL"
            )
        )
        print(f"[2] 반품 생성일 기준 채움: {r2.rowcount:,}건")

        after = (
            await s.execute(
                text(
                    "SELECT count(*) total, "
                    "count(*) FILTER (WHERE order_date IS NULL) nulls, "
                    "count(*) FILTER (WHERE order_date >= now()::date) today "
                    "FROM samba_return"
                )
            )
        ).first()
        print(f"[after] 전체={after[0]:,} NULL={after[1]:,} 오늘이후={after[2]:,}")

        if after[0] != before[0]:
            print("!! 전체 건수 변동 — 롤백")
            await s.rollback()
            return

        await s.commit()
        print("[commit] 완료")


if __name__ == "__main__":
    asyncio.run(main())

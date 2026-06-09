"""진단: 반품/교환 표 '지역(region)' 칸이 비는 이유 확인 (프로덕션 실데이터)."""

import asyncio

from sqlalchemy import text

from backend.db.orm import get_read_session


async def main():
    async with get_read_session() as s:
        print("=== samba_return region 채움 현황 ===")
        r = (
            await s.execute(
                text(
                    "SELECT count(*) total, "
                    "count(*) FILTER (WHERE region IS NOT NULL AND region <> '') filled, "
                    "count(*) FILTER (WHERE region IS NULL OR region = '') empty "
                    "FROM samba_return"
                )
            )
        ).first()
        print(f"  전체={r[0]:,}  region채움={r[1]:,}  region빔={r[2]:,}")

        print("\n=== region 빈 반품의 연결 주문 customer_address 현황 ===")
        r = (
            await s.execute(
                text(
                    "SELECT "
                    "count(*) total, "
                    "count(*) FILTER (WHERE o.customer_address IS NOT NULL AND o.customer_address <> '') addr_filled, "
                    "count(*) FILTER (WHERE o.customer_address IS NULL OR o.customer_address = '') addr_empty, "
                    "count(*) FILTER (WHERE o.id IS NULL) no_order "
                    "FROM samba_return rt LEFT JOIN samba_order o ON o.id = rt.order_id "
                    "WHERE rt.region IS NULL OR rt.region = ''"
                )
            )
        ).first()
        print(
            f"  region빈반품={r[0]:,}  주문주소있음={r[1]:,}  "
            f"주문주소없음={r[2]:,}  주문매칭실패={r[3]:,}"
        )

        print("\n=== 전체 samba_order customer_address 채움 현황 ===")
        r = (
            await s.execute(
                text(
                    "SELECT count(*) total, "
                    "count(*) FILTER (WHERE customer_address IS NOT NULL AND customer_address <> '') filled "
                    "FROM samba_order"
                )
            )
        ).first()
        print(f"  전체주문={r[0]:,}  주소채움={r[1]:,}")

        print("\n=== 최근 반품 10건 샘플 (region / 주문주소 / region생성테스트) ===")
        rows = (
            await s.execute(
                text(
                    "SELECT rt.id, rt.market, "
                    "COALESCE(NULLIF(rt.region,''),'(빈값)') region, "
                    "COALESCE(NULLIF(o.customer_address,''),'(빈값)') addr "
                    "FROM samba_return rt LEFT JOIN samba_order o ON o.id = rt.order_id "
                    "ORDER BY rt.created_at DESC LIMIT 10"
                )
            )
        ).all()
        for row in rows:
            addr = (row[3] or "")[:30]
            print(f"  [{row[1]:>8}] region={row[2]:<10} 주소={addr}")


if __name__ == "__main__":
    asyncio.run(main())

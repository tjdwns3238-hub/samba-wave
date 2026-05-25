"""사이트별 발주완료 + 배송전 주문 1건씩 추출 (취소 분석용)."""

import asyncio

import asyncpg


async def main() -> None:
    from backend.core.config import settings

    conn = await asyncpg.connect(
        host=settings.write_db_host,
        port=settings.write_db_port,
        user=settings.write_db_user,
        password=settings.write_db_password,
        database=settings.write_db_name,
        ssl=False,
    )
    try:
        rows = await conn.fetch(
            """
            WITH ranked AS (
              SELECT
                o.source_site,
                o.sourcing_order_number,
                o.sourcing_account_id,
                o.order_number,
                o.channel_name,
                o.product_name,
                o.status,
                o.shipping_status,
                o.paid_at,
                ROW_NUMBER() OVER (
                  PARTITION BY o.source_site
                  ORDER BY o.paid_at DESC NULLS LAST
                ) AS rn
              FROM samba_order o
              WHERE o.sourcing_order_number IS NOT NULL
                AND o.sourcing_order_number <> ''
                AND COALESCE(o.source_site, '') <> ''
                AND COALESCE(o.status, '') NOT IN (
                  'cancel_requested','cancelling','cancelled',
                  'return_requested','returning','returned','return_completed',
                  'exchange_requested','exchanging','exchanged',
                  'exchange_pending','exchange_done',
                  'ship_failed','undeliverable'
                )
                AND COALESCE(o.shipping_status, '') NOT LIKE '%배송중%'
                AND COALESCE(o.shipping_status, '') NOT LIKE '%배송완료%'
                AND COALESCE(o.shipping_status, '') NOT LIKE '%구매확정%'
                AND COALESCE(o.shipping_status, '') NOT LIKE '%송장전송완료%'
                AND COALESCE(o.shipping_status, '') NOT LIKE '%취소%'
            )
            SELECT * FROM ranked WHERE rn = 1 ORDER BY source_site
            """
        )
        print(
            f"{'site':<14} {'sourcing_order_no':<28} {'acct':<14} {'status':<22} {'ship_status':<14} {'paid_at'}"
        )
        for r in rows:
            print(
                f"{(r['source_site'] or ''):<14} "
                f"{(r['sourcing_order_number'] or ''):<28} "
                f"{(r['sourcing_account_id'] or '')[:12]:<14} "
                f"{(r['status'] or '')[:20]:<22} "
                f"{(r['shipping_status'] or '')[:12]:<14} "
                f"{r['paid_at']}"
            )
        print(f"\nTotal sites with cancellable orders: {len(rows)}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

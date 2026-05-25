"""DB에 존재하는 모든 source_site + 카운트."""

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
            SELECT
              COALESCE(source_site, '(null)') AS site,
              COUNT(*) AS total,
              COUNT(*) FILTER (
                WHERE sourcing_order_number IS NOT NULL
                  AND sourcing_order_number <> ''
              ) AS with_order_no,
              COUNT(*) FILTER (
                WHERE sourcing_order_number IS NOT NULL
                  AND sourcing_order_number <> ''
                  AND COALESCE(status, '') NOT IN (
                    'cancel_requested','cancelling','cancelled',
                    'return_requested','returning','returned','return_completed',
                    'exchange_requested','exchanging','exchanged',
                    'exchange_pending','exchange_done',
                    'ship_failed','undeliverable'
                  )
                  AND COALESCE(shipping_status, '') NOT LIKE '%배송중%'
                  AND COALESCE(shipping_status, '') NOT LIKE '%배송완료%'
                  AND COALESCE(shipping_status, '') NOT LIKE '%구매확정%'
                  AND COALESCE(shipping_status, '') NOT LIKE '%송장전송완료%'
                  AND COALESCE(shipping_status, '') NOT LIKE '%취소%'
              ) AS cancellable
            FROM samba_order
            GROUP BY source_site
            ORDER BY total DESC
            """
        )
        print(f"{'site':<20} {'total':>8} {'with_no':>8} {'cancellable':>12}")
        for r in rows:
            print(
                f"{r['site']:<20} {r['total']:>8} {r['with_order_no']:>8} {r['cancellable']:>12}"
            )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

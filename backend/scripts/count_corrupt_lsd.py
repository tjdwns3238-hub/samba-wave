"""last_sent_data 가 JSON 배열(오염)인 상품 수 집계."""

import asyncio

from sqlalchemy import text

from backend.db.orm import get_write_sessionmaker


async def main():
    Session = get_write_sessionmaker()
    async with Session() as session:
        # jsonb_typeof 로 타입별 집계
        rows = (
            await session.execute(
                text(
                    "SELECT jsonb_typeof(CAST(last_sent_data AS jsonb)) AS t, COUNT(*) "
                    "FROM samba_collected_product "
                    "WHERE last_sent_data IS NOT NULL "
                    "GROUP BY 1 ORDER BY 2 DESC"
                )
            )
        ).all()
        print("=== last_sent_data jsonb 타입 분포 ===")
        for t, c in rows:
            print(f"  {t}: {c}")

        # 배열(오염) 상품 샘플
        bad = (
            await session.execute(
                text(
                    "SELECT id, source_site, status, last_sent_data "
                    "FROM samba_collected_product "
                    "WHERE last_sent_data IS NOT NULL "
                    "  AND jsonb_typeof(CAST(last_sent_data AS jsonb)) = 'array' "
                    "LIMIT 10"
                )
            )
        ).all()
        print(f"\n=== 오염(array) 샘플 {len(bad)}건 ===")
        for r in bad:
            print(f"  {r[0]} {r[1]} {r[2]} lsd={str(r[3])[:120]}")


if __name__ == "__main__":
    asyncio.run(main())

"""배포 후 DB 상태 검증"""

import asyncio

import asyncpg

from backend.core.config import settings


async def main() -> None:
    conn = await asyncpg.connect(
        host=settings.write_db_host,
        port=settings.write_db_port,
        user=settings.write_db_user,
        password=settings.write_db_password,
        database=settings.write_db_name,
        ssl=False,
        timeout=30,
    )
    try:
        ver = await conn.fetch("SELECT version_num FROM alembic_version")
        print("alembic_version:", [v["version_num"] for v in ver])

        idx = await conn.fetch(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_samba_order_lotteon_line'"
        )
        print("lotteon_idx:", [i["indexdef"] for i in idx])

        # 중복 잔존 확인
        dup = await conn.fetch(
            """
            SELECT tenant_id, od_no, od_seq, COUNT(*) as c
            FROM samba_order WHERE source = 'lotteon' AND od_no IS NOT NULL AND od_no <> ''
            GROUP BY tenant_id, od_no, od_seq HAVING COUNT(*) > 1 LIMIT 5
            """
        )
        print(f"잔존 중복: {len(dup)}건 (샘플)")
        for d in dup:
            print(f"  {dict(d)}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

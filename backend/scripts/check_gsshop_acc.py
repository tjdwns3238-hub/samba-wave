"""GS샵 관련 계정/소싱처/로그 출처 진단"""

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
        # 1) samba_market_account 에 GS샵/GSShop 가 있는지
        rows = await conn.fetch(
            """
            SELECT id, tenant_id, market_type, market_name, seller_id, is_active
            FROM samba_market_account
            WHERE market_name ILIKE '%GS%' OR market_type ILIKE '%gs%'
            """
        )
        print(f"[1] samba_market_account GS 매칭: {len(rows)}건")
        for r in rows:
            print(f"  {dict(r)}")

        # 2) samba_order source_site / channel_name 에 GS 등장
        rows2 = await conn.fetch(
            """
            SELECT source_site, channel_name, COUNT(*) c
            FROM samba_order
            WHERE source_site ILIKE '%GS%' OR channel_name ILIKE '%GS%'
            GROUP BY source_site, channel_name
            ORDER BY c DESC
            LIMIT 10
            """
        )
        print(f"\n[2] samba_order GS 매칭: {len(rows2)}건")
        for r in rows2:
            print(f"  {dict(r)}")

        # 3) PlayAuto SiteId GSShop 변환 로직 source — playauto alias map
        rows3 = await conn.fetch(
            "SELECT key, value FROM samba_settings WHERE key LIKE 'store_playauto%' OR key LIKE '%:store_playauto%' LIMIT 5"
        )
        print(f"\n[3] PlayAuto store 설정: {len(rows3)}건")
        for r in rows3:
            print(f"  key={r['key']} value_sample={str(r['value'])[:200]}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

"""별칭 배지 진단 — store_playauto alias1~5 + 샘플 주문 sales_channel_alias 확인."""

import asyncio
import asyncpg
from backend.core.config import settings


async def main():
    conn = await asyncpg.connect(
        host=settings.write_db_host,
        port=settings.write_db_port,
        user=settings.write_db_user,
        password=settings.write_db_password,
        database=settings.write_db_name,
        ssl=settings.db_ssl_required,
    )
    try:
        row = await conn.fetchrow(
            "SELECT value FROM samba_settings WHERE key='store_playauto'"
        )
        print("=== store_playauto 설정 ===")
        if not row:
            print("  ❌ 행 없음 — 한 번도 저장 안 됨")
        else:
            v = row["value"]
            if isinstance(v, dict):
                for k in ("alias1", "alias2", "alias3", "alias4", "alias5"):
                    print(f"  {k}: {v.get(k, '(없음)')!r}")
                other_keys = [k for k in v.keys() if not k.startswith("alias")]
                print(f"  (기타 키: {other_keys})")
            else:
                print(f"  ❌ value 가 dict 아님: {type(v).__name__} = {v!r}")

        print()
        print("=== 샘플 주문 sales_channel_alias ===")
        rows = await conn.fetch(
            "SELECT order_number, source_site, sales_channel_alias, source "
            "FROM samba_order "
            "WHERE source='playauto' "
            "AND sales_channel_alias LIKE 'KT알파%' "
            "ORDER BY created_at DESC LIMIT 10"
        )
        for r in rows:
            print(
                f"  {r['order_number']:30s} src_site={r['source_site']!r:20s} "
                f"alias={r['sales_channel_alias']!r}"
            )
        if not rows:
            print("  (KT알파 매칭 주문 없음)")

        print()
        print("=== sales_channel_alias 분포 (playauto 전체) ===")
        rows2 = await conn.fetch(
            "SELECT sales_channel_alias, COUNT(*) as c "
            "FROM samba_order WHERE source='playauto' "
            "GROUP BY sales_channel_alias ORDER BY c DESC LIMIT 20"
        )
        for r in rows2:
            print(f"  {r['c']:6d}  {r['sales_channel_alias']!r}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

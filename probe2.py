import asyncio, asyncpg
from backend.core.config import settings


async def main():
    c = await asyncpg.connect(
        host="172.18.0.2",
        port=5432,
        user=settings.read_db_user,
        password=settings.read_db_password,
        database=settings.read_db_name,
        ssl=False,
    )

    print("=== samba_tenants ===")
    for r in await c.fetch("SELECT id, name, created_at FROM samba_tenants ORDER BY created_at"):
        print(f"  tenant={r['id']} name='{r['name']}'")

    print()
    print("=== samba_extension_key 컬럼 ===")
    cols = await c.fetch(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name='samba_extension_key' ORDER BY ordinal_position"
    )
    for r in cols:
        print(f"  {r['column_name']} ({r['data_type']})")

    print()
    print("=== samba_extension_key 전체 (마스킹) ===")
    try:
        rows = await c.fetch(
            "SELECT * FROM samba_extension_key ORDER BY last_used_at DESC NULLS LAST LIMIT 20"
        )
        for r in rows:
            d = dict(r)
            # mask any 'key' field
            for k in list(d.keys()):
                v = d[k]
                if isinstance(v, str) and len(v) > 30:
                    d[k] = v[:8] + "..." + v[-4:]
            print(f"  {d}")
    except Exception as e:
        print(f"  error: {e}")

    print()
    print("=== samba_monitor_event 컬럼 ===")
    cols = await c.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name='samba_monitor_event' ORDER BY ordinal_position"
    )
    for r in cols:
        print(f"  {r['column_name']}")

    print()
    print("=== samba_order 컬럼 ===")
    cols = await c.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name='samba_order' ORDER BY ordinal_position LIMIT 60"
    )
    for r in cols:
        print(f"  {r['column_name']}")

    await c.close()


asyncio.run(main())

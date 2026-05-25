"""LOTTEON 자동로그인 라디오 계정 + tenant 일치 진단"""
import asyncio
import asyncpg
from backend.core.config import settings


async def main():
    conn = await asyncpg.connect(
        host="172.18.0.2",
        port=5432,
        user=settings.read_db_user,
        password=settings.read_db_password,
        database=settings.read_db_name,
        ssl=False,
    )

    print("=" * 80)
    print("[1] samba_sourcing_account — LOTTEON 관련 전부")
    print("=" * 80)
    rows = await conn.fetch(
        """
        SELECT id, tenant_id, site_name, is_active, is_login_default,
               account_label, username, created_at, updated_at
        FROM samba_sourcing_account
        WHERE site_name ILIKE '%lotte%' OR site_name ILIKE '%롯데%' OR site_name ILIKE '%LOTTEON%'
        ORDER BY updated_at DESC NULLS LAST
        """
    )
    for r in rows:
        print(
            f"  id={r['id']} tenant={r['tenant_id']} site='{r['site_name']}' "
            f"active={r['is_active']} default={r['is_login_default']} "
            f"label='{r['account_label']}' user='{r['username']}' "
            f"updated={r['updated_at']}"
        )
    print(f"총 {len(rows)}건")

    print()
    print("=" * 80)
    print("[2] is_login_default=True 행만 site별")
    print("=" * 80)
    rows2 = await conn.fetch(
        """
        SELECT site_name, tenant_id, COUNT(*) AS cnt,
               array_agg(DISTINCT is_active) AS actives,
               array_agg(account_label) AS labels
        FROM samba_sourcing_account
        WHERE is_login_default = true
        GROUP BY site_name, tenant_id
        ORDER BY site_name
        """
    )
    for r in rows2:
        print(
            f"  site='{r['site_name']}' tenant={r['tenant_id']} cnt={r['cnt']} "
            f"active={r['actives']} labels={r['labels']}"
        )

    print()
    print("=" * 80)
    print("[3] 모든 테넌트 키 — tenant_id 매핑")
    print("=" * 80)
    try:
        rows3 = await conn.fetch(
            """
            SELECT key_id, tenant_id, label, created_at, last_used_at
            FROM samba_tenant_key
            ORDER BY last_used_at DESC NULLS LAST
            LIMIT 20
            """
        )
        for r in rows3:
            print(
                f"  key_id={r['key_id']} tenant={r['tenant_id']} "
                f"label='{r['label']}' last_used={r['last_used_at']}"
            )
    except Exception as e:
        print(f"  samba_tenant_key 조회 실패: {e}")

    print()
    print("=" * 80)
    print("[4] samba_tenant — 테넌트 목록")
    print("=" * 80)
    try:
        rows4 = await conn.fetch(
            "SELECT id, name, created_at FROM samba_tenant ORDER BY created_at"
        )
        for r in rows4:
            print(f"  tenant_id={r['id']} name='{r['name']}'")
    except Exception as e:
        print(f"  samba_tenant 조회 실패: {e}")

    print()
    print("=" * 80)
    print("[5] 최근 LOTTEON 자동로그인 관련 잡/이벤트")
    print("=" * 80)
    try:
        rows5 = await conn.fetch(
            """
            SELECT id, event_type, severity, message, created_at, extra
            FROM samba_monitor_event
            WHERE (message ILIKE '%LOTTEON%' OR message ILIKE '%롯데%' OR message ILIKE '%자동로그인%' OR message ILIKE '%login%')
              AND created_at > NOW() - INTERVAL '6 hours'
            ORDER BY created_at DESC
            LIMIT 20
            """
        )
        for r in rows5:
            print(
                f"  {r['created_at']} [{r['severity']}] {r['event_type']}: "
                f"{(r['message'] or '')[:120]}"
            )
        print(f"총 {len(rows5)}건")
    except Exception as e:
        print(f"  samba_monitor_event 조회 실패: {e}")

    print()
    print("=" * 80)
    print("[6] 최근 LOTTEON 관련 송장수집/주문매칭 잡")
    print("=" * 80)
    try:
        rows6 = await conn.fetch(
            """
            SELECT order_number, channel, account_id, source_site, source_account_id,
                   product_name, paid_at, created_at
            FROM samba_order
            WHERE source_site ILIKE '%LOTTE%' OR source_site ILIKE '%롯데%'
            ORDER BY paid_at DESC NULLS LAST
            LIMIT 10
            """
        )
        for r in rows6:
            print(
                f"  order={r['order_number']} ch={r['channel']} src={r['source_site']} "
                f"src_acc={r['source_account_id']} paid={r['paid_at']} "
                f"name='{(r['product_name'] or '')[:40]}'"
            )
        print(f"총 {len(rows6)}건")
    except Exception as e:
        print(f"  samba_order 조회 실패: {e}")

    await conn.close()


asyncio.run(main())

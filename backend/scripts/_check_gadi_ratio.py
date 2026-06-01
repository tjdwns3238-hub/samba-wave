"""가디 스마트스토어 계정 판매상품비중 체크.

판매상품비중 = (직전 3개월 판매발생 unique SKU 수) / (현재 등록상품 수)
스마트스토어 정책: 3% 이상 필요
"""

import asyncio
from datetime import datetime, timedelta, timezone

import asyncpg

from backend.core.config import settings


KST = timezone(timedelta(hours=9))


async def main() -> None:
    conn = await asyncpg.connect(
        host=settings.read_db_host,
        port=settings.read_db_port,
        user=settings.read_db_user,
        password=settings.read_db_password,
        database=settings.read_db_name,
        ssl=settings.use_db_ssl,
    )

    # 1. 가디 스마트스토어 계정 찾기
    accounts = await conn.fetch(
        """
        SELECT id, tenant_id, market_type, market_name, account_label, seller_id, business_name, is_active
        FROM samba_market_account
        WHERE market_type IN ('smartstore', 'SMARTSTORE')
          AND (account_label ILIKE '%가디%' OR business_name ILIKE '%가디%'
               OR seller_id ILIKE '%gadi%' OR account_label ILIKE '%gadi%')
        ORDER BY id
        """
    )

    print("=" * 80)
    print("가디 스마트스토어 계정 후보")
    print("=" * 80)
    if not accounts:
        accounts = await conn.fetch(
            """
            SELECT id, tenant_id, market_type, market_name, account_label, seller_id, business_name, is_active
            FROM samba_market_account
            WHERE market_type IN ('smartstore', 'SMARTSTORE')
            ORDER BY account_label
            """
        )
        print("(가디 직매칭 실패 → 전체 스마트스토어 계정 출력)")
    for a in accounts:
        print(f"  id={a['id']} label={a['account_label']!r} seller={a['seller_id']} biz={a['business_name']!r} active={a['is_active']} tenant={a['tenant_id']}")
    print()

    now_kst = datetime.now(KST)
    cutoff = now_kst - timedelta(days=90)

    for a in accounts:
        label = a["account_label"] or ""
        biz = a["business_name"] or ""
        if "가디" not in label and "가디" not in biz and "gadi" not in (a["seller_id"] or "").lower():
            continue
        acc_id = a["id"]
        tenant_id = a["tenant_id"]
        print("=" * 80)
        print(f"[계정] {label} (id={acc_id}, seller={a['seller_id']}, tenant={tenant_id})")
        print("=" * 80)

        # 2. 현재 등록상품 수 (registered_accounts @> [account_id])
        reg_total = await conn.fetchval(
            """
            SELECT COUNT(*) FROM samba_collected_product
            WHERE registered_accounts @> $1::jsonb
            """,
            f'["{acc_id}"]',
        )
        # 추가: sale_status 분포
        sale_dist = await conn.fetch(
            """
            SELECT sale_status, COUNT(*) AS cnt
            FROM samba_collected_product
            WHERE registered_accounts @> $1::jsonb
            GROUP BY sale_status
            ORDER BY cnt DESC
            """,
            f'["{acc_id}"]',
        )
        print(f"등록상품 총 {reg_total:,}개")
        for r in sale_dist:
            print(f"   - {r['sale_status']}: {r['cnt']:,}")

        # 3. 직전 90일 판매 (channel_id 기준 + channel_name fallback)
        sales = await conn.fetchrow(
            """
            SELECT
              COUNT(*) AS orders,
              COUNT(DISTINCT collected_product_id) AS uniq_collected,
              COUNT(DISTINCT product_id) AS uniq_product,
              COALESCE(SUM(COALESCE(total_payment_amount, sale_price * quantity)), 0) AS gmv
            FROM samba_order
            WHERE channel_id = $1
              AND COALESCE(paid_at, created_at) >= $2
              AND COALESCE(status, '') NOT IN ('cancelled', 'cancel_completed', 'returned', 'cancel_requested')
            """,
            acc_id,
            cutoff,
        )
        # tenant + channel_name fallback
        sales_fallback = await conn.fetchrow(
            """
            SELECT
              COUNT(*) AS orders,
              COUNT(DISTINCT collected_product_id) AS uniq_collected,
              COALESCE(SUM(COALESCE(total_payment_amount, sale_price * quantity)), 0) AS gmv
            FROM samba_order
            WHERE tenant_id = $1
              AND COALESCE(channel_name, '') ILIKE $2
              AND COALESCE(paid_at, created_at) >= $3
              AND COALESCE(status, '') NOT IN ('cancelled', 'cancel_completed', 'returned', 'cancel_requested')
            """,
            tenant_id,
            f"%{label}%",
            cutoff,
        )

        orders = sales["orders"] or 0
        uniq_c = sales["uniq_collected"] or 0
        uniq_p = sales["uniq_product"] or 0
        gmv = int(sales["gmv"] or 0)

        # channel_id 매칭이 0이면 fallback 사용
        used_fallback = orders == 0 and (sales_fallback["orders"] or 0) > 0
        if used_fallback:
            print("⚠️  channel_id 매칭 0 → channel_name LIKE fallback 사용")
            orders = sales_fallback["orders"] or 0
            uniq_c = sales_fallback["uniq_collected"] or 0
            gmv = int(sales_fallback["gmv"] or 0)

        print(f"직전 90일 주문건수: {orders:,}")
        print(f"직전 90일 판매 unique collected_product_id: {uniq_c:,}")
        print(f"직전 90일 판매 unique product_id: {uniq_p:,}")
        print(f"직전 90일 거래액: {gmv:,}원")

        # 4. 비중 계산
        if reg_total > 0:
            denom = reg_total
            num = max(uniq_c, uniq_p)
            ratio = (num / denom) * 100
            badge = "✅ 통과" if ratio >= 3 else "❌ 미달"
            print(f"판매상품비중: {num:,} / {denom:,} = {ratio:.2f}% {badge}")
            if ratio < 3:
                need = int(denom * 0.03) - num + 1
                print(f"   → 3% 도달까지 추가로 판매발생 SKU {need:,}개 필요")
                print(f"   → 또는 등록상품 {int((num/0.03))-denom:,}개 감축으로 도달")
        else:
            print("판매상품비중: 등록상품 0개 — 계산 불가")

        # 5. 한도 시뮬레이션
        if gmv >= 60_000_000 or orders >= 1000:
            limit = 50000
        elif gmv >= 20_000_000 or orders >= 400:
            limit = 20000
        elif gmv >= 10_000_000 or orders >= 200:
            limit = 10000
        elif gmv >= 5_000_000 or orders >= 100:
            limit = 5000
        else:
            limit = 1000
        print(f"실적 기준 한도 후보(거래/건수): {limit:,}개")
        if reg_total > 0:
            ratio_now = (max(uniq_c, uniq_p) / reg_total) * 100
            if ratio_now < 3:
                print(f"   ⚠️  비중 미달({ratio_now:.2f}%) → 실제 한도는 한 단계 강등 가능성")
        diff = limit - reg_total
        if diff >= 0:
            print(f"현재 등록 {reg_total:,}개 / 한도 {limit:,}개 → 여유 {diff:,}개")
        else:
            print(f"현재 등록 {reg_total:,}개 / 한도 {limit:,}개 → 초과 {-diff:,}개 (자동 판매중지 위험)")
        print()

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

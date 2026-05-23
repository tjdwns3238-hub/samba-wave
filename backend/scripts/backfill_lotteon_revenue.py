"""롯데ON 정산금액(revenue/fee_rate) 백필 스크립트.

배경
----
2026-05-20 a401c15e 이후 ~ 2026-05-23 까지 `samba_order.revenue` 가
`slAmt − 셀러부담할인 − bseCmsn` 공식으로 계산되어 롯데/제휴몰 부담할인 만큼
과대 계상됨. 샵마인 셀러센터 정산내역 공식
(`정산예상 = actualAmt − bseCmsn − pcsCmsn`)과 일치시키도록 재계산.

대상
----
- source = 'lotteon'
- status NOT IN ('confirmed', 'cancelled', 'returned')  ← 구매확정 후엔 SettleItmdSales pymtAmt 매칭이 정확값이므로 제외
- total_payment_amount > 0
- sale_price > 0

계산
----
1. 카테고리: samba_collected_product.category (collected_product_id 조인)
2. fee_rate = LOTTEON_CATEGORY_FEE_RATES[1뎁스] (없으면 DEFAULT)
3. bse_cmsn = sale_price × fee_rate / 100
4. new_revenue = total_payment_amount − bse_cmsn   (PCS 무시 — chNo DB 미저장, 보수적)
5. new_fee_rate = bse_cmsn / total_payment_amount × 100  (롯데 화면 실수수료율 정의)

실행
----
DRY_RUN=1 → 차이만 출력(기본). DRY_RUN=0 → 실제 UPDATE.

VM 컨테이너에서:
  scp -i $HOME/samba-vm-secrets/ssh/deploy_key backend/scripts/backfill_lotteon_revenue.py sbk0674@api.samba-wave.co.kr:/tmp/
  ssh ... 'sudo docker cp /tmp/backfill_lotteon_revenue.py samba-samba-api-1:/tmp/'
  ssh ... 'sudo docker exec -e DRY_RUN=1 samba-samba-api-1 /app/backend/.venv/bin/python /tmp/backfill_lotteon_revenue.py'
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 컨테이너 안에서 backend/ 를 sys.path 에 추가
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))

import asyncpg  # noqa: E402

from backend.core.config import settings  # noqa: E402
from backend.domain.samba.proxy.lotteon.category_fees import (  # noqa: E402
    get_fee_rate_for_category,
)

DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"

# 안전 임계 — 차이 1원 이내는 라운딩 노이즈로 보고 스킵
DIFF_THRESHOLD = 2

# 구매확정/취소/반품 상태 — SettleItmdSales 매칭이 정확값이므로 백필 제외
EXCLUDED_STATUSES = ("confirmed", "cancelled", "returned", "refunded")


async def main() -> None:
    # asyncpg 직접 연결 — SQLAlchemy 의존성 우회
    # VM 컨테이너 내부에서 cloud-sql-proxy 사이드카 IP로 직접 연결
    # (write_db_host는 보통 DNS 명이라 컨테이너 안에선 해석 불가 — 메모리 참조 172.18.0.2)
    db_host = os.environ.get("DB_HOST_OVERRIDE", "172.18.0.2")
    conn = await asyncpg.connect(
        host=db_host,
        port=int(settings.write_db_port),
        user=settings.write_db_user,
        password=settings.write_db_password,
        database=settings.write_db_name,
        ssl=False,
    )

    # 1. 대상 행 조회 — collected_product 카테고리 조인
    rows = await conn.fetch(
        """
        SELECT
            o.id,
            o.order_number,
            o.sale_price,
            o.total_payment_amount,
            o.revenue,
            o.fee_rate,
            o.status,
            cp.category AS category
        FROM samba_order o
        LEFT JOIN samba_collected_product cp ON cp.id = o.collected_product_id
        WHERE o.source = 'lotteon'
          AND o.status NOT IN ($1, $2, $3, $4)
          AND o.total_payment_amount IS NOT NULL
          AND o.total_payment_amount > 0
          AND o.sale_price > 0
        """,
        *EXCLUDED_STATUSES,
    )

    print(f"[조회] lotteon 추정 정산 대상: {len(rows)}건  DRY_RUN={DRY_RUN}")

    updated = 0
    skipped_noise = 0
    skipped_same = 0

    for r in rows:
        sale_price = float(r["sale_price"] or 0)
        customer_paid = float(r["total_payment_amount"] or 0)
        old_revenue = float(r["revenue"] or 0)
        category = r["category"] or ""

        fee = get_fee_rate_for_category(category)
        bse_cmsn = int(sale_price * fee / 100)
        new_revenue = max(0, int(customer_paid - bse_cmsn))
        new_fee_rate = (
            round(bse_cmsn / customer_paid * 100, 2) if customer_paid > 0 else 0.0
        )

        diff = old_revenue - new_revenue

        if abs(diff) < DIFF_THRESHOLD:
            skipped_noise += 1
            continue
        if old_revenue == new_revenue:
            skipped_same += 1
            continue

        if not DRY_RUN:
            await conn.execute(
                """
                UPDATE samba_order
                SET revenue = $1, fee_rate = $2
                WHERE id = $3
                  AND (revenue IS NULL OR revenue <> $1)
                """,
                new_revenue,
                new_fee_rate,
                r["id"],
            )

        updated += 1
        if updated <= 20 or updated % 100 == 0:
            print(
                f"  [{updated:>5}] order={r['order_number']} "
                f"cat={(category or '<none>')[:20]:<20} fee={fee}% "
                f"sale={int(sale_price):>8} paid={int(customer_paid):>8} "
                f"old_rev={int(old_revenue):>8} → new={new_revenue:>8} "
                f"diff={int(diff):>+7}"
            )

    print(
        f"\n[요약] 대상={len(rows)}건  변경={updated}건  "
        f"노이즈스킵={skipped_noise}  동일스킵={skipped_same}  DRY_RUN={DRY_RUN}"
    )
    if DRY_RUN:
        print("  ※ DRY_RUN=0 으로 다시 실행해야 실제 UPDATE 적용됨")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

"""CS 자동화 2차 검증 (읽기 전용).

A. 순수 확인응답(ack) 실제 개수 — Tier2 자동화 주소지 산정
B. 데이터 기간(min/max) + 월간 유입량 — 30분 스케줄 주기 적정성
C. 미답변(pending) 분포 — 스케줄잡이 처리할 대상
D. collected_product_id / market_order_id 연결률 — 컨텍스트 그라운딩 가능성
"""

import asyncio

import asyncpg

from backend.core.config import settings

ACK_PATTERNS = ("확인했습니다", "확인해보겠습니다", "확인 했습니다", "확인하겠습니다")


async def main() -> None:
    conn = await asyncpg.connect(
        host="172.18.0.2",
        port=5432,
        user=settings.write_db_user,
        password=settings.write_db_password,
        database=settings.write_db_name,
        ssl=False,
    )
    try:
        print("=" * 70)
        print("[A] 순수 확인응답(ack) 실제 개수 (Tier2 자동화 주소지)")
        for t in ("urgent_inquiry", "call_center", "문의", "general", "qna"):
            total = await conn.fetchval(
                "SELECT count(*) FROM samba_cs_inquiry WHERE is_hidden=false "
                "AND reply_status='replied' AND reply IS NOT NULL AND inquiry_type=$1",
                t,
            )
            ack = await conn.fetchval(
                "SELECT count(*) FROM samba_cs_inquiry WHERE is_hidden=false "
                "AND reply_status='replied' AND reply IS NOT NULL AND inquiry_type=$1 "
                "AND (length(trim(reply)) <= 12 OR trim(reply) LIKE '확인%')",
                t,
            )
            print(f"  {t:>16}: ack류 {ack:,} / 전체 {total:,}")

        print("=" * 70)
        print("[B] 데이터 기간 + 월간 유입량")
        row = await conn.fetchrow(
            "SELECT min(inquiry_date) mn, max(inquiry_date) mx, count(*) c "
            "FROM samba_cs_inquiry WHERE is_hidden=false AND inquiry_date IS NOT NULL"
        )
        print(f"  최초 문의: {row['mn']}")
        print(f"  최근 문의: {row['mx']}")
        print(f"  기간 내 전체: {row['c']:,}")
        if row["mn"] and row["mx"]:
            days = (row["mx"] - row["mn"]).days or 1
            print(
                f"  기간: {days:,}일 → 일평균 {row['c'] / days:.1f}건, 월평균 {row['c'] / days * 30:.0f}건"
            )

        print("\n  [최근 30일 유입]")
        recent = await conn.fetchval(
            "SELECT count(*) FROM samba_cs_inquiry WHERE is_hidden=false "
            "AND inquiry_date > now() - interval '30 days'"
        )
        print(f"  최근 30일: {recent:,}건")

        print("=" * 70)
        print("[C] 현재 미답변(pending) 분포 — 스케줄잡 처리 대상")
        rows = await conn.fetch(
            "SELECT inquiry_type, market, count(*) c FROM samba_cs_inquiry "
            "WHERE is_hidden=false AND reply_status='pending' "
            "GROUP BY inquiry_type, market ORDER BY c DESC"
        )
        for r in rows:
            print(f"  {r['inquiry_type']:>16} | {r['market']:>12} : {r['c']:,}")

        print("=" * 70)
        print("[D] 컨텍스트 연결률 (그라운딩 가능성)")
        for col in (
            "collected_product_id",
            "market_order_id",
            "market_product_no",
            "product_name",
        ):
            linked = await conn.fetchval(
                f"SELECT count(*) FROM samba_cs_inquiry WHERE is_hidden=false "
                f"AND {col} IS NOT NULL AND length(trim({col}::text))>0"
            )
            tot = await conn.fetchval(
                "SELECT count(*) FROM samba_cs_inquiry WHERE is_hidden=false"
            )
            print(
                f"  {col:>22}: {linked:,}/{tot:,} ({100 * linked / tot if tot else 0:.0f}%)"
            )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

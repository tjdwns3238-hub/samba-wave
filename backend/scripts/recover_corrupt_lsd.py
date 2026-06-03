"""last_sent_data 오염 복구.

- array `[null, {obj}]` (85건) → 마지막 객체만 살림(객체 아니면 NULL)
- JSON null 리터럴 (6,189건) → SQL NULL

before/after 카운트 + 샘플 검증 출력.
"""

import asyncio

from sqlalchemy import text

from backend.db.orm import get_write_sessionmaker


async def _dist(session):
    rows = (
        await session.execute(
            text(
                "SELECT jsonb_typeof(CAST(last_sent_data AS jsonb)) AS t, COUNT(*) "
                "FROM samba_collected_product "
                "WHERE last_sent_data IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
            )
        )
    ).all()
    return {t: c for t, c in rows}


async def main():
    Session = get_write_sessionmaker()
    async with Session() as session:
        print("=== BEFORE ===")
        before = await _dist(session)
        print(f"  {before}")

        # 복구 대상 샘플(복구 전)
        sample = (
            await session.execute(
                text(
                    "SELECT id, last_sent_data "
                    "FROM samba_collected_product "
                    "WHERE jsonb_typeof(CAST(last_sent_data AS jsonb)) = 'array' LIMIT 3"
                )
            )
        ).all()
        print("  array 샘플(전):")
        for r in sample:
            print(f"    {r[0]} {str(r[1])[:100]}")

        # ① array → 마지막 객체만(객체 아니면 NULL)
        res1 = await session.execute(
            text(
                "UPDATE samba_collected_product "
                "SET last_sent_data = CASE "
                "  WHEN jsonb_typeof(CAST(last_sent_data AS jsonb) -> -1) = 'object' "
                "    THEN (CAST(last_sent_data AS jsonb) -> -1)::json "
                "  ELSE NULL END "
                "WHERE jsonb_typeof(CAST(last_sent_data AS jsonb)) = 'array'"
            )
        )
        print(f"\n  ① array 복구: {res1.rowcount}건")

        # ② JSON null → SQL NULL
        res2 = await session.execute(
            text(
                "UPDATE samba_collected_product "
                "SET last_sent_data = NULL "
                "WHERE last_sent_data IS NOT NULL "
                "  AND jsonb_typeof(CAST(last_sent_data AS jsonb)) = 'null'"
            )
        )
        print(f"  ② JSON null → SQL NULL: {res2.rowcount}건")

        await session.commit()

        print("\n=== AFTER ===")
        after = await _dist(session)
        print(f"  {after}")

        # 복구된 샘플 재확인(같은 id)
        if sample:
            ids = [r[0] for r in sample]
            chk = (
                await session.execute(
                    text(
                        "SELECT id, last_sent_data, "
                        "jsonb_typeof(CAST(last_sent_data AS jsonb)) "
                        "FROM samba_collected_product WHERE id = ANY(:ids)"
                    ),
                    {"ids": ids},
                )
            ).all()
            print("  array 샘플(후):")
            for r in chk:
                print(f"    {r[0]} type={r[2]} {str(r[1])[:100]}")

        # array 잔존 0 확인
        left = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM samba_collected_product "
                    "WHERE last_sent_data IS NOT NULL "
                    "  AND jsonb_typeof(CAST(last_sent_data AS jsonb)) IN ('array','null')"
                )
            )
        ).scalar()
        print(f"\n  잔존 array/null: {left}건 (0이어야 정상)")


if __name__ == "__main__":
    asyncio.run(main())

"""#349 samba_return 편집칸 컬럼 prod 수동 적용 (마이그레이션 body와 동일).

entrypoint stamp→upgrade 구조라 신규 마이그레이션 자동 미적용 + verify_schema가
컬럼 없으면 startup 차단(배포 실패). idempotent(IF NOT EXISTS)라 재실행 안전.
"""

import asyncio

from sqlalchemy import text

from backend.db.orm import get_write_session

COLS = ["customer_amount", "company_amount", "return_link_manual"]


async def main() -> None:
    async with get_write_session() as session:
        for col in COLS:
            await session.execute(
                text(f"ALTER TABLE samba_return ADD COLUMN IF NOT EXISTS {col} TEXT")
            )
        await session.commit()
        rows = (
            await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='samba_return' "
                    "AND column_name IN ('customer_amount','company_amount','return_link_manual') "
                    "ORDER BY column_name"
                )
            )
        ).all()
        print("적용 후 존재 컬럼:", [r[0] for r in rows])


if __name__ == "__main__":
    asyncio.run(main())

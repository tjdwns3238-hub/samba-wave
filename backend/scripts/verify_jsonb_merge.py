"""jsonb || 병합이 JSON null/배열에서 배열을 만드는지 직접 검증."""

import asyncio

from sqlalchemy import text

from backend.db.orm import get_write_sessionmaker


async def main():
    Session = get_write_sessionmaker()
    async with Session() as session:
        obj = "jsonb_build_object('a', 1)"
        cases = [
            f"SELECT COALESCE(CAST('null'::json AS jsonb), '{{}}'::jsonb) || {obj}",
            f"SELECT COALESCE(CAST(NULL AS jsonb), '{{}}'::jsonb) || {obj}",
            f"SELECT '[null]'::jsonb || {obj}",
            "SELECT jsonb_typeof('null'::jsonb)",
            "SELECT (CAST('null'::json AS jsonb)) IS NULL",
        ]
        for sql in cases:
            r = (await session.execute(text(sql))).scalar()
            print(f"{sql}\n  => {r!r}\n")


if __name__ == "__main__":
    asyncio.run(main())

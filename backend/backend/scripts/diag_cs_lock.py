"""samba_cs_inquiry 락 보유자 진단 + idle in transaction 정리 (프로덕션)."""

import asyncio
from sqlalchemy import text
from backend.db.orm import get_write_session


async def main() -> None:
    async with get_write_session() as session:
        # intent 컬럼 존재 확인
        c = (
            await session.execute(
                text("""
            SELECT count(*) FROM information_schema.columns
            WHERE table_name='samba_cs_inquiry' AND column_name='intent'
        """)
            )
        ).scalar()
        print(f"samba_cs_inquiry.intent 컬럼 존재: {c == 1}")

        # samba_cs_inquiry 잠그고 있는 세션
        rows = (
            await session.execute(
                text("""
            SELECT a.pid, a.state, a.usename,
                   left(coalesce(a.query,''),80) AS q,
                   now()-a.state_change AS idle_for
            FROM pg_locks l
            JOIN pg_class c ON c.oid=l.relation
            JOIN pg_stat_activity a ON a.pid=l.pid
            WHERE c.relname='samba_cs_inquiry' AND a.pid<>pg_backend_pid()
            ORDER BY a.state_change
        """)
            )
        ).fetchall()
        print(f"\n[samba_cs_inquiry 락 보유 세션 {len(rows)}]")
        for r in rows:
            print(f"  pid={r.pid} state={r.state} idle_for={r.idle_for} q={r.q!r}")

        # idle in transaction 전체
        idle = (
            await session.execute(
                text("""
            SELECT count(*) FROM pg_stat_activity
            WHERE state IN ('idle in transaction','idle in transaction (aborted)')
              AND pid<>pg_backend_pid()
        """)
            )
        ).scalar()
        print(f"\nidle in transaction 총: {idle}")


if __name__ == "__main__":
    asyncio.run(main())

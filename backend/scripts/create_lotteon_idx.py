"""롯데ON partial unique 인덱스 수동 사전생성

배포 시 CREATE INDEX CONCURRENTLY 가 활성 트랜잭션 lock_timeout 초과로 실패.
- DELETE/DROP 은 이전 배포에서 이미 commit 됨
- 본 스크립트는 CREATE 만 시도. lock_timeout 5분으로 늘려 인덱스 빌드 완료까지 대기
"""

import asyncio

import asyncpg

from backend.core.config import settings


CREATE_SQL = """
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix_samba_order_lotteon_line
ON samba_order (tenant_id, od_no, od_seq)
WHERE source = 'lotteon'
"""

# 중복 정리 — 이전 배포 마이그레이션의 step 1이 COMMIT 됐는지 모름. 멱등이라 다시 돌려도 안전.
DELETE_SQL = """
DELETE FROM samba_order
WHERE id IN (
  SELECT id FROM (
    SELECT
      id,
      ROW_NUMBER() OVER (
        PARTITION BY tenant_id, od_no, od_seq
        ORDER BY created_at ASC, id ASC
      ) AS rn
    FROM samba_order
    WHERE source = 'lotteon'
      AND od_no IS NOT NULL
      AND od_no <> ''
  ) t
  WHERE rn > 1
)
"""


async def main() -> None:
    print(
        f"connecting to {settings.write_db_host}:{settings.write_db_port}/{settings.write_db_name}..."
    )
    conn = await asyncpg.connect(
        host=settings.write_db_host,
        port=settings.write_db_port,
        user=settings.write_db_user,
        password=settings.write_db_password,
        database=settings.write_db_name,
        ssl=False,
        timeout=30,
    )
    try:
        print("[1/3] idle in transaction 정리")
        killed = await conn.fetchval(
            "SELECT count(*) FROM (SELECT pg_terminate_backend(pid) "
            "FROM pg_stat_activity WHERE state = 'idle in transaction' "
            "AND pid <> pg_backend_pid()) s"
        )
        print(f"  killed={killed}")

        print("[2/3] 중복 삭제")
        result = await conn.execute(DELETE_SQL)
        print(f"  {result}")

        print("[3/3] CONCURRENTLY 인덱스 생성 (lock_timeout=5min)")
        await conn.execute("SET lock_timeout = '300s'")
        await conn.execute(CREATE_SQL)
        print("  완료")

        # 검증
        row = await conn.fetchrow(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'ix_samba_order_lotteon_line'"
        )
        print(f"  indexdef: {row['indexdef'] if row else 'NOT FOUND'}")

        # alembic version 갱신 — migration 통과시키기
        await conn.execute(
            "UPDATE alembic_version SET version_num = 'zzzzzzz_lotteon_dedupe' "
            "WHERE version_num = 'zzzzzz_dedupe_market_default'"
        )
        # 혹시 stamp 가 옛 revision이면 그것도 갱신
        await conn.execute(
            "INSERT INTO alembic_version (version_num) "
            "SELECT 'zzzzzzz_lotteon_dedupe' "
            "WHERE NOT EXISTS (SELECT 1 FROM alembic_version "
            "WHERE version_num = 'zzzzzzz_lotteon_dedupe')"
        )
        ver = await conn.fetch("SELECT version_num FROM alembic_version")
        print(f"  alembic_version: {[v['version_num'] for v in ver]}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

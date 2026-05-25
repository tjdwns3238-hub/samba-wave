"""롯데ON 주문 중복 차단 — partial unique 인덱스에서 channel_id 제거 + 기존 중복 정리

배경:
- 동일 API key를 공유하는 2개 롯데ON 마켓계정이 같은 주문을 양쪽 channel_id에
  중복 저장하던 사고(2026-05-25, order_number L02674917766_2674917769).
- 기존 partial unique `ix_samba_order_lotteon_line(tenant_id, channel_id, od_no, od_seq)`
  는 channel_id 포함이라 다른 채널이면 통과 → 중복 발생.

처리:
1) 기존 중복 행 정리 — (tenant_id, od_no, od_seq) 그룹 안 가장 오래된 created_at
   1개만 유지, 나머지 삭제.
2) 옛 partial unique 삭제(channel_id 포함본) → 새 partial unique 생성
   (tenant_id, od_no, od_seq, source='lotteon').

idempotent:
- 중복 정리는 멱등 (정리 완료 후 재실행해도 0건 삭제).
- DROP INDEX CONCURRENTLY IF EXISTS / CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS.

CONCURRENTLY 사용 이유:
- samba_order는 hot 테이블 — AccessExclusiveLock 시 활성 트랜잭션과 데드락 발생.
- alembic env.py가 transactional 모드 → CONCURRENTLY 호출 전 명시적 COMMIT 필요.

Revision ID: zzzzzzz_lotteon_dedupe
Revises: zzzzzz_dedupe_market_default
Create Date: 2026-05-25 21:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "zzzzzzz_lotteon_dedupe"
down_revision: Union[str, Sequence[str], None] = "zzzzzz_dedupe_market_default"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1) 기존 중복 정리 — 같은 (tenant_id, od_no, od_seq) 그룹에서 가장 오래된 1건만 유지
    conn.exec_driver_sql(
        """
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
    )

    # 2) 인덱스 존재 사전 체크 — pg 의 CREATE INDEX CONCURRENTLY IF NOT EXISTS 는
    #    이미 있어도 SHARE UPDATE EXCLUSIVE lock 을 시도하여 활성 트랜잭션과 lock_timeout
    #    충돌. pg_indexes 조회로 컬럼 구성이 이미 새 형태(channel_id 없음)면 SKIP.
    idx_row = conn.exec_driver_sql(
        "SELECT indexdef FROM pg_indexes "
        "WHERE indexname = 'ix_samba_order_lotteon_line'"
    ).fetchone()
    if idx_row and "channel_id" not in (idx_row[0] or ""):
        # 새 컬럼 구성 이미 적용됨 — SKIP
        return

    # CONCURRENTLY 는 transaction 외부 필요 → 명시적 COMMIT + lock_timeout 무한대기
    conn.exec_driver_sql("COMMIT")
    conn.exec_driver_sql("SET lock_timeout = 0")
    conn.exec_driver_sql(
        "DROP INDEX CONCURRENTLY IF EXISTS ix_samba_order_lotteon_line"
    )
    conn.exec_driver_sql(
        """
        CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix_samba_order_lotteon_line
        ON samba_order (tenant_id, od_no, od_seq)
        WHERE source = 'lotteon'
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("COMMIT")
    conn.exec_driver_sql(
        "DROP INDEX CONCURRENTLY IF EXISTS ix_samba_order_lotteon_line"
    )
    conn.exec_driver_sql(
        """
        CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix_samba_order_lotteon_line
        ON samba_order (tenant_id, channel_id, od_no, od_seq)
        WHERE source = 'lotteon'
        """
    )

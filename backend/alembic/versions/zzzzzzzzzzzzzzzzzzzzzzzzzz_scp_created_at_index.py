"""samba_collected_product.created_at 인덱스 추가 — 대시보드 일별 수집상품수 GROUP BY 가속

대시보드 응답시간 ~2분 → ms 단위 단축. CONCURRENTLY 로 hot 테이블 락 없이 생성.

Revision ID: zzzzzzzzzzzzzzzzzzzzzzzzzz_scp_created_at_index
Revises: zzzzzzzzzzzzzzzzzzzzzzzzz_ssg_policy_key
Create Date: 2026-05-16
"""

from typing import Sequence, Union

from alembic import op

revision: str = "zzzzzzzzzzzzzzzzzzzzzzzzzz_scp_created_at_index"
down_revision: Union[str, Sequence[str], None] = (
    "zzzzzzzzzzzzzzzzzzzzzzzzz_ssg_policy_key"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # CONCURRENTLY 는 트랜잭션 밖에서만 실행 가능 — autocommit 블록 사용.
    # IF NOT EXISTS 로 idempotent.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_scp_created_at ON samba_collected_product (created_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_scp_created_at")

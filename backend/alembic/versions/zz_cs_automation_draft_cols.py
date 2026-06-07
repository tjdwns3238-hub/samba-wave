"""samba_cs_inquiry CS 자동화 컬럼 추가 (Tier 0)

Revision ID: zz_cs_auto_draft_001
Revises: z_unshipped_snapshot_001
Create Date: 2026-06-08

CS 자동화(반자동->완전자동)용 컬럼:
  intent           - 룰기반 의도 분류 결과
  draft_reply      - Claude 스케줄잡 작성 답변 초안
  draft_status     - none/suggested/auto_sent/accepted/edited/rejected
  draft_confidence - 0.0~1.0 자동전송 게이트 판정값
  draft_source     - claude/template/rule
  drafted_at       - 초안 작성 일시

모두 IF NOT EXISTS - idempotent. samba_cs_inquiry는 소규모 테이블이라
hot 테이블 데드락 위험 없음. (lifecycle._apply_startup_schema_fixes 가 매 startup 보장)
"""

from typing import Sequence, Union

from alembic import op


revision: str = "zz_cs_auto_draft_001"
down_revision: Union[str, Sequence[str], None] = "z_unshipped_snapshot_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE samba_cs_inquiry ADD COLUMN IF NOT EXISTS intent TEXT")
    op.execute("ALTER TABLE samba_cs_inquiry ADD COLUMN IF NOT EXISTS draft_reply TEXT")
    op.execute(
        "ALTER TABLE samba_cs_inquiry ADD COLUMN IF NOT EXISTS draft_status TEXT "
        "NOT NULL DEFAULT 'none'"
    )
    op.execute(
        "ALTER TABLE samba_cs_inquiry ADD COLUMN IF NOT EXISTS draft_confidence "
        "DOUBLE PRECISION"
    )
    op.execute(
        "ALTER TABLE samba_cs_inquiry ADD COLUMN IF NOT EXISTS draft_source TEXT"
    )
    op.execute(
        "ALTER TABLE samba_cs_inquiry ADD COLUMN IF NOT EXISTS drafted_at "
        "TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_samba_cs_inquiry_intent "
        "ON samba_cs_inquiry (intent)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_samba_cs_inquiry_draft_status "
        "ON samba_cs_inquiry (draft_status)"
    )


def downgrade() -> None:
    pass

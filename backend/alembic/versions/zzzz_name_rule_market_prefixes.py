"""name_rule에 market_prefixes / market_suffixes 추가 (마켓별 접두/접미어)

Revision ID: zzzz_name_rule_mkt_prefix_001
Revises: zzz_return_collect_001
Create Date: 2026-06-09

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "zzzz_name_rule_mkt_prefix_001"
down_revision: Union[str, Sequence[str], None] = "zzz_return_collect_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """마켓별 접두어/접미어 컬럼 추가 (idempotent)."""
    op.execute(
        "ALTER TABLE samba_name_rule ADD COLUMN IF NOT EXISTS market_prefixes JSON"
    )
    op.execute(
        "ALTER TABLE samba_name_rule ADD COLUMN IF NOT EXISTS market_suffixes JSON"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE samba_name_rule DROP COLUMN IF EXISTS market_prefixes")
    op.execute("ALTER TABLE samba_name_rule DROP COLUMN IF EXISTS market_suffixes")

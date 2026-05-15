"""merge heads: order_postal_code + sales_channel_alias

배경:
- PR #178(우편번호 컬럼)이 category_mapping_unique 에서 분기한 사이,
  main 에 musinsa_ord_opt_no → sales_channel_alias 체인이 머지되어 head 2개 발생
- 두 head 를 합치는 빈 merge 마이그레이션 (스키마 변경 없음)
"""

from collections.abc import Sequence
from typing import Union

from alembic import op  # noqa: F401


revision: str = "zzzzzzzzzzzzzzzzzzzzzzzz_merge_heads_postal_sales"
down_revision: Union[str, Sequence[str], None] = (
    "zzzzzzzzzzzzzzzzzzzzzz_add_order_postal_code",
    "zzzzzzzzzzzzzzzzzzzzzzz_sales_channel_alias",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

"""samba_order.customer_postal_code 컬럼 추가

배경:
- 주문 탭에서 우편번호를 화면 확인용으로 표시하려는 요구
- 기존 customer_address 와 분리 — 복사 버튼은 customer_address 만 복사하도록
  frontend 에서 별도 영역으로 렌더링

idempotent — ADD COLUMN IF NOT EXISTS, lock 매우 짧음.
"""

from collections.abc import Sequence
from typing import Union

from alembic import op


revision: str = "zzzzzzzzzzzzzzzzzzzzzz_add_order_postal_code"
down_revision: Union[str, Sequence[str], None] = (
    "zzzzzzzzzzzzzzzzzzzzz_category_mapping_unique"
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE samba_order ADD COLUMN IF NOT EXISTS customer_postal_code TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE samba_order DROP COLUMN IF EXISTS customer_postal_code")

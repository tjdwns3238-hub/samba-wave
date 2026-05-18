"""samba_order.shipment_id SSG 3-토큰 → 2-토큰 정규화

기존 포맷: shppNo|ordItemSeq|orordNo (3-token)
신규 포맷: shppNo|ordItemSeq        (2-token)

orordNo는 order_number 컬럼에 이미 저장되므로 중복 제거.
3-토큰 레코드가 그대로 남아 있으면 다음 주문동기화 시 exact-match 중복 감지
실패 → 동일 주문이 두 번 생성되는 문제 방지.

idempotent — LIKE '%|%|%' 조건으로 이미 2-토큰인 레코드는 건드리지 않음.

Revision ID: zzzzzzzzzzzzzzzzzzzzzzzzzzz_ssg_shipment_id_trim
Revises: zzzzzzzzzzzzzzzzzzzzzzzzzz_scp_created_at_index
Create Date: 2026-05-18
"""

from typing import Sequence, Union

from alembic import op

revision: str = "zzzzzzzzzzzzzzzzzzzzzzzzzzz_ssg_shipment_id_trim"
down_revision: Union[str, Sequence[str], None] = (
    "zzzzzzzzzzzzzzzzzzzzzzzzzz_scp_created_at_index"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE samba_order
        SET shipment_id =
            SPLIT_PART(shipment_id, '|', 1) || '|' || SPLIT_PART(shipment_id, '|', 2)
        WHERE source = 'ssg'
          AND shipment_id LIKE '%|%|%'
    """)


def downgrade() -> None:
    # orordNo는 별도로 보존하지 않았으므로 복원 불가 — no-op
    pass

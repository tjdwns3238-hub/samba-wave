"""samba_order에 쿠팡 취소/반품 사유 9컬럼 추가 (#246)

배경:
- 쿠팡 ordersheets v5 응답에는 cancelRequests/returnRequests 키가 없어
  취소 사유 수집 불가. returnRequests v6 API 응답 매핑용 컬럼 신설.
- approve_cancel / seller_cancel 후속 API 호출 시 receiptId 필수.

신규 컬럼:
- cancel_reason_code         TEXT     (VOC 코드, 예: CHANGEMIND)
- cancel_reason_text         TEXT     (한글 사유, reasonCodeText)
- cancel_reason_category1    TEXT     (예: 고객변심)
- cancel_reason_category2    TEXT     (예: 단순변심)
- cancel_fault_by            TEXT     (CUSTOMER/VENDOR/COUPANG/WMS/GENERAL)
- cancel_receipt_id          BIGINT   (후속 처리 API 핵심 ID)
- cancel_release_status      TEXT     (Y/N/S/A)
- cancel_release_stop_status TEXT     (미처리/처리(이미출고)/처리(출고중지)/...)
- cancel_requested_at        TIMESTAMPTZ

idempotent:
- samba_order 는 hot 테이블 → information_schema 사전체크
- 누락 컬럼만 raw SQL ADD COLUMN (op.add_column 금지)
- ALTER 직전 idle in transaction 정리 + lock_timeout 5분

Revision ID: zzzzzzzzzzz_coupang_cancel_columns
Revises: zzzzzzzzzz_coupang_search_tags
Create Date: 2026-05-27 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "zzzzzzzzzzz_coupang_cancel_columns"
down_revision: Union[str, Sequence[str], None] = "zzzzzzzzzz_coupang_search_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_COLUMNS: list[tuple[str, str]] = [
    ("cancel_reason_code", "TEXT"),
    ("cancel_reason_text", "TEXT"),
    ("cancel_reason_category1", "TEXT"),
    ("cancel_reason_category2", "TEXT"),
    ("cancel_fault_by", "TEXT"),
    ("cancel_receipt_id", "BIGINT"),
    ("cancel_release_status", "TEXT"),
    ("cancel_release_stop_status", "TEXT"),
    ("cancel_requested_at", "TIMESTAMPTZ"),
]


def upgrade() -> None:
    # samba_order 는 hot 테이블 → IF NOT EXISTS 도 ALTER 시점 락 잡아 데드락 가능.
    # information_schema 사전체크로 있는 컬럼은 ALTER 스킵.
    conn = op.get_bind()
    existing = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'samba_order' "
                "AND column_name = ANY(:cols)"
            ),
            {"cols": [c[0] for c in NEW_COLUMNS]},
        ).fetchall()
    }
    need = [(name, dtype) for name, dtype in NEW_COLUMNS if name not in existing]
    if not need:
        return

    op.execute(
        """
        SELECT pg_terminate_backend(pid)
        FROM pg_stat_activity
        WHERE state = 'idle in transaction'
          AND pid <> pg_backend_pid()
        """
    )
    op.execute("SET LOCAL lock_timeout = '5min'")
    for name, dtype in need:
        op.execute(f"ALTER TABLE samba_order ADD COLUMN {name} {dtype}")


def downgrade() -> None:
    for name, _ in NEW_COLUMNS:
        op.execute(f"ALTER TABLE samba_order DROP COLUMN IF EXISTS {name}")

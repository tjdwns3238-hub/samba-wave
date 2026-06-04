"""samba_detail_template.gallery_include_sub 컬럼 추가 + backfill (#342)

마켓 썸네일/갤러리(Image1~N) 추가이미지 포함 여부를 상세페이지 img_checks.sub 와
분리하는 신규 토글. 기존 두 head(ai_image_transformed_column / tetris_board_idx)를
함께 merge 하여 단일 head 로 통합한다.

backfill: 기존 정책 동작(#309) 보존 —
  gallery_include_sub = COALESCE((img_checks->>'sub')::boolean, true)
즉 추가이미지(sub) 제외 템플릿은 갤러리도 단일화(false) 유지, 그 외엔 true.

Revision ID: zzzzzzzzzzzzzzz_gallery_include_sub
Revises: zzzzzzzzzzzzz_ai_image_transformed_column, zzzzzzzzzzzzzz_tetris_board_idx
Create Date: 2026-06-04
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "zzzzzzzzzzzzzzz_gallery_include_sub"
down_revision: Union[str, Sequence[str], None] = (
    "zzzzzzzzzzzzz_ai_image_transformed_column",
    "zzzzzzzzzzzzzz_tetris_board_idx",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # samba_detail_template 은 hot 테이블 아님(템플릿 소수) — 그래도 안전하게 lock_timeout
    conn.execute(text("SET lock_timeout = '30s'"))

    # IF NOT EXISTS 패턴 — idempotent. DEFAULT true 로 기존 row 즉시 채움(NOT NULL 안전)
    conn.execute(
        text(
            """
            ALTER TABLE samba_detail_template
            ADD COLUMN IF NOT EXISTS gallery_include_sub BOOLEAN NOT NULL DEFAULT TRUE
            """
        )
    )

    # backfill: 기존 동작(#309) 보존 — 상세 sub 값을 갤러리에 그대로 이식.
    # img_checks.sub=false 였던 템플릿은 갤러리도 단일화(false) 유지 → 배포 즉시
    # 갤러리가 조용히 부풀어 오르는 회귀 방지. sub 키 없거나 null 이면 true(기본).
    conn.execute(
        text(
            """
            UPDATE samba_detail_template
            SET gallery_include_sub = COALESCE((img_checks->>'sub')::boolean, TRUE)
            WHERE img_checks IS NOT NULL
            """
        )
    )

    conn.execute(text("SET lock_timeout = '0'"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        text(
            "ALTER TABLE samba_detail_template "
            "DROP COLUMN IF EXISTS gallery_include_sub"
        )
    )

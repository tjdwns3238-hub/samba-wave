"""samba_market_account OAuth 컬럼 + is_default 추가

배경:
- `store_*` samba_settings 단일 키 폴백 폐지 → samba_market_account 단일 진실 출처 통일
- OAuth 마켓(cafe24, ebay, amazon) 토큰을 additional_fields JSON 에 두면 만료 임박 토큰
  일괄 갱신 쿼리에서 JSON 파싱 부하 큼 → 별도 컬럼으로 분리
- 다중 계정 환경에서 fallback 우선순위 식별을 위해 is_default 마킹 추가

추가 컬럼:
- oauth_access_token (Text, nullable)
- oauth_refresh_token (Text, nullable)
- oauth_expires_at (Timestamp with TZ, nullable)
- is_default (Boolean, default false, indexed)

idempotent:
- ADD COLUMN IF NOT EXISTS — 재실행 안전 (entrypoint stamp 재배포 대비)
- samba_market_account 는 hot 테이블 아님(설정성 데이터) — CONCURRENTLY 인덱스 불필요

데이터 마이그레이션은 별도 단계에서 SQL 스크립트로 실행 (자동 INSERT 는 본 파일 미포함).

Revision ID: zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz_market_account_oauth_default
Revises: zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz_autotune_cycle_idx
Create Date: 2026-05-25 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz_market_account_oauth_default"
down_revision: Union[str, Sequence[str], None] = (
    "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz_autotune_cycle_idx"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # OAuth 토큰 컬럼 (cafe24/ebay/amazon 등) — 만료/갱신 추적
    op.execute(
        "ALTER TABLE samba_market_account "
        "ADD COLUMN IF NOT EXISTS oauth_access_token TEXT"
    )
    op.execute(
        "ALTER TABLE samba_market_account "
        "ADD COLUMN IF NOT EXISTS oauth_refresh_token TEXT"
    )
    op.execute(
        "ALTER TABLE samba_market_account "
        "ADD COLUMN IF NOT EXISTS oauth_expires_at TIMESTAMP WITH TIME ZONE"
    )

    # is_default — market_type+tenant_id 당 1개 (uniqueness 는 service 레벨 강제)
    op.execute(
        "ALTER TABLE samba_market_account "
        "ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT false"
    )

    # fallback 조회용 인덱스 — (tenant_id, market_type, is_default DESC)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_smk_default_per_market "
        "ON samba_market_account (tenant_id, market_type, is_default)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_smk_default_per_market")
    op.execute("ALTER TABLE samba_market_account DROP COLUMN IF EXISTS is_default")
    op.execute(
        "ALTER TABLE samba_market_account DROP COLUMN IF EXISTS oauth_expires_at"
    )
    op.execute(
        "ALTER TABLE samba_market_account DROP COLUMN IF EXISTS oauth_refresh_token"
    )
    op.execute(
        "ALTER TABLE samba_market_account DROP COLUMN IF EXISTS oauth_access_token"
    )

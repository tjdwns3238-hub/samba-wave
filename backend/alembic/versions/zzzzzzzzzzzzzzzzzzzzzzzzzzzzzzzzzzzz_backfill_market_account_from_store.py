"""samba_settings.store_* → samba_market_account 백필 (2026-05-25).

배경:
- store_* samba_settings 단일 키 구조 폐지 → samba_market_account 단일 진실 출처 통일
- 기존 store_* row 를 market_account row 로 1:1 변환
- 이미 동일 (tenant_id, market_type, is_default=true) 계정 있으면 skip (멱등성)

대상 마켓 10개:
- lotteon, 11st, coupang, ssg, smartstore, gsshop, lottehome, playauto, ebay, kream

매핑 규칙 (backend/domain/samba/account/credentials.py 명세 따름):
- api_key: 마켓별 주 인증키 (apiKey/clientId/accessKey/userId 등)
- api_secret: 마켓별 보조 인증키 (clientSecret/secretKey/password 등)
- seller_id: vendorId/agncNo/hostingId/storeId 등
- additional_fields: 전체 value JSON 그대로 보존 (마이그레이션 후 클라가 기존 키 참조해도 OK)

samba_settings.key 형식:
- '{tenant_id}:store_<market>' — 멀티테넌트 (2026-04-02 이후)
- 'store_<market>' (bare) — 마이그레이션 이전 데이터 (tenant_id=NULL)

samba_settings.tenant_id 컬럼도 함께 활용 — key prefix 와 일치하지 않으면 NULL 우선.

idempotent — 재실행 안전:
- NOT EXISTS 가드로 같은 (tenant, market_type, is_default=true) 있으면 skip
- ON CONFLICT 미사용 (PK 가 generated ID 라 충돌 없음)

Revision ID: zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz_backfill_market_account_from_store
Revises: zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz_market_account_oauth_default
Create Date: 2026-05-25 13:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = (
    "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz_backfill_market_account_from_store"
)
down_revision: Union[str, Sequence[str], None] = (
    "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz_market_account_oauth_default"
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (market_type, korean_name, store_key_suffix, api_key_field, api_secret_field, seller_id_field)
_MARKETS = [
    ("lotteon", "롯데ON", "store_lotteon", "apiKey", None, None),
    ("11st", "11번가", "store_11st", "apiKey", None, None),
    ("coupang", "쿠팡", "store_coupang", "accessKey", "secretKey", "vendorId"),
    ("ssg", "SSG", "store_ssg", "apiKey", None, None),
    (
        "smartstore",
        "스마트스토어",
        "store_smartstore",
        "clientId",
        "clientSecret",
        None,
    ),
    ("gsshop", "GS샵", "store_gsshop", None, None, "storeId"),
    ("lottehome", "롯데홈쇼핑", "store_lottehome", "userId", "password", "agncNo"),
    ("playauto", "플레이오토", "store_playauto", "apiKey", None, "hostingId"),
    ("ebay", "eBay", "store_ebay", "clientId", "clientSecret", None),
    ("kream", "KREAM", "store_kream", None, None, None),
]


def _build_insert_sql(
    market_type: str,
    korean_name: str,
    store_key: str,
    api_key_field: str | None,
    api_secret_field: str | None,
    seller_id_field: str | None,
) -> str:
    """마켓별 백필 INSERT SQL 생성."""

    def _extract(field: str | None) -> str:
        return f"s.value::jsonb ->> '{field}'" if field else "NULL"

    api_key_expr = _extract(api_key_field)
    api_secret_expr = _extract(api_secret_field)
    seller_id_expr = _extract(seller_id_field)

    # samba_settings.key 가 '{tenant_id}:store_*' 면 prefix 에서 tenant 추출,
    # bare 'store_*' 면 NULL.
    tenant_expr = (
        f"CASE WHEN s.key = '{store_key}' THEN s.tenant_id "
        f"ELSE COALESCE(s.tenant_id, split_part(s.key, chr(58), 1)) END"
    )

    # md5(random + key + clock) 로 충돌 가능성 낮은 ID 생성 (27자 + 'ma_' prefix = 30자 한계)
    id_expr = "'ma_' || substr(md5(random()::text || s.key || clock_timestamp()::text), 1, 26)"

    return f"""
    INSERT INTO samba_market_account (
        id, tenant_id, market_type, market_name, account_label,
        api_key, api_secret, seller_id, additional_fields,
        is_active, is_default, created_at, updated_at
    )
    SELECT
        {id_expr},
        {tenant_expr} AS tenant_id_extracted,
        '{market_type}',
        '{korean_name}',
        'default',
        {api_key_expr},
        {api_secret_expr},
        {seller_id_expr},
        s.value,
        true,
        true,
        NOW(),
        NOW()
    FROM samba_settings s
    WHERE (s.key = '{store_key}' OR s.key LIKE '%' || chr(58) || '{store_key}')
      AND s.value IS NOT NULL
      AND s.value::text NOT IN ('null', '{{}}', '""')
      AND NOT EXISTS (
        SELECT 1 FROM samba_market_account ma
        WHERE COALESCE(ma.tenant_id, '') = COALESCE({tenant_expr}, '')
          AND ma.market_type = '{market_type}'
          AND ma.is_default = true
      )
    """


def upgrade() -> None:
    for market in _MARKETS:
        op.execute(_build_insert_sql(*market))


def downgrade() -> None:
    # 백필로 생성된 row 만 삭제 — account_label = 'default' 이고 본 마이그레이션 시점
    # 이후 생성된 row 중 매칭 식별. 안전을 위해 downgrade 는 no-op (사용자가 수동 정리).
    pass

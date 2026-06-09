"""samba_order.shipment_id 인덱스 추가 (issue #393)

배경:
- 미매칭 주문 중복체크가 filter_by_async(shipment_id=...) 로 조회하는데
  samba_order.shipment_id 인덱스 부재로 Seq Scan (EXPLAIN cold 220ms).
- 미매칭 수백 건 × 220ms ≈ 160초 누적 → per-account 300초 타임아웃 → 0건 저장.
- 인덱스 추가 후 Index Scan 1.3ms, 단일계정 동기화 300초 → 49초 완주.

idempotent / hot 테이블 안전:
- samba_order 는 hot 테이블 → CREATE INDEX CONCURRENTLY 로 AccessExclusiveLock
  회피 (활성 트랜잭션과 데드락 방지).
- CONCURRENTLY 는 트랜잭션 밖에서만 실행 가능 → 수동 COMMIT 후 실행.
- IF NOT EXISTS 로 재실행 안전 (entrypoint stamp 재배포 시 반복 실행 대비).
- 배포 전 프로덕션에 인덱스를 수동 사전생성하면 이 마이그레이션은 skip 되어
  green 360초 초과 배포실패를 회피한다.
- 인덱스명 ix_samba_order_shipment_id 는 SQLModel index=True 기본 생성명과 동일
  → 이후 autogenerate 가 누락으로 오인하지 않음.

Revision ID: zzzzzzz_order_shipment_id_idx
Revises: zzzzz_add_tenant_isolation_cols
Create Date: 2026-06-09 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "zzzzzzz_order_shipment_id_idx"
down_revision: Union[str, Sequence[str], None] = "zzzzz_add_tenant_isolation_cols"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # CONCURRENTLY 는 트랜잭션 안에서 실행 불가.
    # 이 레포 env.py는 async(asyncpg) + connection.run_sync 구조라 alembic
    # autocommit_block 이 _transaction 을 추적 못 해 AssertionError 발생(검증 완료).
    # → alembic 트랜잭션을 수동 COMMIT 으로 종료한 뒤 CONCURRENTLY 실행.
    conn = op.get_bind()
    conn.exec_driver_sql("COMMIT")
    conn.exec_driver_sql(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_samba_order_shipment_id "
        "ON samba_order (shipment_id)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("COMMIT")
    conn.exec_driver_sql("DROP INDEX CONCURRENTLY IF EXISTS ix_samba_order_shipment_id")

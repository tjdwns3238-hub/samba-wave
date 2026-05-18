#!/bin/sh

# {backend/entrypoint.sh}
#
# Cloud Run 컨테이너 진입점.
# - production: Cloud SQL 대기 → Emergency schema fixes → alembic upgrade → verify_schema → Gunicorn
# - development: uvicorn --reload
#
# 2026-04-17 사고 이후 조용한 실패 패턴 제거:
#   - alembic 3회 실패 시 exit 1로 Cloud Run이 이전 리비전 유지 (침묵으로 배포 green 사고 재발 방지)

set -e

# Default to development if ENVIRONMENT is not set
if [ -z "$ENVIRONMENT" ]; then
  ENVIRONMENT="development"
fi

# Always load .env file for development
if [ "$ENVIRONMENT" = "development" ]; then
  export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi


echo "Running in $ENVIRONMENT mode"

if [ "$ENVIRONMENT" = "production" ]; then
  # Cloud SQL Auth Proxy 사이드카 대기 — 재시도로 확실히 연결 확인
  # 첫 retry는 1s (proxy가 보통 1~2초 내 ready), 이후 5s로 점증 (배포시간 최적화)
  echo "Waiting for Cloud SQL proxy..."
  for i in 1 2 3 4 5 6; do
    if uv run python -c "
import asyncio, os
async def check():
    import asyncpg
    host = os.environ.get('WRITE_DB_HOST') or ''
    if not host: return False
    kw = dict(user=os.environ.get('WRITE_DB_USER') or 'postgres', password=os.environ.get('WRITE_DB_PASSWORD') or '', database=os.environ.get('WRITE_DB_NAME') or 'railway')
    if host.startswith('/'): kw['host'] = host
    else: kw['host'] = host; kw['port'] = int(os.environ.get('WRITE_DB_PORT') or 5432)
    conn = await asyncpg.connect(**kw)
    await conn.close()
    return True
r = asyncio.run(check())
exit(0 if r else 1)
" 2>/dev/null; then
      echo "Cloud SQL proxy ready (attempt $i)."
      break
    fi
    if [ "$i" = "1" ] || [ "$i" = "2" ]; then
      echo "Cloud SQL proxy not ready, retrying in 1s... (attempt $i/6)"
      sleep 1
    else
      echo "Cloud SQL proxy not ready, retrying in 5s... (attempt $i/6)"
      sleep 5
    fi
  done

  # Emergency schema fixes — alembic_version=873871a20399 stamp 상태에서 누락된 테이블/컬럼 수동 보완
  # (2026-04-17 사고 이후 stamp-DB 간극 해소용. 신규 누락 항목은 여기 추가)
  #
  # 2026-04-28 근본 수정: blue 컨테이너 idle connection이 잡고 있는 ACCESS EXCLUSIVE LOCK 때문에
  # 일부 ALTER TABLE이 무한 대기(>5분)하던 문제 → lock_timeout/statement_timeout으로 fail-fast 처리.
  # lock 못 잡으면 즉시 에러 → except에서 로깅 후 다음 SQL 진행 (다음 startup에서 재시도).
  echo "Applying emergency schema fixes..."
  uv run python -c "
import asyncio, os, sys, time
def _env(key):
    return os.environ.get(key) or os.environ.get(key.lower()) or os.environ.get(key.upper()) or ''
async def fix():
    import asyncpg
    host = _env('WRITE_DB_HOST')
    if not host:
        print('WRITE_DB_HOST not set, skip emergency fix'); return
    kw = dict(
        user=_env('WRITE_DB_USER') or 'postgres',
        password=_env('WRITE_DB_PASSWORD'),
        database=_env('WRITE_DB_NAME') or 'railway',
        # lock_timeout=5s: ACCESS EXCLUSIVE LOCK 5초 못 잡으면 즉시 fail
        # statement_timeout=30s: 쿼리 실행 자체도 30초 안에 끝나야 함 (큰 테이블 CREATE INDEX 마진)
        # blue 컨테이너 idle connection이 락을 쥐고 있어도 startup 차단 안 됨
        server_settings={'lock_timeout': '5s', 'statement_timeout': '30s'},
    )
    if host.startswith('/'):
        kw['host'] = host
    else:
        kw['host'] = host; kw['port'] = int(_env('WRITE_DB_PORT') or 5432)
    conn = await asyncpg.connect(**kw)
    # 각 SQL을 (라벨, sql) 튜플로 묶어 단계별 시간 측정 + 개별 실패 격리
    statements = [
        # 2026-04-28 사고 보강: idle 외에 idle in transaction (5분+ 방치)도 강제 종료.
        # PID 637179가 30분간 idle in transaction 상태로 ACCESS EXCLUSIVE LOCK 보유 →
        # 이후 ALTER TABLE 무한 대기 → connection pool 도미노 고갈 → 1시간 다운.
        # 'idle'은 즉시, 'idle in transaction*'은 5분 이상 방치된 것만 종료(정상 짧은 트랜잭션 보호).
        ('terminate_idle', 'SELECT COUNT(*) FROM pg_stat_activity'
                           ' WHERE datname = current_database()'
                           ' AND pid <> pg_backend_pid()'
                           ' AND ('
                             'state = \'idle\''
                             ' OR ('
                               'state IN (\'idle in transaction\', \'idle in transaction (aborted)\')'
                               ' AND now() - state_change > interval \'5 minutes\''
                             ')'
                           ')'
                           ' AND pg_terminate_backend(pid)'),
        ('alter_search_filter', 'ALTER TABLE samba_search_filter ADD COLUMN IF NOT EXISTS source_brand_name TEXT'),
        ('drop_market_account_sort_order', 'ALTER TABLE samba_market_account DROP COLUMN IF EXISTS sort_order'),
        ('alter_return_clm_req_seq', 'ALTER TABLE samba_return ADD COLUMN IF NOT EXISTS clm_req_seq TEXT'),
        ('alter_return_ord_prd_seq', 'ALTER TABLE samba_return ADD COLUMN IF NOT EXISTS ord_prd_seq TEXT'),
        ('alter_return_exch_retrieval_status', 'ALTER TABLE samba_return ADD COLUMN IF NOT EXISTS exchange_retrieval_status TEXT'),
        ('alter_return_exch_retrieved_at', 'ALTER TABLE samba_return ADD COLUMN IF NOT EXISTS exchange_retrieved_at TIMESTAMPTZ'),
        ('alter_return_exch_reship_company', 'ALTER TABLE samba_return ADD COLUMN IF NOT EXISTS exchange_reship_company TEXT'),
        ('alter_return_exch_reship_tracking', 'ALTER TABLE samba_return ADD COLUMN IF NOT EXISTS exchange_reship_tracking TEXT'),
        ('alter_return_exch_delivered_at', 'ALTER TABLE samba_return ADD COLUMN IF NOT EXISTS exchange_delivered_at TIMESTAMPTZ'),
        ('alter_order_collected_product_id', 'ALTER TABLE samba_order ADD COLUMN IF NOT EXISTS collected_product_id TEXT'),
        ('alter_order_customer_address_detail', 'ALTER TABLE samba_order ADD COLUMN IF NOT EXISTS customer_address_detail TEXT'),
        ('alter_order_ord_prd_seq', 'ALTER TABLE samba_order ADD COLUMN IF NOT EXISTS ord_prd_seq TEXT'),
        ('idx_order_collected_product_id', 'CREATE INDEX IF NOT EXISTS ix_samba_order_collected_product_id ON samba_order (collected_product_id) WHERE collected_product_id IS NOT NULL'),
        ('create_search_cache', '''CREATE TABLE IF NOT EXISTS samba_search_cache (
                id VARCHAR(30) PRIMARY KEY NOT NULL,
                tenant_id VARCHAR(100),
                source_site VARCHAR(50) NOT NULL,
                keyword VARCHAR(200) NOT NULL,
                products JSON,
                ttl_minutes INTEGER NOT NULL DEFAULT 60,
                created_at TIMESTAMPTZ NOT NULL
            )'''),
        ('idx_search_cache_source_site', 'CREATE INDEX IF NOT EXISTS ix_samba_search_cache_source_site ON samba_search_cache (source_site)'),
        ('idx_search_cache_tenant_id', 'CREATE INDEX IF NOT EXISTS ix_samba_search_cache_tenant_id ON samba_search_cache (tenant_id)'),
        ('alembic_version_dedup', '''DELETE FROM alembic_version
            WHERE version_num = \'39f5332d495f\'
              AND EXISTS (
                SELECT 1 FROM alembic_version WHERE version_num = \'z_lotteon_order_line_keys\'
              )'''),
        ('create_license', '''CREATE TABLE IF NOT EXISTS samba_license (
                id VARCHAR PRIMARY KEY NOT NULL,
                license_key VARCHAR NOT NULL,
                buyer_name VARCHAR NOT NULL,
                buyer_email VARCHAR NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                expires_at TIMESTAMP,
                notes VARCHAR,
                last_verified_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )'''),
        ('idx_license_key', 'CREATE UNIQUE INDEX IF NOT EXISTS ix_samba_license_license_key ON samba_license (license_key)'),
    ]
    try:
        total_start = time.time()
        for label, sql in statements:
            stmt_start = time.time()
            try:
                if label == 'terminate_idle':
                    val = await conn.fetchval(sql)
                    print(f'  [{label}] OK ({time.time()-stmt_start:.2f}s) terminated={val}')
                else:
                    await conn.execute(sql)
                    print(f'  [{label}] OK ({time.time()-stmt_start:.2f}s)')
            except Exception as exc:
                # lock_timeout/statement_timeout 시 즉시 빠져나와 다음 SQL 진행
                # (마이그레이션이 같은 작업을 다시 시도하므로 fail-safe)
                print(f'  [{label}] SKIP ({time.time()-stmt_start:.2f}s) — {type(exc).__name__}: {exc}')
        print(f'Emergency schema fixes applied. (total {time.time()-total_start:.2f}s)')
    finally:
        await conn.close()
asyncio.run(fix())
" || echo "Emergency fix failed (non-fatal)"

  # DB 마이그레이션 — RUN_MIGRATIONS=0 설정 시 스킵 (긴급 롤백/디버깅 전용)
  # 기본값 1 (미설정 포함) → 실행 + 실패 시 exit 1. 스킵해도 verify_schema는 아래에서 실행됨.
  if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
    echo "Running database migrations..."
    # 최신 HEAD로 stamp — 컬럼/인덱스가 이미 DB에 존재하는 상태에서 hot 테이블 ALTER가
    # 활성 트랜잭션과 데드락 일으키는 문제 방지. 누락 컬럼이 진짜 있다면
    # alembic upgrade heads 가 IF NOT EXISTS로 추가하므로 안전.
    echo "Stamping alembic to current head..."
    uv run alembic stamp --purge zzzzzzzzzzzzzzzzzzzzzzzzzz_scp_created_at_index 2>/dev/null || true
    _MIGRATION_OK=0
    for i in 1 2 3; do
      if uv run alembic upgrade heads; then
        echo "Migrations complete."
        _MIGRATION_OK=1
        break
      else
        echo "Migration attempt $i failed, retrying in 3s..."
        sleep 3
      fi
    done
    if [ "$_MIGRATION_OK" != "1" ]; then
      echo "=========================================================="
      echo "FATAL: 마이그레이션 3회 연속 실패 — 서버 시작 차단"
      echo "  이전 리비전이 계속 서빙되며 이 revision은 교체되지 않음"
      echo "  alembic upgrade heads 로그에서 정확한 원인 확인 후"
      echo "  마이그레이션 파일 수정 or 수동 복구 후 재배포"
      echo "=========================================================="
      exit 1
    fi
  else
    echo "=========================================================="
    echo "⚠️  WARNING: RUN_MIGRATIONS=$RUN_MIGRATIONS → 마이그레이션 스킵됨"
    echo "    긴급 상황(롤백/디버깅) 외 사용 금지"
    echo "    영구 설정 시 스키마 불일치 사고 위험 (2026-04-17 4주 사고 참조)"
    echo "    복구 후 반드시 Cloud Run env에서 RUN_MIGRATIONS 제거할 것"
    echo "=========================================================="
  fi

  # 모델 ↔ DB 스키마 정합성 검증 — 불일치 시 서버 시작 차단
  echo "Verifying schema consistency..."
  if ! uv run python scripts/verify_schema.py; then
    echo "FATAL: 스키마 불일치로 서버 시작을 차단합니다. 이전 리비전이 계속 서빙됩니다."
    exit 1
  fi

  # Gunicorn + Uvicorn worker (--no-dev: 런타임 dev 패키지 재설치 방지)
  echo "Starting production server with Gunicorn (1 worker, uvicorn worker class)..."
  exec uv run --no-dev -m gunicorn -w 1 -k uvicorn.workers.UvicornWorker backend.main:app --bind 0.0.0.0:8080 --timeout 120 --graceful-timeout 600
else
  # Run the development server with Uvicorn and --reload
  echo "Starting development server with Uvicorn..."
  exec uv run -m uvicorn backend.main:app --host 0.0.0.0 --port 28080 --reload
fi

# how to kill
# fuser -k 8000/tcp

disown

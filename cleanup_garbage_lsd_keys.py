"""백필 첫 버그런이 last_sent_data 에 주입한 쓰레기 키 정리.

버그: registered_accounts(JSON 문자열)를 json.loads 안 하고 list() → 문자단위 분해 →
각 단일문자가 가짜 account_id 로 전송 시도 → 실패 경로가 last_sent_data[단일문자]=failed_at
주입. 실제 account_id 는 ~28자('ma_...'), 쓰레기는 1자. length(key)<10 인 키만 제거.

DRY-RUN 기본 — 인자 'apply' 줘야 실제 수정.
"""

import asyncio
import json
import sys

import asyncpg

from backend.core.config import settings

APPLY = len(sys.argv) > 1 and sys.argv[1] == "apply"


async def main():
    c = await asyncpg.connect(
        host=settings.write_db_host,
        port=int(settings.write_db_port),
        user=settings.write_db_user,
        password=settings.write_db_password,
        database=settings.write_db_name,
        ssl=False,
    )
    # 쓰레기 키(짧은 키) 보유 상품만 선별 — jsonb_object_keys 로 length<10 존재 확인
    rows = await c.fetch(
        "SELECT id, last_sent_data FROM samba_collected_product"
        " WHERE last_sent_data IS NOT NULL"
        " AND jsonb_typeof(CAST(last_sent_data AS jsonb)) = 'object'"
        " AND EXISTS ("
        "   SELECT 1 FROM jsonb_object_keys(CAST(last_sent_data AS jsonb)) k"
        "   WHERE length(k) < 10"
        " )"
    )
    print(f"쓰레기 키 보유 상품={len(rows)} (apply={APPLY})", flush=True)
    fixed = 0
    for r in rows:
        d = r["last_sent_data"]
        if isinstance(d, str):
            d = json.loads(d)
        clean = {k: v for k, v in (d or {}).items() if len(k) >= 10}
        removed = [k for k in (d or {}) if len(k) < 10]
        if len(clean) == len(d or {}):
            continue
        if fixed < 5:
            print(f"  {r['id']} 제거키={removed} 남김={list(clean.keys())}", flush=True)
        if APPLY:
            await c.execute(
                "UPDATE samba_collected_product SET last_sent_data = CAST($1 AS json)"
                " WHERE id = $2",
                json.dumps(clean),
                r["id"],
            )
        fixed += 1
    print(f"정리 대상={fixed} {'적용완료' if APPLY else '(DRY-RUN, apply 인자로 실행)'}", flush=True)
    await c.close()


asyncio.run(main())

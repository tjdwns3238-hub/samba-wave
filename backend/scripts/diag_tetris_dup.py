"""테트리스 transmit 중복 잡 진단 — created_at 간격으로 in-process vs cross-process 판별.

일회성 진단 스크립트. 프로덕션 VM 컨테이너에서 직접 실행.
"""

import asyncio

from sqlalchemy import text

from backend.db.orm import get_write_session


async def main() -> None:
    async with get_write_session() as s:
        # 1) 현재 pending/running 중복 조합
        rows = await s.execute(
            text("""
                SELECT
                    payload->>'source_site'         AS site,
                    payload->>'brand_name'          AS brand,
                    payload->>'target_account_ids'  AS accts,
                    count(*)                        AS cnt,
                    array_agg(created_at ORDER BY created_at) AS times,
                    array_agg(id ORDER BY created_at)         AS ids
                FROM samba_jobs
                WHERE job_type = 'transmit'
                  AND status IN ('pending', 'running')
                  AND payload->>'origin' = 'tetris_sync'
                GROUP BY 1, 2, 3
                HAVING count(*) > 1
                ORDER BY cnt DESC
            """)
        )
        dup = rows.fetchall()
        print(f"=== 중복 조합 {len(dup)}건 ===")
        for r in dup:
            times = r.times
            # 첫 잡과 둘째 잡 간격(초)
            gap = None
            if len(times) >= 2:
                gap = (times[1] - times[0]).total_seconds()
            print(
                f"[{r.cnt}x] {r.site}/{r.brand} {r.accts} "
                f"gap={gap}s times={[t.isoformat() for t in times]}"
            )

        # 2) 전체 tetris_sync 잡 created_at 분포 — 클러스터(런) 식별
        rows2 = await s.execute(
            text("""
                SELECT date_trunc('second', created_at) AS sec, count(*) AS cnt
                FROM samba_jobs
                WHERE job_type = 'transmit'
                  AND status IN ('pending', 'running')
                  AND payload->>'origin' = 'tetris_sync'
                GROUP BY 1
                ORDER BY 1
            """)
        )
        print("\n=== 초단위 생성 분포(런 클러스터) ===")
        for r in rows2.fetchall():
            print(f"{r.sec.isoformat()}  {r.cnt}건")


if __name__ == "__main__":
    asyncio.run(main())

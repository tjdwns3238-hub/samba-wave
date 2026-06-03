"""테트리스 transmit 중복 잡 정리 — 조합별 가장 먼저 생성된 1개만 남기고 나머지 cancel.

일회성 운영 스크립트. 프로덕션 VM 컨테이너에서 직접 실행.
- 대상: job_type='transmit', status='pending', origin='tetris_sync'
- running 은 이미 처리 중이므로 건드리지 않음 (pending 만 정리)
- 보존: 동일 (source_site, brand_name, target_account_ids) 중 created_at 최소(MIN) 1개
"""

import asyncio

from sqlalchemy import text

from backend.db.orm import get_write_session


async def main() -> None:
    async with get_write_session() as s:
        # pending 중복 중 가장 이른 1개를 제외한 나머지 id 수집
        rows = await s.execute(
            text("""
                WITH ranked AS (
                    SELECT
                        id,
                        row_number() OVER (
                            PARTITION BY
                                payload->>'source_site',
                                payload->>'brand_name',
                                payload->>'target_account_ids'
                            ORDER BY created_at ASC
                        ) AS rn
                    FROM samba_jobs
                    WHERE job_type = 'transmit'
                      AND status = 'pending'
                      AND payload->>'origin' = 'tetris_sync'
                )
                SELECT id FROM ranked WHERE rn > 1
            """)
        )
        dup_ids = [r.id for r in rows.fetchall()]
        print(f"정리 대상(중복 pending) {len(dup_ids)}건")
        if not dup_ids:
            print("정리할 중복 없음")
            return

        result = await s.execute(
            text("""
                UPDATE samba_jobs
                SET status = 'cancelled'
                WHERE id = ANY(:ids)
                  AND status = 'pending'
            """),
            {"ids": dup_ids},
        )
        await s.commit()
        print(f"cancelled {result.rowcount}건")


if __name__ == "__main__":
    asyncio.run(main())

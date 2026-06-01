"""롯데ON 취소클레임 목록 item 키 확인 — 승인 API용 odSeq/procSeq/orglProcSeq 존재 검증.

VM 컨테이너에서 실행. 롯데ON 계정 API 키로 getCancellationRequestAndComplateList 호출 →
원시 item 키 + odSeq/procSeq/orglProcSeq/clmNo 값 샘플 출력.
"""

import asyncio
import json

from backend.db.orm import get_read_session
from backend.domain.samba.proxy.lotteon import LotteonClient


async def main() -> None:
    from sqlalchemy import text

    async with get_read_session() as sess:
        rows = (
            await sess.execute(
                text(
                    "SELECT id, api_key, additional_fields FROM samba_market_account "
                    "WHERE market_type = 'lotteon' AND is_active = true LIMIT 5"
                )
            )
        ).fetchall()

    if not rows:
        print("롯데ON 계정 없음")
        return

    for acc_id, api_key, extras in rows:
        extras = extras or {}
        key = (extras.get("apiKey") if isinstance(extras, dict) else "") or api_key or ""
        if not key:
            print(f"계정 {acc_id}: API Key 없음")
            continue
        client = LotteonClient(key)
        try:
            claims = await client.get_cancel_orders(days=14)
        except Exception as e:
            print(f"계정 {acc_id}: 조회 실패 {e}")
            continue
        print(f"계정 {acc_id}: 취소클레임 {len(claims)}건")
        # odPrgsStepCd 분포 — '완료' 코드 확정용 (멱등 성공 처리 기준)
        step_dist: dict[str, int] = {}
        for c in claims:
            sc = str(c.get("odPrgsStepCd", "") or "")
            step_dist[sc] = step_dist.get(sc, 0) + 1
        print("  odPrgsStepCd 분포:", json.dumps(step_dist, ensure_ascii=False))
        for c in claims[:5]:
            print("  keys:", sorted(c.keys()))
            print(
                "  sample:",
                json.dumps(
                    {
                        k: c.get(k)
                        for k in (
                            "odNo",
                            "clmNo",
                            "odSeq",
                            "procSeq",
                            "orglProcSeq",
                            "odPrgsStepCd",
                            "odTypCd",
                            "clmRsnCd",
                        )
                    },
                    ensure_ascii=False,
                ),
            )
        if claims:
            return


if __name__ == "__main__":
    asyncio.run(main())

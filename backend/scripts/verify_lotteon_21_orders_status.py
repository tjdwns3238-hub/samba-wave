"""롯데ON 취소완료(odPrgsStepCd=21) 클레임 주문들의 현재 samba_order 상태 확인.

21 매핑 활성화 시 배송완료/구매확정 주문을 cancelled 로 flip 하는지 + 정산(profit) 잔존
여부를 판별하기 위한 read-only 검증. 승인 호출 없음.
"""

import asyncio
import json

from backend.db.orm import get_read_session
from backend.domain.samba.proxy.lotteon import LotteonClient


async def main() -> None:
    from sqlalchemy import text

    async with get_read_session() as sess:
        accs = (
            await sess.execute(
                text(
                    "SELECT id, api_key, additional_fields FROM samba_market_account "
                    "WHERE market_type = 'lotteon' AND is_active = true LIMIT 5"
                )
            )
        ).fetchall()

        all_od_nos: dict[str, int] = {}  # od_no → rmdrQty
        for acc_id, api_key, extras in accs:
            extras = extras or {}
            key = (
                (extras.get("apiKey") if isinstance(extras, dict) else "")
                or api_key
                or ""
            )
            if not key:
                continue
            client = LotteonClient(key)
            try:
                claims = await client.get_cancel_orders(days=14)
            except Exception as e:
                print(f"계정 {acc_id}: 조회 실패 {e}")
                continue
            for c in claims:
                if str(c.get("odPrgsStepCd", "") or "") == "21":
                    od_no = str(c.get("odNo", "") or "")
                    try:
                        rmdr = int(c.get("rmdrQty", 0) or 0)
                    except (TypeError, ValueError):
                        rmdr = 0
                    if od_no:
                        all_od_nos[od_no] = rmdr

        print(f"step=21(취소완료) 클레임 주문: {len(all_od_nos)}건")
        if not all_od_nos:
            return

        od_list = list(all_od_nos.keys())
        rows = (
            await sess.execute(
                text(
                    "SELECT od_no, status, shipping_status, profit, cost "
                    "FROM samba_order WHERE source = 'lotteon' AND od_no = ANY(:ods)"
                ),
                {"ods": od_list},
            )
        ).fetchall()

        status_dist: dict[str, int] = {}
        non_cancelled = []
        nonzero_profit_cancelled = []
        found = set()
        for od_no, status, ship, profit, cost in rows:
            found.add(od_no)
            status_dist[str(status)] = status_dist.get(str(status), 0) + 1
            if str(status) != "cancelled":
                non_cancelled.append(
                    {
                        "od_no": od_no,
                        "status": status,
                        "ship": ship,
                        "profit": float(profit or 0),
                        "rmdr": all_od_nos.get(od_no),
                    }
                )
            elif float(profit or 0) != 0 or float(cost or 0) != 0:
                nonzero_profit_cancelled.append(
                    {"od_no": od_no, "profit": float(profit or 0), "cost": float(cost or 0)}
                )

        print("현재 samba status 분포:", json.dumps(status_dist, ensure_ascii=False))
        print(f"DB 미존재: {len(set(od_list) - found)}건")
        print(
            f"cancelled 아님(=21 flip 대상): {len(non_cancelled)}건",
            json.dumps(non_cancelled[:20], ensure_ascii=False, default=str),
        )
        print(
            f"이미 cancelled 인데 profit/cost≠0: {len(nonzero_profit_cancelled)}건",
            json.dumps(nonzero_profit_cancelled[:20], ensure_ascii=False),
        )


if __name__ == "__main__":
    asyncio.run(main())

"""SSG reconciler 푸시 전 프로덕션 shape 검증 (issue #308, read-only).

3가지 load-bearing 가정 검증:
1. market_type='ssg' 계정 row 조회 (대소문자/값 확인)
2. registered_accounts @> [account_id] 쿼리 → 실제 market_product_nos 형식 출력
3. 실제 itemId 1~3개로 get_item_sales_status / get_item_approval_status raw 응답 출력
   → salesStatus.sellStatCd / chngDemndProcStatCd 위치 확인

AUTO_CLEAN 무관 — 어떤 쓰기도 안 함.
"""

import asyncio
import json

from sqlalchemy import text
from sqlmodel import select

from backend.db.orm import get_write_session
from backend.domain.samba.collector.model import SambaCollectedProduct
from backend.domain.samba.proxy.ssg import SSGClient


async def main() -> None:
    # 1) ssg 계정
    async with get_write_session() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT id, account_label, api_key, additional_fields "
                        "FROM samba_market_account "
                        "WHERE market_type='ssg' AND is_active=true"
                    )
                )
            )
            .mappings()
            .all()
        )
    accounts = [dict(r) for r in rows]
    print(f"[1] market_type='ssg' is_active=true 계정: {len(accounts)}개")
    for a in accounts:
        print(f"    - id={a['id']} label={a['account_label']}")
    if not accounts:
        print("    !! ssg 계정 0개 — market_type 값 확인 필요 (대소문자/별칭?)")
        # 진단: 전체 market_type distinct
        async with get_write_session() as session:
            mt = (
                await session.execute(
                    text(
                        "SELECT DISTINCT market_type, count(*) "
                        "FROM samba_market_account GROUP BY market_type"
                    )
                )
            ).all()
        print(f"    distinct market_type: {mt}")
        return

    acc = accounts[0]
    account_id = acc["id"]
    af = acc.get("additional_fields") or {}
    api_key = (
        str(af.get("apiKey")) if isinstance(af, dict) and af.get("apiKey") else ""
    ) or str(acc.get("api_key") or "").strip()
    print(f"\n[2] 대상 계정 {acc['account_label']} api_key 존재: {bool(api_key)}")

    # 2) registered_accounts @> [account_id] → market_product_nos 형식
    async with get_write_session() as session:
        prods = (
            (
                await session.execute(
                    select(SambaCollectedProduct)
                    .where(
                        SambaCollectedProduct.registered_accounts.op("@>")([account_id])
                    )
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
    print(f"    registered_accounts @> [{account_id}] 매칭: {len(prods)}개 (limit 5)")
    sample_item_ids: list[str] = []
    for p in prods:
        nos = p.market_product_nos or {}
        v = nos.get(account_id)
        print(f"    - product_id={p.id}")
        print(f"      market_product_nos keys={list(nos.keys())}")
        print(f"      [{account_id}] = {json.dumps(v, ensure_ascii=False)}")
        item_id = ""
        if isinstance(v, str):
            item_id = v.strip()
        elif isinstance(v, dict):
            item_id = str(v.get("itemId") or v.get("productNo") or "").strip()
        if item_id:
            sample_item_ids.append(item_id)

    if not sample_item_ids:
        print("    !! itemId 추출 실패 — _extract_item_id 가정 깨짐. 위 형식 보고 수정 필요")
        return

    # 3) 실제 itemId raw 응답
    if not api_key:
        print("\n[3] api_key 없음 — API 호출 스킵")
        return
    client = SSGClient(api_key)
    for item_id in sample_item_ids[:3]:
        print(f"\n[3] itemId={item_id}")
        try:
            ss = await client.get_item_sales_status(item_id)
            res_obj = ss.get("result", {})
            sales_status = (
                (res_obj.get("salesStatus") if isinstance(res_obj, dict) else None)
                or ss.get("salesStatus")
                or {}
            )
            sell_stat = (
                sales_status.get("sellStatCd")
                if isinstance(sales_status, dict)
                else None
            )
            print(f"    sales-status sellStatCd={sell_stat}")
            print(f"    salesStatus keys={list(sales_status.keys()) if isinstance(sales_status, dict) else type(sales_status)}")
        except Exception as e:
            print(f"    sales-status 조회 실패: {e}")
        try:
            demands = await client.get_item_approval_status(item_id, "00")
            print(f"    approval(div=00) 건수={len(demands or [])}")
            for d in (demands or [])[:2]:
                if isinstance(d, dict):
                    print(
                        f"      chngDemndProcStatCd={d.get('chngDemndProcStatCd')} "
                        f"keys={list(d.keys())[:8]}"
                    )
        except Exception as e:
            print(f"    approval 조회 실패: {e}")


if __name__ == "__main__":
    asyncio.run(main())

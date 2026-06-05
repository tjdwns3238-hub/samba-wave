"""SSG listBrand 실제 응답 구조 실측 — 이슈 #358 동적 브랜드 해석 구현 전 검증.

프로덕션 SSG 계정 api_key 로 get_brands(keyword) 실제 호출.
응답 구조(result.brands 중첩 여부, brandId 타입, useYn)를 확인한다.
VM 컨테이너에서 실행: /app/backend/.venv/bin/python3 /tmp/diag_ssg_listbrand_probe.py
"""

import asyncio
import json

from sqlalchemy import text

from backend.db.orm import get_read_session
from backend.domain.samba.proxy.ssg import SSGClient


async def main() -> None:
    async with get_read_session() as s:
        dist = (
            await s.execute(
                text(
                    "SELECT market_type, count(*), "
                    "sum(CASE WHEN is_active THEN 1 ELSE 0 END) "
                    "FROM samba_market_account GROUP BY market_type ORDER BY 2 DESC"
                )
            )
        ).all()
        print("=== market_type 분포 (type, 전체, 활성) ===")
        for d in dist:
            print(f"  {d[0]}: total={d[1]} active={d[2]}")

        rows = (
            await s.execute(
                text(
                    "SELECT api_key, additional_fields, market_name, market_type, is_active "
                    "FROM samba_market_account "
                    "WHERE market_type ILIKE '%ssg%' OR market_name LIKE '%신세계%' "
                    "LIMIT 20"
                )
            )
        ).all()

    if not rows:
        print("NO_SSG_ACCOUNT — ssg/신세계 계정 전혀 없음")
        return
    print(f"\nssg/신세계 후보 {len(rows)}개")

    print(f"ssg 활성 계정 {len(rows)}개\n")

    def _parse_brands(resp: dict) -> list[dict]:
        """result.brands[*].brand (단일 dict / 복수 list / 빈 '') 평탄화."""
        out: list[dict] = []
        res = resp.get("result") if isinstance(resp, dict) else None
        raw = (res or {}).get("brands") or []
        for item in raw:
            if not isinstance(item, dict):
                continue
            b = item.get("brand")
            if isinstance(b, dict):
                out.append(b)
            elif isinstance(b, list):
                out.extend(x for x in b if isinstance(x, dict))
        return out

    for ai, r in enumerate(rows):
        _ak, _af, _mn = r[0], r[1] or {}, r[2]
        if isinstance(_af, str):
            try:
                _af = json.loads(_af)
            except (ValueError, TypeError):
                _af = {}
        api_key = _ak or _af.get("apiKey") or ""
        store_id = str(_af.get("storeId") or "6004")
        sid = _af.get("storeId")
        print(f"\n########## 계정#{ai} {_mn} store_id={store_id}(설정값={sid}) api_key={api_key[:6]}… ##########")
        if not api_key:
            print("  apiKey 없음 — skip")
            continue
        client = SSGClient(api_key, site_no=store_id)

        # 전체 계약목록 (페이지네이션 여부 확인용 count)
        try:
            full = await client.get_brands("")
            fb = _parse_brands(full)
            print(f"  전체 계약 브랜드 수: {len(fb)}")
            print(f"  목록: {[b.get('brandNm') for b in fb]}")
        except Exception as e:  # noqa: BLE001
            print(f"  전체조회 ERROR: {type(e).__name__}: {e}")

        for kw in ["써코니", "아식스", "아이더", "다이나핏", "조던"]:
            try:
                resp = await client.get_brands(kw)
                pb = _parse_brands(resp)
                hit = [
                    (b.get("brandId"), b.get("brandNm"), b.get("useYn")) for b in pb
                ]
                print(f"  keyword='{kw}' → 후보 {len(pb)}개: {hit}")
            except Exception as e:  # noqa: BLE001
                print(f"  keyword='{kw}' ERROR: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())

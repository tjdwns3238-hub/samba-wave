"""이슈 #358 동적 브랜드 해석 fix 검증 — 두 SSG 계정 실데이터 시뮬레이션.

신규 코드(get_contracted_brand_map) 로직을 인라인 재현(기존 get_brands+match_brand)해
미배포 상태로 end-to-end 검증한다. 각 브랜드의 최종 brandId가 기타(9999999999)→정식으로
복구되는지 확인.
VM: /app/backend/.venv/bin/python3 /tmp/diag_ssg_brand_resolve_verify.py
"""

import asyncio
import json

from sqlalchemy import text

from backend.db.orm import get_read_session
from backend.domain.samba.proxy.ssg import SSGClient


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "")


def _parse(resp: dict) -> list[dict]:
    out: list[dict] = []
    res = resp.get("result") if isinstance(resp, dict) else None
    for item in (res or {}).get("brands") or []:
        if not isinstance(item, dict):
            continue
        b = item.get("brand")
        if isinstance(b, dict):
            out.append(b)
        elif isinstance(b, list):
            out.extend(x for x in b if isinstance(x, dict))
    return out


async def main() -> None:
    async with get_read_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT api_key, additional_fields, market_name "
                    "FROM samba_market_account "
                    "WHERE market_type='ssg' AND is_active=true LIMIT 20"
                )
            )
        ).all()

    test_brands = ["아식스", "아이더", "다이나핏", "써코니", "나이키", "노스페이스"]

    for ai, r in enumerate(rows):
        _ak, _af, _mn = r[0], r[1] or {}, r[2]
        if isinstance(_af, str):
            try:
                _af = json.loads(_af)
            except (ValueError, TypeError):
                _af = {}
        api_key = _ak or _af.get("apiKey") or ""
        if not api_key:
            continue
        store_id = str(_af.get("storeId") or "6004")
        client = SSGClient(api_key, site_no=store_id)

        # 계약 브랜드 맵 빌드 (get_contracted_brand_map 로직 재현)
        cmap: dict[str, str] = {}
        for b in _parse(await client.get_brands("")):
            if b.get("useYn") != "Y":
                continue
            bid, nm = b.get("brandId"), b.get("brandNm") or ""
            if bid is None or str(bid) == "9999999999" or not nm:
                continue
            cmap[_norm(nm)] = str(bid)

        print(f"\n###### 계정#{ai} {_mn} (계약 {len(cmap)}개) api_key={api_key[:6]}… ######")
        for brand in test_brands:
            # 1) 하드코딩 match_brand
            hc_id, _ = SSGClient.match_brand(brand)
            # 2) 미해결이면 동적 cmap 보강
            if hc_id == "9999999999":
                dyn = cmap.get(_norm(brand))
                final = dyn or "9999999999"
                src = "동적해석" if dyn else "기타폴백(미계약)"
            else:
                final = hc_id
                src = "하드코딩"
            mark = "✅복구" if (hc_id == "9999999999" and final != "9999999999") else ""
            print(f"  {brand:6s}: 하드코딩={hc_id:>10s} → 최종={final:>10s} [{src}] {mark}")


if __name__ == "__main__":
    asyncio.run(main())

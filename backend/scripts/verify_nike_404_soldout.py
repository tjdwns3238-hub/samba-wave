"""Nike 404 → sold_out 매핑 검증 스크립트.

A안 패치(plugins/sourcing/nike.py)가 IU3054-233(단종 컬러)에서
HTTPStatusError(404)를 잡아 sold_out RefreshResult로 정상 변환하는지 확인.

대조군으로 IU3054-054(재고 있는 컬러)도 호출해 회귀 없는지 본다.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.domain.samba.plugins.sourcing.nike import NikePlugin


async def run_case(plugin: NikePlugin, label: str, style_color: str) -> None:
    product = SimpleNamespace(
        id=f"test-{style_color}",
        site_product_id=style_color,
        sale_status="in_stock",
        sale_price=169000,
        original_price=169000,
        options=[],
    )
    print(f"\n[{label}] {style_color} refresh 시작")
    result = await plugin.refresh(product)
    print(f"  error              : {result.error}")
    print(f"  new_sale_status    : {result.new_sale_status}")
    print(f"  stock_changed      : {result.stock_changed}")
    print(f"  deleted_from_source: {result.deleted_from_source}")
    print(f"  new_sale_price     : {result.new_sale_price}")
    print(f"  new_original_price : {result.new_original_price}")
    opts = result.new_options
    print(f"  new_options count  : {len(opts) if opts is not None else 'None'}")


async def main() -> None:
    plugin = NikePlugin()
    # 화면 [실패] 케이스
    await run_case(plugin, "IU3054-233 화면에서 [실패]", "IU3054-233")
    # 같은 모델 다른 컬러 — 회귀 확인
    await run_case(plugin, "IU3054-054 대조군", "IU3054-054")
    # NikeSKIMS 다른 모델(쇼츠) — 화면에 정상 등록
    await run_case(plugin, "IU3052 NSKM 쇼츠 정상등록", "IU3052-200")
    # 존재할 가능성 낮은 styleColor — 검색 0건 또는 fallback 경로 확인
    await run_case(plugin, "존재 안 함 추정", "XX9999-999")


if __name__ == "__main__":
    asyncio.run(main())

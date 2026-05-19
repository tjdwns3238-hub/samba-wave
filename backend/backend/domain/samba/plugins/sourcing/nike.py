"""나이키 소싱처 플러그인."""

import logging
from typing import TYPE_CHECKING

import httpx

from backend.domain.samba.plugins.sourcing_base import SourcingPlugin

if TYPE_CHECKING:
    from backend.domain.samba.collector.refresher import RefreshResult

logger = logging.getLogger(__name__)


class NikePlugin(SourcingPlugin):
    """나이키 소싱처 플러그인.

    concurrency=5: 동시 5개 요청 (Nike CDN 안정적)
    request_interval=0.3: 요청 간 300ms 딜레이
    """

    site_name = "NIKE"
    concurrency = 5
    request_interval = 0.3

    async def search(self, keyword: str, **filters) -> list[dict]:
        """나이키 키워드 검색."""
        from backend.domain.samba.proxy.nike import NikeClient

        max_count = int(filters.get("max_count", 100))
        client = NikeClient()
        result = await self.safe_call(client.search(keyword, max_count=max_count))
        return result.get("products", [])

    async def scan_categories(self, keyword: str) -> dict:
        """나이키 카테고리 스캔."""
        from backend.domain.samba.proxy.nike import NikeClient

        client = NikeClient()
        return await self.safe_call(client.scan_categories(keyword))

    async def get_detail(self, site_product_id: str) -> dict:
        """나이키 상품 상세 조회."""
        from backend.domain.samba.proxy.nike import NikeClient

        client = NikeClient()
        return await self.safe_call(client.get_detail(site_product_id))

    async def refresh(self, product) -> "RefreshResult":
        """가격/재고 갱신 — NikeClient로 재조회 후 변경분 반환."""
        from backend.domain.samba.collector.refresher import RefreshResult
        from backend.domain.samba.proxy.nike import NikeClient

        product_id = getattr(product, "id", "")
        site_product_id = getattr(product, "site_product_id", "")

        if not site_product_id:
            return RefreshResult(product_id=product_id, error="site_product_id 없음")

        try:
            client = NikeClient()
            fresh = await client.get_detail(site_product_id)
        except httpx.HTTPStatusError as e:
            # PDP 404 = Nike Korea에서 해당 컬러 단종/품절 (페이지는 SSR로 살아있지만 detail API는 404)
            # changed=True는 abcmart/gsshop/lotteon/musinsa 동일 패턴 — sold_out 모니터 이벤트 발행 경로 진입용
            if e.response.status_code == 404:
                return RefreshResult(
                    product_id=product_id,
                    new_sale_status="sold_out",
                    changed=True,
                    deleted_from_source=True,
                )
            logger.warning(f"[Nike] 갱신 실패 {site_product_id}: {e}")
            return RefreshResult(product_id=product_id, error=str(e))
        except Exception as e:
            logger.warning(f"[Nike] 갱신 실패 {site_product_id}: {e}")
            return RefreshResult(product_id=product_id, error=str(e))

        if fresh.get("error"):
            return RefreshResult(product_id=product_id, error=fresh["error"])

        new_sale_price = fresh.get("sale_price")
        new_original_price = fresh.get("original_price")
        new_options = fresh.get("options")  # 사이즈 목록

        old_sale_price = getattr(product, "sale_price", None)
        old_original_price = getattr(product, "original_price", None)

        price_changed = (
            new_sale_price is not None
            and new_sale_price != old_sale_price
            or new_original_price is not None
            and new_original_price != old_original_price
        )
        # 재고 품절 감지: 옵션 없거나 모든 옵션 stock=0이면 품절
        if not new_options:
            new_sale_status = "sold_out"
        elif all(opt.get("stock", 0) <= 0 for opt in new_options):
            new_sale_status = "sold_out"
        else:
            new_sale_status = "in_stock"

        # 옵션별 0 경계 전환을 stock_changed로 인정 — 일부 옵션만 품절/재입고된 경우도 감지
        from backend.domain.samba.collector.refresher import count_stock_transitions

        old_options = getattr(product, "options", None) or []
        _stock_changes = count_stock_transitions(old_options, new_options or [])
        old_sale_status = getattr(product, "sale_status", "in_stock")
        stock_changed = _stock_changes > 0 or new_sale_status != old_sale_status

        return RefreshResult(
            product_id=product_id,
            new_sale_price=new_sale_price,
            new_original_price=new_original_price,
            new_sale_status=new_sale_status,
            new_options=new_options,
            changed=price_changed,
            stock_changed=stock_changed,
        )

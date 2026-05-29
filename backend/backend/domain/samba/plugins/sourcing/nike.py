"""나이키 소싱처 플러그인."""

import logging
import os
from typing import TYPE_CHECKING

import httpx

from backend.domain.samba.plugins.sourcing_base import SourcingPlugin

if TYPE_CHECKING:
    from backend.domain.samba.collector.refresher import RefreshResult

logger = logging.getLogger(__name__)


def _availability_enabled() -> bool:
    """`NIKE_AVAILABILITY_ENABLED` env가 truthy면 size-level reconcile 활성.

    default off (opt-in). 운영 동작 변경 없이 머지 후 별도 env 설정으로 켤 수 있도록 함.
    """
    return os.environ.get("NIKE_AVAILABILITY_ENABLED", "0").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


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
            err = fresh["error"]
            # search_not_found = 검색 인덱스 누락/단종 → sold_out 이벤트 경로 진입
            # (abcmart/gsshop/lotteon/musinsa의 404 처리와 동일 패턴)
            if err == "search_not_found":
                return RefreshResult(
                    product_id=product_id,
                    new_sale_status="sold_out",
                    changed=True,
                    deleted_from_source=True,
                )
            return RefreshResult(product_id=product_id, error=err)

        new_sale_price = fresh.get("sale_price")
        new_original_price = fresh.get("original_price")
        new_options = fresh.get("options")  # 사이즈 목록

        # 사이즈별 availability reconcile — opt-in (NIKE_AVAILABILITY_ENABLED=1).
        # PDP `sizes[*].status='ACTIVE'`는 listing availability 메타데이터라
        # size-level stock source로는 부적합. 일부 SKU에서 모든 사이즈가 ACTIVE로
        # 평탄화되어 sold_out/restock 감지 불가한 경로가 있음.
        # threads API(_fetch_availability)는 GTIN→available bool을 노출하므로
        # 매칭된 GTIN만 stock 보정. 응답 누락/실패는 기존 파서 결과 유지 (보수적).
        if new_options and _availability_enabled():
            try:
                availability = await client._fetch_availability(site_product_id)
            except Exception as e:
                logger.warning(
                    f"[Nike] availability 조회 실패 {site_product_id}: {e}"
                )
                availability = {}
            if availability:
                matched = 0
                missing = 0
                for opt in new_options:
                    gtin = opt.get("gtin")
                    if not gtin:
                        continue
                    if gtin in availability:
                        opt["stock"] = 99 if availability[gtin] else 0
                        matched += 1
                    else:
                        # threads API 응답에 없는 GTIN — 기존 stock 유지 (절대 0 강제 X).
                        # 응답 누락은 region/캐시/일시 숨김 등 다양한 원인 가능.
                        missing += 1
                if missing:
                    logger.info(
                        "[Nike] availability reconcile %s: matched=%d missing_gtin=%d",
                        site_product_id,
                        matched,
                        missing,
                    )

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

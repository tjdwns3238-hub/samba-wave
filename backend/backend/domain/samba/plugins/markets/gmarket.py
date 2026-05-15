"""지마켓 마켓 플러그인 — ESM Plus API 기반 (siteType=2).

ESM Trading API v2를 통해 지마켓 상품 등록/수정/삭제.
옥션 플러그인(auction.py)과 동일한 ESMPlusClient를 공유하며
siteType, siteKey, ssiPrefix만 다르다.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from backend.domain.samba.plugins.market_base import MarketPlugin
from backend.utils import add_lazy_loading
from backend.utils.logger import logger

# ESM Plus 호스팅 인증정보는 서버 환경변수(ESMPLUS_HOSTING_ID/ESMPLUS_SECRET_KEY)에서 로드


class GMarketMarketPlugin(MarketPlugin):
    """지마켓 판매처 플러그인 — ESM Plus siteType=2."""

    market_type = "gmarket"
    policy_key = "지마켓"
    required_fields = ["name", "sale_price"]

    def transform(self, product: dict, category_id: str, **kwargs) -> dict:
        from backend.domain.samba.proxy.esmplus import ESMPlusClient

        return ESMPlusClient.transform_product(product, category_id, site="gmarket")

    async def execute(
        self,
        session,
        product: dict,
        creds: dict,
        category_id: str,
        account,
        existing_no: str,
    ) -> dict[str, Any]:
        """지마켓 상품 등록/수정."""
        from backend.domain.samba.proxy.esmplus import ESMPlusClient

        # 판매자 ID — creds에서 apiKey(sellerId) 가져오기
        seller_id = creds.get("apiKey", "") or creds.get("sellerId", "")
        if not seller_id:
            return {
                "success": False,
                "message": "지마켓 판매자 ID(apiKey)가 없습니다. 계정 설정에서 입력해주세요.",
            }

        # 호스팅 인증정보 — 서버 환경변수에서 로드 (셀링툴업체 고정값)
        from backend.core.config import settings

        hosting_id = settings.esmplus_hosting_id
        secret_key = settings.esmplus_secret_key
        if not hosting_id or not secret_key:
            return {
                "success": False,
                "message": "서버 환경변수(ESMPLUS_HOSTING_ID/ESMPLUS_SECRET_KEY)가 설정되지 않았습니다.",
            }

        client = ESMPlusClient(hosting_id, secret_key, seller_id, site="gmarket")

        # 상품 데이터 복사 + 계정 설정 주입
        product_copy = dict(product)
        product_copy = await self._inject_account_settings(
            session, product_copy, account
        )

        # 상세 HTML 프로토콜 보정 + lazy loading 삽입
        detail_html = product_copy.get("detail_html", "")
        if detail_html:
            detail_html = re.sub(r'(src=["\'])\/\/', r"\1https://", detail_html)
            product_copy["detail_html"] = add_lazy_loading(detail_html)

        # transform
        data = ESMPlusClient.transform_product(
            product_copy, category_id, site="gmarket"
        )

        # 이미지 모델 (등록 후 별도 API 호출용)
        pending_images = data.pop("_pending_images", None)

        # 가격/재고만 업데이트 모드
        skip_image = product.get("_skip_image_upload", False) and bool(existing_no)
        price_only = product.get("_price_stock_only", False)

        if skip_image or price_only:
            # sell-status API로 가격/재고만 수정
            return await self._update_price_stock(
                client, existing_no, product_copy, data
            )

        # 등록/수정 분기
        if existing_no:
            return await self._update_product(client, existing_no, data, pending_images)
        else:
            samba_options = product.get("options") or []
            return await self._register_product(
                client,
                data,
                pending_images,
                samba_options=samba_options,
                cat_code=category_id,
            )

    async def _register_product(
        self,
        client: Any,
        data: dict[str, Any],
        pending_images: dict | None,
        samba_options: list[dict] | None = None,
        cat_code: str = "",
    ) -> dict[str, Any]:
        """신규 상품 등록 + 옵션/이미지 후처리."""
        result = await client.register_product(data)
        goods_no = result.get("goodsNo", "")
        site_goods_no = result.get("siteGoodsNo", "")

        # 추가 이미지 설정 (등록 후 propagation 대기 필요 — ESM CDN 캐시)
        if pending_images and goods_no:
            try:
                await asyncio.sleep(3)
                await client.update_images(goods_no, {"imageModel": pending_images})
                logger.info(f"[지마켓] 추가 이미지 설정 완료: goodsNo={goods_no}")
            except Exception as img_e:
                logger.warning(
                    f"[지마켓] 추가 이미지 설정 실패 (등록 직후 제한): {img_e}"
                )

        # 추천옵션 등록 — samba options 있고 cat_code 있을 때만.
        # ESM 측 이미지 캐시 propagation 미완료 시 옵션 PUT 거부 → 30s sleep.
        if samba_options and goods_no and cat_code:
            try:
                from backend.domain.samba.proxy.esmplus import register_esm_options

                await asyncio.sleep(30)
                opt_result = await register_esm_options(
                    client, goods_no, cat_code, samba_options, site="gmarket"
                )
                if opt_result.get("success"):
                    logger.info(
                        f"[지마켓] 옵션 등록 완료: goodsNo={goods_no} matched={opt_result.get('matched')}/{opt_result.get('requested')}"
                    )
                else:
                    logger.warning(
                        f"[지마켓] 옵션 등록 부분 실패: {opt_result.get('message')}"
                    )
            except Exception as opt_e:
                logger.warning(
                    f"[지마켓] 옵션 등록 실패 (상품 등록은 성공 처리): {opt_e}"
                )

        return {
            "success": True,
            "message": "지마켓 등록 성공",
            "data": {
                "sellerProductId": str(goods_no),
                "siteGoodsNo": site_goods_no,
            },
        }

    async def _update_product(
        self,
        client: Any,
        goods_no: str,
        data: dict[str, Any],
        pending_images: dict | None,
    ) -> dict[str, Any]:
        """기존 상품 수정."""
        try:
            await client.update_product(goods_no, data)
        except RuntimeError as e:
            err_msg = str(e)
            # 상품 없음 → 신규등록 전환
            if "상품이 없습니다" in err_msg or "not exist" in err_msg.lower():
                logger.warning(f"[지마켓] 상품 {goods_no} 없음 → 신규등록 전환")
                result = await client.register_product(data)
                new_goods_no = result.get("goodsNo", "")
                return {
                    "success": True,
                    "message": "지마켓 등록 성공 (기존 상품 없음 → 신규)",
                    "data": {"sellerProductId": str(new_goods_no)},
                    "_clear_product_no": True,
                }
            raise

        # 추가 이미지 업데이트
        if pending_images:
            try:
                await client.update_images(goods_no, {"imageModel": pending_images})
            except Exception as img_e:
                logger.warning(f"[지마켓] 이미지 수정 실패: {img_e}")

        return {
            "success": True,
            "message": "지마켓 수정 성공",
            "data": {"sellerProductId": goods_no},
        }

    async def _update_price_stock(
        self,
        client: Any,
        goods_no: str,
        product: dict,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """가격/재고만 수정 — sell-status API 활용."""
        if not goods_no:
            return {"success": False, "message": "상품번호가 없어 가격/재고 수정 불가"}

        # ESM Plus 스펙 — 등록과 sell-status 모두 PascalCase(Gmkt). 실 호출 검증 결과
        # 'isSell' camelCase 는 'IsSell 필드가 필요합니다' 응답 → PascalCase 통일.
        price = data.get("itemAddtionalInfo", {}).get("price", {}).get("Gmkt", 0)
        stock = data.get("itemAddtionalInfo", {}).get("stock", {}).get("Gmkt", 0)

        sell_data: dict[str, Any] = {
            "IsSell": {"Gmkt": True},
            "itemBasicInfo": {
                "price": {"Gmkt": price},
                "stock": {"Gmkt": stock},
                "sellingPeriod": {"Gmkt": 0},  # 0=기존 유지
            },
        }

        try:
            await client.update_sell_status(goods_no, sell_data)
            logger.info(
                f"[지마켓] 가격/재고 수정 성공: goodsNo={goods_no}, price={price}, stock={stock}"
            )
            return {
                "success": True,
                "message": "지마켓 가격/재고 수정 성공",
                "data": {"sellerProductId": goods_no},
            }
        except RuntimeError as e:
            if "상품이 없습니다" in str(e):
                return {
                    "success": False,
                    "error_type": "product_not_found",
                    "message": f"상품 #{goods_no}이 지마켓에 없습니다.",
                    "_clear_product_no": True,
                }
            raise

    async def delete(self, session, product_no: str, account) -> dict[str, Any]:
        """지마켓 상품 판매중지 → 삭제."""
        from backend.domain.samba.proxy.esmplus import ESMPlusClient

        creds = await self._load_auth(session, account)
        if not creds:
            return {"success": False, "message": "인증정보 없음"}

        seller_id = creds.get("apiKey", "") or creds.get("sellerId", "")
        if not seller_id:
            return {"success": False, "message": "지마켓 판매자 ID 없음"}

        from backend.core.config import settings

        hosting_id = settings.esmplus_hosting_id
        secret_key = settings.esmplus_secret_key
        if not hosting_id or not secret_key:
            return {"success": False, "message": "호스팅 인증정보(환경변수) 없음"}
        client = ESMPlusClient(hosting_id, secret_key, seller_id, site="gmarket")

        # 판매중지 — 실 호출 검증 schema (PascalCase). 'IsSell' 만으로도 ESM 측 검증 통과.
        suspend_data = {"IsSell": {"Gmkt": False}}
        await client.update_sell_status(product_no, suspend_data)
        logger.info(f"[지마켓] 판매중지 완료: goodsNo={product_no}")
        return {"success": True, "message": "지마켓 판매중지 완료"}

    async def _inject_account_settings(self, session, product: dict, account) -> dict:
        """계정/정책에서 마켓별 설정 주입."""
        if account:
            extras = account.additional_fields or {}
            if extras.get("asPhone"):
                product["_as_phone"] = extras["asPhone"]
            if extras.get("stockQuantity"):
                product["_stock_quantity"] = int(extras["stockQuantity"])
            if extras.get("shippingCompanyNo"):
                product["_shipping_company_no"] = int(extras["shippingCompanyNo"])
            if extras.get("dispatchPolicyNo"):
                product["_dispatch_policy_no"] = int(extras["dispatchPolicyNo"])
            if extras.get("shippingPlaceNo"):
                product["_shipping_place_no"] = int(extras["shippingPlaceNo"])
            if extras.get("returnPlaceNo"):
                product["_return_place_no"] = int(extras["returnPlaceNo"])
            if extras.get("shippingFeeType"):
                product["_delivery_fee_type"] = extras["shippingFeeType"]
            if extras.get("shippingFee"):
                product["_delivery_base_fee"] = int(extras["shippingFee"])

        # 정책에서 배송비/재고 제한 읽기
        policy_id = product.get("applied_policy_id")
        if policy_id:
            from backend.domain.samba.policy.repository import SambaPolicyRepository

            policy_repo = SambaPolicyRepository(session)
            policy = await policy_repo.get_async(policy_id)
            if policy:
                pr = policy.pricing or {}
                mp = (policy.market_policies or {}).get("지마켓", {})
                shipping = int(mp.get("shippingCost") or pr.get("shippingCost") or 0)
                if shipping > 0:
                    product["_delivery_fee_type"] = "PAID"
                    product["_delivery_base_fee"] = shipping
                if mp.get("maxStock"):
                    product["_max_stock"] = mp["maxStock"]

        return product

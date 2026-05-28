"""쿠팡 마켓 플러그인.

기존 dispatcher._handle_coupang 로직을 플러그인 구조로 추출.
인증 로드는 base._load_auth 가 처리하므로 execute 에서는 creds dict 사용.
"""

from __future__ import annotations

from typing import Any

from backend.domain.samba.plugins.market_base import MarketPlugin
from backend.utils.logger import logger


class CoupangPlugin(MarketPlugin):
    market_type = "coupang"
    policy_key = "쿠팡"
    required_fields = ["name", "sale_price"]

    def _validate_category(self, category_id: str) -> str:
        """쿠팡은 비숫자 카테고리(경로 문자열)도 허용 — resolve_category_code 로 동적 조회."""
        return category_id

    def transform(self, product: dict, category_id: str, **kwargs) -> dict:
        """상품 데이터 → 쿠팡 API 포맷 변환."""
        from backend.domain.samba.proxy.coupang import CoupangClient

        return CoupangClient.transform_product(
            product,
            category_id,
            return_center_code=kwargs.get("return_center_code", ""),
            outbound_shipping_place_code=kwargs.get("outbound_shipping_place_code", ""),
        )

    async def execute(
        self,
        session,
        product: dict,
        creds: dict,
        category_id: str,
        account,
        existing_no: str,
    ) -> dict[str, Any]:
        """쿠팡 상품 등록/수정 — 전체 로직."""
        from backend.domain.samba.proxy.coupang import CoupangClient

        access_key = creds.get("accessKey", "")
        secret_key = creds.get("secretKey", "")
        vendor_id = creds.get("vendorId", "")

        # account 필드에서 보완
        if account:
            access_key = access_key or getattr(account, "api_key", "") or ""
            secret_key = secret_key or getattr(account, "api_secret", "") or ""
            vendor_id = vendor_id or getattr(account, "seller_id", "") or ""

        if not access_key or not secret_key:
            return {
                "success": False,
                "message": "쿠팡 Access Key/Secret Key가 없습니다.",
            }

        if not vendor_id:
            return {
                "success": False,
                "message": "쿠팡 Vendor ID가 없습니다. 계정 설정을 확인해주세요.",
            }

        client = CoupangClient(access_key, secret_key, vendor_id)

        # ── 등록된 상품 가격/재고 업데이트 — 부분 endpoint(vendor-items) ──
        # 쿠팡 spec 상 update_product PUT(/seller-products/{id})는 정의되지 않아 404.
        # 가격/재고는 vendor-items 부분 endpoint로 가야 함. 이미지/이름 등 다른
        # 필드 변경은 본 분기에서 다루지 않음 (별도 의도 — 후속 PR).
        if existing_no:
            try:
                existing = await client.get_product(existing_no)
                prod_data = existing.get("data", existing)
                if isinstance(prod_data, dict):
                    items = prod_data.get("items") or []
                else:
                    items = []

                if not items:
                    logger.warning(
                        f"[쿠팡] 경량 업데이트 실패 — items 없음, 전체 수정으로 폴백: {existing_no}"
                    )
                else:
                    new_price = int(product.get("sale_price", 0)) // 10 * 10
                    new_options = product.get("options") or []
                    opt_stock_map = {
                        (o.get("name", "") or o.get("size", "") or ""): o.get(
                            "stock", 999
                        )
                        for o in new_options
                    }

                    # vendorItemId 단위 부분 endpoint(/prices, /quantities)로 호출.
                    # update_product PUT(/seller-products/{id})는 쿠팡 spec에 미정의(GET/DELETE만) — 404 반환됨.
                    price_updates = 0
                    qty_updates = 0
                    skipped = 0
                    for item in items:
                        vendor_item_id = item.get("vendorItemId")
                        if not vendor_item_id:
                            skipped += 1
                            continue

                        # 가격: 변경 시만 호출
                        if new_price > 0 and item.get("salePrice") != new_price:
                            await client.update_item_price(vendor_item_id, new_price)
                            price_updates += 1

                        # 재고: 옵션명 매칭 후 변경 시만 호출
                        item_name = item.get("itemName", "")
                        if item_name in opt_stock_map:
                            stk = opt_stock_map[item_name]
                        elif new_options:
                            stk = min(
                                (o.get("stock", 999) for o in new_options),
                                default=999,
                            )
                        else:
                            stk = 999
                        new_stk = min(int(stk), 99999)
                        # maximumBuyCount는 1회 구매 한도 필드라 PUT /quantities 후 GET 응답에 반영되지 않음.
                        # 이전 조건 비교는 항상 false로 떨어져 재고 API 호출이 스킵되는 버그가 있었음(issue #200).
                        await client.update_item_quantity(vendor_item_id, new_stk)
                        qty_updates += 1

                    _parts = []
                    if new_price > 0:
                        _parts.append(f"가격({new_price:,}원, {price_updates}건)")
                    if new_options:
                        _parts.append(f"재고({qty_updates}건)")
                    if skipped:
                        _parts.append(f"skip({skipped}건 vendorItemId 없음)")
                    logger.info(
                        f"[쿠팡] 경량 업데이트 완료: {existing_no} — {', '.join(_parts)}"
                    )
                    return {
                        "success": True,
                        "product_no": existing_no,
                        "message": f"쿠팡 경량 업데이트: {', '.join(_parts)}",
                        "data": {"sellerProductId": existing_no},
                    }

            except Exception as e:
                logger.warning(
                    f"[쿠팡] 경량 업데이트 실패, 전체 수정으로 폴백: {existing_no} — {e}"
                )
                # 폴백: 아래 전체 로직으로 계속 진행

        # 카테고리 코드가 숫자가 아니면 쿠팡 API로 동적 조회
        if category_id and not str(category_id).isdigit():
            resolved = await client.resolve_category_code(category_id)
            category_id = str(resolved) if resolved else ""

        # vendorUserId: Wing 로그인 ID (seller_id 사용)
        vendor_user_id = ""
        if account:
            vendor_user_id = getattr(account, "seller_id", "") or ""

        # 계정별 사전 저장된 출고지/반품지 코드 읽기 (다계정 자연 지원)
        extras = (account.additional_fields or {}) if account else {}
        if not isinstance(extras, dict):
            extras = {}
        outbound_code = str(extras.get("outboundShippingPlaceCode", "") or "")
        return_center_code = str(extras.get("returnCenterCode", "") or "")
        return_address = str(extras.get("returnCenterAddress", "") or "")
        return_address_detail = str(extras.get("returnCenterAddressDetail", "") or "")
        return_zipcode = str(extras.get("returnCenterZipcode", "") or "")
        return_phone = str(extras.get("returnCenterPhone", "") or "")

        if not outbound_code or not return_center_code:
            return {
                "success": False,
                "message": "쿠팡 설정에서 출고지/반품지를 먼저 조회 후 선택해주세요.",
            }

        # 카테고리별 정확한 noticeCategoryName/Detail을 쿠팡 메타 API로 동적 조회
        # — 의류/신발 등록 시 정적 매핑이 쿠팡 표준과 미스매치되어 옵션 notice가 거부되는
        # 문제(2026-05 보고)의 근본 해결. 실패 시 transform_product 내부에서 정적 매핑 폴백.
        notice_meta = None
        if category_id and str(category_id).isdigit():
            try:
                notice_meta = await client.get_notice_categories(str(category_id))
            except Exception as _e:
                # 메타 조회 실패는 등록 자체를 막지 않음 — fallback 사용
                pass

        # 쿠팡 이미지 검증 사양 정규화 — 승인 반려 사유 대응
        # 사양: 최대 10MB / 최소 500x500 / 최대 5000x5000 (대표/추가/DETAIL/detail_html 공통)
        try:
            from backend.domain.samba.image.service import ImageTransformService

            _img_svc = ImageTransformService(session)
            _kw = dict(
                max_bytes=10 * 1024 * 1024,
                max_dim=5000,
                min_dim=500,
                enforce_max_dim=True,
            )
            product = dict(product)  # 원본 dict 변형 방지
            _images = product.get("images") or []
            _detail_images = product.get("detail_images") or []
            _main = product.get("coupang_main_image") or ""
            _detail_html = product.get("detail_html") or ""

            if _images:
                product["images"], _ = await _img_svc.mirror_oversized_to_r2(
                    _images, **_kw
                )
            if _detail_images:
                (
                    product["detail_images"],
                    _,
                ) = await _img_svc.mirror_oversized_to_r2(_detail_images, **_kw)
            if _main:
                fixed, _ = await _img_svc.mirror_oversized_to_r2([_main], **_kw)
                product["coupang_main_image"] = fixed[0] if fixed else ""
            if _detail_html:
                product["detail_html"] = await _img_svc.mirror_oversized_in_html(
                    _detail_html, **_kw
                )
        except Exception as e:
            logger.warning(f"[쿠팡] 이미지 정규화 단계 오류 — 원본 URL 유지: {e}")

        # 2026-08-01 쿠팡 brandId 의무화 — 브랜드 검색 API 로 매핑 (실패 시 brand 문자열만 사용)
        brand_id = ""
        brand_name = (product.get("brand") or "").strip()
        if brand_name:
            try:
                brand_id = await client.search_brand_id(brand_name)
            except Exception as _e:
                logger.info(f"[쿠팡 brandId] '{brand_name}' 매핑 실패: {_e}")

        # 2026-08-01 필수 구매옵션 의무화 — notice_meta 응답에서 추출 (없으면 [])
        required_attr_types: list[str] = []
        if notice_meta is not None:
            try:
                from backend.domain.samba.proxy.notice_utils import (
                    extract_required_attribute_types,
                )

                required_attr_types = extract_required_attribute_types(notice_meta)
            except Exception as _e:
                logger.info(f"[쿠팡 필수 attribute] 추출 실패: {_e}")

        # AS 전화번호 주입은 base._apply_market_settings 에서 처리됨
        data = CoupangClient.transform_product(
            product,
            category_id,
            return_center_code=return_center_code,
            outbound_shipping_place_code=outbound_code,
            notice_meta=notice_meta,
            brand_id=brand_id,
            required_attribute_types=required_attr_types,
        )
        data["vendorId"] = vendor_id
        data["vendorUserId"] = vendor_user_id or vendor_id

        # 반품지 실제 주소 정보 덮어쓰기 (캐시된 값 사용)
        if return_zipcode:
            data["returnZipCode"] = return_zipcode
        if return_address:
            data["returnAddress"] = return_address
        if return_address_detail:
            data["returnAddressDetail"] = return_address_detail
        if return_phone:
            data["companyContactNumber"] = return_phone

        # 계정 popup 설정의 fee 3종 반영 (이슈 #262, 2026-05-27)
        # transform_product 의 하드코딩(deliveryChargeOnReturn=2500, returnCharge=2500,
        # remoteAreaDeliverable="N") 덮어쓰기. 다른 마켓(smartstore/lotteon/elevenst)과
        # 동일 패턴.
        try:
            _return_fee = int(extras.get("returnFee") or 0)
        except (TypeError, ValueError):
            _return_fee = 0
        try:
            _jeju_fee = int(extras.get("jejuFee") or 0)
        except (TypeError, ValueError):
            _jeju_fee = 0
        if _return_fee > 0:
            data["returnCharge"] = _return_fee
            data["deliveryChargeOnReturn"] = _return_fee
        if _jeju_fee > 0:
            # remoteAreaDeliverable=Y 는 출고지 도서산간 가능 택배사 + 상품
            # deliveryCompanyCode 정합성 필요 — popup 에서 명시한 셀러 의도 존중
            data["remoteAreaDeliverable"] = "Y"

        # 기존 상품번호가 있으면 수정, 없으면 신규등록
        if existing_no:
            await client.update_product(existing_no, data)
            return {
                "success": True,
                "product_no": existing_no,
                "message": "쿠팡 수정 성공",
                "data": {"sellerProductId": existing_no},
            }
        else:
            result = await client.register_product(data)

            # 쿠팡 응답에서 sellerProductId 추출 (data 필드에 숫자로 반환)
            seller_product_id = ""
            if isinstance(result, dict):
                inner = result.get("data", {})
                if isinstance(inner, dict):
                    seller_product_id = str(inner.get("data", ""))
                elif inner:
                    seller_product_id = str(inner)

            # 응답에 sellerProductId 가 없거나 숫자가 아니면 실패로 처리
            # — register API 가 200 OK 주더라도 실제 등록 안된 케이스 방어
            if not seller_product_id or not seller_product_id.isdigit():
                return {
                    "success": False,
                    "message": f"쿠팡 등록 실패: sellerProductId 미수신 (응답: {str(result)[:300]})",
                    "data": result,
                }

            # NOTE: approve_product 호출하지 않음 — 호출 시 contributorType 이
            # None → API_SELLER 로 변경되어 Wing UI 노출 트랙에서 이탈하는
            # 부작용 확인(2026-05-11). 쿠팡은 register 직후 자동 승인 처리
            # 흐름이 있으니 우리가 별도 호출 안 하는 게 정확.

            # 쿠팡 vp/products URL 은 {productId}?vendorItemId={vendorItemId} 형식.
            # register 응답에는 sellerProductId 만 오므로 GET 으로 즉시 보강 시도.
            # 임시저장중이면 productId/vendorItemId 가 null 일 수 있음 → 빈값 저장.
            coupang_product_id = ""
            coupang_vendor_item_id = ""
            try:
                gr = await client.get_product(seller_product_id)
                inner = gr.get("data", gr) if isinstance(gr, dict) else {}
                if isinstance(inner, dict):
                    _pid = inner.get("productId")
                    if _pid:
                        coupang_product_id = str(_pid)
                    _items = inner.get("items") or []
                    if _items and isinstance(_items[0], dict):
                        _vid = _items[0].get("vendorItemId")
                        if _vid:
                            coupang_vendor_item_id = str(_vid)
            except Exception as e:
                logger.warning(
                    f"[쿠팡] 등록 후 productId/vendorItemId 조회 실패(추후 동기화): "
                    f"spid={seller_product_id} — {e}"
                )

            return {
                "success": True,
                "product_no": seller_product_id,
                "coupang_product_id": coupang_product_id,
                "coupang_vendor_item_id": coupang_vendor_item_id,
                "message": "쿠팡 등록 성공",
                "data": {"sellerProductId": seller_product_id},
            }

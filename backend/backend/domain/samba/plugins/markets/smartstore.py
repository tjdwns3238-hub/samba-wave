"""스마트스토어 마켓 플러그인.

기존 dispatcher._handle_smartstore 로직을 플러그인 구조로 추출.
인증 로드는 base._load_auth 가 처리하므로 execute 에서는 creds dict 사용.
"""

from __future__ import annotations

import re
from typing import Any

from backend.domain.samba.plugins.market_base import MarketPlugin
from backend.utils import add_lazy_loading
from backend.utils.logger import logger

# 전송 속도 최적화: API 결과 캐시 (같은 브랜드/카테고리/제조사 반복 호출 방지)
_brand_cache: dict[str, Any] = {}  # brand_name → (id, name) or None
_mfr_cache: dict[str, Any] = {}  # mfr_name → id or None
_cat_attrs_cache: dict[str, Any] = {}  # category_id → attrs
_cert_cache: dict[str, Any] = {}  # category_id → cert_infos


class SmartStorePlugin(MarketPlugin):
    market_type = "smartstore"
    policy_key = "스마트스토어"
    required_fields = ["name", "sale_price"]

    def transform(self, product: dict, category_id: str, **kwargs) -> dict:
        from backend.domain.samba.proxy.smartstore import SmartStoreClient

        return SmartStoreClient.transform_product(product, category_id)

    async def execute(
        self,
        session,
        product: dict,
        creds: dict,
        category_id: str,
        account,
        existing_no: str,
    ) -> dict[str, Any]:
        """스마트스토어 상품 등록/수정 — 전체 로직."""
        from backend.domain.samba.proxy.smartstore import SmartStoreClient

        if isinstance(existing_no, dict):
            existing_no = str(
                existing_no.get("originProductNo")
                or existing_no.get("smartstoreChannelProductNo")
                or existing_no.get("groupProductNo")
                or ""
            )
        elif existing_no:
            existing_no = str(existing_no)
        else:
            existing_no = ""

        client_id = creds.get("clientId", "")
        client_secret = creds.get("clientSecret", "")

        if not client_id or not client_secret:
            return {
                "success": False,
                "message": "스마트스토어 Client ID/Secret이 없습니다. 설정에서 해당 계정을 수정 후 저장해주세요.",
            }

        client = SmartStoreClient(client_id, client_secret)

        # 이미지 업로드 스킵 여부 (기존 상품 수정 시)
        skip_image = product.get("_skip_image_upload", False) and bool(existing_no)

        naver_images = []
        detail_html = product.get("detail_html", "")
        # detail_html이 비어있으면 detail_images에서 HTML 자동 생성
        if not detail_html:
            detail_imgs = product.get("detail_images") or []
            if detail_imgs:
                detail_html = "\n".join(
                    f'<div style="text-align:center;"><img src="{img}" style="max-width:860px;width:100%;" /></div>'
                    for img in detail_imgs
                )
                product["detail_html"] = detail_html
                logger.info(
                    f"[스마트스토어] detail_html 비어있어 detail_images {len(detail_imgs)}장으로 자동 생성"
                )
        # 프로토콜 없는 이미지 URL 보정 (src="//... → src="https://...)
        if detail_html:
            detail_html = re.sub(r'(src=["\'])\/\/', r"\1https://", detail_html)
            # lazy loading 속성 자동 삽입
            detail_html = add_lazy_loading(detail_html)

        import asyncio as _aio
        import httpx as _httpx

        # 동시 4장 업로드 + 공유 httpx 클라이언트 (커넥션 풀 재사용)
        _upload_sem = _aio.Semaphore(4)
        _dl_client = _httpx.AsyncClient(timeout=30, follow_redirects=True)
        _ul_client = _httpx.AsyncClient(timeout=30)

        async def _upload_safe(url: str) -> str | None:
            # 프로토콜 없는 URL 보정 (//image.msscdn.net/... → https://image.msscdn.net/...)
            if url.startswith("//"):
                url = "https:" + url
            async with _upload_sem:
                try:
                    return await client.upload_image_from_url(
                        url, _dl_client=_dl_client, _ul_client=_ul_client
                    )
                except Exception as e:
                    logger.warning(f"[스마트스토어] 이미지 업로드 실패: {e}")
                    return None

        # 이미지 업로드 함수 (404 → 신규등록 시 재사용)
        async def _upload_images() -> tuple[list[str], str]:
            imgs_raw = product.get("images") or []
            detail_src_urls: list[str] = []
            dhtml = product.get("detail_html", "")
            if dhtml:
                img_pat = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
                all_srcs = img_pat.findall(dhtml)
                detail_src_urls = list(
                    dict.fromkeys(
                        u
                        for u in all_srcs
                        if "naver.net" not in u and "pstatic.net" not in u
                    )
                )
            all_urls = list(imgs_raw[:5]) + detail_src_urls
            all_res = await _aio.gather(*[_upload_safe(url) for url in all_urls])
            tc = min(len(imgs_raw), 5)
            uploaded = [r for r in all_res[:tc] if r]
            for orig, naver_url in zip(detail_src_urls, all_res[tc:]):
                if naver_url:
                    dhtml = dhtml.replace(orig, naver_url)
            return uploaded, dhtml

        # product_copy 준비 (계정/정책 설정 주입)
        product_copy = dict(product)

        if account:
            extras = account.additional_fields or {}
            if extras.get("asPhone"):
                product_copy["_as_phone"] = extras["asPhone"]
            if extras.get("asMessage"):
                product_copy["_as_message"] = extras["asMessage"]
            if extras.get("returnSafeguard") in (True, "true", "True"):
                product_copy["_return_safeguard"] = True
            naver_shopping = extras.get("naverShopping", "true")
            product_copy["_naver_shopping"] = naver_shopping in (True, "true", "True")
            if extras.get("returnFee"):
                product_copy["_return_fee"] = int(extras["returnFee"])
            if extras.get("exchangeFee"):
                product_copy["_exchange_fee"] = int(extras["exchangeFee"])
            if extras.get("jejuFee"):
                product_copy["_jeju_fee"] = int(extras["jejuFee"])
            if extras.get("stockQuantity"):
                product_copy["_stock_quantity"] = int(extras["stockQuantity"])
                logger.info(
                    f"[스마트스토어] 계정 재고수량 설정: {extras['stockQuantity']}"
                )
            if extras.get("multiPurchaseDiscount") in (True, "true"):
                product_copy["_multi_purchase"] = True
                if extras.get("multiPurchaseQty"):
                    product_copy["_multi_purchase_qty"] = int(
                        extras["multiPurchaseQty"]
                    )
                if extras.get("multiPurchaseRate"):
                    product_copy["_multi_purchase_rate"] = int(
                        extras["multiPurchaseRate"]
                    )
            product_copy["_purchase_point"] = extras.get("purchasePointEnabled") in (
                True,
                "true",
            )
            if extras.get("purchasePointRate"):
                product_copy["_purchase_point_rate"] = int(extras["purchasePointRate"])
            product_copy["_review_point"] = extras.get("reviewPointEnabled") in (
                True,
                "true",
            )
            if extras.get("reviewTextPoint"):
                product_copy["_review_text_point"] = int(extras["reviewTextPoint"])
            if extras.get("reviewPhotoPoint"):
                product_copy["_review_photo_point"] = int(extras["reviewPhotoPoint"])
            if extras.get("reviewMonthTextPoint"):
                product_copy["_review_month_text_point"] = int(
                    extras["reviewMonthTextPoint"]
                )
            if extras.get("reviewMonthPhotoPoint"):
                product_copy["_review_month_photo_point"] = int(
                    extras["reviewMonthPhotoPoint"]
                )
            if extras.get("reviewPhotoUrl"):
                product_copy["_review_photo_url"] = extras["reviewPhotoUrl"]
            if extras.get("discountRate"):
                product_copy["_discount_rate"] = int(extras["discountRate"])

        # 재고제한: 정책에서 읽기
        policy_id = product.get("applied_policy_id")
        if policy_id:
            from backend.domain.samba.policy.repository import SambaPolicyRepository

            policy_repo = SambaPolicyRepository(session)
            _policy = await policy_repo.get_async(policy_id)
            if _policy:
                pr = _policy.pricing or {}
                mp = (_policy.market_policies or {}).get("스마트스토어", {})
                shipping = int(mp.get("shippingCost") or pr.get("shippingCost") or 0)
                if shipping > 0:
                    product_copy["_delivery_fee_type"] = "PAID"
                    product_copy["_delivery_base_fee"] = shipping
                if mp.get("maxStock"):
                    product_copy["_max_stock"] = mp["maxStock"]

        # 가격/재고만 업데이트 시 이미지+카탈로그 조회 모두 스킵
        if skip_image and existing_no:
            logger.info(
                "[스마트스토어] 가격/재고 모드 → 이미지/카탈로그/브랜드/속성 조회 스킵"
            )
        else:
            # ── 이미지 업로드 + 카탈로그/브랜드/속성 조회 동시 실행 ──
            style_code = product_copy.get("style_code", "")
            if not style_code:
                code_match = re.search(
                    r"[A-Z]{2,}[\dA-Z]{4,}", product_copy.get("name", "")
                )
                if code_match:
                    style_code = code_match.group()

            # 브랜드명 정제 — "나이키 키즈" → "나이키", "아디다스 골프" → "아디다스"
            _brand_suffixes = r"\s*(키즈|kids|kid|주니어|junior|jr|아동|유아|베이비|baby|우먼|women|맨즈|men|골프|golf|스포츠|sports|아웃도어|outdoor)\s*$"
            brand_name = product_copy.get("brand", "")
            if brand_name:
                brand_name = (
                    re.sub(_brand_suffixes, "", brand_name, flags=re.IGNORECASE).strip()
                    or brand_name
                )
            mfr_name = product_copy.get("manufacturer", "") or brand_name
            if mfr_name:
                mfr_name = (
                    re.sub(_brand_suffixes, "", mfr_name, flags=re.IGNORECASE).strip()
                    or mfr_name
                )

            async def _search_catalog():
                if style_code:
                    return await client.search_catalog(
                        style_code, category_id=str(category_id)
                    )
                return None

            async def _search_brand():
                if not brand_name:
                    return None
                if brand_name in _brand_cache:
                    return _brand_cache[brand_name]
                result = await client.search_brand(brand_name)
                _brand_cache[brand_name] = result
                return result

            async def _search_mfr():
                if not mfr_name:
                    return None
                if mfr_name in _mfr_cache:
                    return _mfr_cache[mfr_name]
                result = await client.search_manufacturer(mfr_name)
                _mfr_cache[mfr_name] = result
                return result

            async def _get_cat_attrs():
                cid = str(category_id)
                if cid in _cat_attrs_cache:
                    return _cat_attrs_cache[cid]
                result = await client.get_category_attributes(category_id)
                _cat_attrs_cache[cid] = result
                return result

            async def _get_cert_infos():
                cid = str(category_id)
                if cid in _cert_cache:
                    return _cert_cache[cid]
                result = await client.get_category_certification_infos(category_id)
                _cert_cache[cid] = result
                return result

            # 이미지 URL 수집
            images_raw = product.get("images") or []
            detail_img_urls: list[str] = []
            if detail_html:
                img_pattern = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
                all_src_urls = img_pattern.findall(detail_html)
                detail_img_urls = list(
                    dict.fromkeys(
                        url
                        for url in all_src_urls
                        if "naver.net" not in url and "pstatic.net" not in url
                    )
                )[:20]  # OOM 방지: 상세이미지 최대 20장 제한 (50→20 축소)

            all_img_urls = list(images_raw[:5]) + detail_img_urls

            async def _upload_all_images():
                """1장씩 순차 업로드 — OOM 방지 (피크 메모리 최소화)."""
                results: list[str | None] = []
                import httpx as _img_httpx
                from backend.core.config import settings as _img_settings

                _dl = _img_httpx.AsyncClient(
                    timeout=_img_settings.http_timeout_default,
                    follow_redirects=True,
                )
                _ul = _img_httpx.AsyncClient(
                    timeout=_img_settings.http_timeout_default,
                )
                try:
                    for url in all_img_urls:
                        try:
                            naver_url = await client.upload_image_from_url(
                                url, _dl_client=_dl, _ul_client=_ul
                            )
                            results.append(naver_url)
                        except Exception as e:
                            logger.warning(f"[스마트스토어] 이미지 업로드 실패: {e}")
                            results.append(None)
                finally:
                    await _dl.aclose()
                    await _ul.aclose()
                return results

            # 이미지 업로드 + 5개 API 조회 동시 실행
            (
                img_results,
                catalog,
                brand_id,
                mfr_id,
                cat_attrs,
                cert_infos,
            ) = await _aio.gather(
                _upload_all_images(),
                _search_catalog(),
                _search_brand(),
                _search_mfr(),
                _get_cat_attrs(),
                _get_cert_infos(),
            )

            # 이미지 결과 반영
            thumb_count = min(len(images_raw), 5)
            naver_images = [r for r in img_results[:thumb_count] if r]
            if naver_images:
                product_copy["images"] = naver_images
            if detail_img_urls:
                detail_map = img_results[thumb_count:]
                replaced = 0
                removed = 0
                # OOM 방지: 한 번에 치환 맵 구성 후 일괄 replace
                _replace_map: dict[str, str] = {}
                _remove_patterns: list[str] = []
                for orig, naver_url in zip(detail_img_urls, detail_map):
                    if naver_url:
                        _replace_map[orig] = naver_url
                        replaced += 1
                    else:
                        _remove_patterns.append(re.escape(orig))
                        removed += 1
                # 성공 이미지 일괄 치환
                if _replace_map:
                    _rep_pat = re.compile(
                        "|".join(re.escape(k) for k in _replace_map), re.I
                    )
                    detail_html = _rep_pat.sub(
                        lambda m: _replace_map[m.group(0)], detail_html
                    )
                # 실패 이미지 img 태그 일괄 제거
                if _remove_patterns:
                    _rm_pat = re.compile(
                        r'<img[^>]*src=["\'](?:'
                        + "|".join(_remove_patterns)
                        + r')["\'][^>]*/?\s*>',
                        re.I,
                    )
                    detail_html = _rm_pat.sub("", detail_html)
                product_copy["detail_html"] = detail_html
                logger.info(
                    f"[스마트스토어] 이미지 업로드 완료 — 대표 {len(naver_images)}장, 상세 {replaced}장, 제거 {removed}장"
                )
            elif naver_images:
                logger.info(
                    f"[스마트스토어] 이미지 업로드 완료 — 대표 {len(naver_images)}장"
                )

            # 카탈로그/브랜드 결과 반영
            if catalog:
                catalog_cat = str(catalog.get("categoryId", ""))
                if catalog_cat == str(category_id):
                    product_copy["_catalog_model_id"] = catalog["modelId"]
                else:
                    logger.info(
                        f"[스마트스토어] 카탈로그 카테고리 불일치: 카탈로그={catalog_cat}, 상품={category_id} → modelId 스킵"
                    )
                product_copy["_brand_id"] = catalog["brandId"]
                # 카탈로그의 brandName도 함께 전달 — brandId와 brandName 일치 보장
                if catalog.get("brandName"):
                    product_copy["brand"] = catalog["brandName"]
                product_copy["_manufacturer_id"] = catalog["manufacturerId"]
                if catalog.get("manufacturerName"):
                    product_copy["manufacturer"] = catalog["manufacturerName"]
            if not product_copy.get("_brand_id") and brand_id:
                # brand_id는 (id, name) 튜플 — 네이버 정확한 이름으로 덮어쓰기
                if isinstance(brand_id, tuple):
                    product_copy["_brand_id"] = brand_id[0]
                    product_copy["brand"] = brand_id[1]
                else:
                    product_copy["_brand_id"] = brand_id
            if not product_copy.get("_manufacturer_id") and mfr_id:
                product_copy["_manufacturer_id"] = mfr_id
            if cat_attrs:
                product_copy["_category_attributes"] = cat_attrs
            if cert_infos:
                product_copy["_certification_infos"] = cert_infos
                logger.info(
                    f"[스마트스토어] 카테고리 인증정보 {len(cert_infos)}개 → transform에 주입"
                )

        # DB에서 스마트스토어 금지 태그 불러와 사전 필터링 (공백 제거 후 비교)
        try:
            banned_row = await self._get_setting(session, "smartstore_banned_tags")
            if banned_row and isinstance(banned_row, list):
                banned_set = {w.lower().replace(" ", "") for w in banned_row}
                raw_tags = product_copy.get("tags") or []
                product_copy["tags"] = [
                    t
                    for t in raw_tags
                    if t.startswith("__")
                    or t.lower().replace(" ", "") not in banned_set
                ]
        except Exception:
            pass

        # 옵션삭제어 로드
        try:
            from backend.domain.samba.forbidden.repository import (
                SambaForbiddenWordRepository as _FWRepo,
            )

            _fw_repo = _FWRepo(session)
            _opt_del_words = await _fw_repo.list_active("option_deletion")
            if _opt_del_words:
                product_copy["_option_deletion_words"] = [
                    w.word for w in _opt_del_words
                ]
        except Exception:
            pass

        # 디버그: 전송 직전 detail_html 로그
        _dh = product_copy.get("detail_html", "")
        logger.info(
            f"[스마트스토어] detailContent 전송 직전 — 길이:{len(_dh)}, 미리보기:{_dh[:300]}"
        )

        data = SmartStoreClient.transform_product(product_copy, category_id)

        # PUT은 전체 데이터가 필요 → 기존 상품 GET 후 변경 필드만 덮어쓰기
        if skip_image and existing_no:
            new_price = data.get("originProduct", {}).get("salePrice")
            new_stock = data.get("originProduct", {}).get("stockQuantity")
            new_opt = (
                data.get("originProduct", {})
                .get("detailAttribute", {})
                .get("optionInfo")
            )
            new_benefit = data.get("originProduct", {}).get("customerBenefit")
            new_seller_code = str(product_copy.get("id") or "")
            try:
                existing_data = await client.get_product(existing_no)
                origin = existing_data.get("originProduct", {})
                # statusType 보강 — GET 응답 origin에 statusType이 누락된 경우,
                # channelProducts[].statusType 또는 transform 기본값("SALE")으로 채움.
                # pop으로 channelProducts 제거 전에 먼저 추출해야 함.
                if not origin.get("statusType"):
                    ch_list = existing_data.get("channelProducts") or []
                    ch_status = None
                    if ch_list and isinstance(ch_list, list):
                        ch_status = (ch_list[0] or {}).get("statusType")
                    origin["statusType"] = (
                        ch_status
                        or data.get("originProduct", {}).get("statusType")
                        or "SALE"
                    )
                # 읽기전용 필드 제거
                for k in [
                    "productNo",
                    "channelProducts",
                    "regDate",
                    "modifiedDate",
                    "saleStartDate",
                    "saleEndDate",
                ]:
                    origin.pop(k, None)
                # 가격/재고만 덮어쓰기
                if new_price is not None:
                    origin["salePrice"] = new_price
                if new_stock is not None:
                    origin["stockQuantity"] = new_stock
                if new_opt:
                    origin.setdefault("detailAttribute", {})["optionInfo"] = new_opt
                if new_benefit:
                    origin["customerBenefit"] = new_benefit
                # 판매자 상품코드 누락 시 삼바 내부 ID(cp_ULID)로 보정
                if new_seller_code:
                    existing_code = origin.get("sellerCodeInfo", {}).get(
                        "sellerManagementCode", ""
                    )
                    if not existing_code:
                        origin.setdefault("sellerCodeInfo", {})[
                            "sellerManagementCode"
                        ] = new_seller_code
                data = {"originProduct": origin}
                if "smartstoreChannelProduct" in existing_data:
                    data["smartstoreChannelProduct"] = existing_data[
                        "smartstoreChannelProduct"
                    ]
                logger.info(
                    f"[스마트스토어] 가격/재고 업데이트 모드 (PUT): salePrice={new_price}, stockQuantity={new_stock}"
                )
            except Exception as get_e:
                logger.error(f"[스마트스토어] 기존 상품 조회 실패: {get_e}")
                return {"success": False, "message": f"기존 상품 조회 실패: {get_e}"}
        else:
            # 전체 전송 시 디버그 로깅
            da = data.get("originProduct", {}).get("detailAttribute", {})
            logger.info(
                f"[스마트스토어] 전송 detailAttribute — modelName={da.get('modelName')}, brandId={da.get('brandId')}, brandName={da.get('brandName')}, mfr={da.get('manufacturerName')}, attrs={len(da.get('productAttributes', []))}개, cancelGuide={da.get('cancelGuide')}"
            )

        # KC/어린이제품 인증 에러 감지 헬퍼
        def _is_kc_cert_error(err: Exception) -> bool:
            err_str = str(err)
            return "인증" in err_str and (
                "어린이" in err_str
                or "KC" in err_str
                or "productCertificationInfos" in err_str
                or "certificationInfos" in err_str
            )

        # 인증대상 아님 선언 payload 생성 (신규등록 재시도용 — 전체 재생성)
        def _build_cert_exclusion_payload(
            source_product: dict[str, Any], cat_id: str
        ) -> dict[str, Any]:
            retry_product = dict(source_product)
            retry_product.pop("_certification_infos", None)
            retry_data = SmartStoreClient.transform_product(retry_product, cat_id)
            detail_attr = retry_data["originProduct"].setdefault("detailAttribute", {})
            exclude = detail_attr.setdefault("certificationTargetExcludeContent", {})
            exclude["childCertifiedProductExclusionYn"] = True
            exclude.setdefault("kcCertifiedProductExclusionYn", "TRUE")
            detail_attr.pop("productCertificationInfos", None)
            return retry_data

        # 기존 payload에 인증 면제 플래그만 덧붙임 (PUT 수정 재시도용 — 구조 보존)
        def _apply_cert_exclusion_inplace(payload: dict[str, Any]) -> dict[str, Any]:
            detail_attr = payload.setdefault("originProduct", {}).setdefault(
                "detailAttribute", {}
            )
            exclude = detail_attr.setdefault("certificationTargetExcludeContent", {})
            exclude["childCertifiedProductExclusionYn"] = True
            exclude.setdefault("kcCertifiedProductExclusionYn", "TRUE")
            detail_attr.pop("productCertificationInfos", None)
            return payload

        # 상품속성(productAttributes) 입력 오류 감지 (속성실제값/단위 누락)
        def _is_product_attr_error(err: Exception) -> bool:
            s = str(err)
            return (
                "속성실제값" in s
                or "attributeRealValue" in s
                or "상품속성 단위" in s
                or "단위코드" in s
            )

        # payload에서 productAttributes 제거 (오매칭된 속성 통째로 제거)
        def _strip_product_attributes_inplace(
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            op = payload.get("originProduct")
            if isinstance(op, dict):
                da = op.get("detailAttribute")
                if isinstance(da, dict):
                    da.pop("productAttributes", None)
            return payload

        # 반품안심케어 비허용 카테고리 에러 감지
        # 메시지 예: "반품안심케어를 설정할 수 없는 카테고리입니다."
        def _is_return_safeguard_error(err: Exception) -> bool:
            s = str(err)
            return "반품안심" in s or "freeReturnInsuranceYn" in s

        # payload에서 freeReturnInsuranceYn 제거 (단일/그룹 구조 모두 대응)
        def _strip_return_safeguard_inplace(
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            # 단일 상품: originProduct.deliveryInfo.claimDeliveryInfo
            op = payload.get("originProduct")
            if isinstance(op, dict):
                cd = (op.get("deliveryInfo") or {}).get("claimDeliveryInfo")
                if isinstance(cd, dict):
                    cd.pop("freeReturnInsuranceYn", None)
            # 그룹 상품: groupProduct.specificProducts[].deliveryInfo.claimDeliveryInfo
            gp = payload.get("groupProduct")
            if isinstance(gp, dict):
                for sp in gp.get("specificProducts") or []:
                    cd = (sp.get("deliveryInfo") or {}).get("claimDeliveryInfo")
                    if isinstance(cd, dict):
                        cd.pop("freeReturnInsuranceYn", None)
            return payload

        # register_product 호출을 공통 fallback으로 감싸는 헬퍼
        # leafCategoryId 에러 → 기본 카테고리 fallback
        # KC/어린이제품 인증 에러 → 인증대상 아님 선언 fallback
        # (leafCategoryId fallback 이후 KC 에러도 중첩 처리)
        async def _register_product_with_fallback(
            payload: dict[str, Any],
            source_product: dict[str, Any],
            cat_id: str,
        ) -> dict[str, Any]:
            try:
                return await client.register_product(payload)
            except Exception as reg_e:
                reg_err_str = str(reg_e)
                if "leafCategoryId" in reg_err_str:
                    _default_cat = "50000803"
                    logger.warning(
                        f"[스마트스토어] leafCategoryId 에러 → 기본 카테고리({_default_cat})로 재시도: {reg_e}"
                    )
                    payload["originProduct"]["leafCategoryId"] = _default_cat
                    try:
                        return await client.register_product(payload)
                    except Exception as retry_e:
                        if _is_kc_cert_error(retry_e):
                            logger.warning(
                                f"[스마트스토어] (카테고리 fallback 후) 인증대상 에러 감지 → childCertifiedProductExclusionYn=True 재시도: {retry_e}"
                            )
                            retry_payload = _build_cert_exclusion_payload(
                                source_product, _default_cat
                            )
                            return await client.register_product(retry_payload)
                        raise

                if _is_kc_cert_error(reg_e):
                    logger.warning(
                        f"[스마트스토어] 인증대상 에러 감지 → childCertifiedProductExclusionYn=True 재시도: {reg_e}"
                    )
                    retry_payload = _build_cert_exclusion_payload(
                        source_product, cat_id
                    )
                    return await client.register_product(retry_payload)

                if _is_product_attr_error(reg_e):
                    logger.warning(
                        f"[스마트스토어] 상품속성 오류 감지 → productAttributes 제거 후 재시도: {reg_e}"
                    )
                    _strip_product_attributes_inplace(payload)
                    return await client.register_product(payload)

                if _is_return_safeguard_error(reg_e):
                    logger.warning(
                        f"[스마트스토어] 반품안심케어 비허용 카테고리 감지 → freeReturnInsuranceYn 제거 후 재시도: {reg_e}"
                    )
                    _strip_return_safeguard_inplace(payload)
                    return await client.register_product(payload)

                raise

        # 기존 상품번호가 있으면 수정, 없으면 신규등록
        async def _try_send(d: dict[str, Any]) -> dict[str, Any]:
            if existing_no:
                try:
                    logger.info(
                        f"[스마트스토어] PUT 시도: origin={existing_no}, client_id={client_id[:8]}..."
                    )
                    r = await client.update_product(existing_no, d)
                    return {
                        "success": True,
                        "message": "스마트스토어 수정 성공",
                        "data": r,
                    }
                except Exception as e:
                    # 어린이제품/KC 인증정보 요구 → 인증대상 아님으로 선언 후 수정 재시도
                    if _is_kc_cert_error(e):
                        logger.warning(
                            f"[스마트스토어] PUT 인증대상 에러 감지 → childCertifiedProductExclusionYn=True 재시도: {e}"
                        )
                        try:
                            _apply_cert_exclusion_inplace(d)
                            r = await client.update_product(existing_no, d)
                            return {
                                "success": True,
                                "message": "스마트스토어 수정 성공 (인증면제 fallback)",
                                "data": r,
                            }
                        except Exception as _cert_retry_e:
                            logger.error(
                                f"[스마트스토어] PUT 인증면제 재시도도 실패: {_cert_retry_e}"
                            )
                            raise

                    if _is_product_attr_error(e):
                        logger.warning(
                            f"[스마트스토어] PUT 상품속성 오류 감지 → productAttributes 제거 후 재시도: {e}"
                        )
                        try:
                            _strip_product_attributes_inplace(d)
                            r = await client.update_product(existing_no, d)
                            return {
                                "success": True,
                                "message": "스마트스토어 수정 성공 (productAttributes 제거 fallback)",
                                "data": r,
                            }
                        except Exception as _pa_retry_e:
                            logger.error(
                                f"[스마트스토어] PUT productAttributes 제거 재시도도 실패: {_pa_retry_e}"
                            )
                            raise

                    if _is_return_safeguard_error(e):
                        logger.warning(
                            f"[스마트스토어] PUT 반품안심케어 비허용 카테고리 감지 → freeReturnInsuranceYn 제거 후 재시도: {e}"
                        )
                        try:
                            _strip_return_safeguard_inplace(d)
                            r = await client.update_product(existing_no, d)
                            return {
                                "success": True,
                                "message": "스마트스토어 수정 성공 (반품안심 제거 fallback)",
                                "data": r,
                            }
                        except Exception as _rs_retry_e:
                            logger.error(
                                f"[스마트스토어] PUT 반품안심 제거 재시도도 실패: {_rs_retry_e}"
                            )
                            raise

                    if "404" in str(e):
                        # PATCH 404 → GET으로 상품 존재 여부 재확인
                        product_exists = False
                        try:
                            await client.get_product(existing_no)
                            product_exists = True
                        except Exception:
                            pass

                        if product_exists:
                            # 상품이 있는데 PATCH 404 → 등록 직후 검수 중. 재시도
                            import asyncio as _retry_aio

                            for _wait in [10, 20]:
                                logger.warning(
                                    f"[스마트스토어] 상품 {existing_no} PUT 404이지만 GET 성공 → {_wait}초 후 재시도"
                                )
                                await _retry_aio.sleep(_wait)
                                try:
                                    r = await client.update_product(existing_no, d)
                                    return {
                                        "success": True,
                                        "message": "스마트스토어 수정 성공 (재시도)",
                                        "data": r,
                                    }
                                except Exception:
                                    continue
                            # 재시도 모두 실패 — 상품번호 보존, 신규등록 차단
                            logger.warning(
                                "[스마트스토어] PUT 재시도 2회 실패 — 검수 완료 후 다시 시도 필요"
                            )
                            return {
                                "success": False,
                                "error_type": "patch_delayed",
                                "message": f"상품 #{existing_no} 수정 실패 (검수 중). 잠시 후 다시 시도해주세요.",
                            }

                        # GET도 404 → 상품이 진짜 없음
                        price_only = product.get("_price_stock_only", False)
                        if skip_image or price_only:
                            logger.warning(
                                f"[스마트스토어] 수정 모드 상품 {existing_no} 404 → 신규등록 차단"
                            )
                            return {
                                "success": False,
                                "error_type": "product_not_found",
                                "message": f"상품 #{existing_no}이 스마트스토어에 없습니다. 강제삭제 후 재등록해주세요.",
                                "_clear_product_no": True,
                            }
                        # 전체 전송 모드 + GET 404 → 상품이 삭제됨 → 신규등록 전환
                        logger.warning(
                            f"[스마트스토어] 상품 {existing_no} GET/PATCH 모두 404 → 신규등록 전환"
                        )
                        try:
                            full_copy = dict(product_copy)
                            full_data = SmartStoreClient.transform_product(
                                full_copy, category_id
                            )
                            r = await _register_product_with_fallback(
                                full_data, full_copy, category_id
                            )
                            return {
                                "success": True,
                                "message": "스마트스토어 등록 성공 (404→신규전환)",
                                "data": r,
                                "_clear_product_no": True,
                            }
                        except Exception as reg_e:
                            logger.error(f"[스마트스토어] 404 → 신규등록 실패: {reg_e}")
                            return {
                                "success": False,
                                "error_type": "product_not_found",
                                "message": f"상품 #{existing_no} 수정/등록 실패: {reg_e}",
                                "_clear_product_no": True,
                            }

                    # 대분류(leafCategoryId) 변경 거부 → 기존 상품 마켓삭제 후 신규등록 전환
                    # SmartStore는 등록 후 대분류 변경 불가 정책 → 기존 originProductNo 폐기 필수
                    err_str = str(e)
                    if "대분류" in err_str and "변경" in err_str:
                        price_only = product.get("_price_stock_only", False)
                        if skip_image or price_only:
                            logger.warning(
                                f"[스마트스토어] 대분류 변경 불가(origin={existing_no}) — "
                                f"가격/재고 전용 모드라 신규등록 차단"
                            )
                            return {
                                "success": False,
                                "error_type": "category_locked",
                                "message": (
                                    f"상품 #{existing_no} 대분류 변경 불가. "
                                    "전체 전송 모드로 다시 시도해주세요."
                                ),
                            }

                        logger.warning(
                            f"[스마트스토어] 대분류 변경 거부 감지 → "
                            f"마켓삭제({existing_no}) 후 신규등록 전환: {err_str[:120]}"
                        )
                        # 1) 기존 origin 마켓삭제 (404는 이미 삭제로 간주하여 진행)
                        try:
                            await client.delete_product(str(existing_no))
                            logger.info(
                                f"[스마트스토어] 대분류 fallback — 기존 상품 마켓삭제 성공: {existing_no}"
                            )
                        except Exception as del_e:
                            del_err_str = str(del_e)
                            if "HTTP 404" in del_err_str:
                                logger.info(
                                    f"[스마트스토어] 대분류 fallback — {existing_no} 이미 삭제됨(404) → 신규등록 진행"
                                )
                            else:
                                logger.error(
                                    f"[스마트스토어] 대분류 fallback — 마켓삭제 실패({existing_no}): {del_e}"
                                )
                                return {
                                    "success": False,
                                    "error_type": "category_locked",
                                    "message": (
                                        f"상품 #{existing_no} 대분류 변경 거부 — "
                                        f"마켓삭제 실패: {del_err_str[:120]}"
                                    ),
                                }

                        # 2) 신규등록 전환 (sellerManagementCode 잔여 매칭 시 update 무한루프 방지를 위해
                        #    find_by_management_code 우회: 직접 register_product 호출)
                        try:
                            full_copy = dict(product_copy)
                            full_data = SmartStoreClient.transform_product(
                                full_copy, category_id
                            )
                            r = await _register_product_with_fallback(
                                full_data, full_copy, category_id
                            )
                            return {
                                "success": True,
                                "message": "스마트스토어 등록 성공 (대분류변경→마켓삭제 후 신규전환)",
                                "data": r,
                                "_clear_product_no": True,
                            }
                        except Exception as reg_e:
                            logger.error(
                                f"[스마트스토어] 대분류 fallback — 신규등록 실패: {reg_e}"
                            )
                            return {
                                "success": False,
                                "error_type": "category_locked",
                                "message": (
                                    f"상품 #{existing_no} 대분류 변경 거부 — "
                                    f"삭제 후 재등록 실패: {str(reg_e)[:120]}"
                                ),
                                "_clear_product_no": True,
                            }

                    raise
            else:
                # 중복 등록 방지: 이미 Naver에 등록된 상품인지 sellerManagementCode로 사전 확인
                _mgmt_code = (
                    d.get("originProduct", {})
                    .get("sellerCodeInfo", {})
                    .get("sellerManagementCode", "")
                )
                if _mgmt_code:
                    _existing = await client.find_by_management_code(_mgmt_code)
                    if _existing:
                        _origin_no = str(
                            _existing.get("originProductNo")
                            or _existing.get("originProduct", {}).get("id", "")
                            or ""
                        )
                        logger.warning(
                            f"[스마트스토어] 중복등록 방지 — sellerManagementCode={_mgmt_code} 이미 존재: originProductNo={_origin_no}"
                        )
                        return {
                            "success": True,
                            "message": "스마트스토어 기등록 상품 재연결 (중복등록 차단)",
                            "data": _existing,
                            "_already_registered": True,
                            "_origin_no": _origin_no,
                        }
                r = await _register_product_with_fallback(d, product_copy, category_id)
                return {"success": True, "message": "스마트스토어 등록 성공", "data": r}

        # 태그사전 미등록 태그 사전 필터링 (누적 DB 기반)
        try:
            unregistered_row = await self._get_setting(
                session, "smartstore_unregistered_tags"
            )
            if unregistered_row and isinstance(unregistered_row, list):
                unreg_set = {w.lower() for w in unregistered_row}
                seo = (
                    data.get("originProduct", {})
                    .get("detailAttribute", {})
                    .get("seoInfo", {})
                )
                old_tags = seo.get("sellerTags", [])
                if old_tags:
                    filtered = [
                        t
                        for t in old_tags
                        if t.get("text", "").lower() not in unreg_set
                    ]
                    removed = len(old_tags) - len(filtered)
                    if removed:
                        logger.info(
                            f"[스마트스토어] 미등록 태그 사전 필터링: {removed}개 제거"
                        )
                        if filtered:
                            data["originProduct"]["detailAttribute"]["seoInfo"][
                                "sellerTags"
                            ] = filtered
                        else:
                            data["originProduct"]["detailAttribute"].pop(
                                "seoInfo", None
                            )
        except Exception:
            pass

        try:
            result = await _try_send(data)
            return result
        except Exception as e:
            err_msg = str(e)
            # 등록불가 단어 에러 → 해당 태그 제거 후 재시도 + DB 저장
            if "등록불가" in err_msg:
                # 에러에서 금지 단어 추출: "등록불가인 단어(A,B,C)가"
                m = re.search(r"등록불가인 단어\(([^)]+)\)", err_msg)
                if m:
                    banned = {w.strip().lower() for w in m.group(1).split(",")}
                    # 1. DB에 금지 단어 누적 저장 (PK = key)
                    try:
                        from backend.domain.samba.forbidden.repository import (
                            SambaSettingsRepository,
                        )

                        repo = SambaSettingsRepository(session)
                        row = await repo.find_by_async(key="smartstore_banned_tags")
                        existing_banned: list[str] = []
                        if row and isinstance(row.value, list):
                            existing_banned = row.value
                        merged = list(set(existing_banned + [w for w in banned]))
                        if row:
                            row.value = merged
                            session.add(row)
                        else:
                            from backend.domain.samba.forbidden.model import (
                                SambaSettings,
                            )

                            session.add(
                                SambaSettings(
                                    key="smartstore_banned_tags", value=merged
                                )
                            )
                        await session.commit()
                        logger.info(
                            f"[스마트스토어] 금지 태그 DB 저장: +{banned} (총 {len(merged)}개)"
                        )
                    except Exception as save_err:
                        logger.warning(
                            f"[스마트스토어] 금지 태그 저장 실패: {save_err}"
                        )

                    # 2. 상품 + 동일 그룹 전체 tags에서 금지 태그 일괄 제거
                    try:
                        from backend.domain.samba.collector.repository import (
                            SambaCollectedProductRepository,
                        )
                        from backend.domain.samba.collector.model import (
                            SambaCollectedProduct as _CP,
                        )
                        from sqlmodel import select as _sel

                        product_id = product.get("id", "")
                        if product_id:
                            prod_repo = SambaCollectedProductRepository(session)
                            prod_row = await prod_repo.get_async(product_id)
                            if prod_row:
                                # 같은 그룹 상품 전체 조회
                                group_products = [prod_row]
                                if prod_row.search_filter_id:
                                    grp_result = await session.exec(
                                        _sel(_CP).where(
                                            _CP.search_filter_id
                                            == prod_row.search_filter_id
                                        )
                                    )
                                    group_products = grp_result.all()
                                cleaned_count = 0
                                for gp in group_products:
                                    if gp.tags:
                                        cleaned = [
                                            t
                                            for t in gp.tags
                                            if t.startswith("__")
                                            or t.lower() not in banned
                                        ]
                                        if len(cleaned) != len(gp.tags):
                                            await prod_repo.update_async(
                                                gp.id, tags=cleaned
                                            )
                                            cleaned_count += 1
                                await session.commit()
                                logger.info(
                                    f"[스마트스토어] 그룹 {cleaned_count}개 상품에서 금지 태그 {banned} 제거"
                                )
                    except Exception as tag_err:
                        logger.warning(f"[스마트스토어] 그룹 태그 제거 실패: {tag_err}")

                    # 3. sellerTags에서 해당 단어 제거 후 재시도 (공백 제거 후 비교)
                    banned_nospace = {w.replace(" ", "") for w in banned}
                    seo = (
                        data.get("originProduct", {})
                        .get("detailAttribute", {})
                        .get("seoInfo", {})
                    )
                    old_tags = seo.get("sellerTags", [])
                    if old_tags:
                        new_tags = [
                            t
                            for t in old_tags
                            if t.get("text", "").lower().replace(" ", "")
                            not in banned_nospace
                        ]
                        if new_tags:
                            data["originProduct"]["detailAttribute"]["seoInfo"][
                                "sellerTags"
                            ] = new_tags
                        else:
                            data["originProduct"]["detailAttribute"].pop(
                                "seoInfo", None
                            )
                        logger.info(
                            f"[스마트스토어] 금지 태그 {banned} 제거 후 재시도 ({len(old_tags)}→{len(new_tags)}개)"
                        )
                        return await _try_send(data)
            raise
        finally:
            # 공유 httpx 클라이언트 정리
            await _dl_client.aclose()
            await _ul_client.aclose()

    async def delete(self, session, product_no: str, account) -> dict[str, Any]:
        """스마트스토어 상품 판매중지."""
        creds = await self._load_auth(session, account)
        if not creds:
            return {"success": False, "message": "인증정보 없음"}
        from backend.domain.samba.proxy.smartstore import SmartStoreClient

        client = SmartStoreClient(
            creds.get("clientId", ""), creds.get("clientSecret", "")
        )
        data = {"originProduct": {"statusType": "SUSPENSION"}}
        await client.update_product(product_no, data)
        return {"success": True, "message": "판매중지 완료"}

    @staticmethod
    async def _get_setting(session, key: str) -> Any:
        """samba_settings 테이블에서 설정값 조회 후 즉시 커밋 — idle in transaction 방지."""
        from backend.domain.samba.forbidden.repository import SambaSettingsRepository

        repo = SambaSettingsRepository(session)
        row = await repo.find_by_async(key=key)
        val = row.value if row else None
        try:
            await session.commit()
        except Exception:
            pass
        return val

"""SSG(신세계몰) 마켓 플러그인.

기존 dispatcher._handle_ssg 로직을 플러그인 구조로 추출.
SSGClient를 통해 인프라 조회 + 상품 변환 + 등록/수정 처리.
"""

from __future__ import annotations

from typing import Any

from backend.domain.samba.plugins.market_base import MarketPlugin
from backend.utils.logger import logger


class SSGPlugin(MarketPlugin):
    market_type = "ssg"
    policy_key = "신세계몰(전시)"
    required_fields = ["name", "sale_price"]

    def _validate_category(self, category_id: str) -> str:
        # SSG 전시카테고리 ID(dispCtgId)는 숫자이지만, base의 isdigit 검사가
        # 비정상 매핑값을 잘못 차단할 수 있으므로 롯데ON과 동일하게 pass-through.
        return category_id or ""

    def transform(self, product: dict, category_id: str, **kwargs) -> dict:
        """SSGClient.transform_product 위임."""
        from backend.domain.samba.proxy.ssg import SSGClient

        api_key = kwargs.get("api_key", "")
        store_id = kwargs.get("store_id", "6004")
        infra = kwargs.get("infra", {})
        client = SSGClient(api_key, site_no=store_id)
        return client.transform_product(product, category_id, infra=infra)

    async def execute(
        self,
        session,
        product: dict,
        creds: dict,
        category_id: str,
        account,
        existing_no: str,
    ) -> dict[str, Any]:
        """SSG 상품 등록/수정 — 전체 로직."""
        from backend.domain.samba.proxy.ssg import SSGClient

        api_key = creds.get("apiKey", "")
        if not api_key:
            return {"success": False, "message": "SSG 인증키가 비어있습니다."}

        # 전시카테고리 미매핑 시 등록 불가 — 명확한 에러 반환
        if not category_id:
            product_name = product.get("name", "")
            return {
                "success": False,
                "message": f"신세계몰 전시카테고리가 매핑되지 않았습니다. 카테고리 매핑 후 다시 시도하세요. (상품: {product_name[:30]})",
            }

        store_id = creds.get("storeId", "6004")
        client = SSGClient(api_key, site_no=store_id)

        # 배송비/주소 인프라 데이터 조회
        # 경량 모드: 설정에 인프라 ID가 모두 있으면 fetch_infra() API 호출 스킵
        skip_image = product.get("_skip_image_upload", False) and bool(existing_no)
        if skip_image:
            _infra_keys = (
                "whoutAddrId",
                "snbkAddrId",
                "whoutShppcstId",
                "retShppcstId",
            )
            _all_present = all(creds.get(k) for k in _infra_keys)
            if _all_present:
                # 배송 ID는 설정값 사용, 원산지 코드는 fetch_infra 캐시에서 취득
                _full_infra = await client.fetch_infra()
                infra: dict[str, Any] = {"origin_code_map": _full_infra.get("origin_code_map", {})}
                logger.info("[SSG] 경량 가격/재고 모드 → 배송 ID 설정값 사용, 원산지 코드만 별도 조회")
            else:
                infra = await client.fetch_infra()
                logger.info(
                    f"[SSG] 경량 모드이나 인프라 ID 부족 → fetch_infra 호출: {list(infra.keys())}"
                )
        else:
            infra = await client.fetch_infra()
            logger.info(f"[SSG] 인프라 조회 완료: {list(infra.keys())}")

        # 설정 페이지 값을 infra에 주입 (설정값이 있으면 fetch_infra 조회값 우선 덮어쓰기)
        setting_shppcst_ids = {
            "whoutShppcstId": creds.get("whoutShppcstId", ""),
            "retShppcstId": creds.get("retShppcstId", ""),
            "addShppcstIdJeju": creds.get("addShppcstIdJeju", ""),
            "addShppcstIdIsland": creds.get("addShppcstIdIsland", ""),
            "whoutAddrId": creds.get("whoutAddrId", ""),
            "snbkAddrId": creds.get("snbkAddrId", ""),
        }
        for k, v in setting_shppcst_ids.items():
            if v:
                infra[k] = v

        # 필수 배송 인프라 ID 검증 — 없으면 SSG API 필수값 오류 발생
        missing_infra = []
        if not infra.get("whoutAddrId"):
            missing_infra.append("출고주소ID(whoutAddrId)")
        if not infra.get("snbkAddrId"):
            missing_infra.append("반품주소ID(snbkAddrId)")
        if not infra.get("whoutShppcstId"):
            missing_infra.append("출고배송비ID(whoutShppcstId)")
        if not infra.get("retShppcstId"):
            missing_infra.append("반품배송비ID(retShppcstId)")
        if missing_infra:
            return {
                "success": False,
                "message": f"SSG 배송 설정 누락: {', '.join(missing_infra)}. 설정 페이지에서 배송정보를 확인하세요.",
            }

        # 정책 브랜드 매핑 추출
        brand_mappings: list[dict] = creds.get("ssgBrandMappings") or []

        # 설정에서 마진율/배송소요일/구매수량 제한 추출 (정책값 우선, 설정값 폴백)
        margin_rate = int(creds.get("marginRate") or 0)
        shpp_rqrm_dcnt = int(creds.get("shppRqrmDcnt") or 3)
        day_max_qty = int(product.get("_day_max_qty") or creds.get("dayMaxQty") or 5)
        once_min_qty = int(product.get("_once_min_qty") or creds.get("onceMinQty") or 1)
        once_max_qty = int(product.get("_once_max_qty") or creds.get("onceMaxQty") or 5)

        # 고시정보 정책값 주입
        notice_overrides: dict[str, str] = {}
        _notice_field_map = {
            "ssgNoticeGroup": "_ssg_notice_group",
            "ssgNoticeMaterial": "_ssg_notice_material",
            "ssgNoticeColor": "_ssg_notice_color",
            "ssgNoticeSize": "_ssg_notice_size",
            "ssgNoticeImport": "_ssg_import_yn",
            "ssgNoticeImporter": "_ssg_notice_importer",
            "ssgNoticeCaution": "_ssg_notice_caution",
            "ssgNoticeAsContact": "_ssg_notice_as_contact",
            "ssgNoticeManufacturer": "_ssg_notice_manufacturer",
            "ssgNoticeOrigin": "_ssg_notice_origin",
        }
        for cred_key, prod_key in _notice_field_map.items():
            val = creds.get(cred_key)
            if val:
                notice_overrides[prod_key] = val
        if notice_overrides:
            product = {**product, **notice_overrides}

        # A/S 정보 주입 — 고시정보 통합 연락처 우선, 없으면 설정값
        if not creds.get("ssgNoticeAsContact"):
            as_phone = creds.get("asPhone") or ""
            as_message = creds.get("asMessage") or ""
            if as_phone:
                product = {**product, "_as_phone": as_phone}
            if as_message:
                product = {**product, "_as_message": as_message}

        # category_id = 전시카테고리 ID, _std_category_id = 표준카테고리 ID
        std_category_id = product.get("_std_category_id", "") or ""
        if not std_category_id:
            logger.warning(
                "[SSG] _std_category_id 없음 — stdCtgId 빈값으로 전송. 등록 실패 가능."
            )
        logger.info(
            f"[SSG] 전시카테고리={category_id!r}, 표준카테고리={std_category_id!r}"
        )

        # SSG.COM(6005) 메인매장 전시카테고리 자동 조회
        # 1단계: 신세계몰(6004) 카테고리 ID → 카테고리명 조회
        # 2단계: 해당 leaf명으로 SSG.COM(6005) 전시카테고리 검색
        main_category_id = ""
        if category_id:
            try:
                # 1단계: 신세계몰 카테고리 이름 조회
                name_resp = await client._call_api(
                    "GET",
                    "/common/0.1/displayCategory.ssg",
                    params={"dispCtgId": category_id},
                )

                def _extract_cats(resp: dict) -> list:
                    raw = resp.get("result", {}).get("displayCategorys", [])
                    if isinstance(raw, dict):
                        inner = raw.get("category", [])
                        return [inner] if isinstance(inner, dict) else (inner or [])
                    if isinstance(raw, list):
                        result = []
                        for item in raw:
                            if not isinstance(item, dict):
                                continue
                            cat = item.get("category")
                            if isinstance(cat, dict):
                                result.append(cat)
                            elif isinstance(cat, list):
                                result.extend(cat)
                            else:
                                result.append(item)
                        return result
                    return []

                name_cats = _extract_cats(name_resp)
                leaf_name = ""
                if name_cats:
                    # category_id와 일치하는 항목 우선, 없으면 마지막 항목(가장 세분류)
                    target = next(
                        (
                            c
                            for c in name_cats
                            if str(c.get("dispCtgId", "")) == category_id
                        ),
                        name_cats[-1],
                    )
                    path = target.get("dispCtgPathNm", "") or target.get(
                        "dispCtgNm", ""
                    )
                    leaf_name = (
                        path.split(">")[-1].strip() if ">" in path else path.strip()
                    )
                    logger.info(f"[SSG] 신세계몰 카테고리 이름: {leaf_name!r}")

                # 2단계: SSG.COM(6005)에서 leaf_name 검색 (전체 → 첫 단어 순으로 시도)
                if leaf_name:
                    keywords = [leaf_name]
                    short = leaf_name.split("/")[0].strip()
                    if short and short != leaf_name:
                        keywords.append(short)
                    for kw in keywords:
                        com_resp = await client.search_display_categories(
                            kw, site_no="6005"
                        )
                        com_cats = _extract_cats(com_resp)
                        if com_cats:
                            main_category_id = str(com_cats[0].get("dispCtgId", ""))
                            logger.info(
                                f"[SSG] SSG.COM(6005) 전시카테고리 자동 조회 성공 ({kw!r}): {main_category_id}"
                            )
                            break
                    else:
                        logger.warning(
                            f"[SSG] SSG.COM(6005) '{leaf_name}' 검색 결과 없음"
                        )
            except Exception as _e:
                logger.warning(f"[SSG] SSG.COM(6005) 전시카테고리 조회 실패: {_e}")

        # 무신사 등 referer 차단 CDN URL을 R2로 미러링
        # — SSG는 등록 URL을 자체 서버가 fetch하므로 핫링크 차단 시 워터마크 이미지로 캐싱됨
        try:
            from backend.domain.samba.image.service import ImageTransformService

            _img_svc = ImageTransformService(session)
            _images = product.get("images") or []
            _detail_images = product.get("detail_images") or []
            _detail_html = product.get("detail_html") or ""
            if _images or _detail_images or _detail_html:
                product = dict(product)
                if _images:
                    product["images"], _ = await _img_svc.mirror_external_to_r2(_images)
                if _detail_images:
                    product["detail_images"], _ = await _img_svc.mirror_external_to_r2(
                        _detail_images
                    )
                if _detail_html:
                    product["detail_html"] = await _img_svc.mirror_urls_in_html(
                        _detail_html
                    )
                if not product.get("images"):
                    return {
                        "success": False,
                        "message": "SSG 등록 실패: 이미지 미러링 후 사용 가능한 이미지가 없습니다.",
                    }
        except Exception as e:
            logger.warning(f"[SSG] 이미지 미러링 단계 오류 — 원본 URL 유지: {e}")

        try:
            data = client.transform_product(
                product,
                category_id,
                std_category_id=std_category_id,
                main_category_id=main_category_id,
                infra=infra,
                margin_rate=margin_rate,
                shpp_rqrm_dcnt=shpp_rqrm_dcnt,
                day_max_qty=day_max_qty,
                once_min_qty=once_min_qty,
                once_max_qty=once_max_qty,
                brand_mappings=brand_mappings,
            )
        except Exception as e:
            import traceback as _tb

            logger.error(f"[SSG] transform_product 예외: {e}\n{_tb.format_exc()}")
            return {
                "success": False,
                "message": f"SSG 상품 데이터 변환 실패: {str(e)[:200]}",
            }

        # 기존 상품번호가 있으면 수정, 없으면 신규등록
        if existing_no:
            data["itemId"] = existing_no
            result = await client.update_product(data)
            # 영구판매중지 상품은 수정 불가 → 상품번호 초기화 후 신규등록
            result_data_chk = result.get("data", {})
            if isinstance(result_data_chk, dict):
                res_chk = result_data_chk.get("result", {})
                if isinstance(res_chk, dict):
                    msg_chk = (
                        res_chk.get("resultDesc", "")
                        or res_chk.get("resultMessage", "")
                        or ""
                    )
                    if "영구판매중지" in msg_chk:
                        logger.info(
                            f"[SSG] 영구판매중지 상품 감지 → 상품번호 초기화 후 신규등록: itemId={existing_no}"
                        )
                        data.pop("itemId", None)
                        result = await client.register_product(data)
                        result["_clear_product_no"] = (
                            True  # 호출자에서 DB 상품번호 초기화
                        )
        else:
            result = await client.register_product(data)

        # SSG API 응답 검증
        result_data = result.get("data", {})
        if isinstance(result_data, dict):
            res = result_data.get("result", {})
            if isinstance(res, dict):
                code = res.get("resultCode", "")
                if code and str(code) != "00" and str(code) != "SUCCESS":
                    # resultDesc에 상세 에러 포함 — resultMessage("FAIL")보다 우선
                    msg = (
                        res.get("resultDesc", "")
                        or res.get("resultMessage", "")
                        or f"resultCode={code}"
                    )
                    return {
                        "success": False,
                        "message": f"SSG 등록 실패: {msg}",
                        "data": result_data,
                    }

        action = "수정" if existing_no else "등록"
        return {"success": True, "message": f"SSG {action} 성공", "data": result}

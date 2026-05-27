"""옥션 마켓 플러그인 — ESM Plus API 기반 (siteType=1).

ESM Trading API v2를 통해 옥션 상품 등록/수정/삭제.
지마켓 플러그인(gmarket.py)과 동일한 ESMPlusClient를 공유하며
siteType=1, siteKey=Iac, ssiPrefix=A 로 동작.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from backend.domain.samba.plugins.market_base import MarketPlugin
from backend.utils import add_lazy_loading
from backend.utils.logger import logger

# ESM Plus 호스팅 인증정보는 서버 환경변수(ESMPLUS_HOSTING_ID/ESMPLUS_SECRET_KEY)에서 로드


def _to_grouped_options(options: list[dict], group_names: list[str]) -> list[dict]:
    """무신사 flat 옵션 리스트를 register_esm_options용 그룹 구조로 변환.

    이미 grouped 형태(values 키 있음)면 그대로 반환.
    단일 그룹(0~1개): 모든 옵션값을 해당 그룹 하나로 묶음.
      - 그룹명 없으면 "사이즈" 기본값.
    다중 그룹(2개+): "블랙/S" 형태 조합을 축별로 파싱 + 조합재고 맵 포함.
    """
    if not options:
        return []
    if (
        options[0].get("values") is not None
        or options[0].get("option_values") is not None
    ):
        return options
    if len(group_names) <= 1:
        group_name = group_names[0] if group_names else "사이즈"
        return [{"name": group_name, "values": options}]
    return _split_multi_group_options(options, group_names)


def _split_multi_group_options(
    options: list[dict], group_names: list[str]
) -> list[dict]:
    """'색상/사이즈' flat 조합 → 축별 그룹 + _combo_stock_map 변환.

    _combo_stock_map은 _build_combination이 per-combination 재고로 활용한다.
    separator "/"는 maxsplit=n-1 로 처리해 값 내부 "/" 포함 케이스(A/XS 등)를 보존.
    """
    n = len(group_names)
    axis_order: list[list[str]] = [[] for _ in range(n)]
    axis_seen: list[set] = [set() for _ in range(n)]
    combo_stock_map: dict[str, dict] = {}

    for opt in options:
        parts = [p.strip() for p in opt.get("name", "").split("/", n - 1)]
        if len(parts) != n:
            continue
        for i, val in enumerate(parts):
            if val not in axis_seen[i]:
                axis_seen[i].add(val)
                axis_order[i].append(val)
        stock = int(opt.get("stock") or 0)
        combo_stock_map["/".join(parts)] = {
            "stock": stock,
            "isSoldOut": bool(opt.get("isSoldOut") or stock <= 0),
        }

    result: list[dict] = []
    for i, gname in enumerate(group_names):
        grp: dict = {
            "name": gname,
            "values": [{"name": v, "stock": 99} for v in axis_order[i]],
        }
        if i == 0:
            grp["_combo_stock_map"] = combo_stock_map
        result.append(grp)
    return result


class AuctionPlugin(MarketPlugin):
    """옥션 마켓 플러그인 — ESM Plus siteType=1."""

    market_type = "auction"
    policy_key = "옥션"
    required_fields = ["name", "sale_price"]

    def transform(self, product: dict, category_id: str, **kwargs) -> dict:
        from backend.domain.samba.proxy.esmplus import ESMPlusClient

        return ESMPlusClient.transform_product(product, category_id, site="auction")

    async def execute(
        self,
        session,
        product: dict,
        creds: dict,
        category_id: str,
        account,
        existing_no: str,
    ) -> dict[str, Any]:
        """옥션 상품 등록/수정."""
        from backend.domain.samba.proxy.esmplus import ESMPlusClient

        # 판매자 ID
        seller_id = (
            creds.get("apiKey", "")
            or creds.get("sellerId", "")
            or (getattr(account, "seller_id", "") or "")
        )
        if not seller_id:
            return {
                "success": False,
                "message": "옥션 판매자 ID(apiKey)가 없습니다. 계정 설정에서 입력해주세요.",
            }

        # 호스팅 인증정보 — 서버 환경변수에서 로드 (셀링툴업체 고정값)
        from backend.domain.samba.proxy.esmplus import resolve_esm_credentials

        hosting_id, secret_key = await resolve_esm_credentials(session, account)
        if not hosting_id or not secret_key:
            return {
                "success": False,
                "message": "ESM 인증정보 없음 — account.additional_fields / samba_settings.esm_credentials / ESMPLUS_HOSTING_ID env 중 하나 필요.",
            }

        client = ESMPlusClient(hosting_id, secret_key, seller_id, site="auction")

        # 상품 데이터 복사 + 계정 설정 주입
        product_copy = dict(product)
        product_copy = await self._inject_account_settings(
            session, product_copy, account
        )

        # 무신사 등 referer/hotlink 차단 CDN(msscdn 등) → R2 미러링 (11번가 동일 패턴)
        # ESM 서버가 등록 이미지 URL을 직접 fetch하므로, 차단 도메인을
        # api.samba-wave.co.kr 미러 URL로 치환해야 워터마크/차단 회피가 완성됨.
        try:
            from backend.domain.samba.image.service import ImageTransformService

            _img_svc = ImageTransformService(session)
            _imgs = product_copy.get("images") or []
            _detail_imgs = product_copy.get("detail_images") or []
            _dhtml = product_copy.get("detail_html") or ""
            if _imgs:
                # min_dim=600 — ESM 최소 600x600 미달 이미지 LANCZOS 업스케일 + R2 미러
                # (msscdn 등 차단 도메인도 strict 모드로 다운로드/재호스팅됨)
                product_copy["images"], _ = await _img_svc.mirror_oversized_to_r2(
                    _imgs, min_dim=600
                )
            if _detail_imgs:
                (
                    product_copy["detail_images"],
                    _,
                ) = await _img_svc.mirror_with_persistence(
                    product_copy.get("id"), _detail_imgs
                )
            if _dhtml:
                product_copy["detail_html"] = await _img_svc.mirror_urls_in_html(_dhtml)
            # 미러링 후에도 핫링크 차단 URL이 남으면 등록 차단(깨진 이미지 방지)
            _still_blocked = [
                u
                for u in (product_copy.get("images") or [])
                if ImageTransformService.is_hotlink_blocked_url(u)
            ]
            if _still_blocked:
                return {
                    "success": False,
                    "message": (
                        f"옥션 등록 취소: R2 미러링 실패로 핫링크 차단 URL "
                        f"{len(_still_blocked)}개 잔존. R2 설정 확인 후 재시도."
                    ),
                }
        except Exception as e:
            try:
                from backend.domain.samba.image.service import (
                    ImageTransformService as _ITS,
                )

                _blk = [
                    u
                    for u in (product_copy.get("images") or [])
                    if _ITS.is_hotlink_blocked_url(u)
                ]
            except Exception:
                _blk = []
            if _blk:
                logger.error(f"[옥션] R2 미러링 오류 + 차단 URL 존재 — 등록 차단: {e}")
                return {
                    "success": False,
                    "message": f"옥션 등록 취소: R2 미러링 오류. {e}",
                }
            logger.warning(f"[옥션] 이미지 미러링 오류 — 차단 URL 없어 원본 유지: {e}")

        # 상세 HTML 프로토콜 보정 + lazy loading 삽입
        detail_html = product_copy.get("detail_html", "")
        if detail_html:
            detail_html = re.sub(r'(src=["\'])\/\/', r"\1https://", detail_html)
            product_copy["detail_html"] = add_lazy_loading(detail_html)

        # transform
        data = ESMPlusClient.transform_product(
            product_copy, category_id, site="auction"
        )

        # 이미지 모델 (등록 후 별도 API 호출용)
        pending_images = data.pop("_pending_images", None)

        # 가격/재고만 업데이트 모드
        skip_image = product.get("_skip_image_upload", False) and bool(existing_no)
        price_only = product.get("_price_stock_only", False)

        if skip_image or price_only:
            return await self._update_price_stock(
                client, existing_no, product_copy, data
            )

        # 등록/수정 분기
        if existing_no:
            return await self._update_product(client, existing_no, data, pending_images)
        else:
            samba_options = _to_grouped_options(
                product.get("options") or [],
                product.get("option_group_names") or [],
            )
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

        # ESM 옥션 중복등록 silent fail 감지(이슈#278)
        # — 같은 상품 재등록 시 resultCode=0(성공) + goodsNo=0 + siteGoodsNo=null 반환.
        # 검증 없이 통과시키면 market_product_nos가 "0"으로 덮어써져 PUT /goods/0 404 무한.
        _gno_str = str(goods_no or "").strip()
        if _gno_str in ("", "0", "0.0") or not site_goods_no:
            logger.error(
                f"[옥션] 등록 응답 무효(중복등록 의심): goodsNo={goods_no!r}, "
                f"siteGoodsNo={site_goods_no!r} → 기존 유효 ID 보존 위해 실패 처리"
            )
            return {
                "success": False,
                "message": "옥션 중복등록 의심(goodsNo=0 또는 siteGoodsNo 누락) — 기존 등록 확인 필요",
                "_already_registered": True,
            }

        # ESM이 신규 등록 상품을 색인하는 데 시간이 필요하므로 재시도 로직 적용
        if pending_images and goods_no:
            for _img_wait in (10, 15, 20):
                try:
                    await asyncio.sleep(_img_wait)
                    await client.update_images(goods_no, {"imageModel": pending_images})
                    logger.info(f"[옥션] 추가 이미지 설정 완료: goodsNo={goods_no}")
                    break
                except Exception as img_e:
                    logger.warning(
                        f"[옥션] 추가 이미지 설정 실패 ({_img_wait}s 후 재시도): {img_e}"
                    )
            else:
                logger.warning(f"[옥션] 추가 이미지 설정 최종 실패: goodsNo={goods_no}")

        # 추천옵션 등록 — samba options 있고 cat_code 있을 때만.
        # register_esm_options 가 이미지 propagation polling (0/30/60s, 최대 90s) 자체 처리.
        opt_msg = ""
        if samba_options and goods_no and cat_code:
            try:
                import asyncio as _asyncio
                from backend.domain.samba.proxy.esmplus import register_esm_options

                opt_result = await _asyncio.wait_for(
                    register_esm_options(
                        client, goods_no, cat_code, samba_options, site="auction"
                    ),
                    timeout=120,
                )
                if opt_result.get("success"):
                    opt_msg = f" [옵션 {opt_result.get('matched')}/{opt_result.get('requested')}개 등록]"
                    logger.info(
                        f"[옥션] 옵션 등록 완료: goodsNo={goods_no} matched={opt_result.get('matched')}/{opt_result.get('requested')}"
                    )
                else:
                    opt_msg = f" [옵션 등록 실패: {opt_result.get('message', '')[:80]}]"
                    logger.warning(
                        f"[옥션] 옵션 등록 부분 실패: {opt_result.get('message')}"
                    )
            except (_asyncio.TimeoutError, Exception) as opt_e:
                opt_msg = f" [옵션 등록 오류: {str(opt_e)[:60]}]"
                logger.warning(
                    f"[옥션] 옵션 등록 실패 (상품 등록은 성공 처리): {opt_e}"
                )
        elif samba_options and not cat_code:
            opt_msg = " [옵션 등록 스킵: cat_code 없음]"

        return {
            "success": True,
            "message": f"옥션 등록 성공{opt_msg}",
            "data": {
                "sellerProductId": str(site_goods_no or goods_no),
                "siteGoodsNo": site_goods_no,
                "goodsNo": goods_no,
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
        # PUT 엔드포인트는 isSell을 루트 레벨에 요구함 (POST는 itemAddtionalInfo 안)
        _is_sell = data.get("itemAddtionalInfo", {}).get("isSell", {"Iac": 1})
        update_data = {**data, "isSell": _is_sell}
        try:
            await client.update_product(goods_no, update_data)
        except RuntimeError as e:
            err_msg = str(e)
            if "상품이 없습니다" in err_msg or "not exist" in err_msg.lower():
                logger.warning(f"[옥션] 상품 {goods_no} 없음 → 신규등록 전환")
                result = await client.register_product(update_data)
                new_goods_no = result.get("goodsNo", "")
                return {
                    "success": True,
                    "message": "옥션 등록 성공 (기존 상품 없음 → 신규)",
                    "data": {"sellerProductId": str(new_goods_no)},
                    "_clear_product_no": True,
                }
            raise

        if pending_images:
            try:
                await client.update_images(goods_no, {"imageModel": pending_images})
            except Exception as img_e:
                logger.warning(f"[옥션] 이미지 수정 실패: {img_e}")

        return {
            "success": True,
            "message": "옥션 수정 성공",
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

        # ESM Plus 스펙 — 등록과 sell-status 모두 PascalCase(Iac). 실 호출 검증 결과
        # 'isSell' camelCase 는 'IsSell 필드가 필요합니다' 응답 → PascalCase 통일.
        price = data.get("itemAddtionalInfo", {}).get("price", {}).get("Iac", 0)
        stock = data.get("itemAddtionalInfo", {}).get("stock", {}).get("Iac", 0)

        sell_data: dict[str, Any] = {
            "IsSell": {"Iac": True},
            "itemBasicInfo": {
                "price": {"Iac": price},
                "stock": {"Iac": stock},
                "sellingPeriod": {"Iac": 0},
            },
        }

        try:
            await client.update_sell_status(goods_no, sell_data)
            logger.info(
                f"[옥션] 가격/재고 수정 성공: goodsNo={goods_no}, price={price}, stock={stock}"
            )
            return {
                "success": True,
                "message": "옥션 가격/재고 수정 성공",
                "data": {"sellerProductId": goods_no},
            }
        except RuntimeError as e:
            if "상품이 없습니다" in str(e):
                return {
                    "success": False,
                    "error_type": "product_not_found",
                    "message": f"상품 #{goods_no}이 옥션에 없습니다.",
                    "_clear_product_no": True,
                }
            raise

    async def delete(self, session, product_no: str, account) -> dict[str, Any]:
        """옥션 상품 판매중지."""
        from backend.domain.samba.proxy.esmplus import ESMPlusClient

        creds = await self._load_auth(session, account)
        if not creds:
            return {"success": False, "message": "인증정보 없음"}

        seller_id = (
            creds.get("apiKey", "")
            or creds.get("sellerId", "")
            or (getattr(account, "seller_id", "") or "")
        )
        if not seller_id:
            return {"success": False, "message": "옥션 판매자 ID 없음"}

        from backend.domain.samba.proxy.esmplus import resolve_esm_credentials

        hosting_id, secret_key = await resolve_esm_credentials(session, account)
        if not hosting_id or not secret_key:
            return {"success": False, "message": "ESM 인증정보 없음"}
        client = ESMPlusClient(hosting_id, secret_key, seller_id, site="auction")

        # 판매중지 — 실 호출 검증 schema (PascalCase). 'IsSell' 만으로도 ESM 측 검증 통과.
        suspend_data = {"IsSell": {"Iac": False}}
        await client.update_sell_status(product_no, suspend_data)
        logger.info(f"[옥션] 판매중지 완료: goodsNo={product_no}")
        return {"success": True, "message": "옥션 판매중지 완료"}

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

        policy_id = product.get("applied_policy_id")
        if policy_id:
            from backend.db.orm import get_write_session
            from backend.domain.samba.policy.repository import SambaPolicyRepository

            async with get_write_session() as fresh_session:
                policy_repo = SambaPolicyRepository(fresh_session)
                policy = await policy_repo.get_async(policy_id)
            if policy:
                pr = policy.pricing or {}
                mp = (policy.market_policies or {}).get("옥션", {})
                shipping = int(mp.get("shippingCost") or pr.get("shippingCost") or 0)
                if shipping > 0:
                    product["_delivery_fee_type"] = "PAID"
                    product["_delivery_base_fee"] = shipping
                if mp.get("maxStock"):
                    product["_max_stock"] = mp["maxStock"]

        return product

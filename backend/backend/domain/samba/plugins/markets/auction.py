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
        # group_names 가 없어도 "블랙/M" 같은 다축 조합이면(SSG 등 소싱처) 축을 추론해
        # 색상/사이즈로 분리. 분리 안 하면 "블랙/M"이 매칭 실패 →
        # recommendedOptValueNo=0(직접입력) → "대표단품"으로 노출되는 버그가 생긴다.
        inferred = _infer_group_names(options)
        if inferred:
            return _split_multi_group_options(options, inferred)
        group_name = group_names[0] if group_names else "사이즈"
        return [{"name": group_name, "values": options}]
    return _split_multi_group_options(options, group_names)


_SIZE_RE = re.compile(r"^(x{0,3}[sl]|ss|m|free|onesize|\d{1,3}|\d+x[sl])$", re.I)


def _infer_group_names(options: list[dict]) -> list[str] | None:
    """group_names 없이 "블랙/M" 같은 2축 조합 옵션이면 축 이름(색상/사이즈)을 추론.

    모든 옵션이 동일하게 "/"로 2분할되고, 한 축만 사이즈 패턴일 때만 ["색상","사이즈"]
    (또는 순서 반대)로 확정. 애매하면 None(기존 단일 그룹 동작 유지).
    """
    names = [str(o.get("name") or "").strip() for o in options]
    names = [n for n in names if n]
    if not names or any(n.count("/") != 1 for n in names):
        return None
    axis0: list[str] = []
    axis1: list[str] = []
    for n in names:
        a, b = (p.strip() for p in n.split("/", 1))
        axis0.append(a)
        axis1.append(b)

    def _is_size_axis(vals: list[str]) -> bool:
        u = [v for v in vals if v]
        if not u:
            return False
        hit = sum(1 for v in u if _SIZE_RE.match(v.replace(" ", "")))
        return hit >= len(u) * 0.6

    a0, a1 = _is_size_axis(axis0), _is_size_axis(axis1)
    if a1 and not a0:
        return ["색상", "사이즈"]
    if a0 and not a1:
        return ["사이즈", "색상"]
    return None


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
                (
                    product_copy["images"],
                    _,
                    _failed_imgs,
                ) = await _img_svc.mirror_oversized_to_r2(_imgs, min_dim=600)
                if _failed_imgs:
                    product_copy["images"] = [
                        u for u in product_copy["images"] if u not in _failed_imgs
                    ]
                    logger.warning(
                        f"[옥션] 이미지 다운로드 실패 {len(_failed_imgs)}개 → 제외: "
                        + ", ".join(list(_failed_imgs)[:3])
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

        # 반품/교환지 placeNo → addrNo 해석 (ESM returnAndExchange.addrNo, #389).
        # 가격/재고 동기화 모드는 반품지 미변경 → get_places 호출 스킵(오토튠 부하 회피).
        if not (
            product.get("_price_stock_only")
            or (product.get("_skip_image_upload") and existing_no)
        ):
            _rpn = int(product_copy.get("_return_place_no", 0) or 0)
            if _rpn:
                _addr = await client.resolve_return_addr_no(_rpn)
                if _addr:
                    product_copy["_return_addr_no"] = _addr

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
            # 오토튠 가격/재고/판매중지 동기화 — 옵션상품은 sell-status 본품재고가
            # 무시되므로 옵션별 재고/품절은 recommended-options로 처리.
            # 브랜드 조회 전에 조기 반환해 불필요한 search_brands 호출 차단.
            return await self._update_price_stock(
                client, existing_no, product_copy, data, cat_code=category_id
            )

        # 브랜드 코드 매핑 — 신규등록·전체수정 경로만 (오토튠 가격/재고 제외).
        # ESM 실제 브랜드 필드는 catalog.brandNo(정수). 문자열 brand만 보내면
        # ESM이 무시 → 마켓 리스팅 브랜드 빈칸.
        from backend.domain.samba.proxy.esmplus import resolve_esm_brand_no

        _brand = (data.get("itemBasicInfo", {}) or {}).get("brand") or ""
        if _brand:
            _brand_no = await resolve_esm_brand_no(client, _brand)
            if _brand_no:
                data.setdefault("itemBasicInfo", {}).setdefault("catalog", {})[
                    "brandNo"
                ] = _brand_no
                logger.info(
                    f"[옥션] 브랜드 코드 매핑: '{_brand}' → brandNo={_brand_no}"
                )

        # 등록/수정 모두 옵션 필요 — 수정 시 미동봉하면 PUT 전체교체로 옵션 소멸 (#394)
        samba_options = _to_grouped_options(
            product.get("options") or [],
            product.get("option_group_names") or [],
        )
        if existing_no:
            return await self._update_product(
                client,
                existing_no,
                data,
                pending_images,
                samba_options=samba_options,
                cat_code=category_id,
            )
        else:
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
        """신규 상품 등록 + 옵션/이미지 후처리.

        옵션은 등록 POST 본문 itemAddtionalInfo.recommendedOpts 에 인라인 동봉
        (atomic). propagation polling/race 제거 (#368 ①).
        """
        # 옵션 인라인 빌드 — 등록 전에 recommendedOpts payload 동봉
        opt_msg = ""
        opt_built = False
        if samba_options and cat_code:
            try:
                from backend.domain.samba.proxy.esmplus import register_esm_options

                _bo = await register_esm_options(
                    client,
                    "",
                    cat_code,
                    samba_options,
                    site="auction",
                    build_only=True,
                )
                if _bo.get("success") and _bo.get("payload"):
                    data.setdefault("itemAddtionalInfo", {})["recommendedOpts"] = _bo[
                        "payload"
                    ]
                    opt_built = True
                    opt_msg = (
                        f" [옵션 {_bo.get('matched')}/{_bo.get('requested')}개 인라인]"
                    )
                elif _bo.get("multi_variant"):
                    # 미발행 가드 — 멀티변형인데 옵션매핑 실패 시 옵션없는 등록 차단(#361)
                    logger.warning(
                        f"[옥션] 옵션 매핑 실패(멀티변형) → 미발행: {_bo.get('message')}"
                    )
                    return {
                        "success": False,
                        "message": f"옵션 매핑 실패로 미발행(멀티변형): {str(_bo.get('message', ''))[:80]}",
                    }
                else:
                    # 단일변형(선택지 1개) — 옵션없이 등록 허용
                    opt_msg = f" [옵션 매핑실패·단일변형 옵션없이 등록: {str(_bo.get('message', ''))[:60]}]"
            except Exception as opt_e:
                logger.warning(f"[옥션] 옵션 인라인 빌드 오류: {opt_e}")
                opt_msg = f" [옵션 빌드 오류: {str(opt_e)[:60]}]"
        elif samba_options and not cat_code:
            opt_msg = " [옵션 등록 스킵: cat_code 없음]"

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

        # 옵션은 등록 POST 에 인라인 동봉됨 (위 build_only) — 사후 PUT 제거 (#368 ①)
        if opt_built:
            logger.info(f"[옥션] 옵션 인라인 등록 완료: goodsNo={goods_no}{opt_msg}")

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
        samba_options: list[dict] | None = None,
        cat_code: str = "",
    ) -> dict[str, Any]:
        """기존 상품 수정."""
        from backend.domain.samba.proxy.esmplus import resolve_esm_master_goods_no

        # 수정 API는 마스터 goodsNo 필요 — 저장값이 siteGoodsNo면 404. 변환.
        master_no = await resolve_esm_master_goods_no(client, goods_no) or goods_no

        # PUT 엔드포인트는 isSell을 루트 레벨에 요구함 (POST는 itemAddtionalInfo 안)
        _is_sell = data.get("itemAddtionalInfo", {}).get("isSell", {"Iac": 1})
        update_data = {**data, "isSell": _is_sell}

        # 옵션 인라인 동봉 — 미동봉 시 PUT 전체교체로 기존 옵션 소멸 (#394)
        opt_msg = ""
        if samba_options and cat_code:
            try:
                from backend.domain.samba.proxy.esmplus import register_esm_options

                _bo = await register_esm_options(
                    client,
                    "",
                    cat_code,
                    samba_options,
                    site="auction",
                    build_only=True,
                )
                if _bo.get("success") and _bo.get("payload"):
                    update_data.setdefault("itemAddtionalInfo", {})[
                        "recommendedOpts"
                    ] = _bo["payload"]
                    opt_msg = (
                        f" [옵션 {_bo.get('matched')}/{_bo.get('requested')}개 인라인]"
                    )
                elif _bo.get("multi_variant"):
                    logger.warning(
                        f"[옥션] 옵션 매핑 실패(멀티변형) → 수정 차단: {_bo.get('message')}"
                    )
                    return {
                        "success": False,
                        "message": f"옵션 매핑 실패로 수정 차단(멀티변형): {str(_bo.get('message', ''))[:80]}",
                    }
                else:
                    opt_msg = f" [옵션 매핑실패·단일변형 옵션없이 수정: {str(_bo.get('message', ''))[:60]}]"
            except Exception as opt_e:
                logger.warning(f"[옥션] 옵션 인라인 빌드 오류(수정): {opt_e}")
                opt_msg = f" [옵션 빌드 오류: {str(opt_e)[:60]}]"

        try:
            await client.update_product(master_no, update_data)
        except RuntimeError as e:
            err_msg = str(e)
            if "상품이 없습니다" in err_msg or "not exist" in err_msg.lower():
                logger.warning(f"[옥션] 상품 {master_no} 없음 → 신규등록 전환")
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
                await client.update_images(master_no, {"imageModel": pending_images})
            except Exception as img_e:
                logger.warning(f"[옥션] 이미지 수정 실패: {img_e}")

        return {
            "success": True,
            "message": f"옥션 수정 성공{opt_msg}",
            "data": {"sellerProductId": goods_no, "goodsNo": master_no},
        }

    async def _update_price_stock(
        self,
        client: Any,
        goods_no: str,
        product: dict,
        data: dict[str, Any],
        cat_code: str = "",
    ) -> dict[str, Any]:
        """가격/재고/판매중지 수정 (오토튠).

        - 마스터 goodsNo 해석(저장값 siteGoodsNo면 404 → 카탈로그 스캔 변환).
        - 가격 + 판매상태: sell-status.
        - 옵션상품: 옵션별 재고/품절은 recommended-options PUT (sell-status 본품재고 무시됨).
        - 전 옵션 품절: sell-status isSell=false 만 (recommended-options "최소 1개 판매" 에러 회피).
        """
        if not goods_no:
            return {"success": False, "message": "상품번호가 없어 가격/재고 수정 불가"}

        from backend.domain.samba.proxy.esmplus import (
            register_esm_options,
            resolve_esm_master_goods_no,
        )

        master_no = await resolve_esm_master_goods_no(client, goods_no) or goods_no

        # ESM Plus 스펙 — sell-status PascalCase(Iac).
        price = data.get("itemAddtionalInfo", {}).get("price", {}).get("Iac", 0)
        stock = data.get("itemAddtionalInfo", {}).get("stock", {}).get("Iac", 0)

        # 판매가능 여부 — 옵션상품은 옵션별, 본품상품은 본품 재고로 판단
        options = product.get("options") or []
        has_options = bool(options)
        if has_options:
            any_sellable = any(
                not (o.get("isSoldOut") or o.get("is_sold_out"))
                and int(o.get("stock", 0) or 0) > 0
                for o in options
            )
        else:
            any_sellable = int(stock or 0) > 0
        is_sell = bool(any_sellable)

        sell_data: dict[str, Any] = {
            "IsSell": {"Iac": is_sell},
            "itemBasicInfo": {
                "price": {"Iac": price},
                # 옵션상품은 본품재고 무시되나 1~99999 범위 필수
                "stock": {"Iac": max(1, min(int(stock or 1), 99999))},
                "sellingPeriod": {"Iac": 0},
            },
        }

        try:
            await client.update_sell_status(master_no, sell_data)
        except RuntimeError as e:
            if "상품이 없습니다" in str(e):
                return {
                    "success": False,
                    "error_type": "product_not_found",
                    "message": f"상품 #{goods_no}이 옥션에 없습니다.",
                    "_clear_product_no": True,
                }
            raise

        # 옵션별 재고/품절 동기화 — 판매가능 옵션 있을 때만.
        # 전 옵션 품절이면 위 isSell=false로 전체 중지 완료 — recommended-options는
        # "주문선택사항 최소 1개 판매" 에러나므로 호출 안 함.
        opt_msg = ""
        if has_options and any_sellable and cat_code:
            try:
                samba_options = _to_grouped_options(
                    options, product.get("option_group_names") or []
                )
                opt_result = await register_esm_options(
                    client, master_no, cat_code, samba_options, site="auction"
                )
                if opt_result.get("success"):
                    opt_msg = (
                        f" [옵션재고 {opt_result.get('matched')}/"
                        f"{opt_result.get('requested')}]"
                    )
                else:
                    opt_msg = (
                        f" [옵션재고 동기화 실패: {opt_result.get('message', '')[:60]}]"
                    )
                    logger.warning(
                        f"[옵션] 옵션 재고 동기화 실패: {opt_result.get('message')}"
                    )
            except Exception as opt_e:
                opt_msg = f" [옵션재고 오류: {str(opt_e)[:50]}]"
                logger.warning(f"[옥션] 옵션 재고 동기화 오류: {opt_e}")

        logger.info(
            f"[옥션] 가격/재고/판매상태 수정: master={master_no}, price={price}, "
            f"isSell={is_sell}{opt_msg}"
        )
        return {
            "success": True,
            "message": f"옥션 가격/재고 수정 성공{opt_msg}",
            "data": {"sellerProductId": goods_no, "goodsNo": master_no},
        }

    async def delete(self, session, product_no: str, account) -> dict[str, Any]:
        """옥션 상품 판매중지 → 삭제."""
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

        # 판매중지도 마스터 goodsNo 필요 — 저장값이 siteGoodsNo면 404. 변환.
        from backend.domain.samba.proxy.esmplus import resolve_esm_master_goods_no

        master_no = await resolve_esm_master_goods_no(client, product_no) or product_no

        # 판매중지 — 실 호출 검증 schema (PascalCase). 'IsSell' 만으로도 ESM 측 검증 통과.
        # delete_product 는 "판매중지 상태 필수" 전제 → 선행 판매중지로 충족.
        suspend_data = {"IsSell": {"Iac": False}}
        await client.update_sell_status(master_no, suspend_data)
        logger.info(f"[옥션] 판매중지 완료: goodsNo={master_no}")

        # 실삭제 — DELETE /item/v1/goods/{goodsNo}. cooldown 재시도는 delete_product 내부 처리.
        # 삭제 실패해도 판매중지는 완료된 상태이므로 success 유지(카탈로그 노출은 이미 차단).
        try:
            await client.delete_product(master_no)
            logger.info(f"[옥션] 삭제 완료: goodsNo={master_no}")
            return {"success": True, "message": "옥션 판매중지+삭제 완료"}
        except Exception as e:
            logger.warning(
                f"[옥션] 삭제 실패(판매중지는 완료): goodsNo={master_no}, {e}"
            )
            return {"success": True, "message": f"옥션 판매중지 완료(삭제 보류: {e})"}

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
            if extras.get("returnFee"):
                product["_return_fee"] = int(extras["returnFee"])
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

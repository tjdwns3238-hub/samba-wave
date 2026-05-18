"""11번가 마켓 플러그인.

기존 dispatcher._handle_11st 로직을 플러그인 구조로 추출.
인증 로드는 base._load_auth 가 처리하므로 execute 에서는 creds dict 사용.
"""

from __future__ import annotations

from typing import Any

from backend.domain.samba.plugins.market_base import MarketPlugin
from backend.utils.logger import logger


# 11번가 PUT 응답 중 "유령 매핑(이미 삭제됨/존재하지 않음)" 으로 판정할 메시지 패턴.
# - "삭제된 상품은 수정할 수 없습니다" : 등록은 됐지만 11번가가 deleted 처리한 케이스
#   (검수 반려 자동삭제, 셀러 직접 삭제, 정책 위반 차단 등)
# - "존재하지 않는 상품"             : 그런 prdNo 자체가 11번가에 없음 (잘못 저장된 prdNo)
_GHOST_ERROR_PATTERNS = ("삭제된 상품", "존재하지 않는 상품")


def _is_ghost_error(err_msg: str) -> bool:
    """11번가 응답 메시지가 '유령 매핑' 신호인지 판별."""
    return any(p in err_msg for p in _GHOST_ERROR_PATTERNS)


async def _purge_ghost_mapping(
    session, product_id: str, account_id: str, prd_no: str, reason: str
) -> bool:
    """DB에서 해당 product의 11번가 unclehg/계정 매핑을 정리.

    - registered_accounts 배열에서 account_id 제거
    - market_product_nos 에서 account_id / f"{account_id}_origin" 키 제거
    실패해도 호출부에 영향 안 가도록 예외는 삼키고 False 리턴.
    """
    try:
        from sqlalchemy.orm.attributes import flag_modified
        from sqlmodel import select

        from backend.domain.samba.collector.model import SambaCollectedProduct

        if not (product_id and account_id):
            return False
        stmt = select(SambaCollectedProduct).where(
            SambaCollectedProduct.id == product_id
        )
        prod = (await session.execute(stmt)).scalars().first()
        if not prod:
            return False
        changed = False
        nos = dict(prod.market_product_nos or {})
        for k in (account_id, f"{account_id}_origin"):
            if k in nos:
                nos.pop(k, None)
                changed = True
        if changed:
            prod.market_product_nos = nos
            flag_modified(prod, "market_product_nos")
        regs = list(prod.registered_accounts or [])
        if account_id in regs:
            regs = [a for a in regs if a != account_id]
            prod.registered_accounts = regs
            flag_modified(prod, "registered_accounts")
            changed = True
        if changed:
            session.add(prod)
            await session.commit()
            logger.warning(
                f"[11번가][유령정리] product={product_id} account={account_id} prdNo={prd_no} 사유={reason}"
            )
        return changed
    except Exception as e:
        logger.warning(f"[11번가][유령정리] 실패 product={product_id} — {e}")
        return False


class ElevenstPlugin(MarketPlugin):
    market_type = "11st"
    policy_key = "11번가"
    required_fields = ["name", "sale_price"]

    def transform(self, product: dict, category_id: str, **kwargs) -> dict:
        """상품 데이터 → 11번가 XML 포맷 변환.

        주의: 키속성(ProductCtgrAttribute) 메타는 API 호출이 필요하므로
        본 동기 transform()에서는 누락된다. 실제 등록 시에는 execute() 에서
        get_category_attributes()를 호출하여 키속성을 포함한 XML을 생성한다.
        """
        from backend.domain.samba.proxy.elevenst import ElevenstClient

        settings = kwargs.get("settings", {})
        return ElevenstClient.transform_product(product, category_id, settings=settings)

    async def execute(
        self,
        session,
        product: dict,
        creds: dict,
        category_id: str,
        account,
        existing_no: str,
    ) -> dict[str, Any]:
        """11번가 상품 등록/수정 — 전체 로직."""
        from backend.domain.samba.proxy.elevenst import ElevenstClient

        api_key = creds.get("apiKey", "")

        # account 필드에서 보완
        if not api_key and account:
            api_key = getattr(account, "api_key", "") or ""

        if not api_key:
            return {
                "success": False,
                "message": "11번가 API Key가 비어있습니다. 설정에서 해당 계정을 수정 후 저장해주세요.",
            }

        # 카테고리 코드가 숫자가 아니면 (경로 문자열이면) 빈값 처리
        cat_code = category_id
        if cat_code and not cat_code.isdigit():
            cat_code = ""

        if not cat_code:
            return {
                "success": False,
                "message": "11번가 카테고리 코드가 없습니다. 카테고리 매핑을 설정해주세요.",
            }

        client = ElevenstClient(api_key)

        # 스토어설정 재고수량 상한 (전체 경로 공통)
        _acct_extras = (account.additional_fields or {}) if account else {}
        _max_stock_cap = int(
            _acct_extras.get("stockQuantity") or product.get("_max_stock") or 0
        )

        # ── 경량 가격/재고 업데이트 (오토튠 최적화) ──────────────────────
        # _skip_image_upload=True → price/stock만 변경된 경우
        # 전체 XML 변환 없이 가격/재고만 포함된 최소 XML로 수정
        if product.get("_skip_image_upload") and existing_no:
            from backend.domain.samba.proxy.elevenst import (
                ElevenstRateLimitError,
                _build_elevenst_option_xml,
            )

            try:
                new_price = int(product.get("sale_price", 0))
                options = product.get("options") or []

                # 신규등록과 동일한 구조 유지 — 옵션 구조 불일치 시 11번가가 "옵션 동일" 판정 실패
                # 2D(슬래시) → 멀티옵션, 1D → 싱글옵션, 옵션별 추가요금/재고 반영
                option_xml = ""
                if options:
                    option_xml = _build_elevenst_option_xml(
                        options,
                        max_stock_cap=_max_stock_cap,
                        option_group_names=product.get("option_group_names"),
                    )

                _brand = (
                    (product.get("brand") or "")
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                _brand_xml = f"<brand>{_brand}</brand>" if _brand else ""
                xml_data = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    "<Product>"
                    "<selMthdCd>01</selMthdCd>"
                    f"<selPrc>{new_price}</selPrc>"
                    f"{_brand_xml}"
                    f"{option_xml}"
                    "</Product>"
                )

                logger.info(f"[11번가] 경량 업데이트 XML:\n{xml_data}")
                result = await client.update_product(existing_no, xml_data)
                logger.info(f"[11번가] 경량 업데이트 응답: {result}")

                _parts = [f"가격({new_price:,}원)"]
                if options:
                    _parts.append(f"옵션({len(options)}건)")
                logger.info(
                    f"[11번가] 경량 업데이트 완료: {existing_no} — {', '.join(_parts)}"
                )
                return {
                    "success": True,
                    "product_no": existing_no,
                    "message": f"11번가 경량 업데이트: {', '.join(_parts)}",
                    "data": result,
                }

            except ElevenstRateLimitError:
                raise  # Rate Limit은 폴백 없이 즉시 전파
            except Exception as e:
                _err_msg = str(e)
                # 유령 매핑이면 폴백 시도 무의미 — 즉시 정리하고 종료
                if _is_ghost_error(_err_msg):
                    pid = str(product.get("id") or "")
                    aid = getattr(account, "id", "") if account else ""
                    await _purge_ghost_mapping(session, pid, aid, existing_no, _err_msg)
                    return {
                        "success": True,
                        "product_no": "",
                        "message": f"11번가 유령 매핑 자동정리 (사유: {_err_msg})",
                        "ghost_cleanup": True,
                    }
                logger.warning(
                    f"[11번가] 경량 업데이트 실패, 전체 수정으로 폴백: {existing_no} — {e}"
                )
                # 폴백: 아래 전체 로직으로 계속 진행

        account_settings = (account.additional_fields or {}) if account else {}

        # 무신사 등 referer 차단 CDN URL을 R2로 미러링
        # — 11번가는 등록 URL을 자체 서버가 fetch하므로 핫링크 차단 시 워터마크 이미지로 캐싱됨
        # — detail_html은 shipment service에서 미러링 이전에 생성되므로
        #   문자열 내부 <img src="..."> 도 같이 치환해야 워터마크 회피가 완성됨.
        try:
            from backend.domain.samba.image.service import ImageTransformService

            _img_svc = ImageTransformService(session)
            _images = product.get("images") or []
            _detail_images = product.get("detail_images") or []
            _detail_html = product.get("detail_html") or ""
            if _images or _detail_images or _detail_html:
                product = dict(product)  # 원본 dict 변형 방지
                if _images:
                    product["images"], _ = await _img_svc.mirror_external_to_r2(_images)
                if _detail_images:
                    (
                        product["detail_images"],
                        _,
                    ) = await _img_svc.mirror_external_to_r2(_detail_images)
                if _detail_html:
                    product["detail_html"] = await _img_svc.mirror_urls_in_html(
                        _detail_html
                    )
                if not product.get("images"):
                    return {
                        "success": False,
                        "message": "11번가 등록 실패: 이미지 미러링 후 사용 가능한 이미지가 없습니다.",
                    }
        except Exception as e:
            logger.warning(f"[11번가] 이미지 미러링 단계 오류 — 원본 URL 유지: {e}")

        # 카테고리 키속성 메타 조회 (TTL 캐시 — 재호출 시 즉시 반환)
        # 선글라스/시계 등은 치수 키속성이 필수이며, 누락 시 11번가가 500 반환
        try:
            ctgr_attributes = await client.get_category_attributes(cat_code)
        except Exception as e:
            logger.warning(
                f"[11번가] 키속성 메타 조회 실패 cat={cat_code} — 키속성 없이 진행: {e}"
            )
            ctgr_attributes = []

        xml_data = ElevenstClient.transform_product(
            product,
            cat_code,
            settings=account_settings,
            ctgr_attributes=ctgr_attributes,
        )

        if existing_no:
            logger.info(f"[11번가] 폴백 전체XML (전체):\n{xml_data}")

        # 기존 상품번호가 있으면 수정, 없으면 신규등록
        from backend.domain.samba.proxy.elevenst import (
            ElevenstApiError,
            ElevenstRateLimitError,
        )

        try:
            if existing_no:
                result = await client.update_product(existing_no, xml_data)
                logger.info(f"[11번가] 폴백 응답: {result}")
                return {
                    "success": True,
                    "product_no": existing_no,
                    "message": "11번가 수정 성공",
                    "data": result,
                }
            else:
                result = await client.register_product(xml_data)
                prd_no = result.get("prd_no") or result.get("data", {}).get("prdNo", "")
                if not prd_no:
                    # prdNo 미수신 → ghost 매핑 생성 차단 (registered_accounts에 추가 X)
                    # 11번가 셀러센터엔 등록됐을 수도 있으나 우리 DB가 prdNo 못 받았으니
                    # 다음 사이클에서 신규등록 재시도하도록 success=False 처리.
                    logger.warning(
                        f"[11번가] 신규 등록 후 prdNo 미수신 — result keys={list(result.keys()) if isinstance(result, dict) else type(result)}"
                    )
                    return {
                        "success": False,
                        "product_no": "",
                        "message": "11번가 등록 응답에 prdNo 없음 — ghost 매핑 방지로 실패 처리",
                        "data": result,
                    }
                logger.info(f"[11번가] 신규 등록 완료 — product_no={prd_no}")
                return {
                    "success": True,
                    "product_no": prd_no,
                    "message": "11번가 등록 성공",
                    "data": result,
                }
        except ElevenstRateLimitError:
            raise  # worker까지 전파시켜 Rate Limit 동적 감소 동작하도록
        except ElevenstApiError as e:
            err = str(e)
            # 유령 매핑(이미 삭제됨/존재하지 않음) → DB 매핑 정리 후 success 처리
            if existing_no and _is_ghost_error(err):
                pid = str(product.get("id") or "")
                aid = getattr(account, "id", "") if account else ""
                await _purge_ghost_mapping(session, pid, aid, existing_no, err)
                return {
                    "success": True,
                    "product_no": "",
                    "message": f"11번가 유령 매핑 자동정리 (사유: {err})",
                    "ghost_cleanup": True,
                }
            if "해외 쇼핑 카테고리" in err:
                return {
                    "success": False,
                    "message": f"카테고리 오류: 코드 {cat_code}가 해외쇼핑 카테고리입니다. 카테고리매핑에서 국내 카테고리 코드로 수정해주세요.",
                }
            return {"success": False, "message": f"11번가 등록 실패: {err}"}

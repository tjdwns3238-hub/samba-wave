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
                _escape_xml,
                _resolve_origin,
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

                # 원산지 — 11번가는 PUT 시 저장된 값까지 조건부 재검증함
                # orgnTypCd=02(해외)인데 저장된 상세지역이 빈 경우 가격만 보내도 검증 실패
                # → 경량 XML에서도 원산지 필드를 함께 보내 검증 통과 보장
                _cfg_origin = _acct_extras.get("origin") or ""
                if _cfg_origin == "기타":
                    _cfg_origin = ""
                _origin_raw = _cfg_origin or product.get("origin") or ""
                _otc, _otd, _onv = _resolve_origin(_origin_raw)
                _origin_parts = [f"<orgnTypCd>{_otc}</orgnTypCd>"]
                if _otd:
                    _origin_parts.append(f"<orgnTypDtlsCd>{_otd}</orgnTypDtlsCd>")
                if _onv:
                    _origin_parts.append(f"<orgnNmVal>{_escape_xml(_onv)}</orgnNmVal>")
                _origin_xml = "".join(_origin_parts)

                # 배송 — 11번가는 PUT 시 dlvCstInstBasiCd(배송비 설정 코드)도 조건부 재검증
                # 저장된 값이 빈 경우 가격만 보내도 STATUS[103] 검증 실패 → 전체XML 폴백 → dispCtgrNo 권한 에러 연쇄
                # → 경량 XML에서도 배송 블록 동봉 (transform_product 와 동일 매핑)
                _dlv_code_map = {"DV_FREE": "01", "DV_FIXED": "02", "DV_COND": "03"}
                _raw_dlv = _acct_extras.get("deliveryType", "01")
                _delivery_type = _dlv_code_map.get(_raw_dlv, _raw_dlv) or "01"
                _delivery_fee = int(_acct_extras.get("deliveryFee", 0) or 0)
                _return_fee = int(_acct_extras.get("returnFee", 4000) or 4000)
                _exchange_fee = int(_acct_extras.get("exchangeFee", 8000) or 8000)
                _jeju_fee = int(_acct_extras.get("jejuFee", 0) or 0)
                _island_fee = int(_acct_extras.get("islandFee", 0) or 0)
                _ship_from = _acct_extras.get("shipFromAddress", "") or ""
                _return_addr = _acct_extras.get("returnAddress", "") or ""
                _dispatch_tmpl = str(
                    _acct_extras.get("dispatchTemplateNo", "") or ""
                ).strip()
                _dispatch_tmpl_xml = (
                    f"<dlvSendCloseTmpltNo>{_escape_xml(_dispatch_tmpl)}</dlvSendCloseTmpltNo>"
                    if _dispatch_tmpl
                    else ""
                )
                _delivery_xml = (
                    f"<dlvCnFee>{_delivery_fee}</dlvCnFee>"
                    f"<dlvGrntYn>Y</dlvGrntYn>"
                    f"{_dispatch_tmpl_xml}"
                    f"<dlvCstInstBasiCd>{_delivery_type}</dlvCstInstBasiCd>"
                    f"<jejuDlvCst>{_jeju_fee}</jejuDlvCst>"
                    f"<islandDlvCst>{_island_fee}</islandDlvCst>"
                    f"<rtngdDlvCst>{_return_fee}</rtngdDlvCst>"
                    f"<exchDlvCst>{_exchange_fee}</exchDlvCst>"
                    f"<dlvBsPlc>{_escape_xml(_ship_from)}</dlvBsPlc>"
                    f"<rtngBsPlc>{_escape_xml(_return_addr)}</rtngBsPlc>"
                )

                # A/S·교환반품 안내 — 11번가 PUT 시 빈값 재검증 가드
                # 저장된 값이 빈 경우 경량 PUT 응답 "교환반품 안내는 반드시 입력하셔야 합니다 STATUS[103]"
                # → 전체 XML 폴백 시 다시 카테고리 권한 에러로 종료되어 자동복구가 못 잡음
                _as_msg = (
                    _acct_extras.get("asMessage") or ""
                ).strip() or "상세페이지 참조"
                _rtn_exch = (
                    _acct_extras.get("returnExchangeGuide") or ""
                ).strip() or "상세페이지 참조"
                _after_xml = (
                    f"<asDetail>{_escape_xml(_as_msg)}</asDetail>"
                    f"<rtngExchDetail>{_escape_xml(_rtn_exch)}</rtngExchDetail>"
                )

                # 안전인증정보 빈값 재검증 가드 — 경량 PUT에 ProductCertGroup 누락 시
                # 11번가 응답 "안전인증정보 설정 오류 [인증유형 및 인증번호를 입력해주세요] STATUS[103]"
                # → 전체XML 폴백 → dispCtgrNo 권한 에러 연쇄. transform_product 와 동일 매핑 동봉
                _cert_xml = (
                    "<ProductCertGroup><crtfGrpTypCd>01</crtfGrpTypCd><crtfGrpObjClfCd>03</crtfGrpObjClfCd></ProductCertGroup>"
                    "<ProductCertGroup><crtfGrpTypCd>02</crtfGrpTypCd><crtfGrpObjClfCd>03</crtfGrpObjClfCd></ProductCertGroup>"
                    "<ProductCertGroup><crtfGrpTypCd>03</crtfGrpTypCd><crtfGrpObjClfCd>03</crtfGrpObjClfCd></ProductCertGroup>"
                    "<ProductCertGroup><crtfGrpTypCd>04</crtfGrpTypCd><crtfGrpObjClfCd>05</crtfGrpObjClfCd></ProductCertGroup>"
                    "<ProductCert><certTypeCd>131</certTypeCd><certKey></certKey></ProductCert>"
                )

                xml_data = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    "<Product>"
                    "<selMthdCd>01</selMthdCd>"
                    f"<selPrc>{new_price}</selPrc>"
                    f"{_brand_xml}"
                    f"{_origin_xml}"
                    f"{_delivery_xml}"
                    f"{_after_xml}"
                    f"{_cert_xml}"
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
                # 발송마감 템플릿 오류 — 템플릿 제거 후 즉시 재시도 (전체 폴백도 동일 에러)
                if "발송마감 템플릿" in _err_msg:
                    import re as _re_tmpl

                    _xml_no_tmpl = _re_tmpl.sub(
                        r"<dlvSendCloseTmpltNo>[^<]*</dlvSendCloseTmpltNo>",
                        "",
                        xml_data,
                    )
                    logger.warning(
                        f"[11번가] 발송마감 템플릿 미존재 — 템플릿 없이 재시도: {existing_no}"
                    )
                    try:
                        _r2 = await client.update_product(existing_no, _xml_no_tmpl)
                        _p2 = [f"가격({new_price:,}원)"]
                        if options:
                            _p2.append(f"옵션({len(options)}건)")
                        return {
                            "success": True,
                            "product_no": existing_no,
                            "message": f"11번가 경량 업데이트 (발송마감 템플릿 제외): {', '.join(_p2)}",
                            "data": _r2,
                        }
                    except Exception as _e2:
                        logger.warning(
                            f"[11번가] 템플릿 제거 재시도도 실패, 전체 수정으로 폴백: {_e2}"
                        )
                        # 폴백: 아래 전체 로직으로 계속 진행
                else:
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
                _pid = product.get("id")
                if _images:
                    product["images"], _ = await _img_svc.mirror_with_persistence(
                        _pid, _images
                    )
                if _detail_images:
                    (
                        product["detail_images"],
                        _,
                    ) = await _img_svc.mirror_with_persistence(_pid, _detail_images)
                if _detail_html:
                    product["detail_html"] = await _img_svc.mirror_urls_in_html(
                        _detail_html
                    )
                if not product.get("images"):
                    return {
                        "success": False,
                        "message": "11번가 등록 실패: 이미지 미러링 후 사용 가능한 이미지가 없습니다.",
                    }
                # issue #218 — 미러링 후에도 핫링크 차단 도메인(msscdn 등) URL이 남아있으면 등록 차단
                # 그대로 등록되면 무신사 광고 배너가 11번가 상품 페이지에 노출됨
                _still_blocked = [
                    u
                    for u in (product.get("images") or [])
                    if ImageTransformService.is_hotlink_blocked_url(u)
                ]
                if _still_blocked:
                    return {
                        "success": False,
                        "message": (
                            f"11번가 등록 취소: R2 미러링 실패로 핫링크 차단 URL {len(_still_blocked)}개 잔존. "
                            "R2 설정을 확인하고 재시도하세요."
                        ),
                    }
        except Exception as e:
            # R2 설정 자체가 없는 경우(개발환경) 대비 — 차단 도메인 잔존 시에만 등록 차단
            try:
                from backend.domain.samba.image.service import (
                    ImageTransformService as _ITS,
                )

                _blocked = [
                    u
                    for u in (product.get("images") or [])
                    if _ITS.is_hotlink_blocked_url(u)
                ]
            except Exception:
                _blocked = []
            if _blocked:
                logger.error(
                    f"[11번가] R2 미러링 오류 + 차단 URL 존재 — 등록 차단: {e}"
                )
                return {
                    "success": False,
                    "message": f"11번가 등록 취소: R2 미러링 오류. {e}",
                }
            logger.warning(
                f"[11번가] 이미지 미러링 단계 오류 — 차단 URL 없어 원본 유지: {e}"
            )

        # 키속성(ProductCtgrAttribute) 조회 API는 11번가 OpenAPI에 존재하지 않는다
        # (공식 문서 전수 확인 2026-06: 카테고리 서비스는 전체/하위 카테고리조회 2개뿐,
        # 상품관리 메뉴에도 속성 API 없음). 기존 /rest/cateservice/categoryAttributes/{id}
        # 호출은 매 등록마다 -997("등록된 API 정보 없음")만 반환하던 死코드 → 제거.
        # 키속성 XML은 항상 빈 값이며, 치수 등 키속성이 필수인 카테고리(선글라스 등)는
        # 11번가 OpenAPI로 자동등록이 불가하다(수동 등록 대상).
        ctgr_attributes: list[dict[str, str]] = []

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
                # 수정 PUT 시 dispCtgrNo 제거 — 카테고리매핑 변경으로 보낸 코드가 등록 카테고리와
                # 다르면 11번가가 STATUS[103] "상위 카테고리를 수정할 수 있는 권한이 없습니다" 반환
                # 오토튠 가격/재고 갱신 경로에서 카테고리 변경 의도 없음. 신규등록 path는 영향 없음
                import re as _re

                xml_data_for_put = _re.sub(
                    r"<dispCtgrNo>[^<]*</dispCtgrNo>", "", xml_data, count=1
                )
                # 이미지 슬롯 클리어 (#310): 이미지 개수 축소 시(예: 4장→1장) 미전송된
                # prdImage02~04 슬롯에 옛 이미지가 잔존. 11번가 PUT은 누락 슬롯을
                # 기존값으로 유지하므로, 안 쓰는 슬롯을 빈 태그로 명시 전송해 강제 클리어.
                # prdImage01 이 있을 때(이미지 전송 케이스)만 보강 — 경량XML/무이미지 PUT은 무영향.
                if "</prdImage01>" in xml_data_for_put:
                    _missing_slots = "".join(
                        f"<prdImage{_n}></prdImage{_n}>"
                        for _n in ("02", "03", "04")
                        if f"<prdImage{_n}>" not in xml_data_for_put
                    )
                    if _missing_slots:
                        xml_data_for_put = xml_data_for_put.replace(
                            "</prdImage01>", "</prdImage01>" + _missing_slots, 1
                        )
                try:
                    result = await client.update_product(existing_no, xml_data_for_put)
                except Exception as _put_e:
                    # 발송마감 템플릿 오류 — 템플릿 태그 제거 후 재시도
                    if "발송마감 템플릿" in str(_put_e):
                        logger.warning(
                            f"[11번가] 전체 수정 발송마감 템플릿 미존재 — 템플릿 없이 재시도: {existing_no}"
                        )
                        xml_data_for_put = _re.sub(
                            r"<dlvSendCloseTmpltNo>[^<]*</dlvSendCloseTmpltNo>",
                            "",
                            xml_data_for_put,
                        )
                        result = await client.update_product(
                            existing_no, xml_data_for_put
                        )
                    else:
                        raise
                logger.info(f"[11번가] 폴백 응답: {result}")
                return {
                    "success": True,
                    "product_no": existing_no,
                    "message": "11번가 수정 성공",
                    "data": result,
                }
            else:
                # 중복등록 방지(유령 차단): 등록 전 sellerPrdCd(=samba product.id)로
                # 11번가 기존 등록 여부 확인. DB 매핑이 유실돼 existing_no가 비어도
                # 11번가에 이미 있으면 재등록(중복 생성) 대신 기존 prdNo를 채택한다.
                _seller_code = str(product.get("id") or "").strip()
                if _seller_code:
                    try:
                        _dup = await client.find_by_seller_code(_seller_code)
                        # 금지(108) 상태는 재연결 부적합 — 그 외 존재 시 기존 prdNo 채택
                        if (
                            _dup.get("found")
                            and _dup.get("prd_no")
                            and str(_dup.get("sel_stat_cd") or "") != "108"
                        ):
                            _exist_prd = str(_dup["prd_no"])
                            logger.warning(
                                f"[11번가] 중복등록 방지 — sellerPrdCd={_seller_code} "
                                f"이미 존재(prdNo={_exist_prd}, 상태={_dup.get('sel_stat_cd')}) → 기존 연결"
                            )
                            return {
                                "success": True,
                                "product_no": _exist_prd,
                                "message": "11번가 기등록 상품 재연결 (중복등록 차단)",
                                "data": _dup,
                                "_already_registered": True,
                            }
                    except Exception as _dup_e:
                        logger.warning(
                            f"[11번가] 중복등록 사전조회 실패 — 등록 진행: {_dup_e}"
                        )
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
            # 상위 카테고리 권한 에러(STATUS[103]) → DB prdNo가 11번가 실제 prdNo와 불일치할 때 발생
            # sellerprodcode로 진짜 prdNo 재조회 후 DB 동기화 + 1회 재시도
            if existing_no and (
                "상위 카테고리를 수정할 수 있는 권한" in err or "STATUS : [103]" in err
            ):
                pid = str(product.get("id") or "")
                aid = getattr(account, "id", "") if account else ""
                try:
                    lookup = await client.find_by_seller_code(pid)
                    real_prd_no = (lookup.get("prd_no") or "").strip()
                except Exception as _e:
                    real_prd_no = ""
                    logger.warning(
                        f"[11번가] STATUS[103] 자동복구 — sellerprodcode 조회 실패 product={pid}: {_e}"
                    )
                if real_prd_no and real_prd_no != existing_no:
                    # DB market_product_nos[aid] 갱신
                    try:
                        from sqlalchemy.orm.attributes import flag_modified
                        from sqlmodel import select

                        from backend.domain.samba.collector.model import (
                            SambaCollectedProduct,
                        )

                        stmt = select(SambaCollectedProduct).where(
                            SambaCollectedProduct.id == pid
                        )
                        prod = (await session.execute(stmt)).scalars().first()
                        if prod and aid:
                            nos = dict(prod.market_product_nos or {})
                            nos[aid] = real_prd_no
                            prod.market_product_nos = nos
                            flag_modified(prod, "market_product_nos")
                            session.add(prod)
                            await session.commit()
                            logger.warning(
                                f"[11번가] STATUS[103] 자동복구 — DB prdNo 갱신 product={pid} account={aid} {existing_no}→{real_prd_no}"
                            )
                    except Exception as _e:
                        logger.warning(f"[11번가] STATUS[103] DB 갱신 실패: {_e}")
                    # 진짜 prdNo로 PUT 1회 재시도
                    try:
                        retry_result = await client.update_product(
                            real_prd_no, xml_data
                        )
                        logger.info(
                            f"[11번가] STATUS[103] 재시도 성공 prdNo={real_prd_no}"
                        )
                        return {
                            "success": True,
                            "product_no": real_prd_no,
                            "message": f"11번가 수정 성공 (prdNo 재동기화: {existing_no}→{real_prd_no})",
                            "data": retry_result,
                            "prd_no_resync": True,
                        }
                    except Exception as _e:
                        return {
                            "success": False,
                            "message": f"11번가 수정 실패 (prdNo 재동기화 후 재시도 실패): {_e}",
                        }
            if "해외 쇼핑 카테고리" in err:
                return {
                    "success": False,
                    "message": f"카테고리 오류: 코드 {cat_code}가 해외쇼핑 카테고리입니다. 카테고리매핑에서 국내 카테고리 코드로 수정해주세요.",
                }
            return {"success": False, "message": f"11번가 등록 실패: {err}"}

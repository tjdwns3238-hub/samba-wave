"""주문 마켓 송장 전송 통일 service.

ship_order 라우터(수동 마켓전송 버튼)와 dispatch_to_market(자동 dispatch) 양쪽이
이 함수만 호출하도록 통일. 이전엔 마켓별 분기를 양쪽이 중복 구현해서
자격증명 누락/필드 차이로 자동 dispatch 가 실패하던 회귀 차단 (도혜연 사례).

사용:
    market_sent, msg = await send_invoice_to_market(
        order, shipping_company, tracking_number, session
    )
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.utils.logger import logger


async def send_invoice_to_market(
    order,
    shipping_company: str,
    tracking_number: str,
    session: AsyncSession,
) -> tuple[bool, str]:
    """단일 주문 마켓 송장 전송 — 마켓별 분기 통일.

    Returns: (market_sent, message)
    """
    if not order.channel_id:
        return False, "마켓 채널 계정 미연결(channel_id 없음)"

    from backend.domain.samba.account.repository import SambaMarketAccountRepository

    account_repo = SambaMarketAccountRepository(session)
    account = await account_repo.get_async(order.channel_id)
    if not account:
        return False, "마켓 계정 조회 실패"

    market_type = (account.market_type or "").lower()
    courier = shipping_company or ""
    tracking = tracking_number or ""

    try:
        if market_type == "lotteon":
            return await _send_lotteon(order, account, courier, tracking, session)
        if market_type == "smartstore":
            return await _send_smartstore(order, account, courier, tracking, session)
        if market_type == "11st":
            return await _send_11st(order, account, courier, tracking, session)
        if market_type == "ebay":
            return await _send_ebay(order, account, courier, tracking, session)
        if market_type == "coupang":
            return await _send_coupang(order, account, courier, tracking, session)
        if market_type == "playauto":
            # 플레이오토는 EMP API 실효성 없어 마켓 전송 생략, DB 저장만.
            return True, "플레이오토 주문 — 송장번호 저장만 완료 (마켓 전송 생략)"
        if market_type == "lottehome":
            return await _send_lottehome(order, account, courier, tracking, session)
        if market_type == "ssg":
            return await _send_ssg(order, account, courier, tracking, session)
        return False, f"미지원 마켓: {market_type}"
    except Exception as e:
        logger.warning(
            f"[송장전송] order={order.order_number} market={market_type} err={e}"
        )
        return False, f"{market_type} 송장 전송 실패: {e}"


# ──────────────────────────────────────────────────────────────────────────
# 마켓별 구현 — 모두 ship_order 라우터의 로직과 동일.
# ──────────────────────────────────────────────────────────────────────────


async def _send_lotteon(order, account, courier, tracking, session):
    from backend.domain.samba.account.resolver import resolve_market_creds
    from backend.domain.samba.proxy.lotteon import LotteonClient

    lo_api_key = (
        (account.additional_fields or {}).get("apiKey", "") or account.api_key or ""
    )
    if not lo_api_key:
        # (2026-05-25) resolver 위임 — account_id 명시로 그 계정 자격증명만 해석.
        # default 계정 폴백 금지: 테트리스/정책으로 정해진 계정 결정을 덮어선 안 됨.
        _tid = getattr(account, "tenant_id", None) if account else None
        _aid = getattr(account, "id", None) if account else None
        _creds = await resolve_market_creds(
            session,
            _tid,
            market_type="lotteon",
            store_key="store_lotteon",
            account_id=_aid,
        )
        if _creds:
            lo_api_key = _creds.get("apiKey", "")
    if not lo_api_key:
        return False, "롯데ON API Key 누락"

    client = LotteonClient(lo_api_key)
    await client.test_auth()
    sent = await client.ship_order(
        od_no=order.od_no or order.order_number,
        od_seq=order.od_seq or "1",
        proc_seq=order.proc_seq or "1",
        sitm_no=order.sitm_no or order.shipment_id or "",
        spd_no=order.product_id or "",
        quantity=order.quantity or 1,
        shipping_company=courier,
        tracking_number=tracking,
    )
    return (True, "롯데ON 송장 등록 완료") if sent else (False, "롯데ON 송장 등록 실패")


async def _send_smartstore(order, account, courier, tracking, session):
    from backend.domain.samba.account.resolver import resolve_market_creds
    from backend.domain.samba.proxy.smartstore import SmartStoreClient

    _extras = account.additional_fields or {}
    cid = _extras.get("clientId", "") or account.api_key or ""
    csecret = _extras.get("clientSecret", "") or account.api_secret or ""
    if not cid or not csecret:
        # (2026-05-25) resolver 위임 — account_id 명시로 그 계정 자격증명만 해석.
        # default 계정 폴백 금지: 테트리스/정책으로 정해진 계정 결정을 덮어선 안 됨.
        _tid = getattr(account, "tenant_id", None) if account else None
        _aid = getattr(account, "id", None) if account else None
        _creds = await resolve_market_creds(
            session,
            _tid,
            market_type="smartstore",
            store_key="store_smartstore",
            account_id=_aid,
        )
        if _creds:
            cid = cid or _creds.get("clientId", "")
            csecret = csecret or _creds.get("clientSecret", "")
    if not cid or not csecret:
        return False, "스마트스토어 자격증명(clientId/Secret) 누락"

    product_order_id = (order.order_number or order.ext_order_number or "").strip()
    if not product_order_id:
        return False, "스마트스토어 product_order_id 누락"

    client = SmartStoreClient(cid, csecret)
    await client.ship_product_order(product_order_id, courier, tracking)
    return True, "스마트스토어 송장 전송 완료"


async def _send_11st(order, account, courier, tracking, session):
    from backend.domain.samba.proxy.elevenst import ElevenstClient

    # 11번가 공식 배송업체 코드 (sendDeliveryInfo.dlvEtprsCd)
    # 주의: 우체국=00007 / 로젠=00002 / 경동=00026 / 합동=00035 (자주 혼동되는 코드)
    _CARRIER_MAP = {
        # 국내 주요
        "CJ대한통운": "00034",
        "대한통운": "00034",
        "CJ택배": "00034",
        "씨제이대한통운": "00034",
        "CJGLS": "00034",
        "롯데택배": "00012",
        "롯데": "00012",
        "현대택배": "00012",
        "한진택배": "00011",
        "한진": "00011",
        "HANJIN": "00011",
        "우체국택배": "00007",
        "우체국": "00007",
        "우체국소포": "00007",
        "등기": "00007",
        "로젠택배": "00002",
        "로젠": "00002",
        # 편의점/지역
        "CU편의점택배": "00061",
        "GS25편의점택배": "00060",
        "CVSnet편의점택배": "00060",
        "GS포스트박스": "00060",
        "편의점택배": "00060",
        # 중소
        "경동택배": "00026",
        "경동": "00026",
        "합동택배": "00035",
        "합동": "00035",
        "대신택배": "00021",
        "대신": "00021",
        "일양로지스": "00022",
        "일양택배": "00022",
        "ILYANG": "00022",
        "천일택배": "00027",
        "천일": "00027",
        "건영택배": "00037",
        "건영": "00037",
        "농협택배": "00067",
        "농협": "00067",
        "SLX택배": "00063",
        "SLX": "00063",
        "한의사랑택배": "00064",
        "용마로지스": "00065",
        "용마": "00065",
        "세방택배": "00066",
        "세방": "00066",
        "HI택배": "00068",
        "원더스퀵": "00069",
        "홈픽택배": "00070",
        "홈픽": "00070",
        "퍼레버택배": "00081",
        "팀프레시": "00116",
        "(주)팀프레시": "00116",
        "GTS로지스": "00114",
        "로지스밸리택배": "00101",
        "로지스밸리": "00101",
        "딜리박스": "00133",
        "이스트라": "00129",
        # 해외/특송
        "DHL": "00039",
        "Fedex": "00047",
        "FedEx": "00047",
        "UPS": "00053",
        "EMS": "00058",
        "TNT": "00051",
        "TNT Express": "00051",
        # 직접배송 / 기타 (dlvMthdCd=03 분기에서 처리)
        "직접배송": "00099",
        "직접 배송": "00099",
        "기타": "00099",
    }
    api_key = account.api_key or ""
    if not api_key and isinstance(account.additional_fields, dict):
        api_key = account.additional_fields.get("apiKey", "") or ""
    if not api_key:
        return False, "11번가 API Key 누락"

    dlv_no = order.shipment_id or ""
    if not dlv_no:
        return False, "11번가 배송번호(dlvNo) 누락"

    dlv_etprs_cd = _CARRIER_MAP.get(courier, courier)
    is_direct = (courier or "").replace(" ", "") == "직접배송"
    client = ElevenstClient(api_key)
    sent = await client.ship_order(
        dlv_no=dlv_no,
        invc_no=tracking,
        dlv_etprs_cd=dlv_etprs_cd,
        dlv_mthd_cd="03" if is_direct else "01",
    )
    return (True, "11번가 송장 전송 완료") if sent else (False, "11번가 송장 전송 실패")


async def _send_ebay(order, account, courier, tracking, session):
    from backend.domain.samba.proxy.ebay import EbayApiError, EbayClient

    extras = account.additional_fields or {}
    app_id = extras.get("clientId") or extras.get("appId") or account.api_key or ""
    cert_id = (
        extras.get("clientSecret") or extras.get("certId") or account.api_secret or ""
    )
    refresh_token = extras.get("oauthToken") or extras.get("authToken", "") or ""
    if not (app_id and cert_id and refresh_token):
        return False, "eBay 자격증명(appId/certId/oauthToken) 누락"

    client = EbayClient(
        app_id=app_id,
        dev_id="",
        cert_id=cert_id,
        refresh_token=refresh_token,
        sandbox=bool(extras.get("sandbox", False)),
    )
    carrier_map = {"USPS": "USPS", "UPS": "UPS", "FedEx": "FEDEX", "DHL": "DHL"}
    ebay_carrier = carrier_map.get(courier, "KoreaPost")
    ebay_order_id = order.ext_order_number or order.order_number
    try:
        await client.ship_order(
            order_id=ebay_order_id,
            tracking_number=tracking,
            carrier_code=ebay_carrier,
        )
        return True, "eBay 송장 전송 완료"
    except EbayApiError as e:
        return False, f"eBay 송장 실패: {e}"


async def _send_coupang(order, account, courier, tracking, session):
    """쿠팡 — additional_fields 기반 자격증명 (Wing OpenAPI HMAC)."""
    from backend.domain.samba.proxy.coupang import CoupangClient

    extras = account.additional_fields or {}
    access_key = extras.get("accessKey") or account.api_key or ""
    secret_key = extras.get("secretKey") or account.api_secret or ""
    vendor_id = extras.get("vendorId") or ""
    if not (access_key and secret_key and vendor_id):
        return False, "쿠팡 자격증명(accessKey/secretKey/vendorId) 누락"

    try:
        shipment_box_id = int(order.ext_order_number or order.shipment_id or 0)
    except (TypeError, ValueError):
        return False, f"쿠팡 shipmentBoxId 형식 오류: {order.ext_order_number}"
    if not shipment_box_id:
        return False, "쿠팡 shipmentBoxId 누락 (ext_order_number/shipment_id)"

    try:
        client = CoupangClient(
            access_key=access_key, secret_key=secret_key, vendor_id=vendor_id
        )
    except TypeError:
        # CoupangClient 시그니처가 다른 경우 폴백 — 기본 생성자
        client = CoupangClient()
    api_resp = await client.update_shipping(
        shipment_box_id=shipment_box_id,
        delivery_company_code=courier,
        invoice_number=tracking,
    )
    if isinstance(api_resp, dict) and api_resp.get("ok", True):
        return True, "쿠팡 송장 전송 완료"
    return False, f"쿠팡 송장 전송 실패: {(api_resp or {}).get('error') or 'unknown'}"


async def _send_lottehome(order, account, courier, tracking, session):
    """롯데홈쇼핑 — _dispatch_lottehome_invoice 와 동일 흐름."""
    from backend.domain.samba.proxy.lottehome import (
        LotteHomeClient,
        lottehome_courier_code,
    )

    raw = (order.ext_order_number or "").strip() or (order.shipment_id or "").strip()
    if not raw:
        return False, "롯데홈쇼핑 주문번호(ext_order_number) 누락"

    sep = ":" if ":" in raw else ("/" if "/" in raw else None)
    if not sep:
        return False, f"롯데홈쇼핑 주문번호 형식 오류 (ord_no:ord_dtl_sn): {raw}"
    parts = [p.strip() for p in raw.split(sep, 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False, f"롯데홈쇼핑 주문번호 파싱 실패: {raw}"
    ord_no, ord_dtl_sn = parts[0], parts[1]

    courier_code = lottehome_courier_code(courier)
    extras = account.additional_fields or {}

    # issue #216 — plugins/markets/lottehome.py 와 동일한 자격증명 우선순위 적용:
    # additional_fields → lottehome_credentials → store_lottehome → seller_id
    from backend.domain.samba.forbidden.model import SambaSettings
    from sqlmodel import select as _select

    async def _load_setting(key: str):
        try:
            _r = await session.execute(
                _select(SambaSettings).where(SambaSettings.key == key)
            )
            _row = _r.scalars().first()
            return _row.value if _row else None
        except Exception:
            return None

    _lh_creds = await _load_setting("lottehome_credentials")
    _lh_store = await _load_setting("store_lottehome")

    def _pick(*keys: str) -> str:
        for src in (
            extras,
            _lh_creds if isinstance(_lh_creds, dict) else {},
            _lh_store if isinstance(_lh_store, dict) else {},
        ):
            for k in keys:
                v = src.get(k) if isinstance(src, dict) else None
                if v:
                    return str(v)
        return ""

    user_id = _pick("userId") or (getattr(account, "seller_id", "") or "")
    password = _pick("password")
    agnc_no = _pick("agncNo") or user_id
    env = _pick("env") or "test"

    if not user_id or not password:
        return False, "롯데홈쇼핑 자격증명(userId/password) 누락"

    client = LotteHomeClient(
        user_id=user_id,
        password=password,
        agnc_no=agnc_no,
        env=env,
    )
    api_resp = await client.send_invoice(
        ord_no=ord_no,
        ord_dtl_sn=ord_dtl_sn,
        courier_code=courier_code,
        tracking_number=tracking,
    )
    if api_resp.get("ok"):
        return True, "롯데홈쇼핑 송장 전송 완료"
    return False, f"롯데홈쇼핑 송장 전송 실패: {api_resp.get('result')}"


async def _send_ssg(order, account, courier, tracking, session):
    """SSG(신세계몰) — 운송장 등록 후 출고처리로 배송중 상태 전환."""
    from backend.domain.samba.proxy.ssg import SSGClient

    extras = account.additional_fields or {}
    api_key = extras.get("apiKey", "") or account.api_key or ""
    if not api_key:
        return False, "SSG API Key 누락"

    shipment_id = (order.shipment_id or "").strip()
    if not shipment_id or "|" not in shipment_id:
        return (
            False,
            f"SSG shipment_id 형식 오류: {shipment_id!r} (shppNo|shppSeq 필요)",
        )

    parts = shipment_id.split("|")
    shpp_no = parts[0].strip()
    shpp_seq = parts[1].strip() if len(parts) > 1 else ""
    if not shpp_no or not shpp_seq:
        return False, f"SSG shppNo/shppSeq 누락: {shipment_id!r}"

    if not tracking:
        return False, "운송장 번호 누락"

    client = SSGClient(api_key)
    delico_ven_id = client.get_courier_code(courier)
    if not delico_ven_id:
        return False, f"SSG 미등록 택배사: {courier!r} — delicoVenId 매핑 없음"

    qty = int(order.quantity or 1)
    try:
        await client.send_invoice(
            shpp_no=shpp_no,
            shpp_seq=shpp_seq,
            wbl_no=tracking,
            delico_ven_id=delico_ven_id,
        )
        await client.process_outbound(shpp_no=shpp_no, shpp_seq=shpp_seq, qty=qty)
        return True, "SSG 운송장 등록 및 출고처리 완료"
    except RuntimeError as e:
        return False, str(e)

"""SambaWave CS 문의 API router."""

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_read_session_dependency, get_write_session_dependency
from backend.dtos.samba.cs_inquiry import (
    CSInquiryBatchDelete,
    CSInquiryCreate,
    CSInquiryReply,
)

router = APIRouter(prefix="/cs-inquiries", tags=["samba-cs-inquiries"])


def _read_service(session: AsyncSession):
    from backend.domain.samba.cs_inquiry.repository import SambaCSInquiryRepository
    from backend.domain.samba.cs_inquiry.service import SambaCSInquiryService

    return SambaCSInquiryService(SambaCSInquiryRepository(session))


def _write_service(session: AsyncSession):
    from backend.domain.samba.cs_inquiry.repository import SambaCSInquiryRepository
    from backend.domain.samba.cs_inquiry.service import SambaCSInquiryService

    return SambaCSInquiryService(SambaCSInquiryRepository(session))


@router.get("/stats")
async def get_cs_stats(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """CS 문의 통계."""
    svc = _read_service(session)
    return await svc.get_stats()


@router.get("/templates")
async def get_reply_templates(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """CS 답변 템플릿 목록 (DB 저장분 + 기본 템플릿 병합)."""
    from backend.domain.samba.cs_inquiry.service import CS_REPLY_TEMPLATES
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository

    repo = SambaSettingsRepository(session)
    row = await repo.find_by_async(key="cs_reply_templates")
    db_templates = {}
    if row and isinstance(row.value, dict):
        db_templates = row.value
    # 기본 템플릿 + DB 템플릿 병합 (DB가 우선)
    merged = {**CS_REPLY_TEMPLATES, **db_templates}
    return merged


class TemplateBody(BaseModel):
    key: str
    name: str
    content: str


@router.post("/templates")
async def add_reply_template(
    body: TemplateBody,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """CS 답변 템플릿 추가/수정."""
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository
    from backend.domain.samba.forbidden.service import SambaForbiddenService
    from backend.domain.samba.forbidden.repository import SambaForbiddenWordRepository

    settings_repo = SambaSettingsRepository(session)
    svc = SambaForbiddenService(SambaForbiddenWordRepository(session), settings_repo)

    row = await settings_repo.find_by_async(key="cs_reply_templates")
    templates = {}
    if row and isinstance(row.value, dict):
        templates = row.value
    templates[body.key] = {"name": body.name, "content": body.content}
    await svc.save_setting("cs_reply_templates", templates)
    return {"ok": True, "key": body.key}


@router.delete("/templates/{template_key}")
async def delete_reply_template(
    template_key: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """CS 답변 템플릿 삭제."""
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository
    from backend.domain.samba.forbidden.service import SambaForbiddenService
    from backend.domain.samba.forbidden.repository import SambaForbiddenWordRepository

    settings_repo = SambaSettingsRepository(session)
    svc = SambaForbiddenService(SambaForbiddenWordRepository(session), settings_repo)

    row = await settings_repo.find_by_async(key="cs_reply_templates")
    templates = {}
    if row and isinstance(row.value, dict):
        templates = row.value
    if template_key in templates:
        del templates[template_key]
        await svc.save_setting("cs_reply_templates", templates)
    return {"ok": True}


@router.get("")
async def list_cs_inquiries(
    skip: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=500),
    market: Optional[str] = None,
    inquiry_type: Optional[str] = None,
    reply_status: Optional[str] = None,
    search: Optional[str] = None,
    sort_field: str = Query("inquiry_date"),
    sort_desc: bool = Query(True),
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """CS 문의 목록 (필터 + 페이지네이션)."""
    svc = _read_service(session)
    return await svc.list_inquiries(
        skip=skip,
        limit=limit,
        market=market,
        inquiry_type=inquiry_type,
        reply_status=reply_status,
        search=search,
        sort_field=sort_field,
        sort_desc=sort_desc,
        start_date=start_date,
        end_date=end_date,
    )


@router.post("", status_code=201)
async def create_cs_inquiry(
    body: CSInquiryCreate,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """CS 문의 수동 등록."""
    svc = _write_service(session)
    return await svc.create_inquiry(body.model_dump(exclude_unset=True))


class CSSyncBody(BaseModel):
    market_name: Optional[str] = None
    account_id: Optional[str] = None


@router.post("/sync-from-markets")
async def sync_cs_from_markets(
    body: Optional[CSSyncBody] = Body(default=None),
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """마켓에서 CS 문의 동기화. market_name 전달 시 해당 마켓만 동기화."""
    market_name = body.market_name if body else None
    account_id = body.account_id if body else None
    return await _do_sync_cs_from_markets(
        session, market_name=market_name, account_id=account_id
    )


@router.get("/{inquiry_id}")
async def get_cs_inquiry(
    inquiry_id: str,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """CS 문의 단건 조회."""
    svc = _read_service(session)
    inquiry = await svc.get_inquiry(inquiry_id)
    if not inquiry:
        raise HTTPException(status_code=404, detail="문의를 찾을 수 없습니다")
    return inquiry


@router.post("/{inquiry_id}/reply")
async def reply_cs_inquiry(
    inquiry_id: str,
    body: CSInquiryReply,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """CS 문의 답변 등록 — DB 저장 + 마켓 전송 통합."""
    import json
    import logging
    from sqlmodel import select
    from backend.domain.samba.forbidden.model import SambaSettings
    from backend.domain.samba.proxy.smartstore import SmartStoreClient

    logger = logging.getLogger(__name__)
    svc = _write_service(session)
    inquiry = await svc.get_inquiry(inquiry_id)
    if not inquiry:
        raise HTTPException(status_code=404, detail="문의를 찾을 수 없습니다")

    market_sent = False
    market_msg = ""
    answer_no = (
        inquiry.market_answer_no
        if inquiry.market_answer_no and inquiry.market_answer_no != "None"
        else ""
    )

    # 마켓 전송 시도 (market_inquiry_no가 있는 경우)
    if inquiry.market_inquiry_no:
        try:
            if inquiry.market == "스마트스토어":
                from backend.domain.samba.account.model import SambaMarketAccount

                client = None
                # inquiry.account_id → SambaMarketAccount 우선 조회
                if inquiry.account_id:
                    acc_result = await session.execute(
                        select(SambaMarketAccount).where(
                            SambaMarketAccount.id == inquiry.account_id,
                            SambaMarketAccount.is_active == True,  # noqa: E712
                        )
                    )
                    acc = acc_result.scalar_one_or_none()
                    if acc:
                        af = acc.additional_fields or {}
                        cid = af.get("clientId", "") or acc.api_key or ""
                        csec = af.get("clientSecret", "") or acc.api_secret or ""
                        if cid and csec:
                            client = SmartStoreClient(cid, csec)
                # account_id 없거나 SambaMarketAccount 미등록 → SambaSettings 폴백
                if client is None:
                    settings_result = await session.execute(
                        select(SambaSettings).where(
                            SambaSettings.key.like("store_smartstore%")
                        )
                    )
                    ss_settings = settings_result.scalars().first()
                    if ss_settings:
                        config = (
                            json.loads(ss_settings.value)
                            if isinstance(ss_settings.value, str)
                            else ss_settings.value
                        )
                        client = SmartStoreClient(
                            config["clientId"], config["clientSecret"]
                        )
                if client:
                    inq_no = int(inquiry.market_inquiry_no)

                    if inquiry.inquiry_type == "product_question":
                        result = await client.answer_product_qna(inq_no, body.reply)
                        market_sent = True
                        market_msg = "상품문의 답변 전송 완료"
                    else:
                        if inquiry.market_answer_no:
                            result = await client.update_inquiry_answer(
                                inq_no,
                                int(inquiry.market_answer_no),
                                body.reply,
                            )
                        else:
                            result = await client.answer_inquiry(inq_no, body.reply)
                        answer_data = (
                            result.get("data", {}) if isinstance(result, dict) else {}
                        )
                        new_answer_no = str(answer_data.get("inquiryCommentNo", ""))
                        if new_answer_no:
                            answer_no = new_answer_no
                        market_sent = True
                        market_msg = "고객문의 답변 전송 완료"
            elif inquiry.market == "롯데ON":
                from backend.domain.samba.proxy.lotteon import LotteonClient
                from backend.domain.samba.account.model import SambaMarketAccount

                lo_client = None
                # inquiry.account_id → SambaMarketAccount 우선 조회
                if inquiry.account_id:
                    acc_result = await session.execute(
                        select(SambaMarketAccount).where(
                            SambaMarketAccount.id == inquiry.account_id,
                            SambaMarketAccount.is_active == True,  # noqa: E712
                        )
                    )
                    acc = acc_result.scalar_one_or_none()
                    if acc:
                        af = acc.additional_fields or {}
                        api_key = af.get("apiKey", "") or acc.api_key or ""
                        if api_key:
                            lo_client = LotteonClient(api_key=api_key)
                # account_id 없거나 SambaMarketAccount 미등록 → SambaSettings 폴백
                if lo_client is None:
                    settings_result = await session.execute(
                        select(SambaSettings).where(
                            SambaSettings.key.like("store_lotteon%")
                        )
                    )
                    lo_settings = settings_result.scalars().first()
                    if lo_settings:
                        config = (
                            json.loads(lo_settings.value)
                            if isinstance(lo_settings.value, str)
                            else lo_settings.value
                        )
                        lo_client = LotteonClient(api_key=config["apiKey"])
                if lo_client:
                    await lo_client.test_auth()  # trGrpCd/trNo 획득 (필수)
                    inq_no = inquiry.market_inquiry_no or ""

                    if inq_no.startswith("PQNA_"):
                        # 상품 Q&A 답변
                        pqna_no = inq_no[5:]
                        await lo_client.reply_product_qna(pqna_no, body.reply)
                        market_sent = True
                        market_msg = "롯데ON 상품Q&A 답변 전송 완료"
                    elif inq_no.startswith("CNTC_"):
                        # 판매자 연락(Contact) 답변
                        cntc_no = inq_no[5:]
                        await lo_client.answer_contact(cntc_no, body.reply)
                        market_sent = True
                        market_msg = "롯데ON 판매자연락 답변 전송 완료"
                    elif inq_no.startswith("COMP_"):
                        # 보상 요청은 판매자센터에서 직접 처리 (답변 API 없음)
                        market_sent = False
                        market_msg = (
                            "롯데ON 보상요청은 판매자센터에서 직접 처리해주세요"
                        )
                    else:
                        # 일반 Q&A (Inquiry)
                        if inquiry.market_answer_no:
                            await lo_client.update_qna_answer(
                                inq_no, inquiry.market_answer_no, body.reply
                            )
                        else:
                            data = await lo_client.answer_qna(inq_no, body.reply)
                            new_ans_no = str(
                                data.get("ansNo", data.get("qnaAnsNo", ""))
                            )
                            if new_ans_no:
                                answer_no = new_ans_no
                        market_sent = True
                        market_msg = "롯데ON Q&A 답변 전송 완료"

            elif inquiry.market == "11번가":
                from backend.domain.samba.account.model import SambaMarketAccount
                from backend.domain.samba.proxy.elevenst import ElevenstClient

                acc_stmt = select(SambaMarketAccount).where(
                    SambaMarketAccount.market_type == "11st",
                    SambaMarketAccount.is_active == True,  # noqa: E712
                )
                acc_result = await session.execute(acc_stmt)
                elevenst_acc = acc_result.scalars().first()
                if elevenst_acc:
                    elevenst_extras = elevenst_acc.additional_fields or {}
                    elevenst_api_key = (
                        elevenst_extras.get("apiKey", "") or elevenst_acc.api_key or ""
                    )
                    if elevenst_api_key:
                        elevenst_client = ElevenstClient(elevenst_api_key)
                        if inquiry.inquiry_type == "urgent_inquiry":
                            # 긴급알리미는 답변 불가 → 확인처리(상태 03)로 처리
                            await elevenst_client.confirm_alimi(
                                inquiry.market_inquiry_no
                            )
                            market_sent = True
                            market_msg = "확인처리완료"
                        else:
                            prd_no = (
                                inquiry.market_answer_no
                                or inquiry.market_product_no
                                or ""
                            )
                            await elevenst_client.reply_qna(
                                inquiry.market_inquiry_no, prd_no, body.reply
                            )
                            market_sent = True
                            market_msg = "11번가 Q&A 답변 전송 완료"

            elif inquiry.market == "쿠팡":
                from backend.domain.samba.account.model import SambaMarketAccount
                from backend.domain.samba.proxy.coupang import CoupangClient

                cp_acc = None
                if inquiry.account_id:
                    acc_result = await session.execute(
                        select(SambaMarketAccount).where(
                            SambaMarketAccount.id == inquiry.account_id,
                            SambaMarketAccount.is_active == True,  # noqa: E712
                        )
                    )
                    cp_acc = acc_result.scalar_one_or_none()
                if cp_acc is None:
                    acc_result = await session.execute(
                        select(SambaMarketAccount).where(
                            SambaMarketAccount.market_type == "coupang",
                            SambaMarketAccount.is_active == True,  # noqa: E712
                        )
                    )
                    cp_acc = acc_result.scalars().first()
                if cp_acc:
                    af = cp_acc.additional_fields or {}
                    access_key = af.get("accessKey", "") or cp_acc.api_key or ""
                    secret_key = af.get("secretKey", "") or cp_acc.api_secret or ""
                    vendor_id = af.get("vendorId", "") or cp_acc.seller_id or ""
                    reply_by = (
                        af.get("replyBy", "")
                        or cp_acc.seller_id
                        or af.get("wingLoginId", "")
                        or af.get("loginId", "")
                        or ""
                    )
                    if access_key and secret_key and vendor_id and reply_by:
                        cp_client = CoupangClient(access_key, secret_key, vendor_id)
                        await cp_client.reply_inquiry(
                            inquiry_id=int(inquiry.market_inquiry_no),
                            content=body.reply,
                            reply_by=reply_by,
                        )
                        market_sent = True
                        market_msg = "쿠팡 CS 답변 전송 완료"
                    elif not reply_by:
                        market_msg = "쿠팡 Wing 로그인 ID 미설정 (계정 스토어 ID 입력 필요)"

            elif inquiry.market == "eBay":
                from backend.domain.samba.account.model import SambaMarketAccount
                from backend.domain.samba.proxy.ebay import EbayClient

                if inquiry.account_id:
                    acct_result = await session.execute(
                        select(SambaMarketAccount).where(
                            SambaMarketAccount.id == inquiry.account_id
                        )
                    )
                    acct = acct_result.scalar_one_or_none()
                    if acct:
                        extras = acct.additional_fields or {}
                        app_id = (
                            extras.get("clientId")
                            or extras.get("appId")
                            or acct.api_key
                            or ""
                        )
                        cert_id = (
                            extras.get("clientSecret")
                            or extras.get("certId")
                            or acct.api_secret
                            or ""
                        )
                        refresh_token = (
                            extras.get("oauthToken")
                            or extras.get("authToken", "")
                            or ""
                        )
                        if app_id and cert_id and refresh_token:
                            # ExternalMessageID 우선, 없으면 market_inquiry_no
                            ext_id = inquiry.market_answer_no or ""
                            if not ext_id:
                                mid = inquiry.market_inquiry_no or ""
                                if mid.startswith("msg_"):
                                    mid = mid[4:]
                                ext_id = mid
                            if ext_id:
                                ebay_client = EbayClient(
                                    app_id=app_id,
                                    dev_id="",
                                    cert_id=cert_id,
                                    refresh_token=refresh_token,
                                    sandbox=bool(extras.get("sandbox", False)),
                                )
                                await ebay_client.reply_message(
                                    parent_message_id=ext_id,
                                    text=body.reply,
                                    recipient=inquiry.questioner or "",
                                    item_id=inquiry.market_product_no or "",
                                )
                                market_sent = True
                                market_msg = "eBay 메시지 답장 전송 완료"
                            else:
                                market_msg = "eBay messageId 없음"
                        else:
                            market_msg = "eBay 인증정보 없음"

        except Exception as e:
            logger.warning(f"[CS답변] 마켓 전송 실패 (DB 저장은 진행): {e}")
            market_msg = f"마켓 전송 실패: {e}"

    # DB 저장: 마켓 전송 성공 시에만 replied로 마킹, 실패 시 답변 내용만 저장
    # market_inquiry_no 없는 문의(플레이오토 등)는 항상 replied로 마킹
    mark_replied = market_sent or not inquiry.market_inquiry_no
    updated = await svc.reply_inquiry(inquiry_id, body.reply, mark_replied=mark_replied)
    if answer_no and answer_no != (inquiry.market_answer_no or ""):
        from backend.domain.samba.cs_inquiry.repository import SambaCSInquiryRepository

        repo = SambaCSInquiryRepository(session)
        await repo.update_async(inquiry_id, market_answer_no=answer_no)

    return {
        **(updated.__dict__ if updated else {}),
        "market_sent": market_sent,
        "market_message": market_msg,
    }


async def _find_collected_product_by_market_product_no(
    session: AsyncSession,
    market_product_no: str,
    product_name: str = "",
) -> "dict | None":
    """마켓 상품번호로 수집상품을 찾아 연결 정보를 반환하는 공통 함수.

    1차: market_product_nos JSON 컬럼에서 해당 상품번호 검색 (PostgreSQL JSON 연산)
    2차(매칭 실패 시): product_name 끝 토큰이 4자리 이상 숫자면 site_product_id로 fallback 매칭
        — 우리 시스템은 등록 시 상품명 끝에 site_product_id를 붙이므로
        registered_accounts/market_product_nos가 NULL이거나 cleanup된 케이스도 매칭 가능

    Returns:
        { id, source_site, site_product_id, name, images, original_link, product_link } or None
    """
    from sqlalchemy import text as sa_text

    pid = source_site = site_product_id = name = None
    images = None

    # ── 1차: market_product_nos LIKE 매칭 (정수형 저장 케이스도 잡히도록 따옴표 제외) ──
    if market_product_no:
        # market_product_no 는 외부 (CS 게시물 본문) 에서 유래 — `%`/`_` 와일드카드를
        # 리터럴로 강제하기 위해 escape 후 ESCAPE '\\' 절 명시.
        from backend.core.sql_safe import escape_like

        sql = sa_text(
            "SELECT id, source_site, site_product_id, name, images "
            "FROM samba_collected_product "
            "WHERE market_product_nos::text LIKE :pattern ESCAPE '\\' "
            "LIMIT 1"
        )
        safe = escape_like(str(market_product_no))
        result = await session.execute(sql, {"pattern": f"%{safe}%"})
        row = result.fetchone()
        if row:
            pid, source_site, site_product_id, name, images = row

    # ── 2차: product_name 끝 site_product_id로 fallback 매칭 ──
    if pid is None and product_name:
        tokens = str(product_name).strip().split()
        if tokens:
            last = tokens[-1]
            if last.isdigit() and len(last) >= 4:
                sql2 = sa_text(
                    "SELECT id, source_site, site_product_id, name, images "
                    "FROM samba_collected_product "
                    "WHERE site_product_id = :sp_id "
                    "LIMIT 1"
                )
                result2 = await session.execute(sql2, {"sp_id": last})
                row2 = result2.fetchone()
                if row2:
                    pid, source_site, site_product_id, name, images = row2

    if pid is None:
        return None

    # 소싱처 URL 생성
    sourcing_urls = {
        "MUSINSA": f"https://www.musinsa.com/products/{site_product_id}",
        "KREAM": f"https://kream.co.kr/products/{site_product_id}",
        "FashionPlus": f"https://www.fashionplus.co.kr/goods/detail/{site_product_id}",
        "ABCmart": f"https://www.a-rt.com/product?prdtNo={site_product_id}",
        "GrandStage": f"https://www.a-rt.com/product?prdtNo={site_product_id}",
        "REXMONDE": f"https://www.okmall.com/products/detail/{site_product_id}",
        "LOTTEON": f"https://www.lotteon.com/p/product/{site_product_id}",
        "GSShop": f"https://www.gsshop.com/prd/prd.gs?prdid={site_product_id}",
        "ElandMall": f"https://www.elandmall.com/goods/goods.action?goodsNo={site_product_id}",
        "SSF": f"https://www.ssfshop.com/goods/{site_product_id}",
        "SSG": f"https://www.ssg.com/item/itemView.ssg?itemId={site_product_id}",
        "Nike": f"https://www.nike.com/kr/t/{site_product_id}",
        "Adidas": f"https://www.adidas.co.kr/{site_product_id}.html",
    }
    original_link = sourcing_urls.get(source_site, "")

    # 대표 이미지
    thumb = ""
    if images and isinstance(images, list) and len(images) > 0:
        thumb = images[0]

    return {
        "id": pid,
        "source_site": source_site,
        "site_product_id": site_product_id,
        "name": name,
        "images": images,
        "original_link": original_link,
        "product_image": thumb,
    }


def _build_market_product_url(
    market: str, product_no: str, store_slug: str = ""
) -> str:
    """마켓별 상품 판매 페이지 URL 생성. 모든 마켓 공통."""
    urls = {
        "스마트스토어": f"https://smartstore.naver.com/{store_slug}/products/{product_no}"
        if store_slug
        else "",
        "쿠팡": f"https://www.coupang.com/vp/products/{product_no}",
        "11번가": f"https://www.11st.co.kr/products/{product_no}",
        "롯데ON": f"https://www.lotteon.com/product/{product_no}",
        "SSG": f"https://www.ssg.com/item/itemView.ssg?itemId={product_no}",
        "롯데홈쇼핑": f"https://www.lotteimall.com/product/{product_no}",
        "GS샵": f"https://www.gsshop.com/prd/prd.gs?prdid={product_no}",
        "KREAM": f"https://kream.co.kr/products/{product_no}",
        "Toss": f"https://toss.im/shopping/product/{product_no}",
    }
    return urls.get(market, "")


async def _sync_lotteon_qna(
    client: "Any",
    session: "AsyncSession",
    svc: "Any",
    account_name: str,
) -> int:
    """롯데ON Q&A 동기화 내부 함수.

    sync_cs_from_markets()의 가독성 확보를 위해 분리.
    각 롯데ON 계정의 Q&A를 조회하고 DB에 저장한다.

    Returns:
        새로 동기화된 문의 건수
    """
    import logging
    from sqlmodel import select
    from backend.domain.samba.cs_inquiry.model import SambaCSInquiry

    logger = logging.getLogger(__name__)
    synced = 0

    items = await client.get_qna_list(days=30)

    # 롯데ON vocLcsfCd → inquiry_type 매핑 (실제 API 문서 기준)
    VOC_TYPE_MAP = {
        "IC00000263": "exchange_return",  # 교환/반품/AS (고객문의)
        "IC00000264": "delivery",  # 배송 (고객문의)
        "IC00000312": "general",  # 회원정보
        "IC00000313": "general",  # 이벤트/프로모션
        "IC00000316": "product_question",  # 상품 (고객문의)
        "IC00000597": "general",  # 주문/결제
        "IC00000618": "exchange_return",  # 환불일정
        "IC00000619": "general",  # 오류
        "IC00000620": "general",  # 사이트이용/개선
        "IC00000621": "exchange_return",  # 취소
        "IC00000265": "product_question",  # 상품 (판매자문의)
        "IC00000266": "delivery",  # 배송 (판매자문의)
        "IC00000267": "exchange_return",  # 교환/반품/AS (판매자문의)
    }

    for item in items:
        # 판매자문의번호 (slrInqNo)
        qna_no = str(item.get("slrInqNo", ""))
        if not qna_no or qna_no == "0":
            continue

        # 중복 체크
        existing = await session.execute(
            select(SambaCSInquiry).where(
                SambaCSInquiry.market == "롯데ON",
                SambaCSInquiry.market_inquiry_no == qna_no,
            )
        )
        if existing.scalar_one_or_none():
            continue

        # 상품번호 (pdNo)
        market_product_no = str(item.get("pdNo", "") or "")

        # 답변 여부 (slrInqProcStatCd: ANS=답변, UNANS=미답변)
        is_answered = item.get("slrInqProcStatCd", "UNANS") == "ANS"
        reply_content = str(item.get("ansCnts", "") or "")

        # 문의 유형 코드 매핑 (vocLcsfCd)
        voc_cd = str(item.get("vocLcsfCd", ""))
        inquiry_type = VOC_TYPE_MAP.get(voc_cd, "general")

        # 접수일시 파싱 (accpDttm: yyyyMMddHHmmss)
        raw_date = str(item.get("accpDttm", "") or "")
        parsed_date = None
        if raw_date and len(raw_date) >= 8:
            try:
                from datetime import datetime as _dt

                parsed_date = _dt.strptime(raw_date[:14], "%Y%m%d%H%M%S")
            except Exception:
                try:
                    from dateutil.parser import parse as parse_dt

                    parsed_date = parse_dt(raw_date)
                except Exception:
                    parsed_date = None

        # 수집 상품 매칭
        _pd_nm = str(item.get("pdNm", "") or "")
        matched = await _find_collected_product_by_market_product_no(
            session, market_product_no, _pd_nm
        )
        product_link = (
            _build_market_product_url("롯데ON", market_product_no)
            if market_product_no
            else ""
        )

        inquiry_data = {
            "market": "롯데ON",
            "market_inquiry_no": qna_no,
            "market_answer_no": None,  # 롯데ON은 답변번호 없음 (문의번호로 식별)
            "market_order_id": str(item.get("odNo", "") or "") or None,
            "market_product_no": market_product_no or None,
            "account_name": account_name,
            "inquiry_type": inquiry_type,
            "questioner": str(
                item.get("slrNo", "") or ""
            ),  # 판매자번호 (구매자 ID 미제공)
            "product_name": str(item.get("pdNm", "") or ""),
            "product_image": matched["product_image"] if matched else "",
            "product_link": product_link,
            "original_link": matched["original_link"] if matched else "",
            "collected_product_id": matched["id"] if matched else None,
            "content": str(item.get("inqCnts", "") or ""),
            "reply": reply_content if is_answered else None,
            "reply_status": "replied" if is_answered else "pending",
            "inquiry_date": parsed_date,
            "replied_at": None,
        }

        await svc.create_inquiry(inquiry_data)
        synced += 1

    logger.info(
        f"[CS동기화] 롯데ON({account_name}) Q&A: {len(items)}건 조회, {synced}건 동기화"
    )
    return synced


async def _sync_lotteon_product_qna(
    client: "Any",
    session: "AsyncSession",
    svc: "Any",
    account_name: str,
) -> int:
    """롯데ON 상품 Q&A 동기화.

    GET /v1/openapi/product/v1/product/qna/list 결과를 DB에 저장한다.
    market_inquiry_no = "PQNA_{qnaNo}" 형태로 저장하여 판매자문의와 구분.

    Returns:
        새로 동기화된 문의 건수
    """
    import logging
    from sqlmodel import select
    from backend.domain.samba.cs_inquiry.model import SambaCSInquiry

    logger = logging.getLogger(__name__)
    synced = 0

    items = await client.get_product_qna_list(days=30)

    for item in items:
        qna_no = str(item.get("qnaNo", "") or "")
        if not qna_no or qna_no == "0":
            continue

        # PQNA_ 접두사로 판매자문의(slrInqNo)와 구분
        market_inquiry_no = f"PQNA_{qna_no}"

        # 중복 체크
        existing = await session.execute(
            select(SambaCSInquiry).where(
                SambaCSInquiry.market == "롯데ON",
                SambaCSInquiry.market_inquiry_no == market_inquiry_no,
            )
        )
        if existing.scalar_one_or_none():
            continue

        market_product_no = str(item.get("pdNo", "") or "")

        # 답변 여부 (ansStatCd: ANS=답변완료, UNANS=미답변)
        ans_stat = str(item.get("ansStatCd", "UNANS") or "UNANS")
        is_answered = ans_stat == "ANS"
        reply_content = str(item.get("ansCnts", "") or "")

        # 등록일시 파싱 (regDttm: yyyyMMddHHmmss)
        raw_date = str(item.get("regDttm", "") or "")
        parsed_date = None
        if raw_date and len(raw_date) >= 8:
            try:
                from datetime import datetime as _dt

                parsed_date = _dt.strptime(raw_date[:14], "%Y%m%d%H%M%S")
            except Exception:
                try:
                    from dateutil.parser import parse as parse_dt

                    parsed_date = parse_dt(raw_date)
                except Exception:
                    parsed_date = None

        # 수집 상품 매칭
        _pd_nm = str(item.get("pdNm", "") or "")
        matched = await _find_collected_product_by_market_product_no(
            session, market_product_no, _pd_nm
        )
        product_link = (
            _build_market_product_url("롯데ON", market_product_no)
            if market_product_no
            else ""
        )

        inquiry_data = {
            "market": "롯데ON",
            "market_inquiry_no": market_inquiry_no,
            "market_answer_no": None,
            "market_order_id": None,
            "market_product_no": market_product_no or None,
            "account_name": account_name,
            "inquiry_type": "product_question",  # 상품 Q&A는 항상 product_question
            "questioner": str(item.get("buyerId", "") or ""),
            "product_name": str(item.get("pdNm", "") or ""),
            "product_image": matched["product_image"] if matched else "",
            "product_link": product_link,
            "original_link": matched["original_link"] if matched else "",
            "collected_product_id": matched["id"] if matched else None,
            "content": str(item.get("qnaCnts", "") or ""),
            "reply": reply_content if is_answered else None,
            "reply_status": "replied" if is_answered else "pending",
            "inquiry_date": parsed_date,
            "replied_at": None,
        }

        await svc.create_inquiry(inquiry_data)
        synced += 1

    logger.info(
        f"[CS동기화] 롯데ON({account_name}) 상품Q&A: {len(items)}건 조회, {synced}건 동기화"
    )
    return synced


async def _sync_lotteon_contact(
    client: "Any",
    session: "AsyncSession",
    svc: "Any",
    account_name: str,
) -> int:
    """롯데ON 판매자 연락(Contact) 동기화.

    getSellerContactList API → CS DB 저장.
    market_inquiry_no = "CNTC_{cntcNo}" 형태로 저장하여 Inquiry와 구분.
    """
    import logging
    from sqlmodel import select
    from backend.domain.samba.cs_inquiry.model import SambaCSInquiry

    logger = logging.getLogger(__name__)
    synced = 0

    items = await client.get_contact_list(days=30)

    VOC_TYPE_MAP = {
        "IC00000263": "exchange_return",
        "IC00000264": "delivery",
        "IC00000265": "product_question",
        "IC00000266": "delivery",
        "IC00000267": "exchange_return",
        "IC00000312": "general",
        "IC00000316": "product_question",
        "IC00000597": "general",
        "IC00000618": "exchange_return",
        "IC00000621": "exchange_return",
    }

    for item in items:
        # 연락번호 — 실제 필드명 다를 수 있어 여러 키 체크
        raw_no = item.get("cntcNo") or item.get("contNo") or item.get("contactNo") or ""
        cntc_no = str(raw_no)
        if not cntc_no or cntc_no == "0":
            logger.debug(f"[CS동기화][롯데ON][Contact] 연락번호 없음: {item}")
            continue

        market_inq_no = f"CNTC_{cntc_no}"

        existing = await session.execute(
            select(SambaCSInquiry).where(
                SambaCSInquiry.market == "롯데ON",
                SambaCSInquiry.market_inquiry_no == market_inq_no,
            )
        )
        if existing.scalar_one_or_none():
            continue

        # 내용 — 여러 필드명 시도
        content = str(
            item.get("cntcCnts")
            or item.get("contCnts")
            or item.get("cnts")
            or item.get("contents")
            or item.get("content")
            or ""
        )
        if not content:
            content = "내용 없음"

        proc_stat = str(
            item.get("procStatCd") or item.get("slrInqProcStatCd") or "UNANS"
        )
        is_answered = proc_stat == "ANS"
        reply_content = str(item.get("ansCnts") or "")

        voc_cd = str(item.get("vocLcsfCd") or "")
        inquiry_type = VOC_TYPE_MAP.get(voc_cd, "general")

        market_product_no = str(item.get("pdNo") or "")
        od_no = str(item.get("odNo") or "")

        raw_date = str(item.get("accpDttm") or item.get("regDttm") or "")
        parsed_date = None
        if raw_date and len(raw_date) >= 8:
            try:
                from datetime import datetime as _dt

                parsed_date = _dt.strptime(raw_date[:14], "%Y%m%d%H%M%S")
            except Exception:
                pass

        _pd_nm = str(item.get("pdNm") or "")
        matched = await _find_collected_product_by_market_product_no(
            session, market_product_no, _pd_nm
        )
        product_link = (
            _build_market_product_url("롯데ON", market_product_no)
            if market_product_no
            else ""
        )

        inquiry_data = {
            "market": "롯데ON",
            "market_inquiry_no": market_inq_no,
            "market_answer_no": None,
            "market_order_id": od_no or None,
            "market_product_no": market_product_no or None,
            "account_name": account_name,
            "inquiry_type": inquiry_type,
            "questioner": str(item.get("mbId") or item.get("custId") or ""),
            "product_name": str(item.get("pdNm") or ""),
            "product_image": matched["product_image"] if matched else "",
            "product_link": product_link,
            "original_link": matched["original_link"] if matched else "",
            "collected_product_id": matched["id"] if matched else None,
            "content": content,
            "reply": reply_content if is_answered else None,
            "reply_status": "replied" if is_answered else "pending",
            "inquiry_date": parsed_date,
            "replied_at": None,
        }
        await svc.create_inquiry(inquiry_data)
        synced += 1

    logger.info(
        f"[CS동기화] 롯데ON({account_name}) 판매자연락: {len(items)}건 조회, {synced}건 동기화"
    )
    return synced


async def _sync_lotteon_compensate(
    client: "Any",
    session: "AsyncSession",
    svc: "Any",
    account_name: str,
) -> int:
    """롯데ON 보상 요청(Compensate) 동기화.

    getSellerCompensateList API → CS DB 저장.
    market_inquiry_no = "COMP_{compNo}" 형태로 저장.
    """
    import logging
    from sqlmodel import select
    from backend.domain.samba.cs_inquiry.model import SambaCSInquiry

    logger = logging.getLogger(__name__)
    synced = 0

    items = await client.get_compensate_list(days=30)

    for item in items:
        raw_no = (
            item.get("compNo") or item.get("compensateNo") or item.get("cmpNo") or ""
        )
        comp_no = str(raw_no)
        if not comp_no or comp_no == "0":
            logger.debug(f"[CS동기화][롯데ON][Compensate] 보상번호 없음: {item}")
            continue

        market_inq_no = f"COMP_{comp_no}"

        existing = await session.execute(
            select(SambaCSInquiry).where(
                SambaCSInquiry.market == "롯데ON",
                SambaCSInquiry.market_inquiry_no == market_inq_no,
            )
        )
        if existing.scalar_one_or_none():
            continue

        content = str(
            item.get("compCnts")
            or item.get("cmpCnts")
            or item.get("cnts")
            or item.get("contents")
            or item.get("content")
            or ""
        )
        if not content:
            content = "보상 요청"

        proc_stat = str(item.get("procStatCd") or "UNANS")
        is_answered = proc_stat == "ANS"
        reply_content = str(item.get("ansCnts") or "")

        market_product_no = str(item.get("pdNo") or "")
        od_no = str(item.get("odNo") or "")

        raw_date = str(item.get("accpDttm") or item.get("regDttm") or "")
        parsed_date = None
        if raw_date and len(raw_date) >= 8:
            try:
                from datetime import datetime as _dt

                parsed_date = _dt.strptime(raw_date[:14], "%Y%m%d%H%M%S")
            except Exception:
                pass

        _pd_nm = str(item.get("pdNm") or "")
        matched = await _find_collected_product_by_market_product_no(
            session, market_product_no, _pd_nm
        )
        product_link = (
            _build_market_product_url("롯데ON", market_product_no)
            if market_product_no
            else ""
        )

        inquiry_data = {
            "market": "롯데ON",
            "market_inquiry_no": market_inq_no,
            "market_answer_no": None,
            "market_order_id": od_no or None,
            "market_product_no": market_product_no or None,
            "account_name": account_name,
            "inquiry_type": "exchange_return",  # 보상은 교환/반품 유형으로 분류
            "questioner": str(item.get("mbId") or item.get("custId") or ""),
            "product_name": str(item.get("pdNm") or ""),
            "product_image": matched["product_image"] if matched else "",
            "product_link": product_link,
            "original_link": matched["original_link"] if matched else "",
            "collected_product_id": matched["id"] if matched else None,
            "content": content,
            "reply": reply_content if is_answered else None,
            "reply_status": "replied" if is_answered else "pending",
            "inquiry_date": parsed_date,
            "replied_at": None,
        }
        await svc.create_inquiry(inquiry_data)
        synced += 1

    logger.info(
        f"[CS동기화] 롯데ON({account_name}) 보상요청: {len(items)}건 조회, {synced}건 동기화"
    )
    return synced


async def _do_sync_cs_from_markets(
    session: AsyncSession,
    market_name: Optional[str] = None,
    account_id: Optional[str] = None,
):
    """마켓에서 CS 문의 동기화 실제 구현체.

    market_name 전달 시 해당 마켓만 동기화 (예: "롯데ON", "스마트스토어"), 없으면 전체 동기화.
    """
    import logging
    from datetime import datetime, timedelta, timezone
    from sqlmodel import select
    from backend.domain.samba.forbidden.model import SambaSettings
    from backend.domain.samba.proxy.smartstore import SmartStoreClient
    from backend.domain.samba.cs_inquiry.model import SambaCSInquiry

    logger = logging.getLogger(__name__)
    svc = _write_service(session)
    sync_started_at = datetime.now(timezone.utc)
    synced = 0
    errors = []
    result_labels: list[str] = []
    target_account = None
    target_market_type = None
    if account_id:
        from backend.domain.samba.account.model import SambaMarketAccount

        account_result = await session.execute(
            select(SambaMarketAccount).where(
                SambaMarketAccount.id == account_id,
                SambaMarketAccount.is_active == True,  # noqa: E712
            )
        )
        target_account = account_result.scalar_one_or_none()
        if not target_account:
            raise HTTPException(404, "CS sync account not found")
        target_market_type = (target_account.market_type or "").lower()
        if not market_name:
            market_name = target_account.market_name
        result_labels = [
            target_account.account_label
            or target_account.seller_id
            or target_account.business_name
            or target_account.market_name
            or account_id
        ]
    elif market_name:
        try:
            from backend.domain.samba.account.model import SambaMarketAccount

            label_rows = await session.execute(
                select(SambaMarketAccount).where(
                    SambaMarketAccount.market_name == market_name,
                    SambaMarketAccount.is_active == True,  # noqa: E712
                )
            )
            result_labels = [
                acc.account_label
                or acc.seller_id
                or acc.business_name
                or acc.market_name
                for acc in label_rows.scalars().all()
            ]
        except Exception:
            result_labels = []
    else:
        try:
            from backend.domain.samba.account.model import SambaMarketAccount

            label_rows = await session.execute(
                select(SambaMarketAccount).where(
                    SambaMarketAccount.is_active == True  # noqa: E712
                )
            )
            result_labels = [
                acc.account_label
                or acc.seller_id
                or acc.business_name
                or acc.market_name
                for acc in label_rows.scalars().all()
            ]
        except Exception:
            result_labels = []

    # 스마트스토어 계정 조회 — SambaMarketAccount 1차, SambaSettings 레거시 폴백
    ss_settings: list[Any] = []
    if account_id and target_market_type == "smartstore" and target_account is not None:
        # 단일 계정 지정
        ss_settings = [target_account]
    elif not market_name or market_name == "스마트스토어":
        try:
            from backend.domain.samba.account.model import SambaMarketAccount

            ss_acc_result = await session.execute(
                select(SambaMarketAccount).where(
                    SambaMarketAccount.market_type == "smartstore",
                    SambaMarketAccount.is_active == True,  # noqa: E712
                )
            )
            ss_settings = list(ss_acc_result.scalars().all())
        except Exception as e:
            logger.warning(f"[CS동기화] 스마트스토어 SambaMarketAccount 조회 실패: {e}")

        # SambaMarketAccount가 비어있을 때만 레거시 SambaSettings 폴백
        if not ss_settings:
            try:
                settings_result = await session.execute(
                    select(SambaSettings).where(
                        SambaSettings.key.like("store_smartstore%")
                    )
                )
                ss_settings = list(settings_result.scalars().all())
            except Exception as e:
                raise HTTPException(500, f"설정 조회 실패: {e}")

    _ss_slug_map: dict[str, str] = {}
    try:
        from backend.domain.samba.account.model import SambaMarketAccount

        _ss_acc_result = await session.execute(
            select(SambaMarketAccount).where(
                SambaMarketAccount.market_type == "smartstore",
                SambaMarketAccount.is_active == True,  # noqa: E712
            )
        )
        for _acc in _ss_acc_result.scalars().all():
            _af = _acc.additional_fields or {}
            _cid = _af.get("clientId", "") or _acc.api_key or ""
            _slug = _af.get("storeSlug", "")
            if _cid and _slug:
                _ss_slug_map[_cid] = _slug
    except Exception:
        pass  # 슬러그 조회 실패 시 기존 로직으로 폴백

    for setting in ss_settings:
        try:
            import json

            if hasattr(setting, "additional_fields"):
                config = dict(setting.additional_fields or {})
                client_id = config.get("clientId", "") or setting.api_key or ""
                client_secret = (
                    config.get("clientSecret", "") or setting.api_secret or ""
                )
                account_name = (
                    setting.account_label
                    or setting.business_name
                    or setting.seller_id
                    or ""
                )
                sync_account_id = setting.id
            else:
                config = (
                    json.loads(setting.value)
                    if isinstance(setting.value, str)
                    else setting.value
                )
                config = config or {}
                client_id = config.get("clientId", "")
                client_secret = config.get("clientSecret", "")
                account_name = config.get("businessName", "") or config.get(
                    "storeId", ""
                )
                sync_account_id = None
            # storeSlug 우선순위: SambaMarketAccount > settings.storeSlug
            # settings.storeId가 이메일 형태이면 슬러그로 사용 불가
            _raw_slug = config.get("storeSlug", "") or config.get("storeId", "")
            store_slug = _ss_slug_map.get(client_id) or (
                _raw_slug if _raw_slug and "@" not in _raw_slug else ""
            )

            if not client_id or not client_secret:
                continue

            client = SmartStoreClient(client_id, client_secret)

            # 최근 30일 문의 조회 (KST 기준 ISO 8601)
            from zoneinfo import ZoneInfo

            kst = ZoneInfo("Asia/Seoul")
            now_kst = datetime.now(kst)
            end_date = (now_kst + timedelta(days=1)).strftime(
                "%Y-%m-%dT00:00:00.000+09:00"
            )
            start_date = (now_kst - timedelta(days=30)).strftime(
                "%Y-%m-%dT00:00:00.000+09:00"
            )

            result = await client.get_inquiries(
                from_date=start_date,
                to_date=end_date,
                size=100,
            )

            # 응답 구조 파싱
            data = result.get("data", result)
            contents = []
            if isinstance(data, dict):
                contents = data.get("contents", []) or data.get("content", [])
                if not contents:
                    for key in data:
                        val = data[key]
                        if isinstance(val, list) and val:
                            contents = val
                            break
            elif isinstance(data, list):
                contents = data

            for item in contents:
                inquiry_no = str(
                    item.get("questionId", item.get("inquiryNo", item.get("id", "")))
                )
                if not inquiry_no:
                    continue

                # 중복 체크
                existing = await session.execute(
                    select(SambaCSInquiry).where(
                        SambaCSInquiry.market == "스마트스토어",
                        SambaCSInquiry.market_inquiry_no == inquiry_no,
                        SambaCSInquiry.account_id == sync_account_id,
                    )
                )
                if existing.scalar_one_or_none():
                    continue

                inquiry_type = "product_question"
                is_answered = item.get("answered", False)
                reply_content = item.get("answer", "")

                # inquiry_date 문자열 → datetime 변환
                raw_date = item.get("createDate", None)
                parsed_date = None
                if raw_date:
                    try:
                        from dateutil.parser import parse as parse_dt

                        parsed_date = parse_dt(raw_date)
                    except Exception:
                        parsed_date = None

                # 마켓 상품번호로 수집상품 매칭 (스마트스토어 qnas 응답 다양한 필드 대응)
                # /v1/contents/qnas 는 보통 channelProductNo / originProductNo 를 반환
                market_product_no = str(
                    item.get("channelProductNo")
                    or item.get("smartstoreChannelProductNo")
                    or item.get("productId")
                    or item.get("productNo")
                    or item.get("originProductNo")
                    or ""
                )
                _pd_nm = str(item.get("productName", "") or "")
                matched = await _find_collected_product_by_market_product_no(
                    session, market_product_no, _pd_nm
                )

                product_link = (
                    _build_market_product_url(
                        "스마트스토어", market_product_no, store_slug
                    )
                    if market_product_no
                    else ""
                )

                inquiry_data = {
                    "market": "스마트스토어",
                    "market_inquiry_no": inquiry_no,
                    "market_answer_no": None,
                    "market_order_id": None,
                    "market_product_no": market_product_no or None,
                    "account_id": sync_account_id,
                    "account_name": account_name,
                    "inquiry_type": inquiry_type,
                    "questioner": item.get("maskedWriterId", ""),
                    "product_name": item.get("productName", ""),
                    "product_image": matched["product_image"] if matched else "",
                    "product_link": product_link,
                    "original_link": matched["original_link"] if matched else "",
                    "collected_product_id": matched["id"] if matched else None,
                    "content": item.get("question", ""),
                    "reply": reply_content if is_answered else None,
                    "reply_status": "replied" if is_answered else "pending",
                    "inquiry_date": parsed_date,
                    "replied_at": None,
                }

                await svc.create_inquiry(inquiry_data)
                synced += 1

            logger.info(
                f"[CS동기화] 스마트스토어({account_name}) 상품문의: {len(contents)}건 조회, {synced}건 동기화"
            )

            # ── 고객문의 (구매 후 1:1 문의, /v1/pay-user/inquiries) ──
            try:
                # LocalDate 형식 (YYYY-MM-DD)
                start_local = (now_kst - timedelta(days=90)).strftime("%Y-%m-%d")
                end_local = (now_kst + timedelta(days=1)).strftime("%Y-%m-%d")

                purchase_result = await client.get_purchase_inquiries(
                    start_date=start_local,
                    end_date=end_local,
                    size=100,
                )
                p_data = purchase_result.get("data", purchase_result)
                p_contents = []
                if isinstance(p_data, dict):
                    p_contents = p_data.get("content", []) or p_data.get("contents", [])
                    if not p_contents:
                        for key in p_data:
                            val = p_data[key]
                            if isinstance(val, list) and val:
                                p_contents = val
                                break
                elif isinstance(p_data, list):
                    p_contents = p_data

                for item in p_contents:
                    inq_no = str(item.get("inquiryNo", item.get("id", "")))
                    if not inq_no:
                        continue

                    existing = await session.execute(
                        select(SambaCSInquiry).where(
                            SambaCSInquiry.market == "스마트스토어",
                            SambaCSInquiry.market_inquiry_no == inq_no,
                            SambaCSInquiry.account_id == sync_account_id,
                        )
                    )
                    if existing.scalar_one_or_none():
                        continue

                    # 문의 유형 (category 필드: 배송, 교환/반품 등)
                    category_raw = item.get(
                        "category", item.get("inquiryType", "general")
                    )
                    type_map = {
                        "배송": "delivery",
                        "교환/반품": "exchange_return",
                        "교환": "exchange_return",
                        "반품": "exchange_return",
                        "취소": "exchange_return",
                        "상품": "product",
                        "DELIVERY": "delivery",
                        "EXCHANGE_RETURN": "exchange_return",
                        "CANCEL": "exchange_return",
                        "ETC": "general",
                    }
                    mapped_type = type_map.get(str(category_raw), "general")

                    is_answered = item.get("answered", False)
                    reply_content = item.get("answerContent", "") or ""

                    raw_date = item.get("inquiryRegistrationDateTime", None)
                    parsed_date = None
                    if raw_date:
                        try:
                            from dateutil.parser import parse as parse_dt

                            parsed_date = parse_dt(raw_date)
                        except Exception:
                            parsed_date = None

                    # /v1/pay-user/inquiries 응답 필드 다양성 대응
                    mpno = str(
                        item.get("channelProductNo")
                        or item.get("smartstoreChannelProductNo")
                        or item.get("productNo")
                        or item.get("originProductNo")
                        or item.get("productId")
                        or ""
                    )
                    _pd_nm = str(item.get("productName", "") or "")
                    matched = await _find_collected_product_by_market_product_no(
                        session, mpno, _pd_nm
                    )
                    product_link = (
                        _build_market_product_url("스마트스토어", mpno, store_slug)
                        if mpno
                        else ""
                    )

                    # market_order_id 결정: 주문관리(samba_order.order_number)가
                    # 상품주문번호(productOrderId)로 저장되므로 검색/연결을 위해
                    # productOrderIdList[0]을 우선 사용하고, 없으면 orderId로 폴백
                    _po_list = item.get("productOrderIdList")
                    if isinstance(_po_list, list) and _po_list:
                        _market_order_id = str(_po_list[0])
                    elif isinstance(_po_list, str) and _po_list:
                        _market_order_id = _po_list
                    else:
                        _market_order_id = str(item.get("orderId", "")) or None

                    inquiry_data = {
                        "market": "스마트스토어",
                        "market_inquiry_no": inq_no,
                        "market_answer_no": str(item["answerContentId"])
                        if item.get("answerContentId")
                        else None,
                        "market_order_id": _market_order_id,
                        "market_product_no": mpno or None,
                        "account_id": sync_account_id,
                        "account_name": account_name,
                        "inquiry_type": mapped_type,
                        "questioner": item.get(
                            "customerId", item.get("customerName", "")
                        ),
                        "product_name": item.get("productName", ""),
                        "product_image": matched["product_image"] if matched else "",
                        "product_link": product_link,
                        "original_link": matched["original_link"] if matched else "",
                        "collected_product_id": matched["id"] if matched else None,
                        "content": item.get(
                            "inquiryContent",
                            item.get("question", item.get("content", "")),
                        ),
                        "reply": reply_content if is_answered else None,
                        "reply_status": "replied" if is_answered else "pending",
                        "inquiry_date": parsed_date,
                        "replied_at": None,
                    }

                    await svc.create_inquiry(inquiry_data)
                    synced += 1

                logger.info(
                    f"[CS동기화] 스마트스토어({account_name}) 구매문의: {len(p_contents)}건 조회"
                )
            except Exception as e:
                logger.warning(f"[CS동기화] 스마트스토어 구매문의 조회 실패: {e}")
                errors.append(f"스마트스토어({account_name}) 구매문의: {str(e)}")

        except Exception as e:
            logger.error(f"[CS동기화] 스마트스토어 동기화 실패: {e}")
            errors.append(str(e))

    # eBay CS 동기화 (Post-Order inquiry + Trading GetMyMessages)
    if not market_name or market_name == "eBay":
        try:
            from backend.domain.samba.account.model import SambaMarketAccount
            from backend.domain.samba.proxy.ebay import EbayClient

            ebay_accts_result = await session.execute(
                select(SambaMarketAccount).where(
                    SambaMarketAccount.market_type == "ebay",
                    SambaMarketAccount.is_active == True,  # noqa: E712
                )
            )
            ebay_accts = ebay_accts_result.scalars().all()
            for acct in ebay_accts:
                extras = acct.additional_fields or {}
                app_id = extras.get("clientId") or extras.get("appId") or acct.api_key
                cert_id = (
                    extras.get("clientSecret")
                    or extras.get("certId")
                    or acct.api_secret
                )
                refresh_token = extras.get("oauthToken") or extras.get("authToken", "")
                # store_ebay 폴백
                if not (app_id and cert_id and refresh_token):
                    row_result = await session.execute(
                        select(SambaSettings).where(SambaSettings.key == "store_ebay")
                    )
                    row = row_result.scalar_one_or_none()
                    if row and isinstance(row.value, dict):
                        app_id = (
                            app_id
                            or row.value.get("clientId", "")
                            or row.value.get("appId", "")
                        )
                        cert_id = (
                            cert_id
                            or row.value.get("clientSecret", "")
                            or row.value.get("certId", "")
                        )
                        refresh_token = (
                            refresh_token
                            or row.value.get("oauthToken", "")
                            or row.value.get("authToken", "")
                        )
                if not (app_id and cert_id and refresh_token):
                    continue

                client = EbayClient(
                    app_id=app_id,
                    dev_id="",
                    cert_id=cert_id,
                    refresh_token=refresh_token,
                    sandbox=bool(extras.get("sandbox", False)),
                )

                # ① INR 분쟁 문의 수집 (Post-Order inquiry)
                try:
                    inquiries = await client.get_inquiries(days=90)
                except Exception as e:
                    logger.warning(
                        f"[CS동기화] eBay({acct.market_name}) 문의 조회 실패: {e}"
                    )
                    inquiries = []

                inq_count = 0
                for inq in inquiries:
                    inq_id = str(inq.get("inquiryId", "") or "")
                    if not inq_id:
                        continue
                    exists_q = await session.execute(
                        select(SambaCSInquiry).where(
                            SambaCSInquiry.market == "eBay",
                            SambaCSInquiry.market_inquiry_no == inq_id,
                        )
                    )
                    if exists_q.scalar_one_or_none():
                        continue
                    item_id = str(inq.get("itemId", "") or "")
                    buyer = (inq.get("buyer") or {}).get("userId", "") or ""
                    creation = inq.get("creationDate")
                    if isinstance(creation, dict):
                        creation = creation.get("value", "")
                    inquiry_dt = None
                    if isinstance(creation, str) and creation:
                        try:
                            inquiry_dt = datetime.fromisoformat(
                                creation.replace("Z", "+00:00")
                            )
                        except Exception:
                            inquiry_dt = None
                    session.add(
                        SambaCSInquiry(
                            market="eBay",
                            market_inquiry_no=inq_id,
                            market_order_id=str(inq.get("orderId", "") or ""),
                            market_product_no=item_id,
                            account_id=acct.id,
                            account_name=acct.market_name,
                            inquiry_type="order_inquiry",
                            questioner=buyer,
                            product_name="",
                            content=str(inq.get("reason", "") or "INR"),
                            reply="",
                            reply_status="pending",
                            inquiry_date=inquiry_dt,
                        )
                    )
                    inq_count += 1

                # ② 구매자-판매자 메시지 수집 (Trading API GetMyMessages)
                try:
                    messages = await client.get_my_messages(days=90)
                except Exception as e:
                    logger.warning(
                        f"[CS동기화] eBay({acct.market_name}) 메시지 조회 실패: {e}"
                    )
                    messages = []

                msg_count = 0
                for m in messages:
                    msg_id = str(m.get("messageId", "") or "")
                    if not msg_id:
                        continue
                    exists_m = await session.execute(
                        select(SambaCSInquiry).where(
                            SambaCSInquiry.market == "eBay",
                            SambaCSInquiry.market_inquiry_no == f"msg_{msg_id}",
                        )
                    )
                    if exists_m.scalar_one_or_none():
                        continue
                    recv = m.get("receiveDate") or ""
                    recv_dt = None
                    if isinstance(recv, str) and recv:
                        try:
                            recv_dt = datetime.fromisoformat(
                                recv.replace("Z", "+00:00")
                            )
                        except Exception:
                            recv_dt = None
                    _item_id = str(m.get("itemId", "") or "")
                    session.add(
                        SambaCSInquiry(
                            market="eBay",
                            market_inquiry_no=f"msg_{msg_id}",
                            market_answer_no=str(m.get("externalMessageId", "") or ""),
                            market_order_id="",
                            market_product_no=_item_id,
                            product_link=(
                                f"https://www.ebay.com/itm/{_item_id}"
                                if _item_id
                                else ""
                            ),
                            account_id=acct.id,
                            account_name=acct.market_name,
                            inquiry_type="message",
                            questioner=str(m.get("sender", "") or ""),
                            product_name=str(m.get("subject", "") or "")[:200],
                            content=str(m.get("text", "") or ""),
                            reply="",
                            reply_status="pending",
                            inquiry_date=recv_dt,
                        )
                    )
                    msg_count += 1

                await session.commit()
                synced += inq_count + msg_count
                logger.info(
                    f"[CS동기화] eBay({acct.market_name}): INR {inq_count}건 + 메시지 {msg_count}건 신규"
                )
        except Exception as e:
            logger.error(f"[CS동기화] eBay 동기화 실패: {e}")
            errors.append(f"eBay: {e}")

    # 롯데ON Q&A 동기화 — SambaMarketAccount 1차, SambaSettings 레거시 폴백
    lo_settings_list: list[Any] = []
    if account_id and target_market_type == "lotteon" and target_account is not None:
        # 단일 계정 지정
        lo_settings_list = [target_account]
    elif not market_name or market_name == "롯데ON":
        try:
            from backend.domain.samba.account.model import SambaMarketAccount

            lo_acc_result = await session.execute(
                select(SambaMarketAccount).where(
                    SambaMarketAccount.market_type == "lotteon",
                    SambaMarketAccount.is_active == True,  # noqa: E712
                )
            )
            lo_settings_list = list(lo_acc_result.scalars().all())
        except Exception as e:
            logger.warning(f"[CS동기화] 롯데ON SambaMarketAccount 조회 실패: {e}")

        # SambaMarketAccount가 비어있을 때만 레거시 SambaSettings 폴백
        if not lo_settings_list:
            try:
                lo_settings_result = await session.execute(
                    select(SambaSettings).where(
                        SambaSettings.key.like("store_lotteon%")
                    )
                )
                lo_settings_list = list(lo_settings_result.scalars().all())
            except Exception as e:
                logger.warning(f"[CS동기화] 롯데ON 설정 조회 실패: {e}")

    # 롯데ON 클라이언트 import (for 루프 진입 전 보장)
    LotteonClient = None  # type: ignore[assignment]
    if lo_settings_list:
        try:
            from backend.domain.samba.proxy.lotteon import LotteonClient  # noqa: F811
        except Exception as e:
            logger.warning(f"[CS동기화] 롯데ON 클라이언트 import 실패: {e}")
            lo_settings_list = []

    for lo_setting in lo_settings_list:
        try:
            import json as _json

            if hasattr(lo_setting, "additional_fields"):
                lo_config = dict(lo_setting.additional_fields or {})
                api_key = lo_config.get("apiKey", "") or lo_setting.api_key or ""
                account_name = (
                    lo_setting.account_label
                    or lo_setting.business_name
                    or lo_setting.seller_id
                    or ""
                )
            else:
                lo_config = (
                    _json.loads(lo_setting.value)
                    if isinstance(lo_setting.value, str)
                    else lo_setting.value
                )
                lo_config = lo_config or {}
                api_key = lo_config.get("apiKey", "")
                account_name = (
                    lo_config.get("businessName", "")
                    or lo_config.get("storeId", "")
                    or lo_setting.key
                )

            if not api_key:
                continue

            lo_client = LotteonClient(api_key=api_key)
            await lo_client.test_auth()  # trGrpCd/trNo 획득 (필수)

            lo_synced = await _sync_lotteon_qna(lo_client, session, svc, account_name)
            synced += lo_synced

            # 상품 Q&A 동기화
            try:
                lo_pqna_synced = await _sync_lotteon_product_qna(
                    lo_client, session, svc, account_name
                )
                synced += lo_pqna_synced
            except Exception as pqe:
                logger.warning(f"[CS동기화] 롯데ON 상품Q&A 동기화 실패 (무시): {pqe}")

            # 판매자 연락(Contact) 동기화
            try:
                lo_contact_synced = await _sync_lotteon_contact(
                    lo_client, session, svc, account_name
                )
                synced += lo_contact_synced
            except Exception as ce:
                logger.warning(f"[CS동기화] 롯데ON Contact 동기화 실패 (무시): {ce}")

            # 보상 요청(Compensate) 동기화
            try:
                lo_comp_synced = await _sync_lotteon_compensate(
                    lo_client, session, svc, account_name
                )
                synced += lo_comp_synced
            except Exception as compe:
                logger.warning(
                    f"[CS동기화] 롯데ON Compensate 동기화 실패 (무시): {compe}"
                )

        except Exception as e:
            logger.error(f"[CS동기화] 롯데ON({lo_setting.key}) 동기화 실패: {e}")
            errors.append(
                f"롯데ON({lo_setting.key}): {str(e)}"
            )  # 독립 에러 격리 — 다른 마켓에 영향 없음

    # 미연결 CS 문의 일괄 매칭 (market_product_no → market_product_nos)
    linked = 0
    try:
        from sqlmodel import select as sel

        unlinked = await session.execute(
            sel(SambaCSInquiry).where(
                SambaCSInquiry.collected_product_id.is_(None),
            )
        )
        unlinked_items = unlinked.scalars().all()
        if unlinked_items:
            from sqlalchemy import text as sa_text

            cp_result = await session.execute(
                sa_text(
                    "SELECT id, source_site, site_product_id, images, market_product_nos "
                    "FROM samba_collected_product "
                    "WHERE market_product_nos IS NOT NULL LIMIT 50000"
                )
            )
            cp_rows = cp_result.fetchall()

            # 마켓상품번호 → 수집상품 매핑 (+ site_product_id 매핑)
            mpn_map: dict[str, tuple] = {}
            spid_map: dict[str, tuple] = {}
            for row in cp_rows:
                pid, site, spid, imgs, mpnos = row
                # site_product_id 매핑 — product_name 끝 토큰 fallback용
                if spid:
                    spid_map[str(spid)] = (pid, site, spid, imgs, mpnos)
                if mpnos and isinstance(mpnos, dict):
                    for k, v in mpnos.items():
                        if not v:
                            continue
                        # 그룹전송 시 dict 형태({"originProductNo": ..., "smartstoreChannelProductNo": ...})
                        if isinstance(v, dict):
                            for inner_v in v.values():
                                if inner_v:
                                    mpn_map[str(inner_v)] = (
                                        pid,
                                        site,
                                        spid,
                                        imgs,
                                        mpnos,
                                    )
                        else:
                            mpn_map[str(v)] = (pid, site, spid, imgs, mpnos)

            sourcing_urls = {
                "MUSINSA": "https://www.musinsa.com/products/{}",
                "KREAM": "https://kream.co.kr/products/{}",
                "FashionPlus": "https://www.fashionplus.co.kr/goods/detail/{}",
                "ABCmart": "https://www.a-rt.com/product?prdtNo={}",
                "GrandStage": "https://www.a-rt.com/product?prdtNo={}",
                "REXMONDE": "https://www.okmall.com/products/detail/{}",
                "LOTTEON": "https://www.lotteon.com/p/product/{}",
                "GSShop": "https://www.gsshop.com/prd/prd.gs?prdid={}",
                "ElandMall": "https://www.elandmall.com/goods/goods.action?goodsNo={}",
                "SSF": "https://www.ssfshop.com/goods/{}",
                "SSG": "https://www.ssg.com/item/itemView.ssg?itemId={}",
                "Nike": "https://www.nike.com/kr/t/{}",
                "Adidas": "https://www.adidas.co.kr/{}.html",
            }

            for inq in unlinked_items:
                # 1차: market_product_no로 매칭
                matched = None
                mpno = inq.market_product_no
                if mpno:
                    matched = mpn_map.get(str(mpno))
                # 2차: product_name 끝 토큰(site_product_id)으로 fallback 매칭
                # — 등록 후 cleanup으로 market_product_nos가 NULL이 된 케이스 대응
                if not matched and inq.product_name:
                    tokens = str(inq.product_name).strip().split()
                    if tokens:
                        last = tokens[-1]
                        if last.isdigit() and len(last) >= 4:
                            matched = spid_map.get(last)
                if not matched:
                    continue

                pid, site, spid, imgs, mpnos = matched
                inq.collected_product_id = pid
                if not inq.original_link and site in sourcing_urls and spid:
                    inq.original_link = sourcing_urls[site].format(spid)
                if (
                    (not inq.product_image or inq.product_image == "")
                    and imgs
                    and isinstance(imgs, list)
                    and imgs
                ):
                    inq.product_image = imgs[0]
                # product_link: market_product_nos에서 마켓 상품번호 추출
                if (
                    not inq.product_link
                    and mpnos
                    and isinstance(mpnos, dict)
                    and inq.market
                ):
                    for mk, mv in mpnos.items():
                        if mv and not mk.endswith("_origin"):
                            inq.product_link = _build_market_product_url(
                                inq.market, str(mv)
                            )
                            break
                linked += 1

            if linked > 0:
                await session.commit()
                logger.info(f"[CS동기화] 미연결 문의 {linked}건 상품 매칭 완료")
    except Exception as e:
        logger.warning(f"[CS동기화] 미연결 매칭 중 오류: {e}")

    # ── 플레이오토 EMP 문의 동기화 ──
    try:
        from backend.domain.samba.account.model import SambaMarketAccount
        from backend.domain.samba.proxy.playauto import PlayAutoClient

        pa_stmt = select(SambaMarketAccount).where(
            SambaMarketAccount.market_type == "playauto",
            SambaMarketAccount.is_active == True,  # noqa: E712
        )
        pa_result = await session.execute(pa_stmt)
        pa_accounts = pa_result.scalars().all()
        if account_id:
            pa_accounts = (
                [acc for acc in pa_accounts if acc.id == account_id]
                if target_market_type == "playauto"
                else []
            )

        for pa_acc in pa_accounts:
            pa_extras = pa_acc.additional_fields or {}
            pa_api_key = pa_extras.get("apiKey", "") or pa_acc.api_key or ""
            if not pa_api_key:
                continue
            pa_label = pa_acc.account_label or pa_acc.business_name or "플레이오토"
            pa_client = PlayAutoClient(pa_api_key)
            try:
                from zoneinfo import ZoneInfo

                kst = ZoneInfo("Asia/Seoul")
                now_kst = datetime.now(kst)
                pa_start = (now_kst - timedelta(days=30)).strftime("%Y%m%d")
                pa_end = (now_kst + timedelta(days=1)).strftime("%Y%m%d")

                # 신규 + 답변완료 문의 조회
                pa_qnas = await pa_client.get_qnas(
                    start_date=pa_start, end_date=pa_end, count=100
                )

                pa_synced = 0
                for qna in pa_qnas:
                    # 긴급메시지/문의만 수집, 나머지(상품평 등) 제외
                    if qna.get("QType", "") not in ("긴급메시지", "문의"):
                        continue
                    qna_no = str(qna.get("Number", ""))
                    if not qna_no:
                        continue

                    # 중복 체크 (기존 데이터에 product_link 없으면 업데이트)
                    existing_result = await session.execute(
                        select(SambaCSInquiry).where(
                            SambaCSInquiry.market == "플레이오토",
                            SambaCSInquiry.market_inquiry_no == qna_no,
                        )
                    )
                    existing_row = existing_result.scalar_one_or_none()

                    state = qna.get("State", "")
                    is_answered = state in ("답변완료", "전송완료")

                    site_name = qna.get("SiteName", "")
                    prod_code = qna.get("ProdCode") or qna.get("MasterCode") or ""

                    # SiteName → 판매 구매페이지 URL 매핑
                    _pa_site_url_map = {
                        "GS이숍": "https://www.gsshop.com/prd/prd.gs?prdid={code}",
                        "GS홈쇼핑": "https://www.gsshop.com/prd/prd.gs?prdid={code}",
                        "GS샵": "https://www.gsshop.com/prd/prd.gs?prdid={code}",
                        "지마켓": "https://item.gmarket.co.kr/Item?goodsCode={code}",
                        "옥션": "https://itempage3.auction.co.kr/DetailView.aspx?ItemNo={code}",
                        "11번가": "https://www.11st.co.kr/products/{code}",
                        "쿠팡": "https://www.coupang.com/vp/products/{code}",
                        "롯데ON": "https://www.lotteon.com/product/{code}",
                        "인터파크": "https://shopping.interpark.com/product/productInfo.do?prdNo={code}",
                        "위메프": "https://www.wemakeprice.com/product/{code}",
                        "티몬": "https://www.tmon.co.kr/deal/{code}",
                    }
                    _pa_tpl = _pa_site_url_map.get(site_name, "")
                    pa_product_link = (
                        _pa_tpl.format(code=prod_code)
                        if (_pa_tpl and prod_code)
                        else ""
                    )

                    # 기존 데이터가 있지만 product_link가 비어 있으면 URL만 업데이트
                    if existing_row:
                        if pa_product_link and not existing_row.product_link:
                            existing_row.product_link = pa_product_link
                            session.add(existing_row)
                        continue

                    raw_date = qna.get("WriteDate") or qna.get("QDate")
                    parsed_date = None
                    if raw_date:
                        try:
                            from dateutil.parser import parse as parse_dt

                            parsed_date = parse_dt(raw_date)
                        except Exception:
                            pass

                    inquiry_data = {
                        "market": "플레이오토",
                        "market_inquiry_no": qna_no,
                        "market_answer_no": None,
                        "market_order_id": qna.get("OrderCode"),
                        "market_product_no": prod_code or None,
                        "account_name": f"{pa_label} ({site_name})"
                        if site_name
                        else pa_label,
                        "inquiry_type": qna.get("QType", "문의"),
                        "questioner": qna.get("QName", ""),
                        "product_name": "",
                        "product_image": "",
                        "product_link": pa_product_link,
                        "original_link": "",
                        "collected_product_id": None,
                        "content": qna.get("QContent", "") or qna.get("QSubject", ""),
                        "reply": qna.get("AContent", "") if is_answered else None,
                        "reply_status": "replied" if is_answered else "pending",
                        "inquiry_date": parsed_date,
                        "replied_at": None,
                    }
                    await svc.create_inquiry(inquiry_data)
                    pa_synced += 1

                if pa_synced > 0:
                    logger.info(
                        f"[CS동기화] 플레이오토({pa_label}): {pa_synced}건 동기화"
                    )
                synced += pa_synced
            except Exception as e:
                logger.warning(f"[CS동기화] 플레이오토({pa_label}) 실패: {e}")
                errors.append(f"플레이오토({pa_label}): {e}")
            finally:
                await pa_client.close()
    except Exception as e:
        logger.warning(f"[CS동기화] 플레이오토 계정 조회 실패: {e}")

    # ── 11번가 Q&A 동기화 ──
    if not market_name or market_name == "11번가":
        try:
            from backend.domain.samba.account.model import SambaMarketAccount
            from backend.domain.samba.proxy.elevenst import ElevenstClient

            elevenst_stmt = select(SambaMarketAccount).where(
                SambaMarketAccount.market_type == "11st",
                SambaMarketAccount.is_active == True,  # noqa: E712
            )
            elevenst_result = await session.execute(elevenst_stmt)
            elevenst_accounts = elevenst_result.scalars().all()

            for elevenst_acc in elevenst_accounts:
                elevenst_extras = elevenst_acc.additional_fields or {}
                elevenst_api_key = (
                    elevenst_extras.get("apiKey", "") or elevenst_acc.api_key or ""
                )
                if not elevenst_api_key:
                    continue
                elevenst_label = (
                    elevenst_acc.account_label or elevenst_acc.business_name or "11번가"
                )
                elevenst_client = ElevenstClient(elevenst_api_key)
                try:
                    from zoneinfo import ZoneInfo

                    kst = ZoneInfo("Asia/Seoul")
                    now_kst_dt = datetime.now(kst)
                    es_start = (now_kst_dt - timedelta(days=7)).strftime("%Y%m%d")
                    es_end = now_kst_dt.strftime("%Y%m%d")

                    qna_items = await elevenst_client.get_qna_list(es_start, es_end)
                    es_synced = 0
                    for qna in qna_items:
                        brd_info_no = str(qna.get("brdInfoNo", "") or "")
                        if not brd_info_no:
                            continue

                        existing_result = await session.execute(
                            select(SambaCSInquiry).where(
                                SambaCSInquiry.market == "11번가",
                                SambaCSInquiry.market_inquiry_no == brd_info_no,
                                SambaCSInquiry.is_hidden == False,  # noqa: E712
                            )
                        )
                        if existing_result.scalar_one_or_none():
                            continue

                        is_answered = str(qna.get("answerYn", "N")).upper() == "Y"
                        subj = str(qna.get("brdInfoSbjct", "") or "")
                        is_urgent = "긴급" in subj
                        # 긴급알림은 답변완료여도 수집, 일반 Q&A는 답변완료 제외
                        if is_answered and not is_urgent:
                            continue
                        reply_content = str(qna.get("answerCont", "") or "")
                        prd_no = str(
                            qna.get("brdInfoClfNo", "") or qna.get("prdNo", "") or ""
                        )
                        prd_nm = str(qna.get("prdNm", "") or "")
                        content = str(qna.get("brdInfoCont", "") or "")
                        questioner = str(
                            qna.get("buyMbrNo", "") or qna.get("brdMbrNo", "") or ""
                        )

                        raw_date_str = str(qna.get("brdInfoDt", "") or "")
                        from datetime import datetime as _dt
                        from datetime import timezone as _tz

                        parsed_date = None
                        if raw_date_str and len(raw_date_str) >= 8:
                            try:
                                parsed_date = _dt.strptime(
                                    raw_date_str[:14], "%Y%m%d%H%M%S"
                                )
                            except Exception:
                                try:
                                    parsed_date = _dt.strptime(
                                        raw_date_str[:8], "%Y%m%d"
                                    )
                                except Exception:
                                    parsed_date = None
                        if parsed_date is None:
                            parsed_date = _dt.now(_tz.utc)

                        matched = await _find_collected_product_by_market_product_no(
                            session, prd_no
                        )
                        product_link = (
                            _build_market_product_url("11번가", prd_no)
                            if prd_no
                            else ""
                        )

                        inquiry_data = {
                            "market": "11번가",
                            "market_inquiry_no": brd_info_no,
                            "market_answer_no": prd_no or None,
                            "market_order_id": None,
                            "market_product_no": prd_no or None,
                            "account_name": elevenst_label,
                            "inquiry_type": (
                                "urgent_inquiry" if is_urgent else "product_question"
                            ),
                            "questioner": questioner,
                            "product_name": prd_nm,
                            "product_image": matched["product_image"]
                            if matched
                            else "",
                            "product_link": product_link,
                            "original_link": matched["original_link"]
                            if matched
                            else "",
                            "collected_product_id": matched["id"] if matched else None,
                            "content": content,
                            "reply": reply_content if is_answered else None,
                            "reply_status": "replied" if is_answered else "pending",
                            "inquiry_date": parsed_date,
                            "replied_at": None,
                        }
                        try:
                            await svc.create_inquiry(inquiry_data)
                            es_synced += 1
                        except Exception as _qe:
                            logger.warning(
                                f"[CS동기화] 11번가 Q&A {brd_info_no} 저장 실패: {_qe}"
                            )

                    if es_synced > 0:
                        logger.info(
                            f"[CS동기화] 11번가({elevenst_label}): {es_synced}건 동기화"
                        )
                    synced += es_synced

                    # ── 긴급알림 수집 ──
                    try:
                        urgent_items = await elevenst_client.get_urgent_inquiry_list(
                            es_start, es_end
                        )
                        for uq in urgent_items:
                            inq_no = str(uq.get("emerNtceSeq", "") or "")
                            if not inq_no:
                                continue
                            existing_result = await session.execute(
                                select(SambaCSInquiry).where(
                                    SambaCSInquiry.market == "11번가",
                                    SambaCSInquiry.market_inquiry_no == inq_no,
                                    SambaCSInquiry.is_hidden == False,  # noqa: E712
                                )
                            )
                            existing = existing_result.scalar_one_or_none()
                            if existing:
                                continue
                            prd_no_u = str(uq.get("prdNo", "") or "")
                            prd_nm_u = str(uq.get("prdNm", "") or "")
                            content_u = str(uq.get("emerCtnt", "") or "")
                            questioner_u = str(uq.get("memId", "") or "")
                            ord_no_u = str(uq.get("ordNo", "") or "")
                            create_dt = str(uq.get("createDt", "") or "")
                            create_tm = str(uq.get("createTm", "") or "").replace(
                                ":", ""
                            )
                            raw_date_u = create_dt + create_tm
                            from datetime import datetime as _dt
                            from datetime import timezone as _tz

                            parsed_date_u = None
                            if raw_date_u and len(raw_date_u) >= 8:
                                try:
                                    parsed_date_u = _dt.strptime(
                                        raw_date_u[:14], "%Y%m%d%H%M%S"
                                    )
                                except Exception:
                                    try:
                                        parsed_date_u = _dt.strptime(
                                            raw_date_u[:8], "%Y%m%d"
                                        )
                                    except Exception:
                                        parsed_date_u = None
                            if parsed_date_u is None:
                                parsed_date_u = _dt.now(_tz.utc)
                            matched_u = (
                                await _find_collected_product_by_market_product_no(
                                    session, prd_no_u
                                )
                            )
                            product_link_u = (
                                _build_market_product_url("11번가", prd_no_u)
                                if prd_no_u
                                else ""
                            )
                            urgent_data = {
                                "market": "11번가",
                                "market_inquiry_no": inq_no,
                                "market_answer_no": None,
                                "market_order_id": ord_no_u or None,
                                "market_product_no": prd_no_u or None,
                                "account_name": elevenst_label,
                                "inquiry_type": "urgent_inquiry",
                                "questioner": questioner_u,
                                "product_name": prd_nm_u,
                                "product_image": (
                                    matched_u["product_image"] if matched_u else ""
                                ),
                                "product_link": product_link_u,
                                "original_link": (
                                    matched_u["original_link"] if matched_u else ""
                                ),
                                "collected_product_id": (
                                    matched_u["id"] if matched_u else None
                                ),
                                "content": content_u,
                                "reply": None,
                                "reply_status": "pending",
                                "inquiry_date": parsed_date_u,
                                "replied_at": None,
                            }
                            await svc.create_inquiry(urgent_data)
                            es_synced += 1
                            synced += 1
                        if urgent_items:
                            logger.info(
                                f"[CS동기화] 11번가({elevenst_label}) 긴급알림 처리 완료"
                            )
                    except Exception as ue:
                        logger.warning(
                            f"[CS동기화] 11번가({elevenst_label}) 긴급알림 실패: {ue}"
                        )

                except Exception as e:
                    logger.warning(f"[CS동기화] 11번가({elevenst_label}) Q&A 실패: {e}")
                    errors.append(f"11번가({elevenst_label}): {e}")
        except Exception as e:
            logger.warning(f"[CS동기화] 11번가 계정 조회 실패: {e}")

    # ── 쿠팡 CS 문의 동기화 ──
    if not market_name or market_name == "쿠팡":
        try:
            from backend.domain.samba.account.model import SambaMarketAccount
            from backend.domain.samba.proxy.coupang import CoupangClient

            coupang_stmt = select(SambaMarketAccount).where(
                SambaMarketAccount.market_type == "coupang",
                SambaMarketAccount.is_active == True,  # noqa: E712
            )
            coupang_result = await session.execute(coupang_stmt)
            coupang_accounts = list(coupang_result.scalars().all())
            if account_id:
                coupang_accounts = [a for a in coupang_accounts if a.id == account_id]

            for cp_acc in coupang_accounts:
                af = cp_acc.additional_fields or {}
                access_key = af.get("accessKey", "") or cp_acc.api_key or ""
                secret_key = af.get("secretKey", "") or cp_acc.api_secret or ""
                vendor_id = af.get("vendorId", "") or cp_acc.seller_id or ""
                if not (access_key and secret_key and vendor_id):
                    continue
                cp_label = cp_acc.account_label or cp_acc.business_name or "쿠팡"
                cp_client = CoupangClient(
                    access_key=access_key,
                    secret_key=secret_key,
                    vendor_id=vendor_id,
                )
                try:
                    items = await cp_client.get_inquiries(days=7)
                    cp_synced = 0
                    for item in items:
                        inq_id = str(item.get("inquiryId", "") or "")
                        if not inq_id:
                            continue

                        existing_result = await session.execute(
                            select(SambaCSInquiry).where(
                                SambaCSInquiry.market == "쿠팡",
                                SambaCSInquiry.market_inquiry_no == inq_id,
                                SambaCSInquiry.is_hidden == False,  # noqa: E712
                            )
                        )
                        if existing_result.scalar_one_or_none():
                            continue

                        # v5 응답 키: inquiryId, content, inquiryAt, productId,
                        #   sellerProductId, sellerItemId, vendorItemId, orderIds[],
                        #   buyerEmail, commentDtoList[], sellerProductName
                        product_no = str(
                            item.get("productId", "")
                            or item.get("sellerProductId", "")
                            or ""
                        )
                        content = str(item.get("content", "") or "")
                        questioner = str(item.get("buyerEmail", "") or "")
                        order_ids = item.get("orderIds") or []
                        order_id = str(order_ids[0]) if order_ids else ""

                        comments = item.get("commentDtoList") or []
                        is_answered = bool(comments)
                        reply_content = ""
                        if is_answered and isinstance(comments, list):
                            last_comment = comments[-1] if comments else {}
                            if isinstance(last_comment, dict):
                                reply_content = str(
                                    last_comment.get("content", "") or ""
                                )

                        raw_dt = str(item.get("inquiryAt", "") or "")
                        parsed_date = None
                        for fmt in (
                            "%Y-%m-%dT%H:%M:%S",
                            "%Y-%m-%d %H:%M:%S",
                            "%Y-%m-%d",
                        ):
                            try:
                                parsed_date = datetime.strptime(raw_dt[:19], fmt)
                                break
                            except Exception:
                                continue
                        if parsed_date is None:
                            parsed_date = datetime.now(timezone.utc)

                        matched = await _find_collected_product_by_market_product_no(
                            session, product_no
                        )
                        product_link = (
                            _build_market_product_url("쿠팡", product_no)
                            if product_no
                            else ""
                        )

                        inquiry_data = {
                            "market": "쿠팡",
                            "market_inquiry_no": inq_id,
                            "market_answer_no": None,
                            "market_order_id": order_id or None,
                            "market_product_no": product_no or None,
                            "account_id": cp_acc.id,
                            "account_name": cp_label,
                            "inquiry_type": "product_question",
                            "questioner": questioner,
                            "product_name": str(
                                item.get("sellerProductName", "") or ""
                            ),
                            "product_image": matched["product_image"]
                            if matched
                            else "",
                            "product_link": product_link,
                            "original_link": matched["original_link"]
                            if matched
                            else "",
                            "collected_product_id": matched["id"] if matched else None,
                            "content": content,
                            "reply": reply_content if is_answered else None,
                            "reply_status": "replied" if is_answered else "pending",
                            "inquiry_date": parsed_date,
                            "replied_at": None,
                        }
                        try:
                            await svc.create_inquiry(inquiry_data)
                            cp_synced += 1
                        except Exception as ce:
                            logger.warning(
                                f"[CS동기화] 쿠팡 문의 {inq_id} 저장 실패: {ce}"
                            )

                    if cp_synced > 0:
                        logger.info(
                            f"[CS동기화] 쿠팡({cp_label}): {cp_synced}건 동기화"
                        )
                    synced += cp_synced
                except Exception as e:
                    logger.warning(f"[CS동기화] 쿠팡({cp_label}) 실패: {e}")
                    errors.append(f"쿠팡({cp_label}): {e}")
        except Exception as e:
            logger.warning(f"[CS동기화] 쿠팡 계정 조회 실패: {e}")

    # ── SSG 쪽지/Q&A 수집 ──
    try:
        from backend.domain.samba.account.model import SambaMarketAccount
        from backend.domain.samba.proxy.ssg import SSGClient
        from sqlalchemy import func as sa_func

        ssg_stmt = select(SambaMarketAccount).where(
            sa_func.lower(SambaMarketAccount.market_type) == "ssg",
            SambaMarketAccount.is_active == True,  # noqa: E712
        )
        ssg_result = await session.execute(ssg_stmt)
        ssg_accounts = ssg_result.scalars().all()
        if account_id:
            ssg_accounts = (
                [acc for acc in ssg_accounts if acc.id == account_id]
                if target_market_type == "ssg"
                else []
            )

        for ssg_acc in ssg_accounts:
            ssg_extras = ssg_acc.additional_fields or {}
            ssg_api_key = ssg_extras.get("apiKey", "") or ssg_acc.api_key or ""
            if not ssg_api_key:
                continue
            ssg_label = ssg_acc.account_label or ssg_acc.business_name or "SSG"
            ssg_client = SSGClient(ssg_api_key)
            try:
                result = await svc.collect_from_ssg(
                    ssg_client,
                    days_back=30,
                    account_id=ssg_acc.id,
                    account_label=ssg_label,
                )
                ssg_total = result["notes_collected"] + result["qna_collected"]
                if ssg_total > 0:
                    logger.info(
                        f"[CS동기화] SSG({ssg_label}): 쪽지 {result['notes_collected']}건, Q&A {result['qna_collected']}건 수집"
                    )
                synced += ssg_total
            except Exception as e:
                logger.error(f"[CS동기화] SSG({ssg_label}) 실패: {e}", exc_info=True)
                errors.append(f"SSG({ssg_label}): {e}")
            finally:
                await ssg_client.close()
    except Exception as e:
        logger.warning(f"[CS동기화] SSG 계정 조회 실패: {e}")

    results: list[dict[str, Any]] = []
    try:
        from sqlalchemy import func as sa_func

        created_rows = await session.execute(
            select(
                SambaCSInquiry.account_name,
                sa_func.count(SambaCSInquiry.id),
            )
            .where(SambaCSInquiry.created_at >= sync_started_at)
            .group_by(SambaCSInquiry.account_name)
        )
        created_count_map = {
            str(account_name or "").strip(): int(count or 0)
            for account_name, count in created_rows.all()
            if str(account_name or "").strip()
        }

        error_map: dict[str, str] = {}
        for err in errors:
            label = str(err).split(":", 1)[0].strip()
            if label and label not in error_map:
                error_map[label] = str(err)

        ordered_labels: list[str] = []
        for label in result_labels:
            clean = str(label or "").strip()
            if clean and clean not in ordered_labels:
                ordered_labels.append(clean)
        for label in created_count_map.keys():
            if label not in ordered_labels:
                ordered_labels.append(label)
        for label in error_map.keys():
            if label not in ordered_labels:
                ordered_labels.append(label)

        results = [
            {
                "account": label,
                "synced": created_count_map.get(label, 0),
                "error": error_map.get(label, ""),
            }
            for label in ordered_labels
        ]
    except Exception as e:
        logger.warning(f"[CS동기화] 계정별 결과 집계 실패: {e}")

    return {
        "success": True,
        "synced": synced,
        "linked": linked,
        "results": results,
        "errors": errors,
        "message": f"CS 문의 {synced}건 동기화 완료"
        + (f", {linked}건 상품연결" if linked else "")
        + (f" (에러 {len(errors)}건)" if errors else ""),
    }


@router.post("/{inquiry_id}/send-reply")
async def send_reply_to_market(
    inquiry_id: str,
    body: CSInquiryReply,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """CS 문의 답변을 마켓에 전송."""
    import json
    from datetime import datetime, timezone
    from sqlmodel import select
    from backend.domain.samba.forbidden.model import SambaSettings
    from backend.domain.samba.proxy.smartstore import SmartStoreClient

    svc = _write_service(session)
    inquiry = await svc.get_inquiry(inquiry_id)
    if not inquiry:
        raise HTTPException(404, "문의를 찾을 수 없습니다")

    if not inquiry.market_inquiry_no:
        raise HTTPException(
            400, "마켓 문의 번호가 없습니다 (수동 등록 문의는 마켓 전송 불가)"
        )

    if inquiry.market == "스마트스토어":
        from backend.domain.samba.account.model import SambaMarketAccount

        # inquiry.account_id → SambaMarketAccount 우선 조회
        client = None
        if inquiry.account_id:
            acc_result = await session.execute(
                select(SambaMarketAccount).where(
                    SambaMarketAccount.id == inquiry.account_id,
                    SambaMarketAccount.is_active == True,  # noqa: E712
                )
            )
            acc = acc_result.scalar_one_or_none()
            if acc:
                af = acc.additional_fields or {}
                cid = af.get("clientId", "") or acc.api_key or ""
                csec = af.get("clientSecret", "") or acc.api_secret or ""
                if cid and csec:
                    client = SmartStoreClient(cid, csec)
        # account_id 없거나 SambaMarketAccount 미등록 → SambaSettings 폴백
        if client is None:
            settings_result = await session.execute(
                select(SambaSettings).where(SambaSettings.key.like("store_smartstore%"))
            )
            ss_settings = settings_result.scalars().first()
            if ss_settings:
                config = (
                    json.loads(ss_settings.value)
                    if isinstance(ss_settings.value, str)
                    else ss_settings.value
                )
                client = SmartStoreClient(config["clientId"], config["clientSecret"])
        if not client:
            raise HTTPException(400, "스마트스토어 계정 설정이 없습니다")

        inquiry_no = int(inquiry.market_inquiry_no)

        if inquiry.inquiry_type == "product_question":
            # 상품문의(Q&A) → PUT /v1/contents/qnas/{questionId}
            result = await client.answer_product_qna(inquiry_no, body.reply)
            answer_no = ""
        else:
            # 고객문의(1:1) → POST /v1/pay-merchant/inquiries/{inquiryNo}/answer
            if inquiry.market_answer_no:
                result = await client.update_inquiry_answer(
                    inquiry_no,
                    int(inquiry.market_answer_no),
                    body.reply,
                )
            else:
                result = await client.answer_inquiry(inquiry_no, body.reply)
            answer_data = result.get("data", {})
            answer_no = str(answer_data.get("inquiryCommentNo", ""))

        from backend.domain.samba.cs_inquiry.repository import SambaCSInquiryRepository

        repo = SambaCSInquiryRepository(session)
        await repo.update_async(
            inquiry_id,
            reply=body.reply,
            reply_status="replied",
            market_answer_no=answer_no if answer_no else inquiry.market_answer_no,
            replied_at=datetime.now(timezone.utc),
        )

        msg = (
            "상품문의 답변 전송 완료"
            if inquiry.inquiry_type == "product_question"
            else "고객문의 답변 전송 완료"
        )
        return {
            "success": True,
            "message": f"스마트스토어 {msg}",
            "data": result.get("data") if isinstance(result, dict) else {},
        }

    if inquiry.market == "롯데ON":
        from backend.domain.samba.proxy.lotteon import LotteonClient
        from backend.domain.samba.account.model import SambaMarketAccount

        # inquiry.account_id → SambaMarketAccount 우선 조회
        lo_client = None
        if inquiry.account_id:
            acc_result = await session.execute(
                select(SambaMarketAccount).where(
                    SambaMarketAccount.id == inquiry.account_id,
                    SambaMarketAccount.is_active == True,  # noqa: E712
                )
            )
            acc = acc_result.scalar_one_or_none()
            if acc:
                af = acc.additional_fields or {}
                api_key = af.get("apiKey", "") or acc.api_key or ""
                if api_key:
                    lo_client = LotteonClient(api_key=api_key)
        # account_id 없거나 SambaMarketAccount 미등록 → SambaSettings 폴백
        if lo_client is None:
            settings_result = await session.execute(
                select(SambaSettings).where(SambaSettings.key.like("store_lotteon%"))
            )
            lo_settings = settings_result.scalars().first()
            if lo_settings:
                config = (
                    json.loads(lo_settings.value)
                    if isinstance(lo_settings.value, str)
                    else lo_settings.value
                )
                lo_client = LotteonClient(api_key=config["apiKey"])
        if not lo_client:
            raise HTTPException(400, "롯데ON 계정 설정이 없습니다")

        await lo_client.test_auth()  # trGrpCd/trNo 획득 (필수)

        qna_no = inquiry.market_inquiry_no
        answer_no = ""
        if inquiry.market_answer_no:
            await lo_client.update_qna_answer(
                qna_no, inquiry.market_answer_no, body.reply
            )
            answer_no = inquiry.market_answer_no
        else:
            data = await lo_client.answer_qna(qna_no, body.reply)
            answer_no = str(data.get("ansNo", data.get("qnaAnsNo", "")))

        from backend.domain.samba.cs_inquiry.repository import SambaCSInquiryRepository

        repo = SambaCSInquiryRepository(session)
        await repo.update_async(
            inquiry_id,
            reply=body.reply,
            reply_status="replied",
            market_answer_no=answer_no if answer_no else inquiry.market_answer_no,
            replied_at=datetime.now(timezone.utc),
        )
        return {"success": True, "message": "롯데ON Q&A 답변 전송 완료", "data": {}}

    if inquiry.market == "11번가":
        from datetime import timezone

        from sqlmodel import select as _sel

        from backend.domain.samba.account.model import SambaMarketAccount
        from backend.domain.samba.cs_inquiry.repository import SambaCSInquiryRepository
        from backend.domain.samba.proxy.elevenst import ElevenstClient

        acc_stmt = _sel(SambaMarketAccount).where(
            SambaMarketAccount.market_type == "11st",
            SambaMarketAccount.is_active == True,  # noqa: E712
        )
        acc_result = await session.execute(acc_stmt)
        elevenst_acc = acc_result.scalars().first()
        if not elevenst_acc:
            raise HTTPException(400, "11번가 계정 설정이 없습니다")

        elevenst_extras = elevenst_acc.additional_fields or {}
        elevenst_api_key = (
            elevenst_extras.get("apiKey", "") or elevenst_acc.api_key or ""
        )
        if not elevenst_api_key:
            raise HTTPException(400, "11번가 API 키가 없습니다")

        elevenst_client = ElevenstClient(elevenst_api_key)
        prd_no = inquiry.market_answer_no or inquiry.market_product_no or ""
        await elevenst_client.reply_qna(inquiry.market_inquiry_no, prd_no, body.reply)

        repo = SambaCSInquiryRepository(session)
        await repo.update_async(
            inquiry_id,
            reply=body.reply,
            reply_status="replied",
            replied_at=datetime.now(timezone.utc),
        )
        return {"success": True, "message": "11번가 Q&A 답변 전송 완료", "data": {}}

    if inquiry.market == "쿠팡":
        from datetime import timezone

        from sqlmodel import select as _sel

        from backend.domain.samba.account.model import SambaMarketAccount
        from backend.domain.samba.cs_inquiry.repository import SambaCSInquiryRepository
        from backend.domain.samba.proxy.coupang import CoupangClient

        cp_acc = None
        if inquiry.account_id:
            acc_result = await session.execute(
                _sel(SambaMarketAccount).where(
                    SambaMarketAccount.id == inquiry.account_id,
                    SambaMarketAccount.is_active == True,  # noqa: E712
                )
            )
            cp_acc = acc_result.scalar_one_or_none()
        if cp_acc is None:
            acc_result = await session.execute(
                _sel(SambaMarketAccount).where(
                    SambaMarketAccount.market_type == "coupang",
                    SambaMarketAccount.is_active == True,  # noqa: E712
                )
            )
            cp_acc = acc_result.scalars().first()
        if not cp_acc:
            raise HTTPException(400, "쿠팡 계정 설정이 없습니다")

        af = cp_acc.additional_fields or {}
        access_key = af.get("accessKey", "") or cp_acc.api_key or ""
        secret_key = af.get("secretKey", "") or cp_acc.api_secret or ""
        vendor_id = af.get("vendorId", "") or cp_acc.seller_id or ""
        reply_by = (
            af.get("replyBy", "")
            or cp_acc.seller_id
            or af.get("wingLoginId", "")
            or af.get("loginId", "")
            or ""
        )
        if not (access_key and secret_key and vendor_id):
            raise HTTPException(400, "쿠팡 인증정보 없음")
        if not reply_by:
            raise HTTPException(
                400,
                "쿠팡 Wing 로그인 ID 가 설정되지 않았습니다. 계정 설정에서 스토어 ID 를 입력하세요.",
            )

        cp_client = CoupangClient(access_key, secret_key, vendor_id)
        await cp_client.reply_inquiry(
            inquiry_id=int(inquiry.market_inquiry_no),
            content=body.reply,
            reply_by=reply_by,
        )

        repo = SambaCSInquiryRepository(session)
        await repo.update_async(
            inquiry_id,
            reply=body.reply,
            reply_status="replied",
            replied_at=datetime.now(timezone.utc),
        )
        return {"success": True, "message": "쿠팡 CS 답변 전송 완료", "data": {}}

    if inquiry.market == "eBay":
        # eBay 메시지 답장 — Trading API AddMemberMessageRTQ
        from backend.domain.samba.account.model import SambaMarketAccount
        from backend.domain.samba.cs_inquiry.repository import SambaCSInquiryRepository
        from backend.domain.samba.proxy.ebay import EbayApiError, EbayClient

        if not inquiry.account_id:
            raise HTTPException(400, "eBay 계정 ID 없음")
        acct_result = await session.execute(
            select(SambaMarketAccount).where(
                SambaMarketAccount.id == inquiry.account_id
            )
        )
        acct = acct_result.scalar_one_or_none()
        if not acct:
            raise HTTPException(400, "eBay 계정을 찾을 수 없음")
        extras = acct.additional_fields or {}
        app_id = extras.get("clientId") or extras.get("appId") or acct.api_key or ""
        cert_id = (
            extras.get("clientSecret") or extras.get("certId") or acct.api_secret or ""
        )
        refresh_token = extras.get("oauthToken") or extras.get("authToken", "") or ""
        if not (app_id and cert_id and refresh_token):
            raise HTTPException(400, "eBay 인증정보 없음")

        # 답장용 ID = market_answer_no(ExternalMessageID) 우선, 없으면 market_inquiry_no
        ext_msg_id = inquiry.market_answer_no or ""
        if not ext_msg_id:
            msg_id = inquiry.market_inquiry_no or ""
            if msg_id.startswith("msg_"):
                msg_id = msg_id[4:]
            ext_msg_id = msg_id
        if not ext_msg_id:
            raise HTTPException(
                400, "eBay messageId 없음 (INR inquiry는 API 답장 불가)"
            )

        client = EbayClient(
            app_id=app_id,
            dev_id="",
            cert_id=cert_id,
            refresh_token=refresh_token,
            sandbox=bool(extras.get("sandbox", False)),
        )
        try:
            await client.reply_message(
                parent_message_id=ext_msg_id,
                text=body.reply,
                recipient=inquiry.questioner or "",
                item_id=inquiry.market_product_no or "",
            )
        except EbayApiError as e:
            raise HTTPException(500, f"eBay 메시지 답장 실패: {e}")

        repo = SambaCSInquiryRepository(session)
        await repo.update_async(
            inquiry_id,
            reply=body.reply,
            reply_status="replied",
            replied_at=datetime.now(timezone.utc),
        )
        return {"success": True, "message": "eBay 메시지 답장 완료", "data": {}}

    raise HTTPException(
        400, f"'{inquiry.market}' 마켓은 아직 답변 전송을 지원하지 않습니다"
    )


@router.post("/batch-delete")
async def batch_delete_cs_inquiries(
    body: CSInquiryBatchDelete,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """CS 문의 선택 삭제."""
    svc = _write_service(session)
    count = await svc.delete_batch(body.ids)
    return {"deleted": count}


@router.delete("/{inquiry_id}")
async def delete_cs_inquiry(
    inquiry_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """CS 문의 단건 삭제."""
    svc = _write_service(session)
    deleted = await svc.delete_inquiry(inquiry_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="문의를 찾을 수 없습니다")
    return {"ok": True}


@router.post("/{inquiry_id}/hide")
async def hide_cs_inquiry(
    inquiry_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """CS 문의 숨기기."""
    svc = _write_service(session)
    inquiry = await svc.get_inquiry(inquiry_id)
    if not inquiry:
        raise HTTPException(status_code=404, detail="문의를 찾을 수 없습니다")
    await svc.repo.update_async(inquiry_id, is_hidden=True)
    return {"ok": True}


class CollectSSGRequest(BaseModel):
    account_id: str
    days_back: int = 7


@router.post("/collect/ssg")
async def collect_ssg_cs(
    body: CollectSSGRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """SSG 쪽지/Q&A 수집."""
    from sqlmodel import select
    from backend.domain.samba.account.model import SambaMarketAccount
    from backend.domain.samba.proxy.ssg import SSGClient

    stmt = select(SambaMarketAccount).where(SambaMarketAccount.id == body.account_id)
    result = await session.execute(stmt)
    account = result.scalar_one_or_none()
    if not account:
        raise HTTPException(404, "계정을 찾을 수 없습니다")

    extras = account.additional_fields or {}
    api_key = extras.get("apiKey", "") or account.api_key or ""
    if not api_key:
        raise HTTPException(400, "SSG API 키가 없습니다")

    ssg_client = SSGClient(api_key)
    try:
        svc = _write_service(session)
        account_label = account.account_label or account.business_name or "SSG"
        return await svc.collect_from_ssg(
            ssg_client,
            days_back=body.days_back,
            account_id=body.account_id,
            account_label=account_label,
        )
    finally:
        await ssg_client.close()


@router.post("/{inquiry_id}/reply-to-market")
async def reply_to_market(
    inquiry_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """SSG에 답변 전송 (DB에 저장된 답변 내용 기준)."""
    from sqlmodel import select
    from backend.domain.samba.account.model import SambaMarketAccount
    from backend.domain.samba.proxy.ssg import SSGClient

    svc = _write_service(session)
    inquiry = await svc.get_inquiry(inquiry_id)
    if not inquiry:
        raise HTTPException(404, "문의를 찾을 수 없습니다")
    if inquiry.market != "SSG":
        raise HTTPException(
            400, f"'{inquiry.market}' 마켓은 지원하지 않습니다 (SSG만 가능)"
        )
    if not inquiry.reply:
        raise HTTPException(400, "답변이 없습니다. 먼저 답변을 저장해주세요")

    # 계정 조회
    account = None
    if inquiry.account_id:
        stmt = select(SambaMarketAccount).where(
            SambaMarketAccount.id == inquiry.account_id
        )
        result = await session.execute(stmt)
        account = result.scalar_one_or_none()

    if not account:
        stmt = select(SambaMarketAccount).where(
            SambaMarketAccount.market_type == "ssg",
            SambaMarketAccount.is_active == True,  # noqa: E712
        )
        result = await session.execute(stmt)
        account = result.scalars().first()

    if not account:
        raise HTTPException(400, "SSG 계정 설정이 없습니다")

    extras = account.additional_fields or {}
    api_key = extras.get("apiKey", "") or account.api_key or ""
    if not api_key:
        raise HTTPException(400, "SSG API 키가 없습니다")

    ssg_client = SSGClient(api_key)
    try:
        ok = await svc.send_reply_to_market(inquiry_id, ssg_client)
        if not ok:
            raise HTTPException(500, "마켓 답변 전송에 실패했습니다")
        return {"ok": True}
    finally:
        await ssg_client.close()

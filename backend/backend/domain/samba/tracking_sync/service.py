"""송장 자동전송 서비스.

흐름:
  1) enqueue(order_id) — SambaOrder.sourcing_order_number 기반으로 SourcingQueue에
     `type='tracking'` 잡 적재 + SambaTrackingSyncJob row 생성 (status=PENDING)
  2) 확장앱이 잡 수신 → 소싱처 배송조회 페이지 열고 운송장 추출
  3) apply_tracking(request_id, courier, tracking) — DB 저장 (status=SCRAPED) →
     SambaOrder.tracking_number/shipping_company 갱신
  4) dispatch_to_market(job_id, dry_run) — 마켓 dispatch API 호출 (status=SENT_TO_MARKET)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlmodel import select

from backend.db.orm import get_write_session
from backend.domain.samba.order.model import (
    EXCLUDED_ORDER_STATUSES,
    SHIPPED_SHIPPING_STATUS_KEYWORDS,
    SambaOrder,
    is_order_cancelled,
)
from backend.domain.samba.tracking_sync.model import (
    STATUS_CANCELLED,
    STATUS_DISPATCH_FAILED,
    STATUS_DISPATCHED,
    STATUS_FAILED,
    STATUS_NO_TRACKING,
    STATUS_PENDING,
    STATUS_SCRAPED,
    STATUS_SENT,
    SambaTrackingSyncJob,
)
from backend.utils.logger import logger

_UTC = timezone.utc


# 소싱처 배송조회 URL 빌더 — 확장앱 content-script와 셀렉터 짝꿍
# overlink-invoice-extension config.js 검증값 이식 (2026-05-13)
def build_tracking_url(site: str, sourcing_order_number: str) -> str:
    raw = site or ""
    s = raw.upper()
    ord_no = sourcing_order_number
    # 한글/별칭 별명 정규화 — 일부 주문에 'GS이숍(고경)' 같은 계정 라벨이 source_site에 들어옴
    if "GS이숍" in raw or "GS샵" in raw or s.startswith("GSSHOP"):
        s = "GSSHOP"
    if s == "MUSINSA":
        # 직접 trace URL은 "정상적인 접근이 아닙니다" 거부됨 (ord_opt_no 필수).
        # 주문상세 페이지로 진입 → 확장앱이 "배송 조회" 버튼 클릭 → trace 페이지로 navigation.
        return f"https://www.musinsa.com/order/order-detail/{ord_no}"
    if s == "LOTTEON":
        return f"https://www.lotteon.com/p/order/claim/orderDetail?odNo={ord_no}"
    if s == "SSG":
        return f"https://pay.ssg.com/myssg/orderInfoDetail.ssg?orordNo={ord_no}"
    if s == "ABCMART":
        return (
            f"https://abcmart.a-rt.com/mypage/order/read-order-detail?orderNo={ord_no}"
        )
    if s == "GRANDSTAGE":
        return f"https://grandstage.a-rt.com/mypage/order/read-order-detail?orderNo={ord_no}"
    if s == "GSSHOP":
        return f"https://www.gsshop.com/ord/dlvcursta/popup/ordDtl.gs?ordNo={ord_no}&ecOrdTypCd=S"
    if s == "FASHIONPLUS":
        return f"https://www.fashionplus.co.kr/mypage/order/detail/{ord_no}"
    if s == "NIKE":
        return f"https://www.nike.com/kr/orders/sales/{ord_no}/"
    if s == "OLIVEYOUNG":
        return f"https://www.oliveyoung.co.kr/store/mypage/getOrderDetail.do?ordNo={ord_no}"
    raise ValueError(f"지원하지 않는 소싱처 송장조회: {site}")


# 택배사 이름 정규화 — overlink utils.js의 매핑을 백엔드로 이식
# 키: 소싱처에서 추출되는 한글/영문 이름의 다양한 변형 (소문자/공백제거 후 비교)
COURIER_NAME_ALIASES: dict[str, str] = {
    "cj대한통운": "CJ대한통운",
    "cj": "CJ대한통운",
    "대한통운": "CJ대한통운",
    "한진택배": "한진택배",
    "한진": "한진택배",
    "롯데택배": "롯데택배",
    "롯데글로벌로지스": "롯데택배",
    "우체국택배": "우체국택배",
    "우체국": "우체국택배",
    "로젠택배": "로젠택배",
    "로젠": "로젠택배",
    "cu편의점택배": "CU편의점택배",
    "gs편의점택배": "GS편의점택배",
    "gspostbox": "GS편의점택배",
    "경동택배": "경동택배",
    "쿠팡": "쿠팡로지스틱스",
    "cls": "쿠팡로지스틱스",
    "쿠팡로지스틱스": "쿠팡로지스틱스",
    "agility": "Agility",
}


def normalize_courier_name(raw: str) -> str:
    """확장앱이 추출한 택배사 이름을 정규화된 한글명으로 변환."""
    if not raw:
        return ""
    key = raw.strip().lower().replace(" ", "")
    return COURIER_NAME_ALIASES.get(key, raw.strip())


# ---------------------------------------------------------------------------
# 1) 잡 큐잉
# ---------------------------------------------------------------------------


async def _resolve_owner_device_id(sourcing_account_id: Optional[str]) -> str:
    """소싱처 계정 → chrome_profile → samba_chrome_profile → device_id 매핑.

    MVP: chrome_profile 문자열이 있으면 그대로 owner_device_id로 사용한다.
    확장앱은 자기 chrome 프로필명과 매칭되는 잡만 받게 background-sourcing.js에서
    필터링한다. 매핑이 더 정교해지면 여기서 발전시킨다.
    """
    if not sourcing_account_id:
        return ""
    try:
        from backend.domain.samba.sourcing_account.model import SambaSourcingAccount

        async with get_write_session() as session:
            row = await session.get(SambaSourcingAccount, sourcing_account_id)
            if not row:
                return ""
            return (row.chrome_profile or "").strip()
    except Exception as exc:
        logger.warning(f"[송장동기화] owner 조회 실패: {exc}")
        return ""


def _detect_site_from_url(url: str) -> str:
    """source_url 도메인에서 소싱처 코드 추론. 매칭 없으면 ''."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or url).lower()
    except Exception:
        host = (url or "").lower()
    if "musinsa.com" in host:
        return "MUSINSA"
    if "kream.co.kr" in host:
        return "KREAM"
    if "fashionplus.co.kr" in host:
        return "FASHIONPLUS"
    if "grandstage.a-rt.com" in host:
        return "GRANDSTAGE"
    if "abcmart" in host or host == "www.a-rt.com" or host == "a-rt.com":
        return "ABCMART"
    if "nike.com" in host:
        return "NIKE"
    if "ssg.com" in host:
        return "SSG"
    if "lotteon.com" in host:
        return "LOTTEON"
    if "gsshop.com" in host:
        return "GSSHOP"
    if "oliveyoung.co.kr" in host:
        return "OLIVEYOUNG"
    return ""


async def _try_backend_fetch_musinsa(order, session) -> Optional[dict[str, Any]]:
    """MUSINSA 송장 정보 백엔드 직접 fetch — 성공 시 SCRAPED 잡 row 생성 후 결과 반환.

    쿠키 풀(musinsa_cookies) 전체를 순회하며 한 쿠키라도 성공하면 OK.
    무신사 마이페이지 deliveryInfo는 그 주문이 자기 계정 주문일 때만 200 SUCCESS,
    아니면 FAIL 반환. 즉 풀 순회로 "이 주문이 누구 계정 거냐"를 자동 매칭함.

    실패/누락 시 None 반환 → enqueue_for_order 호출자가 SourcingQueue 폴백.
    """
    from backend.domain.samba.collector.refresher import _get_musinsa_cookies
    from backend.domain.samba.proxy.musinsa import MusinsaClient

    ord_no = (order.sourcing_order_number or "").strip()
    ord_opt_no = (order.musinsa_ord_opt_no or "").strip()
    if not ord_no or not ord_opt_no:
        return None

    cookies = await _get_musinsa_cookies()
    if not cookies:
        logger.info("[송장동기화] MUSINSA backend fetch 스킵 — 쿠키 풀 비어있음")
        return None

    result = None
    last_error = ""
    for idx, cookie in enumerate(cookies):
        client = MusinsaClient(cookie=cookie)
        result = await client.fetch_tracking(ord_no, ord_opt_no)
        if result.get("ok"):
            logger.info(
                f"[송장동기화] MUSINSA backend fetch 성공 (cookie #{idx + 1}/{len(cookies)}): "
                f"order={order.id} ord_no={ord_no}"
            )
            break
        last_error = result.get("error") or ""
    else:
        logger.info(
            f"[송장동기화] MUSINSA backend fetch 모든 쿠키 실패 ({len(cookies)}개 시도, 폴백): "
            f"order={order.id} ord_no={ord_no} last_err={last_error}"
        )
        return None
    if not result or not result.get("ok"):
        return None

    courier = normalize_courier_name(result.get("courier") or "")
    tracking = (result.get("trackingNumber") or "").strip()
    if not tracking:
        return None

    # SambaTrackingSyncJob을 SCRAPED 상태로 즉시 생성
    job = SambaTrackingSyncJob(
        tenant_id=order.tenant_id,
        order_id=order.id,
        sourcing_site="MUSINSA",
        sourcing_order_number=ord_no,
        sourcing_account_id=order.sourcing_account_id,
        owner_device_id=None,
        request_id=None,
        status=STATUS_SCRAPED,
        scraped_courier=courier,
        scraped_tracking=tracking,
        scraped_at=datetime.now(_UTC),
    )
    session.add(job)

    # SambaOrder.tracking_number / shipping_company도 함께 갱신
    order.tracking_number = tracking
    order.shipping_company = courier or order.shipping_company
    order.updated_at = datetime.now(_UTC)
    session.add(order)

    await session.commit()
    logger.info(
        f"[송장동기화] MUSINSA backend fetch 성공: order={order.id} "
        f"courier={courier} tracking={tracking}"
    )
    return {
        "success": True,
        "jobId": job.id,
        "backendFetch": True,
        "courier": courier,
        "tracking": tracking,
    }


_KNOWN_SOURCE_SITES = {
    "MUSINSA",
    "KREAM",
    "LOTTEON",
    "GSSHOP",
    "GSSHOP_KKD",
    "SSG",
    "ABCMART",
    "GRANDSTAGE",
    "NIKE",
    "FASHIONPLUS",
    "OLIVEYOUNG",
}


async def _resolve_actual_source_site(order, session) -> str:
    """주문의 진짜 소싱처 코드 결정.

    우선순위: source_url 도메인 → collected_product.source_site → order.source_site
    OrderInfoCell.tsx의 배지 로직과 동일.

    Note: order.source_site 에 PlayAuto 별칭(예: "GS이숍(캐논)")이 과거 데이터로
    남아있을 수 있어, 괄호 포함 / 미지의 코드는 무시한다. 이런 경우 송장 추출은
    collected_product 매칭이 되어야만 가능.
    """
    detected = _detect_site_from_url(order.source_url or "")
    if detected:
        return detected
    if order.collected_product_id:
        try:
            from backend.domain.samba.collector.model import SambaCollectedProduct

            cp = await session.get(SambaCollectedProduct, order.collected_product_id)
            if cp and cp.source_site:
                return cp.source_site.strip()
        except Exception as exc:
            logger.warning(f"[송장동기화] collected_product 조회 실패: {exc}")
    raw = (order.source_site or "").strip()
    if not raw or "(" in raw:
        return ""
    return raw if raw.upper() in _KNOWN_SOURCE_SITES else ""


async def enqueue_for_order(order_id: str, *, force: bool = False) -> dict[str, Any]:
    """단건 주문에 대해 송장 추출 잡을 큐에 적재.

    force=False (기본): 이미 PENDING/DISPATCHED 잡이 있으면 중복 큐잉 안 함.
    """
    from backend.domain.samba.proxy.sourcing_queue import SourcingQueue

    async with get_write_session() as session:
        order = await session.get(SambaOrder, order_id)
        if not order:
            return {"success": False, "error": "주문을 찾을 수 없습니다"}
        # 취소 가드 — 마켓에서 취소요청이 들어온 건은 송장 추출/등록 진행 금지
        if is_order_cancelled(order):
            logger.warning(
                f"[송장동기화][가드] 취소 주문 enqueue 차단 order_id={order_id} "
                f"status={order.status} shipping_status={order.shipping_status}"
            )
            return {
                "success": False,
                "blocked": True,
                "error": "취소요청 주문은 송장 추출 대상이 아닙니다",
            }
        if not order.sourcing_order_number:
            return {"success": False, "error": "소싱처 주문번호가 없습니다"}
        # 까대기 주문은 자체 직배라 소싱처 운송장 추출 불가 — 명시적 거부
        _tags = f",{(order.action_tag or '').strip()},"
        if ",kkadaegi," in _tags:
            return {
                "success": True,
                "skipped": True,
                "reason": "까대기 주문은 송장 추출 대상이 아닙니다",
            }
        # 진짜 소싱처는 source_url > collected_product > source_site 순으로 결정
        actual_site = await _resolve_actual_source_site(order, session)
        if not actual_site:
            return {"success": False, "error": "소싱처 정보가 없습니다"}
        if order.tracking_number and not force:
            return {
                "success": True,
                "skipped": True,
                "reason": "이미 송장번호가 있습니다",
            }

        # 중복 큐잉 방지 / force=True 시 기존 PENDING·DISPATCHED 잡 FAILED 처리 후 재큐잉
        if not force:
            stmt = (
                select(SambaTrackingSyncJob)
                .where(
                    SambaTrackingSyncJob.order_id == order_id,
                    SambaTrackingSyncJob.status.in_(
                        [STATUS_PENDING, STATUS_DISPATCHED]
                    ),
                )
                .limit(1)
            )
            existing = (await session.execute(stmt)).scalars().first()
            if existing:
                return {
                    "success": True,
                    "skipped": True,
                    "reason": "이미 진행 중인 잡이 있습니다",
                    "jobId": existing.id,
                }
        else:
            # 기존 PENDING/DISPATCHED 잡을 FAILED 로 닫고 신규 잡 생성 — 중복 누적 방지
            stale_stmt = select(SambaTrackingSyncJob).where(
                SambaTrackingSyncJob.order_id == order_id,
                SambaTrackingSyncJob.status.in_([STATUS_PENDING, STATUS_DISPATCHED]),
            )
            for stale in (await session.execute(stale_stmt)).scalars().all():
                stale.status = STATUS_FAILED
                stale.last_error = "강제 재큐잉으로 만료 처리"
                stale.updated_at = datetime.now(_UTC)

        owner_device_id = await _resolve_owner_device_id(order.sourcing_account_id)

        # MUSINSA 백엔드 직접 fetch 분기 — ord_opt_no가 DB에 저장돼 있으면
        # 확장앱 탭 폴링 없이 cookie 기반 deliveryInfo API 직접 호출.
        if actual_site == "MUSINSA" and (order.musinsa_ord_opt_no or "").strip():
            backend_result = await _try_backend_fetch_musinsa(order, session)
            if backend_result:
                return backend_result
            # backend fetch 실패 시 기존 SourcingQueue 폴백으로 진행

        # 1) SourcingQueue에 잡 적재 (확장앱 폴링이 받음) — actual_site 사용
        try:
            url = build_tracking_url(actual_site, order.sourcing_order_number)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        request_id, _future = SourcingQueue.add_tracking_job(
            site=actual_site,
            url=url,
            order_id=order.id,
            sourcing_order_number=order.sourcing_order_number,
            owner_device_id=owner_device_id or None,
            sourcing_account_id=order.sourcing_account_id or None,
        )

        # 2) DB row 생성
        job = SambaTrackingSyncJob(
            tenant_id=order.tenant_id,
            order_id=order.id,
            sourcing_site=actual_site,
            sourcing_order_number=order.sourcing_order_number,
            sourcing_account_id=order.sourcing_account_id,
            owner_device_id=owner_device_id or None,
            request_id=request_id,
            status=STATUS_PENDING,
        )
        session.add(job)
        await session.commit()

        logger.info(
            f"[송장동기화] 큐 적재: order={order.id} site={actual_site} "
            f"ord_no={order.sourcing_order_number} req={request_id}"
        )
        return {"success": True, "jobId": job.id, "requestId": request_id}


async def enqueue_pending_orders(
    tenant_id: Optional[str] = None,
    limit: int = 500,
    days: int = 7,
    force: bool = False,
) -> dict[str, Any]:
    """미발송 주문 일괄 적재 — 수동 트리거 + 스케줄러 공용.

    조건: KST 캘린더 7일(오늘 포함 -6일) + paid_at(폴백 created_at) +
          소싱처 주문번호 있음 + 송장번호 없음 + 소싱처 식별됨.
    """
    queued = 0
    skipped = 0
    errors: list[str] = []

    # KST 캘린더 N일 (오늘 포함, 즉 days=7 → 오늘 + 이전 6일)
    _KST = timezone(timedelta(hours=9))
    _today_kst = datetime.now(_KST).replace(hour=0, minute=0, second=0, microsecond=0)
    _start_kst = _today_kst - timedelta(days=days - 1)
    _end_kst = _today_kst + timedelta(days=1)  # exclusive upper bound
    since = _start_kst.astimezone(_UTC)
    until = _end_kst.astimezone(_UTC)

    async with get_write_session() as session:
        from sqlalchemy import func

        # action_tag(csv) 에 'kkadaegi' 토큰이 들어 있으면 송장 추출 대상에서 제외.
        # 까대기는 자체 직접 배송이라 소싱처 운송장 추출 불가.
        action_tag_expr = func.concat(
            ",", func.coalesce(SambaOrder.action_tag, ""), ","
        )
        # 페이지 필터와 동일하게 paid_at 기준 (NULL 시 created_at 폴백)
        date_col = func.coalesce(SambaOrder.paid_at, SambaOrder.created_at)

        # 페이지 필터 "취소/반품/교환 제외 + 배송중/배송완료 제외" 와 정확히 동일 기준
        stmt = (
            select(SambaOrder)
            .where(
                # tracking_number는 NULL 또는 빈 문자열 모두 "송장 미입력"으로 취급
                # (페이지 필터와 동일 — 실 데이터에 ''로 들어오는 케이스가 다수)
                (SambaOrder.tracking_number.is_(None))
                | (SambaOrder.tracking_number == ""),
                SambaOrder.sourcing_order_number.is_not(None),
                SambaOrder.sourcing_order_number != "",
                # source_site DB 컬럼이 비어 있어도 source_url 도메인 / collected_product 로 추론 가능하면 OK.
                # _resolve_actual_source_site 가 Python 레벨에서 실제 소싱처 결정.
                (
                    (
                        SambaOrder.source_site.is_not(None)
                        & (SambaOrder.source_site != "")
                    )
                    | (
                        SambaOrder.source_url.is_not(None)
                        & (SambaOrder.source_url != "")
                    )
                    | (SambaOrder.collected_product_id.is_not(None))
                ),
                date_col >= since,
                date_col < until,
                ~SambaOrder.status.in_(EXCLUDED_ORDER_STATUSES),
                ~action_tag_expr.like("%,kkadaegi,%"),
            )
            .order_by(date_col.desc())
            .limit(limit)
        )
        for kw in SHIPPED_SHIPPING_STATUS_KEYWORDS:
            stmt = stmt.where(
                (SambaOrder.shipping_status.is_(None))
                | (~SambaOrder.shipping_status.like(f"%{kw}%"))
            )
        if tenant_id:
            stmt = stmt.where(SambaOrder.tenant_id == tenant_id)
        orders = (await session.execute(stmt)).scalars().all()

    for order in orders:
        try:
            res = await enqueue_for_order(order.id, force=force)
            if res.get("skipped"):
                skipped += 1
            elif res.get("success"):
                queued += 1
            else:
                errors.append(f"{order.id}: {res.get('error')}")
        except Exception as exc:
            errors.append(f"{order.id}: {exc}")

    logger.info(
        f"[송장동기화] 일괄 적재 결과: queued={queued} skipped={skipped} "
        f"errors={len(errors)}"
    )
    return {
        "success": True,
        "queued": queued,
        "skipped": skipped,
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# 2) 결과 수신 (확장앱 → 백엔드)
# ---------------------------------------------------------------------------


async def apply_tracking_result(
    request_id: str,
    *,
    success: bool,
    courier_name: str = "",
    tracking_number: str = "",
    error: str = "",
    cancelled: bool = False,
    auto_dispatch: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """확장앱이 보낸 결과를 DB에 반영하고 (옵션) 마켓 dispatch까지 트리거.

    auto_dispatch=True 기본 — SCRAPED 직후 자동 마켓 push. 실패 시 DISPATCH_FAILED 로
    표시되고 UI 에서 재시도 가능.

    cancelled=True 또는 error에 "order_cancelled"/"주문취소" 마커가 있으면
    소싱처 원주문 취소로 간주 — SambaOrder.status="cancelling" + notes "원주문 취소" prepend.
    """
    reason_lower = (error or "").lower()
    is_cancelled = (
        bool(cancelled)
        or "order_cancelled" in reason_lower
        or "주문취소" in (error or "")
    )

    async with get_write_session() as session:
        stmt = select(SambaTrackingSyncJob).where(
            SambaTrackingSyncJob.request_id == request_id
        )
        job = (await session.execute(stmt)).scalars().first()
        if not job:
            logger.warning(f"[송장동기화] request_id 매칭 실패: {request_id}")
            return {"success": False, "error": "잡을 찾을 수 없습니다"}

        job.attempts = (job.attempts or 0) + 1
        job.updated_at = datetime.now(_UTC)

        # 원주문 취소 분기 — 가장 먼저 처리 (재시도 안 함)
        if is_cancelled:
            job.status = STATUS_CANCELLED
            job.last_error = "원주문 취소"
            order = await session.get(SambaOrder, job.order_id)
            if order:
                order.status = "cancelling"
                prev_notes = (order.notes or "").strip()
                tag = "원주문 취소"
                if tag not in prev_notes:
                    order.notes = f"{tag} / {prev_notes}" if prev_notes else tag
                order.updated_at = datetime.now(_UTC)
                session.add(order)
            session.add(job)
            await session.commit()
            logger.info(f"[송장동기화] 원주문 취소 감지: order={job.order_id}")
            return {"success": False, "status": job.status, "reason": "원주문 취소"}

        if not success or not tracking_number:
            # 캡챠/미발송/실패 — 재시도 여지 두기
            reason = (error or "송장번호 없음")[:500]
            job.last_error = reason
            if (
                "captcha" in reason.lower()
                or "미발송" in reason
                or "no_tracking" in reason.lower()
            ):
                job.status = STATUS_NO_TRACKING
            else:
                job.status = STATUS_FAILED
            session.add(job)
            await session.commit()
            return {"success": False, "status": job.status, "reason": reason}

        normalized_courier = normalize_courier_name(courier_name)
        job.scraped_courier = normalized_courier
        job.scraped_tracking = tracking_number.strip()
        job.scraped_at = datetime.now(_UTC)
        job.status = STATUS_SCRAPED

        # SambaOrder도 함께 갱신
        order = await session.get(SambaOrder, job.order_id)
        if order:
            order.tracking_number = job.scraped_tracking
            order.shipping_company = normalized_courier or order.shipping_company
            order.updated_at = datetime.now(_UTC)
            session.add(order)

        session.add(job)
        await session.commit()

        logger.info(
            f"[송장동기화] 추출 완료: order={job.order_id} courier={normalized_courier} "
            f"tracking={job.scraped_tracking}"
        )

    # 마켓 자동 dispatch (옵션)
    if auto_dispatch:
        try:
            await dispatch_to_market(job.id, dry_run=dry_run)
        except Exception as exc:
            logger.warning(f"[송장동기화] 자동 dispatch 실패 {job.id}: {exc}")

    return {
        "success": True,
        "jobId": job.id,
        "courier": normalized_courier,
        "tracking": tracking_number,
    }


# ---------------------------------------------------------------------------
# 3) 마켓 dispatch
# ---------------------------------------------------------------------------


async def dispatch_pending_to_market(*, dry_run: bool = False) -> dict[str, Any]:
    """SCRAPED + DISPATCH_FAILED 상태 잡 전체 일괄 마켓 전송 (재시도 포함)."""
    from sqlalchemy import select as _select

    sent = 0
    failed = 0
    errors: list[str] = []
    async with get_write_session() as session:
        stmt = _select(SambaTrackingSyncJob.id).where(
            SambaTrackingSyncJob.status.in_([STATUS_SCRAPED, STATUS_DISPATCH_FAILED])
        )
        job_ids = [row[0] for row in (await session.execute(stmt)).all()]

    for job_id in job_ids:
        try:
            res = await dispatch_to_market(job_id, dry_run=dry_run)
            if res.get("success"):
                sent += 1
            else:
                failed += 1
                errors.append(f"{job_id}: {res.get('error')}")
        except Exception as exc:
            failed += 1
            errors.append(f"{job_id}: {exc}")

    logger.info(f"[송장동기화] 일괄 dispatch 결과: sent={sent} failed={failed}")
    return {
        "success": True,
        "total": len(job_ids),
        "sent": sent,
        "failed": failed,
        "errors": errors[:20],
    }


async def dispatch_to_market(
    tracking_sync_job_id: str, *, dry_run: bool = False
) -> dict[str, Any]:
    """SCRAPED / DISPATCH_FAILED 잡의 운송장을 마켓 API로 push.

    dry_run=False (기본): 실제 마켓 API 호출. 실패 시 STATUS_DISPATCH_FAILED 로 표시.
    재시도는 DISPATCH_FAILED 상태에서도 호출 가능.
    """
    async with get_write_session() as session:
        job = await session.get(SambaTrackingSyncJob, tracking_sync_job_id)
        if not job:
            return {"success": False, "error": "잡을 찾을 수 없습니다"}
        if job.status not in (STATUS_SCRAPED, STATUS_DISPATCH_FAILED, STATUS_FAILED):
            return {
                "success": False,
                "error": f"dispatch 가능한 상태 아님: {job.status}",
            }
        if not job.scraped_tracking:
            return {"success": False, "error": "추출된 송장번호 없음"}

        order = await session.get(SambaOrder, job.order_id)
        if not order:
            return {"success": False, "error": "주문을 찾을 수 없습니다"}

        # 취소 가드 — 마켓 송장 등록 직전 최종 차단. 잡 자체는 CANCELLED 로 닫는다.
        if is_order_cancelled(order):
            logger.warning(
                f"[송장동기화][가드] 취소 주문 dispatch 차단 job_id={job.id} "
                f"order_id={order.id} status={order.status} "
                f"shipping_status={order.shipping_status}"
            )
            job.status = STATUS_CANCELLED
            job.last_error = (
                f"취소요청 감지로 dispatch 차단 (status={order.status}, "
                f"shipping_status={order.shipping_status})"
            )
            job.updated_at = datetime.now(_UTC)
            await session.commit()
            return {
                "success": False,
                "blocked": True,
                "error": "취소요청 주문은 송장 등록을 진행하지 않습니다",
            }

        channel_source = (order.source or "").lower()
        result: dict[str, Any] = {
            "channel": channel_source,
            "ext_order_number": order.ext_order_number,
            "courier": job.scraped_courier,
            "tracking": job.scraped_tracking,
            "dry_run": dry_run,
        }

        if dry_run:
            logger.info(f"[송장동기화][DRY] {result}")
            job.dispatch_result = {"dryRun": True, **result}
            job.dispatched_to_market_at = datetime.now(_UTC)
            job.status = STATUS_SENT
            session.add(job)
            await session.commit()
            return {"success": True, "dryRun": True, **result}

        # 실전송 분기
        try:
            if channel_source == "smartstore":
                from backend.domain.samba.proxy.smartstore import SmartStoreClient

                client = SmartStoreClient()
                api_resp = await client.ship_product_order(
                    product_order_id=order.ext_order_number or "",
                    delivery_company=job.scraped_courier or "",
                    tracking_number=job.scraped_tracking,
                )
                result["api"] = api_resp
            elif channel_source == "coupang":
                from backend.domain.samba.proxy.coupang import CoupangClient

                client = CoupangClient()
                # 쿠팡은 shipmentBoxId(int) + 코드(string) 사용 — order.ext_order_number에 박스ID 저장 가정
                shipment_box_id = int(order.ext_order_number or 0)
                api_resp = await client.update_shipping(
                    shipment_box_id=shipment_box_id,
                    delivery_company_code=job.scraped_courier or "",
                    invoice_number=job.scraped_tracking,
                )
                result["api"] = api_resp
            elif channel_source == "playauto":
                api_resp = await _dispatch_playauto_invoice(order, job)
                result["api"] = api_resp
                if not api_resp.get("ok"):
                    raise RuntimeError(
                        api_resp.get("error") or "플레이오토 송장 전송 실패"
                    )
            else:
                # 미지원 채널 — DISPATCH_FAILED 로 명시
                raise RuntimeError(
                    f"미지원 채널: {channel_source} (마켓 송장 전송 미구현)"
                )

            job.status = STATUS_SENT
            job.dispatched_to_market_at = datetime.now(_UTC)
            job.dispatch_result = result
            session.add(job)

            # 마켓 전송 성공 시 주문 status 드롭다운을 "국내배송중"으로 갱신
            # (STATUS_MAP의 'shipping' = '국내배송중')
            if order.status != "shipping":
                order.status = "shipping"
                order.shipping_status = "국내배송중"
                order.shipped_at = datetime.now(_UTC)
                session.add(order)

            await session.commit()
            logger.info(
                f"[송장동기화] 마켓 전송 완료: order={order.id} channel={channel_source}"
            )
            return {"success": True, **result}

        except Exception as exc:
            job.status = STATUS_DISPATCH_FAILED
            job.last_error = f"dispatch 실패: {exc}"[:500]
            job.dispatch_result = {"error": str(exc), **result}
            session.add(job)
            await session.commit()
            logger.warning(
                f"[송장동기화] 마켓 전송 실패: order={order.id} ch={channel_source} "
                f"err={exc}"
            )
            return {"success": False, "error": str(exc), **result}


async def _dispatch_playauto_invoice(order, job) -> dict[str, Any]:
    """플레이오토 송장 전송 — EMP API send_invoice 호출.

    필수 사전 조건:
      - order.shipment_id 가 int 변환 가능한 PlayAuto Number
      - order.channel_id → SambaMarketAccount(api_key) 조회 가능
      - PlayAuto 택배사 코드 lookup 가능
    """
    from backend.domain.samba.account.model import SambaMarketAccount
    from backend.domain.samba.proxy.playauto import PlayAutoClient

    try:
        pa_number = int(order.shipment_id or 0)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": f"플레이오토 Number 형식 오류: shipment_id={order.shipment_id}",
        }
    if not pa_number:
        return {"ok": False, "error": "플레이오토 Number 없음(shipment_id 미설정)"}

    # 채널 계정 → API Key
    if not order.channel_id:
        return {"ok": False, "error": "플레이오토 채널 계정 미연결(channel_id 없음)"}

    async with get_write_session() as acc_session:
        account = await acc_session.get(SambaMarketAccount, order.channel_id)
    if not account:
        return {"ok": False, "error": "플레이오토 마켓 계정 조회 실패"}
    pa_extras = account.additional_fields or {}
    pa_api_key = pa_extras.get("apiKey", "") or account.api_key or ""
    if not pa_api_key:
        return {"ok": False, "error": "플레이오토 API Key 미설정"}

    pa_client = PlayAutoClient(pa_api_key)
    try:
        deliv_codes = await pa_client.get_deliv_codes()
    except Exception as exc:
        return {"ok": False, "error": f"플레이오토 택배사 코드 조회 실패: {exc}"}

    # 한글 택배사명 → T-code 매핑 (order.py 의 _playauto_carrier_candidates 와 동일 로직)
    def _norm(s: str) -> str:
        return (s or "").replace(" ", "").strip()

    courier_name = (job.scraped_courier or "").strip()
    aliases = {
        "CJ대한통운": ["대한통운", "CJ택배", "씨제이대한통운", "CJGLS"],
        "대한통운": ["CJ대한통운", "CJ택배", "씨제이대한통운", "CJGLS"],
        "한진택배": ["한진", "HANJIN"],
        "롯데택배": ["롯데", "현대택배"],
        "로젠택배": ["로젠"],
        "우체국택배": ["우체국", "우체국소포"],
    }
    wanted_names = {_norm(courier_name)} | {
        _norm(a) for a in aliases.get(courier_name, [])
    }

    sender_code = ""
    for row in deliv_codes or []:
        row_name = _norm(
            row.get("name") or row.get("Name") or row.get("deliveryCompanyName") or ""
        )
        if row_name and row_name in wanted_names:
            sender_code = row.get("code", "") or row.get("Code", "")
            if sender_code:
                break
    if not sender_code:
        return {
            "ok": False,
            "error": f"플레이오토 택배사 코드 미매칭: {courier_name}",
        }

    try:
        results = await pa_client.send_invoice(
            invoices=[
                {
                    "number": pa_number,
                    "sender": sender_code,
                    "senderno": job.scraped_tracking,
                }
            ],
            change_state=False,
            overwrite=True,
        )
    except Exception as exc:
        return {"ok": False, "error": f"플레이오토 API 호출 실패: {exc}"}

    # 응답 파싱: {status, msg} 직접 또는 {"성공 유형N":{...}} 래핑 모두 대응
    for r in results or []:
        if not isinstance(r, dict):
            continue
        candidates = (
            [r]
            if "status" in r
            else [v for v in r.values() if isinstance(v, dict) and "status" in v]
        )
        for c in candidates:
            if str(c.get("status", "")).lower() == "true":
                return {"ok": True, "raw": results}
    # 모든 결과가 실패
    err_msgs = []
    for r in results or []:
        if isinstance(r, dict):
            err_msgs.append(str(r.get("msg") or r))
    return {
        "ok": False,
        "error": "; ".join(err_msgs)[:400] or "플레이오토 송장 전송 실패",
    }

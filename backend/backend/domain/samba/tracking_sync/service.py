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
    STATUS_WRONG_ACCOUNT,
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
        # orderDetail 페이지는 조회 실패(선물/직배 무관) → giftBoxDetail?type=snd 통일
        return f"https://www.lotteon.com/p/order/claim/giftBoxDetail?odNo={ord_no}&type=snd"
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
        # 롯데ON 선물주문은 일반 배송조회 페이지에 송장이 노출되지 않음 — 명시적 거부
        if actual_site == "LOTTEON" and ",gift," in _tags:
            return {
                "success": True,
                "skipped": True,
                "reason": "롯데ON 선물주문은 송장 추출 대상이 아닙니다",
            }
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
            # PENDING 만 만료 — DISPATCHED(확장앱이 이미 받아 처리 중)는 보호해서 끝까지 처리.
            # 무신사 잡은 직렬화 + SPA 응답 60~120초 걸리므로 진행 중 잡 닫으면 결과 손실.
            # 사용자가 송장수집 빠르게 다시 누를 때 진행 중 잡까지 만료되던 회귀 차단.
            stale_stmt = select(SambaTrackingSyncJob).where(
                SambaTrackingSyncJob.order_id == order_id,
                SambaTrackingSyncJob.status == STATUS_PENDING,
            )
            stale_existed = False
            for stale in (await session.execute(stale_stmt)).scalars().all():
                stale.status = STATUS_FAILED
                stale.last_error = "강제 재큐잉으로 만료 처리"
                stale.updated_at = datetime.now(_UTC)
                stale_existed = True
            # DISPATCHED 잡이 이미 있으면 중복 큐잉 안 함 (진행 중 보호)
            dispatched_stmt = (
                select(SambaTrackingSyncJob)
                .where(
                    SambaTrackingSyncJob.order_id == order_id,
                    SambaTrackingSyncJob.status == STATUS_DISPATCHED,
                )
                .limit(1)
            )
            dispatched = (await session.execute(dispatched_stmt)).scalars().first()
            if dispatched:
                return {
                    "success": True,
                    "skipped": True,
                    "reason": "이미 처리 중인 잡이 있어 강제 재큐잉 스킵",
                    "jobId": dispatched.id,
                }
            _ = stale_existed  # 컨벤션 유지용

        # owner_device_id 미사용 — 어느 PC가 폴링하든 잡을 가져갈 수 있게 None 으로 적재.
        # 확장앱이 받아 현재 로그인 계정으로 시도 → 다른 계정 주문이면 패스(NO_TRACKING).
        # 계정 분리 운영은 사용자가 PC별 로그인으로 관리.

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

        request_id, _future = await SourcingQueue.add_tracking_job(
            site=actual_site,
            url=url,
            order_id=order.id,
            sourcing_order_number=order.sourcing_order_number,
            owner_device_id=None,
            sourcing_account_id=order.sourcing_account_id or None,
        )

        # 2) DB row 생성
        job = SambaTrackingSyncJob(
            tenant_id=order.tenant_id,
            order_id=order.id,
            sourcing_site=actual_site,
            sourcing_order_number=order.sourcing_order_number,
            sourcing_account_id=order.sourcing_account_id,
            owner_device_id=None,
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
    # 이번 배치에서 생성/재사용된 잡 id — 프론트가 모달에 고정 표시할 때 사용
    job_ids: list[str] = []

    # 송장수집 트리거 = "리셋 + 새 batch" 시맨틱.
    # 옛 sourcing_job tracking 잡 + 옛 tracking_sync_job PENDING/DISPATCHED 모두 만료/취소해서
    # 모달 리스트 = 큐 카운트 = 새 batch 1:1 매칭 보장.
    # (자동 재큐잉 도미노로 인한 "시작하자마자 FAILED" 회귀 차단)
    if force:
        from sqlalchemy import text as _text

        async with get_write_session() as _reset_session:
            _now = datetime.now(_UTC)
            # 1) sourcing_job: 옛 tracking pending 모두 expired (확장앱 폴링 큐 비우기)
            _src_res = await _reset_session.execute(
                _text(
                    "UPDATE samba_sourcing_job SET status='expired', "
                    "completed_at=:now, error='새 batch 시작으로 만료' "
                    "WHERE job_type='tracking' AND status='pending'"
                ),
                {"now": _now},
            )
            # 2) tracking_sync_job: 옛 PENDING/DISPATCHED 모두 CANCELLED
            _tsj_res = await _reset_session.execute(
                _text(
                    "UPDATE samba_tracking_sync_job SET status=:cancelled, "
                    "last_error='새 batch 시작으로 취소', updated_at=:now "
                    "WHERE status IN (:pending, :dispatched)"
                ),
                {
                    "cancelled": STATUS_CANCELLED,
                    "pending": STATUS_PENDING,
                    "dispatched": STATUS_DISPATCHED,
                    "now": _now,
                },
            )
            await _reset_session.commit()
            logger.info(
                f"[송장동기화] 큐 리셋: sourcing_job expired={_src_res.rowcount} "
                f"tracking_sync_job cancelled={_tsj_res.rowcount}"
            )

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
            # 계정별 그룹화 적재 — 확장앱이 같은 계정 잡을 연속으로 받게 해서
            # ensureLoggedIn 자동 스왑 횟수를 "계정 수"만큼만 발생시킨다.
            # NULLS LAST 로 계정 미지정 잡은 뒤로 밀어둠.
            # 모달 리스트 정렬(paid_at ASC)과 dispatch 순서 일치시키기 위해 ASC 통일.
            # DESC면 모달 1번(가장 오래된)이 실제로는 마지막에 dispatch → 사용자 혼란.
            .order_by(
                SambaOrder.sourcing_account_id.asc().nulls_last(),
                date_col.asc(),
            )
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
            jid = res.get("jobId")
            if jid:
                job_ids.append(jid)
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
        "job_ids": job_ids,
    }


async def retry_failed_jobs(
    tenant_id: Optional[str] = None,
    days: int = 7,
) -> dict[str, Any]:
    """WRONG_ACCOUNT / FAILED / DISPATCH_FAILED 잡들을 재큐잉.

    enqueue_pending_orders 와 다른 점:
    - 송장 미입력 주문이 아니라 "송장 잡이 실패 상태" 인 주문만 대상
    - SCRAPED 상태 잡은 dispatch_pending_to_market 로 별도 처리
    """
    from sqlalchemy import select as sa_select

    retry_target_statuses = [
        STATUS_WRONG_ACCOUNT,
        STATUS_FAILED,
        STATUS_DISPATCH_FAILED,
    ]

    _KST = timezone(timedelta(hours=9))
    _today_kst = datetime.now(_KST).replace(hour=0, minute=0, second=0, microsecond=0)
    since = (_today_kst - timedelta(days=days - 1)).astimezone(_UTC)

    queued = 0
    skipped = 0
    errors: list[str] = []
    job_ids: list[str] = []

    async with get_write_session() as session:
        stmt = (
            sa_select(SambaTrackingSyncJob.order_id)
            .where(
                SambaTrackingSyncJob.status.in_(retry_target_statuses),
                SambaTrackingSyncJob.created_at >= since,
            )
            .distinct()
        )
        if tenant_id:
            stmt = stmt.where(SambaTrackingSyncJob.tenant_id == tenant_id)
        order_ids = [row[0] for row in (await session.execute(stmt)).all()]

    for order_id in order_ids:
        try:
            res = await enqueue_for_order(order_id, force=True)
            jid = res.get("jobId")
            if jid:
                job_ids.append(jid)
            if res.get("skipped"):
                skipped += 1
            elif res.get("success"):
                queued += 1
            else:
                errors.append(f"{order_id}: {res.get('error')}")
        except Exception as exc:
            errors.append(f"{order_id}: {exc}")

    logger.info(
        f"[송장동기화] 실패 재수집: target={len(order_ids)} queued={queued} "
        f"skipped={skipped} errors={len(errors)}"
    )
    return {
        "success": True,
        "target": len(order_ids),
        "queued": queued,
        "skipped": skipped,
        "errors": errors[:20],
        "job_ids": job_ids,
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

        # 모달 닫기 등으로 이미 취소된 잡은 결과 폐기 — 상태 덮어쓰기 차단
        if job.status == STATUS_CANCELLED:
            logger.info(
                f"[송장동기화] 취소된 잡 결과 폐기: request_id={request_id} job_id={job.id}"
            )
            return {"success": False, "status": STATUS_CANCELLED, "reason": "취소된 잡"}

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
            # 캡챠/미발송/계정불일치/실패 — 재시도 여지 두기
            reason = (error or "송장번호 없음")[:500]
            job.last_error = reason
            reason_lc = reason.lower()
            # 계정불일치 — 확장앱이 현재 로그인된 소싱처 계정으로 해당 주문 못 찾음
            # 운영자가 해당 계정 PC에서 재시도하거나 다른 PC 폴링 대기 필요
            if (
                "wrong_account" in reason_lc
                or "not_my_order" in reason_lc
                or "account_mismatch" in reason_lc
                or "계정불일치" in reason
                or "다른 계정" in reason
            ):
                job.status = STATUS_WRONG_ACCOUNT
                # 메시지 표준화 — 운영자가 모달에서 보고 즉시 매핑 확인 가능하게 명확화.
                # [정책 2026-05-16] 자동 다른 계정 순회 안 함 (무신사 보안 차단 위험).
                job.last_error = (
                    "⚠ 계정불일치 — 등록된 소싱처 계정 매핑이 잘못됐을 가능성. "
                    "운영자가 주문관리에서 진짜 무신사 계정 확인 후 sourcing_account_id 재매핑 필요."
                )
            elif (
                "captcha" in reason_lc
                or "미발송" in reason
                or "배송대기" in reason
                or "no_tracking" in reason_lc
            ):
                job.status = STATUS_NO_TRACKING
                # 메시지 표준화 — UI 오류/메모 컬럼에 "배송대기중" 같은 raw 에러 대신 명확한 한국어.
                if "captcha" in reason_lc:
                    job.last_error = "캡챠 발생 — 수동 처리 필요"
                else:
                    job.last_error = "미발송 — 소싱처 송장 미도착"
            else:
                job.status = STATUS_FAILED
            session.add(job)
            await session.commit()

            # [자동 재큐잉 2026-05-16] timeout/unexpected_page/abnormal_access 등 일시적 오류는
            # 최대 3회까지 자동 재큐잉 (1시간 내 FAILED 카운트 기반). 무신사 SPA 가 산발적으로
            # 응답 안 하거나 다른 페이지로 자동 리다이렉트되는 케이스 자동 복구.
            # wrong_account 는 매핑 오류 가능성 커서 자동 재시도 제외 (운영자 매핑 확인 필요).
            # 폭주 방지: 같은 order 의 1시간 이내 FAILED 잡이 3건 이상이면 재큐잉 차단.
            # 자동 재큐잉 영구 제거 (2026-05-18).
            # 이전에 timeout/unexpected_page 자동 재큐잉을 force=True 로 호출했더니
            # 같은 order 옛 PENDING이 만료되면서 모달이 "시작하자마자 FAILED 도배" 상태가 됨.
            # 재시도는 다음 사용자 트리거 / 자동실행 사이클에서 자연스럽게 처리.
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

        # [통일 2026-05-16] 마켓별 분기 인라인 → dispatch_service.send_invoice_to_market 으로 이관.
        # ship_order 라우터(수동)와 자동 dispatch 가 동일 service 사용 → 자격증명/필드 차이 사고 차단.
        from backend.domain.samba.order.dispatch_service import send_invoice_to_market

        try:
            market_sent, market_msg = await send_invoice_to_market(
                order,
                job.scraped_courier or "",
                job.scraped_tracking,
                session,
            )
            result["api"] = {"market_sent": market_sent, "message": market_msg}
            if not market_sent:
                raise RuntimeError(market_msg or "마켓 송장 전송 실패")

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


async def _resolve_lottehome_creds(channel_id: Optional[str]) -> dict[str, Any]:
    """롯데홈쇼핑 자격증명 해석 — account.additional_fields → settings 폴백.

    SambaMarketAccount(channel_id) 우선, 없으면 settings의
    'lottehome_credentials' / 'store_lottehome' 키 사용.
    """
    from backend.domain.samba.account.model import SambaMarketAccount
    from backend.domain.samba.shipment.dispatcher import _get_setting

    creds: dict[str, Any] = {}
    db_creds: dict[str, Any] = {}

    async with get_write_session() as session:
        # settings 폴백 로드
        for key in ("lottehome_credentials", "store_lottehome"):
            val = await _get_setting(session, key)
            if isinstance(val, dict) and val:
                db_creds = val
                break

        if channel_id:
            account = await session.get(SambaMarketAccount, channel_id)
            if account:
                extra = account.additional_fields or {}
                if isinstance(extra, dict) and (
                    extra.get("userId")
                    or extra.get("password")
                    or extra.get("agncNo")
                    or extra.get("env")
                ):
                    creds = {**db_creds, **extra}
        if not creds:
            creds = db_creds

    return creds if isinstance(creds, dict) else {}


async def _dispatch_lottehome_invoice(order, job) -> dict[str, Any]:
    """롯데홈쇼핑 송장 전송 — registDeliver.lotte (sfin = 출고확정).

    필수 사전 조건:
      - order.ext_order_number = "ord_no:ord_dtl_sn" 형식 (콜론 구분)
        (없으면 ord_no 단독 → ord_dtl_sn 추정 불가로 실패)
      - 자격증명 (userId/password/env) 해석 가능
    """
    from backend.domain.samba.proxy.lottehome import (
        LotteHomeClient,
        lottehome_courier_code,
    )

    # 신규 수집 데이터: ext_order_number = "ord_no:ord_dtl_sn"
    # 구버전 폴백: shipment_id 에 동일 형식이 들어있는 경우도 허용
    raw = (order.ext_order_number or "").strip() or (order.shipment_id or "").strip()
    if not raw:
        return {"ok": False, "error": "롯데홈쇼핑 주문번호(ext_order_number) 없음"}

    # ord_no:ord_dtl_sn 또는 ord_no/ord_dtl_sn 형식 모두 허용
    sep = ":" if ":" in raw else ("/" if "/" in raw else None)
    if not sep:
        return {
            "ok": False,
            "error": (
                "롯데홈쇼핑은 'ord_no:ord_dtl_sn' 형식이 필요합니다 — "
                "구주문이면 재수집 후 재시도 필요 "
                f"(현재값={raw})"
            ),
        }
    parts = [p.strip() for p in raw.split(sep, 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return {"ok": False, "error": f"주문번호 파싱 실패: {raw}"}
    ord_no, ord_dtl_sn = parts[0], parts[1]

    courier_code = lottehome_courier_code(job.scraped_courier or "")
    if courier_code == "99" and (job.scraped_courier or ""):
        logger.warning(
            f"[송장동기화][롯데홈] 택배사 코드 매칭 실패 → 99(기타) 폴백 "
            f"name={job.scraped_courier}"
        )

    creds = await _resolve_lottehome_creds(getattr(order, "channel_id", None))
    if not creds.get("userId") or not creds.get("password"):
        return {"ok": False, "error": "롯데홈쇼핑 자격증명(userId/password) 미설정"}

    client = LotteHomeClient(
        user_id=creds.get("userId", ""),
        password=creds.get("password", ""),
        agnc_no=creds.get("agncNo", ""),
        env=creds.get("env", "test"),
    )
    try:
        api_resp = await client.send_invoice(
            ord_no=ord_no,
            ord_dtl_sn=ord_dtl_sn,
            courier_code=courier_code,
            tracking_number=job.scraped_tracking or "",
        )
    except Exception as exc:
        return {"ok": False, "error": f"롯데홈쇼핑 API 호출 실패: {exc}"}

    if api_resp.get("ok"):
        return {"ok": True, "raw": api_resp}
    return {
        "ok": False,
        "error": f"롯데홈쇼핑 응답 result={api_resp.get('result')}",
        "raw": api_resp,
    }

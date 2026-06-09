"""SambaWave Order domain model."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, Index, Integer, String, text
from sqlmodel import Column, DateTime, Field, SQLModel, Text

from ulid import ULID


def generate_order_id() -> str:
    return f"ord_{ULID()}"


def generate_order_number() -> str:
    now = datetime.now(tz=timezone.utc)
    date_part = now.strftime("%y%m%d%H%M")
    import random

    rand_part = str(random.randint(0, 999)).zfill(3)
    return f"{date_part}{rand_part}"


class SambaOrder(SQLModel, table=True):
    """주문 테이블."""

    __tablename__ = "samba_order"
    __table_args__ = (
        # 11번가 1주문 다중상품 대응 (issue #208): order_number + ord_prd_seq 조합 unique
        # ord_prd_seq NULL은 PG가 distinct로 취급 → 타 마켓(NULL) 영향 없음.
        Index(
            "uq_order_tenant_number_seq",
            "tenant_id",
            "order_number",
            "ord_prd_seq",
            unique=True,
        ),
        # 롯데ON 중복 차단 — 동일 테넌트 내 (od_no, od_seq) 단위 unique.
        # channel_id 포함 시 동일 API key를 공유하는 2개 마켓계정이 같은 주문을
        # 양쪽 채널에 중복 저장하던 사고 발생(2026-05-25). channel_id 제거로
        # 테넌트 전역 단일 행 보장.
        Index(
            "ix_samba_order_lotteon_line",
            "tenant_id",
            "od_no",
            "od_seq",
            unique=True,
            postgresql_where=text("source = 'lotteon'"),
        ),
        Index("ix_samba_order_tenant_paid_at", "tenant_id", "paid_at"),
    )

    id: str = Field(
        default_factory=generate_order_id,
        primary_key=True,
        max_length=30,
    )
    # 테넌트 격리
    tenant_id: Optional[str] = Field(
        default=None, sa_column=Column(String, index=True, nullable=True)
    )

    order_number: str = Field(
        default_factory=generate_order_number,
        sa_column=Column(Text, nullable=False, index=True),
    )

    # 연결 정보
    channel_id: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True, index=True)
    )
    channel_name: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    product_id: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True, index=True)
    )
    product_name: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    product_image: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    product_option: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    coupang_display_name: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    source_url: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    source_site: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    # 판매처 별칭 — PlayAuto 1 채널 × 다 site_id 구조 (예: "GS이숍(캐논)", "롯데홈쇼핑(037800LT)").
    # source_site 는 진짜 소싱처 코드(MUSINSA/LOTTEON/SSG 등)만 들어가도록 분리.
    sales_channel_alias: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    # 수집상품 직접 참조 (근본적 연결)
    collected_product_id: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True, index=True)
    )

    # 고객 정보
    customer_name: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    # 주문자명 (수취인 customer_name과 다를 수 있음 — 선물하기 등)
    orderer_name: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    customer_phone: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    customer_address: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    # 상세주소(동/호/층 등) — 마켓 API가 base/detail 분리 제공하는 경우 별도 저장
    # 분리 미제공 마켓(eBay, 플레이오토 EMP)은 NULL 유지하고 customer_address에 단일 문자열 저장
    customer_address_detail: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    # 우편번호 — 화면 확인용. 복사 버튼은 customer_address만 복사하도록 frontend에서 분리.
    customer_postal_code: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # 수량/금액
    quantity: int = Field(default=1)
    sale_price: float = Field(default=0)
    # 고객결제금액 (할인 적용 후 실제 고객이 결제한 금액)
    # 롯데ON: slAmt - fvrAmtSum
    # 다른 마켓: 미설정 시 sale_price 폴백 사용 (UI 단)
    total_payment_amount: Optional[float] = Field(default=None)
    cost: float = Field(default=0)
    shipping_fee: float = Field(default=0)
    fee_rate: float = Field(default=0)
    revenue: float = Field(default=0)
    profit: float = Field(default=0)
    profit_rate: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # 상태
    status: str = Field(
        default="pending",
        sa_column=Column(Text, nullable=False, index=True),
    )
    payment_status: str = Field(
        default="completed",
        sa_column=Column(Text, nullable=False),
    )
    shipping_status: str = Field(
        default="preparing",
        sa_column=Column(Text, nullable=False),
    )
    return_status: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # 배송 정보
    shipping_company: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    tracking_number: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    # 반품 회수 송장 (소싱처 회수조회에서 자동 수집 — CS 답변용, 마켓 전송 안 함)
    return_collect_courier: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    return_collect_tracking: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    return_collect_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    customer_note: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    notes: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))

    # 타마켓 주문번호
    ext_order_number: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # 소싱처 구매주문번호
    sourcing_order_number: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # 소싱처 주문계정 ID
    sourcing_account_id: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # 액션 태그 (no_price/no_stock/direct/kkadaegi/gift)
    action_tag: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # 롯데ON 라인 키 (동일 주문 내 다른 옵션 식별)
    od_no: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    od_seq: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    proc_seq: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    sitm_no: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))

    # 무신사 주문옵션번호 — 마이페이지 trace URL의 ord_opt_no 파라미터.
    # ord_no만으로는 deliveryInfo API 호출 불가, 옵션번호 함께 필요.
    # 확장앱이 마이페이지 API 가로채서 매핑 캡처 → 백엔드 저장.
    musinsa_ord_opt_no: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # 11번가 라인 키 (판매불가처리/취소승인 등 클레임 API 필수 파라미터)
    ord_prd_seq: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # 쿠팡 옵션 ID (송장업로드 /orders/invoices 본문 필수 파라미터)
    vendor_item_id: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # 쿠팡 취소/반품 사유 (이슈 #246 — returnRequests API 응답 매핑)
    # receiptId: 후속 승인/거부 API 호출 핵심 ID
    # faultByType: CUSTOMER/VENDOR/COUPANG/WMS/GENERAL
    # releaseStatus: Y(출고됨)/N(미출고)/S(출고중지됨)/A(이미출고)
    cancel_reason_code: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    cancel_reason_text: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    cancel_reason_category1: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    cancel_reason_category2: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    cancel_fault_by: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    cancel_receipt_id: Optional[int] = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    cancel_release_status: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    cancel_release_stop_status: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    cancel_requested_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    # 출처
    source: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    # issue #393 — 미매칭 주문 중복체크 filter_by_async(shipment_id=...) 가
    # Seq Scan 으로 per-account 300초 타임아웃 유발. 인덱스로 Index Scan 화.
    shipment_id: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True, index=True)
    )

    # 고객 결제시간 (대시보드/날짜 범위 조회의 핵심 필터 — 인덱스 필수)
    paid_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True, index=True),
    )

    # Timestamps
    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )
    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )
    shipped_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    delivered_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


# 송장수집·진행현황 모달에서 "취소/반품/교환"으로 분류해 제외하는 status 영문 enum 집합.
# 페이지 필터 "취소/반품/교환 제외"가 사용하는 기준과 동일.
EXCLUDED_ORDER_STATUSES: tuple[str, ...] = (
    "cancel_requested",
    "cancelling",
    "cancelled",
    "return_requested",
    "returning",
    "returned",
    "return_completed",
    "exchange_requested",
    "exchanging",
    "exchanged",
    "exchange_pending",
    "exchange_done",
    "ship_failed",
    "undeliverable",
)

# 배송이 이미 진행/종료된 단계 — shipping_status(마켓 원본 한글)에 이 키워드 포함 시 제외.
# 화면 배지 변환 규칙(OrdersTable.tsx:309)과 일치시키기 위해 raw 값을 모두 포함:
#   raw "송장전송완료" → 배지 "국내배송중", raw "국내배송중" → 배지 "국내배송중"
SHIPPED_SHIPPING_STATUS_KEYWORDS: tuple[str, ...] = (
    "배송중",
    "배송완료",
    "구매확정",
    "국내배송중",
    "송장전송완료",
)


# ---------------------------------------------------------------------------
# 발주/송장 차단 가드 (취소요청 누락 사고 방지)
# ---------------------------------------------------------------------------
# 이 상태값에 해당하면 신규 발주·송장 dispatch 절대 진행 금지.
CANCEL_BLOCKED_STATUSES: frozenset[str] = frozenset(
    {"cancel_requested", "cancelling", "cancelled"}
)


class OrderCancelledError(RuntimeError):
    """주문이 취소 단계라 발주/송장 진행이 차단됐음을 알리는 명시적 예외."""

    def __init__(self, order_id: str, status: str, shipping_status: str = "") -> None:
        self.order_id = order_id
        self.status = status
        self.shipping_status = shipping_status
        super().__init__(
            f"주문 {order_id} 취소상태({status}/{shipping_status}) — 발주·송장 차단"
        )


def is_order_cancelled(order: "SambaOrder") -> bool:
    """주문이 취소 단계인지 판단. status enum 또는 한글 shipping_status 둘 다 검사."""
    status = (order.status or "").lower().strip()
    shipping_status = (order.shipping_status or "").strip()
    if status in CANCEL_BLOCKED_STATUSES:
        return True
    # 영문 enum이 아직 동기화 안 된 케이스 대비 — 한글 shipping_status에 "취소" 포함이면 차단.
    if "취소" in shipping_status:
        return True
    return False


def assert_order_dispatchable(order: "SambaOrder") -> None:
    """발주/송장 진입점 공통 가드. 취소 단계면 OrderCancelledError 발생."""
    if is_order_cancelled(order):
        raise OrderCancelledError(
            order.id, order.status or "", order.shipping_status or ""
        )


# ---------------------------------------------------------------------------
# 자동 발주취소 트리거 — SQLAlchemy event listener
# ---------------------------------------------------------------------------
# 마켓 폴러/동기화 코드에서 order.status 가 'cancel_requested' 또는 'cancelling' 로
# 변경되는 순간 자동 캐치 → 소싱처 발주취소 잡 발행.
#
# 폴러별 wiring (쿠팡/eBay/SSG/PlayAuto/스마트스토어 등 6+곳) 누락 위험을
# 단일 listener 로 해소. status 변경 모두 commit 직전 hook 으로 트리거.
#
# 가드 (helper 내부 4중):
#  - 새 status 가 취소요청/취소중 인지
#  - prev_status 가 동일하지 않은지 (실제 진입 시점만)
#  - source_site / sourcing_order_number 존재
#  - shipping_status 배송키워드 미포함 (배송 후 자동취소 차단)
#  - 동일 (site, ord_no) cancel_order 잡 in-flight 미존재 (멱등성)
#
# fire-and-forget asyncio.create_task — DB 트랜잭션과 분리.
def _register_auto_cancel_trigger() -> None:
    """앱 부팅 시 1회 호출. SQLAlchemy after_flush event 등록."""
    import asyncio

    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from sqlalchemy.orm.attributes import get_history

    from backend.utils.logger import logger as _log

    _TRIGGER_STATUSES = {"cancel_requested", "cancelling"}

    # 한글 shipping_status → 내부 status enum 매핑.
    # status='pending'(주문접수) 인데 마켓 shipping_status 가 취소단계로 넘어온 경우
    # status 도 자동 동기화. (전송로직/대시보드/오토튠이 status 기준으로 동작하므로 필수)
    _PENDING_AUTO_FIX_MAP = {
        "취소완료": "cancelled",
        "취소요청": "cancel_requested",
    }

    @event.listens_for(Session, "before_flush")
    def _normalize_pending_cancel(session, flush_context, instances) -> None:  # noqa: ARG001
        for obj in list(session.new) + list(session.dirty):
            if not isinstance(obj, SambaOrder):
                continue
            cur_status = (obj.status or "").strip().lower()
            if cur_status != "pending":
                continue
            ship = (obj.shipping_status or "").strip()
            new_status = _PENDING_AUTO_FIX_MAP.get(ship)
            if not new_status:
                continue
            obj.status = new_status
            obj.updated_at = datetime.now(tz=timezone.utc)

    @event.listens_for(Session, "after_flush")
    def _on_session_flush(session, flush_context) -> None:  # noqa: ARG001
        for obj in session.dirty:
            if not isinstance(obj, SambaOrder):
                continue
            try:
                hist = get_history(obj, "status")
            except Exception:
                continue
            if not hist.has_changes():
                continue
            new_status = (obj.status or "").strip().lower()
            if new_status not in _TRIGGER_STATUSES:
                continue
            prev_list = hist.deleted or []
            prev_status = (prev_list[0] if prev_list else "") or ""
            if (prev_status or "").strip().lower() == new_status:
                continue
            if not obj.source_site or not obj.sourcing_order_number:
                continue
            # async task 발행 — 현재 loop 없으면 skip (대부분 FastAPI async 컨텍스트)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                _log.debug(f"[자동취소] order={obj.id} async loop 없음 — skip")
                continue
            # lazy import — 순환 회피
            from backend.domain.samba.proxy.sourcing_queue import (
                SourcingQueue as _SQ,
            )

            loop.create_task(
                _SQ.maybe_trigger_auto_cancel(
                    order_id=obj.id,
                    source_site=obj.source_site,
                    sourcing_order_number=obj.sourcing_order_number,
                    sourcing_account_id=obj.sourcing_account_id,
                    new_status=new_status,
                    shipping_status=obj.shipping_status,
                    prev_status=prev_status,
                )
            )


class SambaDailyUnshippedSnapshot(SQLModel, table=True):
    """일별 미발송(송장 대기) 스냅샷 — 대시보드 "최근 일주일 매출" 미발송 칼럼 데이터.

    매일 0시 크론(daily_maintenance.task_daily_unshipped_snapshot)에서
    "현재 트레일링 7일 송장 대기수"(송장 진행현황 모달 '대기'와 동일 산식)를
    그날의 snapshot_date(KST)로 저장.

    오늘 행은 대시보드 엔드포인트가 라이브로 재계산하고, 과거 행은 이 스냅샷을 사용.
    스냅샷 없는 과거일은 None(프론트 "-" 표시) — 거짓 0 채움 금지.
    """

    __tablename__ = "samba_daily_unshipped_snapshot"

    snapshot_date: str = Field(
        sa_column=Column(String(10), primary_key=True),
        description="YYYY-MM-DD (KST)",
    )
    unshipped_count: int = Field(
        sa_column=Column(Integer, nullable=False, server_default="0")
    )
    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )

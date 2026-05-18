"""SambaWave Order API router."""

import asyncio
import re
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import (
    get_read_session,
    get_read_session_dependency,
    get_write_session_dependency,
)
from backend.domain.samba.cache import cache
from backend.domain.samba.tenant.middleware import get_optional_tenant_id
from backend.domain.samba.order.model import SambaOrder
from backend.domain.samba.order.playauto_alias import (
    normalize_playauto_alias_code,
    parse_playauto_alias_entry,
)
from backend.domain.samba.order.repository import SambaOrderRepository
from backend.domain.samba.order.service import SambaOrderService
from backend.dtos.samba.order import (
    FetchProductImageRequest,
    OrderCreate,
    OrderStatusUpdate,
    OrderUpdate,
)
from backend.utils.logger import logger

router = APIRouter(prefix="/orders", tags=["samba-orders"])


# ── 매칭 캐시(_mpn_cache) 모듈 전역 TTL 캐시 ──
# 매 sync마다 collected_product 12.6만건 SELECT + 인덱싱(약 7초) 부담을
# 60초 TTL로 한 번만 빌드해 재사용. 신규 cp 등록은 60초 안에 매칭됨.
_MPN_CACHE_TTL_SEC = 60.0
# (by_global, by_account) 튜플 — by_account는 정확 매칭(account_id, product_no) 인덱스
_mpn_cache_data: tuple[dict[str, dict], dict[str, dict]] | None = None
_mpn_cache_built_at: float = 0.0
_mpn_cache_lock = asyncio.Lock()


async def _get_mpn_cache(
    session, sourcing_urls: dict
) -> tuple[dict[str, dict], dict[str, dict]]:
    """market_product_no → collected_product 인덱스 (TTL 60초).

    리턴: (by_global, by_account)
      - by_global[product_no]            = entry  (기존 호환 키, 충돌 시 entry["ambiguous"]=True)
      - by_account[f"{account_id}:{no}"] = entry  (정확 매칭용, 충돌 없음)

    SELECT 전용이므로 write session(외부 마켓 API 동안 idle in transaction
    timeout으로 죽을 수 있음)에 의존하지 않고 내부에서 별도 read session을
    연다. 인자 ``session``은 호환을 위해 유지되며 사용되지 않는다.
    """
    import time as _t

    from sqlalchemy import text as _sa_text

    global _mpn_cache_data, _mpn_cache_built_at
    async with _mpn_cache_lock:
        now = _t.monotonic()
        if (
            _mpn_cache_data is not None
            and (now - _mpn_cache_built_at) < _MPN_CACHE_TTL_SEC
        ):
            return _mpn_cache_data
        async with get_read_session() as _read_sess:
            _cp_result = await _read_sess.execute(
                _sa_text(
                    "SELECT id, source_site, site_product_id, images, market_product_nos, source_url, category "
                    "FROM samba_collected_product WHERE market_product_nos IS NOT NULL"
                )
            )
            _cp_rows = _cp_result.fetchall()
        new_cache: dict[str, dict] = {}
        new_by_account: dict[str, dict] = {}
        _ambiguous_count = 0
        for _row in _cp_rows:
            _cpid, _site, _spid, _imgs, _mpnos, _src_url, _cat = _row
            if not (_mpnos and isinstance(_mpnos, dict)):
                continue
            _thumb = _imgs[0] if _imgs and isinstance(_imgs, list) and _imgs else ""
            _olink = _src_url or (
                sourcing_urls.get(_site, "").format(_spid)
                if _site in sourcing_urls and _spid
                else ""
            )
            # account_id별 등록된 site_ids 모음 — `{account_id}_sites` 키 패턴.
            # 신규 등록 액션에서 이 키에 [site_id, ...] 저장 (Phase 3에서 구현).
            # 기존 cp는 _sites 키 없음 → cache value의 site_ids_by_account = {} 유지 →
            # 매칭 시 호환 모드(site_id 검증 안 함).
            _sites_by_account: dict[str, list[str]] = {}
            for _k, _v in _mpnos.items():
                if _k.endswith("_sites") and isinstance(_v, list):
                    _account_id = _k[: -len("_sites")]
                    _sites_by_account[_account_id] = [str(s) for s in _v if s]

            for _k, _v in _mpnos.items():
                if not _v or _k.endswith("_qa") or _k.endswith("_sites"):
                    continue
                # _origin 키도 인덱싱한다 — 스마트스토어 주문 product_id 에는
                # channelProductNo 대신 originProductNo 가 들어오는 케이스가 있어
                # 매칭 실패 → source_site/source_url 공란 저장 사고가 반복되어 추가.
                # 정확 매칭 인덱스 키는 account_id 로 정규화하여 동일 account 의
                # 두 키(channel/origin) 가 동일 entry 를 가리키도록 통일.
                if _k.endswith("_origin"):
                    _account_key = _k[: -len("_origin")]
                else:
                    _account_key = str(_k)
                if isinstance(_v, dict):
                    _values = [
                        _v.get("smartstoreChannelProductNo"),
                        _v.get("originProductNo"),
                        _v.get("channelProductNo"),
                    ]
                else:
                    _values = [_v]
                for _sub_v in _values:
                    if not _sub_v:
                        continue
                    _key = str(_sub_v)
                    # 글로벌 인덱스 — 충돌 감지 (다른 cp가 같은 키 차지 시 ambiguous)
                    _existing_global = new_cache.get(_key)
                    if not _existing_global:
                        _entry = {
                            "collected_product_id": _cpid,
                            "source_site": _site,
                            "product_image": _thumb,
                            "original_link": _olink,
                            "category": _cat or "",
                            "site_ids_by_account": dict(_sites_by_account),
                        }
                        new_cache[_key] = _entry
                    elif _existing_global.get("collected_product_id") != _cpid:
                        # 다른 cp가 같은 글로벌 키를 차지 → 글로벌 매칭 거부 표시
                        if not _existing_global.get("ambiguous"):
                            _ambiguous_count += 1
                        _existing_global["ambiguous"] = True
                    else:
                        # 같은 cp 내 여러 키 — site_ids만 보강
                        for acc, sites in _sites_by_account.items():
                            _existing_global["site_ids_by_account"].setdefault(
                                acc, []
                            ).extend(
                                s
                                for s in sites
                                if s
                                not in _existing_global["site_ids_by_account"].get(
                                    acc, []
                                )
                            )
                    # 정확 매칭 인덱스 — (account_id, product_no) 쌍은 충돌 거의 없음.
                    # 첫 번째 entry 유지(같은 키에 여러 cp 등록 시 가장 오래된 것 우선).
                    _acc_key = f"{_account_key}:{_key}"
                    if _acc_key not in new_by_account:
                        new_by_account[_acc_key] = {
                            "collected_product_id": _cpid,
                            "source_site": _site,
                            "product_image": _thumb,
                            "original_link": _olink,
                            "category": _cat or "",
                            "site_ids_by_account": dict(_sites_by_account),
                        }
        _mpn_cache_data = (new_cache, new_by_account)
        _mpn_cache_built_at = now
        logger.info(
            f"[주문동기화] _mpn_cache 재빌드 완료 — global={len(new_cache):,} "
            f"by_account={len(new_by_account):,} ambiguous={_ambiguous_count:,} "
            f"TTL={_MPN_CACHE_TTL_SEC}s"
        )
        return _mpn_cache_data


ACTIVE_ORDER_STATUSES = (
    "new_order",
    "invoice_printed",
    "pending",
    "preparing",
    "wait_ship",
    "arrived",
)
EXCLUDED_ORDER_STATUSES = (
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
    "shipping",
    "delivered",
    "confirmed",
)
PENDING_ORDER_STATUSES = (
    "pending",
    "preparing",
    "wait_ship",
    "arrived",
    "ship_failed",
    "undeliverable",
)

# 취소요청 알람 — 마켓에서 취소 신호(shipping_status='취소요청'/'취소완료')가 들어왔지만
# 우리 내부 status는 아직 처리/배송 단계라 발주·송장 등록 사고 위험이 있는 케이스.
# UI 라벨 기준: 주문접수/상품준비중/배송대기중/사무실도착/국내배송중/송장전송실패/배송완료
CANCEL_ALERT_SHIPPING_STATUSES = ("취소요청", "취소완료")
CANCEL_ALERT_TARGET_STATUSES = (
    "pending",
    "preparing",
    "wait_ship",
    "arrived",
    "shipping",
    "ship_failed",
    "delivered",
)


def _build_cancel_alert_clause():
    """알람 카운트와 알람 필터에서 공통으로 쓰는 WHERE 조각.

    조건: 마켓 shipping_status 가 '취소요청'/'취소완료' + 우리 내부 status는 아직 처리/배송 단계
      → 발주·송장 등록 사고 위험. 운영자가 보고 막아야 할 미처리 케이스.

    내부 status='cancel_requested'는 운영자가 이미 인지하고 드롭박스를 전환한 상태라
    더 이상 발주/송장이 나가지 않으므로 알람 대상에서 제외.
    """
    from sqlalchemy import and_

    return and_(
        SambaOrder.shipping_status.in_(CANCEL_ALERT_SHIPPING_STATUSES),
        SambaOrder.status.in_(CANCEL_ALERT_TARGET_STATUSES),
    )


def _build_action_tag_filter(action_tag: str):
    from sqlalchemy import func, or_

    normalized = action_tag.strip()
    if not normalized:
        return None

    padded = f",{normalized},"
    action_expr = func.concat(",", func.coalesce(SambaOrder.action_tag, ""), ",")
    return or_(
        SambaOrder.action_tag == normalized,
        action_expr.like(f"{padded}%"),
        action_expr.like(f"%{padded}"),
        action_expr.like(f"%{padded}%"),
    )


class PaginatedOrdersResponse(BaseModel):
    items: list[SambaOrder]
    total_count: int
    total_sale: float
    pending_count: int


def _read_service(session: AsyncSession) -> SambaOrderService:
    return SambaOrderService(SambaOrderRepository(session))


def _write_service(session: AsyncSession) -> SambaOrderService:
    return SambaOrderService(SambaOrderRepository(session))


async def _resolve_market_filter_channel_ids(
    session: AsyncSession,
    market_filter: Optional[str],
    tenant_id: Optional[str],
) -> list[str]:
    if not market_filter or not market_filter.startswith("type:"):
        return []

    from sqlalchemy import or_, select

    from backend.domain.samba.account.model import SambaMarketAccount

    market_type = market_filter[5:]
    stmt = select(SambaMarketAccount.id).where(
        SambaMarketAccount.market_type == market_type
    )
    if tenant_id is not None:
        stmt = stmt.where(
            or_(
                SambaMarketAccount.tenant_id == tenant_id,
                SambaMarketAccount.tenant_id == None,  # noqa: E711
            )
        )
    result = await session.execute(stmt)
    return [row[0] for row in result.all() if row[0]]


async def _build_order_filters(
    session: AsyncSession,
    tenant_id: Optional[str],
    *,
    market_filter: str = "",
    site_filter: str = "",
    account_filter: str = "",
    market_status: str = "",
    status_filter: str = "",
    input_filter: str = "",
    invoice_filter: str = "",
    registration_filter: str = "",
    search_text: str = "",
    search_category: str = "customer",
) -> list[Any]:
    from sqlalchemy import and_, func, or_

    filters: list[Any] = []

    if tenant_id is not None:
        filters.append(
            or_(
                SambaOrder.tenant_id == tenant_id,
                SambaOrder.tenant_id == None,  # noqa: E711
            )
        )

    if market_filter:
        if market_filter.startswith("acc:"):
            filters.append(SambaOrder.channel_id == market_filter[4:])
        elif market_filter.startswith("type:"):
            channel_ids = await _resolve_market_filter_channel_ids(
                session, market_filter, tenant_id
            )
            if channel_ids:
                filters.append(SambaOrder.channel_id.in_(channel_ids))
            else:
                filters.append(SambaOrder.channel_id == "__no_matching_channel__")

    if site_filter:
        normalized_site_filter = site_filter.replace(" ", "")
        normalized_source_site = func.replace(
            func.coalesce(SambaOrder.source_site, ""), " ", ""
        )
        # GSSHOP 통합 필터 — DB에는 GSShop/GS이숍/GS이숍(고경) 등 변형 혼재 → 모두 매칭
        gs_aliases = {"GSSHOP", "GSShop", "GS이숍", "GS이샵", "GS샵"}
        if normalized_site_filter in gs_aliases:
            from backend.core.sql_safe import escape_like

            gs_filters = []
            for alias in gs_aliases:
                safe_alias = escape_like(alias)
                gs_filters.append(normalized_source_site == alias)
                gs_filters.append(
                    normalized_source_site.like(f"{safe_alias}(%", escape="\\")
                )
            filters.append(or_(*gs_filters))
        elif "(" in normalized_site_filter:
            filters.append(normalized_source_site == normalized_site_filter)
        else:
            # site_filter 는 외부 입력 — `%`/`_` 메타 escape 후 ESCAPE '\\' 명시.
            # `(%` 는 의도된 wildcard 이므로 보존, escape 는 site_filter 부분만 적용.
            from backend.core.sql_safe import escape_like

            safe_site = escape_like(normalized_site_filter)
            filters.append(
                or_(
                    normalized_source_site == normalized_site_filter,
                    normalized_source_site.like(f"{safe_site}(%", escape="\\"),
                )
            )
    if account_filter:
        filters.append(SambaOrder.sourcing_account_id == account_filter)
    if market_status:
        filters.append(SambaOrder.shipping_status == market_status)

    if status_filter:
        if status_filter == "active":
            filters.append(SambaOrder.status.in_(ACTIVE_ORDER_STATUSES))
        elif status_filter == "cancel_return_excluded":
            # status 컬럼만 기준 — shipping_status 는 일절 관여 금지
            filters.append(~SambaOrder.status.in_(EXCLUDED_ORDER_STATUSES))
        elif status_filter == "pending":
            filters.append(SambaOrder.status.in_(PENDING_ORDER_STATUSES))
        elif status_filter == "cancel_alert":
            # 알람 카운트와 동일한 조건 — 발주·송장 사고 위험 케이스
            filters.append(_build_cancel_alert_clause())
        else:
            filters.append(SambaOrder.status == status_filter)

    if input_filter == "has_order":
        filters.append(
            and_(
                SambaOrder.sourcing_order_number != None,  # noqa: E711
                SambaOrder.sourcing_order_number != "",
            )
        )
    elif input_filter == "no_order":
        filters.append(
            or_(
                SambaOrder.sourcing_order_number == None,  # noqa: E711
                SambaOrder.sourcing_order_number == "",
            )
        )
    elif input_filter == "has_invoice":
        filters.append(
            and_(
                SambaOrder.tracking_number != None,  # noqa: E711
                SambaOrder.tracking_number != "",
            )
        )
    elif input_filter == "no_invoice":
        filters.append(
            or_(
                SambaOrder.tracking_number == None,  # noqa: E711
                SambaOrder.tracking_number == "",
            )
        )
    elif input_filter in {
        "no_price",
        "no_stock",
        "direct",
        "kkadaegi",
        "gift",
        "staff_a",
        "staff_b",
    }:
        action_filter = _build_action_tag_filter(input_filter)
        if action_filter is not None:
            filters.append(action_filter)

    # 송장필터 — 입력필터와 독립적으로 동작 (이중 선택 가능)
    if invoice_filter == "has_invoice":
        filters.append(
            and_(
                SambaOrder.tracking_number != None,  # noqa: E711
                SambaOrder.tracking_number != "",
            )
        )
    elif invoice_filter == "no_invoice":
        filters.append(
            or_(
                SambaOrder.tracking_number == None,  # noqa: E711
                SambaOrder.tracking_number == "",
            )
        )

    # 등록필터 — 입력필터와 독립적으로 동작 (이중 선택 가능)
    if registration_filter == "registered":
        # collected_product_id가 있거나, "미등록 입력"으로 source_url/product_image를 채운 주문도 등록된 것으로 간주
        filters.append(
            or_(
                SambaOrder.collected_product_id != None,  # noqa: E711
                and_(
                    SambaOrder.source_url != None,  # noqa: E711
                    SambaOrder.source_url != "",
                ),
                and_(
                    SambaOrder.product_image != None,  # noqa: E711
                    SambaOrder.product_image != "",
                ),
            )
        )
    elif registration_filter == "unregistered":
        # collected_product_id가 없고 source_url/product_image도 모두 비어있어야 미등록
        filters.append(
            and_(
                SambaOrder.collected_product_id == None,  # noqa: E711
                or_(
                    SambaOrder.source_url == None,  # noqa: E711
                    SambaOrder.source_url == "",
                ),
                or_(
                    SambaOrder.product_image == None,  # noqa: E711
                    SambaOrder.product_image == "",
                ),
            )
        )

    normalized_search = search_text.strip()
    if normalized_search:
        # search_text 는 외부 입력 — `%`/`_` 메타 escape 후 ESCAPE '\\' 명시.
        from backend.core.sql_safe import escape_like

        safe_q = escape_like(normalized_search.lower())
        lower_q = f"%{safe_q}%"
        if search_category == "product":
            filters.append(SambaOrder.product_name.ilike(lower_q, escape="\\"))
        elif search_category == "product_id":
            filters.append(SambaOrder.product_id.ilike(lower_q, escape="\\"))
        elif search_category == "order_number":
            # 상품주문번호(order_number) + 묶음주문번호(shipment_id) + 외부주문번호(ext_order_number) 모두 매칭
            filters.append(
                or_(
                    SambaOrder.order_number.ilike(lower_q, escape="\\"),
                    SambaOrder.shipment_id.ilike(lower_q, escape="\\"),
                    SambaOrder.ext_order_number.ilike(lower_q, escape="\\"),
                )
            )
        else:
            # 고객명(수령인) + 주문자명 모두 매칭 — 선물하기 등 수령인≠주문자 케이스 대응
            filters.append(
                or_(
                    SambaOrder.customer_name.ilike(lower_q, escape="\\"),
                    SambaOrder.orderer_name.ilike(lower_q, escape="\\"),
                )
            )

    return filters


def _build_order_sort(sort_by: str):
    from sqlalchemy import func

    date_col = func.coalesce(SambaOrder.paid_at, SambaOrder.created_at)
    sort_map = {
        "date_asc": date_col.asc(),
        "profit_desc": SambaOrder.profit.desc(),
        "profit_asc": SambaOrder.profit.asc(),
        "price_desc": SambaOrder.sale_price.desc(),
        "price_asc": SambaOrder.sale_price.asc(),
    }
    return sort_map.get(sort_by, date_col.desc())


async def _run_paginated_order_query(
    session: AsyncSession,
    base_filters: list[Any],
    *,
    skip: int,
    limit: int,
    sort_by: str,
    extra_filters: Optional[list[Any]] = None,
) -> PaginatedOrdersResponse:
    from sqlalchemy import case, func, select

    sale_expr = func.coalesce(SambaOrder.total_payment_amount, SambaOrder.sale_price, 0)
    query_filters = [*base_filters, *(extra_filters or [])]

    total_stmt = select(
        func.count().label("total_count"),
        func.coalesce(func.sum(sale_expr), 0).label("total_sale"),
        func.coalesce(
            func.sum(case((SambaOrder.status.in_(PENDING_ORDER_STATUSES), 1), else_=0)),
            0,
        ).label("pending_count"),
    )
    if query_filters:
        total_stmt = total_stmt.where(*query_filters)
    total_row = (await session.execute(total_stmt)).one()

    items_stmt = select(SambaOrder)
    if query_filters:
        items_stmt = items_stmt.where(*query_filters)
    items_stmt = (
        items_stmt.order_by(_build_order_sort(sort_by)).offset(skip).limit(limit)
    )
    items = list((await session.execute(items_stmt)).scalars().all())

    return PaginatedOrdersResponse(
        items=items,
        total_count=int(total_row.total_count or 0),
        total_sale=float(total_row.total_sale or 0),
        pending_count=int(total_row.pending_count or 0),
    )


@router.get("", response_model=list[SambaOrder])
async def list_orders(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    status: Optional[str] = None,
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    from sqlmodel import select

    # tenant_id가 있으면 해당 테넌트 주문만 조회
    if tenant_id is not None:
        stmt = (
            select(SambaOrder)
            .order_by(SambaOrder.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        from sqlalchemy import or_

        stmt = stmt.where(
            or_(
                SambaOrder.tenant_id == tenant_id,
                SambaOrder.tenant_id == None,  # noqa: E711
            )
        )
        if status:
            stmt = stmt.where(SambaOrder.status == status)
        result = await session.execute(stmt)
        return result.scalars().all()
    svc = _read_service(session)
    return await svc.list_orders(skip=skip, limit=limit, status=status)


@router.get("/dashboard-stats")
async def dashboard_stats(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """대시보드 집계 — DB에서 SUM/COUNT 후 결과만 반환 (빠름)."""
    # 캐시 조회 (TTL 60초, tenant별 키)
    _cache_key = f"order:dashboard-stats-v3:{tenant_id or '_global'}"
    _cached = await cache.get(_cache_key)
    if _cached:
        return _cached

    from sqlalchemy import select, func, case, and_, extract, text, or_
    from datetime import datetime, timedelta, timezone as tz

    # 이행매출 대상 상태 (주문상태 드롭박스 기준)
    FULFILLMENT_STATUSES = (
        "pending",
        "wait_ship",
        "processing",
        "arrived",
        "ship_failed",
        "shipping",
        "shipped",
        "delivered",
        "exchanged",
        "exchanging",
        "exchange_requested",
    )

    # KST 기준 (UTC+9)
    KST = tz(timedelta(hours=9))
    now = datetime.now(KST).replace(tzinfo=None)
    this_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 1:
        last_month_start = this_month_start.replace(year=now.year - 1, month=12)
    else:
        last_month_start = this_month_start.replace(month=now.month - 1)
    week_ago = (now - timedelta(days=6)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # 날짜 기준: 고객결제일(paid_at)만 사용, KST 변환
    order_date = SambaOrder.paid_at + text("INTERVAL '9 hours'")

    # 금월 집계
    this_month_q = select(
        func.count().label("count"),
        func.coalesce(func.sum(SambaOrder.sale_price), 0).label("sales"),
        func.coalesce(
            func.sum(
                case(
                    (
                        SambaOrder.status.in_(FULFILLMENT_STATUSES),
                        SambaOrder.sale_price,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("fulfillment_sales"),
        func.sum(
            case(
                (SambaOrder.status.in_(FULFILLMENT_STATUSES), 1),
                else_=0,
            )
        ).label("fulfillment_count"),
    ).where(SambaOrder.paid_at != None, order_date >= this_month_start)  # noqa: E711
    if tenant_id is not None:
        this_month_q = this_month_q.where(
            or_(
                SambaOrder.tenant_id == tenant_id,
                SambaOrder.tenant_id == None,  # noqa: E711
            )
        )
    tm = (await session.execute(this_month_q)).one()

    # 전월 집계
    last_month_q = select(
        func.count().label("count"),
        func.coalesce(func.sum(SambaOrder.sale_price), 0).label("sales"),
        func.coalesce(
            func.sum(
                case(
                    (
                        SambaOrder.status.in_(FULFILLMENT_STATUSES),
                        SambaOrder.sale_price,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("fulfillment_sales"),
        func.sum(
            case(
                (SambaOrder.status.in_(FULFILLMENT_STATUSES), 1),
                else_=0,
            )
        ).label("fulfillment_count"),
    ).where(
        SambaOrder.paid_at != None,
        and_(order_date >= last_month_start, order_date < this_month_start),
    )  # noqa: E711
    if tenant_id is not None:
        last_month_q = last_month_q.where(
            or_(
                SambaOrder.tenant_id == tenant_id,
                SambaOrder.tenant_id == None,  # noqa: E711
            )
        )
    lm = (await session.execute(last_month_q)).one()

    # 최근 7일 일별 집계
    daily_q = (
        select(
            func.date(order_date).label("day"),
            func.count().label("count"),
            func.coalesce(func.sum(SambaOrder.sale_price), 0).label("sales"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            SambaOrder.status.in_(FULFILLMENT_STATUSES),
                            SambaOrder.sale_price,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("fulfillment_sales"),
            func.sum(
                case(
                    (SambaOrder.status.in_(FULFILLMENT_STATUSES), 1),
                    else_=0,
                )
            ).label("fulfillment_count"),
        )
        .where(SambaOrder.paid_at != None, order_date >= week_ago)  # noqa: E711
        .group_by(func.date(order_date))
    )
    if tenant_id is not None:
        daily_q = daily_q.where(
            or_(
                SambaOrder.tenant_id == tenant_id,
                SambaOrder.tenant_id == None,  # noqa: E711
            )
        )
    daily_rows = (await session.execute(daily_q)).all()
    weekly = []
    for i in range(7):
        d = week_ago + timedelta(days=i)
        day_str = d.strftime("%Y-%m-%d")
        row = next((r for r in daily_rows if str(r.day) == day_str), None)
        weekly.append(
            {
                "date": day_str,
                "sales": float(row.sales) if row else 0,
                "count": int(row.count) if row else 0,
                "fulfillmentSales": float(row.fulfillment_sales) if row else 0,
                "fulfillmentCount": int(row.fulfillment_count) if row else 0,
            }
        )

    # 월별 집계 (연간 12개월)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_q = (
        select(
            extract("month", order_date).label("month"),
            func.coalesce(func.sum(SambaOrder.sale_price), 0).label("sales"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            SambaOrder.status.in_(FULFILLMENT_STATUSES),
                            SambaOrder.sale_price,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("fulfillment_sales"),
        )
        .where(
            SambaOrder.paid_at != None,  # noqa: E711
            and_(
                order_date >= year_start,
                extract("year", order_date) == now.year,
            ),
        )
        .group_by(extract("month", order_date))
    )
    if tenant_id is not None:
        monthly_q = monthly_q.where(
            or_(
                SambaOrder.tenant_id == tenant_id,
                SambaOrder.tenant_id == None,  # noqa: E711
            )
        )
    monthly_rows = (await session.execute(monthly_q)).all()
    monthly = []
    for m in range(1, 13):
        row = next((r for r in monthly_rows if int(r.month) == m), None)
        monthly.append(
            {
                "month": m,
                "sales": float(row.sales) if row else 0,
                "fulfillmentSales": float(row.fulfillment_sales) if row else 0,
            }
        )

    # 최근 7일 신규등록/마켓삭제 상품 단위 일별 카운트 (KST 기준)
    # 신규등록: registered_accounts 0→≥1 전환 시점 (first_market_registered_at)
    # 마켓삭제: 품절 인식 이벤트(sold_out) 기준 — 1상품/1일 중복 제거
    from backend.api.v1.routers.samba.collector_common import (
        build_market_registered_conditions,
    )
    from backend.domain.samba.collector.model import SambaCollectedProduct

    reg_date = SambaCollectedProduct.first_market_registered_at + text(
        "INTERVAL '9 hours'"
    )
    new_reg_q = (
        select(
            func.date(reg_date).label("day"),
            func.count().label("cnt"),
        )
        .where(
            SambaCollectedProduct.first_market_registered_at != None,  # noqa: E711
            reg_date >= week_ago,
        )
        .group_by(func.date(reg_date))
    )
    if tenant_id is not None:
        new_reg_q = new_reg_q.where(
            or_(
                SambaCollectedProduct.tenant_id == tenant_id,
                SambaCollectedProduct.tenant_id == None,  # noqa: E711
            )
        )
    new_reg_rows = (await session.execute(new_reg_q)).all()
    new_reg_map = {str(r.day): int(r.cnt) for r in new_reg_rows}

    # 마켓삭제(이탈) 카운트: 품절 인식 이벤트 기준
    # — 품절 인식 시 다운스트림에서 전 마켓 자동 삭제/판매중지 처리되므로
    #   sold_out 이벤트 = 마켓 이탈 1회로 간주 (1상품/1일 중복 제거)
    from backend.domain.samba.warroom.model import SambaMonitorEvent

    sold_out_date = SambaMonitorEvent.created_at + text("INTERVAL '9 hours'")
    del_q = (
        select(
            func.date(sold_out_date).label("day"),
            func.count(func.distinct(SambaMonitorEvent.product_id)).label("cnt"),
        )
        .where(
            SambaMonitorEvent.event_type == "sold_out",
            SambaMonitorEvent.product_id != None,  # noqa: E711
            sold_out_date >= week_ago,
        )
        .group_by(func.date(sold_out_date))
    )
    if tenant_id is not None:
        # 멀티테넌시 필터: 상품의 tenant_id로 제한
        del_q = del_q.where(
            SambaMonitorEvent.product_id.in_(
                select(SambaCollectedProduct.id).where(
                    or_(
                        SambaCollectedProduct.tenant_id == tenant_id,
                        SambaCollectedProduct.tenant_id == None,  # noqa: E711
                    )
                )
            )
        )
    del_rows = (await session.execute(del_q)).all()
    del_map = {str(r.day): int(r.cnt) for r in del_rows}

    # 일별 누적 등록상품수: "지금 마켓에 1개 이상 등록된 상품수" 정의로 통일
    #   - 오늘(today_str): 실시간 build_market_registered_conditions 계산값
    #   - 과거 6일: samba_daily_registered_snapshot 테이블의 그날 0시 스냅샷
    #   - 스냅샷이 없는 과거일은 오늘값으로 평탄 채움
    #     (역산은 first_market_registered_at의 0→≥1 사이클 재진입 때문에
    #      오토튠 재등록 시 신규등록이 과대 집계돼 과거값이 비정상적으로 작아짐 — 사용 금지)
    from backend.domain.samba.collector.model import SambaDailyRegisteredSnapshot

    today_str = (week_ago + timedelta(days=6)).strftime("%Y-%m-%d")
    reg_count_map: dict[str, int] = {}

    # 마켓 1개 이상 등록된 상품수 (현재 시점) — KPI + 오늘 행에 사용
    market_registered_q = select(func.count(SambaCollectedProduct.id)).where(
        *build_market_registered_conditions(SambaCollectedProduct)
    )
    if tenant_id is not None:
        market_registered_q = market_registered_q.where(
            or_(
                SambaCollectedProduct.tenant_id == tenant_id,
                SambaCollectedProduct.tenant_id == None,  # noqa: E711
            )
        )
    market_registered_count = (await session.execute(market_registered_q)).scalar() or 0
    reg_count_map[today_str] = int(market_registered_count)

    # 과거 6일 스냅샷 일괄 조회
    past_dates = [(week_ago + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6)]
    snap_q = select(
        SambaDailyRegisteredSnapshot.snapshot_date,
        SambaDailyRegisteredSnapshot.registered_count,
    ).where(SambaDailyRegisteredSnapshot.snapshot_date.in_(past_dates))
    snap_rows = (await session.execute(snap_q)).all()
    snap_map = {r.snapshot_date: int(r.registered_count) for r in snap_rows}

    # 스냅샷이 있으면 사용, 없으면 오늘값으로 평탄 채움
    # — 매일 0시 TASK 6 누적되면 자연스럽게 진짜 스냅샷으로 대체됨
    today_count = int(market_registered_count)
    for d_str in past_dates:
        reg_count_map[d_str] = snap_map.get(d_str, today_count)

    # 일별 누적 수집상품수 = "그 날(말) 시점 삼바에 저장되어있는 전체 상품수"
    # 구현: 현재 total 에서 그 다음날 이후 created 된 행수를 빼서 역산 (1풀스캔 + 1범위스캔)
    total_collected_q = select(func.count(SambaCollectedProduct.id))
    if tenant_id is not None:
        total_collected_q = total_collected_q.where(
            or_(
                SambaCollectedProduct.tenant_id == tenant_id,
                SambaCollectedProduct.tenant_id == None,  # noqa: E711
            )
        )
    total_collected = int((await session.execute(total_collected_q)).scalar() or 0)

    created_kst = SambaCollectedProduct.created_at + text("INTERVAL '9 hours'")
    daily_new_q = (
        select(
            func.date(created_kst).label("day"),
            func.count().label("cnt"),
        )
        .where(
            SambaCollectedProduct.created_at != None,  # noqa: E711
            created_kst >= week_ago,
        )
        .group_by(func.date(created_kst))
    )
    if tenant_id is not None:
        daily_new_q = daily_new_q.where(
            or_(
                SambaCollectedProduct.tenant_id == tenant_id,
                SambaCollectedProduct.tenant_id == None,  # noqa: E711
            )
        )
    daily_new_rows = (await session.execute(daily_new_q)).all()
    daily_new_map = {str(r.day): int(r.cnt) for r in daily_new_rows}

    # 7일 누적 카운트: 오늘=total, 어제=total-(오늘신규), 그저께=어제-(어제신규) ...
    collected_count_map: dict[str, int] = {today_str: total_collected}
    running_total = total_collected
    for i in range(5, -1, -1):
        d_str = past_dates[i]
        next_d_str = past_dates[i + 1] if i + 1 < 6 else today_str
        running_total -= daily_new_map.get(next_d_str, 0)
        collected_count_map[d_str] = max(running_total, 0)

    for w in weekly:
        w["newRegistered"] = int(new_reg_map.get(w["date"], 0))
        w["marketDeleted"] = int(del_map.get(w["date"], 0))
        w["registeredCount"] = int(reg_count_map.get(w["date"], 0))
        w["collectedCount"] = int(collected_count_map.get(w["date"], 0))

    tm_fulfillment_rate = (
        round(int(tm.fulfillment_count or 0) / int(tm.count) * 100) if tm.count else 0
    )
    lm_fulfillment_rate = (
        round(int(lm.fulfillment_count or 0) / int(lm.count) * 100) if lm.count else 0
    )
    sales_change = (
        round(((float(tm.sales) - float(lm.sales)) / float(lm.sales)) * 100, 1)
        if lm.sales
        else 0
    )

    result = {
        "thisMonth": {
            "count": int(tm.count),
            "sales": float(tm.sales),
            "fulfillmentSales": float(tm.fulfillment_sales or 0),
            "fulfillmentCount": int(tm.fulfillment_count or 0),
            "fulfillment": tm_fulfillment_rate,
        },
        "lastMonth": {
            "count": int(lm.count),
            "sales": float(lm.sales),
            "fulfillmentSales": float(lm.fulfillment_sales or 0),
            "fulfillmentCount": int(lm.fulfillment_count or 0),
            "fulfillment": lm_fulfillment_rate,
        },
        "salesChange": sales_change,
        "weekly": weekly,
        "monthly": monthly,
        "marketRegisteredCount": int(market_registered_count),
    }
    # 캐시 TTL 5분 — 첫 로드는 무거우나 후속 로드는 즉시. 매출 집계는 1분 단위
    # 변화 의미 없고, 매 새로고침마다 풀스캔 도는 게 더 큰 비용.
    await cache.set(_cache_key, result, ttl=300)
    return result


@router.get("/search", response_model=list[SambaOrder])
async def search_orders(
    q: str = Query(..., min_length=1),
    session: AsyncSession = Depends(get_read_session_dependency),
):
    svc = _read_service(session)
    return await svc.search_orders(q)


@router.get("/by-date-range-paged", response_model=PaginatedOrdersResponse)
async def list_orders_by_date_range_paged(
    start: str = Query(..., description="start date YYYY-MM-DD"),
    end: str = Query(..., description="end date YYYY-MM-DD"),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=500),
    market_filter: str = Query(""),
    site_filter: str = Query(""),
    account_filter: str = Query(""),
    market_status: str = Query(""),
    status_filter: str = Query(""),
    input_filter: str = Query(""),
    invoice_filter: str = Query(""),
    registration_filter: str = Query(""),
    search_text: str = Query(""),
    search_category: str = Query("customer"),
    sort_by: str = Query("date_desc"),
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    from backend.utils import kst_date_range_to_utc

    start_dt, end_dt = kst_date_range_to_utc(start, end)
    filters = await _build_order_filters(
        session,
        tenant_id,
        market_filter=market_filter,
        site_filter=site_filter,
        account_filter=account_filter,
        market_status=market_status,
        status_filter=status_filter,
        input_filter=input_filter,
        invoice_filter=invoice_filter,
        registration_filter=registration_filter,
        search_text=search_text,
        search_category=search_category,
    )
    return await _run_paginated_order_query(
        session,
        filters,
        skip=skip,
        limit=limit,
        sort_by=sort_by,
        extra_filters=[
            SambaOrder.paid_at != None,  # noqa: E711
            SambaOrder.paid_at >= start_dt,
            SambaOrder.paid_at <= end_dt,
        ],
    )


@router.get("/by-collected-product-paged", response_model=PaginatedOrdersResponse)
async def list_orders_by_collected_product_paged(
    collected_product_id: str = Query(..., description="collected product ID"),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=500),
    market_filter: str = Query(""),
    site_filter: str = Query(""),
    account_filter: str = Query(""),
    market_status: str = Query(""),
    status_filter: str = Query(""),
    input_filter: str = Query(""),
    invoice_filter: str = Query(""),
    registration_filter: str = Query(""),
    search_text: str = Query(""),
    search_category: str = Query("customer"),
    sort_by: str = Query("date_desc"),
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    filters = await _build_order_filters(
        session,
        tenant_id,
        market_filter=market_filter,
        site_filter=site_filter,
        account_filter=account_filter,
        market_status=market_status,
        status_filter=status_filter,
        input_filter=input_filter,
        invoice_filter=invoice_filter,
        registration_filter=registration_filter,
        search_text=search_text,
        search_category=search_category,
    )
    return await _run_paginated_order_query(
        session,
        filters,
        skip=skip,
        limit=limit,
        sort_by=sort_by,
        extra_filters=[SambaOrder.collected_product_id == collected_product_id],
    )


@router.get("/by-date-range", response_model=list[SambaOrder])
async def list_orders_by_date_range(
    start: str = Query(..., description="시작일 YYYY-MM-DD"),
    end: str = Query(..., description="종료일 YYYY-MM-DD"),
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """기간별 주문 조회 — paid_at(고객결제일) 기준, 제한 없이 전체 반환."""
    from sqlalchemy import select as sa_select, or_
    from backend.utils import kst_date_range_to_utc

    start_dt, end_dt = kst_date_range_to_utc(start, end)

    stmt = (
        sa_select(SambaOrder)
        .where(
            SambaOrder.paid_at != None,  # noqa: E711
            SambaOrder.paid_at >= start_dt,
            SambaOrder.paid_at <= end_dt,
        )
        .order_by(SambaOrder.paid_at.desc())
    )
    if tenant_id is not None:
        stmt = stmt.where(
            or_(
                SambaOrder.tenant_id == tenant_id,
                SambaOrder.tenant_id == None,  # noqa: E711
            )
        )
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get("/by-collected-product", response_model=list[SambaOrder])
async def list_orders_by_collected_product(
    collected_product_id: str = Query(..., description="수집상품 ID (cp_ULID)"),
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """수집상품 ID로 해당 상품의 전체 주문 이력 조회."""
    from sqlalchemy import select as sa_select, func as sa_func, or_

    date_col = sa_func.coalesce(SambaOrder.paid_at, SambaOrder.created_at)
    stmt = (
        sa_select(SambaOrder)
        .where(SambaOrder.collected_product_id == collected_product_id)
        .order_by(date_col.desc())
    )
    if tenant_id is not None:
        stmt = stmt.where(
            or_(
                SambaOrder.tenant_id == tenant_id,
                SambaOrder.tenant_id == None,  # noqa: E711
            )
        )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# 한국어 택배사명 → 딜리버리트래커 carrier ID 매핑
SHIPPING_COMPANY_TO_CARRIER_ID: dict[str, str] = {
    "CJ대한통운": "kr.cjlogistics",
    "한진택배": "kr.hanjin",
    "롯데택배": "kr.lotte",
    "로젠택배": "kr.logen",
    "우체국택배": "kr.epost",
    "경동택배": "kr.kdexp",
    "대신택배": "kr.daesin",
    "일양로지스": "kr.ilyanglogis",
    "편의점택배": "kr.cvsnet",
    "합동택배": "kr.hdexp",
    "쿠팡택배": "kr.coupangls",
    "딜리박스": "kr.dilibox",
    "DHL": "de.dhl",
}


@router.get("/tracking")
async def get_tracking(
    carrier: str = Query(..., description="택배사 한국어명 (예: CJ대한통운)"),
    invoice: str = Query(..., description="운송장번호"),
):
    """딜리버리트래커 v1 API를 프록시하여 통합 배송조회 결과를 반환."""
    import httpx

    carrier_id = SHIPPING_COMPANY_TO_CARRIER_ID.get(carrier)
    if not carrier_id:
        raise HTTPException(400, f"지원하지 않는 택배사: {carrier}")

    invoice_clean = re.sub(r"[^0-9A-Za-z]", "", invoice or "")
    if not invoice_clean:
        raise HTTPException(400, "유효하지 않은 송장번호입니다")

    url = f"https://apis.tracker.delivery/carriers/{carrier_id}/tracks/{invoice_clean}"
    try:
        async with httpx.AsyncClient(timeout=10) as hc:
            resp = await hc.get(url)
    except httpx.HTTPError as e:
        logger.warning(
            "[tracking] 외부 API 통신 실패 %s/%s: %s", carrier, invoice_clean, e
        )
        raise HTTPException(502, "택배 조회 서비스에 연결할 수 없습니다")

    if resp.status_code == 404:
        raise HTTPException(
            404, "조회 결과가 없습니다 (송장번호/택배사를 확인해주세요)"
        )
    if resp.status_code >= 400:
        logger.warning(
            "[tracking] 비정상 응답 %s/%s status=%s body=%s",
            carrier,
            invoice_clean,
            resp.status_code,
            resp.text[:200],
        )
        raise HTTPException(502, "택배 조회 결과를 불러오지 못했습니다")

    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(502, "택배 조회 응답 형식 오류")

    progresses = data.get("progresses") or []
    return {
        "carrier_name": carrier,
        "carrier_id": carrier_id,
        "invoice": invoice_clean,
        "from_name": (data.get("from") or {}).get("name"),
        "to_name": (data.get("to") or {}).get("name"),
        "state": (data.get("state") or {}).get("text"),
        "events": [
            {
                "time": p.get("time"),
                "status": (p.get("status") or {}).get("text"),
                "status_code": (p.get("status") or {}).get("id"),
                "location": (p.get("location") or {}).get("name"),
                "description": p.get("description"),
            }
            for p in progresses
        ],
    }


@router.get("/find-by-number")
async def find_by_order_number(
    order_number: str = Query(...),
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """상품주문번호로 주문 조회."""
    svc = _read_service(session)
    order = await svc.repo.find_by_async(order_number=order_number)
    if not order:
        return None
    # 테넌트 소유권 검증
    if tenant_id is not None and order.tenant_id != tenant_id:
        raise HTTPException(403, "해당 주문에 대한 권한이 없습니다")
    return {"id": order.id, "order_number": order.order_number}


@router.post("/{order_id}/sync-tracking")
async def sync_order_tracking(order_id: str, force: bool = False) -> dict:
    """소싱처에서 운송장 추출 잡을 큐에 적재 (단건).

    force=True 면 이미 송장이 있어도 다시 큐잉.
    """
    from backend.domain.samba.tracking_sync.service import enqueue_for_order

    return await enqueue_for_order(order_id, force=force)


@router.post("/sync-tracking/bulk")
async def sync_order_tracking_bulk(
    limit: int = Query(500, ge=1, le=1000),
    days: int = Query(7, ge=1, le=90),
    force: bool = Query(False),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> dict:
    """미발송 주문 일괄 송장 추출 큐잉 — 최근 N일 + 소싱처 주문번호 있음 + 송장 미입력.

    force=True 면 기존 PENDING/DISPATCHED 잡(만료 좀비 포함)을 FAILED 로 닫고 새로 큐잉.
    """
    from backend.domain.samba.tracking_sync.service import enqueue_pending_orders

    return await enqueue_pending_orders(
        tenant_id=tenant_id, limit=limit, days=days, force=force
    )


@router.post("/tracking-sync/dispatch/bulk")
async def dispatch_tracking_bulk(dry_run: bool = False) -> dict:
    """SCRAPED + DISPATCH_FAILED 잡 전부 일괄 마켓 전송 (재시도 포함)."""
    from backend.domain.samba.tracking_sync.service import dispatch_pending_to_market

    return await dispatch_pending_to_market(dry_run=dry_run)


@router.post("/tracking-sync/retry-failed")
async def retry_failed_tracking_jobs(
    days: int = 7,
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> dict:
    """WRONG_ACCOUNT / FAILED / DISPATCH_FAILED 잡들을 자동 재큐잉.

    송장수집이 실패한 주문들만 모아서 다시 자동 로그인 + 송장 추출 시도.
    송장 미입력 주문 전체 재큐잉(sync-tracking/bulk)과 다른 점:
    - 미발송으로 끝난 잡은 제외 (실패한 것만)
    - 한 번에 빠르게 retry 트리거 가능
    """
    from backend.domain.samba.tracking_sync.service import retry_failed_jobs

    return await retry_failed_jobs(tenant_id=tenant_id, days=days)


@router.post("/tracking-sync/{job_id}/dispatch")
async def dispatch_tracking_to_market(job_id: str, dry_run: bool = False) -> dict:
    """추출 완료된(SCRAPED) 잡의 운송장을 마켓으로 push.

    dry_run=True (기본): 페이로드만 로그. False면 실제 마켓 API 호출.
    """
    from backend.domain.samba.tracking_sync.service import dispatch_to_market

    return await dispatch_to_market(job_id, dry_run=dry_run)


@router.get("/tracking-sync/recent")
async def list_recent_tracking_sync_jobs(
    limit: int = Query(50, ge=1, le=200),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> dict:
    """최근 송장 자동전송 잡 목록 + 상태 카운트.

    프론트가 일괄 송장수집 후 폴링해서 진행상황 보여주는 용도.
    SambaOrder (상품주문번호/고객명) + SambaSourcingAccount (소싱처 계정 라벨) LEFT JOIN.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import aliased
    from backend.db.orm import get_read_session
    from backend.domain.samba.order.model import (
        EXCLUDED_ORDER_STATUSES,
        SHIPPED_SHIPPING_STATUS_KEYWORDS,
        SambaOrder,
    )
    from backend.domain.samba.sourcing_account.model import SambaSourcingAccount
    from backend.domain.samba.tracking_sync.model import SambaTrackingSyncJob

    def _is_excluded(order_status, shipping_status) -> bool:
        """페이지 필터 '취소/반품/교환 제외 + 배송중/배송완료 제외' 와 동일 기준."""
        if order_status and order_status in EXCLUDED_ORDER_STATUSES:
            return True
        if shipping_status and any(
            kw in shipping_status for kw in SHIPPED_SHIPPING_STATUS_KEYWORDS
        ):
            return True
        return False

    async with get_read_session() as session:
        O = aliased(SambaOrder)
        A = aliased(SambaSourcingAccount)
        # 잡 + 주문 메타를 한 번에 가져와 Python에서 dedup → 카운트/리스트 일관 처리
        # 큐잉 필터(enqueue_pending_orders)와 100% 동일 조건 적용:
        #   2) sourcing_order_number 있음
        #   3) source_site 있음
        #   4) 최근 7일 (created_at >= now-7d)
        #   7) action_tag 에 'kkadaegi' 토큰 없음
        # 1/5/6 (송장 미입력 / 상태 제외 / 배송중·완료 제외) 은 Python loop 에서 처리.
        from datetime import timedelta, timezone
        from sqlalchemy import and_, func, not_, or_

        # KST 캘린더 7일 (오늘 포함 -6일) + paid_at(폴백 created_at) 기준
        _KST = timezone(timedelta(hours=9))
        _today_kst = datetime.now(_KST).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        _since = (_today_kst - timedelta(days=6)).astimezone(timezone.utc)
        _until = (_today_kst + timedelta(days=1)).astimezone(timezone.utc)
        action_tag_expr = func.concat(",", func.coalesce(O.action_tag, ""), ",")
        date_col = func.coalesce(O.paid_at, O.created_at)
        base_stmt = (
            select(
                SambaTrackingSyncJob,
                O.order_number,
                O.customer_name,
                O.channel_name,
                O.status,
                O.shipping_status,
                A.account_label,
                O.tracking_number,
                O.paid_at,
                O.action_tag,
            )
            .join(O, O.id == SambaTrackingSyncJob.order_id, isouter=True)
            .join(A, A.id == SambaTrackingSyncJob.sourcing_account_id, isouter=True)
            .where(
                and_(
                    O.sourcing_order_number.is_not(None),
                    O.sourcing_order_number != "",
                    # source_site 비어있어도 source_url / collected_product 로 추론 가능하면 포함
                    or_(
                        and_(O.source_site.is_not(None), O.source_site != ""),
                        and_(O.source_url.is_not(None), O.source_url != ""),
                        O.collected_product_id.is_not(None),
                    ),
                    date_col >= _since,
                    date_col < _until,
                    not_(action_tag_expr.like("%,kkadaegi,%")),
                    # 송장 채워졌어도 잡 자체는 표시 (수집 결과 확인용).
                    # 큐 적재 단계에서만 송장 있는 주문 제외 — enqueue_for_order 가 처리.
                )
            )
            .order_by(SambaTrackingSyncJob.updated_at.desc())
            .limit(limit * 10)
        )
        if tenant_id:
            base_stmt = base_stmt.where(SambaTrackingSyncJob.tenant_id == tenant_id)
        raw_rows = (await session.execute(base_stmt)).all()

        # order_id별 최신 1건만 선별 + 페이지 필터와 동일 기준 제외 +
        # 이미 송장 입력된 주문은 처리 대상 아니므로 제외 (모달 = "처리 필요" 잡만 표시)
        seen_order_ids: set[str] = set()
        result_rows = []
        counts: dict[str, int] = {}
        for row in raw_rows:
            j = row[0]
            order_status = row[4]
            shipping_status = row[5]
            order_tracking_number = row[7]
            if j.order_id in seen_order_ids:
                continue
            seen_order_ids.add(j.order_id)
            if _is_excluded(order_status, shipping_status):
                continue
            # 송장 채워진 주문은 모달 대상 아님 — "송장수집 = 송장 미입력건만 처리" 정책.
            # 외부 수동입력/이전 수집완료 무관하게 송장 있으면 숨김.
            if order_tracking_number:
                continue
            counts[j.status] = counts.get(j.status, 0) + 1
            if len(result_rows) < limit:
                result_rows.append(row)

    return {
        "counts": counts,
        "recent": [
            {
                "id": j.id,
                "orderId": j.order_id,
                "orderNumber": order_number or "",
                "customerName": customer_name or "",
                "channelName": channel_name or "",
                "site": j.sourcing_site,
                "sourcingOrderNumber": j.sourcing_order_number,
                "sourcingAccountLabel": account_label or "",
                "status": j.status,
                "courier": j.scraped_courier,
                "tracking": j.scraped_tracking,
                "lastError": j.last_error,
                "attempts": j.attempts,
                "updatedAt": j.updated_at.isoformat() if j.updated_at else None,
                "paidAt": paid_at.isoformat() if paid_at else None,
                "actionTag": action_tag or "",
            }
            for j, order_number, customer_name, channel_name, _os, _ss, account_label, _otn, paid_at, action_tag in result_rows
        ],
    }


@router.post("/tracking-sync/by-ids")
async def list_tracking_sync_jobs_by_ids(body: dict) -> dict:
    """송장수집 배치에 속한 잡들만 조회 — 모달 "이번 배치 고정" 용도.

    프론트가 일괄 송장수집 직후 받은 job_ids 를 그대로 전달.
    송장 채워진 행도 응답에 포함(상태 변화 추적용)하고, 순서는 paid_at ASC 로 고정.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import aliased
    from backend.db.orm import get_read_session
    from backend.domain.samba.order.model import SambaOrder
    from backend.domain.samba.sourcing_account.model import SambaSourcingAccount
    from backend.domain.samba.tracking_sync.model import SambaTrackingSyncJob

    raw_ids = body.get("job_ids") or []
    if not isinstance(raw_ids, list):
        raise HTTPException(400, "job_ids 는 배열이어야 합니다")
    job_ids: list[str] = [str(x) for x in raw_ids if x]
    if not job_ids:
        return {"counts": {}, "recent": []}
    if len(job_ids) > 1000:
        job_ids = job_ids[:1000]

    async with get_read_session() as session:
        from sqlalchemy import func

        O = aliased(SambaOrder)
        A = aliased(SambaSourcingAccount)
        date_col = func.coalesce(O.paid_at, O.created_at)
        stmt = (
            select(
                SambaTrackingSyncJob,
                O.order_number,
                O.customer_name,
                O.channel_name,
                A.account_label,
                O.paid_at,
                O.action_tag,
            )
            .join(O, O.id == SambaTrackingSyncJob.order_id, isouter=True)
            .join(A, A.id == SambaTrackingSyncJob.sourcing_account_id, isouter=True)
            .where(SambaTrackingSyncJob.id.in_(job_ids))
            .order_by(date_col.asc())
        )
        raw_rows = (await session.execute(stmt)).all()

    counts: dict[str, int] = {}
    items = []
    for row in raw_rows:
        j = row[0]
        order_number = row[1]
        customer_name = row[2]
        channel_name = row[3]
        account_label = row[4]
        paid_at = row[5]
        action_tag = row[6]
        counts[j.status] = counts.get(j.status, 0) + 1
        items.append(
            {
                "id": j.id,
                "orderId": j.order_id,
                "orderNumber": order_number or "",
                "customerName": customer_name or "",
                "channelName": channel_name or "",
                "site": j.sourcing_site,
                "sourcingOrderNumber": j.sourcing_order_number,
                "sourcingAccountLabel": account_label or "",
                "status": j.status,
                "courier": j.scraped_courier,
                "tracking": j.scraped_tracking,
                "lastError": j.last_error,
                "attempts": j.attempts,
                "updatedAt": j.updated_at.isoformat() if j.updated_at else None,
                "paidAt": paid_at.isoformat() if paid_at else None,
                "actionTag": action_tag or "",
            }
        )

    return {"counts": counts, "recent": items}


@router.post("/tracking-sync/cancel-batch")
async def cancel_tracking_sync_batch(body: dict) -> dict:
    """송장수집 모달 닫기 시 배치 잡 일괄 취소.

    PENDING/DISPATCHED 상태의 잡만 CANCELLED 로 전환. 이미 SCRAPED/SENT 등
    완료된 잡은 변경 안 함 (결과 보존). 확장앱이 in-flight 로 들고 있는 잡은
    apply_tracking_result 진입 시 상태가 CANCELLED 면 결과 폐기.
    """
    from sqlalchemy import update
    from datetime import datetime, timezone
    from backend.db.orm import get_write_session
    from backend.domain.samba.tracking_sync.model import (
        SambaTrackingSyncJob,
        STATUS_PENDING,
        STATUS_DISPATCHED,
        STATUS_CANCELLED,
    )

    raw_ids = body.get("job_ids") or []
    if not isinstance(raw_ids, list):
        raise HTTPException(400, "job_ids 는 배열이어야 합니다")
    job_ids: list[str] = [str(x) for x in raw_ids if x]
    if not job_ids:
        return {"cancelled": 0}
    if len(job_ids) > 1000:
        job_ids = job_ids[:1000]

    async with get_write_session() as session:
        stmt = (
            update(SambaTrackingSyncJob)
            .where(
                SambaTrackingSyncJob.id.in_(job_ids),
                SambaTrackingSyncJob.status.in_([STATUS_PENDING, STATUS_DISPATCHED]),
            )
            .values(
                status=STATUS_CANCELLED,
                last_error="모달 닫기로 배치 취소",
                updated_at=datetime.now(timezone.utc),
            )
        )
        result = await session.execute(stmt)
        await session.commit()
        return {"cancelled": result.rowcount or 0}


@router.get("/cancel-alert-count")
async def get_cancel_alert_count(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """아직 처리 안 한 취소요청 건수 반환.

    인지 누락 사고 방지가 목적. 조건은 _build_cancel_alert_clause() 와 동일.
    """
    from sqlalchemy import select, func
    from backend.domain.samba.order.model import SambaOrder as OrderModel

    stmt = select(func.count()).where(_build_cancel_alert_clause())
    if tenant_id is not None:
        stmt = stmt.where(OrderModel.tenant_id == tenant_id)
    # session.exec(select(func.count()))는 SQLModel에서 Row 객체를 반환해
    # FastAPI 직렬화가 실패한다(500). session.execute().scalar_one() 으로 정수만 추출.
    result = await session.execute(stmt)
    count = int(result.scalar_one())
    return {"count": count}


@router.get("/alarm-settings")
async def get_alarm_settings(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """취소알람 수집 주기 및 영업시간 설정 조회."""
    from backend.api.v1.routers.samba.proxy import _get_setting

    data = await _get_setting(session, "cancel_alarm_settings") or {}
    return {
        "hour": data.get("hour", 0),
        "min": data.get("min", 5),
        "sleep_start": data.get("sleep_start", "23:00"),
        "sleep_end": data.get("sleep_end", "07:00"),
    }


@router.post("/alarm-settings")
async def save_alarm_settings(
    body: dict,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """취소알람 수집 주기 및 영업시간 설정 저장."""
    from backend.api.v1.routers.samba.proxy import _set_setting

    await _set_setting(
        session,
        "cancel_alarm_settings",
        {
            "hour": int(body.get("hour", 0)),
            "min": int(body.get("min", 5)),
            "sleep_start": body.get("sleep_start", "23:00"),
            "sleep_end": body.get("sleep_end", "07:00"),
        },
    )
    return {"ok": True}


@router.get("/auto-sync-interval")
async def get_auto_sync_interval(
    session: AsyncSession = Depends(get_read_session_dependency),
) -> dict:
    """주문 자동수집 인터벌 설정 조회 (분 단위, 0=OFF)."""
    from backend.api.v1.routers.samba.proxy import _get_setting

    val = await _get_setting(session, "order_auto_sync_interval_minutes")
    try:
        minutes = int(val) if val is not None else 0
    except (TypeError, ValueError):
        minutes = 0
    return {"interval_minutes": minutes}


@router.post("/auto-sync-interval")
async def set_auto_sync_interval(
    body: dict,
    session: AsyncSession = Depends(get_write_session_dependency),
) -> dict:
    """주문 자동수집 인터벌 설정 저장 (분 단위, 0 이하면 OFF)."""
    from backend.api.v1.routers.samba.proxy import _set_setting

    try:
        minutes = int(body.get("interval_minutes", 0))
    except (TypeError, ValueError):
        minutes = 0
    if minutes < 0:
        minutes = 0
    await _set_setting(session, "order_auto_sync_interval_minutes", minutes)
    return {"interval_minutes": minutes}


@router.get("/auto-sync-history")
async def get_auto_sync_history(
    limit: int = 2,
    session: AsyncSession = Depends(get_read_session_dependency),
) -> dict:
    """주문 자동실행(order_sync 잡) 최근 이력 N건 요약.

    프론트 '주문 자동실행' 섹션에서 최근 수집 결과를 표시하기 위함.
    """
    from sqlalchemy import text as _t

    limit = max(1, min(int(limit or 2), 10))
    rows = await session.execute(
        _t(
            "SELECT id, status, created_at, started_at, completed_at, result, error "
            "FROM samba_jobs WHERE job_type = 'order_sync' "
            "ORDER BY created_at DESC LIMIT :lim"
        ),
        {"lim": limit},
    )
    items: list[dict] = []
    for r in rows.fetchall():
        job_id, status, created_at, started_at, completed_at, result, error = r
        result_dict = result if isinstance(result, dict) else {}
        results_list = result_dict.get("results") or []
        per_market: list[dict] = []
        for it in results_list:
            if not isinstance(it, dict):
                continue
            per_market.append(
                {
                    "account": it.get("account", ""),
                    "status": it.get("status", ""),
                    "synced": int(it.get("synced") or 0),
                    "fetched": int(it.get("fetched") or 0),
                    "message": (it.get("message") or "")[:200],
                }
            )
        duration_sec: int | None = None
        if started_at and completed_at:
            duration_sec = int((completed_at - started_at).total_seconds())
        ts = result_dict.get("tracking_sync") or {}
        tracking_summary: dict | None = None
        if isinstance(ts, dict) and ts:
            tracking_summary = {
                "success": bool(ts.get("success")),
                "queued": int(ts.get("queued") or 0),
                "skipped": int(ts.get("skipped") or 0),
                "jobs": int(ts.get("job_ids_count") or 0),
                "errors": [str(e)[:200] for e in (ts.get("errors") or [])][:3],
                "ran_at": ts.get("ran_at"),
            }
        items.append(
            {
                "job_id": job_id,
                "status": status,
                "created_at": created_at.isoformat() if created_at else None,
                "started_at": started_at.isoformat() if started_at else None,
                "completed_at": completed_at.isoformat() if completed_at else None,
                "duration_sec": duration_sec,
                "total_synced": int(result_dict.get("total_synced") or 0),
                "per_market": per_market,
                "tracking_sync": tracking_summary,
                "error": (error or "")[:300] if error else None,
            }
        )
    return {"items": items}


@router.get("/{order_id}", response_model=SambaOrder)
async def get_order(
    order_id: str,
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    svc = _read_service(session)
    order = await svc.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="주문을 찾을 수 없습니다")
    # 테넌트 소유권 검증
    if tenant_id is not None and order.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="해당 주문에 대한 권한이 없습니다")
    return order


@router.post("", response_model=SambaOrder, status_code=201)
async def create_order(
    body: OrderCreate,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    svc = _write_service(session)
    return await svc.create_order(body.model_dump(exclude_unset=True))


@router.patch("/{order_id}/link-product")
async def link_order_to_product(
    order_id: str,
    body: dict,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """주문에 수집상품 ID 연결 (지연 채움)."""
    cpid = body.get("collected_product_id", "")
    if not cpid:
        raise HTTPException(400, "collected_product_id 필수")
    from sqlalchemy import text as _t

    await session.execute(
        _t(
            "UPDATE samba_order SET collected_product_id = :cpid WHERE id = :oid AND collected_product_id IS NULL"
        ),
        {"cpid": cpid, "oid": order_id},
    )
    await session.commit()
    return {"ok": True}


@router.post("/backfill-product-links")
async def backfill_product_links(
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """기존 주문의 collected_product_id 일괄 백필."""
    from sqlalchemy import text as _t

    # market_product_nos에서 역매핑 캐시 빌드
    cp_rows = await session.execute(
        _t(
            "SELECT id, market_product_nos FROM samba_collected_product "
            "WHERE market_product_nos IS NOT NULL"
        )
    )
    mpn_map: dict[str, str] = {}
    for cpid, mpnos in cp_rows.fetchall():
        if not mpnos or not isinstance(mpnos, dict):
            continue
        for _v in mpnos.values():
            if not _v:
                continue
            if isinstance(_v, dict):
                for sv in [
                    _v.get("smartstoreChannelProductNo"),
                    _v.get("originProductNo"),
                    _v.get("channelProductNo"),
                ]:
                    if sv:
                        mpn_map[str(sv)] = cpid
            else:
                mpn_map[str(_v)] = cpid

    # collected_product_id가 없는 주문 조회
    null_orders = await session.execute(
        _t(
            "SELECT id, product_id FROM samba_order "
            "WHERE collected_product_id IS NULL AND product_id IS NOT NULL"
        )
    )
    linked = 0
    for oid, pid in null_orders.fetchall():
        cpid = mpn_map.get(str(pid))
        if cpid:
            await session.execute(
                _t(
                    "UPDATE samba_order SET collected_product_id = :cpid WHERE id = :oid"
                ),
                {"cpid": cpid, "oid": oid},
            )
            linked += 1
    await session.commit()
    return {"linked": linked, "total_cache": len(mpn_map)}


@router.post("/fix-musinsa-fashionplus-mismatch")
async def fix_musinsa_fashionplus_mismatch(
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """FashionPlus로 잘못 매칭된 무신사 주문 일괄 수정.

    상품명 끝 숫자가 MUSINSA site_product_id와 일치하는데
    collected_product_id가 FashionPlus 상품을 가리키는 주문을 찾아 수정한다.
    """
    import re as _re
    from sqlalchemy import text as _t

    # FashionPlus로 매칭된 주문 중 상품명 끝에 숫자가 있는 건 조회
    bad_orders = await session.execute(
        _t(
            "SELECT o.id, o.product_name, o.collected_product_id "
            "FROM samba_order o "
            "JOIN samba_collected_product cp ON cp.id = o.collected_product_id "
            "WHERE cp.source_site = 'FashionPlus' "
            "AND o.product_name ~ E'\\\\d{7,}\\\\s*$'"
        )
    )
    rows = bad_orders.fetchall()

    fixed = 0
    skipped = 0
    for oid, pname, old_cpid in rows:
        m = _re.search(r"(\d{7,})\s*$", pname or "")
        if not m:
            skipped += 1
            continue
        sid = m.group(1)

        # 동일 site_product_id를 가진 MUSINSA 상품 조회
        cp_row = await session.execute(
            _t(
                "SELECT id FROM samba_collected_product "
                "WHERE site_product_id = :sid AND source_site = 'MUSINSA' "
                "ORDER BY (market_product_nos IS NOT NULL) DESC, created_at ASC "
                "LIMIT 1"
            ),
            {"sid": sid},
        )
        correct_cp = cp_row.fetchone()
        if not correct_cp:
            skipped += 1
            continue

        await session.execute(
            _t(
                "UPDATE samba_order "
                "SET collected_product_id = :cpid, source_site = 'MUSINSA' "
                "WHERE id = :oid"
            ),
            {"cpid": correct_cp[0], "oid": oid},
        )
        fixed += 1

    await session.commit()
    return {"fixed": fixed, "skipped": skipped, "total_checked": len(rows)}


@router.put("/{order_id}", response_model=SambaOrder)
async def update_order(
    order_id: str,
    body: OrderUpdate,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    from sqlalchemy import text as _t

    svc = _write_service(session)
    data = body.model_dump(exclude_unset=True)
    order = await svc.update_order(order_id, data)
    if not order:
        raise HTTPException(status_code=404, detail="주문을 찾을 수 없습니다")

    # source_url/product_image 변경 시 동일 product_id+channel_name 주문 일괄 업데이트
    batch_fields = {
        k: v for k, v in data.items() if k in ("source_url", "product_image")
    }
    if batch_fields and order.product_id and order.channel_name:
        set_clauses = ", ".join(f"{k} = :{k}" for k in batch_fields)
        params = {
            **batch_fields,
            "pid": order.product_id,
            "cname": order.channel_name,
            "oid": order_id,
        }
        await session.execute(
            _t(
                f"UPDATE samba_order SET {set_clauses} "
                "WHERE product_id = :pid AND channel_name = :cname AND id != :oid"
            ),
            params,
        )
        await session.commit()

    return order


@router.put("/{order_id}/status", response_model=SambaOrder)
async def update_order_status(
    order_id: str,
    body: OrderStatusUpdate,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    svc = _write_service(session)
    order = await svc.update_order_status(order_id, body.status)
    if not order:
        raise HTTPException(status_code=404, detail="주문을 찾을 수 없습니다")
    return order


@router.delete("/{order_id}")
async def delete_order(
    order_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    svc = _write_service(session)
    deleted = await svc.delete_order(order_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="주문을 찾을 수 없습니다")
    return {"ok": True}


# ══════════════════════════════════════════════
# 취소승인
# ══════════════════════════════════════════════


@router.post("/{order_id}/approve-cancel")
async def approve_cancel(
    order_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """취소요청 주문에 대해 마켓 취소승인 실행."""
    from backend.domain.samba.account.repository import SambaMarketAccountRepository
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository

    svc = _write_service(session)
    order = await svc.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="주문을 찾을 수 없습니다")

    if not order.order_number:
        raise HTTPException(status_code=400, detail="상품주문번호가 없습니다")

    # 마켓 계정 조회
    if not order.channel_id:
        raise HTTPException(status_code=400, detail="마켓 계정 정보가 없습니다")

    account_repo = SambaMarketAccountRepository(session)
    account = await account_repo.get_async(order.channel_id)
    if not account:
        raise HTTPException(status_code=400, detail="마켓 계정을 찾을 수 없습니다")

    if account.market_type == "smartstore":
        from backend.domain.samba.proxy.smartstore import SmartStoreClient

        extras = account.additional_fields or {}
        client_id = extras.get("clientId", "") or account.api_key or ""
        client_secret = extras.get("clientSecret", "") or account.api_secret or ""
        if not client_id or not client_secret:
            settings_repo = SambaSettingsRepository(session)
            row = await settings_repo.find_by_async(key="store_smartstore")
            if row and isinstance(row.value, dict):
                client_id = client_id or row.value.get("clientId", "")
                client_secret = client_secret or row.value.get("clientSecret", "")
        if not client_id or not client_secret:
            raise HTTPException(status_code=400, detail="스마트스토어 인증정보 없음")

        client = SmartStoreClient(client_id, client_secret)
        try:
            await client.approve_cancel(order.order_number)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"취소승인 실패: {e}")

        # DB 상태 업데이트
        await svc.update_order(
            order_id,
            {
                "shipping_status": "취소완료",
            },
        )
        logger.info(f"[취소승인] {order.order_number} 취소승인 완료")
        return {"ok": True, "message": "취소승인 완료"}

    elif account.market_type == "11st":
        from backend.domain.samba.proxy.elevenst import ElevenstClient
        from backend.domain.samba.returns.repository import SambaReturnRepository

        api_key = (
            (account.additional_fields or {}).get("apiKey", "") or account.api_key or ""
        )
        if not api_key:
            raise HTTPException(status_code=400, detail="11번가 API 키 없음")

        return_repo = SambaReturnRepository(session)
        existing_returns = await return_repo.filter_by_async(order_id=order_id)
        ret = existing_returns[0] if existing_returns else None
        clm_req_seq = (ret.clm_req_seq if ret else None) or ""
        ord_prd_seq = (ret.ord_prd_seq if ret else None) or ""

        if not clm_req_seq or not ord_prd_seq:
            raise HTTPException(
                status_code=400,
                detail="11번가 취소 클레임 정보 없음 (clm_req_seq 또는 ord_prd_seq 미수집)",
            )

        client = ElevenstClient(api_key)
        try:
            await client.confirm_cancel(clm_req_seq, order.order_number, ord_prd_seq)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"취소승인 실패: {e}")

        await svc.update_order(order_id, {"shipping_status": "취소완료"})
        if ret:
            await return_repo.update_async(
                ret.id, status="cancelled", market_order_status="취소완료"
            )

        logger.info(f"[취소승인][11번가] {order.order_number} 취소승인 완료")
        return {"ok": True, "message": "취소승인 완료"}

    elif account.market_type == "ebay":
        # eBay는 seller_cancel_order로 이미 취소 처리됨 → DB 상태만 동기화
        await svc.update_order(
            order_id,
            {"shipping_status": "취소완료", "status": "cancelled"},
        )
        # samba_return 상태도 업데이트
        from backend.domain.samba.returns.repository import SambaReturnRepository

        ret_repo = SambaReturnRepository(session)
        rets = await ret_repo.filter_by_async(order_id=order_id)
        for ret in rets:
            await ret_repo.update_async(
                ret.id,
                status="completed",
                market_order_status="취소완료",
            )
        logger.info(f"[취소승인] eBay {order.order_number} 취소완료 동기화")
        return {"ok": True, "message": "eBay 취소완료 처리"}

    else:
        raise HTTPException(
            status_code=400, detail=f"{account.market_type} 취소승인 미지원"
        )


# ══════════════════════════════════════════════
# 판매자 주도 취소 (재고부족, 가격변동 등)
# ══════════════════════════════════════════════


class SellerCancelBody(BaseModel):
    reason_code: str = (
        "111"  # 111=품절, 132=가격오등록, 133=리셀러, 135=고객변심, 137=택배불가
    )
    reason_text: Optional[str] = None


@router.post("/{order_id}/seller-cancel")
async def seller_cancel(
    order_id: str,
    body: SellerCancelBody,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """판매자 주도 주문 취소 (재고부족/가격변동 등)."""
    from backend.domain.samba.account.repository import SambaMarketAccountRepository

    svc = _write_service(session)
    order = await svc.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="주문을 찾을 수 없습니다")
    if not order.order_number:
        raise HTTPException(status_code=400, detail="상품주문번호가 없습니다")
    if not order.channel_id:
        raise HTTPException(status_code=400, detail="마켓 계정 정보가 없습니다")

    account_repo = SambaMarketAccountRepository(session)
    account = await account_repo.get_async(order.channel_id)
    if not account:
        raise HTTPException(status_code=400, detail="마켓 계정을 찾을 수 없습니다")

    if account.market_type == "lotteon":
        from backend.domain.samba.proxy.lotteon import LotteonClient

        extras = account.additional_fields or {}
        api_key = extras.get("apiKey", "") or account.api_key or ""
        if not api_key:
            raise HTTPException(status_code=400, detail="롯데ON API Key 없음")

        client = LotteonClient(api_key)
        try:
            await client.test_auth()
            success, message = await client.seller_cancel_order(
                od_no=order.od_no or order.order_number,
                reason_code=body.reason_code,
                reason_text=body.reason_text or "고객변심",
                od_seq=int(order.od_seq or 1),
                proc_seq=int(order.proc_seq or 1),
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"판매자 취소 실패: {e}")

        if not success:
            raise HTTPException(status_code=500, detail=f"판매자 취소 실패: {message}")

        await svc.update_order(
            order_id,
            {"shipping_status": "취소완료", "status": "cancelled"},
        )
        # 롯데ON은 단일 itemList 요청으로 같은 odNo의 모든 옵션이 함께 취소됨.
        # 삼바 DB도 같은 odNo의 다른 옵션 레코드를 일괄 cancelled 처리해 UI 정합성 유지.
        od_no_val = order.od_no
        sibling_count = 0
        if od_no_val:
            from sqlmodel import select

            sibling_stmt = (
                select(SambaOrder)
                .where(SambaOrder.od_no == od_no_val)
                .where(SambaOrder.channel_id == order.channel_id)
                .where(SambaOrder.id != order_id)
                .where(SambaOrder.status != "cancelled")
            )
            sibling_rows = (await session.execute(sibling_stmt)).scalars().all()
            for sib in sibling_rows:
                await svc.update_order(
                    sib.id,
                    {"shipping_status": "취소완료", "status": "cancelled"},
                )
            sibling_count = len(sibling_rows)
        if sibling_count:
            logger.info(
                f"[판매자취소] 롯데ON {order.order_number} 동일 주문 옵션 {sibling_count}건 동반 취소"
            )
        logger.info(
            f"[판매자취소] 롯데ON {order.order_number} 완료 ({body.reason_code})"
        )
        user_msg = (
            "이미 취소된 주문 — DB 상태 갱신 완료"
            if message == "이미 취소된 주문"
            else "판매자 취소 완료"
        )
        return {"ok": True, "message": user_msg, "detail": message}

    elif account.market_type == "smartstore":
        from backend.domain.samba.proxy.smartstore import SmartStoreClient
        from backend.domain.samba.forbidden.repository import SambaSettingsRepository

        extras = account.additional_fields or {}
        client_id = extras.get("clientId", "") or account.api_key or ""
        client_secret = extras.get("clientSecret", "") or account.api_secret or ""
        if not client_id or not client_secret:
            settings_repo = SambaSettingsRepository(session)
            row = await settings_repo.find_by_async(key="store_smartstore")
            if row and isinstance(row.value, dict):
                client_id = client_id or row.value.get("clientId", "")
                client_secret = client_secret or row.value.get("clientSecret", "")
        if not client_id or not client_secret:
            raise HTTPException(status_code=400, detail="스마트스토어 인증정보 없음")

        client = SmartStoreClient(client_id, client_secret)
        try:
            await client.request_cancel(
                product_order_id=order.order_number,
                cancel_reason="INTENT_CHANGED",
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"판매자 취소 실패: {e}")

        await svc.update_order(
            order_id,
            {"shipping_status": "취소완료", "status": "cancelled"},
        )
        logger.info(
            f"[판매자취소] 스마트스토어 {order.order_number} 완료 (INTENT_CHANGED)"
        )
        return {"ok": True, "message": "판매자 취소 완료"}

    elif account.market_type == "playauto":
        # 플레이오토 EMP API는 주문확인 상태변경 미지원 (송장입력만 가능)
        # DB 상태만 변경하여 이행 불가 건 구분용으로 사용
        await svc.update_order(
            order_id,
            {"shipping_status": "주문확인"},
        )
        logger.info(f"[주문확인] 플레이오토 {order.order_number} 주문확인 완료 (DB)")
        return {"ok": True, "message": "주문확인 완료"}

    elif account.market_type == "ebay":
        from backend.domain.samba.proxy.ebay import EbayApiError, EbayClient

        extras = account.additional_fields or {}
        app_id = extras.get("clientId") or extras.get("appId") or account.api_key or ""
        cert_id = (
            extras.get("clientSecret")
            or extras.get("certId")
            or account.api_secret
            or ""
        )
        refresh_token = extras.get("oauthToken") or extras.get("authToken", "") or ""
        if not (app_id and cert_id and refresh_token):
            raise HTTPException(status_code=400, detail="eBay 인증정보 없음")

        client = EbayClient(
            app_id=app_id,
            dev_id="",
            cert_id=cert_id,
            refresh_token=refresh_token,
            sandbox=bool(extras.get("sandbox", False)),
        )
        # order_number에 legacyOrderId 저장되어 있음
        try:
            reason_map = {
                "111": "OUT_OF_STOCK_OR_CANNOT_FULFILL",
                "SOLD_OUT": "OUT_OF_STOCK_OR_CANNOT_FULFILL",
                "112": "BUYER_CANCEL_OR_ADDRESS_ISSUE",
                "113": "BUYER_ASKED_CANCEL",
            }
            ebay_reason = reason_map.get(
                body.reason_code, "OUT_OF_STOCK_OR_CANNOT_FULFILL"
            )
            await client.seller_cancel_order(
                legacy_order_id=order.order_number,
                reason=ebay_reason,
            )
        except EbayApiError as e:
            raise HTTPException(status_code=500, detail=f"eBay 취소 실패: {e}")

        await svc.update_order(
            order_id,
            {"shipping_status": "취소요청", "status": "cancel_requested"},
        )
        logger.info(f"[판매자취소] eBay {order.order_number} 취소 요청 완료")
        return {"ok": True, "message": "eBay 판매자 취소 요청 완료"}

    elif account.market_type == "11st":
        # 11번가 판매불가처리 (재고부족 등 판매자 주도 취소)
        # 사유코드 10(고객변심) 고정 — 신용점수 차감 회피
        # 운영 가이드: 고객 동의 후 진행
        from backend.domain.samba.proxy.elevenst import (
            ElevenstApiError,
            ElevenstClient,
        )

        api_key = (
            (account.additional_fields or {}).get("apiKey", "") or account.api_key or ""
        )
        if not api_key:
            raise HTTPException(status_code=400, detail="11번가 API Key 없음")

        if not order.ord_prd_seq:
            raise HTTPException(
                status_code=400,
                detail="11번가 ordPrdSeq 미수집 — 주문 동기화 후 다시 시도해주세요",
            )

        client = ElevenstClient(api_key)
        try:
            await client.reject_order(
                ord_no=order.order_number,
                ord_prd_seq=order.ord_prd_seq,
                ord_cn_rsn_cd="10",  # 고객변심
                ord_cn_dtls_rsn="구매자 요청으로 취소 처리",
            )
        except ElevenstApiError as e:
            raise HTTPException(
                status_code=500, detail=f"11번가 판매불가처리 실패: {e}"
            )
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"11번가 판매불가처리 실패: {e}"
            )

        await svc.update_order(
            order_id,
            {"shipping_status": "취소완료", "status": "cancelled"},
        )
        logger.info(
            f"[판매자취소] 11번가 {order.order_number} 판매불가처리 완료 (사유=10/고객변심)"
        )
        return {"ok": True, "message": "11번가 판매불가처리 완료"}

    raise HTTPException(
        status_code=400, detail=f"{account.market_type} 판매자 취소 미지원"
    )


@router.post("/{order_id}/confirm")
async def confirm_order(
    order_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """주문확인(발주확인) 수동 처리 — 원소싱처 재고/가격 확인 후 사용자가 실행."""
    from backend.domain.samba.account.repository import SambaMarketAccountRepository
    from backend.domain.samba.order.model import is_order_cancelled

    svc = _write_service(session)
    order = await svc.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="주문을 찾을 수 없습니다")
    # 취소 가드 — 발주확인(주문확인) 직전 차단. 마켓 인지 후 잘못 발주되는 사고 방지.
    if is_order_cancelled(order):
        raise HTTPException(
            status_code=409,
            detail=(
                f"취소요청 상태(주문={order.status}/마켓={order.shipping_status})라 "
                "발주확인을 진행할 수 없습니다"
            ),
        )
    if not order.order_number:
        raise HTTPException(status_code=400, detail="상품주문번호가 없습니다")
    if not order.channel_id:
        raise HTTPException(status_code=400, detail="마켓 계정 정보가 없습니다")

    account_repo = SambaMarketAccountRepository(session)
    account = await account_repo.get_async(order.channel_id)
    if not account:
        raise HTTPException(status_code=400, detail="마켓 계정을 찾을 수 없습니다")

    if account.market_type == "lotteon":
        from backend.domain.samba.proxy.lotteon import LotteonClient

        extras = account.additional_fields or {}
        api_key = extras.get("apiKey", "") or account.api_key or ""
        if not api_key:
            raise HTTPException(status_code=400, detail="롯데ON API Key 없음")

        # SellerIfCompleteInform은 odNo/odSeq/procSeq만 필요 (비클레임은 기본 1/1)
        client = LotteonClient(api_key)
        try:
            await client.test_auth()
            ok = await client.confirm_orders(
                [
                    {
                        "odNo": order.od_no or order.order_number,
                        "odSeq": int(order.od_seq or 1),
                        "procSeq": int(order.proc_seq or 1),
                    }
                ]
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"주문확인 실패: {e}")

        if not ok:
            raise HTTPException(
                status_code=500,
                detail="롯데ON 주문확인 실패 — SellerIfCompleteInform 응답 rsltCd≠0000 (서버 로그 확인)",
            )

        await svc.update_order(order_id, {"shipping_status": "출고지시"})
        logger.info(f"[주문확인] 롯데ON {order.order_number} 완료")
        return {"ok": True, "message": "주문확인 완료"}

    raise HTTPException(
        status_code=400, detail=f"{account.market_type} 주문확인 미지원"
    )


@router.post("/{order_id}/market-delete")
async def market_delete_order_product(
    order_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """주문 카드의 '마켓상품삭제' — 해당 주문 상품을 마켓에서 완전 삭제(판매종료가 아닌 삭제)."""
    from backend.domain.samba.account.repository import SambaMarketAccountRepository

    svc = _write_service(session)
    order = await svc.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="주문을 찾을 수 없습니다")
    if not order.product_id:
        raise HTTPException(status_code=400, detail="마켓 상품번호가 없습니다")
    if not order.channel_id:
        raise HTTPException(status_code=400, detail="마켓 계정 정보가 없습니다")

    account_repo = SambaMarketAccountRepository(session)
    account = await account_repo.get_async(order.channel_id)
    if not account:
        raise HTTPException(status_code=400, detail="마켓 계정을 찾을 수 없습니다")

    if account.market_type == "lotteon":
        from backend.domain.samba.proxy.lotteon import LotteonClient

        extras = account.additional_fields or {}
        api_key = extras.get("apiKey", "") or account.api_key or ""
        if not api_key:
            raise HTTPException(status_code=400, detail="롯데ON API Key 없음")

        spd_no = order.product_id
        client = LotteonClient(api_key)
        try:
            await client.test_auth()
            result = await client.delete_product(spd_no)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"마켓상품삭제 실패: {e}")

        logger.info(
            f"[마켓상품삭제] 롯데ON spdNo={spd_no} order={order.order_number} result={result}"
        )
        return {"ok": True, "message": "마켓 상품 삭제 완료", "detail": result}

    if account.market_type == "smartstore":
        from backend.domain.samba.proxy.smartstore import SmartStoreClient

        extras = account.additional_fields or {}
        client_id = extras.get("clientId", "") or account.api_key or ""
        client_secret = extras.get("clientSecret", "") or account.api_secret or ""
        if not client_id or not client_secret:
            raise HTTPException(status_code=400, detail="스마트스토어 인증 정보 없음")

        # originProductNo: collected_product의 market_product_nos에서 우선 조회
        origin_product_no = ""
        if order.collected_product_id:
            from backend.domain.samba.collector.repository import (
                SambaCollectorRepository,
            )

            cp_repo = SambaCollectorRepository(session)
            cp = await cp_repo.get_async(order.collected_product_id)
            if cp and cp.market_product_nos:
                origin_product_no = (cp.market_product_nos or {}).get(
                    order.channel_id, ""
                )

        # fallback: channelProductNo (order.product_id)
        if not origin_product_no:
            origin_product_no = order.product_id or ""

        if not origin_product_no:
            raise HTTPException(
                status_code=400, detail="스마트스토어 상품번호를 찾을 수 없습니다"
            )

        client = SmartStoreClient(client_id, client_secret)
        try:
            result = await client.delete_product(origin_product_no)
            logger.info(
                f"[마켓상품삭제] 스마트스토어 삭제 성공 productNo={origin_product_no} "
                f"order={order.order_number}"
            )
            return {"ok": True, "message": "마켓 상품 삭제 완료", "detail": result}
        except Exception as del_err:
            # 진행중 주문 등으로 삭제 불가 시 → 전 옵션 재고 0 (품절) 폴백
            logger.warning(
                f"[마켓상품삭제] 스마트스토어 삭제 실패({del_err}), 품절 폴백 시도: {origin_product_no}"
            )

        try:
            existing = await client.get_product(origin_product_no)
            origin = existing.get("originProduct", {})
            for k in ["productNo", "channelProducts", "regDate", "modifiedDate"]:
                origin.pop(k, None)

            # 전 옵션 재고 0 + usable=False
            origin["stockQuantity"] = 0
            opt_info = origin.get("detailAttribute", {}).get("optionInfo") or {}
            combos = opt_info.get("optionCombinations") or opt_info.get(
                "combinations", []
            )
            for combo in combos:
                combo["stockQuantity"] = 0
                combo["usable"] = False

            put_data: dict[str, Any] = {"originProduct": origin}
            if "smartstoreChannelProduct" in existing:
                put_data["smartstoreChannelProduct"] = existing[
                    "smartstoreChannelProduct"
                ]

            await client.update_product(origin_product_no, put_data)
            logger.info(
                f"[마켓상품삭제] 스마트스토어 품절 폴백 완료 productNo={origin_product_no}"
            )
            return {
                "ok": True,
                "message": "마켓 삭제 불가 — 전 옵션 품절처리 완료",
                "fallback": True,
            }
        except Exception as fb_err:
            raise HTTPException(
                status_code=500,
                detail=f"마켓상품삭제 및 품절처리 모두 실패: {fb_err}",
            )

    raise HTTPException(
        status_code=400, detail=f"{account.market_type} 마켓상품삭제 미지원"
    )


class CancelSourceOrderRequest(BaseModel):
    order_number: str
    reason: str = "단순변심"


@router.post("/cancel-source-order")
async def cancel_source_order(
    req: CancelSourceOrderRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """소싱처 원주문 취소 (무신사 등 소비자 주문취소)."""
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository

    settings_repo = SambaSettingsRepository(session)

    # 현재는 무신사만 지원
    cookie_row = await settings_repo.find_by_async(key="musinsa_cookie")
    musinsa_cookie = cookie_row.value if cookie_row else ""
    if not musinsa_cookie:
        raise HTTPException(status_code=400, detail="무신사 쿠키가 설정되지 않았습니다")

    from backend.domain.samba.proxy.musinsa import MusinsaClient

    client = MusinsaClient(cookie=musinsa_cookie)

    try:
        result = await client.cancel_order(req.order_number, req.reason)
        return result
    except Exception as e:
        logger.error(f"[원주문취소] 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════
# 교환 처리 (재배송 / 거부 / 반품변경)
# ══════════════════════════════════════════════


class ExchangeActionBody(BaseModel):
    action: str  # "reship" | "reject" | "convert_return"
    reason: Optional[str] = None
    clm_no: Optional[str] = None  # 롯데ON 교환 클레임번호
    tracking_number: Optional[str] = None  # 롯데ON 교환 재배송 송장번호
    shipping_company: Optional[str] = None  # 롯데ON 교환 재배송 택배사


@router.post("/{order_id}/exchange-action")
async def exchange_action(
    order_id: str,
    body: ExchangeActionBody,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """교환요청에 대한 처리 (재배송/거부/반품변경)."""
    from backend.domain.samba.account.repository import SambaMarketAccountRepository
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository

    svc = _write_service(session)
    order = await svc.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="주문을 찾을 수 없습니다")
    if not order.order_number:
        raise HTTPException(status_code=400, detail="상품주문번호가 없습니다")
    if not order.channel_id:
        raise HTTPException(status_code=400, detail="마켓 계정 정보가 없습니다")

    account_repo = SambaMarketAccountRepository(session)
    account = await account_repo.get_async(order.channel_id)
    if not account:
        raise HTTPException(status_code=400, detail="마켓 계정을 찾을 수 없습니다")

    if account.market_type == "smartstore":
        from backend.domain.samba.proxy.smartstore import SmartStoreClient

        extras = account.additional_fields or {}
        client_id = extras.get("clientId", "") or account.api_key or ""
        client_secret = extras.get("clientSecret", "") or account.api_secret or ""
        if not client_id or not client_secret:
            settings_repo = SambaSettingsRepository(session)
            row = await settings_repo.find_by_async(key="store_smartstore")
            if row and isinstance(row.value, dict):
                client_id = client_id or row.value.get("clientId", "")
                client_secret = client_secret or row.value.get("clientSecret", "")
        if not client_id or not client_secret:
            raise HTTPException(status_code=400, detail="스마트스토어 인증정보 없음")

        client = SmartStoreClient(client_id, client_secret)
        action_labels = {
            "reship": "교환재배송",
            "reject": "교환거부",
            "convert_return": "반품변경",
        }
        label = action_labels.get(body.action, body.action)

        try:
            if body.action == "reship":
                await client.approve_exchange(order.order_number)
                new_status = "교환완료"
            elif body.action == "reject":
                await client.reject_exchange(
                    order.order_number, body.reason or "판매자 교환 거부"
                )
                new_status = "교환거부"
            elif body.action == "convert_return":
                await client.convert_exchange_to_return(order.order_number)
                new_status = "반품변경"
            else:
                raise HTTPException(
                    status_code=400, detail=f"알 수 없는 액션: {body.action}"
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"{label} 실패: {e}")

        await svc.update_order(order_id, {"shipping_status": new_status})
        logger.info(f"[교환처리] {order.order_number} {label} 완료")
        return {"ok": True, "message": f"{label} 완료"}

    elif account.market_type == "lotteon":
        from backend.domain.samba.proxy.lotteon import LotteonClient

        extras = account.additional_fields or {}
        api_key = extras.get("apiKey", "") or account.api_key or ""
        if not api_key:
            raise HTTPException(status_code=400, detail="롯데ON API 키 없음")

        client = LotteonClient(api_key=api_key)
        await client.test_auth()

        # 교환 클레임 정보 자동 탐색 (clmNo, procSeq, orglProcSeq)
        clm_no = body.clm_no or ""
        found_claim: dict = {}
        try:
            exchange_claims = await client.get_exchanges(days=30)
            for claim in exchange_claims:
                if str(claim.get("odNo", "")) == str(order.od_no or order.order_number):
                    if not clm_no:
                        clm_no = claim.get("clmNo", "")
                    found_claim = claim
                    logger.info(
                        f"[교환처리] clmNo 탐색 성공: {clm_no} stepCd={claim.get('odPrgsStepCd', '')}"
                    )
                    break
        except Exception as ce:
            logger.warning(f"[교환처리] 클레임 탐색 실패: {ce}")

        if body.action == "reship":
            # 교환 재배송: 승인 → 발송 처리
            tracking_number = body.tracking_number or ""
            shipping_company = body.shipping_company or ""
            sitm_no = order.shipment_id or ""
            spd_no = order.product_id or ""
            quantity = order.quantity or 1

            if not tracking_number:
                raise HTTPException(
                    status_code=400, detail="교환 재배송 송장번호가 필요합니다"
                )

            # 교환 승인 (회수 지시) — 접수(03) 상태인 경우 먼저 승인
            step_cd = str(found_claim.get("odPrgsStepCd", "") or "")
            if step_cd == "03" and clm_no:
                proc_seq = str(found_claim.get("procSeq", 1))
                orgl_proc_seq = str(found_claim.get("orglProcSeq", 1))
                clm_rsn_cd = str(found_claim.get("clmRsnCd", "204"))
                try:
                    approved = await client.approve_exchange(
                        od_no=order.od_no or order.order_number,
                        clm_no=clm_no,
                        items=[
                            {
                                "odSeq": int(order.od_seq or 1),
                                "procSeq": int(proc_seq),
                                "orglProcSeq": int(orgl_proc_seq),
                                "slrRsnCd": clm_rsn_cd,
                            }
                        ],
                    )
                    if approved:
                        logger.info(f"[교환처리] {order.order_number} 교환 승인 완료")
                except Exception as ae:
                    logger.warning(f"[교환처리] 교환 승인 실패 (계속 진행): {ae}")

            try:
                sent = await client.ship_order_exchange(
                    od_no=order.od_no or order.order_number,
                    od_seq=order.od_seq or "1",
                    proc_seq=order.proc_seq or "1",
                    sitm_no=sitm_no,
                    spd_no=spd_no,
                    clm_no=clm_no,
                    quantity=quantity,
                    shipping_company=shipping_company,
                    tracking_number=tracking_number,
                )
                if not sent:
                    raise HTTPException(
                        status_code=500, detail="롯데ON 교환 재배송 전송 실패"
                    )
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"교환 재배송 실패: {e}")

            await svc.update_order(
                order_id,
                {
                    "shipping_status": "교환재배송",
                    "tracking_number": tracking_number,
                    "shipping_company": shipping_company,
                },
            )
            logger.info(f"[교환처리] {order.order_number} 롯데ON 교환재배송 완료")
            return {"ok": True, "message": "교환 재배송 처리 완료"}

        elif body.action == "convert_return":
            # 교환→반품 변경: 롯데ON API 미지원 → 삼바 내부 처리만
            # 반품교환 레코드 타입을 exchange→return으로 변경
            from backend.domain.samba.returns.repository import SambaReturnRepository

            return_repo = SambaReturnRepository(session)
            ret = await return_repo.find_by_async(order_id=order_id)
            if ret:
                await return_repo.update_async(
                    ret.id,
                    type="return",
                    market_order_status="반품요청",
                    status="pending",
                )
            await svc.update_order(
                order_id, {"shipping_status": "반품요청", "status": "return_requested"}
            )
            logger.info(
                f"[교환처리] {order.order_number} 교환→반품 변경 완료 (삼바 내부)"
            )
            return {
                "ok": True,
                "message": "교환→반품 변경 완료 (롯데ON 판매자센터에서도 별도 처리 필요)",
            }

        elif body.action == "reject":
            # 교환 거부: 삼바 내부 상태 업데이트 (롯데ON 교환 거부 API 스펙 확인 후 연동 필요)
            from backend.domain.samba.returns.repository import SambaReturnRepository

            return_repo = SambaReturnRepository(session)
            ret = await return_repo.find_by_async(order_id=order_id)
            if ret:
                await return_repo.update_async(
                    ret.id,
                    status="rejected",
                    market_order_status="교환거부",
                )
            await svc.update_order(order_id, {"shipping_status": "교환거부"})
            logger.info(f"[교환처리] {order.order_number} 교환거부 완료 (삼바 내부)")
            return {
                "ok": True,
                "message": "교환거부 완료 (롯데ON 판매자센터에서도 별도 처리 필요)",
            }

        else:
            raise HTTPException(
                status_code=400, detail=f"롯데ON 교환처리 미지원 액션: {body.action}"
            )

    elif account.market_type == "11st":
        from backend.domain.samba.forbidden.repository import SambaSettingsRepository
        from backend.domain.samba.proxy.elevenst_exchange import (
            ElevenstApiError,
            ElevenstExchangeClient,
        )
        from backend.domain.samba.returns.repository import SambaReturnRepository

        api_key = account.api_key or ""
        if not api_key:
            # account.api_key 미설정 시 settings 테이블의 store_11st.apiKey fallback
            settings_repo = SambaSettingsRepository(session)
            st_row = await settings_repo.find_by_async(key="store_11st")
            if st_row and isinstance(st_row.value, dict):
                api_key = st_row.value.get("apiKey", "") or ""
        if not api_key:
            raise HTTPException(status_code=400, detail="11번가 API 키가 없습니다")

        return_repo = SambaReturnRepository(session)
        ret_records = await return_repo.list_by_order(order_id)
        ret = next((r for r in ret_records if r.type == "exchange"), None)

        if body.action in ("reject", "approve", "reship"):
            clm_req_seq = (ret.clm_req_seq or "") if ret else ""
            ord_prd_seq = (ret.ord_prd_seq or "") if ret else ""
            ord_no = order.order_number or ""

            if not clm_req_seq or not ord_no or not ord_prd_seq:
                raise HTTPException(
                    status_code=400,
                    detail="교환 처리에 필요한 클레임 식별자(clm_req_seq, ord_no, ord_prd_seq)가 없습니다",
                )

            client = ElevenstExchangeClient(api_key)
            action_labels = {
                "reship": "교환승인(재배송)",
                "approve": "교환승인(재배송)",
                "reject": "교환거부",
            }
            label = action_labels.get(body.action, body.action)

            try:
                if body.action in ("reship", "approve"):
                    await client.confirm_exchange(clm_req_seq, ord_no, ord_prd_seq)
                    new_status = "교환승인"
                else:
                    await client.reject_exchange(
                        clm_req_seq,
                        ord_no,
                        ord_prd_seq,
                        refs_rsn_cd="204",
                        refs_rsn=body.reason or "기타",
                    )
                    new_status = "교환거부"
            except HTTPException:
                raise
            except ElevenstApiError as e:
                raise HTTPException(status_code=502, detail=f"{label} API 오류: {e}")
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"{label} 실패: {e}")

            await svc.update_order(order_id, {"shipping_status": new_status})
            if ret:
                await return_repo.update_async(
                    ret.id,
                    status="approved" if new_status == "교환승인" else "rejected",
                    market_order_status=new_status,
                )
            logger.info(f"[교환처리] {order.order_number} 11번가 {label} 완료")
            return {"ok": True, "message": f"{label} 완료"}

        elif body.action == "convert_return":
            if ret:
                await return_repo.update_async(
                    ret.id,
                    type="return",
                    market_order_status="반품요청",
                    status="pending",
                )
            await svc.update_order(
                order_id, {"shipping_status": "반품요청", "status": "return_requested"}
            )
            logger.info(f"[교환처리] {order.order_number} 11번가 교환→반품 변경 완료")
            return {
                "ok": True,
                "message": "교환→반품 변경 완료 (11번가 판매자센터에서도 별도 처리 필요)",
            }

        else:
            raise HTTPException(
                status_code=400, detail=f"11번가 교환처리 미지원 액션: {body.action}"
            )

    else:
        raise HTTPException(
            status_code=400, detail=f"{account.market_type} 교환처리 미지원"
        )


# ══════════════════════════════════════════════
# 반품 처리 (승인 / 거부)
# ══════════════════════════════════════════════


class ReturnActionBody(BaseModel):
    action: str  # "approve" | "reject"
    reason: Optional[str] = None


@router.post("/{order_id}/return-action")
async def return_action(
    order_id: str,
    body: ReturnActionBody,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """반품요청에 대한 처리 (승인/거부)."""
    from backend.domain.samba.account.repository import SambaMarketAccountRepository
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository

    svc = _write_service(session)
    order = await svc.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="주문을 찾을 수 없습니다")
    if not order.order_number:
        raise HTTPException(status_code=400, detail="상품주문번호가 없습니다")
    if not order.channel_id:
        raise HTTPException(status_code=400, detail="마켓 계정 정보가 없습니다")

    account_repo = SambaMarketAccountRepository(session)
    account = await account_repo.get_async(order.channel_id)
    if not account:
        raise HTTPException(status_code=400, detail="마켓 계정을 찾을 수 없습니다")

    if account.market_type == "smartstore":
        from backend.domain.samba.proxy.smartstore import SmartStoreClient

        extras = account.additional_fields or {}
        client_id = extras.get("clientId", "") or account.api_key or ""
        client_secret = extras.get("clientSecret", "") or account.api_secret or ""
        if not client_id or not client_secret:
            settings_repo = SambaSettingsRepository(session)
            row = await settings_repo.find_by_async(key="store_smartstore")
            if row and isinstance(row.value, dict):
                client_id = client_id or row.value.get("clientId", "")
                client_secret = client_secret or row.value.get("clientSecret", "")
        if not client_id or not client_secret:
            raise HTTPException(status_code=400, detail="스마트스토어 인증정보 없음")

        client = SmartStoreClient(client_id, client_secret)
        label = "반품승인" if body.action == "approve" else "반품거부"

        try:
            if body.action == "approve":
                try:
                    await client.approve_return(order.order_number)
                except Exception as first_err:
                    if "환불보류" in str(first_err):
                        # 환불보류 해제 후 재시도
                        logger.info(
                            f"[반품처리] {order.order_number} 환불보류 감지 → 보류해제 후 재시도"
                        )
                        await client.release_return_hold(order.order_number)
                        await client.approve_return(order.order_number)
                    else:
                        raise
                new_status = "반품승인"
            elif body.action == "reject":
                await client.reject_return(
                    order.order_number, body.reason or "판매자 반품 거부"
                )
                new_status = "반품거부"
            else:
                raise HTTPException(
                    status_code=400, detail=f"알 수 없는 액션: {body.action}"
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"{label} 실패: {e}")

        await svc.update_order(order_id, {"shipping_status": new_status})

        # 반품교환(samba_return) 레코드도 상태 업데이트
        from backend.domain.samba.returns.repository import SambaReturnRepository
        from datetime import UTC, datetime

        return_repo = SambaReturnRepository(session)
        existing_returns = await return_repo.filter_by_async(order_id=order_id)
        if existing_returns:
            ret = existing_returns[0]
            if body.action == "approve":
                await return_repo.update_async(
                    ret.id,
                    status="completed",
                    market_order_status="반품완료",
                    completion_date=datetime.now(UTC),
                )
            elif body.action == "reject":
                await return_repo.update_async(
                    ret.id,
                    status="rejected",
                    market_order_status="반품거부",
                )

        logger.info(f"[반품처리] {order.order_number} {label} 완료")
        return {"ok": True, "message": f"{label} 완료"}

    elif account.market_type == "lotteon":
        from backend.domain.samba.proxy.lotteon import LotteonClient

        api_key = (
            (account.additional_fields or {}).get("apiKey", "") or account.api_key or ""
        )
        if not api_key:
            raise HTTPException(status_code=400, detail="롯데ON API 키 없음")

        client = LotteonClient(api_key=api_key)
        label = "반품승인" if body.action == "approve" else "반품거부"

        try:
            if body.action == "approve":
                # 반품 클레임 목록에서 해당 주문 item 조회
                raw_returns = await client.get_returns(days=30)
                _lo_od_no = order.od_no or order.order_number
                claim_items = [i for i in raw_returns if i.get("odNo") == _lo_od_no]
                if not claim_items:
                    raise HTTPException(
                        status_code=400,
                        detail="롯데ON 반품 클레임 정보 없음 (최근 30일 내 조회되지 않음)",
                    )
                ci = claim_items[0]
                clm_no = ci.get("clmNo", "")
                od_seq = int(ci.get("odSeq") or 1)
                proc_seq = int(ci.get("procSeq") or od_seq)
                orgl_proc_seq = int(ci.get("orglProcSeq") or proc_seq)
                items_payload = [
                    {
                        "odSeq": od_seq,
                        "procSeq": proc_seq,
                        "orglProcSeq": orgl_proc_seq,
                        "spdNo": ci.get("spdNo", ""),
                        "spdNm": ci.get("spdNm", ""),
                        "sitmNo": ci.get("sitmNo", ""),
                        "sitmNm": ci.get("sitmNm", ""),
                    }
                ]
                await client.approve_return(_lo_od_no, clm_no, items_payload)
                new_status = "반품승인"
            elif body.action == "reject":
                await client.reject_return(_lo_od_no, body.reason or "")
                new_status = "반품거부"
            else:
                raise HTTPException(
                    status_code=400, detail=f"알 수 없는 액션: {body.action}"
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"{label} 실패: {e}")

        await svc.update_order(order_id, {"shipping_status": new_status})

        # samba_return 상태 업데이트
        from backend.domain.samba.returns.repository import SambaReturnRepository
        from datetime import UTC, datetime

        return_repo = SambaReturnRepository(session)
        existing_returns = await return_repo.filter_by_async(order_id=order_id)
        if existing_returns:
            ret = existing_returns[0]
            if body.action == "approve":
                await return_repo.update_async(
                    ret.id,
                    status="completed",
                    market_order_status="반품완료",
                    completion_date=datetime.now(UTC),
                )
            elif body.action == "reject":
                await return_repo.update_async(
                    ret.id,
                    status="rejected",
                    market_order_status="반품거부",
                )

        logger.info(f"[반품처리][롯데ON] {order.order_number} {label} 완료")
        return {"ok": True, "message": f"{label} 완료"}

    elif account.market_type == "11st":
        from datetime import UTC, datetime

        from backend.domain.samba.proxy.elevenst import ElevenstClient
        from backend.domain.samba.returns.repository import SambaReturnRepository

        api_key = (
            (account.additional_fields or {}).get("apiKey", "") or account.api_key or ""
        )
        if not api_key:
            raise HTTPException(status_code=400, detail="11번가 API 키 없음")

        return_repo = SambaReturnRepository(session)
        existing_returns = await return_repo.filter_by_async(order_id=order_id)
        ret = existing_returns[0] if existing_returns else None
        clm_req_seq = (ret.clm_req_seq if ret else None) or ""
        ord_prd_seq = (ret.ord_prd_seq if ret else None) or ""

        if not clm_req_seq or not ord_prd_seq:
            raise HTTPException(
                status_code=400,
                detail="11번가 반품 클레임 정보 없음 (clm_req_seq 또는 ord_prd_seq 미수집)",
            )

        client = ElevenstClient(api_key)
        label = "반품승인" if body.action == "approve" else "반품거부"

        try:
            if body.action == "approve":
                await client.confirm_return(
                    clm_req_seq, order.order_number, ord_prd_seq
                )
                new_status = "반품승인"
            elif body.action == "reject":
                await client.reject_return(clm_req_seq, order.order_number, ord_prd_seq)
                new_status = "반품거부"
            else:
                raise HTTPException(
                    status_code=400, detail=f"알 수 없는 액션: {body.action}"
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"{label} 실패: {e}")

        await svc.update_order(order_id, {"shipping_status": new_status})

        if ret:
            if body.action == "approve":
                await return_repo.update_async(
                    ret.id,
                    status="completed",
                    market_order_status="반품완료",
                    completion_date=datetime.now(UTC),
                )
            elif body.action == "reject":
                await return_repo.update_async(
                    ret.id,
                    status="rejected",
                    market_order_status="반품거부",
                )

        logger.info(f"[반품처리][11번가] {order.order_number} {label} 완료")
        return {"ok": True, "message": f"{label} 완료"}

    elif account.market_type == "ebay":
        # eBay 반품은 SambaReturn.market_order_status 에 저장된 returnId 필요
        from backend.domain.samba.proxy.ebay import EbayApiError, EbayClient
        from backend.domain.samba.returns.repository import SambaReturnRepository

        extras = account.additional_fields or {}
        app_id = extras.get("clientId") or extras.get("appId") or account.api_key or ""
        cert_id = (
            extras.get("clientSecret")
            or extras.get("certId")
            or account.api_secret
            or ""
        )
        refresh_token = extras.get("oauthToken") or extras.get("authToken", "") or ""
        if not (app_id and cert_id and refresh_token):
            raise HTTPException(status_code=400, detail="eBay 인증정보 없음")

        # returnId 는 samba_return.notes 또는 market_order_status에 저장 권장
        ret_repo = SambaReturnRepository(session)
        existing = await ret_repo.filter_by_async(order_id=order_id)
        if not existing:
            raise HTTPException(
                status_code=400, detail="해당 주문에 반품 데이터가 없습니다"
            )
        return_id = existing[0].memo or existing[0].market_order_status or ""
        # memo/market_order_status 에 returnId 저장 관례. 비어있으면 사용자 입력 필요
        if not return_id:
            raise HTTPException(
                status_code=400,
                detail="eBay returnId 없음 (samba_return.memo에 저장 필요)",
            )

        client = EbayClient(
            app_id=app_id,
            dev_id="",
            cert_id=cert_id,
            refresh_token=refresh_token,
            sandbox=bool(extras.get("sandbox", False)),
        )
        try:
            if body.action == "approve":
                await client.approve_return(return_id)
                new_status = "반품승인"
                ret_update = {"status": "completed", "market_order_status": "반품승인"}
            elif body.action == "reject":
                await client.reject_return(return_id, body.reason or "Seller decline")
                new_status = "반품거부"
                ret_update = {"status": "rejected", "market_order_status": "반품거부"}
            else:
                raise HTTPException(
                    status_code=400, detail=f"eBay 반품 액션 미지원: {body.action}"
                )
        except EbayApiError as e:
            raise HTTPException(status_code=500, detail=f"eBay 반품처리 실패: {e}")

        await svc.update_order(order_id, {"shipping_status": new_status})
        await ret_repo.update_async(existing[0].id, **ret_update)
        logger.info(f"[반품처리][eBay] {order.order_number} {body.action} 완료")
        return {"ok": True, "message": f"eBay 반품 {body.action} 완료"}

    else:
        raise HTTPException(
            status_code=400, detail=f"{account.market_type} 반품처리 미지원"
        )


# ══════════════════════════════════════════════
# 송장번호 전송 (발송처리)
# ══════════════════════════════════════════════


class ShipRequest(BaseModel):
    shipping_company: str
    tracking_number: str


@router.post("/{order_id}/ship")
async def ship_order(
    order_id: str,
    body: ShipRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """송장번호 저장 + 마켓 발송처리."""
    svc = _write_service(session)
    order = await svc.get_order(order_id)
    if not order:
        raise HTTPException(404, "주문을 찾을 수 없습니다")

    # DB 저장 (마켓 전송 성공 여부와 무관하게 항상 저장)
    await svc.update_order(
        order_id,
        {
            "shipping_company": body.shipping_company,
            "tracking_number": body.tracking_number,
        },
    )

    # 마켓 송장 전송 — 통일 service (자동 dispatch_to_market 도 같은 함수 호출).
    # [통일 2026-05-16] 이전엔 이곳과 dispatch_to_market 가 마켓별 분기를 중복 구현 →
    # 자동 dispatch 가 자격증명 누락/필드 차이로 실패하던 회귀 차단. 단일 진실의 출처.
    from backend.domain.samba.order.dispatch_service import send_invoice_to_market

    market_sent, market_msg = await send_invoice_to_market(
        order, body.shipping_company, body.tracking_number, session
    )

    # 마켓 송장 전송 성공 시 status를 '국내배송중'으로 일괄 변경
    if market_sent:
        await svc.update_order(
            order_id,
            {"shipping_status": "송장전송완료", "status": "shipping"},
        )

    return {
        "ok": True,
        "market_sent": market_sent,
        "message": market_msg or "송장번호 저장 완료",
    }


# ══════════════════════════════════════════════
# URL에서 상품 대표이미지 추출
# ══════════════════════════════════════════════


@router.post("/fetch-product-image")
async def fetch_product_image(
    body: FetchProductImageRequest,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """URL에서 상품 대표이미지를 추출해 반환."""
    from urllib.parse import urlparse

    import httpx

    url = body.url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "올바른 URL을 입력해주세요")

    parsed = urlparse(url)
    host = parsed.hostname or ""

    try:
        # ── 무신사 ──
        if "musinsa.com" in host:
            # URL에서 상품번호 추출: /products/1234 또는 /app/goods/1234
            m = re.search(r"(?:/products/|/app/goods/|/goods/)(\d+)", url)
            if not m:
                raise HTTPException(400, "무신사 상품번호를 URL에서 추출할 수 없습니다")
            goods_no = m.group(1)

            from backend.domain.samba.proxy.musinsa import MusinsaClient

            # 쿠키 로드
            from backend.domain.samba.forbidden.repository import (
                SambaSettingsRepository,
            )

            settings_repo = SambaSettingsRepository(session)
            row = await settings_repo.find_by_async(key="musinsa_cookie")
            cookie = ""
            if row and row.value:
                cookie = str(row.value)
            client = MusinsaClient(cookie=cookie)
            detail = await client.get_goods_detail(goods_no)
            images = detail.get("images", [])
            if images:
                return {"image_url": images[0]}
            raise HTTPException(404, "무신사 상품에서 이미지를 찾을 수 없습니다")

        # ── KREAM ──
        elif "kream.co.kr" in host:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as hc:
                resp = await hc.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    },
                )
                text = resp.text
            m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]*)"', text)
            if m:
                return {"image_url": m.group(1).split("?")[0]}
            raise HTTPException(404, "KREAM 상품에서 이미지를 찾을 수 없습니다")

        # ── 범용 fallback (og:image) ──
        else:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as hc:
                resp = await hc.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    },
                )
                text = resp.text
            # og:image 추출
            m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]*)"', text)
            if not m:
                # content가 앞에 오는 경우도 처리
                m = re.search(
                    r'<meta[^>]+content="([^"]*)"[^>]+property="og:image"', text
                )
            if m:
                return {"image_url": m.group(1)}
            raise HTTPException(404, "해당 페이지에서 대표이미지를 찾을 수 없습니다")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[fetch-product-image] 이미지 추출 실패: {e}")
        raise HTTPException(500, f"이미지 추출 중 오류: {str(e)}")


# ══════════════════════════════════════════════
# 마켓 주문 동기화
# ══════════════════════════════════════════════


class SyncOrdersRequest(BaseModel):
    days: int = 7
    account_id: Optional[str] = None  # 특정 계정만 동기화


@router.post("/sync-from-markets")
async def sync_orders_from_markets(
    body: SyncOrdersRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """활성 마켓 계정에서 주문 데이터를 가져와 DB에 저장."""
    from backend.domain.samba.account.repository import SambaMarketAccountRepository
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository

    account_repo = SambaMarketAccountRepository(session)

    # 특정 계정 또는 전체 활성 계정
    if body.account_id:
        target = await account_repo.get_async(body.account_id)
        if not target:
            active_accounts = []
        else:
            # 테넌트 소유권 검증
            if tenant_id is not None and target.tenant_id != tenant_id:
                raise HTTPException(403, "해당 계정에 대한 권한이 없습니다")
            active_accounts = [target]
    else:
        # 테넌트 필터링: tenant_id가 있으면 해당 테넌트 계정만 조회
        if tenant_id is not None:
            active_accounts = await account_repo.filter_by_async(
                is_active=True, order_by="created_at", order_by_desc=True
            )
            # in-memory 필터링으로 tenant_id 또는 None(공용) 계정만 유지
            active_accounts = [
                a
                for a in active_accounts
                if a.tenant_id == tenant_id or a.tenant_id is None
            ]
        else:
            active_accounts = await account_repo.filter_by_async(
                is_active=True, order_by="created_at", order_by_desc=True
            )

    svc = _write_service(session)
    results: list[dict[str, Any]] = []
    total_synced = 0

    # ORM 객체를 딕셔너리로 미리 추출 — rollback 후 lazy loading MissingGreenlet 방지
    account_snapshots = [
        {
            "id": a.id,
            "market_type": a.market_type,
            "market_name": a.market_name,
            "seller_id": a.seller_id or "",
            "api_key": a.api_key,
            "api_secret": a.api_secret,
            "additional_fields": a.additional_fields or {},
            "tenant_id": a.tenant_id,
        }
        for a in active_accounts
    ]

    # 소싱처별 원문 URL 템플릿 (상수)
    _sourcing_urls = {
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

    # ── 병렬 사전조회: 각 마켓 get_orders() HTTP 호출을 동시에 실행 ──────────
    # 세션 없이 순수 HTTP만 병렬화 — DB 작업/파싱/발주확인은 기존 루프에서 수행
    _pre_settings_keys: set[str] = set()
    for _pacc in account_snapshots:
        _pmt = _pacc["market_type"]
        _pex = _pacc["additional_fields"]
        if _pmt == "smartstore" and not (
            (_pex.get("clientId") or _pacc["api_key"])
            and (_pex.get("clientSecret") or _pacc["api_secret"])
        ):
            _pre_settings_keys.add("store_smartstore")
        elif _pmt == "11st" and not (_pex.get("apiKey") or _pacc["api_key"]):
            _pre_settings_keys.add("store_11st")
        elif _pmt == "ebay" and not (
            (_pex.get("clientId") or _pex.get("appId") or _pacc["api_key"])
            and (_pex.get("clientSecret") or _pex.get("certId") or _pacc["api_secret"])
            and (_pex.get("oauthToken") or _pex.get("authToken"))
        ):
            _pre_settings_keys.add("store_ebay")
        elif _pmt == "ssg" and not (_pex.get("apiKey") or _pacc["api_key"]):
            _pre_settings_keys.add("store_ssg")

    _pre_settings: dict[str, dict] = {}
    if _pre_settings_keys:
        _pre_svc_repo = SambaSettingsRepository(session)
        for _psk in _pre_settings_keys:
            _prow = await _pre_svc_repo.find_by_async(key=_psk)
            if _prow and isinstance(_prow.value, dict):
                _pre_settings[_psk] = _prow.value

    async def _pre_fetch_orders(
        acc: dict[str, Any], days: int
    ) -> tuple[str, list | None]:
        """마켓 API에서 초기 주문 목록 조회 (세션 없음, HTTP만)"""
        _aid = acc["id"]
        _mtype = acc["market_type"]
        _extr = acc["additional_fields"]
        _sid = acc["seller_id"]
        try:
            if _mtype == "smartstore":
                _cid = _extr.get("clientId", "") or acc["api_key"] or ""
                _csec = _extr.get("clientSecret", "") or acc["api_secret"] or ""
                if not _cid or not _csec:
                    _sv = _pre_settings.get("store_smartstore", {})
                    _cid = _cid or _sv.get("clientId", "")
                    _csec = _csec or _sv.get("clientSecret", "")
                if not _cid or not _csec:
                    return _aid, None
                from backend.domain.samba.proxy.smartstore import SmartStoreClient

                _c = SmartStoreClient(_cid, _csec)
                return _aid, await _c.get_orders(days=days)

            elif _mtype == "lotteon":
                _ak = _extr.get("apiKey", "") or acc["api_key"] or ""
                if not _ak:
                    return _aid, None
                from backend.domain.samba.proxy.lotteon import LotteonClient

                _c = LotteonClient(_ak)
                await _c.test_auth()
                return _aid, await _c.get_orders(days=days)

            elif _mtype == "playauto":
                _ak = _extr.get("apiKey", "") or acc["api_key"] or ""
                if not _ak:
                    return _aid, None
                from datetime import UTC as _paut, datetime as _padt, timedelta as _patd

                from backend.domain.samba.proxy.playauto import PlayAutoClient

                _c = PlayAutoClient(_ak)
                try:
                    _sd = (_padt.now(_paut) - _patd(days=days)).strftime("%Y%m%d")
                    return _aid, await _c.get_orders(start_date=_sd, count=500)
                finally:
                    await _c.close()

            elif _mtype == "coupang":
                _ack = _extr.get("accessKey", "") or acc.get("api_key", "") or ""
                _sck = _extr.get("secretKey", "") or acc.get("api_secret", "") or ""
                _vid = _extr.get("vendorId", "") or _sid or ""
                if not all([_ack, _sck, _vid]):
                    return _aid, None
                from backend.domain.samba.proxy.coupang import CoupangClient

                _c = CoupangClient(_ack, _sck, _vid)
                return _aid, await _c.get_orders(days=days)

            elif _mtype == "11st":
                _ak = _extr.get("apiKey", "") or acc["api_key"] or ""
                if not _ak:
                    _sv = _pre_settings.get("store_11st", {})
                    _ak = _sv.get("apiKey", "") or ""
                if not _ak:
                    return _aid, None
                from datetime import datetime as _11dt, timedelta as _11td
                from zoneinfo import ZoneInfo as _11zi

                from backend.domain.samba.proxy.elevenst import ElevenstClient

                _KST11 = _11zi("Asia/Seoul")
                _fmt11 = "%Y%m%d%H%M"
                _st11 = (_11dt.now(_KST11) - _11td(days=days)).strftime(_fmt11)
                _et11 = _11dt.now(_KST11).strftime(_fmt11)
                _c = ElevenstClient(_ak)
                return _aid, await _c.get_orders(_st11, _et11)

            elif _mtype == "ebay":
                _appid = _extr.get("clientId") or _extr.get("appId") or acc["api_key"]
                _certid = (
                    _extr.get("clientSecret")
                    or _extr.get("certId")
                    or acc["api_secret"]
                )
                _rtok = _extr.get("oauthToken") or _extr.get("authToken", "")
                if not (_appid and _certid and _rtok):
                    _sv = _pre_settings.get("store_ebay", {})
                    _appid = _appid or _sv.get("clientId", "") or _sv.get("appId", "")
                    _certid = (
                        _certid or _sv.get("clientSecret", "") or _sv.get("certId", "")
                    )
                    _rtok = (
                        _rtok or _sv.get("oauthToken", "") or _sv.get("authToken", "")
                    )
                if not (_appid and _certid and _rtok):
                    return _aid, None
                from backend.domain.samba.proxy.ebay import EbayClient

                _c = EbayClient(
                    app_id=_appid,
                    dev_id="",
                    cert_id=_certid,
                    refresh_token=_rtok,
                    sandbox=bool(_extr.get("sandbox", False)),
                )
                return _aid, await _c.get_orders(days=days)

            elif _mtype == "ssg":
                _ak = _extr.get("apiKey", "") or acc["api_key"] or ""
                if not _ak:
                    _sv = _pre_settings.get("store_ssg", {})
                    _ak = _sv.get("apiKey", "") or ""
                if not _ak:
                    return _aid, None
                from backend.domain.samba.proxy.ssg import SSGClient

                _c = SSGClient(_ak)
                return _aid, await _c.get_orders(days=days)

        except Exception as _pfe:
            logger.warning(f"[주문동기화] 병렬 사전조회 실패 ({_mtype}): {_pfe}")
        return _aid, None

    _prefetch_raw = await asyncio.gather(
        *[_pre_fetch_orders(acc, body.days) for acc in account_snapshots],
        return_exceptions=True,
    )
    _raw_cache: dict[str, list] = {}
    for _pr in _prefetch_raw:
        if isinstance(_pr, Exception):
            continue
        _paid, _praw = _pr
        if _praw is not None:
            _raw_cache[_paid] = _praw
    logger.info(
        f"[주문동기화] 병렬 사전조회 완료: {len(_raw_cache)}/{len(account_snapshots)}개 계정"
    )
    # ── 병렬 사전조회 끝 ──────────────────────────────────────────────────────

    for account in account_snapshots:
        market_type = account["market_type"]
        extras = account["additional_fields"]
        seller_id = account["seller_id"]
        label = f"{account['market_name']}({seller_id})"

        # 마켓 클라이언트들의 httpx keepalive 좀비 차단 — 매 계정 처리 후 명시적 aclose.
        # 미회수 시 hang 한 번에 다음 계정·다른 마켓 호출까지 영향(2026-05-15 사고).
        _clients_to_close: list[Any] = []

        try:
            orders_data: list[dict[str, Any]] = []
            unconfirmed_ids: list[str] = []

            if market_type == "smartstore":
                from backend.domain.samba.proxy.smartstore import SmartStoreClient

                client_id = extras.get("clientId", "") or account["api_key"] or ""
                client_secret = (
                    extras.get("clientSecret", "") or account["api_secret"] or ""
                )
                if not client_id or not client_secret:
                    # fallback: 공유 설정
                    settings_repo = SambaSettingsRepository(session)
                    row = await settings_repo.find_by_async(key="store_smartstore")
                    if row and isinstance(row.value, dict):
                        client_id = client_id or row.value.get("clientId", "")
                        client_secret = client_secret or row.value.get(
                            "clientSecret", ""
                        )
                if not client_id or not client_secret:
                    results.append(
                        {"account": label, "status": "skip", "message": "인증정보 없음"}
                    )
                    continue
                client = SmartStoreClient(client_id, client_secret)
                _clients_to_close.append(client)
                raw_orders = _raw_cache.get(account["id"])
                if raw_orders is None:
                    raw_orders = await client.get_orders(days=body.days)
                # 발주 미확인(PAYED) 주문 자동 발주확인
                unconfirmed_ids = []
                for ro in raw_orders:
                    po = ro.get("productOrder", ro)
                    order_info = ro.get("order", {})
                    # 클레임 정보: claim / cancel / currentClaim 순으로 확인
                    # 취소요청 시 응답 최상위에 'cancel' 키로 오는 경우 처리
                    claim_info = (
                        ro.get("claim")
                        or ro.get("cancel")
                        or ro.get("currentClaim")
                        or po.get("claim")
                        or {}
                    )
                    orders_data.append(
                        _parse_smartstore_order(
                            po, order_info, account["id"], label, claim_info=claim_info
                        )
                    )
                    if (
                        po.get("placeOrderStatus") == "NOT_YET"
                        and po.get("productOrderStatus") == "PAYED"
                    ):
                        unconfirmed_ids.append(po.get("productOrderId", ""))
                # 발주확인 실행
                if unconfirmed_ids:
                    try:
                        await client.confirm_product_orders(unconfirmed_ids)
                        logger.info(
                            f"[주문동기화] {label}: {len(unconfirmed_ids)}건 발주확인 완료"
                        )
                    except Exception as ce:
                        logger.warning(f"[주문동기화] {label}: 발주확인 실패 — {ce}")

                # last-changed API 권한 제한 보완:
                # DB에 있는 미완결 주문을 직접 재조회하여 배송완료/취소요청 등 최신 상태 반영
                _pending_statuses = {
                    "발주미확인",
                    "발송대기",
                    "결제완료",
                    "배송대기중",
                    "송장전송완료",
                    "국내배송중",
                }
                _already_fetched = {
                    d["order_number"] for d in orders_data if d.get("order_number")
                }
                from sqlalchemy import select as _sa_select
                from backend.domain.samba.order.model import SambaOrder as _SambaOrder
                from datetime import datetime as _dt, timedelta, timezone as _tz

                _cutoff = _dt.now(_tz.utc) - timedelta(days=max(body.days, 30))
                _stmt = (
                    _sa_select(_SambaOrder.order_number)
                    .where(
                        _SambaOrder.channel_id == account["id"],
                        _SambaOrder.shipping_status.in_(_pending_statuses),
                        _SambaOrder.updated_at >= _cutoff,
                    )
                    .limit(300)
                )
                _res = await session.execute(_stmt)
                _pending_numbers = [
                    r[0]
                    for r in _res.fetchall()
                    if r[0] and r[0] not in _already_fetched
                ]
                if _pending_numbers:
                    logger.info(
                        f"[주문동기화] {label}: 미완결 주문 {len(_pending_numbers)}건 직접 재조회"
                    )
                    try:
                        _extra_raws = await client.get_product_orders_by_ids(
                            _pending_numbers
                        )
                        for ro2 in _extra_raws:
                            po2 = ro2.get("productOrder", ro2)
                            order_info2 = ro2.get("order", {})
                            claim_info2 = (
                                ro2.get("claim")
                                or ro2.get("cancel")
                                or ro2.get("currentClaim")
                                or po2.get("claim")
                                or {}
                            )
                            orders_data.append(
                                _parse_smartstore_order(
                                    po2,
                                    order_info2,
                                    account["id"],
                                    label,
                                    claim_info=claim_info2,
                                )
                            )
                    except Exception as _ex:
                        logger.warning(
                            f"[주문동기화] {label}: 미완결 주문 직접 재조회 실패 — {_ex}"
                        )

            elif market_type == "lotteon":
                from backend.domain.samba.proxy.lotteon import LotteonClient

                api_key = extras.get("apiKey", "") or account["api_key"] or ""
                if not api_key:
                    results.append(
                        {
                            "account": label,
                            "status": "skip",
                            "message": "롯데ON API Key 없음",
                        }
                    )
                    continue
                lotteon_client = LotteonClient(api_key)
                _clients_to_close.append(lotteon_client)
                await lotteon_client.test_auth()
                raw_orders = _raw_cache.get(account["id"])
                if raw_orders is None:
                    raw_orders = await lotteon_client.get_orders(days=body.days)
                logger.info(
                    f"[주문동기화] {label}: 롯데ON 주문 {len(raw_orders)}건 조회"
                )
                # 신규주문(odPrgsStepCd=11=출고지시) 자동 연동완료 통보 대상 수집
                # SellerDeliveryOrdersSearch는 11(출고지시)/23(회수지시)만 반환 — "10"은 영원히 안 잡힘(공식 문서 기준)
                # SellerIfCompleteInform(ifCplYN=Y) 호출 시 롯데ON에서 자동으로 11→12(상품준비)로 전이됨
                lotteon_confirmed_count = 0
                unconfirmed_items: list[dict] = []
                for ro in raw_orders:
                    orders_data.append(_parse_lotteon_order(ro, account["id"], label))
                    step_cd = str(ro.get("odPrgsStepCd", "") or "")
                    if step_cd == "11":
                        unconfirmed_items.append(
                            {
                                "odNo": ro.get("odNo", ""),
                                "odSeq": ro.get("odSeq", 1) or 1,
                                "procSeq": ro.get("procSeq", 1) or 1,
                            }
                        )

                # 주문확인(SellerIfCompleteInform, ifCplYN=Y) 일괄 실행 — 호출 후 셀러센터에서 상품준비중 자동 전이
                if unconfirmed_items:
                    try:
                        ok = await lotteon_client.confirm_orders(unconfirmed_items)
                        if ok:
                            lotteon_confirmed_count = len(unconfirmed_items)
                            logger.info(
                                f"[주문동기화] {label}: {len(unconfirmed_items)}건 주문확인 완료 (출고지시→상품준비중 자동 전이)"
                            )
                            # 로컬 표시도 즉시 상품준비중으로 갱신 (다음 sync까지 기다리지 않음)
                            _confirmed_keys = {
                                f"{it['odNo']}_{it['odSeq']}_{it['procSeq']}"
                                for it in unconfirmed_items
                            }
                            for od in orders_data:
                                if (
                                    od.get("source") == "lotteon"
                                    and od.get("order_number") in _confirmed_keys
                                    and od.get("shipping_status")
                                    in ("발주확인대기", "출고지시")
                                ):
                                    od["shipping_status"] = "상품준비"
                                    od["status"] = "preparing"
                        else:
                            logger.warning(
                                f"[주문동기화] {label}: 주문확인 API 응답 실패(rsltCd != 0000)"
                            )
                    except Exception as ce:
                        logger.warning(f"[주문동기화] {label}: 주문확인 실패 — {ce}")

                # ── 정산예상 계산용 raw 필드 매핑 (롯데ON 공식 정산공식, 2026-04-30 재확인) ─
                # SellerDeliveryOrdersSearch 실제 응답 키:
                #   slAmt            = 총판매금액 (= 판매단가 × 수량)
                #   actualAmt        = 고객결제금액 (= 슬amt − 전체할인)
                #   prSfcoShrAmtSum  = 당사(롯데/이커머스) 부담 할인 합 (= ajstDcAmt 역할)
                #   prEntpShrAmtSum  = 제휴몰 부담 할인 합
                #   sptDcPgmCmsnSum  = 셀러 부담 할인 합 (지원할인 PGM)
                #   fvrAmtSum        = 전체 할인합 (= prSfco + prEntp + sptDcPgm)
                # → bseCmsn/pcsCmsn/dvCmsn/ajstDcAmt 필드는 이 API에 존재하지 않음.
                #   기본수수료는 카테고리 fee_rate × slAmt, PCS는 가격비교 채널만 부과,
                #   조정(당사부담환급)은 prSfcoShrAmtSum 으로 대체.
                # 정산공식: pymtAmt = actualAmt − (bseCmsn + pcsCmsn + dvCmsn − ajstDcAmt)
                # 키: (odNo, odSeq) — 같은 odNo에 여러 옵션/수량이 묶인 멀티라인 주문에서
                # odNo만 사용하면 한 라인의 값이 다른 라인을 덮어써 모든 라인의 결제/정산 금액이
                # 동일해지는 버그가 발생함(2026-05-15 수정).
                sl_amt_map: dict[tuple[str, str], int] = {}  # 총판매금액 (slAmt)
                fvr_amt_map: dict[tuple[str, str], int] = {}  # 전체 할인합
                actual_amt_map: dict[
                    tuple[str, str], int
                ] = {}  # 고객결제금액 (actualAmt)
                lotte_dc_map: dict[
                    tuple[str, str], int
                ] = {}  # 당사부담할인 (prSfcoShrAmtSum)
                ch_no_map: dict[
                    str, str
                ] = {}  # 채널번호 (chNo) — 주문 단위라 odNo 키 유지

                def _pick(d: dict, *keys: str) -> int:
                    for k in keys:
                        v = d.get(k)
                        if v not in (None, "", 0, "0"):
                            try:
                                return int(float(v))
                            except (TypeError, ValueError):
                                continue
                    return 0

                for ro in raw_orders:
                    _od_no = str(ro.get("odNo") or "")
                    if not _od_no:
                        continue
                    _od_seq = str(ro.get("odSeq", "1") or "1")
                    _line_key = (_od_no, _od_seq)
                    _slamt = _pick(ro, "slAmt", "slPrc")
                    _fvr = _pick(ro, "fvrAmtSum")
                    _actual = _pick(ro, "actualAmt")
                    _lotte_dc = _pick(ro, "prSfcoShrAmtSum")
                    _ch_no = str(ro.get("chNo") or "")
                    # 라인(odSeq) 단위 저장 — 같은 odNo의 다른 옵션/수량이 서로 덮어쓰지 않도록.
                    if _slamt > sl_amt_map.get(_line_key, 0):
                        sl_amt_map[_line_key] = _slamt
                    if _fvr > fvr_amt_map.get(_line_key, 0):
                        fvr_amt_map[_line_key] = _fvr
                    if _actual > actual_amt_map.get(_line_key, 0):
                        actual_amt_map[_line_key] = _actual
                    if _lotte_dc > lotte_dc_map.get(_line_key, 0):
                        lotte_dc_map[_line_key] = _lotte_dc
                    if _ch_no:
                        ch_no_map[_od_no] = _ch_no
                logger.info(
                    f"[주문동기화] {label}: 정산필드 매핑 {len(sl_amt_map)}건 "
                    f"(raw_orders {len(raw_orders)}건)"
                )

                # ── 정산금액 매칭 (SettleItmdSales) ─────────────────────────
                # 정산 데이터는 배송완료 → 구매확정 후 수일 지나서 생성되므로
                # 주문 조회 기간(body.days)보다 넓게(최대 30일) 조회해야 매칭률 ↑.
                # 최대값 30은 api_client.get_settlement_items 내부에서 cap.
                try:
                    settle_items = await lotteon_client.get_settlement_items(days=30)
                    # (odNo, odSeq, procSeq) → 정산 데이터 매핑
                    settle_map: dict[tuple[str, str, str], dict] = {}
                    for si in settle_items:
                        key = (
                            str(si.get("odNo", "")),
                            str(si.get("odSeq", "")),
                            str(si.get("procSeq", "")),
                        )
                        settle_map[key] = si
                    # 매출 주문에 매칭 → revenue/fee_rate 갱신
                    matched = 0
                    for i, ro in enumerate(raw_orders):
                        key = (
                            str(ro.get("odNo", "")),
                            str(ro.get("odSeq", "1")),
                            str(ro.get("procSeq", "1")),
                        )
                        si = settle_map.get(key)
                        if not si:
                            continue
                        pymt_amt = float(si.get("pymtAmt", 0) or 0)
                        sl_amt = float(si.get("slAmt", 0) or 0)
                        sl_qty = float(si.get("slQty", 1) or 1)
                        gross = sl_amt * sl_qty
                        # 고객결제금액 = 총판매 - 셀러부담할인 - 상품할인(셀러+이커머스)
                        slr_dc = float(si.get("slrDcAmt", 0) or 0)
                        pd_dc_slr = float(si.get("pdDcSlrAmt", 0) or 0)
                        pd_dc_oco = float(si.get("pdDcOcoAmt", 0) or 0)
                        customer_paid = max(0.0, gross - slr_dc - pd_dc_slr - pd_dc_oco)
                        if pymt_amt > 0 and customer_paid > 0:
                            fee_rate = round((1 - pymt_amt / customer_paid) * 100, 2)
                            orders_data[i]["revenue"] = pymt_amt
                            orders_data[i]["fee_rate"] = fee_rate
                            orders_data[i]["total_payment_amount"] = customer_paid
                            matched += 1
                        elif pymt_amt > 0 and gross > 0:
                            # 할인 필드가 비어 있으면 기존 방식(총판매 기준)으로 폴백
                            fee_rate = round((1 - pymt_amt / gross) * 100, 2)
                            orders_data[i]["revenue"] = pymt_amt
                            orders_data[i]["fee_rate"] = fee_rate
                            matched += 1
                    logger.info(
                        f"[주문동기화] {label}: 정산 매칭 {matched}/{len(raw_orders)}건 "
                        f"(정산 API {len(settle_items)}건)"
                    )

                    # ── 기존 DB 주문 보정 (구매확정 후 정산 데이터로 정확값 덮어쓰기) ─
                    # raw_orders는 odPrgsStepCd=11/23만 반환하므로,
                    # 이미 발주확인되어 raw에서 빠진 주문은 위 in-memory 매칭으로 보정 안 됨.
                    # 정산 API에 있는 모든 키에 대해 DB를 직접 UPDATE 한다.
                    db_updated = 0
                    from sqlalchemy import text as _sa_text

                    for (od_no_k, od_seq_k, proc_seq_k), si in settle_map.items():
                        if not od_no_k:
                            continue
                        pymt_amt = float(si.get("pymtAmt", 0) or 0)
                        if pymt_amt <= 0:
                            continue
                        sl_amt = float(si.get("slAmt", 0) or 0)
                        sl_qty = float(si.get("slQty", 1) or 1)
                        gross = sl_amt * sl_qty
                        slr_dc = float(si.get("slrDcAmt", 0) or 0)
                        pd_dc_slr = float(si.get("pdDcSlrAmt", 0) or 0)
                        pd_dc_oco = float(si.get("pdDcOcoAmt", 0) or 0)
                        customer_paid = max(0.0, gross - slr_dc - pd_dc_slr - pd_dc_oco)
                        base = customer_paid if customer_paid > 0 else gross
                        if base <= 0:
                            continue
                        new_fee_rate = round((1 - pymt_amt / base) * 100, 2)
                        # od_seq/proc_seq는 SambaOrder에 Text로 저장되어 있음
                        # 동일 odNo + odSeq + procSeq 매칭 (account 무관 — odNo는 전역 유일)
                        try:
                            res = await session.execute(
                                _sa_text(
                                    "UPDATE samba_order "
                                    "SET revenue = :rev, fee_rate = :fr, "
                                    "    total_payment_amount = COALESCE(NULLIF(:cp, 0), total_payment_amount), "
                                    "    updated_at = now() "
                                    "WHERE source = 'lotteon' "
                                    "  AND od_no = :od "
                                    "  AND COALESCE(od_seq, '1') = :os "
                                    "  AND COALESCE(proc_seq, '1') = :ps "
                                    "  AND (revenue IS NULL OR revenue <> :rev)"
                                ),
                                {
                                    "rev": pymt_amt,
                                    "fr": new_fee_rate,
                                    "cp": customer_paid,
                                    "od": od_no_k,
                                    "os": od_seq_k or "1",
                                    "ps": proc_seq_k or "1",
                                },
                            )
                            db_updated += res.rowcount or 0
                        except Exception as ue:
                            logger.warning(
                                f"[주문동기화] {label}: 정산 DB UPDATE 실패 odNo={od_no_k} — {ue}"
                            )
                    if db_updated:
                        logger.info(
                            f"[주문동기화] {label}: 정산 API → DB 보정 {db_updated}건 "
                            "(구매확정된 기존 주문 revenue/fee_rate 갱신)"
                        )
                except Exception as se:
                    logger.warning(f"[주문동기화] {label}: 정산 조회 실패 — {se}")

                # 발주확인은 수동 처리 (원소싱처 재고/가격 확인 후 사용자가 결정)
                # 교환 클레임 조회 → 기존 주문 shipping_status 업데이트
                try:
                    exchange_claims = await lotteon_client.get_exchanges(days=body.days)
                    logger.info(f"[롯데ON] 교환 클레임 조회: {len(exchange_claims)}건")
                    if exchange_claims:
                        exchange_step_map = {
                            "21": "교환요청",
                            "22": "교환회수완료",
                            "23": "교환회수완료",
                            "24": "교환재배송",
                            "25": "교환완료",
                        }
                        exchange_priority = {
                            "교환요청": 1,
                            "교환회수완료": 2,
                            "교환재배송": 3,
                            "교환완료": 4,
                        }
                        for claim in exchange_claims:
                            ex_od_no = claim.get("odNo", "")
                            clm_no = claim.get("clmNo", "")
                            step_cd = str(claim.get("odPrgsStepCd", "") or "")
                            ex_status = exchange_step_map.get(step_cd, "교환요청")
                            logger.info(
                                f"[롯데ON][교환클레임] odNo={ex_od_no} clmNo={clm_no} stepCd={step_cd} → {ex_status}"
                            )
                            found_in_data = False
                            for od in orders_data:
                                # order_number는 합성키(odNo_odSeq_procSeq)이므로 od_no로 비교
                                if od.get("od_no") == ex_od_no:
                                    cur_status = od.get("shipping_status", "")
                                    cur_p = exchange_priority.get(cur_status, 0)
                                    new_p = exchange_priority.get(ex_status, 0)
                                    if cur_p == 0 or new_p >= cur_p:
                                        od["shipping_status"] = ex_status
                                        if step_cd in ("21", "22", "23", "24"):
                                            od["status"] = "exchanging"
                                        elif step_cd == "25":
                                            od["status"] = "exchanged"
                                    found_in_data = True
                                    break
                            if not found_in_data and ex_od_no:
                                from sqlalchemy import text as _sa_text_ex

                                _ex_row = await session.execute(
                                    _sa_text_ex(
                                        "SELECT id FROM samba_order "
                                        "WHERE source = 'lotteon' AND od_no = :od_no LIMIT 1"
                                    ),
                                    {"od_no": ex_od_no},
                                )
                                _ex_id = (_ex_row.fetchone() or [None])[0]
                                existing = (
                                    await svc.repo.get_async(_ex_id) if _ex_id else None
                                )
                                if existing:
                                    cur_p = exchange_priority.get(
                                        existing.shipping_status, 0
                                    )
                                    new_p = exchange_priority.get(ex_status, 0)
                                    if cur_p == 0 or new_p >= cur_p:
                                        await svc.update_order(
                                            existing.id,
                                            {"shipping_status": ex_status},
                                        )
                                        logger.info(
                                            f"[롯데ON][교환클레임] DB 직접 업데이트: {ex_od_no} → {ex_status}"
                                        )
                except Exception as ex_err:
                    logger.warning(f"[롯데ON] 교환 클레임 조회 실패: {ex_err}")

                # 취소 클레임 조회 → samba_order.status 갱신
                # step_cd: 11=취소요청, 12=취소처리중, 13=취소완료
                try:
                    cancel_claims = await lotteon_client.get_cancel_orders(
                        days=body.days
                    )
                    logger.info(f"[롯데ON] 취소 클레임 조회: {len(cancel_claims)}건")
                    cancel_step_map = {
                        "11": ("취소요청", "cancel_requested"),
                        "12": ("취소처리중", "cancel_requested"),
                        "13": ("취소완료", "cancelled"),
                    }
                    cancel_priority = {
                        "취소요청": 1,
                        "취소처리중": 2,
                        "취소완료": 3,
                    }
                    for claim in cancel_claims:
                        cn_od_no = claim.get("odNo", "")
                        step_cd_c = str(claim.get("odPrgsStepCd", "") or "")
                        mapped = cancel_step_map.get(step_cd_c)
                        if not mapped or not cn_od_no:
                            continue
                        cn_ship_status, cn_status = mapped
                        found_in_data_c = False
                        for od in orders_data:
                            if od.get("od_no") == cn_od_no:
                                cur_p = cancel_priority.get(
                                    od.get("shipping_status", ""), 0
                                )
                                new_p = cancel_priority.get(cn_ship_status, 0)
                                if cur_p == 0 or new_p >= cur_p:
                                    od["shipping_status"] = cn_ship_status
                                    od["status"] = cn_status
                                found_in_data_c = True
                                break
                        if not found_in_data_c:
                            from sqlalchemy import text as _sa_text_cn

                            _cn_row = await session.execute(
                                _sa_text_cn(
                                    "SELECT id FROM samba_order "
                                    "WHERE source = 'lotteon' AND od_no = :od_no LIMIT 1"
                                ),
                                {"od_no": cn_od_no},
                            )
                            _cn_id = (_cn_row.fetchone() or [None])[0]
                            existing_c = (
                                await svc.repo.get_async(_cn_id) if _cn_id else None
                            )
                            if existing_c:
                                cur_p = cancel_priority.get(
                                    existing_c.shipping_status, 0
                                )
                                new_p = cancel_priority.get(cn_ship_status, 0)
                                if cur_p == 0 or new_p >= cur_p:
                                    await svc.update_order(
                                        existing_c.id,
                                        {"shipping_status": cn_ship_status},
                                    )
                                    logger.info(
                                        f"[롯데ON][취소클레임] DB 직접 업데이트: {cn_od_no} → {cn_ship_status}"
                                    )
                except Exception as cn_err:
                    logger.warning(f"[롯데ON] 취소 클레임 조회 실패: {cn_err}")

                # 반품 클레임 조회 → samba_order.status 갱신
                # step_cd: 11=반품요청, 12=반품수거중, 13=반품완료, 14=반품거부
                try:
                    return_claims = await lotteon_client.get_returns(days=body.days)
                    logger.info(f"[롯데ON] 반품 클레임 조회: {len(return_claims)}건")
                    return_step_map = {
                        "11": ("반품요청", "return_requested"),
                        "12": ("반품요청", "returning"),
                        "13": ("반품완료", "returned"),
                        "14": ("반품거부", "return_requested"),
                    }
                    return_priority = {
                        "반품요청": 1,
                        "반품거부": 1,
                        "반품완료": 2,
                    }
                    for claim in return_claims:
                        rt_od_no = claim.get("odNo", "")
                        step_cd_r = str(claim.get("odPrgsStepCd", "") or "")
                        mapped_r = return_step_map.get(step_cd_r)
                        if not mapped_r or not rt_od_no:
                            continue
                        rt_ship_status, rt_status = mapped_r
                        found_in_data_r = False
                        for od in orders_data:
                            if od.get("od_no") == rt_od_no:
                                cur_p = return_priority.get(
                                    od.get("shipping_status", ""), 0
                                )
                                new_p = return_priority.get(rt_ship_status, 0)
                                if cur_p == 0 or new_p >= cur_p:
                                    od["shipping_status"] = rt_ship_status
                                    od["status"] = rt_status
                                found_in_data_r = True
                                break
                        if not found_in_data_r:
                            from sqlalchemy import text as _sa_text_rt

                            _rt_row = await session.execute(
                                _sa_text_rt(
                                    "SELECT id FROM samba_order "
                                    "WHERE source = 'lotteon' AND od_no = :od_no LIMIT 1"
                                ),
                                {"od_no": rt_od_no},
                            )
                            _rt_id = (_rt_row.fetchone() or [None])[0]
                            existing_r = (
                                await svc.repo.get_async(_rt_id) if _rt_id else None
                            )
                            if existing_r:
                                cur_p = return_priority.get(
                                    existing_r.shipping_status, 0
                                )
                                new_p = return_priority.get(rt_ship_status, 0)
                                if cur_p == 0 or new_p >= cur_p:
                                    await svc.update_order(
                                        existing_r.id,
                                        {"shipping_status": rt_ship_status},
                                    )
                                    logger.info(
                                        f"[롯데ON][반품클레임] DB 직접 업데이트: {rt_od_no} → {rt_ship_status}"
                                    )
                except Exception as rt_err:
                    logger.warning(f"[롯데ON] 반품 클레임 조회 실패: {rt_err}")

                # 배송 진행 상태 갱신 (SellerDeliveryProgressStateSearch)
                # 이미 수집된 주문(상품준비→발송완료→배송완료→구매확정) 상태 업데이트
                _lo_delivery_status_map = {
                    "11": ("출고지시", "preparing"),
                    "12": ("상품준비", "preparing"),
                    "13": ("발송완료", "shipping"),
                    "14": ("배송완료", "delivered"),
                    "15": ("수취완료", "delivered"),
                    "21": ("취소완료", "cancelled"),
                    "22": ("철회", "cancelled"),
                    "23": ("회수지시", "return_requested"),
                    "24": ("회수진행", "return_requested"),
                    "25": ("회수완료", "return_requested"),
                    "26": ("회수확정", "return_requested"),
                    "27": ("반품완료", "return_requested"),
                }
                # 이미 orders_data에서 처리한 주문은 중복 갱신 불필요
                _already_in_data = {
                    od.get("order_number")
                    for od in orders_data
                    if od.get("order_number")
                }
                try:
                    progress_states = await lotteon_client.get_delivery_progress_states(
                        days=body.days
                    )
                    _ps_updated = 0
                    for ps in progress_states:
                        od_no = str(ps.get("odNo", "") or "")
                        od_seq = str(ps.get("odSeq", 1) or 1)
                        if not od_no:
                            continue
                        # 저장 시 키와 동일하게 (odNo, odSeq) 2부분만 사용
                        # procSeq는 처리 단계마다 바뀌므로 키에서 제외
                        order_number = f"{od_no}_{od_seq}"
                        if order_number in _already_in_data:
                            continue
                        step_cd = str(ps.get("odPrgsStepCd", "") or "")
                        mapped = _lo_delivery_status_map.get(step_cd)
                        if not mapped:
                            continue
                        new_ship_status, new_status = mapped
                        invc_no = str(ps.get("invcNo", "") or "")
                        dv_co_cd = str(ps.get("dvCoCd", "") or "")
                        from sqlalchemy import text as _sa_text_ps

                        _set_parts = [
                            "shipping_status = :ship_status",
                            "updated_at = now()",
                        ]
                        _ps_params: dict[str, Any] = {
                            "order_number": order_number,
                            "ship_status": new_ship_status,
                        }
                        if invc_no:
                            _set_parts.append("tracking_number = :invc_no")
                            _ps_params["invc_no"] = invc_no
                        if dv_co_cd:
                            _set_parts.append("shipping_company = :dv_co_cd")
                            _ps_params["dv_co_cd"] = dv_co_cd
                        _ps_result = await session.execute(
                            _sa_text_ps(
                                f"UPDATE samba_order SET {', '.join(_set_parts)} "
                                "WHERE source = 'lotteon' AND order_number = :order_number "
                                "AND status NOT IN ('cancelled', 'confirmed', 'return_requested')"
                            ),
                            _ps_params,
                        )
                        if _ps_result.rowcount:
                            _ps_updated += 1
                    if _ps_updated:
                        logger.info(
                            f"[주문동기화] {label}: 배송상태 갱신 {_ps_updated}건"
                        )
                except Exception as ps_err:
                    logger.warning(
                        f"[주문동기화] {label}: 롯데ON 배송상태 갱신 실패 — {ps_err}"
                    )

            elif market_type == "playauto":
                from datetime import UTC, datetime, timedelta

                from backend.domain.samba.proxy.playauto import PlayAutoClient

                api_key = extras.get("apiKey", "") or account["api_key"] or ""
                if not api_key:
                    results.append(
                        {"account": label, "status": "skip", "message": "API Key 없음"}
                    )
                    continue
                # 별칭 매핑 로드 (store_playauto 설정에서)
                alias_map: dict[str, str] = {}
                try:
                    settings_repo = SambaSettingsRepository(session)
                    pa_setting = await settings_repo.find_by_async(key="store_playauto")
                    if pa_setting and isinstance(pa_setting.value, dict):
                        for ak in ("alias1", "alias2", "alias3", "alias4", "alias5"):
                            av = pa_setting.value.get(ak, "")
                            code, nick = parse_playauto_alias_entry(av)
                            if code and nick:
                                alias_map[code] = nick
                except Exception:
                    pass
                # 롯데홈쇼핑 직접 연동 계정이 있으면 플레이오토 중복 주문 차단
                # account_id 단독 호출 시 account_snapshots에 해당 계정만 있어 DB 전체 조회로 판단
                from sqlalchemy import text as _check_text  # noqa: F811

                _lottehome_row = await session.execute(
                    _check_text(
                        "SELECT 1 FROM samba_market_account "
                        "WHERE market_type = 'lottehome' AND is_active = true LIMIT 1"
                    )
                )
                _has_lottehome = _lottehome_row.first() is not None
                pa_client = PlayAutoClient(api_key)
                _clients_to_close.append(pa_client)
                try:
                    start_date = (
                        datetime.now(UTC) - timedelta(days=body.days)
                    ).strftime("%Y%m%d")
                    # 전체 상태 한번에 조회 (상태 필터 없이)
                    raw_orders = _raw_cache.get(account["id"])
                    if raw_orders is None:
                        raw_orders = await pa_client.get_orders(
                            start_date=start_date,
                            count=500,
                        )
                    logger.info(f"[주문동기화] 플레이오토: {len(raw_orders)}건 조회")

                    # 롯데홈쇼핑 직접 연동 시 기존 플레이오토 중복 주문 삭제 (최초 1회)
                    if _has_lottehome:
                        from sqlalchemy import text as _pa_text

                        _del_result = await session.execute(
                            _pa_text(
                                "DELETE FROM samba_order "
                                "WHERE source = 'playauto' "
                                "AND channel_id = :cid "
                                "AND (source_site LIKE '%롯데아이몰%' OR source_site LIKE '%롯데홈쇼핑%')"
                            ),
                            {"cid": account["id"]},
                        )
                        if _del_result.rowcount:
                            logger.info(
                                f"[주문동기화] 플레이오토 롯데홈쇼핑 중복 주문 {_del_result.rowcount}건 삭제"
                            )

                    for ro in raw_orders:
                        # 파생 주문 스킵 (사본-취소마감, ★교환주문 — 원주문에 이미 정보 포함)
                        _pname = ro.get("ProdName", "")
                        if _pname.startswith("[사본-") or "★교환주문" in _pname:
                            continue
                        # 롯데홈쇼핑 직접 연동 계정이 있으면 플레이오토 롯데홈 주문 스킵
                        if _has_lottehome:
                            _ro_site = str(ro.get("SiteName", "") or "").strip()
                            if "롯데아이몰" in _ro_site or "롯데홈쇼핑" in _ro_site:
                                continue
                        orders_data.append(
                            _parse_playauto_order(ro, account["id"], label, alias_map)
                        )
                except Exception as e:
                    logger.warning(f"[주문동기화] {label}: 플레이오토 조회 실패 — {e}")
                    results.append(
                        {"account": label, "status": "error", "message": str(e)[:100]}
                    )
                    continue
                finally:
                    await pa_client.close()
            elif market_type == "coupang":
                from backend.domain.samba.proxy.coupang import CoupangClient

                access_key = (
                    extras.get("accessKey", "") or account.get("api_key", "") or ""
                )
                secret_key = (
                    extras.get("secretKey", "") or account.get("api_secret", "") or ""
                )
                vendor_id = extras.get("vendorId", "") or seller_id or ""

                if not all([access_key, secret_key, vendor_id]):
                    results.append(
                        {
                            "account": label,
                            "status": "skip",
                            "message": "쿠팡 인증정보 없음 (accessKey/secretKey/vendorId)",
                        }
                    )
                    continue

                client = CoupangClient(access_key, secret_key, vendor_id)
                _clients_to_close.append(client)
                try:
                    raw_orders = _raw_cache.get(account["id"])
                    if raw_orders is None:
                        raw_orders = await client.get_orders(days=body.days)
                    logger.info(f"[주문동기화] 쿠팡({label}): {len(raw_orders)}건 조회")
                    # ACCEPT(결제완료) + 취소/반품요청 없는 건만 자동 발주확인 대상
                    unconfirmed_box_ids: list[int] = []
                    for ro in raw_orders:
                        try:
                            orders_data.append(
                                _parse_coupang_order(ro, account["id"], label)
                            )
                        except Exception as parse_err:
                            logger.warning(f"[주문동기화] 쿠팡 파싱 실패: {parse_err}")
                            continue
                        if (
                            (ro.get("status") or "").upper() == "ACCEPT"
                            and not ro.get("cancelRequests")
                            and not ro.get("returnRequests")
                        ):
                            box_id_raw = ro.get("shipmentBoxId")
                            try:
                                if box_id_raw is not None:
                                    unconfirmed_box_ids.append(int(box_id_raw))
                            except (TypeError, ValueError):
                                pass

                    # 발주확인 호출 (ACCEPT → INSTRUCT, 상품준비중)
                    if unconfirmed_box_ids:
                        try:
                            ack_results = await client.confirm_orders(
                                unconfirmed_box_ids
                            )
                            success_box_strs = {
                                str(r["shipmentBoxId"])
                                for r in ack_results
                                if r.get("success")
                            }
                            if success_box_strs:
                                # 로컬 표시도 즉시 상품준비중으로 갱신 (다음 sync 까지 대기 X)
                                for od in orders_data:
                                    if (
                                        od.get("source") == "coupang"
                                        and od.get("order_number") in success_box_strs
                                        and od.get("shipping_status") == "결제완료"
                                    ):
                                        od["shipping_status"] = "상품준비중"
                            logger.info(
                                f"[주문동기화] 쿠팡({label}): "
                                f"{len(success_box_strs)}/{len(unconfirmed_box_ids)}건 발주확인 완료"
                            )
                        except Exception as ce:
                            logger.warning(
                                f"[주문동기화] {label}: 쿠팡 발주확인 실패 — {ce}"
                            )
                except Exception as e:
                    logger.warning(f"[주문동기화] {label}: 쿠팡 조회 실패 — {e}")
                    results.append(
                        {"account": label, "status": "error", "message": str(e)[:100]}
                    )
                    continue
            elif market_type == "11st":
                from datetime import UTC, datetime, timedelta

                from backend.domain.samba.proxy.elevenst import ElevenstClient

                api_key = extras.get("apiKey", "") or account["api_key"] or ""
                if not api_key:
                    # SambaSettings의 store_11st에서 fallback
                    settings_repo = SambaSettingsRepository(session)
                    _11st_setting = await settings_repo.find_by_async(key="store_11st")
                    if _11st_setting and isinstance(_11st_setting.value, dict):
                        api_key = _11st_setting.value.get("apiKey", "") or ""
                if not api_key:
                    results.append(
                        {
                            "account": label,
                            "status": "skip",
                            "message": "11번가 API Key 없음",
                        }
                    )
                    continue

                _11st_client = ElevenstClient(api_key)
                _clients_to_close.append(_11st_client)
                _confirm_targets: list[dict[str, str]] = []
                _confirmed = 0
                _fmt = "%Y%m%d%H%M"
                # 11번가 API는 KST 기준 시간을 요구 (UTC+9)
                from zoneinfo import ZoneInfo

                _KST = ZoneInfo("Asia/Seoul")
                _start_dt = datetime.now(_KST) - timedelta(days=body.days)
                _end_dt = datetime.now(_KST)
                _start_time = _start_dt.strftime(_fmt)
                _end_time = _end_dt.strftime(_fmt)

                try:
                    # 결제완료 주문 조회
                    _raw_orders = _raw_cache.get(account["id"])
                    if _raw_orders is None:
                        _raw_orders = await _11st_client.get_orders(
                            _start_time, _end_time
                        )
                    logger.info(
                        f"[주문동기화] {label}: 11번가 주문 {len(_raw_orders)}건 조회"
                    )
                    # 결제완료(ordPrdStat=200) 주문 자동 발주확인
                    for _ro in _raw_orders:
                        # ordPrdStat=900(취소완료)은 orders_data에서 제외
                        # 취소 상태는 get_cancel_requests(취소클레임)에서만 처리
                        # → 이렇게 하지 않으면 취소요청 선제 업데이트 이후 upsert가 취소완료로 덮어씀
                        if str(_ro.get("ordPrdStat", "")) == "900":
                            continue
                        orders_data.append(
                            _parse_elevenst_order(_ro, account["id"], label)
                        )
                        # 결제완료(200) 및 처리중(202) 모두 발주확인 대상
                        if str(_ro.get("ordPrdStat", "")) in ("200", "202"):
                            _ord_no = str(_ro.get("ordNo", "") or "")
                            _ord_prd_seq = str(_ro.get("ordPrdSeq", "") or "")
                            _dlv_no = str(_ro.get("dlvNo", "") or "")
                            if _ord_no and _ord_prd_seq and _dlv_no:
                                _confirm_targets.append(
                                    {
                                        "ord_no": _ord_no,
                                        "ord_prd_seq": _ord_prd_seq,
                                        "dlv_no": _dlv_no,
                                    }
                                )
                            else:
                                logger.warning(
                                    "[주문동기화] %s: 발주확인 스킵 (dlvNo 없음) ordNo=%s ordPrdSeq=%s dlvNo=%r",
                                    label,
                                    _ord_no,
                                    _ord_prd_seq,
                                    _dlv_no,
                                )

                    if _confirm_targets:
                        _confirmed = 0
                        _confirmed_ord_nos: set[str] = set()
                        for _ct in _confirm_targets:
                            try:
                                await _11st_client.confirm_order(
                                    _ct["ord_no"], _ct["ord_prd_seq"], _ct["dlv_no"]
                                )
                                _confirmed += 1
                                _confirmed_ord_nos.add(_ct["ord_no"])
                            except Exception as _ce:
                                logger.warning(
                                    f"[주문동기화] {label}: 11번가 발주확인 실패 "
                                    f"ordNo={_ct['ord_no']} — {_ce}"
                                )
                        # 발주확인 성공한 주문의 status/shipping_status를 배송대기중으로 업데이트
                        for _od in orders_data:
                            if _od.get("order_number") in _confirmed_ord_nos:
                                _od["status"] = "wait_ship"
                                _od["shipping_status"] = "배송대기중"
                        # 이미 DB에 저장된 주문도 즉시 배송대기중으로 갱신
                        for _ord_no in _confirmed_ord_nos:
                            _ex = await svc.repo.find_by_async(order_number=_ord_no)
                            if _ex:
                                await svc.update_order(
                                    _ex.id,
                                    {"shipping_status": "배송대기중"},
                                )
                        logger.info(
                            f"[주문동기화] {label}: 11번가 발주확인 {_confirmed}/{len(_confirm_targets)}건 완료"
                        )

                    # 배송준비중 주문 추가 수집 (결제완료 목록에 없는 건만)
                    _raw_packaging = await _11st_client.get_packaging_orders(
                        _start_time, _end_time
                    )
                    logger.info(
                        f"[주문동기화] {label}: 11번가 배송준비중 {len(_raw_packaging)}건 조회"
                    )
                    _fetched_nos = {d["order_number"] for d in orders_data}
                    for _ro in _raw_packaging:
                        _ord_no = _ro.get("ordNo", "")
                        if _ord_no and _ord_no not in _fetched_nos:
                            orders_data.append(
                                _parse_elevenst_order(_ro, account["id"], label)
                            )
                            _fetched_nos.add(_ord_no)

                except Exception as _e:
                    logger.warning(
                        f"[주문동기화] {label}: 11번가 주문 조회 실패 — {_e}"
                    )
                    results.append(
                        {"account": label, "status": "error", "message": str(_e)[:100]}
                    )
                    continue

                # 취소/반품/교환 클레임 → 주문 상태 업데이트 (3종 병렬 조회)
                try:
                    import asyncio as _asyncio

                    from backend.domain.samba.proxy.elevenst_exchange import (
                        ElevenstExchangeClient,
                    )

                    _exchange_client = ElevenstExchangeClient(api_key)
                    _clients_to_close.append(_exchange_client)
                    (
                        _cancel_claims,
                        _return_claims,
                        _exchange_claims,
                    ) = await _asyncio.gather(
                        _11st_client.get_cancel_requests(_start_time, _end_time),
                        _11st_client.get_return_requests(_start_time, _end_time),
                        _exchange_client.get_exchange_requests(_start_time, _end_time),
                    )
                    logger.info(
                        f"[주문동기화] {label}: 취소 {len(_cancel_claims)}건, "
                        f"반품 {len(_return_claims)}건, "
                        f"교환 {len(_exchange_claims)}건"
                    )

                    for _claim in _cancel_claims:
                        _c_ord_no = _claim.get("ordNo", "")
                        if not _c_ord_no:
                            continue
                        _found = False
                        for _od in orders_data:
                            if _od.get("order_number") == _c_ord_no:
                                _od["shipping_status"] = "취소요청"
                                _od["status"] = "cancelled"
                                _found = True
                                break
                        # _found 여부와 관계없이 DB에 즉시 반영
                        # (upsert 단계에서 ordPrdStat=900 → 취소완료로 덮어씌워질 수 있으므로 선제 업데이트)
                        _ex_cancel = await svc.repo.find_by_async(
                            order_number=_c_ord_no
                        )
                        if _ex_cancel:
                            await svc.update_order(
                                _ex_cancel.id,
                                {"shipping_status": "취소요청"},
                            )

                    for _claim in _return_claims:
                        _r_ord_no = _claim.get("ordNo", "")
                        if not _r_ord_no:
                            continue
                        _found = False
                        for _od in orders_data:
                            if _od.get("order_number") == _r_ord_no:
                                _od["shipping_status"] = "반품요청"
                                _od["status"] = "return_requested"
                                _found = True
                                break
                        if not _found:
                            _ex_return = await svc.repo.find_by_async(
                                order_number=_r_ord_no
                            )
                            if _ex_return:
                                await svc.update_order(
                                    _ex_return.id,
                                    {"shipping_status": "반품요청"},
                                )

                    for _claim in _exchange_claims:
                        _e_ord_no = _claim.get("ordNo", "")
                        if not _e_ord_no:
                            continue
                        _found = False
                        for _od in orders_data:
                            if _od.get("order_number") == _e_ord_no:
                                _od["shipping_status"] = "교환요청"
                                _od["status"] = "exchange_requested"
                                _found = True
                                break
                        # orders_data에 없어도 DB에 즉시 반영
                        # (반품거부 후 교환요청 시 orders_data에 해당 주문이 없을 수 있음)
                        _ex_exchange = await svc.repo.find_by_async(
                            order_number=_e_ord_no
                        )
                        if _ex_exchange:
                            logger.info(
                                f"[주문동기화] {label}: 교환요청 DB 반영 "
                                f"{_e_ord_no} {_ex_exchange.shipping_status} → 교환요청"
                            )
                            await svc.update_order(
                                _ex_exchange.id,
                                {"shipping_status": "교환요청"},
                            )

                except Exception as _ce:
                    logger.warning(
                        f"[주문동기화] {label}: 11번가 클레임 조회 실패 — {_ce}"
                    )
            elif market_type == "ebay":
                from backend.domain.samba.proxy.ebay import (
                    EbayApiError,
                    EbayClient,
                )

                app_id = (
                    extras.get("clientId") or extras.get("appId") or account["api_key"]
                )
                cert_id = (
                    extras.get("clientSecret")
                    or extras.get("certId")
                    or account["api_secret"]
                )
                refresh_token = extras.get("oauthToken") or extras.get("authToken", "")
                # SambaSettings 폴백
                if not (app_id and cert_id and refresh_token):
                    settings_repo = SambaSettingsRepository(session)
                    row = await settings_repo.find_by_async(key="store_ebay")
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
                    results.append(
                        {
                            "account": label,
                            "status": "skip",
                            "message": "eBay 인증정보 없음",
                        }
                    )
                    continue

                ebay_client = EbayClient(
                    app_id=app_id,
                    dev_id="",
                    cert_id=cert_id,
                    refresh_token=refresh_token,
                    sandbox=bool(extras.get("sandbox", False)),
                )
                _clients_to_close.append(ebay_client)
                raw_orders = _raw_cache.get(account["id"])
                if raw_orders is None:
                    try:
                        raw_orders = await ebay_client.get_orders(days=body.days)
                    except EbayApiError as e:
                        err = str(e)
                        if (
                            "scope" in err.lower()
                            or "invalid_scope" in err.lower()
                            or "insufficient" in err.lower()
                        ):
                            results.append(
                                {
                                    "account": label,
                                    "status": "error",
                                    "message": "sell.fulfillment scope 누락 — eBay 재인증 필요",
                                }
                            )
                        else:
                            results.append(
                                {
                                    "account": label,
                                    "status": "error",
                                    "message": err[:150],
                                }
                            )
                        continue

                logger.info(f"[주문동기화] {label}: eBay 주문 {len(raw_orders)}건 조회")

                # USD → KRW 환율 (exchange_rate_service의 USD effectiveRate 우선)
                ebay_exchange_rate = 1400.0
                try:
                    from backend.domain.samba.exchange_rate_service import (
                        build_exchange_rate_response,
                        get_exchange_rate_settings,
                        get_latest_exchange_rates,
                    )

                    _er_settings = await get_exchange_rate_settings(
                        session, account["tenant_id"] or tenant_id
                    )
                    _er_latest = await get_latest_exchange_rates()
                    _er_resp = build_exchange_rate_response(_er_settings, _er_latest)
                    _usd_info = _er_resp.get("currencies", {}).get("USD", {}) or {}
                    _eff_rate = float(_usd_info.get("effectiveRate") or 0)
                    if _eff_rate > 0:
                        ebay_exchange_rate = _eff_rate
                except Exception as e:
                    logger.warning(
                        f"[주문동기화] {label}: 환율 조회 실패, 폴백 1400 사용 — {e}"
                    )

                for ro in raw_orders:
                    orders_data.append(
                        _parse_ebay_order(ro, account["id"], label, ebay_exchange_rate)
                    )

                # Finance API 실제 정산액 조회 — orderId → (net_usd, fee_usd) 매핑
                # sell.finances scope 필요. 방금 들어온 주문은 거래 미확정 상태라 매핑 없을 수 있음
                try:
                    tx_list = await ebay_client.get_transactions(days=body.days)
                    # Finance API 응답 필드:
                    #   amount                = net (이미 수수료 차감된 값)
                    #   totalFeeBasisAmount   = gross (판매가)
                    #   totalFeeAmount        = 실제 수수료
                    # 같은 orderId에 여러 거래(SALE, SHIPPING_LABEL 등) 있을 수 있음 → 누적
                    tx_map: dict[str, dict[str, float]] = {}
                    for tx in tx_list:
                        oid = tx.get("orderId", "") or ""
                        if not oid:
                            continue
                        net = float((tx.get("amount") or {}).get("value", 0) or 0)
                        gross = float(
                            (tx.get("totalFeeBasisAmount") or {}).get("value", 0) or 0
                        )
                        fee = float(
                            (tx.get("totalFeeAmount") or {}).get("value", 0) or 0
                        )
                        booking = tx.get("bookingEntry", "CREDIT")
                        tx_type = tx.get("transactionType", "")
                        tx_id = tx.get("transactionId", "")
                        tx_status = tx.get("transactionStatus", "")
                        logger.info(
                            "[eBay Finance tx] order=%s type=%s book=%s status=%s "
                            "gross=%.2f fee=%.2f net=%.2f id=%s",
                            oid,
                            tx_type,
                            booking,
                            tx_status,
                            gross,
                            fee,
                            net,
                            tx_id,
                        )
                        # DEBIT = 판매자 잔액 차감 (환불, 배송라벨 등)
                        if booking == "DEBIT":
                            net = -net
                            gross = -gross
                            fee = -fee
                        cur = tx_map.setdefault(
                            oid, {"net": 0.0, "gross": 0.0, "fee": 0.0}
                        )
                        cur["net"] += net
                        cur["gross"] += gross
                        cur["fee"] += fee

                    matched = 0
                    for od in orders_data:
                        oid = od.get("ext_order_number") or ""
                        if oid in tx_map:
                            net_usd = tx_map[oid]["net"]
                            gross_usd = tx_map[oid]["gross"]
                            fee_usd = tx_map[oid]["fee"]
                            od["revenue"] = int(round(net_usd * ebay_exchange_rate))
                            if gross_usd > 0:
                                od["fee_rate"] = round(fee_usd / gross_usd * 100, 2)
                            od["notes"] = (
                                f"gross ${gross_usd:.2f} - fee ${fee_usd:.2f} "
                                f"= net ${net_usd:.2f} @ {ebay_exchange_rate:.2f}원/USD "
                                f"(Finance API)"
                            )
                            matched += 1
                    logger.info(
                        f"[주문동기화] {label}: Finance 실제 정산 매칭 "
                        f"{matched}/{len(orders_data)}건"
                    )
                except Exception as e:
                    logger.warning(
                        f"[주문동기화] {label}: Finance API 조회 실패 "
                        f"(예상 수수료 유지) — {e}"
                    )

                # 반품/취소 수집 (최근 90일 고정)
                try:
                    returns_raw = await ebay_client.get_returns(days=90)
                    cancellations_raw = await ebay_client.get_cancellations(days=90)
                    _apply_ebay_claims_to_orders(
                        orders_data, returns_raw, cancellations_raw
                    )
                    logger.info(
                        f"[주문동기화] {label}: eBay 반품 {len(returns_raw)}건 "
                        f"+ 취소 {len(cancellations_raw)}건 매칭 (90일)"
                    )
                except Exception as e:
                    logger.warning(
                        f"[주문동기화] {label}: eBay 반품/취소 조회 실패 — {e}"
                    )
            # (dead code 제거: 두 번째 롯데ON 블록 → 첫 번째에 병합 완료)
            elif market_type == "ssg":
                from backend.domain.samba.proxy.ssg import SSGClient

                _ssg_api_key = extras.get("apiKey", "") or account["api_key"] or ""
                if not _ssg_api_key:
                    settings_repo = SambaSettingsRepository(session)
                    _ssg_setting = await settings_repo.find_by_async(key="store_ssg")
                    if _ssg_setting and isinstance(_ssg_setting.value, dict):
                        _ssg_api_key = _ssg_setting.value.get("apiKey", "") or ""
                if not _ssg_api_key:
                    results.append(
                        {
                            "account": label,
                            "status": "skip",
                            "message": "SSG API Key 없음",
                        }
                    )
                    continue

                _ssg_client = SSGClient(_ssg_api_key)
                _clients_to_close.append(_ssg_client)
                # 정산 API 호출: (ordNo, ordItemSeq) → settIAmt(정산금액), sellFeeRt(수수료율)
                _ssg_settle_map: dict[str, dict] = {}
                try:
                    _ssg_settle_items = await _ssg_client.get_settlement_items(
                        days=body.days
                    )
                    for _si in _ssg_settle_items:
                        _key = f"{_si.get('ordNo', '')}|{_si.get('ordItemSeq', '')}"
                        if _key and _key != "|":
                            _ssg_settle_map[_key] = _si
                    logger.info(
                        f"[주문동기화] {label}: SSG 정산 {len(_ssg_settle_map)}건 조회"
                    )
                except Exception as _ssg_se:
                    logger.warning(
                        f"[주문동기화] {label}: SSG 정산 조회 실패 (무시) — {_ssg_se}"
                    )
                try:
                    _ssg_raw_orders = _raw_cache.get(account["id"])
                    if _ssg_raw_orders is None:
                        _ssg_raw_orders = await _ssg_client.get_orders(days=body.days)
                    logger.info(
                        f"[주문동기화] {label}: SSG 주문 {len(_ssg_raw_orders)}건 조회"
                    )
                    for _ssg_ro in _ssg_raw_orders:
                        _ord = _ssg_client.parse_order(
                            _ssg_ro, account["id"], label, fee_rate=0
                        )
                        # 정산 API 매칭: settIAmt(정산금액), sellFeeRt(판매수수료율)로 revenue/fee_rate 확정
                        _ssg_key = f"{_ssg_ro.get('ordNo', '')}|{_ssg_ro.get('ordItemSeq', '')}"
                        _ssg_si = _ssg_settle_map.get(_ssg_key)
                        if _ssg_si:
                            _settl_amt = float(_ssg_si.get("settIAmt", 0) or 0)
                            _fee_rt = float(_ssg_si.get("sellFeeRt", 0) or 0)
                            if _settl_amt > 0:
                                _ord["revenue"] = _settl_amt
                                _ord["fee_rate"] = _fee_rt
                        orders_data.append(_ord)
                except Exception as _ssg_e:
                    logger.warning(
                        f"[주문동기화] {label}: SSG 주문 조회 실패 — {_ssg_e}"
                    )
                    results.append(
                        {
                            "account": label,
                            "status": "error",
                            "message": f"SSG 주문 조회 실패: {_ssg_e}",
                        }
                    )
                    continue
            elif market_type == "lottehome":
                from backend.domain.samba.proxy.lottehome import LotteHomeClient
                from backend.domain.samba.forbidden.model import SambaSettings
                from sqlmodel import select as _select_lh

                _lh_creds_result = await session.exec(
                    _select_lh(SambaSettings).where(
                        SambaSettings.key == "lottehome_credentials"
                    )
                )
                _lh_creds_row = _lh_creds_result.first()
                lh_creds = _lh_creds_row.value if _lh_creds_row else {}

                lh_user_id = (
                    lh_creds.get("userId", "")
                    or extras.get("userId", "")
                    or account["seller_id"]
                    or ""
                )
                lh_password = (
                    lh_creds.get("password", "") or extras.get("password", "") or ""
                )
                lh_agnc_no = lh_creds.get("agncNo", "") or extras.get("agncNo", "")
                lh_env = lh_creds.get("env", "prod")

                if not lh_user_id or not lh_password:
                    results.append(
                        {
                            "account": label,
                            "status": "skip",
                            "message": "롯데홈쇼핑 인증정보 없음",
                        }
                    )
                    continue

                await session.commit()
                lh_client = LotteHomeClient(lh_user_id, lh_password, lh_agnc_no, lh_env)
                _clients_to_close.append(lh_client)

                from datetime import datetime as _dt, timedelta as _td, UTC as _UTC

                lh_end = _dt.now(_UTC)
                lh_start = lh_end - _td(days=body.days)
                lh_start_str = lh_start.strftime("%Y%m%d")
                lh_end_str = lh_end.strftime("%Y%m%d")

                _lh_seen: set[str] = set()

                def _lh_order_key(ro: dict) -> str:
                    prod = (
                        ro.get("ProdInfo", {})
                        if isinstance(ro.get("ProdInfo"), dict)
                        else {}
                    )
                    return str(
                        ro.get("SubOrdNo")
                        or prod.get("DlvUnitSn")
                        or prod.get("OrdDtlSn")
                        or ro.get("OrdNo", "")
                        or ""
                    )

                _new_ord_status_map = {
                    "01": ("pending", "주문접수"),
                    "02": ("pending", "출하지시"),
                    "03": ("pending", "발송약정"),
                }
                for _lh_sel in ["01", "02", "03"]:
                    _lh_orders = await lh_client.search_new_orders(
                        lh_start_str, lh_end_str, sel_option=_lh_sel
                    )
                    _fs, _fss = _new_ord_status_map[_lh_sel]
                    for ro in _lh_orders:
                        _oid = _lh_order_key(ro)
                        if _oid and _oid not in _lh_seen:
                            _lh_seen.add(_oid)
                            orders_data.append(
                                _parse_lottehome_order(
                                    ro, account["id"], label, _fs, _fss
                                )
                            )

                _dlv_status_map = {
                    "15": ("shipping", "출고지시"),
                    "16": ("shipping", "배송대기중"),
                    "17": ("delivered", "배송완료"),
                    "18": ("confirmed", "구매확정"),
                }
                for _lh_stat in ["15", "16", "17", "18"]:
                    try:
                        _lh_dlv = await lh_client.search_deliver_list(
                            lh_start_str, lh_end_str, ord_dtl_stat_cd=_lh_stat
                        )
                        _fs, _fss = _dlv_status_map[_lh_stat]
                        for ro in _lh_dlv:
                            _oid = _lh_order_key(ro)
                            if _oid and _oid not in _lh_seen:
                                _lh_seen.add(_oid)
                                orders_data.append(
                                    _parse_lottehome_order(
                                        ro, account["id"], label, _fs, _fss
                                    )
                                )
                    except Exception as _dlv_e:
                        logger.warning(
                            f"[주문동기화] {label}: 배송조회(stat={_lh_stat}) 실패: {_dlv_e}"
                        )

                def _lh_override(parsed: dict) -> None:
                    _oid = parsed.get("order_number", "")
                    if not _oid:
                        return
                    orders_data[:] = [
                        o for o in orders_data if o.get("order_number") != _oid
                    ]
                    orders_data.append(parsed)
                    _lh_seen.add(_oid)

                try:
                    _lh_cncl = await lh_client.search_cancel_orders(
                        lh_start_str, lh_end_str
                    )
                    for ro in _lh_cncl:
                        for parsed in _parse_lottehome_order_multi(
                            ro, account["id"], label, "cancelled"
                        ):
                            _lh_override(parsed)
                except Exception as _e:
                    logger.warning(f"[주문동기화] {label}: 취소주문 실패: {_e}")

                for _ret_stat in ["20", "21"]:
                    try:
                        _lh_ret = await lh_client.search_return_orders(
                            lh_start_str, lh_end_str, ord_dtl_stat_cd=_ret_stat
                        )
                        ret_status = (
                            "return_requested"
                            if _ret_stat == "20"
                            else "return_completed"
                        )
                        for ro in _lh_ret:
                            for parsed in _parse_lottehome_order_multi(
                                ro, account["id"], label, ret_status
                            ):
                                _lh_override(parsed)
                    except Exception as _e:
                        logger.warning(
                            f"[주문동기화] {label}: 반품조회(stat={_ret_stat}) 실패: {_e}"
                        )

                logger.info(
                    f"[주문동기화] {label}: 롯데홈쇼핑 주문 {len(orders_data)}건 조회"
                )

            else:
                results.append(
                    {
                        "account": label,
                        "status": "skip",
                        "message": f"{market_type} 주문 조회 미지원",
                    }
                )
                continue

            # 수집상품 매칭 캐시 — 모듈 전역 60초 TTL 캐시 사용 (sync마다 재빌드 X)
            from sqlalchemy import text as _sa_text

            # 외부 마켓 API 호출이 길어 write session이 idle in transaction
            # timeout으로 끊겼을 수 있음. 이후 INSERT/UPDATE 전 rollback으로
            # 죽은 connection을 invalidate하고 풀에서 새 connection을 받는다.
            try:
                await session.rollback()
            except BaseException as _rb_e:
                logger.warning(
                    f"[주문동기화] write session rollback 실패(무시): {_rb_e}"
                )

            _mpn_global, _mpn_by_account = await _get_mpn_cache(session, _sourcing_urls)

            # 미등록 입력 캐시 — 정확 키 매칭만 허용(2026-05-11 보완).
            # 과거 사고: 동일 (product_id, channel_name) 키 헐거움 → 시계 cp 800건 오염.
            # 보완:
            #   - 키: (channel_id, product_id) — 마켓×상품 정확 식별
            #   - playauto: (channel_id, product_id, _pa_site_id) — 1채널 5별칭 분리
            #   - 소스: 수동 입력본(collected_product_id IS NULL + source_url 존재)만
            #     자동매칭으로 채워진 행은 _matched 경로가 이미 처리하므로 캐시 미포함.
            _unreg_cache: dict[str, dict[str, str]] = {}
            try:
                async with get_read_session() as _unreg_sess:
                    _unreg_result = await _unreg_sess.execute(
                        _sa_text(
                            "SELECT channel_id, product_id, source, product_name, source_url, product_image "
                            "FROM samba_order "
                            "WHERE source_url IS NOT NULL AND source_url <> '' "
                            "AND collected_product_id IS NULL "
                            "AND channel_id IS NOT NULL "
                            "AND product_id IS NOT NULL"
                        )
                    )
                    _unreg_rows = _unreg_result.fetchall()
                for _ur in _unreg_rows:
                    _u_ch = str(_ur[0] or "")
                    _u_pid = str(_ur[1] or "")
                    _u_src = str(_ur[2] or "")
                    if not _u_ch or not _u_pid:
                        continue
                    if _u_src == "playauto":
                        # playauto는 _pa_site_id 차원이 필요하지만 DB엔 별도 컬럼 없음.
                        # 별칭 cross-매칭 사고 방지 위해 playauto 수동입력 전파는 보류.
                        continue
                    _ukey_build = f"{_u_ch}|{_u_pid}"
                    _unreg_cache[_ukey_build] = {
                        "source_url": _ur[4],
                        "product_image": _ur[5] or "",
                    }
            except Exception as _unreg_e:
                logger.warning(f"[주문동기화] _unreg_cache 빌드 실패(무시): {_unreg_e}")
                _unreg_cache = {}

            # 비-롯데ON 주문: order_number 배치 조회로 N+1 SELECT 제거
            _non_lotteon_nos = list(
                {
                    str(od.get("order_number", ""))
                    for od in orders_data
                    if od.get("source") != "lotteon" and od.get("order_number")
                }
            )
            _existing_id_map: dict[str, int] = {}
            if _non_lotteon_nos:
                _batch_tid = account["tenant_id"] or tenant_id
                _batch_cid = next(
                    (
                        od.get("channel_id")
                        for od in orders_data
                        if od.get("channel_id")
                    ),
                    None,
                )
                # asyncpg text()에서 list 파라미터 타입 오류 방지 — IN (...)으로 처리
                _ph = ", ".join(f":no_{i}" for i in range(len(_non_lotteon_nos)))
                _bulk_params: dict = {
                    f"no_{i}": v for i, v in enumerate(_non_lotteon_nos)
                }
                _bulk_params["tid"] = _batch_tid
                _bulk_params["cid"] = _batch_cid
                _bulk_q = await session.execute(
                    _sa_text(
                        f"SELECT id, order_number FROM samba_order "
                        f"WHERE order_number IN ({_ph}) "
                        f"AND tenant_id IS NOT DISTINCT FROM :tid "
                        f"AND channel_id IS NOT DISTINCT FROM :cid "
                        f"ORDER BY created_at DESC"
                    ),
                    _bulk_params,
                )
                for _br in _bulk_q.fetchall():
                    if _br[1] not in _existing_id_map:
                        _existing_id_map[_br[1]] = _br[0]
                logger.info(
                    f"[주문동기화] {label}: 배치 중복 조회 완료 "
                    f"{len(_existing_id_map)}/{len(_non_lotteon_nos)}건 기존"
                )

            # 중복 확인 후 저장 (기존 주문은 금액/상태 업데이트)
            synced = 0
            _processed = 0
            _total = len(orders_data)
            for order_data in orders_data:
                _processed += 1
                if _processed % 50 == 0:
                    logger.info(
                        f"[주문동기화] {label}: 주문 처리 중 {_processed}/{_total}건"
                    )
                # tenant_id 주입 (멀티테넌트 격리 — account 우선, JWT fallback)
                _tid = account["tenant_id"] or tenant_id
                if _tid:
                    order_data["tenant_id"] = _tid
                # 수집상품 매칭 — collected_product_id, product_image, source_site, source_url 보충
                # 매칭 우선순위 (오염 방지):
                #   1) (channel_id, product_id) 정확 매칭 (by_account)
                #   2) playauto master_code 글로벌 매칭 (충돌 시 거부)
                #   3) product_id 글로벌 매칭 (충돌 시 거부)
                _pid = str(order_data.get("product_id", ""))
                _pa_mc = str(order_data.get("_pa_master_code") or "")
                _ch_id = str(order_data.get("channel_id") or "")
                _matched = None
                # 1) 정확 매칭 — (channel_id, product_id)
                if _ch_id and _pid:
                    _matched = _mpn_by_account.get(f"{_ch_id}:{_pid}")
                # 2) playauto master_code 글로벌 (master_code는 통상 unique)
                if not _matched and order_data.get("source") == "playauto" and _pa_mc:
                    _cand = _mpn_global.get(_pa_mc)
                    if _cand and not _cand.get("ambiguous"):
                        _matched = _cand
                # 3) product_id 글로벌 — 충돌(ambiguous)이면 거부
                if not _matched and _pid:
                    _cand = _mpn_global.get(_pid)
                    if _cand and not _cand.get("ambiguous"):
                        _matched = _cand
                # 플레이오토 별칭(site_id) 단위 매칭 검증 — 1 channel_id에 5개 별칭이
                # 묶인 구조에서 사용자가 특정 별칭에만 등록한 cp가 다른 별칭 주문에
                # 잘못 매칭되는 것을 차단. cp.market_product_nos에 `{account_id}_sites`
                # 키가 있을 때만 엄격 매칭, 없으면 호환 모드(기존 동작).
                if _matched and order_data.get("source") == "playauto":
                    _order_site_id = str(order_data.get("_pa_site_id") or "").strip()
                    _account_id = str(order_data.get("channel_id") or "")
                    _allowed_sites = _matched.get("site_ids_by_account", {}).get(
                        _account_id
                    )
                    if (
                        _allowed_sites
                        and _order_site_id
                        and _order_site_id not in _allowed_sites
                    ):
                        # 등록된 site_id에 해당 주문의 별칭이 없음 → 매칭 거부
                        _matched = None
                if _matched:
                    if not order_data.get("collected_product_id"):
                        order_data["collected_product_id"] = _matched[
                            "collected_product_id"
                        ]
                    if not order_data.get("product_image"):
                        order_data["product_image"] = _matched["product_image"]
                    if not order_data.get(
                        "source_site"
                    ) and _can_override_source_site_from_sourcing(order_data):
                        order_data["source_site"] = _matched["source_site"]
                    if not order_data.get("source_url") and _matched.get(
                        "original_link"
                    ):
                        order_data["source_url"] = _matched["original_link"]
                # 매칭 검증용 임시 키 제거 (DB 저장 직전, 모델에 없는 필드)
                order_data.pop("_pa_site_id", None)
                order_data.pop("_pa_master_code", None)
                # 롯데ON 예상 정산금액 계산 (롯데ON 공식 정산공식, 2026-04-30 재확인)
                #   pymtAmt = actualAmt − (bseCmsn + pcsCmsn + dvCmsn − ajstDcAmt)
                # raw 응답엔 수수료 필드가 없으므로:
                #   기본수수료 = slAmt × 카테고리 fee_rate
                #   PCS수수료  = slAmt × 2% (가격비교 채널 유입 주문만)
                #   조정(환급) = prSfcoShrAmtSum (당사부담할인)
                # 정산 API(SettleItmdSales) 매칭으로 이미 revenue가 세팅됐으면 확정값이므로 건드리지 않음.
                if order_data.get("source") == "lotteon":
                    _od_no = str(order_data.get("od_no") or "")
                    _od_seq = str(order_data.get("od_seq", "1") or "1")
                    _line_key = (_od_no, _od_seq)
                    _slamt = int(sl_amt_map.get(_line_key, 0))
                    _actual = int(actual_amt_map.get(_line_key, 0))
                    _lotte_dc = int(lotte_dc_map.get(_line_key, 0))
                    _ch_no = ch_no_map.get(_od_no, "")

                    # 가격비교 채널 = PCS 수수료 부과 대상
                    # 운영 데이터로 확장 필요 — 가디 계정 chNo=100065 표본 기준
                    _PRICE_COMPARE_CHANNELS = {"100065"}
                    _pcs_rate = 2.0 if _ch_no in _PRICE_COMPARE_CHANNELS else 0.0

                    if _slamt > 0:
                        # 고객결제금액 우선 actualAmt 사용, 없으면 slAmt − fvrAmtSum 폴백
                        _customer_paid = (
                            _actual
                            if _actual > 0
                            else max(0, _slamt - int(fvr_amt_map.get(_line_key, 0)))
                        )
                        order_data["total_payment_amount"] = _customer_paid

                        if not order_data.get("revenue"):
                            from backend.domain.samba.proxy.lotteon.category_fees import (
                                get_fee_rate_for_category,
                            )

                            _cat_for_fee = (
                                _matched.get("category", "") if _matched else ""
                            )
                            _fee = get_fee_rate_for_category(_cat_for_fee)
                            _bse_cmsn = int(_slamt * _fee / 100)
                            _pcs_cmsn = int(_slamt * _pcs_rate / 100)
                            _net_cmsn = max(0, _bse_cmsn + _pcs_cmsn - _lotte_dc)
                            _revenue = max(0, _customer_paid - _net_cmsn)
                            order_data["revenue"] = _revenue
                            order_data["fee_rate"] = (
                                round(_net_cmsn / _customer_paid * 100, 2)
                                if _customer_paid > 0
                                else 0
                            )
                    elif not order_data.get("revenue"):
                        # raw 매핑 실패 폴백 — 카테고리 수수료 공식
                        from backend.domain.samba.proxy.lotteon.category_fees import (
                            get_fee_rate_for_category,
                        )

                        _cat_for_fee = _matched.get("category", "") if _matched else ""
                        _fee = get_fee_rate_for_category(_cat_for_fee)
                        _sp = int(order_data.get("sale_price", 0) or 0)
                        order_data["total_payment_amount"] = _sp
                        order_data["fee_rate"] = _fee
                        order_data["revenue"] = max(0, int(_sp * (1 - _fee / 100)))
                # 롯데홈쇼핑 정산금액 계산 — account.additional_fields.commission_rate 우선, 폴백 25%
                if order_data.get("source") == "lottehome":
                    _lh_fee = float(
                        (account.get("additional_fields") or {}).get("commission_rate")
                        or 25.0
                    )
                    _lh_total = int(order_data.get("total_payment_amount") or 0)
                    order_data["fee_rate"] = _lh_fee
                    if not order_data.get("revenue") and _lh_total > 0:
                        order_data["revenue"] = max(
                            0, int(_lh_total * (1 - _lh_fee / 100))
                        )
                # 미등록 입력 자동 적용 — 정확 키 매칭만 허용(2026-05-11 보완).
                # 과거 (product_id, channel_name) 키는 헐거워서 시계 cp 800건 오염 사고 발생.
                # 보완: (channel_id, product_id) 정확 매칭 + playauto는 site_id 추가.
                # _matched(수집상품 자동매칭)가 이미 채운 경우 그쪽 우선이므로 건드리지 않음.
                if not _matched and _ch_id and _pid:
                    if order_data.get("source") == "playauto":
                        _pa_sid = str(order_data.get("_pa_site_id") or "")
                        _ukey = f"{_ch_id}|{_pid}|{_pa_sid}"
                    else:
                        _ukey = f"{_ch_id}|{_pid}"
                    _unreg_matched = _unreg_cache.get(_ukey)
                    if _unreg_matched:
                        if not order_data.get("source_url"):
                            order_data["source_url"] = _unreg_matched["source_url"]
                        if (
                            not order_data.get("product_image")
                            and _unreg_matched["product_image"]
                        ):
                            order_data["product_image"] = _unreg_matched[
                                "product_image"
                            ]
                # 상품명에서 소싱처 상품번호 추출 → source_site/source_url 보충
                # 플레이오토는 1 channel에 5 별칭이 묶인 구조라 product_name 끝 공통 무신사
                # goods_no가 별칭 무관하게 cross-매칭됨 (예: 캐논 주문이 고경 등록 cp에 매칭).
                # → 플레이오토 주문은 본 분기 비활성화. master_code 직접 매칭만 신뢰.
                if (
                    not order_data.get("source_url")
                    and order_data.get("source") != "playauto"
                ):
                    import re as _re

                    _pname = order_data.get("product_name", "")
                    _id_match = _re.search(r"\b(\d{6,})\s*$", _pname)
                    if _id_match:
                        _sid = _id_match.group(1)
                        # 1차-A: site_product_id 정확 매칭
                        _cp_check = await session.execute(
                            _sa_text(
                                "SELECT id, source_site, images, site_product_id "
                                "FROM samba_collected_product "
                                "WHERE site_product_id = :sid "
                                "ORDER BY (market_product_nos IS NOT NULL) DESC, created_at ASC "
                                "LIMIT 1"
                            ),
                            {"sid": _sid},
                        )
                        _cp_row = _cp_check.fetchone()
                        # 1차-B: prefix 매칭 (예: SSG itemId '1000807183548'은 _sid '1000807183'로
                        # 시작 — 정확 매칭 실패 시 prefix로 시도하되, 단 1건일 때만 신뢰)
                        if not _cp_row:
                            _cp_pref = await session.execute(
                                _sa_text(
                                    "SELECT id, source_site, images, site_product_id "
                                    "FROM samba_collected_product "
                                    "WHERE site_product_id LIKE :pfx "
                                    "ORDER BY (market_product_nos IS NOT NULL) DESC, created_at ASC "
                                    "LIMIT 2"
                                ),
                                {"pfx": _sid + "%"},
                            )
                            _cp_pref_rows = _cp_pref.fetchall()
                            if len(_cp_pref_rows) == 1:
                                _cp_row = _cp_pref_rows[0]
                        if _cp_row:
                            _matched_spid = _cp_row[3] or _sid
                            if not order_data.get("collected_product_id"):
                                order_data["collected_product_id"] = _cp_row[0]
                            if _can_override_source_site_from_sourcing(order_data):
                                order_data["source_site"] = _cp_row[1]
                            order_data["source_url"] = _sourcing_urls.get(
                                _cp_row[1], ""
                            ).format(_matched_spid)
                            if (
                                not order_data.get("product_image")
                                and _cp_row[2]
                                and isinstance(_cp_row[2], list)
                            ):
                                order_data["product_image"] = _cp_row[2][0]
                        # 매칭 실패 시 무신사 단정하지 않음 — source_site/url 오염 방지
                        # (과거 자릿수만으로 MUSINSA로 추론하던 fallback 제거: 2026-05-10)
                # 중복 체크: 롯데ON은 od_no+od_seq 기반, 기타는 order_number 기반
                # proc_seq는 주문 상태 변경 시 바뀌므로 중복 체크에서 제외
                _normalize_synced_order_status(order_data)
                if order_data.get("source") == "lotteon" and order_data.get("od_no"):
                    _lo_row = await session.execute(
                        _sa_text(
                            "SELECT id FROM samba_order "
                            "WHERE source = 'lotteon' "
                            "AND tenant_id IS NOT DISTINCT FROM :tid "
                            "AND channel_id = :cid "
                            "AND od_no = :od_no "
                            "AND od_seq = :od_seq "
                            "LIMIT 1"
                        ),
                        {
                            "tid": order_data.get("tenant_id"),
                            "cid": order_data.get("channel_id"),
                            "od_no": order_data["od_no"],
                            "od_seq": order_data.get("od_seq", "1"),
                        },
                    )
                    _lo_id = (_lo_row.fetchone() or [None])[0]
                    existing = await svc.repo.get_async(_lo_id) if _lo_id else None
                else:
                    _existing_id = _existing_id_map.get(
                        str(order_data.get("order_number", ""))
                    )
                    existing = (
                        await svc.repo.get_async(_existing_id) if _existing_id else None
                    )
                if (
                    not existing
                    and order_data.get("shipment_id")
                    and order_data.get("product_id")
                ):
                    # 같은 orderId + 상품번호로 이미 있는 주문 검색
                    _dup_candidates = await svc.repo.filter_by_async(
                        shipment_id=order_data["shipment_id"], limit=10
                    )
                    existing = next(
                        (
                            d
                            for d in _dup_candidates
                            if d.product_id == order_data["product_id"]
                            and (d.product_option or "")
                            == (order_data.get("product_option") or "")
                        ),
                        None,
                    )
                    if existing:
                        # order_number 갱신 (발주확인 후 변경된 productOrderId)
                        await svc.repo.update_async(
                            existing.id, order_number=order_data["order_number"]
                        )
                if existing:
                    # 기존 주문: sale_price, 이미지, 상태, 마켓주문상태 업데이트
                    update_fields: dict[str, Any] = {}
                    # tenant_id 보충 (기존 NULL 데이터 대응)
                    if order_data.get("tenant_id") and not existing.tenant_id:
                        update_fields["tenant_id"] = order_data["tenant_id"]
                    if (
                        order_data.get("sale_price")
                        and order_data["sale_price"] != existing.sale_price
                    ):
                        update_fields["sale_price"] = order_data["sale_price"]
                    # 고객결제금액 갱신: 변경됐거나 기존 NULL이면 채움
                    new_total_paid = order_data.get("total_payment_amount")
                    if new_total_paid is not None:
                        existing_total = (
                            existing.total_payment_amount
                            if existing.total_payment_amount is not None
                            else None
                        )
                        if existing_total is None or float(new_total_paid) != float(
                            existing_total
                        ):
                            update_fields["total_payment_amount"] = float(
                                new_total_paid
                            )
                    if order_data.get("product_image") and not existing.product_image:
                        update_fields["product_image"] = order_data["product_image"]
                    # 상품명/옵션명이 빈 경우 새 데이터로 복구
                    if order_data.get("product_name") and not existing.product_name:
                        update_fields["product_name"] = order_data["product_name"]
                    if order_data.get("product_option") and not existing.product_option:
                        update_fields["product_option"] = order_data["product_option"]
                    new_source_site = str(order_data.get("source_site") or "").strip()
                    existing_source_site = str(existing.source_site or "").strip()
                    if new_source_site and not existing_source_site:
                        update_fields["source_site"] = new_source_site
                    elif (
                        order_data.get("source") == "playauto"
                        and new_source_site
                        and new_source_site != existing_source_site
                        and "(" in new_source_site
                    ):
                        update_fields["source_site"] = new_source_site
                    if order_data.get("source_url") and not existing.source_url:
                        update_fields["source_url"] = order_data["source_url"]
                    # collected_product_id 백필 — 과거 매칭 캐시 LIMIT 컷오프로 끊긴
                    # 기존 주문이 다음 sync 때 자동 재연결되도록.
                    if (
                        order_data.get("collected_product_id")
                        and not existing.collected_product_id
                    ):
                        update_fields["collected_product_id"] = order_data[
                            "collected_product_id"
                        ]
                    if order_data.get("customer_note") and order_data[
                        "customer_note"
                    ] != str(existing.customer_note or ""):
                        update_fields["customer_note"] = order_data["customer_note"]
                    if order_data.get("shipment_id") and order_data[
                        "shipment_id"
                    ] != str(existing.shipment_id or ""):
                        update_fields["shipment_id"] = order_data["shipment_id"]
                    if order_data.get("ord_prd_seq") and not existing.ord_prd_seq:
                        update_fields["ord_prd_seq"] = order_data["ord_prd_seq"]
                    # 결제일 갱신: 기존이 NULL이거나 더 이른 값일 때만 채택
                    # (고객 결제시각은 변하지 않음 — 더 늦은 값은 마켓이 sync/처리시각을 결제칸으로 돌려준 케이스로 간주하고 무시)
                    # tz-aware/naive 혼재 방지: 비교 직전 양쪽을 UTC tz-aware로 normalize
                    new_paid = order_data.get("paid_at")
                    if new_paid:
                        if existing.paid_at is None:
                            update_fields["paid_at"] = new_paid
                        else:
                            from datetime import timezone as _tz

                            _np = (
                                new_paid.replace(tzinfo=_tz.utc)
                                if new_paid.tzinfo is None
                                else new_paid
                            )
                            _ep = (
                                existing.paid_at.replace(tzinfo=_tz.utc)
                                if existing.paid_at.tzinfo is None
                                else existing.paid_at
                            )
                            if _np < _ep:
                                update_fields["paid_at"] = new_paid
                    # 수령인 정보 갱신 — 선물하기 주문 등에서 보내는 사람으로 잘못 저장된
                    # customer_name/phone을 다시 가져오기로 수령인 기준으로 교정.
                    # 마켓 응답에 값이 있고 기존과 다르면 덮어쓴다.
                    new_cust_name = order_data.get("customer_name")
                    if new_cust_name and new_cust_name != str(
                        existing.customer_name or ""
                    ):
                        update_fields["customer_name"] = new_cust_name
                    new_orderer_name = order_data.get("orderer_name")
                    if new_orderer_name and new_orderer_name != str(
                        existing.orderer_name or ""
                    ):
                        update_fields["orderer_name"] = new_orderer_name
                    new_cust_phone = order_data.get("customer_phone")
                    if new_cust_phone and new_cust_phone != str(
                        existing.customer_phone or ""
                    ):
                        update_fields["customer_phone"] = new_cust_phone
                    new_cust_addr = order_data.get("customer_address")
                    if new_cust_addr and new_cust_addr != str(
                        existing.customer_address or ""
                    ):
                        update_fields["customer_address"] = new_cust_addr
                    new_cust_addr_dtl = order_data.get("customer_address_detail")
                    if new_cust_addr_dtl is not None and new_cust_addr_dtl != str(
                        existing.customer_address_detail or ""
                    ):
                        update_fields["customer_address_detail"] = new_cust_addr_dtl
                    # 우편번호 — UPDATE path 에서도 채움 (신규 INSERT 만 채워지던 버그 fix)
                    new_postal = order_data.get("customer_postal_code")
                    if new_postal and new_postal != (
                        existing.customer_postal_code or ""
                    ):
                        update_fields["customer_postal_code"] = new_postal
                    # 마켓 상품번호 보충 (기존 주문에 없으면 채움)
                    if order_data.get("product_id") and not existing.product_id:
                        update_fields["product_id"] = order_data["product_id"]
                    # 송장전송완료/배송중 이상 상태는 덮어쓰지 않음
                    # 단, 롯데ON은 발송완료/배송중/배송완료로 진행된 경우 갱신 허용
                    new_ship_status = order_data.get("shipping_status")
                    if new_ship_status:
                        cancel_statuses = {"취소요청", "취소처리중", "취소완료"}
                        exchange_statuses = {
                            "교환요청",
                            "교환회수완료",
                            "교환재배송",
                            "교환완료",
                        }
                        advanced = {"발송완료", "국내배송중", "배송완료", "구매확정"}
                        if new_ship_status in cancel_statuses:
                            # 취소 상태는 항상 갱신 (송장전송완료 → 취소요청 등 역행 허용)
                            # 단, 이미 반품 진행 중인 주문은 취소로 되돌리지 않음
                            if existing.shipping_status in (
                                "반품요청",
                                "반품완료",
                                "반품거부",
                            ):
                                logger.info(
                                    f"[주문동기화] 반품 상태 보호: {order_data.get('order_number')} "
                                    f"{existing.shipping_status} → {new_ship_status} 차단"
                                )
                            else:
                                update_fields["shipping_status"] = new_ship_status
                        elif new_ship_status in exchange_statuses:
                            # 교환 상태는 항상 갱신 (배송완료 → 교환요청 등 역행 허용)
                            # 단, 이미 반품 상태인 주문은 교환으로 되돌리지 않음
                            if existing.shipping_status in (
                                "반품요청",
                                "반품완료",
                                "반품거부",
                            ):
                                logger.info(
                                    f"[주문동기화] 반품 상태 보호: {order_data.get('order_number')} "
                                    f"{existing.shipping_status} → {new_ship_status} 차단"
                                )
                            else:
                                update_fields["shipping_status"] = new_ship_status
                        elif (
                            existing.shipping_status == "송장전송완료"
                            and new_ship_status in advanced
                        ):
                            update_fields["shipping_status"] = new_ship_status
                        elif (
                            new_ship_status in ("반품요청", "반품완료", "반품거부")
                            and existing.shipping_status in exchange_statuses
                        ):
                            # 반품 상태는 교환 상태를 덮어씀 (교환→반품 재접수 케이스)
                            update_fields["shipping_status"] = new_ship_status
                            logger.info(
                                f"[주문동기화] 교환→반품 상태 전환: {order_data.get('order_number')} "
                                f"{existing.shipping_status} → {new_ship_status}"
                            )
                        elif existing.shipping_status not in (
                            "송장전송완료",
                            "국내배송중",
                            "배송완료",
                            "교환재배송",
                            "교환요청",
                            "교환회수완료",
                            "교환완료",
                            "교환거부",
                            "반품요청",
                            "반품완료",
                            "반품거부",
                            "회수확정",
                            "취소완료",
                        ):
                            update_fields["shipping_status"] = new_ship_status
                    # shipping_status 가 "국내배송중"으로 진입 시 status 드롭다운도 함께 동기화.
                    # 라벨/드롭다운이 어긋난 채 wait_ship 으로 남아 페이지 필터를 통과해 노출되던 사고 방지.
                    _new_ss_final = update_fields.get(
                        "shipping_status", existing.shipping_status
                    )
                    if _new_ss_final == "국내배송중" and existing.status in (
                        "pending",
                        "preparing",
                        "wait_ship",
                        "arrived",
                        "processing",
                        "shipped",
                    ):
                        update_fields["status"] = "shipping"
                    # 정산금액(revenue) / 수수료율 갱신
                    new_revenue = order_data.get("revenue")
                    new_fee_rate = order_data.get("fee_rate")
                    sp = float(
                        update_fields.get("sale_price", existing.sale_price) or 0
                    )
                    if new_revenue and float(new_revenue) != float(
                        existing.revenue or 0
                    ):
                        rev = float(new_revenue)
                        update_fields["revenue"] = rev
                        update_fields["fee_rate"] = (
                            new_fee_rate
                            if new_fee_rate is not None
                            else (existing.fee_rate or 0)
                        )
                        cost = float(existing.cost or 0)
                        ship_fee = float(existing.shipping_fee or 0)
                        update_fields["profit"] = rev - cost - ship_fee
                        update_fields["profit_rate"] = (
                            f"{((rev - cost - ship_fee) / rev * 100):.2f}"
                            if rev > 0
                            else "0.00"
                        )
                    elif "sale_price" in update_fields:
                        fr = float(
                            new_fee_rate
                            if new_fee_rate is not None
                            else (existing.fee_rate or 0)
                        )
                        rev = sp * (1 - fr / 100)
                        cost = float(existing.cost or 0)
                        ship_fee = float(existing.shipping_fee or 0)
                        update_fields["revenue"] = rev
                        update_fields["profit"] = rev - cost - ship_fee
                        update_fields["profit_rate"] = (
                            f"{((rev - cost - ship_fee) / rev * 100):.2f}"
                            if rev > 0
                            else "0.00"
                        )
                    if update_fields:
                        await svc.update_order(existing.id, update_fields)
                    continue
                await svc.create_order(order_data)
                synced += 1

            total_synced += synced
            if market_type == "smartstore":
                confirmed_count = len(unconfirmed_ids)
            elif market_type == "lotteon":
                confirmed_count = lotteon_confirmed_count
            elif market_type == "11st":
                confirmed_count = _confirmed if _confirm_targets else 0
            else:
                confirmed_count = 0

            # ── 클레임(취소/반품/교환) → SambaReturn 자동 생성 ──────────────
            returns_synced = 0
            claim_statuses = {
                "취소요청",
                "취소처리중",
                "취소완료",
                "반품요청",
                "반품완료",
                "반품거부",
                "교환요청",
                "교환회수완료",
                "교환재배송",
                "교환완료",
            }
            claim_orders = [
                od for od in orders_data if od.get("shipping_status") in claim_statuses
            ]
            if claim_orders:
                from backend.domain.samba.returns.service import SambaReturnService
                from backend.domain.samba.returns.repository import (
                    SambaReturnRepository,
                )
                from backend.domain.samba.returns.model import SambaReturn
                from sqlmodel import select as _sel

                return_svc = SambaReturnService(SambaReturnRepository(session))

                claim_type_map = {
                    "취소요청": "cancel",
                    "취소처리중": "cancel",
                    "취소완료": "cancel",
                    "반품요청": "return",
                    "반품완료": "return",
                    "반품거부": "return",
                    "교환요청": "exchange",
                    "교환회수완료": "exchange",
                    "교환재배송": "exchange",
                    "교환완료": "exchange",
                }
                claim_return_status_map = {
                    "취소완료": "completed",
                    "반품완료": "completed",
                    "교환완료": "completed",
                    "반품거부": "rejected",
                }
                claim_completion_detail_map = {
                    "취소완료": "취소",
                    "반품완료": "반품",
                    "교환완료": "교환",
                    "반품거부": "거부",
                }
                for od in claim_orders:
                    order_no = od.get("order_number", "")
                    if not order_no:
                        continue
                    shipping_status = od.get("shipping_status", "")
                    ret_type = claim_type_map.get(shipping_status, "return")
                    return_status = claim_return_status_map.get(shipping_status)
                    completion_detail = claim_completion_detail_map.get(shipping_status)
                    # 중복 체크
                    existing_ret_result = await session.execute(
                        _sel(SambaReturn).where(SambaReturn.order_number == order_no)
                    )
                    existing_ret = existing_ret_result.scalars().first()
                    if existing_ret:
                        update_fields: dict[str, Any] = {
                            "type": ret_type,
                            "market_order_status": shipping_status,
                        }
                        if return_status:
                            update_fields["status"] = return_status
                        if completion_detail:
                            update_fields["completion_detail"] = completion_detail
                        if return_status in ("completed", "rejected"):
                            from datetime import UTC, datetime as _dt

                            update_fields["completion_date"] = _dt.now(UTC)
                        await return_svc.repo.update_async(
                            existing_ret.id, **update_fields
                        )
                        continue
                    # 연결 주문 조회
                    linked_order = await svc.repo.find_by_async(order_number=order_no)
                    if not linked_order:
                        continue
                    ret = await return_svc.create_return(
                        {
                            "order_id": linked_order.id,
                            "order_number": order_no,
                            "type": ret_type,
                            "market": label,
                            "market_order_status": shipping_status,
                            "product_name": od.get("product_name", ""),
                            "product_image": od.get("product_image", ""),
                            "customer_name": od.get("customer_name", ""),
                            "customer_phone": od.get("customer_phone", ""),
                            "customer_address": od.get("customer_address", ""),
                            "requested_amount": od.get("sale_price", 0),
                        }
                    )
                    if return_status or completion_detail:
                        update_fields: dict[str, Any] = {}
                        if return_status:
                            update_fields["status"] = return_status
                        if completion_detail:
                            update_fields["completion_detail"] = completion_detail
                        if return_status in ("completed", "rejected"):
                            from datetime import UTC, datetime as _dt

                            update_fields["completion_date"] = _dt.now(UTC)
                        await return_svc.repo.update_async(ret.id, **update_fields)
                    returns_synced += 1
                logger.info(
                    f"[주문동기화] {label}: 클레임 {len(claim_orders)}건 중 {returns_synced}건 반품교환 생성"
                )

            cancel_requested = sum(
                1 for od in orders_data if od.get("shipping_status") == "취소요청"
            )
            results.append(
                {
                    "account": label,
                    "status": "success",
                    "fetched": len(orders_data),
                    "synced": synced,
                    "confirmed": confirmed_count,
                    "cancel_requested": cancel_requested,
                    "returns_synced": returns_synced,
                }
            )
            logger.info(
                f"[주문동기화] {label}: {len(orders_data)}건 조회, {synced}건 저장, {confirmed_count}건 발주확인"
            )

            # ── paid_at 백필 — 스마트스토어 NULL paid_at 주문 직접 재조회 ──
            if market_type == "smartstore":
                try:
                    _null_rows = await session.execute(
                        _sa_text(
                            "SELECT order_number FROM samba_order "
                            "WHERE paid_at IS NULL AND source = 'smartstore' "
                            "AND channel_id = :cid LIMIT 100"
                        ),
                        {"cid": account["id"]},
                    )
                    _null_po_ids = [r[0] for r in _null_rows.fetchall()]
                    if _null_po_ids:
                        _details = await client.get_product_orders_by_ids(_null_po_ids)
                        _backfilled = 0
                        for _d in _details:
                            _po = _d.get("productOrder", _d)
                            _oi = _d.get("order", {})
                            _paid = _parse_iso_datetime(
                                _oi.get("paymentDate") or _po.get("paymentDate")
                            )
                            if _paid:
                                _poid = _po.get("productOrderId", "")
                                await session.execute(
                                    _sa_text(
                                        "UPDATE samba_order SET paid_at = :paid "
                                        "WHERE order_number = :on AND paid_at IS NULL"
                                    ),
                                    {"paid": _paid, "on": _poid},
                                )
                                _backfilled += 1
                        if _backfilled:
                            await session.commit()
                            logger.info(
                                f"[주문동기화] {label}: paid_at 백필 {_backfilled}건"
                            )
                except Exception as _bf_err:
                    logger.warning(
                        f"[주문동기화] {label}: paid_at 백필 실패 — {_bf_err}"
                    )

            # ── paid_at 백필 — 플레이오토 NULL paid_at 주문 → 동기화 데이터에서 매칭 ──
            elif market_type == "playauto":
                try:
                    # 현재 동기화에서 paid_at이 유효한 주문의 order_number → paid_at 매핑
                    _pa_paid_map: dict[str, datetime] = {}
                    for od in orders_data:
                        if od.get("paid_at") and od.get("order_number"):
                            _pa_paid_map[od["order_number"]] = od["paid_at"]
                    if _pa_paid_map:
                        _null_rows = await session.execute(
                            _sa_text(
                                "SELECT order_number FROM samba_order "
                                "WHERE paid_at IS NULL AND source = 'playauto' "
                                "AND channel_id = :cid LIMIT 200"
                            ),
                            {"cid": account["id"]},
                        )
                        _null_ons = [r[0] for r in _null_rows.fetchall()]
                        _backfilled = 0
                        for _on in _null_ons:
                            _paid = _pa_paid_map.get(_on)
                            if _paid:
                                await session.execute(
                                    _sa_text(
                                        "UPDATE samba_order SET paid_at = :paid "
                                        "WHERE order_number = :on AND paid_at IS NULL"
                                    ),
                                    {"paid": _paid, "on": _on},
                                )
                                _backfilled += 1
                        if _backfilled:
                            await session.commit()
                            logger.info(
                                f"[주문동기화] {label}: 플레이오토 paid_at 백필 {_backfilled}건"
                            )
                except Exception as _bf_err:
                    logger.warning(
                        f"[주문동기화] {label}: 플레이오토 paid_at 백필 실패 — {_bf_err}"
                    )

            # ── paid_at 백필 — 롯데ON NULL paid_at 주문 → 동기화 데이터에서 매칭 ──
            # order_number = "{od_no}_{od_seq}_{proc_seq}" 합성키 기반 (order.py:3406)
            elif market_type == "lotteon":
                try:
                    _lo_paid_map: dict[str, datetime] = {}
                    for od in orders_data:
                        if od.get("paid_at") and od.get("order_number"):
                            _lo_paid_map[od["order_number"]] = od["paid_at"]
                    if _lo_paid_map:
                        _null_rows = await session.execute(
                            _sa_text(
                                "SELECT order_number FROM samba_order "
                                "WHERE paid_at IS NULL AND source = 'lotteon' "
                                "AND channel_id = :cid LIMIT 200"
                            ),
                            {"cid": account["id"]},
                        )
                        _null_ons = [r[0] for r in _null_rows.fetchall()]
                        _backfilled = 0
                        for _on in _null_ons:
                            _paid = _lo_paid_map.get(_on)
                            if _paid:
                                await session.execute(
                                    _sa_text(
                                        "UPDATE samba_order SET paid_at = :paid "
                                        "WHERE order_number = :on AND paid_at IS NULL"
                                    ),
                                    {"paid": _paid, "on": _on},
                                )
                                _backfilled += 1
                        if _backfilled:
                            await session.commit()
                            logger.info(
                                f"[주문동기화] {label}: 롯데ON paid_at 백필 {_backfilled}건"
                            )
                except Exception as _bf_err:
                    logger.warning(
                        f"[주문동기화] {label}: 롯데ON paid_at 백필 실패 — {_bf_err}"
                    )

        except Exception as e:
            await session.rollback()  # 세션 복구 — 다음 계정 연쇄 실패 방지
            logger.error(f"[주문동기화] {label} 실패: {e}")
            results.append({"account": label, "status": "error", "message": str(e)})
        finally:
            # 마켓 클라이언트 httpx keepalive 좀비 정리 — 다음 계정 hang 도미노 차단.
            # CancelledError(상위 wait_for timeout) 시에도 이 finally 가 먼저 실행되므로
            # connection pool 즉시 회수됨.
            for _c in _clients_to_close:
                try:
                    _aclose = getattr(_c, "aclose", None)
                    if _aclose is not None:
                        await _aclose()
                except Exception as _ce:
                    logger.warning(
                        f"[주문동기화] {label} 클라이언트 aclose 실패(무시): {_ce}"
                    )

    # DB 기반 원주문 shipping_status 일괄 동기화
    # samba_return 레코드가 있고 진행 중인 주문의 shipping_status를 강제 업데이트
    try:
        from sqlalchemy import text as _sa_text_upd

        await session.execute(
            _sa_text_upd(
                """
            UPDATE samba_order o
            SET shipping_status = CASE
                WHEN r.type = 'exchange' THEN '교환요청'
                WHEN r.type = 'return' THEN '반품요청'
                WHEN r.type = 'cancel' THEN '취소요청'
                ELSE o.shipping_status
            END
            FROM samba_return r
            WHERE r.order_id = o.id
              AND r.status NOT IN ('completed', 'cancelled', 'rejected')
              AND o.shipping_status NOT IN (
                  '교환요청', '교환회수완료', '교환재배송', '교환완료',
                  '반품요청', '반품완료', '반품거부',
                  '취소완료'
              )
        """
            )
        )
        await session.commit()
        logger.info(
            "[주문동기화] 반품/교환/취소 진행 중 원주문 shipping_status 일괄 업데이트 완료"
        )
    except Exception as _upd_err:
        logger.warning(f"[주문동기화] 원주문 일괄 업데이트 실패: {_upd_err}")

    if total_synced > 0:
        from backend.utils.kakao_notify import send_kakao_message

        synced_lines = [
            f"  {r['account']}: {r.get('synced', 0)}건"
            for r in results
            if r.get("synced", 0) > 0
        ]
        msg = f"🛒 주문 {total_synced}건 동기화 완료"
        if synced_lines:
            msg += "\n" + "\n".join(synced_lines)
        asyncio.create_task(send_kakao_message(msg))

    return {"total_synced": total_synced, "results": results}


def _parse_iso_datetime(val: str | None) -> datetime | None:
    """ISO 8601 문자열 → datetime 변환. 실패 시 None."""
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _parse_smartstore_order(
    po: dict,
    order_info: dict,
    account_id: str,
    account_label: str,
    claim_info: dict | None = None,
) -> dict[str, Any]:
    """스마트스토어 productOrder + order → SambaOrder 데이터 변환."""
    status_map = {
        "PAYED": "pending",
        "DELIVERING": "shipped",
        "DELIVERED": "delivered",
        "PURCHASE_DECIDED": "delivered",
        "EXCHANGED": "delivered",
        "CANCELED": "cancelled",
        "RETURNED": "returned",
        "CANCEL_REQUESTED": "pending",
    }
    naver_status = po.get("productOrderStatus", "")
    place_status = po.get("placeOrderStatus", "")
    sale_price = po.get("totalPaymentAmount", 0) or po.get("unitPrice", 0) or 0
    quantity = po.get("quantity", 1) or 1

    # 클레임 상태 (취소/반품/교환 요청)
    # 우선순위: 호출자가 전달한 claim 서브 객체 → productOrder 최상위 순으로 fallback
    _ci = claim_info or {}
    claim_type = _ci.get("claimType") or po.get("claimType", "") or ""
    claim_status = _ci.get("claimStatus") or po.get("claimStatus", "") or ""

    claim_status_map = {
        "CANCEL_REQUEST": "취소요청",
        "CANCELING": "취소처리중",
        "CANCEL_DONE": "취소완료",
        "CANCEL_REJECT": "취소거부",
        "RETURN_REQUEST": "반품요청",
        "COLLECTING": "수거중",
        "COLLECT_DONE": "수거완료",
        "RETURN_DONE": "반품완료",
        "RETURN_REJECT": "반품거부",
        "EXCHANGE_REQUEST": "교환요청",
        "EXCHANGING": "교환처리중",
        "EXCHANGE_DONE": "교환완료",
        "EXCHANGE_REJECT": "교환거부",
    }

    # 정산금액: API에서 직접 가져오기
    expected_settlement = po.get("expectedSettlementAmount")
    if expected_settlement and sale_price > 0:
        fee_rate = round((1 - expected_settlement / sale_price) * 100, 2)
    else:
        expected_settlement = None
        fee_rate = 0

    # 마켓 주문상태 한글 변환
    market_status_map: dict[str, str] = {
        "PAYED": "결제완료",
        "DELIVERING": "국내배송중",
        "DELIVERED": "배송완료",
        "PURCHASE_DECIDED": "구매확정",
        "EXCHANGED": "교환완료",
        "CANCELED": "취소완료",
        "RETURNED": "반품완료",
        "CANCEL_REQUESTED": "취소요청",
        "RETURN_REQUESTED": "반품요청",
        "EXCHANGE_REQUESTED": "교환요청",
    }
    # 클레임이 있으면 클레임 상태 우선
    if claim_status and claim_status in claim_status_map:
        market_order_status = claim_status_map[claim_status]
    elif place_status == "NOT_YET" and naver_status == "PAYED":
        market_order_status = "발주미확인"
    elif naver_status == "PAYED":
        market_order_status = "발송대기"
    else:
        market_order_status = market_status_map.get(naver_status, naver_status)

    # 배송지 정보
    shipping = po.get("shippingAddress", {})
    # 우편번호 후보 키 모두 비어있으면 1회 INFO 로그 (실제 응답 키 진단용)
    if shipping and not (
        shipping.get("zipCode")
        or shipping.get("zipcode")
        or shipping.get("postCode")
        or shipping.get("zipNo")
    ):
        logger.info(
            f"[스마트스토어][zip진단] po={po.get('productOrderId')} "
            f"keys={list(shipping.keys())}"
        )
    # 수령인(배송지) 우선 — 선물하기 주문은 주문자(보내는 사람) ≠ 수령인(받는 사람)이므로
    # CS/배송 단위에서 의미있는 customer는 수령인. 일반 주문은 둘이 동일하므로 영향 없음.
    customer_name = shipping.get("name", "") or order_info.get("ordererName", "")
    customer_tel = (
        shipping.get("tel1", "")
        or shipping.get("tel2", "")
        or order_info.get("ordererTel", "")
    )

    # 마켓 상품번호 (구매페이지 URL 생성용 + 수집상품 매칭 키)
    # 우선순위: channelProductNo > originalProductId > productId
    # - 다른 정상 케이스는 channelProductNo가 있어 그대로 동작
    # - 선물하기/위탁판매 옵션 상품은 channelProductNo 누락 + productId가 옵션별로 별도 발급되어
    #   수집상품 매칭 실패 사고가 있었음(2026-05-12 이종영 주문). 등록은 originalProductId로
    #   되어있는 경우가 많아 fallback 키로 활용.
    channel_product_no = str(
        po.get("channelProductNo", "")
        or po.get("originalProductId", "")
        or po.get("productId", "")
        or ""
    )

    return {
        "order_number": po.get("productOrderId", ""),
        "shipment_id": order_info.get("orderId", ""),
        "channel_id": account_id,
        "channel_name": account_label,
        "product_id": channel_product_no,
        "product_name": po.get("productName", ""),
        "product_option": po.get("productOption", "") or "",
        "product_image": po.get("imageUrl", ""),
        "customer_name": customer_name,
        "orderer_name": order_info.get("ordererName", "") or "",
        "customer_phone": customer_tel,
        "customer_address": (shipping.get("baseAddress", "") or "").strip(),
        "customer_address_detail": (shipping.get("detailedAddress", "") or "").strip(),
        # 우편번호 — 화면 확인용 (복사 버튼 분리). 네이버 응답 케이스 변형 흡수 fallback chain
        "customer_postal_code": (
            str(
                shipping.get("zipCode")
                or shipping.get("zipcode")
                or shipping.get("postCode")
                or shipping.get("zipNo")
                or ""
            ).strip()
            or None
        ),
        "customer_note": po.get("shippingMemo", "") or "",
        "quantity": quantity,
        "sale_price": sale_price,
        "cost": 0,
        "fee_rate": fee_rate,
        "revenue": expected_settlement if expected_settlement else sale_price,
        # 내부 status도 클레임 반영
        "status": (
            "cancel_requested"
            if claim_status in ("CANCEL_REQUEST", "CANCELING")
            else (
                "cancelled"
                if claim_status == "CANCEL_DONE"
                else (
                    "return_requested"
                    if claim_status in ("RETURN_REQUEST", "COLLECTING", "COLLECT_DONE")
                    else (
                        "returned"
                        if claim_status == "RETURN_DONE"
                        else status_map.get(naver_status, "pending")
                    )
                )
            )
        ),
        "shipping_status": market_order_status,
        "shipping_company": po.get("deliveryCompany", ""),
        "tracking_number": po.get("trackingNumber", ""),
        "paid_at": _parse_iso_datetime(
            order_info.get("paymentDate") or po.get("paymentDate")
        ),
        "source": "smartstore",
    }


def _coupang_paid_to_utc(val: str | None) -> datetime | None:
    """쿠팡 paidAt(KST naive ISO) → UTC tz-aware datetime.

    쿠팡 ordersheet 응답의 paidAt/orderedAt은 timezone 정보 없는 KST 문자열이라
    그대로 사용하면 SambaOrder.paid_at(DateTime(timezone=True))과 비교 시
    'can't compare offset-naive and offset-aware datetimes' 에러 발생.
    naive 면 KST 부여, aware 면 그대로 UTC astimezone.
    """
    from datetime import timezone
    from zoneinfo import ZoneInfo

    dt = _parse_iso_datetime(val)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    return dt.astimezone(timezone.utc)


def _parse_coupang_order(
    order: dict,
    account_id: str,
    account_label: str,
) -> dict[str, Any]:
    """쿠팡 ordersheet 1건 → SambaOrder 데이터 변환."""
    status_map = {
        "ACCEPT": "pending",
        "INSTRUCT": "pending",
        "DEPARTURE": "shipped",
        "DELIVERING": "shipped",
        "FINAL_DELIVERY": "delivered",
        "CANCEL": "cancelled",
    }
    market_status_map = {
        "ACCEPT": "결제완료",
        "INSTRUCT": "상품준비중",
        "DEPARTURE": "국내배송중",
        "DELIVERING": "국내배송중",
        "FINAL_DELIVERY": "배송완료",
        "CANCEL": "취소완료",
    }

    coupang_status = (order.get("status") or "").upper()
    shipment_box_id = order.get("shipmentBoxId") or 0
    order_id = order.get("orderId") or 0

    # 클레임 (취소/반품 요청) 우선
    cancel_requests = order.get("cancelRequests") or []
    return_requests = order.get("returnRequests") or []
    if cancel_requests:
        market_order_status = "취소요청"
        internal_status = "cancel_requested"
    elif return_requests:
        market_order_status = "반품요청"
        internal_status = "return_requested"
    else:
        market_order_status = market_status_map.get(coupang_status, coupang_status)
        internal_status = status_map.get(coupang_status, "pending")

    order_items = order.get("orderItems") or []
    first_item = order_items[0] if order_items else {}
    product_name = first_item.get("sellerProductName", "") or ""
    # 쿠팡 옵션 없음 placeholder 패턴 (대소문자/공백/구두점 변형 허용)
    _NO_OPTION_PATTERNS = ("옵션없음", "no option")

    option_name = (
        first_item.get("sellerProductItemName", "")
        or first_item.get("firstSellerProductItemName", "")
        or ""
    ).strip()

    # placeholder 텍스트 정규화 (예: "옵션없음. 옵션없음." → "FREE")
    _normalized = option_name.lower().replace(" ", "").replace(".", "")
    if not option_name or any(
        p.replace(" ", "") in _normalized for p in _NO_OPTION_PATTERNS
    ):
        option_name = "FREE"
    sales_price = int(first_item.get("salesPrice", 0) or 0)
    quantity = int(first_item.get("orderQuantity", 1) or 1)
    shipping_price = int(order.get("shippingPrice", 0) or 0)
    sale_price = sales_price + shipping_price

    # 쿠팡 정률 수수료 10.5% + VAT 10% = 실효 11.55%
    fee_rate = 11.55
    revenue = round(sale_price * (1 - fee_rate / 100))

    # 쿠팡 ordersheet 응답은 receiver/orderer를 nested object로 내려줌.
    # 과거 flat key (receiverAddr1 등) 사용 코드가 빈값을 만들었음.
    receiver = order.get("receiver") or {}
    orderer = order.get("orderer") or {}

    receiver_addr = (
        receiver.get("addr1")
        or order.get("receiverAddr1", "")
        or order.get("receiverAddress", "")
        or ""
    )
    receiver_addr_detail = (
        receiver.get("addr2")
        or order.get("receiverAddr2", "")
        or order.get("receiverAddrDetail", "")
        or ""
    )
    customer_address = receiver_addr.strip()
    customer_address_detail = receiver_addr_detail.strip()
    # 우편번호 — 화면 확인용 (복사 버튼 분리)
    customer_postal_code = (
        str(receiver.get("postCode") or order.get("receiverPostCode") or "").strip()
        or None
    )

    orderer_name = (
        orderer.get("name")
        or receiver.get("name")
        or order.get("ordererName", "")
        or order.get("receiverName", "")
        or ""
    )
    orderer_tel = (
        orderer.get("safeNumber")
        or orderer.get("ordererNumber")
        or receiver.get("safeNumber")
        or receiver.get("receiverNumber")
        or order.get("ordererPhoneNumber", "")
        or order.get("orderPhoneNumber", "")
        or order.get("receiverPhoneNumber", "")
        or ""
    )

    if not orderer_name and not customer_address:
        logger.warning(
            f"[쿠팡][주문파싱] customer 빈값 — keys={list(order.keys())[:25]} "
            f"receiver_keys={list(receiver.keys()) if isinstance(receiver, dict) else 'NA'} "
            f"orderer_keys={list(orderer.keys()) if isinstance(orderer, dict) else 'NA'}"
        )

    # shipmentBoxId 우선 (배송단위 안정 ID), orderId fallback
    order_number = str(shipment_box_id or order_id or "")

    return {
        "order_number": order_number,
        "shipment_id": str(order_id) if order_id else "",
        "channel_id": account_id,
        "channel_name": account_label,
        "product_id": str(
            first_item.get("productId", "")
            or first_item.get("sellerProductId", "")
            or ""
        ),
        "product_name": product_name,
        "coupang_display_name": first_item.get("vendorItemPackageName", "") or "",
        "product_option": option_name,
        "product_image": "",
        "customer_name": orderer_name,
        "customer_phone": orderer_tel,
        "customer_address": customer_address,
        "customer_address_detail": customer_address_detail,
        "customer_postal_code": customer_postal_code,
        "customer_note": (
            order.get("parcelPrintMessage", "")
            or order.get("shippingMessage", "")
            or ""
        ),
        "quantity": quantity,
        "sale_price": sale_price,
        "cost": 0,
        "fee_rate": fee_rate,
        "revenue": revenue,
        "status": internal_status,
        "shipping_status": market_order_status,
        "shipping_company": order.get("deliveryCompanyName", "") or "",
        "tracking_number": order.get("invoiceNumber", "") or "",
        "paid_at": _coupang_paid_to_utc(order.get("paidAt") or order.get("orderedAt")),
        "source": "coupang",
    }


def _parse_lotteon_order(item: dict, account_id: str, label: str) -> dict:
    """롯데ON 주문 데이터 → SambaOrder dict 변환."""

    # 주문 진행 단계 코드 → 내부 status/shipping_status 매핑
    step_cd = str(item.get("odPrgsStepCd", "") or "")
    status_map = {
        "10": "pending",  # 발주확인대기
        "11": "preparing",  # 발주확인완료(출고지시) — sync에서 자동 ifCplYN=Y 호출되어 12로 전이
        "12": "preparing",  # 상품준비
        "13": "shipping",  # 발송완료
        "14": "delivered",  # 배송완료
        "20": "pending",  # 발주확인
        "21": "return_requested",  # 교환회수중
        "22": "return_requested",  # 교환회수완료
        "23": "return_requested",  # 교환회수완료확인
        "24": "shipping",  # 교환재배송
        "25": "delivered",  # 교환배송완료
        "30": "shipping",  # 배송중
        "40": "delivered",  # 배송완료
        "50": "confirmed",  # 구매확정
        "90": "cancelled",  # 취소
    }
    shipping_map = {
        "10": "발주확인대기",
        "11": "출고지시",
        "12": "상품준비",
        "13": "발송완료",
        "14": "배송완료",
        "20": "출고지시",
        "21": "교환요청",
        "22": "교환회수완료",
        "23": "교환회수완료",
        "24": "교환재배송",
        "25": "교환완료",
        "30": "국내배송중",
        "40": "배송완료",
        "50": "구매확정",
        "90": "취소완료",
    }
    status = status_map.get(step_cd, "pending")
    shipping_status = shipping_map.get(step_cd, "출고지시")

    # 롯데ON 반품 사유코드(200/300번대)인데 교환 stepCd(21~25)로 들어온 경우
    # → 실제로는 반품이므로 반품 상태로 재매핑
    clm_rsn_cd = str(item.get("clmRsnCd", "") or "")
    if clm_rsn_cd.startswith(("2", "3")) and step_cd in ("21", "22", "23", "24", "25"):
        status = "return_requested"
        shipping_status = "반품요청"
        logger.info(
            f"[롯데ON][주문파싱] 반품 사유코드({clm_rsn_cd}) 교환 stepCd({step_cd}) "
            f"→ 반품요청으로 재매핑: odNo={item.get('odNo')}"
        )

    # 결제일시 파싱 — 롯데ON 응답 실측 키는 odCmptDttm (yyyymmddHHmmss, KST)
    # 참고: owhoDttm(발주확인, ISO 포맷)은 결제 이후 시각이라 결제시각 폴백으로 부적합
    from backend.utils import kst_str_to_utc

    order_dttm_str = item.get("odCmptDttm") or ""
    paid_at = kst_str_to_utc(order_dttm_str)
    if not paid_at:
        logger.warning(
            f"[롯데ON][주문파싱] 결제일시 키 없음 odNo={item.get('odNo')} "
            f"odCmptDttm={item.get('odCmptDttm')!r} "
            f"키후보={[k for k in item.keys() if 'tt' in k.lower() or 'dt' in k.lower()]}"
        )

    # 배송지 주소 분리 저장 (dvpStnmZipAddr=도로명기본주소, dvpStnmDtlAddr=상세주소)
    addr_base = (item.get("dvpStnmZipAddr") or "").strip()
    addr_detail = (item.get("dvpStnmDtlAddr") or "").strip()
    # 우편번호 — 화면 확인용 (복사 버튼 분리). 롯데ON 응답 키 변형 흡수 fallback chain
    postal_code = (
        str(
            item.get("dvpZpcd")
            or item.get("dvpZipNo")
            or item.get("dvpStnmZpcd")
            or item.get("dvpJbngZpcd")
            or item.get("zipNo")
            or ""
        ).strip()
        or None
    )
    # 모든 후보 비어있으면 1회 키 후보 로그 (실제 응답 키 진단용)
    if not postal_code:
        _zip_keys = [k for k in item.keys() if "zp" in k.lower() or "zip" in k.lower()]
        if _zip_keys:
            logger.info(f"[롯데ON][zip진단] od={item.get('odNo')} zip_keys={_zip_keys}")

    _od_no = str(item.get("odNo", "") or "")
    _od_seq = str(item.get("odSeq", "1") or "1")
    _proc_seq = str(item.get("procSeq", "1") or "1")
    _sitm_no = str(item.get("sitmNo", "") or "")

    return {
        "channel_id": account_id,
        "channel_name": label,
        "source": "lotteon",
        # 합성 키: (odNo, odSeq) — procSeq는 처리 단계에 따라 변하므로 제외
        "order_number": f"{_od_no}_{_od_seq}" if _od_no else "",
        "od_no": _od_no,
        "od_seq": _od_seq,
        "proc_seq": _proc_seq,
        "sitm_no": _sitm_no,
        "shipment_id": _sitm_no,
        "product_id": str(item.get("spdNo", "") or ""),
        "product_name": item.get("spdNm", "") or "",
        "product_option": item.get("sitmNm", "") or "",
        # 롯데ON 주문 응답 수량 필드는 slQty(판매 수량) — odQty는 존재하지 않아 폴백값 1로 박혔던 버그
        "quantity": int(item.get("slQty") or item.get("odQty") or 1),
        "sale_price": int(item.get("slAmt", 0) or item.get("slPrc", 0) or 0),
        "cost": 0,
        "status": status,
        "shipping_status": shipping_status,
        "customer_name": item.get("dvpCustNm", "") or "",
        "orderer_name": item.get("odrNm", "") or "",
        "customer_phone": item.get("dvpMphnNo", "")
        or item.get("dvpTelNo", "")
        or item.get("mphnNo", "")
        or "",
        "customer_address": addr_base,
        "customer_address_detail": addr_detail,
        "customer_postal_code": postal_code,
        "customer_note": item.get("dvMsg", "") or "",
        "paid_at": paid_at,
        # created_at은 명시 X — DB default_factory(now)가 실제 삽입 시각 기록
    }


def _normalize_playauto_alias_code(value: Any) -> str:
    return normalize_playauto_alias_code(value)


def _normalize_synced_order_status(order_data: dict[str, Any]) -> None:
    """Market sync must only drive shipping_status; status stays user-managed."""
    order_data["status"] = "pending"


def _can_override_source_site_from_sourcing(order_data: dict[str, Any]) -> bool:
    """매칭된 collected_product 의 source_site 로 order.source_site 를 덮어써도 되는지.

    과거: PlayAuto 주문은 source_site 에 별칭("GS이숍(캐논)" 등)을 넣어서 매칭으로 덮어쓰면 안 됐음.
    현재(sales_channel_alias 분리 후): PlayAuto 도 source_site="" 로 임포트되므로 비어 있으면 채워야 정상.
    별칭은 이제 sales_channel_alias 컬럼에 별도 보관됨.
    """
    raw = str(order_data.get("source_site") or "").strip()
    # 비어 있으면 항상 채움. 이미 값이 있으면 (소싱처 코드든 별칭이든) 보존.
    return not raw


def _normalize_carrier_name(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    normalized = re.sub(r"[\s()\-_/]", "", raw)
    normalized = normalized.replace("주식회사", "").replace("(주)", "")
    return normalized


def _playauto_carrier_candidates(value: Any) -> list[str]:
    normalized = _normalize_carrier_name(value)
    if not normalized:
        return []
    variants = {normalized}
    alias_map = {
        "CJ대한통운": ["대한통운", "CJ택배", "씨제이대한통운", "CJGLS"],
        "대한통운": ["CJ대한통운", "CJ택배", "씨제이대한통운", "CJGLS"],
        "한진택배": ["한진", "HANJIN"],
        "롯데택배": ["롯데", "현대택배"],
        "로젠택배": ["로젠"],
        "우체국택배": ["우체국", "우체국소포"],
        "경동택배": ["경동"],
        "대신택배": ["대신"],
        "일양로지스": ["일양택배", "ILYANG"],
        "편의점택배": ["CU편의점택배", "GS25편의점택배", "CVSNET"],
    }
    for alias in alias_map.get(str(value or "").strip(), []):
        variants.add(_normalize_carrier_name(alias))
    return [v for v in variants if v]


def _parse_playauto_order(
    ro: dict,
    account_id: str,
    account_label: str,
    alias_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """플레이오토 EMP 주문 → SambaOrder 데이터 변환."""

    # spec 진단용 — SiteId(별칭)별 첫 1건씩 raw 로깅. MasterCode/MyCateName 등 키별 값 확인.
    _logged_sites = getattr(_parse_playauto_order, "_logged_sites", set())
    _site_raw = str(ro.get("SiteId", "")).strip()
    if _site_raw and _site_raw not in _logged_sites:
        try:
            import json as _json

            sample = {
                k: str(ro.get(k, ""))[:80]
                for k in (
                    "SiteId",
                    "SiteName",
                    "ProdCode",
                    "MasterCode",
                    "MyCateName",
                    "SellerCode",
                    "Groupkey",
                    "ProdName",
                    "OrderCode",
                    "Number",
                )
            }
            logger.info(
                f"[플레이오토 raw site={_site_raw}] {_json.dumps(sample, ensure_ascii=False)}"
            )
            _logged_sites.add(_site_raw)
            _parse_playauto_order._logged_sites = _logged_sites  # type: ignore[attr-defined]
        except Exception:
            pass

    # MasterCode 추출 (응답에 있으면 매칭에 활용 — Phase 4)
    master_code = (
        ro.get("MasterCode") or ro.get("master_code") or ro.get("masterCode") or ""
    )

    status_map = {
        "신규주문": "pending",
        "송장출력": "wait_ship",
        "송장입력": "processing",
        # shipping_status 가 "국내배송중"일 때 status 드롭다운도 "국내배송중"(shipping)으로 보이도록 동기화.
        # 과거에 "shipped"로 매핑되어 프론트 STATUS_MAP 에 없는 enum 으로 저장되던 버그도 같이 닫힘.
        "출고": "shipping",
        "배송중": "shipping",
        "국내배송중": "shipping",
        "수취확인": "delivered",
        "정산완료": "delivered",
        "주문확인": "pending",
        "취소": "cancelled",
        "취소마감": "cancelled",
        "반품요청": "return_requested",
        "반품마감": "returned",
        "교환요청": "exchange_requested",
        "교환마감": "exchanged",
        "보류": "pending",
    }

    # shipping_status 매핑 (스킬 가이드 기준)
    shipping_status_map = {
        "신규주문": "주문접수",
        "송장출력": "배송대기중",
        "송장입력": "송장전송완료",
        "출고": "국내배송중",
        "배송중": "국내배송중",
        "주문확인": "취소중",
        "취소마감": "취소완료",
        "수취확인": "배송완료",
        "정산완료": "배송완료",
    }

    order_state = ro.get("OrderState", "")
    sale_price = int(ro.get("Price", 0) or 0)
    quantity = int(ro.get("Count", 1) or 1)

    site_name = str(ro.get("SiteName", "") or "").strip()
    site_id = _normalize_playauto_alias_code(ro.get("SiteId", ""))
    supply_price = int(ro.get("SupplyPrice", 0) or 0)

    # 결제일 파싱 — 플레이오토는 KST 기준
    from backend.utils import kst_str_to_utc

    order_date_raw = ro.get("OrderDate", "") or ""
    paid_at = kst_str_to_utc(order_date_raw)

    # 주소 분리 — 플레이오토는 RecipientAddress 한 필드에 도로명+상세를 통째로 내려줌
    # (openapi.json 확인: 별도 상세주소 필드 없음). 휴리스틱으로 기본/상세 분리.
    # 우선순위 (프론트 splitCustomerAddress 와 동일 — 괄호 안 콤마로 잘리지 않도록):
    #  패턴A: 끝 메타괄호 `(법정동/건물명)` + 그 앞 `동/호/층/호실` 패턴
    #         → base = 도로주소 + 메타괄호, detail = 동/호 토큰
    #  패턴B: 마지막 `)` 뒤에 내용이 있으면 그 지점으로 split (괄호 안 콤마 무시)
    #         (예) "...압구정로 403(압구정동, 한양아파트) 81동 1207호"
    #             → base="...압구정로 403(압구정동, 한양아파트)", detail="81동 1207호"
    #  패턴C: 괄호가 없으면 ", " 명시 구분 ("디지털로26길 123, 14층 플레이오토")
    #  패턴D: 도로명(...대로/로/길) + 본번 뒤 공백 기준 분리
    import re as _re_addr

    _addr_full = str(ro.get("RecipientAddress", "") or "").strip()
    _addr_base = _addr_full
    _addr_detail = ""
    if _addr_full:
        _matched = False
        # 패턴A: 끝 메타괄호 + 동/호 패턴 (전체가 `(...)$` 로 끝나는 경우)
        _meta_m = _re_addr.match(r"^(.*?)\s*(\([^)]*\))\s*$", _addr_full)
        if _meta_m:
            _before_meta = _meta_m.group(1).strip()
            _meta = _meta_m.group(2)
            # 옵션 prefix: 건물명(숫자로 시작하지 않는 토큰). 본번 "218"·"1462-14"가
            # detail로 빨려들지 않도록 첫 글자에 숫자 금지.
            _dongho_m = _re_addr.match(
                r"^(.+?)\s+((?:[^\d\s]\S*\s+)?(?:\d+\s*동\s+)?\d+\s*(?:호|층|호실))$",
                _before_meta,
            )
            if _dongho_m:
                _addr_base = f"{_dongho_m.group(1).strip()} {_meta}".strip()
                _addr_detail = _dongho_m.group(2).strip()
                _matched = True
        # 패턴B: 마지막 `)` 기준 분리 — `, ` 보다 우선.
        # 괄호 안 콤마("(압구정동, 한양아파트)")로 base/detail 가 잘못 잘리지 않도록.
        if not _matched:
            _last_paren = _addr_full.rfind(")")
            if 0 < _last_paren < len(_addr_full) - 1:
                _after = _addr_full[_last_paren + 1 :].strip()
                if _after:
                    _addr_base = _addr_full[: _last_paren + 1].strip()
                    _addr_detail = _after
                    _matched = True
        if not _matched:
            # 패턴C: 괄호 없는 도로명주소 — ", " 단순 분리
            if "(" not in _addr_full and ", " in _addr_full:
                _b, _, _d = _addr_full.partition(", ")
                _addr_base, _addr_detail = _b.strip(), _d.strip()
            else:
                # 패턴D: 도로명 + 본번 뒤 공백 기준
                _m = _re_addr.match(
                    r"^(.+?(?:대로|로|길)\s+\d+(?:-\d+)?)\s+(.+)$", _addr_full
                )
                if _m:
                    _addr_base = _m.group(1).strip()
                    _addr_detail = _m.group(2).strip()

    return {
        "order_number": ro.get("OrderCode", ""),
        "shipment_id": str(ro.get("Number", "")),
        "channel_id": account_id,
        "channel_name": account_label,
        "product_id": ro.get("ProdCode", ""),
        "product_name": ro.get("ProdName", ""),
        "product_option": ro.get("Option", ""),
        "product_image": "",
        "customer_name": ro.get("RecipientName", "") or ro.get("OrderName", ""),
        "customer_phone": ro.get("RecipientHtel", "")
        or ro.get("RecipientTel", "")
        or ro.get("OrderHtel", "")
        or ro.get("OrderTel", ""),
        "customer_address": _addr_base,
        "customer_address_detail": _addr_detail,
        # 우편번호 — 화면 확인용 (복사 버튼 분리). 플레이오토 EMP는 RecipientZipCode 필드 사용.
        "customer_postal_code": str(ro.get("RecipientZipCode") or "").strip() or None,
        "quantity": quantity,
        "sale_price": sale_price,
        "cost": 0,
        "fee_rate": 0,
        "revenue": supply_price if supply_price else sale_price,
        "status": status_map.get(order_state, "pending"),
        "shipping_status": shipping_status_map.get(order_state, order_state),
        "shipping_company": ro.get("Sender", ""),
        "tracking_number": ro.get("SenderNo", ""),
        "paid_at": paid_at,
        "source": "playauto",
        # 별칭 단위 매칭 검증용 — DB 저장 전 pop. site_id가 cp의 등록된 site_ids에
        # 포함될 때만 매칭 허용 (기존 cp는 site_ids 미저장이라 호환 매칭).
        "_pa_site_id": site_id,
        # 매칭용 임시 키 — DB 저장 전 pop. plapro 응답에 MasterCode 있으면 추출해
        # _mpn_cache 매칭에 ProdCode와 함께 시도. 매칭 우선순위: master_code > product_id.
        "_pa_master_code": master_code,
        # 판매처(사업자) 별칭 — PlayAuto 1 채널 × 다 site_id 구조 (예: "GS이숍(캐논)").
        # source_site 와 분리 — source_site 는 진짜 소싱처 코드 전용.
        "sales_channel_alias": (
            f"{site_name}({alias_map[site_id]})"
            if alias_map and site_id in alias_map and site_name
            else f"{site_name}({site_id})"
            if site_name
            else ""
        ),
        # source_site 는 collected_product 매칭 후 자동 채워짐 — 임포트 시점엔 빈 값.
        "source_site": "",
    }


def _parse_elevenst_order(item: dict, account_id: str, label: str) -> dict:
    """11번가 주문 데이터를 SambaOrder dict로 변환."""
    from datetime import datetime, timedelta, timezone

    KST = timezone(timedelta(hours=9))

    def _to_int(value, default: int = 0) -> int:
        """콤마, None, 빈 문자열 안전하게 int 변환."""
        try:
            if value in (None, ""):
                return default
            return int(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return default

    # ordPrdStat 상태 코드 맵핑
    stat_code = str(item.get("ordPrdStat", "") or "")
    status_map = {
        "200": "pending",  # 결제완료
        "202": "pending",  # 처리중 (배송완료 이전 단계)
        "301": "wait_ship",  # 발주확인(배송대기)
        "400": "shipping",  # 출고완료
        "500": "shipping",  # 배송중
        "600": "delivered",  # 배송완료
        "700": "confirmed",  # 구매확정
        "900": "cancelled",  # 취소완료
        "1000": "returned",  # 반품완료
    }
    shipping_map = {
        "200": "결제완료",
        "202": "결제완료",  # 11번가 내부 처리중 상태 (결제완료와 동일 단계)
        "301": "배송대기중",  # 발주확인 완료
        "400": "출고완료",
        "500": "국내배송중",
        "600": "배송완료",
        "700": "구매확정",
        "900": "취소완료",
        "1000": "반품완료",
    }
    status = status_map.get(stat_code, "pending")
    shipping_status = shipping_map.get(stat_code, "처리중" if stat_code else "결제완료")

    # 주문일 파싱 (API 응답: "YYYY-MM-DD HH:MM:SS" 또는 "YYYYMMDDhhmm", KST)
    ord_dt = str(item.get("ordDt", "") or "").strip()
    try:
        if "-" in ord_dt:
            paid_at = (
                datetime.strptime(ord_dt, "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=KST)
                .astimezone(timezone.utc)
            )
        else:
            paid_at = (
                datetime.strptime(ord_dt[:12], "%Y%m%d%H%M")
                .replace(tzinfo=KST)
                .astimezone(timezone.utc)
            )
    except Exception:
        paid_at = datetime.now(timezone.utc)

    # 수령인 주소 분리 저장 (실제 API 필드: rcvrBaseAddr=기본, rcvrDtlsAddr=상세)
    addr_base = str(item.get("rcvrBaseAddr", "") or "").strip()
    addr_detail = str(item.get("rcvrDtlsAddr", "") or "").strip()
    # 우편번호 — 화면 확인용 (복사 버튼 분리). 11번가 API 우편번호 필드: rcvrMlmtNo
    postal_code = str(item.get("rcvrMlmtNo") or "").strip() or None

    # 판매금액: selPrc(단가) 우선, 없으면 ordAmt(주문금액)
    sale_price = _to_int(item.get("selPrc"), _to_int(item.get("ordAmt")))

    # 정산예정금액: stlPlnAmt
    revenue = _to_int(item.get("stlPlnAmt"), sale_price)

    # 수수료율 = (1 - 정산예정금액 / 판매가) × 100
    # 음수/이상값 방지: revenue가 sale_price보다 크면 0으로 처리
    if sale_price > 0 and 0 < revenue <= sale_price:
        fee_rate = round((1 - revenue / sale_price) * 100, 2)
    else:
        fee_rate = 0.0

    return {
        "channel_id": account_id,
        "channel_name": label,
        "source": "11st",
        "order_number": str(item.get("ordNo", "") or ""),
        "ord_prd_seq": str(item.get("ordPrdSeq", "") or ""),
        "shipment_id": str(item.get("dlvNo", "") or ""),
        "product_id": str(item.get("prdNo", "") or ""),
        "product_name": str(item.get("prdNm", "") or ""),
        "product_option": str(item.get("slctPrdOptNm", "") or ""),
        "quantity": max(1, _to_int(item.get("ordQty"), 1)),
        "sale_price": sale_price,
        "cost": 0,
        "revenue": revenue,
        "fee_rate": fee_rate,
        "status": status,
        "shipping_status": shipping_status,
        "customer_name": str(item.get("rcvrNm", "") or item.get("ordNm", "") or ""),
        # 주문자명 — 11번가 API ordNm (수령인 rcvrNm과 다를 수 있음: 선물하기 등)
        "orderer_name": str(item.get("ordNm", "") or item.get("rcvrNm", "") or ""),
        "customer_phone": str(
            item.get("rcvrPrtblNo", "") or item.get("ordPrtblTel", "") or ""
        ),
        "customer_address": addr_base,
        "customer_address_detail": addr_detail,
        "customer_postal_code": postal_code,
        "customer_note": str(
            item.get("ordDlvReqCont", "") or item.get("dlvMsg", "") or ""
        ),
        "paid_at": paid_at,
        "created_at": paid_at,
    }


def _parse_ebay_datetime(val) -> Optional[datetime]:
    """eBay 날짜 필드는 문자열 또는 {"value": "..."} dict 형태."""
    if val is None:
        return None
    if isinstance(val, dict):
        val = val.get("value", "")
    return _parse_iso_datetime(val if isinstance(val, str) else None)


def _parse_ebay_order(
    o: dict,
    account_id: str,
    account_label: str,
    exchange_rate: float = 1400.0,
) -> dict[str, Any]:
    """eBay Fulfillment API 주문 dict → SambaOrder 필드 매핑.

    eBay는 USD 결제이므로 ``exchange_rate``(USD→KRW)로 변환해 KRW로 저장한다.
    다른 마켓(스마트스토어/롯데ON)과 통일된 KRW 체계 유지.
    """
    order_id = o.get("orderId", "") or ""
    legacy_id = o.get("legacyOrderId", "") or order_id

    line_items = o.get("lineItems") or []
    first_item: dict[str, Any] = line_items[0] if line_items else {}

    # 배송지
    ship_to: dict[str, Any] = {}
    for inst in o.get("fulfillmentStartInstructions") or []:
        step = inst.get("shippingStep") or {}
        ship_to = step.get("shipTo") or {}
        if ship_to:
            break
    contact = ship_to.get("contactAddress") or {}
    # 우편번호 — 화면 확인용으로 별도 컬럼에 저장 (복사 버튼 분리)
    ebay_postal_code = str(contact.get("postalCode", "") or "").strip() or None
    addr_parts = [
        contact.get("addressLine1", ""),
        contact.get("addressLine2", ""),
        contact.get("city", ""),
        contact.get("stateOrProvince", ""),
        contact.get("countryCode", ""),
    ]
    customer_address = ", ".join([p for p in addr_parts if p])

    # 가격 (USD → KRW 변환)
    pricing = o.get("pricingSummary") or {}
    total = pricing.get("total") or {}
    sale_price_usd = float(total.get("value", 0) or 0)
    sale_price_krw = int(round(sale_price_usd * exchange_rate))

    # 수수료 (eBay 마켓플레이스 수수료, USD → KRW 변환)
    marketplace_fee_usd = float(
        (o.get("totalMarketplaceFee") or {}).get("value", 0) or 0
    )
    marketplace_fee_krw = int(round(marketplace_fee_usd * exchange_rate))
    try:
        fee_rate = (
            round(marketplace_fee_usd / sale_price_usd * 100, 2)
            if sale_price_usd > 0
            else 0
        )
    except Exception:
        fee_rate = 0
    revenue = sale_price_krw - marketplace_fee_krw

    # 상태 매핑
    ff_status = o.get("orderFulfillmentStatus", "") or ""
    cancel_state = (o.get("cancelStatus") or {}).get(
        "cancelState", "NONE_REQUESTED"
    ) or "NONE_REQUESTED"
    if cancel_state != "NONE_REQUESTED":
        status = "cancel_requested"
        shipping_status = "취소요청"
    elif ff_status == "FULFILLED":
        status = "pending"
        shipping_status = "국내배송중"
    elif ff_status == "IN_PROGRESS":
        status = "pending"
        shipping_status = "발송대기"
    else:
        status = "pending"
        shipping_status = "발주확인"

    buyer_username = (o.get("buyer") or {}).get("username", "") or ""

    return {
        "order_number": legacy_id,
        "ext_order_number": order_id,
        "shipment_id": first_item.get("sku", ""),
        "channel_id": account_id,
        "channel_name": account_label,
        "product_id": first_item.get("legacyItemId", "") or first_item.get("sku", ""),
        "product_name": first_item.get("title", ""),
        "product_option": first_item.get("legacyVariationId", "") or "",
        "product_image": "",
        "customer_name": ship_to.get("fullName", "") or buyer_username,
        "customer_phone": (ship_to.get("primaryPhone") or {}).get("phoneNumber", "")
        or "",
        "customer_address": customer_address,
        "customer_postal_code": ebay_postal_code,
        "quantity": int(first_item.get("quantity", 1) or 1),
        "sale_price": sale_price_krw,
        "cost": 0,
        "fee_rate": fee_rate,
        "revenue": revenue,
        "status": status,
        "shipping_status": shipping_status,
        "shipping_company": "",
        "tracking_number": "",
        "paid_at": _parse_ebay_datetime(o.get("creationDate")),
        "source": "ebay",
        "notes": f"USD {sale_price_usd:.2f} @ {exchange_rate:.2f}원/USD",
    }


def _apply_ebay_claims_to_orders(
    orders_data: list[dict[str, Any]],
    returns_raw: list[dict[str, Any]],
    cancellations_raw: list[dict[str, Any]],
) -> None:
    """eBay 반품/취소 데이터로 orders_data의 shipping_status 덮어쓰기.

    return.state / cancellation.cancelState 를 기준으로 상태 매핑.
    orders_data에 없는 주문이면 추가하지 않음 (sync 범위 내 주문만 반영).
    """
    # 반품
    return_state_map = {
        "OPEN": "반품요청",
        "ESCALATED": "반품요청",
        "CLOSED": "반품완료",
    }
    for r in returns_raw or []:
        order_id = (
            r.get("orderId")
            or (r.get("itemInfo") or {}).get("orderId")
            or (r.get("creationInfo") or {}).get("orderId")
            or ""
        )
        state = (r.get("status") or {}).get("state", "") or ""
        ss = return_state_map.get(state, "반품요청")
        for od in orders_data:
            if od.get("ext_order_number") == order_id or od.get("order_number") == str(
                order_id
            ):
                od["shipping_status"] = ss
                od["status"] = "returned" if ss == "반품완료" else "return_requested"
                break

    # 취소
    cancel_state_map = {
        "IN_PROGRESS": "취소요청",
        "CANCEL_PENDING": "취소요청",
        "CANCEL_CLOSED": "취소완료",
        "CANCEL_CLOSED_FOR_COMMITMENT": "취소요청",
    }
    for c in cancellations_raw or []:
        legacy_order_id = c.get("legacyOrderId", "") or ""
        state = c.get("cancelState", "") or ""
        ss = cancel_state_map.get(state, "취소요청")
        for od in orders_data:
            if od.get("order_number") == legacy_order_id:
                od["shipping_status"] = ss
                od["status"] = "cancelled" if ss == "취소완료" else "cancel_requested"
                break


def _parse_lottehome_order_multi(
    item: dict, account_id: str, label: str, force_status: str = ""
) -> list[dict]:
    """취소/반품처럼 ProdInfo가 리스트인 롯데홈쇼핑 주문 → 상품별 SambaOrder dict 리스트 반환."""
    _shipping_status_map = {
        "cancelled": "취소완료",
        "return_requested": "반품요청",
        "return_completed": "회수확정",
    }
    prod_info_raw = item.get("ProdInfo", [])
    if isinstance(prod_info_raw, dict):
        prod_info_raw = [prod_info_raw]
    if not prod_info_raw:
        prod_info_raw = [{}]
    results = []
    for prod in prod_info_raw:
        flat = dict(item)
        flat["ProdInfo"] = prod
        parsed = _parse_lottehome_order(flat, account_id, label)
        if force_status:
            parsed["status"] = force_status
            parsed["shipping_status"] = _shipping_status_map.get(
                force_status, force_status
            )
        results.append(parsed)
    return results


def _parse_lottehome_order(
    item: dict,
    account_id: str,
    label: str,
    force_status: str = "",
    force_shipping_status: str = "",
) -> dict:
    """롯데홈쇼핑 주문 데이터 → SambaOrder dict 변환."""
    from datetime import datetime, timezone

    def _lh_str(*vals) -> str:
        for v in vals:
            s = str(v or "").strip()
            if s and s.lower() not in ("null", "none", "0"):
                return s
        return ""

    prod_info = (
        item.get("ProdInfo", {}) if isinstance(item.get("ProdInfo"), dict) else {}
    )
    delv_info = (
        item.get("DelvInfo", {}) if isinstance(item.get("DelvInfo"), dict) else {}
    )

    order_no = str(item.get("OrdNo", "") or "")
    sub_ord_no = str(item.get("SubOrdNo") or "")
    # SubOrdNo = 상품주문번호, OrdNo = 주문번호
    # 반품API는 SubOrdNo가 없고 OrdNo에 상품주문번호가 들어옴 → 폴백으로 일치
    order_number = sub_ord_no or order_no

    # 송장전송(registDeliver.lotte)에 ord_no + ord_dtl_sn 둘 다 필수.
    # ext_order_number 에 "ord_no:ord_dtl_sn" 형식으로 합쳐 저장한다.
    ord_dtl_sn = str(prod_info.get("OrdDtlSn") or prod_info.get("DlvUnitSn") or "")
    ext_order_number = (
        f"{order_no}:{ord_dtl_sn}" if (order_no and ord_dtl_sn) else order_no
    )

    proc_stat = str(item.get("OrdProcStat", "") or "")
    is_deliver_api = bool(prod_info.get("DlvUnitSn") or prod_info.get("GoodsNo"))
    status_map = {
        "업체지시": "pending",
        "정상": "pending",
        "출고확정": "shipping",
        "배송완료": "delivered",
        "구매확정": "confirmed",
        "취소": "cancelled",
        "반품진행": "return_requested",
        "회수확정": "return_requested",
        "발송불가": "undeliverable",
    }
    if force_status:
        status = force_status
        shipping_status = force_shipping_status or proc_stat or "출고지시"
    elif is_deliver_api and not proc_stat:
        status = "shipping"
        shipping_status = "배송대기중"
    else:
        status = status_map.get(proc_stat, "pending")
        shipping_status = proc_stat or "출고지시"
        if shipping_status == "출고확정":
            shipping_status = "배송대기중"

    product_name = str(prod_info.get("ProdName") or prod_info.get("GoodsNm") or "")
    product_option = str(
        prod_info.get("prodOption") or prod_info.get("GoodsDesc") or ""
    )
    product_id = str(prod_info.get("ProdCode") or prod_info.get("GoodsNo") or "")
    sale_price = int(float(prod_info.get("ordPrice") or prod_info.get("SalePrc") or 0))
    buy_real_price = int(float(prod_info.get("buyRealPrice", 0) or 0))
    qty = int(prod_info.get("ordQty") or prod_info.get("OrdQty") or 1)

    recv_name = str(
        delv_info.get("recvName")
        or delv_info.get("RmitNm")
        or item.get("OrderName")
        or ""
    )
    recv_addr = str(
        delv_info.get("recvAddr1", "")
        or delv_info.get("Addr", "")
        or item.get("OrderAddr1", "")
    )
    recv_addr2 = str(delv_info.get("recvAddr2", "") or item.get("OrderAddr2", ""))
    recv_tel = str(
        delv_info.get("recvTel")
        or delv_info.get("recvHp")
        or item.get("OrderTelNo")
        or ""
    )
    shipping_company = str(delv_info.get("delvName") or delv_info.get("HdcNm") or "")
    tracking_number = _lh_str(delv_info.get("invoiceNo"), delv_info.get("InvNo"))

    trd_date = str(item.get("TrdDate", "") or "")
    paid_at = None
    if trd_date:
        try:
            paid_at = datetime.strptime(trd_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    if paid_at is None and len(order_no) >= 8:
        try:
            paid_at = datetime.strptime(order_no[:8], "%Y%m%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    if paid_at is None:
        paid_at = datetime.now(timezone.utc)

    return {
        "order_number": order_number,
        "channel_id": account_id,
        "channel_name": label,
        "product_id": product_id,
        "product_name": product_name,
        "product_option": product_option,
        "customer_name": recv_name,
        "customer_phone": recv_tel,
        "customer_address": f"{recv_addr} {recv_addr2}".strip(),
        # 우편번호 — 화면 확인용 (복사 버튼 분리). 롯데홈쇼핑 API 필드: recvZipCd
        "customer_postal_code": (
            str(delv_info.get("recvZipCd") or delv_info.get("ZipCd") or "").strip()
            or None
        ),
        "quantity": qty,
        "sale_price": sale_price,
        "total_payment_amount": sale_price * qty,
        "cost": 0,
        "fee_rate": 0,
        "revenue": buy_real_price,
        "status": status,
        "shipping_status": shipping_status,
        "shipping_company": shipping_company,
        "tracking_number": tracking_number,
        "paid_at": paid_at,
        "source": "lottehome",
        "shipment_id": order_no,
        "ext_order_number": ext_order_number,
    }

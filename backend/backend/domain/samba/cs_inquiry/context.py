"""CS 답변용 컨텍스트 수집기 (Tier 0).

문의 1건에 대해 답변 작성에 필요한 사실을 DB에서 끌어와 구조화한다.
누적 답변 분석 결과 진짜 작업은 '템플릿 고르기'가 아니라 '주문/재고/송장
같은 주문별 사실을 끌어와 답변을 쓰는 것'이므로, 이 수집기가 자동화의 핵심.

산출 dict는 Claude 스케줄잡(Tier 1)의 프롬프트 입력으로 사용된다.
재고(stock_check) 문의는 source_url(원소싱처 원문링크)을 함께 제공해
에이전트가 직접 자료를 검색·확인하도록 한다.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import text as sa_text
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.domain.samba.cs_inquiry.model import SambaCSInquiry


def _trim(v: Any, n: int = 800) -> Any:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + "…"
    return v


async def _fetch_order(
    session: AsyncSession, inquiry: SambaCSInquiry
) -> Optional[Dict[str, Any]]:
    """문의에 연결된 주문 1건 조회 (market_order_id → order_number 폴백)."""
    key = inquiry.market_order_id
    if not key:
        return None
    # market_order_id 는 마켓 원본 주문번호일 수도, 내부 order_number 일 수도 있어 둘 다 시도
    row = (
        await session.execute(
            sa_text(
                "SELECT order_number, product_name, product_option, quantity, "
                "       status, payment_status, shipping_status, shipping_company, "
                "       tracking_number, sourcing_order_number, customer_name, "
                "       created_at, paid_at "
                "FROM samba_order "
                "WHERE order_number = :k OR ext_order_number = :k "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"k": key},
        )
    ).first()
    if not row:
        return None
    m = row._mapping
    return {
        "order_number": m["order_number"],
        "product_name": m["product_name"],
        "product_option": m["product_option"],
        "quantity": m["quantity"],
        "status": m["status"],
        "payment_status": m["payment_status"],
        "shipping_status": m["shipping_status"],
        "shipping_company": m["shipping_company"],
        "tracking_number": m["tracking_number"],
        "sourcing_order_number": m["sourcing_order_number"],
        "customer_name": m["customer_name"],
        "created_at": m["created_at"].isoformat() if m["created_at"] else None,
        "paid_at": m["paid_at"].isoformat() if m["paid_at"] else None,
    }


async def _fetch_product(
    session: AsyncSession, inquiry: SambaCSInquiry
) -> Optional[Dict[str, Any]]:
    """문의에 연결된 수집상품(재고·소싱처) 조회."""
    pid = inquiry.collected_product_id
    if not pid:
        return None
    row = (
        await session.execute(
            sa_text(
                "SELECT name, source_site, source_url, sale_status, is_sold_out, "
                "       options, last_refreshed_at "
                "FROM samba_collected_product WHERE id = :pid LIMIT 1"
            ),
            {"pid": pid},
        )
    ).first()
    if not row:
        return None
    m = row._mapping
    return {
        "name": m["name"],
        "source_site": m["source_site"],
        "source_url": m["source_url"],
        "sale_status": m["sale_status"],
        "is_sold_out": m["is_sold_out"],
        "options": m["options"],  # 옵션별 재고 [{name, stock, ...}]
        "last_refreshed_at": (
            m["last_refreshed_at"].isoformat() if m["last_refreshed_at"] else None
        ),
    }


async def gather_context(
    session: AsyncSession, inquiry: SambaCSInquiry, intent: str
) -> Dict[str, Any]:
    """문의 1건의 답변 컨텍스트 번들 조립.

    반환 dict:
      question        - 고객 문의 본문
      market/product  - 마켓·상품 기본 정보
      order           - 연결 주문 사실 (없으면 None)
      product         - 수집상품/재고 사실 (없으면 None)
      source_url      - 원소싱처 원문링크 (재고 검증용)
      needs_source_lookup - 재고 의도면 True (에이전트가 원문 검색)
    """
    order = await _fetch_order(session, inquiry)
    product = await _fetch_product(session, inquiry)

    # 재고 문의는 원소싱처 원문링크 자료 검색이 필요
    source_url = None
    if product and product.get("source_url"):
        source_url = product["source_url"]
    elif inquiry.original_link:
        source_url = inquiry.original_link

    return {
        "inquiry_id": inquiry.id,
        "intent": intent,
        "question": _trim(inquiry.content),
        "market": inquiry.market,
        "product_name": inquiry.product_name,
        "product_link": inquiry.product_link,
        "order": order,
        "product": product,
        "source_url": source_url,
        "needs_source_lookup": intent == "stock_check" and bool(source_url),
    }

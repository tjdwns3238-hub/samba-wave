"""SambaWave Collector 공통 모듈 — 상수, 헬퍼 함수, 팩토리 메서드."""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

from sqlalchemy import cast, func, String as _StrType
from sqlalchemy.dialects.postgresql import JSONB as _JSONB  # noqa: F401
from sqlmodel.ext.asyncio.session import AsyncSession
from backend.domain.samba.collector.grouping import (
    generate_group_key,
    parse_color_from_name,
)


# ── 상수 ──

# HTML 태그 및 불필요 문자 정제 (상품명/브랜드/옵션 등에서 제거)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# 스크롤/목록 조회 시 제외할 무거운 필드 (source_url은 가볍고 목록에서 필요하므로 제외 안 함)
_HEAVY_FIELDS = {
    "price_history",
    "detail_html",
    "extra_data",
    "kream_data",
    "detail_images",  # 상품목록 불필요 — 상세 페이지에서만 사용
}


# ── 블랙리스트 캐시 ──

# 수집 블랙리스트 캐시 (서버 수명 동안 유지, 변경 시 갱신)
_blacklist_cache: set[str] | None = None


async def _load_blacklist(session: AsyncSession) -> set[str]:
    """블랙리스트를 DB에서 로드하여 캐시."""
    global _blacklist_cache
    if _blacklist_cache is not None:
        return _blacklist_cache
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository

    repo = SambaSettingsRepository(session)
    row = await repo.find_by_async(key="collection_blacklist")
    items = row.value if row and isinstance(row.value, list) else []
    _blacklist_cache = {
        f"{b['source_site']}:{b['site_product_id']}"
        for b in items
        if b.get("source_site") and b.get("site_product_id")
    }
    return _blacklist_cache


def _invalidate_blacklist_cache():
    """블랙리스트 캐시 무효화."""
    global _blacklist_cache
    _blacklist_cache = None


async def _is_blacklisted(
    session: AsyncSession, source_site: str, site_product_id: str
) -> bool:
    """블랙리스트 체크 — 캐시 없으면 자동 로드."""
    if _blacklist_cache is None:
        await _load_blacklist(session)
    return f"{source_site}:{site_product_id}" in (_blacklist_cache or set())


# ── 텍스트 정제 ──


def _clean_text(value: str) -> str:
    """HTML 태그 제거 + 연속 공백 정리."""
    if not value:
        return value
    cleaned = _HTML_TAG_RE.sub(" ", value)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


# ── 상품 데이터 빌드 ──

# _build_product_data에서 명시적으로 매핑하는 프록시 키 목록
# 여기에 없는 키는 extra_data에 자동 저장됨
_KNOWN_PROXY_KEYS = {
    "name",
    "brand",
    "images",
    "detailImages",
    "options",
    "sourceUrl",
    "category",
    "manufacturer",
    "origin",
    "material",
    "color",
    "style_code",
    "sex",
    "season",
    "care_instructions",
    "quality_guarantee",
    "saleStatus",
    "freeShipping",
    "sameDayDelivery",
    "originalPrice",
    "salePrice",
    "cost",
    "detail_html",
    "video_url",
    "kreamData",
    "collectedAt",
    "updatedAt",
}


def _build_product_data(
    detail: dict,
    goods_no: str,
    filter_id: str,
    site: str,
    cost: Optional[float],
    sale_price: float,
    original_price: float,
    raw_cat: str,
    cat_parts: list,
    raw_detail_html: str,
) -> dict:
    """수집 상품 데이터 빌드 (collect_by_url / collect_by_filter 공통).

    프록시에서 보내는 모든 데이터를 보존:
    - 명시 매핑 필드 → DB 컬럼에 직접 저장
    - sourceUrl → source_url 컬럼에 저장
    - 미매핑 필드 → extra_data JSON에 자동 저장
    """
    initial_snapshot = {
        "date": datetime.now(timezone.utc).isoformat(),
        "sale_price": sale_price,
        "original_price": original_price,
        "cost": cost,
        "options": detail.get("options", []),
    }
    # 옵션 정제 (옵션명에서도 HTML 태그 제거)
    raw_options = detail.get("options", [])
    cleaned_options = []
    for opt in raw_options:
        if isinstance(opt, dict):
            cleaned_opt = {**opt}
            for k in ("name", "value", "label"):
                if k in cleaned_opt and isinstance(cleaned_opt[k], str):
                    cleaned_opt[k] = _clean_text(cleaned_opt[k])
            cleaned_options.append(cleaned_opt)
        else:
            cleaned_options.append(opt)

    # 미매핑 필드 → extra_data에 자동 보존
    extra = {k: v for k, v in detail.items() if k not in _KNOWN_PROXY_KEYS}
    extra_data = extra if extra else None

    return {
        "source_site": site,
        "site_product_id": goods_no,
        "search_filter_id": filter_id,
        "source_url": detail.get("sourceUrl", ""),
        "name": _clean_text(detail.get("name", "")),
        "brand": _clean_text(detail.get("brand", "")),
        "original_price": original_price,
        "sale_price": sale_price,
        "cost": cost,
        "images": detail.get("images", []),
        "detail_images": detail.get("detailImages")
        or detail.get("detail_images")
        or [],
        "options": [
            {**o, "stock": o.get("stock") if o.get("stock") is not None else 0}
            for o in cleaned_options
        ],
        # 추가구성상품 (메인 옵션과 별개 차원 — 스마트스토어 productAddItems 등으로 매핑)
        "addon_options": detail.get("addonOptions") or None,
        # 메인 옵션 그룹명 목록 (예: ["색상","사이즈"])
        "option_group_names": detail.get("optionGroupNames") or None,
        "category": raw_cat,
        "category1": cat_parts[0] if len(cat_parts) > 0 else None,
        "category2": cat_parts[1] if len(cat_parts) > 1 else None,
        "category3": cat_parts[2] if len(cat_parts) > 2 else None,
        "category4": cat_parts[3] if len(cat_parts) > 3 else None,
        "manufacturer": _clean_text(detail.get("manufacturer") or "")
        or _clean_text(detail.get("brand") or ""),
        "origin": _clean_text(detail.get("origin") or ""),
        "material": _clean_text(detail.get("material") or ""),
        "color": _clean_text(detail.get("color") or "")
        or parse_color_from_name(detail.get("name", "")),
        "sex": detail.get("sex", "") or "남녀공용",
        "season": detail.get("season", "") or "사계절",
        "care_instructions": _clean_text(detail.get("care_instructions", "")),
        "quality_guarantee": _clean_text(detail.get("quality_guarantee", "")),
        "similar_no": str(detail.get("similarNo", "0")),
        "style_code": _clean_text(
            detail.get("styleNo", "") or detail.get("style_code", "")
        ),
        "group_key": generate_group_key(
            brand=detail.get("brand", ""),
            similar_no=str(detail.get("similarNo", "0")),
            style_code=detail.get("styleNo", "") or detail.get("style_code", ""),
            name=detail.get("name", ""),
        ),
        "detail_html": raw_detail_html
        or detail.get("detailHtml")
        or detail.get("detail_html")
        or "",
        "status": "collected",
        "sale_status": detail.get("saleStatus", "in_stock"),
        "free_shipping": detail.get("freeShipping", False),
        "same_day_delivery": detail.get("sameDayDelivery", False),
        "price_history": [initial_snapshot],
        "extra_data": extra_data,
    }


def _trim_history(history: list) -> list:
    """price_history dedup + cap.

    - dedup: history[0](최신)과 history[1] 의 sale_price/original_price/cost/sale_status
      가 모두 동일하면 새 snapshot(history[0]) 제거. 가격/상태 변동 흔적만 보존하여
      변동 없는 routine ping(autotune/enrich/refresh) 이 cap 을 채우지 않도록.
    - cap: 최초 수집 1개 + 최근 49개 = 최대 50개.
    """
    if isinstance(history, list) and len(history) >= 2:
        curr = history[0] if isinstance(history[0], dict) else {}
        prev = history[1] if isinstance(history[1], dict) else {}
        same = (
            curr.get("sale_price") == prev.get("sale_price")
            and curr.get("original_price") == prev.get("original_price")
            and curr.get("cost") == prev.get("cost")
            and curr.get("sale_status") == prev.get("sale_status")
        )
        if same:
            history = history[1:]
    if len(history) <= 50:
        return history
    # history[0]이 최신, history[-1]이 최초
    return history[:49] + [history[-1]]


# ── KREAM 가격이력 스냅샷 ──


def _build_kream_price_snapshot(sale_price, original_price, cost, options):
    """KREAM 전용 가격이력 스냅샷 — 빠른배송/일반배송 최저가 포함."""
    fast_prices = [
        o.get("kreamFastPrice", 0)
        for o in (options or [])
        if o.get("kreamFastPrice", 0) > 0
    ]
    general_prices = [
        o.get("kreamGeneralPrice", 0)
        for o in (options or [])
        if o.get("kreamGeneralPrice", 0) > 0
    ]

    return {
        "date": datetime.now(timezone.utc).isoformat(),
        "sale_price": sale_price,
        "original_price": original_price,
        "cost": cost,
        "kream_fast_min": min(fast_prices) if fast_prices else 0,
        "kream_general_min": min(general_prices) if general_prices else 0,
        "options": [
            {
                "name": o.get("name", ""),
                "price": o.get("price", 0),
                "stock": o.get("stock", 0),
                "kreamFastPrice": o.get("kreamFastPrice", 0),
                "kreamGeneralPrice": o.get("kreamGeneralPrice", 0),
            }
            for o in (options or [])
        ],
    }


# ── 서비스 팩토리 ──


def _get_services(session: AsyncSession):
    """CollectorService 인스턴스 생성 팩토리."""
    from backend.domain.samba.collector.repository import (
        SambaCollectedProductRepository,
        SambaSearchFilterRepository,
    )
    from backend.domain.samba.collector.service import SambaCollectorService

    return SambaCollectorService(
        SambaSearchFilterRepository(session),
        SambaCollectedProductRepository(session),
    )


# ── 무신사 쿠키 조회 ──


async def _get_login_default_musinsa_cookie(session: AsyncSession) -> str:
    """is_login_default=True 무신사 계정의 cookie 조회 (단일 진실).

    SourcingAccount.additional_fields.musinsa_cookie 만 신뢰. 만료(cookie_expired=True)
    이거나 미설정이면 빈 문자열 반환 → 호출부가 fallback 결정.
    """
    try:
        from backend.domain.samba.sourcing_account.service import (
            SambaSourcingAccountService,
        )
        from backend.domain.samba.sourcing_account.repository import (
            SambaSourcingAccountRepository,
        )

        svc = SambaSourcingAccountService(SambaSourcingAccountRepository(session))
        acc = await svc.get_login_default("MUSINSA")
        if not acc:
            return ""
        af = acc.additional_fields or {}
        if af.get("cookie_expired"):
            return ""
        return af.get("musinsa_cookie", "") or ""
    except Exception:
        return ""


async def get_musinsa_cookie(session: AsyncSession | None = None) -> str:
    """DB에서 무신사 쿠키를 조회하여 반환.

    우선순위 (2026-05-27 변경):
      1) is_login_default=True SourcingAccount.additional_fields.musinsa_cookie
         — 자동로그인 계정 단일 진실. 오토튠 _get_autologin_musinsa_cookie 와 동일 경로로
         일치시켜 cost 계산 들쑥날쑥 차단.
      2) (fallback) SambaSettings.musinsa_cookie — 자동로그인 계정 미설정/만료 시.

    SambaSettings.musinsa_cookie 는 _set_setting 을 통해 Fernet 암호화 상태로 저장되므로
    조회 시 반드시 decrypt_value 로 복호화 (2026-05-01 진단).
    """
    from backend.domain.samba.forbidden.model import SambaSettings
    from backend.utils.crypto import decrypt_value
    from sqlmodel import select as _sel

    if session is not None:
        try:
            cookie = await _get_login_default_musinsa_cookie(session)
            if cookie:
                return cookie
            result = await session.execute(
                _sel(SambaSettings).where(SambaSettings.key == "musinsa_cookie")
            )
            row = result.scalar_one_or_none()
            raw = (row.value if row and row.value else "") or ""
            return decrypt_value(raw) if raw else ""
        except Exception:
            logger.warning(
                "[get_musinsa_cookie] 쿠키 조회 실패 (세션 전달)", exc_info=True
            )
            return ""

    # 세션이 없으면 새 읽기 세션 생성
    try:
        from backend.db.orm import get_read_session

        async with get_read_session() as new_session:
            cookie = await _get_login_default_musinsa_cookie(new_session)
            if cookie:
                return cookie
            result = await new_session.execute(
                _sel(SambaSettings).where(SambaSettings.key == "musinsa_cookie")
            )
            row = result.scalar_one_or_none()
            raw = (row.value if row and row.value else "") or ""
            return decrypt_value(raw) if raw else ""
    except Exception:
        logger.warning("[get_musinsa_cookie] 쿠키 조회 실패 (신규 세션)", exc_info=True)
        return ""


# ── 판매이력상품 필터 조건 ──


async def build_has_orders_conditions(session: AsyncSession, model_class: Any) -> list:
    """판매이력상품 필터 — 주문 product_id가 market_product_nos JSON 값에 포함된 상품.

    SambaOrder.product_id는 마켓상품번호(channelProductNo, spdNo, ProdCode 등)를 저장하고,
    SambaCollectedProduct.market_product_nos는 {"account_id": "마켓상품번호"} 형태 JSON이므로,
    LIKE 패턴으로 JSON value를 매칭한다.
    """
    from sqlalchemy import or_, text
    from sqlmodel import select

    from backend.core.sql_safe import escape_like
    from backend.domain.samba.order.model import SambaOrder

    result = await session.execute(
        select(SambaOrder.product_id)
        .where(SambaOrder.product_id.isnot(None))
        .where(SambaOrder.product_id != "")
        .distinct()
    )
    order_pids = result.scalars().all()

    if not order_pids:
        return [text("1=0")]

    # pid 는 마켓에서 수신해 저장된 product_id — defense-in-depth 로 LIKE 메타 escape.
    return [
        or_(
            *[
                cast(model_class.market_product_nos, _StrType).like(
                    f'%"{escape_like(pid)}"%', escape="\\"
                )
                for pid in order_pids
            ]
        )
    ]


# ── 마켓등록상품 필터 조건 ──


def has_registered_accounts(model_class: Any):
    """registered_accounts 배열이 비어있지 않음 — is_unregistered=FALSE 대체 표현식."""
    from sqlalchemy import and_

    return and_(
        model_class.registered_accounts.isnot(None),
        func.jsonb_typeof(model_class.registered_accounts) == "array",
        model_class.registered_accounts.op("!=")(cast("[]", _JSONB)),
    )


def no_registered_accounts(model_class: Any):
    """registered_accounts 배열이 비어있거나 NULL — is_unregistered=TRUE 대체 표현식."""
    from sqlalchemy import or_

    return or_(
        model_class.registered_accounts.is_(None),
        func.jsonb_typeof(model_class.registered_accounts) != "array",
        model_class.registered_accounts.op("=")(cast("[]", _JSONB)),
    )


def build_market_registered_conditions(model_class: Any) -> list:
    """마켓등록상품 판별 SQLAlchemy 조건 리스트 반환.

    registered_accounts IS NOT NULL AND != '[]' (JSONB 비교 — jsonb_array_length 금지)
    AND market_product_nos IS NOT NULL / != 'null' / != '{}'
    """
    return [
        model_class.registered_accounts.isnot(None),
        func.jsonb_typeof(model_class.registered_accounts) == "array",
        # jsonb_array_length 금지: PostgreSQL은 WHERE 절 단락 평가를 보장하지 않아
        # jsonb_typeof 체크보다 먼저 평가되면 스칼라값에서 에러 발생
        model_class.registered_accounts.op("!=")(cast("[]", _JSONB)),
        model_class.market_product_nos.isnot(None),
        cast(model_class.market_product_nos, _StrType) != "null",
        cast(model_class.market_product_nos, _StrType) != "{}",
    ]

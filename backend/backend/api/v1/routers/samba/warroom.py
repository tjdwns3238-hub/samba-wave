"""SambaWave 워룸(모니터링) API 라우터."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_read_session_dependency, get_write_session_dependency
from backend.domain.samba.cache import cache
from backend.domain.samba.collector.refresher import (
    get_refresh_logs,
    get_site_intervals_info,
)
from backend.domain.samba.warroom.service import SambaMonitorService
from backend.domain.samba.warroom.repository import SambaMonitorEventRepository

# 워룸/모니터링 — monitor_event 테이블에 tenant_id 컬럼 추가됨 (zzzz_monitor_event_tenant_id)
# ORM 자동 필터로 entity SELECT는 격리. projection 쿼리(count_by_type_since 등)는 수동 패치.
router = APIRouter(prefix="/monitor", tags=["samba-monitor"])


def _normalize_source_site(site: str | None) -> str:
    raw = (site or "").strip()
    if not raw:
        return "기타"
    if raw.upper() == "GSSHOP":
        return "GSShop"
    return raw


@router.get("/dashboard")
async def get_dashboard(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """대시보드 전체 통계 (30초 폴링 대상)."""
    svc = SambaMonitorService(session)
    return await svc.get_dashboard_stats()


@router.get("/events")
async def list_events(
    event_type: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """이벤트 목록 — 필터 가능. 60초 캐시 + single-flight."""
    cache_key = f"warroom:events:{event_type or '_'}:{severity or '_'}:{limit}"

    async def _factory():
        repo = SambaMonitorEventRepository(session)
        if severity:
            events = await repo.list_by_severity(severity, limit)
        elif event_type:
            events = await repo.list_by_type(event_type, limit)
        else:
            events = await repo.list_recent(limit)
        return [
            {
                "id": e.id,
                "event_type": e.event_type,
                "severity": e.severity,
                "source_site": _normalize_source_site(e.source_site),
                "market_type": e.market_type,
                "product_id": e.product_id,
                "product_name": e.product_name,
                "summary": e.summary,
                "detail": e.detail,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ]

    return await cache.get_or_compute(cache_key, _factory, ttl=60)


@router.get("/events/recent")
async def list_recent_events(
    limit: int = Query(50, ge=1, le=100),
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """최근 이벤트 — scheduler_tick 최신 3건 보장. 60초 캐시 + single-flight."""
    cache_key = f"warroom:events_recent:{limit}"

    async def _factory():
        repo = SambaMonitorEventRepository(session)
        recent, ticks, cycles = (
            await repo.list_recent(limit),
            await repo.list_latest_per_site("scheduler_tick", per_site_limit=2),
            await repo.list_latest_per_site("scheduler_cycle", per_site_limit=2),
        )
        seen = {e.id for e in recent}
        tick_ids = {t.id for t in ticks}
        merged = (
            list(recent)
            + [t for t in ticks if t.id not in seen]
            + [c for c in cycles if c.id not in seen and c.id not in tick_ids]
        )
        merged.sort(key=lambda e: e.created_at, reverse=True)
        return [
            {
                "id": e.id,
                "event_type": e.event_type,
                "severity": e.severity,
                "source_site": _normalize_source_site(e.source_site),
                "market_type": e.market_type,
                "product_id": e.product_id,
                "product_name": e.product_name,
                "summary": e.summary,
                "detail": e.detail,
                "created_at": e.created_at.isoformat(),
            }
            for e in merged
        ]

    return await cache.get_or_compute(cache_key, _factory, ttl=60)


@router.get("/price-changes")
async def list_price_changes(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """최근 24시간 가격 변동 이벤트. 60초 캐시 + single-flight."""

    async def _factory():
        repo = SambaMonitorEventRepository(session)
        now = datetime.now(timezone.utc)
        since_24h = now - timedelta(hours=24)
        recent = await repo.list_by_type("price_changed", limit=100, since=since_24h)
        return [
            {
                "id": e.id,
                "product_id": e.product_id,
                "product_name": e.product_name,
                "source_site": _normalize_source_site(e.source_site),
                "detail": e.detail,
                "created_at": e.created_at.isoformat(),
            }
            for e in recent
        ]

    return await cache.get_or_compute("warroom:price_changes", _factory, ttl=60)


@router.get("/events/site-changes")
async def list_site_changes(
    limit: int = Query(5, ge=1, le=20),
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """소싱처별 최근 가격변동·재고변동 이벤트 (각 N건). 60초 캐시 + single-flight."""
    cache_key = f"warroom:site_changes:{limit}"

    async def _factory():
        repo = SambaMonitorEventRepository(session)
        events = await repo.list_changes_per_site(
            event_types=["price_changed", "sold_out", "restock"],
            per_site_limit=limit,
        )
        result: dict[str, dict[str, list]] = {}
        for e in events:
            site = e.source_site or "기타"
            etype = e.event_type
            if site not in result:
                result[site] = {}
            if etype not in result[site]:
                result[site][etype] = []
            result[site][etype].append(
                {
                    "id": e.id,
                    "product_id": e.product_id,
                    "product_name": e.product_name,
                    "detail": e.detail,
                    "created_at": e.created_at.isoformat(),
                }
            )
        return result

    return await cache.get_or_compute(cache_key, _factory, ttl=60)


@router.get("/events/market-changes")
async def list_market_changes(
    limit: int = Query(5, ge=1, le=20),
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """판매처(마켓)별 최근 가격변동·품절 이벤트 fan-out (각 N건). 60초 캐시 + single-flight."""
    cache_key = f"warroom:market_changes:{limit}"

    async def _factory():
        svc = SambaMonitorService(session)
        return await svc.get_market_changes(per_market_limit=limit)

    return await cache.get_or_compute(cache_key, _factory, ttl=60)


@router.get("/site-health")
async def get_site_health(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """소싱처/마켓 헬스 상태. 60초 캐시 + single-flight."""

    async def _factory():
        svc = SambaMonitorService(session)
        site_health = await svc._get_site_health()
        market_health = await svc._get_market_health()
        return {
            "sources": site_health,
            "markets": market_health,
        }

    return await cache.get_or_compute("warroom:site_health", _factory, ttl=60)


@router.get("/refresh-logs")
async def get_refresh_log_entries(
    since_idx: int = Query(0, ge=0),
):
    """오토튠 실시간 로그 (인메모리 링 버퍼). since_idx 이후 증분 반환. 오토튠 로그만 필터."""
    logs, current_idx = get_refresh_logs(since_idx, source_filter="autotune")
    intervals_info = get_site_intervals_info()
    return {
        "logs": logs,
        "current_idx": current_idx,
        "intervals": intervals_info,
    }


@router.post("/refresh-logs/clear")
async def clear_refresh_logs_endpoint():
    """오토튠 실시간 로그 버퍼 초기화."""
    from backend.domain.samba.collector.refresher import clear_refresh_logs

    clear_refresh_logs()
    return {"ok": True}


@router.delete("/events/cleanup")
async def cleanup_old_events(
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """오래된 이벤트 정리."""
    before = datetime.now(timezone.utc) - timedelta(days=days)
    repo = SambaMonitorEventRepository(session)
    deleted = await repo.cleanup_old(before)
    await session.commit()
    return {"deleted": deleted}


# ═══════════════════════════════════════════════
# 스토어 현황 점수 모니터링
# ═══════════════════════════════════════════════

_GRADE_LABELS: dict[str, str] = {
    "01": "프리미엄",
    "02": "빅파워",
    "03": "파워",
    "04": "새싹",
    "05": "씨앗",
}

# 스마트스토어 등급별 최대 등록 상품 수
_SS_MAX_PRODUCTS: dict[str, int] = {
    "01": 100000,
    "02": 50000,
    "03": 10000,
    "04": 5000,
    "05": 1000,
}

# 마켓별 기본 최대 등록 상품 수
_MARKET_MAX_PRODUCTS: dict[str, int] = {
    "coupang": 0,  # 무제한
    "11st": 50000,
    "lotteon": 50000,
    "ssg": 10000,
}


@router.get("/store-scores")
async def get_store_scores(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """전체 계정의 스토어 점수 조회 (DB 캐시)."""
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository

    repo = SambaSettingsRepository(session)
    row = await repo.find_by_async(key="store_scores_cache")
    return row.value if row and row.value else {}


@router.post("/store-scores/refresh")
async def refresh_store_scores(
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """전체 마켓 계정의 판매자 등급/상태를 API로 조회 후 캐시 저장."""
    from backend.domain.samba.account.repository import SambaMarketAccountRepository
    from backend.domain.samba.proxy.smartstore import SmartStoreClient
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository
    from backend.domain.samba.forbidden.model import SambaSettings
    from backend.utils.logger import logger

    account_repo = SambaMarketAccountRepository(session)
    accounts = await account_repo.list_async(limit=200)
    active_accounts = [a for a in accounts if a.is_active]

    results: dict[str, dict] = {}

    # 기존 캐시 로드
    settings_repo = SambaSettingsRepository(session)
    cache_row = await settings_repo.find_by_async(key="store_scores_cache")
    if cache_row and isinstance(cache_row.value, dict):
        results = cache_row.value

    now_iso = datetime.now(timezone.utc).isoformat()

    for acc in active_accounts:
        extras = acc.additional_fields or {}
        market = acc.market_type or ""
        old = results.get(acc.id, {})

        try:
            if market == "smartstore":
                cid = extras.get("clientId", "")
                cs = extras.get("clientSecret", "")
                if not cid or not cs:
                    continue
                client = SmartStoreClient(cid, cs)
                data = await client._call_api("GET", "/v1/seller/account")
                grade_code = data.get("grade", "")
                grade_label = _GRADE_LABELS.get(grade_code, grade_code)
                # 상품 수 조회
                product_count = 0
                try:
                    search = await client._call_api(
                        "POST", "/v1/products/search", body={"page": 1, "size": 1}
                    )
                    product_count = search.get("totalElements", search.get("total", 0))
                except Exception:
                    pass
                max_products = _SS_MAX_PRODUCTS.get(grade_code, 1000)
                results[acc.id] = {
                    **old,
                    "account_id": acc.id,
                    "account_label": acc.account_label or acc.seller_id,
                    "market_type": market,
                    "grade": grade_label,
                    "grade_code": grade_code,
                    "product_count": product_count,
                    "max_products": max_products,
                    "good_service": old.get("good_service"),
                    "penalty": old.get("penalty"),
                    "penalty_rate": old.get("penalty_rate"),
                    "updated_at": now_iso,
                }
                logger.info(
                    f"[워룸] {acc.account_label} 등급: {grade_label}, 상품: {product_count}/{max_products}개"
                )

            elif market == "11st":
                api_key = extras.get("apiKey", "")
                if not api_key:
                    continue
                from backend.domain.samba.proxy.elevenst import ElevenstClient

                client_11 = ElevenstClient(api_key)
                # 상품 검색으로 등록 상품 수 확인
                product_count = 0
                try:
                    search = await client_11.get_product(
                        "0"
                    )  # 존재하지 않는 상품 조회 → 인증 확인
                except Exception:
                    pass
                results[acc.id] = {
                    **old,
                    "account_id": acc.id,
                    "account_label": acc.account_label or acc.seller_id,
                    "market_type": market,
                    "grade": "연결됨",
                    "grade_code": "connected",
                    "max_products": _MARKET_MAX_PRODUCTS.get(market, 0),
                    "updated_at": now_iso,
                }
                logger.info(f"[워룸] 11번가 {acc.account_label} 연결 확인")

            elif market == "coupang":
                access_key = extras.get("accessKey", "")
                secret_key = extras.get("secretKey", "")
                vendor_id = extras.get("vendorId", "")
                if not access_key or not secret_key:
                    continue

                results[acc.id] = {
                    **old,
                    "account_id": acc.id,
                    "account_label": acc.account_label or acc.seller_id,
                    "market_type": market,
                    "grade": "연결됨" if vendor_id else "Vendor ID 없음",
                    "grade_code": "connected" if vendor_id else "no_vendor",
                    "max_products": _MARKET_MAX_PRODUCTS.get(market, 0),
                    "updated_at": now_iso,
                }
                logger.info(f"[워룸] 쿠팡 {acc.account_label} 연결 확인")

            elif market == "lotteon":
                api_key = extras.get("apiKey", "")
                if not api_key:
                    continue
                from backend.domain.samba.proxy.lotteon import LotteonClient

                client_lt = LotteonClient(api_key)
                try:
                    auth = await client_lt.test_auth()
                    auth_data = auth.get("data", {})
                    tr_grp = auth_data.get("trGrpCd", "")
                    results[acc.id] = {
                        **old,
                        "account_id": acc.id,
                        "account_label": acc.account_label or acc.seller_id,
                        "market_type": market,
                        "grade": f"연결됨 ({tr_grp})" if tr_grp else "연결됨",
                        "grade_code": "connected",
                        "max_products": _MARKET_MAX_PRODUCTS.get(market, 0),
                        "updated_at": now_iso,
                    }
                except Exception:
                    results[acc.id] = {
                        **old,
                        "account_id": acc.id,
                        "account_label": acc.account_label or acc.seller_id,
                        "market_type": market,
                        "grade": "인증 실패",
                        "grade_code": "auth_failed",
                        "max_products": _MARKET_MAX_PRODUCTS.get(market, 0),
                        "updated_at": now_iso,
                    }
                logger.info(f"[워룸] 롯데ON {acc.account_label} 연결 확인")

            elif market == "ssg":
                api_key = extras.get("apiKey", "")
                if not api_key:
                    continue
                results[acc.id] = {
                    **old,
                    "account_id": acc.id,
                    "account_label": acc.account_label or acc.seller_id,
                    "market_type": market,
                    "grade": "연결됨",
                    "grade_code": "connected",
                    "max_products": _MARKET_MAX_PRODUCTS.get(market, 0),
                    "updated_at": now_iso,
                }
                logger.info(f"[워룸] SSG {acc.account_label} 연결 확인")

            else:
                # 기타 마켓 (기본 정보만)
                results[acc.id] = {
                    **old,
                    "account_id": acc.id,
                    "account_label": acc.account_label or acc.seller_id,
                    "market_type": market,
                    "grade": "등록됨",
                    "grade_code": "registered",
                    "updated_at": now_iso,
                }

        except Exception as e:
            logger.warning(f"[워룸] {acc.account_label} ({market}) 조회 실패: {e}")

    # 캐시 저장 (JSON 변경 감지를 위해 flag_modified 사용)
    from sqlalchemy.orm.attributes import flag_modified

    if cache_row:
        cache_row.value = results
        flag_modified(cache_row, "value")
        session.add(cache_row)
    else:
        session.add(SambaSettings(key="store_scores_cache", value=results))
    await session.commit()

    return {"success": True, "accounts": len(results), "data": results}


@router.post("/store-scores/update")
async def update_store_scores(
    body: dict,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """확장앱에서 스크래핑한 점수 데이터 수신 (굿서비스/패널티 등)."""
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository
    from backend.domain.samba.forbidden.model import SambaSettings

    account_id = body.get("account_id", "")
    if not account_id:
        return {"success": False, "message": "account_id 필요"}

    settings_repo = SambaSettingsRepository(session)
    cache_row = await settings_repo.find_by_async(key="store_scores_cache")
    results: dict = (
        cache_row.value if cache_row and isinstance(cache_row.value, dict) else {}
    )

    existing = results.get(account_id, {})
    existing.update(
        {
            "good_service": body.get("good_service"),
            "penalty": body.get("penalty"),
            "penalty_rate": body.get("penalty_rate"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    results[account_id] = existing

    if cache_row:
        cache_row.value = results
        session.add(cache_row)
    else:
        session.add(SambaSettings(key="store_scores_cache", value=results))
    await session.commit()

    return {"success": True}

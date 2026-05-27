"""SambaWave Collector API router - 필터 CRUD + 수집 상품 CRUD."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import or_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import (
    get_read_session,
    get_read_session_dependency,
    get_write_session_dependency,
)
from backend.domain.samba.cache import cache
from backend.domain.samba.collector.model import FIXED_REQUESTED_COUNT
from backend.domain.user.auth_service import get_user_id
from backend.domain.samba.tenant.middleware import get_optional_tenant_id

from backend.api.v1.routers.samba.collector_common import (
    _HEAVY_FIELDS,
    _get_services,
    _invalidate_blacklist_cache,
    has_registered_accounts,
    no_registered_accounts,
)

router = APIRouter(prefix="/collector", tags=["samba-collector"])


def _all_options_sold_out(cp):
    """options JSON 배열의 모든 옵션이 stock <= 0인 조건 (sale_status 무관)."""
    from sqlalchemy import and_, text, cast, String

    return and_(
        cp.options.isnot(None),
        cast(cp.options, String) != "null",
        cast(cp.options, String) != "[]",
        text(
            "NOT EXISTS ("
            "  SELECT 1 FROM json_array_elements(options) AS elem"
            "  WHERE COALESCE((elem->>'stock')::int, 0) > 0"
            ")"
        ),
    )


# ── Inline DTOs (will be replaced by dtos/samba/collector.py when ready) ──


class SearchFilterCreate(BaseModel):
    source_site: str
    name: str
    keyword: Optional[str] = None
    category_filter: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    exclude_sold_out: bool = True
    requested_count: int = FIXED_REQUESTED_COUNT
    parent_id: Optional[str] = None
    is_folder: bool = False


class SearchFilterUpdate(BaseModel):
    name: Optional[str] = None
    keyword: Optional[str] = None
    category_filter: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    exclude_sold_out: Optional[bool] = None
    is_active: Optional[bool] = None
    requested_count: Optional[int] = None
    applied_policy_id: Optional[str] = None
    target_mappings: Optional[dict] = None


class CollectedProductCreate(BaseModel):
    source_site: str
    site_product_id: Optional[str] = None
    search_filter_id: Optional[str] = None
    name: str
    name_en: Optional[str] = None
    name_ja: Optional[str] = None
    brand: Optional[str] = None
    original_price: float = 0
    sale_price: float = 0
    cost: Optional[float] = None
    images: Optional[list] = None
    options: Optional[list] = None
    category: Optional[str] = None
    category1: Optional[str] = None
    category2: Optional[str] = None
    category3: Optional[str] = None
    category4: Optional[str] = None
    manufacturer: Optional[str] = None
    style_code: Optional[str] = None
    origin: Optional[str] = None
    sex: Optional[str] = None
    season: Optional[str] = None
    color: Optional[str] = None
    material: Optional[str] = None
    detail_images: Optional[list] = None
    tags: Optional[list] = None
    status: str = "collected"


class CollectedProductUpdate(BaseModel):
    name: Optional[str] = None
    brand: Optional[str] = None
    manufacturer: Optional[str] = None
    style_code: Optional[str] = None
    origin: Optional[str] = None
    sex: Optional[str] = None
    season: Optional[str] = None
    color: Optional[str] = None
    material: Optional[str] = None
    care_instructions: Optional[str] = None
    quality_guarantee: Optional[str] = None
    sale_price: Optional[float] = None
    cost: Optional[float] = None
    status: Optional[str] = None
    applied_policy_id: Optional[str] = None
    market_prices: Optional[dict] = None
    market_enabled: Optional[dict] = None
    is_sold_out: Optional[bool] = None
    sale_status: Optional[str] = None
    lock_delete: Optional[bool] = None
    lock_stock: Optional[bool] = None
    images: Optional[list] = None
    detail_images: Optional[list] = None
    tags: Optional[list] = None
    seo_keywords: Optional[list] = None
    coupang_search_tags: Optional[list] = None
    options: Optional[list] = None
    extra_data: Optional[dict] = None


class BulkCreateRequest(BaseModel):
    items: list[CollectedProductCreate]


class BulkProductIdsRequest(BaseModel):
    ids: list[str]


class BlockProductRequest(BaseModel):
    product_ids: list[str]


class BrandDeleteRequest(BaseModel):
    source_site: str
    brand_name: str


class FolderCreateRequest(BaseModel):
    source_site: str
    name: str
    parent_id: Optional[str] = None


class MoveFilterRequest(BaseModel):
    parent_id: Optional[str] = None


class BulkImageRemoveRequest(BaseModel):
    image_url: str
    field: str = "images"  # 하위호환
    fields: Optional[list[str]] = (
        None  # ['images', 'detail_images', 'detail_html'] 선택 가능
    )


class BulkTagUpdateRequest(BaseModel):
    ids: list[str]
    tags: list[str] | None = None
    seo_keywords: list[str] | None = None


# ── Duplicate Detection ──


@router.get("/products/duplicates")
async def get_duplicate_products(
    request: Request,
    source_site: Optional[str] = Query(None),
    filter_ids: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """마켓 등록 상품과 동일 원상품명인 중복 상품 그룹 반환."""
    svc = _get_services(session)
    parsed_filter_ids = filter_ids.split(",") if filter_ids else None
    groups = await svc.get_duplicate_products(
        tenant_id=tenant_id, source_site=source_site, filter_ids=parsed_filter_ids
    )
    return {"groups": groups, "total": len(groups)}


# ── Status / Health ──


@router.get("/proxy-status")
async def proxy_status():
    """프록시 서버 연결 상태 확인 — 백엔드 통합으로 항상 정상."""
    return {"status": "ok", "message": "프록시 서버 정상 작동 중 (백엔드 통합)"}


@router.get("/pool-status")
async def pool_status(
    write_session: AsyncSession = Depends(get_write_session_dependency),
    read_session: AsyncSession = Depends(get_read_session_dependency),
):
    """Write/Read 커넥션 풀 현황 + pg_stat_activity 반환 — 수집 페이지 모니터링용."""
    import asyncio

    from sqlalchemy import text

    from backend.db.orm import get_read_engine, get_write_engine

    def _pool_stats(engine):
        p = engine.sync_engine.pool
        max_overflow = getattr(p, "_max_overflow", 15)
        return {
            "size": p.size(),
            "checkedout": p.checkedout(),
            "overflow": p.overflow(),
            "checkedin": p.checkedin(),
            "pool_max": p.size() + max_overflow,
        }

    async def _pg_stats(session: AsyncSession):
        try:
            # state별 카운트 + IIT 좀비(state_change >= 30s) 분리.
            # asyncpg/SQLAlchemy 정상 트랜잭션은 BEGIN 직후 ClientRead 대기 상태로 IIT에 잡힘 →
            # age 기반으로 진짜 좀비만 구분해야 false positive 알람 방지.
            result = await session.execute(
                text("""
                    SELECT state,
                           count(*) AS cnt,
                           count(*) FILTER (
                             WHERE state = 'idle in transaction'
                               AND state_change < now() - interval '30 seconds'
                           ) AS zombie_cnt
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                    GROUP BY state
                """)
            )
            data: dict = {}
            zombie_total = 0
            for state, cnt, zombie_cnt in result.all():
                key = (state or "unknown").replace(" ", "_")
                data[key] = int(cnt)
                zombie_total += int(zombie_cnt or 0)
            data["total"] = sum(v for k, v in data.items() if k != "total")
            data["iit_zombie"] = zombie_total
            return data
        except Exception:
            return None

    try:
        write_pool = _pool_stats(get_write_engine())
        read_pool = _pool_stats(get_read_engine())
        write_pg, read_pg = await asyncio.gather(
            _pg_stats(write_session),
            _pg_stats(read_session),
        )
        return {
            "write": {**write_pool, "pg": write_pg},
            "read": {**read_pool, "pg": read_pg},
            # 하위 호환 유지 — 신규 UI는 write/read 각각의 pool_max 사용
            "pool_max": write_pool["pool_max"],
            "write_pool_max": write_pool["pool_max"],
            "read_pool_max": read_pool["pool_max"],
        }
    except Exception:
        return {"write": None, "read": None, "pool_max": 35}


@router.get("/musinsa-auth-status")
async def musinsa_auth_status(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """무신사 인증 상태 + 자동로그인 자리 쿠키 주인 식별.

    - is_login_default row 가 cost 계산의 단일 진실. 그 row 의 쿠키를 본다.
      비어있으면 SambaSettings.musinsa_cookie pool 폴백 (UI 표시 한정).
    - 쿠키 JWT(mss_mac) 디코딩 → hashId/등급/성별/가입일.
    - row.additional_fields.musinsa_hash_id 와 쿠키 hashId 비교 → match 필드.
    """
    from backend.api.v1.routers.samba.proxy._musinsa_jwt import (
        musinsa_account_brief,
        musinsa_hash_id,
    )
    from backend.domain.samba.forbidden.model import SambaSettings
    from backend.domain.samba.sourcing_account.repository import (
        SambaSourcingAccountRepository,
    )
    from backend.domain.samba.sourcing_account.service import (
        SambaSourcingAccountService,
    )
    from backend.utils.crypto import decrypt_value

    try:
        # 1) 자동로그인 row 조회
        svc = SambaSourcingAccountService(SambaSourcingAccountRepository(session))
        acc = await svc.get_login_default("MUSINSA")

        slot_label: Optional[str] = None
        slot_username: Optional[str] = None
        slot_hash_id: Optional[str] = None
        row_cookie: str = ""
        row_updated_at: Optional[str] = None
        if acc:
            slot_label = acc.account_label
            slot_username = acc.username
            af = acc.additional_fields or {}
            slot_hash_id = af.get("musinsa_hash_id")
            if not af.get("cookie_expired"):
                row_cookie = af.get("musinsa_cookie", "") or ""
            row_updated_at = af.get("cookie_updated_at")

        # 2) row 비어있으면 pool 폴백 (status 판정용)
        pool_cookie = ""
        pool_updated_at: Optional[str] = None
        if not row_cookie:
            try:
                result = await session.execute(
                    select(SambaSettings).where(SambaSettings.key == "musinsa_cookie")
                )
                _row = result.scalar_one_or_none()
                if _row and _row.value:
                    pool_cookie = decrypt_value(_row.value) or ""
                    pool_updated_at = (
                        _row.updated_at.isoformat() if _row.updated_at else None
                    )
            except Exception:
                pass

        active_cookie = row_cookie or pool_cookie
        active_source = "slot" if row_cookie else ("pool" if pool_cookie else None)
        if not active_cookie:
            return {
                "status": "error",
                "message": "무신사 인증 필요",
                "updated_at": None,
                "account": None,
            }

        brief = musinsa_account_brief(active_cookie) or {}
        cookie_hash_id = brief.get("hash_id") or musinsa_hash_id(active_cookie)
        match: Optional[bool] = None
        if slot_hash_id and cookie_hash_id:
            match = slot_hash_id == cookie_hash_id

        return {
            "status": "ok",
            "message": "무신사 인증 완료",
            "updated_at": row_updated_at if row_cookie else pool_updated_at,
            "account": {
                "slot_label": slot_label,
                "slot_username": slot_username,
                "slot_hash_id": slot_hash_id,
                "cookie_hash_id": cookie_hash_id,
                "match": match,
                "source": active_source,
                "level": brief.get("level"),
                "gender": brief.get("gender"),
                "birth_year": brief.get("birth_year"),
                "register_date": brief.get("register_date"),
                "order_count": brief.get("order_count"),
            },
        }
    except Exception as e:
        logger.error(f"[musinsa-auth-status] 조회 실패: {e}", exc_info=True)
    return {
        "status": "error",
        "message": "무신사 인증 필요",
        "updated_at": None,
        "account": None,
    }


# ── Search Filters ──


@router.get("/filters")
async def list_filters(session: AsyncSession = Depends(get_write_session_dependency)):
    """검색필터 목록 + 필터별 카운트 6종. 60초 캐시 + single-flight."""

    async def _factory():
        svc = _get_services(session)
        all_filters = await svc.list_filters(limit=10000)
        filters = [f for f in all_filters if not f.is_folder]

        from backend.domain.samba.collector.model import SambaCollectedProduct as _CP
        from sqlalchemy import func, case, and_, literal, text as _text2

        _AI_TAGGED2 = _text2("'[\"__ai_tagged__\"]'::jsonb")
        _AI_IMAGE2 = _text2("'[\"__ai_image__\"]'::jsonb")

        filter_ids = [f.id for f in filters]
        if not filter_ids:
            return []

        count_stmt = (
            select(
                _CP.search_filter_id,
                func.count().label("collected_count"),
                func.count(case((has_registered_accounts(_CP), literal(1)))).label(
                    "market_registered_count"
                ),
                func.count(
                    case((and_(_CP.applied_policy_id != None), literal(1)))  # noqa: E711
                ).label("policy_applied_count"),
                func.count(
                    case(
                        (
                            _CP.tags.op("@>")(_AI_TAGGED2),
                            literal(1),
                        )
                    )
                ).label("ai_tagged_count"),
                func.count(
                    case(
                        (
                            _CP.tags.op("@>")(_AI_IMAGE2),
                            literal(1),
                        )
                    )
                ).label("ai_image_count"),
                func.count(
                    case(
                        (
                            and_(
                                _CP.tags.isnot(None),
                                func.jsonb_array_length(_CP.tags) > 0,
                            ),
                            literal(1),
                        )
                    )
                ).label("tag_applied_count"),
            )
            .where(_CP.search_filter_id.in_(filter_ids))
            .group_by(_CP.search_filter_id)
        )
        count_result = await session.execute(count_stmt)
        count_map = {}
        for row in count_result.all():
            count_map[row[0]] = {
                "collected_count": row[1],
                "market_registered_count": row[2],
                "policy_applied_count": row[3],
                "ai_tagged_count": row[4],
                "ai_image_count": row[5],
                "tag_applied_count": row[6],
            }

        result = []
        for f in filters:
            data = {c.key: getattr(f, c.key) for c in f.__table__.columns}
            counts = count_map.get(f.id, {})
            data["collected_count"] = counts.get("collected_count", 0)
            data["market_registered_count"] = counts.get("market_registered_count", 0)
            data["policy_applied_count"] = counts.get("policy_applied_count", 0)
            data["ai_tagged_count"] = counts.get("ai_tagged_count", 0)
            data["ai_image_count"] = counts.get("ai_image_count", 0)
            data["tag_applied_count"] = counts.get("tag_applied_count", 0)
            result.append(data)
        return result

    return await cache.get_or_compute("collector:filters:v1", _factory, ttl=60)


@router.post("/filters", status_code=201)
async def create_filter(
    body: SearchFilterCreate,
    session: AsyncSession = Depends(get_write_session_dependency),
    user_id: str = Depends(get_user_id),
):
    svc = _get_services(session)
    data = body.model_dump(exclude_unset=True)
    data["created_by"] = user_id
    result = await svc.create_filter(data)
    await cache.delete("filters:tree:v3")
    await cache.clear_pattern("filters:tree:counts:*")
    return result


@router.put("/filters/{filter_id}")
async def update_filter(
    filter_id: str,
    body: SearchFilterUpdate,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    svc = _get_services(session)
    data = body.model_dump(exclude_unset=True)
    result = await svc.update_filter(filter_id, data)
    if not result:
        raise HTTPException(404, "필터를 찾을 수 없습니다")
    await cache.delete("filters:tree:v3")

    # 정책 적용 시 해당 그룹 상품에 백그라운드 전파 (즉시 응답)
    if "applied_policy_id" in data and data["applied_policy_id"]:
        policy_id = data["applied_policy_id"]

        async def _propagate():
            try:
                from backend.db.orm import get_write_session

                async with get_write_session() as bg_session:
                    from backend.domain.samba.policy.repository import (
                        SambaPolicyRepository,
                    )

                    policy_repo = SambaPolicyRepository(bg_session)
                    policy = await policy_repo.get_async(policy_id)
                    policy_data = None
                    if policy and policy.pricing:
                        pr = policy.pricing if isinstance(policy.pricing, dict) else {}
                        policy_data = {
                            "margin_rate": pr.get("marginRate", 15),
                            "shipping_cost": pr.get("shippingCost", 0),
                            "extra_charge": pr.get("extraCharge", 0),
                            "source_site_margins": pr.get("sourceSiteMargins", {}),
                        }
                    bg_svc = _get_services(bg_session)
                    count = await bg_svc.apply_policy_to_filter_products(
                        filter_id, policy_id, policy_data
                    )
                    await bg_session.commit()
                    logger.info(f"정책 전파 완료: 필터 {filter_id} → {count}개 상품")
            except Exception as e:
                logger.error(f"정책 전파 실패: 필터 {filter_id} → {e}")

        asyncio.create_task(_propagate())

    return result


class BulkApplyPolicyRequest(BaseModel):
    filter_ids: list[str]
    policy_id: str


@router.post("/filters/bulk-apply-policy")
async def bulk_apply_policy(
    body: BulkApplyPolicyRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """여러 그룹에 정책을 한 번에 적용 — 단일 UPDATE + 단일 백그라운드 전파."""
    from sqlalchemy import update as sa_update
    from backend.domain.samba.collector.model import SambaSearchFilter as _SF

    if not body.filter_ids:
        return {"applied": 0}

    # 모든 필터 정책을 단일 쿼리로 일괄 업데이트
    stmt = (
        sa_update(_SF)
        .where(_SF.id.in_(body.filter_ids))
        .values(applied_policy_id=body.policy_id)
    )
    result = await session.exec(stmt)  # type: ignore[arg-type]
    await session.commit()
    applied_count = result.rowcount

    # 단일 백그라운드 태스크로 상품 전파 (개별 호출 시 풀 고갈 방지)
    policy_id = body.policy_id
    filter_ids = list(body.filter_ids)

    async def _propagate_all():
        try:
            from backend.db.orm import get_write_session
            from backend.domain.samba.policy.repository import SambaPolicyRepository

            async with get_write_session() as bg_session:
                policy_repo = SambaPolicyRepository(bg_session)
                policy = await policy_repo.get_async(policy_id)
                policy_data = None
                if policy and policy.pricing:
                    pr = policy.pricing if isinstance(policy.pricing, dict) else {}
                    policy_data = {
                        "margin_rate": pr.get("marginRate", 15),
                        "shipping_cost": pr.get("shippingCost", 0),
                        "extra_charge": pr.get("extraCharge", 0),
                        "source_site_margins": pr.get("sourceSiteMargins", {}),
                    }
                bg_svc = _get_services(bg_session)
                for fid in filter_ids:
                    try:
                        await bg_svc.apply_policy_to_filter_products(
                            fid, policy_id, policy_data
                        )
                    except Exception as e:
                        logger.warning(f"정책 전파 실패 (필터 {fid}): {e}")
                await bg_session.commit()
                logger.info(
                    f"정책 일괄 전파 완료: {len(filter_ids)}개 필터 → policy {policy_id}"
                )
        except Exception as e:
            logger.error(f"정책 일괄 전파 실패: {e}", exc_info=True)

    asyncio.create_task(_propagate_all())
    return {"applied": applied_count}


@router.delete("/filters/{filter_id}")
async def delete_filter(
    filter_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    from sqlalchemy import delete as sa_delete
    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP

    svc = _get_services(session)

    async def _invalidate_filter_caches() -> None:
        # /filters (60s), /filters/tree (300s), /filters/tree/counts:* (300s) 모두 invalidate —
        # 누락 시 UI 가 삭제 직후 몇 분간 stale 그룹 잔존.
        await cache.delete("collector:filters:v1")
        await cache.delete("filters:tree:v3")
        await cache.clear_pattern("filters:tree:counts:*")

    sf = await svc.filter_repo.get_async(filter_id)
    if not sf:
        # row 이미 부재 — idempotent 처리. UI stale 캐시 시 "삭제 실패" 사고 차단.
        logger.info(f"필터 삭제 요청 — row 이미 부재: {filter_id} (idempotent)")
        await _invalidate_filter_caches()
        return {"ok": True, "deleted_products": 0, "already_deleted": True}

    # 마켓등록 상품 체크
    products = await svc.product_repo.list_by_filter(filter_id, limit=100000)
    registered = [
        p for p in products if p.registered_accounts and len(p.registered_accounts) > 0
    ]
    if registered:
        raise HTTPException(
            400, f"마켓등록 상품이 {len(registered)}건 있어서 삭제할 수 없습니다"
        )

    # 상품 벌크 삭제 → 그룹 삭제
    deleted_count = len(products)
    if products:
        await session.execute(sa_delete(_CP).where(_CP.search_filter_id == filter_id))
        logger.info(f"그룹 삭제: {filter_id} → 상품 {deleted_count}건 연동 삭제")

    await svc.delete_filter(filter_id)
    await _invalidate_filter_caches()
    return {"ok": True, "deleted_products": deleted_count}


@router.post("/brands/delete")
async def delete_brand_scope(
    body: BrandDeleteRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    svc = _get_services(session)
    result = await svc.delete_brand_scope(
        source_site=body.source_site,
        brand_name=body.brand_name,
        tenant_id=tenant_id,
    )
    await cache.clear_pattern("products:*")
    await cache.delete("products:counts")
    # 캐시 키 테넌트 분리 — pattern으로 모든 테넌트 캐시 일괄 무효화
    await cache.clear_pattern("products:dashboard-stats-v5:*")
    await cache.delete("products:category-tree")
    return {"ok": True, **result}


@router.delete("/products/orphans")
async def delete_orphan_products(
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """그룹이 삭제되었지만 상품이 남은 고아 상품을 정리."""
    from sqlalchemy import select, delete as sa_delete, and_
    from backend.domain.samba.collector.model import (
        SambaCollectedProduct as _CP,
        SambaSearchFilter as _SF,
    )

    # search_filter_id가 있지만 해당 필터가 존재하지 않는 상품 조회
    existing_filter_ids = select(_SF.id)
    orphan_stmt = select(_CP.id, _CP.search_filter_id, _CP.registered_accounts).where(
        and_(
            _CP.search_filter_id != None,
            _CP.search_filter_id.notin_(existing_filter_ids),
        )
    )
    orphans = (await session.execute(orphan_stmt)).all()

    # 마켓등록 상품은 제외
    registered = [
        o for o in orphans if o.registered_accounts and len(o.registered_accounts) > 0
    ]
    deletable = [
        o
        for o in orphans
        if not o.registered_accounts or len(o.registered_accounts) == 0
    ]

    if deletable:
        del_ids = [o.id for o in deletable]
        await session.execute(sa_delete(_CP).where(_CP.id.in_(del_ids)))
        await session.commit()
        logger.info(
            f"고아 상품 정리: {len(deletable)}건 삭제 (마켓등록 {len(registered)}건 보존)"
        )

    return {
        "ok": True,
        "deleted": len(deletable),
        "preserved_registered": len(registered),
        "total_orphans_found": len(orphans),
    }


def _build_filter_tree(all_filters: list, count_map: dict | None = None) -> list:
    """필터 목록에서 트리 구조를 빌드. count_map 없으면 모든 카운트 0."""
    filter_data = []
    for f in all_filters:
        data = {c.key: getattr(f, c.key) for c in f.__table__.columns}
        counts = count_map.get(f.id, {}) if count_map and not f.is_folder else {}
        data["collected_count"] = counts.get("cnt", 0)
        data["market_registered_count"] = counts.get("market_registered", 0)
        data["ai_tagged_count"] = counts.get("ai_tagged", 0)
        data["ai_image_count"] = counts.get("ai_image", 0)
        data["tag_applied_count"] = counts.get("tag_applied", 0)
        data["policy_applied_count"] = counts.get("policy_applied", 0)
        filter_data.append(data)

    by_id = {f["id"]: f for f in filter_data}
    roots: list = []
    orphans_by_site: dict[str, list] = {}
    for f in filter_data:
        f["children"] = []
    for f in filter_data:
        pid = f.get("parent_id")
        if pid and pid in by_id:
            by_id[pid]["children"].append(f)
        elif not pid:
            if f.get("is_folder"):
                roots.append(f)
            else:
                site = f.get("source_site") or "기타"
                orphans_by_site.setdefault(site, []).append(f)

    existing_site_folders = {r["source_site"]: r for r in roots if r.get("is_folder")}
    for site, orphans in orphans_by_site.items():
        if site in existing_site_folders:
            existing_site_folders[site]["children"].extend(orphans)
        else:
            virtual = {
                "id": f"__virtual_{site}",
                "source_site": site,
                "name": site,
                "is_folder": True,
                "children": orphans,
                "collected_count": 0,
            }
            roots.append(virtual)
    return roots


@router.get("/filters/tree")
async def get_filter_tree(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """검색그룹 트리 구조 반환 (카운트 없음). 소싱처 클릭 시 /filters/tree/counts로 카운트 로드."""
    cached = await cache.get("filters:tree:v3")
    if cached:
        return cached

    svc = _get_services(session)
    all_filters = await svc.list_filters(limit=10000)
    roots = _build_filter_tree(all_filters)
    await cache.set("filters:tree:v3", roots, ttl=300)
    return roots


@router.get("/filters/tree/counts")
async def get_filter_tree_counts(
    source_site: str | None = None,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """leaf 필터 카운트 반환. source_site 미지정 시 전체 사이트 통합 집계.

    초기 로드시 단일 호출로 모든 사이트의 카운트를 prefetch 하기 위함 —
    이전엔 사이트별 lazy load 만 가능해 그룹 클릭 전엔 (0) 으로 표기되는
    UX 문제. GROUP BY 쿼리 한 번이 N 개 사이트별 호출보다 효율적.
    """
    cache_key = f"filters:tree:counts:{source_site or '__all__'}"
    cached = await cache.get(cache_key)
    if cached:
        return cached

    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP
    from sqlalchemy import func as _func, case, and_, literal, text as _text

    _AI_TAGGED_JSONB = _text("'[\"__ai_tagged__\"]'::jsonb")
    _AI_IMAGE_JSONB = _text("'[\"__ai_image__\"]'::jsonb")

    svc = _get_services(session)
    all_filters = await svc.list_filters(limit=10000)
    if source_site is None:
        leaf_ids = [f.id for f in all_filters if not f.is_folder]
    else:
        leaf_ids = [
            f.id
            for f in all_filters
            if not f.is_folder and f.source_site == source_site
        ]

    if not leaf_ids:
        return {}

    count_stmt = (
        select(
            _CP.search_filter_id,
            _func.count().label("cnt"),
            _func.count(case((has_registered_accounts(_CP), literal(1)))).label(
                "market_registered"
            ),
            _func.count(
                case(
                    (
                        _CP.tags.op("@>")(_AI_TAGGED_JSONB),
                        literal(1),
                    )
                )
            ).label("ai_tagged"),
            _func.count(
                case(
                    (
                        _CP.tags.op("@>")(_AI_IMAGE_JSONB),
                        literal(1),
                    )
                )
            ).label("ai_image"),
            _func.count(
                case(
                    (
                        and_(
                            _CP.tags.isnot(None),
                            _func.jsonb_typeof(_CP.tags) == "array",
                            _func.jsonb_array_length(_CP.tags) > 0,
                        ),
                        literal(1),
                    )
                )
            ).label("tag_applied"),
            _func.count(case((_CP.applied_policy_id != None, literal(1)))).label(  # noqa: E711
                "policy_applied"
            ),
        )
        .where(_CP.search_filter_id.in_(leaf_ids))
        .group_by(_CP.search_filter_id)
    )
    count_result = await session.execute(count_stmt)
    counts: dict[str, dict] = {}
    for row in count_result.all():
        counts[row[0]] = {
            "collected_count": row[1],
            "market_registered_count": row[2],
            "ai_tagged_count": row[3],
            "ai_image_count": row[4],
            "tag_applied_count": row[5],
            "policy_applied_count": row[6],
        }
    await cache.set(cache_key, counts, ttl=300)
    return counts


@router.post("/filters/folder", status_code=201)
async def create_folder(
    body: FolderCreateRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """폴더(분류) 노드 생성."""
    svc = _get_services(session)
    data = {
        "source_site": body.source_site,
        "name": body.name,
        "parent_id": body.parent_id,
        "is_folder": True,
        "requested_count": 0,
    }
    return await svc.create_filter(data)


@router.patch("/filters/{filter_id}/move")
async def move_filter(
    filter_id: str,
    body: MoveFilterRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """필터/폴더를 다른 폴더로 이동."""
    svc = _get_services(session)
    result = await svc.update_filter(filter_id, {"parent_id": body.parent_id})
    if not result:
        raise HTTPException(404, "필터를 찾을 수 없습니다")
    return result


# ── Collected Products ──


def _split_product_ids(q: str) -> list[str] | None:
    """검색어에 콤마(,) 가 있으면 다중 상품번호로 분할.

    Returns: 콤마 split 결과(공백 strip + 빈 항목 제외) 또는 None(콤마 없음/유효 항목 0개).
    """
    if "," not in q:
        return None
    ids = [s.strip() for s in q.split(",") if s.strip()]
    return ids if ids else None


@router.get("/products/scroll")
async def scroll_products(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=10000),
    search: str = Query("", max_length=2000),
    search_type: str = Query("name"),
    source_site: Optional[str] = None,
    source_sites: Optional[str] = None,
    status: Optional[str] = None,
    sold_out_filter: Optional[str] = None,
    ai_filter: Optional[str] = None,
    search_filter_id: Optional[str] = None,
    sort_by: str = Query("collect-desc"),
    ids_only: bool = Query(False),
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """서버사이드 필터/정렬/페이지네이션 — 무한스크롤용.

    ids_only=True이면 {ids: [...], total: int}만 반환 (경량).
    Returns: {items: [...], total: int, sites: [str]}
    """
    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP
    from sqlalchemy import (
        func,
        cast,
        String,
        and_,
        text,
        select,
        inspect as _sa_inspect,
    )

    mapper = _sa_inspect(_CP)

    # 기본 조건 — 테넌트 격리 강제 (projection/count 쿼리는 ORM 자동 필터 우회됨)
    conditions = []
    if tenant_id is not None:
        conditions.append(_CP.tenant_id == tenant_id)

    # 텍스트 검색
    q = search.strip()
    if q:
        # search 는 외부 입력 — `%`/`_` 메타 escape 후 ESCAPE '\\' 명시.
        from backend.core.sql_safe import escape_like

        q_pat = f"%{escape_like(q)}%"
        q_no_space_pat = f"%{escape_like(q.replace(' ', ''))}%"

        # 상품번호 다중 입력(콤마) 지원 — split 결과가 있으면 IN, 없으면 단일 ilike
        _multi_ids = _split_product_ids(q)
        _site_id_clause = (
            _CP.site_product_id.in_(_multi_ids)
            if _multi_ids
            else _CP.site_product_id.ilike(q_pat, escape="\\")
        )

        if search_type == "name":
            # 원상품명 + 등록상품명 + 마켓등록명 통합 부분 일치 (공백 무시)
            conditions.append(
                or_(
                    _CP.name.ilike(q_pat, escape="\\"),
                    func.replace(_CP.name, " ", "").ilike(q_no_space_pat, escape="\\"),
                    _CP.name_en.ilike(q_pat, escape="\\"),
                    func.replace(func.coalesce(_CP.name_en, ""), " ", "").ilike(
                        q_no_space_pat, escape="\\"
                    ),
                    func.coalesce(cast(_CP.market_names, String), "").ilike(
                        q_pat, escape="\\"
                    ),
                    func.coalesce(_CP.brand, "").ilike(q_pat, escape="\\"),
                    func.coalesce(_CP.style_code, "").ilike(q_pat, escape="\\"),
                    _site_id_clause,
                )
            )
        elif search_type == "name_all":
            # 상품명 + 등록상품명 구성 요소(brand/style_code/site_product_id 포함) 동시 검색
            # market_names 포함 — 셀하 등 마켓 등록명으로 검색 시 누락 방지
            conditions.append(
                or_(
                    _CP.name.ilike(q_pat, escape="\\"),
                    func.replace(_CP.name, " ", "").ilike(q_no_space_pat, escape="\\"),
                    _CP.name_en.ilike(q_pat, escape="\\"),
                    func.replace(func.coalesce(_CP.name_en, ""), " ", "").ilike(
                        q_no_space_pat, escape="\\"
                    ),
                    func.coalesce(cast(_CP.market_names, String), "").ilike(
                        q_pat, escape="\\"
                    ),
                    func.coalesce(_CP.brand, "").ilike(q_pat, escape="\\"),
                    func.coalesce(_CP.style_code, "").ilike(q_pat, escape="\\"),
                    _site_id_clause,
                )
            )
        elif search_type == "no":
            conditions.append(_site_id_clause)
        elif search_type == "filter":
            # 검색필터 이름으로 검색 → search_filter_id 서브쿼리
            from backend.domain.samba.collector.model import SambaSearchFilter as _SF

            sf_ids = select(_SF.id).where(_SF.name.ilike(q_pat, escape="\\"))
            conditions.append(_CP.search_filter_id.in_(sf_ids))
        elif search_type == "brand":
            conditions.append(_CP.brand.ilike(q_pat, escape="\\"))
        elif search_type == "id":
            conditions.append(_CP.id == q)
        elif search_type == "policy":
            from backend.domain.samba.policy.model import SambaPolicy as _POL

            pol_ids = select(_POL.id).where(_POL.name.ilike(q_pat, escape="\\"))
            conditions.append(_CP.applied_policy_id.in_(pol_ids))

    # 소싱처 필터 (단일 또는 복수)
    if source_sites:
        sites_list = [s.strip() for s in source_sites.split(",") if s.strip()]
        if sites_list:
            conditions.append(_CP.source_site.in_(sites_list))
    elif source_site:
        conditions.append(_CP.source_site == source_site)

    # 그룹(검색필터) 필터
    if search_filter_id:
        conditions.append(_CP.search_filter_id == search_filter_id)

    # 상태 필터
    # ※ "market_registered/market_unregistered"는 registered_accounts(실제 마켓 등록 계정) 기준
    #    "registered/collected/saved"는 상품 처리 상태(status 컬럼) 기준 — 혼동 주의
    _KNOWN_STATUS_VALUES = {"collected", "saved", "registered"}
    if status == "has_orders":
        from backend.api.v1.routers.samba.collector_common import (
            build_has_orders_conditions,
        )

        conditions.extend(await build_has_orders_conditions(session, _CP))
    elif status == "free_ship":
        conditions.append(_CP.free_shipping == True)
    elif status == "same_day":
        conditions.append(_CP.same_day_delivery == True)
    elif status == "free_same":
        conditions.append(_CP.free_shipping == True)
        conditions.append(_CP.same_day_delivery == True)
    elif status == "market_registered":
        # 마켓등록상품 공통 조건 (registered_accounts + market_product_nos)
        from backend.api.v1.routers.samba.collector_common import (
            build_market_registered_conditions,
        )

        conditions.extend(build_market_registered_conditions(_CP))
    elif status == "market_unregistered":
        conditions.append(no_registered_accounts(_CP))
    elif status == "sold_out":
        conditions.append(
            or_(_CP.sale_status == "sold_out", _all_options_sold_out(_CP))
        )
    elif status and status.startswith("mtype_reg_"):
        # 마켓타입별 등록 필터: 해당 마켓타입의 계정 중 하나라도 등록된 상품 (JSONB @>)
        market_type = status[10:]
        from backend.domain.samba.account.model import SambaMarketAccount as _MA

        acc_result = await session.execute(
            select(_MA.id).where(_MA.market_type == market_type, _MA.is_active == True)
        )
        acc_ids = acc_result.scalars().all()
        if acc_ids:
            conditions.append(
                or_(
                    *[
                        _CP.registered_accounts.op("@>")(func.jsonb_build_array(aid))
                        for aid in acc_ids
                    ]
                )
            )
        else:
            conditions.append(text("1=0"))
    elif status and status.startswith("mtype_unreg_"):
        # 마켓타입별 미등록 필터: 해당 마켓타입의 계정이 하나도 등록되지 않은 상품 (JSONB @>)
        market_type = status[12:]
        from backend.domain.samba.account.model import SambaMarketAccount as _MA

        acc_result = await session.execute(
            select(_MA.id).where(_MA.market_type == market_type, _MA.is_active == True)
        )
        acc_ids = acc_result.scalars().all()
        if acc_ids:
            # jsonb_array_length 금지 — 스칼라값 행에서 에러. no_registered_accounts 헬퍼 사용
            conditions.append(
                or_(
                    no_registered_accounts(_CP),
                    and_(
                        *[
                            ~_CP.registered_accounts.op("@>")(
                                func.jsonb_build_array(aid)
                            )
                            for aid in acc_ids
                        ]
                    ),
                )
            )
    elif status and status.startswith("reg_"):
        # 특정 계정에 등록된 상품: registered_accounts JSONB에 account_id 포함 (@>)
        account_id = status[4:]  # "reg_ma_xxx" → "ma_xxx"
        conditions.append(
            _CP.registered_accounts.op("@>")(func.jsonb_build_array(account_id))
        )
    elif status and status.startswith("unreg_"):
        # 특정 계정에 미등록된 상품: registered_accounts JSONB에 account_id 미포함 (~@>)
        account_id = status[6:]  # "unreg_ma_xxx" → "ma_xxx"
        # jsonb_array_length 금지 — 스칼라값 행에서 에러. no_registered_accounts 헬퍼 사용
        conditions.append(
            or_(
                no_registered_accounts(_CP),
                ~_CP.registered_accounts.op("@>")(func.jsonb_build_array(account_id)),
            )
        )
    elif status and status in _KNOWN_STATUS_VALUES:
        conditions.append(_CP.status == status)

    # 품절 독립 필터 — status 필터와 AND 조합
    if sold_out_filter == "sold_out":
        conditions.append(
            or_(_CP.sale_status == "sold_out", _all_options_sold_out(_CP))
        )
    elif sold_out_filter == "not_sold_out":
        conditions.append(
            and_(
                _CP.sale_status != "sold_out",
                ~_all_options_sold_out(_CP),
            )
        )

    # AI 필터 (JSONB @> 연산자 — GIN 인덱스 활용)
    # cast(literal, JSONB)는 asyncpg JSONB 코덱이 문자열을 JSON 문자열로 인코딩해
    # tags(array) @> "json string" 형태가 되어 절대 매칭 안 됨 → 필터 무력화 버그.
    # text() 인라인 ::jsonb 캐스트로 PG가 직접 파싱하도록 강제 (line 693 패턴과 동일).
    from sqlalchemy import text as _text_jsonb

    _ai_tag = _text_jsonb("'[\"__ai_tagged__\"]'::jsonb")
    _ai_img = _text_jsonb("'[\"__ai_image__\"]'::jsonb")
    _img_filtered = _text_jsonb("'[\"__img_filtered__\"]'::jsonb")
    _img_edited = _text_jsonb("'[\"__img_edited__\"]'::jsonb")
    if ai_filter == "sold_out":
        conditions.append(
            or_(_CP.sale_status == "sold_out", _all_options_sold_out(_CP))
        )
    elif ai_filter == "ai_tag_yes":
        conditions.append(_CP.tags.op("@>")(_ai_tag))
    elif ai_filter == "ai_tag_no":
        conditions.append(or_(_CP.tags.is_(None), ~_CP.tags.op("@>")(_ai_tag)))
    elif ai_filter == "ai_img_yes":
        conditions.append(_CP.tags.op("@>")(_ai_img))
    elif ai_filter == "ai_img_no":
        conditions.append(or_(_CP.tags.is_(None), ~_CP.tags.op("@>")(_ai_img)))
    elif ai_filter == "filter_yes":
        conditions.append(_CP.tags.op("@>")(_img_filtered))
    elif ai_filter == "filter_no":
        conditions.append(or_(_CP.tags.is_(None), ~_CP.tags.op("@>")(_img_filtered)))
    elif ai_filter == "img_edit_yes":
        conditions.append(_CP.tags.op("@>")(_img_edited))
    elif ai_filter == "img_edit_no":
        conditions.append(or_(_CP.tags.is_(None), ~_CP.tags.op("@>")(_img_edited)))
    elif ai_filter == "video_yes":
        conditions.append(_CP.video_url.isnot(None))
        conditions.append(_CP.video_url != "")
    elif ai_filter == "video_no":
        conditions.append(or_(_CP.video_url.is_(None), _CP.video_url == ""))
    elif ai_filter == "has_orders":
        from backend.api.v1.routers.samba.collector_common import (
            build_has_orders_conditions,
        )

        conditions.extend(await build_has_orders_conditions(session, _CP))

    # ids_only 모드: ID만 반환 (검색결과전송 최적화)
    # COUNT 쿼리 제거 — 프론트는 ids만 사용, total은 len(ids)로 계산
    if ids_only:
        id_stmt = select(_CP.id)
        for c in conditions:
            id_stmt = id_stmt.where(c)
        ids_result = await session.execute(id_stmt)
        ids = [r[0] for r in ids_result.all()]
        return {"ids": ids, "total": len(ids)}

    # 목록에 필요한 컬럼 선택 (heavy 필드만 제외)
    list_cols = [c for c in mapper.columns if c.key not in _HEAVY_FIELDS]

    # COUNT + 데이터 + 소싱처 + KPI 병렬 실행
    count_stmt = select(func.count()).select_from(_CP)
    for c in conditions:
        count_stmt = count_stmt.where(c)

    # 소싱처 목록 (캐시 TTL 5분, 테넌트별 분리)
    sites_cache_key = f"products:sites:{tenant_id or 'global'}"
    sites = await cache.get(sites_cache_key)
    sites_stmt = None
    if not sites:
        sites_stmt = (
            select(_CP.source_site).distinct().where(_CP.source_site.isnot(None))
        )
        if tenant_id is not None:
            sites_stmt = sites_stmt.where(_CP.tenant_id == tenant_id)

    # KPI 카운트 (캐시 TTL 5분, 테넌트별 분리)
    counts_cache_key = f"products:counts:{tenant_id or 'global'}"
    counts = await cache.get(counts_cache_key)
    counts_stmt = None
    if not counts:
        from sqlalchemy import case, literal

        counts_stmt = select(
            func.count().label("total"),
            func.count(case((has_registered_accounts(_CP), literal(1)))).label(
                "registered"
            ),
            func.count(case((_CP.applied_policy_id != None, literal(1)))).label(  # noqa: E711
                "policy_applied"
            ),
            func.count(case((_CP.sale_status == "sold_out", literal(1)))).label(
                "sold_out"
            ),
        ).select_from(_CP)
        if tenant_id is not None:
            counts_stmt = counts_stmt.where(_CP.tenant_id == tenant_id)

    # 데이터 쿼리
    data_stmt = select(*list_cols)
    for c in conditions:
        data_stmt = data_stmt.where(c)

    # 정렬
    if sort_by == "collect-asc":
        data_stmt = data_stmt.order_by(_CP.created_at.asc())
    elif sort_by == "update-desc":
        # 오토튠 점검 시각(last_refreshed_at) 기준 — 가격/재고 변경이 없어도 매 사이클 갱신
        data_stmt = data_stmt.order_by(
            func.coalesce(_CP.last_refreshed_at, _CP.updated_at).desc().nullslast(),
            _CP.created_at.desc(),
        )
    elif sort_by == "update-asc":
        data_stmt = data_stmt.order_by(
            func.coalesce(_CP.last_refreshed_at, _CP.updated_at).asc().nullsfirst(),
            _CP.created_at.asc(),
        )
    else:
        data_stmt = data_stmt.order_by(_CP.created_at.desc())

    data_stmt = data_stmt.offset(skip).limit(limit)

    # 병렬 실행: 메인 세션은 count + data 직렬, sites/counts는 별도 read 세션
    # (asyncpg는 같은 세션에서 병렬 쿼리 불가 — 별도 세션으로 우회)
    async def _main_query() -> tuple[int, list[Any]]:
        c_res = await session.execute(count_stmt)
        d_res = await session.execute(data_stmt)
        return (c_res.scalar() or 0, d_res.mappings().all())

    async def _side_query(stmt: Any) -> Any:
        async with get_read_session() as s:
            return await s.execute(stmt)

    tasks: list[Any] = [_main_query()]
    side_indices: dict[str, int] = {}
    if sites_stmt is not None:
        side_indices["sites"] = len(tasks)
        tasks.append(_side_query(sites_stmt))
    if counts_stmt is not None:
        side_indices["counts"] = len(tasks)
        tasks.append(_side_query(counts_stmt))

    results = await asyncio.gather(*tasks)
    total, rows = results[0]

    if "sites" in side_indices:
        sites_result = results[side_indices["sites"]]
        sites = sorted([r[0] for r in sites_result.all() if r[0]])
        await cache.set(sites_cache_key, sites, ttl=300)
    if "counts" in side_indices:
        counts_row = results[side_indices["counts"]].one()
        counts = {
            "total": counts_row.total,
            "registered": counts_row.registered,
            "policy_applied": counts_row.policy_applied,
            "sold_out": counts_row.sold_out,
        }
        await cache.set(counts_cache_key, counts, ttl=300)

    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "sites": sites,
        "counts": counts
        or {"total": 0, "registered": 0, "policy_applied": 0, "sold_out": 0},
    }


@router.get("/products/init-data")
async def products_init_data(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """상품관리 페이지 초기 데이터 통합 API — 8개 API를 1개로 병합.

    Returns: { policies, filters, deletion_words, accounts, order_product_ids,
               name_rules, category_mappings, detail_templates }
    각 섹션이 독립적으로 try/except 처리 — 부분 실패 시에도 나머지 데이터 반환.
    """
    from backend.domain.samba.policy.model import SambaPolicy
    from backend.domain.samba.collector.model import SambaSearchFilter as _SF
    from backend.domain.samba.forbidden.model import SambaForbiddenWord
    from backend.domain.samba.account.model import SambaMarketAccount
    from backend.domain.samba.order.model import SambaOrder

    # SQLModel 인스턴스를 dict로 변환
    def to_dict(obj):
        if isinstance(obj, dict):
            return obj
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "__dict__"):
            d = {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
            return d
        return obj

    # 기본값 초기화 (각 섹션 실패 시 빈 배열 반환)
    policies: list = []
    filters: list = []
    words: list = []
    accounts: list = []
    mappings: list = []
    order_pids: list = []
    rules: list = []
    templates: list = []

    # 1차: 핵심 메타데이터 (정책/필터/금지어/계정/카테고리) 병렬 조회
    try:
        # 카테고리 매핑은 캐시 우선 (변경 빈도 낮음 — TTL 5분)
        mappings = await cache.get("init_data:category_mappings") or []

        pol_r = await session.execute(select(SambaPolicy).limit(50))
        # filters 는 frontend 에서 id↔name 매핑 + target_mappings(카테고리 fallback)
        # 만 사용 — 전체 컬럼 select 시 keyword/timestamp 등이 응답의 76% 차지.
        # 카드 렌더에 불필요한 필드 제외. 다른 페이지가 전체 필드를 필요로 하면
        # /collector/filters 또는 /collector/filters/tree 별도 호출.
        filter_r = await session.execute(
            select(_SF.id, _SF.name, _SF.target_mappings).where(_SF.is_folder == False)  # noqa: E712
        )
        words_r = await session.execute(
            select(SambaForbiddenWord).where(
                SambaForbiddenWord.type == "deletion",
                SambaForbiddenWord.is_active == True,
            )
        )
        accs_r = await session.execute(
            select(SambaMarketAccount).where(SambaMarketAccount.is_active == True)
        )

        if not mappings:
            from backend.domain.samba.category.model import SambaCategoryMapping

            map_r = await session.execute(select(SambaCategoryMapping).limit(2000))
            mappings = [to_dict(r) for r in map_r.scalars().all()]
            await cache.set("init_data:category_mappings", mappings, ttl=300)

        policies = [to_dict(r) for r in pol_r.scalars().all()]
        # filter_r 은 select(id, name, target_mappings) 의 Row 튜플 — scalars() 사용 불가
        filters = [
            {"id": row[0], "name": row[1], "target_mappings": row[2]}
            for row in filter_r.all()
        ]
        words = [r.word for r in words_r.scalars().all()]
        accounts = [to_dict(r) for r in accs_r.scalars().all()]
    except Exception as e:
        logger.exception(f"[init-data] 핵심 메타데이터 조회 실패: {e}")

    # 2차: order_pids (캐시 우선, 주문 테이블 풀 스캔 방지 — 5분 TTL)
    try:
        order_pids = await cache.get("init_data:order_pids") or []
        if not order_pids:
            order_r = await session.execute(
                select(SambaOrder.product_id)
                .where(SambaOrder.product_id.isnot(None))
                .distinct()
            )
            order_pids = [r[0] for r in order_r.all()]
            await cache.set("init_data:order_pids", order_pids, ttl=300)
    except Exception as e:
        logger.exception(f"[init-data] order_pids 조회 실패: {e}")

    # 3차: name_rules + detail_templates (policy 도메인)
    try:
        from backend.domain.samba.policy.model import SambaNameRule, SambaDetailTemplate

        rules_r = await session.execute(select(SambaNameRule))
        tpl_r = await session.execute(select(SambaDetailTemplate))
        rules = [to_dict(r) for r in rules_r.scalars().all()]
        templates = [to_dict(r) for r in tpl_r.scalars().all()]
    except Exception as e:
        logger.exception(f"[init-data] name_rules/detail_templates 조회 실패: {e}")

    return {
        "policies": policies,
        "filters": filters,
        "deletion_words": words,
        "accounts": accounts,
        "order_product_ids": order_pids,
        "name_rules": rules,
        "category_mappings": mappings,
        "detail_templates": templates,
    }


@router.get("/products/counts")
async def product_counts(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """상품 카운트 통계 (대시보드용) — 10만건이어도 즉시 응답."""
    # 캐시 조회 (TTL 30초)
    cached = await cache.get("products:counts")
    if cached:
        return cached

    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP
    from sqlalchemy import func, case, literal

    stmt = select(
        func.count().label("total"),
        func.count(case((has_registered_accounts(_CP), literal(1)))).label(
            "registered"
        ),
        func.count(case((_CP.applied_policy_id != None, literal(1)))).label(
            "policy_applied"
        ),
        func.count(case((_CP.sale_status == "sold_out", literal(1)))).label("sold_out"),
    ).select_from(_CP)
    row = (await session.execute(stmt)).one()
    result = {
        "total": row.total,
        "registered": row.registered,
        "policy_applied": row.policy_applied,
        "sold_out": row.sold_out,
    }
    await cache.set("products:counts", result, ttl=300)
    return result


@router.get("/products/dashboard-stats")
async def product_dashboard_stats(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """대시보드 현황판 — 소싱처별 수집현황 + 마켓/계정별 등록현황 (브랜드별 breakdown 포함).

    기존 4개 풀스캔 + 직렬 실행 → 2개 풀스캔 + 별도 세션 병렬화로 응답시간 단축.
    site_stmt(전체)는 brand_site 집계에서 Python 합산으로 도출. acct 도 동일.
    """
    import asyncio
    from collections import defaultdict

    from backend.db.orm import get_read_session
    from backend.domain.samba.account.model import SambaMarketAccount as _MA
    from sqlalchemy import text

    # 캐시 키 테넌트 분리 — 운영자/임성희 등이 같은 캐시 공유하면 격리 깨짐
    cache_key = f"products:dashboard-stats-v5:{tenant_id or 'global'}"
    cached = await cache.get(cache_key)
    if cached:
        return cached

    # 별도 세션 병렬 실행 헬퍼 — asyncpg 동일 세션 gather 금지 (CLAUDE.md).
    async def _run_brand_site():
        async with get_read_session() as s:
            # 등록 판정은 build_market_registered_conditions와 동일 조건:
            #   registered_accounts != '[]'::jsonb (array) AND market_product_nos NOT NULL/null/{}
            # registered_accounts만 검사하면 옛 데이터(빈 객체/스칼라)가 통과해 과대 집계됨.
            stmt = text("""
                SELECT source_site,
                       COALESCE(NULLIF(TRIM(brand), ''), '기타') AS brand_name,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (
                         WHERE registered_accounts IS NOT NULL
                           AND jsonb_typeof(registered_accounts) = 'array'
                           AND registered_accounts != '[]'::jsonb
                           AND market_product_nos IS NOT NULL
                           AND market_product_nos::text != 'null'
                           AND market_product_nos::text != '{}'
                       ) AS registered,
                       COUNT(*) FILTER (WHERE sale_status = 'sold_out') AS sold_out
                FROM samba_collected_product
                WHERE source_site IS NOT NULL AND source_site != ''
                  AND (:tid IS NULL OR tenant_id = :tid)
                GROUP BY source_site, COALESCE(NULLIF(TRIM(brand), ''), '기타')
                ORDER BY source_site, total DESC
            """).bindparams(tid=tenant_id)
            return (await s.execute(stmt)).all()

    async def _run_brand_acct():
        async with get_read_session() as s:
            stmt = text("""
                SELECT aid,
                       source_site,
                       COALESCE(NULLIF(TRIM(brand), ''), '기타') AS brand_name,
                       COUNT(*) AS cnt
                FROM (
                    SELECT jsonb_array_elements_text(registered_accounts) AS aid,
                           source_site,
                           brand
                    FROM (
                        SELECT registered_accounts, source_site, brand
                        FROM samba_collected_product
                        WHERE registered_accounts IS NOT NULL
                          AND registered_accounts != '[]'::jsonb
                          AND jsonb_typeof(registered_accounts) = 'array'
                          AND (:tid IS NULL OR tenant_id = :tid)
                    ) safe_rows
                ) sub
                GROUP BY aid, source_site, COALESCE(NULLIF(TRIM(brand), ''), '기타')
                ORDER BY aid, cnt DESC
            """).bindparams(tid=tenant_id)
            return (await s.execute(stmt)).all()

    async def _run_sold():
        async with get_read_session() as s:
            stmt = text("""
                SELECT channel_id, COUNT(DISTINCT collected_product_id) AS sold_cnt
                FROM samba_order
                WHERE collected_product_id IS NOT NULL
                  AND channel_id IS NOT NULL
                  AND COALESCE(paid_at, created_at) >= NOW() - INTERVAL '30 days'
                  AND (:tid IS NULL OR tenant_id = :tid)
                GROUP BY channel_id
            """).bindparams(tid=tenant_id)
            rows = (await s.execute(stmt)).all()
            return {r.channel_id: int(r.sold_cnt) for r in rows}

    # 3개 무거운 쿼리 병렬 실행 — return_exceptions 로 부분 실패 허용
    brand_site_rows, brand_acct_rows, sold_by_acct = await asyncio.gather(
        _run_brand_site(),
        _run_brand_acct(),
        _run_sold(),
        return_exceptions=True,
    )
    if isinstance(brand_site_rows, Exception):
        logger.warning("대시보드 brand_site 조회 실패: %s", brand_site_rows)
        brand_site_rows = []
    if isinstance(brand_acct_rows, Exception):
        logger.warning("대시보드 brand_acct 조회 실패: %s", brand_acct_rows)
        brand_acct_rows = []
    if isinstance(sold_by_acct, Exception):
        logger.warning("대시보드 sold 조회 실패: %s", sold_by_acct)
        sold_by_acct = {}

    # 소싱처별 합계는 brand_site 집계에서 Python 합산으로 도출 — 추가 쿼리 불필요
    brand_by_source: dict[str, list[dict]] = defaultdict(list)
    site_totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "registered": 0, "sold_out": 0}
    )
    for r in brand_site_rows:
        brand_by_source[r.source_site].append(
            {
                "brand": r.brand_name,
                "total": r.total,
                "registered": r.registered,
                "sold_out": r.sold_out,
            }
        )
        site_totals[r.source_site]["total"] += r.total
        site_totals[r.source_site]["registered"] += r.registered
        site_totals[r.source_site]["sold_out"] += r.sold_out

    by_source = sorted(
        [
            {
                "source_site": site,
                "total": tot["total"],
                "registered": tot["registered"],
                "sold_out": tot["sold_out"],
                "brands": brand_by_source.get(site, []),
            }
            for site, tot in site_totals.items()
        ],
        key=lambda x: x["total"],
        reverse=True,
    )

    # 계정별 합계도 brand_acct 에서 Python 합산
    brand_by_acct: dict[str, list[dict]] = defaultdict(list)
    acct_totals: dict[str, int] = defaultdict(int)
    for r in brand_acct_rows:
        brand_by_acct[r.aid].append(
            {
                "source_site": r.source_site,
                "brand": r.brand_name,
                "registered": r.cnt,
            }
        )
        acct_totals[r.aid] += r.cnt

    # 계정 ID → 마켓명/계정라벨 매핑 (작은 쿼리, 메인 세션 사용)
    acct_ids = list(acct_totals.keys())
    acct_map: dict[str, dict[str, str]] = {}
    if acct_ids:
        try:
            ma_stmt = select(_MA.id, _MA.market_name, _MA.account_label).where(
                _MA.id.in_(acct_ids)
            )
            ma_rows = (await session.execute(ma_stmt)).all()
            for m in ma_rows:
                acct_map[m.id] = {
                    "market_name": m.market_name,
                    "account_label": m.account_label,
                }
        except Exception as e:
            logger.warning("대시보드 계정 매핑 조회 실패: %s", e)

    by_account = sorted(
        [
            {
                "account_id": aid,
                "market_name": acct_map.get(aid, {}).get("market_name", "알 수 없음"),
                "account_label": acct_map.get(aid, {}).get("account_label", ""),
                "registered": cnt,
                "sold_products": sold_by_acct.get(aid, 0),
                "brands": brand_by_acct.get(aid, []),
            }
            for aid, cnt in acct_totals.items()
        ],
        key=lambda x: x["registered"],
        reverse=True,
    )

    result = {"by_source": by_source, "by_account": by_account}
    await cache.set(cache_key, result, ttl=600)
    return result


@router.get("/products/category-tree")
async def product_category_tree(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """소싱처별 카테고리 트리 (카테고리매핑용) — 상품 전체 로드 없이 GROUP BY."""
    # 캐시 조회 (TTL 5분)
    cached = await cache.get("products:category-tree")
    if cached:
        return cached

    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP
    from sqlalchemy import func

    stmt = (
        select(
            _CP.source_site,
            _CP.category,
            func.count().label("cnt"),
        )
        .where(
            _CP.source_site != None,
            _CP.category != None,
            _CP.category != "",
            # fallback 카테고리 제외 (category가 source_site와 동일한 경우)
            _CP.category != _CP.source_site,
        )
        .group_by(_CP.source_site, _CP.category)
        .order_by(_CP.source_site, _CP.category)
    )
    rows = (await session.execute(stmt)).all()
    result = [{"source_site": r[0], "category": r[1], "count": r[2]} for r in rows]
    await cache.set("products:category-tree", result, ttl=300)
    return result


@router.get("/products")
async def list_collected_products(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100000),
    status: Optional[str] = None,
    source_site: Optional[str] = None,
    category: Optional[str] = None,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP
    from sqlalchemy import inspect as _sa_inspect

    # DB 레벨에서 무거운 컬럼 제외하여 조회 (응답 크기 93% 절감)
    mapper = _sa_inspect(_CP)
    light_cols = [c for c in mapper.columns if c.key not in _HEAVY_FIELDS]

    stmt = select(*light_cols)
    if status:
        stmt = stmt.where(_CP.status == status)
    if source_site:
        stmt = stmt.where(_CP.source_site == source_site)
    if category:
        # prefix 매칭: "여성" → "여성" 또는 "여성 > ..." 모두 포함
        stmt = stmt.where(
            (_CP.category == category) | (_CP.category.startswith(category + " > "))
        )
    stmt = stmt.order_by(_CP.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(stmt)
    rows = result.mappings().all()
    return [dict(r) for r in rows]


@router.post("/products/by-ids")
async def get_products_by_ids(
    body: dict[str, Any],
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """ID 리스트로 상품 조회 (light 컬럼만)."""
    ids = body.get("ids", [])
    if not ids:
        return []
    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP
    from sqlalchemy import inspect as _sa_inspect

    mapper = _sa_inspect(_CP)
    light_cols = [c for c in mapper.columns if c.key not in _HEAVY_FIELDS]
    stmt = select(*light_cols).where(_CP.id.in_(ids))
    result = await session.execute(stmt)
    rows = result.mappings().all()
    return [dict(r) for r in rows]


@router.get("/products/with-orders")
async def get_product_ids_with_orders(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """주문 이력이 있는 상품 ID 목록 조회."""
    from sqlmodel import text

    result = await session.execute(
        text(
            "SELECT DISTINCT product_id FROM samba_order "
            "WHERE product_id IS NOT NULL "
            "  AND (:tid IS NULL OR tenant_id = :tid)"
        ).bindparams(tid=tenant_id)
    )
    return [row[0] for row in result.all()]


@router.get("/products/search")
async def search_collected_products(
    q: str = Query(..., min_length=1),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_read_session_dependency),
):
    svc = _get_services(session)
    return await svc.search_collected_products(q, limit)


@router.get("/products/{product_id}")
async def get_collected_product(
    product_id: str,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    svc = _get_services(session)
    p = await svc.get_collected_product(product_id)
    if not p:
        raise HTTPException(404, "상품을 찾을 수 없습니다")
    return p


@router.get("/products/{product_id}/price-history")
async def get_price_history(
    product_id: str,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """가격변경이력만 경량 조회 (price_history 컬럼만 SELECT)."""
    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP
    from sqlalchemy import select as _sel

    # 상품 존재 여부와 price_history 값을 분리 조회
    # scalar_one_or_none()은 컬럼값이 NULL일 때도 None 반환하므로
    # id 컬럼을 함께 SELECT하여 상품 존재 여부를 정확히 판단
    stmt = _sel(_CP.id, _CP.price_history).where(_CP.id == product_id)
    result = await session.execute(stmt)
    row = result.one_or_none()
    if row is None:
        raise HTTPException(404, "상품을 찾을 수 없습니다")
    raw = row[1] or []
    # null/non-dict 엔트리 제거 — 데이터 손상 방어
    return [h for h in raw if isinstance(h, dict)] if isinstance(raw, list) else []


@router.post("/products", status_code=201)
async def create_collected_product(
    body: CollectedProductCreate,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    from fastapi.responses import JSONResponse

    svc = _get_services(session)
    result = await svc.create_collected_product(body.model_dump(exclude_unset=True))
    if result is None:
        return JSONResponse(
            status_code=409,
            content={"detail": "동일 소싱처에 동일 원 상품명이 이미 존재합니다."},
        )
    # 상품 생성 시 캐시 무효화
    await cache.clear_pattern("products:*")
    return result


@router.get("/products/lookup-by-market-no/{market_product_no}")
async def lookup_by_market_product_no(
    market_product_no: str,
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """마켓 상품번호로 수집상품 조회 (원문링크/이미지 등 반환)."""
    from sqlalchemy import text as sa_text

    from backend.core.sql_safe import escape_like

    # 하이픈/공백 제거한 정규화 값 (IQ2245-068 → IQ2245068)
    spid_norm = market_product_no.replace("-", "").replace(" ", "")
    # market_product_no 는 path param (외부 입력) — `%`/`_` 메타 문자를 리터럴로
    # 강제하기 위해 escape 후 ESCAPE '\\' 절 명시. 단순 substring/JSON-quoted 두
    # 패턴 모두 적용.
    safe = escape_like(market_product_no)
    # tid는 None 가능 → asyncpg 타입 추론 위해 .bindparams로 명시 (issue #202).
    # 동일 파일 다른 함수(dashboard-stats 등)의 패턴과 통일.
    sql = sa_text(
        "SELECT id, source_site, site_product_id, name, images, source_url, market_product_nos "
        "FROM samba_collected_product "
        "WHERE (:tid IS NULL OR tenant_id = :tid) AND ("
        "    market_product_nos::text LIKE :pattern ESCAPE '\\' "
        " OR market_product_nos::text LIKE :pattern_bare ESCAPE '\\' "
        " OR site_product_id = :spid "
        " OR REPLACE(site_product_id, '-', '') = :spid_norm "
        ") "
        "LIMIT 1"
    ).bindparams(tid=tenant_id)
    result = await session.execute(
        sql,
        {
            "pattern": f'%"{safe}"%',
            "pattern_bare": f"%{safe}%",
            "spid": market_product_no,
            "spid_norm": spid_norm,
        },
    )
    row = result.fetchone()
    if not row:
        return {"found": False}
    pid, source_site, site_product_id, name, images, source_url, market_product_nos = (
        row
    )
    thumb = images[0] if images and isinstance(images, list) and images else ""
    return {
        "found": True,
        "id": pid,
        "source_site": source_site,
        "site_product_id": site_product_id,
        "name": name,
        "original_link": source_url or "",
        "product_image": thumb,
        # 스마트스토어 originProductNo 등 마켓별 등록 상품번호 (account_id / account_id_origin 키)
        "market_product_nos": market_product_nos or {},
    }


@router.post("/products/bulk", status_code=201)
async def bulk_create_collected_products(
    body: BulkCreateRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    svc = _get_services(session)
    items = [item.model_dump(exclude_unset=True) for item in body.items]
    created_count = await svc.bulk_create_products(items)
    # 상품 일괄 생성 시 캐시 무효화
    await cache.clear_pattern("products:*")
    return {"created": created_count}


@router.post("/products/images/bulk-remove")
async def bulk_remove_image(
    body: BulkImageRemoveRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """특정 이미지 URL을 모든 상품에서 일괄 삭제 (추적삭제).

    대상 필드:
      - images: 대표/추가 이미지 배열
      - detail_images: 상세페이지 이미지 배열
      - detail_html: 상세페이지 HTML 본문 — 해당 URL을 포함한 <img> 태그 통째 제거
    """
    import re

    from backend.domain.samba.collector.model import SambaCollectedProduct
    from sqlalchemy import String, cast
    from sqlmodel import select

    # fields 우선, 없으면 기존 field 하위호환
    target_fields = body.fields if body.fields else [body.field]
    image_url = body.image_url

    # image_url 은 외부 입력 — `%`/`_` 메타 escape 후 ESCAPE '\\' 명시.
    from backend.core.sql_safe import escape_like

    image_pat = f"%{escape_like(image_url)}%"

    # DB 레벨에서 해당 이미지 URL을 포함하는 상품만 필터링 (전체 로드 방지)
    conditions = []
    if "images" in target_fields:
        conditions.append(
            cast(SambaCollectedProduct.images, String).like(image_pat, escape="\\")
        )
    if "detail_images" in target_fields:
        conditions.append(
            cast(SambaCollectedProduct.detail_images, String).like(
                image_pat, escape="\\"
            )
        )
    if "detail_html" in target_fields:
        conditions.append(
            SambaCollectedProduct.detail_html.like(image_pat, escape="\\")
        )
    if not conditions:
        return {"removed": 0}

    # detail_html에서 해당 URL을 포함한 <img> 태그를 통째로 제거
    img_tag_re = re.compile(r"<img\b[^>]*>", flags=re.IGNORECASE)

    def _strip_img_tags(html: str, url: str) -> str:
        def _repl(m: re.Match) -> str:
            return "" if url in m.group(0) else m.group(0)

        return img_tag_re.sub(_repl, html)

    stmt = select(SambaCollectedProduct).where(or_(*conditions))
    result = await session.exec(stmt)
    removed_count = 0
    for p in result.all():
        found = False
        if "images" in target_fields and p.images and image_url in p.images:
            p.images = [u for u in p.images if u != image_url]
            found = True
        if (
            "detail_images" in target_fields
            and p.detail_images
            and image_url in p.detail_images
        ):
            p.detail_images = [u for u in p.detail_images if u != image_url]
            found = True
        if (
            "detail_html" in target_fields
            and p.detail_html
            and image_url in p.detail_html
        ):
            new_html = _strip_img_tags(p.detail_html, image_url)
            if new_html != p.detail_html:
                p.detail_html = new_html
                found = True
        if found:
            tags = list(p.tags or [])
            if "__img_edited__" not in tags:
                tags.append("__img_edited__")
                p.tags = tags
            session.add(p)
            removed_count += 1
    await session.commit()
    return {"removed": removed_count}


@router.put("/products/{product_id}")
async def update_collected_product(
    product_id: str,
    body: CollectedProductUpdate,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    svc = _get_services(session)
    data = body.model_dump(exclude_unset=True)

    # extra_data는 덮어쓰지 않고 기존 값과 병합
    if "extra_data" in data and data["extra_data"] is not None:
        repo = svc.product_repo
        existing = await repo.get_async(product_id)
        if existing:
            existing_extra = getattr(existing, "extra_data", {}) or {}
            data["extra_data"] = {**existing_extra, **data["extra_data"]}

    result = await svc.update_collected_product(product_id, data)
    if not result:
        raise HTTPException(404, "상품을 찾을 수 없습니다")
    return result


@router.post("/products/{product_id}/reset-registration")
async def reset_product_registration(
    product_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """상품의 마켓 등록 정보(registered_accounts, market_product_nos) 초기화."""
    svc = _get_services(session)
    result = await svc.update_collected_product(
        product_id,
        {
            "registered_accounts": None,
            "market_product_nos": None,
            "status": "collected",
        },
    )
    if not result:
        raise HTTPException(404, "상품을 찾을 수 없습니다")
    return {"ok": True}


@router.delete("/products/{product_id}")
async def delete_collected_product(
    product_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    svc = _get_services(session)
    if not await svc.delete_collected_product(product_id):
        raise HTTPException(404, "상품을 찾을 수 없습니다")
    return {"ok": True}


@router.post("/products/bulk-delete")
async def bulk_delete_products(
    body: BulkProductIdsRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """상품 일괄 삭제 — 단일 DELETE 쿼리."""
    svc = _get_services(session)
    deleted = await svc.bulk_delete_collected_products(body.ids)
    # 상품 삭제 시 캐시 무효화
    await cache.clear_pattern("products:*")
    return {"deleted": deleted}


@router.post("/products/block-and-delete")
async def block_and_delete_products(
    body: BlockProductRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """수집차단 + 삭제 — 블랙리스트 등록 후 상품 삭제."""
    from sqlalchemy import delete as sa_delete
    from sqlmodel import col, select
    from backend.domain.samba.collector.model import SambaCollectedProduct
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository

    # 삭제 대상 상품 정보 조회
    stmt = select(SambaCollectedProduct).where(
        col(SambaCollectedProduct.id).in_(body.product_ids)
    )
    result = await session.execute(stmt)
    products = result.scalars().all()

    if not products:
        raise HTTPException(404, "상품을 찾을 수 없습니다")

    # 블랙리스트 로드
    settings_repo = SambaSettingsRepository(session)
    row = await settings_repo.find_by_async(key="collection_blacklist")
    blacklist: list[dict] = []
    if row and isinstance(row.value, list):
        blacklist = row.value

    # 블랙리스트에 추가
    existing_keys = {f"{b['source_site']}:{b['site_product_id']}" for b in blacklist}
    added = 0
    for p in products:
        key = f"{p.source_site}:{p.site_product_id}"
        if key not in existing_keys and p.source_site and p.site_product_id:
            blacklist.append(
                {
                    "source_site": p.source_site,
                    "site_product_id": p.site_product_id,
                    "name": (p.name or "")[:50],
                    "blocked_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            existing_keys.add(key)
            added += 1

    # 블랙리스트 저장
    if row:
        row.value = blacklist
        session.add(row)
    else:
        from backend.domain.samba.forbidden.model import SambaSettings

        new_row = SambaSettings(key="collection_blacklist", value=blacklist)
        session.add(new_row)

    # 상품 삭제
    del_stmt = sa_delete(SambaCollectedProduct).where(
        col(SambaCollectedProduct.id).in_(body.product_ids)
    )
    del_result = await session.exec(del_stmt)  # type: ignore[arg-type]
    await session.commit()
    await cache.clear_pattern("products:*")
    _invalidate_blacklist_cache()

    return {"ok": True, "blocked": added, "deleted": del_result.rowcount}


class BulkResetRegistrationRequest(BaseModel):
    ids: list[str]
    # 지정된 account_id만 등록 정보에서 제거 (None/빈 리스트 → 전체 초기화)
    account_ids: Optional[list[str]] = None


@router.post("/products/bulk-reset-registration")
async def bulk_reset_registration(
    body: BulkResetRegistrationRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """상품 마켓 등록 정보 일괄 초기화.

    account_ids 미지정 → 전체 초기화 (단일 UPDATE).
    account_ids 지정 → 각 상품에서 해당 계정만 제거 (per-row).
    """
    from sqlalchemy import update as sa_update
    from sqlmodel import col, select
    from backend.domain.samba.collector.model import SambaCollectedProduct

    if not body.account_ids:
        # last_sent_data도 함께 정리 (issue #206) — 누락 시 registered_accounts=NULL인데
        # last_sent_data엔 송신 이력이 살아있어 "유령 등록상품"으로 표시되던 사고 방지.
        stmt = (
            sa_update(SambaCollectedProduct)
            .where(col(SambaCollectedProduct.id).in_(body.ids))
            .values(
                registered_accounts=None,
                market_product_nos=None,
                last_sent_data=None,
                status="collected",
            )
        )
        result = await session.exec(stmt)  # type: ignore[arg-type]
        await session.commit()
        return {"reset": result.rowcount}

    # 선택된 계정만 제거 — 각 상품의 JSON 필드를 갱신
    remove_set = set(body.account_ids)
    rows = (
        (
            await session.execute(
                select(SambaCollectedProduct).where(
                    col(SambaCollectedProduct.id).in_(body.ids)
                )
            )
        )
        .scalars()
        .all()
    )

    reset = 0
    for product in rows:
        regs = list(product.registered_accounts or [])
        remaining = [aid for aid in regs if aid not in remove_set]
        if len(remaining) == len(regs):
            continue  # 변경 없음
        product.registered_accounts = remaining or None

        nos = dict(product.market_product_nos or {})
        for aid in remove_set:
            nos.pop(aid, None)
            nos.pop(f"{aid}_origin", None)
        product.market_product_nos = nos or None

        # last_sent_data도 동일하게 정리 (issue #206 유령 등록상품 방지)
        sent = dict(product.last_sent_data or {})
        for aid in remove_set:
            sent.pop(aid, None)
        product.last_sent_data = sent or None

        if not remaining:
            product.status = "collected"

        session.add(product)
        reset += 1

    await session.commit()
    return {"reset": reset}


@router.post("/products/fix-nike-categories")
async def fix_nike_categories(
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """기존 Nike 상품 카테고리를 search_filter.category_filter 기반으로 보정."""
    from backend.domain.samba.collector.model import (
        SambaCollectedProduct,
        SambaSearchFilter,
    )

    stmt = (
        select(SambaCollectedProduct, SambaSearchFilter.category_filter)
        .join(
            SambaSearchFilter,
            SambaCollectedProduct.search_filter_id == SambaSearchFilter.id,
        )
        .where(SambaCollectedProduct.source_site == "Nike")
    )
    rows = (await session.execute(stmt)).all()

    updated = 0
    for product, cat_filter in rows:
        if not cat_filter:
            continue
        # "남성_러닝화" → cat2="남성", cat3="러닝화"
        # "가방" (언더스코어 없음) → cat2="", cat3="가방"
        parts = cat_filter.split("_", 1)
        if len(parts) == 2:
            cat2, cat3 = parts
        else:
            cat2, cat3 = "", parts[0]
        new_category = " > ".join([x for x in [cat2, cat3] if x])
        if product.category != new_category:
            product.category = new_category
            product.category2 = cat2
            product.category3 = cat3
            session.add(product)
            updated += 1

    await session.commit()
    # category-tree 캐시 무효화
    await cache.delete("products:category-tree")
    await cache.delete("products:counts")
    return {"updated": updated, "total": len(rows)}


@router.post("/products/bulk-update-tags")
async def bulk_update_tags(
    body: BulkTagUpdateRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """상품 태그/SEO키워드 일괄 업데이트."""
    from backend.domain.samba.collector.model import SambaCollectedProduct
    from sqlmodel import col

    stmt = select(SambaCollectedProduct).where(
        col(SambaCollectedProduct.id).in_(body.ids)
    )
    results = await session.exec(stmt)
    products = results.all()
    for p in products:
        if body.tags is not None:
            # issue #239 — `p.tags = body.tags` 통째 덮어쓰기 금지. __접두 시스템 태그 보존.
            preserved = [
                t for t in (p.tags or []) if isinstance(t, str) and t.startswith("__")
            ]
            p.tags = list(dict.fromkeys([*preserved, *body.tags]))
        if body.seo_keywords is not None:
            p.seo_keywords = body.seo_keywords
        session.add(p)
    await session.commit()
    return {"updated": len(products)}


@router.post("/products/bulk-add-account")
async def bulk_add_registered_account(
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """플레이오토 API에서 등록 상품 조회 → site_product_id 매칭 → registered_accounts에 계정 추가."""
    from backend.domain.samba.account.model import SambaMarketAccount
    from backend.domain.samba.collector.model import SambaCollectedProduct
    from backend.domain.samba.proxy.playauto import PlayAutoClient

    # 플레이오토 계정 조회
    pa_stmt = select(SambaMarketAccount).where(
        SambaMarketAccount.market_type == "playauto",
        SambaMarketAccount.is_active == True,  # noqa: E712
    )
    pa_result = await session.exec(pa_stmt)
    pa_acc = pa_result.first()
    if not pa_acc:
        raise HTTPException(400, "플레이오토 계정이 없습니다")

    pa_extras = pa_acc.additional_fields or {}
    pa_api_key = pa_extras.get("apiKey", "") or getattr(pa_acc, "api_key", "")
    if not pa_api_key:
        raise HTTPException(400, "플레이오토 API Key가 없습니다")

    # 플레이오토 API에서 등록 상품 조회
    client = PlayAutoClient(pa_api_key)
    try:
        pa_products = await client.get_products()
    finally:
        await client.close()

    # ModelName(=site_product_id) 추출
    pa_model_names = set()
    for pp in pa_products:
        mn = str(pp.get("ModelName", "") or "").strip()
        if mn:
            pa_model_names.add(mn)

    if not pa_model_names:
        return {"error": "플레이오토에 등록된 상품이 없습니다", "pa_count": 0}

    # DB에서 매칭되는 상품 조회
    stmt = select(SambaCollectedProduct).where(
        SambaCollectedProduct.status == "registered",
        SambaCollectedProduct.site_product_id.in_(pa_model_names),
    )
    results = await session.exec(stmt)
    products = results.all()

    updated = 0
    already = 0
    for p in products:
        reg = list(p.registered_accounts or [])
        if pa_acc.id not in reg:
            reg.append(pa_acc.id)
            p.registered_accounts = reg
            session.add(p)
            updated += 1
        else:
            already += 1

    if updated > 0:
        await session.commit()
    return {
        "pa_products": len(pa_model_names),
        "matched": len(products),
        "updated": updated,
        "already": already,
        "account_id": pa_acc.id,
    }

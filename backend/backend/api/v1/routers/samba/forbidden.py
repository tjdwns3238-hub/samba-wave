"""SambaWave Forbidden Word API router."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_read_session_dependency, get_write_session_dependency
from backend.domain.samba.tenant.middleware import get_optional_tenant_id

router = APIRouter(prefix="/forbidden", tags=["samba-forbidden"])


class WordCreate(BaseModel):
    word: str
    type: str = "forbidden"  # forbidden | deletion
    scope: str = "title"  # title | description | both
    group_id: Optional[str] = None
    is_active: bool = True


class WordUpdate(BaseModel):
    word: Optional[str] = None
    type: Optional[str] = None
    scope: Optional[str] = None
    is_active: Optional[bool] = None


class ValidateRequest(BaseModel):
    name: str


def _get_service(session: AsyncSession):
    from backend.domain.samba.forbidden.repository import (
        SambaForbiddenWordRepository,
        SambaSettingsRepository,
    )
    from backend.domain.samba.forbidden.service import SambaForbiddenService

    return SambaForbiddenService(
        SambaForbiddenWordRepository(session),
        SambaSettingsRepository(session),
    )


@router.get("/words")
async def list_words(
    type: Optional[str] = None,
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    from backend.domain.samba.forbidden.model import SambaForbiddenWord

    # tenant_id가 있으면 해당 테넌트 금지어만 조회
    stmt = select(SambaForbiddenWord).order_by(SambaForbiddenWord.created_at.desc())
    if type:
        stmt = stmt.where(SambaForbiddenWord.type == type)
    if tenant_id is not None:
        from sqlalchemy import or_

        stmt = stmt.where(
            or_(
                SambaForbiddenWord.tenant_id == tenant_id,
                SambaForbiddenWord.tenant_id == None,  # noqa: E711
            )
        )
    result = await session.execute(stmt)
    return result.scalars().all()


@router.post("/words", status_code=201)
async def create_word(
    body: WordCreate,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    data = body.model_dump(exclude_unset=True)
    # tenant_id가 있으면 신규 금지어에 테넌트 정보 설정
    if tenant_id is not None:
        data["tenant_id"] = tenant_id
    return await _get_service(session).create_word(data)


class BulkWordsRequest(BaseModel):
    type: str  # forbidden | deletion
    words: list[str]


@router.post("/words/bulk", status_code=201)
async def bulk_save_words(
    body: BulkWordsRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """기존 타입의 단어를 전부 삭제 후 새 단어 벌크 저장 (단일 트랜잭션)."""
    from sqlmodel import delete
    from backend.domain.samba.forbidden.model import SambaForbiddenWord

    # tenant_id가 있으면 해당 테넌트 타입만, 없으면 전체 삭제
    del_stmt = delete(SambaForbiddenWord).where(SambaForbiddenWord.type == body.type)
    if tenant_id is not None:
        from sqlalchemy import or_

        del_stmt = del_stmt.where(
            or_(
                SambaForbiddenWord.tenant_id == tenant_id,
                SambaForbiddenWord.tenant_id == None,  # noqa: E711
            )
        )
    await session.exec(del_stmt)

    # 새 단어 일괄 추가 (중복 제거)
    created = 0
    seen: set[str] = set()
    for word in body.words:
        w = word.strip()
        if not w or w.lower() in seen:
            continue
        seen.add(w.lower())
        session.add(
            SambaForbiddenWord(
                word=w,
                type=body.type,
                scope="all",
                is_active=True,
                tenant_id=tenant_id,
            )
        )
        created += 1

    await session.commit()
    return {"ok": True, "created": created}


@router.put("/words/{word_id}")
async def update_word(
    word_id: str,
    body: WordUpdate,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    result = await _get_service(session).update_word(
        word_id, body.model_dump(exclude_unset=True)
    )
    if not result:
        raise HTTPException(404, "단어를 찾을 수 없습니다")
    return result


@router.put("/words/{word_id}/toggle")
async def toggle_word(
    word_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    result = await _get_service(session).toggle_word(word_id)
    if not result:
        raise HTTPException(404, "단어를 찾을 수 없습니다")
    return result


@router.delete("/words/{word_id}")
async def delete_word(
    word_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    if not await _get_service(session).delete_word(word_id):
        raise HTTPException(404, "단어를 찾을 수 없습니다")
    return {"ok": True}


@router.post("/validate")
async def validate_product_name(
    body: ValidateRequest,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    svc = _get_service(session)
    return await svc.validate_product({"name": body.name})


@router.post("/clean")
async def clean_product_name(
    body: ValidateRequest,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    svc = _get_service(session)
    return {"clean_name": await svc.clean_product_name(body.name)}


# ── Settings (generic key-value store) ──


@router.get("/settings/{key}")
async def get_setting(
    key: str,
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    from backend.domain.samba.forbidden.model import SambaSettings

    # tenant_id가 있으면 테넌트 전용 키(tenant_id:key) 우선 조회
    # 멀티테넌트 격리 이전(2026-05-18)에는 bare 키로 저장됨 → 폴백으로 bare 키 시도
    effective_key = f"{tenant_id}:{key}" if tenant_id is not None else key
    stmt = select(SambaSettings).where(SambaSettings.key == effective_key)
    result = await session.execute(stmt)
    row = result.scalars().first()
    if row is None and tenant_id is not None:
        stmt = select(SambaSettings).where(SambaSettings.key == key)
        result = await session.execute(stmt)
        row = result.scalars().first()
    value = row.value if row else None
    # None이면 빈 dict 반환 (프론트에서 .catch(() => null) 호환)
    return value if value is not None else {}


@router.put("/settings/{key}")
async def save_setting(
    key: str,
    body: dict,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    from datetime import UTC, datetime

    from backend.domain.samba.forbidden.model import SambaSettings
    from backend.utils.masking import drop_masked_secret_fields

    # (2026-05-25) store_* deprecation 경고 — 마켓 자격증명은 samba_market_account 로 통일 중.
    # frontend 가 accountApi.create/update 로 전환 완료(7d) 후 410 차단 예정.
    # 지금은 호환을 위해 저장 허용하되 로그로 사용 빈도 추적.
    if key.startswith("store_") and key not in ("store_network_ips",):
        from backend.utils.logger import logger as _lg

        _lg.warning(
            "[deprecated] PUT /forbidden/settings/%s — 마켓 자격증명 store_* 저장 경로 폐기 예정. "
            "POST /api/v1/samba/accounts 로 이전 필요 (tenant_id=%s)",
            key,
            tenant_id,
        )

    value = body.get("value")
    # tenant_id가 있으면 테넌트 전용 키(tenant_id:key)로 upsert
    # SambaSettings PK가 key 단일 컬럼이므로 테넌트별 네임스페이스 분리
    effective_key = f"{tenant_id}:{key}" if tenant_id is not None else key
    existing_stmt = select(SambaSettings).where(SambaSettings.key == effective_key)
    result = await session.execute(existing_stmt)
    existing = result.scalars().first()
    # store_* 키이면 마켓 인증정보가 들어감 → 마스킹값(****XXXX) 덮어쓰기 차단
    # dispatcher/proxy/order/cs_inquiry 가 이 settings를 직접 읽으므로 마스킹값이 들어가면
    # 발송/주문 동기화/CS 호출 모두 인증 실패로 무력화됨.
    # 단, 전체 merge는 select "설정안함"으로 키 삭제하려는 의도를 깨므로 REPLACE 시맨틱 유지하고
    # 마스킹값으로 들어온 secret 키만 existing에서 복원.
    if (
        isinstance(value, dict)
        and key.startswith("store_")
        and key != "store_network_ips"
    ):
        from backend.utils.masking import ALL_NESTED_SECRET_KEYS

        cleaned = drop_masked_secret_fields(value)
        if existing and isinstance(existing.value, dict):
            for sk in ALL_NESTED_SECRET_KEYS:
                # absent(프론트가 빈 password 필드를 payload에서 제거한 케이스) 또는
                # 마스킹값으로 drop된 케이스 모두 기존값 보존
                if sk in existing.value and sk not in cleaned:
                    cleaned[sk] = existing.value[sk]
        value = cleaned
    if existing:
        existing.value = value
        existing.updated_at = datetime.now(UTC)
        if tenant_id is not None:
            existing.tenant_id = tenant_id
        session.add(existing)
        await session.commit()
        await session.refresh(existing)

        # SSG 설정 변경 시 인프라 캐시 무효화
        if key == "store_ssg" and isinstance(value, dict):
            ssg_api_key = value.get("apiKey", "")
            if ssg_api_key:
                from backend.domain.samba.proxy.ssg import (
                    invalidate_infra_cache,
                )

                await invalidate_infra_cache(ssg_api_key)

        return existing
    new_setting = SambaSettings(
        key=effective_key,
        tenant_id=tenant_id,
        value=value,
        updated_at=datetime.now(UTC),
    )
    session.add(new_setting)
    await session.commit()
    await session.refresh(new_setting)

    # SSG 설정 변경 시 인프라 캐시 무효화
    if key == "store_ssg" and isinstance(value, dict):
        ssg_api_key = value.get("apiKey", "")
        if ssg_api_key:
            from backend.domain.samba.proxy.ssg import invalidate_infra_cache

            await invalidate_infra_cache(ssg_api_key)

    return new_setting


@router.get("/exchange-rates")
async def get_exchange_rates(
    force_refresh: bool = False,
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    from backend.domain.samba.exchange_rate_service import (
        build_exchange_rate_response,
        get_exchange_rate_settings,
        get_latest_exchange_rates,
    )

    settings = await get_exchange_rate_settings(session, tenant_id)
    latest_rates = await get_latest_exchange_rates(force_refresh=force_refresh)
    return build_exchange_rate_response(settings, latest_rates)


@router.get("/tag-banned-words")
async def get_tag_banned_words(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """태그 금지어 통합 조회: 소싱처 + 수집 브랜드 + API 거부 태그."""
    from sqlmodel import select
    from backend.domain.samba.collector.model import SambaCollectedProduct

    svc = _get_service(session)

    # 1. API 거부 태그 (DB 누적)
    rejected = await svc.get_setting("smartstore_banned_tags")
    rejected_tags: list[str] = rejected if isinstance(rejected, list) else []

    # 2. 수집된 브랜드 (distinct) — projection 쿼리이므로 수동 tenant 필터 필요
    stmt = (
        select(SambaCollectedProduct.brand)
        .where(
            SambaCollectedProduct.brand.isnot(None),
            SambaCollectedProduct.brand != "",
        )
        .distinct()
        .limit(500)
    )
    if tenant_id is not None:
        stmt = stmt.where(SambaCollectedProduct.tenant_id == tenant_id)
    result = await session.exec(stmt)
    brands = sorted(set(b for b in result.all() if b and len(b.strip()) >= 2))

    # 3. 소싱처 (고정)
    source_sites = [
        "MUSINSA",
        "무신사",
        "KREAM",
        "크림",
        "ABCmart",
        "ABC마트",
        "Nike",
        "나이키",
        "Adidas",
        "아디다스",
        "올리브영",
        "OliveYoung",
        "SSG",
        "신세계",
        "롯데온",
        "LOTTEON",
        "GSShop",
        "GS샵",
        "eBay",
        "이베이",
        "Zara",
        "자라",
        "FashionPlus",
        "패션플러스",
        "GrandStage",
        "그랜드스테이지",
        "REXMONDE",
        "ElandMall",
        "이랜드몰",
        "SSF",
        "SSF샵",
    ]

    return {
        "rejected": rejected_tags,
        "brands": brands,
        "source_sites": source_sites,
    }

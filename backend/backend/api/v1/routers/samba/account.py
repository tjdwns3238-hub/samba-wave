"""SambaWave Market Account API router."""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_read_session_dependency, get_write_session_dependency
from backend.domain.samba.tenant.middleware import (
    get_optional_tenant_id,
)
from backend.utils.masking import mask_model_secrets

router = APIRouter(prefix="/accounts", tags=["samba-accounts"])


class AccountCreate(BaseModel):
    market_type: str
    seller_id: Optional[str] = None
    business_name: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    additional_fields: Optional[Any] = None
    is_active: bool = True


class AccountUpdate(BaseModel):
    account_label: Optional[str] = None
    seller_id: Optional[str] = None
    business_name: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    additional_fields: Optional[Any] = None
    is_active: Optional[bool] = None


def _get_service(session: AsyncSession):
    from backend.domain.samba.account.repository import SambaMarketAccountRepository
    from backend.domain.samba.account.service import SambaAccountService

    return SambaAccountService(SambaMarketAccountRepository(session))


@router.get("")
async def list_accounts(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    from backend.domain.samba.account.model import SambaMarketAccount

    # tenant_id가 있으면 해당 테넌트 + 기존(NULL) 마켓 계정 모두 조회
    stmt = select(SambaMarketAccount).order_by(SambaMarketAccount.created_at.desc())
    if tenant_id is not None:
        # backfill 완료 후 NULL 계정 없음 — 본인 테넌트만 조회
        stmt = stmt.where(SambaMarketAccount.tenant_id == tenant_id)
    result = await session.execute(stmt)
    accounts = result.scalars().all()
    return [mask_model_secrets(a.model_dump()) for a in accounts]


@router.get("/active")
async def list_active_accounts(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    from backend.domain.samba.account.model import SambaMarketAccount

    # tenant_id가 있으면 해당 테넌트 + 기존(NULL) 활성 계정 모두 조회
    stmt = (
        select(SambaMarketAccount)
        .where(SambaMarketAccount.is_active == True)  # noqa: E712
        .order_by(SambaMarketAccount.created_at.desc())
    )
    if tenant_id is not None:
        # backfill 완료 후 NULL 계정 없음 — 본인 테넌트만 조회
        stmt = stmt.where(SambaMarketAccount.tenant_id == tenant_id)
    result = await session.execute(stmt)
    accounts = result.scalars().all()
    return [mask_model_secrets(a.model_dump()) for a in accounts]


@router.get("/markets")
async def get_supported_markets():
    from backend.domain.samba.account.service import SambaAccountService

    return SambaAccountService.SUPPORTED_MARKETS


@router.get("/{account_id}")
async def get_account(
    account_id: str,
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    svc = _get_service(session)
    account = await svc.get_account(account_id)
    if not account:
        raise HTTPException(404, "계정을 찾을 수 없습니다")
    # IDOR 방지: 테넌트 소유권 검증
    if tenant_id is not None and account.tenant_id != tenant_id:
        raise HTTPException(403, "해당 계정에 대한 권한이 없습니다")
    return mask_model_secrets(account.model_dump())


@router.get("/{account_id}/secrets")
async def get_account_secrets(
    account_id: str,
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """additional_fields를 마스킹 없이 반환 (설정 화면 '보기' 전용)."""
    svc = _get_service(session)
    account = await svc.get_account(account_id)
    if not account:
        raise HTTPException(404, "계정을 찾을 수 없습니다")
    if tenant_id is not None and account.tenant_id != tenant_id:
        raise HTTPException(403, "해당 계정에 대한 권한이 없습니다")
    return account.additional_fields or {}


@router.post("", status_code=201)
async def create_account(
    body: AccountCreate,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    # 플랜 제한 영구 제거 (2026-05-20) — 자기운영 환경, 한도 체크 불필요
    data = body.model_dump(exclude_unset=True)
    # tenant_id가 있으면 신규 계정에 테넌트 정보 설정
    if tenant_id is not None:
        data["tenant_id"] = tenant_id
    await _enrich_store_slug(data)
    return await _get_service(session).create_account(data)


@router.put("/{account_id}")
async def update_account(
    account_id: str,
    body: AccountUpdate,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    data = body.model_dump(exclude_unset=True)
    svc = _get_service(session)
    # 기존 계정의 market_type 조회
    existing = await svc.get_account(account_id)
    if not existing:
        raise HTTPException(404, "계정을 찾을 수 없습니다")
    # tenant_id가 있으면 소유권 검증
    if tenant_id is not None and existing.tenant_id != tenant_id:
        raise HTTPException(403, "해당 계정에 대한 권한이 없습니다")
    data.setdefault("_market_type", existing.market_type)
    await _enrich_store_slug(data)
    data.pop("_market_type", None)
    result = await svc.update_account(account_id, data)
    if not result:
        raise HTTPException(404, "계정을 찾을 수 없습니다")
    return result


async def _enrich_store_slug(data: dict[str, Any]) -> None:
    """스마트스토어 계정이면 API로 스토어 슬러그를 자동 조회하여 additional_fields에 저장."""
    from backend.utils.logger import logger

    market_type = data.get("market_type") or data.get("_market_type", "")
    if market_type != "smartstore":
        return

    extras = data.get("additional_fields") or {}
    if not isinstance(extras, dict):
        return

    client_id = extras.get("clientId", "") or data.get("api_key", "")
    client_secret = extras.get("clientSecret", "") or data.get("api_secret", "")
    if not client_id or not client_secret:
        return

    try:
        from backend.domain.samba.proxy.smartstore import SmartStoreClient

        client = SmartStoreClient(client_id, client_secret)
        info = await client.get_channel_info()
        if info.get("storeSlug"):
            extras["storeSlug"] = info["storeSlug"]
            data["additional_fields"] = extras
            logger.info(f"[계정] 스토어 슬러그 자동 조회: {info['storeSlug']}")
        else:
            # fallback: 등록된 상품에서 슬러그 추출
            logger.info("[계정] 채널 API에서 슬러그 없음 — fallback 시도")
            slug = await client.get_store_slug_fallback()
            if slug:
                extras["storeSlug"] = slug
                data["additional_fields"] = extras
                logger.info(f"[계정] 스토어 슬러그 fallback 성공: {slug}")
            else:
                logger.warning("[계정] 스토어 슬러그 fallback도 실패")
    except Exception as e:
        logger.warning(f"[계정] 스토어 슬러그 조회 실패 (무시): {e}")


@router.put("/{account_id}/toggle")
async def toggle_account(
    account_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    svc = _get_service(session)
    # tenant_id가 있으면 소유권 검증
    if tenant_id is not None:
        existing = await svc.get_account(account_id)
        if not existing:
            raise HTTPException(404, "계정을 찾을 수 없습니다")
        if existing.tenant_id != tenant_id:
            raise HTTPException(403, "해당 계정에 대한 권한이 없습니다")
    result = await svc.toggle_active(account_id)
    if not result:
        raise HTTPException(404, "계정을 찾을 수 없습니다")
    return result


@router.delete("/{account_id}")
async def delete_account(
    account_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    svc = _get_service(session)
    # tenant_id가 있으면 소유권 검증
    if tenant_id is not None:
        existing = await svc.get_account(account_id)
        if not existing:
            raise HTTPException(404, "계정을 찾을 수 없습니다")
        if existing.tenant_id != tenant_id:
            raise HTTPException(403, "해당 계정에 대한 권한이 없습니다")
    if not await svc.delete_account(account_id):
        raise HTTPException(404, "계정을 찾을 수 없습니다")
    return {"ok": True}

"""SambaWave Tetris 정책 배치 API router."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_read_session_dependency, get_write_session_dependency
from backend.domain.samba.tetris.repository import SambaTetrisRepository
from backend.domain.samba.tetris.service import SambaTetrisService
from backend.domain.samba.tenant.middleware import get_optional_tenant_id
from backend.dtos.samba.tetris import (
    TetrisAssignRequest,
    TetrisAssignResponse,
    TetrisBoardResponse,
    TetrisExcludeRequest,
    TetrisMoveRequest,
    TetrisReorderRequest,
    TetrisSyncIntervalRequest,
    TetrisSyncIntervalResponse,
    TetrisSyncResponse,
)

TETRIS_SYNC_INTERVAL_KEY = "tetris_sync_interval_hours"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tetris", tags=["samba-tetris"])


def _get_service(session: AsyncSession) -> SambaTetrisService:
    """서비스 인스턴스 생성 헬퍼."""
    return SambaTetrisService(SambaTetrisRepository(session), session)


@router.get("/board", response_model=TetrisBoardResponse)
async def get_board(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> TetrisBoardResponse:
    """테트리스 보드 전체 구조 조회."""
    svc = _get_service(session)
    board = await svc.get_board(tenant_id)
    return TetrisBoardResponse(**board)


@router.post("/assign", response_model=TetrisAssignResponse, status_code=201)
async def assign_brand(
    body: TetrisAssignRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> TetrisAssignResponse:
    """브랜드를 마켓 계정에 배치하고 상품 전송 트리거."""
    svc = _get_service(session)
    assignment = await svc.assign(
        tenant_id=tenant_id,
        source_site=body.source_site,
        brand_name=body.brand_name,
        market_account_id=body.market_account_id,
        policy_id=body.policy_id,
        position_order=body.position_order,
    )
    return TetrisAssignResponse(
        id=assignment.id,
        source_site=assignment.source_site,
        brand_name=assignment.brand_name,
        market_account_id=assignment.market_account_id,
        policy_id=assignment.policy_id,
        position_order=assignment.position_order,
    )


@router.delete("/assign/{assignment_id}", status_code=200)
async def remove_assignment(
    assignment_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> dict[str, bool]:
    """배치 삭제 후 마켓 상품 삭제 트리거."""
    svc = _get_service(session)
    deleted = await svc.remove(assignment_id=assignment_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="배치를 찾을 수 없습니다")
    return {"deleted": True}


@router.patch("/assign/{assignment_id}/move", response_model=TetrisAssignResponse)
async def move_assignment(
    assignment_id: str,
    body: TetrisMoveRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> TetrisAssignResponse:
    """배치를 다른 계정으로 이동 — 기존 계정 마켓삭제 → 신규 계정 전송."""
    svc = _get_service(session)
    assignment = await svc.move(
        assignment_id=assignment_id,
        tenant_id=tenant_id,
        new_account_id=body.market_account_id,
        policy_id=body.policy_id,
        position_order=body.position_order,
    )
    return TetrisAssignResponse(
        id=assignment.id,
        source_site=assignment.source_site,
        brand_name=assignment.brand_name,
        market_account_id=assignment.market_account_id,
        policy_id=assignment.policy_id,
        position_order=assignment.position_order,
    )


@router.patch("/assign/{assignment_id}/reorder", response_model=TetrisAssignResponse)
async def reorder_assignment(
    assignment_id: str,
    body: TetrisReorderRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> TetrisAssignResponse:
    """배치 순서만 변경 (shipment 트리거 없음)."""
    svc = _get_service(session)
    assignment = await svc.reorder(
        assignment_id=assignment_id,
        tenant_id=tenant_id,
        position_order=body.position_order,
    )
    return TetrisAssignResponse(
        id=assignment.id,
        source_site=assignment.source_site,
        brand_name=assignment.brand_name,
        market_account_id=assignment.market_account_id,
        policy_id=assignment.policy_id,
        position_order=assignment.position_order,
    )


@router.get("/sync-interval", response_model=TetrisSyncIntervalResponse)
async def get_sync_interval(
    session: AsyncSession = Depends(get_read_session_dependency),
) -> TetrisSyncIntervalResponse:
    """테트리스 자동 sync 인터벌 설정 조회."""
    from backend.api.v1.routers.samba.proxy._helpers import _get_setting

    val = await _get_setting(session, TETRIS_SYNC_INTERVAL_KEY)
    return TetrisSyncIntervalResponse(interval_hours=int(val) if val else 0)


@router.post("/sync-interval", response_model=TetrisSyncIntervalResponse)
async def set_sync_interval(
    body: TetrisSyncIntervalRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> TetrisSyncIntervalResponse:
    """테트리스 자동 sync 인터벌 설정 저장.

    interval_hours <= 0 이면 토글 OFF 로 간주 — 대기 중인 테트리스 발 PENDING 잡을
    일괄 취소한다 (RUNNING 잡은 건드리지 않음).
    """
    from backend.api.v1.routers.samba.proxy._helpers import _set_setting

    await _set_setting(session, TETRIS_SYNC_INTERVAL_KEY, body.interval_hours)

    cancelled = 0
    if body.interval_hours <= 0:
        svc = _get_service(session)
        cancelled = await svc.cancel_pending_tetris_jobs(tenant_id)

    return TetrisSyncIntervalResponse(
        interval_hours=body.interval_hours,
        cancelled=cancelled,
    )


@router.post("/sync", response_model=TetrisSyncResponse)
async def run_sync(
    clear_pending: bool = False,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> TetrisSyncResponse:
    """현재 배치 기준 미등록 상품 전송 잡 즉시 생성 (수동 실행).

    clear_pending=true: sync_all 전에 origin='tetris_sync' pending 잡을 모두 취소한다.
    테트리스 매칭 OFF→ON 토글 시 기존 잡을 비우고 현재 배치 기준으로 재생성하는 용도.
    """
    svc = _get_service(session)
    cancelled = 0
    if clear_pending:
        cancelled = await svc.cancel_pending_tetris_jobs(tenant_id)
    result = await svc.sync_all(tenant_id)
    result["cancelled_before_sync"] = cancelled
    return TetrisSyncResponse(**result)


@router.get("/assignments")
async def list_assignments(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> list[dict]:
    """현재 배치된 브랜드 목록 반환 — source_site, brand_name, market_account_id."""
    repo = SambaTetrisRepository(session)
    assignments = await repo.list_by_tenant(tenant_id)
    return [
        {
            "source_site": a.source_site,
            "brand_name": a.brand_name,
            "market_account_id": a.market_account_id,
        }
        for a in assignments
    ]


class RemoveByBrandRequest(BaseModel):
    source_site: str
    brand_name: str
    market_account_id: str


@router.post("/remove-by-brand", status_code=200)
async def remove_by_brand(
    body: RemoveByBrandRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> dict:
    """레거시 블럭 삭제 — 해당 계정 pending 전송잡 취소 + delete_market 잡 등록."""
    svc = _get_service(session)
    result = await svc.remove_by_brand(
        tenant_id=tenant_id,
        source_site=body.source_site,
        brand_name=body.brand_name,
        market_account_id=body.market_account_id,
    )
    await session.commit()
    return result


@router.post("/exclude", status_code=200)
async def set_excluded(
    body: TetrisExcludeRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> dict:
    """브랜드 블럭 배제 토글 — 레거시 블럭은 assignment 신규 생성 후 excluded=True."""
    svc = _get_service(session)
    assignment = await svc.set_excluded(
        tenant_id=tenant_id,
        source_site=body.source_site,
        brand_name=body.brand_name,
        market_account_id=body.market_account_id,
        excluded=body.excluded,
    )
    await session.commit()
    return {
        "id": assignment.id,
        "excluded": assignment.excluded,
    }

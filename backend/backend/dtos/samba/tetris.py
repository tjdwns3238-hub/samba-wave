"""SambaWave Tetris 정책 배치 DTO."""

from typing import Optional

from pydantic import BaseModel


class TetrisBrandBlock(BaseModel):
    """계정 내 브랜드 블록 정보."""

    id: Optional[str]
    source_site: str
    brand_name: str
    policy_id: Optional[str]
    policy_name: Optional[str]
    policy_color: str
    registered_count: int
    collected_count: int
    ai_tagged_count: int = 0
    position_order: int
    is_legacy: bool
    excluded: bool = False


class TetrisAccountBlock(BaseModel):
    """마켓 계정 블록 정보."""

    account_id: str
    account_label: str
    account_order: Optional[int] = None
    max_count: int
    total_registered: int
    total_collected: int
    assignments: list[TetrisBrandBlock]


class TetrisMarketGroup(BaseModel):
    """마켓 타입 그룹 정보."""

    market_type: str
    market_name: str
    accounts: list[TetrisAccountBlock]


class TetrisUnassigned(BaseModel):
    """미배치 브랜드 정보."""

    source_site: str
    brand_name: str
    policy_id: Optional[str] = None
    policy_name: Optional[str] = None
    policy_color: Optional[str] = None
    registered_count: int
    collected_count: int
    ai_tagged_count: int = 0


class TetrisBoardResponse(BaseModel):
    """테트리스 보드 전체 응답."""

    markets: list[TetrisMarketGroup]
    unassigned: list[TetrisUnassigned]


class TetrisAssignRequest(BaseModel):
    """배치 저장 요청."""

    source_site: str
    brand_name: str
    market_account_id: str
    policy_id: Optional[str] = None
    position_order: int = 0


class TetrisMoveRequest(BaseModel):
    """배치 이동 요청."""

    market_account_id: str
    policy_id: Optional[str] = None
    position_order: int = 0


class TetrisReorderRequest(BaseModel):
    """배치 순서 변경 요청."""

    position_order: int


class TetrisAssignResponse(BaseModel):
    """배치 저장/이동/순서변경 응답."""

    id: str
    source_site: str
    brand_name: str
    market_account_id: str
    policy_id: Optional[str]
    position_order: int


class TetrisSyncIntervalRequest(BaseModel):
    """자동 sync 인터벌 설정 요청."""

    interval_hours: int


class TetrisSyncIntervalResponse(BaseModel):
    """자동 sync 인터벌 설정 응답."""

    interval_hours: int
    cancelled: int = 0


class TetrisSyncResponse(BaseModel):
    """수동 sync 실행 응답."""

    assignments: int
    jobs: int
    triggered: int
    skipped: bool = False
    paused: bool = False
    cancelled_before_sync: int = 0


class TetrisExcludeRequest(BaseModel):
    """배제 토글 요청 — 레거시 블럭도 처리 가능 (assignment 자동 생성)."""

    source_site: str
    brand_name: str
    market_account_id: str
    excluded: bool

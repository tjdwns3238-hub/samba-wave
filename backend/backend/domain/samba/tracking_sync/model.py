"""송장 자동전송 잡 모델."""

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import String
from sqlmodel import Column, DateTime, Field, JSON, SQLModel, Text

from ulid import ULID


def generate_tracking_sync_job_id() -> str:
    return f"tsj_{ULID()}"


# 잡 상태 — service.py에서만 변경
STATUS_PENDING = "PENDING"  # 큐 적재 직후
STATUS_DISPATCHED = "DISPATCHED"  # 확장앱이 잡 받음
STATUS_SCRAPED = "SCRAPED"  # 운송장 추출 성공, 마켓 전송 대기
STATUS_SENT = "SENT_TO_MARKET"  # 마켓 dispatch 완료
STATUS_FAILED = "FAILED"  # 추출 실패
STATUS_DISPATCH_FAILED = (
    "DISPATCH_FAILED"  # 추출은 성공했으나 마켓 전송 실패 (재시도 대상)
)
STATUS_NO_TRACKING = "NO_TRACKING"  # 소싱처에 송장 아직 없음 (재시도 대상)
STATUS_WRONG_ACCOUNT = "WRONG_ACCOUNT"  # 현재 로그인된 소싱처 계정과 주문 계정 불일치 (해당 계정 PC에서 재시도 필요)
STATUS_CANCELLED = "CANCELLED"  # 소싱처에서 원주문이 취소됨 (재시도 안함)


class SambaTrackingSyncJob(SQLModel, table=True):
    """소싱처 송장 추출 + 마켓 자동전송 잡."""

    __tablename__ = "samba_tracking_sync_job"

    id: str = Field(
        default_factory=generate_tracking_sync_job_id,
        primary_key=True,
        max_length=30,
    )
    tenant_id: Optional[str] = Field(
        default=None, sa_column=Column(String, index=True, nullable=True)
    )
    order_id: str = Field(sa_column=Column(Text, nullable=False))
    sourcing_site: str = Field(sa_column=Column(Text, nullable=False))
    sourcing_order_number: str = Field(sa_column=Column(Text, nullable=False))
    sourcing_account_id: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    # 확장앱 잡 라우팅용 — 소싱처 계정과 매핑된 PC의 deviceId
    owner_device_id: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    # SourcingQueue가 발급한 requestId (잡 결과 매칭 키)
    request_id: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    status: str = Field(default=STATUS_PENDING, sa_column=Column(Text, nullable=False))
    attempts: int = Field(default=0)
    last_error: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    scraped_courier: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    scraped_tracking: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    scraped_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    dispatched_to_market_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    dispatch_result: Optional[Any] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )
    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )

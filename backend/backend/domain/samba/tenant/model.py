"""테넌트(고객사) 모델 — 멀티테넌시 SaaS 기반."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, String, Boolean, JSON, DateTime, text
from sqlmodel import SQLModel, Field
from ulid import ULID


UTC = timezone.utc


class SambaTenant(SQLModel, table=True):
    """테넌트(고객사) — 각 고객의 데이터 격리 단위."""

    __tablename__ = "samba_tenants"

    id: str = Field(
        default_factory=lambda: f"tn_{ULID()}",
        sa_column=Column(String, primary_key=True),
    )
    name: str = Field(sa_column=Column(String, nullable=False))  # 사업자명
    owner_user_id: str = Field(
        default="", sa_column=Column(String, nullable=False)
    )  # 최초 생성 User ID
    plan: str = Field(
        default="free", sa_column=Column(String, nullable=False)
    )  # free / basic / pro / enterprise
    limits: Optional[dict] = Field(
        default_factory=lambda: {
            "max_products": 1000,
            "max_markets": 3,
            "max_sourcing": 2,
        },
        sa_column=Column(JSON, nullable=True),
    )
    # 구독 기간 관리
    subscription_start: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    subscription_end: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    autotune_enabled: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="false"),
    )

    is_active: bool = Field(default=True, sa_column=Column(Boolean, default=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), server_default=text("now()")),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), server_default=text("now()")),
    )

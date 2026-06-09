"""SambaWave Policy (가격정책) domain model."""

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Boolean, String
from sqlmodel import Column, DateTime, Field, JSON, SQLModel, Text

from ulid import ULID


def generate_policy_id() -> str:
    return f"pol_{ULID()}"


class SambaPolicy(SQLModel, table=True):
    """가격정책 테이블."""

    __tablename__ = "samba_policy"

    id: str = Field(
        default_factory=generate_policy_id,
        primary_key=True,
        max_length=30,
    )
    # 테넌트 격리
    tenant_id: Optional[str] = Field(
        default=None, sa_column=Column(String, index=True, nullable=True)
    )

    name: str = Field(
        default="새 정책",
        sa_column=Column(Text, nullable=False),
    )
    site_name: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # 가격 계산 설정 (JSON)
    # {
    #   shippingCost, marginRate, marginAmount, useRangeMargin,
    #   rangeMargins: [{min, max, rate, amount}],
    #   extraCharge, minMarginAmount, discountRate, discountAmount, ...
    # }
    pricing: Optional[Any] = Field(default=None, sa_column=Column(JSON, nullable=True))

    # 마켓별 정책 오버라이드 (JSON)
    market_policies: Optional[Any] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )

    # 부가 설정 (상세페이지 템플릿, 상품명 규칙, 금지어/삭제어 등)
    extras: Optional[Any] = Field(default=None, sa_column=Column(JSON, nullable=True))

    # Timestamps
    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )
    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )


def generate_detail_template_id() -> str:
    return f"dt_{ULID()}"


def generate_name_rule_id() -> str:
    return f"nr_{ULID()}"


class SambaDetailTemplate(SQLModel, table=True):
    """상세페이지 템플릿 테이블."""

    __tablename__ = "samba_detail_template"

    id: str = Field(
        default_factory=generate_detail_template_id,
        primary_key=True,
        max_length=30,
    )
    # 테넌트 격리
    tenant_id: Optional[str] = Field(
        default=None, sa_column=Column(String, index=True, nullable=True)
    )

    name: str = Field(
        default="새 템플릿",
        sa_column=Column(Text, nullable=False),
    )
    # 대표이미지 번호 (0-based index, -1이면 랜덤)
    main_image_index: int = Field(default=0)
    # 상세페이지 상단/하단 HTML
    top_html: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    bottom_html: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    # 상단/하단 이미지 S3 키
    top_image_s3_key: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    bottom_image_s3_key: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    # 상세페이지 이미지 체크 설정 (어떤 항목을 포함할지)
    img_checks: Optional[dict] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    # 상세페이지 이미지 순서 설정
    img_order: Optional[list] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    # 마켓 썸네일/갤러리(Image1~N) 추가이미지 포함 여부 (#342)
    # 상세페이지 img_checks.sub 와 독립 — 상세는 상세대로, 갤러리는 이 토글대로.
    # 기본 True = #309 이전 동작(갤러리에 추가이미지 전부). 기존 데이터는
    # 마이그레이션에서 img_checks.sub 값으로 backfill 하여 현행 동작 보존.
    gallery_include_sub: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default="true"),
    )

    # Timestamps
    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )
    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )


class SambaNameRule(SQLModel, table=True):
    """상품/옵션명 규칙 테이블."""

    __tablename__ = "samba_name_rule"

    id: str = Field(
        default_factory=generate_name_rule_id,
        primary_key=True,
        max_length=30,
    )
    # 테넌트 격리
    tenant_id: Optional[str] = Field(
        default=None, sa_column=Column(String, index=True, nullable=True)
    )

    name: str = Field(
        default="새 규칙",
        sa_column=Column(Text, nullable=False),
    )
    # 상품명 앞/뒤 추가 텍스트
    prefix: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    suffix: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    # 치환 규칙 목록 (JSON): [{from: str, to: str, caseInsensitive?: bool}]
    replacements: Optional[Any] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    # 치환 방식: simultaneous(동시) / sequential(순차)
    replace_mode: str = Field(
        default="simultaneous",
        sa_column=Column(Text, nullable=False, server_default="simultaneous"),
    )
    # 옵션명 변환 규칙 (JSON): [{from: str, to: str}]
    option_rules: Optional[Any] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    # 상품명 조합 (JSON): ["{상품명}", "{브랜드명}", ...] 순서 배열
    name_composition: Optional[Any] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    # 브랜드명 위치 처리: show_at_position(조합위치에표시) / show_once(중복제거)
    brand_display: str = Field(
        default="show_at_position",
        sa_column=Column(Text, nullable=False, server_default="show_at_position"),
    )
    # 마켓별 상품명 조합 (JSON): { "smartstore": ["{상품명}", ...], "coupang": [...] }
    market_name_compositions: Optional[Any] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    # 마켓별 접두어/접미어 (JSON): { "coupang": "매장정품", ... }
    # 마켓별 값이 있으면 전역 prefix/suffix 대신 사용(없으면 전역값 폴백)
    market_prefixes: Optional[Any] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    market_suffixes: Optional[Any] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    # 중복단어 필터링 활성화
    dedup_enabled: bool = Field(
        default=True, sa_column=Column(Boolean, nullable=False, server_default="true")
    )

    # Timestamps
    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )
    updated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )

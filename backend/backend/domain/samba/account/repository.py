"""SambaWave Account repository."""

from typing import Optional

from sqlalchemy import select

from backend.domain.shared.base_repository import BaseRepository
from backend.domain.samba.account.model import SambaMarketAccount


class SambaMarketAccountRepository(BaseRepository[SambaMarketAccount]):
    def __init__(self, session):
        super().__init__(session, SambaMarketAccount)

    async def find_default(
        self,
        market_type: str,
        tenant_id: Optional[str],
    ) -> Optional[SambaMarketAccount]:
        """market_type + tenant_id 의 기본(is_default=True) 활성 계정 1건 조회.

        store_* samba_settings 단일 키 폴백을 대체하는 진실의 출처.
        기본 계정 없으면 가장 최근 updated_at 활성 계정으로 폴백.
        """
        stmt = (
            select(SambaMarketAccount)
            .where(SambaMarketAccount.market_type == market_type)
            .where(SambaMarketAccount.is_active.is_(True))
        )
        if tenant_id is not None:
            stmt = stmt.where(SambaMarketAccount.tenant_id == tenant_id)
        else:
            stmt = stmt.where(SambaMarketAccount.tenant_id.is_(None))

        # 1순위: is_default=True
        default_stmt = stmt.where(SambaMarketAccount.is_default.is_(True)).limit(1)
        result = await self.session.execute(default_stmt)
        account = result.scalars().first()
        if account is not None:
            return account

        # 2순위: updated_at desc 활성 계정
        fallback_stmt = stmt.order_by(SambaMarketAccount.updated_at.desc()).limit(1)
        result = await self.session.execute(fallback_stmt)
        return result.scalars().first()

    async def list_by_market(
        self,
        market_type: str,
        tenant_id: Optional[str],
    ) -> list[SambaMarketAccount]:
        """market_type + tenant_id 의 모든 활성 계정 목록 (default 우선, updated_at desc)."""
        stmt = (
            select(SambaMarketAccount)
            .where(SambaMarketAccount.market_type == market_type)
            .where(SambaMarketAccount.is_active.is_(True))
            .order_by(
                SambaMarketAccount.is_default.desc(),
                SambaMarketAccount.updated_at.desc(),
            )
        )
        if tenant_id is not None:
            stmt = stmt.where(SambaMarketAccount.tenant_id == tenant_id)
        else:
            stmt = stmt.where(SambaMarketAccount.tenant_id.is_(None))

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

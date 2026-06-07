"""SambaWave Collector repository."""

from typing import List

from sqlalchemy import cast, func, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import select

from backend.domain.shared.base_repository import BaseRepository
from backend.domain.samba.collector.model import (
    SambaCollectedProduct,
    SambaSearchFilter,
)


class SambaSearchFilterRepository(BaseRepository[SambaSearchFilter]):
    def __init__(self, session):
        super().__init__(session, SambaSearchFilter)


class SambaCollectedProductRepository(BaseRepository[SambaCollectedProduct]):
    def __init__(self, session):
        super().__init__(session, SambaCollectedProduct)

    async def bulk_delete(self, ids: list[str]) -> int:
        """Delete products through the ORM path so single/bulk behavior matches."""
        if not ids:
            return 0

        stmt = select(SambaCollectedProduct).where(SambaCollectedProduct.id.in_(ids))
        result = await self.session.execute(stmt)
        products = list(result.scalars().all())
        for product in products:
            await self.session.delete(product)
        await self.session.commit()
        return len(products)

    async def search(self, query: str, limit: int = 100) -> List[SambaCollectedProduct]:
        from backend.core.sql_safe import escape_like

        lower_q = f"%{escape_like(query.lower())}%"
        stmt = (
            select(SambaCollectedProduct)
            .where(
                or_(
                    SambaCollectedProduct.name.ilike(lower_q, escape="\\"),
                    SambaCollectedProduct.brand.ilike(lower_q, escape="\\"),
                    SambaCollectedProduct.source_site.ilike(lower_q, escape="\\"),
                )
            )
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_status(
        self, status: str, limit: int = 200
    ) -> List[SambaCollectedProduct]:
        return await self.filter_by_async(
            status=status, order_by="created_at", order_by_desc=True, limit=limit
        )

    async def list_by_filters(
        self,
        status: str | None = None,
        source_site: str | None = None,
        limit: int = 200,
    ) -> List[SambaCollectedProduct]:
        """status, source_site 조합 필터링."""
        kwargs: dict = {"order_by": "created_at", "order_by_desc": True, "limit": limit}
        if status:
            kwargs["status"] = status
        if source_site:
            kwargs["source_site"] = source_site
        return await self.filter_by_async(**kwargs)

    async def list_by_filter(
        self, search_filter_id: str, skip: int = 0, limit: int = 10000
    ) -> List[SambaCollectedProduct]:
        """필터에 속한 전체 상품 조회 (정책 전파 등에 사용).

        주의: 정책 전파 등 전체 순회가 필요한 경우 iter_by_filter() 사용 권장
        (10,000개 limit 우회 + 메모리 절약).
        """
        return await self.filter_by_async(
            search_filter_id=search_filter_id,
            skip=skip,
            limit=limit,
            order_by="created_at",
            order_by_desc=True,
        )

    async def iter_by_filter(self, search_filter_id: str, batch_size: int = 1000):
        """필터에 속한 모든 상품을 id 기준 cursor 페이지네이션으로 yield.

        list_by_filter 의 10,000개 limit 우회용. 정책 전파 등 전체 순회 시 사용.
        """
        last_id = ""
        while True:
            stmt = (
                select(SambaCollectedProduct)
                .where(
                    SambaCollectedProduct.search_filter_id == search_filter_id,
                    SambaCollectedProduct.id > last_id,
                )
                .order_by(SambaCollectedProduct.id.asc())
                .limit(batch_size)
            )
            result = await self.session.execute(stmt)
            batch = list(result.scalars().all())
            if not batch:
                return
            for p in batch:
                yield p
            last_id = batch[-1].id

    @staticmethod
    def _tenant_filter(tenant_id):
        """tenant_id None이면 IS NULL, 있으면 = 조건."""
        if tenant_id is None:
            return SambaCollectedProduct.tenant_id.is_(None)
        return SambaCollectedProduct.tenant_id == tenant_id

    async def get_registered_name_keys(self, tenant_id) -> tuple[set, set]:
        """마켓 등록된 상품의 (name_set, (source_site, site_product_id)_set) 반환."""

        stmt = select(
            SambaCollectedProduct.name,
            SambaCollectedProduct.source_site,
            SambaCollectedProduct.site_product_id,
        ).where(
            self._tenant_filter(tenant_id),
            SambaCollectedProduct.registered_accounts.isnot(None),
            func.jsonb_typeof(SambaCollectedProduct.registered_accounts) == "array",
            SambaCollectedProduct.registered_accounts.op("!=")(cast("[]", JSONB)),
        )
        result = await self.session.execute(stmt)
        rows = result.all()
        name_set = {(r[0] or "").strip() for r in rows if r[0]}
        key_set = {(r[1], r[2]) for r in rows if r[1] and r[2]}
        return name_set, key_set

    async def get_registered_name_keys_by_tenants(
        self, tenant_ids
    ) -> dict[str, tuple[set, set]]:
        """여러 tenant의 등록상품 (name_set, key_set)을 1쿼리로 묶어 반환.

        기존 get_registered_name_keys 를 tenant 수만큼 루프 호출하던 N+1 제거.
        반환 키 = tenant_id 문자열, None tenant 는 '__null__'.
        WHERE 술어는 단일 버전과 동일하게 유지해 결과가 동일하다.
        """
        tids = list(tenant_ids)
        if not tids:
            return {}

        has_null = any(t is None for t in tids)
        non_null = [t for t in tids if t is not None]
        tenant_conds = []
        if non_null:
            tenant_conds.append(SambaCollectedProduct.tenant_id.in_(non_null))
        if has_null:
            tenant_conds.append(SambaCollectedProduct.tenant_id.is_(None))

        stmt = select(
            SambaCollectedProduct.tenant_id,
            SambaCollectedProduct.name,
            SambaCollectedProduct.source_site,
            SambaCollectedProduct.site_product_id,
        ).where(
            or_(*tenant_conds),
            SambaCollectedProduct.registered_accounts.isnot(None),
            func.jsonb_typeof(SambaCollectedProduct.registered_accounts) == "array",
            SambaCollectedProduct.registered_accounts.op("!=")(cast("[]", JSONB)),
        )
        result = await self.session.execute(stmt)

        # 호출 측이 요청한 모든 tenant 키를 빈 set 으로 선초기화(누락 키 없게)
        out: dict[str, tuple[set, set]] = {
            (str(t) if t is not None else "__null__"): (set(), set()) for t in tids
        }
        for tid, name, site, spid in result.all():
            key = str(tid) if tid is not None else "__null__"
            names, keys = out.setdefault(key, (set(), set()))
            if name:
                names.add((name or "").strip())
            if site and spid:
                keys.add((site, spid))
        return out

    async def find_duplicates(
        self,
        tenant_id,
        source_site: str | None = None,
        filter_ids: list[str] | None = None,
    ) -> list:
        """동일 name이 2개 이상이며 그 중 마켓 등록 상품이 포함된 그룹 전체 반환.
        filter_ids 지정 시 해당 search_filter_id 상품만 대상 (드릴 컨텍스트 정밀 필터).
        source_site 지정 시 해당 소싱처만 대상 (filter_ids 없을 때 사용).
        """

        tf = self._tenant_filter(tenant_id)
        if filter_ids:
            fc: list = [SambaCollectedProduct.search_filter_id.in_(filter_ids)]
            sc: list = (
                [SambaCollectedProduct.source_site == source_site]
                if source_site
                else []
            )
        else:
            fc = []
            sc = (
                [SambaCollectedProduct.source_site == source_site]
                if source_site
                else []
            )

        registered_names_sq = (
            select(SambaCollectedProduct.name)
            .where(
                tf,
                *sc,
                *fc,
                SambaCollectedProduct.registered_accounts.isnot(None),
                func.jsonb_typeof(SambaCollectedProduct.registered_accounts) == "array",
                SambaCollectedProduct.registered_accounts.op("!=")(cast("[]", JSONB)),
            )
            .distinct()
        ).subquery()

        dup_names_sq = (
            select(SambaCollectedProduct.name)
            .where(
                tf,
                *sc,
                *fc,
                SambaCollectedProduct.name.in_(select(registered_names_sq.c.name)),
            )
            .group_by(SambaCollectedProduct.name)
            .having(func.count() > 1)
        ).subquery()

        stmt = (
            select(SambaCollectedProduct)
            .where(
                tf,
                *sc,
                *fc,
                SambaCollectedProduct.name.in_(select(dup_names_sq.c.name)),
            )
            .order_by(
                SambaCollectedProduct.name,
                SambaCollectedProduct.created_at,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_by_market_name_and_account(
        self,
        tenant_id,
        market_key: str,
        product_name: str,
        account_id: str,
        exclude_product_id: int | None = None,
    ) -> "SambaCollectedProduct | None":
        """동일 마켓 계정에 같은 등록상품명이 이미 등록된 상품 조회.

        market_key: market_names의 키 (예: "스마트스토어")
        account_id: registered_accounts 배열에서 확인할 계정 ID 문자열
        """

        stmt = select(SambaCollectedProduct).where(
            self._tenant_filter(tenant_id),
            cast(SambaCollectedProduct.market_names, JSONB)[market_key].astext
            == product_name,
            SambaCollectedProduct.registered_accounts.op("@>")(
                cast(f'["{account_id}"]', JSONB)
            ),
        )
        if exclude_product_id is not None:
            stmt = stmt.where(SambaCollectedProduct.id != exclude_product_id)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def bulk_update_by_filter(self, search_filter_id: str, **kwargs) -> int:
        """search_filter_id에 해당하는 모든 상품을 한 번의 쿼리로 업데이트."""
        from sqlalchemy import update

        stmt = (
            update(SambaCollectedProduct)
            .where(SambaCollectedProduct.search_filter_id == search_filter_id)
            .values(**kwargs)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount

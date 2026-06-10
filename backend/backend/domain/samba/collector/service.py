"""SambaWave Collector service."""

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from backend.domain.samba.collector.model import (
    FIXED_REQUESTED_COUNT,
    SambaCollectedProduct,
    SambaSearchFilter,
)
from backend.domain.samba.collector.repository import (
    SambaCollectedProductRepository,
    SambaSearchFilterRepository,
)

# 브랜드/제조사 필드의 의미없는 플레이스홀더 값
_BRAND_PLACEHOLDERS = {
    "상세참조",
    "상품상세참조",
    "상세설명참조",
    "상세페이지참조",
    "상세 참조",
}

# 제조사 필드에 잘못 들어가는 국가명 (소싱처가 제조사 칸에 국가를 입력한 케이스)
# 예: 무신사 HOKA 상품의 경우 essential API가 제조사="베트남"으로 잘못 반환
_COUNTRY_NAMES = {
    "한국",
    "대한민국",
    "중국",
    "중화인민공화국",
    "베트남",
    "일본",
    "미국",
    "인도",
    "인도네시아",
    "방글라데시",
    "캄보디아",
    "태국",
    "미얀마",
    "파키스탄",
    "필리핀",
    "말레이시아",
    "싱가포르",
    "홍콩",
    "대만",
    "스리랑카",
    "터키",
    "튀르키예",
    "이탈리아",
    "독일",
    "프랑스",
    "영국",
    "스페인",
    "포르투갈",
    "폴란드",
    "체코",
    "헝가리",
    "루마니아",
    "스위스",
    "네덜란드",
    "벨기에",
    "오스트리아",
    "러시아",
    "우크라이나",
    "멕시코",
    "브라질",
    "페루",
    "캐나다",
    "호주",
    "뉴질랜드",
    "모로코",
    "이집트",
    "남아프리카공화국",
}


def _is_placeholder(value: str) -> bool:
    """브랜드/제조사 필드의 플레이스홀더 값 여부 판별."""
    return value.strip() in _BRAND_PLACEHOLDERS


def _looks_like_country(value: str) -> bool:
    """제조사 필드에 잘못 들어간 국가명인지 판별."""
    return value.strip() in _COUNTRY_NAMES


def _apply_preserved_system_tags(
    existing_tags: list | None, new_tags: list | None
) -> list:
    """기존 tags의 시스템 태그(__로 시작)를 new_tags에 머지하여 반환.

    재수집 IntegrityError fallback에서 새 태그로 통째 덮어쓸 때
    __ai_image__/__img_edited__/__ai_tagged__ 같은 시스템 마커가 유실되는
    것을 방지한다 (#233). 사용자 태그는 보존 대상 아님(덮어쓰기 정책 유지).
    """
    preserved = [
        t for t in (existing_tags or []) if isinstance(t, str) and t.startswith("__")
    ]
    merged = list(new_tags or [])
    for t in preserved:
        if t not in merged:
            merged.append(t)
    return merged


def _derive_sale_status(data: Dict[str, Any]) -> None:
    """전옵션 품절이면 sale_status를 sold_out으로 자동 설정."""
    if data.get("sale_status") and data["sale_status"] != "in_stock":
        return
    options = data.get("options")
    if not options or not isinstance(options, list) or len(options) == 0:
        return
    if all((opt.get("stock", 0) or 0) <= 0 for opt in options if isinstance(opt, dict)):
        data["sale_status"] = "sold_out"


def _norm_brand_key(value: str | None) -> str:
    return "".join((value or "").split()).casefold()


class SambaCollectorService:
    def __init__(
        self,
        filter_repo: SambaSearchFilterRepository,
        product_repo: SambaCollectedProductRepository,
    ):
        self.filter_repo = filter_repo
        self.product_repo = product_repo

    # ==================== Search Filters ====================

    async def list_filters(
        self, skip: int = 0, limit: int = 50
    ) -> List[SambaSearchFilter]:
        return await self.filter_repo.list_async(
            skip=skip, limit=limit, order_by="-created_at"
        )

    async def create_filter(self, data: Dict[str, Any]) -> SambaSearchFilter:
        if not data.get("is_folder") and "requested_count" not in data:
            data["requested_count"] = FIXED_REQUESTED_COUNT
        # #402 — 재수집 시 동일 (tenant, source_site, name) 비-폴더 필터가 중복 생성되는 문제 방지.
        # 이미 같은 그룹이 있으면 새로 만들지 않고 기존을 재사용(keyword/요청수만 최신화)한다.
        # tenant 스코프는 ORM 자동필터(tenant_filter)가 처리 — SambaSearchFilter 에 tenant_id 컬럼 존재.
        if not data.get("is_folder"):
            name = data.get("name")
            source_site = data.get("source_site")
            if name and source_site:
                existing = await self.filter_repo.find_by_async(
                    is_folder=False, source_site=source_site, name=name
                )
                if existing:
                    updates: Dict[str, Any] = {}
                    for key in ("keyword", "requested_count"):
                        if key in data and getattr(existing, key, None) != data[key]:
                            updates[key] = data[key]
                    if updates:
                        refreshed = await self.filter_repo.update_async(
                            existing.id, **updates
                        )
                        if refreshed is not None:
                            return refreshed
                    return existing
        return await self.filter_repo.create_async(**data)

    async def update_filter(
        self, filter_id: str, data: Dict[str, Any]
    ) -> Optional[SambaSearchFilter]:
        return await self.filter_repo.update_async(filter_id, **data)

    async def delete_filter(self, filter_id: str) -> bool:
        return await self.filter_repo.delete_async(filter_id)

    # ==================== Collected Products ====================

    async def list_collected_products(
        self,
        skip: int = 0,
        limit: int = 50,
        status: Optional[str] = None,
        source_site: Optional[str] = None,
    ) -> List[SambaCollectedProduct]:
        if status and source_site:
            return await self.product_repo.list_by_filters(
                status=status, source_site=source_site, limit=limit
            )
        if status:
            return await self.product_repo.list_by_status(status, limit=limit)
        if source_site:
            return await self.product_repo.list_by_filters(
                source_site=source_site, limit=limit
            )
        return await self.product_repo.list_async(
            skip=skip, limit=limit, order_by="-created_at"
        )

    async def get_collected_product(
        self, product_id: str
    ) -> Optional[SambaCollectedProduct]:
        return await self.product_repo.get_async(product_id)

    async def _exists_by_name(
        self,
        tenant_id: Optional[str],
        source_site: str,
        name: str,
        exclude_site_product_id: Optional[str] = None,
    ) -> bool:
        """동일 소싱처 내 동일 원 상품명 존재 여부 (삭제 포함)."""
        q = select(SambaCollectedProduct).where(
            SambaCollectedProduct.tenant_id == tenant_id,
            SambaCollectedProduct.source_site == source_site,
            SambaCollectedProduct.name == name.strip(),
        )
        if exclude_site_product_id:
            q = q.where(
                SambaCollectedProduct.site_product_id != exclude_site_product_id
            )
        result = await self.product_repo.session.execute(q.limit(1))
        return result.scalars().first() is not None

    async def _ensure_category_mapping_row(
        self,
        source_site: str,
        source_category: str,
        tenant_id: str | None = None,
    ) -> None:
        """수집된 (source_site, source_category) 가 매핑 테이블에 없으면 INSERT.

        TASK 5(상품 수집) ↔ TASK 1(카테고리 매핑) 누락 방지: 수집 시점에 빈 매핑 행을
        만들어 두면 TASK 1 또는 사용자가 매핑 작업할 때 누락 없이 인식된다.
        target_mappings 는 빈 객체로 시작.
        """
        if not source_site or not source_category:
            return
        from sqlalchemy import text  # noqa: F811
        from ulid import ULID  # noqa: F811

        try:
            await self.product_repo.session.execute(
                text(
                    """
                    INSERT INTO samba_category_mapping
                        (id, tenant_id, source_site, source_category,
                         target_mappings, created_at, updated_at)
                    VALUES
                        (:id, :tid, :ss, :sc, '{}'::json, NOW(), NOW())
                    ON CONFLICT (source_site, source_category) DO NOTHING
                    """
                ),
                {
                    "id": f"cm_{ULID()}",
                    "tid": tenant_id,
                    "ss": source_site,
                    "sc": source_category,
                },
            )
        except Exception as e:
            logger.warning(
                f"[카테고리매핑] UPSERT 실패 (무시) — {source_site}/{source_category}: {e}"
            )

    async def _backfill_tenant_id(self, data: Dict[str, Any]) -> None:
        """tenant_id 누락 시 search_filter의 tenant_id로 자동 채움.

        중복 상품 수집 재발방지(2026-05-10): 수집 경로에서 tenant_id를 빠뜨려
        (NULL, source_site, site_product_id) 조합이 유니크 인덱스를 우회하던 버그
        대응. 누락된 모든 호출 경로를 한 곳에서 흡수.
        """
        if data.get("tenant_id"):
            return
        fid = data.get("search_filter_id")
        if not fid:
            return
        try:
            sf = await self.filter_repo.get_async(fid)
        except Exception:
            return
        if sf and sf.tenant_id:
            data["tenant_id"] = sf.tenant_id

    async def create_collected_product(
        self, data: Dict[str, Any]
    ) -> Optional[SambaCollectedProduct]:
        self._sanitize_kream_data(data)
        self._clean_company_names(data)
        await self._backfill_tenant_id(data)
        await self._fill_source_brand(data)
        await self._inherit_group_attributes(data)
        _derive_sale_status(data)
        # 동일 소싱처 내 동일 원 상품명 차단은 비활성화 — 색상/사이즈별 SKU가
        # 별개 site_product_id로 등록되는 소싱처(ABCmart/MUSINSA/LOTTEON/SSG/Nike 등)에서
        # 동명 상품이 정상이며, 차단 시 수집량이 크게 줄어 누락 발생.
        # 진짜 중복은 (source_site, site_product_id) 유니크 제약 + IntegrityError 핸들러로 처리.
        # 블랙리스트 체크 — 수집차단된 상품 재수집 방지 (모든 소싱처 공통)
        _src = data.get("source_site", "")
        _spid = data.get("site_product_id", "")
        if _src and _spid:
            from backend.api.v1.routers.samba.collector_common import _is_blacklisted

            if await _is_blacklisted(self.product_repo.session, _src, _spid):
                return None
        # 카테고리 매핑 테이블에 (source_site, category) 행 미리 보장
        await self._ensure_category_mapping_row(
            _src, data.get("category", ""), data.get("tenant_id")
        )
        try:
            return await self.product_repo.create_async(**data)
        except IntegrityError:
            # 다른 검색필터에서 이미 수집된 상품 → 기존 상품 업데이트
            await self.product_repo.session.rollback()
            # tenant_id NULL-safe 필터 — 멀티테넌시에서 다른 테넌트 row를
            # 잘못 가져오는 것을 방지 (2026-05-10 중복 수집 재발방지)
            _tid = data.get("tenant_id")
            existing = (
                (
                    await self.product_repo.session.execute(
                        select(SambaCollectedProduct).where(
                            self.product_repo._tenant_filter(_tid),
                            SambaCollectedProduct.source_site
                            == data.get("source_site"),
                            SambaCollectedProduct.site_product_id
                            == data.get("site_product_id"),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing:
                # 시스템 태그(__로 시작) 보존 — AI 이미지 변환/편집 흔적 유실 방지 (#227, #233)
                prev_tags = list(existing.tags or [])
                for k, v in data.items():
                    if k not in ("id", "source_site", "site_product_id", "created_at"):
                        setattr(existing, k, v)
                existing.tags = _apply_preserved_system_tags(prev_tags, existing.tags)
                await self.product_repo.session.flush()
                return existing
            raise

    async def _fill_source_brand(self, data: Dict[str, Any]) -> None:
        """검색필터의 source_brand_name으로 빈 brand/manufacturer 자동 채움."""
        fid = data.get("search_filter_id")
        if not fid:
            return
        sf = await self.filter_repo.get_async(fid)
        if not sf or not sf.source_brand_name:
            return
        brand = sf.source_brand_name
        if not (data.get("brand") or "").strip():
            data["brand"] = brand
        mfr = (data.get("manufacturer") or "").strip()
        if not mfr or _is_placeholder(mfr):
            data["manufacturer"] = brand

    async def _inherit_group_attributes(self, data: Dict[str, Any]) -> None:
        """같은 그룹 기존 상품의 태그/SEO/정책/마켓가격을 신규 상품에 상속."""
        fid = data.get("search_filter_id")
        if not fid:
            return
        # 이미 설정된 값은 덮어쓰지 않음
        if (
            data.get("tags")
            or data.get("seo_keywords")
            or data.get("applied_policy_id")
        ):
            return
        # 태그/SEO/정책: SearchFilter 전체에서 참조 (같은 검색그룹이면 공유)
        filter_refs = await self.product_repo.list_by_filter(fid, limit=1)
        # market_prices: group_key 단위로 참조 (동일 SKU 패밀리에서만 의미 있음)
        group_refs: list = []
        group_key = (data.get("group_key") or "").strip()
        if group_key:
            group_refs = await self.product_repo.filter_by_async(
                search_filter_id=fid,
                group_key=group_key,
                limit=1,
                order_by="created_at",
                order_by_desc=True,
            )
        if filter_refs:
            ref = filter_refs[0]
            # 태그 복사 (내부 시스템 태그 제외)
            ref_tags = [t for t in (ref.tags or []) if not t.startswith("__")]
            if ref_tags:
                data["tags"] = ref_tags + ["__ai_tagged__"]
            # SEO 키워드 복사
            if ref.seo_keywords:
                data["seo_keywords"] = list(ref.seo_keywords)
            # 정책 복사
            if ref.applied_policy_id:
                data["applied_policy_id"] = ref.applied_policy_id
        # 기존 상품에서 정책 못 가져왔으면 SearchFilter 자체에서 fallback(이슈#277)
        # — 같은 필터 첫 수집(기존 상품 0건)이면 filter_refs=[]라 위 블록 skip되어
        #   applied_policy_id=NULL 저장 → 이후 수집도 NULL ref 받아 도미노 전파
        if not data.get("applied_policy_id"):
            sf = await self.filter_repo.get_async(fid)
            if sf and sf.applied_policy_id:
                data["applied_policy_id"] = sf.applied_policy_id
        if group_refs:
            gref = group_refs[0]
            # 마켓 가격 복사 (동일 모델 SKU 패밀리 내에서만)
            if gref.market_prices:
                data["market_prices"] = dict(gref.market_prices)

    def prepare_product_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """전처리만 수행 (배치 저장용). DB 저장은 별도."""
        self._sanitize_kream_data(data)
        self._clean_company_names(data)
        return data

    async def bulk_create_products(self, items: list[Dict[str, Any]]) -> int:
        """배치 INSERT — 전처리 완료된 데이터 리스트.
        검색필터에 정책이 설정되어 있으면 신규 상품에 자동 적용한다.
        모든 소싱처(무신사/롯데온/SSG 등)가 이 함수를 공통 사용.
        """
        if not items:
            return 0
        # 검색필터의 정책을 신규 상품에 자동 적용
        filter_ids = {
            item.get("search_filter_id")
            for item in items
            if item.get("search_filter_id")
        }
        filter_policy_map: Dict[str, str] = {}
        filter_brand_map: Dict[str, str] = {}
        if filter_ids:
            filters = await self.filter_repo.filter_by_async(limit=len(filter_ids) + 10)
            for f in filters:
                if f.id in filter_ids:
                    if f.applied_policy_id:
                        filter_policy_map[f.id] = f.applied_policy_id
                    if f.source_brand_name:
                        filter_brand_map[f.id] = f.source_brand_name
        # filter_id → tenant_id 매핑 (중복 수집 재발방지: tenant_id 누락 시 자동 채움)
        filter_tenant_map: Dict[str, str] = {}
        if filter_ids:
            # filter_policy_map 빌드 시 이미 filters를 가져왔지만 스코프가 분리됨 → 재조회
            tenant_filters = await self.filter_repo.filter_by_async(
                limit=len(filter_ids) + 10
            )
            for f in tenant_filters:
                if f.id in filter_ids and f.tenant_id:
                    filter_tenant_map[f.id] = f.tenant_id
        for item in items:
            if not item.get("applied_policy_id"):
                fid = item.get("search_filter_id", "")
                if fid in filter_policy_map:
                    item["applied_policy_id"] = filter_policy_map[fid]
            # tenant_id 누락 시 search_filter의 tenant_id로 채움
            if not item.get("tenant_id"):
                fid = item.get("search_filter_id", "")
                if fid in filter_tenant_map:
                    item["tenant_id"] = filter_tenant_map[fid]
        # 소싱처 브랜드명으로 빈 brand/manufacturer 자동 채움
        for item in items:
            fid = item.get("search_filter_id", "")
            source_brand = filter_brand_map.get(fid)
            if not source_brand:
                continue
            if not (item.get("brand") or "").strip():
                item["brand"] = source_brand
            mfr = (item.get("manufacturer") or "").strip()
            if not mfr or _is_placeholder(mfr):
                item["manufacturer"] = source_brand
        from backend.domain.samba.collector.model import SambaCollectedProduct
        from sqlalchemy.exc import IntegrityError

        # 마켓 등록된 상품명 + (source_site, site_product_id) 키 사전 조회
        # tenant_id가 None인 경우도 포함 (멀티테넌시 미적용 환경)
        tenant_ids = {d.get("tenant_id") for d in items}
        registered_names_by_tid: Dict[str, set] = {}
        registered_keys_by_tid: Dict[str, set] = {}
        # N+1 제거: tenant 수만큼 쿼리하던 것을 1쿼리 배치로
        by_tenant = await self.product_repo.get_registered_name_keys_by_tenants(
            tenant_ids
        )
        for key, (names, keys) in by_tenant.items():
            registered_names_by_tid[key] = names
            registered_keys_by_tid[key] = keys

        # 동일 소싱처 내 동일 원 상품명 중복 필터링
        source_site_pairs = {(d.get("tenant_id"), d.get("source_site")) for d in items}
        # (tenant_id, source_site, name) → set of site_product_ids
        existing_name_spids: Dict[tuple, set] = {}
        for tid, ss in source_site_pairs:
            rows = (
                await self.product_repo.session.execute(
                    select(
                        SambaCollectedProduct.name,
                        SambaCollectedProduct.site_product_id,
                    ).where(
                        SambaCollectedProduct.tenant_id == tid,
                        SambaCollectedProduct.source_site == ss,
                    )
                )
            ).all()
            for row_name, row_spid in rows:
                k = (str(tid or ""), ss, (row_name or "").strip())
                existing_name_spids.setdefault(k, set()).add(row_spid)
        seen_names: set = set()
        filtered_items: list = []
        for d in items:
            tid = str(d.get("tenant_id") or "")
            ss = d.get("source_site")
            nm = (d.get("name") or "").strip()
            spid = d.get("site_product_id")
            key = (tid, ss, nm)
            existing_spids = existing_name_spids.get(key, set())
            if existing_spids:
                if spid and spid in existing_spids:
                    # 동일 site_product_id 재수집 → upsert 허용
                    pass
                else:
                    # 다른 상품이 동일 이름 → skip
                    continue
            elif key in seen_names:
                # 배치 내 자체 중복 → skip
                continue
            # 마켓 등록된 상품명 차단: 동일 상품(같은 키) 갱신은 허용
            tid_key = tid if tid else "__null__"
            reg_names = registered_names_by_tid.get(tid_key, set())
            reg_keys = registered_keys_by_tid.get(tid_key, set())
            if nm in reg_names and (ss, spid) not in reg_keys:
                continue
            seen_names.add(key)
            filtered_items.append(d)
        items = filtered_items

        created = 0
        # 항목별 savepoint 적용: IntegrityError 발생 시 해당 항목만 롤백하고
        # 이전 루프에서 성공한 항목들은 보존한다 (session.rollback은 트랜잭션 전체를
        # 롤백하므로 직전 성공 항목들이 모두 소실되는 데이터 손실 버그가 있었음).
        for d in items:
            _derive_sale_status(d)
            try:
                async with self.product_repo.session.begin_nested():
                    obj = SambaCollectedProduct(**d)
                    self.product_repo.session.add(obj)
                    await self.product_repo.session.flush()
                created += 1
            except IntegrityError:
                # 동시 수집으로 중복 발생 시 기존 상품 업데이트 (savepoint 자동 롤백)
                # tenant_id NULL-safe 필터 (2026-05-10 중복 수집 재발방지)
                _tid_d = d.get("tenant_id")
                existing = (
                    (
                        await self.product_repo.session.execute(
                            select(SambaCollectedProduct).where(
                                self.product_repo._tenant_filter(_tid_d),
                                SambaCollectedProduct.source_site
                                == d.get("source_site"),
                                SambaCollectedProduct.site_product_id
                                == d.get("site_product_id"),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing:
                    # 시스템 태그(__로 시작) 보존 — 배치 재수집 시 __ai_image__ 등 유실 방지 (#233)
                    prev_tags = list(existing.tags or [])
                    update_fields = {
                        k: v
                        for k, v in d.items()
                        if k
                        not in ("id", "source_site", "site_product_id", "created_at")
                    }
                    try:
                        async with self.product_repo.session.begin_nested():
                            for k, v in update_fields.items():
                                setattr(existing, k, v)
                            existing.tags = _apply_preserved_system_tags(
                                prev_tags, existing.tags
                            )
                            await self.product_repo.session.flush()
                        created += 1
                    except IntegrityError:
                        # 업데이트조차 실패 (희귀 케이스) — 이 항목 스킵, 진행 계속
                        continue
        # 배치 내 distinct (source_site, category) 카테고리 매핑 행 보장
        _seen_cat: set[tuple] = set()
        for d in items:
            _ss = d.get("source_site")
            _sc = d.get("category")
            _tid = d.get("tenant_id")
            _key = (_tid, _ss, _sc)
            if _ss and _sc and _key not in _seen_cat:
                _seen_cat.add(_key)
                await self._ensure_category_mapping_row(_ss, _sc, _tid)
        await self.product_repo.session.commit()
        return created

    async def update_collected_product(
        self, product_id: str, data: Dict[str, Any]
    ) -> Optional[SambaCollectedProduct]:
        self._sanitize_kream_data(data)
        self._clean_company_names(data)
        # tags가 None으로 전달되면 기존 태그를 덮어쓰지 않도록 제거
        # (명시적으로 빈 리스트 []를 보내면 태그 초기화 허용)
        if "tags" in data and data["tags"] is None:
            del data["tags"]
        return await self.product_repo.update_async(product_id, **data)

    @staticmethod
    def _sanitize_kream_data(data: Dict[str, Any]) -> None:
        """비-KREAM 상품의 kream_data 오염 방지.

        확장앱이 무신사 고시정보를 kream_data로 보내는 경우,
        올바른 필드(material, color 등)로 분리하고 kream_data를 제거한다.
        """
        if data.get("source_site") == "KREAM":
            return
        kd = data.get("kream_data")
        if not isinstance(kd, dict):
            return
        field_map = {
            "color": "color",
            "material": "material",
            "brandNation": "origin",
        }
        for kd_key, field in field_map.items():
            if kd.get(kd_key) and not data.get(field):
                data[field] = kd[kd_key]
        data.pop("kream_data", None)

    @staticmethod
    def _clean_company_names(data: Dict[str, Any]) -> None:
        """브랜드/제조사에서 (주), ㈜, (株) 제거.
        제조사가 국가명이거나 제조국과 동일하면 비워서 brand fallback이 동작하게 함.
        """

        _pattern = re.compile(r"\(주\)|㈜|\(株\)")
        for field in ("brand", "manufacturer"):
            val = data.get(field)
            if val and isinstance(val, str):
                cleaned = _pattern.sub("", val).strip()
                if cleaned:
                    data[field] = cleaned

        # 무신사 등 일부 소싱처가 제조사 칸에 국가명을 잘못 넣는 케이스 보정
        mfr = (data.get("manufacturer") or "").strip()
        origin = (data.get("origin") or "").strip()
        if mfr and (_looks_like_country(mfr) or (origin and mfr == origin)):
            data["manufacturer"] = ""

    async def get_duplicate_products(
        self,
        tenant_id,
        source_site: str | None = None,
        filter_ids: list[str] | None = None,
    ) -> list:
        """동일 name 중복 상품 그룹 반환 (원본=가장 먼저 수집된 것, 나머지=중복)."""
        from collections import defaultdict

        products = await self.product_repo.find_duplicates(
            tenant_id, source_site, filter_ids
        )
        groups_map: dict = defaultdict(list)
        for p in products:
            groups_map[(p.name or "").strip()].append(p)

        def _to_dict(p) -> dict:
            return {
                "id": p.id,
                "name": p.name,
                "source_site": p.source_site,
                "brand": p.brand,
                "sale_price": p.sale_price,
                "images": (p.images or [])[:1],
                "registered_accounts": p.registered_accounts,
                "status": p.status,
            }

        def _is_registered(p) -> bool:
            ra = p.registered_accounts
            return bool(ra and ra not in ([], "null", None))

        def _min_pid(p) -> int:
            """market_product_nos 값 중 가장 작은 pid 숫자. 없으면 inf."""
            mpn = p.market_product_nos
            if not mpn or mpn in ({}, None):
                return 2**62
            try:
                pids = [int(v) for v in mpn.values() if str(v).isdigit()]
                return min(pids) if pids else 2**62
            except Exception:
                return 2**62

        result = []
        for name, items in groups_map.items():
            if len(items) <= 1:
                continue
            # 등록된 상품 중 pid가 가장 작은 것(먼저 마켓 등록)을 원본으로
            registered = [p for p in items if _is_registered(p)]
            if registered:
                original = min(registered, key=_min_pid)
            else:
                original = items[0]
            duplicates = [p for p in items if p.id != original.id]
            result.append(
                {
                    "name": name,
                    "total": len(items),
                    "registered": [_to_dict(original)],
                    "duplicates": [_to_dict(p) for p in duplicates],
                }
            )
        return result

    async def delete_collected_product(self, product_id: str) -> bool:
        return await self.product_repo.delete_async(product_id)

    async def bulk_delete_collected_products(self, product_ids: list[str]) -> int:
        return await self.product_repo.bulk_delete(product_ids)

    async def delete_brand_scope(
        self,
        source_site: str,
        brand_name: str,
        tenant_id: Optional[str],
    ) -> dict[str, int]:
        """소싱처+브랜드 기준으로 관련 상품과 그룹을 모두 삭제한다."""
        from sqlalchemy import func, or_, delete as sa_delete
        from sqlmodel import select

        brand_key = _norm_brand_key(brand_name)
        tenant_clause = self.product_repo._tenant_filter(tenant_id)
        filter_tenant_clause = (
            SambaSearchFilter.tenant_id.is_(None)
            if tenant_id is None
            else SambaSearchFilter.tenant_id == tenant_id
        )

        filter_stmt = select(SambaSearchFilter).where(
            filter_tenant_clause,
            SambaSearchFilter.source_site == source_site,
            SambaSearchFilter.is_folder == False,  # noqa: E712
        )
        filter_rows = (
            (await self.product_repo.session.execute(filter_stmt)).scalars().all()
        )
        filter_ids = [
            f.id
            for f in filter_rows
            if _norm_brand_key(f.source_brand_name or "") == brand_key
            or _norm_brand_key(f.name or "") == brand_key
        ]

        brand_expr = func.lower(
            func.replace(func.btrim(SambaCollectedProduct.brand), " ", "")
        )
        conditions = [brand_expr == brand_key]
        if filter_ids:
            conditions.append(SambaCollectedProduct.search_filter_id.in_(filter_ids))
        product_stmt = select(SambaCollectedProduct).where(
            tenant_clause,
            SambaCollectedProduct.source_site == source_site,
            or_(*conditions),
        )
        products = list(
            (await self.product_repo.session.execute(product_stmt)).scalars().all()
        )

        registered = [
            p
            for p in products
            if p.registered_accounts and len(p.registered_accounts) > 0
        ]
        if registered:
            raise HTTPException(
                400,
                f"등록된 상품이 {len(registered)}건 있어 브랜드 전체 삭제를 진행할 수 없습니다.",
            )

        product_ids = [p.id for p in products]
        deleted_products = 0
        deleted_filters = 0
        if product_ids:
            await self.product_repo.session.execute(
                sa_delete(SambaCollectedProduct).where(
                    SambaCollectedProduct.id.in_(product_ids)
                )
            )
            deleted_products = len(product_ids)
        if filter_ids:
            await self.product_repo.session.execute(
                sa_delete(SambaSearchFilter).where(SambaSearchFilter.id.in_(filter_ids))
            )
            deleted_filters = len(filter_ids)
        await self.product_repo.session.commit()
        return {
            "deleted_products": deleted_products,
            "deleted_filters": deleted_filters,
        }

    async def search_collected_products(
        self, query: str, limit: int = 100
    ) -> List[SambaCollectedProduct]:
        return await self.product_repo.search(query, limit)

    async def apply_policy_to_filter_products(
        self,
        filter_id: str,
        policy_id: str,
        policy_data: Optional[Dict[str, Any]] = None,
    ) -> int:
        """그룹(필터)에 적용된 정책을 해당 그룹의 모든 상품에 전파.

        - 가격 계산 불필요(`policy_data=None`): 단일 UPDATE
        - 가격 계산 필요: cursor 페이지네이션으로 10,000개 limit 없이 전체 순회
        - 상품별 try/except: 1건 실패해도 다음 상품 계속 처리, 실패 ID 로깅
        """
        if not policy_data:
            return await self.product_repo.bulk_update_by_filter(
                filter_id, applied_policy_id=policy_id
            )

        use_range = policy_data.get("use_range_margin", False)
        range_margins = policy_data.get("range_margins", [])
        default_margin = policy_data.get("margin_rate", 15)
        shipping = policy_data.get("shipping_cost", 0)
        extra = policy_data.get("extra_charge", 0)
        ssm_data = policy_data.get("source_site_margins") or {}

        updated = 0
        failed_ids: list[str] = []

        # 이슈 #261 (2026-05-27) — minMarginAmount 강제 누락 fix.
        # market_prices.default 는 마켓별 fee 분할 전 "참고 default" 이므로
        # 최소한 base 기준 margin 적용 후 minMargin 강제만이라도 보장.
        min_margin = int(policy_data.get("min_margin_amount") or 0)

        async for p in self.product_repo.iter_by_filter(filter_id):
            try:
                base = p.sale_price or p.original_price or 0
                # 범위 마진: 원가 구간별 마진율 적용
                margin_rate = default_margin
                if use_range and range_margins:
                    for r in range_margins:
                        max_val = r.get("max") or 9999999999
                        if base >= r.get("min", 0) and base < max_val:
                            margin_rate = r.get("rate", 15)
                            break
                # 소싱처별 추가 마진
                source_margin = 0
                if ssm_data and p.source_site:
                    _ssm = ssm_data.get(p.source_site, {})
                    _ss_rate = _ssm.get("marginRate", 0)
                    _ss_amount = _ssm.get("marginAmount", 0)
                    # pointOnly=true: 적립금 사용 가능 상품(is_point_restricted=False)에만 적용
                    _point_only = bool(_ssm.get("pointOnly"))
                    _is_pr = getattr(p, "is_point_restricted", None)
                    _apply_ssm = (not _point_only) or (_is_pr is False)
                    if _apply_ssm:
                        if _ss_rate > 0:
                            source_margin += round(base * _ss_rate / 100)
                        if _ss_amount > 0:
                            source_margin += _ss_amount
                # 기본 마진 (markup)
                margin_amt = round(base * margin_rate / 100)
                # minMarginAmount 강제 (이슈 #261)
                if min_margin > 0 and margin_amt < min_margin:
                    margin_amt = min_margin
                calculated = int(base + margin_amt + source_margin + shipping + extra)
                await self.product_repo.update_async(
                    p.id,
                    applied_policy_id=policy_id,
                    market_prices={"default": calculated},
                )
                updated += 1
            except Exception as e:
                failed_ids.append(p.id)
                logger.warning(f"정책 전파 실패 (상품 {p.id}, 필터 {filter_id}): {e}")

        if failed_ids:
            logger.error(
                f"정책 전파 부분 실패: 필터 {filter_id} → 성공 {updated}건, "
                f"실패 {len(failed_ids)}건 (id 일부: {failed_ids[:10]})"
            )
        return updated

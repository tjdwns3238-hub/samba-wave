"""SambaWave Tetris 정책 배치 service — board 조회 + shipment 트리거."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.domain.samba.account.model import SambaMarketAccount
from backend.domain.samba.policy.model import SambaPolicy
from backend.domain.samba.shipment.repository import SambaShipmentRepository
from backend.domain.samba.shipment.service import SambaShipmentService
from backend.domain.samba.tetris.model import SambaTetrisAssignment
from backend.domain.samba.tetris.repository import SambaTetrisRepository
from backend.utils.logger import logger
from sqlalchemy import or_
from sqlmodel import select

# get_board() 인메모리 캐시 — 5분 TTL (61초짜리 쿼리, 자주 갱신 불필요)
_BOARD_CACHE_TTL = 300.0
_board_cache: dict = {}
_board_cache_lock = asyncio.Lock()


def clear_board_cache() -> None:
    """테트리스 보드 캐시 전체 무효화 (AI 태그 변경 시 호출)."""
    _board_cache.clear()


# 마켓타입 → 표시명 매핑 (account.market_type 기준)
_MARKET_DISPLAY_NAMES: dict[str, str] = {
    "smartstore": "스마트스토어",
    "coupang": "쿠팡",
    "ssg": "신세계몰",
    "11st": "11번가",
    "gmarket": "지마켓",
    "auction": "옥션",
    "gsshop": "GS샵",
    "lotteon": "롯데ON",
    "lottehome": "롯데홈쇼핑",
    "homeand": "홈앤쇼핑",
    "hmall": "HMALL",
    "kream": "KREAM",
    "playauto": "플레이오토",
}

# 레거시(배치 없이 등록된) 브랜드 기본 색상
_LEGACY_COLOR = "#6B7280"


def _norm_tetris_key(value: str | None) -> str:
    return "".join((value or "").split()).casefold()


def _norm_site_key(value: str | None) -> str:
    key = _norm_tetris_key(value)
    site_aliases = {
        "gsshop": "gsshop",
        "abcmart": "abcmart",
        "grandstage": "abcmart",
        "lotteon": "lotteon",
        "musinsa": "musinsa",
        "ssg": "ssg",
    }
    return site_aliases.get(key, key)


class SambaTetrisService:
    """테트리스 정책 배치 서비스."""

    def __init__(
        self,
        repo: SambaTetrisRepository,
        session: AsyncSession,
    ) -> None:
        self._repo = repo
        self._session = session

    # ──────────────────────────────────────────────
    # 내부 헬퍼
    # ──────────────────────────────────────────────

    def _make_ship_svc(self) -> SambaShipmentService:
        """Shipment 서비스 인스턴스 생성 (write session 공유)."""
        return SambaShipmentService(
            SambaShipmentRepository(self._session),
            self._session,
        )

    async def _exists_pending_transmit(
        self,
        source_site: str,
        brand_name: str,
        market_account_id: str,
    ) -> bool:
        """같은 (site, brand, account) pending/running transmit 잡 존재 여부.

        sync_all 중복 잡 누적 방지용 atomic 가드 — INSERT 직전 한 번 더 DB 확인.
        in-memory `pending_transmit_keys` 외 race/key-mismatch 케이스 마지막 방어선.
        """
        row = await self._session.execute(
            text("""
                SELECT 1
                FROM samba_jobs
                WHERE job_type = 'transmit'
                  AND status IN ('pending', 'running')
                  AND BTRIM(payload->>'source_site') = BTRIM(:site)
                  AND BTRIM(payload->>'brand_name') = BTRIM(:brand)
                  AND payload->>'target_account_ids' LIKE :acct_like
                LIMIT 1
            """),
            {
                "site": source_site,
                "brand": brand_name,
                "acct_like": f"%{market_account_id}%",
            },
        )
        return row.first() is not None

    async def _market_account_exists(self, market_account_id: str) -> bool:
        """samba_market_account 테이블에 해당 ID 존재 여부."""
        row = await self._session.execute(
            text("SELECT 1 FROM samba_market_account WHERE id = :aid LIMIT 1"),
            {"aid": market_account_id},
        )
        return row.first() is not None

    async def _cleanup_dead_registered_account(
        self,
        tenant_id: Optional[str],
        source_site: str,
        brand_name: str,
        dead_account_id: str,
    ) -> int:
        """삭제된 계정 ID 를 해당 상품 registered_accounts JSONB 에서 제거.

        sync_all legacy 루프가 죽은 계정으로 잡을 무한 생성하는 사고를 막는다.
        반환: 정리된 상품 수.
        """
        result = await self._session.execute(
            text("""
                UPDATE samba_collected_product
                SET registered_accounts = registered_accounts - :aid
                WHERE (tenant_id IS NULL AND :tid_is_null OR tenant_id = :tid)
                  AND source_site = :site
                  AND BTRIM(brand) = BTRIM(:brand)
                  AND registered_accounts::jsonb ? :aid
            """),
            {
                "aid": dead_account_id,
                "tid": tenant_id,
                "tid_is_null": tenant_id is None,
                "site": source_site,
                "brand": brand_name,
            },
        )
        await self._session.commit()
        return result.rowcount or 0

    async def _get_product_ids_for_assign(
        self,
        tenant_id: Optional[str],
        source_site: str,
        brand_name: str,
        market_account_id: str,
    ) -> list[str]:
        """해당 브랜드 상품 중 해당 계정에 미등록된 상품 ID 목록 반환.

        안전망: 동일 (cp, account) 전송 실패가 3회 이상 누적된 상품은 제외.
        plugin 응답 추출 실패로 A칸(registered_accounts) 동기화가 깨져 무한 재등록
        도는 사고 방지 (issue #187 — 같은 cp가 마켓에 N번 중복 등록되는 케이스).
        failure_count는 전송 성공 시 sent_snapshot 덮어쓰기로 자동 클리어됨.
        """
        rows = await self._session.execute(
            text("""
                SELECT id FROM samba_collected_product
                WHERE (tenant_id IS NULL AND :tid_is_null OR tenant_id = :tid)
                  AND source_site = :site
                  AND BTRIM(brand) = :brand
                  AND (
                    registered_accounts IS NULL
                    OR NOT (registered_accounts::jsonb ? :account_id)
                  )
                  AND COALESCE(
                    (last_sent_data -> :account_id ->> 'failure_count')::int,
                    0
                  ) < 3
            """),
            {
                "tid": tenant_id,
                "tid_is_null": tenant_id is None,
                "site": source_site,
                "brand": brand_name,
                "account_id": market_account_id,
            },
        )
        return [row[0] for row in rows]

    async def _get_product_ids_for_remove(
        self,
        tenant_id: Optional[str],
        source_site: str,
        brand_name: str,
        market_account_id: str,
    ) -> list[str]:
        """해당 브랜드 상품 중 해당 계정에 등록된 상품 ID 목록 반환."""
        try:
            # _get_product_ids_for_assign과 동일한 ? 연산자 패턴 사용 (검증된 방식)
            rows = await self._session.execute(
                text("""
                    SELECT id FROM samba_collected_product
                    WHERE (tenant_id IS NULL AND :tid_is_null OR tenant_id = :tid)
                      AND source_site = :site
                      AND BTRIM(brand) = :brand
                      AND registered_accounts IS NOT NULL
                      AND registered_accounts::jsonb ? :account_id
                """),
                {
                    "tid": tenant_id,
                    "tid_is_null": tenant_id is None,
                    "site": source_site,
                    "brand": brand_name,
                    "account_id": market_account_id,
                },
            )
            return [row[0] for row in rows]
        except Exception as e:
            logger.error(f"[테트리스] _get_product_ids_for_remove 쿼리 실패: {e}")
            return []

    # ──────────────────────────────────────────────
    # 보드 조회
    # ──────────────────────────────────────────────

    async def get_board(self, tenant_id: Optional[str]) -> dict[str, Any]:
        """
        테트리스 보드 전체 구조 반환.

        구조:
        {
          "markets": [{ market_type, market_name, accounts: [...] }],
          "unassigned": [{ source_site, brand_name, collected_count }]
        }
        """
        import time as _time

        cache_key = str(tenant_id)
        now_ts = _time.monotonic()
        cached = _board_cache.get(cache_key)
        if cached is not None and (now_ts - cached["ts"]) < _BOARD_CACHE_TTL:
            return cached["data"]

        async with _board_cache_lock:
            cached = _board_cache.get(cache_key)
            if (
                cached is not None
                and (_time.monotonic() - cached["ts"]) < _BOARD_CACHE_TTL
            ):
                return cached["data"]

            result = await self._get_board_uncached(tenant_id)
            _board_cache[cache_key] = {"data": result, "ts": _time.monotonic()}
            return result

    async def _get_board_uncached(self, tenant_id: Optional[str]) -> dict[str, Any]:
        # 1. 마켓 계정 전체 로드
        acc_stmt = select(SambaMarketAccount)
        if tenant_id is not None:
            # Account APIs expose both tenant-scoped accounts and pre-tenant legacy
            # accounts with NULL tenant_id. Keep Tetris aligned with that behavior.
            acc_stmt = acc_stmt.where(
                or_(
                    SambaMarketAccount.tenant_id == tenant_id,
                    SambaMarketAccount.tenant_id == None,  # noqa: E711
                )
            )
        else:
            acc_stmt = acc_stmt.where(SambaMarketAccount.tenant_id == None)  # noqa: E711
        acc_result = await self._session.execute(acc_stmt)
        accounts: list[SambaMarketAccount] = list(acc_result.scalars().all())

        # 2. 테트리스 배치 전체 로드
        assignments: list[SambaTetrisAssignment] = await self._repo.list_by_tenant(
            tenant_id
        )

        # 3. 정책 전체 로드 → id: (name, color) 딕셔너리
        # 계정 쿼리와 동일하게 테넌트 정책 + 레거시(NULL) 정책 모두 포함
        if tenant_id is not None:
            pol_stmt = select(SambaPolicy).where(
                or_(
                    SambaPolicy.tenant_id == tenant_id,
                    SambaPolicy.tenant_id == None,  # noqa: E711
                )
            )
        else:
            pol_stmt = select(SambaPolicy).where(SambaPolicy.tenant_id == None)  # noqa: E711
        pol_result = await self._session.execute(pol_stmt)
        policies: list[SambaPolicy] = list(pol_result.scalars().all())
        policy_map: dict[str, tuple[str, str]] = {}
        for pol in policies:
            extras = pol.extras or {}
            color = (
                extras.get("color", "#3B82F6")
                if isinstance(extras, dict)
                else "#3B82F6"
            )
            policy_map[pol.id] = (pol.name, color)

        # 3.5. (소싱처, 브랜드)별 정책 색상 로드 — collected_product.applied_policy_id 기준
        # search_filter.source_brand_name은 표기 차이/NULL이 있어 매칭 누락됨
        # → 상품 자체에 채워진 applied_policy_id를 (site, BTRIM(brand))로 집계해 사용
        sf_rows = await self._session.execute(
            text("""
                SELECT DISTINCT ON (source_site, brand_norm)
                    source_site, brand_norm AS source_brand_name, applied_policy_id, cnt
                FROM (
                    SELECT
                        source_site,
                        BTRIM(brand) AS brand_norm,
                        applied_policy_id,
                        COUNT(*) AS cnt
                    FROM samba_collected_product
                    WHERE applied_policy_id IS NOT NULL
                      AND brand IS NOT NULL
                      AND BTRIM(brand) != ''
                      AND (tenant_id IS NULL AND :tid_is_null OR tenant_id = :tid)
                    GROUP BY source_site, BTRIM(brand), applied_policy_id
                ) sub
                ORDER BY source_site, brand_norm, cnt DESC
            """),
            {"tid": tenant_id, "tid_is_null": tenant_id is None},
        )
        # (norm_site, norm_brand) → (policy_id, policy_name, policy_color)
        sf_policy_map: dict[tuple[str, str], tuple[str, str, str]] = {}
        for row in sf_rows:
            site, brand, pid = row[0], row[1], row[2]
            if not brand or not pid or pid not in policy_map:
                continue
            nkey = (_norm_site_key(site), _norm_tetris_key(brand))
            if nkey not in sf_policy_map:
                pol_name, pol_color = policy_map[pid]
                sf_policy_map[nkey] = (pid, pol_name, pol_color)

        # 4. Raw SQL 집계 — 소싱처·브랜드별 수집 수 (상품수집 페이지와 동일 기준: cp.brand만)
        collected_rows = await self._session.execute(
            text("""
                SELECT
                    source_site,
                    BTRIM(brand) AS effective_brand,
                    COUNT(*) AS cnt,
                    COUNT(*) FILTER (
                        WHERE tags @> '["__ai_tagged__"]'::jsonb
                    ) AS ai_tagged_cnt
                FROM samba_collected_product
                WHERE (tenant_id IS NULL AND :tid_is_null OR tenant_id = :tid)
                  AND source_site IS NOT NULL
                  AND brand IS NOT NULL
                  AND BTRIM(brand) != ''
                GROUP BY source_site, BTRIM(brand)
            """),
            {"tid": tenant_id, "tid_is_null": tenant_id is None},
        )
        # collected_map[(source_site, trimmed_brand)] = count
        collected_map: dict[tuple[str, str], int] = {}
        ai_tagged_map: dict[tuple[str, str], int] = {}
        collected_label_map: dict[tuple[str, str], tuple[str, str]] = {}
        normalized_collected_map: dict[tuple[str, str], int] = {}
        normalized_ai_tagged_map: dict[tuple[str, str], int] = {}
        normalized_label_map: dict[tuple[str, str], tuple[str, str]] = {}
        for row in collected_rows:
            site = row[0]
            brand = row[1]
            if not brand:
                continue
            key = (site, brand)
            norm_key = (_norm_site_key(site), _norm_tetris_key(brand))
            collected_map[key] = collected_map.get(key, 0) + int(row[2] or 0)
            ai_tagged_map[key] = ai_tagged_map.get(key, 0) + int(row[3] or 0)
            collected_label_map.setdefault(key, (site, brand))
            normalized_collected_map[norm_key] = normalized_collected_map.get(
                norm_key, 0
            ) + int(row[2] or 0)
            normalized_ai_tagged_map[norm_key] = normalized_ai_tagged_map.get(
                norm_key, 0
            ) + int(row[3] or 0)
            normalized_label_map.setdefault(norm_key, (site, brand))

        logger.info(
            f"[테트리스] collected_map={len(collected_map)}, "
            f"accounts={len(accounts)}, tenant_id={tenant_id}"
        )

        # 5. Raw SQL 집계 — JSONB 함수로 account_id 전개 후 DB에서 집계 (cp.brand만)
        # 서브쿼리로 배열 타입 행만 먼저 필터링 후 jsonb_array_elements_text 호출
        # (PostgreSQL은 WHERE 조건 순서를 보장하지 않아 직접 체크 시 스칼라 에러 발생)
        registered_rows = await self._session.execute(
            text("""
                SELECT
                    source_site,
                    effective_brand,
                    jsonb_array_elements_text(registered_accounts) AS account_id,
                    COUNT(*) AS cnt
                FROM (
                    SELECT source_site, BTRIM(brand) AS effective_brand, registered_accounts
                    FROM samba_collected_product
                    WHERE (tenant_id IS NULL AND :tid_is_null OR tenant_id = :tid)
                      AND registered_accounts IS NOT NULL
                      AND registered_accounts != '[]'::jsonb
                      AND jsonb_typeof(registered_accounts) = 'array'
                      AND source_site IS NOT NULL
                      AND brand IS NOT NULL
                      AND BTRIM(brand) != ''
                ) sub
                GROUP BY source_site, effective_brand, account_id
            """),
            {"tid": tenant_id, "tid_is_null": tenant_id is None},
        )
        # registered_map[(source_site, trimmed_brand, account_id)] = count
        registered_map: dict[tuple[str, str, str], int] = {}
        normalized_registered_map: dict[tuple[str, str, str], int] = {}
        for row in registered_rows:
            site = row[0]
            brand = row[1]
            account_id = row[2]
            if not brand or not account_id:
                continue
            key = (site, brand, account_id)
            norm_key = (_norm_site_key(site), _norm_tetris_key(brand), account_id)
            registered_map[key] = registered_map.get(key, 0) + int(row[3] or 0)
            normalized_registered_map[norm_key] = normalized_registered_map.get(
                norm_key, 0
            ) + int(row[3] or 0)

        logger.info(
            f"[테트리스] registered_map={len(registered_map)}, assignments={len(assignments)}"
        )

        # 6. 계정별 등록 총 수 집계
        # account_registered_total[account_id] = sum
        account_registered_total: dict[str, int] = {}
        for (_, _, acc_id), cnt in registered_map.items():
            account_registered_total[acc_id] = (
                account_registered_total.get(acc_id, 0) + cnt
            )

        # 7. 등록된 (site, brand, account_id) 집합 — 레거시 감지용
        registered_keys: set[tuple[str, str, str]] = set(
            normalized_registered_map.keys()
        )

        # 9. 보드 조립
        # O(n²) 방지: 계정별 assignment 사전 인덱싱
        assignments_by_account: dict[str, list[SambaTetrisAssignment]] = {}
        for a in assignments:
            assignments_by_account.setdefault(a.market_account_id, []).append(a)

        # O(n²) 방지: 계정별 registered legacy_keys 사전 인덱싱
        legacy_keys_by_account: dict[str, list[tuple[str, str]]] = {}
        for site, brand, aid in registered_keys:
            legacy_keys_by_account.setdefault(aid, []).append((site, brand))

        # market_type → market group dict
        market_groups: dict[str, dict[str, Any]] = {}
        market_order: list[str] = []

        for acc in accounts:
            mt = acc.market_type
            # 플레이오토는 계정별로 별도 컬럼 분리
            is_playauto = mt == "playauto"
            group_key = f"playauto:{acc.id}" if is_playauto else mt
            if group_key not in market_groups:
                base_name = acc.market_name or _MARKET_DISPLAY_NAMES.get(mt, mt)
                display_name = (
                    f"{base_name} ({acc.account_label})" if is_playauto else base_name
                )
                market_groups[group_key] = {
                    "market_type": mt,  # 응답 필드는 그대로 'playauto' 유지
                    "market_name": display_name,
                    "accounts": [],
                }
                market_order.append(group_key)

            # max_count: additional_fields.maxCount
            add_fields: dict[str, Any] = (
                acc.additional_fields if isinstance(acc.additional_fields, dict) else {}
            ) or {}
            max_count: int = int(add_fields.get("maxCount", 0) or 0)
            account_order_raw = add_fields.get("tetrisAccountOrder")
            account_order = (
                int(account_order_raw)
                if isinstance(account_order_raw, (int, float, str))
                and str(account_order_raw).strip() != ""
                else None
            )

            # 해당 계정에 배치된 assignment 목록 — O(1) dict 조회
            acc_assignments: list[SambaTetrisAssignment] = sorted(
                assignments_by_account.get(acc.id, []),
                key=lambda a: a.position_order,
            )

            assignment_blocks: list[dict[str, Any]] = []
            for a in acc_assignments:
                if a.policy_id:
                    pol_name, pol_color = policy_map.get(
                        a.policy_id, ("기본정책", "#3B82F6")
                    )
                    eff_policy_id = a.policy_id
                else:
                    _nk = (
                        _norm_site_key(a.source_site),
                        _norm_tetris_key(a.brand_name),
                    )
                    _sf = sf_policy_map.get(_nk)
                    if _sf:
                        eff_policy_id, pol_name, pol_color = _sf
                    else:
                        eff_policy_id, pol_name, pol_color = (
                            None,
                            "기본정책",
                            "#3B82F6",
                        )
                exact_key = (a.source_site, a.brand_name)
                norm_key = (
                    _norm_site_key(a.source_site),
                    _norm_tetris_key(a.brand_name),
                )
                reg_cnt = registered_map.get((a.source_site, a.brand_name, acc.id), 0)
                if reg_cnt <= 0:
                    reg_cnt = normalized_registered_map.get((*norm_key, acc.id), 0)
                col_cnt = collected_map.get(exact_key, 0)
                ai_cnt = ai_tagged_map.get(exact_key, 0)
                display_site, display_brand = collected_label_map.get(
                    exact_key, (a.source_site, a.brand_name)
                )
                if col_cnt <= 0:
                    col_cnt = normalized_collected_map.get(norm_key, 0)
                    ai_cnt = normalized_ai_tagged_map.get(norm_key, 0)
                    display_site, display_brand = normalized_label_map.get(
                        norm_key, (a.source_site, a.brand_name)
                    )
                if col_cnt <= 0 and reg_cnt <= 0:
                    continue
                assignment_blocks.append(
                    {
                        "id": a.id,
                        "source_site": display_site,
                        "brand_name": display_brand,
                        "policy_id": eff_policy_id,
                        "policy_name": pol_name,
                        "policy_color": pol_color,
                        "registered_count": reg_cnt,
                        "collected_count": col_cnt,
                        "ai_tagged_count": ai_cnt,
                        "position_order": a.position_order,
                        "is_legacy": False,
                        "excluded": bool(a.excluded),
                    }
                )

            # 레거시: registered_map에 있지만 tetris 배치 없는 브랜드
            assigned_site_brand = {
                (_norm_site_key(a.source_site), _norm_tetris_key(a.brand_name))
                for a in acc_assignments
            }
            # O(1) dict 조회 — registered_keys 전체 순회 불필요
            legacy_keys = [
                (site, brand)
                for (site, brand) in legacy_keys_by_account.get(acc.id, [])
                if (site, brand) not in assigned_site_brand
            ]
            _fallback_color = next((v[1] for v in policy_map.values()), _LEGACY_COLOR)
            for site, brand in legacy_keys:
                reg_cnt = normalized_registered_map.get((site, brand, acc.id), 0)
                col_cnt = normalized_collected_map.get((site, brand), 0)
                ai_cnt = normalized_ai_tagged_map.get((site, brand), 0)
                if col_cnt <= 0 and ai_cnt <= 0:
                    continue
                orig_site, orig_brand = normalized_label_map.get(
                    (site, brand), (site, brand)
                )
                _sf_leg = sf_policy_map.get((site, brand))
                assignment_blocks.append(
                    {
                        "id": None,
                        "source_site": orig_site,
                        "brand_name": orig_brand,
                        "policy_id": _sf_leg[0] if _sf_leg else None,
                        "policy_name": _sf_leg[1] if _sf_leg else None,
                        "policy_color": _sf_leg[2] if _sf_leg else _fallback_color,
                        "registered_count": reg_cnt,
                        "collected_count": col_cnt,
                        "ai_tagged_count": ai_cnt,
                        "position_order": 9999,
                        "is_legacy": True,
                        "excluded": False,
                    }
                )
            # 계정 총 수집 수 (배치된 브랜드 기준)
            total_collected = sum(b["collected_count"] for b in assignment_blocks)
            total_registered = account_registered_total.get(acc.id, 0)

            market_groups[group_key]["accounts"].append(
                {
                    "account_id": acc.id,
                    "account_label": acc.account_label,
                    "account_order": account_order,
                    "max_count": max_count,
                    "total_registered": total_registered,
                    "total_collected": total_collected,
                    "assignments": assignment_blocks,
                }
            )

        # 10. unassigned: 수집 상품이 있는 모든 브랜드 표시 (다중 계정 중복 배치 허용)
        # 이미 일부 계정에 배치된 브랜드도 다른 계정에 추가 배치 가능하도록 풀에 항상 포함
        unassigned: list[dict[str, Any]] = []
        registered_total_by_brand: dict[tuple[str, str], int] = {}
        for (site, brand, _), cnt in registered_map.items():
            key = (site, brand)
            registered_total_by_brand[key] = registered_total_by_brand.get(key, 0) + cnt

        for (site, brand), cnt in collected_map.items():
            if cnt > 0:
                unassigned.append(
                    {
                        "source_site": collected_label_map.get(
                            (site, brand), (site, brand)
                        )[0],
                        "brand_name": collected_label_map.get(
                            (site, brand), (site, brand)
                        )[1],
                        "policy_id": sf_policy_map.get(
                            (_norm_site_key(site), _norm_tetris_key(brand)),
                            (None, None, None),
                        )[0],
                        "policy_name": sf_policy_map.get(
                            (_norm_site_key(site), _norm_tetris_key(brand)),
                            (None, None, None),
                        )[1],
                        "policy_color": sf_policy_map.get(
                            (_norm_site_key(site), _norm_tetris_key(brand)),
                            (None, None, None),
                        )[2],
                        "registered_count": registered_total_by_brand.get(
                            (site, brand), 0
                        ),
                        "collected_count": cnt,
                        "ai_tagged_count": ai_tagged_map.get((site, brand), 0),
                    }
                )

        if unassigned:
            logger.info(f"[테트리스] unassigned 샘플: {unassigned[:3]}")

        return {
            "markets": [market_groups[gk] for gk in market_order],
            "unassigned": unassigned,
        }

    # ──────────────────────────────────────────────
    # 배치 저장
    # ──────────────────────────────────────────────

    async def assign(
        self,
        tenant_id: Optional[str],
        source_site: str,
        brand_name: str,
        market_account_id: str,
        policy_id: Optional[str],
        position_order: int,
    ) -> SambaTetrisAssignment:
        """배치 저장 후 해당 브랜드 미등록 상품 전송 트리거."""
        # 동일 계정에 동일 브랜드 중복 배치 방지 (다른 계정에는 허용)
        existing = await self._repo.find_existing(
            tenant_id, source_site, brand_name, market_account_id
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"{source_site}/{brand_name} 배치가 이미 해당 계정에 존재합니다 (id={existing.id})",
            )

        assignment = await self._repo.create_async(
            tenant_id=tenant_id,
            source_site=source_site,
            brand_name=brand_name,
            market_account_id=market_account_id,
            policy_id=policy_id,
            position_order=position_order,
        )

        # 즉시 전송하지 않음 — 인터벌 루프(sync_all)에서 잡큐로 스테이징
        # clear_board_cache() 금지 — 61초 쿼리 유발로 프론트 15초 타임아웃 발생

        # 동일 브랜드의 다른 계정 pending/running 잡 취소 (중복 전송 방지)
        cancelled = await self._cancel_other_account_transmit_jobs(
            source_site, brand_name, market_account_id
        )
        if cancelled > 0:
            logger.info(
                f"[테트리스] assign — 다른 계정 pending 잡 {cancelled}건 취소 "
                f"({source_site}/{brand_name})"
            )

        logger.info(
            f"[테트리스] assign 저장 완료 — {source_site}/{brand_name} "
            f"→ {market_account_id} (인터벌 루프에서 등록 예정)"
        )

        return assignment

    # ──────────────────────────────────────────────
    # 배치 삭제
    # ──────────────────────────────────────────────

    # ──────────────────────────────────────────────
    # 잡큐 헬퍼
    # ──────────────────────────────────────────────

    async def _cancel_other_account_transmit_jobs(
        self, source_site: str, brand_name: str, keep_account_id: str
    ) -> int:
        """동일 브랜드의 다른 계정 pending/running 잡 취소 — 새 배치 계정만 유지."""
        from backend.domain.samba.job.model import JobStatus, SambaJob
        from backend.domain.samba.shipment.service import request_cancel_transmit
        from sqlmodel import select

        rows = await self._session.execute(
            select(SambaJob).where(
                SambaJob.job_type == "transmit",
                SambaJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                SambaJob.payload.op("->>")("brand_name") == brand_name,
                SambaJob.payload.op("->>")("source_site") == source_site,
            )
        )
        jobs = rows.scalars().all()
        cancelled = 0
        for job in jobs:
            target_ids = (job.payload or {}).get("target_account_ids", [])
            if keep_account_id in target_ids:
                continue  # 새 배치 계정 잡은 유지
            if job.status == JobStatus.PENDING:
                job.status = JobStatus.CANCELLED
                self._session.add(job)
                cancelled += 1
                logger.info(
                    f"[테트리스] 다른 계정 pending 잡 취소 — {job.id[:8]} "
                    f"({source_site}/{brand_name} targets={target_ids})"
                )
            else:  # RUNNING
                request_cancel_transmit(job.id)
                cancelled += 1
                logger.info(
                    f"[테트리스] 다른 계정 running 잡 취소 신호 — {job.id[:8]} "
                    f"({source_site}/{brand_name} targets={target_ids})"
                )
        return cancelled

    async def cancel_pending_tetris_jobs(self, tenant_id: Optional[str]) -> int:
        """테트리스 발 PENDING + RUNNING transmit 잡을 모두 취소.

        식별 기준 — payload.origin == 'tetris_sync' (sync_all 생성 시 부착한 마커).
        - PENDING: 상태만 CANCELLED 로 변경 (워커가 픽업 안 함)
        - RUNNING: request_cancel_transmit(job_id) 로 워커에 graceful 중단 신호 +
          상태도 CANCELLED 로 즉시 변경 (전송 페이지 즉시 반영).

        tenant_id 가 주어지면 해당 테넌트 잡만, None 이면 NULL 테넌트 잡만 대상.
        """
        from backend.domain.samba.job.model import JobStatus, SambaJob
        from backend.domain.samba.shipment.service import request_cancel_transmit
        from sqlmodel import select

        stmt = select(SambaJob).where(
            SambaJob.job_type == "transmit",
            SambaJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            SambaJob.payload.op("->>")("origin") == "tetris_sync",
        )
        if tenant_id is None:
            stmt = stmt.where(SambaJob.tenant_id.is_(None))
        else:
            stmt = stmt.where(SambaJob.tenant_id == tenant_id)

        rows = await self._session.execute(stmt)
        jobs = rows.scalars().all()
        cancelled = 0
        running_cancelled = 0
        for job in jobs:
            if job.status == JobStatus.RUNNING:
                request_cancel_transmit(job.id)
                running_cancelled += 1
            job.status = JobStatus.CANCELLED
            self._session.add(job)
            cancelled += 1
        if cancelled > 0:
            await self._session.commit()
            logger.info(
                f"[테트리스] 토글 OFF — 테트리스 발 잡 {cancelled}건 취소 "
                f"(RUNNING {running_cancelled}건 graceful 중단 신호 포함, "
                f"tenant_id={tenant_id})"
            )
        return cancelled

    async def _cancel_stale_transmit_jobs(
        self,
        tenant_id: Optional[str],
        assignments: list,
    ) -> int:
        """sync_all 실행 시 현재 배치 기준으로 유효하지 않은 pending/running 잡 취소.

        배치된 (source_site, brand_name) → assigned_account_id 매핑을 기준으로
        다른 계정을 타깃하는 잡을 정리한다.
        테트리스 배치가 없는 브랜드(레거시)는 건드리지 않는다.
        """
        from backend.domain.samba.job.model import JobStatus, SambaJob
        from backend.domain.samba.shipment.service import request_cancel_transmit
        from sqlmodel import select

        if not assignments:
            return 0

        # 현재 배치: (source_site, brand_name) → 유효한 account_id 집합
        # 동일 브랜드에 여러 계정 배치 가능 → set으로 모두 수집
        valid_map: dict[tuple[str, str], set[str]] = {}
        for a in assignments:
            key = (a.source_site, a.brand_name)
            valid_map.setdefault(key, set()).add(a.market_account_id)

        rows = await self._session.execute(
            select(SambaJob).where(
                SambaJob.job_type == "transmit",
                SambaJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                SambaJob.payload.op("->>")("brand_name").isnot(None),
                SambaJob.payload.op("->>")("source_site").isnot(None),
            )
        )
        jobs = rows.scalars().all()
        cancelled = 0
        for job in jobs:
            payload = job.payload or {}
            site = payload.get("source_site")
            brand = payload.get("brand_name")
            target_ids = payload.get("target_account_ids", [])

            key = (site, brand)
            if key not in valid_map:
                continue  # 테트리스 배치 없는 레거시 브랜드 — 건드리지 않음

            valid_accounts = valid_map[key]
            if any(acc in target_ids for acc in valid_accounts):
                continue  # 유효한 계정 잡 — 유지

            # 배치와 다른 계정 잡 → 취소
            if job.status == JobStatus.PENDING:
                job.status = JobStatus.CANCELLED
                self._session.add(job)
                cancelled += 1
                logger.info(
                    f"[테트리스 sync] stale pending 잡 취소 — {job.id[:8]} "
                    f"({site}/{brand} targets={target_ids}, valid={valid_accounts})"
                )
            else:  # RUNNING
                request_cancel_transmit(job.id)
                cancelled += 1
                logger.info(
                    f"[테트리스 sync] stale running 잡 취소 신호 — {job.id[:8]} "
                    f"({site}/{brand} targets={target_ids}, valid={valid_accounts})"
                )
        return cancelled

    async def _cancel_pending_transmit_jobs(
        self, source_site: str, brand_name: str, account_id: str
    ) -> int:
        """특정 브랜드+계정의 pending 전송잡 취소."""
        from backend.domain.samba.job.model import JobStatus, SambaJob
        from sqlmodel import select

        rows = await self._session.execute(
            select(SambaJob).where(
                SambaJob.job_type == "transmit",
                SambaJob.status == JobStatus.PENDING,
                SambaJob.payload.op("->>")("brand_name") == brand_name,
                SambaJob.payload.op("->>")("source_site") == source_site,
            )
        )
        jobs = rows.scalars().all()
        cancelled = 0
        for job in jobs:
            target_ids = (job.payload or {}).get("target_account_ids", [])
            if account_id in target_ids:
                job.status = JobStatus.CANCELLED
                self._session.add(job)
                cancelled += 1
        return cancelled

    async def _cancel_running_transmit_jobs(
        self, source_site: str, brand_name: str, account_id: str
    ) -> None:
        """특정 브랜드+계정의 running 전송잡에 취소 신호 전송."""
        from backend.domain.samba.job.model import JobStatus, SambaJob
        from backend.domain.samba.shipment.service import request_cancel_transmit
        from sqlmodel import select

        rows = await self._session.execute(
            select(SambaJob).where(
                SambaJob.job_type == "transmit",
                SambaJob.status == JobStatus.RUNNING,
                SambaJob.payload.op("->>")("brand_name") == brand_name,
                SambaJob.payload.op("->>")("source_site") == source_site,
            )
        )
        jobs = rows.scalars().all()
        for job in jobs:
            target_ids = (job.payload or {}).get("target_account_ids", [])
            if account_id in target_ids:
                request_cancel_transmit(job.id)
                logger.info(
                    f"[테트리스] running 전송잡 취소 신호 — {job.id[:8]} "
                    f"({source_site}/{brand_name} ← {account_id})"
                )

    async def _create_delete_market_job(
        self,
        tenant_id: Optional[str],
        product_ids: list[str],
        account_id: str,
        source_site: str,
        brand_name: str,
    ) -> None:
        """마켓삭제 잡 생성."""
        from backend.domain.samba.job.repository import SambaJobRepository

        job_repo = SambaJobRepository(self._session)
        await job_repo.create_async(
            tenant_id=tenant_id,
            job_type="delete_market",
            payload={
                "product_ids": product_ids,
                "target_account_ids": [account_id],
                "source_site": source_site,
                "brand_name": brand_name,
            },
        )
        logger.info(
            f"[테트리스] delete_market 잡 생성 — {source_site}/{brand_name} "
            f"← {account_id} ({len(product_ids)}건)"
        )

    async def remove(
        self,
        assignment_id: str,
        tenant_id: Optional[str],
    ) -> bool:
        """배치 삭제 후 해당 계정 상품 마켓삭제 잡 등록."""
        assignment = await self._repo.get_async(assignment_id)
        if not assignment:
            return False
        # 테넌트 권한 검증
        if assignment.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="권한이 없습니다")

        source_site = assignment.source_site
        brand_name = assignment.brand_name
        market_account_id = assignment.market_account_id

        deleted = await self._repo.delete_async(assignment_id)
        # clear_board_cache() 금지 — 61초 쿼리 유발로 프론트 15초 타임아웃 발생

        # pending 전송잡 취소 + running 전송잡 취소 신호
        pending_cancelled = await self._cancel_pending_transmit_jobs(
            source_site, brand_name, market_account_id
        )
        await self._cancel_running_transmit_jobs(
            source_site, brand_name, market_account_id
        )

        # 등록된 상품 → delete_market 잡 큐 등록
        product_ids = await self._get_product_ids_for_remove(
            tenant_id, source_site, brand_name, market_account_id
        )
        if product_ids:
            await self._create_delete_market_job(
                tenant_id, product_ids, market_account_id, source_site, brand_name
            )

        logger.info(
            f"[테트리스] remove 완료 — {source_site}/{brand_name} ← {market_account_id} "
            f"(pending취소={pending_cancelled}, delete_market잡={len(product_ids)}건)"
        )

        return deleted

    async def remove_by_brand(
        self,
        tenant_id: Optional[str],
        source_site: str,
        brand_name: str,
        market_account_id: str,
    ) -> dict:
        """레거시 블럭 삭제 — assignment 없이 registered_accounts 기준으로 마켓삭제 잡 등록."""
        # pending 전송잡 취소 + running 전송잡 취소 신호
        pending_cancelled = await self._cancel_pending_transmit_jobs(
            source_site, brand_name, market_account_id
        )
        await self._cancel_running_transmit_jobs(
            source_site, brand_name, market_account_id
        )

        # 등록된 상품 → delete_market 잡 큐 등록
        product_ids = await self._get_product_ids_for_remove(
            tenant_id, source_site, brand_name, market_account_id
        )
        if product_ids:
            await self._create_delete_market_job(
                tenant_id, product_ids, market_account_id, source_site, brand_name
            )

        # issue #219 — 레거시 블럭 삭제 후 sync_all 보충등록 재발 방지 영구 마커.
        # delete_market 잡 완료 후 pending_delete_keys 가드가 풀려도 excluded=True 배치가
        # 남아있으면 sync_all 레거시 루프가 (site, brand, account) 를 재등록 대상에서 제외.
        try:
            _existing = await self._repo.find_existing(
                tenant_id, source_site, brand_name, market_account_id
            )
            if _existing is None:
                await self._repo.create_async(
                    tenant_id=tenant_id,
                    source_site=source_site,
                    brand_name=brand_name,
                    market_account_id=market_account_id,
                    policy_id=None,
                    position_order=0,
                    excluded=True,
                )
                logger.info(
                    f"[테트리스] 레거시 영구 배제 마커 생성 — {source_site}/{brand_name} ← {market_account_id}"
                )
        except Exception as _e:
            logger.warning(f"[테트리스] 레거시 영구 배제 마커 생성 실패(무시): {_e}")

        logger.info(
            f"[테트리스] remove_by_brand 완료 — {source_site}/{brand_name} ← {market_account_id} "
            f"(pending취소={pending_cancelled}, delete_market잡={len(product_ids)}건)"
        )

        return {
            "pending_cancelled": pending_cancelled,
            "delete_job_products": len(product_ids),
        }

    # ──────────────────────────────────────────────
    # 배제 토글 (legacy 포함)
    # ──────────────────────────────────────────────

    async def set_excluded(
        self,
        tenant_id: Optional[str],
        source_site: str,
        brand_name: str,
        market_account_id: str,
        excluded: bool,
    ) -> SambaTetrisAssignment:
        """(소싱처, 브랜드, 계정) 조합의 배제 상태 설정.

        - 기존 assignment 존재 시: excluded 컬럼만 갱신
        - 없고 excluded=True 면: assignment 신규 생성 (레거시 블럭 배제 케이스)
        - 없고 excluded=False 면: 아무 일도 하지 않음 (no-op)
        excluded=True 로 전환되는 경우 해당 (브랜드, 계정) 의 pending/running 전송잡 취소.
        """
        existing = await self._repo.find_existing(
            tenant_id, source_site, brand_name, market_account_id
        )

        if existing is None:
            if not excluded:
                raise HTTPException(
                    status_code=404,
                    detail="배치를 찾을 수 없습니다",
                )
            # 레거시 블럭 → excluded=True 로 신규 생성 (전송잡 등록 차단 목적)
            assignment = await self._repo.create_async(
                tenant_id=tenant_id,
                source_site=source_site,
                brand_name=brand_name,
                market_account_id=market_account_id,
                policy_id=None,
                position_order=0,
                excluded=True,
            )
        else:
            if existing.tenant_id != tenant_id:
                raise HTTPException(status_code=403, detail="권한이 없습니다")
            updated = await self._repo.update_async(
                existing.id,
                excluded=excluded,
                updated_at=datetime.now(tz=timezone.utc),
            )
            if not updated:
                raise HTTPException(status_code=404, detail="배치 업데이트 실패")
            assignment = updated

        # excluded=True 로 전환되는 경우 진행 중인 전송잡 정리
        if excluded:
            pending_cancelled = await self._cancel_pending_transmit_jobs(
                source_site, brand_name, market_account_id
            )
            await self._cancel_running_transmit_jobs(
                source_site, brand_name, market_account_id
            )
            logger.info(
                f"[테트리스] set_excluded(True) — {source_site}/{brand_name} "
                f"← {market_account_id} (pending취소={pending_cancelled})"
            )
        else:
            logger.info(
                f"[테트리스] set_excluded(False) — {source_site}/{brand_name} "
                f"← {market_account_id}"
            )

        return assignment

    # ──────────────────────────────────────────────
    # 배치 이동
    # ──────────────────────────────────────────────

    async def move(
        self,
        assignment_id: str,
        tenant_id: Optional[str],
        new_account_id: str,
        policy_id: Optional[str],
        position_order: int,
    ) -> SambaTetrisAssignment:
        """다른 계정으로 이동 — 기존 계정 마켓삭제 → 신규 계정 전송."""
        assignment = await self._repo.get_async(assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="배치를 찾을 수 없습니다")
        if assignment.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="권한이 없습니다")

        old_account_id = assignment.market_account_id
        source_site = assignment.source_site
        brand_name = assignment.brand_name

        # 기존 계정 마켓삭제 트리거 (백그라운드)
        old_product_ids = await self._get_product_ids_for_remove(
            tenant_id, source_site, brand_name, old_account_id
        )
        if old_product_ids:
            ship_svc = self._make_ship_svc()
            asyncio.create_task(
                ship_svc.delete_from_markets(
                    product_ids=old_product_ids,
                    target_account_ids=[old_account_id],
                )
            )
            logger.info(
                f"[테트리스] move 마켓삭제 트리거 — {source_site}/{brand_name} "
                f"← {old_account_id} ({len(old_product_ids)}건)"
            )

        # 배치 업데이트
        updated = await self._repo.update_async(
            assignment_id,
            market_account_id=new_account_id,
            policy_id=policy_id,
            position_order=position_order,
            updated_at=datetime.now(tz=timezone.utc),
        )
        if not updated:
            raise HTTPException(status_code=404, detail="배치 업데이트 실패")

        # 신규 계정 전송 트리거 (백그라운드)
        new_product_ids = await self._get_product_ids_for_assign(
            tenant_id, source_site, brand_name, new_account_id
        )
        if new_product_ids:
            ship_svc = self._make_ship_svc()
            asyncio.create_task(
                ship_svc.start_update(
                    product_ids=new_product_ids,
                    update_items=["price", "stock", "image", "description"],
                    target_account_ids=[new_account_id],
                )
            )
            logger.info(
                f"[테트리스] move 전송 트리거 — {source_site}/{brand_name} "
                f"→ {new_account_id} ({len(new_product_ids)}건)"
            )

        return updated

    # ──────────────────────────────────────────────
    # 순서 변경
    # ──────────────────────────────────────────────

    async def reorder(
        self,
        assignment_id: str,
        tenant_id: Optional[str],
        position_order: int,
    ) -> SambaTetrisAssignment:
        """순서만 변경 (shipment 트리거 없음)."""
        assignment = await self._repo.get_async(assignment_id)
        if not assignment:
            raise HTTPException(status_code=404, detail="배치를 찾을 수 없습니다")
        if assignment.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="권한이 없습니다")

        updated = await self._repo.update_async(
            assignment_id,
            position_order=position_order,
            updated_at=datetime.now(tz=timezone.utc),
        )
        if not updated:
            raise HTTPException(status_code=404, detail="배치 업데이트 실패")

        return updated

    # ──────────────────────────────────────────────
    # 전체 sync (인터벌 자동등록 — A안: 미등록 보충)
    # ──────────────────────────────────────────────

    async def sync_all(self, tenant_id: Optional[str]) -> dict[str, Any]:
        """현재 배치 전체 기준으로 미등록 상품 transmit 잡 생성 (브랜드×계정별 별도 잡).

        samba_tetris_assignment 배치뿐 아니라, registered_accounts에 이미 계정이 있는
        레거시 블록(배치 미등록)도 함께 처리해 미등록 상품을 보충 등록한다.

        토글 OFF (tetris_sync_interval_hours <= 0) 인 경우 어떤 잡도 만들지 않고 즉시 반환.
        """
        from backend.api.v1.routers.samba.proxy._helpers import _get_setting
        from backend.domain.samba.job.repository import SambaJobRepository

        # 토글 가드 — 루프/엔드포인트 어디서 호출돼도 OFF면 신규 잡 생성 차단
        toggle_val = await _get_setting(self._session, "tetris_sync_interval_hours")
        toggle_interval = int(toggle_val) if toggle_val else 0
        if toggle_interval <= 0:
            logger.info("[테트리스 sync] 토글 OFF (interval=0) — 스킵")
            return {
                "assignments": 0,
                "jobs": 0,
                "triggered": 0,
                "skipped": True,
            }

        # 일시정지 가드 — 사용자가 전송 일시정지(__all__ 마커) 상태이면 신규 잡 생성 차단
        # 이유: 일시정지 중에 sync가 잡을 계속 쌓아두면 서버 재시작 시 마커가 휘발되어
        #       누적된 잡들이 한꺼번에 자동 실행되는 폭주가 발생할 수 있음
        from backend.domain.samba.shipment.service import is_cancel_requested

        if is_cancel_requested("__all__"):
            logger.info("[테트리스 sync] 일시정지(__all__) 감지 — 신규 잡 생성 스킵")
            return {
                "assignments": 0,
                "jobs": 0,
                "triggered": 0,
                "paused": True,
            }

        assignments = await self._repo.list_by_tenant(tenant_id)

        job_repo = SambaJobRepository(self._session)
        job_count = 0
        total_products = 0

        # 현재 배치 기준으로 다른 계정 pending/running 잡 정리
        # — 배치 이동 후 이전 계정 잡이 남아 중복 전송되는 버그 방지
        stale_cancelled = await self._cancel_stale_transmit_jobs(tenant_id, assignments)
        if stale_cancelled > 0:
            logger.info(f"[테트리스 sync] 배치 외 계정 잡 {stale_cancelled}건 취소")

        # 처리 완료된 (source_site, brand_name, account_id) 중복 방지
        processed_keys: set[tuple[str, str, str]] = set()

        # 이미 pending/running인 transmit 잡 집합 조회 — 중복 잡 생성 방지
        existing_rows = await self._session.execute(
            text("""
                SELECT
                    payload->>'source_site' AS source_site,
                    payload->>'brand_name' AS brand_name,
                    payload->>'target_account_ids' AS account_ids
                FROM samba_jobs
                WHERE job_type = 'transmit'
                  AND status IN ('pending', 'running')
                  AND payload->>'brand_name' IS NOT NULL
            """)
        )
        # target_account_ids는 '["account_id"]' 형태 text — 단순 포함 체크
        # brand_name/source_site 양쪽 공백 정규화로 매칭 실패 차단 (assignment.brand_name 끝/앞 공백 케이스 방어)
        pending_transmit_keys: set[tuple[str, str, str]] = set()
        for row in existing_rows:
            if row.source_site and row.brand_name and row.account_ids:
                pending_transmit_keys.add(
                    (
                        (row.source_site or "").strip(),
                        (row.brand_name or "").strip(),
                        row.account_ids,
                    )
                )

        # pending/running 상태의 delete_market 잡 조합 조회
        # — 삭제 진행 중인 (소싱처, 브랜드, 계정) 조합을 레거시 복원 루프에서 스킵하기 위함
        # — registered_accounts JSONB 가 delete_market 잡 처리 후에야 갱신되므로,
        #   그 사이에 sync_all 이 도래하면 레거시 블럭으로 오인해 transmit 잡을 재생성하던 버그 방지
        pending_delete_rows = await self._session.execute(
            text("""
                SELECT
                    payload->>'source_site' AS source_site,
                    payload->>'brand_name' AS brand_name,
                    payload->>'target_account_ids' AS account_ids
                FROM samba_jobs
                WHERE job_type = 'delete_market'
                  AND status IN ('pending', 'running')
                  AND payload->>'brand_name' IS NOT NULL
            """)
        )
        pending_delete_keys: set[tuple[str, str, str]] = set()
        for row in pending_delete_rows:
            if row.source_site and row.brand_name and row.account_ids:
                # account_ids 는 '["account_id"]' JSON text — 단일 계정 가정으로 파싱
                try:
                    import json as _json

                    parsed = _json.loads(row.account_ids)
                    if isinstance(parsed, list):
                        for acct in parsed:
                            pending_delete_keys.add(
                                (row.source_site, row.brand_name, str(acct))
                            )
                except Exception:
                    pass

        # 브랜드×계정 조합별 별도 잡 — 계정이 같아도 브랜드마다 독립 잡으로 스테이징
        # (워커가 동일 계정 잡은 순차, 다른 계정 잡은 병렬로 자동 처리)
        for a in assignments:
            # 배제 플래그가 켜진 배치는 transmit 잡 생성 스킵 + 레거시 루프에서도 처리되지 않도록
            # processed_keys 에 미리 등록
            if a.excluded:
                processed_keys.add((a.source_site, a.brand_name, a.market_account_id))
                continue
            # brand_name 양쪽 공백 정규화 — 가드 매칭 실패로 중복 누적 방지
            a_brand_norm = (a.brand_name or "").strip()
            a_site_norm = (a.source_site or "").strip()
            acct_key = f'["{a.market_account_id}"]'
            if (a_site_norm, a_brand_norm, acct_key) in pending_transmit_keys:
                logger.debug(
                    f"[테트리스 sync] 이미 pending/running 잡 존재 — 건너뜀: "
                    f"{a.source_site}/{a.brand_name} → {a.market_account_id}"
                )
                processed_keys.add((a.source_site, a.brand_name, a.market_account_id))
                continue
            pids = await self._get_product_ids_for_assign(
                tenant_id, a.source_site, a.brand_name, a.market_account_id
            )
            processed_keys.add((a.source_site, a.brand_name, a.market_account_id))
            if not pids:
                continue
            # Atomic 더블체크 — 가드 query 이후 다른 사이클이 잡 생성했는지 INSERT 직전 재확인
            # (sync_all 동시 호출/race 방지)
            if await self._exists_pending_transmit(
                a.source_site, a.brand_name, a.market_account_id
            ):
                logger.info(
                    f"[테트리스 sync] atomic 가드 — INSERT 직전 동일 잡 발견, skip: "
                    f"{a.source_site}/{a.brand_name} → {a.market_account_id}"
                )
                continue
            await job_repo.create_async(
                tenant_id=tenant_id,
                job_type="transmit",
                payload={
                    "product_ids": pids,
                    "update_items": ["price", "stock", "image", "description"],
                    "target_account_ids": [a.market_account_id],
                    "source_site": a.source_site,
                    "brand_name": a.brand_name,
                    "source_sites": [a.source_site],
                    "brands": [a.brand_name],
                    "skip_unchanged": True,
                    # 테트리스가 계정을 직접 결정 → 정책 accountIds 필터 스킵
                    # (DB 설정 tetris_matching_enabled OFF여도 동작하도록 명시)
                    "skip_policy_account_filter": True,
                    "origin": "tetris_sync",
                },
            )
            job_count += 1
            total_products += len(pids)

        # 테트리스 배치가 있는 (source_site, brand) 조합 — legacy 루프 개입 금지
        # 배치된 브랜드는 고경 등 지정 계정에서만 처리해야 하므로 다른 계정으로 번지면 안 됨
        # excluded 배치는 제외 — 배제는 해당 (브랜드, 계정) 조합만 막아야 하고
        # 같은 브랜드의 다른 레거시 계정 자동등록은 계속 동작해야 함
        tetris_brand_keys: set[tuple[str, str]] = {
            (a.source_site, a.brand_name) for a in assignments if not a.excluded
        }

        # 레거시 블록 처리: registered_accounts에 이미 계정이 있지만 배치가 없는 브랜드
        # → 미등록 상품이 남아있을 경우 보충 등록 잡 생성
        # CASE WHEN으로 스칼라 registered_accounts를 빈 배열로 치환
        # — PostgreSQL 옵티마이저가 CTE를 인라인화해서 WHERE 조건 순서가 보장 안 됨
        # — LATERAL + CASE WHEN 방식으로 스칼라 에러 없이 처리
        legacy_rows = await self._session.execute(
            text("""
                SELECT DISTINCT scp.source_site, BTRIM(scp.brand) AS brand_name, acc.val AS account_id
                FROM samba_collected_product scp
                CROSS JOIN LATERAL (
                    SELECT val
                    FROM jsonb_array_elements_text(
                        CASE WHEN jsonb_typeof(scp.registered_accounts) = 'array'
                             THEN scp.registered_accounts
                             ELSE '[]'::jsonb
                        END
                    ) AS t(val)
                ) AS acc
                WHERE (scp.tenant_id IS NULL AND :tid_is_null OR scp.tenant_id = :tid)
                  AND scp.registered_accounts IS NOT NULL
            """),
            {"tid": tenant_id, "tid_is_null": tenant_id is None},
        )
        for row in legacy_rows:
            # 테트리스 배치된 브랜드는 legacy 루프에서 절대 처리하지 않음
            if (row.source_site, row.brand_name) in tetris_brand_keys:
                continue
            key = (row.source_site, row.brand_name, row.account_id)
            if key in processed_keys:
                continue
            # 삭제(delete_market) 잡 진행 중인 조합은 레거시 복원 금지
            # — registered_accounts 갱신이 지연되어 오인 복원되는 버그 방지
            if key in pending_delete_keys:
                logger.info(
                    f"[테트리스 sync 레거시] delete_market 잡 진행 중 — 복원 스킵: "
                    f"{row.source_site}/{row.brand_name} → {row.account_id}"
                )
                processed_keys.add(key)
                continue
            # 삭제된 마켓 계정으로의 잡 생성 금지 — 잡 처리 단계에서 "계정 못 찾음" 에러 발생 차단
            if not await self._market_account_exists(row.account_id):
                logger.warning(
                    f"[테트리스 sync 레거시] 삭제된 계정 — 잡 생성 스킵 + registered_accounts 정리 예약: "
                    f"{row.source_site}/{row.brand_name} → {row.account_id}"
                )
                await self._cleanup_dead_registered_account(
                    tenant_id, row.source_site, row.brand_name, row.account_id
                )
                processed_keys.add(key)
                continue
            # brand_name/site 양쪽 공백 정규화 후 키 매칭 (assignment 가드와 동일 패턴)
            row_brand_norm = (row.brand_name or "").strip()
            row_site_norm = (row.source_site or "").strip()
            acct_key = f'["{row.account_id}"]'
            if (row_site_norm, row_brand_norm, acct_key) in pending_transmit_keys:
                logger.debug(
                    f"[테트리스 sync 레거시] 이미 pending/running 잡 존재 — 건너뜀: "
                    f"{row.source_site}/{row.brand_name} → {row.account_id}"
                )
                processed_keys.add(key)
                continue
            processed_keys.add(key)
            pids = await self._get_product_ids_for_assign(
                tenant_id, row.source_site, row.brand_name, row.account_id
            )
            if not pids:
                continue
            # Atomic 더블체크
            if await self._exists_pending_transmit(
                row.source_site, row.brand_name, row.account_id
            ):
                logger.info(
                    f"[테트리스 sync 레거시] atomic 가드 — INSERT 직전 동일 잡 발견, skip: "
                    f"{row.source_site}/{row.brand_name} → {row.account_id}"
                )
                continue
            await job_repo.create_async(
                tenant_id=tenant_id,
                job_type="transmit",
                payload={
                    "product_ids": pids,
                    "update_items": ["price", "stock", "image", "description"],
                    "target_account_ids": [row.account_id],
                    "source_site": row.source_site,
                    "brand_name": row.brand_name,
                    "source_sites": [row.source_site],
                    "brands": [row.brand_name],
                    "skip_unchanged": True,
                    # 레거시 복원 잡도 sync_all 컨텍스트 — 정책 accountIds 필터 스킵
                    "skip_policy_account_filter": True,
                    "origin": "tetris_sync",
                },
            )
            job_count += 1
            total_products += len(pids)

        logger.info(
            f"[테트리스 sync] {len(assignments)}개 배치 + 레거시 → "
            f"{job_count}개 잡, {total_products}개 상품"
        )
        return {
            "assignments": len(assignments),
            "jobs": job_count,
            "triggered": total_products,
        }

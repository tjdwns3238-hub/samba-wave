"""SambaWave 모니터링 서비스 — 이벤트 발행 + 대시보드 통계."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import cast, func
from sqlalchemy.dialects.postgresql import JSONB  # noqa: F401
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_read_session
from backend.domain.samba.warroom.model import SambaMonitorEvent
from backend.domain.samba.warroom.repository import SambaMonitorEventRepository
from backend.utils.logger import logger


_RETENTION_DAYS = 30  # 이벤트 보존 기간
_emit_counter = 0  # 100회마다 정리 실행
_emit_counter_lock = asyncio.Lock()  # 코루틴 race 방지: += 연산 보호

# ── 대시보드 결과 인메모리 캐시 ──
# 갱신통계 집계 쿼리(_get_refresh_stats의 last_refreshed_at count + registered JSONB 체크)가
# 10만+ 상품에서 ~50초 걸려 30초 TTL은 무용(채워지기 전 만료 → 매 폴링마다 재실행 → read 풀 18칸 고갈).
# TTL을 쿼리 시간보다 충분히 길게 잡아 캐시 실효성 확보 (대시보드라 3분 갱신 간격 무방).
_DASHBOARD_CACHE_TTL = 180.0  # 초
_dashboard_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_dashboard_cache_lock = asyncio.Lock()

# Cold start 시 페이지 무한 블로킹 차단용 — 빈 구조 즉시 반환
_DASHBOARD_EMPTY: Dict[str, Any] = {
    "product_stats": {"total": 0, "by_source": {}, "by_sale_status": {}},
    "refresh_stats": {
        "last_refreshed_at": None,
        "refreshed_1h": 0,
        "refreshed_24h": 0,
        "error_products": 0,
    },
    "price_change_stats": {"changes_24h": 0, "avg_change_pct": 0, "top_changes": []},
    "site_health": {},
    "market_health": {},
    "event_summary": {},
    "hourly_changes": {},
    "_warming": True,
}


class SambaMonitorService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = SambaMonitorEventRepository(session)

    async def emit(
        self,
        event_type: str,
        severity: str = "info",
        summary: str = "",
        source_site: Optional[str] = None,
        market_type: Optional[str] = None,
        product_id: Optional[str] = None,
        product_name: Optional[str] = None,
        detail: Optional[Any] = None,
        tenant_id: Optional[str] = None,
    ) -> Optional[str]:
        """이벤트 기록 — 메인 로직을 방해하지 않도록 try/except 감싸기.

        tenant_id 미지정 시 product_id로 SambaCollectedProduct.tenant_id 자동 lookup.
        Returns:
            생성된 이벤트 id. 실패 시 None.
        """
        global _emit_counter
        try:
            # tenant_id 자동 채움 — 워커는 HTTP context 없어 contextvar=None
            if not tenant_id and product_id:
                try:
                    from backend.domain.samba.collector.model import (
                        SambaCollectedProduct as _CP,
                    )
                    from sqlmodel import select as _sel

                    _tid_stmt = _sel(_CP.tenant_id).where(_CP.id == product_id)
                    _tid_result = await self.session.execute(_tid_stmt)
                    tenant_id = _tid_result.scalar_one_or_none()
                except Exception:
                    pass

            event = SambaMonitorEvent(
                event_type=event_type,
                severity=severity,
                summary=summary,
                source_site=source_site,
                market_type=market_type,
                product_id=product_id,
                product_name=product_name,
                detail=detail,
                tenant_id=tenant_id,
            )
            self.session.add(event)
            await self.session.flush()

            # 100회마다 30일 이전 이벤트 자동 정리 (lock으로 race 방지)
            should_cleanup = False
            async with _emit_counter_lock:
                _emit_counter += 1
                if _emit_counter >= 100:
                    _emit_counter = 0
                    should_cleanup = True
            if should_cleanup:
                cutoff = datetime.now(timezone.utc) - timedelta(days=_RETENTION_DAYS)
                deleted = await self.repo.cleanup_old(cutoff)
                if deleted:
                    logger.info(
                        f"[monitor] {_RETENTION_DAYS}일 이전 이벤트 {deleted}건 정리"
                    )
            return event.id
        except Exception as e:
            logger.warning(f"[monitor] 이벤트 기록 실패: {e}")
            return None

    async def get_dashboard_stats(self) -> Dict[str, Any]:
        """대시보드 전체 통계 (stale-while-revalidate 패턴).

        개선 포인트:
        1) 캐시 있으면 stale 도 즉시 반환 → 페이지 첫 진입 80~120초 블로킹 차단.
        2) stale 시 백그라운드 재계산 (lock locked 면 skip → 중복 쿼리 0).
        3) cold start 만 블로킹 (lifecycle warmup 으로 첫 사용자 부담 최소화).
        4) 서브쿼리 8개 asyncio.gather 병렬 유지.
        """
        now_ts = time.monotonic()
        cached = _dashboard_cache.get("data")
        if cached is not None:
            stale = (now_ts - _dashboard_cache["ts"]) >= _DASHBOARD_CACHE_TTL
            if stale and not _dashboard_cache_lock.locked():
                # 백그라운드 재계산 trigger (caller 는 stale 즉시 반환)
                asyncio.create_task(self._refresh_dashboard_in_background())
            return cached

        # Cold start — 블로킹 금지. 빈 구조 즉시 반환 + 백그라운드 계산 1회만 트리거.
        # (이전: lock 블록 안에서 7개 gather 25~80초 블로킹 → "대시보드 로딩 중" 무한 표시)
        # 사용자는 _warming:true 응답 받음 → 다음 폴링(10초)에 채워진 데이터 반환.
        if not _dashboard_cache_lock.locked():
            asyncio.create_task(self._refresh_dashboard_in_background())
        return _DASHBOARD_EMPTY

    async def _compute_dashboard_now(self) -> Dict[str, Any]:
        """동기 cold-compute (lifecycle warmup 전용)."""

        async with _dashboard_cache_lock:
            cached = _dashboard_cache.get("data")
            if cached is not None:
                return cached

            now = datetime.now(timezone.utc)
            since_24h = now - timedelta(hours=24)
            since_1h = now - timedelta(hours=1)

            async def _run(coro_factory):
                """각 서브쿼리를 독립된 read 세션으로 실행 — 커넥션 풀 내 병렬."""
                async with get_read_session() as sess:
                    svc = SambaMonitorService(sess)
                    return await coro_factory(svc)

            (
                product_stats,
                refresh_stats,
                price_change_stats,
                site_health,
                market_health,
                event_summary,
                hourly_changes,
            ) = await asyncio.gather(
                _run(lambda s: s._get_product_stats()),
                _run(lambda s: s._get_refresh_stats(since_1h, since_24h)),
                _run(lambda s: s._get_price_change_stats(since_24h)),
                _run(lambda s: s._get_site_health()),
                _run(lambda s: s._get_market_health()),
                _run(lambda s: s._get_event_summary(since_24h)),
                _run(lambda s: s._get_hourly_changes(since_24h)),
            )

            data = {
                "product_stats": product_stats,
                "refresh_stats": refresh_stats,
                "price_change_stats": price_change_stats,
                "site_health": site_health,
                "market_health": market_health,
                "event_summary": event_summary,
                "hourly_changes": hourly_changes,
            }
            _dashboard_cache["data"] = data
            _dashboard_cache["ts"] = time.monotonic()
            return data

    async def _refresh_dashboard_in_background(self) -> None:
        """stale·cold 캐시 백그라운드 재계산 — 페이지 응답 차단 X. 중복 시 lock 으로 skip.

        _compute_dashboard_now 가 lock 잡고 7쿼리 gather 후 캐시에 결과 박음.
        다음 호출부터 stale-while-revalidate 정상 동작.
        """
        if _dashboard_cache_lock.locked():
            return
        try:
            await self._compute_dashboard_now()
        except Exception as exc:
            logger.warning(f"[monitor] dashboard 백그라운드 재계산 실패: {exc}")

    async def _get_product_stats(self) -> Dict[str, Any]:
        """상품 통계: 전체, 소싱처별, 상태별 — 단일 쿼리."""
        from backend.domain.samba.collector.model import SambaCollectedProduct
        from backend.api.v1.routers.samba.collector_common import (
            build_market_registered_conditions,
        )

        # 소싱처·상태별 카운트를 한 번에 가져오기
        combo_stmt = select(
            SambaCollectedProduct.source_site,
            SambaCollectedProduct.sale_status,
            func.count(SambaCollectedProduct.id),
        ).group_by(
            SambaCollectedProduct.source_site,
            SambaCollectedProduct.sale_status,
        )
        combo_result = await self.session.execute(combo_stmt)
        rows = combo_result.all()

        total = 0
        by_source: Dict[str, int] = {}
        by_sale_status: Dict[str, int] = {}
        for src, status, cnt in rows:
            total += cnt
            by_source[src] = by_source.get(src, 0) + cnt
            by_sale_status[status] = by_sale_status.get(status, 0) + cnt

        # 마켓등록상품 수 (별도 조건)
        registered_stmt = select(func.count(SambaCollectedProduct.id)).where(
            *build_market_registered_conditions(SambaCollectedProduct),
        )
        registered = (await self.session.execute(registered_stmt)).scalar() or 0

        return {
            "total": total,
            "registered": registered,
            "by_source": by_source,
            "by_sale_status": by_sale_status,
        }

    async def _get_refresh_stats(
        self,
        since_1h: datetime,
        since_24h: datetime,
    ) -> Dict[str, Any]:
        """갱신 통계 — 단일 쿼리로 4개 값 동시 집계."""
        from backend.domain.samba.collector.model import SambaCollectedProduct

        stmt = select(
            func.max(SambaCollectedProduct.last_refreshed_at),
            func.count(SambaCollectedProduct.id).filter(
                SambaCollectedProduct.last_refreshed_at >= since_1h
            ),
            func.count(SambaCollectedProduct.id).filter(
                SambaCollectedProduct.last_refreshed_at >= since_24h,
                SambaCollectedProduct.registered_accounts.isnot(None),
                func.jsonb_typeof(SambaCollectedProduct.registered_accounts) == "array",
                SambaCollectedProduct.registered_accounts.op("!=")(cast("[]", JSONB)),
            ),
            func.count(SambaCollectedProduct.id).filter(
                SambaCollectedProduct.refresh_error_count > 0
            ),
        )
        row = (await self.session.execute(stmt)).one()
        last_refreshed = row[0]

        return {
            "last_refreshed_at": last_refreshed.isoformat() if last_refreshed else None,
            "refreshed_1h": row[1] or 0,
            "refreshed_24h": row[2] or 0,
            "error_products": row[3] or 0,
        }

    async def _get_price_change_stats(
        self,
        since_24h: datetime,
    ) -> Dict[str, Any]:
        """24시간 가격 변동 통계."""
        # DB 레벨에서 24시간 필터링 (ix_sme_event_type_created_at_desc 인덱스 활용)
        recent_events = await self.repo.list_by_type(
            "price_changed", limit=100, since=since_24h
        )

        changes_24h = len(recent_events)

        # 평균 변동률 + TOP 변동
        top_changes: List[Dict[str, Any]] = []
        total_pct = 0.0
        for e in recent_events[:10]:
            d = e.detail or {}
            pct = d.get("diff_pct", 0)
            total_pct += pct
            top_changes.append(
                {
                    "product_id": e.product_id,
                    "name": e.product_name or "",
                    "old": d.get("old_price", 0),
                    "new": d.get("new_price", 0),
                    "pct": round(pct, 1),
                    "at": e.created_at.isoformat(),
                }
            )

        avg_change_pct = round(total_pct / changes_24h, 1) if changes_24h > 0 else 0

        return {
            "changes_24h": changes_24h,
            "avg_change_pct": avg_change_pct,
            "top_changes": top_changes,
        }

    async def _get_site_health(self) -> Dict[str, Any]:
        """소싱처 헬스 상태 — 'probe_%' 키 일괄 조회(N+1 제거)."""
        from backend.domain.samba.collector.refresher import (
            _site_intervals,
            _site_consecutive_errors,
        )
        from backend.domain.samba.forbidden.model import SambaSettings

        sites = ["MUSINSA", "KREAM", "LOTTEON"]
        keys = [f"probe_{s}" for s in sites]
        rows = (
            await self.session.execute(
                select(SambaSettings.key, SambaSettings.value).where(
                    SambaSettings.key.in_(keys)
                )
            )
        ).all()
        kv = {k: v for k, v in rows}

        result: Dict[str, Any] = {}
        for site in sites:
            probe_data = kv.get(f"probe_{site}") or None
            result[site] = {
                "interval": _site_intervals.get(site, 1.0),
                "errors": _site_consecutive_errors.get(site, 0),
                "probe_ok": probe_data.get("ok") if probe_data else None,
                "latency_ms": probe_data.get("latency_ms", 0) if probe_data else None,
                "checked_at": probe_data.get("checked_at") if probe_data else None,
            }

        return result

    async def _get_market_health(self) -> Dict[str, Any]:
        """마켓 헬스 상태 — 'probe_market_%' 키 일괄 조회(N+1 제거)."""
        from backend.domain.samba.forbidden.model import SambaSettings
        from backend.domain.samba.probe.health_checker import MARKET_PROBES

        keys = [f"probe_market_{mt}" for mt in MARKET_PROBES]
        rows = (
            await self.session.execute(
                select(SambaSettings.key, SambaSettings.value).where(
                    SambaSettings.key.in_(keys)
                )
            )
        ).all()
        kv = {k: v for k, v in rows if v}

        result: Dict[str, Any] = {}
        for mt in MARKET_PROBES:
            d = kv.get(f"probe_market_{mt}")
            if d:
                result[mt] = {
                    "probe_ok": d.get("ok"),
                    "latency_ms": d.get("latency_ms", 0),
                    "error": d.get("error"),
                    "checked_at": d.get("checked_at"),
                }

        return result

    async def _get_event_summary(
        self,
        since_24h: datetime,
    ) -> Dict[str, Any]:
        """이벤트 요약: 24시간 타입별 카운트 + 최근 위험/경고."""
        counts = await self.repo.count_by_type_since(since_24h)
        recent_critical = await self.repo.list_by_severity("critical", limit=5)
        recent_warnings = await self.repo.list_by_severity("warning", limit=10)

        def _serialize(e: SambaMonitorEvent) -> Dict[str, Any]:
            return {
                "id": e.id,
                "event_type": e.event_type,
                "severity": e.severity,
                "source_site": e.source_site,
                "product_name": e.product_name,
                "summary": e.summary,
                "created_at": e.created_at.isoformat(),
            }

        return {
            "counts_24h": counts,
            "recent_critical": [_serialize(e) for e in recent_critical],
            "recent_warnings": [_serialize(e) for e in recent_warnings],
        }

    async def get_market_changes(
        self,
        per_market_limit: int = 5,
        limit_per_type: int = 200,
    ) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        """판매처별 최근 가격변동/품절 이벤트 fan-out.

        SambaMonitorEvent의 product_id → SambaCollectedProduct.registered_accounts/
        market_product_nos → SambaMarketAccount(market_type, account_label) 로 펼친다.
        같은 (market_type, event_type) 그룹 안에서 created_at DESC 상위 N건만 유지.

        event_type별로 limit_per_type건씩 가져와 가격변동 폭주가 sold_out/restock을
        윈도우 밖으로 밀어내는 편향을 방지한다.
        """
        from backend.domain.samba.collector.model import SambaCollectedProduct
        from backend.domain.samba.account.model import SambaMarketAccount

        events = await self.repo.list_recent_changes_for_markets(
            event_types=["price_changed", "sold_out", "restock"],
            limit_per_type=limit_per_type,
        )
        if not events:
            return {}

        # 1) 상품 일괄 조회 → registered_accounts / market_product_nos 매핑
        pids = list({e.product_id for e in events if e.product_id})
        prod_map: Dict[str, Dict[str, Any]] = {}
        if pids:
            prod_stmt = select(
                SambaCollectedProduct.id,
                SambaCollectedProduct.registered_accounts,
                SambaCollectedProduct.market_product_nos,
                SambaCollectedProduct.site_product_id,
            ).where(SambaCollectedProduct.id.in_(pids))
            prod_rows = (await self.session.execute(prod_stmt)).all()
            for pid, regs, mpns, spid in prod_rows:
                prod_map[pid] = {
                    "registered_accounts": regs or [],
                    "market_product_nos": mpns or {},
                    "site_product_id": spid,
                }

        # 2) 모든 account_id 모아서 일괄 조회 → market_type/account_label 매핑
        all_account_ids: set[str] = set()
        for v in prod_map.values():
            for aid in v["registered_accounts"]:
                if aid:
                    all_account_ids.add(aid)
            mpns = v["market_product_nos"]
            if isinstance(mpns, dict):
                for aid in mpns.keys():
                    if aid:
                        all_account_ids.add(aid)

        acc_map: Dict[str, Dict[str, Any]] = {}
        if all_account_ids:
            acc_stmt = select(
                SambaMarketAccount.id,
                SambaMarketAccount.market_type,
                SambaMarketAccount.account_label,
                SambaMarketAccount.business_name,
            ).where(SambaMarketAccount.id.in_(all_account_ids))
            for aid, mt, label, biz in (await self.session.execute(acc_stmt)).all():
                acc_map[aid] = {
                    "market_type": mt,
                    "account_label": label or biz or "",
                }

        # 3) fan-out: 이벤트 × 등록된 마켓계정
        result: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for ev in events:
            pinfo = prod_map.get(ev.product_id or "")
            if not pinfo:
                continue
            account_ids = pinfo["registered_accounts"] or []
            mpns = pinfo["market_product_nos"] or {}
            for aid in account_ids:
                ainfo = acc_map.get(aid)
                if not ainfo:
                    continue
                market_type = ainfo["market_type"]
                row = {
                    "id": f"{ev.id}__{aid}",
                    "event_id": ev.id,
                    "created_at": ev.created_at.isoformat(),
                    "source_site": ev.source_site,
                    "market_product_no": (
                        mpns.get(aid) if isinstance(mpns, dict) else None
                    ),
                    "site_product_id": pinfo.get("site_product_id"),
                    "account_id": aid,
                    "account_label": ainfo["account_label"],
                    "product_id": ev.product_id,
                    "product_name": ev.product_name,
                    "detail": ev.detail,
                }
                bucket = result.setdefault(market_type, {}).setdefault(
                    ev.event_type, []
                )
                bucket.append(row)

        # 각 (market_type, event_type)별로 최신순 정렬 후 상위 N개만 유지
        for market_type in result:
            for event_type in result[market_type]:
                # created_at DESC로 정렬하여 최신순 우선
                result[market_type][event_type].sort(
                    key=lambda x: x["created_at"], reverse=True
                )
                # 상위 per_market_limit개만 유지
                result[market_type][event_type] = result[market_type][event_type][
                    :per_market_limit
                ]
        return result

    async def _get_hourly_changes(
        self,
        since_24h: datetime,
    ) -> List[int]:
        """24시간 시간대별 가격변동 건수 (0시~23시)."""
        hourly_data = await self.repo.count_hourly_since("price_changed", since_24h)
        hour_map = {item["hour"]: item["count"] for item in hourly_data}
        return [hour_map.get(h, 0) for h in range(24)]

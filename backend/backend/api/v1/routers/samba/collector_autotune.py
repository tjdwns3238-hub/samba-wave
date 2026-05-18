"""SambaWave Collector — 자동조율(오토튠) 엔드포인트.

PC별 독립 인스턴스 모델 (2026-05-12):
  - 각 PC가 자기 device_id를 키로 자기 인스턴스를 가짐
  - 시작/중지/사이클/잡 발행 모두 PC 단위로 분리
  - 같은 사이트를 두 PC가 동시 처리 가능 (중복 갱신은 멱등)
  - 잡 발행 시 owner_device_id=발행자_PC로 박혀서 다른 PC가 가로채지 못함
"""

import asyncio
import contextvars
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import func, case, update as sa_update
from sqlalchemy.orm import defer
from sqlmodel import select

from backend.api.v1.routers.samba.collector_common import (
    _trim_history,
)
from backend.domain.samba.exchange_rate_service import convert_cost_by_source_site

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collector", tags=["samba-collector"])


def _is_stale_conn_error(exc: BaseException) -> bool:
    """좀비 connection/끊긴 트랜잭션 감지.

    Cloud SQL idle_in_transaction_session_timeout으로 끊긴 세션을 재사용할 때
    SQLAlchemy/asyncpg가 던지는 대표 메시지/예외 이름을 모아서 판정한다.
    """
    msg = str(exc)
    name = type(exc).__name__
    return (
        "Can't reconnect" in msg
        or "invalid transaction" in msg
        or "InvalidRequestError" in name
        or "PendingRollbackError" in name
        or "OperationalError" in name
        or "InterfaceError" in name
        or "connection is closed" in msg.lower()
        or "ssl connection has been closed" in msg.lower()
        or "terminating connection due to" in msg.lower()
    )


# ══════════════════════════════════════════════════════════════
# 오토튠 백그라운드 루프 — PC별 독립 인스턴스
# ══════════════════════════════════════════════════════════════

# PC별 인스턴스 상태 (key = device_id)
_pc_running: dict[str, asyncio.Event] = {}
_pc_main_task: dict[str, asyncio.Task] = {}
_pc_cycle_count: dict[str, int] = {}
_pc_restart_count: dict[str, int] = {}
_pc_last_tick: dict[str, str] = {}
_pc_site_tasks: dict[str, dict[str, asyncio.Task]] = {}
_pc_site_cycle_counts: dict[str, dict[str, int]] = {}
_pc_site_last_ticks: dict[str, dict[str, str]] = {}
_pc_site_empty_hits: dict[str, dict[str, int]] = {}
_pc_site_heartbeats: dict[str, dict[str, float]] = {}
_pc_target_ids: dict[str, Optional[set]] = {}

# 잡 발행자 PC를 사이트별/상품별 호출 컨텍스트에 전파 (sourcing_queue.get_autotune_owner가 읽음).
# 사이트 루프 진입 시 set, 종료 시 reset. PC별 독립 실행 → 컨텍스트 격리 보장.
current_pc_owner: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_pc_owner", default=""
)

# 소싱처별 품절 서킷브레이커 (사이트 단위 글로벌 — 모든 PC 공유)
SOLDOUT_BREAK_THRESHOLD = 10  # 연속 품절 N개 → 해당 소싱처 중단
_site_consecutive_soldout: dict[str, int] = {}  # {소싱처: 연속 품절 수}
_site_breaker_tripped: dict[str, bool] = {}  # {소싱처: 중단 여부}

# 원가 상승 확인 대기 — 소싱처 보조 API(쿠폰/혜택) 실패 시 1사이클 확인 후 전송
# {product_id: pending_cost}: 이전 사이클에서 감지된 원가 상승값 보관
_pending_cost_increase: dict[str, float] = {}

# 사이트별 연속 빈 결과 cooldown (사이트 단위 글로벌 — 모든 PC 공유)
SITE_EMPTY_SKIP_THRESHOLD = 3  # N회 연속 빈 결과 시 해당 소싱처 60초 제외
_site_empty_skip_until: dict[str, float] = {}  # {소싱처: 제외 해제 시각(time.time())}

# 오토튠 진행도 글로벌 카운터 — 로그 [n/total] 표시용
# 사이클당 200건 배치이지만, 분자/분모는 "이번 회전의 전체 대상" 기준으로 누적 표시.
# 한 바퀴 회전(분자 ≥ 분모) 시 0부터 다시 시작.
_autotune_global_idx: dict[tuple[str, str], int] = {}  # (device_id, site) → 처리누계
_autotune_global_total: dict[
    tuple[str, str], int
] = {}  # (device_id, site) → 전체 대상수


# 한 사이클(전체 1바퀴) 누적 통계 — 배치마다 합산, 사이클 완료 시 출력 후 리셋
def _new_cycle_stats() -> dict:
    return {
        "ok": 0,
        "err": 0,
        "no_pid": 0,
        "blocked": 0,
        "timeout": 0,
        "other": 0,
        "total": 0,
        "price_pids": set(),
        "stock_pids": set(),
        "synced": 0,
        "deleted": 0,
        "batches": 0,
        "started_at": None,
    }


_autotune_cycle_stats: dict[tuple[str, str], dict] = {}

# Watchdog
STUCK_TIMEOUT_SECONDS = 300  # 5분간 heartbeat 없으면 stuck 판정
MAX_RESTART_COUNT = 50  # 코디네이터 재시작 상한선


def _get_pc_event(dev: str) -> asyncio.Event:
    """PC별 running event 가져오기/생성."""
    ev = _pc_running.get(dev)
    if ev is None:
        ev = asyncio.Event()
        _pc_running[dev] = ev
    return ev


def _is_pc_running(dev: str) -> bool:
    """해당 PC의 오토튠 인스턴스가 실행 중인지."""
    ev = _pc_running.get(dev)
    return ev is not None and ev.is_set()


def _pc_st(dev: str) -> dict[str, asyncio.Task]:
    """PC별 site_tasks dict (없으면 생성)."""
    d = _pc_site_tasks.get(dev)
    if d is None:
        d = {}
        _pc_site_tasks[dev] = d
    return d


def _pc_hb(dev: str) -> dict[str, float]:
    """PC별 site_heartbeats dict."""
    d = _pc_site_heartbeats.get(dev)
    if d is None:
        d = {}
        _pc_site_heartbeats[dev] = d
    return d


def _pc_scc(dev: str) -> dict[str, int]:
    """PC별 site_cycle_counts dict."""
    d = _pc_site_cycle_counts.get(dev)
    if d is None:
        d = {}
        _pc_site_cycle_counts[dev] = d
    return d


def _pc_slt(dev: str) -> dict[str, str]:
    """PC별 site_last_ticks dict."""
    d = _pc_site_last_ticks.get(dev)
    if d is None:
        d = {}
        _pc_site_last_ticks[dev] = d
    return d


def _pc_seh(dev: str) -> dict[str, int]:
    """PC별 site_empty_hits dict."""
    d = _pc_site_empty_hits.get(dev)
    if d is None:
        d = {}
        _pc_site_empty_hits[dev] = d
    return d


def _cleanup_pc_instance(dev: str) -> None:
    """PC 인스턴스 상태 전부 제거 (stop 후 호출)."""
    _pc_running.pop(dev, None)
    _pc_main_task.pop(dev, None)
    _pc_cycle_count.pop(dev, None)
    _pc_restart_count.pop(dev, None)
    _pc_last_tick.pop(dev, None)
    _pc_site_tasks.pop(dev, None)
    _pc_site_cycle_counts.pop(dev, None)
    _pc_site_last_ticks.pop(dev, None)
    _pc_site_empty_hits.pop(dev, None)
    _pc_site_heartbeats.pop(dev, None)
    _pc_target_ids.pop(dev, None)


def any_pc_running() -> bool:
    """어떤 PC라도 오토튠 실행 중이면 True."""
    return any(ev.is_set() for ev in _pc_running.values())


# 등급 분류 기준 기간 (일)
CLASSIFY_WINDOW_DAYS = 7

# 오토튠 필터 설정 키 (samba_settings)
AUTOTUNE_FILTER_SOURCES_KEY = "autotune_enabled_sources"
AUTOTUNE_FILTER_MARKETS_KEY = "autotune_enabled_markets"
AUTOTUNE_PRIORITY_ENABLED_KEY = "autotune_priority_enabled"

# 오토튠 전송 글로벌 동시실행 제한 — refresher가 fire-and-forget으로 띄운 transmit task가
# OOM 일으키지 않도록 상한. 너무 낮으면 backlog, 너무 높으면 메모리 폭주.
# 정책 변경 직후 폭주 시 backlog는 이벤트 루프가 자연스럽게 흡수 (백프레셔).
_AUTOTUNE_TRANSMIT_MAX_CONCURRENCY = int(
    os.environ.get("AUTOTUNE_TRANSMIT_MAX_CONCURRENCY", "5")
)
_autotune_transmit_sem: Optional[asyncio.Semaphore] = None


def _get_transmit_sem() -> asyncio.Semaphore:
    """이벤트 루프에 바인딩된 세마포어 lazy init (모듈 import 시점엔 루프 없음)."""
    global _autotune_transmit_sem
    if _autotune_transmit_sem is None:
        _autotune_transmit_sem = asyncio.Semaphore(_AUTOTUNE_TRANSMIT_MAX_CONCURRENCY)
    return _autotune_transmit_sem


async def _run_transmit_in_background(coro_factory):
    """fire-and-forget으로 전송 실행 — 세마포어로 동시 실행 제한.

    coro_factory: 호출 시 코루틴을 반환하는 callable.
    예외는 로그로만 남김 (refresher 본 흐름에 영향 없음).
    """
    from backend.domain.samba.collector.refresher import is_bulk_cancelled

    sem = _get_transmit_sem()
    async with sem:
        if is_bulk_cancelled("transmit"):
            return
        try:
            await coro_factory()
        except Exception as exc:
            logger.warning(f"[오토튠][백그라운드전송] 실패: {exc}")


# PC별 분담 등록: device_id → set of sites this PC will process.
# 폴링 시점에 X-Allowed-Sites 헤더로 갱신, PC_LAST_SEEN_TTL 동안 폴링 없으면 자동 제거.
_pc_allowed_sites: dict[str, set[str]] = {}
_pc_last_seen: dict[str, float] = {}
PC_LAST_SEEN_TTL = 86400.0  # 24시간
# 다음 폴링 시 해당 PC에게만 forceStop 신호를 전달할 집합 (개별 중지용)
_pc_force_stop_set: set[str] = set()


def update_pc_last_seen(device_id: str) -> None:
    """확장앱 폴링 도착 시 호출 — 해당 PC가 살아있다는 표시."""
    if device_id:
        _pc_last_seen[device_id.strip()] = time.time()


def register_pc_allowed_sites(device_id: str, sites: list[str] | None) -> None:
    """PC 분담 등록/갱신 (UI/폴링용 메타데이터).

    sites=None → 등록 제거
    sites=[] → 빈 분담 (이 PC는 아무 사이트 안 받음)
    sites=[...] → 명시 사이트만 받음

    이 등록값은 UI 표시(pc_assignments)와 폴링 X-Allowed-Sites 헤더의 출처일 뿐,
    실제 오토튠 사이클 active_sites 결정은 시작한 PC의 인스턴스가 자기 사이트만
    독립적으로 처리하므로 직접적인 영향 없음.
    """
    dev = (device_id or "").strip()
    if not dev:
        return
    if sites is None:
        _pc_allowed_sites.pop(dev, None)
        _pc_last_seen.pop(dev, None)
        return
    _pc_allowed_sites[dev] = {s.strip() for s in sites if s and s.strip()}
    _pc_last_seen[dev] = time.time()


def get_active_pcs() -> dict[str, set[str]]:
    """stale PC 정리 후 살아있는 PC들의 분담 매핑 반환."""
    now = time.time()
    stale = [d for d, ts in _pc_last_seen.items() if now - ts > PC_LAST_SEEN_TTL]
    for d in stale:
        _pc_last_seen.pop(d, None)
        _pc_allowed_sites.pop(d, None)
    return {d: sites for d, sites in _pc_allowed_sites.items() if d in _pc_last_seen}


def get_pc_allowed_sites(device_id: str) -> set[str] | None:
    """해당 PC가 처리할 사이트 집합. 미등록이면 None(=전체)."""
    dev = (device_id or "").strip()
    if not dev:
        return None
    pcs = get_active_pcs()
    return pcs.get(dev)


async def _classify_products(session) -> dict[str, int]:
    """마켓등록상품 대상 hot/warm/cold 자동 분류 (벌크 SQL 3건).

    hot  = 최근 7일 주문 있음 AND 가격/재고 변동 있음
    warm = 최근 7일 가격/재고 변동 있음 (주문 없음)
    cold = 나머지 (마켓등록상품 한정)
    """
    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP
    from backend.domain.samba.order.model import SambaOrder

    log = logging.getLogger("autotune")
    cutoff = datetime.now(timezone.utc) - timedelta(days=CLASSIFY_WINDOW_DAYS)

    # 마켓등록상품 공통 조건 (collector_common에서 통합 관리)
    from backend.api.v1.routers.samba.collector_common import (
        build_market_registered_conditions,
    )

    registered_cond = build_market_registered_conditions(_CP)

    # 최근 7일 주문이 있는 product_id 서브쿼리
    order_subq = (
        select(SambaOrder.product_id)
        .where(SambaOrder.created_at >= cutoff)
        .where(SambaOrder.product_id != None)
        .distinct()
    )

    # 가격/재고 변동 조건: price_changed_at이 7일 이내
    has_changes = _CP.price_changed_at >= cutoff

    # 1단계: 마켓등록상품 전체 → cold
    stmt_cold = (
        sa_update(_CP)
        .where(*registered_cond)
        .where(_CP.monitor_priority != "cold")
        .values(monitor_priority="cold")
    )
    r_cold = await session.execute(stmt_cold)

    # 2단계: 변동 있는 상품 → warm
    stmt_warm = (
        sa_update(_CP)
        .where(*registered_cond, has_changes)
        .values(monitor_priority="warm")
    )
    r_warm = await session.execute(stmt_warm)

    # 3단계: 변동 + 주문 있는 상품 → hot
    stmt_hot = (
        sa_update(_CP)
        .where(*registered_cond, has_changes)
        .where(_CP.id.in_(order_subq))
        .values(monitor_priority="hot")
    )
    r_hot = await session.execute(stmt_hot)

    await session.commit()

    counts = {"hot": r_hot.rowcount, "warm": r_warm.rowcount, "cold": r_cold.rowcount}
    log.info(
        "[오토튠] 등급 분류 완료 — hot %d, warm %d, cold %d",
        counts["hot"],
        counts["warm"],
        counts["cold"],
    )
    return counts


async def _stream_event(
    event_type: str,
    severity: str,
    *,
    summary: str,
    source_site: Optional[str] = None,
    product_id: Optional[str] = None,
    product_name: Optional[str] = None,
    detail: Optional[dict] = None,
) -> Optional[str]:
    """사이클 도중 변동 감지 시 즉시 DB에 이벤트 저장.

    사이클 완료를 기다리지 않고 바로 커밋하므로, 서버 재시작이 발생해도
    감지된 변동은 유실되지 않는다. 별도 세션을 열어 본 사이클 세션에 영향 없음.

    Returns:
        생성된 이벤트 id. 실패 시 None.
    """
    try:
        from backend.db.orm import get_write_session
        from backend.domain.samba.warroom.service import SambaMonitorService

        async with get_write_session() as _sess:
            _mon = SambaMonitorService(_sess)
            _eid = await _mon.emit(
                event_type,
                severity,
                summary=summary,
                source_site=source_site,
                product_id=product_id,
                product_name=product_name,
                detail=detail or {},
            )
            await _sess.commit()
            return _eid
    except Exception as _err:
        logging.getLogger("autotune").warning(
            "[오토튠] 이벤트 스트리밍 실패(%s): %s", event_type, _err
        )
        return None


async def _patch_event_detail(event_id: str, patch: dict) -> None:
    """기 발행된 이벤트의 detail JSONB를 사후 update (merge).

    예: 품절 이벤트를 즉시 스트리밍해 둔 뒤, 마켓 판매중지가 끝났을 때
    suspended_markets 라벨을 채우기 위해 호출한다. 별도 세션을 사용한다.
    """
    if not event_id:
        return
    try:
        from backend.db.orm import get_write_session
        from backend.domain.samba.warroom.repository import (
            SambaMonitorEventRepository,
        )

        async with get_write_session() as _sess:
            _repo = SambaMonitorEventRepository(_sess)
            await _repo.update_event_detail(event_id, patch)
            await _sess.commit()
    except Exception as _err:
        logging.getLogger("autotune").warning(
            "[오토튠] 이벤트 detail 업데이트 실패(%s): %s", event_id, _err
        )


async def _site_autotune_loop(device_id: str, site: str):
    """소싱처별 독립 오토튠 루프 — 작업 완료 즉시 다음 사이클 재시작.

    device_id: 이 루프 소속 PC. 발행되는 모든 잡의 owner_device_id로 박힘.
    """
    log = logging.getLogger("autotune")
    log.info("[오토튠][%s][%s] 소싱처 루프 시작", device_id[:8], site)

    # 발행자 PC를 컨텍스트에 박아 sourcing_queue.get_autotune_owner가 읽을 수 있게 함
    _owner_token = current_pc_owner.set(device_id)
    try:
        _cycle_seq = 0
        while _is_pc_running(device_id):
            _cycle_seq += 1
            _cycle_started_ts = time.time()
            log.info(
                "[오토튠][디버그][%s][%s] 사이클 #%d 시작 (epoch=%.3f)",
                device_id[:8],
                site,
                _cycle_seq,
                _cycle_started_ts,
            )
            try:
                # Watchdog heartbeat 갱신
                _pc_hb(device_id)[site] = time.time()

                from backend.domain.samba.emergency import is_emergency_stopped

                if is_emergency_stopped():
                    await asyncio.sleep(5)
                    continue

                # 서킷브레이커 확인
                if _site_breaker_tripped.get(site):
                    log.info("[오토튠][%s] 서킷브레이커 작동 중 — 대기", site)
                    await asyncio.sleep(30)
                    continue

                from backend.db.orm import get_write_session

                async with get_write_session() as session:
                    from backend.domain.samba.collector.refresher import (
                        refresh_products_bulk,
                    )
                    from backend.domain.samba.collector.repository import (
                        SambaCollectedProductRepository,
                    )
                    from backend.domain.samba.collector.model import (
                        SambaCollectedProduct as _CP,
                    )
                    from backend.domain.samba.warroom.service import SambaMonitorService

                    now = datetime.now(timezone.utc)
                    repo = SambaCollectedProductRepository(session)

                    # 이 소싱처 상품만 조회
                    from backend.api.v1.routers.samba.collector_common import (
                        build_market_registered_conditions,
                    )
                    from backend.api.v1.routers.samba.proxy import _get_setting

                    market_cond = build_market_registered_conditions(_CP)

                    _priority_enabled = await _get_setting(
                        session, AUTOTUNE_PRIORITY_ENABLED_KEY
                    )
                    _use_priority = (
                        _priority_enabled
                        if isinstance(_priority_enabled, bool)
                        else True
                    )

                    if _use_priority:
                        priority_order = case(
                            (_CP.monitor_priority == "hot", 0),
                            (_CP.monitor_priority == "warm", 1),
                            else_=2,
                        )
                        _order_clause = (
                            priority_order,
                            _CP.last_refreshed_at.asc().nullsfirst(),
                        )
                    else:
                        _order_clause = (_CP.last_refreshed_at.asc().nullsfirst(),)

                    _where = [
                        *market_cond,
                        _CP.applied_policy_id != None,
                        _CP.source_site == site,
                    ]
                    # 단일 상품 오토튠 필터 (PC별)
                    _target_ids = _pc_target_ids.get(device_id)
                    if _target_ids:
                        _where.append(_CP.id.in_(_target_ids))
                    # 사이클당 배치 상한 — 무신사처럼 등록상품 많은 소싱처에서
                    # 한 사이클 SELECT/사전로딩이 너무 길어져 첫 처리까지 수십 초~수 분
                    # 대기하는 문제 방지. 정렬이 last_refreshed_at asc nullsfirst 이므로
                    # 오래된 상품부터 자연스럽게 순환됨. 단일 타겟(_target_ids)은 그대로 전체 처리.
                    _AUTOTUNE_CYCLE_BATCH = int(
                        os.environ.get("AUTOTUNE_CYCLE_BATCH", "200")
                    )
                    stmt = (
                        select(_CP)
                        .where(*_where)
                        .order_by(*_order_clause)
                        .options(
                            defer(_CP.detail_html),
                            defer(_CP.detail_images),
                            defer(_CP.images),
                            defer(_CP.extra_data),
                        )
                    )
                    if not _target_ids:
                        stmt = stmt.limit(_AUTOTUNE_CYCLE_BATCH)
                    result = await session.exec(stmt)
                    _seen_ids: set[str] = set()
                    products = []
                    for p in result.all():
                        if p.id not in _seen_ids:
                            _seen_ids.add(p.id)
                            products.append(p)

                    # ── 판매처 필터 사전 적용 ──
                    # _enabled_markets 활성 시, 활성 마켓 타입에 등록된 계정이 하나라도
                    # 있는 상품만 통과시킨다. (refresh 전에 잘라서 ABC/무신사 등 대용량
                    # 소싱처에서 마켓 등록 안 된 상품까지 무의미하게 갱신되는 낭비를 차단)
                    # _on_result(833-845)의 per-account 필터는 다중 마켓 등록 상품의
                    # 송신 계정 좁히기 용도로 별개 — 그대로 유지한다.
                    _enabled_markets = await _get_setting(
                        session, AUTOTUNE_FILTER_MARKETS_KEY
                    )
                    _market_filter_active = bool(
                        _enabled_markets and isinstance(_enabled_markets, list)
                    )
                    if _market_filter_active and products:
                        _enabled_markets_set = set(_enabled_markets)
                        _pre_acc_ids: set[str] = set()
                        for _p in products:
                            if _p.registered_accounts:
                                _pre_acc_ids.update(_p.registered_accounts)
                        _eligible_acc_ids: set[str] = set()
                        if _pre_acc_ids:
                            from backend.domain.samba.account.model import (
                                SambaMarketAccount as _PreAcc,
                            )

                            _pre_acc_stmt = select(
                                _PreAcc.id, _PreAcc.market_type
                            ).where(_PreAcc.id.in_(list(_pre_acc_ids)))
                            _pre_acc_res = await session.execute(_pre_acc_stmt)
                            for _aid, _mt in _pre_acc_res.all():
                                if _mt in _enabled_markets_set:
                                    _eligible_acc_ids.add(_aid)
                        _before_cnt = len(products)
                        products = [
                            _p
                            for _p in products
                            if _p.registered_accounts
                            and any(
                                _a in _eligible_acc_ids for _a in _p.registered_accounts
                            )
                        ]
                        if len(products) < _before_cnt:
                            log.info(
                                "[오토튠][%s] 판매처 필터: %d→%d건 (활성 %s)",
                                site,
                                _before_cnt,
                                len(products),
                                ",".join(_enabled_markets),
                            )

                    if products:
                        filtered_count = len(products)
                        # 진행도 표시용 전체 대상 COUNT — 같은 WHERE 사용 (판매처 필터 SQL 미적용분은 분모가 약간 클 수 있음)
                        try:
                            _count_stmt = (
                                select(func.count()).select_from(_CP).where(*_where)
                            )
                            _total_global_res = await session.execute(_count_stmt)
                            _total_global = int(_total_global_res.scalar() or 0)
                        except Exception:
                            _total_global = filtered_count
                        _gkey = (device_id, site)
                        # 한 바퀴 회전 완료(분자 ≥ 분모) 또는 분모 변동 시 0부터 재시작
                        _prev_idx = _autotune_global_idx.get(_gkey, 0)
                        _prev_total = _autotune_global_total.get(_gkey, 0)
                        if (
                            _prev_idx >= _total_global
                            or _total_global <= 0
                            or _prev_total != _total_global
                        ):
                            _autotune_global_idx[_gkey] = 0
                            _autotune_cycle_stats[_gkey] = _new_cycle_stats()
                            _autotune_cycle_stats[_gkey]["started_at"] = now.isoformat()
                        elif _gkey not in _autotune_cycle_stats:
                            _autotune_cycle_stats[_gkey] = _new_cycle_stats()
                            _autotune_cycle_stats[_gkey]["started_at"] = now.isoformat()
                        _autotune_global_total[_gkey] = _total_global
                        log.info(
                            "[오토튠][디버그][%s][%s] 사이클 #%d SELECT 완료: %d건 대상 / 전체 %d건 (진행 %d) (elapsed=%.1fs)",
                            device_id[:8],
                            site,
                            _cycle_seq,
                            filtered_count,
                            _total_global,
                            _autotune_global_idx.get(_gkey, 0),
                            time.time() - _cycle_started_ts,
                        )

                        # 결과 처리에 필요한 서비스 사전 초기화
                        import backend.domain.samba.collector.refresher as _ref_mod
                        from backend.domain.samba.shipment.service import (
                            calc_market_price,
                        )
                        from backend.domain.samba.policy.repository import (
                            SambaPolicyRepository,
                        )
                        from backend.domain.samba.account.repository import (
                            SambaMarketAccountRepository,
                        )
                        from backend.domain.samba.shipment.dispatcher import (
                            delete_from_market,
                        )
                        from backend.domain.samba.emergency import is_emergency_stopped

                        product_map: dict[str, object] = {p.id: p for p in products}
                        _policy_cache: dict[str, object] = {}
                        _account_cache: dict[str, object] = {}
                        account_repo = SambaMarketAccountRepository(session)
                        policy_repo = SambaPolicyRepository(session)

                        # 계정 사전 로드
                        _all_account_ids: set[str] = set()
                        for _p in products:
                            if _p.registered_accounts:
                                _all_account_ids.update(_p.registered_accounts)
                        if _all_account_ids:
                            from backend.domain.samba.account.model import (
                                SambaMarketAccount,
                            )

                            _acc_stmt = select(SambaMarketAccount).where(
                                SambaMarketAccount.id.in_(list(_all_account_ids))
                            )
                            _acc_result = await session.exec(_acc_stmt)
                            for _acc in _acc_result.all():
                                _account_cache[_acc.id] = _acc

                        # 정책 사전 로드 — _on_result에서 세션 없이 캐시에서 읽을 수 있도록
                        _all_policy_ids = {
                            _p.applied_policy_id
                            for _p in products
                            if _p.applied_policy_id
                        }
                        if _all_policy_ids:
                            from backend.domain.samba.policy.model import (
                                SambaPolicy as _SPol,
                            )

                            _pol_stmt = select(_SPol).where(
                                _SPol.id.in_(list(_all_policy_ids))
                            )
                            _pol_result = await session.exec(_pol_stmt)
                            for _pol in _pol_result.all():
                                _policy_cache[_pol.id] = _pol

                        # 롯데홈쇼핑 자격증명 사전 로드 (등록 계정 중 lottehome이 있을 때만)
                        _lottehome_creds: dict = {}
                        _has_lottehome = any(
                            getattr(_account_cache.get(acc_id), "market_type", "")
                            == "lottehome"
                            for _p in products
                            for acc_id in (_p.registered_accounts or [])
                        )
                        if _has_lottehome:
                            from backend.domain.samba.forbidden.model import (
                                SambaSettings as _SS2,
                            )
                            from sqlmodel import select as _sel2

                            _lh_row = (
                                await session.exec(
                                    _sel2(_SS2).where(
                                        _SS2.key == "lottehome_credentials"
                                    )
                                )
                            ).first()
                            _lottehome_creds = (_lh_row.value if _lh_row else {}) or {}

                        # SELECT 완료 후 즉시 커밋 + 연결 반납 — refresh HTTP 동안 idle in transaction 방지
                        # expire_on_commit=False이므로 products/_account_cache/_policy_cache 객체는 커밋 후에도 유효
                        # session.close()는 연결만 풀에 반납, 세션 객체는 재사용 가능 (soldout 재시도 블록에서 재획득)
                        await session.commit()
                        await session.close()

                        retransmitted = 0
                        deleted_count = 0
                        price_changed_count = 0
                        _all_price_pids: set[str] = set()
                        _price_tx_items: list[dict] = []
                        _stock_tx_items: list[dict] = []
                        # product_id → event (사이클 끝 배치 발행용) — 상품당 1건 dedupe
                        _price_change_events: dict[str, dict] = {}
                        _all_stock_pids: set[str] = set()
                        _cycle_deleted_pids: set[str] = (
                            set()
                        )  # 사이클 중 삭제된 상품 ID
                        _session_lock = asyncio.Lock()
                        _synced_count = 0
                        # LOTTEON slPrc 재동기화 — 사이클당 최대 검증 건수 (환경변수로 튜닝)
                        _lot_verify_count = 0
                        _lot_verify_cap = int(
                            os.environ.get("LOTTEON_VERIFY_SLPRC_BATCH", "50")
                        )

                        def _log_line(site, pid, msg, level="info"):
                            """오토튠 통합 로그 (한 줄)."""
                            _kst_now = (
                                datetime.now(timezone.utc) + timedelta(hours=9)
                            ).strftime("%H:%M:%S")
                            _ref_mod._refresh_log_buffer.append(
                                {
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                    "site": site,
                                    "product_id": pid,
                                    "name": "",
                                    "msg": f"[{_kst_now}] {msg}",
                                    "level": level,
                                    "source": "autotune",
                                }
                            )
                            _ref_mod._refresh_log_total += 1

                        async def _partial_delete(pid: str) -> bool:
                            """오토튠 품절 → 전 마켓 삭제 성공 시 상품 자체 삭제.

                            _partial_update와 동일하게 새 세션을 매번 획득해 좀비
                            connection 회피 + 1회 재시도 패턴 적용.
                            """
                            from sqlalchemy import delete as _sa_delete
                            from backend.domain.samba.collector.model import (
                                SambaCollectedProduct as _PD_CP,
                            )
                            from backend.db.orm import (
                                get_write_session as _get_pd_session,
                            )

                            stmt = _sa_delete(_PD_CP).where(_PD_CP.id == pid)
                            _last_exc: Exception | None = None
                            for _attempt in range(2):
                                try:
                                    async with _get_pd_session() as _pd_s:
                                        await _pd_s.execute(stmt)
                                        await _pd_s.commit()
                                    return True
                                except Exception as _ex:
                                    _last_exc = _ex
                                    if _is_stale_conn_error(_ex) and _attempt == 0:
                                        log.warning(
                                            "[오토튠][DB재시도] partial_delete %s "
                                            "좀비 connection 감지 → 새 세션으로 재시도: %s",
                                            pid,
                                            str(_ex)[:120],
                                        )
                                        await asyncio.sleep(0.1)
                                        continue
                                    raise
                            if _last_exc:
                                raise _last_exc
                            return False

                        async def _partial_update(pid: str, vals: dict):
                            """last_sent_data를 건드리지 않는 partial UPDATE.

                            outer session은 refresh 직전 close()됐으므로 재사용 시
                            greenlet_spawn 에러 발생 — 매번 새 세션 획득.

                            좀비 connection을 풀에서 받아오면 첫 execute가 'Can't
                            reconnect until invalid transaction is rolled back'으로
                            깨지는데, 풀이 다음 연결을 새로 채워주므로 1회 재시도하면
                            대부분 통과한다. 2회까지만 시도해 무한루프 방지.
                            """
                            from backend.domain.samba.collector.model import (
                                SambaCollectedProduct as _PU_CP,
                            )
                            from backend.db.orm import (
                                get_write_session as _get_pu_session,
                            )

                            vals["updated_at"] = datetime.now(timezone.utc)
                            stmt = (
                                sa_update(_PU_CP).where(_PU_CP.id == pid).values(**vals)
                            )
                            _last_exc: Exception | None = None
                            for _attempt in range(2):
                                try:
                                    async with _get_pu_session() as _pu_s:
                                        await _pu_s.execute(stmt)
                                        await _pu_s.commit()
                                    return
                                except Exception as _ex:
                                    _last_exc = _ex
                                    if _is_stale_conn_error(_ex) and _attempt == 0:
                                        log.warning(
                                            "[오토튠][DB재시도] partial_update %s "
                                            "좀비 connection 감지 → 새 세션으로 재시도: %s",
                                            pid,
                                            str(_ex)[:120],
                                        )
                                        await asyncio.sleep(0.1)
                                        continue
                                    raise
                            if _last_exc:
                                raise _last_exc

                        async def _on_result(product, r, idx=0, total=0):
                            """리프레시 직후 호출 — DB 업데이트 + 즉시 마켓 전송."""
                            nonlocal \
                                retransmitted, \
                                deleted_count, \
                                price_changed_count, \
                                _cycle_deleted_pids, \
                                _synced_count, \
                                _lot_verify_count

                            async with _session_lock:
                                # heartbeat 갱신 — Watchdog stuck 오판 방지 (PC별)
                                _pc_hb(device_id)[product.source_site or "UNKNOWN"] = (
                                    time.time()
                                )

                                if (
                                    not _is_pc_running(device_id)
                                    or is_emergency_stopped()
                                ):
                                    return

                                site = product.source_site or "UNKNOWN"
                                _prod_name = (product.name or "")[:40]
                                _site_pid = product.site_product_id or ""
                                _brand = (product.brand or "")[:20]
                                _name_part = (
                                    f"[{site}] {_brand} {_prod_name}".strip()
                                    if _brand
                                    else f"[{site}] {_prod_name}"
                                )
                                _prod_label = (
                                    f"{_name_part} ({_site_pid})"
                                    if _site_pid
                                    else _name_part
                                )
                                # 진행도 표시 — 사이클 배치(200) 기준이 아닌 "전체 대상" 누계 기준
                                _autotune_global_idx[_gkey] = (
                                    _autotune_global_idx.get(_gkey, 0) + 1
                                )
                                _g_idx = _autotune_global_idx[_gkey]
                                _g_total = _autotune_global_total.get(_gkey, 0)
                                _idx_prefix = (
                                    f"[{_g_idx:,}/{_g_total:,}] "
                                    if _g_idx and _g_total
                                    else (
                                        f"[{idx:,}/{total:,}] " if idx and total else ""
                                    )
                                )

                                # 원가: 항상 최신 계산값 사용
                                if r.new_cost is not None:
                                    _cur_cost = r.new_cost
                                else:
                                    _cur_cost = product.cost or product.sale_price or 0
                                _cost_int = int(_cur_cost) if _cur_cost else 0

                                # DB 업데이트 준비 — 실제 처리 시점 기록 (사이클 시작 now 아님)
                                updates: dict = {
                                    "last_refreshed_at": datetime.now(timezone.utc),
                                    "refresh_error_count": 0,
                                }
                                snapshot: dict = {
                                    "date": now.isoformat(),
                                    "source": "autotune",
                                    "sale_price": r.new_sale_price
                                    if r.new_sale_price is not None
                                    else product.sale_price,
                                    "original_price": r.new_original_price
                                    if r.new_original_price is not None
                                    else product.original_price,
                                    "cost": r.new_cost
                                    if r.new_cost is not None
                                    else product.cost,
                                    "sale_status": r.new_sale_status,
                                    "changed": r.changed,
                                }
                                # 옵션: 신규 수집 우선, 없으면 기존 DB 옵션 폴백
                                # 품절인데 new_options 없으면 기존 옵션의 재고를 0으로 처리
                                _snap_options = r.new_options
                                if not _snap_options and product.options:
                                    if r.new_sale_status == "sold_out":
                                        _snap_options = [
                                            {**o, "stock": 0}
                                            if isinstance(o, dict)
                                            else o
                                            for o in product.options
                                        ]
                                    else:
                                        _snap_options = product.options
                                if _snap_options:
                                    snapshot["options"] = _snap_options
                                history = list(product.price_history or [])
                                history.insert(0, snapshot)
                                updates["price_history"] = _trim_history(history)

                                # 가격/옵션 필드는 변동 여부와 무관하게 항상 DB 반영
                                # (changed=False여도 cost만 바뀔 수 있음 → 전송 시 DB 읽으므로 필수)
                                if r.new_sale_price is not None:
                                    updates["sale_price"] = r.new_sale_price
                                if r.new_original_price is not None:
                                    updates["original_price"] = r.new_original_price
                                if r.new_cost is not None:
                                    updates["cost"] = r.new_cost
                                if r.new_options is not None:
                                    updates["options"] = r.new_options
                                # 적립금 사용 제한 여부 (무신사 등) — 오토튠에서 함께 갱신
                                if r.new_is_point_restricted is not None:
                                    updates["is_point_restricted"] = (
                                        r.new_is_point_restricted
                                    )
                                elif (
                                    r.new_sale_status == "sold_out" and product.options
                                ):
                                    # new_options 없지만 품절 → 기존 옵션 재고를 0으로 강제 업데이트
                                    updates["options"] = [
                                        {**o, "stock": 0} if isinstance(o, dict) else o
                                        for o in product.options
                                    ]
                                updates["sale_status"] = r.new_sale_status
                                # cost 변경도 price_changed_at에 반영 (warm/hot 분류 기준)
                                if (
                                    r.changed
                                    or r.stock_changed
                                    or (
                                        r.new_cost is not None
                                        and r.new_cost
                                        != (getattr(product, "cost", None) or 0)
                                    )
                                ):
                                    updates["price_changed_at"] = now

                                # price_changed 이벤트 수집은 아래 계정 루프의
                                # expected_price != last_price(등록마켓 판매가 기준) 지점에서 수행

                                # per-product 옵션품절/리스탁 이벤트 — 옵션별 0 경계 전환 시에만
                                # 오토튠 실전송 기준(옵션 stock이 0/양수 사이를 넘나드는 경우)과 동일
                                # 전체 품절/소싱처삭제는 아래 블록에서 별도 append
                                _direction_for_tick: str | None = None
                                if r.stock_changed and r.new_sale_status != "sold_out":

                                    def _opt_map(opts):
                                        """옵션 key(name/size) → stock 맵."""
                                        result: dict[str, int] = {}
                                        if not opts:
                                            return result
                                        for _o in opts:
                                            if not isinstance(_o, dict):
                                                continue
                                            _k = _o.get("name", "") or _o.get(
                                                "size", ""
                                            )
                                            try:
                                                result[_k] = int(_o.get("stock") or 0)
                                            except (TypeError, ValueError):
                                                result[_k] = 0
                                        return result

                                    _old_map = _opt_map(product.options)
                                    _new_map = _opt_map(r.new_options)
                                    # 0 경계를 넘은 옵션을 방향별로 분리
                                    # 양수→0: 품절 전환, 0→양수: 재입고
                                    _sold_out_keys: list[str] = []
                                    _restocked_keys: list[str] = []
                                    for _k in set(_old_map) | set(_new_map):
                                        _os = _old_map.get(_k, 0)
                                        _ns = _new_map.get(_k, 0)
                                        if (_os <= 0) != (_ns <= 0):
                                            if _ns <= 0:
                                                _sold_out_keys.append(_k)
                                            else:
                                                _restocked_keys.append(_k)
                                    # 빈 키는 안정적 라벨로 치환
                                    _sold_out_keys = [
                                        k or "(이름없음)" for k in _sold_out_keys
                                    ]
                                    _restocked_keys = [
                                        k or "(이름없음)" for k in _restocked_keys
                                    ]

                                    if _sold_out_keys and _restocked_keys:
                                        _direction_for_tick = "mixed"
                                    elif _sold_out_keys:
                                        _direction_for_tick = "sold_out"
                                    elif _restocked_keys:
                                        _direction_for_tick = "restock"

                                    # 옵션 품절(양수→0) 이벤트
                                    if _sold_out_keys:
                                        _old_stock = sum(_old_map.values())
                                        _new_stock = sum(_new_map.values())
                                        _opts_join = ", ".join(_sold_out_keys[:5])
                                        # 변동 감지 즉시 DB 저장 — 사이클 미완주에도 유실 없음
                                        _name_short = (product.name or "")[:30]
                                        await _stream_event(
                                            "sold_out",
                                            "info",
                                            summary=f"옵션품절 — {_name_short} {_opts_join}",
                                            source_site=product.source_site,
                                            product_id=r.product_id,
                                            product_name=product.name,
                                            detail={
                                                "old_stock": _old_stock,
                                                "new_stock": _new_stock,
                                                "sale_status": r.new_sale_status,
                                                "site_product_id": product.site_product_id,
                                                "reason": "option_partial",
                                                "sold_out_options": _sold_out_keys,
                                                "suspended_markets": [],
                                            },
                                        )

                                    # 옵션 재입고(0→양수) 이벤트
                                    if _restocked_keys:
                                        _name_short = (product.name or "")[:30]
                                        _opts_join = ", ".join(_restocked_keys[:5])
                                        await _stream_event(
                                            "restock",
                                            "info",
                                            summary=f"재입고(옵션리스탁) — {_name_short} {_opts_join}",
                                            source_site=product.source_site,
                                            product_id=r.product_id,
                                            product_name=product.name,
                                            detail={
                                                "sale_status": r.new_sale_status,
                                                "site_product_id": product.site_product_id,
                                                "reason": "option_restock",
                                                "restocked_options": _restocked_keys,
                                                "suspended_markets": [],
                                            },
                                        )

                                # 재고변동 항목 수집 (scheduler_tick detail용) — direction 포함
                                if r.stock_changed and len(_stock_tx_items) < 10:
                                    _stock_tx_items.append(
                                        {
                                            "pid": r.product_id,
                                            "site_product_id": product.site_product_id,
                                            "name": (product.name or "")[:40],
                                            "sale_status": r.new_sale_status,
                                            "direction": _direction_for_tick
                                            or (
                                                "sold_out"
                                                if r.new_sale_status == "sold_out"
                                                else None
                                            ),
                                        }
                                    )

                                # 품절 → 서킷브레이커 + 즉시 마켓삭제
                                if r.new_sale_status == "sold_out":
                                    # 변동 감지 즉시 DB 저장 — 사이클 미완주에도 유실 없음
                                    # suspended_markets는 마켓삭제 완료 후 _patch_event_detail로 사후 반영
                                    _soldout_reason = (
                                        "source_deleted"
                                        if getattr(r, "deleted_from_source", False)
                                        else "all_soldout"
                                    )
                                    _reason_lbl = (
                                        "소싱처삭제"
                                        if _soldout_reason == "source_deleted"
                                        else "전체품절"
                                    )
                                    _name_short = (product.name or "")[:30]
                                    _soldout_event_id = await _stream_event(
                                        "sold_out",
                                        "info",
                                        summary=f"품절({_reason_lbl}) — {_name_short}",
                                        source_site=product.source_site,
                                        product_id=r.product_id,
                                        product_name=product.name,
                                        detail={
                                            "old_stock": None,
                                            "new_stock": 0,
                                            "sale_status": "sold_out",
                                            "site_product_id": product.site_product_id,
                                            "reason": _soldout_reason,
                                            "suspended_markets": [],
                                        },
                                    )

                                    _site_consecutive_soldout[site] = (
                                        _site_consecutive_soldout.get(site, 0) + 1
                                    )
                                    if (
                                        _site_consecutive_soldout[site]
                                        >= SOLDOUT_BREAK_THRESHOLD
                                    ):
                                        _site_breaker_tripped[site] = True
                                        log.error(
                                            "[오토튠] 서킷브레이커 작동! %s 연속 %d개 품절",
                                            site,
                                            _site_consecutive_soldout[site],
                                        )
                                        await _partial_update(r.product_id, updates)
                                        return
                                    if not getattr(product, "lock_delete", False):
                                        product_dict = product.model_dump()
                                        _ok_del_ids: list[str] = []
                                        for _del_acc_id in (
                                            product.registered_accounts or []
                                        ):
                                            _del_acc = _account_cache.get(_del_acc_id)
                                            if not _del_acc:
                                                continue
                                            m_nos = product.market_product_nos or {}
                                            if _del_acc.market_type == "smartstore":
                                                pno = m_nos.get(
                                                    f"{_del_acc_id}_origin", ""
                                                )
                                                if not pno:
                                                    _raw = m_nos.get(_del_acc_id, "")
                                                    if isinstance(_raw, dict):
                                                        pno = (
                                                            _raw.get("originProductNo")
                                                            or _raw.get(
                                                                "smartstoreChannelProductNo"
                                                            )
                                                            or _raw.get(
                                                                "groupProductNo"
                                                            )
                                                            or ""
                                                        )
                                                    else:
                                                        pno = _raw
                                                pno = str(pno) if pno else ""
                                            else:
                                                pno = m_nos.get(_del_acc_id, "")
                                            pd = {
                                                **product_dict,
                                                "market_product_no": {
                                                    _del_acc.market_type: pno
                                                },
                                            }
                                            _del_label = f"{_del_acc.market_name}({_del_acc.seller_id or '-'})"
                                            try:
                                                await session.commit()
                                            except Exception:
                                                pass
                                            try:
                                                dr = await delete_from_market(
                                                    session,
                                                    _del_acc.market_type,
                                                    pd,
                                                    account=_del_acc,
                                                )
                                                if dr.get("success") and not dr.get(
                                                    "soldout_fallback"
                                                ):
                                                    deleted_count += 1
                                                    _ok_del_ids.append(_del_acc_id)
                                                    _log_line(
                                                        site,
                                                        r.product_id,
                                                        f"{_idx_prefix}{_prod_label}: 품절 → {_del_label} 마켓삭제 완료 [원가 {_cost_int:,}]",
                                                    )
                                                else:
                                                    log.warning(
                                                        "[오토튠] %s → %s 마켓삭제 실패: %s",
                                                        r.product_id,
                                                        _del_acc.market_type,
                                                        dr.get("message"),
                                                    )
                                            except Exception as e:
                                                log.error(
                                                    "[오토튠] %s → 마켓삭제 오류: %s",
                                                    r.product_id,
                                                    e,
                                                )
                                        # 삭제 성공한 계정 → 이미 발행된 품절 이벤트의 detail에 사후 반영
                                        if _ok_del_ids and _soldout_event_id:
                                            _suspended_labels = []
                                            for _acc_id in _ok_del_ids:
                                                _acc_obj = _account_cache.get(_acc_id)
                                                if _acc_obj:
                                                    _suspended_labels.append(
                                                        f"{_acc_obj.market_name}({_acc_obj.seller_id or '-'})"
                                                    )
                                            if _suspended_labels:
                                                await _patch_event_detail(
                                                    _soldout_event_id,
                                                    {
                                                        "suspended_markets": _suspended_labels,
                                                    },
                                                )
                                        # 삭제 성공한 계정 → registered_accounts/market_product_nos 정리
                                        _all_markets_deleted = False
                                        if _ok_del_ids:
                                            _cycle_deleted_pids.add(r.product_id)
                                            _orig_reg = list(
                                                product.registered_accounts or []
                                            )
                                            _orig_mnos = dict(
                                                product.market_product_nos or {}
                                            )
                                            _new_reg = [
                                                a
                                                for a in _orig_reg
                                                if a not in _ok_del_ids
                                            ]
                                            _new_mnos = {
                                                k: v
                                                for k, v in _orig_mnos.items()
                                                if not any(
                                                    k == d or k.startswith(f"{d}_")
                                                    for d in _ok_del_ids
                                                )
                                            }
                                            # 등록된 모든 마켓 삭제 성공 → 상품 자체 삭제
                                            if _orig_reg and not _new_reg:
                                                _all_markets_deleted = True
                                            else:
                                                updates["registered_accounts"] = (
                                                    _new_reg if _new_reg else []
                                                )
                                                updates["market_product_nos"] = (
                                                    _new_mnos if _new_mnos else {}
                                                )
                                    if _all_markets_deleted:
                                        try:
                                            await _partial_delete(r.product_id)
                                            _log_line(
                                                site,
                                                r.product_id,
                                                f"{_idx_prefix}{_prod_label}: 품절 전 마켓 삭제 성공 → 상품 DB 삭제 완료",
                                            )
                                        except Exception as _pd_err:
                                            log.error(
                                                "[오토튠] %s 상품 DB 삭제 실패: %s",
                                                r.product_id,
                                                _pd_err,
                                            )
                                    else:
                                        await _partial_update(r.product_id, updates)
                                    _site_consecutive_soldout[site] = 0
                                    return
                                else:
                                    _site_consecutive_soldout[site] = 0

                                # 소싱처 보조 API 부분실패 → 가격 데이터 불확실
                                if getattr(r, "price_uncertain", False):
                                    updates.pop("cost", None)
                                    snapshot["price_uncertain"] = True
                                    log.warning(
                                        "[오토튠][가격불확실] %s: "
                                        "API 부분실패 → 가격갱신/전송 보류 "
                                        "(수집원가=%s, DB원가=%s)",
                                        _prod_label,
                                        _cost_int,
                                        int(product.cost or 0),
                                    )
                                    await _partial_update(r.product_id, updates)
                                    return

                                # DB 먼저 업데이트 (전송 전에 최신 데이터 반영)
                                await _partial_update(r.product_id, updates)

                                # ★ 마켓별 최종 판매가 비교 → 전송 판정
                                new_cost = _cur_cost
                                reg_accounts = product.registered_accounts or []
                                # 판매처 필터 적용 (market_type 기준)
                                if _market_filter_active:
                                    reg_accounts = [
                                        a
                                        for a in reg_accounts
                                        if (
                                            _account_cache.get(a)
                                            and getattr(
                                                _account_cache[a], "market_type", ""
                                            )
                                            in _enabled_markets
                                        )
                                    ]
                                last_sent = product.last_sent_data or {}

                                policy = (
                                    _policy_cache.get(product.applied_policy_id)
                                    if product.applied_policy_id
                                    else None
                                )

                                _tx_actions: list[
                                    str
                                ] = []  # 전송 예정 액션 (_fire_transmit에서 결과와 함께 출력)
                                _nontx_actions: list[
                                    str
                                ] = []  # 비전송 액션 (즉시 출력)
                                _transmit_queue: list[
                                    tuple
                                ] = []  # (pid, items, acc_id, label, action_text)

                                for acc_id in reg_accounts:
                                    acc = _account_cache.get(acc_id)
                                    if not acc:
                                        continue
                                    # market_product_nos에 상품번호가 없는 계정은 스킵
                                    # (등록된 적 없는 계정에 신규 등록 시도하는 것은 오토튠 역할 아님)
                                    _m_nos = product.market_product_nos or {}
                                    _has_pno = bool(
                                        _m_nos.get(f"{acc_id}_origin")
                                        or _m_nos.get(acc_id)
                                    )
                                    if not _has_pno:
                                        continue
                                    acc_label = (
                                        f"{acc.market_name}({acc.seller_id or '-'})"
                                    )
                                    market_type = acc.market_type or ""

                                    # 롯데홈쇼핑 MD 승인 인라인 체크는 전송 시점으로 이동
                                    # — 갱신은 그냥 진행, 변동 감지되어 전송 큐에 들어가기 직전에만 QA 확인
                                    # (모든 lottehome 상품마다 외부 API를 부르던 비용 99% 감소)

                                    if policy and policy.pricing:
                                        cost_info = await convert_cost_by_source_site(
                                            session,
                                            new_cost,
                                            site,
                                            getattr(product, "tenant_id", None),
                                        )
                                        expected_price = calc_market_price(
                                            cost_info["convertedCost"],
                                            policy.pricing,
                                            market_type,
                                            policy.market_policies,
                                            source_site=site,
                                            is_point_restricted=getattr(
                                                product,
                                                "is_point_restricted",
                                                None,
                                            ),
                                        )
                                    else:
                                        expected_price = int(new_cost)

                                    acc_last = last_sent.get(acc_id, {})
                                    last_price = (
                                        (int(acc_last.get("sale_price", 0)) // 100)
                                        * 100
                                        if acc_last
                                        else 0
                                    )

                                    # ── LOTTEON slPrc 재동기화 훅 ───────────────────
                                    # 과거 update_price 페이로드 스펙 오류(INVALID_INPUT)로
                                    # last_sent_data.sale_price와 실제 LOTTEON slPrc가 불일치
                                    # 가능성. 미검증 상품에 한해 get_product로 실제 slPrc 조회 →
                                    # last_price 대체 → expected_price와 다르면 재전송 트리거.
                                    # 검증 후 slprc_verified_at 기록해 다음 사이클부터 스킵.
                                    # 비활성화: 환경변수 LOTTEON_VERIFY_SLPRC=false
                                    # 배치 상한: LOTTEON_VERIFY_SLPRC_BATCH (기본 50, 사이클당)
                                    if (
                                        site == "LOTTEON"
                                        and os.environ.get(
                                            "LOTTEON_VERIFY_SLPRC", "true"
                                        ).lower()
                                        != "false"
                                        and acc_last
                                        and not acc_last.get("slprc_verified_at")
                                        and _lot_verify_count < _lot_verify_cap
                                    ):
                                        _spd_no = str(
                                            (product.market_product_nos or {}).get(
                                                acc_id, ""
                                            )
                                            or ""
                                        )
                                        _api_key_verify = (
                                            (acc.additional_fields or {}).get("apiKey")
                                            or acc.api_key
                                            or ""
                                        )
                                        if _spd_no.startswith("LO") and _api_key_verify:
                                            try:
                                                from backend.domain.samba.proxy.lotteon import (
                                                    LotteonClient as _LOTClient,
                                                )

                                                _lc = _LOTClient(
                                                    api_key=_api_key_verify
                                                )
                                                await _lc.test_auth()
                                                _pr = await _lc.get_product(_spd_no)
                                                _d = _pr.get("data", _pr)
                                                _sp = _d.get("spdLst") or [_d]
                                                if isinstance(_sp, list) and _sp:
                                                    _sp = _sp[0]
                                                _it = (
                                                    (_sp.get("itmLst") or [{}])[0]
                                                    if isinstance(_sp, dict)
                                                    else {}
                                                )
                                                _actual_slprc = int(
                                                    _it.get("slPrc") or 0
                                                )
                                                if _actual_slprc > 0:
                                                    last_price = (
                                                        _actual_slprc // 100
                                                    ) * 100
                                                await _lc.aclose()
                                                _lot_verify_count += 1
                                                # 검증 완료 플래그 기록 (일치/불일치 무관)
                                                _lsd_up = dict(
                                                    updates.get("last_sent_data")
                                                    or product.last_sent_data
                                                    or {}
                                                )
                                                _snap = dict(
                                                    _lsd_up.get(acc_id)
                                                    or acc_last
                                                    or {}
                                                )
                                                _snap["slprc_verified_at"] = (
                                                    datetime.now(
                                                        timezone.utc
                                                    ).isoformat()
                                                )
                                                _lsd_up[acc_id] = _snap
                                                updates["last_sent_data"] = _lsd_up
                                                log.info(
                                                    "[오토튠][LOTTEON][slPrc검증] "
                                                    "pid=%s acc=%s actual=%s expected=%s",
                                                    r.product_id,
                                                    acc_id[:20],
                                                    _actual_slprc,
                                                    expected_price,
                                                )
                                            except Exception as _ev:
                                                log.warning(
                                                    "[오토튠][LOTTEON][slPrc검증실패]"
                                                    " pid=%s acc=%s: %s",
                                                    r.product_id,
                                                    acc_id[:20],
                                                    str(_ev)[:100],
                                                )

                                    # 가격 변동 → 전송 예약
                                    # 스마트스토어: 300원 올림 (25% 역산 시 100원 단위 보장)
                                    import math as _m

                                    if market_type == "smartstore":
                                        expected_price = (
                                            _m.ceil(expected_price / 300) * 300
                                        )
                                    else:
                                        expected_price = (expected_price // 100) * 100

                                    # 가격 이상치 방어: 원가 < 정상가 5%이면 재전송 차단
                                    _orig_p = getattr(product, "original_price", 0) or 0
                                    _price_blocked = (
                                        _orig_p > 0
                                        and new_cost > 0
                                        and new_cost < _orig_p * 0.05
                                    )
                                    if _price_blocked:
                                        _nontx_actions.append(
                                            f"가격방어 차단 (원가 {int(new_cost):,}"
                                            f" < 정상가 {int(_orig_p):,}의 5%)"
                                        )
                                        log.error(
                                            "[오토튠][가격방어] %s: 원가 이상치 → "
                                            "재전송 차단 (원가=%s, 정상가=%s)",
                                            _prod_label,
                                            int(new_cost),
                                            int(_orig_p),
                                        )
                                    # 계정별 전송 아이템 수집 (가격+재고 합산 후 단일 전송)
                                    _acc_items: list[str] = []
                                    _acc_action_parts: list[str] = []

                                    # failed_at 마킹: 이전 cycle에서 마켓 전송 실패 → 무조건 재시도
                                    # (사용자 케이스: cost 124,000 cycle에서 fail → cost 원복 후
                                    # expected==last로 영구 스킵되는 버그 해결)
                                    _has_failed_mark = (
                                        bool(acc_last.get("failed_at"))
                                        if acc_last
                                        else False
                                    )
                                    if (
                                        expected_price != last_price or _has_failed_mark
                                    ) and not _price_blocked:
                                        price_changed_count += 1
                                        _all_price_pids.add(r.product_id)
                                        if len(_price_tx_items) < 10:
                                            _price_tx_items.append(
                                                {
                                                    "pid": r.product_id,
                                                    "site_product_id": product.site_product_id,
                                                    "name": (product.name or "")[:40],
                                                    "old_price": last_price,
                                                    "new_price": expected_price,
                                                }
                                            )
                                        _last_cost_v = (
                                            int(acc_last.get("cost", 0) or 0)
                                            if acc_last
                                            else 0
                                        )
                                        if _has_failed_mark:
                                            _reason_lbl = "(재시도)"
                                        elif (
                                            _last_cost_v > 0
                                            and int(new_cost) == _last_cost_v
                                        ):
                                            _reason_lbl = "(정책변경)"
                                        else:
                                            _reason_lbl = ""
                                        _price_action_txt = f"가격변동{_reason_lbl} {last_price:,}→{expected_price:,} → {acc_label}"
                                        _acc_items.append("price")
                                        _acc_action_parts.append(_price_action_txt)
                                        # 워룸 타임라인용 이벤트 수집 — 등록마켓 판매가 변경 기준
                                        # (오토튠의 실제 전송 트리거와 동일 기준, 상품당 1건)
                                        if r.product_id not in _price_change_events:
                                            _diff_pct = (
                                                round(
                                                    (expected_price - last_price)
                                                    / last_price
                                                    * 100,
                                                    1,
                                                )
                                                if last_price
                                                else 0
                                            )
                                            _price_change_events[r.product_id] = {
                                                "source_site": product.source_site,
                                                "product_id": r.product_id,
                                                "product_name": product.name,
                                                "site_product_id": product.site_product_id,
                                                "old_price": last_price,
                                                "new_price": expected_price,
                                                "diff_pct": _diff_pct,
                                            }
                                            # 변동 감지 즉시 DB 저장 — 사이클 미완주에도 유실 없음
                                            _name_short = (product.name or "")[:30]
                                            await _stream_event(
                                                "price_changed",
                                                "info",
                                                summary=f"가격 변동 — {_name_short} ₩{int(last_price):,}→₩{int(expected_price):,}",
                                                source_site=product.source_site,
                                                product_id=r.product_id,
                                                product_name=product.name,
                                                detail={
                                                    "old_price": last_price,
                                                    "new_price": expected_price,
                                                    "diff_pct": _diff_pct,
                                                    "site_product_id": product.site_product_id,
                                                },
                                            )
                                    elif expected_price == last_price:
                                        # 가격 동일 스킵 — 다중 마켓 디버그 로그
                                        if len(reg_accounts) > 1:
                                            _last_cost_sent = (
                                                int(acc_last.get("cost", 0) or 0)
                                                if acc_last
                                                else 0
                                            )
                                            log.info(
                                                "[오토튠][가격스킵] %s %s: "
                                                "expected=%s==last=%s, "
                                                "cost_now=%s, cost_sent=%s",
                                                _prod_label,
                                                acc_label,
                                                expected_price,
                                                last_price,
                                                int(new_cost),
                                                _last_cost_sent,
                                            )

                                    # 재고 변동 → last_sent_data 옵션 vs API 옵션 비교
                                    _sent_opts = (
                                        acc_last.get("options") if acc_last else None
                                    )
                                    _api_opts = r.new_options
                                    _stock_diff = False
                                    _stock_changes_acc = 0
                                    # 디버그: 첫 3개 상품만 로그
                                    if idx <= 3:
                                        log.info(
                                            "[재고디버그] %s api_opts=%s, sent_opts=%s, acc=%s",
                                            r.product_id,
                                            len(_api_opts) if _api_opts else _api_opts,
                                            "있음" if _sent_opts else "없음",
                                            acc_id[:20],
                                        )
                                    if _sent_opts is None and _api_opts is not None:
                                        # 기준값 없음 → 첫 1회 무조건 전송
                                        _stock_diff = True
                                        _stock_changes_acc = (
                                            len(_api_opts) if _api_opts else 0
                                        )
                                    elif _api_opts and _sent_opts:
                                        _sent_map = {
                                            (
                                                o.get("name", "") or o.get("size", "")
                                            ): o.get("stock", 0)
                                            for o in _sent_opts
                                        }
                                        for _o in _api_opts:
                                            _k = _o.get("name", "") or _o.get(
                                                "size", ""
                                            )
                                            _ss = _sent_map.get(_k, 0) or 0
                                            _ns = _o.get("stock", 0) or 0
                                            if market_type == "lottehome":
                                                # 롯데홈쇼핑: 정확한 수량 비교 (1→2도 감지)
                                                if _ss != _ns:
                                                    _stock_diff = True
                                                    _stock_changes_acc += 1
                                            elif (_ss <= 0) != (_ns <= 0):
                                                _stock_diff = True
                                                _stock_changes_acc += 1
                                    if _stock_diff:
                                        _all_stock_pids.add(r.product_id)
                                        _stock_action_txt = f"재고전송({_stock_changes_acc}건) → {acc_label}"
                                        _acc_items.append("stock")
                                        _acc_action_parts.append(_stock_action_txt)

                                    # 가격+재고 합산 단일 전송 (충돌 방지)
                                    if _acc_items:
                                        # 롯데홈쇼핑 MD 승인 대기 — 전송 직전에만 외부 API 확인
                                        # (변동이 감지된 상품 한정 → 외부 호출 폭증 방지)
                                        if market_type == "lottehome":
                                            _qa_status = _m_nos.get(f"{acc_id}_qa", "")
                                            if _qa_status == "pending":
                                                _goods_no = _m_nos.get(acc_id, "")
                                                _approved = False
                                                if _goods_no:
                                                    try:
                                                        from backend.domain.samba.proxy.lottehome import (
                                                            LotteHomeClient,
                                                        )

                                                        _creds = _lottehome_creds
                                                        _lh = LotteHomeClient(
                                                            _creds.get("userId", ""),
                                                            _creds.get("password", ""),
                                                            _creds.get("agncNo", ""),
                                                            _creds.get("env", "prod"),
                                                        )
                                                        _detail = (
                                                            await _lh.search_goods_view(
                                                                _goods_no
                                                            )
                                                        )
                                                        _d = _detail.get("data", {})
                                                        _result = _d.get("Result", _d)
                                                        _goods_info = (
                                                            _result.get(
                                                                "GoodsInfo", _result
                                                            )
                                                            if isinstance(_result, dict)
                                                            else _result
                                                        )
                                                        # API가 빈 문자열/None을 반환할 때 .get() AttributeError 가드
                                                        if not isinstance(
                                                            _goods_info, dict
                                                        ):
                                                            _sale_stat = ""
                                                            _qa_rslt = ""
                                                        else:
                                                            _sale_stat = str(
                                                                _goods_info.get(
                                                                    "SaleStatCd", ""
                                                                )
                                                                or ""
                                                            )
                                                            _qa_rslt = str(
                                                                _goods_info.get(
                                                                    "QaRsltCd", ""
                                                                )
                                                                or ""
                                                            )
                                                        if (
                                                            _sale_stat == "10"
                                                            or _qa_rslt
                                                            in ("10", "15", "30")
                                                        ):
                                                            _approved = True
                                                            _new_nos = dict(_m_nos)
                                                            _new_nos[f"{acc_id}_qa"] = (
                                                                "approved"
                                                            )
                                                            from sqlalchemy import (
                                                                update as _sa_upd,
                                                            )
                                                            from backend.domain.samba.collector.model import (
                                                                SambaCollectedProduct as _CP,
                                                            )

                                                            async with (
                                                                get_write_session() as _upd_s
                                                            ):
                                                                await _upd_s.execute(
                                                                    _sa_upd(_CP)
                                                                    .where(
                                                                        _CP.id
                                                                        == product.id
                                                                    )
                                                                    .values(
                                                                        market_product_nos=_new_nos
                                                                    )
                                                                )
                                                                await _upd_s.commit()
                                                            log.info(
                                                                "[오토튠] %s 롯데홈쇼핑 승인 확인 → approved 처리",
                                                                product.id,
                                                            )
                                                    except Exception as _qa_e:
                                                        log.warning(
                                                            "[오토튠] %s 롯데홈쇼핑 QA 확인 실패: %s",
                                                            product.id,
                                                            _qa_e,
                                                        )
                                                if not _approved:
                                                    log.info(
                                                        "[오토튠] %s → 롯데홈쇼핑 MD 승인 대기 중, 전송 스킵",
                                                        product.id,
                                                    )
                                                    continue

                                        retransmitted += 1
                                        _combined_action_txt = " + ".join(
                                            _acc_action_parts
                                        )
                                        _tx_actions.append(_combined_action_txt)
                                        _transmit_queue.append(
                                            (
                                                r.product_id,
                                                _acc_items,
                                                acc_id,
                                                f"{_prod_label}",
                                                _combined_action_txt,
                                            )
                                        )

                                # 통합 한 줄 로그 (전송 전에 즉시 출력)
                                # 원가 변동: 마지막 전송 시 원가 vs 현재 원가 비교
                                _prev_costs = [
                                    int(last_sent.get(a, {}).get("cost", 0) or 0)
                                    for a in reg_accounts
                                    if last_sent.get(a)
                                ]
                                _prev_cost = (
                                    _prev_costs[0] if _prev_costs else _cost_int
                                )
                                if _prev_cost != _cost_int:
                                    _cost_str = f"원가변동 {_prev_cost:,}→{_cost_int:,}"
                                else:
                                    _cost_str = f"원가 {_cost_int:,}"
                                _sc = "Y" if r.product_id in _all_stock_pids else "0"
                                _tail = f" [{_cost_str}, 재고변동 {_sc}]"
                                # 비전송 액션(가격방어 차단 등)은 즉시 출력
                                # 전송 예정 액션은 _fire_transmit에서 결과와 함께 출력
                                if _nontx_actions:
                                    _log_line(
                                        site,
                                        r.product_id,
                                        f"{_idx_prefix}{_prod_label}: {' | '.join(_nontx_actions)}{_tail}",
                                    )
                                elif not _tx_actions:
                                    # 전송 예정 액션도 없으면 스킵
                                    _log_line(
                                        site,
                                        r.product_id,
                                        f"{_idx_prefix}{_prod_label}: 스킵{_tail}",
                                    )

                            # lock 밖: 즉시 전송 (사이클 내 완료 보장 → last_sent_data.cost 정확성)
                            # 같은 items 조합의 계정들을 묶어 한 번의 start_update로 호출
                            # → service.start_update 내부 asyncio.gather가 계정별 동시 전송 (account 단위 세마포어로 안전)
                            _tx_groups: dict[tuple, list[tuple]] = {}
                            for (
                                _tx_pid,
                                _tx_items,
                                _tx_acc,
                                _tx_label,
                                _tx_action_text,
                            ) in _transmit_queue:
                                _items_key = tuple(sorted(_tx_items))
                                _tx_groups.setdefault(_items_key, []).append(
                                    (_tx_pid, _tx_acc, _tx_label, _tx_action_text)
                                )

                            for _items_key, _entries in _tx_groups.items():
                                _items_list = list(_items_key)
                                _gpid = _entries[0][0]  # 사이클 내 동일 pid
                                _accs = [e[1] for e in _entries]

                                async def _fire_transmit_group(
                                    _pid=_gpid,
                                    _items=_items_list,
                                    _accs_list=_accs,
                                    _entries_list=_entries,
                                    _site=site,
                                    _idx_pfx=_idx_prefix,
                                    _t=_tail,
                                ):
                                    nonlocal _synced_count
                                    # 세마포어를 여기서 획득하면 안 됨
                                    # — start_update → _dispatch_one 내부에서 계정별 세마포어를
                                    #   다시 획득하므로 데드락 발생 (Semaphore(1) 비재진입)
                                    #
                                    # 세션-수명 설계: start_update는 내부에서 마켓 HTTP를
                                    # 호출하므로 트랜잭션이 90~180s 가까이 idle 상태가 될 수
                                    # 있다. 좀비 connection으로 깨지면 (Can't reconnect /
                                    # InvalidRequestError 계열) 새 세션을 받아 1회 재시도한다.
                                    try:
                                        _tx_result = None
                                        _tx_exc: Exception | None = None
                                        for _tx_attempt in range(2):
                                            try:
                                                async with get_write_session() as _tx_s:
                                                    from backend.domain.samba.shipment.repository import (
                                                        SambaShipmentRepository as _FRepo,
                                                    )
                                                    from backend.domain.samba.shipment.service import (
                                                        SambaShipmentService as _FSvc,
                                                    )

                                                    _svc = _FSvc(_FRepo(_tx_s), _tx_s)
                                                    _tx_result = (
                                                        await _svc.start_update(
                                                            [_pid],
                                                            _items,
                                                            _accs_list,
                                                            skip_unchanged=False,
                                                            skip_refresh=True,
                                                        )
                                                    )
                                                    # HTTP 끝났으면 즉시 commit — 세션이 idle
                                                    # 상태로 더 머무르지 않도록 transaction 종료
                                                    await _tx_s.commit()
                                                _tx_exc = None
                                                break
                                            except Exception as _try_exc:
                                                _tx_exc = _try_exc
                                                if (
                                                    _is_stale_conn_error(_try_exc)
                                                    and _tx_attempt == 0
                                                ):
                                                    log.warning(
                                                        "[오토튠][DB재시도] transmit_group "
                                                        "pid=%s 좀비 connection 감지 → "
                                                        "새 세션으로 재시도: %s",
                                                        _pid,
                                                        str(_try_exc)[:120],
                                                    )
                                                    await asyncio.sleep(0.2)
                                                    continue
                                                raise
                                        if _tx_exc:
                                            raise _tx_exc
                                        if _tx_result is None:
                                            _tx_result = {"results": []}
                                        # 결과 검증: start_update는 실패 시 예외 없이 dict로 반환
                                        # 결과 구조: results[0] = {product_id, status, transmit_result: {acc: status}, transmit_error: {acc: err}, update_result: {acc: ...}}
                                        _tx_res_list = _tx_result.get("results", [])
                                        _tx_row = next(
                                            (
                                                r
                                                for r in _tx_res_list
                                                if isinstance(r, dict)
                                            ),
                                            None,
                                        )
                                        _tx_status_map = (
                                            _tx_row.get("transmit_result", {})
                                            if _tx_row
                                            else {}
                                        )
                                        _tx_err_map = (
                                            _tx_row.get("transmit_error", {})
                                            if _tx_row
                                            else {}
                                        )
                                        _tx_update_map = (
                                            _tx_row.get("update_result", {})
                                            if _tx_row
                                            else {}
                                        )
                                        # 그룹 전체 ok 판정 (기존 호환성: row.status로 판정)
                                        _row_status = (
                                            _tx_row.get("status") if _tx_row else None
                                        )
                                        _group_ok = _row_status in (
                                            "success",
                                            "completed",
                                        )

                                        for _entry in _entries_list:
                                            _, _eacc, _elabel, _eaction = _entry
                                            # 계정별 결과 추출
                                            _acc_status = (
                                                _tx_status_map.get(_eacc)
                                                if isinstance(_tx_status_map, dict)
                                                else None
                                            )
                                            _acc_err = (
                                                _tx_err_map.get(_eacc)
                                                if isinstance(_tx_err_map, dict)
                                                else None
                                            )
                                            # 마켓삭제 판정: update_result 내 값 확인
                                            _acc_was_deleted = False
                                            if isinstance(_tx_update_map, dict):
                                                _u = _tx_update_map.get(_eacc)
                                                if isinstance(_u, dict):
                                                    _acc_was_deleted = any(
                                                        v
                                                        in (
                                                            "deleted",
                                                            "soldout_fallback",
                                                        )
                                                        for v in _u.values()
                                                    )
                                                elif _u in (
                                                    "deleted",
                                                    "soldout_fallback",
                                                ):
                                                    _acc_was_deleted = True
                                            # 계정별 ok: status가 명시적으로 실패 아니고 에러 없음
                                            _acc_ok = not _acc_err and (
                                                _acc_status
                                                in (None, "success", "completed")
                                                or _group_ok
                                            )
                                            if _acc_ok:
                                                _synced_count += 1
                                                if _acc_was_deleted:
                                                    _log_line(
                                                        _site,
                                                        _pid,
                                                        f"{_idx_pfx}{_elabel}: {_eaction} → 마켓삭제(품절){_t}",
                                                    )
                                                else:
                                                    _log_line(
                                                        _site,
                                                        _pid,
                                                        f"{_idx_pfx}{_elabel}: {_eaction} 전송완료{_t}",
                                                    )
                                            else:
                                                _fail_msg = (
                                                    str(_acc_err)[:200]
                                                    if _acc_err
                                                    else "결과없음"
                                                )
                                                _log_line(
                                                    _site,
                                                    _pid,
                                                    f"{_idx_pfx}{_elabel}: {_eaction} 전송실패(검증): {_fail_msg}{_t}",
                                                    "error",
                                                )
                                    except Exception as _fe:
                                        for _entry in _entries_list:
                                            _, _eacc, _elabel, _eaction = _entry
                                            _log_line(
                                                _site,
                                                _pid,
                                                f"{_idx_pfx}{_elabel}: {_eaction} 전송실패: {str(_fe)[:200]}{_t}",
                                                "error",
                                            )
                                    await asyncio.sleep(0.3)

                                # 전송을 fire-and-forget으로 띄움 — 갱신은 즉시 다음 상품으로 진행.
                                # 세마포어로 동시 transmit 수 제한해 OOM 방지.
                                # 정책 변경 직후 폭주(수천 건)에서도 refresher가 await에 막혀
                                # throughput이 1/min으로 떨어지던 문제 해결.
                                asyncio.create_task(
                                    _run_transmit_in_background(_fire_transmit_group)
                                )

                        # ③ 소싱처별 병렬 갱신 + 결과 즉시 처리 (콜백)
                        from backend.domain.samba.collector.refresher import (
                            SITE_AUTOTUNE_CONCURRENCY as _SAC,
                        )

                        async def _on_result_releasing(product, r, idx=0, total=0):
                            await _on_result(product, r, idx, total)

                        log.info(
                            "[오토튠][디버그][%s][%s] 사이클 #%d bulk 시작 "
                            "(concurrency=%s, elapsed=%.1fs)",
                            device_id[:8],
                            site,
                            _cycle_seq,
                            dict(_SAC).get(site, "default"),
                            time.time() - _cycle_started_ts,
                        )
                        _bulk_start_ts = time.time()
                        results, summary = await refresh_products_bulk(
                            products,
                            max_concurrency=dict(_SAC),
                            on_result=_on_result_releasing,
                        )
                        log.info(
                            "[오토튠][디버그][%s][%s] 사이클 #%d bulk 종료: "
                            "results=%d/%d (refreshed=%d, errors=%d) bulk_elapsed=%.1fs, cycle_elapsed=%.1fs",
                            device_id[:8],
                            site,
                            _cycle_seq,
                            len(results),
                            filtered_count,
                            summary.refreshed,
                            summary.errors,
                            time.time() - _bulk_start_ts,
                            time.time() - _cycle_started_ts,
                        )

                        # 에러 결과 후처리 (콜백에서 처리 안 된 에러 건)
                        for r in results:
                            if r.error and r.error != "cancelled":
                                _ep = product_map.get(r.product_id)
                                if _ep:
                                    try:
                                        await repo.update_async(
                                            r.product_id,
                                            refresh_error_count=(
                                                _ep.refresh_error_count or 0
                                            )
                                            + 1,
                                            last_refreshed_at=now,
                                        )
                                    except Exception:
                                        pass
                        # 에러 핸들링 쓰기 커밋 — 다음 블록 진입 전 트랜잭션 종료
                        try:
                            await session.commit()
                        except Exception:
                            pass

                        # ④ 즉시전송으로 전환 — _pending_syncs 일괄 처리 제거됨

                        # 사이클 완료 로그 — 에러 유형별 분류
                        _err_count = sum(1 for r in results if r.error)
                        _ok_count = len(results) - _err_count
                        _no_pid_count = sum(
                            1
                            for r in results
                            if r.error and "site_product_id" in r.error
                        )
                        _blocked_count = sum(
                            1 for r in results if r.error and "차단" in r.error
                        )
                        _timeout_count = sum(
                            1
                            for r in results
                            if r.error
                            and ("타임아웃" in r.error or "Timeout" in r.error)
                        )
                        _other_err = (
                            _err_count - _no_pid_count - _blocked_count - _timeout_count
                        )
                        _now = datetime.now(timezone.utc)
                        _kst = _now + timedelta(hours=9)
                        # 에러 상세 문자열 구성
                        _err_parts = []
                        if _no_pid_count:
                            _err_parts.append(f"ID없음 {_no_pid_count:,}")
                        if _blocked_count:
                            _err_parts.append(f"차단 {_blocked_count:,}")
                        if _timeout_count:
                            _err_parts.append(f"타임아웃 {_timeout_count:,}")
                        if _other_err > 0:
                            _err_parts.append(f"기타 {_other_err:,}")
                        _err_detail = (
                            f" ({', '.join(_err_parts)})" if _err_parts else ""
                        )
                        # ── 사이클 누적 통계 합산 ──
                        _cstats = _autotune_cycle_stats.setdefault(
                            _gkey, _new_cycle_stats()
                        )
                        if _cstats.get("started_at") is None:
                            _cstats["started_at"] = now.isoformat()
                        _cstats["ok"] += _ok_count
                        _cstats["err"] += _err_count
                        _cstats["no_pid"] += _no_pid_count
                        _cstats["blocked"] += _blocked_count
                        _cstats["timeout"] += _timeout_count
                        _cstats["other"] += _other_err
                        _cstats["total"] += len(results)
                        _cstats["price_pids"].update(_all_price_pids)
                        _cstats["stock_pids"].update(_all_stock_pids)
                        _cstats["synced"] += _synced_count
                        _cstats["deleted"] += deleted_count
                        _cstats["batches"] += 1

                        _g_idx_now = _autotune_global_idx.get(_gkey, 0)
                        _g_total_now = _autotune_global_total.get(_gkey, 0)
                        _is_full_cycle = _g_total_now > 0 and _g_idx_now >= _g_total_now

                        # 배치 완료 로그 (매 배치마다)
                        _ref_mod._refresh_log_buffer.append(
                            {
                                "ts": _now.isoformat(),
                                "site": site,
                                "product_id": "",
                                "name": "",
                                "msg": f"[{_kst.strftime('%H:%M:%S')}] -- [{site}] 배치 완료 [{_g_idx_now:,}/{_g_total_now:,}]: {_ok_count:,}건 성공, {_err_count:,}건 실패{_err_detail} / 총 {len(results):,}건, 가격전송 {len(_all_price_pids):,}건, 재고전송 {len(_all_stock_pids):,}건, 동기 {_synced_count:,}건, 마켓삭제 {deleted_count:,}건 --",
                                "level": "info",
                                "source": "autotune",
                            }
                        )
                        _ref_mod._refresh_log_total += 1
                        log.info(
                            "[오토튠] 배치 완료 [%s/%s]: %d성공, %d실패%s / %d건",
                            f"{_g_idx_now:,}",
                            f"{_g_total_now:,}",
                            _ok_count,
                            _err_count,
                            _err_detail,
                            len(results),
                        )

                        # 사이클(전체 1바퀴) 완료 로그
                        if _is_full_cycle:
                            _c_err_parts = []
                            if _cstats["no_pid"]:
                                _c_err_parts.append(f"ID없음 {_cstats['no_pid']:,}")
                            if _cstats["blocked"]:
                                _c_err_parts.append(f"차단 {_cstats['blocked']:,}")
                            if _cstats["timeout"]:
                                _c_err_parts.append(f"타임아웃 {_cstats['timeout']:,}")
                            if _cstats["other"] > 0:
                                _c_err_parts.append(f"기타 {_cstats['other']:,}")
                            _c_err_detail = (
                                f" ({', '.join(_c_err_parts)})" if _c_err_parts else ""
                            )
                            _c_started = _cstats.get("started_at")
                            _c_dur_str = ""
                            try:
                                if _c_started:
                                    _c_dur = (
                                        _now - datetime.fromisoformat(_c_started)
                                    ).total_seconds()
                                    _c_dur_str = f", 소요 {int(_c_dur):,}초"
                            except Exception:
                                pass
                            _ref_mod._refresh_log_buffer.append(
                                {
                                    "ts": _now.isoformat(),
                                    "site": site,
                                    "product_id": "",
                                    "name": "",
                                    "msg": f"[{_kst.strftime('%H:%M:%S')}] ══ [{site}] 사이클 완료 (1바퀴 {_g_total_now:,}건): {_cstats['ok']:,}건 성공, {_cstats['err']:,}건 실패{_c_err_detail} / 배치 {_cstats['batches']:,}회, 가격전송 {len(_cstats['price_pids']):,}건, 재고전송 {len(_cstats['stock_pids']):,}건, 동기 {_cstats['synced']:,}건, 마켓삭제 {_cstats['deleted']:,}건{_c_dur_str} ══",
                                    "level": "info",
                                    "source": "autotune",
                                }
                            )
                            _ref_mod._refresh_log_total += 1
                            log.info(
                                "[오토튠][%s] 사이클 완료 (1바퀴 %s건): %d성공, %d실패%s / 배치 %d회",
                                site,
                                f"{_g_total_now:,}",
                                _cstats["ok"],
                                _cstats["err"],
                                _c_err_detail,
                                _cstats["batches"],
                            )
                            # 다음 회전을 위해 통계 리셋
                            _autotune_cycle_stats[_gkey] = _new_cycle_stats()

                        # ★ 품절 잔존 상품 마켓삭제 재시도
                        # sale_status="sold_out"인데 registered_accounts가 남아있는 상품
                        try:
                            _soldout_where = [
                                *market_cond,
                                _CP.sale_status == "sold_out",
                                _CP.lock_delete != True,
                                _CP.source_site == site,
                            ]
                            # 사이클 중 이미 삭제된 상품 제외
                            if _cycle_deleted_pids:
                                _soldout_where.append(
                                    _CP.id.not_in(list(_cycle_deleted_pids))
                                )
                            _soldout_retry_stmt = (
                                select(_CP).where(*_soldout_where).limit(50)
                            )
                            _soldout_result = await session.exec(_soldout_retry_stmt)
                            _soldout_products = _soldout_result.all()

                            if _soldout_products:
                                log.info(
                                    "[오토튠] 품절 잔존 마켓삭제 재시도: %d건",
                                    len(_soldout_products),
                                )
                                # 재시도용 계정 캐시 보충
                                _retry_acc_ids: set[str] = set()
                                for _sp in _soldout_products:
                                    if _sp.registered_accounts:
                                        _retry_acc_ids.update(_sp.registered_accounts)
                                _missing_acc_ids = _retry_acc_ids - set(
                                    _account_cache.keys()
                                )
                                if _missing_acc_ids:
                                    from backend.domain.samba.account.model import (
                                        SambaMarketAccount,
                                    )

                                    _retry_acc_stmt = select(SambaMarketAccount).where(
                                        SambaMarketAccount.id.in_(
                                            list(_missing_acc_ids)
                                        )
                                    )
                                    _retry_acc_result = await session.exec(
                                        _retry_acc_stmt
                                    )
                                    for _ra in _retry_acc_result.all():
                                        _account_cache[_ra.id] = _ra

                                # 읽기 완료 후 커밋 — delete_from_market HTTP 호출 중 idle in transaction 방지
                                try:
                                    await session.commit()
                                except Exception:
                                    pass

                                for _sp in _soldout_products:
                                    _sp_dict = _sp.model_dump()
                                    _sp_reg = list(_sp.registered_accounts or [])
                                    _sp_mnos = dict(_sp.market_product_nos or {})
                                    _sp_deleted_ids: list[str] = []

                                    for _del_acc_id in _sp_reg:
                                        _del_acc = _account_cache.get(_del_acc_id)
                                        if not _del_acc:
                                            continue
                                        _m_nos = _sp.market_product_nos or {}
                                        if _del_acc.market_type == "smartstore":
                                            _pno = _m_nos.get(
                                                f"{_del_acc_id}_origin", ""
                                            )
                                            if not _pno:
                                                _raw2 = _m_nos.get(_del_acc_id, "")
                                                if isinstance(_raw2, dict):
                                                    _pno = (
                                                        _raw2.get("originProductNo")
                                                        or _raw2.get(
                                                            "smartstoreChannelProductNo"
                                                        )
                                                        or _raw2.get("groupProductNo")
                                                        or ""
                                                    )
                                                else:
                                                    _pno = _raw2
                                            _pno = str(_pno) if _pno else ""
                                        else:
                                            _pno = _m_nos.get(_del_acc_id, "")
                                        _pd = {
                                            **_sp_dict,
                                            "market_product_no": {
                                                _del_acc.market_type: _pno
                                            },
                                        }
                                        _del_label = f"{_del_acc.market_name}({_del_acc.seller_id or '-'})"
                                        try:
                                            _dr = await delete_from_market(
                                                session,
                                                _del_acc.market_type,
                                                _pd,
                                                account=_del_acc,
                                            )
                                            if _dr.get("success") and not _dr.get(
                                                "soldout_fallback"
                                            ):
                                                deleted_count += 1
                                                _sp_deleted_ids.append(_del_acc_id)
                                                _log_line(
                                                    _sp.source_site or "",
                                                    _sp.id,
                                                    f"{_sp.name or _sp.id}: 품절잔존 → {_del_label} 마켓삭제 완료",
                                                )
                                            else:
                                                log.warning(
                                                    "[오토튠] 품절잔존 %s → %s 마켓삭제 실패: %s",
                                                    _sp.id,
                                                    _del_acc.market_type,
                                                    _dr.get("message"),
                                                )
                                        except Exception as _del_err:
                                            log.error(
                                                "[오토튠] 품절잔존 %s → 마켓삭제 오류: %s",
                                                _sp.id,
                                                _del_err,
                                            )

                                    # 삭제 성공한 계정 정리
                                    if _sp_deleted_ids:
                                        _new_reg = [
                                            a
                                            for a in _sp_reg
                                            if a not in _sp_deleted_ids
                                        ]
                                        _new_mnos = {
                                            k: v
                                            for k, v in _sp_mnos.items()
                                            if not any(
                                                k == did or k.startswith(f"{did}_")
                                                for did in _sp_deleted_ids
                                            )
                                        }
                                        # 등록된 모든 마켓 삭제 성공 → 상품 자체 삭제
                                        if _sp_reg and not _new_reg:
                                            try:
                                                await repo.delete_async(_sp.id)
                                                _log_line(
                                                    _sp.source_site or "",
                                                    _sp.id,
                                                    f"{_sp.name or _sp.id}: 품절잔존 전 마켓 삭제 성공 → 상품 DB 삭제 완료",
                                                )
                                            except Exception as _pd_err:
                                                log.error(
                                                    "[오토튠] 품절잔존 %s 상품 DB 삭제 실패: %s",
                                                    _sp.id,
                                                    _pd_err,
                                                )
                                        else:
                                            _cleanup: dict = {
                                                "registered_accounts": _new_reg
                                                if _new_reg
                                                else [],
                                                "market_product_nos": _new_mnos
                                                if _new_mnos
                                                else {},
                                            }
                                            await repo.update_async(_sp.id, **_cleanup)

                                try:
                                    await asyncio.wait_for(session.commit(), timeout=30)
                                    # 쓰기 완료 즉시 커넥션 반납 — 사이클 대기 중 풀 점유 방지
                                    await session.close()
                                except Exception as _retry_commit_err:
                                    log.error(
                                        "[오토튠] 품절잔존 commit 실패: %s",
                                        _retry_commit_err,
                                    )
                                    try:
                                        await asyncio.wait_for(
                                            session.rollback(), timeout=10
                                        )
                                    except Exception:
                                        pass
                        except Exception as _retry_err:
                            log.error(
                                "[오토튠] 품절잔존 재시도 오류: %s",
                                _retry_err,
                                exc_info=True,
                            )

                        # 이벤트 발행 (별도 세션)
                        _ended = datetime.now(timezone.utc)
                        _duration_sec = round((_ended - now).total_seconds(), 1)
                        _rate = (
                            round(filtered_count / _duration_sec, 1)
                            if _duration_sec > 0
                            else 0
                        )
                        # per-product price_changed / sold_out 이벤트는
                        # 감지 즉시 _stream_event로 DB에 이미 저장됨 (유실 방지)
                        # 여기서는 사이클 완료 요약 tick만 발행한다.
                        try:
                            async with get_write_session() as ev_session:
                                monitor = SambaMonitorService(ev_session)
                                await monitor.emit(
                                    "scheduler_tick",
                                    "info",
                                    summary=f"오토튠[{site}] — 대상 {filtered_count:,}건, 갱신 {summary.refreshed:,}건 (성공 {_ok_count:,}, 실패 {_err_count:,}{_err_detail}) | {_duration_sec:,}초, {_rate:,}건/초",
                                    source_site=site,
                                    detail={
                                        "total": filtered_count,
                                        "refreshed": summary.refreshed,
                                        "ok": _ok_count,
                                        "errors": _err_count,
                                        "no_pid": _no_pid_count,
                                        "blocked": _blocked_count,
                                        "timeouts": _timeout_count,
                                        "other_errors": _other_err,
                                        "price_transmit": len(_all_price_pids),
                                        "price_changed_items": _price_tx_items,
                                        "stock_transmit": len(_all_stock_pids),
                                        "stock_changed_items": _stock_tx_items,
                                        "sold_out": summary.sold_out,
                                        "retransmitted": retransmitted,
                                        "synced": _synced_count,
                                        "deleted": deleted_count,
                                        "started_at": now.isoformat(),
                                        "ended_at": _ended.isoformat(),
                                        "duration_sec": _duration_sec,
                                        "rate": _rate,
                                    },
                                )
                                await ev_session.commit()
                            log.info(
                                "[오토튠] 이벤트 발행 완료 (%s초, %s건/초)",
                                _duration_sec,
                                _rate,
                            )
                        except Exception as ev_err:
                            log.error("[오토튠] 이벤트 발행 실패: %s", ev_err)

                        # commit
                        try:
                            await asyncio.wait_for(session.commit(), timeout=30)
                        except (asyncio.TimeoutError, Exception) as commit_err:
                            log.error(
                                "[오토튠] 결과 commit 실패 (무시하고 진행): %s",
                                commit_err,
                            )
                            _ref_mod._refresh_log_buffer.append(
                                {
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                    "site": "",
                                    "product_id": "",
                                    "name": "",
                                    "msg": f"[{(datetime.now(timezone.utc) + timedelta(hours=9)).strftime('%H:%M:%S')}] 결과 commit 실패: {type(commit_err).__name__}: {str(commit_err)[:100]}",
                                    "level": "error",
                                    "source": "autotune",
                                }
                            )
                            _ref_mod._refresh_log_total += 1
                            try:
                                await asyncio.wait_for(session.rollback(), timeout=10)
                            except Exception:
                                pass

                        log.info(
                            "[오토튠] tick 완료: 대상 %d, 갱신 %d, 가격전송 %d, 재고전송 %d, 동기 %d, 삭제 %d",
                            filtered_count,
                            summary.refreshed,
                            len(_all_price_pids),
                            len(_all_stock_pids),
                            _synced_count,
                            deleted_count,
                        )
                    else:
                        _seh = _pc_seh(device_id)
                        _seh[site] = _seh.get(site, 0) + 1
                        if _seh[site] >= SITE_EMPTY_SKIP_THRESHOLD:
                            _site_empty_skip_until[site] = time.time() + 60
                            log.info(
                                "[오토튠][%s] 대상 상품 없음 (%d회 연속) — 60초 제외",
                                site,
                                _seh[site],
                            )
                            _seh[site] = 0
                        else:
                            log.info("[오토튠][%s] 대상 상품 없음 — 루프 종료", site)
                        break

                    _pc_seh(device_id)[site] = 0  # 정상 사이클 → 카운터 리셋
                    _scc = _pc_scc(device_id)
                    _scc[site] = _scc.get(site, 0) + 1
                    _pc_slt(device_id)[site] = now.isoformat()
                    log.info(
                        "[오토튠][%s] 배치 완료 (누적 %d회) — 즉시 재시작",
                        site,
                        _scc.get(site, 0),
                    )

            except asyncio.CancelledError:
                log.warning(
                    "[오토튠][디버그][%s][%s] 사이클 #%d CancelledError 진입 "
                    "(cycle_elapsed=%.1fs, pc_running=%s)",
                    device_id[:8],
                    site,
                    _cycle_seq,
                    time.time() - _cycle_started_ts,
                    _is_pc_running(device_id),
                    exc_info=True,
                )
                if not _is_pc_running(device_id):
                    log.info("[오토튠][%s] 루프 취소됨 (정상 종료)", site)
                    break
                # Watchdog에 의해 site_tasks에서 제거된 경우 → 좀비 루프 방지 종료
                _my_task = _pc_st(device_id).get(site)
                if _my_task is not asyncio.current_task():
                    log.info(
                        "[오토튠][%s] Watchdog에 의해 교체됨 — 좀비 루프 방지 종료",
                        site,
                    )
                    break
                try:
                    import backend.domain.samba.collector.refresher as _ref_cancel

                    _now_cancel = datetime.now(timezone.utc)
                    _kst_cancel = _now_cancel + timedelta(hours=9)
                    _ref_cancel._refresh_log_buffer.append(
                        {
                            "ts": _now_cancel.isoformat(),
                            "site": site,
                            "product_id": "",
                            "name": "",
                            "msg": f"[{_kst_cancel.strftime('%H:%M:%S')}] !! [{site}] CancelledError — 사이클 재시작",
                            "level": "error",
                            "source": "autotune",
                        }
                    )
                    _ref_cancel._refresh_log_total += 1
                except Exception:
                    pass
                await asyncio.sleep(2)
            except Exception as e:
                log.error(
                    "[오토튠][디버그][%s][%s] 사이클 #%d tick 오류 "
                    "(cycle_elapsed=%.1fs): %s",
                    device_id[:8],
                    site,
                    _cycle_seq,
                    time.time() - _cycle_started_ts,
                    e,
                    exc_info=True,
                )
                try:
                    import backend.domain.samba.collector.refresher as _ref_err

                    _now_err = datetime.now(timezone.utc)
                    _kst_err = _now_err + timedelta(hours=9)
                    _ref_err._refresh_log_buffer.append(
                        {
                            "ts": _now_err.isoformat(),
                            "site": site,
                            "product_id": "",
                            "name": "",
                            "msg": f"[{_kst_err.strftime('%H:%M:%S')}] !! [{site}] tick 오류: {type(e).__name__}: {str(e)[:100]}",
                            "level": "error",
                            "source": "autotune",
                        }
                    )
                    _ref_err._refresh_log_total += 1
                except Exception:
                    pass
                await asyncio.sleep(2)

    finally:
        try:
            current_pc_owner.reset(_owner_token)
        except Exception:
            pass
        log.info("[오토튠][%s][%s] 소싱처 루프 종료", device_id[:8], site)


async def _autotune_loop(device_id: str):
    """오토튠 코디네이터 (PC별) — 이 PC가 처리할 소싱처 루프만 생성/관리.

    이 PC의 _pc_allowed_sites 등록값을 active_sites로 사용.
    소싱처 루프는 _site_autotune_loop(device_id, site)로 실행되며
    발행하는 모든 잡에 owner_device_id=이 PC가 박혀서 다른 PC가 가로채지 못함.
    """
    log = logging.getLogger("autotune")
    _dev_tag = device_id[:8] if device_id else "?"
    log.info("[오토튠][%s] 코디네이터 시작", _dev_tag)
    _last_logged_active: set[str] = set()  # active_sites 변동 감지용

    # 발행자 PC를 컨텍스트에 박음 (예방 차원 — 사이트 루프에서도 각자 set)
    _owner_token = current_pc_owner.set(device_id)

    try:
        while _is_pc_running(device_id):
            try:
                # 이전 취소/비상정지 플래그 잔존 방지
                from backend.domain.samba.collector.refresher import clear_bulk_cancel

                clear_bulk_cancel("autotune")
                clear_bulk_cancel("transmit")
                from backend.domain.samba.emergency import (
                    clear_emergency_stop,
                    is_emergency_stopped as _is_es,
                )

                if _is_es():
                    clear_emergency_stop()
                    log.info("[오토튠][%s] 잔존 비상정지 해제", _dev_tag)

                from backend.db.orm import get_write_session

                # 공통 사전 작업 (분류, 쿠키)
                async with get_write_session() as session:
                    from backend.domain.samba.collector.model import (
                        SambaCollectedProduct as _CP,
                    )
                    from backend.api.v1.routers.samba.proxy import _get_setting

                    # 롯데ON 쿠키 갱신
                    from backend.domain.samba.proxy.lotteon_sourcing import (
                        set_lotteon_cookie,
                    )

                    _lt_cookie = await _get_setting(session, "lotteon_cookie")
                    if _lt_cookie:
                        set_lotteon_cookie(str(_lt_cookie))

                    # 등급 분류
                    _priority_enabled = await _get_setting(
                        session, AUTOTUNE_PRIORITY_ENABLED_KEY
                    )
                    _use_priority = (
                        _priority_enabled
                        if isinstance(_priority_enabled, bool)
                        else True
                    )
                    if _use_priority:
                        try:
                            await _classify_products(session)
                        except Exception as cls_err:
                            log.warning(
                                "[오토튠][%s] 등급 분류 실패: %s", _dev_tag, cls_err
                            )

                    # 활성 소싱처 목록 파악
                    from backend.api.v1.routers.samba.collector_common import (
                        build_market_registered_conditions,
                    )

                    market_cond = build_market_registered_conditions(_CP)

                    site_stmt = select(func.distinct(_CP.source_site)).where(
                        *market_cond,
                        _CP.applied_policy_id != None,
                        _CP.source_site != None,
                        _CP.source_site != "",
                    )
                    site_result = await session.execute(site_stmt)
                    active_sites = [r[0] for r in site_result.all() if r[0]]

                    # 이 PC가 처리할 사이트만 — _pc_allowed_sites 기준
                    # 미등록(None) → 이 PC가 모든 사이트 처리 (단일 PC 운영)
                    # 등록됨 → 그 사이트만
                    my_sites = get_pc_allowed_sites(device_id)
                    if my_sites is not None:
                        active_sites = [s for s in active_sites if s in my_sites]

                    # 서킷브레이커 제외
                    active_sites = [
                        s for s in active_sites if not _site_breaker_tripped.get(s)
                    ]

                    # 연속 빈 결과 소싱처 일시 제외
                    _now_skip = time.time()
                    active_sites = [
                        s
                        for s in active_sites
                        if _now_skip >= _site_empty_skip_until.get(s, 0)
                    ]

                    if set(active_sites) != _last_logged_active:
                        log.info(
                            "[오토튠][%s] active_sites=%s (allowed=%s)",
                            _dev_tag,
                            sorted(active_sites),
                            sorted(my_sites) if my_sites is not None else "전체",
                        )
                        _last_logged_active = set(active_sites)

                # 소싱처별 독립 루프 태스크 생성 (이 PC의 site_tasks dict에)
                _site_tasks = _pc_st(device_id)
                _newly_spawned = []
                for _site in active_sites:
                    existing = _site_tasks.get(_site)
                    if existing and not existing.done():
                        continue
                    task = asyncio.create_task(
                        _site_autotune_loop(device_id, _site),
                        name=f"autotune-{_dev_tag}-{_site}",
                    )
                    _site_tasks[_site] = task
                    _newly_spawned.append(_site)

                if _newly_spawned:
                    log.info(
                        "[오토튠][%s] 소싱처 루프 시작: %s (활성 %d개)",
                        _dev_tag,
                        ", ".join(_newly_spawned),
                        len([t for t in _site_tasks.values() if not t.done()]),
                    )

                # 완료된 태스크 정리
                for _s in list(_site_tasks.keys()):
                    if _site_tasks[_s].done():
                        try:
                            _site_tasks[_s].result()
                        except asyncio.CancelledError:
                            pass
                        except Exception as _te:
                            log.error(
                                "[오토튠][%s] %s 소싱처 루프 예외 종료: %s",
                                _dev_tag,
                                _s,
                                _te,
                            )
                        del _site_tasks[_s]

                # 필터에서 빠진 site의 살아있는 task 종료
                _active_set = set(active_sites)
                _heartbeats = _pc_hb(device_id)
                for _s in list(_site_tasks.keys()):
                    if _s in _active_set:
                        continue
                    _t = _site_tasks[_s]
                    if not _t.done():
                        log.info("[오토튠][%s][%s] 필터 제외 — 루프 종료", _dev_tag, _s)
                        _t.cancel()
                    del _site_tasks[_s]
                    _heartbeats.pop(_s, None)

                # Watchdog — stuck 소싱처 루프 강제 재시작
                _now_ts = time.time()
                for _s, _t in list(_site_tasks.items()):
                    if _t.done():
                        continue
                    _last_hb = _heartbeats.get(_s, _now_ts)
                    if _now_ts - _last_hb > STUCK_TIMEOUT_SECONDS:
                        log.warning(
                            "[오토튠][%s][%s] stuck 감지 (%.0f초 무응답) — 강제 재시작",
                            _dev_tag,
                            _s,
                            _now_ts - _last_hb,
                        )
                        _t.cancel()
                        del _site_tasks[_s]
                        _heartbeats.pop(_s, None)

                # PC별 통계 갱신
                _pc_cycle_count[device_id] = sum(_pc_scc(device_id).values())
                _ticks = [v for v in _pc_slt(device_id).values() if v]
                if _ticks:
                    _pc_last_tick[device_id] = max(_ticks)

                # 5초 대기 (1초 단위로 중지 확인)
                for _ in range(5):
                    if not _is_pc_running(device_id):
                        break
                    await asyncio.sleep(1)

            except asyncio.CancelledError:
                if not _is_pc_running(device_id):
                    log.info("[오토튠][%s] 코디네이터 취소 (정상 종료)", _dev_tag)
                    break
                _pc_restart_count[device_id] = _pc_restart_count.get(device_id, 0) + 1
                if _pc_restart_count[device_id] >= MAX_RESTART_COUNT:
                    log.error(
                        "[오토튠][%s] 재시작 상한(%d회) 도달 — 코디네이터 중단",
                        _dev_tag,
                        MAX_RESTART_COUNT,
                    )
                    break
                log.warning(
                    "[오토튠][%s] CancelledError — 코디네이터 재시작 (누적 %d회)",
                    _dev_tag,
                    _pc_restart_count[device_id],
                )
                await asyncio.sleep(2)
            except Exception as e:
                _pc_restart_count[device_id] = _pc_restart_count.get(device_id, 0) + 1
                if _pc_restart_count[device_id] >= MAX_RESTART_COUNT:
                    log.error(
                        "[오토튠][%s] 재시작 상한(%d회) 도달 — 코디네이터 중단",
                        _dev_tag,
                        MAX_RESTART_COUNT,
                    )
                    break
                log.error(
                    "[오토튠][%s] 코디네이터 오류 (누적 %d회): %s",
                    _dev_tag,
                    _pc_restart_count[device_id],
                    e,
                    exc_info=True,
                )
                await asyncio.sleep(5)

    finally:
        try:
            current_pc_owner.reset(_owner_token)
        except Exception:
            pass
        # 이 PC의 모든 소싱처 태스크 종료
        _site_tasks = _pc_st(device_id)
        for _s, _t in list(_site_tasks.items()):
            if not _t.done():
                _t.cancel()
        _site_tasks.clear()
        ev = _pc_running.get(device_id)
        if ev is not None:
            ev.clear()
        log.info("[오토튠][%s] 코디네이터 종료", _dev_tag)


class AutotuneStartRequest(BaseModel):
    target_product_no: Optional[str] = None
    # 오토튠을 시작하는 브라우저의 확장앱 deviceId. 이 PC 인스턴스의 키이자
    # 발행되는 모든 잡의 owner_device_id로 박혀서 다른 PC가 가로채지 못함.
    device_id: Optional[str] = None


async def _save_autotune_state(enabled: bool, device_id: str = ""):
    """DB에 오토튠 ON/OFF 상태 + 소유자 deviceId 저장.

    Cloud Run 인스턴스가 교체·스케일아웃될 때 auto_start_if_enabled가
    복원하면서 소유자 deviceId까지 함께 복구해야, SSG/롯데온 탭 작업이
    다른 PC의 확장앱으로 새나가지 않는다.
    """
    try:
        from backend.db.orm import get_write_session
        from backend.api.v1.routers.samba.proxy import _set_setting

        async with get_write_session() as session:
            await _set_setting(session, "autotune_enabled", enabled)
            if enabled:
                # 시작 시에만 deviceId 갱신, 중지 시에는 기존값 유지하지 않고 초기화
                await _set_setting(session, "autotune_owner_device_id", device_id or "")
            else:
                await _set_setting(session, "autotune_owner_device_id", "")
            await session.commit()
    except Exception as e:
        logger.warning(f"[오토튠] 상태 저장 실패: {e}")


async def auto_start_if_enabled():
    """서버 시작 시 DB에서 오토튠 상태 확인 → ON이면 자동 시작.

    전송 Job이 존재하면 완료될 때까지 대기 후 시작 (OOM 방지).
    """
    try:
        # 저장된 인터벌 설정 복원
        from backend.domain.samba.collector.refresher import (
            load_site_autotune_concurrency_from_db,
            load_site_intervals_from_db,
        )

        await load_site_intervals_from_db()
        await load_site_autotune_concurrency_from_db()

        from backend.db.orm import get_read_session
        from backend.api.v1.routers.samba.proxy import _get_setting

        async with get_read_session() as session:
            enabled = await _get_setting(session, "autotune_enabled")
            saved_device_id = (
                await _get_setting(session, "autotune_owner_device_id") or ""
            )
        if enabled:
            # deviceId가 비어 있으면 자동시작을 건너뛴다.
            # 그렇지 않으면 소싱큐 owner가 빈 값으로 세팅돼 모든 PC 확장앱이
            # SSG/롯데온 탭 작업을 집어가게 된다 (다른 PC에서 탭이 계속 열리는 증상).
            # 사용자가 브라우저에서 명시적으로 다시 시작하면 정상 owner와 함께 복구된다.
            if not saved_device_id:
                logger.warning(
                    "[오토튠] 저장된 deviceId 없음 → 자동시작 건너뜀 "
                    "(브라우저에서 수동 시작 필요, 다른 PC 탭 열림 방지)"
                )
                # DB 상태를 실제 상태(중지)로 강제 동기화 — 유령 enabled=True 제거
                # 이렇게 해야 프런트 UI 토글이 OFF로 보이고 사용자가 수동 재시작할 수 있다
                try:
                    from backend.db.orm import get_write_session as _get_ws
                    from backend.api.v1.routers.samba.proxy import (
                        _set_setting as _ss,
                    )

                    async with _get_ws() as _ws:
                        await _ss(_ws, "autotune_enabled", False)
                        await _ws.commit()
                except Exception as _e:
                    logger.warning(f"[오토튠] enabled=False 동기화 실패: {_e}")

                # 워룸 타임라인에 경고 이벤트 발행 — 관리자가 UI에서 즉시 인지 가능
                try:
                    from backend.domain.samba.warroom.service import (
                        SambaMonitorService,
                    )
                    from backend.db.orm import get_write_session as _get_ws2

                    async with _get_ws2() as _ws2:
                        _monitor = SambaMonitorService(_ws2)
                        await _monitor.emit(
                            "autotune_auto_stopped",
                            "warning",
                            summary=(
                                "오토튠 자동복원 실패 — 저장된 deviceId 없음. "
                                "워룸에서 수동으로 다시 시작하세요."
                            ),
                            detail={"reason": "missing_owner_device_id"},
                        )
                        await _ws2.commit()
                except Exception as _e:
                    logger.warning(f"[오토튠] 경고 이벤트 발행 실패: {_e}")
                return

            # 전송 Job 존재 시 대기 (OOM 방지 — 전송과 동시 실행 차단)
            from backend.db.orm import get_read_session as _get_rs
            from sqlalchemy import text as _st

            for _wait in range(12):  # 최대 60초 대기
                async with _get_rs() as _s:
                    _r = await _s.execute(
                        _st(
                            "SELECT count(*) FROM samba_jobs "
                            "WHERE status IN ('pending', 'running') "
                            "AND job_type = 'transmit'"
                        )
                    )
                    _tx_count = _r.scalar() or 0
                if _tx_count == 0:
                    break
                logger.info(
                    "[오토튠] 전송 Job %d건 진행 중 — 시작 대기 (%d/12)",
                    _tx_count,
                    _wait + 1,
                )
                await asyncio.sleep(5)

            from backend.domain.samba.collector.refresher import clear_bulk_cancel

            if not _is_pc_running(saved_device_id):
                _site_empty_skip_until.clear()
                clear_bulk_cancel("autotune")
                clear_bulk_cancel("transmit")
                ev = _get_pc_event(saved_device_id)
                ev.set()
                _pc_cycle_count[saved_device_id] = 0
                _pc_restart_count[saved_device_id] = 0
                _pc_main_task[saved_device_id] = asyncio.create_task(
                    _autotune_loop(saved_device_id),
                    name=f"autotune-main-{saved_device_id[:8]}",
                )
                logger.info(
                    "[오토튠] 서버 시작 — DB 설정에 따라 자동 시작 (owner=%s)",
                    saved_device_id[:8],
                )
    except Exception as e:
        logger.warning(f"[오토튠] 자동 시작 실패: {e}")


class RefreshOneRequest(BaseModel):
    product_no: str


@router.post("/autotune/refresh-one")
async def autotune_refresh_one(body: RefreshOneRequest):
    """단일 상품 오토튠 갱신 — 상품번호로 검색 후 1건 갱신."""
    from backend.db.orm import get_write_session
    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP
    from backend.domain.samba.collector.refresher import refresh_products_bulk
    from backend.domain.samba.collector.repository import (
        SambaCollectedProductRepository,
    )

    pno = body.product_no.strip()
    if not pno:
        return {"ok": False, "error": "상품번호를 입력해주세요"}

    async with get_write_session() as session:
        repo = SambaCollectedProductRepository(session)

        # 1) id 검색
        product = await repo.get_async(pno)

        # 2) site_product_id 검색
        if not product:
            stmt = select(_CP).where(_CP.site_product_id == pno).limit(1)
            result = await session.execute(stmt)
            product = result.scalars().first()

        # 3) market_product_nos 값 검색 (JSON 내부 value 매칭)
        if not product:
            from sqlalchemy import cast, String

            stmt = (
                select(_CP)
                .where(cast(_CP.market_product_nos, String).contains(pno))
                .limit(5)
            )
            result = await session.execute(stmt)
            candidates = list(result.scalars().all())
            for c in candidates:
                nos = c.market_product_nos or {}
                if pno in str(nos.values()):
                    product = c
                    break

        if not product:
            return {"ok": False, "error": f"'{pno}' 상품을 찾을 수 없습니다"}

        # 갱신 실행
        results, summary = await refresh_products_bulk([product], source="manual")

        now = datetime.now(timezone.utc)
        kst_now = now + timedelta(hours=9)
        ts_str = kst_now.strftime("%H:%M:%S")
        r = results[0] if results else None
        detail_text = "갱신 실패"
        status = "error"
        site = getattr(product, "source_site", "") or ""
        brand = getattr(product, "brand", "") or ""
        name = (getattr(product, "name", "") or "")[:50]

        if r and not r.error:
            old_price = product.sale_price or 0
            new_price = r.new_sale_price if r.new_sale_price is not None else old_price
            old_status = getattr(product, "sale_status", "in_stock")
            changes: list[str] = []
            if new_price != old_price:
                changes.append(f"가격 ₩{int(old_price):,}→₩{int(new_price):,}")
            if r.new_sale_status and r.new_sale_status != old_status:
                changes.append(f"상태 {old_status}→{r.new_sale_status}")
            if r.stock_changed:
                changes.append("재고변동")

            if changes:
                detail_text = " / ".join(changes)
                status = "changed"
            else:
                detail_text = "변동 없음"
                status = "unchanged"

            # DB 업데이트
            from backend.api.v1.routers.samba.collector_common import _trim_history

            updates: dict = {
                "last_refreshed_at": now,
                "refresh_error_count": 0,
            }

            # 가격이력 스냅샷 — 변동 여부와 관계없이 항상 기록
            snapshot: dict = {
                "date": now.isoformat(),
                "source": "refresh-one",
                "sale_price": r.new_sale_price
                if r.new_sale_price is not None
                else product.sale_price,
                "original_price": r.new_original_price
                if r.new_original_price is not None
                else product.original_price,
                "cost": r.new_cost if r.new_cost is not None else product.cost,
                "sale_status": r.new_sale_status,
                "changed": r.changed,
            }
            # 옵션: 신규 수집 우선, 없으면 기존 DB 옵션 폴백
            _snap_options = r.new_options
            if not _snap_options and product.options:
                _snap_options = product.options
            if _snap_options:
                snapshot["options"] = _snap_options
            history = list(product.price_history or [])
            history.insert(0, snapshot)
            updates["price_history"] = _trim_history(history)

            # 옵션은 항상 갱신
            if r.new_options is not None:
                updates["options"] = r.new_options
            updates["sale_status"] = r.new_sale_status
            if r.changed:
                if r.new_sale_price is not None:
                    updates["sale_price"] = r.new_sale_price
                if r.new_original_price is not None:
                    updates["original_price"] = r.new_original_price
            # cost는 changed 여부와 무관하게 항상 반영 (혜택가 단독 변경 대응)
            if r.new_cost is not None:
                updates["cost"] = r.new_cost
            await repo.update_async(product.id, **updates)
            await session.commit()
        elif r and r.error:
            detail_text = r.error[:80]

        # 오토튠 로그 버퍼에 직접 추가 → 실시간 로그 패널에 표시
        from backend.domain.samba.collector.refresher import (
            _refresh_log_buffer,
        )
        import backend.domain.samba.collector.refresher as _rfr

        site_tag = f"[{site}] " if site else ""
        log_msg = f"[{ts_str}] [단일갱신] {site_tag}{brand} {name}: {detail_text}"
        _refresh_log_buffer.append(
            {
                "ts": now.isoformat(),
                "site": site,
                "product_id": product.id,
                "name": name,
                "msg": log_msg,
                "level": "info" if status != "error" else "warning",
                "source": "autotune",
            }
        )
        _rfr._refresh_log_total += 1

        return {"ok": True}


@router.post("/autotune/start")
async def autotune_start(
    body: AutotuneStartRequest = AutotuneStartRequest(),
    request: Request = None,
):
    """오토튠 무한 루프 시작 — 메인 이벤트 루프에서 실행."""
    # 티어 제한 체크 — 오토튠 접근 권한
    try:
        from backend.db.orm import get_read_session
        from backend.domain.samba.tenant.middleware import (
            check_autotune_access,
        )

        if request:
            async with get_read_session() as session:
                # JWT에서 tenant_id 추출 시도
                auth_header = request.headers.get("Authorization") or ""
                if auth_header.startswith("Bearer "):
                    token = auth_header.split(" ", 1)[1]
                    try:
                        from backend.core.config import settings
                        import jwt as _jwt

                        payload = _jwt.decode(
                            token,
                            settings.jwt_secret_key,
                            algorithms=[settings.jwt_algorithm],
                        )
                        user_id = payload.get("sub", "")
                        if user_id:
                            from backend.domain.samba.user.model import SambaUser

                            stmt = select(SambaUser).where(SambaUser.id == user_id)
                            result = (await session.execute(stmt)).scalars().first()
                            tid = getattr(result, "tenant_id", None) if result else None
                            if tid:
                                await check_autotune_access(tid, session)
                    except Exception:
                        pass  # 인증 실패 시 기존 동작 유지
    except Exception:
        pass  # 모듈 로드 실패 시 기존 동작 유지

    from backend.domain.samba.collector.refresher import clear_bulk_cancel

    dev = (body.device_id or "").strip()
    if not dev:
        return {"ok": False, "error": "device_id 필수"}

    if _is_pc_running(dev):
        return {"ok": True, "status": "already_running"}

    # 단일 상품 오토튠: 상품번호 → 내부 ID 변환 (이 PC 인스턴스에만 적용)
    _target_ids: Optional[set] = None
    if body.target_product_no:
        pno = body.target_product_no.strip()
        if pno:
            from backend.db.orm import get_read_session
            from backend.domain.samba.collector.model import (
                SambaCollectedProduct as _CP,
            )
            from sqlalchemy import cast, String

            async with get_read_session() as session:
                # id 검색
                stmt = select(_CP.id).where(_CP.id == pno).limit(1)
                row = (await session.execute(stmt)).scalar()
                if not row:
                    # site_product_id 검색
                    stmt = select(_CP.id).where(_CP.site_product_id == pno).limit(1)
                    row = (await session.execute(stmt)).scalar()
                if not row:
                    # market_product_nos 값 검색
                    stmt = (
                        select(_CP.id, _CP.market_product_nos)
                        .where(cast(_CP.market_product_nos, String).contains(pno))
                        .limit(5)
                    )
                    rows = (await session.execute(stmt)).all()
                    for r in rows:
                        nos = r[1] or {}
                        if pno in str(nos.values()):
                            row = r[0]
                            break
                if not row:
                    return {
                        "ok": False,
                        "error": f"'{pno}' 상품을 찾을 수 없습니다",
                    }
                _target_ids = {row}

    _pc_target_ids[dev] = _target_ids
    _pc_force_stop_set.discard(dev)
    # 이 PC 인스턴스 상태 초기화
    _pc_cycle_count[dev] = 0
    _pc_restart_count[dev] = 0
    _pc_last_tick.pop(dev, None)
    _pc_site_cycle_counts[dev] = {}
    _pc_site_last_ticks[dev] = {}
    _pc_site_empty_hits[dev] = {}
    _pc_site_heartbeats[dev] = {}
    # 이전 소싱처 루프 명시적 취소
    _existing_tasks = _pc_site_tasks.get(dev) or {}
    for _t in list(_existing_tasks.values()):
        if not _t.done():
            _t.cancel()
    _pc_site_tasks[dev] = {}

    # bulk_cancel 플래그는 이 PC만의 신호가 아니라 모든 PC 공통이므로 신중히 다룸 —
    # 시작 시점에서 fresh state 보장을 위해 전체 클리어 (다른 PC는 자기 사이클 다음 틱에서 자연 복원)
    clear_bulk_cancel()

    ev = _get_pc_event(dev)
    ev.set()
    _pc_main_task[dev] = asyncio.create_task(
        _autotune_loop(dev),
        name=f"autotune-main-{dev[:8]}",
    )
    if not body.target_product_no:
        # 서버 재시작 후 자동 복원 — 한 PC라도 켜져 있었다는 사실 + 마지막 owner deviceId 저장
        await _save_autotune_state(True, dev)
    return {"ok": True, "status": "started", "target": "registered"}


class AutotuneStopRequest(BaseModel):
    device_id: str = ""


@router.post("/autotune/stop")
async def autotune_stop(body: AutotuneStopRequest = AutotuneStopRequest()):
    """오토튠 정지 — 이 PC 인스턴스만 정지. 다른 PC는 영향 없음."""
    from backend.domain.samba.collector.refresher import request_bulk_cancel_all

    dev = (body.device_id or "").strip()
    if not dev:
        return {"ok": False, "error": "device_id 필수"}

    if not _is_pc_running(dev):
        return {"ok": True, "status": "already_stopped"}

    # 다음 폴링 시 이 PC 확장앱에 forceStop 신호 전달
    _pc_force_stop_set.add(dev)

    ev = _pc_running.get(dev)
    if ev is not None:
        ev.clear()

    # 이 PC의 소싱처 루프 모두 취소
    _site_tasks = _pc_site_tasks.get(dev) or {}
    for _st in list(_site_tasks.values()):
        if not _st.done():
            _st.cancel()
    _site_tasks.clear()

    # 메인 코디네이터 태스크 취소
    _main = _pc_main_task.get(dev)
    if _main and not _main.done():
        _main.cancel()
    _pc_main_task.pop(dev, None)

    # 어떤 PC도 안 돌면 전역 bulk cancel + DB 상태 OFF로 표시
    if not any_pc_running():
        request_bulk_cancel_all()
        try:
            from backend.domain.samba.proxy.sourcing_queue import SourcingQueue

            SourcingQueue.cancel_all("all PCs stopped")
        except Exception:
            pass
        await _save_autotune_state(False)

    # 인스턴스 상태 cleanup
    _cleanup_pc_instance(dev)

    return {"ok": True, "status": "stopped"}


class PcAllowedSitesRequest(BaseModel):
    """PC 분담 등록/갱신 요청.

    sites=null → 등록 자체 제거 (오토튠 합집합에서 빠짐)
    sites=[] → 빈 분담 (이 PC는 작업 안 받음)
    sites=[...] → 명시 사이트만 받음
    """

    device_id: str
    sites: Optional[list[str]] = None


@router.post("/autotune/pc-allowed-sites")
async def autotune_pc_allowed_sites_set(body: PcAllowedSitesRequest):
    """PC 분담 등록 — 이 PC가 처리할 사이트 목록."""
    register_pc_allowed_sites(body.device_id, body.sites)
    pcs = get_active_pcs()
    return {
        "ok": True,
        "registered_pcs": len(pcs),
        "this_pc": sorted(pcs.get(body.device_id.strip(), set())),
    }


@router.get("/autotune/pc-allowed-sites")
async def autotune_pc_allowed_sites_get():
    """현재 등록된 모든 PC 분담 매핑 조회 (디버그용)."""
    pcs = get_active_pcs()
    return {
        "registered_pcs": len(pcs),
        "by_device": {dev: sorted(sites) for dev, sites in pcs.items()},
    }


@router.get("/autotune/status")
async def autotune_status(device_id: str = ""):
    """오토튠 상태 조회 — device_id 미지정 시 글로벌 합계(any_pc_running) 반환.

    device_id 지정 시 그 PC 인스턴스 기준 cycle/last_tick/site_loops 반환.
    """
    from backend.db.orm import get_read_session
    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP2

    tripped = {
        site: count
        for site, count in _site_consecutive_soldout.items()
        if _site_breaker_tripped.get(site)
    }

    # DB 기반 24h 갱신 수 (서버 재시작해도 유지)
    refreshed_24h = 0
    try:
        since_24h = datetime.now(timezone.utc) - timedelta(hours=24)
        async with get_read_session() as rs:
            cnt_stmt = select(func.count(_CP2.id)).where(
                _CP2.last_refreshed_at >= since_24h
            )
            refreshed_24h = (await rs.execute(cnt_stmt)).scalar() or 0
    except Exception:
        refreshed_24h = 0

    # 소싱처별 인터벌 정보
    from backend.domain.samba.collector.refresher import (
        get_effective_autotune_concurrency,
        get_site_intervals_info,
    )

    intervals_info = get_site_intervals_info()

    # 등급 분류 ON/OFF
    priority_enabled = True
    try:
        from backend.api.v1.routers.samba.proxy import _get_setting

        async with get_read_session() as rs2:
            _pv = await _get_setting(rs2, AUTOTUNE_PRIORITY_ENABLED_KEY)
        priority_enabled = _pv if isinstance(_pv, bool) else True
    except Exception:
        pass

    dev = (device_id or "").strip()
    _now_hb = time.time()
    if dev:
        _site_tasks = _pc_site_tasks.get(dev) or {}
        _scc = _pc_site_cycle_counts.get(dev) or {}
        _shb = _pc_site_heartbeats.get(dev) or {}
        _active_site_loops = {
            s: {
                "running": not t.done(),
                "cycles": _scc.get(s, 0),
                "heartbeat_ago": round(_now_hb - _shb.get(s, _now_hb)),
            }
            for s, t in _site_tasks.items()
        }
        main_task = _pc_main_task.get(dev)
        running = _is_pc_running(dev) and main_task is not None and not main_task.done()
        last_tick = _pc_last_tick.get(dev)
        cycle_count = _pc_cycle_count.get(dev, 0)
        restart_count = _pc_restart_count.get(dev, 0)
    else:
        # 글로벌 뷰: 어떤 PC라도 실행 중이면 running=True. 사이트 루프는 모든 PC 합산
        _active_site_loops = {}
        for _d, _stasks in _pc_site_tasks.items():
            _scc = _pc_site_cycle_counts.get(_d) or {}
            _shb = _pc_site_heartbeats.get(_d) or {}
            for s, t in _stasks.items():
                prev = _active_site_loops.get(s)
                cycles = _scc.get(s, 0)
                hb_ago = round(_now_hb - _shb.get(s, _now_hb))
                if prev is None:
                    _active_site_loops[s] = {
                        "running": not t.done(),
                        "cycles": cycles,
                        "heartbeat_ago": hb_ago,
                    }
                else:
                    prev["running"] = prev["running"] or not t.done()
                    prev["cycles"] = prev["cycles"] + cycles
                    prev["heartbeat_ago"] = min(prev["heartbeat_ago"], hb_ago)
        running = any_pc_running()
        last_tick_vals = [v for v in _pc_last_tick.values() if v]
        last_tick = max(last_tick_vals) if last_tick_vals else None
        cycle_count = sum(_pc_cycle_count.values())
        restart_count = sum(_pc_restart_count.values())

    return {
        "running": running,
        "last_tick": last_tick,
        "cycle_count": cycle_count,
        "restart_count": restart_count,
        "max_restart": MAX_RESTART_COUNT,
        "refreshed_count": refreshed_24h,
        "target": "registered",
        "breaker_tripped": tripped,
        "site_intervals": intervals_info.get("base_intervals", {}),
        "site_autotune_concurrency": get_effective_autotune_concurrency(),
        "priority_enabled": priority_enabled,
        "site_loops": _active_site_loops,
        "stuck_timeout": STUCK_TIMEOUT_SECONDS,
        # PC별 분담 현황 (UI 표시용)
        "pc_assignments": {
            dev: sorted(sites) for dev, sites in get_active_pcs().items() if sites
        },
        # 현재 오토튠 실행 중인 PC 목록 (UI에서 본인이 그 중에 있는지 판단)
        "running_pcs": sorted(d for d, ev in _pc_running.items() if ev.is_set()),
    }


class AutotuneIntervalRequest(BaseModel):
    site: str
    interval: float  # 초


@router.post("/autotune/interval")
async def autotune_update_interval(body: AutotuneIntervalRequest):
    """소싱처별 오토튠 인터벌 동적 변경 (초 단위)."""
    from backend.domain.samba.collector.refresher import set_site_base_interval

    if body.interval < 0 or body.interval > 60:
        return {"ok": False, "error": "인터벌은 0~60초 범위만 가능합니다"}
    await set_site_base_interval(body.site, body.interval)
    logger.info("[오토튠] 인터벌 변경: %s → %.1f초", body.site, body.interval)
    return {"ok": True, "site": body.site, "interval": body.interval}


class AutotuneConcurrencyRequest(BaseModel):
    site: str
    value: int  # 동시 처리 상품 수


@router.post("/autotune/concurrency")
async def autotune_update_concurrency(body: AutotuneConcurrencyRequest):
    """소싱처별 오토튠 동시성(병렬도) 동적 변경."""
    from backend.domain.samba.collector.refresher import set_site_autotune_concurrency

    if body.value < 1 or body.value > 50:
        return {"ok": False, "error": "동시성은 1~50 범위만 가능합니다"}
    await set_site_autotune_concurrency(body.site, body.value)
    logger.info("[오토튠] 동시성 변경: %s → %d", body.site, body.value)
    return {"ok": True, "site": body.site, "value": body.value}


@router.get("/autotune/concurrency")
async def autotune_get_concurrency():
    """소싱처별 오토튠 동시성 조회."""
    from backend.domain.samba.collector.refresher import (
        get_effective_autotune_concurrency,
    )

    return {"ok": True, "concurrency": get_effective_autotune_concurrency()}


@router.post("/autotune/breaker-reset")
async def autotune_breaker_reset(site: str = ""):
    """소싱처별 서킷브레이커 수동 해제. site 미지정 시 전체 해제."""
    if site:
        _site_breaker_tripped.pop(site, None)
        _site_consecutive_soldout.pop(site, None)
        logger.info("[오토튠] 서킷브레이커 해제: %s", site)
        return {"ok": True, "reset": site}
    else:
        _site_breaker_tripped.clear()
        _site_consecutive_soldout.clear()
        _pending_cost_increase.clear()
        logger.info("[오토튠] 서킷브레이커 전체 해제 (pending 가격 상승 초기화 포함)")
        return {"ok": True, "reset": "all"}


# ── 등급 분류(hot/warm/cold) ON/OFF ──


@router.get("/autotune/priority")
async def autotune_get_priority():
    """등급 분류 ON/OFF 상태 조회."""
    from backend.db.orm import get_read_session
    from backend.api.v1.routers.samba.proxy import _get_setting

    async with get_read_session() as session:
        val = await _get_setting(session, AUTOTUNE_PRIORITY_ENABLED_KEY)
    enabled = val if isinstance(val, bool) else True
    return {"ok": True, "priority_enabled": enabled}


class AutotunePriorityRequest(BaseModel):
    enabled: bool


@router.post("/autotune/priority")
async def autotune_set_priority(body: AutotunePriorityRequest):
    """등급 분류 ON/OFF 설정 변경."""
    from backend.db.orm import get_write_session
    from backend.api.v1.routers.samba.proxy import _set_setting

    async with get_write_session() as session:
        await _set_setting(session, AUTOTUNE_PRIORITY_ENABLED_KEY, body.enabled)
        await session.commit()
    label = "ON" if body.enabled else "OFF"
    logger.info("[오토튠] 등급 분류 %s", label)
    return {"ok": True, "priority_enabled": body.enabled}


# ── 오토튠 필터 (소싱처 / 판매처 선택) ──


class AutotuneFilterRequest(BaseModel):
    enabled_sources: Optional[list[str]] = None
    enabled_markets: Optional[list[str]] = None


@router.get("/autotune/filters")
async def autotune_get_filters():
    """오토튠 필터 설정 + 실제 존재하는 소싱처/판매처(마켓 단위) 목록 반환."""
    import json as _json

    from backend.db.orm import get_read_session
    from backend.api.v1.routers.samba.proxy import _get_setting
    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP
    from backend.domain.samba.account.model import SambaMarketAccount
    from sqlalchemy import distinct

    async with get_read_session() as session:
        # 현재 저장된 필터
        saved_sources = await _get_setting(session, AUTOTUNE_FILTER_SOURCES_KEY)
        saved_markets = await _get_setting(session, AUTOTUNE_FILTER_MARKETS_KEY)

        # 마켓 등록 상품이 있는 소싱처만 (수집만 된 것은 제외)
        src_stmt = select(distinct(_CP.source_site)).where(
            _CP.source_site != None,
            _CP.source_site != "",
            _CP.status == "registered",
        )
        src_result = await session.execute(src_stmt)
        available_sources = sorted([r[0] for r in src_result.all() if r[0]])

        # 등록된 상품의 registered_accounts → 계정 ID 수집
        reg_stmt = select(_CP.registered_accounts).where(
            _CP.status == "registered",
            _CP.registered_accounts.isnot(None),
        )
        reg_result = await session.execute(reg_stmt)
        _acc_ids: set[str] = set()
        for row in reg_result.all():
            val = row[0]
            if not val:
                continue
            # JSON 컬럼이 문자열로 반환될 수 있음
            if isinstance(val, str):
                try:
                    val = _json.loads(val)
                except Exception:
                    continue
            if isinstance(val, list):
                _acc_ids.update(str(a) for a in val if a)

        # 계정 → market_type 매핑 후 중복 제거 (마켓 단위)
        available_markets: list[str] = []
        if _acc_ids:
            acc_stmt = select(distinct(SambaMarketAccount.market_type)).where(
                SambaMarketAccount.id.in_(list(_acc_ids))
            )
            acc_result = await session.execute(acc_stmt)
            available_markets = sorted([r[0] for r in acc_result.all() if r[0]])

    return {
        "enabled_sources": saved_sources if isinstance(saved_sources, list) else None,
        "enabled_markets": saved_markets if isinstance(saved_markets, list) else None,
        "available_sources": available_sources,
        "available_markets": available_markets,
    }


@router.put("/autotune/filters")
async def autotune_set_filters(body: AutotuneFilterRequest):
    """오토튠 소싱처/판매처 필터 저장. None이면 전체 허용(필터 해제)."""
    from backend.db.orm import get_write_session
    from backend.api.v1.routers.samba.proxy import _set_setting

    async with get_write_session() as session:
        await _set_setting(session, AUTOTUNE_FILTER_SOURCES_KEY, body.enabled_sources)
        await _set_setting(session, AUTOTUNE_FILTER_MARKETS_KEY, body.enabled_markets)
        await session.commit()

    logger.info(
        "[오토튠] 필터 저장 — 소싱처: %s, 판매처: %s",
        body.enabled_sources if body.enabled_sources else "전체",
        f"{len(body.enabled_markets)}개" if body.enabled_markets else "전체",
    )
    return {
        "ok": True,
        "enabled_sources": body.enabled_sources,
        "enabled_markets": body.enabled_markets,
    }

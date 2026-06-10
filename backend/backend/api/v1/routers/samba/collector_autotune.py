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
from sqlalchemy import func, update as sa_update
from sqlalchemy.orm import defer
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.api.v1.routers.samba.collector_common import (
    _trim_history,
)
from backend.domain.samba.exchange_rate_service import convert_cost_by_source_site

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collector", tags=["samba-collector"])


# ── 활성 소싱처 distinct 결과 글로벌 캐시 ──
# 코디네이터가 5초마다 distinct(source_site)를 풀스캔 → PC 11대 동시 실행 시
# IPC/BufferIO 대기로 active 쿼리 60초까지 누적. 결과는 모든 PC에서 동일하므로
# 30초 TTL 글로벌 캐시 1개로 통합 → 부하 12배+ 감소.
_ACTIVE_SITES_CACHE_TTL = 30.0
_active_sites_cache: dict = {"ts": 0.0, "data": None}
_active_sites_cache_lock = asyncio.Lock()

# 품절 옵션 강제 재확인 주기 — 한 번 0 기록 후 boolean flip 없어도 STALE 이면 재전송 (#400)
SOLDOUT_REASSERT_SEC = float(
    os.environ.get("AUTOTUNE_SOLDOUT_REASSERT_SEC", "21600")
)  # 6h


def _is_send_stale(sent_at: str | None, max_age_sec: float) -> bool:
    """sent_at(ISO) 이 max_age_sec 초보다 오래됐거나 없으면 True(보수적 재확인)."""
    if not sent_at:
        return True
    try:
        dt = datetime.fromisoformat(sent_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() > max_age_sec
    except (ValueError, TypeError):
        return True


async def _get_active_sites_cached() -> list[str]:
    """모든 PC가 공유하는 활성 소싱처 distinct 결과 (TTL 30s).

    독립된 read session 사용 — 호출자의 write session 점유 시간 단축.
    Cold start 시 lock 대기는 read pool에서만 발생, write pool 영향 없음.
    """
    from backend.api.v1.routers.samba.collector_common import (
        build_market_registered_conditions,
    )
    from backend.db.orm import get_read_session
    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP

    now_ts = time.monotonic()
    cached = _active_sites_cache.get("data")
    if (
        cached is not None
        and (now_ts - _active_sites_cache["ts"]) < _ACTIVE_SITES_CACHE_TTL
    ):
        return list(cached)

    # lock 점유 중이면 다른 코루틴이 갱신 중 → stale 반환(없으면 직접 조회)
    if _active_sites_cache_lock.locked() and cached is not None:
        return list(cached)

    async with _active_sites_cache_lock:
        # double-check
        cached = _active_sites_cache.get("data")
        now_ts = time.monotonic()
        if (
            cached is not None
            and (now_ts - _active_sites_cache["ts"]) < _ACTIVE_SITES_CACHE_TTL
        ):
            return list(cached)

        market_cond = build_market_registered_conditions(_CP)
        stmt = select(func.distinct(_CP.source_site)).where(
            *market_cond,
            _CP.applied_policy_id != None,
            _CP.source_site != None,
            _CP.source_site != "",
        )
        async with get_read_session() as rs:
            result = await rs.execute(stmt)
            sites = [r[0] for r in result.all() if r[0]]
        _active_sites_cache["data"] = sites
        _active_sites_cache["ts"] = time.monotonic()
        return list(sites)


# ── autotune/status refreshed_24h 글로벌 캐시 ──
# /autotune/status 엔드포인트가 매 호출마다 count(last_refreshed_at>=24h) 풀스캔.
# PC 8대 폴링 시 동시 8개 → IPC/BufferIO 누적. 60초 TTL이면 충분.
_REFRESHED_24H_CACHE_TTL = 60.0
_refreshed_24h_cache: dict = {"ts": 0.0, "value": None}
_refreshed_24h_cache_lock = asyncio.Lock()


async def _get_refreshed_24h_cached() -> int:
    """24h 갱신 건수 글로벌 캐시 (TTL 60s)."""
    from backend.db.orm import get_read_session
    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP2

    now_ts = time.monotonic()
    cached = _refreshed_24h_cache.get("value")
    if (
        cached is not None
        and (now_ts - _refreshed_24h_cache["ts"]) < _REFRESHED_24H_CACHE_TTL
    ):
        return int(cached)

    if _refreshed_24h_cache_lock.locked() and cached is not None:
        return int(cached)

    async with _refreshed_24h_cache_lock:
        cached = _refreshed_24h_cache.get("value")
        now_ts = time.monotonic()
        if (
            cached is not None
            and (now_ts - _refreshed_24h_cache["ts"]) < _REFRESHED_24H_CACHE_TTL
        ):
            return int(cached)

        try:
            since_24h = datetime.now(timezone.utc) - timedelta(hours=24)
            async with get_read_session() as rs:
                cnt_stmt = select(func.count(_CP2.id)).where(
                    _CP2.last_refreshed_at >= since_24h
                )
                value = (await rs.execute(cnt_stmt)).scalar() or 0
        except Exception:
            value = 0

        _refreshed_24h_cache["value"] = int(value)
        _refreshed_24h_cache["ts"] = time.monotonic()
        return int(value)


def _is_stale_conn_error(exc: BaseException) -> bool:
    """좀비 connection/끊긴 트랜잭션 감지.

    Cloud SQL idle_in_transaction_session_timeout으로 끊긴 세션을 재사용할 때
    SQLAlchemy/asyncpg가 던지는 대표 메시지/예외 이름을 모아서 판정한다.

    NOTE: "session is in 'prepared' state" 는 SQLAlchemy SessionTransaction 상태 머신
    에러로, 근본 원인 미파악(begin_nested 명시 호출 없음 — collector/service.py 외).
    단발 발생에 대한 retry 가드 — 빈번 발생 시 별도 추적 필요.
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
        or "session is in 'prepared' state" in msg.lower()
        or "greenlet_spawn has not been called" in msg.lower()
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
# 사용자 중단(cancel-cycle) 억제 플래그 — (device_id, site) -> 재spawn 거부 만료 시각.
# allowed_sites 가 비어있거나(확장앱) 불일치해도 중단을 보장하는 핵심 가드.
# 코디네이터 spawn 루프 + 사이트 루프 CancelledError 핸들러가 함께 확인해
# task.cancel() 후 즉시 자가부활/재spawn 되던 버그(2026-05-29) 차단.
_pc_site_cancel_until: dict[tuple[str, str], float] = {}


def _is_site_cancel_suppressed(dev: str, site: str) -> bool:
    """(dev, site) 가 사용자 중단으로 재spawn 억제 중인지. 만료 시 자동 정리."""
    until = _pc_site_cancel_until.get((dev, site), 0.0)
    if until <= 0:
        return False
    if time.time() >= until:
        _pc_site_cancel_until.pop((dev, site), None)
        return False
    return True


# PC별 백그라운드 transmit fire-and-forget 태스크 — autotune_stop에서 함께 cancel.
# fire-and-forget으로 띄운 transmit 잡이 main_task/site_tasks와 분리돼 정지 후에도 계속
# 살아 전송되던 버그(2026-05-27) 해결용. dev별 분리, done 시 discard.
_pc_bg_transmit_tasks: dict[str, set[asyncio.Task]] = {}
_pc_site_cycle_counts: dict[str, dict[str, int]] = {}
_pc_site_last_ticks: dict[str, dict[str, str]] = {}
_pc_site_empty_hits: dict[str, dict[str, int]] = {}
_pc_site_heartbeats: dict[str, dict[str, float]] = {}
_pc_target_ids: dict[str, Optional[set]] = {}
# 사이트별 적응 배치 크기 (device_id → site → int).
# 직전 배치 소요시간 기준으로 다음 배치 SELECT limit 자동 조정. 미설정 시 env 기본값 사용.
_pc_site_batch_size: dict[str, dict[str, int]] = {}


def _pc_bs(dev: str) -> dict[str, int]:
    return _pc_site_batch_size.setdefault(dev, {})


def _adapt_batch_size(dev: str, site: str, elapsed: float, env_max: int) -> None:
    """직전 배치 elapsed 기반 다음 배치 크기 조정. 하한 50, 상한 max(env_max, 400).

    - elapsed > 120초: 절반으로 (사이클 길어짐 → 풀/응답 부담 완화)
    - elapsed < 30초: +50 (여유 있으면 처리량 증가)
    - 그 사이: 유지
    """
    _bs = _pc_bs(dev)
    _cur = _bs.get(site, env_max)
    _hi = max(env_max, 400)
    if elapsed > 120:
        _new = max(50, _cur // 2)
    elif elapsed < 30:
        _new = min(_hi, _cur + 50)
    else:
        _new = _cur
    if _new != _cur:
        _bs[site] = _new


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

# 무신사 자동로그인 쿠키 손실(refresher MUSINSA_AUTH_MISSING) 추적 —
# 오토튠 무신사 사이클 중단 + 경고용. {device_id: 손실 감지 epoch}.
# 재확인 인터벌 경과 시 1회 프로브 사이클을 허용해 쿠키 복구를 자동 감지한다.
_musinsa_auth_lost_at: dict[str, float] = {}
# {device_id: 마지막 경고 발행 epoch} — 6시간 쿨다운으로 경고 폭주 차단
_musinsa_auth_alerted_at: dict[str, float] = {}
_MUSINSA_AUTH_RECHECK_SEC = 300.0  # 5분마다 1회 프로브 사이클 허용
_MUSINSA_AUTH_ALERT_COOLDOWN_SEC = 6 * 60 * 60  # 경고 6시간 쿨다운

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
        "deleted_pids": set(),  # 품절 unique 상품 수 (1 상품 N 마켓삭제 = 1)
        "synced": 0,
        "deleted": 0,
        "batches": 0,
        "started_at": None,
    }


_autotune_cycle_stats: dict[tuple[str, str], dict] = {}

# Watchdog
STUCK_TIMEOUT_SECONDS = 300  # 5분간 heartbeat 없으면 stuck 판정
# 사이트별 stuck timeout — LOTTEON은 응답 느려 5분 내 cycle 완료 어려움.
# 늘려서 cycle 끝까지 가도록 함 → scheduler_tick 이벤트 정상 발행.
_SITE_STUCK_TIMEOUT_OVERRIDE = {
    "LOTTEON": 900,  # 15분 (concurrency=1 + WAF 차단 대응)
    "MUSINSA": 600,  # 10분 (IP 차단/로테이션으로 200건 배치 5분 초과 → Watchdog 강제재시작 방지)
}
MAX_RESTART_COUNT = 50  # 코디네이터 재시작 상한선

# 품절잔존 마켓삭제 영구실패(롯데홈쇼핑 "MD 승인 대기" 등) 재시도 쿨다운.
# 승인 전엔 삭제 불가한 상품을 매 사이클 헛시도 → 무신사 사이클 4분 점유(화면 공백)
# + 처리량 절반 낭비 + STUCK_TIMEOUT(600초) 초과로 Watchdog 강제재시작 유발.
# 실패 시 product_id → 재시도 차단 만료시각(UTC) 기록, 다음 SELECT에서 제외.
_soldout_delete_retry_block: dict[str, datetime] = {}
_SOLDOUT_DELETE_BLOCK_TTL_SEC = 21600  # 6시간 (승인 완료까지 대기 후 재시도)


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
    _pc_site_batch_size.pop(dev, None)
    _pc_bg_transmit_tasks.pop(dev, None)


def any_pc_running() -> bool:
    """어떤 PC라도 오토튠 실행 중이면 True."""
    return any(ev.is_set() for ev in _pc_running.values())


# 오토튠 필터 설정 키 (samba_settings)
AUTOTUNE_FILTER_SOURCES_KEY = "autotune_enabled_sources"
AUTOTUNE_FILTER_MARKETS_KEY = "autotune_enabled_markets"

# autotune_get_filters available_sources/markets 캐시 (2026-05-24).
# registered 78k 스캔이 distinct(21초)+registered_accounts 전체 fetch(80초)=~100초라
# 매 호출 시 프론트 fetch 무한대기 → 그동안 availSources 빈 채 → 소싱처/판매처
# 체크박스가 화면에서 통째로 사라지던 버그. available_* 는 거의 안 변하므로 TTL 캐시.
# Lock 으로 동시 cache-miss 시 중복 무거운 쿼리 차단(read 풀 고갈 방지).
_FILTERS_AVAIL_TTL = 600.0  # 10분
_filters_avail_cache: dict = {}  # {"sources":[...], "markets":[...], "ts":float}
_filters_avail_lock = asyncio.Lock()

# 오토튠 전송 글로벌 동시실행 제한 — refresher가 fire-and-forget으로 띄운 transmit task가
# OOM 일으키지 않도록 상한. 너무 낮으면 backlog, 너무 높으면 메모리 폭주.
# 정책 변경 직후 폭주 시 backlog는 이벤트 루프가 자연스럽게 흡수 (백프레셔).
_AUTOTUNE_TRANSMIT_MAX_CONCURRENCY = int(
    os.environ.get("AUTOTUNE_TRANSMIT_MAX_CONCURRENCY", "3")
)
# 사이트별 transmit 슬롯 — 사이트 특성 (가격변동 빈도/마켓 응답 속도) 반영.
# 무신사 = 6 (대용량 + 잦은 변동), GSShop = 1 (정책 변동 적음 + 보수적),
# 나머지 = 3 (default).
SITE_TRANSMIT_CONCURRENCY: dict[str, int] = {
    "MUSINSA": 8,
    "GSShop": 1,
}
# PC × 사이트별 transmit 세마포어 — (device_id, site) 단위 분리.
# 한 PC 의 한 사이트 transmit 점유가 다른 사이트/다른 PC 영향 X.
# 옛 글로벌 단일 세마포어 = MUSINSA transmit 점유 시 ABC cycle blocked 사고
# (2026-05-26 ABC starvation).
_autotune_transmit_sems: dict[tuple[str, str], asyncio.Semaphore] = {}


def _market_display_price(price: int, market_type: str, extra_fee_rate: float) -> int:
    """오토튠 로그/타임라인 표시 전용 — 실제 마켓 등록가 재현.

    SSG·롯데홈쇼핑은 전송 시 플러그인(ssg.py / lottehome.py execute)이 sale_price에
    추가수수료율을 역산해 올림한다. 변동감지용 calc_market_price에는 이 역산이 없어
    로그가 추가수수료만큼 낮게 찍히므로, 표시값만 보정한다.
    detection·last_sent 값은 base(역산 전) 그대로 유지 — flip-flop 방지.
    원본 라운딩: ssg.py:146-148(100원 올림), lottehome.py:95-99(10원 올림).
    """
    import math as _math

    p = int(price or 0)
    if p <= 0:
        return p
    r = float(extra_fee_rate or 0)
    if market_type == "ssg":
        if r > 0:
            p = _math.ceil(p / (1 - r / 100))
        return _math.ceil(p / 100) * 100
    if market_type == "lottehome":
        if 0 < r < 100:
            p = _math.ceil(p / (1 - r / 100))
        if p % 10 != 0:
            p = (p // 10 + 1) * 10
        return p
    return p


def _get_transmit_sem(device_id: str = "", site: str = "") -> asyncio.Semaphore:
    """PC × 사이트별 세마포어 lazy init — 사이트별 cap 적용."""
    _site = (site or "").strip()
    key = ((device_id or "").strip() or "_default", _site or "_any")
    sem = _autotune_transmit_sems.get(key)
    if sem is None:
        cap = SITE_TRANSMIT_CONCURRENCY.get(_site, _AUTOTUNE_TRANSMIT_MAX_CONCURRENCY)
        sem = asyncio.Semaphore(cap)
        _autotune_transmit_sems[key] = sem
    return sem


async def _run_transmit_in_background(coro_factory, site: str = ""):
    """fire-and-forget으로 전송 실행 — PC × 사이트별 세마포어로 동시 실행 제한.

    coro_factory: 호출 시 코루틴을 반환하는 callable.
    site: 발행 사이트명 (PC × 사이트 슬롯 분리용).
    예외는 로그로만 남김 (refresher 본 흐름에 영향 없음).
    """
    from backend.domain.samba.collector.refresher import is_bulk_cancelled

    # 현재 사이클 publisher device_id 컨텍스트 — _site_autotune_loop 에서 set 됨.
    try:
        _dev = current_pc_owner.get()
    except LookupError:
        _dev = ""
    sem = _get_transmit_sem(_dev, site)
    async with sem:
        if is_bulk_cancelled("transmit"):
            return
        # 세마포어 대기 중 stop 눌렸으면 대기 잡도 진입 차단 — 정지 후 잔여 transmit 방지.
        if _dev and not _is_pc_running(_dev):
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

# Gunicorn 다중 worker 환경에서 in-memory dict 는 worker 마다 별도.
# lifecycle background task 가 매 10초 sync_pc_allowed_sites_from_db 호출 → 모든
# worker 의 _pc_allowed_sites 가 DB 진실 출처와 일치.


async def sync_pc_allowed_sites_from_db() -> None:
    """DB autotune_pc_allowed_sites 를 in-memory _pc_allowed_sites 로 동기화.

    Gunicorn 다중 worker 환경에서 UI POST 가 1개 worker 만 갱신해 다른 worker 가
    잡 발행 시 stale → "데몬 미등록 skip" 발생. lifecycle background task 가 10초마다
    호출해 모든 worker 의 in-memory 를 DB 진실 출처와 일치시킴.

    last_seen 은 보존 — 데몬/확장앱 폴링 시점 그대로. 새 device 는 last_seen=now 박음.
    """
    try:
        from backend.db.orm import get_read_session
        from backend.api.v1.routers.samba.proxy._helpers import _get_setting

        async with get_read_session() as _sess:
            data = await _get_setting(_sess, "autotune_pc_allowed_sites")
        if not isinstance(data, dict):
            return
        now = time.time()
        seen: set[str] = set()
        for dev, sites in data.items():
            if not isinstance(dev, str) or not isinstance(sites, list):
                continue
            dev_clean = dev.strip()
            if not dev_clean:
                continue
            new_set = {s.strip() for s in sites if isinstance(s, str) and s.strip()}
            # GrandStage strip — ABCmart 통합 사이트 (2026-05-27).
            new_set.discard("GrandStage")
            _pc_allowed_sites[dev_clean] = new_set
            if dev_clean not in _pc_last_seen:
                _pc_last_seen[dev_clean] = now
            seen.add(dev_clean)
        # DB 에 없는 device 정리 — UI 에서 사용자가 토글 off 한 분담
        for dev in list(_pc_allowed_sites.keys()):
            if dev not in seen:
                _pc_allowed_sites.pop(dev, None)
                _pc_last_seen.pop(dev, None)
    except Exception as _exc:
        logging.getLogger("autotune").warning(
            f"[sync_pc_allowed_sites_from_db] sync 실패(무시): {_exc}"
        )


# deviceId당 최근 폴링된 사이트셋 이력 — {device_id: {frozenset(sites): last_ts}}.
# 같은 deviceId 를 여러 PC(예: 프로필 복사/시드로 deviceId 중복)가 서로 다른
# X-Allowed-Sites 로 폴링하면 register 가 매 폴링 last-write 로 덮어써 active_sites 가
# flip-flop(예: [LOTTEON]↔[ABCmart,SSG...]) → 일부 소싱처가 계속 탈락하던 문제.
# deviceId 단위로 TTL 내 폴링된 사이트셋을 합집합(union)해 안정화한다.
# (서로 다른 deviceId 간 PC분담은 영향 없음 — 같은 deviceId 충돌만 합쳐짐.)
_pc_site_history: dict[str, dict[frozenset, float]] = {}
_PC_SITE_UNION_TTL = 90.0  # 90초 — 폴링 끊긴 사이트셋은 만료돼 자연 축소
# 다음 폴링 시 해당 PC에게만 forceStop 신호를 전달할 집합 (개별 중지용)
_pc_force_stop_set: set[str] = set()
# 사용자 의도(전역 enabled) 인메모리 미러 — DB autotune_enabled 와 동기.
# autotune_status 가 매 폴링마다 DB 안 읽도록 미러. 단일 작성처 _save_autotune_state +
# auto_start_if_enabled 에서 갱신. 프론트 자동재합류가 "사용자 정지(enabled=False)"를
# 구분해, 정지를 무시하고 60초마다 재시작하던 루프를 막는 데 쓴다.
_autotune_enabled_flag: bool = False


def update_pc_last_seen(device_id: str) -> None:
    """확장앱 폴링 도착 시 호출 — 해당 PC가 살아있다는 표시."""
    if device_id:
        _pc_last_seen[device_id.strip()] = time.time()


def touch_daemon_presence(device_id: str) -> bool:
    """데몬 폴링/concurrency 조회 도착 시 호출.

    last_seen 갱신 + 미등록 데몬은 빈 분담([])으로 1회 등록해 UI '연결된 데몬' 목록에
    뜨게 한다. 이미 등록(UI 지정)된 데몬의 사이트는 건드리지 않는다 — 데몬 헤더가
    배정을 부풀리지 못하게 함(union 스킵). 신규 등록 발생 시 True 반환(DB 영속화용).
    """
    dev = (device_id or "").strip()
    if not dev:
        return False
    _pc_last_seen[dev] = time.time()
    if dev not in _pc_allowed_sites:
        _pc_allowed_sites[dev] = set()
        _pc_site_history[dev] = {}
        return True
    return False


PC_ALLOWED_SITES_DB_KEY = "autotune_pc_allowed_sites"


def register_pc_allowed_sites(
    device_id: str, sites: list[str] | None, *, authoritative: bool = False
) -> bool:
    """PC 분담 등록/갱신 (UI/폴링용 메타데이터).

    sites=None → 등록 제거
    sites=[] → 빈 분담 (이 PC는 아무 사이트 안 받음)
    sites=[...] → 명시 사이트만 받음

    authoritative=True (UI에서 데몬 사이트 지정 시) → 폴링 union 이력 무시하고
      입력값을 그대로 확정 등록. 데몬은 자기 사이트를 폴링 헤더로 선언하지 않으므로
      (sourcing.collect-queue 가 데몬 device 는 union 등록 스킵) UI 지정값이 유일한
      출처가 되어 체크 해제 시 실제로 사이트가 빠진다(눈가림 아님).
    authoritative=False (확장앱 폴링) → 기존 union 이력 누적(같은 deviceId flip-flop 방지).

    실제 변경이 발생했을 때 True 반환 — 호출자가 DB 영속화 필요 여부 판단용.
    """
    dev = (device_id or "").strip()
    if not dev:
        return False
    if sites is None:
        existed = dev in _pc_allowed_sites or dev in _pc_last_seen
        _pc_allowed_sites.pop(dev, None)
        _pc_last_seen.pop(dev, None)
        _pc_site_history.pop(dev, None)
        return existed
    # 데몬 전용 사이트 strip — 비데몬 dev 분담에 4개 사이트 절대 박히지 않음.
    # 옛 확장앱이 X-Allowed-Sites 헤더에 SSG/ABC/GrandStage/LOTTEON 박아 보내도
    # backend 가 무시. 사용자 룰 (3일 강조) 영구 차단 단일 진실 출처.
    if not dev.startswith("samba-daemon-"):
        from backend.domain.samba.proxy.sourcing_queue import DAEMON_ONLY_SITES

        _block = {s.upper() for s in DAEMON_ONLY_SITES}
        sites = [s for s in sites if (s or "").strip().upper() not in _block]
    # GrandStage 는 abcmart.a-rt.com 의 GRAND STAGE 탭 — backend 수집이 두 탭 통합해
    # source_site='ABCmart' 로 저장. 별도 GrandStage 분담 불필요. 옛 데몬/UI 잔재 stale
    # entry 자동 정리 (2026-05-27, 사용자 정정 "ABC = ABCmart + GrandStage 통합").
    sites = [s for s in sites if (s or "").strip() != "GrandStage"]
    new_set = frozenset(s.strip() for s in sites if s and s.strip())
    now = time.time()
    # authoritative: UI 확정 지정 — union 이력 비우고 입력값 그대로 박는다.
    if authoritative:
        _pc_site_history[dev] = {new_set: now}
        prev = _pc_allowed_sites.get(dev)
        changed = prev != set(new_set)
        _pc_allowed_sites[dev] = set(new_set)
        _pc_last_seen[dev] = now
        return changed
    # deviceId 단위 사이트셋 이력에 기록 + TTL 만료 정리 후 union 산출.
    # 같은 deviceId 를 여러 PC가 다른 사이트셋으로 폴링해도 union 으로 안정화
    # (last-write 덮어쓰기로 인한 active_sites flip-flop 차단).
    hist = _pc_site_history.setdefault(dev, {})
    hist[new_set] = now
    for _fs in [_fs for _fs, _ts in hist.items() if now - _ts > _PC_SITE_UNION_TTL]:
        del hist[_fs]
    union: set[str] = set()
    for _fs in hist:
        union |= _fs
    prev = _pc_allowed_sites.get(dev)
    changed = prev != union
    _pc_allowed_sites[dev] = union
    _pc_last_seen[dev] = now
    return changed


async def persist_pc_allowed_sites(
    session: AsyncSession, device_id: str | None = None
) -> None:
    """PC 분담 dict를 samba_settings 에 저장.

    device_id 지정 (권장): read-modify-write 로 그 dev 1개만 갱신. 다른 데몬 분담 보존.
    device_id=None (legacy): 메모리 전체 snapshot 으로 DB 덮어쓰기 — 콜드 스타트 race
      에서 다른 데몬 분담 0으로 만드는 사고 (2026-05-27) 의 진원지. restore 자가치유 등
      "메모리가 진실 출처임이 확실한 경우"에만 사용.
    """
    _log = logging.getLogger("autotune")
    try:
        from backend.api.v1.routers.samba.proxy._helpers import (
            _set_setting,
        )

        if device_id:
            dev = device_id.strip()
            if not dev:
                return
            # atomic JSONB merge UPSERT — 멀티 worker / 멀티 PC 동시 register race 차단.
            # 옛 read-modify-write (_get_setting → dict 갱신 → _set_setting) 는
            # save_setting (forbidden/service.py:79) 의 자체 commit 으로 트랜잭션 분리되어
            # last-write-wins. advisory_lock 도 connection pool 영향으로 위험.
            # 단일 SQL UPSERT 가 진짜 atomic — race 원천 차단.
            # samba_settings.value 컬럼 타입 = json (jsonb 아님). jsonb 연산 결과를
            # ::json 캐스트로 컬럼 타입 일치 (DatatypeMismatchError 차단).
            # (2026-05-28: PC1 시작 시 1ec58a10 row 증발 + _add_running_device 의 ::text
            # 캐스트 DatatypeMismatchError 사고 둘 다 같은 패턴으로 fix)
            from sqlalchemy import text as _sa_text

            mem = _pc_allowed_sites.get(dev)
            if mem is None:
                # row 제거 — value 에서 dev 키만 삭제
                await session.execute(
                    _sa_text(
                        """
                        UPDATE samba_settings
                        SET value = (COALESCE(value::jsonb, '{}'::jsonb) - CAST(:dev AS text))::json,
                            updated_at = NOW()
                        WHERE key = :k
                        """
                    ),
                    {"k": PC_ALLOWED_SITES_DB_KEY, "dev": dev},
                )
            else:
                sites_list = sorted(mem)
                # SQLAlchemy text + asyncpg: ':name::type' 패턴은 placeholder parser 와
                # PostgreSQL cast 연산자 ':: '충돌로 PostgresSyntaxError. CAST(:name AS type)
                # 형식 강제. (2026-05-28: 옛 :dev::text 패턴이 syntax error 로 silent fail
                # 하던 사고 — try-except 로 잡혀 "DB 저장 실패(무시)" 만 출력)
                await session.execute(
                    _sa_text(
                        """
                        INSERT INTO samba_settings (key, value, updated_at)
                        VALUES (
                            :k,
                            jsonb_build_object(
                                CAST(:dev AS text),
                                to_jsonb(CAST(:sites AS text[]))
                            )::json,
                            NOW()
                        )
                        ON CONFLICT (key) DO UPDATE SET
                            value = (
                                COALESCE(samba_settings.value::jsonb, '{}'::jsonb)
                                || jsonb_build_object(
                                    CAST(:dev AS text),
                                    to_jsonb(CAST(:sites AS text[]))
                                )
                            )::json,
                            updated_at = NOW()
                        """
                    ),
                    {
                        "k": PC_ALLOWED_SITES_DB_KEY,
                        "dev": dev,
                        "sites": sites_list,
                    },
                )
            return
        snapshot = {dev: sorted(sites) for dev, sites in _pc_allowed_sites.items()}
        await _set_setting(session, PC_ALLOWED_SITES_DB_KEY, snapshot)
    except Exception as _e:
        _log.warning("[오토튠] PC 분담 DB 저장 실패(무시): %s", _e)


async def restore_pc_allowed_sites_from_db() -> int:
    """서버 시작 시 samba_settings에서 PC 분담 dict 복원. 복원 건수 반환.

    last_seen은 복원하지 않음 — 24h TTL이 의미를 잃음. 첫 폴링 도착 시 갱신.
    그러나 분담 매핑 자체는 `get_pc_allowed_sites()`에서 사용되므로 복원 효과 충분.

    자가치유 (2026-05-25): 옛 DB snapshot 에 비데몬 dev 분담에 4개 사이트
    (SSG/ABCmart/GrandStage/LOTTEON) 박혀있으면 strip + 1회 재기록. 다음 부팅엔
    안 떠야 정상. register_pc_allowed_sites 진입 가드와 짝.
    """
    _log = logging.getLogger("autotune")
    try:
        from backend.db.orm import get_read_session
        from backend.api.v1.routers.samba.proxy._helpers import _get_setting
        from backend.domain.samba.proxy.sourcing_queue import DAEMON_ONLY_SITES

        async with get_read_session() as _sess:
            data = await _get_setting(_sess, PC_ALLOWED_SITES_DB_KEY)
        if not isinstance(data, dict):
            return 0
        count = 0
        dirty = False
        now = time.time()
        _block = {s.upper() for s in DAEMON_ONLY_SITES}
        for dev, sites in data.items():
            if not isinstance(dev, str) or not isinstance(sites, list):
                continue
            dev_clean = dev.strip()
            if not dev_clean:
                continue
            raw_set = {s.strip() for s in sites if isinstance(s, str) and s.strip()}
            if not dev_clean.startswith("samba-daemon-"):
                stripped = {s for s in raw_set if s.upper() in _block}
                if stripped:
                    _log.warning(
                        "[오토튠] PC 분담 복원 시 데몬전용 사이트 strip: "
                        "dev=%s removed=%s",
                        dev_clean,
                        sorted(stripped),
                    )
                    dirty = True
                    raw_set -= stripped
            # GrandStage strip — abcmart.a-rt.com 통합 사이트 (ABCmart 가 두 탭 모두 처리).
            # 옛 분담 DB stale entry 자동 정리 (2026-05-27).
            if "GrandStage" in raw_set:
                _log.warning(
                    "[오토튠] PC 분담 복원 시 GrandStage strip "
                    "(ABCmart 통합 사이트): dev=%s",
                    dev_clean,
                )
                dirty = True
                raw_set.discard("GrandStage")
            _pc_allowed_sites[dev_clean] = raw_set
            # last_seen 복원 안 함 — 실제 폴링 전 "살아있음" 오판 방지
            count += 1
        if count:
            _log.info(
                "[오토튠] PC 분담 복원: %d PCs (devices=%s)",
                count,
                sorted(_pc_allowed_sites.keys()),
            )
        # 자가치유 — DB snapshot 더러우면 1회 재기록
        if dirty:
            try:
                from backend.db.orm import get_write_session

                async with get_write_session() as _ws:
                    await persist_pc_allowed_sites(_ws)
                    await _ws.commit()
                _log.info(
                    "[오토튠] DB snapshot 자가치유 완료 — 데몬전용 사이트 제거 후 재저장"
                )
            except Exception as _e2:
                _log.warning("[오토튠] 자가치유 재저장 실패(무시): %s", _e2)
        return count
    except Exception as _e:
        _log.warning("[오토튠] PC 분담 복원 실패(무시): %s", _e)
        return 0


def get_active_pcs() -> dict[str, set[str]]:
    """stale PC 정리 후 살아있는 PC들의 분담 매핑 반환."""
    now = time.time()
    stale = [d for d, ts in _pc_last_seen.items() if now - ts > PC_LAST_SEEN_TTL]
    for d in stale:
        _pc_last_seen.pop(d, None)
        _pc_allowed_sites.pop(d, None)
        _pc_site_history.pop(d, None)
    return {d: sites for d, sites in _pc_allowed_sites.items() if d in _pc_last_seen}


def get_pc_allowed_sites(device_id: str) -> set[str] | None:
    """해당 PC가 처리할 사이트 집합. 미등록이면 None(=전체)."""
    dev = (device_id or "").strip()
    if not dev:
        return None
    pcs = get_active_pcs()
    return pcs.get(dev)


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


def _musinsa_auth_lost_recent(device_id: str) -> bool:
    """무신사 쿠키 손실이 재확인 인터벌(5분) 내에 감지됐는지.

    True면 무신사 사이클을 스킵한다. 인터벌 경과 시 False를 반환해 1회
    프로브 사이클을 허용하고, 쿠키가 여전히 없으면 _on_result가 재기록한다.
    """
    ts = _musinsa_auth_lost_at.get(device_id, 0.0)
    if not ts:
        return False
    return (time.time() - ts) < _MUSINSA_AUTH_RECHECK_SEC


async def _mark_musinsa_auth_lost(device_id: str) -> None:
    """무신사 쿠키 손실 기록 + 6시간 쿨다운으로 경고 이벤트 1회 발행."""
    now_ts = time.time()
    _musinsa_auth_lost_at[device_id] = now_ts
    last_alert = _musinsa_auth_alerted_at.get(device_id, 0.0)
    if now_ts - last_alert >= _MUSINSA_AUTH_ALERT_COOLDOWN_SEC:
        _musinsa_auth_alerted_at[device_id] = now_ts
        try:
            await _stream_event(
                "cookie_lost",
                "critical",
                summary="무신사 로그인 만료 — 오토튠 무신사 갱신 중단. 무신사 재로그인 필요.",
                source_site="MUSINSA",
                detail={"device_id": device_id, "reason": "cookie_expired"},
            )
        except Exception:
            pass


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

                # 무신사 자동로그인 쿠키 손실 — 사이클 스킵(빈 쿠키로 헛도는 갱신 +
                # 상품마다 읽기세션 폭주 방지). 재확인 인터벌(5분) 경과 시 1회 프로브
                # 사이클을 허용해 재로그인(쿠키 복구)을 자동 감지한다.
                if site == "MUSINSA" and _musinsa_auth_lost_recent(device_id):
                    log.info(
                        "[오토튠][MUSINSA] 쿠키 손실 감지 — 사이클 스킵(대기). 재로그인 시 자동 재개"
                    )
                    await asyncio.sleep(30)
                    continue

                # 데몬 전용 사이트(SSG/ABC/GrandStage/LOTTEON)는 살아있는 데몬이 없으면
                # 잡 발행이 전건 "데몬 미등록"으로 즉시 실패 → 진행 카운터 헛증가 + 로그 폭주.
                # 살아있는 데몬 없으면 SELECT/배치/발행 전부 스킵하고 대기. 데몬 복구 시 재개.
                # (idx 보존 — 데몬 복귀하면 멈췄던 진행 순번부터 이어감)
                from backend.domain.samba.proxy.sourcing_queue import (
                    DAEMON_ONLY_SITES,
                )

                if site in DAEMON_ONLY_SITES:
                    from backend.domain.samba.proxy.daemon_pool import (
                        pick_daemon_owner,
                    )

                    if pick_daemon_owner(site) is None:
                        log.info(
                            "[오토튠][%s] 살아있는 데몬 없음 — 사이클 스킵(대기). 데몬 복구 시 자동 재개",
                            site,
                        )
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

                    # 정렬 안정성 보장 (issue #206) — id를 secondary sort로 두지 않으면
                    # last_refreshed_at NULL 행 수천 개 중 매 cycle 동일 200개만 잡혀
                    # 다른 NULL 행이 영원히 cycle 진입 못 하는 사고 발생.
                    _order_clause = (
                        _CP.last_refreshed_at.asc().nullsfirst(),
                        _CP.id.asc(),
                    )

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
                    # 적응 배치: 직전 배치 소요시간 기반 자동 조정값 우선 (없으면 env 기본)
                    _batch_limit = _pc_bs(device_id).get(site, _AUTOTUNE_CYCLE_BATCH)
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
                        stmt = stmt.limit(_batch_limit)
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
                        _gkey = (device_id, site)
                        _prev_idx = _autotune_global_idx.get(_gkey, 0)
                        _cached_total = _autotune_global_total.get(_gkey, 0)
                        # 사이클 시작 시에만 COUNT 재산정 (모수 freeze) — 사용자 룰 (2026-05-26):
                        # "한 사이클 다 완성되기 전 모수 변경 금지".
                        # 신규 상품/품절은 다음 사이클부터 반영. 사이클 중 batch SELECT 매번
                        # COUNT 하면 [2,403/43,745] [2,415/43,731] 처럼 분모 흔들림 사고.
                        _need_recount = (
                            _prev_idx == 0
                            or _cached_total <= 0
                            or _prev_idx >= _cached_total
                        )
                        if _need_recount:
                            try:
                                _count_stmt = (
                                    select(func.count()).select_from(_CP).where(*_where)
                                )
                                _total_global_res = await session.execute(_count_stmt)
                                _total_global = int(_total_global_res.scalar() or 0)
                            except Exception:
                                _total_global = filtered_count
                        else:
                            _total_global = _cached_total
                        # 한 바퀴 회전 완료(분자 ≥ 분모) 시 0부터 재시작 + 사이클# 증가
                        if _prev_idx >= _total_global or _total_global <= 0:
                            _autotune_global_idx[_gkey] = 0
                            _autotune_cycle_stats[_gkey] = _new_cycle_stats()
                            _autotune_cycle_stats[_gkey]["started_at"] = now.isoformat()
                            # 사이클# = 전체 한 바퀴 완료마다 +1 (2026-05-26 사용자 룰).
                            _scc_done = _pc_scc(device_id)
                            _scc_done[site] = _scc_done.get(site, 0) + 1
                        elif _gkey not in _autotune_cycle_stats:
                            _autotune_cycle_stats[_gkey] = _new_cycle_stats()
                            _autotune_cycle_stats[_gkey]["started_at"] = now.isoformat()
                        # idx dict 시드 — 비어 있으면 refresher가 빈 dict로 오판해 순번이
                        # 1에 갇힌다(aa3beeb7에서 시드해주던 리셋 조건이 빠진 뒤 노출된 버그).
                        _autotune_global_idx.setdefault(_gkey, 0)
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
                        from backend.domain.samba.shipment.dispatcher import (
                            delete_from_market,
                        )
                        from backend.domain.samba.emergency import is_emergency_stopped

                        product_map: dict[str, object] = {p.id: p for p in products}
                        _policy_cache: dict[str, object] = {}
                        _account_cache: dict[str, object] = {}
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

                        # SSG 신상품 MD 승인 상태 일괄 확인
                        # TODO: SSG API 엔드포인트 확인 후 활성화 (현재 비활성)
                        # _ssg_acc_map 로직은 service.py/_save_autotune_state 참고

                        # SELECT 완료 후 즉시 커밋 + 연결 반납 — refresh HTTP 동안 idle in transaction 방지
                        # expire_on_commit=False이므로 products/_account_cache/_policy_cache 객체는 커밋 후에도 유효
                        # session.close()는 연결만 풀에 반납, 세션 객체는 재사용 가능 (soldout 재시도 블록에서 재획득)
                        await session.commit()
                        await session.close()

                        retransmitted = 0
                        deleted_count = 0
                        # 품절 unique 상품 수 — 1 상품 N 마켓삭제해도 +1 (2026-05-26 사용자 룰)
                        _all_delete_pids: set[str] = set()
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
                            # MUSINSA 인터벌 표시 — 차단 시 인터벌 증가 추적 (2026-05-26 사용자 요청).
                            # msg 가 호출자에서 이미 [MUSINSA] site tag 포함 → 그 직후에 [int=X.Xs] 삽입.
                            if site == "MUSINSA":
                                try:
                                    _cur_int = _ref_mod._site_intervals.get(
                                        "MUSINSA", 1.0
                                    )
                                    _tag = f" [int={_cur_int:.1f}s]"
                                    if "[MUSINSA]" in msg:
                                        msg = msg.replace(
                                            "[MUSINSA]", f"[MUSINSA]{_tag}", 1
                                        )
                                    else:
                                        msg = f"{_tag} {msg}"
                                except Exception:
                                    pass
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

                        async def _atomic_merge_lsd(pid: str, updates: dict):
                            """last_sent_data 특정 계정들만 atomic JSONB merge.

                            전체 snapshot 덮어쓰기 대신 계정별 부분 갱신으로
                            동시 실행 그룹 간 race condition 방지.
                            json 컬럼이므로 CAST AS jsonb 후 || 연산자 적용 → ::json 저장.
                            """
                            import json as _alm_j  # noqa: F811
                            from sqlalchemy import text as _alm_text  # noqa: F811
                            from backend.db.orm import (
                                get_write_session as _get_alm_session,
                            )

                            _stmt = _alm_text(
                                "UPDATE samba_collected_product"
                                " SET last_sent_data = ("
                                "  CASE WHEN jsonb_typeof(CAST(last_sent_data AS jsonb)) = 'object'"
                                "       THEN CAST(last_sent_data AS jsonb) ELSE '{}'::jsonb END"
                                "  || CAST(:updates AS jsonb))::json,"
                                " updated_at = NOW()"
                                " WHERE id = :pid"
                            )
                            _last_exc_alm: Exception | None = None
                            for _alm_attempt in range(2):
                                try:
                                    async with _get_alm_session() as _alm_s:
                                        await _alm_s.execute(
                                            _stmt,
                                            {
                                                "updates": _alm_j.dumps(updates),
                                                "pid": pid,
                                            },
                                        )
                                        await _alm_s.commit()
                                    return
                                except Exception as _alm_ex:
                                    _last_exc_alm = _alm_ex
                                    if (
                                        _is_stale_conn_error(_alm_ex)
                                        and _alm_attempt == 0
                                    ):
                                        log.warning(
                                            "[오토튠][DB재시도] atomic_merge_lsd %s"
                                            " 좀비 connection → 재시도: %s",
                                            pid,
                                            str(_alm_ex)[:120],
                                        )
                                        await asyncio.sleep(0.1)
                                        continue
                                    raise
                            if _last_exc_alm:
                                raise _last_exc_alm

                        async def _on_result(product, r, idx=0, total=0):
                            """리프레시 직후 호출 — DB 업데이트 + 즉시 마켓 전송."""
                            nonlocal \
                                retransmitted, \
                                deleted_count, \
                                price_changed_count, \
                                _cycle_deleted_pids, \
                                _synced_count, \
                                _lot_verify_count

                            # 무신사 자동로그인 쿠키 손실(refresher MUSINSA_AUTH_MISSING) →
                            # 전송/DB갱신 중단 + 경고 1회. 다음 사이클부터 사이트 루프가 스킵.
                            if getattr(r, "error", None) == "MUSINSA_AUTH_MISSING":
                                await _mark_musinsa_auth_lost(device_id)
                                return
                            # 무신사 정상 응답 = 쿠키 복구 → 손실 플래그 해제(즉시 재개)
                            if (
                                product.source_site == "MUSINSA"
                                and not getattr(r, "error", None)
                                and device_id in _musinsa_auth_lost_at
                            ):
                                _musinsa_auth_lost_at.pop(device_id, None)

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
                                # 진행도 표시 — refresher._limited 가 작업별로 캡처해 넘긴 idx/total 우선 사용.
                                # 전역 dict 재조회는 동시성 N 배치에서 모든 in-flight 작업이
                                # 같은 값을 보게 되어 금지 (예: 4건이 모두 [16/40,004] 표시 버그).
                                if idx and total:
                                    _idx_prefix = f"[{idx:,}/{total:,}] "
                                else:
                                    _g_idx = _autotune_global_idx.get(_gkey, 0)
                                    _g_total = _autotune_global_total.get(_gkey, 0)
                                    _idx_prefix = (
                                        f"[{_g_idx:,}/{_g_total:,}] "
                                        if _g_idx and _g_total
                                        else ""
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
                                # 보유 적립금 제외 cost (무신사 토글용)
                                if r.new_cost_excl_held_point is not None:
                                    updates["cost_excl_held_point"] = (
                                        r.new_cost_excl_held_point
                                    )
                                elif r.new_cost is not None:
                                    updates["cost_excl_held_point"] = r.new_cost
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
                                # 가격불확실(price_uncertain=True) 시 sale_status 덮어쓰기 보류.
                                # 플러그인이 옵션 stock 만 보고 'in_stock' 박으면, 사용자가 수동 정리한
                                # sold_out 이 한 사이클만에 in_stock 으로 회귀되는 사고 방지.
                                if not r.price_uncertain:
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
                                            elif _del_acc.market_type in (
                                                "gmarket",
                                                "auction",
                                            ):
                                                # ESM 삭제 API는 마스터 goodsNo 필요 — _master 우선
                                                pno = m_nos.get(
                                                    f"{_del_acc_id}_master"
                                                ) or m_nos.get(_del_acc_id, "")
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
                                                    _all_delete_pids.add(r.product_id)
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

                                # 인메모리 product 객체도 갱신 — _partial_update 는 DB만 UPDATE 하므로
                                # product.cost 등이 옛값으로 남는다. 아래 expected_price 계산이
                                # resolve_cost_for_policy(product) → product.cost 를 읽으므로, 갱신 안 하면
                                # 새 원가가 무시돼 expected==last → 원가만 바뀐 상품이 전송 스킵되는 버그.
                                for _k in (
                                    "cost",
                                    "cost_excl_held_point",
                                    "is_point_restricted",
                                ):
                                    if _k in updates:
                                        try:
                                            setattr(product, _k, updates[_k])
                                        except Exception:
                                            pass

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
                                        # 토글 excludeHeldPoint=True 이면 보유적립금 제외 cost 사용
                                        from backend.domain.samba.shipment.service import (
                                            resolve_cost_for_policy,
                                        )

                                        _resolved_cost = resolve_cost_for_policy(
                                            product, policy.pricing, site
                                        )
                                        _cost_for_calc = _resolved_cost or new_cost
                                        cost_info = await convert_cost_by_source_site(
                                            session,
                                            _cost_for_calc,
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
                                    # preemptive failed_at (transmit 큐 진입 시 박힘) 이
                                    # transmit 성공 후 자동 제거되기 전 다음 사이클이 보면
                                    # 가격 동일이라도 재시도 트리거 → write pool 압박 +
                                    # 무신사 처리속도 0.7→4초 사고 (2026-05-27).
                                    # failed_at 가 마지막 sent_at 보다 최신일 때만 진짜
                                    # 실패로 인정. transmit 성공 후 sent_at 갱신되면
                                    # failed_at < sent_at 자동 → _has_failed_mark=False.
                                    if _has_failed_mark and acc_last:
                                        _sent_at_str = acc_last.get("sent_at") or ""
                                        _failed_at_str = acc_last.get("failed_at") or ""
                                        if _sent_at_str and _failed_at_str:
                                            _has_failed_mark = (
                                                _failed_at_str > _sent_at_str
                                            )

                                    # orphan preemptive 만료 [2026-06-04]:
                                    # preemptive failed_at 는 fire-and-forget transmit task 가
                                    # 배포 재시작/cancel/SIGTERM 로 사라지면 영구히 안 떼짐 →
                                    # 매 사이클 재시도 → 전 사이트 5.5만건 백로그(MUSINSA 71%) 적체.
                                    # failure_count==0(=실패경로 미경유, 순수 preemptive)이고
                                    # failed_at 가 grace(30분) 넘게 묵었으면 = task 유실 확정 →
                                    # stale 판정해 무시(무한 재시도 차단). 실제 키 제거는 전송
                                    # 성공 덮어쓰기/일회성 청소에 위임(hot loop DB write 금지).
                                    # fc>=1 진짜 실패는 만료 안 함(재시도 유지).
                                    if _has_failed_mark and acc_last:
                                        _fc_mark = int(
                                            acc_last.get("failure_count") or 0
                                        )
                                        if _fc_mark == 0:
                                            try:
                                                _fa_dt = datetime.fromisoformat(
                                                    str(acc_last.get("failed_at") or "")
                                                )
                                                if _fa_dt.tzinfo is None:
                                                    _fa_dt = _fa_dt.replace(
                                                        tzinfo=timezone.utc
                                                    )
                                                if (
                                                    datetime.now(timezone.utc) - _fa_dt
                                                ).total_seconds() > 1800:
                                                    _has_failed_mark = False
                                            except Exception:
                                                pass

                                    # 초기 cost 교정 — 이전 전송 cost가 정가 폴백(과거 추출실패 잔재)인 경우
                                    # 새로 정확한 혜택가가 들어와도 "가격변동"으로 인식되어 전송되는 사고 차단.
                                    # 조건: 이전 acc_last.cost == product.original_price (정가 폴백 의심)
                                    # AND 새 cost가 정가보다 작음 (혜택가 교정)
                                    # 처리: 전송 스킵 + last_sent_data.cost만 silent 갱신 (다음 사이클부터 정상)
                                    _last_cost_pre = (
                                        int(acc_last.get("cost", 0) or 0)
                                        if acc_last
                                        else 0
                                    )
                                    _orig_p_int = int(
                                        getattr(product, "original_price", 0) or 0
                                    )
                                    _is_initial_cost_correction = (
                                        site in ("LOTTEON", "MUSINSA", "SSG")
                                        and _last_cost_pre > 0
                                        and _orig_p_int > 0
                                        and _last_cost_pre == _orig_p_int
                                        and int(new_cost) > 0
                                        and int(new_cost) < _last_cost_pre
                                        and not _has_failed_mark
                                    )
                                    if (
                                        _is_initial_cost_correction
                                        and expected_price != last_price
                                    ):
                                        _nontx_actions.append(
                                            f"[초기cost·{acc_label}] 이전 cost={_last_cost_pre:,}(정가폴백) "
                                            f"→ 새 cost={int(new_cost):,} 내부 보정만 (마켓 전송 없음)"
                                        )
                                        log.info(
                                            "[오토튠][초기cost] %s acc=%s: "
                                            "이전 cost=%s(=정가) → 새 cost=%s 교정 → 전송 스킵",
                                            _prod_label,
                                            acc_id[:20],
                                            _last_cost_pre,
                                            int(new_cost),
                                        )
                                        # last_sent_data.cost만 silent 갱신 (다음 사이클부터 정상 비교)
                                        _new_acc_last = dict(acc_last)
                                        _new_acc_last["cost"] = int(new_cost)
                                        _new_last_sent = dict(last_sent)
                                        _new_last_sent[acc_id] = _new_acc_last
                                        await _partial_update(
                                            r.product_id,
                                            {"last_sent_data": _new_last_sent},
                                        )
                                        continue

                                    if (
                                        expected_price != last_price or _has_failed_mark
                                    ) and not _price_blocked:
                                        price_changed_count += 1
                                        _all_price_pids.add(r.product_id)
                                        # 표시 전용: 실제 마켓 등록가(추가수수료 역산 반영).
                                        # detection/last_sent은 base값(expected_price/last_price) 유지.
                                        _af = getattr(acc, "additional_fields", None)
                                        _efr = 0.0
                                        if isinstance(_af, dict):
                                            try:
                                                _efr = float(
                                                    _af.get("extraFeeRate") or 0
                                                )
                                            except (TypeError, ValueError):
                                                _efr = 0.0
                                        _disp_old = _market_display_price(
                                            last_price, market_type, _efr
                                        )
                                        _disp_new = _market_display_price(
                                            expected_price, market_type, _efr
                                        )
                                        if len(_price_tx_items) < 10:
                                            _price_tx_items.append(
                                                {
                                                    "pid": r.product_id,
                                                    "site_product_id": product.site_product_id,
                                                    "name": (product.name or "")[:40],
                                                    "old_price": _disp_old,
                                                    "new_price": _disp_new,
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
                                        _price_action_txt = f"가격변동{_reason_lbl} {_disp_old:,}→{_disp_new:,} → {acc_label}"
                                        _acc_items.append("price")
                                        _acc_action_parts.append(_price_action_txt)
                                        # 워룸 타임라인용 이벤트 수집 — 등록마켓 판매가 변경 기준
                                        # (오토튠의 실제 전송 트리거와 동일 기준, 상품당 1건)
                                        if r.product_id not in _price_change_events:
                                            _diff_pct = (
                                                round(
                                                    (_disp_new - _disp_old)
                                                    / _disp_old
                                                    * 100,
                                                    1,
                                                )
                                                if _disp_old
                                                else 0
                                            )
                                            _price_change_events[r.product_id] = {
                                                "source_site": product.source_site,
                                                "product_id": r.product_id,
                                                "product_name": product.name,
                                                "site_product_id": product.site_product_id,
                                                "old_price": _disp_old,
                                                "new_price": _disp_new,
                                                "diff_pct": _diff_pct,
                                            }
                                            # 변동 감지 즉시 DB 저장 — 사이클 미완주에도 유실 없음
                                            _name_short = (product.name or "")[:30]
                                            await _stream_event(
                                                "price_changed",
                                                "info",
                                                summary=f"가격 변동 — {_name_short} ₩{int(_disp_old):,}→₩{int(_disp_new):,}",
                                                source_site=product.source_site,
                                                product_id=r.product_id,
                                                product_name=product.name,
                                                detail={
                                                    "old_price": _disp_old,
                                                    "new_price": _disp_new,
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
                                    # 품절 안전 재확인: boolean flip 안 걸려도 품절 옵션 + STALE 이면 강제 재전송 (#400)
                                    # last_sent=0 기록 후 마켓 미반영(504/미반영) 시 영구 블라인드 방지
                                    if (
                                        not _stock_diff
                                        and _api_opts
                                        and _is_send_stale(
                                            (acc_last or {}).get("sent_at"),
                                            SOLDOUT_REASSERT_SEC,
                                        )
                                        and any(
                                            (o.get("stock", 0) or 0) <= 0
                                            for o in _api_opts
                                        )
                                    ):
                                        _stock_diff = True
                                        _stock_changes_acc += 1
                                    if _stock_diff:
                                        _all_stock_pids.add(r.product_id)
                                        _stock_action_txt = f"재고전송({_stock_changes_acc}건) → {acc_label}"
                                        _acc_items.append("stock")
                                        _acc_action_parts.append(_stock_action_txt)

                                    # SSG 판매상태 체크 (sellStatCd 기준)
                                    # 10=승인대기 → 전송 스킵
                                    # 05=정보추가필요(반려) → 변동 없어도 강제 재신청
                                    # 20=판매중 → 기존 로직대로
                                    if market_type == "ssg":
                                        _ssg_item_id = _m_nos.get(acc_id, "")
                                        if _ssg_item_id:
                                            try:
                                                from backend.domain.samba.proxy.ssg import (
                                                    SSGClient as _SSGClient,
                                                )
                                                import json as _json

                                                _ssg_af = (
                                                    getattr(
                                                        acc, "additional_fields", None
                                                    )
                                                    or {}
                                                )
                                                if isinstance(_ssg_af, str):
                                                    _ssg_af = _json.loads(_ssg_af)
                                                _ssg_api_key = _ssg_af.get("apiKey", "")
                                                if _ssg_api_key:
                                                    _ssg_cli = _SSGClient(_ssg_api_key)
                                                    _sales_resp = await _ssg_cli.get_item_sales_status(
                                                        _ssg_item_id
                                                    )
                                                    _sell_stat = str(
                                                        _sales_resp.get("result", {})
                                                        .get("salesStatus", {})
                                                        .get("sellStatCd", "")
                                                        or ""
                                                    )
                                                    if (
                                                        _sell_stat == "10"
                                                    ):  # 승인대기 → 스킵
                                                        log.info(
                                                            "[오토튠] %s → SSG 승인대기 중, 전송 스킵",
                                                            product.id,
                                                        )
                                                        _acc_items.clear()
                                                    elif (
                                                        _sell_stat == "05"
                                                    ):  # 반려 → 강제 재신청
                                                        log.info(
                                                            "[오토튠] %s → SSG 반려 감지(sellStatCd=05), 재신청",
                                                            product.id,
                                                        )
                                                        # 어드민 저장과 동일한 항목으로 전송해야 재심사 트리거됨
                                                        _acc_items[:] = [
                                                            "price",
                                                            "stock",
                                                            "image",
                                                            "description",
                                                        ]
                                                        _acc_action_parts[:] = [
                                                            f"SSG 반려 → 재신청 → {acc_label}"
                                                        ]
                                            except Exception as _ssg_qa_e:
                                                log.warning(
                                                    "[오토튠] %s SSG 판매상태 확인 실패: %s",
                                                    product.id,
                                                    _ssg_qa_e,
                                                )

                                    # 재고잠금 상품 → 가격/재고 전송 스킵
                                    if _acc_items and getattr(
                                        product, "lock_stock", False
                                    ):
                                        _nontx_actions.append("재고잠금 → 전송 스킵")
                                        _acc_items.clear()

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
                                        # preemptive failed_at — fire-and-forget transmit task 가
                                        # is_bulk_cancelled / task cancel / SIGTERM / 예외 등으로
                                        # _dispatch_one 도달 못한 채 사라지면 last_sent_data 갱신 누락 →
                                        # 다음 사이클이 같은 변동 또 인식 → 무한 반복 (LOTTEON 27h 지연,
                                        # MUSINSA 45일 미전송 1,000+ 건 사고, 2026-05-27).
                                        # 전송 성공 시 sent_snapshot 으로 덮어써져 자동 제거됨
                                        # (shipment/service.py:1989). 누락 시 failed_at 살아남아
                                        # 다음 사이클 _has_failed_mark=True 로 재시도 명확.
                                        _pre_acc_last = dict(
                                            last_sent.get(acc_id) or {}
                                        )
                                        _pre_acc_last["failed_at"] = datetime.now(
                                            timezone.utc
                                        ).isoformat()
                                        last_sent[acc_id] = _pre_acc_last

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

                            # preemptive failed_at DB 박기 — transmit task cancel/누락 대비.
                            # 전체 snapshot 덮어쓰기 대신 큐 계정들만 atomic merge
                            # (전체 덮어쓰기 시 이전 사이클 background transmit의 sent_snapshot을 stale 값으로 되돌리는 race 발생)
                            if _transmit_queue:
                                try:
                                    _pre_updates = {}
                                    for (
                                        _pre_pid,
                                        _pre_items,
                                        _pre_acc,
                                        _pre_label,
                                        _pre_action,
                                    ) in _transmit_queue:
                                        _pre_data = dict(last_sent.get(_pre_acc) or {})
                                        _pre_data["failed_at"] = datetime.now(
                                            timezone.utc
                                        ).isoformat()
                                        _pre_updates[_pre_acc] = _pre_data
                                    if _pre_updates:
                                        await _atomic_merge_lsd(
                                            r.product_id, _pre_updates
                                        )
                                except Exception as _pe:
                                    log.warning(
                                        "[오토튠][preemptive] %s last_sent_data 갱신 실패: %s",
                                        r.product_id,
                                        str(_pe)[:120],
                                    )

                            # 계정별 개별 task — items 조합으로 그룹화하지 않음.
                            # 기존 그룹화(_tx_groups)는 여러 계정을 단일 세션에서 순차 처리해
                            # 세션이 계정수×마켓HTTP 동안 점유 → 120s 초과 시 greenlet 에러.
                            # 계정별 분리로 세션 수명 = 계정 1개 전송시간(~40s)으로 단축.
                            for (
                                _tx_pid,
                                _tx_items,
                                _tx_acc,
                                _tx_label,
                                _tx_action_text,
                            ) in _transmit_queue:

                                async def _fire_transmit_account(
                                    _pid=_tx_pid,
                                    _items=_tx_items,
                                    _acc=_tx_acc,
                                    _label=_tx_label,
                                    _action_text=_tx_action_text,
                                    _site=site,
                                    _idx_pfx=_idx_prefix,
                                    _t=_tail,
                                ):
                                    nonlocal _synced_count
                                    _logged = False
                                    try:
                                        _tx_result = None
                                        _tx_exc: Exception | None = None
                                        _TX_RETRY_DELAYS = (0.2, 0.5, 1.5)
                                        for _tx_attempt in range(
                                            len(_TX_RETRY_DELAYS) + 1
                                        ):
                                            try:
                                                async with get_write_session() as _tx_s:
                                                    from backend.domain.samba.shipment.repository import (
                                                        SambaShipmentRepository as _FRepo,
                                                    )
                                                    from backend.domain.samba.shipment.service import (
                                                        SambaShipmentService as _FSvc,
                                                    )

                                                    _svc = _FSvc(_FRepo(_tx_s), _tx_s)
                                                    _tx_result = await _svc.start_update(
                                                        [_pid],
                                                        _items,
                                                        [_acc],
                                                        skip_unchanged=False,
                                                        skip_refresh=True,
                                                        skip_policy_account_filter=True,
                                                    )
                                                    await _tx_s.commit()
                                                _tx_exc = None
                                                # 단일 계정 stale-conn 판정
                                                _stale_in_result = False
                                                for _rr in (
                                                    _tx_result.get("results") or []
                                                ):
                                                    if not isinstance(_rr, dict):
                                                        continue
                                                    _rr_err = (
                                                        _rr.get("transmit_error") or {}
                                                    )
                                                    _eerr = (
                                                        _rr_err.get(_acc)
                                                        if isinstance(_rr_err, dict)
                                                        else None
                                                    )
                                                    if _eerr and _is_stale_conn_error(
                                                        Exception(str(_eerr))
                                                    ):
                                                        _stale_in_result = True
                                                    # product-level 에러(start_update 가 예외를
                                                    # 잡아 transmit_error 없이 error 만 채운 경우 —
                                                    # greenlet 등)도 stale 판정 → 새 세션 재시도.
                                                    _row_err = _rr.get("error")
                                                    if (
                                                        _row_err
                                                        and _is_stale_conn_error(
                                                            Exception(str(_row_err))
                                                        )
                                                    ):
                                                        _stale_in_result = True
                                                    break
                                                if (
                                                    _stale_in_result
                                                    and _tx_attempt
                                                    < len(_TX_RETRY_DELAYS)
                                                ):
                                                    _delay = _TX_RETRY_DELAYS[
                                                        _tx_attempt
                                                    ]
                                                    log.warning(
                                                        "[오토튠][DB재시도] transmit_account"
                                                        " pid=%s acc=%s stale-conn"
                                                        " (시도 %d/%d, %.1fs 대기)",
                                                        _pid,
                                                        _acc[:12],
                                                        _tx_attempt + 1,
                                                        len(_TX_RETRY_DELAYS) + 1,
                                                        _delay,
                                                    )
                                                    await asyncio.sleep(_delay)
                                                    continue
                                                break
                                            except Exception as _try_exc:
                                                _tx_exc = _try_exc
                                                if _is_stale_conn_error(
                                                    _try_exc
                                                ) and _tx_attempt < len(
                                                    _TX_RETRY_DELAYS
                                                ):
                                                    _delay = _TX_RETRY_DELAYS[
                                                        _tx_attempt
                                                    ]
                                                    log.warning(
                                                        "[오토튠][DB재시도] transmit_account"
                                                        " pid=%s acc=%s 좀비/prepared"
                                                        " (시도 %d/%d, %.1fs): %s",
                                                        _pid,
                                                        _acc[:12],
                                                        _tx_attempt + 1,
                                                        len(_TX_RETRY_DELAYS) + 1,
                                                        _delay,
                                                        str(_try_exc)[:80],
                                                    )
                                                    await asyncio.sleep(_delay)
                                                    continue
                                                raise
                                        if _tx_exc:
                                            raise _tx_exc
                                        if _tx_result is None:
                                            _tx_result = {"results": []}
                                        # 결과 판정
                                        _tx_row = next(
                                            (
                                                r
                                                for r in (
                                                    _tx_result.get("results") or []
                                                )
                                                if isinstance(r, dict)
                                            ),
                                            None,
                                        )
                                        _acc_err = (
                                            (
                                                (
                                                    _tx_row.get("transmit_error") or {}
                                                ).get(_acc)
                                            )
                                            if _tx_row
                                            else None
                                        )
                                        _acc_status = (
                                            (
                                                (
                                                    _tx_row.get("transmit_result") or {}
                                                ).get(_acc)
                                            )
                                            if _tx_row
                                            else None
                                        )
                                        _row_status = (
                                            _tx_row.get("status") if _tx_row else None
                                        )
                                        # product-level 에러(예: start_update 가 예외를 잡아
                                        # {status:"failed", error:...} 만 반환 — transmit_result/
                                        # transmit_error 비어있음) 도 실패로 인정.
                                        # 이 가드 없으면 _acc_status=None 이 성공으로 오판돼
                                        # 실제 전송 실패가 "전송완료" 로 거짓 로깅됨(greenlet 사고).
                                        _row_error = (
                                            _tx_row.get("error") if _tx_row else None
                                        )
                                        # _tx_row 자체가 없으면(결과 0건) 성공 근거 없음 → 실패.
                                        _acc_ok = (
                                            _tx_row is not None
                                            and not _acc_err
                                            and not _row_error
                                            and _row_status != "failed"
                                            and (
                                                _acc_status
                                                in (None, "success", "completed")
                                                or _row_status
                                                in ("success", "completed")
                                            )
                                        )
                                        _acc_was_deleted = False
                                        if _tx_row:
                                            _u = (
                                                _tx_row.get("update_result") or {}
                                            ).get(_acc)
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
                                        if _acc_ok:
                                            _synced_count += 1
                                            _logged = True
                                            # 성공 완료 로그는 dispatch 로그(전송 시작 시점)로
                                            # 갈음 — 늦은 완료 로그가 스킵을 밀어내던 문제 해소.
                                            # 단 마켓삭제(품절)는 dispatch 예고("전송")와 결과가
                                            # 달라 별도 출력 유지.
                                            if _acc_was_deleted:
                                                _log_line(
                                                    _site,
                                                    _pid,
                                                    f"{_idx_pfx}{_label}: {_action_text} → 마켓삭제(품절){_t}",
                                                )
                                        else:
                                            # 실제 에러가 transmit_error['_all'](상품단위 —
                                            # 예: "전송 300초 타임아웃", "이미 전송 중")에 저장되는
                                            # 경우가 있어 계정키 조회만으론 None → 과거 "결과없음"
                                            # 오표시. _all 및 임의 비어있지 않은 transmit_error 값을
                                            # 폴백으로 읽어 진짜 사유를 노출.
                                            _all_err = None
                                            if isinstance(_tx_row, dict):
                                                _te = (
                                                    _tx_row.get("transmit_error") or {}
                                                )
                                                if isinstance(_te, dict):
                                                    _all_err = _te.get("_all") or next(
                                                        (v for v in _te.values() if v),
                                                        None,
                                                    )
                                            _fail_msg = str(
                                                _acc_err
                                                or _row_error
                                                or _all_err
                                                or "결과없음"
                                            )[:200]
                                            _log_line(
                                                _site,
                                                _pid,
                                                f"{_idx_pfx}{_label}: {_action_text} 전송실패(검증): {_fail_msg}{_t}",
                                                "error",
                                            )
                                            _logged = True
                                    except Exception as _fe:
                                        if not _logged:
                                            _log_line(
                                                _site,
                                                _pid,
                                                f"{_idx_pfx}{_label}: {_action_text} 전송실패: {str(_fe)[:200]}{_t}",
                                                "error",
                                            )
                                    await asyncio.sleep(0.3)

                                # 전송 시작(dispatch) 즉시 로그 — 진행순서 정렬용.
                                # background 완료 로그는 전송이 끝나야 찍혀 늦고 순서가
                                # 뒤섞임 → 화면 30줄을 점령해 스킵 로그를 밀어냄
                                # (진행 200인데 스킵 0줄 현상). 평가 순서대로 여기서 찍어
                                # 스킵 로그와 시간순 정렬. 성공 완료 로그는 제거(이 줄로 갈음),
                                # 마켓삭제·실패만 background에서 별도 출력.
                                _log_line(
                                    site,
                                    _tx_pid,
                                    f"{_idx_prefix}{_tx_label}: {_tx_action_text} 전송{_tail}",
                                )
                                # 계정별 fire-and-forget task
                                _bg_task = asyncio.create_task(
                                    _run_transmit_in_background(
                                        _fire_transmit_account, site=site
                                    )
                                )
                                _bg_set = _pc_bg_transmit_tasks.setdefault(
                                    device_id, set()
                                )
                                _bg_set.add(_bg_task)
                                _bg_task.add_done_callback(_bg_set.discard)

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
                            global_counter={
                                "key": _gkey,
                                "idx_ref": _autotune_global_idx,
                                "total_ref": _autotune_global_total,
                            },
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

                        # 적응 배치: 이번 배치 소요시간으로 다음 배치 크기 조정 (단일 타겟 제외)
                        if not _target_ids:
                            _adapt_batch_size(
                                device_id,
                                site,
                                time.time() - _cycle_started_ts,
                                _AUTOTUNE_CYCLE_BATCH,
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
                        _cstats["deleted_pids"].update(_all_delete_pids)
                        _cstats["synced"] += _synced_count
                        _cstats["deleted"] += deleted_count
                        _cstats["batches"] += 1

                        _g_idx_now = _autotune_global_idx.get(_gkey, 0)
                        _g_total_now = _autotune_global_total.get(_gkey, 0)
                        _is_full_cycle = _g_total_now > 0 and _g_idx_now >= _g_total_now

                        # 배치 완료 로그 — UI 실시간 로그에는 미노출(불필요), 서버 로그만 유지
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
                            # scheduler_cycle 이벤트 발행 — 워룸 타임라인용 (한 바퀴 누적 통계)
                            try:
                                _c_dur_sec = 0.0
                                try:
                                    if _c_started:
                                        _c_dur_sec = round(
                                            (
                                                _now
                                                - datetime.fromisoformat(_c_started)
                                            ).total_seconds(),
                                            1,
                                        )
                                except Exception:
                                    _c_dur_sec = 0.0
                                _c_rate = (
                                    round(_cstats["total"] / _c_dur_sec, 1)
                                    if _c_dur_sec > 0
                                    else 0.0
                                )
                                _c_ended_iso = _now.isoformat()
                                from backend.domain.samba.warroom.service import (
                                    SambaMonitorService as _CycleMon,
                                )

                                async with get_write_session() as _cyc_session:
                                    _cyc_monitor = _CycleMon(_cyc_session)
                                    await _cyc_monitor.emit(
                                        "scheduler_cycle",
                                        "info",
                                        summary=f"오토튠[{site}] 사이클 완료 (1바퀴 {_g_total_now:,}건): {_cstats['ok']:,}건 성공, {_cstats['err']:,}건 실패{_c_err_detail} / 배치 {_cstats['batches']:,}회 | {int(_c_dur_sec):,}초, {_c_rate}건/초",
                                        source_site=site,
                                        detail={
                                            # total = 분모(1바퀴 전체 상품 수)로 고정.
                                            # UI 카드 "대상 {total}" 자리에 절대 200 같은 배치 단위 숫자가
                                            # 표시되지 않도록 안전장치.
                                            "total": _g_total_now,
                                            "total_global": _g_total_now,
                                            "processed": _cstats["total"],
                                            "is_full_cycle": True,
                                            "ok": _cstats["ok"],
                                            "errors": _cstats["err"],
                                            "no_pid": _cstats["no_pid"],
                                            "blocked": _cstats["blocked"],
                                            "timeouts": _cstats["timeout"],
                                            "other_errors": _cstats["other"],
                                            "price_transmit": len(
                                                _cstats["price_pids"]
                                            ),
                                            "stock_transmit": len(
                                                _cstats["stock_pids"]
                                            ),
                                            "synced": _cstats["synced"],
                                            "deleted": _cstats["deleted"],
                                            "batches": _cstats["batches"],
                                            "started_at": _c_started,
                                            "ended_at": _c_ended_iso,
                                            "duration_sec": _c_dur_sec,
                                            "rate": _c_rate,
                                        },
                                    )
                                    await _cyc_session.commit()
                            except Exception as _cyc_emit_err:
                                log.error(
                                    "[오토튠] scheduler_cycle 발행 실패: %s",
                                    _cyc_emit_err,
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
                            # 영구실패(승인 대기) 쿨다운 상품 제외 — 매 사이클 헛시도 차단.
                            # 만료 지난 항목은 정리하고, 아직 유효한 차단만 제외 조건에 반영.
                            _now_blk = datetime.now(timezone.utc)
                            for _bpid in [
                                _p
                                for _p, _u in _soldout_delete_retry_block.items()
                                if _u <= _now_blk
                            ]:
                                _soldout_delete_retry_block.pop(_bpid, None)
                            _blocked_del_ids = list(_soldout_delete_retry_block.keys())
                            if _blocked_del_ids:
                                _soldout_where.append(_CP.id.not_in(_blocked_del_ids))
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
                                        elif _del_acc.market_type in (
                                            "gmarket",
                                            "auction",
                                        ):
                                            # ESM 삭제 API는 마스터 goodsNo 필요 — _master 우선
                                            _pno = _m_nos.get(
                                                f"{_del_acc_id}_master"
                                            ) or _m_nos.get(_del_acc_id, "")
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
                                                _all_delete_pids.add(_sp.id)
                                                _sp_deleted_ids.append(_del_acc_id)
                                                _sp_site_tag = (
                                                    f"[{_sp.source_site}] "
                                                    if _sp.source_site
                                                    else ""
                                                )
                                                _sp_brand = (
                                                    getattr(_sp, "brand", "") or ""
                                                )
                                                _sp_brand_part = (
                                                    f"{_sp_brand} " if _sp_brand else ""
                                                )
                                                _log_line(
                                                    _sp.source_site or "",
                                                    _sp.id,
                                                    f"{_sp_site_tag}{_sp_brand_part}{_sp.name or _sp.id}: 품절잔존 → {_del_label} 마켓삭제 완료",
                                                )
                                            else:
                                                # 영구실패(승인대기/중복상품/판매금지/삭제불가 등
                                                # 마켓·사유 무관) 6시간 재시도 차단. 매 사이클 25건
                                                # 헛시도(각 blocking)가 무신사 사이클 600초 점유 →
                                                # STUCK_TIMEOUT 초과 Watchdog 강제재시작 → 처리량
                                                # 1/5 토막의 주범. 6시간 auto-expire라 일시적 실패
                                                # (쿠팡 동기화 지연 등)도 안전(최대 6시간 더 노출).
                                                _soldout_delete_retry_block[_sp.id] = (
                                                    datetime.now(timezone.utc)
                                                    + timedelta(
                                                        seconds=_SOLDOUT_DELETE_BLOCK_TTL_SEC
                                                    )
                                                )
                                                log.warning(
                                                    "[오토튠] 품절잔존 %s → %s 마켓삭제 실패: %s",
                                                    _sp.id,
                                                    _del_acc.market_type,
                                                    _dr.get("message"),
                                                )
                                        except Exception as _del_err:
                                            # 삭제 호출 자체 예외도 영구실패로 간주 → 쿨다운 차단
                                            _soldout_delete_retry_block[_sp.id] = (
                                                datetime.now(timezone.utc)
                                                + timedelta(
                                                    seconds=_SOLDOUT_DELETE_BLOCK_TTL_SEC
                                                )
                                            )
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
                                                _sp_site_tag2 = (
                                                    f"[{_sp.source_site}] "
                                                    if _sp.source_site
                                                    else ""
                                                )
                                                _sp_brand2 = (
                                                    getattr(_sp, "brand", "") or ""
                                                )
                                                _sp_brand_part2 = (
                                                    f"{_sp_brand2} "
                                                    if _sp_brand2
                                                    else ""
                                                )
                                                _log_line(
                                                    _sp.source_site or "",
                                                    _sp.id,
                                                    f"{_sp_site_tag2}{_sp_brand_part2}{_sp.name or _sp.id}: 품절잔존 전 마켓 삭제 성공 → 상품 DB 삭제 완료",
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
                                        "total_global": _total_global,
                                        "global_idx": _autotune_global_idx.get(
                                            _gkey, 0
                                        ),
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
                    _pc_slt(device_id)[site] = now.isoformat()
                    # 사이클# = 한 바퀴 완료 시 증가. batch 단위 increment 폐기
                    # (2026-05-26 사용자 요구: "전체 상품 한바퀴 = 1 사이클").
                    # 한 바퀴 완료 increment 는 line 931 area (idx ≥ total reset) 에서 처리.
                    _scc = _pc_scc(device_id)
                    log.info(
                        "[오토튠][%s] 배치 완료 (한바퀴 누적 %d회) — 즉시 재시작",
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
                # 사용자 중단(cancel-cycle) 억제 중이면 재시작 거부 — task.cancel()
                # 직후 자가부활하던 버그(2026-05-29) 차단.
                if _is_site_cancel_suppressed(device_id, site):
                    log.info("[오토튠][%s] 사용자 중단 — 사이클 종료", site)
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

                from backend.db.orm import get_read_session

                # 공통 사전 작업 (분류, 쿠키). (2026-05-27) write → read 전환:
                # 이 블록은 _get_setting / _get_active_sites_cached 호출만 — read-only.
                # 기존 write 세션 점유로 write pool 33s 핫스팟 (DB풀 모니터 캡처).
                # write pool 압박 감소 + idle in transaction 누적 차단.
                async with get_read_session() as session:
                    from backend.api.v1.routers.samba.proxy import _get_setting

                    # 롯데ON 쿠키 갱신
                    from backend.domain.samba.proxy.lotteon_sourcing import (
                        set_lotteon_cookie,
                    )

                    _lt_cookie = await _get_setting(session, "lotteon_cookie")
                    if _lt_cookie:
                        set_lotteon_cookie(str(_lt_cookie))

                    # 활성 소싱처 목록 파악 (글로벌 30s 캐시 — 모든 PC 공유, read pool 전용)
                    active_sites = await _get_active_sites_cached()

                    # 이 PC가 처리할 사이트만 — _pc_allowed_sites 기준
                    # 미등록(None) → 사이클 발행 차단 (2026-05-26 사고: 옛 owner_device_id
                    # 자동 시작 시 그 device 분담 미등록 → 모든 사이트 spawn → 사용자 정지 무효).
                    # 등록됨 → 그 사이트만
                    my_sites = get_pc_allowed_sites(device_id)
                    if my_sites is None:
                        log.warning(
                            "[오토튠][%s] 미등록 device — 사이클 발행 차단 (분담 mapping 없음)",
                            _dev_tag,
                        )
                        active_sites = []
                    else:
                        active_sites = [s for s in active_sites if s in my_sites]
                        # 데몬은 TRACKING_ONLY 사이트(무신사/GSShop 등)의 가격수집(오토튠) 미지원 —
                        # 송장(tracking)만 처리한다. 데몬 allowed_sites 에 무신사가 있어도(송장 폴링용)
                        # 오토튠 사이클은 스폰하지 않는다. 안 막으면 데몬·확장앱 중복 오토튠 발생.
                        # (오토튠 잡은 owner=device 강제라 _resolve_job_owner 가드를 우회 → 여기서 차단)
                        if (device_id or "").startswith("samba-daemon-"):
                            from backend.domain.samba.proxy.sourcing_queue import (
                                TRACKING_ONLY_DAEMON_SITES,
                            )

                            active_sites = [
                                s
                                for s in active_sites
                                if s not in TRACKING_ONLY_DAEMON_SITES
                            ]

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

                # 소싱처별 독립 루프 태스크 관리 (이 PC의 site_tasks dict에)
                _site_tasks = _pc_st(device_id)
                _active_set = set(active_sites)
                # active_sites 에서 빠진 site 의 옛 task cancel — 사용자 체크박스 해제 시
                # 그 사이트 사이클 즉시 중단 (2026-05-26 사고: 체크 해제해도 backend cycle
                # 계속 돌아서 "체크박스는 로그 가림용일뿐" 사용자 항의).
                _stopped = []
                for _old_site in list(_site_tasks.keys()):
                    if _old_site not in _active_set:
                        _old_task = _site_tasks[_old_site]
                        if _old_task and not _old_task.done():
                            _old_task.cancel()
                        del _site_tasks[_old_site]
                        _stopped.append(_old_site)
                if _stopped:
                    log.info(
                        "[오토튠][%s] 소싱처 루프 중단: %s (체크박스 해제)",
                        _dev_tag,
                        ", ".join(_stopped),
                    )
                _newly_spawned = []
                for _site in active_sites:
                    existing = _site_tasks.get(_site)
                    if existing and not existing.done():
                        continue
                    # 사용자 중단(cancel-cycle) 억제 중이면 재spawn 거부 —
                    # allowed_sites 가 아직 site 를 갖고 있어도(데몬 persist/sync 지연,
                    # 또는 확장앱 []) 억제 만료 전까지 부활 차단.
                    if _is_site_cancel_suppressed(device_id, _site):
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
                    _site_stuck_to = _SITE_STUCK_TIMEOUT_OVERRIDE.get(
                        _s, STUCK_TIMEOUT_SECONDS
                    )
                    if _now_ts - _last_hb > _site_stuck_to:
                        log.warning(
                            "[오토튠][%s][%s] stuck 감지 (%.0f초 무응답, 임계 %ds) — 강제 재시작",
                            _dev_tag,
                            _s,
                            _now_ts - _last_hb,
                            _site_stuck_to,
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
                # 죽은 DB 커넥션(pool_pre_ping=False + pool_recycle=60)으로 인한
                # 일시적 연결끊김(Transaction.rollback/PreparedStatement.fetch 등)은
                # 인프라 블립이므로 재시작 상한(MAX_RESTART_COUNT)에 포함하지 않는다.
                # 짧게 쉬고 즉시 재시도 — DB 복구 시 자동 회복(자가치유).
                # 미포함 안 하면 일 2~3회 블립이 누적돼 _pc_restart_count(성공 사이클에
                # 리셋 안 됨)가 ~25h 만에 50 도달 → 오토튠 무음 중단 사고.
                if _is_stale_conn_error(e):
                    log.warning(
                        "[오토튠][%s] 코디네이터 일시 연결끊김 — 재시도(상한 미포함): %s",
                        _dev_tag,
                        str(e)[:120],
                    )
                    await asyncio.sleep(3)
                    continue
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


async def _add_running_device(dev: str) -> None:
    """배포/재시작 시 자동 복원용 — 실행 중 PC device set 에 dev 추가 (멀티 PC).

    [근본 fix 2026-05-26] 옛 구현 = `_get_setting` (READ) + `set.add` + `_set_setting` (WRITE).
    여러 PC 가 거의 동시에 시작 클릭 시 race condition — 두 호출 모두 옛 raw 읽고
    각자 dev add → 마지막 WRITE 가 다른 dev 덮어씀 → DB running = 한 device 만 박힘
    → 다른 PC publisher 안 시작 → 그 사이트 사이클 발행 0 (일주일 사고).

    fix: PostgreSQL atomic UPDATE — 옛 value 를 직접 jsonb_array_elements 로 읽고
    새 dev 와 union → distinct array_agg → 단일 SQL row-update. race 원천 차단.
    """
    from backend.db.orm import get_write_session
    from sqlalchemy import text as _sa_text

    if not (dev or "").strip():
        return
    try:
        async with get_write_session() as session:
            # samba_settings 가 없으면 INSERT, 있으면 UPDATE 동시 처리.
            # 기존 value 의 array 와 신규 dev 를 union → distinct → 정렬 array.
            await session.execute(
                _sa_text(
                    """
                    INSERT INTO samba_settings (key, value, updated_at)
                    VALUES (
                        'autotune_running_devices',
                        to_jsonb(ARRAY[:dev]::text[])::json,
                        NOW()
                    )
                    ON CONFLICT (key) DO UPDATE SET
                        value = (
                            SELECT to_jsonb(array_agg(DISTINCT x ORDER BY x))::json
                            FROM (
                                SELECT jsonb_array_elements_text(
                                    CASE WHEN samba_settings.value::text ~ '^\\[' THEN samba_settings.value::jsonb
                                         ELSE '[]'::jsonb END
                                ) AS x
                                UNION ALL
                                SELECT :dev
                            ) sub
                            WHERE x IS NOT NULL AND x != ''
                        ),
                        updated_at = NOW()
                    """
                ),
                {"dev": dev},
            )
            await session.commit()
    except Exception as e:
        logger.warning(f"[오토튠] running_devices 추가 실패 dev={dev}: {e}")


async def _remove_running_device(dev: str) -> None:
    """실행 중 PC device set 에서 dev 제거 (정지 시) — atomic SQL.

    [근본 fix 2026-05-26] _add_running_device 와 동일한 race condition 차단.
    """
    from backend.db.orm import get_write_session
    from sqlalchemy import text as _sa_text

    if not (dev or "").strip():
        return
    try:
        async with get_write_session() as session:
            await session.execute(
                _sa_text(
                    """
                    UPDATE samba_settings SET
                        value = COALESCE((
                            SELECT to_jsonb(array_agg(x ORDER BY x))::json
                            FROM jsonb_array_elements_text(
                                CASE WHEN value::text ~ '^\\[' THEN value::jsonb ELSE '[]'::jsonb END
                            ) AS x
                            WHERE x != :dev AND x IS NOT NULL AND x != ''
                        ), '[]'::json),
                        updated_at = NOW()
                    WHERE key = 'autotune_running_devices'
                    """
                ),
                {"dev": dev},
            )
            await session.commit()
    except Exception as e:
        logger.warning(f"[오토튠] running_devices 제거 실패 dev={dev}: {e}")


async def _save_autotune_state(enabled: bool, device_id: str = ""):
    """DB에 오토튠 ON/OFF 상태 + 소유자 deviceId 저장.

    Cloud Run 인스턴스가 교체·스케일아웃될 때 auto_start_if_enabled가
    복원하면서 소유자 deviceId까지 함께 복구해야, SSG/롯데온 탭 작업이
    다른 PC의 확장앱으로 새나가지 않는다.
    """
    global _autotune_enabled_flag
    _autotune_enabled_flag = enabled
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
                    global _autotune_enabled_flag
                    _autotune_enabled_flag = False
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

            # 멀티 PC 복원 — autotune_running_devices set 의 모든 PC 자동 재시작
            # (legacy saved_device_id 1개만 시작하면 다른 PC SSG/ABC owner=None →
            # "데몬 미등록" 잡 발행 실패. 2026-05-26 SSG 800건 실패 사고 차단.)
            import json as _json

            async with get_read_session() as _ds:
                _raw_devs = await _get_setting(_ds, "autotune_running_devices") or "[]"
            try:
                _running_devs = (
                    set(_json.loads(_raw_devs)) if isinstance(_raw_devs, str) else set()
                )
            except Exception:
                _running_devs = set()
            # legacy 단일 device 도 포함
            _running_devs.add(saved_device_id)
            # 데몬은 stop UI 없음 — 분담 등록된 데몬은 영구 자동 시작.
            # autotune_running_devices set 이 어떤 이유 (옛 전역 stop 잔재 등) 로
            # 비어있어도 분담 DB 에 등록된 samba-daemon-* device 는 모두 자동 spawn.
            # 사용자 룰 (2026-05-27): 데몬 1개만 살아남고 나머지 죽는 사고 차단.
            for _dev_polled in list(_pc_allowed_sites.keys()):
                if _dev_polled.startswith("samba-daemon-"):
                    _running_devs.add(_dev_polled)

            _site_empty_skip_until.clear()
            clear_bulk_cancel("autotune")
            clear_bulk_cancel("transmit")
            _started_count = 0
            for _dev in sorted(_running_devs):
                _dev = (_dev or "").strip()
                if not _dev:
                    continue
                if _is_pc_running(_dev):
                    continue
                ev = _get_pc_event(_dev)
                ev.set()
                _pc_cycle_count[_dev] = 0
                _pc_restart_count[_dev] = 0
                _pc_main_task[_dev] = asyncio.create_task(
                    _autotune_loop(_dev),
                    name=f"autotune-main-{_dev[:8]}",
                )
                _started_count += 1
                logger.info("[오토튠] 서버 시작 — 자동 재개 (dev=%s)", _dev[:8])
            if _started_count:
                _autotune_enabled_flag = True
            logger.info(
                "[오토튠] 자동 복원 완료 — %d PC 재개 (목록 %s)",
                _started_count,
                ",".join(d[:8] for d in sorted(_running_devs) if d),
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
    # 옛 main task 명시 cancel — 시작 = 이 PC 의 모든 옛 사이클 비우고 fresh 시작.
    # 미cancel 시 사이클 누적 → 같은 사이트 사이클 N개 동시 진행 (모수 변동 사고).
    _old_main = _pc_main_task.get(dev)
    if _old_main and not _old_main.done():
        _old_main.cancel()
    # 옛 pending 잡 제거 (owner=dev) — 시작 = 큐 비우고 새 체크박스 기준 재발행.
    try:
        from backend.db.orm import get_write_session as _gws_clear
        from sqlalchemy import delete as _sa_delete
        from backend.domain.samba.sourcing_job.model import SambaSourcingJob

        async with _gws_clear() as _csess:
            await _csess.execute(
                _sa_delete(SambaSourcingJob).where(
                    SambaSourcingJob.owner_device_id == dev,
                    SambaSourcingJob.status == "pending",
                )
            )
            await _csess.commit()
    except Exception as _e:
        from backend.utils.logger import logger as _lg

        _lg.warning(f"[autotune_start] 옛 pending 잡 제거 실패(무시): {_e}")
    _pc_main_task[dev] = asyncio.create_task(
        _autotune_loop(dev),
        name=f"autotune-main-{dev[:8]}",
    )
    if not body.target_product_no:
        # 서버 재시작 후 자동 복원 — 한 PC라도 켜져 있었다는 사실 + 마지막 owner deviceId 저장
        await _save_autotune_state(True, dev)
        # 멀티 PC 복원용 — 실행 중 PC set 에 추가 (배포 후 모든 PC 자동 재시작)
        await _add_running_device(dev)
    return {"ok": True, "status": "started", "target": "registered"}


class AutotuneStopRequest(BaseModel):
    device_id: str = ""


@router.post("/autotune/stop")
async def autotune_stop(body: AutotuneStopRequest = AutotuneStopRequest()):
    """오토튠 정지 — 요청 dev 인스턴스만 정지 (다른 PC 영향 없음).

    이전 구현은 전역 정지(모든 실행 dev + bulk_cancel_all + SourcingQueue.cancel_all +
    autotune_enabled=False) 였으나, 멀티 PC 환경에서 1개 PC 정지가 전체 PC를 죽이는
    사고 → dev 한정 정지로 전환 (2026-05-25 사용자 재요청 "분리되어야해").

    dev 비어있고 실행 중인 PC가 1개뿐이면 그 PC를 정지 (UI device_id 누락 가드 보조).
    여러 PC 실행 중이고 dev 없으면 거절 (전역 영향 차단).
    """
    dev = (body.device_id or "").strip()
    running_devs = {d for d, ev in _pc_running.items() if ev.is_set()}

    if not dev:
        if len(running_devs) == 0:
            await _save_autotune_state(False)
            return {"ok": True, "status": "already_stopped"}
        if len(running_devs) == 1:
            dev = next(iter(running_devs))
        else:
            return {
                "ok": False,
                "error": "device_id 필수 — 다중 PC 실행 중, 전역 정지 차단",
            }

    # dev 한 개만 정지
    _pc_force_stop_set.add(dev)
    ev = _pc_running.get(dev)
    if ev is not None:
        ev.clear()
    _site_tasks = _pc_site_tasks.get(dev) or {}
    for _st in list(_site_tasks.values()):
        if not _st.done():
            _st.cancel()
    _site_tasks.clear()
    # 백그라운드 transmit fire-and-forget 태스크도 함께 cancel —
    # 갱신은 멈춰도 전송이 계속되던 버그(2026-05-27) 해결.
    _bg_tasks = _pc_bg_transmit_tasks.pop(dev, set())
    _bg_cancelled = 0
    for _bt in list(_bg_tasks):
        if not _bt.done():
            _bt.cancel()
            _bg_cancelled += 1
    _main = _pc_main_task.get(dev)
    if _main and not _main.done():
        _main.cancel()
    _pc_main_task.pop(dev, None)
    _cleanup_pc_instance(dev)

    # 실행 중 PC set 에서 제거 (배포 후 이 PC 자동 재시작 차단)
    await _remove_running_device(dev)

    # 다른 PC가 한 대도 안 돌고 있으면만 전역 enabled=False (재시작 차단)
    other_running = {d for d, ev in _pc_running.items() if d != dev and ev.is_set()}
    if not other_running:
        await _save_autotune_state(False)

    return {
        "ok": True,
        "status": "stopped",
        "stopped_device": dev,
        "remaining_devices": len(other_running),
        "cancelled_bg_transmits": _bg_cancelled,
    }


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
    """PC 분담 등록 — 이 PC가 처리할 사이트 목록. 변경 시 DB 영속화.

    UI 명시 POST 는 모두 authoritative — 사용자가 체크박스로 직접 지정한 값을 폴링
    union 이 덮어쓰지 않도록 강제. PC 간섭 방지 (2026-05-25 사용자 재요청
    "무조건 서로 간섭없이"). 폴링(X-Allowed-Sites 헤더)은 여전히 union 유지 —
    같은 deviceId 다중 PC flip-flop 가드.

    device_id 가 active key 인지 검증 (2026-05-25) — frontend localStorage 의 옛
    daemonDev 가 revoked device 분담 박는 사고 차단. dead device 잡 라우팅 → 60s
    타임아웃 → 재시도 → 매우 느림 사고 원천 차단.
    """
    dev = (body.device_id or "").strip()
    if not dev:
        return {"ok": False, "error": "device_id 필수"}

    # device_id active 검증 — samba_extension_key 에 revoke 안 된 키 존재해야 함.
    # 단 빈 분담 [] 로 등록 해제는 항상 허용 (사용자가 PC 분담 비우기).
    if body.sites:
        try:
            from backend.db.orm import get_read_session
            from sqlalchemy import text

            async with get_read_session() as _sess:
                _row = await _sess.execute(
                    text(
                        "SELECT 1 FROM samba_extension_key "
                        "WHERE device_id = :d AND revoked_at IS NULL "
                        "AND (expires_at IS NULL OR expires_at > now()) LIMIT 1"
                    ),
                    {"d": dev},
                )
                if _row.first() is None:
                    return {
                        "ok": False,
                        "error": f"device_id 미등록 또는 revoked: {dev[:30]}",
                        "registered_pcs": 0,
                    }
        except Exception as _exc:
            from backend.utils.logger import logger as _lg

            _lg.warning(f"[pc-allowed-sites] active 검증 실패(무시): {_exc}")

    if register_pc_allowed_sites(body.device_id, body.sites, authoritative=True):
        from backend.db.orm import get_write_session

        async with get_write_session() as _sess:
            await persist_pc_allowed_sites(_sess, body.device_id)
            await _sess.commit()
    pcs = get_active_pcs()
    return {
        "ok": True,
        "registered_pcs": len(pcs),
        "this_pc": sorted(pcs.get(dev, set())),
    }


@router.get("/autotune/pc-allowed-sites")
async def autotune_pc_allowed_sites_get():
    """현재 등록된 모든 PC 분담 매핑 조회 + 데몬 목록(UI '연결된 데몬'용).

    by_device: {device_id: [sites]} — 전체 매핑(레거시 호환).
    daemons: [{device_id, sites, last_seen_ago, alive}] — samba-daemon- prefix 만,
             UI에서 데몬별 사이트 지정 카드 렌더용. last_seen_ago=초, alive=60초내.
    """
    pcs = get_active_pcs()
    now = time.time()
    daemons = []
    for dev, sites in pcs.items():
        if not dev.startswith("samba-daemon-"):
            continue
        last = _pc_last_seen.get(dev, 0.0)
        ago = round(now - last) if last else None
        daemons.append(
            {
                "device_id": dev,
                "sites": sorted(sites),
                "last_seen_ago": ago,
                "alive": bool(last and now - last < 60),
            }
        )
    daemons.sort(key=lambda d: d["device_id"])
    return {
        "registered_pcs": len(pcs),
        "by_device": {dev: sorted(sites) for dev, sites in pcs.items()},
        "daemons": daemons,
    }


@router.get("/autotune/active-cycles")
async def autotune_active_cycles():
    """모든 (device_id, site) 분담 목록 — 활성 사이클 + 비활성 분담 둘 다.

    사용자 visibility — 죽은 분담도 보여 사용자가 삭제 액션 가능. 살아있는 task 는
    개별 cancel (POST /autotune/cancel-cycle), 비활성 분담은 분담 자체 제거
    (POST /autotune/pc-allowed-sites 로 sites 빈배열).
    status: "active" = task 실행 중, "inactive" = 분담만 등록 폴링 없음.
    avg_sec_per_item — 현 사이클 시작 후 (now - started_at) / idx 평균 처리 시간.
    """
    cycles = []
    now_ts = time.time()
    seen: set[tuple[str, str]] = set()
    # 1) 활성 task entry — 정상 동작 중
    for dev, site_tasks in list(_pc_site_tasks.items()):
        if not isinstance(site_tasks, dict):
            continue
        for site, task in list(site_tasks.items()):
            if task is None or task.done():
                continue
            gkey = (dev, site)
            seen.add(gkey)
            idx = _autotune_global_idx.get(gkey, 0)
            total = _autotune_global_total.get(gkey, 0)
            cycle_count = _pc_site_cycle_counts.get(dev, {}).get(site, 0)
            last_tick = _pc_site_last_ticks.get(dev, {}).get(site, "")
            hb = _pc_site_heartbeats.get(dev, {}).get(site, 0)
            hb_ago = int(now_ts - hb) if hb else None
            avg_sec: Optional[float] = None
            started_at_iso: Optional[str] = None
            elapsed_sec: Optional[int] = None
            price_count = 0
            stock_count = 0
            soldout_count = 0
            try:
                _cstats = _autotune_cycle_stats.get(gkey) or {}
                _started_iso = _cstats.get("started_at")
                if _started_iso:
                    started_at_iso = str(_started_iso)
                    _started_dt = datetime.fromisoformat(
                        started_at_iso.replace("Z", "+00:00")
                    )
                    _elapsed = (
                        datetime.now(timezone.utc) - _started_dt
                    ).total_seconds()
                    elapsed_sec = int(_elapsed)
                    if idx > 0 and _elapsed > 0:
                        avg_sec = round(_elapsed / idx, 2)
                price_count = len(_cstats.get("price_pids") or set())
                stock_count = len(_cstats.get("stock_pids") or set())
                soldout_count = len(_cstats.get("deleted_pids") or set())
            except Exception:
                pass
            # 데몬 생존 — 백엔드 루프 heartbeat 와 별개로 실제 데몬 폴링 여부 표시.
            # 데몬 죽어도 루프는 계속 돌아 "활성"으로 보이던 착시 제거.
            # 데몬 전용 사이트(SSG/ABC/GrandStage/LOTTEON)이고 데몬 device 일 때만 의미.
            # last_seen 180초 초과 = 죽음(잡 발행 게이트 pick_daemon_owner TTL 과 동일 기준).
            daemon_alive: Optional[bool] = None
            daemon_last_seen_ago: Optional[int] = None
            try:
                from backend.domain.samba.proxy.sourcing_queue import (
                    DAEMON_ONLY_SITES as _DOS,
                )

                if site in _DOS and dev.startswith("samba-daemon-"):
                    _d_last = _pc_last_seen.get(dev, 0.0)
                    daemon_last_seen_ago = int(now_ts - _d_last) if _d_last else None
                    daemon_alive = bool(_d_last and (now_ts - _d_last) <= 180.0)
            except Exception:
                pass
            cycles.append(
                {
                    "device_id": dev,
                    "site": site,
                    "status": "active",
                    "idx": idx,
                    "total": total,
                    "cycle_count": cycle_count,
                    "last_tick": last_tick,
                    "heartbeat_ago_sec": hb_ago,
                    "avg_sec_per_item": avg_sec,
                    "started_at": started_at_iso,
                    "elapsed_sec": elapsed_sec,
                    "price_count": price_count,
                    "stock_count": stock_count,
                    "soldout_count": soldout_count,
                    "daemon_alive": daemon_alive,
                    "daemon_last_seen_ago_sec": daemon_last_seen_ago,
                }
            )
    # 2) 비활성 분담 entry — DB 에 등록됐지만 task 없음 (죽은 데몬/꺼진 PC).
    # 사용자가 visibility 확보 후 삭제 액션 가능.
    for dev, sites in list(_pc_allowed_sites.items()):
        if not sites:
            continue
        last_seen_ts = _pc_last_seen.get(dev, 0)
        last_seen_ago = int(now_ts - last_seen_ts) if last_seen_ts else None
        for site in sorted(sites):
            gkey2 = (dev, site)
            if gkey2 in seen:
                continue
            cycles.append(
                {
                    "device_id": dev,
                    "site": site,
                    "status": "inactive",
                    "idx": 0,
                    "total": 0,
                    "cycle_count": 0,
                    "last_tick": "",
                    "heartbeat_ago_sec": None,
                    "avg_sec_per_item": None,
                    "started_at": None,
                    "elapsed_sec": None,
                    "price_count": 0,
                    "stock_count": 0,
                    "soldout_count": 0,
                    "last_seen_ago_sec": last_seen_ago,
                }
            )
    cycles.sort(key=lambda c: (c["status"] != "active", c["device_id"], c["site"]))
    return {"count": len(cycles), "cycles": cycles}


class CancelCycleRequest(BaseModel):
    device_id: str
    site: str


@router.post("/autotune/cancel-cycle")
async def autotune_cancel_cycle(body: CancelCycleRequest):
    """특정 (device_id, site) cycle 즉시 중단.

    (2026-05-27) 즉시 재spawn 버그 fix.
    기존: task.cancel() + site_tasks.pop 만 수행. _pc_allowed_sites 안 건드려서
    _autotune_loop 다음 tick(~3초) 에서 active_sites 에 site 살아있음 → 라인 3426
    `for _site in active_sites: spawn` → 즉시 재시작. 중단 버튼 1~3초만 멈췄다 재가동.
    수정:
      1) _pc_allowed_sites[dev] 에서 site 제거 → 다음 tick 재spawn 차단
      2) DB autotune_pc_allowed_sites persist → lifecycle sync 가 덮어쓰지 못하게
      3) _active_sites_cache invalidate → TTL 만료 안 기다림
      4) task cancel
    """
    dev = (body.device_id or "").strip()
    site = (body.site or "").strip()
    if not dev or not site:
        return {"ok": False, "error": "device_id 와 site 필수"}

    # 0) 재spawn 억제 — allowed_sites 가 비어있거나(확장앱) 불일치해도 중단 보장.
    #    코디네이터 spawn / 사이트 루프 CancelledError 핸들러가 이 플래그를 확인해
    #    task.cancel() 직후 자가부활/재spawn 하던 버그(2026-05-29 확정) 차단.
    #    (확장앱 device 의 allowed_sites=[] 라서 기존 `site in current` 게이트가
    #     항상 False → 중단 통째로 무효였던 root cause.)
    _SUPPRESS_SEC = 60.0
    _pc_site_cancel_until[(dev, site)] = time.time() + _SUPPRESS_SEC

    # 1) allowed_sites 에서 site 제거 (등록돼 있을 때만) — 영구 재spawn 차단.
    _re_spawn_blocked = False
    current = _pc_allowed_sites.get(dev)
    if current is not None and site in current:
        new_sites = sorted(current - {site})
        register_pc_allowed_sites(dev, new_sites, authoritative=True)
        _re_spawn_blocked = True

        # DB persist — lifecycle sync_pc_allowed_sites_from_db 가 옛 값으로
        # 복원하지 못하도록 진실 출처 갱신.
        try:
            from backend.db.orm import get_write_session

            async with get_write_session() as _ws:
                await persist_pc_allowed_sites(_ws, dev)
                await _ws.commit()
        except Exception as _exc:
            logging.getLogger("autotune").warning(
                f"[cancel-cycle] persist 실패 (무시): {_exc}"
            )

    # 2) active_sites_cache invalidate — 항상 (TTL 만료 대기 제거)
    _active_sites_cache["ts"] = 0.0
    _active_sites_cache["data"] = None

    # 3) task cancel — 항상 시도 (게이트 무관). pop 으로 사이트 루프의 watchdog
    #    체크(_pc_st().get(site) != current_task)도 break 유도.
    site_tasks = _pc_site_tasks.get(dev) or {}
    task = site_tasks.get(site)
    cancelled = False
    if task is not None and not task.done():
        task.cancel()
        cancelled = True
    site_tasks.pop(site, None)

    # 억제 플래그를 항상 설정하므로 "활성 cycle 없음" 조기 반환 제거 — 중단은 항상 ok.
    return {
        "ok": True,
        "cancelled": cancelled,
        "respawn_blocked": _re_spawn_blocked,
        "suppressed_sec": int(_SUPPRESS_SEC),
        "device_id": dev,
        "site": site,
    }


@router.get("/autotune/status")
async def autotune_status(device_id: str = ""):
    """오토튠 상태 조회 — 본인 테넌트의 device만 필터. device_id 미지정 시 본인 tenant 합계.

    device_id 지정 시 그 PC 인스턴스 기준 cycle/last_tick/site_loops 반환.
    """
    from backend.db.orm import get_read_session
    from backend.core.tenant_context import current_tenant_id as _ctv

    # 본인 테넌트의 device_id 목록 조회 → module-global 상태를 본인 것만 필터링
    _my_tid = _ctv.get()
    _my_devices: set[str] = set()
    if _my_tid:
        try:
            from backend.domain.samba.extension_key.model import SambaExtensionKey

            async with get_read_session() as _rs_dev:
                _dev_stmt = select(SambaExtensionKey.device_id).where(
                    SambaExtensionKey.tenant_id == _my_tid,
                    SambaExtensionKey.device_id.isnot(None),
                )
                _dev_result = await _rs_dev.execute(_dev_stmt)
                _my_devices = {d for (d,) in _dev_result.all() if d}
        except Exception:
            _my_devices = set()
        # (2026-05-25) owner_device_ids env fallback 제거 — 키-디바이스 TOFU 바인딩으로
        # samba_extension_key.device_id 가 첫 사용 시 자동 백필되어 _my_devices 자연 충원.

    tripped = {
        site: count
        for site, count in _site_consecutive_soldout.items()
        if _site_breaker_tripped.get(site)
    }

    # 24h 갱신 건수 — 글로벌 60s 캐시 (PC 8대 동시 폴링 시 IPC/BufferIO 누적 차단)
    refreshed_24h = await _get_refreshed_24h_cached()

    # 소싱처별 인터벌 정보
    from backend.domain.samba.collector.refresher import (
        get_effective_autotune_concurrency,
        get_site_intervals_info,
    )

    intervals_info = get_site_intervals_info()

    dev = (device_id or "").strip()
    _now_hb = time.time()

    # 본인 테넌트의 device만 통계 대상 — device_id가 본인 것이 아니면 빈 응답
    def _is_mine(d: str) -> bool:
        if not _my_tid:
            return True  # tenant context 없음(워커/관리자) → 전체 표시
        return d in _my_devices

    if dev:
        if not _is_mine(dev):
            _active_site_loops = {}
            running = False
            last_tick = None
            cycle_count = 0
            restart_count = 0
        else:
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
            running = (
                _is_pc_running(dev) and main_task is not None and not main_task.done()
            )
            last_tick = _pc_last_tick.get(dev)
            cycle_count = _pc_cycle_count.get(dev, 0)
            restart_count = _pc_restart_count.get(dev, 0)
    else:
        # device_id 미지정 — 시크릿창/확장앱 미감지 케이스.
        # (2026-05-25) 타 PC running 합산을 노출하면 시크릿창에서도 "실행 중"으로 보이고
        # 체크박스가 다 켜진 채 표시돼 다른 PC 분담을 침범하는 사고 발생. strict 빈 응답.
        _active_site_loops = {}
        running = False
        last_tick = None
        cycle_count = 0
        restart_count = 0

    return {
        "running": running,
        # 사용자 의도(전역 enabled) — 프론트 자동재합류가 "정지(False)"를 구분해
        # 정지 무시 재시작 루프를 막는 데 사용. running 은 코디네이터 실시간 상태,
        # enabled 는 사용자가 켜둔 상태(정지 누르면 False).
        "enabled": _autotune_enabled_flag,
        "last_tick": last_tick,
        "cycle_count": cycle_count,
        "restart_count": restart_count,
        "max_restart": MAX_RESTART_COUNT,
        "refreshed_count": refreshed_24h,
        "target": "registered",
        "breaker_tripped": tripped,
        "site_intervals": intervals_info.get("base_intervals", {}),
        "site_autotune_concurrency": get_effective_autotune_concurrency(),
        "site_loops": _active_site_loops,
        "stuck_timeout": STUCK_TIMEOUT_SECONDS,
        # PC별 분담 현황 (UI 표시용) — 본인 테넌트 device만
        "pc_assignments": {
            d: sorted(sites)
            for d, sites in get_active_pcs().items()
            if sites and _is_mine(d)
        },
        # 현재 오토튠 실행 중인 PC 목록 (UI에서 본인이 그 중에 있는지 판단)
        "running_pcs": sorted(
            d for d, ev in _pc_running.items() if ev.is_set() and _is_mine(d)
        ),
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


# ── 오토튠 필터 (소싱처 / 판매처 선택) ──


class AutotuneFilterRequest(BaseModel):
    enabled_sources: Optional[list[str]] = None
    enabled_markets: Optional[list[str]] = None


@router.get("/autotune/filters")
async def autotune_get_filters():
    """오토튠 필터 설정 + 실제 존재하는 소싱처/판매처(마켓 단위) 목록 반환.

    available_sources/markets 는 registered 78k 스캔이라 느림(~100초) → TTL 캐시.
    saved_sources/markets(설정값)는 가벼우므로 매 호출 최신 조회.
    """
    from backend.db.orm import get_read_session
    from backend.api.v1.routers.samba.proxy import _get_setting
    from backend.domain.samba.collector.model import SambaCollectedProduct as _CP
    from sqlalchemy import distinct, text as _text

    global _filters_avail_cache

    # 저장된 필터(설정) — 가벼움, 매번 최신
    async with get_read_session() as session:
        saved_sources = await _get_setting(session, AUTOTUNE_FILTER_SOURCES_KEY)
        saved_markets = await _get_setting(session, AUTOTUNE_FILTER_MARKETS_KEY)

    async def _compute_available() -> tuple[list[str], list[str]]:
        """무거운 available_* 계산 — registered 상품 소싱처/마켓."""
        async with get_read_session() as session:
            # 마켓 등록 상품이 있는 소싱처만 (수집만 된 것은 제외)
            src_stmt = select(distinct(_CP.source_site)).where(
                _CP.source_site != None,
                _CP.source_site != "",
                _CP.status == "registered",
            )
            src_result = await session.execute(src_stmt)
            srcs = sorted([r[0] for r in src_result.all() if r[0]])

            # 판매처(마켓) — registered_accounts 전체 fetch(80초) 대신 EXISTS + @>
            # (GIN ix_scp_registered_accounts_gin 활용). 계정수(소수)만큼 containment 프로브.
            mk_stmt = _text(
                "SELECT DISTINCT a.market_type FROM samba_market_account a "
                "WHERE a.market_type IS NOT NULL AND EXISTS ("
                "SELECT 1 FROM samba_collected_product cp "
                "WHERE cp.status = 'registered' "
                "AND cp.registered_accounts @> jsonb_build_array(a.id))"
            )
            mk_result = await session.execute(mk_stmt)
            mkts = sorted([r[0] for r in mk_result.all() if r[0]])
        return srcs, mkts

    # available_* — stale-while-revalidate. cache 있으면 즉시 반환(stale 포함),
    # stale 이면 백그라운드에서 재계산. cache 자체가 없을 때만 블로킹 (콜드 스타트).
    # → TTL 만료 시점에 100초 블로킹 → 체크박스 사라짐 사고 방지 (2026-05-25).
    cached = _filters_avail_cache
    if cached:
        available_sources = cached["sources"]
        available_markets = cached["markets"]
        if (time.time() - cached.get("ts", 0)) >= _FILTERS_AVAIL_TTL:
            # stale — 백그라운드 재계산 트리거 (중복 방지: lock locked 면 skip)
            async def _refresh_in_background():
                global _filters_avail_cache
                async with _filters_avail_lock:
                    # 진입 후 신선해졌으면 skip
                    c2 = _filters_avail_cache
                    if c2 and (time.time() - c2.get("ts", 0)) < _FILTERS_AVAIL_TTL:
                        return
                    try:
                        srcs, mkts = await _compute_available()
                        _filters_avail_cache = {
                            "sources": srcs,
                            "markets": mkts,
                            "ts": time.time(),
                        }
                    except Exception as exc:
                        logger.warning(
                            f"[오토튠][filters] 백그라운드 재계산 실패: {exc}"
                        )

            if not _filters_avail_lock.locked():
                asyncio.create_task(_refresh_in_background())
    else:
        async with _filters_avail_lock:
            cached = _filters_avail_cache
            if cached:
                available_sources = cached["sources"]
                available_markets = cached["markets"]
            else:
                available_sources, available_markets = await _compute_available()
                _filters_avail_cache = {
                    "sources": available_sources,
                    "markets": available_markets,
                    "ts": time.time(),
                }

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

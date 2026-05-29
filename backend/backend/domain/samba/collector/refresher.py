"""소싱처별 가격/재고 재수집 모듈.

서버에서 직접 HTTP 요청으로 최신 가격/품절 상태를 추출한다.
KREAM은 확장앱 큐(KreamClient.collect_queue)를 통해 자동 수집.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


from backend.utils.logger import logger

# 소싱처당 동시 요청 제한 (프로덕션 기준 단일값)
CONCURRENCY_PER_SITE = 10
# 소싱처별 동시 요청 수 (프로덕션 기준 단일값, 확장앱/워커 BATCH와 통일)
SITE_CONCURRENCY: dict[str, int] = {
    "MUSINSA": 20,  # 벌크 갱신용 (오토튠은 collector_autotune.py에서 별도 4로 제한)
    "KREAM": 5,
    "DANAWA": 5,
    "FashionPlus": 10,
    "Nike": 5,
    "Adidas": 5,
    "ABCmart": 5,  # worker._ABC_BATCH=5와 통일
    "GrandStage": 5,
    "REXMONDE": 5,
    # SSG/LOTTEON: 확장앱 경로 — owner deviceId 필터링 적용 후 실행 PC 1대만 처리
    "SSG": 3,  # worker._SSG_BATCH=3과 통일
    "LOTTEON": 2,  # worker BATCH 별도 미정의 — 보수값 유지
    "GSShop": 5,
    "ElandMall": 5,
    "SSF": 5,
    "NAVERSTORE": 5,
    "SNKRDUNK": 2,
}
# 오토튠 전용 동시성 오버라이드 (값 없으면 SITE_CONCURRENCY 기본값 사용)
# 24시간 백그라운드 실행 → 차단 방지를 위해 일반 갱신/수집보다 보수적 운영
SITE_AUTOTUNE_CONCURRENCY: dict[str, int] = {
    "MUSINSA": 4,  # 일반 갱신은 SITE_CONCURRENCY=20, 오토튠만 4
}
# 소싱처별 기본 인터벌 (초) — 전 사이트 0 하드코딩 (2026-05-26 사용자 요구).
# 차단 시 _site_intervals[site] 자동 증가 로직은 유지 (refresher 안 2배 backoff).
SITE_BASE_INTERVAL: dict[str, float] = {
    "MUSINSA": 0,
    "KREAM": 0,
    "DANAWA": 0,
    "FashionPlus": 0,
    "Nike": 0,
    "Adidas": 0,
    "ABCmart": 0,
    "GrandStage": 0,
    "REXMONDE": 0,
    "SSG": 0,
    "LOTTEON": 0,
    "GSShop": 0,
    "ElandMall": 0,
    "SSF": 0,
    "NAVERSTORE": 0,
    "SNKRDUNK": 0,
}
# 소싱처별 최소 인터벌 (초)
SITE_MIN_INTERVAL: dict[str, float] = {
    "MUSINSA": 0,
    "KREAM": 0,
    "DANAWA": 0,
    "FashionPlus": 0,
    "Nike": 0,
    "Adidas": 0,
    "ABCmart": 0,
    "GrandStage": 0,
    "REXMONDE": 0,
    "SSG": 0,
    "LOTTEON": 0,
    "GSShop": 0,
    "ElandMall": 0,
    "SSF": 0,
    "NAVERSTORE": 0,
    "SNKRDUNK": 0.5,
}
# 소싱처별 인터벌 복원 스텝 (성공 시 감소량)
SITE_INTERVAL_STEP: dict[str, float] = {
    "MUSINSA": 0.2,
    "KREAM": 0.3,
    "DANAWA": 0.3,
    "FashionPlus": 0.3,
    "Nike": 0.3,
    "Adidas": 0.3,
    "ABCmart": 0.3,
    "GrandStage": 0.3,
    "REXMONDE": 0.3,
    "SSG": 0.5,
    "LOTTEON": 0.3,
    "GSShop": 0.3,
    "ElandMall": 0.3,
    "SSF": 0.3,
    "NAVERSTORE": 0.3,
    "SNKRDUNK": 0.3,
}
# KREAM 확장앱 대기 타임아웃 (초)
KREAM_TIMEOUT = 90
# 소싱처별 상품 1건 전체 처리 타임아웃 (초)
# 확장앱 의존 마켓(LOTTEON/SSG)은 내부 단계(HTML+pbf+DOM 위임) 합산이 60초를 초과할 수
# 있어 wrapper 한계와 충돌하면 안전망이 무력화된다. 단계별 합산 + 안전마진 기준으로
# 마켓별 분기.
PRODUCT_TIMEOUT_DEFAULT: int = 60
SITE_PRODUCT_TIMEOUT: dict[str, int] = {
    # 실측(2026-05-05): 확장앱 단건 SSG 17s, ABC 13s, LOTTEON 22s.
    # 단건은 빠르지만 큐 적체(동시처리 1개 < 발행속도) 시 대기 시간 폭증.
    # 90s timeout 시 timeout 다수 발생 확인 → 큐 대기 흡수 위해 150s 유지.
    # 근본 해결: 확장앱 동시처리 캡 늘리기(아래 _siteSemaphores).
    "LOTTEON": 150,
    "SSG": 150,
    "ABCmart": 150,
    "GrandStage": 150,
}


def get_product_timeout(site: str) -> int:
    """소싱처별 상품 1건 전체 처리 타임아웃(초) 조회."""
    return SITE_PRODUCT_TIMEOUT.get(site, PRODUCT_TIMEOUT_DEFAULT)


# 소싱처별 적응형 인터벌 관리 (기능별 격리)
# 키 형식: "MUSINSA" (워룸/갱신), "MUSINSA_collect" (수집)
_site_intervals: dict[str, float] = {}
_site_consecutive_errors: dict[str, int] = {}
# 연속 타임아웃 카운터 (좀비 공유 클라이언트 자동 복구용)
_site_consecutive_timeouts: dict[str, int] = {}
# 연속 타임아웃 임계치 — 이 이상이면 공유 클라이언트 풀 강제 폐기
TIMEOUT_RESET_THRESHOLD = 5
# 소싱처별 안전 인터벌 기록 (차단 안 당하는 최소값)
_site_safe_intervals: dict[str, float] = {}


def get_interval_key(site: str, feature: str = "refresh") -> str:
    """기능별 인터벌 키 생성. 수집/갱신/워룸이 서로 간섭하지 않도록 격리."""
    if feature == "refresh":
        return site  # 기존 호환
    return f"{site}_{feature}"


# 벌크 갱신용 캐시 (배치 시작 시 1회 조회)
_bulk_musinsa_cache: dict[str, Any] = {}

# 무신사 오토튠 전용 공유 HTTP 클라이언트 풀 (프록시 URL별로 1개씩 유지)
# TCP 연결 풀링 + TLS 재사용으로 핸드셰이크 부담 감소, 봇 시그널 완화
# 키: 프록시 URL 문자열 (None=메인 IP)
_musinsa_shared_clients: dict[str | None, Any] = {}


def _get_musinsa_shared_client(proxy_url: str | None) -> Any:
    """오토튠 전용 공유 httpx.AsyncClient 반환 (프록시별 풀링)."""
    import httpx as _httpx

    existing = _musinsa_shared_clients.get(proxy_url)
    if existing is not None and not existing.is_closed:
        return existing
    _kwargs: dict[str, Any] = {
        "timeout": _httpx.Timeout(45, connect=10.0),
    }
    if proxy_url:
        _kwargs["proxy"] = proxy_url
    new_client = _httpx.AsyncClient(**_kwargs)
    _musinsa_shared_clients[proxy_url] = new_client
    return new_client


async def reset_musinsa_shared_clients() -> None:
    """공유 클라이언트 전체 폐기 (차단 누적 시 connection 재시작용)."""
    for c in list(_musinsa_shared_clients.values()):
        try:
            await c.aclose()
        except Exception:
            pass
    _musinsa_shared_clients.clear()


async def _prepare_musinsa_cache() -> None:
    """MUSINSA 벌크 갱신 전 쿠키 캐싱 (로테이션 지원).

    등급할인율은 상품 API의 memberGrade.discountRate에서 직접 추출하므로
    별도 회원 API 호출 불필요 (새 멤버십 시스템).
    """
    cookies = await _get_musinsa_cookies()
    _bulk_musinsa_cache["cookies"] = cookies
    # 사이클 간 로테이션 상태 유지 (첫 호출 시만 초기화)
    if "cookie_idx" not in _bulk_musinsa_cache:
        _bulk_musinsa_cache["cookie_idx"] = 0
        _bulk_musinsa_cache["cookie_usage"] = 0
    _bulk_musinsa_cache["cookie"] = (
        cookies[_bulk_musinsa_cache["cookie_idx"] % len(cookies)] if cookies else ""
    )
    _bulk_musinsa_cache["grade_rate"] = 0
    logger.info(
        f"[쿠키 캐싱] 쿠키 {len(cookies)}개 로드, 현재 인덱스 {_bulk_musinsa_cache.get('cookie_idx', 0)}, 사용량 {_bulk_musinsa_cache.get('cookie_usage', 0)}"
    )


# IP 로테이션: 프록시 목록 순환 (동시요청 수 기준으로 교대)
IP_ROTATE_EVERY = 20
# 사이트별 독립 카운터 (무신사·ABCmart 등 소싱처 병렬 실행 시 각각 20건 단위로 로테이션)
_ip_rotate_counters: dict[str, int] = {}
_ip_rotate_idxs: dict[str, int] = {}
_ip_rotate_labels: dict[str, str] = {}
_ip_rotate_totals: dict[str, int] = {}

# DB 프록시 캐시 (autotune 용도)
_db_proxy_cache: list[str] | None = None
_db_proxy_cache_ts: float = 0


# purposes: "autotune" | "collect" | "transmit"
_PROXY_PURPOSES = ("autotune", "collect", "transmit")
_db_proxy_caches: dict[str, list[str]] = {p: [] for p in _PROXY_PURPOSES}


async def _fetch_all_db_proxies() -> dict[str, list[str]]:
    """DB의 proxy_config를 한 번 읽고 purpose별로 분류하여 반환."""
    from sqlmodel import select
    from backend.db.orm import get_read_session
    from backend.domain.samba.forbidden.model import SambaSettings

    buckets: dict[str, list[str]] = {p: [] for p in _PROXY_PURPOSES}
    async with get_read_session() as session:
        result = await session.execute(
            select(SambaSettings).where(SambaSettings.key == "proxy_config")
        )
        row = result.scalar_one_or_none()
        if not row or not row.value:
            return buckets
        for p in row.value:
            if not (p.get("enabled") and p.get("url")):
                continue
            for purpose in p.get("purposes") or []:
                if purpose in buckets:
                    buckets[purpose].append(p["url"])
    return buckets


async def refresh_db_proxy_cache() -> dict[str, list[str]]:
    """DB에서 프록시 목록을 즉시 읽어 purpose별 캐시에 저장.

    FastAPI startup / 설정 변경 시 호출하여 캐시를 최신 상태로 유지한다.
    """
    global _db_proxy_caches, _db_proxy_cache, _db_proxy_cache_ts
    import time

    try:
        buckets = await _fetch_all_db_proxies()
    except Exception as e:
        logger.warning(f"[proxy] DB 프록시 로드 실패: {e}")
        buckets = {p: [] for p in _PROXY_PURPOSES}
    _db_proxy_caches = buckets
    _db_proxy_cache = buckets.get("autotune", [])  # 하위 호환
    _db_proxy_cache_ts = time.monotonic()
    return buckets


def _get_cached_proxies(purpose: str) -> list[str]:
    """purpose별 캐시된 프록시 URL 목록 반환. async 컨텍스트에서는 만료 시 백그라운드 갱신."""
    global _db_proxy_caches, _db_proxy_cache_ts
    import asyncio
    import time

    now = time.monotonic()
    stale = now - _db_proxy_cache_ts > 300
    if stale:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(refresh_db_proxy_cache())
        except RuntimeError:
            try:
                asyncio.run(refresh_db_proxy_cache())
            except Exception:
                pass
    return list(_db_proxy_caches.get(purpose, []))


def get_autotune_proxies() -> list[str]:
    """오토튠 용도로 사용할 활성 프록시 URL 목록 (DB 설정 기반)."""
    return _get_cached_proxies("autotune")


def get_collect_proxies() -> list[str]:
    """수집 용도로 사용할 활성 프록시 URL 목록 (DB 설정 기반)."""
    return _get_cached_proxies("collect")


def get_transmit_proxies() -> list[str]:
    """전송 용도로 사용할 활성 프록시 URL 목록 (DB 설정 기반)."""
    return _get_cached_proxies("transmit")


def get_collect_proxy_url() -> str | None:
    """수집 용도 프록시 중 첫 번째 URL만 반환 (단일 프록시가 필요한 구 API 호환용)."""
    urls = get_collect_proxies()
    return urls[0] if urls else None


def get_transmit_proxy_url() -> str | None:
    """?꾩넚 ?⑸룄 ?꾨줉??以?泥?踰덉㎏ URL留?諛섑솚."""
    urls = get_transmit_proxies()
    return urls[0] if urls else None


def invalidate_db_proxy_cache() -> None:
    """DB 프록시 캐시 무효화 — 설정 변경 시 호출."""
    global _db_proxy_cache, _db_proxy_cache_ts, _db_proxy_caches
    _db_proxy_cache = None
    _db_proxy_caches = {p: [] for p in _PROXY_PURPOSES}
    _db_proxy_cache_ts = 0


def _get_rotated_proxy(site: str = "MUSINSA") -> str | None:
    """프록시 목록을 N건 단위로 순환 — DB 설정 페이지에 등록된 프록시만 사용.

    site 파라미터로 소싱처별 독립 카운터를 관리한다. 환경변수/하드코딩 폴백 없음.
    """
    global _ip_rotate_counters, _ip_rotate_idxs, _ip_rotate_labels, _ip_rotate_totals
    global _refresh_log_total

    # DB 설정 페이지(`/samba/settings`)에 등록된 autotune 용도 프록시만 사용
    proxies = get_autotune_proxies()
    if not proxies:
        return None
    # 프록시만 사용 (메인 IP 제외)
    pool: list[str | None] = proxies

    # 사이트별 카운터 초기화
    if site not in _ip_rotate_counters:
        _ip_rotate_counters[site] = 0
        _ip_rotate_idxs[site] = 0
        _ip_rotate_labels[site] = ""
        _ip_rotate_totals[site] = 0

    _ip_rotate_counters[site] += 1
    _ip_rotate_totals[site] += 1
    if _ip_rotate_counters[site] >= IP_ROTATE_EVERY or _ip_rotate_labels[site] == "":
        _ip_rotate_counters[site] = 0
        if _ip_rotate_labels[site] != "":
            _ip_rotate_idxs[site] = (_ip_rotate_idxs[site] + 1) % len(pool)
        selected = pool[_ip_rotate_idxs[site]]
        label = (
            "main"
            if selected is None
            else (
                selected.split("@")[-1]
                if "@" in selected
                else f"proxy-{_ip_rotate_idxs[site]}"
            )
        )
        _from = _ip_rotate_totals[site]
        _to = _from + IP_ROTATE_EVERY - 1
        _ip_rotate_labels[site] = label
        _msg = f"IP -> {label} ({_from}~{_to}건)"
        logger.info(f"[autotune][{site}] {_msg}")
        now = datetime.now(timezone.utc)
        kst = now + timedelta(hours=9)
        # 메시지에 [IP로테이션] [{site}] 태그를 붙여 프론트 extractSiteFromLog가
        # site를 추출하도록 함 — PC분담 필터(filterSources) 자동 적용으로,
        # 담당 소싱처가 아닌 PC 화면에서는 해당 IP 로테이션 로그가 숨김 처리된다.
        _msg = f"[IP로테이션] [{site}] -> {label} ({_from}~{_to}건)"
        _refresh_log_buffer.append(
            {
                "ts": now.isoformat(),
                "site": site,
                "product_id": "",
                "name": "",
                "msg": f"[{kst.strftime('%H:%M:%S')}] {_msg}",
                "level": "info",
                "source": "autotune",
            }
        )
        _refresh_log_total += 1
    return pool[_ip_rotate_idxs[site]]


# 쿠키 로테이션: 100건마다 다음 쿠키로 전환
COOKIE_ROTATE_EVERY = 100


def _rotate_musinsa_cookie() -> str:
    """벌크 갱신 중 쿠키 로테이션. 100건마다 다음 쿠키로 전환."""
    cookies = _bulk_musinsa_cache.get("cookies", [])
    if not cookies:
        return _bulk_musinsa_cache.get("cookie", "")
    _bulk_musinsa_cache["cookie_usage"] = _bulk_musinsa_cache.get("cookie_usage", 0) + 1
    if _bulk_musinsa_cache["cookie_usage"] >= COOKIE_ROTATE_EVERY:
        _bulk_musinsa_cache["cookie_usage"] = 0
        idx = (_bulk_musinsa_cache.get("cookie_idx", 0) + 1) % len(cookies)
        _bulk_musinsa_cache["cookie_idx"] = idx
        _bulk_musinsa_cache["cookie"] = cookies[idx]
        logger.info(f"[쿠키 로테이션] 쿠키 {idx + 1}/{len(cookies)}로 전환")
    return _bulk_musinsa_cache.get("cookie", "")


# ── 벌크 갱신 취소 플래그 (source별 분리) ──
_cancel_flags: Dict[str, bool] = {"autotune": False, "manual": False, "transmit": False}


def request_bulk_cancel(source: str = "autotune"):
    """특정 source의 벌크 갱신 즉시 중단 요청."""
    _cancel_flags[source] = True


def request_bulk_cancel_all():
    """모든 source의 벌크 갱신 즉시 중단 요청 (서버 종료 등)."""
    for k in _cancel_flags:
        _cancel_flags[k] = True


def clear_bulk_cancel(source: str = "autotune"):
    """특정 source의 취소 플래그 초기화."""
    _cancel_flags[source] = False


def is_bulk_cancelled(source: str = "autotune") -> bool:
    return _cancel_flags.get(source, False)


# ── 실시간 로그 링 버퍼 (최대 300건) ──
_refresh_log_buffer: deque[Dict[str, Any]] = deque(maxlen=300)
_refresh_log_total: int = 0  # 누적 카운터 (밀려나도 증가만)


def _get_current_device_id() -> str:
    """autotune cycle 의 현재 PC owner device_id — 로그 device_id 태깅용.

    cycle 시작 시 collector_autotune.current_pc_owner contextvar 가 세팅됨.
    HTTP/cycle 컨텍스트 없는 경우 빈 문자열(글로벌 메시지로 간주).
    """
    try:
        from backend.api.v1.routers.samba.collector_autotune import current_pc_owner

        return current_pc_owner.get() or ""
    except Exception:
        return ""


def _log_refresh(
    site: str,
    product_id: str,
    product_name: str = "",
    message: str = "",
    level: str = "info",
    idx: int = 0,
    total: int = 0,
    source: str = "autotune",
) -> None:
    """갱신 로그를 링 버퍼에 추가. 오토튠 로그만 저장, 나머지(transmit/manual)는 버림.

    device_id 태깅: 현재 cycle PC owner 자동 첨부. frontend 에서 자기 device_id 로 필터
    하면 다른 PC 잡 로그가 화면에 안 보임 (PC 분리, 2026-05-25 사용자 일주일째 요청).
    """
    current_source = _current_refresh_source.get()
    if current_source != "autotune":
        return
    source = current_source
    global _refresh_log_total
    now = datetime.now(timezone.utc)
    kst = now + timedelta(hours=9)
    ts_str = kst.strftime("%H:%M:%S")
    prefix = f"[{idx:,}/{total:,}] " if idx and total else ""
    site_tag = f"[{site}] " if site else ""
    name_label = f"{product_name[:80]}: " if product_name else ""
    # MUSINSA 인터벌 표시 — 사용자 요청 (2026-05-26): 차단 시 인터벌 증가 추적용.
    # 0(설정 base) → 차단 → 2배씩 → 30s 상한 → 성공 시 점진 복원.
    interval_tag = ""
    if site == "MUSINSA":
        _cur_int = _site_intervals.get("MUSINSA", 1.0)
        interval_tag = f"[int={_cur_int:.1f}s] "
    full_msg = f"[{ts_str}] {prefix}{site_tag}{interval_tag}{name_label}{message}"
    _refresh_log_buffer.append(
        {
            "ts": now.isoformat(),
            "site": site,
            "product_id": product_id,
            "name": "",
            "msg": full_msg,
            "level": level,
            "source": source,
            "device_id": _get_current_device_id(),
        }
    )
    _refresh_log_total += 1


def clear_refresh_logs() -> None:
    """로그 버퍼 초기화."""
    global _refresh_log_total
    _refresh_log_buffer.clear()
    _refresh_log_total = 0


def get_refresh_logs(
    since_idx: int = 0,
    source_filter: str = "",
    device_id_filter: str = "",
) -> tuple[List[Dict[str, Any]], int]:
    """로그 조회. since_idx 이후 로그만 반환 + 누적 인덱스.

    source_filter: "autotune"이면 오토튠 로그만, ""이면 전체.
    device_id_filter: 지정 시 그 device_id 로그만 + 글로벌(device_id 없거나 빈값) 로그
      도 함께 표시 (쿠키 로테이션 등 PC 무관 메시지). 빈 문자열이면 전체(레거시).
    """
    global _refresh_log_total
    buf_len = len(_refresh_log_buffer)
    buf_start = _refresh_log_total - buf_len

    if since_idx >= _refresh_log_total:
        return [], _refresh_log_total
    if since_idx <= buf_start:
        logs = list(_refresh_log_buffer)
    else:
        offset = since_idx - buf_start
        logs = list(_refresh_log_buffer)[offset:]

    if source_filter:
        logs = [l for l in logs if l.get("source") == source_filter]
    if device_id_filter:
        # 쉼표 분리 다중 device_id 허용(브라우저+본인 데몬) + 빈 device_id(글로벌/태깅
        # 누락) 통과. 다른 PC tagged 로그만 명시 차단. ContextVar 가 cycle 안 일부 경로
        # 에서 propagate 안 돼 device_id 빈채로 찍히는 로그가 다수 — strict 차단 시
        # 페이지 0건 사고 (2026-05-25). empty 는 본인 글로벌로 간주.
        allow = {d.strip() for d in device_id_filter.split(",") if d.strip()}
        logs = [
            l for l in logs if not l.get("device_id") or l.get("device_id") in allow
        ]
    return logs, _refresh_log_total


def get_site_intervals_info() -> Dict[str, Any]:
    """사이트별 인터벌 정보 (워룸 표시용)."""
    return {
        "intervals": dict(_site_intervals),
        "errors": dict(_site_consecutive_errors),
        "safe_intervals": dict(_site_safe_intervals),
        "concurrency": dict(SITE_CONCURRENCY),
        "base_intervals": dict(SITE_BASE_INTERVAL),
        "min_intervals": dict(SITE_MIN_INTERVAL),
    }


async def set_site_base_interval(site: str, interval: float) -> None:
    """소싱처 기본 인터벌 동적 변경 (초). DB에 동기적으로 저장."""
    SITE_BASE_INTERVAL[site] = interval
    # 현재 적응형 인터벌도 함께 갱신
    _site_intervals[site] = interval
    # DB에 영속화 (await로 저장 보장)
    await _persist_intervals_to_db()


async def _persist_intervals_to_db() -> None:
    """현재 SITE_BASE_INTERVAL을 DB에 저장."""
    try:
        from backend.db.orm import get_write_session
        from backend.api.v1.routers.samba.proxy import _set_setting

        async with get_write_session() as session:
            await _set_setting(session, "autotune_intervals", dict(SITE_BASE_INTERVAL))
            await session.commit()
    except Exception as e:
        logger.warning("[오토튠] 인터벌 DB 저장 실패: %s", e)


async def load_site_intervals_from_db() -> None:
    """서버 시작 시 DB에서 저장된 인터벌을 로드하여 SITE_BASE_INTERVAL에 반영."""
    try:
        from backend.db.orm import get_read_session
        from backend.api.v1.routers.samba.proxy import _get_setting

        async with get_read_session() as session:
            saved = await _get_setting(session, "autotune_intervals")
        if saved and isinstance(saved, dict):
            for site, val in saved.items():
                if isinstance(val, (int, float)) and 0 <= val <= 60:
                    SITE_BASE_INTERVAL[site] = float(val)
                    _site_intervals[site] = float(val)
    except Exception:
        pass  # 로드 실패 시 기본값 유지


def get_effective_autotune_concurrency() -> dict[str, int]:
    """오토튠 실효 동시성 — SITE_CONCURRENCY 베이스 + SITE_AUTOTUNE_CONCURRENCY 오버라이드 머지."""
    merged: dict[str, int] = dict(SITE_CONCURRENCY)
    merged.update(SITE_AUTOTUNE_CONCURRENCY)
    return merged


async def set_site_autotune_concurrency(site: str, value: int) -> None:
    """오토튠 동시성 동적 변경. DB에 영속화."""
    SITE_AUTOTUNE_CONCURRENCY[site] = int(value)
    await _persist_autotune_concurrency_to_db()


async def _persist_autotune_concurrency_to_db() -> None:
    """현재 SITE_AUTOTUNE_CONCURRENCY를 DB에 저장."""
    try:
        from backend.db.orm import get_write_session
        from backend.api.v1.routers.samba.proxy import _set_setting

        async with get_write_session() as session:
            await _set_setting(
                session, "autotune_concurrency", dict(SITE_AUTOTUNE_CONCURRENCY)
            )
            await session.commit()
    except Exception as e:
        logger.warning("[오토튠] 동시성 DB 저장 실패: %s", e)


async def load_site_autotune_concurrency_from_db() -> None:
    """서버 시작 시 DB에서 저장된 오토튠 동시성을 로드."""
    try:
        from backend.db.orm import get_read_session
        from backend.api.v1.routers.samba.proxy import _get_setting

        async with get_read_session() as session:
            saved = await _get_setting(session, "autotune_concurrency")
        if saved and isinstance(saved, dict):
            for site, val in saved.items():
                if isinstance(val, (int, float)) and 1 <= val <= 50:
                    SITE_AUTOTUNE_CONCURRENCY[site] = int(val)
    except Exception:
        pass  # 로드 실패 시 기본값 유지


@dataclass
class RefreshResult:
    """단일 상품 갱신 결과."""

    product_id: str
    new_sale_price: Optional[float] = None
    new_original_price: Optional[float] = None
    new_cost: Optional[float] = None
    # 무신사 보유 적립금 사용 제외 cost (정책 토글 excludeHeldPoint=True에서 사용)
    new_cost_excl_held_point: Optional[float] = None
    new_sale_status: str = "in_stock"  # in_stock / sold_out
    new_options: Optional[list] = None
    # 수집 시점 일부 경로 버그로 name/brand 가 빈 문자열로 저장된 케이스 백필용.
    # enrich 에서 product.name/brand 가 비어있을 때만 적용 (수동 편집 덮어쓰기 방지).
    new_name: Optional[str] = None
    new_brand: Optional[str] = None
    new_images: Optional[list] = None
    new_detail_images: Optional[list] = None
    new_material: Optional[str] = None
    new_color: Optional[str] = None
    new_free_shipping: Optional[bool] = None
    new_same_day_delivery: Optional[bool] = None
    new_is_point_restricted: Optional[bool] = None
    changed: bool = False
    stock_changed: bool = False
    needs_extension: bool = False
    error: Optional[str] = None
    warnings: list = field(default_factory=list)
    # 소싱처 보조 API(쿠폰/혜택) 실패로 가격 데이터가 불확실한 경우 True
    # True이면 오토튠에서 cost 업데이트 및 전송을 보류함
    price_uncertain: bool = False
    # 소싱처에서 상품 자체가 삭제되어 품절 처리된 경우 True (품절 이벤트 reason 구분용)
    deleted_from_source: bool = False


@dataclass
class BulkRefreshResult:
    """벌크 갱신 요약."""

    total: int = 0
    refreshed: int = 0
    changed: int = 0
    sold_out: int = 0
    retransmitted: int = 0
    needs_extension: list = field(default_factory=list)
    errors: int = 0


# async 컨텍스트별 격리 (전역 변수 레이스 컨디션 방지)
_current_refresh_source: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_refresh_source", default="autotune"
)


async def refresh_product(
    product: Any, idx: int = 0, total: int = 0, source: str = "autotune"
) -> RefreshResult:
    """소싱처에서 최신 가격/재고 재수집. source: autotune | transmit | manual"""
    token = _current_refresh_source.set(source)
    try:
        return await _refresh_product_inner(product, idx, total)
    finally:
        _current_refresh_source.reset(token)


async def _refresh_product_inner(
    product: Any, idx: int = 0, total: int = 0
) -> RefreshResult:
    source_site = getattr(product, "source_site", "")

    # 소싱처 플러그인 우선 호출
    from backend.domain.samba.plugins import SOURCING_PLUGINS

    # DB의 source_site 값과 플러그인 site_name 대소문자 불일치 방어 (예: DB 'Nike' vs site_name 'NIKE').
    # 일치하면 첫 lookup 적중, 불일치면 .upper() fallback. enrich.py 의 동일 패턴.
    plugin = SOURCING_PLUGINS.get(source_site) or (
        SOURCING_PLUGINS.get(source_site.upper()) if source_site else None
    )
    if plugin:
        product._refresh_idx = idx
        product._refresh_total = total
        try:
            result = await plugin.refresh(product)
        except Exception as e:
            logger.error(
                f"[refresher] {product.id} ({source_site}) 플러그인 갱신 실패: {e}",
                exc_info=True,
            )
            return RefreshResult(
                product_id=product.id,
                error=str(e),
            )

        # LOTTEON: benefits API(혜택가) + option/mapping API(재고) 모두
        # 플러그인 refresh()에서 처리 완료 — 확장앱 불필요

        # 오토튠 컨텍스트에서는 콜백이 로그 담당 → 범용 로그 스킵
        if not result.error and _current_refresh_source.get() != "autotune":
            _name = getattr(product, "name", "") or ""
            _sid = getattr(product, "site_product_id", "") or ""
            _label = f"{_name} ({_sid})" if _sid else _name
            _status = "전송" if (result.changed or result.stock_changed) else "스킵"
            _ra = getattr(product, "registered_accounts", None) or []
            _mn = getattr(product, "market_product_nos", None) or {}
            _mi = ""
            if _ra and _mn:
                _ps = [str(_mn.get(a, "")) for a in _ra if _mn.get(a)]
                if _ps:
                    _mi = f" → {','.join(_ps)}"
            _old_p = getattr(product, "sale_price", 0) or 0
            _new_p = (
                result.new_sale_price if result.new_sale_price is not None else _old_p
            )
            _log_refresh(
                source_site,
                product.id,
                _label,
                f"{_status}{_mi} [원가 {int(_old_p):,}>{int(_new_p):,}]",
                idx=idx,
                total=total,
            )
        return result

    # 레거시 폴백 — 소싱처별 파서 선택
    parser = SITE_PARSERS.get(source_site)
    if not parser:
        return RefreshResult(
            product_id=product.id,
            error=f"지원하지 않는 소싱처: {source_site}",
        )

    # idx/total을 thread-local에 임시 저장 (파서에서 접근)
    product._refresh_idx = idx
    product._refresh_total = total

    try:
        result = await parser(product)
        return result
    except Exception as e:
        logger.error(
            f"[refresher] {product.id} ({source_site}) 갱신 실패: {e}",
            exc_info=True,
        )
        return RefreshResult(
            product_id=product.id,
            error=str(e),
        )


# ── 무신사 파서 ──


async def _get_musinsa_cookie() -> str:
    """DB에서 무신사 쿠키 조회 — collector_common 공통 함수 위임."""
    from backend.api.v1.routers.samba.collector_common import get_musinsa_cookie

    return await get_musinsa_cookie()


async def _get_autologin_musinsa_cookie() -> str:
    """자동로그인계정(is_login_default=True) 쿠키 반환 — cost 계산 단일 진실.

    SambaSourcingAccount 풀에서 site_name=MUSINSA + is_login_default=True 계정 1개 선택.
    cookie_expired=True이거나 미설정이면 빈 문자열 반환 → 호출부에서 cost 갱신 차단.
    """
    try:
        from backend.db.orm import get_read_session
        from backend.domain.samba.sourcing_account.service import (
            SambaSourcingAccountService,
        )
        from backend.domain.samba.sourcing_account.repository import (
            SambaSourcingAccountRepository,
        )

        async with get_read_session() as session:
            svc = SambaSourcingAccountService(SambaSourcingAccountRepository(session))
            acc = await svc.get_login_default("MUSINSA")
            if not acc:
                return ""
            af = acc.additional_fields or {}
            if af.get("cookie_expired"):
                return ""
            return af.get("musinsa_cookie", "") or ""
    except Exception:
        return ""


async def _get_musinsa_cookies() -> list[str]:
    """DB에서 무신사 쿠키 목록 조회 (musinsa_cookies JSON 배열 또는 musinsa_cookie 단일).

    반드시 _get_setting을 통해 읽어 암호화 키 자동 복호화. 직접 SQL select 금지
    (암호화된 토큰을 그대로 무신사에 전송해 비로그인 처리되는 이슈 방지).
    """
    try:
        from backend.db.orm import get_read_session
        from backend.api.v1.routers.samba.proxy._helpers import _get_setting
        import json

        async with get_read_session() as session:
            # 먼저 복수 쿠키 키 확인 (_get_setting이 암호화 자동 복호화)
            raw = await _get_setting(session, "musinsa_cookies")
            if raw:
                val = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(val, list) and val:
                    return [c for c in val if c]
            # 없으면 단일 쿠키 폴백
            cookie = await _get_musinsa_cookie()
            return [cookie] if cookie else []
    except Exception:
        cookie = await _get_musinsa_cookie()
        return [cookie] if cookie else []


async def _parse_musinsa(product: Any) -> RefreshResult:
    """무신사 상품 가격/재고 재수집 (MusinsaClient 활용)."""
    from backend.domain.samba.proxy.musinsa import MusinsaClient, RateLimitError

    _idx = getattr(product, "_refresh_idx", 0)
    _total = getattr(product, "_refresh_total", 0)

    site_product_id = getattr(product, "site_product_id", None)
    if not site_product_id:
        return RefreshResult(product_id=product.id, error="site_product_id 없음")

    # 자동로그인계정(is_login_default=True) 쿠키 단일 사용 — cost 일관성 보장
    # 미설정/만료 시 cost 갱신 자체 차단 (계정별 등급/적립률 차이로 인한 stale 방지)
    cookie = await _get_autologin_musinsa_cookie()
    if not cookie:
        return RefreshResult(product_id=product.id, error="MUSINSA_AUTH_MISSING")
    # 오토튠이면 메인↔프록시 IP 로테이션
    _is_autotune = _current_refresh_source.get() == "autotune"
    _proxy = _get_rotated_proxy() if _is_autotune else None
    client = MusinsaClient(cookie, proxy_url=_proxy)
    # 오토튠은 프록시별 공유 HTTP 클라이언트 재사용 (TCP/TLS 핸드셰이크 절감)
    _shared = _get_musinsa_shared_client(_proxy) if _is_autotune else None
    cached_grade_rate = _bulk_musinsa_cache.get("grade_rate")
    warnings: list[str] = []
    # 방어적 초기화: RateLimitError 재시도 경로에서 UnboundLocalError 방지
    detail = None

    try:
        detail = await asyncio.wait_for(
            client.get_goods_detail(
                site_product_id,
                member_grade_rate=cached_grade_rate,
                refresh_only=True,
                _shared_client=_shared,
            ),
            timeout=45,
        )
        # 성공 → 인터벌 점진 복원 (사용자 설정 base_interval을 하한으로 사용)
        base = SITE_BASE_INTERVAL.get("MUSINSA", 1.0)
        step = SITE_INTERVAL_STEP.get("MUSINSA", 0.5)
        prev_interval = _site_intervals.get("MUSINSA", base)
        new_interval = max(base, prev_interval - step)
        _site_intervals["MUSINSA"] = new_interval
        _site_consecutive_errors["MUSINSA"] = 0
        # 정상 응답 → 연속 타임아웃 카운터 리셋 (좀비 클라이언트 복구 트리거 해제)
        _site_consecutive_timeouts["MUSINSA"] = 0
        # 차단 안 당하는 최소 인터벌 기록
        if new_interval <= _site_safe_intervals.get("MUSINSA", 999):
            _site_safe_intervals["MUSINSA"] = new_interval
        pass  # 로그는 변동 판정 후 출력
    except RateLimitError as e:
        # 차단 → 인터벌 2배 증가 (최대 30초)
        current = _site_intervals.get("MUSINSA", 1.0)
        _site_intervals["MUSINSA"] = min(30.0, current * 2)
        _site_consecutive_errors["MUSINSA"] = (
            _site_consecutive_errors.get("MUSINSA", 0) + 1
        )
        _log_refresh(
            "MUSINSA",
            product.id,
            getattr(product, "name", ""),
            f"차단 HTTP {e.status} (연속 {_site_consecutive_errors['MUSINSA']}회, 인터벌→{_site_intervals['MUSINSA']:.1f}s)",
            level="warning",
            idx=_idx,
            total=_total,
        )

        # 연속 5회 이상이면 해당 소싱처 전체 일시 중단
        if _site_consecutive_errors["MUSINSA"] >= 5:
            _log_refresh(
                "MUSINSA",
                product.id,
                getattr(product, "name", ""),
                f"연속 {_site_consecutive_errors['MUSINSA']}회 차단 — 일시 중단",
                level="error",
                idx=_idx,
                total=_total,
            )
            return RefreshResult(
                product_id=product.id,
                error=f"차단 감지: HTTP {e.status} (연속 {_site_consecutive_errors['MUSINSA']}회, "
                f"인터벌 {_site_intervals['MUSINSA']}초)",
            )

        # Retry-After가 있으면 대기 후 1회 재시도 (상한 60초)
        if e.retry_after > 0:
            capped_wait = min(e.retry_after, 60)
            _log_refresh(
                "MUSINSA",
                product.id,
                getattr(product, "name", ""),
                f"[차단대기] Retry-After {capped_wait}초 대기 후 재시도 (원본 {e.retry_after}초)",
                level="warning",
                idx=_idx,
                total=_total,
            )
            logger.warning(
                f"[refresher] {site_product_id} 차단({e.status}), {capped_wait}초 후 재시도 (원본 Retry-After={e.retry_after})"
            )
            await asyncio.sleep(capped_wait)
            try:
                detail = await client.get_goods_detail(
                    site_product_id,
                    member_grade_rate=cached_grade_rate,
                    refresh_only=True,
                    _shared_client=_shared,
                )
                _site_consecutive_errors["MUSINSA"] = 0
            except Exception:
                _log_refresh(
                    "MUSINSA",
                    product.id,
                    getattr(product, "name", ""),
                    f"재시도 실패: HTTP {e.status}",
                    level="error",
                    idx=_idx,
                    total=_total,
                )
                return RefreshResult(
                    product_id=product.id, error=f"차단 후 재시도 실패: HTTP {e.status}"
                )
        else:
            _log_refresh(
                "MUSINSA",
                product.id,
                getattr(product, "name", ""),
                f"[차단] HTTP {e.status} — Retry-After 없음, 건너뜀",
                level="warning",
                idx=_idx,
                total=_total,
            )
            return RefreshResult(product_id=product.id, error=f"차단: HTTP {e.status}")
    except asyncio.TimeoutError:
        # 45초 안에 응답 없음 → 건너뛰기
        # 연속 타임아웃 누적 시 공유 httpx 클라이언트 좀비 의심 → 강제 폐기
        _site_consecutive_timeouts["MUSINSA"] = (
            _site_consecutive_timeouts.get("MUSINSA", 0) + 1
        )
        _consecutive = _site_consecutive_timeouts["MUSINSA"]
        _log_refresh(
            "MUSINSA",
            product.id,
            getattr(product, "name", ""),
            f"응답 없음 (45초 타임아웃, 연속 {_consecutive}회) — 건너뜀",
            level="warning",
            idx=_idx,
            total=_total,
        )
        if _consecutive >= TIMEOUT_RESET_THRESHOLD:
            logger.warning(
                f"[refresher] MUSINSA 연속 {_consecutive}회 타임아웃 → 공유 httpx 클라이언트 풀 강제 폐기 (좀비 연결 복구)"
            )
            try:
                await reset_musinsa_shared_clients()
            except Exception as _e:
                logger.error(f"[refresher] 공유 클라이언트 폐기 실패: {_e}")
            _site_consecutive_timeouts["MUSINSA"] = 0
        return RefreshResult(product_id=product.id, error="응답 없음: 45초 타임아웃")
    except Exception as e:
        _err_brand = getattr(product, "brand", "") or ""
        _err_name = getattr(product, "name", "") or ""
        _err_spid = getattr(product, "site_product_id", "") or ""
        _err_label = (
            f"{_err_brand} {_err_name} ({_err_spid})".strip()
            if _err_spid
            else f"{_err_brand} {_err_name}".strip()
        )
        _err_msg = str(e).strip() or type(e).__name__
        # TCP 좀비 연결류(ConnectError/ConnectTimeout/RemoteProtocolError/ReadError) →
        # asyncio.TimeoutError와 동일 증상(공유 httpx pool 좀비). 카운터 증가 + reset 트리거.
        _err_type_name = type(e).__name__
        _is_connect_error = any(
            _marker in _err_type_name
            for _marker in (
                "ConnectError",
                "ConnectTimeout",
                "RemoteProtocolError",
                "ReadError",
            )
        )
        if _is_connect_error:
            _site_consecutive_timeouts["MUSINSA"] = (
                _site_consecutive_timeouts.get("MUSINSA", 0) + 1
            )
            _consecutive = _site_consecutive_timeouts["MUSINSA"]
            _log_refresh(
                "MUSINSA",
                product.id,
                _err_label,
                f"실패 — {_err_msg} (연속 {_consecutive}회)",
                level="warning",
                idx=_idx,
                total=_total,
            )
            if _consecutive >= TIMEOUT_RESET_THRESHOLD:
                logger.warning(
                    f"[refresher] MUSINSA 연속 {_consecutive}회 연결오류 → 공유 httpx 클라이언트 풀 강제 폐기 (좀비 연결 복구)"
                )
                try:
                    await reset_musinsa_shared_clients()
                except Exception as _e:
                    logger.error(f"[refresher] 공유 클라이언트 폐기 실패: {_e}")
                _site_consecutive_timeouts["MUSINSA"] = 0
            return RefreshResult(
                product_id=product.id, error=f"무신사 API 오류: {_err_msg}"
            )
        if "상품 데이터 없음" in _err_msg:
            # 소싱처 영구 삭제 → 기존 sold_out 플로우와 동일하게 처리
            _log_refresh(
                "MUSINSA",
                product.id,
                _err_label,
                "소싱처 삭제 감지(상품 없음) — 품절 처리",
                level="warning",
                idx=_idx,
                total=_total,
            )
            return RefreshResult(
                product_id=product.id,
                new_sale_status="sold_out",
                changed=True,  # 상태 변경이므로 변동으로 처리 (수동갱신 sold_out 플로우 진입)
                deleted_from_source=True,
            )
        _log_refresh(
            "MUSINSA",
            product.id,
            _err_label,
            f"실패 — {_err_msg}",
            level="error",
            idx=_idx,
            total=_total,
        )
        return RefreshResult(
            product_id=product.id, error=f"무신사 API 오류: {_err_msg}"
        )

    # detail이 None이면 예기치 않은 경로 — 안전하게 에러 반환
    if detail is None:
        _log_refresh(
            "MUSINSA",
            product.id,
            getattr(product, "name", ""),
            "상세 조회 결과 없음",
            level="warning",
            idx=_idx,
            total=_total,
        )
        return RefreshResult(product_id=product.id, error="상품 상세 조회 결과 없음")

    # 결과 처리 전체를 보호 — 예외 발생 시에도 로그 출력
    try:
        return _process_musinsa_detail(
            product, detail, site_product_id, warnings, _idx, _total, _proxy
        )
    except Exception as _proc_e:
        _log_refresh(
            "MUSINSA",
            product.id,
            getattr(product, "name", ""),
            f"처리 오류: {_proc_e}",
            level="error",
            idx=_idx,
            total=_total,
        )
        logger.error(f"[refresher] {product.id} 결과 처리 실패: {_proc_e}")
        return RefreshResult(product_id=product.id, error=f"결과 처리 오류: {_proc_e}")


def _process_musinsa_detail(
    product, detail, site_product_id, warnings, _idx, _total, _proxy=None
) -> RefreshResult:
    """무신사 상세 결과 처리 — 변동 판정 + 로그."""

    new_sale_price = detail.get("salePrice", 0) or 0
    new_original_price = detail.get("originalPrice", 0) or 0
    new_cost = detail.get("bestBenefitPrice")
    if new_cost is not None and new_cost <= 0:
        new_cost = None
    # 보유 적립금 사용 제외 cost (정책 토글용) — 무신사만 별도 계산값 제공
    new_cost_excl_held_point = detail.get("bestBenefitPriceExclHeldPoint")
    if new_cost_excl_held_point is not None and new_cost_excl_held_point <= 0:
        new_cost_excl_held_point = None
    new_sale_status = detail.get("saleStatus", "in_stock")
    new_options = detail.get("options")

    # 품절 상품인데 API가 가격 0 반환 → 기존 가격 보존
    if new_sale_status == "sold_out" and new_sale_price == 0:
        old_sp = getattr(product, "sale_price", 0) or 0
        if old_sp > 0:
            new_sale_price = old_sp
            logger.info(
                f"[refresher] {site_product_id} 품절+가격0 → 기존 판매가 {old_sp:,} 보존"
            )
    if new_sale_status == "sold_out" and new_original_price == 0:
        old_op = getattr(product, "original_price", 0) or 0
        if old_op > 0:
            new_original_price = old_op
    # 품절 시 원가도 기존값 보존
    if new_sale_status == "sold_out" and new_cost is None:
        old_cost = getattr(product, "cost", None)
        if old_cost and old_cost > 0:
            new_cost = old_cost
    if new_sale_status == "sold_out" and new_cost_excl_held_point is None:
        old_excl = getattr(product, "cost_excl_held_point", None)
        if old_excl and old_excl > 0:
            new_cost_excl_held_point = old_excl

    # 품절 상품 옵션 가격 0 → 기존 옵션 가격 보존
    if new_sale_status == "sold_out" and new_options:
        all_zero = all((o.get("price", 0) or 0) == 0 for o in new_options)
        if all_zero:
            old_opts = getattr(product, "options", None) or []
            old_price_map = {
                (o.get("name", "") or o.get("size", "")): o.get("price", 0)
                for o in old_opts
                if (o.get("price", 0) or 0) > 0
            }
            if old_price_map:
                for o in new_options:
                    key = o.get("name", "") or o.get("size", "")
                    if key in old_price_map:
                        o["price"] = old_price_map[key]
                logger.info(
                    f"[refresher] {site_product_id} 품절+옵션가격0 → 기존 옵션 가격 복원"
                )

    # 부분 성공 경고: 주요 필드 누락 감지
    if new_sale_price == 0 and new_original_price == 0:
        warnings.append("salePrice/originalPrice 모두 0 — 무신사 API 구조 변경 가능성")
    if detail.get("name") is None or detail.get("name") == "":
        warnings.append("goodsNm 필드 누락 — 무신사 API 구조 변경 가능성")

    # 경고가 있으면 모니터링 이벤트 발행 (fire-and-forget)
    if warnings:
        try:
            # 서비스 레이어 없이 직접 로그 — 세션이 없으므로 로그만 남김
            logger.warning(f"[refresher] API 구조 변경 감지: {warnings}")
        except Exception:
            pass

    # 변동 판정
    old_sale = getattr(product, "sale_price", 0) or 0
    old_original = getattr(product, "original_price", 0) or 0
    old_cost = getattr(product, "cost", None)
    old_status = getattr(product, "sale_status", "in_stock")

    _old_cost_int = int(old_cost) if old_cost else 0
    _new_cost_int = int(new_cost) if new_cost else 0
    cost_changed = new_cost is not None and _new_cost_int != _old_cost_int
    changed = (
        new_sale_price != old_sale or new_sale_status != old_status or cost_changed
    )

    # 옵션 재고 변동 건수 — 품절↔재고 전환(무↔유)만 카운트
    # (단순 수량변화 제외, 공용 헬퍼 count_stock_transitions 사용)
    old_options = getattr(product, "options", None) or []
    _stock_changes = count_stock_transitions(old_options, new_options)
    if new_options and old_options and _stock_changes > 0:
        old_stock_map = {
            (o.get("name", "") or o.get("size", "")): o.get("stock", 0)
            for o in old_options
        }
        for o in new_options:
            key = o.get("name", "") or o.get("size", "")
            old_stock = old_stock_map.get(key, 0) or 0
            new_stock = o.get("stock", 0) or 0
            was_soldout = old_stock <= 0
            is_soldout = new_stock <= 0 or o.get("isSoldOut", False)
            if was_soldout != is_soldout:
                logger.info(
                    "[재고변동감지] %s %s: DB=%s(sold=%s) → API=%s(sold=%s)",
                    site_product_id,
                    key,
                    old_stock,
                    was_soldout,
                    new_stock,
                    is_soldout,
                )
    if not (new_options and old_options):
        if not old_options and new_options:
            logger.warning(
                "[재고변동] %s DB옵션없음(len=%d), API옵션=%d개",
                site_product_id,
                len(old_options),
                len(new_options),
            )
        elif not new_options:
            logger.warning("[재고변동] %s API옵션없음", site_product_id)

    # 상품명 (품번) 형태 + 마켓/계정 정보
    _brand = getattr(product, "brand", "") or ""
    _name = getattr(product, "name", "") or ""
    _prod_label = (
        f"{_brand} {_name} ({site_product_id})"
        if site_product_id
        else f"{_brand} {_name}"
    )
    _prod_label = _prod_label.strip()
    # 로그는 콜백(_on_result)에서 통합 출력 — refresher에서는 생략

    return RefreshResult(
        product_id=product.id,
        new_sale_price=new_sale_price,
        new_original_price=new_original_price,
        new_cost=new_cost,
        new_cost_excl_held_point=new_cost_excl_held_point,
        new_sale_status=new_sale_status,
        new_options=new_options,
        new_is_point_restricted=detail.get("isPointRestricted"),
        changed=changed,
        stock_changed=_stock_changes > 0,
        warnings=warnings,
        price_uncertain=bool(detail.get("price_uncertain")),
    )


# ── KREAM 파서 (확장앱 큐 방식) ──


async def _parse_kream(product: Any) -> RefreshResult:
    """KREAM 상품 가격/재고 재수집 — 확장앱 큐를 통한 자동 수집.

    흐름:
    1. KreamClient.collect_queue에 job 등록
    2. 확장앱이 폴링으로 job을 가져감
    3. 확장앱이 KREAM 탭 열어서 데이터 수집
    4. 확장앱이 collect-result로 결과 전달
    5. asyncio.Future로 결과 수신
    """
    import uuid
    from backend.domain.samba.proxy.kream import KreamClient

    site_product_id = getattr(product, "site_product_id", None)
    if not site_product_id:
        return RefreshResult(product_id=product.id, error="site_product_id 없음")

    request_id = str(uuid.uuid4())
    url = f"https://kream.co.kr/products/{site_product_id}"

    # 큐에 job 등록
    KreamClient.collect_queue.append(
        {
            "requestId": request_id,
            "productId": site_product_id,
            "url": url,
        }
    )
    logger.info(f"[KREAM 갱신] 큐 등록: {site_product_id} ({request_id})")
    _log_refresh(
        "KREAM",
        product.id,
        getattr(product, "name", ""),
        f"확장앱 큐 등록: {site_product_id}",
    )

    # Future 생성 — 확장앱이 결과를 전달하면 resolve됨
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    KreamClient.collect_resolvers[request_id] = future

    try:
        result = await asyncio.wait_for(future, timeout=KREAM_TIMEOUT)
    except asyncio.TimeoutError:
        KreamClient.collect_resolvers.pop(request_id, None)
        _log_refresh(
            "KREAM",
            product.id,
            getattr(product, "name", ""),
            f"확장앱 타임아웃 ({KREAM_TIMEOUT}초)",
            level="warning",
        )
        return RefreshResult(
            product_id=product.id,
            needs_extension=True,
            error=f"KREAM 확장앱 타임아웃 ({KREAM_TIMEOUT}초)",
        )

    # 결과 파싱
    if not isinstance(result, dict):
        return RefreshResult(product_id=product.id, error="KREAM 결과 형식 오류")

    ext_product = result.get("product", result)
    if not ext_product.get("success", True) if "success" in result else True:
        return RefreshResult(
            product_id=product.id,
            error=result.get("message", "KREAM 수집 실패"),
        )

    # 확장앱이 반환한 데이터에서 가격/옵션 추출
    new_options = ext_product.get("options", [])
    new_sale_price = ext_product.get("salePrice", 0) or 0
    new_original_price = ext_product.get("originalPrice", 0) or 0

    # 품절 판정: 재고 있는 옵션이 하나도 없으면 품절
    in_stock_count = sum(1 for o in new_options if o.get("stock", 0) > 0)
    new_sale_status = (
        "sold_out" if (new_options and in_stock_count == 0) else "in_stock"
    )

    # 변동 판정
    old_sale = getattr(product, "sale_price", 0) or 0
    old_original = getattr(product, "original_price", 0) or 0
    old_status = getattr(product, "sale_status", "in_stock")

    changed = (
        new_sale_price != old_sale
        or new_original_price != old_original
        or new_sale_status != old_status
    )

    # 옵션 재고 변동 — 품절↔재고 전환(무↔유)만 카운트 (단순 수량변화 제외)
    old_options = getattr(product, "options", None) or []
    _stock_changes = count_stock_transitions(old_options, new_options)

    # 마켓 정보
    _reg_accounts = getattr(product, "registered_accounts", None) or []
    _market_nos = getattr(product, "market_product_nos", None) or {}
    _minfo = ""
    if _reg_accounts and _market_nos:
        _mparts = [
            str(_market_nos.get(a, "")) for a in _reg_accounts if _market_nos.get(a)
        ]
        if _mparts:
            _minfo = f" → {','.join(_mparts)}"
    msg = (
        f"완료{_minfo}: 가격 {old_sale}→{new_sale_price}, 상태 {old_status}→{new_sale_status}"
        + (", 변동 감지" if changed else "")
    )
    _log_refresh(
        "KREAM",
        product.id,
        getattr(product, "name", ""),
        msg,
    )
    logger.info(
        f"[KREAM 갱신] 완료: {site_product_id} "
        f"가격 {old_sale}→{new_sale_price}, 상태 {old_status}→{new_sale_status}, "
        f"변동={'Y' if changed else 'N'}"
    )

    return RefreshResult(
        product_id=product.id,
        new_sale_price=new_sale_price,
        new_original_price=new_original_price,
        new_sale_status=new_sale_status,
        new_options=new_options,
        changed=changed,
        stock_changed=_stock_changes > 0,
    )


# ── 범용 HTTP 파서 (ABCmart, Nike 등 — 현재 stub) ──


def count_stock_transitions(old_options: list | None, new_options: list | None) -> int:
    """옵션별 품절↔재고 전환(무↔유) 건수만 카운트.

    단순 수량 변화(예: 3→2)는 제외 — 자동튠 이벤트/전송 트리거 공용 기준.
    신규 소싱처 파서는 반드시 이 함수를 통해 stock_changed 를 판정할 것.
    """
    if not old_options or not new_options:
        return 0
    old_map = {
        (o.get("name", "") or o.get("size", "")): (o.get("stock", 0) or 0)
        for o in old_options
    }
    cnt = 0
    for o in new_options:
        key = o.get("name", "") or o.get("size", "")
        old_s = old_map.get(key, 0) or 0
        new_s = o.get("stock", 0) or 0
        was_soldout = old_s <= 0
        is_soldout = new_s <= 0 or o.get("isSoldOut", False)
        if was_soldout != is_soldout:
            cnt += 1
    return cnt


def _has_stock_diff(old_options: list | None, new_options: list | None) -> bool:
    """옵션 재고 품절↔재고 전환 여부 판별 (단순 수량변화 제외)."""
    return count_stock_transitions(old_options, new_options) > 0


async def _parse_fashionplus(product: Any) -> RefreshResult:
    """패션플러스 가격/재고 갱신 — 검색 API + 상세 페이지."""
    from backend.domain.samba.proxy.fashionplus import FashionPlusClient

    pid = getattr(product, "site_product_id", "")
    if not pid:
        return RefreshResult(product_id=product.id, error="site_product_id 없음")

    client = FashionPlusClient()
    try:
        detail = await client.get_detail(pid)
    except Exception as e:
        return RefreshResult(product_id=product.id, error=f"상세 조회 실패: {e}")

    new_sale = detail.get("sale_price", 0) or 0
    new_orig = detail.get("original_price", 0) or new_sale
    shipping_fee = detail.get("shipping_fee", 0) or 0
    new_cost = new_sale + shipping_fee

    old_sale = getattr(product, "sale_price", 0) or 0
    old_cost = getattr(product, "cost", 0) or 0
    changed = (new_sale != old_sale) or (new_cost != old_cost)

    logger.info(
        f"[패션플러스 갱신] {pid}: "
        f"원가 {old_cost}→{new_cost}, 판매가 {old_sale}→{new_sale}, 배송비 {shipping_fee}"
    )
    new_options = detail.get("options") or None
    # 옵션 기반 품절 판정: 모든 옵션 재고 0이면 sold_out
    is_sold_out = False
    if new_options:
        is_sold_out = all(
            (opt.get("stock", 0) if isinstance(opt, dict) else 0) <= 0
            for opt in new_options
        )
    new_sale_status = "sold_out" if is_sold_out else "in_stock"
    return RefreshResult(
        product_id=product.id,
        new_sale_price=new_sale,
        new_original_price=new_orig,
        new_cost=new_cost,
        new_sale_status=new_sale_status,
        new_options=new_options,
        changed=changed,
        stock_changed=bool(
            new_options
            and _has_stock_diff(getattr(product, "options", None), new_options)
        ),
    )


async def _parse_generic_stub(product: Any) -> RefreshResult:
    """범용 스텁 파서 — 실제 파싱은 소싱처별 HTML 구조에 맞게 확장 예정."""
    return RefreshResult(
        product_id=product.id,
        new_sale_price=getattr(product, "sale_price", 0),
        new_original_price=getattr(product, "original_price", 0),
        new_cost=getattr(product, "cost", None),
        new_sale_status=getattr(product, "sale_status", "in_stock"),
        changed=False,
    )


# 소싱처별 파서 매핑
SITE_PARSERS: dict[str, Any] = {
    "MUSINSA": _parse_musinsa,
    "KREAM": _parse_kream,
    "ABCmart": _parse_generic_stub,
    "Nike": _parse_generic_stub,
    "Adidas": _parse_generic_stub,
    "GrandStage": _parse_generic_stub,
    "REXMONDE": _parse_generic_stub,
    "LOTTEON": _parse_generic_stub,
    "GSShop": _parse_generic_stub,
    "ElandMall": _parse_generic_stub,
    "SSF": _parse_generic_stub,
    "FashionPlus": _parse_fashionplus,
    "SNKRDUNK": _parse_generic_stub,
}


async def refresh_products_bulk(
    products: List[Any],
    source: str = "autotune",
    max_concurrency: dict[str, int] | int | None = None,
    on_result: Any = None,
    global_counter: dict | None = None,
) -> tuple[List[RefreshResult], BulkRefreshResult]:
    """여러 상품을 소싱처별로 그룹핑 후 병렬 갱신.

    소싱처당 동시 요청 수를 CONCURRENCY_PER_SITE로 제한한다.
    max_concurrency: int 지정 시 전체 소싱처 동일 적용, dict 지정 시 소싱처별 오버라이드
    source: autotune | manual | transmit — 로그 출처 태그
    on_result: 각 상품 갱신 완료 시 호출되는 콜백 (product, result) → 즉시 전송 등
    """
    if not products:
        return [], BulkRefreshResult()

    # 시작 시 해당 source의 취소 플래그 초기화
    clear_bulk_cancel(source)

    # 소싱처별 그룹핑
    by_site: dict[str, list] = {}
    for p in products:
        site = getattr(p, "source_site", "unknown")
        by_site.setdefault(site, []).append(p)

    all_results: List[RefreshResult] = []
    summary = BulkRefreshResult(total=len(products))

    async def _process_site(site: str, items: list) -> List[RefreshResult]:
        # 소싱처별 카운터 (번호 건너뜀 방지)
        _counter = {"i": 0}
        _site_total = len(items)
        # IP 로테이션 카운터 초기화 — 사이클마다 1~N건으로 리셋 (누적 방지)
        _ip_rotate_counters[site] = 0
        _ip_rotate_totals[site] = 0
        _ip_rotate_labels[site] = ""
        # _ip_rotate_idxs 는 유지 (프록시 순서 연속성 보존) — 최초 실행 시만 0으로 초기화
        if site not in _ip_rotate_idxs:
            _ip_rotate_idxs[site] = 0
        # 소싱처별 사전 캐싱 (배치 시작 시 1회)
        if site == "MUSINSA":
            await _prepare_musinsa_cache()
        elif site in ("ABCmart", "GrandStage"):
            # 확장앱이 sync한 로그인 쿠키 → 잡 시작 시 강제 재로드
            # (lazy-load는 1회만 동작하므로 잡 사이 새 sync 반영 위해 강제 리셋)
            from backend.domain.samba.proxy.abcmart import (
                ARTSourcingClient,
                prepare_abcmart_cache,
            )

            ARTSourcingClient._bulk_cache = {}
            await prepare_abcmart_cache()
        if isinstance(max_concurrency, dict):
            concurrency = max_concurrency.get(
                site, SITE_CONCURRENCY.get(site, CONCURRENCY_PER_SITE)
            )
        elif max_concurrency:
            concurrency = max_concurrency
        else:
            concurrency = SITE_CONCURRENCY.get(site, CONCURRENCY_PER_SITE)
        base_interval = SITE_BASE_INTERVAL.get(site, 1.0)
        sem = asyncio.Semaphore(concurrency)
        results = []

        async def _limited(p: Any) -> RefreshResult:
            async with sem:
                # 취소 요청 시 즉시 중단 (자기 source만 체크)
                if _cancel_flags.get(source, False):
                    return RefreshResult(
                        product_id=getattr(p, "id", "unknown"), error="cancelled"
                    )
                _counter["i"] += 1
                _idx = _counter["i"]
                # 사이클 전체 카운터 (호출측에서 주입) — 로그 prefix [idx/total] 분모를
                # 배치 크기(200) 가 아닌 사이클 전체(N만) 기준으로 통일.
                if global_counter:
                    _gk = global_counter.get("key")
                    # `or {}` 금지 — idx_ref가 빈 dict({})면 falsy라 매번 새 throwaway dict가
                    # 생성돼 증가분이 모듈 dict에 안 남는다(순번 1 고정 버그). None일 때만 폴백.
                    _idx_ref = global_counter.get("idx_ref")
                    if _idx_ref is None:
                        _idx_ref = {}
                    _total_ref = global_counter.get("total_ref")
                    if _total_ref is None:
                        _total_ref = {}
                    _idx_ref[_gk] = _idx_ref.get(_gk, 0) + 1
                    _g_idx = _idx_ref[_gk]
                    _g_total = _total_ref.get(_gk, 0)
                else:
                    _g_idx = 0
                    _g_total = 0
                _log_idx = _g_idx if (_g_idx and _g_total) else _idx
                _log_total = _g_total if (_g_idx and _g_total) else _site_total
                _product_timeout = get_product_timeout(site)
                try:
                    r = await asyncio.wait_for(
                        refresh_product(p, idx=_idx, total=_site_total, source=source),
                        timeout=_product_timeout,
                    )
                except asyncio.TimeoutError:
                    _log_refresh(
                        site,
                        getattr(p, "id", "unknown"),
                        getattr(p, "name", ""),
                        f"전체 처리 타임아웃 ({_product_timeout}초) — 건너뜀",
                        level="warning",
                        idx=_log_idx,
                        total=_log_total,
                    )
                    r = RefreshResult(
                        product_id=getattr(p, "id", "unknown"),
                        error=f"전체 처리 타임아웃: {_product_timeout}초",
                    )
                # 실패 시 1회 재시도 (오토튠만)
                if r.error and source == "autotune":
                    interval = max(0.1, _site_intervals.get(site, base_interval))
                    await asyncio.sleep(interval)
                    try:
                        r = await asyncio.wait_for(
                            refresh_product(
                                p, idx=_idx, total=_site_total, source=source
                            ),
                            timeout=_product_timeout,
                        )
                        if not r.error:
                            _rb = getattr(p, "brand", "") or ""
                            _rn = getattr(p, "name", "") or ""
                            _rs = getattr(p, "site_product_id", "") or ""
                            _rl = (
                                f"{_rb} {_rn} ({_rs})".strip()
                                if _rs
                                else f"{_rb} {_rn}".strip()
                            )
                            _log_refresh(
                                site,
                                getattr(p, "id", "unknown"),
                                _rl,
                                "재시도 성공",
                                idx=_log_idx,
                                total=_log_total,
                            )
                    except asyncio.TimeoutError:
                        pass  # 재시도도 실패 → 원래 에러 유지
                # 에러 건도 로그에 표시 (on_result 콜백 전)
                if r.error and source == "autotune":
                    _rb = getattr(p, "brand", "") or ""
                    _rn = getattr(p, "name", "") or ""
                    _rs = getattr(p, "site_product_id", "") or ""
                    _rl = (
                        f"{_rb} {_rn} ({_rs})".strip()
                        if _rs
                        else f"{_rb} {_rn}".strip()
                    )
                    _err_short = (r.error or "")[:60]
                    _log_refresh(
                        site,
                        getattr(p, "id", "unknown"),
                        _rl,
                        f"실패: {_err_short}",
                        level="warning",
                        idx=_log_idx,
                        total=_log_total,
                    )
                # 콜백 호출 (리프레시 직후 즉시 전송 등)
                if on_result and not r.error:
                    try:
                        await on_result(p, r, _log_idx, _log_total)
                    except Exception as cb_err:
                        logger.warning("[오토튠] on_result 콜백 오류: %s", cb_err)
                # 소싱처별 적응형 인터벌 (기본값은 소싱처별 base_interval, 최소 0.1초)
                interval = max(0.1, _site_intervals.get(site, base_interval))
                await asyncio.sleep(interval)
                return r

        tasks = [_limited(p) for p in items]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [
            r
            if isinstance(r, RefreshResult)
            else RefreshResult(product_id="unknown", error=str(r))
            for r in results
        ]

    # 소싱처별 병렬 실행
    site_tasks = [_process_site(site, items) for site, items in by_site.items()]
    site_results = await asyncio.gather(*site_tasks)

    for results in site_results:
        for r in results:
            all_results.append(r)
            if r.error:
                summary.errors += 1
            elif r.needs_extension:
                summary.needs_extension.append(r.product_id)
            else:
                summary.refreshed += 1
                if r.changed:
                    summary.changed += 1
                if r.new_sale_status == "sold_out":
                    summary.sold_out += 1

    return all_results, summary

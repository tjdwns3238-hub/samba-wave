"""헤드리스 데몬 풀 사이트별 owner 선택 헬퍼.

(2026-05-25 정리) 단일 룰: PC 체크박스(`_pc_allowed_sites`) 만 본다.
env fallback / 자동 정책 모두 폐기. 매칭 없으면 무조건 None → 잡 발행 skip.
"""

from __future__ import annotations

import time
from threading import Lock

_rr_counters: dict[str, int] = {}
_rr_lock = Lock()


def pick_daemon_owner(site: str, settings_obj: object | None = None) -> str | None:
    """site 를 체크한 데몬 device 1개를 round-robin 선택 (prefix='samba-daemon-')."""
    return _pick_owner_with_prefix(site, daemon_only=True)


def pick_extension_owner(site: str) -> str | None:
    """site 를 체크한 확장앱 device 1개를 round-robin 선택 (prefix 무관, samba-daemon- 제외)."""
    return _pick_owner_with_prefix(site, daemon_only=False)


def pick_any_owner(site: str) -> str | None:
    """site 를 체크한 PC 1개 — 데몬/확장앱 무관. PC 체크박스 단일 룰."""
    d = pick_daemon_owner(site)
    if d:
        return d
    return pick_extension_owner(site)


def _pick_owner_with_prefix(site: str, daemon_only: bool) -> str | None:
    pool: list[str] = []
    try:
        from backend.api.v1.routers.samba.collector_autotune import (
            _pc_allowed_sites,
            _pc_last_seen,
        )

        now = time.time()
        _site_u = (site or "").upper()
        for dev, sites in _pc_allowed_sites.items():
            is_daemon = dev.startswith("samba-daemon-")
            if daemon_only and not is_daemon:
                continue
            if (not daemon_only) and is_daemon:
                continue
            if _site_u not in {s.upper() for s in sites}:
                continue
            last = _pc_last_seen.get(dev, 0)
            if now - last > 60:
                continue
            pool.append(dev)
        pool.sort()
    except Exception:
        pool = []

    if not pool:
        return None

    key = ("daemon:" if daemon_only else "ext:") + site
    with _rr_lock:
        idx = _rr_counters.get(key, 0) % len(pool)
        _rr_counters[key] = idx + 1
    return pool[idx]

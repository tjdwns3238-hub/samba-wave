"""헤드리스 데몬 풀 사이트별 owner 선택 헬퍼.

`samba-daemon-*` prefix device 들 중 polling 중인 데몬을 사이트별로 round-robin
선택한다. LOTTEON 만 사용하던 `_pick_autotune_daemon_owner(settings)`(lotteon.py)
로직을 사이트 인자로 일반화한 모듈이다.

선택 규칙:
1. DB 활성 데몬 풀 — `_pc_allowed_sites[dev]` 에 site 가 포함되고 last_seen 60초
   이내 polling 중인 device 들.
2. autotune_daemon_device_ids (env, 콤마 구분) — LOTTEON 하위호환 폴백.
3. autotune_daemon_device_id (env, 단수) — 하위호환.

풀이 비어있으면 None 반환 → 호출처는 기존 확장앱(`get_autotune_owner`) 흐름 폴백.
"""

from __future__ import annotations

import time
from threading import Lock

_rr_counters: dict[str, int] = {}
_rr_lock = Lock()


def pick_daemon_owner(site: str, settings_obj: object | None = None) -> str | None:
    """주어진 site 를 처리할 데몬 device_id 1개를 round-robin 선택."""
    pool: list[str] = []
    try:
        from backend.api.v1.routers.samba.collector_autotune import (
            _pc_allowed_sites,
            _pc_last_seen,
        )

        now = time.time()
        # 케이싱 무관 매칭 — 데몬 등록 사이트는 'ABCmart'(혼합)인데 송장 site 는
        # 'ABCMART'(대문자)로 들어오므로 UPPER 비교. detail('ABCmart')도 그대로 매칭됨.
        _site_u = (site or "").upper()
        for dev, sites in _pc_allowed_sites.items():
            if not dev.startswith("samba-daemon-"):
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

    if not pool and site == "LOTTEON":
        if settings_obj is None:
            try:
                from backend.core.config import settings as _settings

                settings_obj = _settings
            except Exception:
                settings_obj = None

        if settings_obj is not None:
            raw_pool = (
                getattr(settings_obj, "autotune_daemon_device_ids", "") or ""
            ).strip()
            pool = [s.strip() for s in raw_pool.split(",") if s.strip()]
            if not pool:
                legacy = (
                    getattr(settings_obj, "autotune_daemon_device_id", "") or ""
                ).strip()
                if legacy:
                    pool = [legacy]

    if not pool:
        return None

    with _rr_lock:
        idx = _rr_counters.get(site, 0) % len(pool)
        _rr_counters[site] = idx + 1
    return pool[idx]

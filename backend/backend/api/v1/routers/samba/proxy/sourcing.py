"""소싱 관련 엔드포인트 (sourcing_queue_router 포함)."""

import asyncio
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.core.rate_limit import RATE_SET_COOKIE, limiter
from backend.db.orm import get_write_session_dependency

from ._helpers import _set_setting

router = APIRouter(tags=["samba-proxy"])

# 확장앱 소싱큐 전용 라우터 — 인증 불필요 (확장앱이 토큰 없이 폴링)
sourcing_queue_router = APIRouter(prefix="/proxy", tags=["samba-proxy-public"])

EXTENSION_SITES = {
    "ABCmart",
    "GrandStage",
    "REXMONDE",
    "LOTTEON",
    "GSShop",
    "ElandMall",
    "SSF",
}


def _get_sourcing_client(site: str):
    """직접 API 클라이언트 반환."""
    s = site.lower()
    if s in ("fashionplus", "fp"):
        from backend.domain.samba.proxy.fashionplus import FashionPlusClient

        return FashionPlusClient()
    if s == "nike":
        from backend.domain.samba.proxy.nike import NikeClient

        return NikeClient()
    if s == "adidas":
        from backend.domain.samba.proxy.adidas import AdidasClient

        return AdidasClient()
    if s == "naverstore":
        from backend.domain.samba.proxy.naverstore_sourcing import (
            NaverStoreSourcingClient,
        )

        return NaverStoreSourcingClient()
    return None


class LotteonSetCookieRequest(BaseModel):
    cookie: str


@sourcing_queue_router.post("/lotteon/set-cookie")
@limiter.limit(RATE_SET_COOKIE)
async def lotteon_set_cookie(
    request: Request,
    body: LotteonSetCookieRequest = Body(...),
    write_session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """확장앱에서 롯데ON 쿠키 수신.

    (2026-05-20) owner_device_ids 가드 적용 — 포크 확장앱이 원본 백엔드로
    쿠키 미러 전송하던 누수 차단.
    """
    from backend.api.v1.routers.samba.sourcing_account import _check_owner_device

    _check_owner_device(request)

    if not body.cookie:
        raise HTTPException(status_code=400, detail="쿠키가 필요합니다.")
    await _set_setting(write_session, "lotteon_cookie", body.cookie)
    # 메모리 캐시에도 즉시 반영
    from backend.domain.samba.proxy.lotteon_sourcing import set_lotteon_cookie
    from backend.utils.logger import logger

    set_lotteon_cookie(body.cookie)
    cookie_count = len(body.cookie.split(";"))
    logger.info(f"[LOTTEON] 확장앱에서 쿠키 수신: {cookie_count}개")
    return {"success": True, "cookieCount": cookie_count}


@sourcing_queue_router.get("/sourcing/collect-queue", response_model=None)
async def sourcing_collect_queue(request: Request) -> Any:
    """확장앱이 폴링하는 소싱 수집 큐 (인증 불필요).

    확장앱은 다음 헤더를 전달한다:
      - `X-Device-Id`: 확장앱 고유 deviceId (owner 매칭용)
      - `X-Allowed-Sites`: 이 PC가 처리할 사이트 콤마 구분 목록 (popup 설정)
        예) "ABCmart,MUSINSA" — 그 사이트의 작업만 받음. 비어있으면 모든 사이트.

    PC 분담 시나리오:
      PC A popup: ABCmart, MUSINSA → A 익스텐션은 그 사이트 작업만 처리
      PC B popup: LOTTEON, SSG     → B 익스텐션은 그 사이트 작업만 처리
    """
    if getattr(request.app.state, "is_shutting_down", False):
        return JSONResponse(
            status_code=503,
            content={"hasJob": False, "shuttingDown": True},
            headers={"Connection": "close"},
        )
    from backend.domain.samba.proxy.sourcing_queue import SourcingQueue

    device_id = request.headers.get("X-Device-Id", "").strip()

    # PC 개별 중지 신호 — 이 PC에게만 forceStop (다른 PC는 계속 동작)
    try:
        from backend.api.v1.routers.samba.collector_autotune import _pc_force_stop_set

        if device_id and device_id in _pc_force_stop_set:
            _pc_force_stop_set.discard(device_id)
            return {"hasJob": False, "forceStop": True}
    except Exception:
        pass

    # PC 분담 last_seen 갱신 + allowed_sites 동기화
    # 서버 재시작 후엔 startup 단계에서 DB로 복원되며, 폴링 헤더로도 보조 동기화.
    try:
        from backend.api.v1.routers.samba.collector_autotune import (
            persist_pc_allowed_sites,
            register_pc_allowed_sites,
            touch_daemon_presence,
            update_pc_last_seen,
        )

        update_pc_last_seen(device_id)
        _is_daemon = device_id.startswith("samba-daemon-")
        if _is_daemon:
            # 데몬은 자기 사이트를 헤더(X-Allowed-Sites)로 '선언'하지 않는다 —
            # 폴링 union 이 배정을 부풀려 UI 지정을 무력화하던 문제 차단.
            # 사이트 출처는 오직 UI POST(/autotune/pc-allowed-sites, authoritative).
            # 여기선 last_seen 갱신 + 미등록 데몬 1회 빈 등록(UI 목록 노출)만 한다.
            if touch_daemon_presence(device_id):
                from backend.db.orm import get_write_session

                async with get_write_session() as _persist_sess:
                    await persist_pc_allowed_sites(_persist_sess)
                    await _persist_sess.commit()
        else:
            # 확장앱: 기존 동작 — 폴링 헤더로 union 등록(같은 deviceId flip-flop 방지)
            raw_sites_for_reg = request.headers.get("X-Allowed-Sites")
            if device_id and raw_sites_for_reg is not None:
                sites_for_reg = [
                    s.strip() for s in raw_sites_for_reg.split(",") if s.strip()
                ]
                # 변경 발생 시에만 DB 영속화 — 매 폴링 write 부담 회피
                if register_pc_allowed_sites(device_id, sites_for_reg):
                    from backend.db.orm import get_write_session

                    async with get_write_session() as _persist_sess:
                        await persist_pc_allowed_sites(_persist_sess)
                        await _persist_sess.commit()
    except Exception:
        pass
    # X-Allowed-Sites 헤더 의미:
    #   - 헤더 미부착(None) = 디폴트 '전체 처리' (단일 PC 운영)
    #   - 빈 문자열 ""     = 명시적 '아무 작업도 안 받음' (분담 외 PC)
    #   - "ABCmart,..."    = 그 사이트 작업만 받음
    raw_sites = request.headers.get("X-Allowed-Sites")
    if raw_sites is None:
        allowed_sites: list[str] | None = None
    else:
        allowed_sites = [s.strip() for s in raw_sites.split(",") if s.strip()]
    # X-Poll-Site: 이번 폴링이 dequeue 할 단일 사이트 (사이트별 병렬 데몬 워커용).
    # 등록(X-Allowed-Sites=전체)과 잡필터(X-Poll-Site=단일)를 분리해, 병렬 워커가
    # 단일 사이트로 폴링해도 _pc_allowed_sites 등록값이 전체로 유지된다.
    # → pick_daemon_owner(site) 가 모든 사이트에서 이 데몬을 찾음(60s 타임아웃 회귀 차단).
    # 헤더 미부착(확장앱/단일 PC)이면 기존 X-Allowed-Sites 전체 필터 그대로.
    _poll_site = (request.headers.get("X-Poll-Site") or "").strip()
    if _poll_site:
        allowed_sites = [_poll_site]
    ext_version = request.headers.get("X-Ext-Version", "").strip()
    # [TEMP-DIAG] device_id 등록 flip-flop 추적 — 비데몬 폴러의 X-Allowed-Sites/출처 기록.
    # ebfa9121(코디네이터) 충돌 원인 식별용. 추적 후 제거.
    if device_id and not device_id.startswith("samba-daemon-"):
        from backend.utils.logger import logger as _diag_log

        _diag_log.warning(
            "[TEMP-DIAG][collect-queue] dev=%s allowed=%s poll=%s ext=%s xff=%s ua=%s",
            device_id,
            request.headers.get("X-Allowed-Sites"),
            _poll_site,
            ext_version,
            request.headers.get("X-Forwarded-For", ""),
            (request.headers.get("User-Agent", "") or "")[:60],
        )
    job = await SourcingQueue.get_next_job(
        device_id=device_id,
        allowed_sites=allowed_sites,
        ext_version=ext_version or None,
    )
    # 확장앱 최소 호환 버전 — extension/manifest.json 의 version 과 비교.
    # 확장앱이 미달이면 popup에 경고 표시 + polling 중단 가능 (background-core.js 처리).
    if isinstance(job, dict):
        job.setdefault("minExtVersion", "2.12.0")
    return job


# ====================================================================
# LOTTEON 헤드리스 데몬 — health + 자동 업데이트 버전 메타
# ====================================================================

# build/release 시 갱신. 데몬이 시작 시 비교하여 신버전이면 자기 종료(다음 시작 시 갱신).
AUTOTUNE_DAEMON_LATEST_VERSION = "1.4.3"
AUTOTUNE_DAEMON_DOWNLOAD_URL = (
    "https://github.com/sbk0674-web/samba-wave/releases/download/"
    "samba-daemon-v1.4.3/samba.exe"
)
# 데몬 self-update 경로 — backend 경유로 install-token 박힌 exe 받기.
# 인증: X-Api-Key (데몬 long-lived key). 키 검증 후 새 install-token 발급 + exe tail append.
# 데몬이 자동 업데이트하면서 자동으로 새 키로 갱신됨 (SaaS 1클릭 보장).
AUTOTUNE_DAEMON_SELF_UPDATE_URL = (
    "https://api.samba-wave.co.kr/api/v1/samba/extension-keys/daemon-installer"
)


@sourcing_queue_router.get("/autotune-daemon/latest-version")
async def autotune_daemon_latest_version() -> dict[str, Any]:
    """데몬이 시작 시 호출 — 신버전 감지 시 self-update.

    인증 불필요. 응답 = {version, download_url}.
    """
    return {
        "version": AUTOTUNE_DAEMON_LATEST_VERSION,
        "download_url": AUTOTUNE_DAEMON_DOWNLOAD_URL,
    }


# 확장앱 자가 업데이트 버전 fallback.
# 1차 출처는 deploy.sh 가 주입하는 EXTENSION_LATEST_VERSION env(= manifest.json version).
# env 가 없을 때(로컬 개발 등)만 이 상수 사용 — 평소엔 신경 안 써도 됨.
_EXT_VERSION_FALLBACK = "2.13.44"


def _read_extension_version() -> str:
    # 단일 출처: deploy.sh 가 extension/manifest.json version 을 --build-arg EXT_VERSION
    # 으로 주입 → Dockerfile ENV EXTENSION_LATEST_VERSION. 빌드컨텍스트가 backend/ 라
    # 파일 COPY 는 불가하지만 build-arg 는 값만 전달하므로 가능.
    import os as _os

    env_v = (_os.environ.get("EXTENSION_LATEST_VERSION") or "").strip()
    if env_v:
        return env_v
    # 파일이 컨테이너에 있으면 읽기(레거시/예외 경로), 없으면 fallback 상수.
    import json as _json
    from pathlib import Path as _Path

    try:
        p = _Path("/app/backend/extension/manifest.json")
        if p.is_file():
            v = _json.loads(p.read_text(encoding="utf-8")).get("version")
            if v:
                return str(v)
    except Exception:
        pass
    return _EXT_VERSION_FALLBACK


# 모듈 로드 시 1회 평가 — 컨테이너 재배포마다 갱신됨.
EXTENSION_LATEST_VERSION = _read_extension_version()


@sourcing_queue_router.get("/autotune-daemon/extension-version")
async def extension_latest_version() -> dict[str, Any]:
    """확장앱이 주기적으로 호출 — 신버전 감지 시 chrome.runtime.reload() self-update.

    인증 불필요(EXEMPT prefix). 키 없는 미연결 PC도 업데이트받게 함.
    응답 = {version}.
    """
    return {"version": EXTENSION_LATEST_VERSION}


@sourcing_queue_router.get("/autotune-daemon/health")
async def autotune_daemon_health(
    device_id: str = Query("", description="(legacy, 무시됨)"),
) -> dict[str, Any]:
    """데몬 풀 alive 검사 — `samba-daemon-` prefix device 중 60s 내 polling 1개+ 면 alive.

    device_id 파라미터는 legacy 호환용 (값 무시). frontend localStorage device_id 와
    데몬 hostname device_id 가 달라 매칭 실패하던 사고 차단.
    인증 불필요.
    """
    try:
        from backend.api.v1.routers.samba.collector_autotune import _pc_last_seen
    except Exception:
        return {"alive": False, "last_seen": 0.0}
    import time as _time

    now = _time.time()
    latest_last = 0.0
    for dev, last in _pc_last_seen.items():
        if not dev.startswith("samba-daemon-"):
            continue
        if last > latest_last:
            latest_last = last
    alive = bool(latest_last and now - latest_last < 60)
    return {"alive": alive, "last_seen": float(latest_last)}


@sourcing_queue_router.get("/autotune-daemon/concurrency")
async def autotune_daemon_concurrency(request: Request) -> dict[str, Any]:
    """데몬용 사이트별 동시실행 설정 + 이 데몬이 담당할 사이트 조회 (인증 불필요).

    데몬이 60초마다 호출하는 기존 경로에 `assigned_sites` 를 얹어, 데몬이 UI에서
    지정된 자기 사이트만큼만 워커를 스폰하게 한다(추가 호출 0). 동시에 last_seen 을
    갱신해 사이트 0개(워커 미스폰)인 데몬도 UI '연결된 데몬' 목록에 뜨게 한다.

    응답:
      concurrency: {site: n} — assigned_sites 로 필터된 사이트별 동시실행 캡.
      assigned_sites: [site] — UI 지정 담당 사이트. 빈 배열이면 대기(워커 0).
    """
    device_id = (request.headers.get("X-Device-Id") or "").strip()
    assigned: list[str] = []
    try:
        from backend.api.v1.routers.samba.collector_autotune import (
            get_pc_allowed_sites,
            persist_pc_allowed_sites,
            touch_daemon_presence,
        )

        if device_id.startswith("samba-daemon-"):
            # 하트비트 — 사이트 0개 데몬도 목록에 뜨게(미등록 시 1회 빈 등록)
            if touch_daemon_presence(device_id):
                from backend.db.orm import get_write_session

                async with get_write_session() as _persist_sess:
                    await persist_pc_allowed_sites(_persist_sess)
                    await _persist_sess.commit()
        _my = get_pc_allowed_sites(device_id)
        if _my:
            assigned = sorted(_my)
    except Exception:
        assigned = []

    try:
        from backend.domain.samba.collector.refresher import (
            get_effective_autotune_concurrency,
        )

        conc = get_effective_autotune_concurrency()
    except Exception:
        conc = {}

    # 데몬은 담당 사이트만 워커 스폰 — 배정 없으면 빈 conc(대기). 비데몬은 legacy 전체.
    if device_id.startswith("samba-daemon-"):
        conc = {s: n for s, n in conc.items() if s in assigned}
    return {"concurrency": conc, "assigned_sites": assigned}


@sourcing_queue_router.post("/sourcing/collect-result")
async def sourcing_collect_result(body: dict[str, Any]) -> dict[str, Any]:
    """확장앱이 수집 결과를 전달 (인증 불필요)."""
    from backend.domain.samba.proxy.sourcing_queue import SourcingQueue

    request_id = body.get("requestId", "")
    data = body.get("data", {})
    ok = SourcingQueue.resolve_job(request_id, data)
    return {"success": ok}


@sourcing_queue_router.post("/sourcing/tracking-result")
async def sourcing_tracking_result(body: dict[str, Any]) -> dict[str, Any]:
    """확장앱이 추출한 운송장 정보 수신 (인증 불필요).

    body = {
      requestId: str,
      success: bool,
      courierName?: str,
      trackingNumber?: str,
      error?: str,
    }
    """
    from backend.domain.samba.proxy.sourcing_queue import SourcingQueue
    from backend.domain.samba.tracking_sync.service import apply_tracking_result

    request_id = (body.get("requestId") or "").strip()
    if not request_id:
        raise HTTPException(status_code=400, detail="requestId 누락")

    success = bool(body.get("success"))
    courier_name = (body.get("courierName") or "").strip()
    tracking_number = (body.get("trackingNumber") or "").strip()
    error = (body.get("error") or "").strip()
    cancelled = bool(body.get("cancelled"))

    # 인메모리 Future 깨워서 await 호출자 unblock + DB samba_sourcing_job completed 처리
    SourcingQueue.resolve_job(
        request_id,
        {
            "success": success,
            "courierName": courier_name,
            "trackingNumber": tracking_number,
            "error": error,
            "cancelled": cancelled,
        },
    )

    # tracking 잡 도메인 처리 — DB 저장 + (옵션) 마켓 dispatch
    # auto_dispatch는 안정화 전까지 False, dry_run=True 기본
    res = await apply_tracking_result(
        request_id,
        success=success,
        courier_name=courier_name,
        tracking_number=tracking_number,
        error=error,
        cancelled=cancelled,
        auto_dispatch=False,
        dry_run=True,
    )
    return res


@sourcing_queue_router.post("/sourcing/cancel-result")
async def sourcing_cancel_result(body: dict[str, Any]) -> dict[str, Any]:
    """데몬/확장앱이 발주취소 결과 회신 (인증 불필요, X-Api-Key 사용).

    body = {
      requestId: str,
      success: bool,
      cancelled: bool,
      alreadyShipped?: bool,
      reason?: str,
      error?: str,
    }

    - cancelled=True  → order.status='cancelled', shipping_status='취소완료'
    - alreadyShipped=True → notes 에 "소싱처 이미 발송 — 수동 처리 필요" append, status 변경 없음
    - 그 외(success=False) → notes 에 실패 사유 append, status 변경 없음
    """
    from datetime import datetime, timezone
    from sqlalchemy import update
    from backend.db.orm import get_write_session
    from backend.domain.samba.order.model import SambaOrder
    from backend.domain.samba.proxy.sourcing_queue import SourcingQueue
    from backend.domain.samba.sourcing_job.model import SambaSourcingJob
    from backend.utils.logger import logger
    from fastapi import HTTPException

    request_id = (body.get("requestId") or "").strip()
    if not request_id:
        raise HTTPException(status_code=400, detail="requestId 누락")

    success = bool(body.get("success"))
    cancelled = bool(body.get("cancelled"))
    already_shipped = bool(body.get("alreadyShipped"))
    reason = (body.get("reason") or "").strip()
    error = (body.get("error") or "").strip()

    SourcingQueue.resolve_job(
        request_id,
        {
            "success": success,
            "cancelled": cancelled,
            "alreadyShipped": already_shipped,
            "reason": reason,
            "error": error,
        },
    )

    # 잡 payload 에서 orderId 회수
    order_id = ""
    sourcing_order_number = ""
    site = ""
    try:
        async with get_write_session() as _sess:
            _row = await _sess.get(SambaSourcingJob, request_id)
            if _row and isinstance(_row.payload, dict):
                order_id = (_row.payload.get("orderId") or "").strip()
                sourcing_order_number = (
                    _row.payload.get("sourcingOrderNumber") or ""
                ).strip()
                site = (_row.payload.get("site") or "").strip()
    except Exception as _e:
        logger.warning(f"[cancel-result] 잡 조회 실패 req={request_id}: {_e}")

    if not order_id:
        return {"ok": True, "applied": False, "reason": "orderId 미상"}

    now_kst_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    note_line = ""
    update_values: dict[str, Any] = {}

    if cancelled:
        note_line = (
            f"[{now_kst_tag}] 소싱처 자동취소 완료 ({site} ord={sourcing_order_number})"
        )
        update_values = {
            "status": "cancelled",
            "shipping_status": "취소완료",
            "updated_at": datetime.now(timezone.utc),
        }
    elif already_shipped:
        note_line = (
            f"[{now_kst_tag}] 소싱처 이미 발송 — 자동취소 불가, 수동 처리 필요 "
            f"({site} ord={sourcing_order_number})"
        )
    else:
        note_line = (
            f"[{now_kst_tag}] 소싱처 자동취소 실패: {error or reason or 'unknown'} "
            f"({site} ord={sourcing_order_number})"
        )

    try:
        async with get_write_session() as sess:
            ord_row = await sess.get(SambaOrder, order_id)
            if not ord_row:
                return {"ok": True, "applied": False, "reason": "order 없음"}
            prev_notes = ord_row.notes or ""
            new_notes = (
                (prev_notes + "\n" + note_line).strip() if prev_notes else note_line
            )
            update_values["notes"] = new_notes
            await sess.execute(
                update(SambaOrder)
                .where(SambaOrder.id == order_id)
                .values(**update_values)
            )
            await sess.commit()
    except Exception as e:
        logger.warning(f"[cancel-result] order 업데이트 실패 id={order_id}: {e}")
        return {"ok": False, "error": str(e)}

    logger.info(
        f"[cancel-result] req={request_id} order={order_id} cancelled={cancelled} "
        f"alreadyShipped={already_shipped}"
    )
    return {
        "ok": True,
        "applied": True,
        "cancelled": cancelled,
        "alreadyShipped": already_shipped,
    }


@sourcing_queue_router.get("/autotune/concurrency")
async def get_autotune_concurrency_for_extension() -> dict[str, Any]:
    """확장앱 전용 — 사이트별 동시처리 캡 조회 (X-Api-Key 인증만, JWT 불필요).

    검증(2026-05-05): /collector/autotune/status는 JWT 필수 → 확장앱이 401 받음 →
    동시처리 설정값 못 가져와서 빈 객체 fallback → 큐 적체로 timeout 다발.
    이 endpoint는 X-Api-Key만으로 동시처리 캡만 반환하여 확장앱이 적정값 사용.
    """
    from backend.domain.samba.collector.refresher import (
        get_effective_autotune_concurrency,
    )

    return {"site_autotune_concurrency": get_effective_autotune_concurrency()}


@router.get("/sourcing/{site}/search")
async def sourcing_search(
    site: str,
    request: Request,
    keyword: str = Query("", min_length=1),
    page: int = Query(1, ge=1),
) -> dict[str, Any]:
    """소싱처 통합 검색 API."""
    # 패션플러스: 직접 API
    client = _get_sourcing_client(site)
    if client:
        return await client.search(keyword, page)

    # 확장앱 기반 사이트
    if site in EXTENSION_SITES:
        from backend.domain.samba.proxy.sourcing_queue import SourcingQueue

        # 트리거 PC 의 deviceId 로 owner 박아 해당 PC 확장앱에서만 탭이 열리도록 라우팅.
        # 헤더 누락 시 빈값 → SourcingQueue 내부에서 오토튠 글로벌 폴백.
        _trigger_device_id = request.headers.get("X-Device-Id", "").strip()
        try:
            request_id, future = SourcingQueue.add_search_job(
                site, keyword, owner_device_id=_trigger_device_id or None
            )
            result = await asyncio.wait_for(future, timeout=60)
            return result
        except asyncio.TimeoutError:
            SourcingQueue.resolvers.pop(request_id, None)
            return {"products": [], "total": 0, "error": "확장앱 응답 타임아웃 (60초)"}
        except RuntimeError as e:
            return {"products": [], "total": 0, "error": str(e)}
        except Exception as e:
            return {"products": [], "total": 0, "error": str(e)}

    raise HTTPException(400, f"지원하지 않는 소싱처: {site}")


@router.get("/sourcing/{site}/detail/{product_id}")
async def sourcing_detail(
    site: str,
    product_id: str,
    request: Request,
) -> dict[str, Any]:
    """소싱처 상품 상세 조회 API."""
    # 패션플러스: 직접 API
    client = _get_sourcing_client(site)
    if client:
        return await client.get_detail(product_id)

    # 확장앱 기반 사이트
    if site in EXTENSION_SITES:
        from backend.domain.samba.proxy.sourcing_queue import SourcingQueue

        # 트리거 PC 의 deviceId 로 owner 박아 해당 PC 확장앱에서만 탭이 열리도록 라우팅.
        _trigger_device_id = request.headers.get("X-Device-Id", "").strip()
        try:
            request_id, future = SourcingQueue.add_detail_job(
                site, product_id, owner_device_id=_trigger_device_id or None
            )
            result = await asyncio.wait_for(future, timeout=60)
            return result
        except asyncio.TimeoutError:
            SourcingQueue.resolvers.pop(request_id, None)
            return {"error": "확장앱 응답 타임아웃 (60초)"}
        except RuntimeError as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

    raise HTTPException(400, f"지원하지 않는 소싱처: {site}")

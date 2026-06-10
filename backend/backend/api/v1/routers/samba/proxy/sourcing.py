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


# 프로세스 lifetime 내 자가등록 완료 dev 캐시 — DB read 중복 차단.
_daemon_autoreg_done: set[str] = set()


async def _ensure_daemon_extension_key(session, device_id: str) -> None:
    """데몬 device 가 samba_extension_key 에 없으면 placeholder INSERT.

    근본 fix (2026-05-27): install-token 없는 자가빌드 daemon.exe 는 extension_key
    자가등록 흐름이 없어 _pc_cleanup_loop 가 active_set 외 dev 로 판정 → 분담 dict
    에서 제거 → SSG/ABC/LOTTEON 잡 발행 실패. 데몬 폴링 도착 시 placeholder row 박아
    cleanup_loop active_set 통과시킴. revoked_at=NULL, expires_at=NULL, is_install_token=False.

    placeholder 는 인증 권한 없음 (key_hash="DAEMON_AUTOREG_PLACEHOLDER"). 데몬은
    실제 api_key 별도 발급 흐름(bootstrap_api_key) 로 동작, 이 row 는 cleanup_loop
    스캔용 marker 전용. exchange/auth 어디서도 매칭 안 됨.
    """
    if device_id in _daemon_autoreg_done:
        return
    try:
        import hashlib
        import uuid
        from sqlalchemy import text as _text

        existing = await session.execute(
            _text(
                "SELECT 1 FROM samba_extension_key "
                "WHERE device_id=:d AND revoked_at IS NULL LIMIT 1"
            ),
            {"d": device_id},
        )
        if existing.first():
            _daemon_autoreg_done.add(device_id)
            return
        marker = f"DAEMON_AUTOREG:{device_id}"
        await session.execute(
            _text(
                "INSERT INTO samba_extension_key "
                "(id, key_hash, user_id, label, device_id, is_install_token) "
                "VALUES (:id, :h, :u, :l, :d, false) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "id": str(uuid.uuid4())[:40],
                "h": hashlib.sha256(marker.encode()).hexdigest(),
                "u": "daemon-autoreg",
                "l": f"daemon-autoreg {device_id[:20]}",
                "d": device_id,
            },
        )
        _daemon_autoreg_done.add(device_id)
    except Exception as _exc:
        from backend.utils.logger import logger as _lg

        _lg.warning(f"[데몬자가등록] {device_id} INSERT 실패(무시): {_exc}")


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

    # PC 분담 갱신 = UI 체크박스 저장 endpoint 만 (POST /pc-allowed-sites authoritative).
    # 폴링 헤더 X-Allowed-Sites 로 분담 union 합산 금지 (2026-05-25 사용자 룰).
    # 여기서는 last_seen 갱신만 — 데몬 등록 흐름 외 분담 자동 부여 절대 없음.
    try:
        from backend.api.v1.routers.samba.collector_autotune import (
            touch_daemon_presence,
            update_pc_last_seen,
        )

        update_pc_last_seen(device_id)
        if device_id.startswith("samba-daemon-"):
            # 데몬 등록 — extension_key 자가등록만 (cleanup_loop active_set 통과용).
            # persist_pc_allowed_sites 호출 금지: 메모리 빈 set 이 DB 의 기존 사이트
            # 분담을 덮어쓰는 사고 회귀 (2026-05-27, 4번 패치 시도 끝 root cause 확정).
            # 분담 자체는 UI POST /pc-allowed-sites 가 유일한 작성 권한 + sync_pc_allowed_sites_from_db
            # 가 10초마다 DB→메모리 복원 → 데몬 dev 도 DB 값 자동 복원.
            touch_daemon_presence(device_id)
            if device_id not in _daemon_autoreg_done:
                from backend.db.orm import get_write_session

                async with get_write_session() as _persist_sess:
                    await _ensure_daemon_extension_key(_persist_sess, device_id)
                    await _persist_sess.commit()
        # 확장앱: 분담 자동 갱신 폐기 — UI 체크박스 저장만 분담 갱신 권한.
    except Exception:
        pass

    # X-Heartbeat: 데몬 워커-독립 heartbeat (v1.4.14+). 잡 dequeue 없이 last_seen 만 갱신.
    # long process_job 50s 사이클 동안 폴링 공백으로 last_seen TTL 초과 → "데몬 미등록"
    # 회귀 차단. SourcingQueue.get_next_job 의 사이트 lock 경합 + DB 부하 0.
    if request.headers.get("X-Heartbeat"):
        return {"hasJob": False, "heartbeat": True}
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
AUTOTUNE_DAEMON_LATEST_VERSION = "1.4.33"
# asset 명에 버전 박힘 (`samba-v{ver}.exe`) — 지침: 데몬 설치파일명 버전 노출 필수.
AUTOTUNE_DAEMON_DOWNLOAD_URL = (
    f"https://github.com/sbk0674-web/samba-wave/releases/download/"
    f"samba-daemon-v{AUTOTUNE_DAEMON_LATEST_VERSION}/"
    f"samba-v{AUTOTUNE_DAEMON_LATEST_VERSION}.exe"
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
_EXT_VERSION_FALLBACK = "2.13.70"


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
    # 데몬 long process_job(50s) + heartbeat 15s 주기 고려 180s 까지 alive 인정.
    # daemon_pool._pick_owner_with_prefix TTL 과 일치 (v1.4.14+).
    alive = bool(latest_last and now - latest_last < 180)
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
            touch_daemon_presence,
        )

        if device_id.startswith("samba-daemon-"):
            # 하트비트 — extension_key 자가등록만 (분담 persist 금지, 위 사고 회귀 차단)
            touch_daemon_presence(device_id)
            if device_id not in _daemon_autoreg_done:
                from backend.db.orm import get_write_session

                async with get_write_session() as _persist_sess:
                    await _ensure_daemon_extension_key(_persist_sess, device_id)
                    await _persist_sess.commit()
        _my = get_pc_allowed_sites(device_id)
        if _my:
            assigned = sorted(_my)
        # [2026-06-05 송장 확장앱 복구] 데몬은 송장 안 함(확장앱 전담)으로 되돌림 — 전담 데몬에
        # tracking 사이트를 union 하던 로직 제거. 데몬은 autotune 가격수집만 담당.
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

    # tracking 잡 도메인 처리 — DB 저장 + 마켓 dispatch
    # [2026-06-05] auto_dispatch 활성화 — 그동안 "안정화 전까지 False"로 꺼둔 게 방치돼
    # 송장 수집(SCRAPED)만 되고 마켓 전송·주문상태(배송중) 갱신이 영구 미실행되던 버그 fix.
    # dispatch_to_market은 취소 가드 + 수동 전송과 동일 service(send_invoice_to_market) 사용,
    # 실패 시 DISPATCH_FAILED로 표시돼 재시도 가능 → 활성화 안전.
    res = await apply_tracking_result(
        request_id,
        success=success,
        courier_name=courier_name,
        tracking_number=tracking_number,
        error=error,
        cancelled=cancelled,
        auto_dispatch=True,
        dry_run=False,
    )
    return res


# 롯데ON 취소클레임 진행단계 — '완료' 로 간주하는 코드(멱등 성공 처리용).
# 그 외 모든 단계는 승인 대상으로 보고 cnclRequestApproval 시도한다.
# 실측(2026-06-01 getCancellationRequestAndComplateList): odPrgsStepCd = 02 요청 / 21 취소완료
#   / 22 철회 (문서값 일치, 기존 status sync 의 11/12/13 매핑은 실제로 매칭 안 됨 = 죽은 코드).
# 21=완료만 done 처리 → 02 요청은 승인 대상. 22 철회는 done 에 안 넣음(승인 시도→실패 시 운영자
# 수동, false-success 절대 안 만든다). 13 은 혹시 모를 구버전 대비 방어적으로 유지.
_LOTTEON_CANCEL_DONE_STEPS = {"13", "21"}


def _lotteon_candidate_od_nos(raw: str) -> list[str]:
    """ord_row.od_no/order_number 에서 클레임 odNo 매칭용 후보 토큰들 추출.

    삼바 DB 의 order_number 가 라이브 클레임 응답의 odNo 와 다른 형식인 케이스를 흡수한다.
      예) 'L02682376475_2682376509' → ['L02682376475_2682376509', 'L02682376475', '02682376475']
      예) '20260601139900099_1'    → ['20260601139900099_1', '20260601139900099']
    매칭 누락 → 직접취소 fallback → 3006 false-success 의 진앞을 차단.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    out = [raw]
    if "_" in raw:
        head = raw.split("_", 1)[0]
        if head and head not in out:
            out.append(head)
    # L 접두 (롯데ON 내부 식별자) 제거한 순수 숫자 토큰
    last = out[-1]
    if last.startswith("L") and last[1:].isdigit():
        out.append(last[1:])
    return out


async def _lotteon_approve_or_direct_cancel(client, ord_row) -> tuple[bool, str]:
    """롯데ON 취소 마무리 — 라이브 취소클레임 조회로 승인 대상 판별.

    분기:
      - od_no 매칭 클레임 없음 → 마켓 측 취소요청 자체가 없음 → 판매자직접취소(slrDirectCnclProc)
      - 승인 대상(요청) 클레임 있음 → cnclRequestApproval (취소요청 승인)
      - 완료 클레임만 있음 → 이미 승인됨(멱등) → 성공

    클레임이 존재하는데 직접취소로 fallback 하지 않는 이유: 직접취소는 취소요청 대기 건에
    3006 반환 + 이전 seller_cancel_order 가 3006 을 success 로 처리 → '미승인인데 완료'
    false-success. 현재 seller_cancel_order 는 3006 을 fail 로 신호하고, 본 함수는 3006
    감지 시 클레임 재조회 후 승인 fallback 을 한 번 더 시도한다.
    """
    raw = ord_row.od_no or ord_row.order_number
    candidates = _lotteon_candidate_od_nos(raw)
    try:
        claims = await client.get_cancel_orders(days=14)
    except Exception as e:
        # 조회 실패 시 직접취소로 내려가면 false-success 위험 → 실패 처리(운영자 수동)
        return False, f"취소클레임 조회 실패: {e}"

    def _match(claim_list):
        cand_set = {str(c) for c in candidates}
        m = [c for c in claim_list if str(c.get("odNo", "") or "") in cand_set]
        if ord_row.od_seq:
            by_seq = [
                c for c in m if str(c.get("odSeq", "") or "") == str(ord_row.od_seq)
            ]
            if by_seq:
                m = by_seq
        return m

    matched = _match(claims)

    if not matched:
        # 마켓 측 고객 취소요청 없음(또는 매칭 누락) → 판매자가 직접 취소
        success, message = await client.seller_cancel_order(
            od_no=candidates[-1] if candidates else raw,
            reason_code="CC11",  # 고객변심
            reason_text="고객 취소요청",
            od_seq=int(ord_row.od_seq or 1),
            proc_seq=int(ord_row.proc_seq or 1),
        )
        if success:
            return True, "판매자직접취소 완료"
        # 3006 = 직접취소 거부 (클레임 대기 가능성). 클레임 재조회 후 승인 시도.
        if "3006" in (message or ""):
            try:
                claims2 = await client.get_cancel_orders(days=30)
            except Exception as e:
                return False, f"3006 후 클레임 재조회 실패: {e}"
            matched = _match(claims2)
            if not matched:
                return (
                    False,
                    "3006: 직접취소 거부 + 클레임 매칭 실패 — 운영자 확인 필요",
                )
            # ↓ 아래 승인 흐름으로 이어짐
        else:
            return False, message

    pending = [
        c
        for c in matched
        if str(c.get("odPrgsStepCd", "") or "") not in _LOTTEON_CANCEL_DONE_STEPS
    ]
    if not pending:
        # 완료 클레임만 존재 → 이미 승인 처리됨(멱등)
        return True, "이미 취소 처리된 클레임"

    claim = pending[0]
    matched_od_no = str(claim.get("odNo", "") or "") or (
        candidates[0] if candidates else raw
    )
    try:
        await client.approve_cancel(
            od_no=matched_od_no,
            clm_no=str(claim.get("clmNo", "") or ""),
            items=[
                {
                    "odSeq": int(claim.get("odSeq", 1) or 1),
                    "procSeq": int(claim.get("procSeq", 1) or 1),
                    "orglProcSeq": int(claim.get("orglProcSeq", 1) or 1),
                    # 승인 사유코드 = 클레임사유코드(clmRsnCd) 그대로, 없으면 106(고객변심)
                    "slrRsnCd": str(claim.get("clmRsnCd", "") or "106"),
                    "slrRsnCnts": "고객 취소요청",
                }
            ],
        )
    except Exception as e:
        return False, f"취소요청 승인 실패: {e}"
    return True, "취소요청 승인 완료"


async def _maybe_approve_market_cancel(
    sess, ord_row, now_kst_tag: str, sourcing_site: str, sourcing_ord_no: str
) -> None:
    """소싱처 자동취소 성공 후 → 마켓 측 cancel 승인 자동 처리.

    지원 마켓:
      - smartstore: approve_cancel(order_number)
      - 11st: confirm_cancel(clm_req_seq, order_number, ord_prd_seq) — SambaReturn 필요
      - ebay: 별도 API 없음 — DB 동기화만 (셀러측 이미 자동 취소)
      - 그 외(쿠팡/playauto/lotteon/esm 등): 자동 승인 API 미확인 — 노트만, status=cancelling 유지

    실패 시 status='cancelling' 유지(이미 update 됨) + 노트만 추가.
    """
    from datetime import datetime, timezone

    from sqlalchemy import update

    from backend.domain.samba.order.model import SambaOrder
    from backend.utils.logger import logger

    if not ord_row.channel_id or not ord_row.order_number:
        return

    try:
        from backend.domain.samba.account.repository import (
            SambaMarketAccountRepository,
        )

        acc_repo = SambaMarketAccountRepository(sess)
        account = await acc_repo.get_async(ord_row.channel_id)
    except Exception as e:
        logger.warning(f"[cancel-result] account 조회 실패: {e}")
        return
    if not account:
        return

    market_type = (account.market_type or "").strip().lower()

    async def _finalize_cancelled(approver_name: str) -> None:
        """승인 성공 — status/shipping_status 최종 advance + 원가/배송비 클리어 + 노트.

        원주문 취소 성공 = 실제 발주·배송 발생 안 함 → cost/shipping_fee/profit 0 으로.
        """
        await sess.execute(
            update(SambaOrder)
            .where(SambaOrder.id == ord_row.id)
            .values(
                status="cancelled",
                shipping_status="취소완료",
                cost=0,
                shipping_fee=0,
                profit=0,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await _append_cancel_note(
            sess,
            ord_row.id,
            f"[{now_kst_tag}] {approver_name} 취소승인 완료 → 취소완료 (원가/배송비/실수익 0 처리)",
        )
        await sess.commit()
        logger.info(
            f"[cancel-result] {approver_name} 취소승인 완료 order={ord_row.id} ord_no={ord_row.order_number}"
        )

    async def _record_failure(approver_name: str, reason: str) -> None:
        await _append_cancel_note(
            sess,
            ord_row.id,
            f"[{now_kst_tag}] {approver_name} 취소승인 실패: {reason} — 수동 처리 필요",
        )
        await sess.commit()
        logger.warning(
            f"[cancel-result] {approver_name} 취소승인 실패 order={ord_row.id}: {reason}"
        )

    # ── 스마트스토어 ──────────────────────────────────────────────
    if market_type == "smartstore":
        try:
            from backend.domain.samba.forbidden.repository import (
                SambaSettingsRepository,
            )
            from backend.domain.samba.proxy.smartstore import SmartStoreClient

            extras = account.additional_fields or {}
            client_id = extras.get("clientId", "") or account.api_key or ""
            client_secret = extras.get("clientSecret", "") or account.api_secret or ""
            if not client_id or not client_secret:
                settings_repo = SambaSettingsRepository(sess)
                row = await settings_repo.find_by_async(key="store_smartstore")
                if row and isinstance(row.value, dict):
                    client_id = client_id or row.value.get("clientId", "")
                    client_secret = client_secret or row.value.get("clientSecret", "")
            if not client_id or not client_secret:
                await _record_failure("스마트스토어", "인증정보 없음")
                return
            client = SmartStoreClient(client_id, client_secret)
            await client.approve_cancel(ord_row.order_number)
            await _finalize_cancelled("스마트스토어")
        except Exception as e:
            await _record_failure("스마트스토어", str(e))
        return

    # ── 11번가 ────────────────────────────────────────────────────
    if market_type == "11st":
        try:
            from backend.domain.samba.proxy.elevenst import ElevenstClient
            from backend.domain.samba.returns.repository import SambaReturnRepository

            api_key = (
                (account.additional_fields or {}).get("apiKey", "")
                or account.api_key
                or ""
            )
            if not api_key:
                await _record_failure("11번가", "API 키 없음")
                return
            return_repo = SambaReturnRepository(sess)
            rets = await return_repo.filter_by_async(order_id=ord_row.id)
            ret = rets[0] if rets else None
            clm_req_seq = (ret.clm_req_seq if ret else None) or ""
            ord_prd_seq = (ret.ord_prd_seq if ret else None) or ""
            if not clm_req_seq or not ord_prd_seq:
                await _record_failure(
                    "11번가",
                    "취소 클레임 정보 없음 (clm_req_seq/ord_prd_seq 미수집)",
                )
                return
            client = ElevenstClient(api_key)
            await client.confirm_cancel(clm_req_seq, ord_row.order_number, ord_prd_seq)
            if ret:
                await return_repo.update_async(
                    ret.id, status="cancelled", market_order_status="취소완료"
                )
            await _finalize_cancelled("11번가")
        except Exception as e:
            await _record_failure("11번가", str(e))
        return

    # ── 롯데ON ────────────────────────────────────────────────────
    if market_type == "lotteon":
        try:
            from backend.domain.samba.proxy.lotteon import LotteonClient

            extras = account.additional_fields or {}
            api_key = extras.get("apiKey", "") or account.api_key or ""
            if not api_key:
                await _record_failure("롯데ON", "API Key 없음")
                return
            client = LotteonClient(api_key)
            try:
                await client.test_auth()
            except Exception as e:
                await _record_failure("롯데ON", f"인증 실패: {e}")
                return
            # 마켓 측 고객 취소요청 건은 취소요청 승인(cnclRequestApproval)으로 닫아야 한다.
            # 판매자직접취소(slrDirectCnclProc)는 취소요청 대기 건에 쏘면 3006(주문 상태 확인)
            # 반환되며 고객 취소요청은 미승인 잔존한다. 승인 대상 클레임은 라이브 조회로 찾는다.
            ok, msg = await _lotteon_approve_or_direct_cancel(client, ord_row)
            if not ok:
                await _record_failure("롯데ON", msg)
                return
            # 같은 od_no 동반 cancel — 기존 패턴 (order.py:2440)
            from sqlalchemy import select as _select

            from backend.domain.samba.order.model import SambaOrder as _SO

            if ord_row.od_no:
                stmt = (
                    _select(_SO)
                    .where(_SO.od_no == ord_row.od_no)
                    .where(_SO.channel_id == ord_row.channel_id)
                    .where(_SO.id != ord_row.id)
                    .where(_SO.status != "cancelled")
                )
                sib_rows = (await sess.execute(stmt)).scalars().all()
                for sib in sib_rows:
                    await sess.execute(
                        update(_SO)
                        .where(_SO.id == sib.id)
                        .values(
                            status="cancelled",
                            shipping_status="취소완료",
                            cost=0,
                            shipping_fee=0,
                            profit=0,
                            updated_at=datetime.now(timezone.utc),
                        )
                    )
            await _finalize_cancelled("롯데ON")
        except Exception as e:
            await _record_failure("롯데ON", str(e))
        return

    # ── SSG ───────────────────────────────────────────────────────
    if market_type == "ssg":
        try:
            from backend.domain.samba.proxy.ssg import SSGClient

            api_key = (
                (account.additional_fields or {}).get("apiKey", "")
                or account.api_key
                or ""
            )
            if not api_key:
                await _record_failure("SSG", "API Key 없음")
                return
            # shipment_id 형식: "{shppNo}|{ord_item_seq}" 또는 "|{ord_item_seq}"
            shipment_id = (ord_row.shipment_id or "").strip()
            ord_item_seq = shipment_id.split("|", 1)[1] if "|" in shipment_id else ""
            if not ord_item_seq:
                await _record_failure("SSG", "ordItemSeq 미수집 (shipment_id 비어있음)")
                return
            client = SSGClient(api_key)
            await client.approve_cancel(ord_row.order_number, ord_item_seq)
            await _finalize_cancelled("SSG")
        except Exception as e:
            await _record_failure("SSG", str(e))
        return

    # ── 옥션 / G마켓 (ESM) ────────────────────────────────────────
    if market_type in ("gmarket", "auction"):
        try:
            from backend.domain.samba.proxy.esmplus import ESMPlusClient

            extras = account.additional_fields or {}
            api_key = extras.get("apiKey", "") or account.api_key or ""
            if not api_key:
                await _record_failure(market_type, "API Key 없음")
                return
            site_type = 2 if market_type == "gmarket" else 1  # 1=옥션, 2=G마켓
            client = ESMPlusClient(api_key)
            res = await client.approve_cancel_by_orderno(
                ord_row.order_number, site_type
            )
            result_code = res.get("ResultCode") if isinstance(res, dict) else None
            biz_code = res.get("BizRuleCode", "") if isinstance(res, dict) else ""
            # 0=성공. 8668+W8-2 = 이미 취소승인 (성공 동급 처리)
            if result_code == 0 or (result_code == 8668 and biz_code == "W8-2"):
                await _finalize_cancelled(
                    "옥션" if market_type == "auction" else "G마켓"
                )
            else:
                await _record_failure(
                    market_type,
                    f"ResultCode={result_code} {res.get('Message', '')}",
                )
        except Exception as e:
            await _record_failure(market_type, str(e))
        return

    # ── eBay ───────────────────────────────────────────────────────
    if market_type == "ebay":
        # 셀러측에서 이미 cancel 처리됨 — DB 동기화만
        try:
            from backend.domain.samba.returns.repository import SambaReturnRepository

            await _finalize_cancelled("eBay")
            ret_repo = SambaReturnRepository(sess)
            rets = await ret_repo.filter_by_async(order_id=ord_row.id)
            for ret in rets:
                await ret_repo.update_async(
                    ret.id, status="completed", market_order_status="취소완료"
                )
            await sess.commit()
        except Exception as e:
            await _record_failure("eBay", str(e))
        return

    # ── 그 외 마켓 ─────────────────────────────────────────────────
    # 쿠팡/PlayAuto/LOTTEON/ESM 등 — 자동 승인 API 미확인 또는 셀러측 자동 처리.
    # status='cancelling' 유지 + 노트만. 운영자가 status 드롭다운 직접 'cancelled' 변경.
    await _append_cancel_note(
        sess,
        ord_row.id,
        f"[{now_kst_tag}] {market_type} 자동 취소승인 미지원 — 운영자 수동 처리 필요",
    )
    await sess.commit()


async def _append_cancel_note(sess, order_id: str, line: str) -> None:
    """SambaOrder.notes 에 한 줄 append (별도 commit 안 함 — caller 책임)."""
    from sqlalchemy import update

    from backend.domain.samba.order.model import SambaOrder

    row = await sess.get(SambaOrder, order_id)
    if not row:
        return
    prev = (row.notes or "").strip()
    new_notes = (prev + "\n" + line).strip() if prev else line
    await sess.execute(
        update(SambaOrder).where(SambaOrder.id == order_id).values(notes=new_notes)
    )


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

    # 잡 payload 에서 orderId + prev_status (롤백용) 회수
    order_id = ""
    sourcing_order_number = ""
    site = ""
    prev_status = ""
    try:
        async with get_write_session() as _sess:
            _row = await _sess.get(SambaSourcingJob, request_id)
            if _row and isinstance(_row.payload, dict):
                order_id = (_row.payload.get("orderId") or "").strip()
                sourcing_order_number = (
                    _row.payload.get("sourcingOrderNumber") or ""
                ).strip()
                site = (_row.payload.get("site") or "").strip()
                prev_status = (_row.payload.get("prevStatus") or "").strip()
    except Exception as _e:
        logger.warning(f"[cancel-result] 잡 조회 실패 req={request_id}: {_e}")

    if not order_id:
        return {"ok": True, "applied": False, "reason": "orderId 미상"}

    # KST 단일 — UTC 표기 절대 금지 (사용자 룰 feedback_report_kst_only)
    from datetime import timedelta as _td

    _kst = timezone(_td(hours=9))
    now_kst_tag = datetime.now(_kst).strftime("%Y-%m-%d %H:%M:%S KST")
    note_line = ""
    update_values: dict[str, Any] = {}

    if cancelled:
        # 성공 → '취소중'(cancelling) 으로 advance. 추후 마켓 폴러가 '취소완료' 확정 시 cancelled 로.
        # 원주문 취소 성공 = 실제 발주·배송 발생 안 함 → cost/shipping_fee/profit 즉시 0 처리.
        note_line = (
            f"[{now_kst_tag}] 소싱처 자동취소 성공 → 취소중 "
            f"({site} ord={sourcing_order_number}) (원가/배송비/실수익 0 처리)"
        )
        update_values = {
            "status": "cancelling",
            "cost": 0,
            "shipping_fee": 0,
            "profit": 0,
            "updated_at": datetime.now(timezone.utc),
        }
    elif already_shipped:
        # 이미 발송 — status 그대로 + 수동 처리 안내. 단, cancel_requested 로 박혀있으면
        # 사용자 오해 유발 → prev_status 가 있으면 그 값으로 롤백.
        note_line = (
            f"[{now_kst_tag}] 소싱처 이미 발송 — 자동취소 불가, 수동 처리 필요 "
            f"({site} ord={sourcing_order_number})"
        )
        if prev_status:
            update_values = {
                "status": prev_status,
                "updated_at": datetime.now(timezone.utc),
            }
    else:
        # 실패 → status 를 prev_status 로 롤백. payload 에 없으면 노트만.
        details = body.get("details")
        details_str = ""
        if details:
            try:
                import json as _json

                details_str = (
                    " details=" + _json.dumps(details, ensure_ascii=False)[:600]
                )
            except Exception:
                pass
        note_line = (
            f"[{now_kst_tag}] 소싱처 자동취소 실패: {error or reason or 'unknown'} "
            f"({site} ord={sourcing_order_number}){details_str}"
        )
        if prev_status:
            update_values = {
                "status": prev_status,
                "updated_at": datetime.now(timezone.utc),
            }

    try:
        async with get_write_session() as sess:
            ord_row = await sess.get(SambaOrder, order_id)
            if not ord_row:
                return {"ok": True, "applied": False, "reason": "order 없음"}
            prev_notes = ord_row.notes or ""
            # 노트 중복 차단 — 같은 ord_no + 같은 fail/success 키워드 이미 노트 마지막에 있으면 skip.
            # 마켓 폴러 무한 반복(28분 cooldown 가드 우회 등)으로 같은 메시지 N개 박혀 사용자 화면 어지러움.
            note_signature = f"({site} ord={sourcing_order_number})"
            tail = prev_notes[-800:] if prev_notes else ""
            already_noted = note_signature in tail and (
                ("자동취소 실패" in note_line and "자동취소 실패" in tail)
                or ("자동취소 성공" in note_line and "자동취소 성공" in tail)
                or ("이미 발송" in note_line and "이미 발송" in tail)
            )
            if already_noted:
                new_notes = prev_notes  # 노트 추가 X
            else:
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

            # 무신사 자동취소 성공 시 → 마켓 측 cancel 승인 자동 처리
            # 스마트스토어: approve_cancel → status=cancelled / shipping_status=취소완료
            if cancelled:
                await _maybe_approve_market_cancel(
                    sess, ord_row, now_kst_tag, site, sourcing_order_number
                )
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

"""작업 큐 API."""

from datetime import UTC, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_read_session_dependency, get_write_session_dependency
from backend.domain.samba.job.model import JobStatus
from backend.domain.samba.job.progress_tracker import get_recent_sec_per_item
from backend.domain.samba.job.repository import SambaJobRepository
from backend.domain.samba.job.service import SambaJobService
from backend.domain.samba.tenant.middleware import get_optional_tenant_id

router = APIRouter(prefix="/jobs", tags=["samba-jobs"])

ORDER_SYNC_PENDING_STALE_SEC = 45
ORDER_SYNC_RUNNING_STALE_SEC = 180


class JobCreate(BaseModel):
    job_type: str  # transmit | collect | refresh | ai_tag | order_sync
    payload: dict = {}
    tenant_id: Optional[str] = None


def _is_stale_order_sync_job(active) -> bool:
    now = datetime.now(UTC)

    if active.status == JobStatus.PENDING:
        base = active.created_at or now
        return (now - base) > timedelta(seconds=ORDER_SYNC_PENDING_STALE_SEC)

    if active.status == JobStatus.RUNNING:
        base = active.started_at or active.created_at or now
        if active.current > 0:
            return False
        return (now - base) > timedelta(seconds=ORDER_SYNC_RUNNING_STALE_SEC)

    return False


@router.post("", status_code=201)
async def create_job(
    body: JobCreate,
    session: AsyncSession = Depends(get_write_session_dependency),
    auth_tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """잡 생성 — 즉시 응답, 백그라운드 워커가 처리."""
    # body.tenant_id 미지정 시 인증 컨텍스트의 tenant_id 사용
    # — 테트리스 매칭 등 tenant 스코프 자원이 워커에서 매칭되도록 보장
    if body.tenant_id is None:
        body.tenant_id = auth_tenant_id
    svc = SambaJobService(SambaJobRepository(session))

    # 수집 잡: 대기 큐 위치 계산 (같은 소싱처 PENDING/RUNNING 수)
    queue_position = 0
    if body.job_type == "collect":
        source_site = body.payload.get("source_site", "")
        if source_site:
            from backend.domain.samba.job.model import SambaJob
            from sqlmodel import select, col
            from sqlalchemy import func

            queue_position = (
                (
                    await session.execute(
                        select(func.count())
                        .select_from(SambaJob)
                        .where(
                            SambaJob.job_type == "collect",
                            col(SambaJob.status).in_(
                                [JobStatus.PENDING, JobStatus.RUNNING]
                            ),
                        )
                    )
                ).scalar()
            ) or 0

    # 주문 동기화 잡: 같은 tenant 에서 동시 실행 1개만 허용 — 이중 호출 방지
    if body.job_type == "order_sync":
        from backend.domain.samba.job.model import SambaJob
        from sqlmodel import select, col

        active = (
            (
                await session.execute(
                    select(SambaJob)
                    .where(
                        SambaJob.job_type == "order_sync",
                        col(SambaJob.status).in_(
                            [JobStatus.PENDING, JobStatus.RUNNING]
                        ),
                        SambaJob.tenant_id == body.tenant_id,
                    )
                    .order_by(SambaJob.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if active:
            if _is_stale_order_sync_job(active):
                active.status = JobStatus.FAILED
                active.error = "stale order_sync job auto-failed before restart"
                active.completed_at = datetime.now(UTC)
                session.add(active)
                await session.flush()
            else:
                return {
                    "id": active.id,
                    "status": active.status,
                    "job_type": "order_sync",
                    "duplicate": True,
                    "current": active.current,
                    "total": active.total,
                }

    # 전송 잡: 중복 클릭/요청 차단 + 이어하기
    if body.job_type == "transmit":
        from backend.domain.samba.job.model import SambaJob
        from sqlmodel import select, col
        from sqlalchemy import text as _text

        new_pids: list = body.payload.get("product_ids") or []

        # advisory lock — 동시 중복 요청 직렬화 (트랜잭션 종료 시 자동 해제)
        _lock_key = f"transmit_create:{body.tenant_id or 'default'}"
        await session.execute(
            _text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": _lock_key},
        )

        # 1) PENDING/RUNNING 잡과 중복 → 기존 잡 그대로 반환
        active_jobs = (
            (
                await session.execute(
                    select(SambaJob)
                    .where(
                        SambaJob.job_type == "transmit",
                        col(SambaJob.status).in_(
                            [JobStatus.PENDING, JobStatus.RUNNING]
                        ),
                    )
                    .order_by(SambaJob.created_at.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        for a in active_jobs:
            if not a.payload:
                continue
            existing_pids: list = a.payload.get("product_ids") or []
            existing_accounts: set = set(a.payload.get("target_account_ids") or [])
            new_accounts: set = set(body.payload.get("target_account_ids") or [])
            # 계정이 겹치지 않으면 다른 마켓 전송 → 허용
            if (
                existing_accounts
                and new_accounts
                and not (existing_accounts & new_accounts)
            ):
                continue
            if existing_pids == new_pids and existing_accounts == new_accounts:
                # 완전 동일 (상품+계정) → 중복 클릭, 기존 잡 재활용
                return {
                    "id": a.id,
                    "status": a.status,
                    "job_type": "transmit",
                    "duplicate": True,
                    "current": a.current,
                    "total": a.total,
                }
            if new_pids and set(existing_pids) & set(new_pids):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"진행 중인 전송({a.id[:8]})과 상품이 중복됩니다. "
                        "완료 후 재시도하세요."
                    ),
                )

        # 2) FAILED 잡이 있으면 이어하기 (current 위치부터 재개)
        prev = (
            (
                await session.execute(
                    select(SambaJob)
                    .where(
                        SambaJob.job_type == "transmit",
                        col(SambaJob.status).in_([JobStatus.FAILED]),
                        SambaJob.total > 0,
                        SambaJob.current > 0,
                        SambaJob.current < SambaJob.total,  # 전체 완료된 Job 제외
                    )
                    .order_by(SambaJob.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if prev and prev.payload and prev.payload.get("product_ids") == new_pids:
            # 같은 상품 목록 → 기존 잡을 pending으로 리셋하여 이어하기
            prev.status = JobStatus.PENDING
            prev.started_at = None
            prev.error = None
            prev.completed_at = None
            # current는 유지 → 워커가 이어서 처리
            session.add(prev)
            await session.commit()
            return {
                "id": prev.id,
                "status": JobStatus.PENDING,
                "job_type": "transmit",
                "resumed_from": prev.current,
            }

        # 3) source_sites / brands 사전 계산 → transmit-queue-status 폴링 비용 제거
        if new_pids:
            from backend.domain.samba.collector.model import SambaCollectedProduct

            meta_result = await session.execute(
                select(
                    SambaCollectedProduct.source_site,
                    SambaCollectedProduct.brand,
                )
                .where(col(SambaCollectedProduct.id).in_(list(new_pids)))
                .distinct()
            )
            sites_seen: list[str] = []
            brands_seen: list[str] = []
            for psite, pbrand in meta_result.all():
                if psite and psite not in sites_seen:
                    sites_seen.append(psite)
                if pbrand and pbrand not in brands_seen:
                    brands_seen.append(pbrand)
            body.payload["source_sites"] = sites_seen
            body.payload["brands"] = brands_seen

    job = await svc.create_job(
        {
            "job_type": body.job_type,
            "payload": body.payload,
            "tenant_id": body.tenant_id,
        }
    )
    await session.commit()
    resp: dict = {"id": job.id, "status": job.status, "job_type": job.job_type}
    if queue_position > 0:
        resp["queue_position"] = queue_position
    return resp


@router.get("")
async def list_jobs(
    status: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """잡 목록 조회 (payload 제외 — 경량 응답).

    Write DB 사용 — Read Replica 복제 지연으로 cancel 직후 stale 상태 반환 방지.
    """
    svc = SambaJobService(SambaJobRepository(session))
    jobs = await svc.list_jobs(status=status, skip=skip, limit=limit)
    return [
        {
            "id": j.id,
            "job_type": j.job_type,
            "status": j.status,
            "progress": j.progress,
            "current": j.current,
            "total": j.total,
            "error": j.error,
            "created_at": j.created_at,
            "started_at": j.started_at,
            "completed_at": j.completed_at,
        }
        for j in jobs
    ]


# ── 정적 경로 라우트 (/{job_id}보다 먼저 등록해야 라우트 충돌 방지) ──


@router.get("/shipment-logs")
async def get_shipment_log_buffer(
    since_idx: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """전송 로그 링 버퍼 조회 — 창 닫아도 유지. 버퍼 비어있으면 DB fallback."""
    from backend.domain.samba.job.worker import (
        get_shipment_logs,
        is_shipment_log_cleared,
    )
    from sqlalchemy import text as _text

    logs, current_idx = get_shipment_logs(since_idx)

    # 사용자가 방금 초기화 했다면 DB fallback 건너뜀 (옛 로그 재유입 차단)
    if current_idx == 0 and since_idx == 0 and not is_shipment_log_cleared():
        # jsonb 캐스팅 오류 방지: left('[') 로 배열 여부 텍스트 레벨에서 먼저 확인
        # (planner가 jsonb_array_length를 scalar 행에 먼저 평가해 터지던 버그 회피)
        result = await session.execute(
            _text(
                "SELECT logs FROM samba_jobs"
                " WHERE job_type='transmit' AND logs IS NOT NULL"
                " AND left(trim(logs::text), 1) = '['"
                " ORDER BY created_at DESC LIMIT 1"
            )
        )
        row = result.first()
        if row and row[0]:
            db_logs = row[0] if isinstance(row[0], list) else []
            if db_logs:
                # 인덱스는 메모리 카운터(0) 기준으로 반환 — DB 개수로 주면
                # 서버 재시작 직후 프론트 sinceIdx가 메모리 총량을 앞질러
                # 신규 로그가 영영 반환되지 않는 버그 발생
                return {"logs": db_logs[-300:], "current_idx": 0}

    return {"logs": logs, "current_idx": current_idx}


@router.post("/shipment-logs/clear")
async def clear_shipment_log_buffer():
    """전송 로그 링 버퍼 초기화."""
    from backend.domain.samba.job.worker import clear_shipment_logs

    clear_shipment_logs()
    return {"ok": True}


@router.get("/collect-logs")
async def get_collect_log_buffer(
    since_idx: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """수집 로그 링 버퍼 조회 — 창 닫아도 유지. 버퍼 비어있으면 DB fallback."""
    from backend.domain.samba.job.worker import get_collect_logs
    from sqlalchemy import text as _text

    logs, current_idx = get_collect_logs(since_idx)

    if len(logs) == 0:
        # 크로스 인스턴스 fallback: 실행 중인 collect job의 DB 로그 조회
        # jsonb 캐스팅 오류 방지: left('[') 로 배열 여부 텍스트 레벨에서 먼저 확인
        result = await session.execute(
            _text(
                "SELECT logs FROM samba_jobs"
                " WHERE job_type='collect' AND status='running'"
                " AND logs IS NOT NULL"
                " AND left(trim(logs::text), 1) = '['"
                " ORDER BY started_at DESC LIMIT 1"
            )
        )
        row = result.first()
        if row and row[0]:
            db_logs = row[0] if isinstance(row[0], list) else []
            if len(db_logs) > since_idx:
                return {"logs": db_logs[since_idx:], "current_idx": len(db_logs)}

        # 실행 중인 job 없으면 최근 완료 job fallback (since_idx=0일 때만)
        # 시간 제한 5분: 다른 종류의 잡(예: ABC브랜드전체수집)이 한참 전에 끝난 로그가
        # 새로 시작한 brand-scan 화면에 잘못 노출되는 것 방지
        if since_idx == 0:
            result = await session.execute(
                _text(
                    "SELECT logs FROM samba_jobs"
                    " WHERE job_type='collect' AND logs IS NOT NULL"
                    " AND left(trim(logs::text), 1) = '['"
                    " AND COALESCE(completed_at, started_at, created_at)"
                    "     >= NOW() - INTERVAL '5 minutes'"
                    " ORDER BY created_at DESC LIMIT 1"
                )
            )
            row = result.first()
            if row and row[0]:
                db_logs = row[0] if isinstance(row[0], list) else []
                return {"logs": db_logs[-300:], "current_idx": len(db_logs)}

    return {"logs": logs, "current_idx": current_idx}


@router.post("/collect-logs/clear")
async def clear_collect_log_buffer():
    """수집 로그 링 버퍼 초기화."""
    from backend.domain.samba.job.worker import clear_collect_logs

    clear_collect_logs()
    return {"ok": True}


class CollectLogAddRequest(BaseModel):
    message: str


@router.post("/collect-logs/add")
async def add_collect_log(body: CollectLogAddRequest):
    """프론트엔드 로그를 서버 링 버퍼에 추가 — 페이지 이탈 후 복원용."""
    from backend.domain.samba.job.worker import _add_collect_log

    _add_collect_log(body.message)
    return {"ok": True}


@router.get("/collect-queue-status")
async def get_collect_queue_status(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """수집 Job 큐 상태 — 진행/대기 그룹 이름 포함."""
    from sqlmodel import select, col
    from backend.domain.samba.job.model import SambaJob
    from backend.domain.samba.collector.model import SambaSearchFilter

    stmt = (
        select(SambaJob)
        .where(
            SambaJob.job_type == "collect",
            col(SambaJob.status).in_([JobStatus.RUNNING, JobStatus.PENDING]),
        )
        .order_by(SambaJob.created_at.asc())
    )
    result = await session.execute(stmt)
    jobs = result.scalars().all()

    # filter_id → SearchFilter 이름 일괄 조회
    filter_ids = [
        (j.payload or {}).get("filter_id", "")
        for j in jobs
        if (j.payload or {}).get("filter_id")
    ]
    filter_map: dict[str, tuple[str, str]] = {}
    if filter_ids:
        f_result = await session.execute(
            select(
                SambaSearchFilter.id,
                SambaSearchFilter.name,
                SambaSearchFilter.source_site,
            ).where(col(SambaSearchFilter.id).in_(filter_ids))
        )
        for fid, fname, fsite in f_result.all():
            filter_map[fid] = (fname or "", fsite or "")

    running = []
    pending = []
    for j in jobs:
        payload = j.payload or {}
        fid = payload.get("filter_id", "")
        fname, fsite = filter_map.get(fid, ("", payload.get("source_site", "")))
        # brand_all 잡: filter_id 없이 brand/keyword 조합으로 이름 표시
        if not fname:
            brand = payload.get("brand") or payload.get("keyword") or ""
            fname = brand if brand else ""
        item = {
            "id": j.id,
            "filter_name": fname,
            "source_site": fsite,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "current": j.current or 0,
            "total": j.total or 0,
        }
        if j.status == JobStatus.RUNNING:
            running.append(item)
        else:
            pending.append(item)

    return {"running": running, "pending": pending}


@router.get("/transmit-queue-status")
async def get_transmit_queue_status(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """전송 Job 큐 상태 — 마켓명·계정·진행률·소싱처·브랜드 포함.

    소싱처/브랜드는 잡 생성 시 payload에 사전 계산되어 저장됨
    (create_job 참고). 매 폴링마다 product 메타를 IN 조회하던 N+1 비용 제거.
    """
    from sqlmodel import select, col
    from backend.domain.samba.job.model import SambaJob
    from backend.domain.samba.account.model import SambaMarketAccount

    stmt = (
        select(SambaJob)
        .where(
            col(SambaJob.job_type).in_(["transmit", "delete_market"]),
            col(SambaJob.status).in_([JobStatus.RUNNING, JobStatus.PENDING]),
        )
        .order_by(SambaJob.created_at.asc())
    )
    result = await session.execute(stmt)
    jobs = result.scalars().all()

    # target_account_ids → 마켓 계정 이름 일괄 조회
    all_acc_ids: set[str] = set()
    for j in jobs:
        all_acc_ids.update((j.payload or {}).get("target_account_ids", []))

    acc_map: dict[str, str] = {}
    if all_acc_ids:
        acc_result = await session.execute(
            select(
                SambaMarketAccount.id,
                SambaMarketAccount.market_name,
                SambaMarketAccount.account_label,
            ).where(col(SambaMarketAccount.id).in_(list(all_acc_ids)))
        )
        for aid, mname, alabel in acc_result.all():
            acc_map[aid] = f"{mname}({alabel})" if alabel else mname

    running = []
    pending = []
    for j in jobs:
        payload = j.payload or {}
        target_ids = payload.get("target_account_ids", [])
        markets = ", ".join(
            dict.fromkeys(acc_map.get(a, "") for a in target_ids if acc_map.get(a))
        )
        pids = payload.get("product_ids", [])
        # payload 캐시 우선 사용 — brand_name/source_site 단수 필드로도 fallback
        sites: list[str] = list(payload.get("source_sites") or [])
        if not sites and payload.get("source_site"):
            sites = [payload["source_site"]]
        brands: list[str] = list(payload.get("brands") or [])
        if not brands and payload.get("brand_name"):
            brands = [payload["brand_name"]]
        item = {
            "id": j.id,
            "status": j.status,
            "kind": "delete" if j.job_type == "delete_market" else "transmit",
            "markets": markets or "알 수 없음",
            "source_sites": sites,
            "brands": brands,
            "product_count": len(pids),
            "current": j.current or 0,
            "total": j.total or 0,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            # 최근 윈도우 기준 건당 처리시간(초). 샘플 부족 시 None → 프런트가 누적평균 폴백.
            "per_item_sec": get_recent_sec_per_item(j.id),
        }
        if j.status == JobStatus.RUNNING:
            running.append(item)
        else:
            pending.append(item)

    return {"running": running, "pending": pending}


@router.post("/cancel-collect")
async def cancel_collect_jobs(
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """수집 잡만 취소 — 전송/오토튠은 영향 없음."""
    from sqlalchemy import text
    from backend.domain.samba.emergency import request_cancel_collect

    # 1) 인메모리 플래그로 즉시 중단 (같은 인스턴스 1~2초 내 반응)
    request_cancel_collect()
    # 소싱큐에 대기 중인 작업도 즉시 제거 (확장앱 탭 오픈 방지)
    from backend.domain.samba.proxy.sourcing_queue import SourcingQueue

    SourcingQueue.cancel_all(reason="collect cancelled by user")

    # 2) DB 상태 변경 (멀티인스턴스 대비) — .value로 실제 enum 값 사용
    r = await session.execute(
        text(
            f"UPDATE samba_jobs SET status = '{JobStatus.CANCELLED.value}', completed_at = now() "
            f"WHERE job_type = 'collect' AND status IN ('{JobStatus.PENDING.value}', '{JobStatus.RUNNING.value}')"
        )
    )
    await session.commit()
    return {"ok": True, "cancelled": r.rowcount}


@router.post("/cancel-transmit")
async def cancel_transmit_jobs(
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """전송 잡만 취소 — 수집/오토튠은 영향 없음."""
    from sqlalchemy import text
    from backend.domain.samba.emergency import trigger_emergency_stop
    from backend.domain.samba.shipment.service import request_cancel_transmit

    request_cancel_transmit()
    trigger_emergency_stop()

    # 일시정지(FAILED) 상태도 포함 — 작업취소는 재개 가능 Job도 제거해야 함
    r = await session.execute(
        text(
            f"UPDATE samba_jobs SET status = '{JobStatus.CANCELLED.value}', completed_at = now() "
            f"WHERE job_type = 'transmit' AND status IN ("
            f"'{JobStatus.PENDING.value}', "
            f"'{JobStatus.RUNNING.value}', "
            f"'{JobStatus.FAILED.value}'"
            f")"
        )
    )
    await session.commit()
    return {"ok": True, "cancelled": r.rowcount}


@router.post("/cancel-all")
async def cancel_all_jobs(
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """대기 중(pending) + 실행 중(running) 잡 전부 취소 — 전송도 즉시 중단."""
    from sqlalchemy import text
    from backend.domain.samba.emergency import trigger_emergency_stop
    from backend.domain.samba.shipment.service import request_cancel_transmit

    # 1) 인메모리 플래그로 즉시 중단 (진행 중 전송 포함)
    request_cancel_transmit()
    trigger_emergency_stop()

    # 2) DB 상태 일괄 취소 — .value로 실제 enum 값 사용
    r = await session.execute(
        text(
            f"UPDATE samba_jobs SET status = '{JobStatus.CANCELLED.value}', completed_at = now() "
            f"WHERE status IN ('{JobStatus.PENDING.value}', '{JobStatus.RUNNING.value}')"
        )
    )
    await session.commit()

    # 플래그 해제하지 않음 — 워커가 감지 후 직접 해제
    return {"ok": True, "cancelled": r.rowcount}


@router.get("/last-resumable-transmit")
async def get_last_resumable_transmit(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """재개 가능한 최근 transmit 잡 조회 (payload 포함).

    이어하기 대상은 일시정지(FAILED)만 — 명시적 취소(CANCELLED)는 제외.
    사용자가 "작업취소"/개별 취소한 잡이 새로고침 후 이어하기로 부활하는 것 방지.

    또한 해당 paused 잡 이후에 정상 완료(COMPLETED)된 transmit 잡이 있으면
    옛 paused 잡은 무시 — 새 전송이 한 번이라도 끝났으면 이어하기 버튼이
    유령처럼 켜져 있는 것을 방지한다.
    """
    from backend.domain.samba.job.model import SambaJob
    from sqlmodel import select

    job = (
        (
            await session.execute(
                select(SambaJob)
                .where(
                    SambaJob.job_type == "transmit",
                    SambaJob.status == JobStatus.FAILED,
                    SambaJob.total > 0,
                    SambaJob.current > 0,
                    SambaJob.current < SambaJob.total,
                )
                .order_by(SambaJob.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if not job:
        return None

    # paused 잡 이후 COMPLETED 된 transmit 잡이 존재하면 이어하기 후보에서 제외
    newer_completed = (
        await session.execute(
            select(SambaJob.id)
            .where(
                SambaJob.job_type == "transmit",
                SambaJob.status == JobStatus.COMPLETED,
                SambaJob.created_at > job.created_at,
            )
            .limit(1)
        )
    ).first()
    if newer_completed:
        return None
    return {
        "id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "payload": job.payload,
        "current": job.current,
        "total": job.total,
        "created_at": job.created_at,
    }


# ── 경로 파라미터 라우트 (정적 경로 뒤에 배치) ──


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """잡 상태 + 진행률 조회."""
    svc = SambaJobService(SambaJobRepository(session))
    job = await svc.get_job(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다")
    return job


@router.get("/{job_id}/logs")
async def get_job_logs(
    job_id: str,
    since: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """Job 실시간 로그 조회."""
    from backend.domain.samba.job.worker import get_job_logs
    from sqlalchemy import text as _text

    logs = get_job_logs(job_id, since)
    if logs:
        return {"logs": logs}

    row = (
        await session.execute(
            _text("SELECT logs FROM samba_jobs WHERE id = :jid"),
            {"jid": job_id},
        )
    ).first()
    if not row or not isinstance(row[0], list):
        return {"logs": []}

    db_logs = row[0]
    return {"logs": db_logs[since:] if since > 0 else db_logs}


@router.delete("/{job_id}")
async def cancel_job(
    job_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """잡 취소 (pending/running 모두 가능)."""
    from backend.domain.samba.shipment.service import request_cancel_transmit

    repo = SambaJobRepository(session)
    ok = await repo.cancel_job(job_id)
    if not ok:
        raise HTTPException(
            400, "취소할 수 없는 상태입니다 (pending/running만 취소 가능)"
        )
    await session.commit()
    # 실행 중인 전송 잡이면 인메모리 취소 플래그로 즉시 중단
    request_cancel_transmit(job_id)
    return {"ok": True}

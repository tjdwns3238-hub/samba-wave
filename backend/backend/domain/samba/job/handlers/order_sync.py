"""order_sync 잡 핸들러 — 활성 마켓 계정 순회 주문 동기화.

원래 `POST /samba/orders/sync-from-markets` 가 단일 요청에서 모든 활성 계정을
순차 처리하던 구조를, 백그라운드 잡으로 분리한 구현.

Caddy `response_header_timeout 120s` 우회 + 진행률 폴링 + 취소 가능.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from backend.domain.samba.job.model import SambaJob
from backend.domain.samba.job.repository import SambaJobRepository
from backend.domain.samba.job.worker import _add_job_log

logger = logging.getLogger(__name__)


def _per_account_timeout_seconds(days: int) -> int:
    # 180~300초. 11번가/롯데ON 정상 처리에 주문/배송준비/발주확인 N건/취소/반품/교환 등
    # 5~7개 API가 누적돼 60~90초가 보통이고 살짝 느려지면 120초로는 자주 끊김.
    # hang 좀비는 명시적 rollback + 클라이언트 aclose 로 차단되므로 늘려도 도미노는 안 남.
    return max(180, min(300, days * 60))


async def run(
    job: SambaJob,
    repo: SambaJobRepository,
    session: AsyncSession,
    worker: Any | None = None,
) -> None:
    """활성 마켓 계정을 순회하며 라우터 함수를 직접 호출해 주문 동기화.

    payload:
        days: int = 7        — 동기화 대상 기간(일)
        account_ids: list[str] | None — 특정 계정만 처리 (미지정 시 활성 전체)

    동작:
        1) 활성 계정 목록 조회 (tenant_id 격리)
        2) 진행률 초기화 (total = 계정 수)
        3) 각 계정에 대해 sync_orders_from_markets(account_id=acc.id) 직접 호출
           — 라우터 함수의 1,461줄 로직(스마트스토어/쿠팡/eBay/롯데ON 등)을 그대로 재사용
        4) 매 계정 후 progress 갱신 + 취소 체크
        5) complete_job(result={total_synced, results})
    """
    payload = job.payload or {}
    days = int(payload.get("days") or 7)
    account_ids: list[str] | None = payload.get("account_ids") or None

    # 1) 활성 마켓 계정 조회 — 라우터의 1864-1891 로직과 동일한 정책
    from backend.domain.samba.account.repository import SambaMarketAccountRepository

    acc_repo = SambaMarketAccountRepository(session)
    accs = await acc_repo.filter_by_async(
        is_active=True, order_by="created_at", order_by_desc=True
    )
    # 테넌트 격리: 잡의 tenant_id 가 있으면 해당 테넌트 계정 + 공용(None) 만 유지
    if job.tenant_id is not None:
        accs = [a for a in accs if a.tenant_id == job.tenant_id or a.tenant_id is None]
    # 특정 계정만 지정한 경우 추가 필터
    if account_ids:
        _id_set = set(account_ids)
        accs = [a for a in accs if a.id in _id_set]

    total = len(accs)
    _add_job_log(job.id, f"전체마켓 주문수집 시작 ({total}개 계정, 최근 {days}일)")
    job.total = total
    job.current = 0
    job.progress = 0
    session.add(job)
    await session.flush()

    # 라우터 함수 직접 호출(Depends 우회) — 라우터 변경 0
    from backend.api.v1.routers.samba.order import (
        sync_orders_from_markets,
        SyncOrdersRequest,
    )
    from backend.db.orm import get_write_session

    total_synced = 0
    all_results: list[dict[str, Any]] = []
    per_account_timeout = _per_account_timeout_seconds(days)

    # 동시 실행 한도 — 풀(write max 50) 안에서 오토튠 ~10 + 마진 고려.
    # 5병렬 시 11번가 IP rate-limit/DB 풀 경합으로 다수 timeout 관측되어 3으로 하향.
    # 24계정 기준 sequential 대비 약 3배 빠름. isolation 은 계정별 독립 세션 유지.
    _CONCURRENCY = 3
    _sem = asyncio.Semaphore(_CONCURRENCY)
    _done_counter = {"n": 0}
    _cancel_flag = {"cancelled": False}

    async def _process_account(idx: int, acc: Any) -> None:
        # 사용자 취소 감지 시 새 계정 시작 막음 (이미 시작된 건 끝까지 진행)
        if _cancel_flag["cancelled"]:
            return
        async with _sem:
            if _cancel_flag["cancelled"]:
                return
            label = f"{acc.market_name}({acc.seller_id or '-'})"
            _add_job_log(
                job.id,
                f"{label}: 주문수집 시작 ({idx + 1}/{total}, 최근 {days}일, 제한 {per_account_timeout}초)",
            )
            res: dict[str, Any] | None = None
            try:
                # 계정마다 독립 세션 — 앞 계정의 commit/rollback 잔류 상태로 인한 오염 차단
                async with get_write_session() as acc_session:
                    try:
                        res = await asyncio.wait_for(
                            sync_orders_from_markets(
                                body=SyncOrdersRequest(days=days, account_id=acc.id),
                                session=acc_session,
                                tenant_id=job.tenant_id,
                            ),
                            timeout=per_account_timeout,
                        )
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        # asyncpg + CancelledError 좀비 차단 — 명시적 rollback
                        try:
                            await asyncio.wait_for(acc_session.rollback(), timeout=5)
                        except Exception as _rb_err:
                            logger.warning(
                                f"[order_sync] {label} TimeoutError 후 명시적 rollback 실패: {_rb_err}"
                            )
                        raise
                nonlocal total_synced
                total_synced += int(res.get("total_synced") or 0)
                results = res.get("results") or []
                for r in results:
                    all_results.append(r)
                    if r.get("status") == "success":
                        _add_job_log(
                            job.id,
                            f"{r.get('account', label)}: "
                            f"{r.get('fetched', 0)}건 조회, "
                            f"{r.get('synced', 0)}건 신규 저장",
                        )
                    elif r.get("status") == "skip":
                        _add_job_log(
                            job.id,
                            f"{r.get('account', label)}: {r.get('message', '')}",
                        )
                    else:
                        _add_job_log(
                            job.id,
                            f"{r.get('account', label)}: 오류 — {r.get('message', '')}",
                        )
            except asyncio.TimeoutError:
                logger.error(
                    f"[order_sync] {label} timeout after {per_account_timeout}s"
                )
                _add_job_log(
                    job.id,
                    f"{label} 오류: {per_account_timeout}초 동안 응답이 없어 다음 계정으로 넘어갑니다",
                )
                all_results.append(
                    {
                        "account": label,
                        "status": "error",
                        "message": f"timeout after {per_account_timeout}s",
                    }
                )
            except Exception as e:
                logger.error(f"[order_sync] {label} 실패: {e}")
                _add_job_log(job.id, f"{label} 오류: {e}")
                all_results.append(
                    {"account": label, "status": "error", "message": str(e)[:500]}
                )

            # 진행률 갱신 — 처리 끝낼 때마다 fresh 세션 + 5초 타임아웃
            _done_counter["n"] += 1
            _done = _done_counter["n"]
            try:
                async with get_write_session() as prog_session:
                    prog_repo = SambaJobRepository(prog_session)
                    await asyncio.wait_for(
                        prog_repo.update_progress(job.id, _done, total),
                        timeout=5,
                    )
                    await prog_session.commit()
            except (asyncio.TimeoutError, Exception) as pe:
                logger.warning(
                    f"[order_sync] {job.id} 진행률 갱신 실패 (계속 진행): {pe}"
                )

    # 백그라운드 취소 감시 — 5초마다 is_cancelled 확인 후 _cancel_flag 토글
    async def _cancel_watcher() -> None:
        while not _cancel_flag["cancelled"]:
            await asyncio.sleep(5)
            try:
                if await repo.is_cancelled(job.id):
                    _cancel_flag["cancelled"] = True
                    _add_job_log(job.id, "사용자 취소 — 신규 계정 시작 중단")
                    return
            except Exception:
                pass

    _watcher_task = asyncio.create_task(_cancel_watcher())
    try:
        await asyncio.gather(
            *[_process_account(idx, acc) for idx, acc in enumerate(accs)],
            return_exceptions=True,
        )
    finally:
        _cancel_flag["cancelled"] = True  # 워처 종료 신호
        _watcher_task.cancel()
        try:
            await _watcher_task
        except (asyncio.CancelledError, Exception):
            pass

    _add_job_log(job.id, f"전체마켓 주문수집 완료 — 총 {total_synced}건 신규 저장")

    # 잡 완료 — 워커 세션이 idle in transaction/풀 락으로 hang 되면 status가
    # 영원히 'running' 으로 남아 프론트가 "주문수집 중..." 무한 표시되는 사고가 있어
    # 독립된 fresh 세션에서 즉시 commit (워커 세션과 분리)
    from backend.domain.samba.job.repository import SambaJobRepository as _Repo

    try:
        async with get_write_session() as fin_session:
            fin_repo = _Repo(fin_session)
            await asyncio.wait_for(
                fin_repo.complete_job(
                    job.id,
                    result={"total_synced": total_synced, "results": all_results},
                ),
                timeout=10,
            )
            await fin_session.commit()
    except Exception as fe:
        logger.error(
            f"[order_sync] {job.id} 최종 commit 실패 — 워커 세션 fallback: {fe}"
        )
        # fallback: 워커 세션 — finally 에서 commit 시도
        await repo.complete_job(
            job.id,
            result={"total_synced": total_synced, "results": all_results},
        )

"""주문 자동 폴링 — 새 주문 감지 시 자동 동기화 (8~24시) 또는 카카오톡 알림 발송."""

import asyncio
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

ORDER_POLL_INTERVAL = int(os.environ.get("ORDER_POLL_INTERVAL_SECONDS", str(30 * 60)))
KST = timezone(timedelta(hours=9))


async def _fetch_new_order_numbers(
    session,
) -> tuple[dict[str, list[str]], set[str | None]]:
    """각 마켓 계정에서 최근 1일치 주문 번호를 조회하고, DB에 없는 신규 건만 반환.

    Returns:
        (new_by_market, tenant_ids_with_new_orders)
    """
    from sqlalchemy import text as _text
    from sqlmodel import select

    from backend.domain.samba.account.model import SambaMarketAccount

    result = await session.exec(
        select(SambaMarketAccount).where(SambaMarketAccount.is_active == True)  # noqa: E712
    )
    accounts = result.all()

    new_by_market: dict[str, list[str]] = defaultdict(list)
    tenant_ids_with_new: set[str | None] = set()

    for account in accounts:
        market_type = account.market_type
        extras = account.additional_fields or {}
        label = account.market_name or market_type

        try:
            raw_order_numbers: list[str] = []

            if market_type == "smartstore":
                from backend.domain.samba.proxy.smartstore import SmartStoreClient

                client_id = extras.get("clientId", "") or account.api_key or ""
                client_secret = (
                    extras.get("clientSecret", "") or account.api_secret or ""
                )
                if not client_id or not client_secret:
                    continue
                client = SmartStoreClient(client_id, client_secret)
                raw_orders = await client.get_orders(days=1)
                for ro in raw_orders:
                    po = ro.get("productOrder", ro)
                    oid = po.get("productOrderId", "")
                    if oid:
                        raw_order_numbers.append(oid)

            elif market_type == "lotteon":
                from backend.domain.samba.proxy.lotteon import LotteonClient

                api_key = extras.get("apiKey", "") or account.api_key or ""
                if not api_key:
                    continue
                client = LotteonClient(api_key)
                await client.test_auth()
                raw_orders = await client.get_orders(days=7)
                for ro in raw_orders:
                    od_no = str(ro.get("odNo", "") or "")
                    od_seq = ro.get("odSeq", 1) or 1
                    oid = f"{od_no}_{od_seq}" if od_no else ""
                    if oid:
                        raw_order_numbers.append(oid)

            elif market_type == "playauto":
                from datetime import UTC, datetime, timedelta

                from backend.domain.samba.proxy.playauto import PlayAutoClient

                api_key = extras.get("apiKey", "") or account.api_key or ""
                if not api_key:
                    continue
                start_date = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y%m%d")
                client = PlayAutoClient(api_key)
                try:
                    raw_orders = await client.get_orders(
                        start_date=start_date, count=200
                    )
                    for ro in raw_orders:
                        oid = str(ro.get("OrderCode", "") or "")
                        if oid:
                            raw_order_numbers.append(oid)
                finally:
                    await client.close()

            else:
                continue

            if not raw_order_numbers:
                continue

            # DB에 이미 있는 order_number 필터링
            rows = await session.execute(
                _text(
                    "SELECT order_number FROM samba_order "
                    "WHERE order_number = ANY(:nums) AND channel_id = :cid"
                ),
                {"nums": raw_order_numbers, "cid": account.id},
            )
            existing = {r[0] for r in rows}
            fresh = [n for n in raw_order_numbers if n not in existing]

            if fresh:
                new_by_market[label].extend(fresh)
                tenant_ids_with_new.add(account.tenant_id)
                logger.info("[주문폴러] %s: 신규 주문 %d건 감지", label, len(fresh))

        except Exception as exc:
            logger.warning("[주문폴러] %s 조회 실패: %s", label, exc)

    return dict(new_by_market), tenant_ids_with_new


async def _run_direct_order_sync(tenant_ids: set[str | None]) -> None:
    """신규 주문 감지 시 잡 큐 없이 직접 동기화 실행 (전송 잡에 밀리지 않도록)."""
    from sqlmodel import select

    from backend.api.v1.routers.samba.order import (
        SyncOrdersRequest,
        sync_orders_from_markets,
    )
    from backend.db.orm import get_write_session
    from backend.domain.samba.account.model import SambaMarketAccount

    logger.info("[주문폴러] 직접 동기화 시작 (테넌트 수: %d)", len(tenant_ids))

    async with get_write_session() as session:
        result = await session.exec(
            select(SambaMarketAccount).where(SambaMarketAccount.is_active == True)  # noqa: E712
        )
        all_accounts = result.all()

    for tenant_id in tenant_ids:
        accounts = (
            [a for a in all_accounts if a.tenant_id == tenant_id or a.tenant_id is None]
            if tenant_id is not None
            else list(all_accounts)
        )

        for acc in accounts:
            try:
                # 마켓 API hang 시 폴러가 DB 풀 잠식 → 다른 워커까지 hang 도미노 차단.
                # 1계정 최대 180초 + TimeoutError 시 명시적 rollback (asyncpg + CancelledError
                # 좀비 회피, 2026-05-15 사고 기반).
                async with get_write_session() as acc_session:
                    try:
                        res = await asyncio.wait_for(
                            sync_orders_from_markets(
                                body=SyncOrdersRequest(days=7, account_id=acc.id),
                                session=acc_session,
                                tenant_id=tenant_id,
                            ),
                            timeout=180,
                        )
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        try:
                            await asyncio.wait_for(acc_session.rollback(), timeout=5)
                        except Exception:
                            pass
                        raise
                synced = res.get("total_synced", 0) if isinstance(res, dict) else 0
                if synced:
                    logger.info(
                        "[주문폴러] %s: %d건 신규 저장",
                        acc.market_name or acc.market_type,
                        synced,
                    )
            except Exception as exc:
                logger.warning(
                    "[주문폴러] %s 동기화 실패: %s",
                    acc.market_name or acc.market_type,
                    exc,
                )


async def _create_cs_sync_job(session, tenant_id: str | None) -> None:
    """cs_sync 잡 생성 (중복 실행 방지 포함)."""
    from sqlmodel import col, select

    from backend.domain.samba.job.model import JobStatus, SambaJob, generate_job_id

    active = (
        (
            await session.execute(
                select(SambaJob)
                .where(
                    SambaJob.job_type == "cs_sync",
                    col(SambaJob.status).in_([JobStatus.PENDING, JobStatus.RUNNING]),
                    SambaJob.tenant_id == tenant_id,
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if active:
        logger.info(
            "[주문폴러] cs_sync 잡 이미 실행 중 (tenant=%s, job=%s)",
            tenant_id,
            active.id,
        )
        return

    job = SambaJob(
        id=generate_job_id(),
        tenant_id=tenant_id,
        job_type="cs_sync",
        payload={},
    )
    session.add(job)
    await session.flush()
    logger.info("[주문폴러] cs_sync 잡 생성 (tenant=%s, job=%s)", tenant_id, job.id)


async def start_order_poller() -> None:
    """백그라운드 주문 폴링 루프 (lifecycle.py에서 asyncio.create_task로 실행)."""
    from backend.db.orm import get_write_session
    from backend.utils.kakao_notify import send_kakao_message

    # 서버 완전 기동 대기
    await asyncio.sleep(60)
    logger.info("[주문폴러] 시작 (간격: %d초)", ORDER_POLL_INTERVAL)

    while True:
        try:
            now_kst = datetime.now(KST)
            is_night = 0 <= now_kst.hour < 8  # 0~8시 제외

            async with get_write_session() as session:
                new_by_market, tenant_ids = await _fetch_new_order_numbers(session)

                if not is_night:
                    # CS는 주문 감지 여부와 무관하게 30분마다 전체 동기화
                    await _create_cs_sync_job(session, tenant_id=None)

            if new_by_market and not is_night:
                # 잡 큐 대신 직접 동기화 — 전송 잡에 밀리지 않도록
                asyncio.create_task(_run_direct_order_sync(tenant_ids))

            if new_by_market:
                total = sum(len(v) for v in new_by_market.values())
                lines = [f"🛒 새 주문 {total}건 감지"]
                for market, nums in new_by_market.items():
                    lines.append(f"  {market}: {len(nums)}건")

                if is_night:
                    lines.append("\n동기화 버튼을 눌러 주문을 확인하세요.")
                else:
                    lines.append("\n자동 동기화를 시작했습니다.")

                await send_kakao_message("\n".join(lines))

        except asyncio.CancelledError:
            logger.info("[주문폴러] 종료")
            return
        except Exception as exc:
            logger.warning("[주문폴러] 오류 발생: %s", exc)

        await asyncio.sleep(ORDER_POLL_INTERVAL)

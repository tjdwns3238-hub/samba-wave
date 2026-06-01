"""SSG 반려/영구판매중지 상품 잔존 자동 진단 (issue #308).

SSG는 등록 API 호출 즉시 itemId를 발급하지만 MD 심사는 비동기로 진행됨.
반려(chngDemndProcStatCd=30) 또는 영구판매중지(sellStatCd=90) 처리돼도
registered_accounts / market_product_nos 에 잔존 → 대시보드 과집계 + tetris 가
'이미 등록됨'으로 판단해 재전송 안 함(영원히 미판매 방치).

매일 1회 모든 활성 SSG 계정에 대해:
1. registered_accounts 에 계정 id 가 있는 상품 수집
2. market_product_nos[account_id] 의 itemId 로 SSG 판매상태/승인상태 조회
3. 영구판매중지(90) / MD반려(30) / 정상 / 에러 로 분류
4. 임계치 초과 시 samba_monitor_event 기록 + WARN 로그
5. SSG_AUTO_CLEAN_DEAD=1 환경변수 켜져 있을 때만 실제 정리 (기본은 알림만)

elevenst_ghost_reconciler 패턴을 그대로 따름 (방향만 반대 — '누락'이 아닌 '비판매 잔존').
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from backend.db.orm import get_write_session
from backend.shutdown_state import is_shutting_down


logger = logging.getLogger("backend.ssg.status_reconciler")

RUN_INTERVAL_SECONDS = 24 * 3600
INITIAL_DELAY_SECONDS = 60 * 35
ALERT_THRESHOLD = 10
MAX_CHECK_PER_ACCOUNT = 2000
THROTTLE_SECONDS = 0.4
AUTO_CLEAN = os.environ.get("SSG_AUTO_CLEAN_DEAD", "").lower() in (
    "1",
    "true",
    "yes",
)
# 비판매로 간주하는 판매상태 코드 (영구판매중지)
DEAD_SELL_STAT = {"90"}
# MD 반려 처리상태 코드
REJECTED_DEMAND_STAT = {"30"}


async def _fetch_active_accounts() -> list[dict[str, Any]]:
    async with get_write_session() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT id, account_label, api_key, additional_fields "
                        "FROM samba_market_account "
                        "WHERE market_type='ssg' AND is_active=true"
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def _extract_api_key(acc: dict[str, Any]) -> str:
    af = acc.get("additional_fields") or {}
    if isinstance(af, dict):
        v = af.get("apiKey")
        if v:
            return str(v)
    return str(acc.get("api_key") or "").strip()


def _extract_item_id(nos: dict[str, Any], account_id: str) -> str:
    """market_product_nos[account_id] 에서 SSG itemId 추출 (str / dict 모두 대응)."""
    v = nos.get(account_id)
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return str(v.get("itemId") or v.get("productNo") or "").strip()
    return ""


async def _log_monitor_event(
    account_id: str,
    account_label: str,
    total_dead: int,
    perm_stop: int,
    rejected: int,
) -> None:
    """samba_monitor_event 에 알림 기록."""
    try:
        from backend.domain.samba.warroom.model import SambaMonitorEvent

        async with get_write_session() as session:
            session.add(
                SambaMonitorEvent(
                    event_type="ssg_dead_product_detected",
                    severity="warning" if total_dead < ALERT_THRESHOLD else "critical",
                    market_type="ssg",
                    summary=f"SSG {account_label} 반려/판매중지 잔존 {total_dead}건 감지",
                    detail={
                        "account_id": account_id,
                        "account_label": account_label,
                        "total_dead": total_dead,
                        "perm_stop": perm_stop,
                        "rejected": rejected,
                        "auto_clean_enabled": AUTO_CLEAN,
                    },
                )
            )
            await session.commit()
    except Exception as e:
        logger.debug(f"[ssg_status] monitor_event 기록 스킵: {e}")


def _is_dead(sell_stat_cd: str) -> bool:
    return str(sell_stat_cd or "").strip() in DEAD_SELL_STAT


async def _classify_item(client: Any, item_id: str) -> str:
    """itemId 의 상태를 'perm_stop' / 'rejected' / 'alive' / 'error' 로 분류."""
    # 1) 판매상태 — sellStatCd=90(영구판매중지)
    try:
        status_resp = await client.get_item_sales_status(item_id)
        res_obj = status_resp.get("result", {})
        sales_status = (
            (res_obj.get("salesStatus") if isinstance(res_obj, dict) else None)
            or status_resp.get("salesStatus")
            or {}
        )
        if isinstance(sales_status, dict) and _is_dead(sales_status.get("sellStatCd")):
            return "perm_stop"
    except Exception as e:
        logger.debug(f"[ssg_status] {item_id} 판매상태 조회 실패: {e}")
        return "error"

    # 2) 승인상태 — chngDemndProcStatCd=30(MD반려)
    try:
        demands = await client.get_item_approval_status(item_id, "00")
        for d in demands or []:
            if not isinstance(d, dict):
                continue
            if str(d.get("chngDemndProcStatCd") or "").strip() in REJECTED_DEMAND_STAT:
                return "rejected"
    except Exception as e:
        logger.debug(f"[ssg_status] {item_id} 승인상태 조회 실패: {e}")
        # 판매상태는 정상 조회됐으므로 alive 로 간주

    return "alive"


async def _clean_db_entry(account_id: str, product_id: str) -> bool:
    """registered_accounts + market_product_nos 에서 해당 계정 제거. 변경 여부 반환."""
    from backend.domain.samba.collector.model import SambaCollectedProduct

    try:
        async with get_write_session() as session:
            prod = await session.get(SambaCollectedProduct, product_id)
            if prod is None:
                return False
            changed = False
            nos = dict(prod.market_product_nos or {})
            for k in (account_id, f"{account_id}_origin"):
                if k in nos:
                    nos.pop(k, None)
                    changed = True
            if changed:
                prod.market_product_nos = nos
                flag_modified(prod, "market_product_nos")
            regs_old = list(prod.registered_accounts or [])
            if account_id in regs_old:
                prod.registered_accounts = [a for a in regs_old if a != account_id]
                flag_modified(prod, "registered_accounts")
                changed = True
            if changed:
                session.add(prod)
                await session.commit()
            return changed
    except Exception as e:
        logger.debug(f"[ssg_status] DB정리 예외 {product_id}: {e}")
        return False


async def _reconcile_one_account(acc: dict[str, Any]) -> dict[str, Any]:
    from backend.domain.samba.collector.model import SambaCollectedProduct
    from backend.domain.samba.proxy.ssg import SSGClient

    label = acc["account_label"]
    account_id = acc["id"]
    api_key = _extract_api_key(acc)
    if not api_key:
        return {"account_label": label, "skipped": "no api_key"}

    # 대상 수집: registered_accounts 에 이 계정이 있는 상품
    async with get_write_session() as session:
        prod_q = (
            select(SambaCollectedProduct)
            .where(SambaCollectedProduct.registered_accounts.op("@>")([account_id]))
            .limit(MAX_CHECK_PER_ACCOUNT)
        )
        products = (await session.execute(prod_q)).scalars().all()

    targets: list[dict[str, str]] = []
    for p in products:
        item_id = _extract_item_id(p.market_product_nos or {}, account_id)
        if item_id:
            targets.append({"product_id": str(p.id), "item_id": item_id})

    if not targets:
        logger.info(f"[ssg_status] OK {label} 대상 없음")
        return {"account_label": label, "total_dead": 0, "perm_stop": 0, "rejected": 0}

    client = SSGClient(api_key)
    perm_stop: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    failed = 0

    for t in targets:
        kind = await _classify_item(client, t["item_id"])
        if kind == "perm_stop":
            perm_stop.append(t)
        elif kind == "rejected":
            rejected.append(t)
        elif kind == "error":
            failed += 1
        await asyncio.sleep(THROTTLE_SECONDS)

    total_dead = len(perm_stop) + len(rejected)

    if total_dead > 0:
        severity = "WARN" if total_dead < ALERT_THRESHOLD else "CRIT"
        logger.warning(
            f"[ssg_status] {severity} {label} 비판매잔존={total_dead} "
            f"(perm_stop={len(perm_stop)} rejected={len(rejected)} failed={failed})"
        )
        await _log_monitor_event(
            account_id, label, total_dead, len(perm_stop), len(rejected)
        )

        if AUTO_CLEAN:
            cleaned = 0
            for item in perm_stop + rejected:
                if await _clean_db_entry(account_id, item["product_id"]):
                    cleaned += 1
            logger.warning(f"[ssg_status] {label} AUTO_CLEAN 완료 db_cleared={cleaned}")
    else:
        logger.info(f"[ssg_status] OK {label} 비판매 잔존 없음 (failed={failed})")

    return {
        "account_label": label,
        "total_dead": total_dead,
        "perm_stop": len(perm_stop),
        "rejected": len(rejected),
        "failed": failed,
    }


async def reconcile_all_accounts_once() -> list[dict[str, Any]]:
    """1회 실행 — 수동 트리거/테스트용."""
    results: list[dict[str, Any]] = []
    accounts = await _fetch_active_accounts()
    logger.info(f"[ssg_status] 대상 SSG 계정 {len(accounts)}개")
    for acc in accounts:
        try:
            r = await _reconcile_one_account(acc)
            results.append(r)
        except Exception as e:
            logger.exception(f"[ssg_status] {acc.get('account_label')} 실패: {e}")
            results.append({"account_label": acc.get("account_label"), "error": str(e)})
    return results


async def status_reconciler_loop() -> None:
    """24시간 주기 백그라운드 루프 — lifecycle 에서 create_task 로 기동."""
    logger.info(
        f"[ssg_status] 시작 — interval=24h, auto_clean={AUTO_CLEAN}, "
        f"first_run_in={INITIAL_DELAY_SECONDS}s"
    )
    await asyncio.sleep(INITIAL_DELAY_SECONDS)
    while not is_shutting_down():
        try:
            await reconcile_all_accounts_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"[ssg_status] cycle 실패: {e}")
        slept = 0
        while slept < RUN_INTERVAL_SECONDS and not is_shutting_down():
            await asyncio.sleep(min(30, RUN_INTERVAL_SECONDS - slept))
            slept += 30

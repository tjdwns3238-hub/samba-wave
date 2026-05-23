"""SambaWave Collector — 갱신/모니터링 엔드포인트."""

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_write_session_dependency
from backend.domain.samba.exchange_rate_service import convert_cost_by_source_site

from backend.api.v1.routers.samba.collector_common import (
    _trim_history,
    _get_services,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collector", tags=["samba-collector"])


# ── DTOs ──


class RefreshRequest(BaseModel):
    product_ids: Optional[List[str]] = None
    search_filter_ids: Optional[List[str]] = None  # 선택된 그룹(검색필터) ID
    auto_retransmit: bool = True


class RateLimitTestRequest(BaseModel):
    goods_no: str = "4746833"  # 테스트용 상품번호
    count: int = 100  # 요청 횟수
    interval: float = 0.0  # 요청 간격 (초)
    mode: str = "autotune"  # autotune(상세+옵션 2개) / collect(상세+옵션+고시정보 3개)


class VideoGenerateRequest(BaseModel):
    product_id: str
    max_images: int = 3
    duration_per_image: float = 1.0


# ══════════════════════════════════════════════════════════════
# 재고/가격 변동 모니터링 — 벌크 갱신 + 스케줄러
# ══════════════════════════════════════════════════════════════


@router.post("/products/refresh")
async def refresh_products(
    body: RefreshRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """벌크 재크롤링 — 소싱처에서 최신 가격/재고 재수집 후 자동 업데이트."""
    from backend.domain.samba.collector.refresher import (
        refresh_products_bulk,
    )
    from backend.domain.samba.collector.repository import (
        SambaCollectedProductRepository,
    )

    repo = SambaCollectedProductRepository(session)

    # 대상 상품 조회 (배치 쿼리)
    if body.product_ids:
        from backend.domain.samba.collector.model import SambaCollectedProduct as _CP

        _stmt = select(_CP).where(_CP.id.in_(body.product_ids))
        _result = await session.execute(_stmt)
        products = list(_result.scalars().all())
    elif body.search_filter_ids:
        # 선택된 그룹의 상품만 조회
        products = []
        for sf_id in body.search_filter_ids:
            group_products = await repo.filter_by_async(
                search_filter_id=sf_id, limit=10000
            )
            products.extend(group_products)
    else:
        # 전체 (최대 500건)
        products = await repo.list_async(skip=0, limit=500, order_by="-updated_at")

    if not products:
        return {
            "total": 0,
            "refreshed": 0,
            "changed": 0,
            "sold_out": 0,
            "retransmitted": 0,
            "needs_extension": [],
            "errors": 0,
        }

    # 롯데ON: benefits API 쿠키 캐시 로드
    _has_lotteon = any(
        (getattr(p, "source_site", "") or "").upper() == "LOTTEON" for p in products
    )
    if _has_lotteon:
        from backend.api.v1.routers.samba.proxy import _get_setting
        from backend.domain.samba.proxy.lotteon_sourcing import (
            set_lotteon_cookie,
        )

        # 상품관리 갱신은 1개 상품 단위로 호출되므로 매번 강제 재로드
        # (오토튠과 동일하게 만료 쿠키가 캐시에 남는 문제 방지)
        _lt_ck = await _get_setting(session, "lotteon_cookie")
        if _lt_ck:
            set_lotteon_cookie(str(_lt_ck))

    # HTTP 갱신 전 커밋 — 설정 읽기 트랜잭션 종료 (idle in transaction 방지)
    try:
        await session.commit()
    except Exception:
        pass

    # 벌크 갱신 실행 (수동 갱신 — 오토튠 로그에 노출되지 않음)
    results, summary = await refresh_products_bulk(products, source="manual")

    # 모니터링 서비스 초기화
    from backend.domain.samba.warroom.service import SambaMonitorService

    monitor = SambaMonitorService(session)

    # 상품 Map (재전송/품절 처리에서 재조회 방지)
    product_map = {p.id: p for p in products}

    # 변동 감지된 상품 DB 업데이트
    now = datetime.now(timezone.utc)
    kst_now = now.astimezone(ZoneInfo("Asia/Seoul"))
    changed_ids: list[str] = []
    soldout_ids: list[str] = []
    stock_only_ids: list[str] = []  # 가격 동일, 재고만 변동
    refresh_details: list[dict] = []  # 개별 상품 갱신 결과
    # 품절 감지 시 즉시 발행한 monitor 이벤트 id — 마켓삭제 완료 후
    # detail.suspended_markets를 사후 update 하기 위한 매핑.
    soldout_event_ids: dict[str, str] = {}

    for r in results:
        if r.error:
            # 에러 카운트 증가 — product_map 활용 (N+1 제거)
            product = product_map.get(r.product_id)
            if product:
                await repo.update_async(
                    r.product_id,
                    refresh_error_count=(product.refresh_error_count or 0) + 1,
                    last_refreshed_at=now,
                )
                # 모니터링: 갱신 에러
                await monitor.emit(
                    "refresh_error",
                    "warning",
                    summary=f"갱신 실패 — {product.name[:30] if product.name else r.product_id}",
                    source_site=getattr(product, "source_site", None),
                    product_id=r.product_id,
                    product_name=getattr(product, "name", None),
                    detail={"error": r.error},
                )
                refresh_details.append(
                    {
                        "time": kst_now.strftime("%H:%M:%S"),
                        "brand": getattr(product, "brand", "") or "",
                        "name": (getattr(product, "name", "") or "")[:40],
                        "status": "error",
                        "detail": r.error[:60],
                        "product_id": r.product_id,
                    }
                )
            continue
        if r.needs_extension:
            # 모니터링: 확장앱 타임아웃
            await monitor.emit(
                "extension_timeout",
                "warning",
                summary=f"KREAM 확장앱 타임아웃 — {r.product_id}",
                source_site="KREAM",
                product_id=r.product_id,
            )
            continue

        # 상품 조회 — product_map 활용 (N+1 제거)
        product = product_map.get(r.product_id)
        if not product:
            continue

        # 갱신 시각 업데이트 + 에러 카운트 리셋
        updates: dict = {
            "last_refreshed_at": now,
            "refresh_error_count": 0,
        }

        # 가격이력 스냅샷 — 변동 여부와 관계없이 항상 기록
        snapshot: dict = {
            "date": now.isoformat(),
            "source": "refresh",
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

        # 이미지/소재/색상 — 기존에 비어있고 편집 이력 없으면 갱신
        _tags = product.tags or []
        _img_edited = "__img_edited__" in _tags or "__img_filtered__" in _tags
        if r.new_images and not product.images and not _img_edited:
            updates["images"] = r.new_images
        if (
            r.new_detail_images
            and not getattr(product, "detail_images", None)
            and not _img_edited
        ):
            updates["detail_images"] = r.new_detail_images
        if r.new_material and not getattr(product, "material", None):
            updates["material"] = r.new_material
        if r.new_color and not getattr(product, "color", None):
            updates["color"] = r.new_color
        # 배송 정보 항상 갱신
        if r.new_free_shipping is not None:
            updates["free_shipping"] = r.new_free_shipping
        if r.new_same_day_delivery is not None:
            updates["same_day_delivery"] = r.new_same_day_delivery
        # 적립금 사용 제한 여부 갱신 (무신사 등)
        if r.new_is_point_restricted is not None:
            updates["is_point_restricted"] = r.new_is_point_restricted

        # 옵션은 가격 변동과 무관하게 항상 갱신
        if r.new_options is not None:
            updates["options"] = r.new_options

        # sale_status는 가격 변동 무관하게 항상 반영
        updates["sale_status"] = r.new_sale_status
        old_status = getattr(product, "sale_status", "in_stock")

        # 원가는 가격 변동 여부와 무관하게 항상 최신값으로 갱신
        if r.new_cost is not None:
            updates["cost"] = r.new_cost
        # 보유 적립금 제외 cost — 무신사만 별도 값 제공, 그 외는 cost 동일값 저장
        if r.new_cost_excl_held_point is not None:
            updates["cost_excl_held_point"] = r.new_cost_excl_held_point
        elif r.new_cost is not None:
            updates["cost_excl_held_point"] = r.new_cost

        if r.changed:
            if r.new_sale_price is not None:
                updates["sale_price"] = r.new_sale_price
            if r.new_original_price is not None:
                updates["original_price"] = r.new_original_price

            # 가격 변동 추적 (워룸 price_changed 이벤트는 오토튠에서 등록마켓 판매가 기준으로
            # 발행하므로 수동갱신에서는 emit 하지 않는다 — 타임라인 기준 통일)
            old_price = product.sale_price or 0
            new_price = r.new_sale_price or 0
            if new_price != old_price:
                updates["price_before_change"] = old_price
                updates["price_changed_at"] = now

            changed_ids.append(r.product_id)

            # 변동 상세: 가격/재고 변동 내용 조합
            _changes: list[str] = []
            if r.new_sale_price is not None and r.new_sale_price != (
                product.sale_price or 0
            ):
                _changes.append(
                    f"가격 ₩{int(product.sale_price or 0):,}→₩{int(r.new_sale_price):,}"
                )
            if r.new_sale_status and r.new_sale_status != old_status:
                _changes.append(f"상태 {old_status}→{r.new_sale_status}")
            if r.new_cost is not None:
                _old_c = int(product.cost) if product.cost else 0
                _new_c = int(r.new_cost)
                if _new_c != _old_c:
                    _changes.append(f"원가 ₩{_old_c:,}→₩{_new_c:,}")
            if r.stock_changed:
                _changes.append("재고변동")

            refresh_details.append(
                {
                    "time": kst_now.strftime("%H:%M:%S"),
                    "brand": getattr(product, "brand", "") or "",
                    "name": (getattr(product, "name", "") or "")[:40],
                    "status": "changed",
                    "detail": " / ".join(_changes) if _changes else "변동",
                    "product_id": r.product_id,
                }
            )

            if r.new_sale_status == "sold_out":
                soldout_ids.append(r.product_id)
                # 모니터링: 품절 감지 (수동갱신 — autotune과 동일 스키마)
                _reason_manual = (
                    "source_deleted"
                    if getattr(r, "deleted_from_source", False)
                    else "all_soldout"
                )
                _emitted_id = await monitor.emit(
                    "sold_out",
                    "warning",
                    summary=f"품절 감지 — {product.name[:30] if product.name else r.product_id}",
                    source_site=product.source_site,
                    product_id=r.product_id,
                    product_name=product.name,
                    detail={
                        "site_product_id": getattr(product, "site_product_id", None),
                        "sale_status": "sold_out",
                        "old_stock": None,
                        "new_stock": 0,
                        "reason": _reason_manual,
                        "suspended_markets": [],
                    },
                )
                if _emitted_id:
                    soldout_event_ids[r.product_id] = _emitted_id
        else:
            if r.stock_changed:
                # 가격/상태 동일, 옵션 재고만 변동 → 옵션 품절 이벤트 발행 (0 경계 전환 기준)
                stock_only_ids.append(r.product_id)

                def _opt_map_manual(opts):
                    """옵션 key(name/size) → stock 맵."""
                    result: dict = {}
                    if not opts:
                        return result
                    for _o in opts:
                        if not isinstance(_o, dict):
                            continue
                        _k = _o.get("name", "") or _o.get("size", "")
                        try:
                            result[_k] = int(_o.get("stock") or 0)
                        except (TypeError, ValueError):
                            result[_k] = 0
                    return result

                def _sum_stock_manual(opts):
                    if not opts:
                        return 0
                    total = 0
                    for _o in opts:
                        if isinstance(_o, dict):
                            try:
                                total += int(_o.get("stock") or 0)
                            except (TypeError, ValueError):
                                pass
                    return total

                _old_stock_m = _sum_stock_manual(product.options)
                _new_stock_m = _sum_stock_manual(r.new_options)
                # 옵션별 0 경계 전환을 방향별로 분리
                _old_map_m = _opt_map_manual(product.options)
                _new_map_m = _opt_map_manual(r.new_options)
                _sold_out_keys_m: list[str] = []
                _restocked_keys_m: list[str] = []
                for _k in set(_old_map_m) | set(_new_map_m):
                    _os = _old_map_m.get(_k, 0)
                    _ns = _new_map_m.get(_k, 0)
                    if (_os <= 0) != (_ns <= 0):
                        if _ns <= 0:
                            _sold_out_keys_m.append(_k)
                        else:
                            _restocked_keys_m.append(_k)
                _sold_out_keys_m = [k or "(이름없음)" for k in _sold_out_keys_m]
                _restocked_keys_m = [k or "(이름없음)" for k in _restocked_keys_m]

                # 옵션 품절 이벤트
                if _sold_out_keys_m:
                    _opts_join_m = ", ".join(_sold_out_keys_m[:5])
                    await monitor.emit(
                        "sold_out",
                        "info",
                        summary=f"옵션품절 — {(product.name or '')[:30]} {_opts_join_m}",
                        source_site=product.source_site,
                        product_id=r.product_id,
                        product_name=product.name,
                        detail={
                            "site_product_id": getattr(
                                product, "site_product_id", None
                            ),
                            "sale_status": r.new_sale_status or "in_stock",
                            "old_stock": _old_stock_m,
                            "new_stock": _new_stock_m,
                            "reason": "option_partial",
                            "sold_out_options": _sold_out_keys_m,
                            "suspended_markets": [],
                        },
                    )

                # 옵션 재입고 이벤트
                if _restocked_keys_m:
                    _opts_join_r = ", ".join(_restocked_keys_m[:5])
                    await monitor.emit(
                        "restock",
                        "info",
                        summary=f"재입고(옵션리스탁) — {(product.name or '')[:30]} {_opts_join_r}",
                        source_site=product.source_site,
                        product_id=r.product_id,
                        product_name=product.name,
                        detail={
                            "site_product_id": getattr(
                                product, "site_product_id", None
                            ),
                            "sale_status": r.new_sale_status or "in_stock",
                            "reason": "option_restock",
                            "restocked_options": _restocked_keys_m,
                            "suspended_markets": [],
                        },
                    )
                refresh_details.append(
                    {
                        "time": kst_now.strftime("%H:%M:%S"),
                        "brand": getattr(product, "brand", "") or "",
                        "name": (getattr(product, "name", "") or "")[:40],
                        "status": "stock_changed",
                        "detail": "재고변동",
                        "product_id": r.product_id,
                    }
                )
            else:
                # 변동 없음
                refresh_details.append(
                    {
                        "time": kst_now.strftime("%H:%M:%S"),
                        "brand": getattr(product, "brand", "") or "",
                        "name": (getattr(product, "name", "") or "")[:40],
                        "status": "unchanged",
                        "detail": "변동 없음",
                        "product_id": r.product_id,
                    }
                )

        await repo.update_async(r.product_id, **updates)

    await session.commit()

    # 자동 재전송 + 품절 삭제
    retransmitted = 0
    deleted_ids: list[str] = []
    if body.auto_retransmit and (changed_ids or soldout_ids or stock_only_ids):
        from backend.domain.samba.shipment.repository import SambaShipmentRepository
        from backend.domain.samba.shipment.service import SambaShipmentService

        ship_repo = SambaShipmentRepository(session)
        ship_svc = SambaShipmentService(ship_repo, session)

        # 가격 변동 상품 → 재전송 (계정별로 묶어서 배치 호출)
        price_changed = [pid for pid in changed_ids if pid not in soldout_ids]
        # 계정별 상품 그룹핑
        retransmit_groups: dict[str, list[str]] = {}
        for pid in price_changed:
            product = product_map.get(pid)
            if product and product.registered_accounts:
                acc_key = ",".join(sorted(product.registered_accounts))
                retransmit_groups.setdefault(acc_key, []).append(pid)
        for acc_key, pids in retransmit_groups.items():
            acc_ids = acc_key.split(",")
            try:
                await ship_svc.start_update(
                    pids, ["price"], acc_ids, skip_unchanged=False
                )
                retransmitted += len(pids)
                _pid_set = set(pids)
                for _d in refresh_details:
                    if _d.get("product_id") in _pid_set:
                        _d["retransmitted"] = True
            except Exception as e:
                logger.error(f"[refresh] 재전송 실패 ({len(pids)}건): {e}")

        # 재고만 변동 상품 → 재전송 (계정별로 묶어서 배치 호출)
        stock_retransmit_groups: dict[str, list[str]] = {}
        for pid in stock_only_ids:
            product = product_map.get(pid)
            if product and product.registered_accounts:
                acc_key = ",".join(sorted(product.registered_accounts))
                stock_retransmit_groups.setdefault(acc_key, []).append(pid)
        for acc_key, pids in stock_retransmit_groups.items():
            acc_ids = acc_key.split(",")
            try:
                await ship_svc.start_update(
                    pids, ["stock"], acc_ids, skip_unchanged=False
                )
                retransmitted += len(pids)
                _pid_set = set(pids)
                for _d in refresh_details:
                    if _d.get("product_id") in _pid_set:
                        _d["retransmitted"] = True
            except Exception as e:
                logger.error(f"[refresh] 재고 재전송 실패 ({len(pids)}건): {e}")

        # 품절 상품 → 마켓 판매중지/삭제 → 삼바 DB 삭제
        import asyncio
        from backend.domain.samba.shipment.dispatcher import delete_from_market

        # 계정 배치 조회 (N+1 방지)
        all_acc_ids = set()
        for pid in soldout_ids:
            product = product_map.get(pid)
            if product and product.registered_accounts:
                all_acc_ids.update(product.registered_accounts)
        acc_map: dict = {}
        if all_acc_ids:
            from backend.domain.samba.account.model import SambaMarketAccount as _MA

            _acc_stmt = select(_MA).where(_MA.id.in_(list(all_acc_ids)))
            _acc_result = await session.execute(_acc_stmt)
            acc_map = {a.id: a for a in _acc_result.scalars().all()}

        # 1단계: 삭제 대상 수집 (lock_delete 필터링, product_map 재사용)
        delete_targets: list[tuple] = []  # (pid, product_dict, account_id, account)
        deletable_pids: set[str] = set()  # DB 삭제 대상 pid
        for pid in soldout_ids:
            product = product_map.get(pid)
            if not product:
                continue
            if getattr(product, "lock_delete", False):
                logger.info(f"[refresh] {pid} 품절이지만 lock_delete=True, 삭제 건너뜀")
                continue
            deletable_pids.add(pid)
            product_dict = product.model_dump()
            if product.registered_accounts:
                for account_id in product.registered_accounts:
                    account = acc_map.get(account_id)
                    if not account:
                        continue
                    m_nos = product.market_product_nos or {}
                    pd = {
                        **product_dict,
                        "market_product_no": {
                            account.market_type: m_nos.get(account_id, "")
                        },
                    }
                    delete_targets.append((pid, pd, account_id, account))

        # 2단계: 마켓 판매중지 병렬 처리 (5개씩) — 성공한 계정 ID 추적
        sem = asyncio.Semaphore(5)
        market_delete_success: dict[str, set[str]] = {}  # pid → 성공한 account_id set

        async def _do_market_delete(
            pid: str, pd: dict, account_id: str, acc: object
        ) -> None:
            async with sem:
                try:
                    result = await delete_from_market(
                        session, acc.market_type, pd, account=acc
                    )  # type: ignore[union-attr]
                    if result.get("success") and not result.get("soldout_fallback"):
                        logger.info(
                            f"[refresh] {pid} → {acc.market_type} 판매중지 완료"
                        )  # type: ignore[union-attr]
                        market_delete_success.setdefault(pid, set()).add(account_id)
                    else:
                        logger.warning(
                            f"[refresh] {pid} → {acc.market_type} 판매중지 실패: {result.get('message')}"
                        )  # type: ignore[union-attr]
                except Exception as e:
                    logger.error(f"[refresh] {pid} → 마켓 삭제 오류: {e}")

        if delete_targets:
            await asyncio.gather(
                *[
                    _do_market_delete(pid, pd, account_id, acc)
                    for pid, pd, account_id, acc in delete_targets
                ]
            )

        # 마켓 판매중지 결과 → 이미 발행된 품절 이벤트 detail에 사후 반영
        # (suspended_markets 라벨이 워룸 타임라인에 표시되도록)
        if market_delete_success and soldout_event_ids:
            from backend.domain.samba.warroom.repository import (
                SambaMonitorEventRepository,
            )

            _ev_repo = SambaMonitorEventRepository(session)
            for _pid, _ok_acc_ids in market_delete_success.items():
                _event_id = soldout_event_ids.get(_pid)
                if not _event_id or not _ok_acc_ids:
                    continue
                _labels: list[str] = []
                for _acc_id in _ok_acc_ids:
                    _acc = acc_map.get(_acc_id)
                    if _acc is None:
                        continue
                    _labels.append(f"{_acc.market_name}({_acc.seller_id or '-'})")
                if _labels:
                    try:
                        await _ev_repo.update_event_detail(
                            _event_id,
                            {"suspended_markets": _labels},
                        )
                    except Exception as _patch_err:
                        logger.warning(
                            f"[refresh] {_pid} suspended_markets 업데이트 실패: {_patch_err}"
                        )

        # 3단계: 품절 상품 상태 업데이트
        # — 등록된 모든 마켓 삭제 성공 시 상품 자체 DB 삭제, 그 외는 sold_out 상태로 보존
        deleted_ids: list[str] = []
        db_deleted_ids: list[str] = []
        if deletable_pids:
            from sqlmodel import col
            from backend.domain.samba.collector.model import SambaCollectedProduct

            _upd_stmt = select(SambaCollectedProduct).where(
                col(SambaCollectedProduct.id).in_(list(deletable_pids))
            )
            _upd_result = await session.execute(_upd_stmt)
            _upd_rows = _upd_result.scalars().all()
            for row in _upd_rows:
                ok_accs = market_delete_success.get(row.id, set())
                orig_reg = list(row.registered_accounts or [])
                new_reg = (
                    [a for a in orig_reg if a not in ok_accs] if ok_accs else orig_reg
                )
                # 등록된 모든 마켓 삭제 성공 → 상품 자체 DB 삭제
                if orig_reg and ok_accs and not new_reg:
                    await session.delete(row)
                    db_deleted_ids.append(row.id)
                    continue
                row.sale_status = "sold_out"  # type: ignore[assignment]
                if ok_accs:
                    new_mnos = {
                        k: v
                        for k, v in (row.market_product_nos or {}).items()
                        if not any(
                            k == d_id or k.startswith(f"{d_id}_") for d_id in ok_accs
                        )
                    }
                    row.registered_accounts = new_reg  # type: ignore[assignment]
                    row.market_product_nos = new_mnos  # type: ignore[assignment]
                    if not new_reg:
                        row.status = "collected"  # type: ignore[assignment]
                session.add(row)
            deleted_ids = [r.id for r in _upd_rows if r.id not in db_deleted_ids]
            logger.info(
                f"[refresh] 품절 상품 {len(deleted_ids)}건 sold_out 상태 업데이트, "
                f"{len(db_deleted_ids)}건 상품 DB 삭제 완료"
            )

        await session.commit()

    # 정책 변동 체크: 소싱처 불변 상품 중 등록 계정 있는 상품 → 정책 기반 판매가 비교
    if body.auto_retransmit:
        all_processed = set(changed_ids) | set(soldout_ids) | set(stock_only_ids)
        policy_check_targets = []
        for r in results:
            if r.error or r.needs_extension:
                continue
            if r.product_id in all_processed:
                continue
            product = product_map.get(r.product_id)
            if (
                product
                and product.registered_accounts
                and getattr(product, "applied_policy_id", None)
            ):
                policy_check_targets.append(product)

        if policy_check_targets:
            from backend.domain.samba.policy.repository import SambaPolicyRepository
            from backend.domain.samba.shipment.service import (
                SambaShipmentService,
                calc_market_price,
                resolve_cost_for_policy,
            )
            from backend.domain.samba.shipment.repository import SambaShipmentRepository
            from backend.domain.samba.account.model import SambaMarketAccount as _PCA

            # 정책 배치 조회 (중복 방지)
            policy_ids_set = {p.applied_policy_id for p in policy_check_targets}
            pol_repo = SambaPolicyRepository(session)
            policies_map: dict = {}
            for _pid in policy_ids_set:
                _pol = await pol_repo.get_async(_pid)
                if _pol:
                    policies_map[_pid] = _pol

            # 계정 배치 조회 (N+1 방지)
            all_policy_acc_ids: set = set()
            for p in policy_check_targets:
                all_policy_acc_ids.update(p.registered_accounts)
            policy_acc_map: dict = {}
            if all_policy_acc_ids:
                _pa_stmt = select(_PCA).where(_PCA.id.in_(list(all_policy_acc_ids)))
                _pa_result = await session.execute(_pa_stmt)
                policy_acc_map = {a.id: a for a in _pa_result.scalars().all()}

            # 정책 기반 판매가 비교 → 변동 상품 계정별 그룹핑
            policy_changed_groups: dict[str, list[str]] = {}
            for p in policy_check_targets:
                _pol = policies_map.get(p.applied_policy_id)
                if not _pol or not _pol.pricing:
                    continue
                _source_site = p.source_site or ""
                # 토글 excludeHeldPoint=True 이면 보유적립금 제외 cost 사용
                _cost = resolve_cost_for_policy(p, _pol.pricing, _source_site) or (
                    p.cost or 0
                )
                _last_sent: dict = p.last_sent_data or {}
                _pm_data = _pol.market_policies or {}

                for _acc_id in p.registered_accounts:
                    _acc = policy_acc_map.get(_acc_id)
                    if not _acc:
                        continue
                    _cost_info = await convert_cost_by_source_site(
                        session, _cost, _source_site, getattr(p, "tenant_id", None)
                    )
                    _new_price = calc_market_price(
                        _cost_info["convertedCost"],
                        _pol.pricing,
                        _acc.market_type,
                        _pm_data,
                        source_site=_source_site,
                        is_point_restricted=getattr(p, "is_point_restricted", None),
                    )
                    _old_sent = _last_sent.get(_acc_id, {})
                    _old_price = (int(_old_sent.get("sale_price") or 0) // 100) * 100
                    if _new_price != _old_price:
                        logger.info(
                            f"[refresh 정책변동] {p.id} {_acc.market_type} "
                            f"₩{_old_price:,} → ₩{_new_price:,}"
                        )
                        _acc_key = ",".join(sorted(p.registered_accounts))
                        policy_changed_groups.setdefault(_acc_key, []).append(p.id)
                        break

            if policy_changed_groups:
                _ship_repo = SambaShipmentRepository(session)
                _ship_svc = SambaShipmentService(_ship_repo, session)
                for _acc_key, _pids in policy_changed_groups.items():
                    _acc_ids = _acc_key.split(",")
                    try:
                        await _ship_svc.start_update(
                            _pids, ["price"], _acc_ids, skip_unchanged=False
                        )
                        retransmitted += len(_pids)
                    except Exception as e:
                        logger.error(
                            f"[refresh] 정책 변동 재전송 실패 ({len(_pids)}건): {e}"
                        )

                # 정책 변동으로 재전송된 상품의 refresh_details 업데이트
                _retx_pids: set = set()
                for _pids in policy_changed_groups.values():
                    _retx_pids.update(_pids)
                for _d in refresh_details:
                    if (
                        _d.get("product_id") in _retx_pids
                        and _d.get("status") == "unchanged"
                    ):
                        _d["status"] = "changed"
                        _d["detail"] = "정책 변동 → 재전송"

                await session.commit()

    summary.retransmitted = retransmitted

    # 갱신 후 상품 0건인 그룹 자동 삭제
    cleaned_filter_ids: list[str] = []
    if body.search_filter_ids:
        from sqlalchemy import func as _func, delete as _sa_del
        from backend.domain.samba.collector.model import (
            SambaCollectedProduct as _CP2,
            SambaSearchFilter as _SF,
        )

        for sf_id in body.search_filter_ids:
            _cnt = (
                await session.execute(
                    select(_func.count()).where(_CP2.search_filter_id == sf_id)
                )
            ).scalar() or 0
            if _cnt == 0:
                # 마켓등록 상품 없는지 확인
                _reg = (
                    await session.execute(
                        select(_func.count())
                        .where(_CP2.search_filter_id == sf_id)
                        .where(_CP2.registered_accounts.isnot(None))
                    )
                ).scalar() or 0
                if _reg == 0:
                    await session.execute(
                        _sa_del(_CP2).where(_CP2.search_filter_id == sf_id)
                    )
                    await session.execute(_sa_del(_SF).where(_SF.id == sf_id))
                    cleaned_filter_ids.append(sf_id)
                    logger.info(
                        f"[refresh] 빈 그룹 자동 삭제: {sf_id} (갱신 후 상품 0건)"
                    )
        if cleaned_filter_ids:
            await session.commit()

    # 모니터링: 재전송/삭제 이벤트 (실패해도 응답에 영향 없도록 try/except 처리)
    try:
        if retransmitted > 0:
            await monitor.emit(
                "market_retransmit",
                "info",
                summary=f"가격변동 재전송 {retransmitted}건",
                detail={"count": retransmitted},
            )
        if body.auto_retransmit and deleted_ids:
            for did in deleted_ids:
                _dp = product_map.get(did)
                if _dp:
                    _brand = _dp.brand or ""
                    _pname = (_dp.name or "")[:40]
                    _pid_str = _dp.site_product_id or ""
                    _label = f"{_brand} {_pname}".strip() if _brand else _pname
                    if _pid_str:
                        _label = f"{_label} ({_pid_str})"
                    _del_summary = f"품절 삭제 — {_label}"
                else:
                    _del_summary = f"품절 삭제 — {did}"
                await monitor.emit(
                    "market_deleted",
                    "info",
                    summary=_del_summary,
                    product_id=did,
                )
    except Exception as _mon_err:
        logger.warning(
            f"[refresh] 모니터 이벤트 emit 실패 (응답에 영향 없음): {_mon_err}"
        )

    # 모니터링: 배치 갱신 완료
    await monitor.emit(
        "refresh_batch",
        "info",
        summary=f"배치 갱신 완료 — {summary.total}건 중 {summary.refreshed}건 갱신, {summary.changed}건 변동",
        detail={
            "total": summary.total,
            "refreshed": summary.refreshed,
            "changed": summary.changed,
            "sold_out": summary.sold_out,
            "deleted": len(deleted_ids) if body.auto_retransmit else 0,
            "retransmitted": retransmitted,
            "errors": summary.errors,
        },
    )
    await session.commit()

    # 내부 추적용 product_id 제거 (외부 노출 방지)
    for _d in refresh_details:
        _d.pop("product_id", None)

    return {
        "total": summary.total,
        "refreshed": summary.refreshed,
        "changed": summary.changed + len(stock_only_ids),
        "sold_out": summary.sold_out,
        "deleted": len(deleted_ids) if body.auto_retransmit else 0,
        "retransmitted": summary.retransmitted,
        "needs_extension": summary.needs_extension,
        "errors": summary.errors,
        "cleaned_filters": len(cleaned_filter_ids),
        "details": refresh_details,
    }


# ══════════════════════════════════════════════════════════════
# 무신사 차단 임계값 테스트
# ══════════════════════════════════════════════════════════════


@router.post("/test/rate-limit")
async def test_rate_limit(body: RateLimitTestRequest = RateLimitTestRequest()):
    """무신사 차단 임계값 테스트."""
    import httpx
    import time

    from backend.api.v1.routers.samba.collector_common import get_musinsa_cookie
    from backend.domain.samba.proxy.musinsa import MusinsaClient

    cookie = await get_musinsa_cookie()
    if not cookie:
        return {"error": "무신사 쿠키 없음"}

    client = MusinsaClient(cookie)
    headers = client._headers()
    base = "https://goods-detail.musinsa.com/api2/goods"

    results = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as http:
        for i in range(body.count):
            start = time.monotonic()
            try:
                urls = [f"{base}/{body.goods_no}", f"{base}/{body.goods_no}/options"]
                if body.mode == "collect":
                    urls.append(f"{base}/{body.goods_no}/essential")

                statuses = []
                for url in urls:
                    r = await http.get(url, headers=headers)
                    statuses.append(r.status_code)
                    if r.status_code in (429, 403):
                        elapsed = round((time.monotonic() - start) * 1000)
                        retry_after = r.headers.get("Retry-After", "?")
                        api_name = url.split("/")[-1] if "/" in url else "detail"
                        results.append(
                            {
                                "req": i + 1,
                                "statuses": statuses,
                                "ms": elapsed,
                                "blocked": api_name,
                            }
                        )
                        return {
                            "blocked_at": {
                                "request_no": i + 1,
                                "status": r.status_code,
                                "api": api_name,
                                "retry_after": retry_after,
                            },
                            "total_ok": i,
                            "mode": body.mode,
                            "api_per_req": len(urls),
                            "total_api_calls": i * len(urls) + len(statuses),
                            "results": results[-10:],
                            "summary": f"{body.mode} 모드: {i + 1}번째에서 {api_name} API {r.status_code} 차단",
                        }

                elapsed = round((time.monotonic() - start) * 1000)
                results.append({"req": i + 1, "statuses": statuses, "ms": elapsed})
            except Exception as e:
                elapsed = round((time.monotonic() - start) * 1000)
                results.append({"req": i + 1, "error": str(e), "ms": elapsed})

            if body.interval > 0:
                await asyncio.sleep(body.interval)

    avg_ms = sum(r.get("ms", 0) for r in results) // len(results) if results else 0
    total_apis = body.count * (3 if body.mode == "collect" else 2)
    return {
        "blocked_at": None,
        "total_ok": len(results),
        "mode": body.mode,
        "api_per_req": 3 if body.mode == "collect" else 2,
        "total_api_calls": total_apis,
        "avg_ms": avg_ms,
        "results": results[-10:],
        "summary": f"{body.mode} 모드: {len(results)}회 성공 (API {total_apis}회, 평균 {avg_ms}ms/상품)",
    }


# ══════════════════════════════════════════════════════════════
# 소싱처/마켓 Probe (구조 변경 감지)
# ══════════════════════════════════════════════════════════════


_probe_status_cache: dict = {"data": None, "at": 0.0}
_PROBE_STATUS_TTL = 30.0


@router.get("/probe/status")
async def probe_status():
    """최근 probe 결과 조회 — 30초 in-memory 캐시로 DB 커넥션 절약."""
    import time

    from backend.domain.samba.forbidden.model import SambaSettings
    from backend.domain.samba.probe.health_checker import MARKET_PROBES, PROBE_TARGETS
    from backend.db.orm import get_read_session

    now = time.monotonic()
    if (
        _probe_status_cache["data"] is not None
        and now - _probe_status_cache["at"] < _PROBE_STATUS_TTL
    ):
        return _probe_status_cache["data"]

    source_keys = [f"probe_{s}" for s in PROBE_TARGETS]
    market_keys = [f"probe_market_{m}" for m in MARKET_PROBES]
    all_keys = source_keys + market_keys

    async with get_read_session() as session:
        rows = (
            await session.execute(
                select(SambaSettings.key, SambaSettings.value).where(
                    SambaSettings.key.in_(all_keys)
                )
            )
        ).all()
    kv = {k: v for k, v in rows if v}

    results: dict = {"sources": {}, "markets": {}}
    for site in PROBE_TARGETS:
        v = kv.get(f"probe_{site}")
        if v:
            results["sources"][site] = v
    for mt in MARKET_PROBES:
        v = kv.get(f"probe_market_{mt}")
        if v:
            results["markets"][mt] = v

    _probe_status_cache["data"] = results
    _probe_status_cache["at"] = now
    return results


@router.post("/probe/run")
async def probe_run(
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """수동 probe 실행 — 전체 소싱처+마켓 헬스체크."""
    from backend.domain.samba.probe.health_checker import run_all_probes

    results = await run_all_probes(session)

    # 모니터링: probe 결과 이벤트 발행
    from backend.domain.samba.warroom.service import SambaMonitorService

    monitor = SambaMonitorService(session)

    for site, data in results.get("sources", {}).items():
        if not data.get("ok"):
            missing = data.get("missing_fields", [])
            if missing:
                await monitor.emit(
                    "api_structure_changed",
                    "critical",
                    summary=f"API 구조 변경 감지 — {site} 필드 누락: {', '.join(missing)}",
                    source_site=site,
                    detail={"missing_fields": missing, "error": data.get("error")},
                )
            elif data.get("error"):
                await monitor.emit(
                    "probe_failed",
                    "warning",
                    summary=f"Probe 실패 — {site}: {data.get('error')}",
                    source_site=site,
                    detail=data,
                )

    for mt, data in results.get("markets", {}).items():
        if not data.get("ok") and data.get("error"):
            await monitor.emit(
                "probe_failed",
                "warning",
                summary=f"마켓 Probe 실패 — {mt}: {data.get('error')}",
                market_type=mt,
                detail=data,
            )

    await session.commit()
    return results


# ══════════════════════════════════════════════════════════════
# Ken Burns 영상 생성
# ══════════════════════════════════════════════════════════════


@router.post("/products/generate-video")
async def generate_product_video(
    body: VideoGenerateRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """상품 이미지로 Ken Burns 효과 영상(2~3초) 생성 → R2/로컬 저장 → 상품에 매칭."""
    from backend.domain.samba.video.kenburns import generate_kenburns_video
    from backend.domain.samba.image.service import ImageTransformService
    import uuid
    from pathlib import Path

    svc = _get_services(session)
    product = await svc.get_collected_product(body.product_id)
    if not product:
        raise HTTPException(404, "상품을 찾을 수 없습니다")

    images = product.images or []
    if not images:
        raise HTTPException(400, "상품 이미지가 없습니다")

    # AI 변환 이미지가 없으면 자동 생성
    ai_images = [u for u in images if "/transformed/" in u or "/ai_" in u]
    if not ai_images:
        logger.info(f"[영상생성] AI이미지 없음 — 자동 생성 시작 ({body.product_id})")
        img_svc_auto = ImageTransformService(session)
        try:
            # 대표이미지는 건드리지 않고 별도 생성
            ai_result = await img_svc_auto.transform_single_image(
                body.product_id,
                images[0],
                "video",
            )
            if ai_result:
                logger.info("[영상생성] AI이미지 자동 생성 완료")
                # 추가이미지 마지막에 추가
                updated_images = list(images)
                updated_images.append(ai_result)
                await svc.update_collected_product(
                    body.product_id, {"images": updated_images}
                )
                images = updated_images
                ai_images = [ai_result]
        except Exception as e:
            logger.warning(f"[영상생성] AI이미지 자동 생성 실패, 원본으로 진행: {e}")

    source_images = ai_images if ai_images else images

    try:
        output_path = generate_kenburns_video(
            image_urls=source_images,
            duration_per_image=body.duration_per_image,
            max_images=body.max_images,
        )
    except Exception as e:
        raise HTTPException(500, f"영상 생성 실패: {str(e)}")

    # R2/로컬 저장
    filename = f"video_{product.site_product_id or uuid.uuid4().hex[:8]}_{uuid.uuid4().hex[:6]}.mp4"
    video_bytes = Path(output_path).read_bytes()

    img_svc = ImageTransformService(session)
    r2 = await img_svc._get_r2_client()
    if r2:
        client, bucket_name, public_url = r2
        try:
            import io

            client.upload_fileobj(
                io.BytesIO(video_bytes),
                bucket_name,
                f"videos/{filename}",
                ExtraArgs={"ContentType": "video/mp4"},
            )
            video_url = f"{public_url}/videos/{filename}"
        except Exception:
            # R2 실패 시 로컬 저장
            local_dir = Path("static/videos")
            local_dir.mkdir(parents=True, exist_ok=True)
            (local_dir / filename).write_bytes(video_bytes)
            video_url = f"/static/videos/{filename}"
    else:
        local_dir = Path("static/videos")
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / filename).write_bytes(video_bytes)
        video_url = f"/static/videos/{filename}"

    # 상품에 video_url 매칭
    await svc.update_collected_product(body.product_id, {"video_url": video_url})

    # 임시파일 삭제
    Path(output_path).unlink(missing_ok=True)

    return {"success": True, "video_url": video_url}

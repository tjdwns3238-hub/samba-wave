"""보강 엔드포인트 — enrich_product, enrich_all_products + 관련 헬퍼 함수."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_write_session_dependency
from backend.domain.samba.collector.refresher import _site_intervals

from backend.api.v1.routers.samba.collector_common import (
    _clean_text,
    _trim_history,
    _build_kream_price_snapshot,
    _get_services,
    get_musinsa_cookie,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["samba-collector"])

# ── 실시간 로그 스트리밍용 ContextVar ──
from contextvars import ContextVar
from collections.abc import Callable as _Callable

_enrich_log_fn: ContextVar[_Callable | None] = ContextVar(
    "_enrich_log_fn", default=None
)


async def _emit_log(msg: str) -> None:
    fn = _enrich_log_fn.get()
    if fn:
        try:
            await fn(msg)
        except Exception:
            pass


# ── enrich 전용 헬퍼 ──


async def _retransmit_if_changed(
    session: AsyncSession,
    product: Any,
    updates: dict,
    old_values: dict | None = None,  # 하위 호환성 유지, 미사용
) -> dict:
    """등록된 마켓에 가격/재고 수정등록 (변동 여부 무관하게 항상 전송)."""
    result = {"retransmitted": False, "retransmit_accounts": 0}

    # 품절 전환 → 마켓 삭제 (registered_accounts가 없어도 market_product_nos로 fallback)
    new_status = updates.get("sale_status")
    if new_status == "sold_out":
        if getattr(product, "lock_delete", False):
            logger.info(
                f"[enrich] {product.id} 품절이지만 lock_delete=True, 마켓 삭제 건너뜀"
            )
            return result

        # registered_accounts 우선, 없으면 market_product_nos 키로 fallback
        # (autotune이 soldout_fallback 후 registered_accounts를 제거해도 재시도 가능)
        reg_accounts = list(getattr(product, "registered_accounts", None) or [])
        if not reg_accounts:
            m_nos_fb = getattr(product, "market_product_nos", None) or {}
            reg_accounts = list(m_nos_fb.keys())
        if not reg_accounts:
            return result

        try:
            from backend.domain.samba.shipment.dispatcher import delete_from_market
            from backend.domain.samba.account.model import SambaMarketAccount
            from backend.domain.samba.collector.repository import (
                SambaCollectedProductRepository,
            )

            # 계정 배치 조회 (N+1 방지)
            _acc_stmt = select(SambaMarketAccount).where(
                SambaMarketAccount.id.in_(reg_accounts)
            )
            _acc_result = await session.execute(_acc_stmt)
            acc_map = {a.id: a for a in _acc_result.scalars().all()}

            # DB 변경사항 플러시 (재전송 시 최신 데이터 조회 보장)
            await session.flush()
            product_dict = {**product.model_dump(), **updates}
            deleted_account_ids: list[str] = []
            await _emit_log(f"품절 전환 → {len(reg_accounts)}개 마켓 삭제 시작")
            for account_id in reg_accounts:
                account = acc_map.get(account_id)
                if not account:
                    continue
                m_nos = product.market_product_nos or {}
                raw_no = m_nos.get(account_id, "")
                if account.market_type == "smartstore":
                    # origin 번호 우선: account_id_origin 키가 있으면 사용
                    origin_no = m_nos.get(f"{account_id}_origin", "")
                    if origin_no:
                        raw_no = origin_no
                    elif isinstance(raw_no, dict):
                        # dict 형태로 저장된 경우 (구버전 호환)
                        raw_no = (
                            raw_no.get("originProductNo")
                            or raw_no.get("smartstoreChannelProductNo")
                            or raw_no.get("groupProductNo")
                            or ""
                        )
                pd = {
                    **product_dict,
                    "market_product_no": {
                        account.market_type: str(raw_no) if raw_no else ""
                    },
                }
                try:
                    del_result = await asyncio.wait_for(
                        delete_from_market(
                            session, account.market_type, pd, account=account
                        ),
                        timeout=30,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[enrich] {product.id} 마켓 삭제 타임아웃 (30s): {account.market_type}"
                    )
                    del_result = {"success": False, "error": "삭제 타임아웃"}
                result["retransmit_accounts"] += 1
                _del_label = f"{account.market_type}({getattr(account, 'account_label', account_id) or account_id})"
                if del_result.get("success") and not del_result.get("soldout_fallback"):
                    await _emit_log(f"[마켓삭제] {_del_label}: 성공")
                    deleted_account_ids.append(account_id)
                elif del_result.get("soldout_fallback"):
                    await _emit_log(f"[마켓삭제] {_del_label}: 판매중지(배지유지)")
                else:
                    await _emit_log(
                        f"[마켓삭제] {_del_label}: 실패 — {str(del_result.get('error', ''))[:50]}"
                    )

            # 삭제 성공 계정을 registered_accounts에서 제거
            if deleted_account_ids:
                m_nos_orig = product.market_product_nos or {}
                new_reg = [a for a in reg_accounts if a not in deleted_account_ids]
                product_repo = SambaCollectedProductRepository(session)
                # 등록된 모든 마켓 삭제 성공 → 상품 자체 DB 삭제
                if reg_accounts and not new_reg:
                    try:
                        await product_repo.delete_async(product.id)
                        await _emit_log("전 마켓 삭제 성공 → 상품 DB 삭제 완료")
                    except Exception as _pd_err:
                        logger.error(
                            f"[enrich] {product.id} 상품 DB 삭제 실패: {_pd_err}"
                        )
                else:
                    remove_keys = set(deleted_account_ids) | {
                        f"{aid}_origin" for aid in deleted_account_ids
                    }
                    new_nos = {
                        k: v for k, v in m_nos_orig.items() if k not in remove_keys
                    }
                    update_data: dict[str, Any] = {
                        "registered_accounts": new_reg if new_reg else None,
                        "market_product_nos": new_nos if new_nos else None,
                    }
                    await product_repo.update_async(product.id, **update_data)

            result["retransmitted"] = True
        except Exception as e:
            logger.error(f"[enrich] {product.id} 마켓 판매중지 실패: {e}")
        return result

    if not getattr(product, "registered_accounts", None):
        return result

    # DB 변경사항 플러시 (재전송 시 최신 데이터 조회 보장)
    await session.flush()

    try:
        from backend.domain.samba.shipment.repository import SambaShipmentRepository
        from backend.domain.samba.shipment.service import SambaShipmentService
        from backend.domain.samba.account.model import SambaMarketAccount as _SMALog

        ship_repo = SambaShipmentRepository(session)
        ship_svc = SambaShipmentService(ship_repo, session)

        # 계정 레이블 일괄 조회 (로그용)
        _reg_ids = list(product.registered_accounts)
        _acc_log_res = await session.execute(
            select(_SMALog).where(_SMALog.id.in_(_reg_ids))
        )
        _acc_log_map = {a.id: a for a in _acc_log_res.scalars().all()}
        _labels = [
            f"{a.market_type}({a.account_label or aid})"
            for aid, a in _acc_log_map.items()
        ]
        await _emit_log(
            f"마켓 수정 전송 — {', '.join(_labels) or str(len(_reg_ids)) + '개 계정'}"
        )

        ship_result = await ship_svc.start_update(
            [product.id],
            ["price", "stock"],
            _reg_ids,
            skip_unchanged=False,
            skip_refresh=True,
        )
        result["retransmitted"] = True
        result["retransmit_accounts"] = len(_reg_ids)

        # 마켓별 결과 로깅
        for pr in ship_result.get("results") or []:
            _t_res = pr.get("transmit_result") or {}
            _t_err = pr.get("transmit_error") or {}
            for aid, status in _t_res.items():
                _acc = _acc_log_map.get(aid)
                _lbl = (
                    f"{_acc.market_type}({_acc.account_label or aid})"
                    if _acc
                    else str(aid)[:20]
                )
                if status == "success":
                    await _emit_log(f"[마켓전송] {_lbl}: 성공")
                elif status == "skipped":
                    await _emit_log(f"[마켓전송] {_lbl}: 스킵")
                else:
                    _emsg = _t_err.get(aid, "실패")
                    await _emit_log(f"[마켓전송] {_lbl}: 실패 — {str(_emsg)[:60]}")
            # 계정별 결과 없는 경우(Exception)
            if not _t_res and pr.get("error"):
                await _emit_log(f"[마켓전송] 오류: {str(pr['error'])[:80]}")
    except Exception as e:
        logger.error(f"[enrich] {product.id} 마켓 재전송 실패: {e}")

    return result


# ── 엔드포인트 ──


@router.post("/enrich/{product_id}")
async def enrich_product(
    product_id: str,
    request: Request,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """수집 상품의 상세 정보를 소싱사이트 API에서 보강 (카테고리, 옵션, 상세이미지 등)."""
    from backend.domain.samba.proxy.musinsa import MusinsaClient

    svc = _get_services(session)
    product = await svc.get_collected_product(product_id)
    if not product:
        raise HTTPException(404, "상품을 찾을 수 없습니다")

    if product.source_site == "MUSINSA" and product.site_product_id:
        cookie = await get_musinsa_cookie(session)

        client = MusinsaClient(cookie=cookie)
        try:
            # refresh_only=True: 가격/재고만 갱신, 이미지/고시정보 처리 스킵
            detail = await client.get_goods_detail(
                product.site_product_id, refresh_only=True
            )
        except Exception as e:
            raise HTTPException(502, f"무신사 상세 조회 실패: {str(e)}")

        if not detail or not detail.get("name"):
            raise HTTPException(502, "무신사 상세 조회 실패: 데이터 없음")
        # 긴 상세이미지 분할 (추가이미지 보충분)
        orig_cnt = detail.get("originalImageCount", len(detail.get("images", [])))
        if orig_cnt < len(detail.get("images", [])):
            from backend.domain.samba.image.service import split_long_images

            detail["images"] = await split_long_images(
                detail["images"], orig_cnt, session
            )

        # get_goods_detail은 { category: "키즈 > ...", category1: "키즈", ... } 형태로 반환

        # 가격 0 허용: None이 아닌 경우에만 업데이트, 0도 유효한 값으로 처리
        api_sale = detail.get("salePrice")
        api_original = detail.get("originalPrice")
        new_sale_price = api_sale if api_sale is not None else product.sale_price
        new_original_price = (
            api_original if api_original is not None else product.original_price
        )

        new_sale_status = detail.get("saleStatus", "in_stock")
        # 최대혜택가: best_benefit_price → cost 컬럼에 저장 (0은 None으로 처리)
        _raw_cost = detail.get("bestBenefitPrice")
        new_cost = _raw_cost if (_raw_cost is not None and _raw_cost > 0) else None
        # 가격/재고만 업데이트 (카테고리, 브랜드, 상세HTML 등은 변경하지 않음)
        updates = {
            "original_price": new_original_price,
            "sale_price": new_sale_price,
            "cost": new_cost,
            "sale_status": new_sale_status,
            "is_point_restricted": detail.get("isPointRestricted"),
        }

        # 가격 변동 추적
        if new_sale_price != product.sale_price:
            updates["price_before_change"] = product.sale_price
            updates["price_changed_at"] = datetime.now(timezone.utc)

        # 가격/옵션 이력 스냅샷 추가 (최신순, 최대 200건)
        snapshot = {
            "date": datetime.now(timezone.utc).isoformat(),
            "sale_price": new_sale_price,
            "original_price": new_original_price,
            "cost": new_cost,
            "options": detail.get("options", []),
        }
        history = list(product.price_history or [])
        history.insert(0, snapshot)
        updates["price_history"] = _trim_history(history)

        # 옵션 보강 (HTML 태그 정제)
        if detail.get("options"):
            cleaned = []
            for opt in detail["options"]:
                if isinstance(opt, dict):
                    co = {**opt}
                    for k in ("name", "value", "label"):
                        if k in co and isinstance(co[k], str):
                            co[k] = _clean_text(co[k])
                    cleaned.append(co)
                else:
                    cleaned.append(opt)
            updates["options"] = cleaned

        # 이미지 보강
        if detail.get("images"):
            updates["images"] = detail["images"]

        _stock_label = "품절" if new_sale_status == "sold_out" else "재고있음"
        await _emit_log(
            f"소싱 갱신 완료 — 원가 {new_cost:,}원, 판매가 {new_sale_price:,}원, {_stock_label}"
            if new_cost
            else f"소싱 갱신 완료 — 판매가 {new_sale_price:,}원, {_stock_label}"
        )
        updated = await svc.update_collected_product(product_id, updates)
        retransmit = await _retransmit_if_changed(session, product, updates)
        return {
            "success": True,
            "enriched_fields": list(updates.keys()),
            "product": updated,
            **retransmit,
        }

    if product.source_site == "KREAM" and product.site_product_id:
        from backend.domain.samba.proxy.kream import KreamClient

        client = KreamClient()
        try:
            raw = await client.get_product_via_extension(product.site_product_id)
        except Exception as e:
            raise HTTPException(502, f"KREAM 상세 조회 실패: {str(e)}")

        if isinstance(raw, dict) and raw.get("success") and raw.get("product"):
            pd = raw["product"]
        elif isinstance(raw, dict) and raw.get("name"):
            pd = raw
        else:
            raise HTTPException(502, "KREAM 상세 조회 실패: 데이터 없음")

        opts = pd.get("options", [])

        fast_prices = [
            o.get("kreamFastPrice", 0) for o in opts if o.get("kreamFastPrice", 0) > 0
        ]
        general_prices = [
            o.get("kreamGeneralPrice", 0)
            for o in opts
            if o.get("kreamGeneralPrice", 0) > 0
        ]
        sale_p = (
            min(fast_prices)
            if fast_prices
            else (pd.get("salePrice") or product.sale_price)
        )
        cost_p = min(general_prices) if general_prices else sale_p

        # 가격재고업데이트: 가격/재고(옵션)만 갱신, 상품명/브랜드/이미지/카테고리 스킵
        updates = {
            "original_price": pd.get("originalPrice") or product.original_price,
            "sale_price": sale_p,
            "cost": cost_p,
            "options": opts if opts else product.options,
        }

        # 품절 판정: 모든 옵션 stock=0이면 sold_out
        _kream_opts = opts if opts else []
        if _kream_opts and all(o.get("stock", 0) <= 0 for o in _kream_opts):
            updates["sale_status"] = "sold_out"
        elif not _kream_opts:
            updates["sale_status"] = "sold_out"
        else:
            updates["sale_status"] = "in_stock"

        # 가격이력 스냅샷 추가 (최대 200건)
        snapshot = _build_kream_price_snapshot(
            sale_p, pd.get("originalPrice") or product.original_price, cost_p, opts
        )
        history = list(product.price_history or [])
        history.insert(0, snapshot)
        updates["price_history"] = _trim_history(history)

        updated = await svc.update_collected_product(product_id, updates)
        retransmit = await _retransmit_if_changed(session, product, updates)
        return {
            "success": True,
            "enriched_fields": list(updates.keys()),
            "product": updated,
            **retransmit,
        }

    if product.source_site == "Nike" and product.site_product_id:
        from backend.domain.samba.proxy.nike import NikeClient

        try:
            detail = await NikeClient().get_detail(product.site_product_id)
        except httpx.HTTPStatusError as e:
            # PDP 404 = Nike Korea 단종 컬러 — [[plugins/sourcing/nike.py refresh]] 동일 매핑
            if e.response.status_code == 404:
                updates = {"sale_status": "sold_out"}
                updated = await svc.update_collected_product(product_id, updates)
                retransmit = await _retransmit_if_changed(session, product, updates)
                return {
                    "success": True,
                    "enriched_fields": list(updates.keys()),
                    "product": updated,
                    **retransmit,
                }
            raise HTTPException(502, f"Nike 상세 조회 실패: {e}")
        except Exception as e:
            raise HTTPException(502, f"Nike 상세 조회 실패: {e}")
        if detail.get("error"):
            raise HTTPException(502, detail["error"])

        updates = {}
        for field in (
            "style_code",
            "sex",
            "manufacturer",
            "origin",
            "material",
            "care_instructions",
            "quality_guarantee",
            "color",
            "video_url",
            "detail_html",
            "images",
            "options",
        ):
            val = detail.get(field)
            if val is not None and val != "" and val != []:
                updates[field] = val

        sale_price = detail.get("sale_price")
        original_price = detail.get("original_price")
        if sale_price is not None:
            updates["sale_price"] = sale_price
        if original_price is not None:
            updates["original_price"] = original_price

        # sale_status 반영
        updates["sale_status"] = detail.get("sale_status", "in_stock")

        snapshot = {
            "date": datetime.now(timezone.utc).isoformat(),
            "sale_price": sale_price or product.sale_price,
            "original_price": original_price or product.original_price,
            "options": detail.get("options", []),
        }
        history = list(product.price_history or [])
        history.insert(0, snapshot)
        updates["price_history"] = _trim_history(history)

        updated = await svc.update_collected_product(product_id, updates)
        retransmit = await _retransmit_if_changed(session, product, updates)
        return {
            "success": True,
            "enriched_fields": list(updates.keys()),
            "product": updated,
            **retransmit,
        }

    if product.source_site == "FashionPlus" and product.site_product_id:
        from backend.domain.samba.proxy.fashionplus import FashionPlusClient

        client = FashionPlusClient()
        try:
            detail = await client.get_detail(product.site_product_id)
        except Exception as e:
            raise HTTPException(502, f"패션플러스 상세 조회 실패: {str(e)}")

        new_sale = detail.get("sale_price") or product.sale_price
        new_orig = detail.get("original_price") or product.original_price
        shipping_fee = detail.get("shipping_fee", 0) or 0
        new_cost = new_sale + shipping_fee

        new_options = detail.get("options") or []
        updates: dict[str, Any] = {
            "sale_price": new_sale,
            "original_price": new_orig,
            "cost": new_cost,
            "sourcing_shipping_fee": shipping_fee,
        }
        # 품절 판정
        if new_options and all(o.get("stock", 0) <= 0 for o in new_options):
            updates["sale_status"] = "sold_out"
        elif not new_options:
            updates["sale_status"] = "sold_out"
        else:
            updates["sale_status"] = detail.get("saleStatus", "in_stock")
        if new_options:
            updates["options"] = new_options

        snapshot = {
            "date": datetime.now(timezone.utc).isoformat(),
            "sale_price": new_sale,
            "original_price": new_orig,
            "cost": new_cost,
            "options": detail.get("options", []),
        }
        history = list(product.price_history or [])
        history.insert(0, snapshot)
        updates["price_history"] = _trim_history(history)

        updated = await svc.update_collected_product(product_id, updates)
        retransmit = await _retransmit_if_changed(session, product, updates)
        return {
            "success": True,
            "enriched_fields": list(updates.keys()),
            "product": updated,
            **retransmit,
        }

    # 플러그인 기반 소싱처 (FashionPlus, Nike, Adidas 등)
    from backend.domain.samba.plugins import SOURCING_PLUGINS

    _src = product.source_site or ""
    plugin = SOURCING_PLUGINS.get(_src) or SOURCING_PLUGINS.get(_src.upper())
    if plugin and product.site_product_id:
        # 수동 enrich 컨텍스트 마킹 — SSG 등 plugin.refresh 내부에서
        # owner_device_id="" 분기 트리거 (어떤 PC 확장앱이든 처리 가능).
        # ContextVar 기본값은 "autotune"이므로 명시 set 필수.
        from backend.domain.samba.collector.refresher import (
            _current_refresh_source,
        )

        _ctx_token = _current_refresh_source.set("manual")
        try:
            # 롯데ON: benefits API 쿠키 캐시 로드
            if _src.upper() == "LOTTEON":
                from backend.api.v1.routers.samba.proxy import _get_setting
                from backend.domain.samba.proxy.lotteon_sourcing import (
                    set_lotteon_cookie,
                    _lotteon_cookie_cache,
                )

                if not _lotteon_cookie_cache:
                    _lt_ck = await _get_setting(session, "lotteon_cookie")
                    if _lt_ck:
                        set_lotteon_cookie(str(_lt_ck))
                    # HTTP refresh 전 커밋 — idle in transaction 방지
                    try:
                        await session.commit()
                    except Exception:
                        pass

            # ABCmart/GrandStage: 확장앱이 sync한 로그인 쿠키 강제 재로드
            # refresher.py:1366-1377(상품관리/오토튠 경로)와 동일 패턴 — 진입점 어디서든
            # alwaysDscntAmt 정확값 수신을 보장. 누락 시 익명 폴백으로 멤버십 할인 미반영.
            if _src in ("ABCmart", "GrandStage"):
                from backend.domain.samba.proxy.abcmart import (
                    ARTSourcingClient,
                    prepare_abcmart_cache,
                )

                ARTSourcingClient._bulk_cache = {}
                await prepare_abcmart_cache()

            result = await plugin.refresh(product)
            updates: dict[str, Any] = {}
            if result.new_sale_price is not None:
                updates["sale_price"] = result.new_sale_price
            if result.new_original_price is not None:
                updates["original_price"] = result.new_original_price
            if result.new_cost is not None:
                updates["cost"] = result.new_cost
            # 보유 적립금 제외 cost (무신사 토글용) — 무신사 외 소싱처는 cost 동일값
            if result.new_cost_excl_held_point is not None:
                updates["cost_excl_held_point"] = result.new_cost_excl_held_point
            elif result.new_cost is not None:
                updates["cost_excl_held_point"] = result.new_cost
            if result.new_sale_status:
                updates["sale_status"] = result.new_sale_status
            if result.new_options is not None:
                updates["options"] = result.new_options
            # 수집 시점 빈 문자열로 저장된 name/brand 백필.
            # 사용자 수동 편집 보존을 위해 현재 값이 비어있을 때만 적용.
            if result.new_name and not (product.name or "").strip():
                updates["name"] = result.new_name
            if result.new_brand and not (product.brand or "").strip():
                updates["brand"] = result.new_brand
                if not (product.manufacturer or "").strip():
                    updates["manufacturer"] = result.new_brand
            if result.error:
                return {"success": False, "message": result.error}

            # ABCmart/GrandStage: IP-bound 세션이라 백엔드 cookie sync 무력
            # 확장앱 service worker가 사용자 IP에서 fetch → alwaysDscntAmt 정확값 수신
            if _src in ("ABCmart", "GrandStage") and product.site_product_id:
                try:
                    from backend.domain.samba.proxy.sourcing_queue import SourcingQueue

                    # 수동 enrich 는 트리거 PC 의 deviceId 로 owner 박아 해당 PC 에서만 탭이 열리게 함.
                    # 헤더 누락 시 빈값 → SourcingQueue 글로벌 폴백.
                    _enrich_owner = (
                        request.headers.get("X-Device-Id", "").strip() or None
                    )
                    _req_id, _future = SourcingQueue.add_detail_job(
                        _src,
                        product.site_product_id,
                        owner_device_id=_enrich_owner,
                    )
                    _ext_result = await asyncio.wait_for(_future, timeout=25)
                    if isinstance(_ext_result, dict) and _ext_result.get("success"):
                        _ext_benefit = int(
                            _ext_result.get("best_benefit_price", 0) or 0
                        )
                        if _ext_benefit > 0:
                            updates["cost"] = _ext_benefit
                            logger.info(
                                f"[{_src}] enrich 확장앱 혜택가: "
                                f"{product.site_product_id} → {_ext_benefit:,}"
                            )
                except asyncio.TimeoutError:
                    logger.info(
                        f"[{_src}] enrich 확장앱 타임아웃: {product.site_product_id}"
                    )
                except Exception as _ext_err:
                    logger.debug(
                        f"[{_src}] enrich 확장앱 실패: {product.site_product_id} — {_ext_err}"
                    )

            # LOTTEON: 확장앱 DOM 파싱으로 최대혜택가 수집
            if _src.upper() == "LOTTEON" and product.site_product_id:
                try:
                    from backend.domain.samba.proxy.sourcing_queue import SourcingQueue

                    _sitm = (
                        getattr(product, "sitmNo", "")
                        or getattr(product, "sitm_no", "")
                        or (product.extra_data or {}).get("sitmNo", "")
                    )
                    # 수동 enrich 는 트리거 PC 의 deviceId 로 owner 박아 해당 PC 에서만 탭이 열리게 함.
                    _enrich_owner_lt = (
                        request.headers.get("X-Device-Id", "").strip() or None
                    )
                    _req_id, _future = SourcingQueue.add_detail_job(
                        "LOTTEON",
                        product.site_product_id,
                        sitm_no=_sitm,
                        owner_device_id=_enrich_owner_lt,
                    )
                    _ext_result = await asyncio.wait_for(_future, timeout=25)
                    if isinstance(_ext_result, dict) and _ext_result.get("success"):
                        _ext_benefit = int(
                            _ext_result.get("best_benefit_price", 0) or 0
                        )
                        if _ext_benefit > 0:
                            updates["cost"] = _ext_benefit
                            logger.info(
                                f"[LOTTEON] enrich 확장앱 혜택가: "
                                f"{product.site_product_id} → {_ext_benefit:,}"
                            )
                except asyncio.TimeoutError:
                    logger.info(
                        f"[LOTTEON] enrich 확장앱 타임아웃: {product.site_product_id}"
                    )
                except Exception as _ext_err:
                    logger.debug(
                        f"[LOTTEON] enrich 확장앱 실패: {product.site_product_id} — {_ext_err}"
                    )

            if not updates:
                return {"success": True, "message": "변동 없음", "product": product}
            # 가격이력 스냅샷
            snapshot = {
                "date": datetime.now(timezone.utc).isoformat(),
                "sale_price": updates.get("sale_price", product.sale_price),
                "original_price": updates.get("original_price", product.original_price),
                "cost": updates.get("cost", product.cost),
            }
            # 옵션: 신규 수집 우선, 없으면 기존 DB 옵션 폴백
            _snap_opts = result.new_options
            if not _snap_opts and product.options:
                _snap_opts = product.options
            if _snap_opts:
                snapshot["options"] = _snap_opts
            history = list(product.price_history or [])
            history.insert(0, snapshot)
            updates["price_history"] = _trim_history(history)
            updated = await svc.update_collected_product(product_id, updates)
            retransmit = await _retransmit_if_changed(session, product, updates)
            return {
                "success": True,
                "enriched_fields": list(updates.keys()),
                "product": updated,
                **retransmit,
            }
        except Exception as e:
            raise HTTPException(502, f"{product.source_site} 갱신 실패: {e}")
        finally:
            _current_refresh_source.reset(_ctx_token)

    raise HTTPException(
        400, f"'{product.source_site}' 상세 보강은 아직 지원하지 않습니다"
    )


@router.post("/enrich-all")
async def enrich_all_products(
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """카테고리가 비어있는 모든 MUSINSA 수집 상품의 상세 정보를 일괄 보강."""
    from backend.domain.samba.proxy.musinsa import MusinsaClient

    svc = _get_services(session)
    all_products = await svc.list_collected_products(skip=0, limit=1000)

    # 카테고리 없는 MUSINSA 상품만
    targets = [
        p
        for p in all_products
        if p.source_site == "MUSINSA" and p.site_product_id and not p.category1
    ]

    if not targets:
        return {"enriched": 0, "message": "보강할 상품이 없습니다"}

    # 쿠키 로드
    cookie = await get_musinsa_cookie(session)

    client = MusinsaClient(cookie=cookie)
    enriched = 0

    for product in targets:
        try:
            detail = await client.get_goods_detail(product.site_product_id)
            if not detail or not detail.get("name"):
                continue
            # 긴 상세이미지 분할 (추가이미지 보충분)
            orig_cnt = detail.get("originalImageCount", len(detail.get("images", [])))
            if orig_cnt < len(detail.get("images", [])):
                from backend.domain.samba.image.service import split_long_images

                detail["images"] = await split_long_images(
                    detail["images"], orig_cnt, session
                )

            new_sale_status = detail.get("saleStatus", "in_stock")
            api_sale = detail.get("salePrice")
            api_original = detail.get("originalPrice")
            new_sale_price = api_sale if api_sale is not None else product.sale_price
            new_original_price = (
                api_original if api_original is not None else product.original_price
            )
            _raw_cost = detail.get("bestBenefitPrice")
            new_cost = _raw_cost if (_raw_cost is not None and _raw_cost > 0) else None

            updates = {
                "category": detail.get("category") or product.category,
                "category1": detail.get("category1") or product.category1,
                "category2": detail.get("category2") or product.category2,
                "category3": detail.get("category3") or product.category3,
                "category4": detail.get("category4") or product.category4,
                "brand": detail.get("brand") or product.brand,
                "original_price": new_original_price,
                "sale_price": new_sale_price,
                "cost": new_cost,
                "sale_status": new_sale_status,
                "is_point_restricted": detail.get("isPointRestricted"),
            }

            # 가격 변동 추적
            if new_sale_price != product.sale_price:
                from datetime import datetime, timezone as tz

                updates["price_before_change"] = product.sale_price
                updates["price_changed_at"] = datetime.now(tz.utc)

            # 가격/옵션 이력 스냅샷 추가 (최신순, 최대 200건)
            from datetime import datetime, timezone as tz

            snapshot = {
                "date": datetime.now(tz.utc).isoformat(),
                "sale_price": new_sale_price,
                "original_price": new_original_price,
                "cost": new_cost,
                "options": detail.get("options", []),
            }
            history = list(product.price_history or [])
            history.insert(0, snapshot)
            updates["price_history"] = _trim_history(history)

            if detail.get("options"):
                updates["options"] = detail["options"]
            if detail.get("images"):
                updates["images"] = detail["images"]

            await svc.update_collected_product(product.id, updates)
            enriched += 1

            # 적응형 인터벌: 차단 감지 시 자동 증가
            await asyncio.sleep(_site_intervals.get("MUSINSA", 1.0))
        except Exception:
            continue

    return {"enriched": enriched, "total_targets": len(targets)}

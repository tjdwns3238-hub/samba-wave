"""SambaWave Shipment service — 실제 마켓 API 연동 상품 전송."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from typing import Any, Optional

from sqlmodel.ext.asyncio.session import AsyncSession

from backend.domain.samba.exchange_rate_service import convert_cost_by_source_site
from backend.domain.samba.shipment.model import SambaShipment
from backend.domain.samba.shipment.repository import SambaShipmentRepository
from backend.utils.logger import logger

import math

# 마켓타입(영문 코드) → 정책키(한글 표시명) 매핑
# 마켓 계정의 market_type 필드 값을 정책 설정의 per_market 키로 변환할 때 사용
MARKET_TYPE_TO_POLICY_KEY: dict[str, str] = {
    "coupang": "쿠팡",
    "ssg": "신세계몰(전시)",
    "smartstore": "스마트스토어",
    "11st": "11번가",
    "gmarket": "지마켓",
    "auction": "옥션",
    "gsshop": "GS샵",
    "lotteon": "롯데ON",
    "lottehome": "롯데홈쇼핑",
    "homeand": "홈앤쇼핑",
    "hmall": "HMALL",
    "kream": "KREAM",
    "playauto": "플레이오토",
}


def _resolve_margin_rate(cost: float, pricing: dict) -> float:
    """원가 기반 범위 마진율 반환. useRangeMargin이면 해당 구간 rate 사용."""
    if pricing.get("useRangeMargin") and pricing.get("rangeMargins"):
        for r in pricing["rangeMargins"]:
            max_val = r.get("max") or 9999999999
            if cost >= r.get("min", 0) and cost < max_val:
                return r.get("rate", 15)
    return pricing.get("marginRate", 15)


def _get_source_site_margin(pricing: dict, source_site: str) -> dict:
    margins = pricing.get("sourceSiteMargins", {}) or {}
    if not source_site:
        return {}
    if source_site in margins:
        return margins[source_site] or {}

    aliases = {
        "GSShop": ("GSSHOP",),
        "GSSHOP": ("GSShop",),
    }
    for alias in aliases.get(source_site, ()):
        if alias in margins:
            return margins[alias] or {}
    return {}


def calc_market_price(
    cost: float,
    policy_pricing: dict,
    market_type: str,
    market_policies: dict | None = None,
    source_site: str = "",
    is_point_restricted: Optional[bool] = None,
) -> int:
    """정책 기반 마켓 최종 판매가 계산.

    원가 + 마진 + 배송비 → 소싱처 추가 마진 → 수수료 역산 → 추가요금.
    마켓별 오버라이드 적용. 범위 마진 지원. 소싱처별 추가 마진 지원.
    pointOnly=true 옵션이면 적립금 사용 가능 상품(is_point_restricted=False)에만 추가 마진 적용.
    """
    if not policy_pricing:
        return int(cost)
    pr = policy_pricing
    common_margin_rate = _resolve_margin_rate(cost, pr)
    common_shipping = pr.get("shippingCost", 0)
    common_extra = pr.get("extraCharge", 0)
    common_fee = pr.get("feeRate", 0)
    min_margin = pr.get("minMarginAmount", 0)

    policy_key = MARKET_TYPE_TO_POLICY_KEY.get(market_type, "")
    mp = (market_policies or {}).get(policy_key, {}) if policy_key else {}
    m_margin_rate = mp.get("marginRate") or common_margin_rate
    m_shipping = mp.get("shippingCost") or common_shipping
    m_fee = mp.get("feeRate") or common_fee

    margin_amt = round(cost * m_margin_rate / 100)
    if min_margin > 0 and margin_amt < min_margin:
        margin_amt = min_margin
    calc_price = cost + margin_amt + m_shipping

    # 소싱처별 추가 마진 (수수료 역산 전 적용 — 수수료에도 자동 반영됨)
    if source_site:
        _ssm = _get_source_site_margin(pr, source_site)
        _ss_rate = _ssm.get("marginRate", 0)
        _ss_amount = _ssm.get("marginAmount", 0)
        # pointOnly=true: 적립금 사용 가능 상품(is_point_restricted=False)에만 적용
        _point_only = bool(_ssm.get("pointOnly"))
        _apply_ssm = (not _point_only) or (is_point_restricted is False)
        if _apply_ssm:
            if _ss_rate != 0:
                calc_price += round(cost * _ss_rate / 100)
            if _ss_amount != 0:
                calc_price += _ss_amount

    if m_fee > 0 and calc_price > 0:
        calc_price = math.ceil(calc_price / (1 - m_fee / 100))
    if common_extra > 0:
        calc_price += common_extra
    # 100원 단위 내림 (111 → 100)
    return (int(calc_price) // 100) * 100


# 그룹상품 동시성 제어 락 (account_id별)
_group_locks: dict[str, asyncio.Lock] = {}

# 상품별 전송 락 — 동일 상품+동일 계정 조합 중복 전송 방지
_transmitting_products: set[tuple] = set()

# 계정별 세마포어 — API Rate Limit 방지 (계정당 동시 1건)
_account_semaphores: dict[str, asyncio.Semaphore] = {}

# 전송 중단 플래그 — job_id별 분리 (멀티유저 격리)
import threading as _threading

_cancel_events: dict[str, _threading.Event] = {}
_cancel_lock = _threading.Lock()


def request_cancel_transmit(job_id: str | None = None):
    """전송 취소 요청.

    job_id가 주어지면 해당 잡만, None이면 모든 잡을 취소한다.
    """
    with _cancel_lock:
        if job_id is None:
            # 전체 취소 — 기존 이벤트 모두 set + 글로벌 마커
            for evt in _cancel_events.values():
                evt.set()
            _cancel_events.setdefault("__all__", _threading.Event()).set()
        else:
            evt = _cancel_events.setdefault(job_id, _threading.Event())
            evt.set()


def clear_cancel_transmit(job_id: str | None = None):
    """취소 플래그 해제.

    job_id가 주어지면 해당 잡만, None이면 모든 이벤트를 제거한다.
    """
    with _cancel_lock:
        if job_id is None:
            _cancel_events.clear()
        else:
            _cancel_events.pop(job_id, None)
            # __all__ 은 제거하지 않음 — 다른 잡이 아직 감지하지 못했을 수 있음
            # __all__ 해제는 clear_cancel_transmit(None)으로만 가능


def is_cancel_requested(job_id: str | None = None) -> bool:
    """취소가 요청되었는지 확인.

    job_id가 주어지면 해당 잡 또는 글로벌 취소를 확인,
    None이면 아무 이벤트라도 set이면 True.
    """
    with _cancel_lock:
        if job_id is None:
            return any(evt.is_set() for evt in _cancel_events.values())
        # 해당 job_id 이벤트 또는 글로벌(__all__) 이벤트 확인
        evt = _cancel_events.get(job_id)
        if evt and evt.is_set():
            return True
        global_evt = _cancel_events.get("__all__")
        return bool(global_evt and global_evt.is_set())


def _get_group_lock(account_id: str) -> asyncio.Lock:
    if account_id not in _group_locks:
        _group_locks[account_id] = asyncio.Lock()
    return _group_locks[account_id]


def clear_account_semaphores():
    """별도 스레드 실행 시 이전 이벤트 루프 세마포어 정리."""
    _account_semaphores.clear()


def _get_account_semaphore(account_id: str) -> asyncio.Semaphore:
    if account_id not in _account_semaphores:
        _account_semaphores[account_id] = asyncio.Semaphore(1)
    return _account_semaphores[account_id]


STATUS_LABELS: dict[str, str] = {
    "pending": "대기중",
    "updating": "업데이트중",
    "transmitting": "전송중",
    "completed": "완료",
    "partial": "부분완료",
    "failed": "실패",
}


class SambaShipmentService:
    def __init__(self, repo: SambaShipmentRepository, session: AsyncSession):
        self.repo = repo
        self.session = session

    @staticmethod
    def _extract_market_product_no(result: dict[str, Any] | None) -> str:
        """Scan nested success payloads and recover a market product number."""
        if not isinstance(result, dict):
            return ""

        candidate_keys = (
            "product_no",
            "spdNo",
            "epdNo",
            "originProductNo",
            "smartstoreChannelProductNo",
            "productNo",
            "sellerProductId",
            "itemId",
            "supPrdCd",
            "prdNo",
            "goodsNo",
            "product_id",
            "productId",
        )
        queue: list[Any] = [result]
        seen: set[int] = set()

        while queue:
            current = queue.pop(0)
            obj_id = id(current)
            if obj_id in seen:
                continue
            seen.add(obj_id)

            if isinstance(current, dict):
                for key in candidate_keys:
                    value = current.get(key)
                    if value not in (None, ""):
                        return str(value)
                for value in current.values():
                    if isinstance(value, (dict, list)):
                        queue.append(value)
            elif isinstance(current, list):
                queue.extend(
                    value for value in current if isinstance(value, (dict, list))
                )

        return ""

    @staticmethod
    def _apply_option_name_rules(options: list, name_rule: Any) -> list:
        """옵션명 치환 규칙 적용.

        name_rule.option_rules: [{"from": "원본", "to": "대체"}] 순서대로 치환.
        options 항목이 dict이면 'name'/'option_name' 키, str이면 값 자체를 치환.
        """
        rules: list[dict] = getattr(name_rule, "option_rules", []) or []
        if not rules:
            return options

        def _replace(text: str) -> str:
            for rule in rules:
                src, dst = rule.get("from", ""), rule.get("to", "")
                if src:
                    text = text.replace(src, dst)
            return text

        result = []
        for opt in options:
            if isinstance(opt, dict):
                opt = dict(opt)
                for key in ("name", "option_name"):
                    if key in opt and isinstance(opt[key], str):
                        opt[key] = _replace(opt[key])
                result.append(opt)
            elif isinstance(opt, str):
                result.append(_replace(opt))
            else:
                result.append(opt)
        return result

    async def _apply_name_rule_effects(
        self,
        product_row: Any,
        product_dict: dict,
        policy: Any,
    ) -> None:
        """정책의 명칭 규칙을 상품 옵션에 선적용하고 _name_rule 을 캐시."""
        if not policy:
            return
        name_rule_id = (getattr(policy, "extras", None) or {}).get("name_rule_id")
        if not name_rule_id:
            return
        from sqlmodel import select

        from backend.domain.samba.policy.model import SambaNameRule

        result = await self.session.exec(
            select(SambaNameRule).where(SambaNameRule.id == name_rule_id)
        )
        name_rule = result.first()
        if not name_rule:
            return
        if product_dict.get("options"):
            product_dict["options"] = self._apply_option_name_rules(
                product_dict["options"], name_rule
            )
        product_dict["_name_rule"] = name_rule

    # ==================== CRUD ====================

    async def list_shipments(
        self, skip: int = 0, limit: int = 50, status: Optional[str] = None
    ) -> list[SambaShipment]:
        if status:
            return await self.repo.list_by_status(status)
        return await self.repo.list_async(
            skip=skip, limit=limit, order_by="-created_at"
        )

    async def list_by_status(self, status: str) -> list[SambaShipment]:
        return await self.repo.list_by_status(status)

    async def get_shipment(self, shipment_id: str) -> Optional[SambaShipment]:
        return await self.repo.get_async(shipment_id)

    async def create_shipment(self, data: dict[str, Any]) -> SambaShipment:
        return await self.repo.create_async(**data)

    async def update_shipment(
        self, shipment_id: str, data: dict[str, Any]
    ) -> Optional[SambaShipment]:
        return await self.repo.update_async(shipment_id, **data)

    async def delete_shipment(self, shipment_id: str) -> bool:
        return await self.repo.delete_async(shipment_id)

    async def list_by_product(self, product_id: str) -> list[SambaShipment]:
        return await self.repo.list_by_product(product_id)

    # ==================== 실제 상품 전송 ====================

    async def start_update(
        self,
        product_ids: list[str],
        update_items: list[str],
        target_account_ids: list[str],
        skip_unchanged: bool = False,
        skip_refresh: bool = False,
        skip_policy_account_filter: bool = False,
    ) -> dict[str, Any]:
        """여러 상품을 대상 마켓 계정으로 실제 전송. 마켓별 결과 반환."""

        # 이전 취소 플래그 잔존 방지는 워커가 잡 단위로 처리 (clear_cancel_transmit(job.id))
        # 여기서 인자 없이 호출하면 일시정지 글로벌 마커(__all__)까지 지워져서
        # 일시정지 누른 직후 다음 PENDING 잡이 즉시 클레임됨 — 절대 추가 금지

        processed = 0
        skipped = 0
        cancelled = 0
        results: list[dict[str, Any]] = []
        for product_id in product_ids:
            if False:
                logger.info("[마켓삭제] 클라이언트 연결 종료 감지 - 추가 삭제 중단")
            # 중단 체크
            if is_cancel_requested():
                cancelled = len(product_ids) - processed
                logger.info(
                    f"[전송] 강제 중단 — {processed}건 완료, {cancelled}건 취소"
                )
                # 일시정지 글로벌 마커(__all__) 유지 — 워커가 잡 단위로 해제
                break
            try:
                shipment = await self._transmit_product(
                    product_id,
                    target_account_ids,
                    update_items,
                    skip_unchanged=skip_unchanged,
                    skip_refresh=skip_refresh,
                    skip_policy_account_filter=skip_policy_account_filter,
                )
                results.append(
                    {
                        "product_id": product_id,
                        "status": shipment.status,
                        "transmit_result": shipment.transmit_result or {},
                        "transmit_error": shipment.transmit_error or {},
                        "update_result": shipment.update_result or {},
                        "error": shipment.error,
                    }
                )
                processed += 1
            except Exception as exc:
                logger.error(f"상품 {product_id} 전송 실패: {exc}")
                results.append(
                    {
                        "product_id": product_id,
                        "status": "failed",
                        "error": str(exc),
                    }
                )

        return {
            "processed": processed,
            "skipped": skipped,
            "cancelled": cancelled,
            "results": results,
        }

    # ==================== 그룹상품 전송 ====================

    async def transmit_group(self, product_ids: list[str], account_id: str) -> dict:
        """그룹상품을 스마트스토어에 등록."""

        from backend.domain.samba.account.repository import SambaMarketAccountRepository
        from backend.domain.samba.collector.repository import (
            SambaCollectedProductRepository,
        )
        from backend.domain.samba.policy.repository import SambaPolicyRepository
        from backend.domain.samba.proxy.smartstore import SmartStoreClient

        product_repo = SambaCollectedProductRepository(self.session)
        account_repo = SambaMarketAccountRepository(self.session)

        # 상품 조회
        products = []
        for pid in product_ids:
            p = await product_repo.get_async(pid)
            if p:
                products.append(p)
        if len(products) < 2:
            raise ValueError("그룹상품은 2개 이상의 상품이 필요합니다")

        # 계정 조회
        account = await account_repo.get_async(account_id)
        if not account:
            raise ValueError(f"계정을 찾을 수 없습니다: {account_id}")

        additional = account.additional_fields or {}
        client_id = additional.get("clientId") or account.api_key
        client_secret = additional.get("clientSecret") or account.api_secret
        client = SmartStoreClient(client_id, client_secret)

        # 카테고리 매핑 조회 — product.category(전체 경로) 우선
        first = products[0]
        raw_category = first.category or ""
        if not raw_category:
            cat_parts = [
                first.category1,
                first.category2,
                first.category3,
                first.category4,
            ]
            raw_category = " > ".join(c for c in cat_parts if c)

        mapped = await self._resolve_category_mappings(
            first.source_site or "",
            raw_category,
            [account_id],
        )
        category_id = mapped.get("smartstore", "")
        if not category_id:
            raise ValueError("카테고리 매핑을 찾을 수 없습니다")

        # 정책 조회 (가격 계산용)
        MARKET_TYPE_TO_POLICY_KEY = {
            "coupang": "쿠팡",
            "ssg": "신세계몰(전시)",
            "smartstore": "스마트스토어",
            "11st": "11번가",
            "gmarket": "지마켓",
            "auction": "옥션",
            "gsshop": "GS샵",
            "lotteon": "롯데ON",
            "lottehome": "롯데홈쇼핑",
            "homeand": "홈앤쇼핑",
            "hmall": "HMALL",
            "kream": "KREAM",
            "playauto": "플레이오토",
        }
        policy = None
        policy_market_data: dict[str, Any] = {}
        if first.applied_policy_id:
            pol_repo = SambaPolicyRepository(self.session)
            policy = await pol_repo.get_async(first.applied_policy_id)
            if policy and policy.market_policies:
                policy_market_data = policy.market_policies

        # account_id별 동시성 락
        lock = _get_group_lock(account_id)
        async with lock:
            # guideId 조회
            guides = await client.get_purchase_option_guides(category_id)
            if not guides:
                # 카테고리 미지원 → 단일상품 폴백
                logger.info(
                    f"카테고리 {category_id} 그룹상품 미지원, 단일상품으로 전송"
                )
                for p in products:
                    await self._transmit_product(
                        p.id, [account_id], ["price", "stock", "image", "description"]
                    )
                return {
                    "group_product_no": None,
                    "product_count": len(products),
                    "deleted_count": 0,
                    "fallback": True,
                }
            guide_id = guides[0].get("guideId")

            # 기존 단일상품 삭제
            deleted_nos = []
            for p in products:
                market_nos = p.market_product_nos or {}
                existing_no = market_nos.get(account_id)
                origin_no = market_nos.get(f"{account_id}_origin")
                delete_no = origin_no or existing_no
                if delete_no:
                    try:
                        if isinstance(delete_no, dict):
                            delete_no = delete_no.get("originProductNo", delete_no)
                        await client.delete_product(str(delete_no))
                        deleted_nos.append(delete_no)
                    except Exception as exc:
                        logger.warning(
                            f"[전송] 그룹전송 기존 단일상품 삭제 실패 (no={delete_no}): {exc}"
                        )

            # 상품 데이터 준비 (가격 계산, 이미지 업로드)
            product_dicts = []
            for p in products:
                # OOM 방지: 전송에 불필요한 대용량 필드 제외
                pd = p.model_dump(exclude={"last_sent_data", "extra_data"})

                # 상세 HTML 재생성
                pd["detail_html"] = await self._build_detail_html(pd)

                # 정책 기반 판매가 계산 (기존 _transmit_product 라인 313-341 동일 패턴)
                if policy and policy.pricing:
                    cost = (
                        pd.get("cost")
                        or pd.get("sale_price")
                        or pd.get("original_price")
                        or 0
                    )
                    cost_info = await convert_cost_by_source_site(
                        self.session, cost, p.source_site or "", p.tenant_id
                    )
                    effective_cost = cost_info["convertedCost"]
                    calc_price = calc_market_price(
                        effective_cost,
                        policy.pricing,
                        "smartstore",
                        policy_market_data,
                        source_site=p.source_site or "",
                        is_point_restricted=getattr(p, "is_point_restricted", None),
                    )

                    # 가격 이상치 방어: 원가가 정상가의 5% 미만이면 전송 차단
                    _orig_price = pd.get("original_price") or pd.get("sale_price") or 0
                    if _orig_price > 0 and cost > 0 and cost < _orig_price * 0.05:
                        logger.error(
                            f"[가격방어] 그룹전송 차단 — 원가 이상치: "
                            f"원가={int(cost):,}, 정상가={int(_orig_price):,}, "
                            f"계산가={calc_price:,}"
                        )
                        continue

                    pd["_final_sale_price"] = calc_price
                    logger.info(
                        f"[그룹전송] 가격 계산: 원가={cost} → 판매가={calc_price}"
                    )

                # 이미지 업로드
                uploaded_images = []
                for img_url in (pd.get("images") or [])[:5]:
                    try:
                        naver_url = await client.upload_image_from_url(img_url)
                        uploaded_images.append(naver_url)
                    except Exception as exc:
                        logger.warning(
                            f"[전송] 그룹전송 이미지 업로드 실패, 원본 URL 사용: {exc}"
                        )
                        uploaded_images.append(img_url)
                pd["images"] = uploaded_images
                product_dicts.append(pd)

            # 페이로드 변환
            payload = SmartStoreClient.transform_group_product(
                products=product_dicts,
                category_id=category_id,
                guide_id=guide_id,
                account_settings=additional,
            )

            # 그룹상품 등록
            await client.register_group_product(payload)

            # 폴링
            try:
                poll_result = await client.poll_group_status(max_wait=120)
            except Exception as e:
                # 그룹 등록 실패 → 삭제된 상품 롤백 (단일상품 재등록)
                logger.error(f"그룹등록 실패, 단일상품으로 롤백: {e}")
                for p in products:
                    try:
                        await self._transmit_product(
                            p.id,
                            [account_id],
                            ["price", "stock", "image", "description"],
                        )
                    except Exception as rollback_exc:
                        logger.warning(
                            f"[전송] 그룹등록 실패 후 단일상품 롤백 실패 (pid={p.id}): {rollback_exc}"
                        )
                raise e

            # 결과 저장
            group_product_no = poll_result.get("groupProductNo")
            product_nos = poll_result.get("productNos", [])

            for i, p in enumerate(products):
                updates: dict[str, Any] = {"group_product_no": group_product_no}
                if i < len(product_nos):
                    pno = product_nos[i]
                    market_nos = dict(p.market_product_nos or {})
                    market_nos[account_id] = {
                        "originProductNo": pno.get("originProductNo"),
                        "smartstoreChannelProductNo": pno.get(
                            "smartstoreChannelProductNo"
                        ),
                        "groupProductNo": group_product_no,
                    }
                    updates["market_product_nos"] = market_nos
                    registered = list(p.registered_accounts or [])
                    if account_id not in registered:
                        registered.append(account_id)
                    updates["registered_accounts"] = registered
                    updates["status"] = "registered"
                await product_repo.update_async(p.id, **updates)

            return {
                "group_product_no": group_product_no,
                "product_count": len(products),
                "deleted_count": len(deleted_nos),
            }

    async def _transmit_product(
        self,
        product_id: str,
        target_account_ids: list[str],
        update_items: list[str],
        skip_unchanged: bool = False,
        skip_refresh: bool = False,
        skip_policy_account_filter: bool = False,
    ) -> SambaShipment:
        """단일 상품에 대한 실제 마켓 전송."""

        # 상품 전송 락 — 동일 상품 + 동일 계정 조합 중복 전송 방지
        # (마켓이 다르면 같은 상품이라도 동시 전송 허용)
        _lock_key = (product_id, frozenset(target_account_ids))
        if _lock_key in _transmitting_products:
            shipment = await self.repo.create_async(
                product_id=product_id,
                target_account_ids=target_account_ids,
                update_items=update_items,
                status="failed",
                update_result={},
                transmit_result={},
                transmit_error={"_all": "이미 전송 중인 상품입니다."},
            )
            return shipment
        _transmitting_products.add(_lock_key)

        try:
            return await asyncio.wait_for(
                self._transmit_product_inner(
                    product_id,
                    target_account_ids,
                    update_items,
                    skip_unchanged,
                    skip_refresh,
                    skip_policy_account_filter,
                ),
                timeout=180,  # 상품 1건당 최대 180초 (최신화+이미지업로드 포함)
            )
        except asyncio.TimeoutError:
            logger.warning(f"[전송] 상품 {product_id} 전송 180초 타임아웃 — 스킵")
            shipment = await self.repo.create_async(
                product_id=product_id,
                target_account_ids=target_account_ids,
                update_items=update_items,
                status="failed",
                update_result={},
                transmit_result={},
                transmit_error={"_all": "전송 180초 타임아웃"},
            )
            return shipment
        finally:
            _transmitting_products.discard(_lock_key)

    async def _transmit_product_inner(
        self,
        product_id: str,
        target_account_ids: list[str],
        update_items: list[str],
        skip_unchanged: bool = False,
        skip_refresh: bool = False,
        skip_policy_account_filter: bool = False,
    ) -> SambaShipment:
        """상품 전송 실제 구현 (락 획득 후 호출)."""

        def _mem_mb():
            try:
                with open("/proc/self/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            return int(line.split()[1]) // 1024
            except Exception as exc:
                logger.debug(f"[전송] 메모리 측정 실패 (비Linux 환경): {exc}")
                return -1

        logger.info(f"[메모리] 전송시작: {_mem_mb()}MB")

        from backend.domain.samba.account.model import SambaMarketAccount
        from backend.domain.samba.account.repository import SambaMarketAccountRepository
        from backend.domain.samba.collector.repository import (
            SambaCollectedProductRepository,
        )
        from backend.domain.samba.shipment.dispatcher import dispatch_to_market

        # 강제 중단 체크
        if is_cancel_requested():
            raise Exception("전송 강제 중단됨")

        # 1. shipment 레코드 생성
        shipment = await self.repo.create_async(
            product_id=product_id,
            target_account_ids=target_account_ids,
            update_items=update_items,
            status="pending",
            update_result={},
            transmit_result={},
            transmit_error={},
        )

        # 2. 상품 데이터 조회
        product_repo = SambaCollectedProductRepository(self.session)
        product_row = await product_repo.get_async(product_id)
        if not product_row:
            await self.repo.update_async(
                shipment.id, status="failed", error="상품을 찾을 수 없습니다."
            )
            return shipment

        # 롯데온/SSG의 '나이키' 브랜드 상품만 — AI 이미지 변환된 상품만 전송 허용
        # (다이나핏 등 다른 브랜드는 영향 없음)
        _AI_REQUIRED_SOURCE_SITES = {"LOTTEON", "SSG"}
        _AI_REQUIRED_BRANDS = {"NIKE", "나이키", "JORDAN", "조던"}
        _src_norm = (product_row.source_site or "").upper()
        _brand_norm = (product_row.brand or "").strip().upper().replace(" ", "")
        if (
            _src_norm in _AI_REQUIRED_SOURCE_SITES
            and _brand_norm in _AI_REQUIRED_BRANDS
        ):
            if "__ai_image__" not in (product_row.tags or []):
                _msg = (
                    f"{product_row.source_site} 나이키 상품은 "
                    f"AI 이미지 변환 후에만 등록 가능합니다."
                )
                logger.info(
                    f"[전송] AI 변환 미완료 차단: {product_id} "
                    f"({product_row.source_site}/{product_row.brand})"
                )
                await self.repo.update_async(
                    shipment.id,
                    status="failed",
                    transmit_error={"_all": _msg},
                )
                return shipment

        # OOM 방지: 전송에 불필요한 대용량 필드 제외
        product_dict = product_row.model_dump(exclude={"last_sent_data", "extra_data"})

        # 수동등록 상품의 계정별 카테고리 (extra_data.manual_market_categories: {account_id: category_id})
        # extra_data는 product_dict에서 제외되므로 product_row에서 직접 읽음
        _raw_manual_cats: dict = (product_row.extra_data or {}).get(
            "manual_market_categories"
        ) or {}
        _manual_market_categories: dict[str, str] = {
            str(k): str(v) for k, v in _raw_manual_cats.items()
        }

        # 업데이트 항목이 체크되어 있으면 소싱처 최신화 먼저 실행
        # skip_refresh=True면 오토튠에서 이미 최신화 완료 → 건너뜀
        # 품절 상품만 최신화 — 재고 있는 상품은 불필요한 소싱처 API 호출 차단
        has_update = bool(update_items) and len(update_items) > 0
        _opts_for_sold = product_dict.get("options") or []
        _is_sold_out = (product_row.sale_status == "sold_out") or (
            bool(_opts_for_sold)
            and all(
                (o.get("stock") or 0) <= 0
                for o in _opts_for_sold
                if isinstance(o, dict)
            )
        )
        refresh_status = ""  # 프론트 로그용
        pending_refresh_updates: dict[str, Any] = {}  # 최종 업데이트에 통합
        if (
            has_update
            and not skip_refresh
            and _is_sold_out
            and product_row.source_site
            and product_row.site_product_id
        ):
            try:
                from backend.domain.samba.collector.refresher import refresh_product

                refresh_result = await asyncio.wait_for(
                    refresh_product(product_row, source="transmit"),
                    timeout=60,  # 갱신이 전송 전체를 막지 않도록 60초 제한
                )
                if refresh_result.error:
                    refresh_status = f"최신화실패:{refresh_result.error[:30]}"
                    logger.warning(f"[전송] 소싱처 최신화 실패: {refresh_result.error}")
                else:
                    # DB 반영
                    refresh_updates: dict[str, Any] = {
                        "last_refreshed_at": datetime.now(UTC),
                    }
                    if refresh_result.new_sale_price is not None:
                        refresh_updates["sale_price"] = refresh_result.new_sale_price
                    if refresh_result.new_original_price is not None:
                        refresh_updates["original_price"] = (
                            refresh_result.new_original_price
                        )
                    if refresh_result.new_cost is not None:
                        refresh_updates["cost"] = refresh_result.new_cost
                    if refresh_result.new_options is not None:
                        refresh_updates["options"] = refresh_result.new_options
                    if refresh_result.new_sale_status:
                        refresh_updates["sale_status"] = refresh_result.new_sale_status
                        # is_sold_out 제거 → sale_status로 통일
                    # 이미지 갱신: update_items에 "image"가 명시적으로 체크된 경우만
                    _update_image = update_items and "image" in update_items
                    if refresh_result.new_images and _update_image:
                        refresh_updates["images"] = refresh_result.new_images
                    if refresh_result.new_detail_images and _update_image:
                        refresh_updates["detail_images"] = (
                            refresh_result.new_detail_images
                        )
                    # 가격/재고 이력 스냅샷 기록
                    snapshot: dict[str, Any] = {
                        "date": datetime.now(UTC).isoformat(),
                        "source": "transmit_refresh",
                        "sale_price": refresh_result.new_sale_price
                        if refresh_result.new_sale_price is not None
                        else product_row.sale_price,
                        "original_price": refresh_result.new_original_price
                        if refresh_result.new_original_price is not None
                        else product_row.original_price,
                        "cost": refresh_result.new_cost
                        if refresh_result.new_cost is not None
                        else product_row.cost,
                        "sale_status": refresh_result.new_sale_status or "in_stock",
                        "changed": refresh_result.changed,
                    }
                    # 옵션이 없어도 현재 옵션 스냅샷 기록
                    snap_opts = refresh_result.new_options or (
                        product_row.options if product_row.options else None
                    )
                    if snap_opts:
                        snapshot["options"] = snap_opts
                    history = list(product_row.price_history or [])
                    history.insert(0, snapshot)
                    # 최초 수집 1개 + 최근 4개 = 최대 5개
                    if len(history) <= 5:
                        refresh_updates["price_history"] = history
                    else:
                        refresh_updates["price_history"] = history[:4] + [history[-1]]
                    # 최종 업데이트에서 통합 저장
                    pending_refresh_updates = refresh_updates
                    for k, v in refresh_updates.items():
                        product_dict[k] = v
                    # 가격/재고 변동 각각 판단
                    old_cost = getattr(product_row, "cost", None)
                    new_cost = refresh_result.new_cost
                    cost_changed = new_cost is not None and new_cost != old_cost
                    old_opts = getattr(product_row, "options", None) or []
                    new_opts = refresh_result.new_options
                    stock_changed = False
                    stock_change_count = 0
                    if new_opts is not None:
                        old_stocks = {
                            o.get("name", ""): o.get("stock", 0) for o in old_opts
                        }
                        new_stocks = {
                            o.get("name", ""): o.get("stock", 0) for o in new_opts
                        }
                        stock_changes = [
                            k
                            for k in set(
                                list(old_stocks.keys()) + list(new_stocks.keys())
                            )
                            if old_stocks.get(k) != new_stocks.get(k)
                        ]
                        stock_changed = len(stock_changes) > 0
                        stock_change_count = len(stock_changes)
                    cur_cost_val = (
                        int(new_cost)
                        if new_cost is not None
                        else (int(old_cost) if old_cost else 0)
                    )
                    old_cost_int = int(old_cost) if old_cost else 0
                    new_cost_int = (
                        int(new_cost) if new_cost is not None else old_cost_int
                    )
                    refresh_status = f"원가 {old_cost_int:,}>{new_cost_int:,}, 재고변동 {stock_change_count}건"
                    logger.info(f"[전송] 소싱처 최신화 완료 — {refresh_status}")
            except asyncio.TimeoutError:
                refresh_status = "최신화실패:60초 타임아웃"
                logger.warning("[전송] 소싱처 최신화 타임아웃 (60초) — 갱신 건너뜀")
            except Exception as ref_e:
                refresh_status = f"최신화예외:{str(ref_e)[:30]}"
                logger.warning(f"[전송] 소싱처 최신화 예외: {ref_e}")
        # 최신화를 안 했어도 현재 원가 표시
        if not refresh_status:
            _cur_cost = int(product_row.cost or product_row.sale_price or 0)
            _opt_count = len(product_row.options or [])
            refresh_status = f"원가 {_cur_cost:,}, 옵션 {_opt_count}건"

        # 조기 스킵: 이미 등록된 상품 + 가격재고 업데이트 모드 + 변동 없음 → 나머지 로직 전부 건너뜀
        _is_registered = product_row.status == "registered" and bool(
            product_row.registered_accounts
        )
        if skip_unchanged and has_update and _is_registered:
            # 소싱처 최신화에서 변동이 없었으면 즉시 스킵
            if not pending_refresh_updates or refresh_status.startswith("최신화실패"):
                pass  # 최신화 안 했거나 실패 → 스킵 판정 불가, 계속 진행
            else:
                _old_cost = product_row.cost or 0
                _new_cost = pending_refresh_updates.get("cost", _old_cost)
                _old_opts = product_row.options or []
                _new_opts = pending_refresh_updates.get("options", _old_opts)
                if _new_cost == _old_cost and _new_opts == _old_opts:
                    logger.info(
                        f"[전송] 조기 스킵 — 소싱처 변동 없음 (원가 {int(_old_cost):,})"
                    )
                    shipment = SambaShipment(
                        product_id=product_id,
                        status="completed",
                        result={"refresh": refresh_status} if refresh_status else None,
                    )
                    self.session.add(shipment)
                    await self.session.flush()
                    return shipment

        # 이미지/상세페이지 전송 판단
        is_price_stock_only = bool(update_items) and set(update_items) <= {
            "price",
            "stock",
        }
        needs_image = not is_price_stock_only

        # price/stock만 업데이트 시 이미지 다운로드/업로드 완전 스킵
        if is_price_stock_only:
            product_dict["_skip_image_upload"] = True

        # 상세 HTML은 항상 정책 기반으로 재생성 (원문 상세이미지 유출 방지)
        if not is_price_stock_only:
            product_dict["detail_html"] = await self._build_detail_html(product_dict)

        # 3. 카테고리 매핑 자동 조회 — product.category(전체 경로) 우선
        #    category1~4 개별 필드는 일부 소싱처에서 불완전할 수 있으므로
        #    전체 경로 문자열을 1순위로 사용
        policy = None
        policy_market_data: dict[str, Any] = {}
        if product_row.applied_policy_id:
            from backend.domain.samba.policy.repository import SambaPolicyRepository

            policy_repo = SambaPolicyRepository(self.session)
            policy = await policy_repo.get_async(product_row.applied_policy_id)
            if policy and policy.market_policies:
                policy_market_data = policy.market_policies
            await self._apply_name_rule_effects(product_row, product_dict, policy)
            if not is_price_stock_only:
                product_dict["detail_html"] = await self._build_detail_html(
                    product_dict
                )

        raw_category = product_row.category or ""
        if not raw_category:
            cat_parts = [
                product_row.category1,
                product_row.category2,
                product_row.category3,
                product_row.category4,
            ]
            raw_category = " > ".join(c for c in cat_parts if c)

        # 성별 prefix는 의류 카테고리일 때만 추가 (신발/가방 등은 제외)
        sex_prefix = ""
        cat1 = (product_row.category1 or "").strip()
        clothing_categories = {
            "상의",
            "하의",
            "아우터",
            "원피스",
            "니트",
            "셔츠",
            "팬츠",
            "의류",
        }
        if cat1 in clothing_categories:
            kream = (
                product_row.kream_data if hasattr(product_row, "kream_data") else None
            )
            if isinstance(kream, dict):
                sex_list = kream.get("sex", [])
                if isinstance(sex_list, list) and sex_list:
                    sex = sex_list[0]
                    if "남" in sex:
                        sex_prefix = "남성의류"
                    elif "여" in sex:
                        sex_prefix = "여성의류"

        source_category = (
            f"{sex_prefix} > {raw_category}"
            if sex_prefix and raw_category
            else raw_category
        )

        # 검색필터명 조회 (플레이오토 임의분류용)
        if product_row.search_filter_id:
            from backend.domain.samba.collector.repository import (
                SambaSearchFilterRepository,
            )

            sf_repo = SambaSearchFilterRepository(self.session)
            sf = await sf_repo.get_async(product_row.search_filter_id)
            logger.info(
                f"[전송] 검색필터 조회: search_filter_id={product_row.search_filter_id}, "
                f"found={sf is not None}, name={getattr(sf, 'name', None)}"
            )
            if sf and sf.name:
                product_dict["_search_filter_name"] = sf.name
        else:
            logger.warning(
                f"[전송] 상품 {product_row.id} search_filter_id 없음 → 임의분류 불가"
            )

        mapped_categories = await self._resolve_category_mappings(
            product_row.source_site or "",
            source_category,
            target_account_ids,
        )
        # 성별 prefix 포함 시 매핑 못 찾으면 prefix 없이 재시도
        if sex_prefix and not mapped_categories:
            mapped_categories = await self._resolve_category_mappings(
                product_row.source_site or "",
                raw_category,
                target_account_ids,
            )
        await self.repo.update_async(shipment.id, mapped_categories=mapped_categories)

        # 4. 업데이트 단계
        await self.repo.update_async(shipment.id, status="updating")
        update_result: dict[str, str] = {}
        for item in update_items:
            update_result[item] = "success"
        await self.repo.update_async(
            shipment.id, status="transmitting", update_result=update_result
        )

        # 5. 계정 정보 조회 및 마켓별 전송
        account_repo = SambaMarketAccountRepository(self.session)

        # 정책 기반 계정 필터링: 정책이 있으면 참조하되, 사용자 선택 계정은 보존
        MARKET_TYPE_TO_POLICY_KEY = {
            "coupang": "쿠팡",
            "ssg": "신세계몰(전시)",
            "smartstore": "스마트스토어",
            "11st": "11번가",
            "gmarket": "지마켓",
            "auction": "옥션",
            "gsshop": "GS샵",
            "lotteon": "롯데ON",
            "lottehome": "롯데홈쇼핑",
            "homeand": "홈앤쇼핑",
            "hmall": "HMALL",
            "kream": "KREAM",
            "ebay": "eBay",
            "lazada": "Lazada",
            "qoo10": "Qoo10",
            "shopee": "Shopee",
            "shopify": "Shopify",
            "zoom": "Zum(줌)",
            "toss": "토스",
            "rakuten": "라쿠텐",
            "amazon": "아마존",
            "buyma": "바이마",
            "playauto": "플레이오토",
        }
        if not product_row.applied_policy_id:
            logger.warning(f"[전송] 상품 {product_id} 정책 미설정 — 전송 차단")
            await self.repo.update_async(
                shipment.id,
                status="failed",
                error="정책 미적용 상품은 전송할 수 없습니다.",
            )
            return await self.repo.get_async(shipment.id) or shipment

        from backend.domain.samba.policy.repository import SambaPolicyRepository

        policy_repo = SambaPolicyRepository(self.session)
        policy = await policy_repo.get_async(product_row.applied_policy_id)
        if policy and policy.market_policies:
            policy_market_data = policy.market_policies

        # 글로벌 삭제어 조회 (compose 전에 미리 로드)
        from backend.domain.samba.forbidden.repository import (
            SambaForbiddenWordRepository,
        )

        fw_repo = SambaForbiddenWordRepository(self.session)
        forbidden_words = await fw_repo.list_active("deletion")
        deletion_words = [fw.word for fw in (forbidden_words or []) if fw.word]

        # 정책의 상품명 규칙(name_rule) 기반 상품명 조합 적용
        if policy and policy.extras:
            name_rule_id = (policy.extras or {}).get("name_rule_id")
            if name_rule_id:
                from backend.domain.samba.policy.model import SambaNameRule
                from sqlmodel import select

                stmt = select(SambaNameRule).where(SambaNameRule.id == name_rule_id)
                result = await self.session.exec(stmt)
                name_rule = result.first()
                if name_rule:
                    product_dict["name"] = self._compose_product_name(
                        product_dict, name_rule, deletion_words=deletion_words
                    )
                    # 마켓별 상품명 조합이 있으면 _dispatch_one에서 덮어쓸 수 있도록 name_rule 보관
                    product_dict["_name_rule"] = name_rule
                    product_dict["_original_name"] = product_row.name or ""
                    product_dict["_deletion_words"] = deletion_words

        # 정책이 있으면 계정 필터링, 없으면 사용자 선택 전체 유지
        # skip_policy_account_filter=True(테트리스 매칭 ON)이면 건너뜀 —
        # 테트리스 블럭이 계정을 결정하므로 정책 accountIds 필터 불필요
        if policy_market_data and not skip_policy_account_filter:
            # 배치 조회 (N+1 → 1회)
            from sqlmodel import select as _sel
            from backend.domain.samba.account.model import SambaMarketAccount

            _stmt = _sel(SambaMarketAccount).where(
                SambaMarketAccount.id.in_(target_account_ids)
            )
            _res = await self.session.execute(_stmt)
            _account_map = {a.id: a for a in _res.scalars().all()}

            filtered_ids = []
            for aid in target_account_ids:
                acc = _account_map.get(aid)
                if not acc:
                    continue
                policy_key = MARKET_TYPE_TO_POLICY_KEY.get(acc.market_type)
                if not policy_key:
                    # 정책 키 매핑 안 되는 마켓 → 그대로 허용
                    filtered_ids.append(aid)
                    continue
                mp = policy_market_data.get(policy_key, {})
                if not mp:
                    # 정책에 이 마켓이 없음 → 그대로 허용 (사용자가 직접 선택)
                    filtered_ids.append(aid)
                    continue
                policy_acc_ids = mp.get("accountIds", [])
                if not policy_acc_ids and mp.get("accountId"):
                    policy_acc_ids = [mp["accountId"]]
                # 정책에 계정 목록이 있으면 해당 계정만, 없으면 모두 허용
                if policy_acc_ids and aid not in policy_acc_ids:
                    continue
                filtered_ids.append(aid)
            target_account_ids = filtered_ids
            if not target_account_ids:
                logger.warning(
                    f"[전송] 상품 {product_id} — 정책 accountIds 필터링으로 전송 계정 없음 "
                    f"(정책ID: {product_row.applied_policy_id}). "
                    f"테트리스 매칭 ON 상태에서 발생 시 skip_policy_account_filter 미전달 의심"
                )
                await self.repo.update_async(
                    shipment.id,
                    status="failed",
                    error="정책에 해당 계정이 없어 전송 불가 (정책 > 스마트스토어 계정 설정 확인)",
                )
                return await self.repo.get_async(shipment.id) or shipment
            logger.info(f"[전송] 정책 필터링 후 계정: {len(target_account_ids)}개")

        transmit_result: dict[str, str] = {}
        transmit_error: dict[str, str] = {}
        update_mode_accounts: set[str] = (
            set()
        )  # PATCH 모드였던 계정 (실패해도 등록정보 보존)

        # 전송 대상 계정 배치 조회 (N+1 → 1회)
        from sqlmodel import select as _sel2
        from backend.domain.samba.account.model import SambaMarketAccount as _SMA

        _stmt2 = _sel2(_SMA).where(_SMA.id.in_(target_account_ids))
        _res2 = await self.session.execute(_stmt2)
        _dispatch_account_map = {a.id: a for a in _res2.scalars().all()}

        # 배치 읽기 완료 — soldout refresh(최대 30초) 전 커밋으로 idle in transaction 방지
        try:
            await self.session.commit()
        except Exception:
            pass

        # 전 옵션 품절 시 소싱처 1회 최신화 시도 (30초 타임아웃)
        _all_opts = product_dict.get("options") or []
        _all_sold = _all_opts and all(
            (o.get("isSoldOut", False) or (o.get("stock") or 0) <= 0)
            for o in _all_opts
            if isinstance(o, dict)
        )
        if (
            _all_sold
            and not pending_refresh_updates
            and product_row.source_site
            and product_row.site_product_id
        ):
            logger.info(
                f"[전송] 상품 {product_id} 전 옵션 품절 → 소싱처 1회 최신화 시도 (30초)"
            )
            try:
                from backend.domain.samba.collector.refresher import (
                    refresh_product as _refresh_sold,
                )

                _sold_refresh = await asyncio.wait_for(
                    _refresh_sold(product_row, source="transmit"),
                    timeout=30,
                )
                if not _sold_refresh.error and _sold_refresh.new_options is not None:
                    # 옵션/가격 업데이트
                    product_dict["options"] = _sold_refresh.new_options
                    if _sold_refresh.new_sale_price is not None:
                        product_dict["sale_price"] = _sold_refresh.new_sale_price
                    if _sold_refresh.new_original_price is not None:
                        product_dict["original_price"] = (
                            _sold_refresh.new_original_price
                        )
                    if _sold_refresh.new_cost is not None:
                        product_dict["cost"] = _sold_refresh.new_cost
                    if _sold_refresh.new_sale_status:
                        product_dict["sale_status"] = _sold_refresh.new_sale_status
                    # pending_refresh_updates에도 반영 (최종 DB 저장용)
                    pending_refresh_updates.update(
                        {
                            "options": _sold_refresh.new_options,
                            "last_refreshed_at": datetime.now(UTC),
                        }
                    )
                    if _sold_refresh.new_sale_price is not None:
                        pending_refresh_updates["sale_price"] = (
                            _sold_refresh.new_sale_price
                        )
                    if _sold_refresh.new_original_price is not None:
                        pending_refresh_updates["original_price"] = (
                            _sold_refresh.new_original_price
                        )
                    if _sold_refresh.new_cost is not None:
                        pending_refresh_updates["cost"] = _sold_refresh.new_cost
                    if _sold_refresh.new_sale_status:
                        pending_refresh_updates["sale_status"] = (
                            _sold_refresh.new_sale_status
                        )
                    # 가격/재고 변동 계산 (기존 최신화와 동일 포맷)
                    _old_cost = getattr(product_row, "cost", None) or 0
                    _new_cost = (
                        _sold_refresh.new_cost
                        if _sold_refresh.new_cost is not None
                        else _old_cost
                    )
                    # 재고변동 건수 — 품절↔재고 전환(무↔유)만 카운트 (단순 수량변화 제외)
                    from backend.domain.samba.collector.refresher import (
                        count_stock_transitions,
                    )

                    _old_opts = getattr(product_row, "options", None) or []
                    _stock_change_count = count_stock_transitions(
                        _old_opts, _sold_refresh.new_options
                    )
                    # 가격/재고 이력 스냅샷 기록
                    _snap = {
                        "date": datetime.now(UTC).isoformat(),
                        "source": "transmit_soldout_refresh",
                        "sale_price": _sold_refresh.new_sale_price
                        if _sold_refresh.new_sale_price is not None
                        else product_row.sale_price,
                        "original_price": _sold_refresh.new_original_price
                        if _sold_refresh.new_original_price is not None
                        else product_row.original_price,
                        "cost": _sold_refresh.new_cost
                        if _sold_refresh.new_cost is not None
                        else product_row.cost,
                        "sale_status": _sold_refresh.new_sale_status or "in_stock",
                        "changed": _sold_refresh.changed,
                        "options": _sold_refresh.new_options,
                    }
                    _history = list(product_row.price_history or [])
                    _history.insert(0, _snap)
                    if len(_history) <= 5:
                        pending_refresh_updates["price_history"] = _history
                    else:
                        pending_refresh_updates["price_history"] = _history[:4] + [
                            _history[-1]
                        ]
                    logger.info(
                        f"[전송] 품절 최신화 완료 — 원가 {int(_old_cost):,}>{int(_new_cost):,}, 재고변동 {_stock_change_count}건"
                    )
                    if not refresh_status:
                        refresh_status = f"원가 {int(_old_cost):,}>{int(_new_cost):,}, 재고변동 {_stock_change_count}건"
                else:
                    _err = (
                        _sold_refresh.error
                        if _sold_refresh.error
                        else "옵션 데이터 없음"
                    )
                    logger.info(f"[전송] 품절 최신화 실패 — {_err}")
                    if not refresh_status:
                        refresh_status = f"최신화실패:{_err[:50]}"
            except asyncio.TimeoutError:
                logger.warning("[전송] 전 옵션 품절 소싱처 최신화 타임아웃 (30초)")
            except Exception as _sold_e:
                logger.warning(f"[전송] 전 옵션 품절 소싱처 최신화 예외: {_sold_e}")

        # 모든 pre-read 완료 — asyncio.gather 전 커밋으로 idle in transaction 방지
        # (policy/name_rule/account 읽기가 여기까지 모두 완료됨)
        try:
            await self.session.commit()
        except Exception:
            pass

        # 계정별 전송을 병렬 코루틴으로 실행

        async def _dispatch_one(account_id: str) -> dict[str, Any]:
            """단일 계정 전송 — 결과 dict 반환."""
            res: dict[str, Any] = {
                "account_id": account_id,
                "status": "failed",
                "error": "",
                "product_nos": {},
                "sent_snapshot": None,
                "is_update": False,
                "clear_nos": [],
                "db_update_failed": False,
            }
            try:
                # 전송 시작 전 취소 체크
                if is_cancel_requested():
                    res["error"] = "전송 취소됨"
                    res["status"] = "cancelled"
                    return res

                account = _dispatch_account_map.get(account_id)
                if not account:
                    res["error"] = "계정을 찾을 수 없습니다."
                    return res

                market_type = account.market_type

                # 0순위: 수동등록 상품의 계정별 명시 카테고리
                # manual_market_categories는 {account_id: category_id} 구조
                category_id = _manual_market_categories.get(str(account_id), "")
                if category_id:
                    logger.info(
                        f"[전송] 수동등록 카테고리 사용: {market_type} account={account_id} → {category_id}"
                    )
                # 수동카테고리 없으면 기존 매핑 카테고리 사용
                if not category_id:
                    category_id = mapped_categories.get(market_type, "")

                # ESM Plus 크로스매핑: 지마켓↔옥션 자동 변환
                if not category_id and market_type in ("gmarket", "auction"):
                    other = "auction" if market_type == "gmarket" else "gmarket"
                    other_id = mapped_categories.get(other, "")
                    if other_id and str(other_id).isdigit():
                        from backend.domain.samba.proxy.esmplus import esm_map_category

                        category_id = esm_map_category(other_id, other, market_type)
                        if category_id:
                            logger.info(
                                f"[ESM 크로스매핑] {other}({other_id}) → {market_type}({category_id})"
                            )

                # 카페24/롯데홈쇼핑은 플러그인 내부에서 자체 카테고리(소싱처/정책 disp_no)를 사용하므로 매핑 없어도 허용
                if not category_id and market_type not in (
                    "playauto",
                    "cafe24",
                    "lottehome",
                ):
                    res["error"] = "카테고리 매핑 없음"
                    logger.warning(
                        f"[전송] 상품 {product_id} → {market_type} 카테고리 매핑 없음 (스킵)"
                    )
                    return res

                # 롯데ON은 BC 접두사 카테고리 코드 사용 (BC41030100 형식)
                _lotteon_like = market_type in ("lotteon", "ssg")
                if (
                    market_type not in ("coupang", "playauto", "cafe24", "lottehome")
                    and not _lotteon_like
                    and not str(category_id).isdigit()
                ):
                    res["error"] = f"최하단 카테고리 매핑 필요 (현재: {category_id})"
                    logger.warning(
                        f"[전송] 상품 {product_id} → {market_type} 최하단 카테고리 미매핑: '{category_id}' (스킵)"
                    )
                    return res

                # 전 옵션 품절 체크 — 마켓 등록 상품이면 마켓 삭제, 미등록이면 스킵
                _opts = product_dict.get("options") or []
                if _opts and all(
                    (o.get("isSoldOut", False) or (o.get("stock") or 0) <= 0)
                    for o in _opts
                    if isinstance(o, dict)
                ):
                    # 이미 마켓 등록된 상품이면 삭제 처리
                    _reg_accs = product_dict.get("registered_accounts") or []
                    # 전옵션 품절 처리: 마켓 등록 여부에 따라 삭제 시도
                    if account_id in _reg_accs:
                        # 등록된 계정 → 마켓 삭제 시도
                        try:
                            from backend.domain.samba.shipment.dispatcher import (
                                delete_from_market,
                            )

                            # 디스패처는 product["market_product_no"][market_type] 키를 읽음
                            # product_dict는 model_dump 결과라 market_product_nos(복수형)만 있고
                            # market_product_no(단수형)는 없음 → 명시적으로 주입 필요.
                            # 스마트스토어는 삭제 API가 originProductNo를 요구하므로
                            # {account_id}_origin 키 우선 (delete_from_markets 2347-2363과 동일 패턴)
                            _m_nos = product_row.market_product_nos or {}
                            if market_type == "smartstore":
                                _pno = _m_nos.get(f"{account_id}_origin", "")
                                if not _pno:
                                    _raw = _m_nos.get(account_id, "")
                                    if isinstance(_raw, dict):
                                        _pno = (
                                            _raw.get("originProductNo")
                                            or _raw.get("smartstoreChannelProductNo")
                                            or _raw.get("groupProductNo")
                                            or ""
                                        )
                                    else:
                                        _pno = _raw
                                _pno = str(_pno) if _pno else ""
                            else:
                                _pno = _m_nos.get(account_id, "")
                            _del_pd = {
                                **product_dict,
                                "market_product_no": {market_type: _pno},
                            }

                            # HTTP 마켓 삭제 전 커밋 — idle in transaction 방지
                            try:
                                await self.session.commit()
                            except Exception:
                                pass
                            del_result = await delete_from_market(
                                self.session, market_type, _del_pd, account=account
                            )
                        except Exception as _api_e:
                            logger.warning(
                                f"[전송] 전옵션 품절 마켓 삭제 API 예외: {_api_e}"
                            )
                            res["error"] = "전 옵션 품절 (마켓삭제 실패)"
                        else:
                            # API 호출 성공 → DB 업데이트는 best-effort
                            if del_result.get("success") and not del_result.get(
                                "soldout_fallback"
                            ):
                                # 실제 삭제(DELETE 200) 시에만 registered_accounts 제거
                                try:
                                    _prod = await SambaCollectedProductRepository(
                                        self.session
                                    ).get_async(product_id)
                                    if _prod:
                                        new_reg = [
                                            a
                                            for a in (_prod.registered_accounts or [])
                                            if a != account_id
                                        ]
                                        _prod.registered_accounts = (
                                            new_reg if new_reg else None
                                        )
                                        await self.session.commit()
                                except Exception as _db_e:
                                    logger.warning(
                                        f"[전송] DB 업데이트 실패 (마켓삭제는 성공): {_db_e}"
                                    )
                                    res["db_update_failed"] = True
                                # DB 실패 무관하게 API 성공은 "completed" 처리
                                res["status"] = "completed"
                                res["results"] = {account_id: "deleted"}
                                logger.info(
                                    f"[전송] 상품 {product_id} → {market_type} 전 옵션 품절 → 마켓 삭제 완료"
                                )
                                return res
                            else:
                                # API 호출은 성공했으나 soldout_fallback=True 또는 success=False
                                res["error"] = "전 옵션 품절 (마켓삭제 실패)"

                    # 품절 스킵 케이스: status를 skipped로 명시, error 기본값 보정
                    # (res.status=="completed"는 마켓삭제 완료 → 이미 return된 상태이므로 여기 도달 안함)
                    res["status"] = "skipped"
                    if not res.get("error"):
                        res["error"] = "전 옵션 품절"
                    logger.info(
                        f"[전송] 상품 {product_id} → {market_type} 전 옵션 품절 스킵"
                    )
                    return res

                # 마켓별 판매가 계산 (product_dict 원본 보호를 위해 복사본 사용)
                acct_product = dict(product_dict)

                # SSG 표준카테고리(stdCtgId) 주입 — ssg_std 매핑값을 _std_category_id로 전달
                if market_type == "ssg":
                    _std_cat = mapped_categories.get("ssg_std", "")
                    if _std_cat:
                        acct_product["_std_category_id"] = _std_cat
                        logger.info(
                            f"[SSG] 표준카테고리 주입: dispCtgId={mapped_categories.get('ssg', '')!r}, stdCtgId={_std_cat!r}"
                        )
                    else:
                        logger.warning("[SSG] ssg_std 매핑 없음 — 표준카테고리 미전송")

                # 마켓별 상세페이지 템플릿 오버라이드
                # 프론트엔드는 market_type(영문 ID: "playauto")을 키로 저장
                # 마켓별 상품명 조합 덮어쓰기
                _nr = product_dict.get("_name_rule")
                if _nr and getattr(_nr, "market_name_compositions", None):
                    _market_comp = _nr.market_name_compositions.get(market_type)
                    if _market_comp:
                        # 원본 상품 데이터로 마켓별 조합 실행
                        _orig = dict(product_dict)
                        _orig["name"] = product_dict.get(
                            "_original_name", product_dict.get("name", "")
                        )
                        acct_product["name"] = self._compose_product_name(
                            _orig,
                            _nr,
                            market_type=market_type,
                            deletion_words=product_dict.get("_deletion_words"),
                        )
                if not is_price_stock_only:
                    _detail_tpl_id = ""
                    if policy and policy.extras:
                        _detail_tpl_id = (
                            policy.extras.get("market_detail_templates") or {}
                        ).get(market_type) or ""
                    if _detail_tpl_id:
                        logger.info(
                            f"[전송] 마켓별 상세 템플릿 적용: market={market_type}, tpl_id={_detail_tpl_id}"
                        )
                    acct_product["detail_html"] = await self._build_detail_html(
                        acct_product,
                        template_id_override=_detail_tpl_id,
                    )
                cost = (
                    acct_product.get("cost")
                    or acct_product.get("sale_price")
                    or acct_product.get("original_price")
                    or 0
                )
                if policy and policy.pricing:
                    cost_info = await convert_cost_by_source_site(
                        self.session,
                        cost,
                        product_row.source_site or "",
                        product_row.tenant_id,
                    )
                    effective_cost = cost_info["convertedCost"]
                    calc_price = calc_market_price(
                        effective_cost,
                        policy.pricing,
                        market_type,
                        policy_market_data,
                        source_site=product_row.source_site or "",
                        is_point_restricted=getattr(
                            product_row, "is_point_restricted", None
                        ),
                    )

                    # 가격 이상치 방어: 원가가 정상가의 5% 미만이면 전송 차단
                    _orig_price = int(acct_product.get("original_price") or 0)
                    if _orig_price > 0 and cost > 0 and cost < _orig_price * 0.05:
                        logger.error(
                            f"[가격방어] 전송 차단 — 원가 이상치: "
                            f"원가={int(cost):,}, 정상가={_orig_price:,}, "
                            f"계산가={calc_price:,}"
                        )
                        res["error"] = (
                            f"원가 이상치 감지 "
                            f"(원가 {int(cost):,}원 < 정상가 {_orig_price:,}원의 5%)"
                        )
                        return res

                    acct_product["sale_price"] = calc_price
                    logger.info(
                        f"[전송] 정책 가격 계산: 원가={cost} → 판매가={calc_price}"
                    )
                    logger.info(f"[메모리] 가격계산 후: {_mem_mb()}MB")

                # 스킵 판단
                cur_price = int(acct_product.get("sale_price") or 0)
                cur_cost_int = int(acct_product.get("cost") or 0)
                last_sent = (product_row.last_sent_data or {}).get(account_id)
                if last_sent:
                    last_price = (int(last_sent.get("sale_price") or 0) // 100) * 100
                    last_cost_sent = int(last_sent.get("cost") or 0)
                    last_opts = last_sent.get("options", [])
                    cur_opts = [
                        {
                            "name": o.get("name", ""),
                            "price": o.get("price"),
                            "stock": o.get("stock"),
                        }
                        for o in (acct_product.get("options") or [])
                    ]
                    opts_changed = last_opts != cur_opts
                else:
                    last_price = 0
                    last_cost_sent = 0
                    opts_changed = False

                # 기존 상품번호 확인 — skip_unchanged 판단 전에 먼저 수행
                # (미등록 상품은 last_sent_data가 있어도 스킵하면 안 됨)
                existing_nos = product_row.market_product_nos or {}
                if market_type == "smartstore":
                    existing_product_no = existing_nos.get(f"{account_id}_origin", "")
                    if not existing_product_no:
                        raw_existing = existing_nos.get(account_id, "")
                        if isinstance(raw_existing, dict):
                            existing_product_no = (
                                raw_existing.get("originProductNo")
                                or raw_existing.get("smartstoreChannelProductNo")
                                or raw_existing.get("groupProductNo")
                                or ""
                            )
                        else:
                            existing_product_no = raw_existing
                else:
                    existing_product_no = existing_nos.get(account_id, "")
                if existing_product_no:
                    res["is_update"] = True
                    logger.info(
                        f"[전송] 기존 상품번호 발견 → 수정 모드: {market_type} #{existing_product_no}"
                    )

                # 마켓에 실제 등록된 상품번호가 있는 경우에만 skip_unchanged 적용
                # existing_product_no 없으면 미등록 상품 → 반드시 신규 등록 시도
                if skip_unchanged and has_update and last_sent and existing_product_no:
                    if (
                        last_price == cur_price
                        and last_cost_sent == cur_cost_int
                        and not opts_changed
                    ):
                        res["status"] = "skipped"
                        res["error"] = "이미 등록됨, 변동 없음"
                        logger.info(
                            f"[전송] {market_type} 스킵 (이미 등록됨, 변동 없음)"
                        )
                        return res

                # 마켓 API 호출 (계정별 세마포어 — 120초 대기)
                # httpx 타임아웃과 차등화하여 한 건이 느려도 동반 타임아웃 폭주 방지
                account_sem = _get_account_semaphore(account_id)
                try:
                    await asyncio.wait_for(account_sem.acquire(), timeout=300)
                except asyncio.TimeoutError:
                    res["error"] = f"계정 사용 중 (300초 타임아웃, {market_type})"
                    logger.warning(f"[전송] 계정 {account_id} 세마포어 300초 타임아웃")
                    return res
                try:
                    # 취소 체크 — 세마포어 대기 중 취소됐을 수 있음
                    if is_cancel_requested():
                        res["error"] = "전송 취소됨"
                        res["status"] = "cancelled"
                        logger.info(
                            f"[전송] 취소 감지 → {market_type} 전송 스킵 (계정 {account_id})"
                        )
                        return res
                    # 등록상품명 계정별 중복 등록 차단
                    _mkt_names = product_dict.get("market_names") or {}
                    _reg_name = _mkt_names.get(account.market_name)
                    if _reg_name:
                        _dup = await product_repo.find_by_market_name_and_account(
                            tenant_id=product_row.tenant_id,
                            market_key=account.market_name,
                            product_name=_reg_name,
                            account_id=account_id,
                            exclude_product_id=product_row.id,
                        )
                        if _dup:
                            res["error"] = (
                                f"등록상품명 중복 차단: '{_reg_name}' 이(가) "
                                f"이미 상품 ID={_dup.id}({_dup.name[:20]})에 등록됨"
                            )
                            logger.warning(
                                f"[중복등록 차단] 등록상품명={_reg_name!r} "
                                f"계정={account.account_label}({account_id}) "
                                f"기등록 상품 ID={_dup.id}"
                            )
                            return res

                    # 모든 DB 읽기 완료 — HTTP 전송 전 트랜잭션 종료 (idle in transaction 방지)
                    try:
                        await self.session.commit()
                    except Exception:
                        pass
                    logger.info(f"[메모리] 마켓전송 전: {_mem_mb()}MB")
                    start_time = time.time()
                    result = await dispatch_to_market(
                        self.session,
                        market_type,
                        acct_product,
                        category_id,
                        account=account,
                        existing_product_no=existing_product_no,
                    )
                    elapsed = time.time() - start_time
                    logger.info(
                        f"[마켓전송완료] {market_type} 소요시간: {elapsed:.1f}초 (상품: {product_row.name[:40]})"
                    )
                finally:
                    account_sem.release()

                # 404 → 상품번호 초기화
                if result.get("_clear_product_no"):
                    res["clear_nos"] = [account_id, f"{account_id}_origin"]
                    logger.info(
                        f"[전송] 404 상품번호 초기화: {market_type} (계정: {account_id})"
                    )

                if result.get("success"):
                    res["status"] = "success"
                    # 중복등록 차단 시 pre-check에서 추출한 원상품번호 직접 사용
                    if result.get("_already_registered") and result.get("_origin_no"):
                        _pre_origin = str(result["_origin_no"])
                        res["product_nos"] = {
                            account_id: _pre_origin,
                            f"{account_id}_origin": _pre_origin,
                        }
                        logger.info(
                            f"[전송] 스마트스토어 중복등록 차단 — 기존 originProductNo={_pre_origin} 연결"
                        )
                        res["sent_snapshot"] = {
                            "sale_price": math.ceil(
                                int(acct_product.get("sale_price") or 0) / 300
                            )
                            * 300,
                            "cost": int(acct_product.get("cost") or 0),
                            "options": [
                                {
                                    "name": o.get("name", ""),
                                    "price": o.get("price"),
                                    "stock": o.get("stock"),
                                }
                                for o in (acct_product.get("options") or [])
                            ],
                            "sent_at": datetime.now(UTC).isoformat(),
                        }
                        return res
                    # 상품번호 추출
                    # product_no: 플러그인이 "product_no" 키로 반환 (롯데ON 등)
                    # spdNo: 이전 방식 또는 일부 마켓 직접 반환 — 둘 다 확인
                    product_no = self._extract_market_product_no(result)
                    # 스마트스토어 origin/channel 분리를 위해 api_data 는 항상 추출
                    # (기존: product_no 가 비어있을 때만 → smartstore 도 origin 만 저장하던 버그)
                    api_data: dict[str, Any] = {}
                    result_data = result.get("data", {})
                    if isinstance(result_data, dict):
                        api_data = result_data.get("data", result_data)
                        if isinstance(api_data, list) and api_data:
                            api_data = (
                                api_data[0] if isinstance(api_data[0], dict) else {}
                            )
                        if not isinstance(api_data, dict):
                            api_data = {}
                        if not product_no and api_data:
                            product_no = self._extract_market_product_no(api_data)
                    if product_no:
                        nos: dict[str, str] = {account_id: str(product_no)}
                        if market_type == "smartstore" and isinstance(api_data, dict):
                            origin_no = api_data.get("originProductNo") or ""
                            channel_no = (
                                api_data.get("smartstoreChannelProductNo") or ""
                            )
                            # _origin 키가 없으면 삭제 API 실패 — 항상 저장 (있으면 덮어씀)
                            if origin_no:
                                nos[f"{account_id}_origin"] = str(origin_no)
                                nos[account_id] = str(channel_no or product_no)
                            elif channel_no:
                                # origin 없이 channel 만 온 경우 channel 로 fallback
                                nos[account_id] = str(channel_no)
                            logger.info(
                                f"[전송] 스마트스토어 상품번호 — channel={channel_no or product_no}, origin={origin_no}"
                            )
                        # 쿠팡 — vp/products URL 은 {productId}?vendorItemId={vendorItemId} 형식.
                        # plugin 이 register 후 GET 으로 추출한 값을 별도 sub-key 로 저장.
                        if market_type == "coupang":
                            _cpid = str(result.get("coupang_product_id", "") or "")
                            _cvid = str(result.get("coupang_vendor_item_id", "") or "")
                            if _cpid:
                                nos[f"{account_id}_pid"] = _cpid
                            if _cvid:
                                nos[f"{account_id}_vid"] = _cvid
                        res["product_nos"] = nos
                        logger.info(f"[전송] {market_type} 상품번호: {product_no}")

                    # 스냅샷 준비 (스마트스토어는 300원 올림 반영)
                    _snap_price = int(acct_product.get("sale_price") or 0)
                    if market_type == "smartstore":
                        _snap_price = math.ceil(_snap_price / 300) * 300
                    res["sent_snapshot"] = {
                        "sale_price": _snap_price,
                        "cost": int(acct_product.get("cost") or 0),
                        "options": [
                            {
                                "name": o.get("name", ""),
                                "price": o.get("price"),
                                "stock": o.get("stock"),
                            }
                            for o in (acct_product.get("options") or [])
                        ],
                        "sent_at": datetime.now(UTC).isoformat(),
                    }

                    action = "수정" if existing_product_no else "등록"
                    logger.info(
                        f"[전송] {market_type} {action} 성공 - 상품: {product_id}, 계정: {account_id}"
                    )
                else:
                    # _skip_retry: 플레이오토 미등록 상품코드 — 재시도/신규등록 차단
                    if result.get("_skip_retry"):
                        res["status"] = "skipped"
                        res["_clear_failed_at"] = True
                    _msg = result.get("message", "알 수 없는 오류")
                    res["error"] = str(_msg) if not isinstance(_msg, str) else _msg
                    logger.warning(f"[전송] {market_type} 실패 - {_msg}")

            except Exception as exc:
                _err = str(exc)
                # asyncio 내부 객체 누출 방지
                if "<asyncio" in _err or "Semaphore" in _err:
                    _err = f"전송 타임아웃 또는 동시성 오류 ({market_type})"
                    logger.error(f"[전송] 계정 {account_id} 세마포어 누출: {exc}")
                else:
                    logger.error(f"[전송] 계정 {account_id} 예외: {exc}")
                res["error"] = _err
                try:
                    await self.session.rollback()
                except Exception:
                    pass
            return res

        # 계정별 순차 전송 — 동일 세션 병렬 사용 시 asyncpg 연결 오염 방지
        account_results = []
        for _aid in target_account_ids:
            account_results.append(await _dispatch_one(_aid))

        # 결과 병합 + DB 일괄 업데이트
        merged_nos = dict(product_row.market_product_nos or {})
        merged_sent = dict(product_row.last_sent_data or {})
        # A칸(registered_accounts) 동기화: 정상 전송 성공 경로에서도 함께 갱신
        # — 기존엔 스마트스토어 group 등록·삭제·재시도 경로만 A칸을 갱신해서
        #   11번가/쿠팡/롯데홈 등 일반 마켓은 B칸만 채워지고 A칸은 backfill 루프가
        #   채워줄 때까지(때로는 1시간+) 비어있어, 테트리스 sync가 같은 상품을
        #   '미등록'으로 오판해 헛걸음 잡을 반복 생성했음 → 'skipped(이미 등록됨, 변동 없음)' 로그 발생.
        merged_reg = list(product_row.registered_accounts or [])
        for ar in account_results:
            if isinstance(ar, Exception):
                continue
            aid = ar["account_id"]
            transmit_result[aid] = ar["status"]
            if ar["error"]:
                transmit_error[aid] = ar["error"]
            if ar["is_update"]:
                update_mode_accounts.add(aid)
            # 404 초기화 — B칸과 A칸에서 동시 제거
            for key in ar.get("clear_nos", []):
                merged_nos.pop(key, None)
                if key == aid and aid in merged_reg:
                    merged_reg.remove(aid)
            # 상품번호 병합 — B칸에 account_id 키가 채워지면 A칸도 동기화
            _new_nos = ar.get("product_nos", {}) or {}
            merged_nos.update(_new_nos)
            if (
                ar.get("status") == "success"
                and _new_nos.get(aid)
                and aid not in merged_reg
            ):
                merged_reg.append(aid)
            # 스냅샷 병합
            if ar.get("sent_snapshot"):
                # 전송 성공 — sent_snapshot으로 덮어써서 기존 failed_at 마킹 자동 제거
                merged_sent[aid] = ar["sent_snapshot"]
            elif ar.get("status") == "failed":
                # 전송 실패 — 기존 last_sent 보존 + failed_at 마킹 (오토튠이 다음 cycle에서
                # 무조건 재시도 트리거. last_sent.sale_price는 안 갱신해 expected==last
                # 비교는 그대로 동작하지만 failed_at 존재 여부로 강제 전송)
                _existing = dict(merged_sent.get(aid, {}) or {})
                _existing["failed_at"] = datetime.now(UTC).isoformat()
                merged_sent[aid] = _existing
            elif ar.get("_clear_failed_at") and aid in merged_sent:
                # _skip_retry 케이스 (플레이오토 미등록 상품코드): 기존 failed_at 제거 → 재시도 루프 차단
                _existing = dict(merged_sent[aid] or {})
                _existing.pop("failed_at", None)
                merged_sent[aid] = _existing

        # DB 1회 업데이트
        try:
            await product_repo.update_async(
                product_id,
                market_product_nos=merged_nos or None,
                registered_accounts=merged_reg or None,
                last_sent_data=merged_sent or None,
            )
        except Exception as _db_e:
            logger.warning(f"[전송] DB 업데이트 실패: {_db_e}")
            try:
                await self.session.rollback()
            except Exception:
                pass

        # 마켓삭제 성공 + DB 업데이트 실패 계정 → 새 세션으로 registered_accounts 재시도
        _failed_db_accs = [
            ar["account_id"]
            for ar in account_results
            if isinstance(ar, dict) and ar.get("db_update_failed")
        ]
        if _failed_db_accs:
            from backend.db.orm import get_write_session

            try:
                async with get_write_session() as _retry_s:
                    _retry_prod = await SambaCollectedProductRepository(
                        _retry_s
                    ).get_async(product_id)
                    if _retry_prod:
                        new_reg = [
                            a
                            for a in (_retry_prod.registered_accounts or [])
                            if a not in _failed_db_accs
                        ]
                        _retry_prod.registered_accounts = new_reg if new_reg else None
                        await _retry_s.commit()
                        logger.info(
                            f"[전송] DB 재시도 성공 — registered_accounts 갱신: {_failed_db_accs}"
                        )
            except Exception as _retry_e:
                logger.error(
                    f"[전송] DB 재시도도 실패 — 상품관리 배지 불일치 가능: {_retry_e}"
                )

        # 6. 최종 상태 결정
        values = list(transmit_result.values())
        non_skip = [v for v in values if v != "skipped"]
        all_skipped = len(values) > 0 and len(non_skip) == 0
        all_success = len(non_skip) > 0 and all(
            v in ("success", "completed") for v in non_skip
        )
        all_failed = len(non_skip) > 0 and all(v == "failed" for v in non_skip)

        if all_skipped:
            final_status = "skipped"
        elif all_success:
            final_status = "completed"
        elif all_failed:
            final_status = "failed"
        else:
            final_status = "partial"

        final_update: dict[str, Any] = {
            "status": final_status,
            "transmit_result": transmit_result,
            "transmit_error": transmit_error if transmit_error else None,
            "completed_at": datetime.now(UTC),
        }
        if refresh_status:
            final_update["update_result"] = {"refresh": refresh_status}
        updated = await self.repo.update_async(shipment.id, **final_update)

        # 6. 상품 상태 업데이트 (등록된 계정 목록)
        # 성공한 계정은 추가, 실패한 계정은 제거
        # 단, PATCH(수정) 모드에서 실패한 계정은 등록정보 보존 (404 케이스는 이미 위에서 처리됨)
        success_accounts = [
            aid for aid, status in transmit_result.items() if status == "success"
        ]
        # 신규등록(POST) 실패만 제거 대상 — 수정(PATCH) 실패/스킵은 기존 등록정보 유지
        removable_failed = [
            aid
            for aid, status in transmit_result.items()
            if status not in ("success", "skipped") and aid not in update_mode_accounts
        ]
        # DB에서 최신 상태 다시 읽기 (전송 중 market_product_nos가 업데이트되었을 수 있음)
        refreshed = await product_repo.get_async(product_id)
        existing = (
            refreshed.registered_accounts
            if refreshed
            else product_row.registered_accounts
        ) or []
        existing_nos = dict(
            (
                refreshed.market_product_nos
                if refreshed
                else product_row.market_product_nos
            )
            or {}
        )
        # 성공 추가 + 신규등록 실패만 제거
        new_accounts = list(
            set([a for a in existing if a not in removable_failed] + success_accounts)
        )
        # 신규등록 실패한 계정의 상품번호만 제거
        new_nos = {k: v for k, v in existing_nos.items() if k not in removable_failed}
        # 최신화 실패 시에는 상품 데이터 변경하지 않음 (updated_at 유지)
        if refresh_status and (
            refresh_status.startswith("최신화실패")
            or refresh_status.startswith("최신화예외")
        ):
            logger.info("[전송] 최신화 실패 → 상품 데이터 변경 안 함")
        else:
            update_data: dict[str, Any] = {
                "registered_accounts": new_accounts if new_accounts else None,
                "market_product_nos": new_nos if new_nos else None,
                "status": "registered" if new_accounts else "collected",
                "updated_at": datetime.now(UTC),
            }
            # 소싱처 최신화 결과도 통합 저장
            if pending_refresh_updates:
                update_data.update(pending_refresh_updates)
            await product_repo.update_async(product_id, **update_data)

        logger.info(
            f"Shipment {shipment.id} 완료 status={final_status} "
            f"product={product_id} 성공={sum(1 for v in values if v == 'success')}/{len(values)}"
        )
        if not updated:
            logger.warning(f"Shipment {shipment.id} 업데이트 실패, DB 재조회")
            updated = await self.repo.get_async(shipment.id)
        return updated or shipment

    # ==================== 상품명 조합 ====================

    def _compose_product_name(
        self,
        product: dict[str, Any],
        name_rule: Any,
        *,
        market_type: str | None = None,
        deletion_words: list[str] | None = None,
    ) -> str:
        """정책의 상품명 규칙(name_composition)에 따라 상품명을 조합.

        market_type이 지정되고 market_name_compositions에 해당 마켓 설정이 있으면 마켓별 조합 사용.
        """
        # 마켓별 조합이 있으면 우선 사용
        composition = None
        if market_type and getattr(name_rule, "market_name_compositions", None):
            composition = name_rule.market_name_compositions.get(market_type)
        if not composition:
            composition = name_rule.name_composition
        if not composition:
            return product.get("name", "")

        # SEO 검색키워드: seo_keywords 배열을 공백 연결
        seo_kws = product.get("seo_keywords") or []
        seo_text = " ".join(seo_kws[:2]) if seo_kws else ""

        tag_map = {
            "{상품명}": product.get("name", ""),
            "{브랜드명}": product.get("brand", ""),
            "{모델명}": product.get("style_code", ""),
            "{사이트명}": product.get("source_site", ""),
            "{상품번호}": product.get("site_product_id", ""),
            "{검색키워드}": seo_text,
        }

        # 조합 태그 순서대로 값 치환 (빈 값이면 태그 자체 제거)
        parts = [tag_map.get(tag, "") if tag in tag_map else tag for tag in composition]
        composed = " ".join(p for p in parts if p and p.strip())

        # 치환어 적용 (동시치환/순차치환 분기)

        replacements = name_rule.replacements or []
        if replacements:
            replace_mode = getattr(name_rule, "replace_mode", "simultaneous")
            if replace_mode == "sequential":
                # 순차치환: 위에서 아래로 순서대로 치환
                for r in replacements:
                    fr = (
                        r.get("from", "")
                        if isinstance(r, dict)
                        else getattr(r, "from_", "")
                    )
                    to = (
                        r.get("to", "") if isinstance(r, dict) else getattr(r, "to", "")
                    )
                    if not fr:
                        continue
                    case_insensitive = (
                        r.get("caseInsensitive", True)
                        if isinstance(r, dict)
                        else getattr(r, "caseInsensitive", True)
                    )
                    flags = re.IGNORECASE if case_insensitive else 0
                    composed = re.sub(re.escape(fr), to or "", composed, flags=flags)
            else:
                # 동시치환(기본): 모든 규칙을 한번에 적용, 긴 문자열 우선
                composed = self._simultaneous_replace(composed, replacements)

        # 삭제어 적용 (dedup 전에 적용하여 중복 단어 감지 가능하게)
        if deletion_words:
            for dw in deletion_words:
                composed = re.sub(re.escape(dw), " ", composed, flags=re.IGNORECASE)
            composed = re.sub(r"\s{2,}", " ", composed).strip()

        # 중복 단어 제거 — 구두점 안에 묶인 부분단어까지 감지
        if name_rule.dedup_enabled:
            seen: set[str] = set()

            def _dedup_replace(m: re.Match) -> str:
                word = m.group(0)
                lower = word.lower()
                if lower in seen:
                    return ""
                seen.add(lower)
                return word

            # 2자 이상 한글/영문 + 하이픈 연결 숫자(품번) + 3자 이상 순수 숫자
            composed = re.sub(
                r"[^\W\d_]{2,}|\d+(?:-\d+)+|\d{3,}",
                _dedup_replace,
                composed,
                flags=re.UNICODE,
            )
            # 연속 공백 정리
            composed = re.sub(r"\s+", " ", composed).strip()

        # prefix/suffix 적용
        if name_rule.prefix:
            composed = f"{name_rule.prefix} {composed}"
        if name_rule.suffix:
            composed = f"{composed} {name_rule.suffix}"

        return composed.strip()

    @staticmethod
    def _simultaneous_replace(text: str, replacements: list) -> str:
        """동시치환: 모든 치환규칙의 매칭을 한번에 수집 → 긴 문자열 우선 → 비겹침 선택."""

        # (start, end, to_val, from_len, priority)
        all_matches: list[tuple[int, int, str, int, int]] = []

        for i, r in enumerate(replacements):
            fr = r.get("from", "") if isinstance(r, dict) else getattr(r, "from_", "")
            to_val = r.get("to", "") if isinstance(r, dict) else getattr(r, "to", "")
            if not fr:
                continue
            case_insensitive = (
                r.get("caseInsensitive", True)
                if isinstance(r, dict)
                else getattr(r, "caseInsensitive", True)
            )
            flags = re.IGNORECASE if case_insensitive else 0
            pattern = re.compile(re.escape(fr), flags)
            for m in pattern.finditer(text):
                all_matches.append(
                    (m.start(), m.end(), to_val or "", m.end() - m.start(), i)
                )

        if not all_matches:
            return text

        # 위치(ASC) → 길이(DESC, 긴 것 우선) → 규칙순서(ASC)
        all_matches.sort(key=lambda x: (x[0], -x[3], x[4]))

        # 겹치지 않는 매칭만 선택 (greedy left-to-right)
        selected = []
        last_end = 0
        for match in all_matches:
            if match[0] >= last_end:
                selected.append(match)
                last_end = match[1]

        # 결과 문자열 조립
        parts: list[str] = []
        pos = 0
        for start, end, to_val, _, _ in selected:
            parts.append(text[pos:start])
            parts.append(to_val)
            pos = end
        parts.append(text[pos:])
        return "".join(parts)

    # ==================== 상세페이지 HTML 생성 ====================

    async def _build_detail_html(
        self, product: dict[str, Any], template_id_override: str = ""
    ) -> str:
        """정책의 상세 템플릿(상단/하단 이미지)과 상품 이미지를 조합하여 상세 HTML 생성.

        구조: 상단이미지 → 대표이미지 → 추가이미지 → 하단이미지
        template_id_override: 마켓별 전용 템플릿 ID (있으면 기본 템플릿 대신 사용)
        """
        from backend.domain.samba.policy.repository import SambaPolicyRepository
        from backend.domain.samba.policy.model import SambaDetailTemplate
        from backend.domain.shared.base_repository import BaseRepository

        parts: list[str] = []
        img_tag = '<div style="text-align:center;"><img src="{url}" style="max-width:860px;width:100%;" /></div>'

        def _extract_url(value: str) -> str:
            """img 태그가 저장된 경우 src URL만 추출."""
            if not value:
                return value
            if value.strip().startswith("<img"):
                import re as _re

                m = _re.search(r'src=["\']([^"\']+)["\']', value)
                return m.group(1) if m else value
            return value

        # 정책에서 상세 템플릿 조회
        policy_id = product.get("applied_policy_id")
        top_img = ""
        bottom_img = ""
        # 이미지 포함 설정 (기본값: 상단/대표/추가/상세/하단 포함)
        img_checks: dict[str, bool] = {
            "topImg": True,
            "main": True,
            "sub": True,
            "title": False,
            "option": False,
            "detail": False,
            "bottomImg": True,
        }
        img_order: list[str] = [
            "topImg",
            "main",
            "sub",
            "title",
            "option",
            "detail",
            "bottomImg",
        ]

        # 템플릿 ID 결정: 마켓별 오버라이드 → 정책 기본값 순
        # template_id_override는 policy_id 유무와 무관하게 항상 적용
        template_id = template_id_override
        if not template_id and policy_id:
            policy_repo = SambaPolicyRepository(self.session)
            policy = await policy_repo.get_async(policy_id)
            if policy and policy.extras:
                template_id = policy.extras.get("detail_template_id")
                logger.info(f"[상세HTML] 정책 {policy_id} 템플릿ID: {template_id}")
            else:
                logger.info(
                    f"[상세HTML] 정책 {policy_id} extras 없음 또는 정책 조회 실패"
                )
        elif not template_id:
            logger.info("[상세HTML] applied_policy_id 없음 — 템플릿 미적용")

        if template_id_override:
            logger.info(f"[상세HTML] 마켓별 오버라이드 템플릿 적용: {template_id_override}")

        if template_id:
            tpl_repo = BaseRepository(self.session, SambaDetailTemplate)
            tpl = await tpl_repo.get_async(template_id)
            if tpl:
                top_img = _extract_url(tpl.top_image_s3_key or "")
                bottom_img = _extract_url(tpl.bottom_image_s3_key or "")
                if tpl.img_checks:
                    img_checks.update(tpl.img_checks)
                if tpl.img_order:
                    img_order = tpl.img_order
                logger.info(
                    f"[상세HTML] 템플릿 로드 — 상단:{bool(top_img)}, 하단:{bool(bottom_img)}, checks:{img_checks}"
                )
            else:
                logger.warning(f"[상세HTML] 템플릿 {template_id} 조회 실패")

        images = product.get("images") or []
        detail_images = product.get("detail_images") or []
        # 추가이미지(sub)에서 출력된 URL을 추적 → detail에서 중복 제외
        # 단, sub가 실제로 출력되는 경우에만 필터링(detail만 단독 사용일 때 무필터 정상 노출)
        sub_will_emit = img_checks.get("sub", False) and len(images) > 1
        sub_set = set(images[1:]) if sub_will_emit else set()

        # img_order 순서대로, img_checks가 True인 항목만 생성
        for item_id in img_order:
            if not img_checks.get(item_id, False):
                continue
            if item_id == "topImg" and top_img:
                parts.append(img_tag.format(url=top_img))
            elif item_id == "main" and images:
                parts.append(img_tag.format(url=images[0]))
            elif item_id == "sub":
                for sub_img in images[1:]:
                    parts.append(img_tag.format(url=sub_img))
            elif item_id == "title":
                name = product.get("name", "")
                if name:
                    parts.append(
                        f'<div style="text-align:center;padding:1rem 0;"><h2 style="color:#333;font-size:1.25rem;">{name}</h2></div>'
                    )
            elif item_id == "detail":
                detail_emitted = 0
                for d_img in detail_images:
                    if d_img in sub_set:
                        continue
                    parts.append(img_tag.format(url=d_img))
                    detail_emitted += 1
                # 폴백: detail에 1장도 안 들어갔으면 추가이미지(images[1:])로 채움
                # — detail_images 비어있거나 모두 sub_set와 중복인 경우 대비
                if detail_emitted == 0:
                    fallback_imgs = images[1:] or images[:1]
                    for s_img in fallback_imgs:
                        parts.append(img_tag.format(url=s_img))
                    if fallback_imgs:
                        logger.info(
                            f"[상세HTML] detail 비어있음 → 추가이미지 {len(fallback_imgs)}장 폴백"
                        )
            elif item_id == "bottomImg" and bottom_img:
                parts.append(img_tag.format(url=bottom_img))

        if not parts:
            return f"<p>{product.get('name', '')}</p>"

        return "\n".join(parts)

    # ==================== 카테고리 매핑 자동 조회 ====================

    async def _resolve_category_mappings(
        self,
        source_site: str,
        source_category: str,
        target_account_ids: list[str],
    ) -> dict[str, str]:
        """수집 상품의 소싱처 카테고리 → 각 마켓 카테고리 자동 매핑.

        카테고리매핑 페이지에서 설정한 DB 매핑만 사용. 없으면 해당 마켓 전송 제외.
        """
        from backend.domain.samba.category.repository import (
            SambaCategoryMappingRepository,
        )
        from backend.domain.samba.category.service import SambaCategoryService

        if not source_category:
            return {}

        # DB에서 매핑 조회
        mapping_repo = SambaCategoryMappingRepository(self.session)
        mapping = (
            await mapping_repo.find_mapping(source_site, source_category)
            if source_category
            else None
        )

        result: dict[str, str] = {}

        # 대상 계정의 마켓 타입 배치 조회 (N+1 → 1회)
        from sqlmodel import select as _sel_cat
        from backend.domain.samba.account.model import SambaMarketAccount as _SMA_cat

        _stmt_cat = _sel_cat(_SMA_cat).where(_SMA_cat.id.in_(target_account_ids))
        _res_cat = await self.session.execute(_stmt_cat)
        _cat_accounts = _res_cat.scalars().all()
        market_types = {a.market_type for a in _cat_accounts}

        # ssg 계정이 있으면 ssg_std 카테고리 매핑도 함께 조회
        mapping_market_types = set(market_types)
        if "ssg" in market_types:
            mapping_market_types.add("ssg_std")

        for market_type in mapping_market_types:
            # 카테고리매핑 페이지 설정만 사용
            if mapping and mapping.target_mappings:
                target = mapping.target_mappings.get(market_type, "")
                if target:
                    result[market_type] = target
                    continue

            # DB 매핑 없으면 해당 마켓은 스킵 (사용자가 직접 매핑한 것만 전송)
            logger.info(f"[카테고리] {market_type} DB 매핑 없음 — 전송 대상에서 제외")

        # 경로 문자열 → 숫자 코드 변환
        # ssg/ssg_std는 각각 cat2에 dispCtgId/stdCtgId 코드맵이 있으므로 함께 변환
        from backend.domain.samba.category.repository import SambaCategoryTreeRepository

        category_svc = SambaCategoryService(
            mapping_repo, SambaCategoryTreeRepository(self.session)
        )
        convert_markets = set(market_types) | ({"ssg_std"} if "ssg" in market_types else set())
        for market_type in convert_markets:
            if market_type in result:
                cat_path = result[market_type]
                if cat_path and not cat_path.isdigit():
                    code = await category_svc.resolve_category_code(
                        market_type, cat_path
                    )
                    if code:
                        logger.info(
                            "[카테고리 코드 변환] %s: '%s' → %s",
                            market_type,
                            cat_path,
                            code,
                        )
                        result[market_type] = code

        return result

    # ==================== 재전송 ====================

    async def retransmit(self, shipment_id: str) -> Optional[SambaShipment]:
        """실패한 계정에 대해 기존 shipment 레코드를 업데이트하며 재전송."""
        from backend.domain.samba.collector.repository import (
            SambaCollectedProductRepository,
        )
        from backend.domain.samba.shipment.dispatcher import dispatch_to_market

        shipment = await self.repo.get_async(shipment_id)
        if not shipment:
            return None

        old_result = shipment.transmit_result or {}
        old_errors = shipment.transmit_error or {}
        failed_accounts = [aid for aid, st in old_result.items() if st == "failed"]
        if not failed_accounts:
            return shipment

        # 상품 데이터 조회
        product_repo = SambaCollectedProductRepository(self.session)
        product_row = await product_repo.get_async(shipment.product_id)
        if not product_row:
            return shipment
        # OOM 방지: 전송에 불필요한 대용량 필드 제외
        product_dict = product_row.model_dump(exclude={"last_sent_data", "extra_data"})

        # 재전송
        await self.repo.update_async(shipment_id, status="transmitting")
        new_result = dict(old_result)
        new_errors = dict(old_errors)

        # 실패 계정 배치 조회 (N+1 → 1회)
        from sqlmodel import select as _sel_rt
        from backend.domain.samba.account.model import SambaMarketAccount as _SMA_rt

        _stmt_rt = _sel_rt(_SMA_rt).where(_SMA_rt.id.in_(failed_accounts))
        _res_rt = await self.session.execute(_stmt_rt)
        _rt_account_map = {a.id: a for a in _res_rt.scalars().all()}

        # 카테고리 매핑 재조회
        raw_category = product_row.category or ""

        # 검색필터명 조회 (플레이오토 임의분류용)
        if product_row.search_filter_id:
            from backend.domain.samba.collector.repository import (
                SambaSearchFilterRepository,
            )

            sf_repo = SambaSearchFilterRepository(self.session)
            sf = await sf_repo.get_async(product_row.search_filter_id)
            if sf and sf.name:
                product_dict["_search_filter_name"] = sf.name

        mapped_categories = await self._resolve_category_mappings(
            product_row.source_site or "",
            raw_category,
            failed_accounts,
        )

        for account_id in failed_accounts:
            try:
                account = _rt_account_map.get(account_id)
                if not account:
                    continue
                category_id = mapped_categories.get(account.market_type, "")
                # ESM Plus 크로스매핑: 지마켓↔옥션 자동 변환
                if not category_id and account.market_type in ("gmarket", "auction"):
                    other = "auction" if account.market_type == "gmarket" else "gmarket"
                    other_id = mapped_categories.get(other, "")
                    if other_id and str(other_id).isdigit():
                        from backend.domain.samba.proxy.esmplus import esm_map_category

                        category_id = esm_map_category(
                            other_id, other, account.market_type
                        )
                        if category_id:
                            logger.info(
                                f"[ESM 크로스매핑] {other}({other_id}) → {account.market_type}({category_id})"
                            )
                # 카페24/롯데홈쇼핑은 카테고리 매핑 없이 플러그인 내부에서 자동 처리
                if not category_id and account.market_type not in (
                    "playauto",
                    "cafe24",
                    "lottehome",
                ):
                    new_result[account_id] = "failed"
                    new_errors[account_id] = "카테고리 매핑 없음"
                    continue
                result = await dispatch_to_market(
                    self.session,
                    account.market_type,
                    product_dict,
                    category_id,
                    account=account,
                )
                if result.get("success"):
                    new_result[account_id] = "success"
                    new_errors.pop(account_id, None)
                else:
                    new_result[account_id] = "failed"
                    new_errors[account_id] = result.get("message", "")
            except Exception as exc:
                new_result[account_id] = "failed"
                new_errors[account_id] = str(exc)

        values = list(new_result.values())
        all_success = len(values) > 0 and all(v == "success" for v in values)
        all_failed = len(values) > 0 and all(v == "failed" for v in values)
        final_status = (
            "completed" if all_success else ("failed" if all_failed else "partial")
        )

        updated = await self.repo.update_async(
            shipment_id,
            status=final_status,
            transmit_result=new_result,
            transmit_error=new_errors if new_errors else None,
            completed_at=datetime.now(UTC),
        )
        return updated or shipment

    # ==================== 마켓 상품 삭제 ====================

    async def delete_from_markets(
        self,
        product_ids: list[str],
        target_account_ids: list[str],
        current_idx: int | None = None,
        total_count: int | None = None,
        log_to_buffer: bool = False,
        disconnect_checker: Any | None = None,
        on_progress: Any | None = None,
    ) -> dict[str, Any]:
        """선택된 상품을 대상 마켓에서 삭제.

        log_to_buffer=True: 상품전송삭제 페이지 링 버퍼에 로그 기록 (폴링으로 실시간 표시).
        False(기본): 상품관리 페이지에서 호출 시 — 모달이 자체 로그를 표시하므로 버퍼 불필요.
        """
        from backend.domain.samba.collector.repository import (
            SambaCollectedProductRepository,
        )
        from backend.domain.samba.shipment.dispatcher import delete_from_market

        if log_to_buffer:
            from backend.domain.samba.job.worker import _add_shipment_log
            from datetime import (
                datetime as _dt_del,
                timezone as _tz_del,
                timedelta as _td_del,
            )

            def _del_log(msg: str) -> None:
                kst = (_dt_del.now(_tz_del.utc) + _td_del(hours=9)).strftime("%H:%M:%S")
                _add_shipment_log(f"[{kst}] {msg}")
        else:

            def _del_log(msg: str) -> None:  # type: ignore[misc]
                pass

        # 인덱스 prefix — 프론트에서 [i/N] 전달 시 표시
        idx_prefix = (
            f"[{current_idx:,}/{total_count:,}] "
            if current_idx is not None and total_count is not None
            else ""
        )

        product_repo = SambaCollectedProductRepository(self.session)

        # 대상 계정 배치 조회 (N+1 → 1회)
        from sqlmodel import select as _sel_del
        from backend.domain.samba.account.model import SambaMarketAccount as _SMA_del

        _stmt_del = _sel_del(_SMA_del).where(_SMA_del.id.in_(target_account_ids))
        _res_del = await self.session.execute(_stmt_del)
        _del_account_map = {a.id: a for a in _res_del.scalars().all()}

        results: list[dict[str, Any]] = []

        for product_id in product_ids:
            # 강제 중단 체크
            if is_cancel_requested():
                logger.info(
                    f"[마켓삭제] 강제 중단 — {len(results)}건 완료, {len(product_ids) - len(results)}건 취소"
                )
                break
            product_row = await product_repo.get_async(product_id)
            if not product_row:
                results.append(
                    {"product_id": product_id, "status": "failed", "error": "상품 없음"}
                )
                continue

            # OOM 방지: 삭제에 불필요한 대용량 필드 제외
            product_dict = product_row.model_dump(
                exclude={"last_sent_data", "extra_data"}
            )
            market_product_nos = product_row.market_product_nos or {}
            reg_accounts = product_row.registered_accounts or []
            delete_results: dict[str, str] = {}

            for account_id in target_account_ids:
                if disconnect_checker is not None and await disconnect_checker():
                    logger.info("[마켓삭제] 클라이언트 연결 종료 감지 - 계정 삭제 중단")
                    break
                # 이 상품에 등록된 계정만 삭제 대상
                if account_id not in reg_accounts:
                    acc = _del_account_map.get(account_id)
                    if not acc:
                        continue
                    # PlayAuto: API 삭제 불가 — DB 불일치 상태여도 성공 처리하여 프론트 배지 제거
                    if acc.market_type == "playauto":
                        delete_results[account_id] = "success"
                        continue
                    # non-PlayAuto: 상품번호가 있으면 registered_accounts 불일치 상태 → 삭제 시도
                    has_product_no = bool(
                        market_product_nos.get(f"{account_id}_origin")
                        or market_product_nos.get(account_id)
                    )
                    if not has_product_no:
                        # 상품번호도 없음 → 이미 삭제된 상태로 간주, 배지 정리
                        delete_results[account_id] = "success"
                        continue
                    # 상품번호 있음 → 아래 삭제 로직 fall-through

                account = _del_account_map.get(account_id)
                if not account:
                    delete_results[account_id] = "계정 없음"
                    continue

                # 상품번호를 product_dict에 주입 (디스패처가 사용)
                # 스마트스토어: 삭제 API는 originProductNo 사용 (2143473a 전송경로 패치와 대칭)
                if account.market_type == "smartstore":
                    product_no = market_product_nos.get(f"{account_id}_origin", "")
                    if not product_no:
                        raw = market_product_nos.get(account_id, "")
                        if isinstance(raw, dict):
                            product_no = (
                                raw.get("originProductNo")
                                or raw.get("smartstoreChannelProductNo")
                                or raw.get("groupProductNo")
                                or ""
                            )
                        else:
                            product_no = raw
                    product_no = str(product_no) if product_no else ""
                else:
                    product_no = market_product_nos.get(account_id, "")
                product_dict["market_product_no"] = {account.market_type: product_no}

                result = await delete_from_market(
                    self.session,
                    account.market_type,
                    product_dict,
                    account=account,
                    market_delete=True,
                )
                # 429 방지 — 삭제 요청 간 0.5초 딜레이
                await asyncio.sleep(0.5)

                # 로그용 상품/계정 레이블
                src_tag = (
                    f"[{product_row.source_site}] " if product_row.source_site else ""
                )
                prod_name = (product_row.name or product_id)[:30]
                prod_no = str(product_row.site_product_id or product_id or "")
                prod_label = (
                    f"{prod_name} (상품번호: {prod_no})" if prod_no else prod_name
                )
                acc_label = f"{account.market_name}({account.seller_id or '-'})"

                if result.get("success"):
                    if result.get("soldout_fallback"):
                        # 주문 진행중 → 품절 처리 fallback (등록 상태 유지)
                        delete_results[account_id] = "soldout_fallback"
                        _del_log(
                            f"{idx_prefix}{src_tag}{prod_label} → {acc_label}: 품절 처리 완료"
                        )
                        logger.info(
                            f"[마켓삭제] {account.market_type} 품절 fallback - 상품: {product_id}"
                        )
                    else:
                        delete_results[account_id] = "success"
                        _del_log(
                            f"{idx_prefix}{src_tag}{prod_label} → {acc_label}: 삭제 성공"
                        )
                        logger.info(
                            f"[마켓삭제] {account.market_type} 성공 - 상품: {product_id}"
                        )
                else:
                    delete_results[account_id] = result.get("message", "실패")
                    _del_log(
                        f"{idx_prefix}{src_tag}{prod_label} → {acc_label}: {delete_results[account_id]}"
                    )
                    logger.warning(
                        f"[마켓삭제] {account.market_type} 실패 - {result.get('message')}"
                    )

            # 성공한 계정만 등록 해제 (soldout_fallback은 등록 상태 유지)
            success_ids = [
                aid for aid, status in delete_results.items() if status == "success"
            ]
            if success_ids:
                new_reg = [a for a in reg_accounts if a not in success_ids]
                remove_keys = set(success_ids)
                for aid in success_ids:
                    remove_keys.add(f"{aid}_origin")
                new_nos = {
                    k: v for k, v in market_product_nos.items() if k not in remove_keys
                }
                update_data: dict[str, Any] = {
                    "registered_accounts": new_reg if new_reg else None,
                    "market_product_nos": new_nos if new_nos else None,
                }
                if not new_reg:
                    update_data["status"] = "collected"
                await product_repo.update_async(product_id, **update_data)

            results.append(
                {
                    "product_id": product_id,
                    "delete_results": delete_results,
                    "success_count": len(
                        [v for v in delete_results.values() if v == "success"]
                    ),
                }
            )
            if on_progress:
                await on_progress(len(results), len(product_ids))

        return {
            "processed": len(results),
            "results": results,
        }

    async def delete_all_by_account(
        self,
        account_id: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """특정 마켓 계정에 등록된 전체 상품을 마켓에서 삭제.

        dry_run=True이면 삭제 대상 상품 수만 반환.
        """
        from sqlalchemy import cast
        from sqlalchemy.dialects.postgresql import JSONB as _JSONB
        from sqlmodel import select

        from backend.domain.samba.account.model import SambaMarketAccount
        from backend.domain.samba.collector.model import SambaCollectedProduct

        # 1) 계정 존재 확인
        account = await self.session.get(SambaMarketAccount, account_id)
        if not account:
            raise ValueError(f"계정을 찾을 수 없습니다: {account_id}")

        # 2) 해당 계정에 등록된 상품 ID 조회
        stmt = select(SambaCollectedProduct.id).where(
            SambaCollectedProduct.registered_accounts.op("@>")(
                cast(f'["{account_id}"]', _JSONB)
            )
        )
        result = await self.session.execute(stmt)
        product_ids = list(result.scalars().all())
        total_count = len(product_ids)

        # 3) dry_run이면 상품 수와 예상 시간만 반환
        if dry_run:
            return {
                "dry_run": True,
                "account_id": account_id,
                "account_label": account.account_label,
                "market_type": account.market_type,
                "total_products": total_count,
                "estimated_seconds": total_count * 0.5,
            }

        if total_count == 0:
            return {
                "account_id": account_id,
                "total_products": 0,
                "message": "삭제 대상 상품이 없습니다.",
            }

        # 4) 기존 delete_from_markets 재사용
        logger.info(
            f"[계정삭제] {account.account_label}({account.market_type}) "
            f"전체 {total_count}건 삭제 시작"
        )
        return await self.delete_from_markets(product_ids, [account_id])

    @staticmethod
    def get_status_label(status: str) -> str:
        return STATUS_LABELS.get(status, status)

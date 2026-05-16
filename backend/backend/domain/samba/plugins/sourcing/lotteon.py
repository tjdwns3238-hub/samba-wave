"""롯데ON 소싱처 플러그인."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from backend.domain.samba.plugins.sourcing_base import SourcingPlugin

if TYPE_CHECKING:
    from backend.domain.samba.collector.refresher import RefreshResult

logger = logging.getLogger(__name__)

# 롯데ON 소싱처 적응형 인터벌 상태
_lotteon_interval: float = 0.5  # 현재 인터벌 (초)
_lotteon_consecutive_errors: int = 0  # 연속 차단 횟수
_lotteon_safe_interval: float = 999.0  # 차단 없는 최소 인터벌 기록

# sitmNo 인메모리 캐시 — HTML 폴백 시 추출하여 다음 사이클부터 pbf 빠른경로 사용
# {site_product_id: sitmNo} 형태, 프로세스 수명 동안 유지
_sitm_no_cache: dict[str, str] = {}


def _select_lotteon_proxy() -> str | None:
    """현재 실행 컨텍스트에 따라 롯데ON 호출용 프록시 URL을 선택.

    - autotune 컨텍스트: _get_rotated_proxy("LOTTEON")로 IP 순환
    - 그 외(일반 refresh/수집): get_collect_proxy_url()로 collect 풀 사용

    DB 설정의 프록시 풀이 비어있으면 None(메인 IP)을 반환한다.
    롯데ON WAF는 데이터센터 IP에서 502/403 소프트 차단을 한다(2026-05-16 확인).
    """
    from backend.domain.samba.collector.refresher import (
        _current_refresh_source,
        _get_rotated_proxy,
        get_collect_proxy_url,
    )

    try:
        if _current_refresh_source.get() == "autotune":
            return _get_rotated_proxy(site="LOTTEON")
    except LookupError:
        pass
    return get_collect_proxy_url()


# ── Phase 4 helpers — DOM 재고 병합 + 상품명 정합성 검증 (설계문서 §3.5/§12) ──


def _norm_opt_name(s: str) -> str:
    """옵션명 정규화 — 공백 제거 + 소문자."""
    return "".join(str(s or "").split()).lower()


def _merge_dom_stock(pbf_options: list[dict], dom_options: list[dict]) -> int:
    """DOM 사이즈별 재고를 pbf 옵션 리스트에 주입 (in-place). 변경 건수 반환.

    규칙:
      - DOM isSoldOut=True → pbf stock=0 + isSoldOut=True (품절 확정)
      - DOM stock=정수 → pbf stock 덮어쓰기 (실재고)
      - DOM stock=None (UI에 숫자 미노출, 충분 재고 추정) → pbf 값 유지
      - 매칭 실패 (옵션명 정규화 후 다름) → pbf 값 유지

    단위 테스트: backend/_tmp_lotteon_dom_merge_test.py — 12/12 PASS (2026-04-23).
    """
    if not pbf_options or not dom_options:
        return 0
    dom_map = {_norm_opt_name(o.get("name")): o for o in dom_options if o.get("name")}
    changes = 0
    for pbf in pbf_options:
        key = _norm_opt_name(pbf.get("name"))
        dom = dom_map.get(key)
        if not dom:
            continue
        if dom.get("isSoldOut"):
            if pbf.get("stock") != 0 or not pbf.get("isSoldOut"):
                pbf["stock"] = 0
                pbf["isSoldOut"] = True
                changes += 1
        else:
            ds = dom.get("stock")
            if isinstance(ds, int) and ds != pbf.get("stock"):
                pbf["stock"] = ds
                pbf["isSoldOut"] = False
                changes += 1
    return changes


def _check_name_mismatch(
    site_product_id: str, db_name: str, dom_title: str | None
) -> None:
    """§12 방어 로깅 — DOM이 다른 상품을 긁었을 가능성 조기 감지.

    DB 상품명과 DOM pageTitle의 공통 문자 비율이 30% 미만이면 WARNING 로그.
    호출자는 이 경우에도 pbf 값 그대로 사용 (추가 차단은 하지 않음 — 관측부터).
    """
    if not db_name or not dom_title:
        return
    db_n = _norm_opt_name(db_name)
    dom_n = _norm_opt_name(dom_title)
    if not db_n or not dom_n:
        return
    common = len(set(db_n) & set(dom_n))
    total = max(len(set(db_n) | set(dom_n)), 1)
    ratio = common / total
    if ratio < 0.3:
        logger.warning(
            f"[LOTTEON][name-mismatch] id={site_product_id} "
            f"db={db_name!r} dom={dom_title!r} similarity={ratio:.2f}"
        )


def _safe_stock(v) -> int:
    try:
        if isinstance(v, str):
            v = v.replace(",", "").strip()
        return int(v or 0)
    except (ValueError, TypeError):
        return 0


def _is_soldout_flag(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "y", "yes", "1", "sold_out")
    return bool(v)


class LotteonSourcingPlugin(SourcingPlugin):
    """롯데ON 소싱처 플러그인.

    JSON-LD(schema.org Product) 마크업을 우선 파싱하여 정확도가 높다.
    bestBenefitPrice(최대혜택가)를 new_cost에 반영하여
    정책 적용 시 실질 매입가 기준으로 마진 계산이 가능하다.

    concurrency=4: pbf API는 부하가 적어 동시 4건 처리
    request_interval=0.3: 요청 간 300ms 딜레이 (pbf 빠른경로 기준)
    """

    site_name = "LOTTEON"
    concurrency = 4
    request_interval = 0.3

    async def search(self, keyword: str, **filters) -> list[dict]:
        """롯데ON 키워드 검색."""
        from backend.domain.samba.proxy.lotteon_sourcing import LotteonSourcingClient

        client = LotteonSourcingClient(proxy_url=_select_lotteon_proxy())
        page = filters.get("page", 1)
        size = filters.get("size", 40)
        return await self.safe_call(
            client.search_products(keyword, page=page, size=size, **filters)
        )

    async def get_detail(self, site_product_id: str) -> dict:
        """롯데ON 상품 상세 조회."""
        from backend.domain.samba.proxy.lotteon_sourcing import LotteonSourcingClient

        client = LotteonSourcingClient(proxy_url=_select_lotteon_proxy())
        return await self.safe_call(client.get_product_detail(site_product_id))

    async def scan_categories(
        self,
        keyword: str,
        *,
        selected_brands: list[str] | None = None,
        max_scan: int = 20,
    ) -> dict:
        """롯데ON 카테고리 스캔 — 검색 결과에서 카테고리 분포 집계.

        safe_call() 미사용: scan_categories() 내부에서 asyncio.Semaphore(3)으로
        직접 동시성을 제어하므로 외부 세마포어 래핑이 불필요하다.
        """
        from backend.domain.samba.proxy.lotteon_sourcing import LotteonSourcingClient

        client = LotteonSourcingClient(proxy_url=_select_lotteon_proxy())
        return await client.scan_categories(
            keyword, selected_brands=selected_brands, max_scan=max_scan
        )

    async def discover_brands(self, keyword: str) -> dict:
        """롯데ON 키워드 검색 → 발견된 브랜드 목록 반환 (사용자 선택용)."""
        from backend.domain.samba.proxy.lotteon_sourcing import LotteonSourcingClient

        client = LotteonSourcingClient(proxy_url=_select_lotteon_proxy())
        return await client.discover_brands(keyword)

    async def _fetch_pbf_refresh(self, sitm_no: str) -> dict:
        """pbf API 직접 호출로 refresh용 데이터 취득 (HTML 파싱 스킵).

        Args:
          sitm_no: 롯데ON sitmNo (LE1220156946_1321122096 형태)

        Returns:
          refresh용 detail dict (빈 dict이면 실패)
        """
        from backend.domain.samba.proxy.lotteon_sourcing import LotteonSourcingClient

        client = LotteonSourcingClient(proxy_url=_select_lotteon_proxy())
        pbf = await client.fetch_pbf_standalone(sitm_no)
        if not pbf:
            return {}
        detail = self._parse_pbf_to_detail(pbf)

        # spdNo 추출 (sitmNo: LE1220771485_1325086305 → spdNo: LE1220771485)
        _spd = sitm_no.split("_")[0] if "_" in sitm_no else sitm_no

        # 최대혜택가는 확장앱 DOM에서만 수집 — benefits API 사용 안 함

        opt_stock = await client.fetch_option_stock(pbf, spd_no=_spd, sitm_no=sitm_no)
        if opt_stock:
            detail["options"] = opt_stock
            detail["_option_stock_live"] = True

        # 옵션 가격을 판매가로 보정 (sl_prc 정가 대신)
        _eff_price = detail.get("salePrice") or 0
        if _eff_price > 0 and detail.get("options"):
            for _opt in detail["options"]:
                _opt["price"] = _eff_price

        return detail

    def _parse_pbf_to_detail(self, pbf: dict) -> dict:
        """pbf API 응답 → refresh용 detail dict 변환.

        get_product_detail()이 반환하는 dict와 동일한 키 구조로 변환한다.
        """
        price_info = pbf.get("priceInfo") or {}
        sl_prc = int(price_info.get("slPrc", 0) or 0)
        immd_dc = int(price_info.get("immdDcAplyTotAmt", 0) or 0)
        # adtnDcAplyTotAmt(추가할인)는 판매가 반영 대상 아님.
        # 롯데ON은 "즉시할인(immdDcAplyTotAmt)"만 판매가에 반영하고,
        # "추가할인"은 결제 시 부가 혜택으로 노출되므로 bestBenefitPrice 계산에서 제외.
        # 2026-04-23 S1 검증: 5개 롯데백화점 상품 모두 실노출가 == slPrc - immd_dc.
        # 과거 `sl_prc - immd - adtn` 계산이 실노출가보다 낮은 값(예: PD56368597
        # 59,000 - 8,850 - 5,010 = 45,140)을 만들어 50,150↔45,140 가격 핑퐁을 유발했음.

        if immd_dc > 0:
            # PBF에 즉시할인 있음 → 즉시할인 차감만 수행
            best_benefit = sl_prc - immd_dc if sl_prc > 0 else 0
            if best_benefit <= 0 or best_benefit >= sl_prc:
                best_benefit = sl_prc
        else:
            # PBF에 즉시할인 정보 없음 → slPrc가 정상가일 수 있어
            # bestBenefitPrice를 None으로 설정하여 HTML 폴백 유도
            best_benefit = None

        # 재고
        stck = pbf.get("stckInfo") or {}
        stk_qty_raw = stck.get("stkQty")
        stk_qty = _safe_stock(stk_qty_raw)
        is_out = stk_qty_raw is not None and stk_qty <= 0

        # 옵션
        opt_info = pbf.get("optionInfo") or {}
        option_groups = opt_info.get("optionList") or []
        options: list[dict] = []
        if option_groups:
            primary = option_groups[0]
            for opt in primary.get("options", []):
                label = opt.get("label", "").strip()
                if not label:
                    continue
                disabled = bool(opt.get("disabled", False))
                options.append(
                    {
                        "name": label,
                        "price": sl_prc,
                        "stock": 0 if disabled else (stk_qty or 99),
                        "isSoldOut": disabled,
                    }
                )
            if len(option_groups) >= 2:
                options = []
                for g1 in option_groups[0].get("options", []):
                    for g2 in option_groups[1].get("options", []):
                        dis = g1.get("disabled", False) or g2.get("disabled", False)
                        label = f"{g1.get('label', '')} / {g2.get('label', '')}".strip(
                            " /"
                        )
                        options.append(
                            {
                                "name": label,
                                "price": sl_prc,
                                "stock": 0 if dis else (stk_qty or 99),
                                "isSoldOut": bool(dis),
                            }
                        )

        return {
            "salePrice": sl_prc,
            "bestBenefitPrice": best_benefit,
            "isOutOfStock": is_out,
            "isSoldOut": is_out,
            "saleStatus": "sold_out" if is_out else "in_stock",
            "options": options,
        }

    async def refresh(self, product) -> "RefreshResult":
        """가격/재고 갱신 — sitmNo 있으면 pbf 직접, 없으면 상세 페이지 재조회.

        무신사 수준의 에러 처리 및 적응형 인터벌 조정을 포함한다:
        - 45초 타임아웃
        - RateLimitError 차단 감지 → 인터벌 2배 증가 (최대 30초)
        - 연속 5회 차단 시 전체 중단
        - retry_after 있으면 대기 후 1회 재시도
        - 성공 시 인터벌 점진 복원
        - 가격/재고 상태 변동 판정
        """
        global _lotteon_interval, _lotteon_consecutive_errors, _lotteon_safe_interval

        from backend.domain.samba.collector.refresher import (
            RefreshResult,
            _log_refresh,
            _current_refresh_source,
        )
        from backend.domain.samba.proxy.lotteon_sourcing import (
            LotteonSourcingClient,
            RateLimitError,
        )

        _idx = getattr(product, "_refresh_idx", 0)
        _total = getattr(product, "_refresh_total", 0)
        # wrapper 잔여 예산 계산용 시작 시각
        _started_at = time.monotonic()

        product_id = getattr(product, "id", "")
        site_product_id = getattr(product, "site_product_id", "") or getattr(
            product, "siteProductId", ""
        )

        if not site_product_id:
            return RefreshResult(
                product_id=product_id,
                error="롯데ON 상품 ID 없음",
            )

        client = LotteonSourcingClient(proxy_url=_select_lotteon_proxy())
        detail = None

        # sitmNo 빠른경로: product 객체 → 인메모리 캐시 순서로 조회
        # extra_data는 autotune에서 defer() 처리되어 접근 불가 (greenlet 에러)
        # 인메모리 캐시만 사용
        sitm_no = (
            getattr(product, "sitmNo", "")
            or getattr(product, "sitm_no", "")
            or _sitm_no_cache.get(site_product_id, "")
        )

        try:
            if sitm_no:
                # HTML 파싱 없이 pbf API 직접 호출
                raw = await asyncio.wait_for(
                    self._fetch_pbf_refresh(sitm_no),
                    timeout=20,
                )
                if raw:
                    # PBF에서 할인 정보를 못 가져온 경우(bestBenefitPrice=None)
                    # HTML 상세 페이지로 폴백하여 정확한 최대혜택가 확인
                    if raw.get("bestBenefitPrice") is None:
                        logger.debug(
                            f"[LOTTEON] PBF 할인 미반영, HTML 폴백: {site_product_id}"
                        )
                        html_detail = await asyncio.wait_for(
                            client.get_product_detail(site_product_id),
                            timeout=45,
                        )
                        if html_detail:
                            # HTML에서 재고/옵션은 PBF가 더 정확하므로 병합
                            for k in (
                                "isOutOfStock",
                                "isSoldOut",
                                "saleStatus",
                                "options",
                                "_option_stock_live",
                            ):
                                if k in raw and raw[k] is not None:
                                    html_detail[k] = raw[k]
                            detail = html_detail
                        else:
                            detail = raw
                    else:
                        detail = raw
                    logger.debug(
                        f"[LOTTEON] refresh 빠른경로 성공: {site_product_id} (sitmNo={sitm_no})"
                    )
                else:
                    # pbf 실패 → 기존 방식 폴백
                    logger.debug(
                        f"[LOTTEON] pbf 빠른경로 실패, 폴백: {site_product_id}"
                    )
                    detail = await asyncio.wait_for(
                        client.get_product_detail(site_product_id),
                        timeout=45,
                    )
            else:
                detail = await asyncio.wait_for(
                    client.get_product_detail(site_product_id),
                    timeout=45,
                )
                # HTML 폴백 성공 시 sitmNo 추출 → 인메모리 캐시 저장
                # 다음 사이클부터 pbf 빠른경로 사용
                if detail:
                    _extracted_sitm = detail.get("sitmNo", "")
                    if _extracted_sitm and site_product_id:
                        _sitm_no_cache[site_product_id] = _extracted_sitm
                        logger.info(
                            f"[LOTTEON] sitmNo 캐시 저장: {site_product_id} → {_extracted_sitm}"
                        )
                        # sitmNo 확보 → pbf API로 옵션별 실재고 + 혜택가 보강
                        try:
                            _pbf_enrich = await asyncio.wait_for(
                                self._fetch_pbf_refresh(_extracted_sitm),
                                timeout=15,
                            )
                            if _pbf_enrich:
                                if _pbf_enrich.get("options"):
                                    detail["options"] = _pbf_enrich["options"]
                                if _pbf_enrich.get("_option_stock_live"):
                                    detail["_option_stock_live"] = True
                                _bbp = _pbf_enrich.get("bestBenefitPrice")
                                if _bbp and _bbp > 0:
                                    detail["bestBenefitPrice"] = _bbp
                                logger.info(
                                    f"[LOTTEON] HTML→pbf 보강: {site_product_id} "
                                    f"옵션={len(_pbf_enrich.get('options', []))}개, "
                                    f"혜택가={_bbp}"
                                )
                        except Exception as _pe:
                            logger.debug(
                                f"[LOTTEON] HTML→pbf 보강 실패: {site_product_id} — {_pe}"
                            )
            # 성공 → 인터벌 점진 복원 (최소 0.3초까지)
            _lotteon_interval = max(0.3, _lotteon_interval - 0.3)
            _lotteon_consecutive_errors = 0
            if _lotteon_interval <= _lotteon_safe_interval:
                _lotteon_safe_interval = _lotteon_interval

        except RateLimitError as e:
            # 차단 → 인터벌 2배 증가 (최대 30초)
            _lotteon_interval = min(30.0, _lotteon_interval * 2)
            _lotteon_consecutive_errors += 1
            _log_refresh(
                "LOTTEON",
                product_id,
                getattr(product, "name", ""),
                f"차단 HTTP {e.status} (연속 {_lotteon_consecutive_errors}회, 인터벌→{_lotteon_interval:.1f}s)",
                level="warning",
                idx=_idx,
                total=_total,
            )

            # 연속 5회 이상이면 해당 소싱처 전체 일시 중단
            if _lotteon_consecutive_errors >= 5:
                _log_refresh(
                    "LOTTEON",
                    product_id,
                    getattr(product, "name", ""),
                    f"연속 {_lotteon_consecutive_errors}회 차단 — 일시 중단",
                    level="error",
                    idx=_idx,
                    total=_total,
                )
                return RefreshResult(
                    product_id=product_id,
                    error=f"차단 감지: HTTP {e.status} (연속 {_lotteon_consecutive_errors}회, "
                    f"인터벌 {_lotteon_interval}초)",
                )

            # retry_after 있으면 대기 후 1회 재시도
            if e.retry_after > 0:
                logger.warning(
                    f"[LOTTEON] {site_product_id} 차단({e.status}), {e.retry_after}초 후 재시도"
                )
                await asyncio.sleep(e.retry_after)
                try:
                    detail = await client.get_product_detail(site_product_id)
                    _lotteon_consecutive_errors = 0
                    _log_refresh(
                        "LOTTEON",
                        product_id,
                        getattr(product, "name", ""),
                        f"재시도 성공 (대기 {e.retry_after}s 후)",
                        idx=_idx,
                        total=_total,
                    )
                except Exception:
                    _log_refresh(
                        "LOTTEON",
                        product_id,
                        getattr(product, "name", ""),
                        f"재시도 실패: HTTP {e.status}",
                        level="error",
                        idx=_idx,
                        total=_total,
                    )
                    return RefreshResult(
                        product_id=product_id,
                        error=f"차단 후 재시도 실패: HTTP {e.status}",
                    )
            else:
                return RefreshResult(
                    product_id=product_id, error=f"차단: HTTP {e.status}"
                )

        except asyncio.TimeoutError:
            # 45초 안에 응답 없음 → 건너뛰기
            _log_refresh(
                "LOTTEON",
                product_id,
                getattr(product, "name", ""),
                "응답 없음 (45초 타임아웃) — 건너뜀",
                level="warning",
                idx=_idx,
                total=_total,
            )
            return RefreshResult(
                product_id=product_id, error="응답 없음: 45초 타임아웃"
            )

        except Exception as e:
            logger.error(f"[LOTTEON] 갱신 실패: {site_product_id} — {e}")
            _log_refresh(
                "LOTTEON",
                product_id,
                getattr(product, "name", ""),
                f"실패 — {e}",
                level="error",
                idx=_idx,
                total=_total,
            )
            return RefreshResult(product_id=product_id, error=f"롯데ON API 오류: {e}")

        if not detail:
            return RefreshResult(
                product_id=product_id,
                error=f"롯데ON 상세 조회 실패: {site_product_id}",
            )

        # ── DOM 재고 병합 (설계문서 §3.5) — 지점 단위 pbf 재고 이슈 해소 ──
        # 확장앱이 롯데ON PDP를 열어 사이즈별 실재고(판매자 지점 기준)를 추출해
        # pbf 옵션 리스트의 stock을 덮어쓴다. 미연결/타임아웃/파싱 실패 시 pbf 값 유지.
        # ── DOM 위임 필수 — 최대혜택가/실재고는 DOM에서만 수집 가능 ──
        dom_ext: dict | None = None
        from backend.domain.samba.proxy.sourcing_queue import SourcingQueue

        try:
            _dom_req, _dom_fut = SourcingQueue.add_detail_job(
                "LOTTEON", site_product_id
            )
            dom_ext = await asyncio.wait_for(_dom_fut, timeout=60)
            if isinstance(dom_ext, dict) and dom_ext.get("login_required"):
                _reason = (
                    "창 미오픈"
                    if dom_ext.get("gate_blocked")
                    else "비로그인 확정(#memInfo 없음)"
                )
                logger.warning(f"[LOTTEON] {_reason} → 갱신 차단: {site_product_id}")
                return RefreshResult(
                    product_id=product_id,
                    error=f"LOTTEON {_reason} — 갱신 차단",
                )
            # 매장픽업 전용 상품 — 배송 불가, 수집 부적합 → 마켓 자동 삭제 처리
            # 사용자 검증(LE1216449916): "매장픽업 전용 롯데백화점" 표기, 배송비 0,
            # 일반 배송으로 위장 수집 시 cost에 임의 배송비 가산 + 주문 받아도 발송 불가.
            if isinstance(dom_ext, dict) and dom_ext.get("store_pickup_only"):
                logger.warning(
                    f"[LOTTEON] 매장픽업 전용 상품 감지 → 마켓 삭제 처리: {site_product_id}"
                )
                return RefreshResult(
                    product_id=product_id,
                    new_sale_status="sold_out",
                    changed=True,
                    deleted_from_source=True,
                )
            if not (isinstance(dom_ext, dict) and dom_ext.get("success")):
                dom_ext = None
        except asyncio.TimeoutError:
            logger.warning(
                f"[LOTTEON] 확장앱 미응답(60s) → 갱신 차단: {site_product_id}"
            )
            return RefreshResult(
                product_id=product_id,
                error="LOTTEON 확장앱 미응답 (60s 타임아웃) — 갱신 차단",
            )
        except Exception as _dom_err:
            logger.debug(f"[LOTTEON] DOM 위임 예외: {site_product_id} — {_dom_err}")

        if dom_ext and dom_ext.get("options") and detail.get("options"):
            _changes = _merge_dom_stock(detail["options"], dom_ext["options"])
            if _changes:
                logger.info(
                    f"[LOTTEON] DOM 재고 병합: {site_product_id} {_changes}건 덮어씀 "
                    f"(판매자={dom_ext.get('seller') or '-'})"
                )
            if any(_safe_stock(o.get("stock")) > 0 for o in detail["options"]):
                detail["_option_stock_live"] = True

        # DOM에서 직접 파싱한 "나의 혜택가" — 유일한 혜택가 출처
        if dom_ext:
            _dom_benefit = dom_ext.get("best_benefit_price") or 0
            if _dom_benefit > 0:
                detail["bestBenefitPrice"] = _dom_benefit
                logger.info(
                    f"[LOTTEON] DOM 혜택가 적용: {site_product_id} → {_dom_benefit:,}원"
                )

        if dom_ext:
            # §12 방어 로깅 — DOM이 다른 상품 긁었을 가능성 조기 감지
            _check_name_mismatch(
                site_product_id,
                db_name=getattr(product, "name", "") or "",
                dom_title=dom_ext.get("pageTitle"),
            )

        # ── qapi 프로모션가 보정 ──
        # pbf API의 slPrc는 정가(할인 전)를 반환하므로,
        # qapi 검색의 priceInfo[type=final]로 실제 프로모션가를 조회하여 보정
        _pbf_sale = detail.get("salePrice") or 0
        _name = getattr(product, "name", "") or ""
        try:
            from backend.domain.samba.proxy.lotteon_sourcing import (
                LotteonSourcingClient as _QClient,
            )

            _qapi_price = await _QClient().fetch_qapi_price(site_product_id)
            if _qapi_price:
                _final = _qapi_price.get("final", 0)
                _original = _qapi_price.get("original", 0)
                if _final > 0 and _final < _pbf_sale:
                    detail["salePrice"] = _final
                    if _original > 0:
                        detail["originalPrice"] = _original
                    logger.info(
                        f"[LOTTEON] qapi 프로모션가 보정: {site_product_id} "
                        f"pbf={_pbf_sale:,} → final={_final:,}"
                    )
        except Exception as e:
            logger.debug(
                f"[LOTTEON] qapi 프로모션가 조회 실패: {site_product_id} — {e}"
            )

        # ── 옵션 가격 보정 (sl_prc 정가 대신 실제 판매가/혜택가 사용) ──
        _effective = detail.get("bestBenefitPrice") or detail.get("salePrice") or 0
        if _effective > 0 and detail.get("options"):
            for _opt in detail["options"]:
                _opt["price"] = _effective

        # ── 데이터 추출 ──
        new_sale_price = detail.get("salePrice") or 0
        new_original_price = detail.get("originalPrice") or 0
        best_benefit_price = detail.get("bestBenefitPrice")
        if best_benefit_price is not None and best_benefit_price <= 0:
            best_benefit_price = None

        is_sold_out = detail.get("isOutOfStock", False) or detail.get(
            "isSoldOut", False
        )

        # 옵션 데이터 변환
        new_options = None
        raw_options = detail.get("options") or []
        if raw_options:
            new_options = [
                {
                    "name": opt.get("name", ""),
                    "price": opt.get("price", 0),
                    "stock": 0
                    if _is_soldout_flag(opt.get("isSoldOut"))
                    else opt.get("stock", 1),
                    "isSoldOut": _is_soldout_flag(opt.get("isSoldOut")),
                }
                for opt in raw_options
            ]

        # 역검: stckInfo 품절이나 옵션 실재고 있으면 in_stock 복원
        if is_sold_out and raw_options and detail.get("_option_stock_live"):
            if any(
                _safe_stock(o.get("stock")) > 0
                and not _is_soldout_flag(o.get("isSoldOut"))
                for o in raw_options
            ):
                is_sold_out = False
                _in_stock_cnt = sum(
                    1 for o in raw_options if _safe_stock(o.get("stock")) > 0
                )
                logger.info(
                    f"[LOTTEON] stckInfo 품절이나 옵션 실재고 존재 → in_stock 복원: "
                    f"{site_product_id} (재고옵션 {_in_stock_cnt}/{len(raw_options)}개)"
                )

        # 전 옵션 품절 → sold_out 승격
        # 롯데ON pbf API는 상품 overall isOutOfStock을 정확히 반환하지 않아
        # 옵션 단위 disabled/stock 플래그로만 판단 가능한 경우가 있음
        if not is_sold_out and raw_options:
            _all_opts_sold = all(
                _safe_stock(o.get("stock")) <= 0 or _is_soldout_flag(o.get("isSoldOut"))
                for o in raw_options
            )
            if _all_opts_sold:
                is_sold_out = True
                logger.info(
                    f"[LOTTEON] 전 옵션 품절 → sold_out 승격: {site_product_id} "
                    f"({len(raw_options)}개 옵션)"
                )

        new_sale_status = "sold_out" if is_sold_out else "in_stock"

        # ── 변동 판정 ──
        old_sale = getattr(product, "sale_price", 0) or 0
        old_status = getattr(product, "sale_status", "in_stock")
        changed = new_sale_price != old_sale or new_sale_status != old_status

        # 옵션 재고 변동 건수 — 품절↔재고 전환(무↔유)만 카운트 (단순 수량변화 제외)
        from backend.domain.samba.collector.refresher import count_stock_transitions

        old_options = getattr(product, "options", None) or []
        _stock_changes = count_stock_transitions(old_options, new_options)

        # ── 갱신 로그 (오토튠 컨텍스트에서는 콜백이 담당 → 스킵) ──
        if _current_refresh_source.get() != "autotune":
            _name = getattr(product, "name", "") or ""
            _prod_label = f"{_name} ({site_product_id})" if site_product_id else _name
            _status_label = "전송" if (changed or _stock_changes > 0) else "스킵"
            _log_refresh(
                "LOTTEON",
                product_id,
                _prod_label,
                f"{_status_label} [원가 {int(old_sale):,}→{int(new_sale_price):,}, "
                f"상태 {old_status}→{new_sale_status}, 재고변동 {_stock_changes}건]",
                idx=_idx,
                total=_total,
            )

        return RefreshResult(
            product_id=product_id,
            new_sale_price=float(new_sale_price) if new_sale_price else None,
            new_original_price=float(new_original_price)
            if new_original_price
            else None,
            new_cost=float(best_benefit_price) if best_benefit_price else None,
            new_sale_status=new_sale_status,
            new_options=new_options,
            changed=changed,
            stock_changed=_stock_changes > 0,
            price_uncertain=bool(detail.get("price_uncertain")),
        )

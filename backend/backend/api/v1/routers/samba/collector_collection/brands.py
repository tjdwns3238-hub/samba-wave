"""브랜드 관련 엔드포인트 — brand_refresh, brand_discover, brand_scan, brand_create_groups."""

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_read_session_dependency, get_write_session_dependency
from backend.domain.samba.cache import cache
from backend.domain.samba.collector.model import FIXED_REQUESTED_COUNT

from backend.api.v1.routers.samba.collector_common import (
    _get_services,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["samba-collector"])


# ── DTOs ──


class BrandRefreshRequest(BaseModel):
    brand: str
    brand_name: str = ""
    gf: str = "A"
    options: dict = {}
    source_site: str = "MUSINSA"
    categories: list[str] = []  # 빈 리스트=전체, 값 있으면 해당 카테고리만 처리


class BrandScanRequest(BaseModel):
    brand: str = ""
    gf: str = "A"
    keyword: str = ""
    source_site: str = "MUSINSA"
    selected_brands: list[str] = []
    brand_ids: list[str] = []  # SSG repBrandId 리스트
    brand_total: int = 0  # 선택된 브랜드 총 상품수 (비례 스케일링용)
    options: dict = {}


class BrandDiscoverRequest(BaseModel):
    keyword: str = ""
    source_site: str = "LOTTEON"


class BrandCreateGroupsRequest(BaseModel):
    brand: str = ""
    brand_name: str = ""
    gf: str = "A"
    categories: list[dict] = []
    requested_count_per_group: int = FIXED_REQUESTED_COUNT
    applied_policy_id: Optional[str] = None
    options: dict = {}
    source_site: str = "MUSINSA"
    selected_brands: list[str] = []
    brand_ids: list[str] = []  # SSG repBrandId 리스트


class BrandCollectAllRequest(BaseModel):
    """무신사 브랜드 전체수집 — 단일 Job으로 전상품 수집 후 카테고리별 배분."""

    filter_ids: list[str]  # 대상 SearchFilter ID 목록 (카테고리별)
    source_site: str = "MUSINSA"
    keyword: str  # 브랜드 검색 키워드 (예: 에잇세컨즈)
    brand: str  # 무신사 브랜드 코드 (예: 8seconds)
    gf: str = "A"
    exclude_preorder: bool = True
    exclude_boutique: bool = True
    use_max_discount: bool = False
    include_sold_out: bool = False


# ── 무신사 카테고리 스캔 내부 헬퍼 ──


async def _scan_musinsa_categories(
    keyword: str, brand: str = "", gf: str = "A", cookie: str = ""
) -> dict:
    """무신사 카테고리 스캔 — 검색 결과 상위 20개 상품 상세 조회 후 카테고리 분포 집계."""
    from backend.domain.samba.proxy.musinsa import MusinsaClient

    client = MusinsaClient(cookie=cookie)
    search_result = await client.search_products(keyword, size=20, brand=brand, gf=gf)
    products = search_result.get("data", [])
    if not products:
        return {"categories": [], "total": 0, "groupCount": 0}

    # 동시성 3개로 상세 조회
    sem = asyncio.Semaphore(3)
    cat_counter: dict[str, int] = {}

    async def _fetch(p: dict) -> None:
        async with sem:
            spid = p.get("siteProductId") or p.get("site_product_id") or ""
            if not spid:
                return
            try:
                detail = await client.get_goods_detail(spid)
                c1 = detail.get("category1", "")
                c2 = detail.get("category2", "")
                c3 = detail.get("category3", "")
                if not c1:
                    return
                parts = [c for c in [c1, c2, c3] if c]
                path = " > ".join(parts)
                # category code는 무신사 categoryCode 필드
                code = detail.get("categoryCode", c3 or c2 or c1)
                key = f"{code}||{path}||{c1}||{c2}||{c3}"
                cat_counter[key] = cat_counter.get(key, 0) + 1
            except Exception:
                pass

    await asyncio.gather(*[_fetch(p) for p in products], return_exceptions=True)

    categories = []
    for key, count in sorted(cat_counter.items(), key=lambda x: -x[1]):
        code, path, c1, c2, c3 = key.split("||")
        categories.append(
            {
                "categoryCode": code,
                "path": path,
                "count": count,
                "category1": c1,
                "category2": c2,
                "category3": c3,
            }
        )

    return {
        "categories": categories,
        "total": sum(c["count"] for c in categories),
        "groupCount": len(categories),
    }


# ── 엔드포인트 ──


@router.post("/brand-refresh")
async def brand_refresh(
    req: BrandRefreshRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """브랜드 추가수집 — 신규 카테고리 그룹 생성 + 기존 그룹 요청수 갱신.

    지원 소싱처: MUSINSA, Nike, ABCmart, GrandStage, LOTTEON, GSShop, KREAM
    """
    from urllib.parse import urlencode, urlparse, parse_qs, quote as _quote
    import re as _re

    svc = _get_services(session)
    site = req.source_site
    keyword = req.brand_name or req.brand

    # 1) 카테고리 스캔 — 소싱처별 분기
    try:
        _SCAN_SUPPORTED = {
            "MUSINSA",
            "Nike",
            "ABCmart",
            "GrandStage",
            "LOTTEON",
            "GSShop",
            "KREAM",
        }
        if site not in _SCAN_SUPPORTED:
            raise HTTPException(
                400, f"{site}은(는) 추가수집(카테고리 스캔)을 지원하지 않습니다"
            )

        if site == "Nike":
            from backend.domain.samba.plugins.sourcing.nike import NikePlugin

            scan_result = await NikePlugin().scan_categories(keyword)
            categories = scan_result.get("categories", [])
        elif site in ("ABCmart", "GrandStage"):
            from backend.domain.samba.plugins.sourcing.abcmart import AbcMartPlugin

            scan_result = await AbcMartPlugin().scan_categories(keyword)
            categories = scan_result.get("categories", [])
        elif site == "GSShop":
            from backend.domain.samba.plugins.sourcing.gsshop import (
                GsShopSourcingPlugin,
            )

            scan_result = await GsShopSourcingPlugin().scan_categories(keyword)
            categories = scan_result.get("categories", [])
        elif site == "LOTTEON":
            from backend.domain.samba.plugins.sourcing.lotteon import (
                LotteonSourcingPlugin,
            )

            selected = [keyword]
            scan_result = await LotteonSourcingPlugin().scan_categories(
                keyword, selected_brands=selected
            )
            categories = scan_result.get("categories", [])
        elif site == "KREAM":
            from backend.domain.samba.plugins.sourcing.kream import KreamPlugin

            scan_result = await KreamPlugin().scan_categories(keyword)
            categories = scan_result.get("categories", [])
        else:
            # MUSINSA — brand-scan과 동일한 필터 API 재귀 탐색 방식 사용 (전체 카테고리)
            from backend.domain.samba.proxy.musinsa import MusinsaClient

            client = MusinsaClient()
            categories = await client.scan_brand_categories(
                brand=req.brand,
                gf=req.gf,
                keyword=keyword,
            )
    except Exception as e:
        raise HTTPException(500, f"카테고리 스캔 실패: {e}")

    # 1-b) 선택된 카테고리만 필터링
    if req.categories:
        allowed = set(req.categories)
        categories = [c for c in categories if c.get("categoryCode", "") in allowed]

    # 2) 기존 그룹 조회 — source_site + category_filter로 매칭
    all_filters = await svc.list_filters(limit=10000)
    existing_cat_codes: dict[str, Any] = {}  # categoryCode → filter
    for f in all_filters:
        if f.source_site != site:
            continue
        if site == "MUSINSA":
            # 무신사: URL의 brand + category 파라미터로 매칭
            try:
                parsed = urlparse(f.keyword or "")
                qs = parse_qs(parsed.query)
                f_brand = qs.get("brand", [""])[0]
                f_cat = qs.get("category", [""])[0]
                if f_brand == req.brand and f_cat:
                    existing_cat_codes[f_cat] = f
            except Exception:
                continue
        elif site == "LOTTEON":
            # LOTTEON: brands 파라미터(신) 우선, 없으면 q 첫 토큰(구) 폴백으로 브랜드 매칭.
            # category_filter는 쉼표 구분 BC 배열일 수 있어 각 BC를 키로 등록.
            if f.category_filter:
                try:
                    _fp = urlparse(f.keyword or "")
                    _fq = parse_qs(_fp.query)
                    _brands_val = _fq.get("brands", [""])[0]
                    _q_val = _fq.get("q", [""])[0]
                    if _brands_val:
                        # brands=A,B 형태 다중 브랜드 지원 — 쉼표 split + 트림 후 리스트로 비교.
                        _f_brand_list = [
                            b.strip() for b in _brands_val.split(",") if b.strip()
                        ]
                    elif _q_val:
                        # 구형 q=나이키 또는 신형 q=나이키 운동화 → 첫 토큰 추출
                        _first = _q_val.split(" ", 1)[0] if " " in _q_val else _q_val
                        _first = _first.strip()
                        _f_brand_list = [_first] if _first else []
                    else:
                        _f_brand_list = []
                    if keyword in _f_brand_list:
                        for _bc in (f.category_filter or "").split(","):
                            _bc = _bc.strip()
                            if _bc:
                                existing_cat_codes[_bc] = f
                except Exception:
                    pass
        else:
            # Nike/ABCmart 등: category_filter로 매칭
            if f.category_filter:
                existing_cat_codes[f.category_filter] = f

    new_groups = 0
    updated_groups = 0
    filter_ids: list[str] = []  # 이번 refresh에서 처리된 모든 필터 ID

    for cat in categories:
        cat_code = cat.get("categoryCode", "")
        count = cat.get("count", 0)
        path = cat.get("path", "")

        if cat_code in existing_cat_codes:
            # 기존 그룹 — 요청수 갱신 + keyword URL 옵션 동기화
            f = existing_cat_codes[cat_code]
            filter_ids.append(str(f.id))
            update_data: dict[str, Any] = {}
            # 누적된 실제 수집수가 더 크면 그대로 유지 (스캔 샘플로 축소 금지)
            if count > (f.requested_count or 0):
                update_data["requested_count"] = count

            # keyword URL의 includeSoldOut 파라미터를 현재 옵션과 동기화
            _cur_kw = f.keyword or ""
            if _cur_kw.startswith("http"):
                _p = urlparse(_cur_kw)
                _q = parse_qs(_p.query)
                _had_sold_out = _q.get("includeSoldOut", [""])[0] == "1"
                _want_sold_out = bool(req.options.get("includeSoldOut"))
                if _had_sold_out != _want_sold_out:
                    if _want_sold_out:
                        _sep = "&" if "?" in _cur_kw else "?"
                        update_data["keyword"] = f"{_cur_kw}{_sep}includeSoldOut=1"
                    else:
                        # includeSoldOut 파라미터 제거
                        update_data["keyword"] = _re.sub(
                            r"[&?]includeSoldOut=1", "", _cur_kw
                        )

            if update_data:
                await svc.update_filter(f.id, update_data)
                updated_groups += 1
        else:
            # 신규 카테고리 — 그룹 생성 (소싱처별 keyword/name 포맷)
            # 공통 옵션 파라미터
            _opt_parts: list[str] = []
            if req.options.get("maxDiscount"):
                _opt_parts.append("maxDiscount=1")
            if req.options.get("includeSoldOut"):
                _opt_parts.append("includeSoldOut=1")
            _opt_suffix = ("&" + "&".join(_opt_parts)) if _opt_parts else ""

            segments = path.split(" > ") if path else [cat_code]
            if site == "Nike":
                segments = [s for s in segments if s != "Nike"]
                path_tail = "_".join(segments) if segments else cat_code
                group_name = f"Nike_{path_tail}"
                keyword_url = (
                    f"https://www.nike.com/kr/w?q={_quote(keyword)}{_opt_suffix}"
                )
            elif site in ("ABCmart", "GrandStage"):
                path_tail = "_".join(segments) if segments else cat_code
                group_name = f"{site}_{keyword}_{path_tail}"
                keyword_url = (
                    f"https://abcmart.a-rt.com/display/search-word/result"
                    f"?searchWord={_quote(keyword)}{_opt_suffix}"
                )
            elif site == "GSShop":
                import base64 as _b64

                path_tail = "_".join(segments) if segments else cat_code
                group_name = f"GSShop_{keyword}_{path_tail}"
                _eh = _b64.b64encode(
                    '{"part":"DEPT","selected":"opt-part"}'.encode()
                ).decode()
                keyword_url = (
                    f"https://www.gsshop.com/shop/search/main.gs"
                    f"?tq={_quote(keyword)}&eh={_quote(_eh)}{_opt_suffix}"
                )
            elif site == "LOTTEON":
                path_tail = "_".join(segments) if segments else cat_code
                group_name = f"LOTTEON_{keyword}_{path_tail}"
                # qapi 2,100 상한 우회: q= 에 서브키워드({브랜드} {카테고리 리프}) 사용.
                # brands= 는 사후 브랜드 정확일치 필터용 (worker.py가 읽음).
                _sub_kw = cat.get("subKeyword") or (
                    f"{keyword} {segments[-1]}".strip() if segments else keyword
                )
                keyword_url = (
                    f"https://www.lotteon.com/csearch/search/search"
                    f"?render=search&platform=pc&q={_quote(_sub_kw)}"
                    f"&brands={_quote(keyword)}&mallId=2{_opt_suffix}"
                )
            elif site == "KREAM":
                path_tail = "_".join(segments) if segments else cat_code
                group_name = f"KREAM_{keyword}_{path_tail}"
                keyword_url = (
                    f"https://kream.co.kr/search?keyword={_quote(keyword)}{_opt_suffix}"
                )
            else:
                # MUSINSA
                cat_name = path.replace(" > ", "_").replace("/", "_")
                group_name = f"MUSINSA_{req.brand_name or req.brand}_{cat_name}"
                params = {
                    "keyword": req.brand_name or req.brand,
                    "brand": req.brand,
                    "category": cat_code,
                    "gf": req.gf,
                }
                if req.options.get("excludePreorder"):
                    params["excludePreorder"] = "1"
                if req.options.get("excludeBoutique"):
                    params["excludeBoutique"] = "1"
                if req.options.get("maxDiscount"):
                    params["maxDiscount"] = "1"
                if req.options.get("includeSoldOut"):
                    params["includeSoldOut"] = "1"
                keyword_url = (
                    f"https://www.musinsa.com/search/goods?{urlencode(params)}"
                )

            try:
                create_data: dict[str, Any] = {
                    "source_site": site,
                    "keyword": keyword_url,
                    "name": group_name,
                    "requested_count": count,
                }
                if site != "MUSINSA":
                    # LOTTEON은 BC 묶음 전체 저장 (worker.py가 콤마 split로 처리).
                    # 서브키워드 검색이 여러 BC를 포괄하므로 단일 BC만 저장하면 사후 필터에서 누락됨.
                    if site == "LOTTEON":
                        _bcs = cat.get("bc_codes") or [cat_code]
                        create_data["category_filter"] = ",".join(_bcs)
                    else:
                        create_data["category_filter"] = cat_code
                new_filter = await svc.create_filter(create_data)
                new_groups += 1
                if new_filter and hasattr(new_filter, "id"):
                    filter_ids.append(str(new_filter.id))
            except Exception as e:
                logger.warning(f"[추가수집] 그룹 생성 실패 {group_name}: {e}")

    total_cats = len(categories)
    logger.info(
        f"[추가수집] {site}/{keyword}: 스캔 {total_cats}개, 신규 {new_groups}개, 갱신 {updated_groups}개"
    )
    return {
        "scanned": total_cats,
        "new_groups": new_groups,
        "updated_groups": updated_groups,
        "filter_ids": filter_ids,
        "message": f"스캔 {total_cats}개 카테고리 / 신규 그룹 {new_groups}개 생성 / 기존 {updated_groups}개 요청수 갱신",
    }


@router.post("/brand-discover")
async def brand_discover(body: BrandDiscoverRequest):
    """키워드로 소싱처에서 발견된 브랜드 목록 반환 (사용자 선택용).

    프론트에서 이 결과로 체크박스 목록을 표시하고, 사용자가 선택한
    브랜드를 `/brand-scan`의 `selected_brands`로 전달한다.
    """
    if not body.keyword:
        raise HTTPException(400, "keyword가 필요합니다")

    if body.source_site == "LOTTEON":
        from backend.domain.samba.plugins.sourcing.lotteon import LotteonSourcingPlugin

        plugin = LotteonSourcingPlugin()
        return await plugin.discover_brands(body.keyword)

    if body.source_site == "SSG":
        from backend.domain.samba.plugins.sourcing.ssg import SSGPlugin
        from backend.domain.samba.proxy.ssg_sourcing import RateLimitError

        plugin = SSGPlugin()
        # SSG 429 → anyio TaskGroup 내부에서 ExceptionGroup 으로 전파되어 ASGI 연결 단절
        # 유발. 명시적으로 HTTPException(429) 로 변환하여 클라이언트에 정상 응답.
        try:
            return await plugin.discover_brands(body.keyword)
        except RateLimitError as exc:
            raise HTTPException(429, f"SSG 요청 제한: {exc}") from exc
        except BaseExceptionGroup as eg:
            rate_errs = [e for e in eg.exceptions if isinstance(e, RateLimitError)]
            if rate_errs:
                raise HTTPException(429, f"SSG 요청 제한: {rate_errs[0]}") from eg
            raise

    if body.source_site == "FashionPlus":
        from backend.domain.samba.plugins.sourcing.fashionplus import FashionPlusPlugin

        plugin = FashionPlusPlugin()
        return await plugin.discover_brands(body.keyword)

    raise HTTPException(400, f"브랜드 탐색 미지원 소싱처: {body.source_site}")


@router.get("/gsshop-scan-progress")
async def gsshop_scan_progress():
    """GS샵 카테고리 스캔 진행 상황 폴링."""
    from backend.domain.samba.proxy.gsshop_sourcing import GsShopSourcingClient

    return GsShopSourcingClient.scan_progress or {"stage": "idle"}


async def _run_ssg_brand_scan(
    job_id: str, keyword: str, body: "BrandScanRequest"
) -> None:
    """SSG brand-scan 백그라운드 실행 — ScanJobStore 에 결과/에러 기록."""
    from backend.domain.samba.collector.refresher import get_collect_proxies
    from backend.domain.samba.job.worker import _add_collect_log
    from backend.domain.samba.plugins.sourcing.ssg import SSGPlugin
    from backend.domain.samba.scan_jobs import ScanJobStore

    try:
        _proxy_list = get_collect_proxies()
        plugin = SSGPlugin()
        selected = body.selected_brands or [keyword]
        result = await plugin.scan_categories(
            keyword,
            selected_brands=selected,
            brand_ids=body.brand_ids or None,
            brand_total=body.brand_total,
            log_fn=_add_collect_log,
            proxy_urls=_proxy_list or None,
        )
        ScanJobStore.complete(job_id, result)
    except Exception as e:
        logger.exception(f"[SSG] brand-scan job 실패 ({job_id}): {e}")
        ScanJobStore.fail(job_id, str(e))


@router.get("/brand-scan-progress/{job_id}")
async def brand_scan_progress(job_id: str):
    """brand-scan 비동기 job 의 진행 상태/결과 polling.

    응답: {status: running|done|error, result?: {...}, error?: str, meta: {...}}
    """
    from backend.domain.samba.scan_jobs import ScanJobStore

    job = ScanJobStore.get(job_id)
    if job is None:
        raise HTTPException(404, f"job 없음: {job_id}")
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "result": job.get("result"),
        "error": job.get("error"),
        "meta": job.get("meta", {}),
    }


_BRAND_COLLECT_ALL_SITES = {"MUSINSA", "ABCmart", "SSG", "GSShop"}


@router.post("/brand-collect-all")
async def brand_collect_all(
    body: BrandCollectAllRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """브랜드 전체 상품을 단일 Job으로 수집 후 카테고리별 SearchFilter에 배분.

    지원 소싱처: MUSINSA, ABCmart (ABCmart는 GrandStage 병합 포함), SSG, GSShop
    기존 카테고리별 Job 순차 실행 방식의 두 가지 문제를 해결:
    1) 페이지 이탈 시 수집 중단 → 단일 백엔드 Job으로 완전 독립 실행
    2) 글로벌 dedup으로 인한 카테고리 겹침 누락 → 브랜드 단위 수집 후 카테고리 배분
    """
    if body.source_site not in _BRAND_COLLECT_ALL_SITES:
        raise HTTPException(
            400, f"brand-collect-all은 {_BRAND_COLLECT_ALL_SITES} 전용입니다"
        )
    if not body.filter_ids:
        raise HTTPException(400, "filter_ids가 필요합니다")
    if body.source_site == "MUSINSA" and (not body.keyword or not body.brand):
        raise HTTPException(400, "MUSINSA: keyword와 brand가 필요합니다")
    if body.source_site == "ABCmart" and not body.keyword:
        raise HTTPException(400, "ABCmart: keyword가 필요합니다")
    if body.source_site in ("SSG", "GSShop") and not body.keyword:
        raise HTTPException(400, f"{body.source_site}: keyword가 필요합니다")

    from backend.domain.samba.job.repository import SambaJobRepository
    from backend.domain.samba.job.service import SambaJobService

    svc = SambaJobService(SambaJobRepository(session))
    job = await svc.create_job(
        {
            "job_type": "collect",
            "payload": {
                "brand_all": True,
                "source_site": body.source_site,
                "filter_ids": body.filter_ids,
                "keyword": body.keyword,
                "brand": body.brand,
                "gf": body.gf,
                "exclude_preorder": body.exclude_preorder,
                "exclude_boutique": body.exclude_boutique,
                "use_max_discount": body.use_max_discount,
                "include_sold_out": body.include_sold_out,
            },
        }
    )
    return {"job_id": job.id, "filter_count": len(body.filter_ids)}


@router.post("/brand-scan")
async def brand_scan(
    body: BrandScanRequest,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """키워드/브랜드로 소싱처 카테고리 분포를 스캔하여 검색그룹 생성에 활용.

    지원 소싱처: MUSINSA, LOTTEON, GSSHOP, ABCmart, Nike, SSG, FashionPlus, KREAM
    """
    keyword = body.keyword or body.brand
    if not keyword:
        raise HTTPException(400, "keyword 또는 brand가 필요합니다")

    if body.source_site == "GSSHOP":
        from backend.domain.samba.plugins.sourcing.gsshop import GsShopSourcingPlugin

        plugin = GsShopSourcingPlugin()
        return await plugin.scan_categories(keyword)

    if body.source_site == "LOTTEON":
        from backend.domain.samba.plugins.sourcing.lotteon import LotteonSourcingPlugin

        plugin = LotteonSourcingPlugin()
        # selected_brands가 없으면 keyword 자체를 단일 브랜드로 사용 (하위 호환)
        selected = body.selected_brands or [keyword]
        return await plugin.scan_categories(keyword, selected_brands=selected)

    if body.source_site == "MUSINSA":
        # 무신사 — 필터 API 재귀 탐색 방식으로 전체 카테고리별 상품 수 조회
        from backend.domain.samba.proxy.musinsa import MusinsaClient

        client = MusinsaClient()
        categories = await client.scan_brand_categories(
            brand=body.brand,
            gf=body.gf,
            keyword=keyword,
            include_sold_out=bool(body.options.get("includeSoldOut")),
        )
        total = sum(c["count"] for c in categories)
        return {
            "categories": categories,
            "total": total,
            "groupCount": len(categories),
        }

    if body.source_site in ("ABCmart", "GrandStage"):
        from backend.domain.samba.plugins.sourcing.abcmart import AbcMartPlugin

        plugin = AbcMartPlugin()
        return await plugin.scan_categories(keyword)

    if body.source_site == "Nike":
        from backend.domain.samba.plugins.sourcing.nike import NikePlugin

        plugin = NikePlugin()
        return await plugin.scan_categories(keyword)

    if body.source_site == "SSG":
        # SSG 카테고리 스캔은 ~170s 까지 소요 (47개 세분류 × ~3.5s, concurrency=1).
        # Cloudflare 100s origin response timeout 을 우회하기 위해 job_id 즉시 반환 +
        # background task 패턴 사용. frontend 는 /brand-scan-progress polling.
        from backend.domain.samba.scan_jobs import ScanJobStore

        job_id = ScanJobStore.create(
            kind="brand-scan",
            meta={
                "source_site": "SSG",
                "keyword": keyword,
                "selected_brands": body.selected_brands or [keyword],
            },
        )
        asyncio.create_task(_run_ssg_brand_scan(job_id, keyword, body))
        return {"job_id": job_id, "status": "running"}

    if body.source_site == "FashionPlus":
        from backend.domain.samba.plugins.sourcing.fashionplus import FashionPlusPlugin

        plugin = FashionPlusPlugin()
        selected = body.selected_brands or [keyword]
        return await plugin.scan_categories(keyword, selected_brands=selected)

    if body.source_site == "KREAM":
        from backend.domain.samba.plugins.sourcing.kream import KreamPlugin

        plugin = KreamPlugin()
        return await plugin.scan_categories(keyword)

    raise HTTPException(400, f"카테고리 스캔 미지원 소싱처: {body.source_site}")


@router.post("/brand-create-groups")
async def brand_create_groups(
    body: BrandCreateGroupsRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """카테고리 스캔 결과에서 선택한 카테고리별 검색그룹 생성.

    지원 소싱처: MUSINSA, LOTTEON
    """
    if not body.categories:
        raise HTTPException(400, "categories가 비어있습니다")

    svc = _get_services(session)
    created_groups = []

    for cat in body.categories:
        code = cat.get("categoryCode", "")
        path = cat.get("path", "")
        count = cat.get("count", 0)

        # 그룹명: "{SITE}_{브랜드}_{카테고리}"
        # Nike: source_site와 브랜드가 동일하므로 브랜드 라벨 생략
        label = body.brand_name or body.brand or "브랜드"
        segments = path.split(" > ") if path else [code]
        # Nike: 카테고리 경로에서 "Nike" 제거 (source_site로 충분)
        if body.source_site == "Nike":
            segments = [s for s in segments if s != "Nike"]
        path_tail = "_".join(segments) if segments else code
        if body.source_site == "Nike":
            group_name = f"{body.source_site}_{path_tail}"
        else:
            group_name = f"{body.source_site}_{label}_{path_tail}"

        req_count = count if count > 0 else FIXED_REQUESTED_COUNT

        # 소싱처별 keyword 및 category_filter 결정
        # 공통 옵션: 품절상품 포함
        _opts_include_sold_out = body.options.get("includeSoldOut", False)

        if body.source_site == "MUSINSA":
            parts = [
                f"keyword={body.brand_name or body.brand}",
                "keywordType=keyword",
                f"gf={body.gf}",
            ]
            if body.brand:
                parts.append(f"brand={body.brand}")
            if code:
                parts.append(f"category={code}")
            # MUSINSA 전용 옵션
            if body.options.get("excludePreorder"):
                parts.append("excludePreorder=1")
            if body.options.get("excludeBoutique"):
                parts.append("excludeBoutique=1")
            if body.options.get("maxDiscount"):
                parts.append("maxDiscount=1")
            if _opts_include_sold_out:
                parts.append("includeSoldOut=1")
            keyword = "https://www.musinsa.com/search/goods?" + "&".join(parts)
            category_filter = code or None
        elif body.source_site in ("ABCmart", "GrandStage"):
            from urllib.parse import quote as _quote

            _label = body.brand_name or body.brand or keyword or ""
            _md = "&maxDiscount=1" if body.options.get("maxDiscount") else ""
            _so = "&includeSoldOut=1" if _opts_include_sold_out else ""
            keyword = (
                f"https://abcmart.a-rt.com/display/search-word/result"
                f"?searchWord={_quote(_label)}{_md}{_so}"
            )
            category_filter = code or None
        elif body.source_site == "Nike":
            from urllib.parse import quote as _quote_nike

            _label = body.brand_name or body.brand or keyword or ""
            _so_nike = "&includeSoldOut=1" if _opts_include_sold_out else ""
            keyword = f"https://www.nike.com/kr/w?q={_quote_nike(_label)}{_so_nike}"
            category_filter = code or None
        elif body.source_site == "GSShop":
            import base64 as _b64
            from urllib.parse import quote as _quote_gs

            _label = body.brand_name or body.brand or ""
            _eh = _b64.b64encode(
                '{"part":"DEPT","selected":"opt-part"}'.encode()
            ).decode()
            _md_gs = "&maxDiscount=1" if body.options.get("maxDiscount") else ""
            _so_gs = "&includeSoldOut=1" if _opts_include_sold_out else ""
            keyword = (
                f"https://www.gsshop.com/shop/search/main.gs"
                f"?tq={_quote_gs(_label)}&eh={_quote_gs(_eh)}{_md_gs}{_so_gs}"
            )
            category_filter = code or None
        elif body.source_site == "SSG":
            from urllib.parse import quote as _quote_ssg

            _label_ssg = body.brand_name or body.brand or ""
            _md_ssg = "&maxDiscount=1" if body.options.get("maxDiscount") else ""
            _so_ssg = "&includeSoldOut=1" if _opts_include_sold_out else ""
            # 선택된 브랜드의 repBrandId를 URL에 포함 → 워커가 파싱하여 수집 시 사용
            _rep_brand = (
                f"&repBrandId={'|'.join(body.brand_ids)}" if body.brand_ids else ""
            )
            # 카테고리 전체 경로(전시카테고리) URL에 저장 → 워커가 수집 시 정확한 카테고리 적용
            _ctg_path_ssg = f"&ctgPath={_quote_ssg(path)}" if path else ""
            # ctgId + ctgLv=3 사용 (SSG 실제 검색 URL 파라미터 준수)
            keyword = (
                f"https://department.ssg.com/search"
                f"?query={_quote_ssg(_label_ssg)}&ctgId={code}&ctgLv=3"
                f"{_rep_brand}{_ctg_path_ssg}{_md_ssg}{_so_ssg}"
            )
            category_filter = code or None
        elif body.source_site == "FashionPlus":
            from urllib.parse import quote as _quote_fp

            _label_fp = body.brand_name or body.brand or ""
            _c1 = cat.get("category1Id", "")
            _c2 = cat.get("category2Id", "")
            _c3 = cat.get("category3Id", "")
            # 카테고리 이름도 URL에 저장 — 수집 시 _CATEGORY_MAP 의존 없이 정확한 이름 복원용
            _c1_name = cat.get("category1", "")
            _c2_name = cat.get("category2", "")
            _c3_name = cat.get("category3", "")
            _md_fp = "&maxDiscount=1" if body.options.get("maxDiscount") else ""
            _so_fp = "&includeSoldOut=1" if _opts_include_sold_out else ""
            _cat_params = ""
            if _c1:
                _cat_params += f"&category1Id={_c1}"
                if _c1_name:
                    _cat_params += f"&category1Name={_quote_fp(_c1_name)}"
            if _c2:
                _cat_params += f"&category2Id={_c2}"
                if _c2_name:
                    _cat_params += f"&category2Name={_quote_fp(_c2_name)}"
            if _c3:
                _cat_params += f"&category3Id={_c3}"
                if _c3_name:
                    _cat_params += f"&category3Name={_quote_fp(_c3_name)}"
            keyword = (
                f"https://www.fashionplus.co.kr/search/goods/result"
                f"?searchWord={_quote_fp(_label_fp)}{_cat_params}{_md_fp}{_so_fp}"
            )
            category_filter = code or None
        else:  # LOTTEON
            from urllib.parse import quote as _quote_lt

            _brand_label = body.brand_name or body.brand or ""
            # 롯데백화점(mallId=2) 검색 URL로 저장 (가품 방지 목적)
            _md_lt = "&maxDiscount=1" if body.options.get("maxDiscount") else ""
            _so_lt = "&includeSoldOut=1" if _opts_include_sold_out else ""
            # qapi 2,100 상한 우회: q= 에 서브키워드({브랜드} {카테고리 리프}) 사용.
            # scan_categories 응답의 subKeyword를 우선 사용, 없으면 path 리프에서 조합.
            _path_lt = cat.get("path", "") or ""
            _leaf_lt = ""
            if _path_lt:
                _parts_lt = [p for p in _path_lt.split(" > ") if p]
                _leaf_lt = _parts_lt[-1] if _parts_lt else ""
            _sub_kw_lt = cat.get("subKeyword") or (
                f"{_brand_label} {_leaf_lt}".strip() if _leaf_lt else _brand_label
            )
            # brands= 는 사후 브랜드 정확일치 필터용 (worker.py가 읽음).
            # selected_brands가 있으면 다중 브랜드 필터, 없으면 단일 브랜드명.
            # 단, brand_label과 관련 없는 브랜드가 섞이면 해당 브랜드만 필터링해 오염 방지.
            if body.selected_brands:
                _lbl_norm = _brand_label.lower().replace(" ", "")
                _valid_brands = [
                    b
                    for b in body.selected_brands
                    if _lbl_norm in b.lower().replace(" ", "")
                    or b.lower().replace(" ", "") in _lbl_norm
                ]
                _brands_val_lt = (
                    ",".join(_valid_brands) if _valid_brands else _brand_label
                )
            else:
                _brands_val_lt = _brand_label
            keyword = (
                f"https://www.lotteon.com/csearch/search/search"
                f"?render=search&platform=pc&q={_quote_lt(_sub_kw_lt)}"
                f"&brands={_quote_lt(_brands_val_lt)}&mallId=2{_md_lt}{_so_lt}"
            )
            # 합산된 BC코드들을 콤마로 연결 (같은 path의 여러 BC코드)
            bc_codes = cat.get("bc_codes") or ([code] if code else [])
            category_filter = ",".join(bc_codes) if bc_codes else None

        # 소싱처 브랜드명 저장 (수집 시 빈 brand/manufacturer 자동 채움용)
        _source_brand = body.brand_name or body.brand or ""
        filter_data: dict = {
            "source_site": body.source_site,
            "name": group_name,
            "keyword": keyword,
            "requested_count": req_count,
            "category_filter": category_filter,
        }
        if _source_brand:
            filter_data["source_brand_name"] = _source_brand
        if body.applied_policy_id:
            filter_data["applied_policy_id"] = body.applied_policy_id

        try:
            sf = await svc.create_filter(filter_data)
            created_groups.append(
                {
                    "id": str(sf.id),
                    "name": group_name,
                    "count": req_count,
                    "path": path,
                }
            )
        except Exception as e:
            # 중복 그룹은 건너뜀
            logger.warning(f"그룹 생성 스킵: {group_name} — {e}")

    if created_groups:
        await cache.delete("filters:tree:v3")
        await cache.clear_pattern("filters:tree:counts:*")

    return {
        "created": len(created_groups),
        "groups": created_groups,
    }


# ── 브랜드 단위 정책 일괄 적용 ──


class BrandPolicyApplyRequest(BaseModel):
    source_site: str
    brand_name: str
    policy_id: Optional[str] = None  # None = 정책 해제


@router.post("/brand-policy-apply")
async def brand_policy_apply(
    body: BrandPolicyApplyRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """브랜드 단위 정책 일괄 적용 — 수집상품·검색필터·테트리스배치 모두 갱신.

    테트리스 보드 배지 팔레트에서 호출. 해당 (소싱처, 브랜드) 조합의
    `samba_collected_product.applied_policy_id` /
    `samba_search_filter.applied_policy_id` /
    `samba_tetris_assignment.policy_id`을 한 번에 갱신한다.
    """
    from sqlalchemy import text as _text

    site = (body.source_site or "").strip()
    brand = (body.brand_name or "").strip()
    pid = body.policy_id  # None 허용

    if not site or not brand:
        raise HTTPException(400, "source_site, brand_name이 필요합니다")

    if pid:
        pol = await session.execute(
            _text("SELECT 1 FROM samba_policy WHERE id = :pid"),
            {"pid": pid},
        )
        if not pol.scalar():
            raise HTTPException(404, f"정책 {pid}을 찾을 수 없습니다")

    # 1) 수집상품 — 색상 매칭의 진실 소스 (테트리스 보드가 이 컬럼을 사용)
    cp_res = await session.execute(
        _text("""
            UPDATE samba_collected_product
            SET applied_policy_id = :pid
            WHERE source_site = :site AND BTRIM(brand) = :brand
        """),
        {"pid": pid, "site": site, "brand": brand},
    )

    # 2) 검색필터 — 차후 신규 수집 상품에 자동 상속되는 기본값
    sf_res = await session.execute(
        _text("""
            UPDATE samba_search_filter
            SET applied_policy_id = :pid, updated_at = NOW()
            WHERE source_site = :site AND BTRIM(source_brand_name) = :brand
        """),
        {"pid": pid, "site": site, "brand": brand},
    )

    # 3) 테트리스 배치 — 화면에 즉시 반영
    ta_res = await session.execute(
        _text("""
            UPDATE samba_tetris_assignment
            SET policy_id = :pid, updated_at = NOW()
            WHERE source_site = :site AND brand_name = :brand
        """),
        {"pid": pid, "site": site, "brand": brand},
    )

    await session.commit()

    return {
        "products_updated": cp_res.rowcount,
        "filters_updated": sf_res.rowcount,
        "assignments_updated": ta_res.rowcount,
    }

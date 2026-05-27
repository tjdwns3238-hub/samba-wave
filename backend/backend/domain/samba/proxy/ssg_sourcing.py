"""신세계백화점(department.ssg.com) 소싱용 웹 스크래핑 클라이언트 - httpx 기반.

주의: proxy/ssg.py는 판매처(마켓) 등록용 Open API 클라이언트이므로,
소싱(상품 수집)용은 이 파일에서 별도로 관리한다.

소싱 대상:
  - https://department.ssg.com/ (신세계백화점 온라인전용 상품만 취급)
  - siteNo=6009 (신세계백화점 고정)
  - 일반 SSG.COM 마켓플레이스 판매자 상품 제외

SSG 사이트 정보:
  - 검색: https://department.ssg.com/search?query={keyword}&page={n}
  - 상세: https://department.ssg.com/item/itemView.ssg?itemId={13자리}&siteNo=6009
  - 이미지 CDN: sitem.ssgcdn.com

파싱 전략:
  - 검색 결과: HTML 내 <script id="__NEXT_DATA__"> 태그 JSON 파싱
               queries → fetchSearchItemListArea → ITEM_UNIT_LIST → dataList
  - 상세 조회: HTML 내 var resultItemObj / uitemObjList JS 변수 파싱 (1순위)
               og: 메타태그 + CSS 패턴 폴백 (2순위)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx

from backend.utils.logger import logger


# 임직원/사업자 회원 전용 상품 — 일반 고객 구매 불가 (수집·오토튠 대상 아님).
# department.ssg.com이 페이지 진입 시 alert(...)로 안내하며, HTML 본문 인라인 스크립트에 동일 문구가 박혀 있음.
_STAFF_ONLY_MARKERS: tuple[str, ...] = (
    "임직원 및 사업자 회원",
    "임직원만 구매",
    "임직원 전용",
)


def _is_staff_only(html: str) -> bool:
    """SSG 백화점관 임직원/사업자 회원 전용 상품 여부."""
    if not html:
        return False
    return any(marker in html for marker in _STAFF_ONLY_MARKERS)


class RateLimitError(Exception):
    """SSG 차단 감지 (429/403)."""

    def __init__(self, status: int, retry_after: int = 0):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"HTTP {status} (retry_after={retry_after})")


class SSGSourcingClient:
    """신세계백화점(department.ssg.com) 소싱용 웹 스크래핑 클라이언트 (검색, 상세).

    신세계백화점 온라인 전용 상품만 수집한다 (siteNo=6009).
    일반 SSG.COM 마켓플레이스 판매자 상품은 수집하지 않는다.
    """

    BASE = "https://department.ssg.com"
    SEARCH_URL = "https://department.ssg.com/search"
    ITEM_URL = "https://department.ssg.com/item/itemView.ssg"
    SITE_NO = "6009"

    HEADERS: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://department.ssg.com/",
    }

    def __init__(self, cookie: str = "", *, proxy_url: str | None = None) -> None:
        """cookie가 있으면 로그인 상태로 최대혜택가 정밀 계산 가능.
        proxy_url이 있으면 SSG 차단 우회에 사용한다.
        """
        from backend.core.config import settings

        self._timeout = httpx.Timeout(settings.http_timeout_default, connect=10.0)
        self.cookie = cookie
        self.proxy_url = proxy_url

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        """요청 헤더 생성. 쿠키가 있으면 포함."""
        h = {**self.HEADERS}
        if self.cookie:
            h["Cookie"] = self.cookie
        if extra:
            h.update(extra)
        return h

    async def get_detail(
        self, item_id: str, *, _shared_client: Optional[httpx.AsyncClient] = None
    ) -> dict[str, Any]:
        """worker.py get_detail 패턴 호환 래퍼."""
        return await self.get_product_detail(item_id, _shared_client=_shared_client)

    # ------------------------------------------------------------------
    # 검색
    # ------------------------------------------------------------------

    async def _fetch_brand_ids(self, keyword: str) -> list[str]:
        """keyword에 매칭되는 repBrandId 목록 추출 (1회 HTTP 요청).

        SSG __NEXT_DATA__의 brandFilter에서 keyword로 시작하는 브랜드를 전부 반환.
        """
        _ck: dict[str, Any] = {"timeout": self._timeout, "follow_redirects": True}
        if self.proxy_url:
            _ck["proxy"] = self.proxy_url
        try:
            async with httpx.AsyncClient(**_ck) as client:
                _url = f"{self.SEARCH_URL}?query={quote(keyword)}&page=1"
                _r = await client.get(_url, headers=self._headers())
                if _r.status_code == 200:
                    return self._extract_matching_brand_ids(_r.text, keyword)
        except Exception:
            pass
        return []

    async def search(
        self, keyword: str, max_count: int = 100, **kwargs: Any
    ) -> dict[str, Any]:
        """worker.py 직접 API 패턴 호환 래퍼 — 멀티페이지 검색.

        사전에 brand_ids를 추출하여 전 페이지(1+)에 브랜드 필터를 일관 적용한다.
        (기존: page 2+에서 brand_ids 유실로 비브랜드 상품 혼입 버그 수정)
        """
        import asyncio

        products: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 1
        rate_limit_retries = 3
        # 무한루프 안전망 (이슈 #263) — 40건/page × 500 = 2만건 상한
        MAX_PAGES = 500

        # 외부에서 brand_ids가 제공되면 _fetch_brand_ids 건너뛰기
        _brand_ids: list[str] | None = kwargs.pop("brand_ids", None)
        if _brand_ids is None:
            _brand_ids = await self._fetch_brand_ids(keyword)
        logger.info(f"[SSG] 검색 brand_ids: {_brand_ids}")

        while len(products) < max_count and page <= MAX_PAGES:
            # 모든 페이지에 brand_ids 전달 (page 1 포함, search_products 내부 추출 생략)
            raw: list[dict[str, Any]] = []
            for attempt in range(rate_limit_retries + 1):
                try:
                    raw = await self.search_products(
                        keyword, page=page, size=40, brand_ids=_brand_ids, **kwargs
                    )
                    break
                except RateLimitError as exc:
                    if attempt >= rate_limit_retries:
                        raise
                    wait_seconds = exc.retry_after or min(15, 3 * (attempt + 1))
                    logger.warning(
                        f"[SSG] 검색 rate limit: keyword={keyword} page={page} "
                        f"status={exc.status} wait={wait_seconds}s "
                        f"retry={attempt + 1}/{rate_limit_retries}"
                    )
                    await asyncio.sleep(wait_seconds)
            if not raw:
                break

            new_count = 0
            for item in raw:
                if len(products) >= max_count:
                    break
                pid = item.get("siteProductId") or item.get("goodsNo") or ""
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                new_count += 1
                products.append(
                    {
                        "site_product_id": pid,
                        "name": item.get("name", ""),
                        "brand": item.get("brand", ""),
                        "sale_price": item.get("salePrice", 0),
                        "original_price": item.get("originalPrice", 0),
                        "images": [item.get("image", "")] if item.get("image") else [],
                        "source_url": item.get("sourceUrl", ""),
                        "free_shipping": item.get("freeShipping", False),
                        "is_sold_out": item.get("isSoldOut", False),
                    }
                )

            if new_count == 0:
                break
            page += 1
            await asyncio.sleep(1.0)

        return {"products": products, "total": len(products)}

    async def search_products(
        self,
        keyword: str,
        page: int = 1,
        size: int = 40,
        _shared_client: Optional[httpx.AsyncClient] = None,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        """신세계백화점 브랜드 검색.

        1단계: keyword로 검색 → 브랜드 필터에서 keyword로 시작하는 브랜드 ID 전부 수집
        2단계: 수집된 repBrandId를 파이프(|)로 결합한 URL로 상품 수집

        예) keyword='아디다스' → 아디다스|아디다스오리지널스|아디다스키즈|아디다스골프
        _shared_client: 외부에서 공유 클라이언트를 넘기면 TCP 연결 재사용 (대량 수집 성능 향상)
        """
        logger.info(f'[SSG] 검색 시작: "{keyword}" (page={page})')

        _client_kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "follow_redirects": True,
        }
        if self.proxy_url:
            _client_kwargs["proxy"] = self.proxy_url

        async def _run(client: httpx.AsyncClient) -> list[dict[str, Any]]:
            # brand_ids 결정: filters에서 제공되면 그대로 사용, 없으면 page=1 자동 추출
            provided_brand_ids: list[str] | None = filters.get("brand_ids", None)

            if provided_brand_ids is not None:
                # search() 래퍼에서 사전 추출된 brand_ids 사용 (추가 요청 불필요)
                brand_ids = provided_brand_ids
            elif page == 1:
                # 단독 호출(search_products 직접 사용) 시 page=1에서 자동 추출
                first_url = f"{self.SEARCH_URL}?query={quote(keyword)}&page=1"
                resp = await client.get(first_url, headers=self._headers())
                if resp.status_code in (429, 403):
                    raise RateLimitError(int(resp.status_code))
                if resp.status_code != 200:
                    logger.warning(f"[SSG] 검색 페이지 HTTP {resp.status_code}")
                    return []
                brand_ids = self._extract_matching_brand_ids(resp.text, keyword)
                logger.info(
                    f"[SSG] 매칭 브랜드 자동추출: {len(brand_ids)}개 → {brand_ids}"
                )
            else:
                # page > 1이고 brand_ids 미제공: 필터 없이 진행
                brand_ids = []

            # 브랜드/카테고리 필터 적용 URL 구성
            # [중요] SSG 검색 API는 repBrandId + ctgId 동시 사용 시 ctgId를 무시함.
            # → 카테고리 필터가 있으면 repBrandId를 제외하고 query 키워드로만 브랜드 제한.
            # 하위호환: 기존 disp_ctg_id 키로 저장된 그룹도 지원
            search_url = f"{self.SEARCH_URL}?query={quote(keyword)}&page={page}"
            ctg_id = filters.get("ctg_id", "") or filters.get("disp_ctg_id", "")
            if ctg_id:
                search_url += f"&ctgId={ctg_id}"
                ctg_lv = filters.get("ctg_lv", "")
                if ctg_lv:
                    search_url += f"&ctgLv={ctg_lv}"
            elif brand_ids:
                search_url += f"&repBrandId={'|'.join(brand_ids)}"
            # 할인상품만 보기 (사용자 UI에서 maxDiscount=1 필터 지정한 경우 동일 결과 반환 보장)
            max_discount = filters.get("maxDiscount", "")
            if max_discount:
                search_url += f"&maxDiscount={max_discount}"

            resp = await client.get(search_url, headers=self._headers())
            if resp.status_code in (429, 403):
                raise RateLimitError(int(resp.status_code))
            if resp.status_code != 200:
                return []
            html = resp.text

            products = self._parse_search_html(html, keyword)

            # [중요] ctgId 사용 시 SSG 서버에서 repBrandId를 무시하므로
            # 검색 결과에 하위 브랜드(예: 나이키키즈/스윔/골프)가 혼입됨.
            # 클라이언트 post-filter 로 제거한다.
            if brand_ids and ctg_id:
                _allowed = {str(b).strip() for b in brand_ids if str(b).strip()}
                _keyword_norm = str(keyword or "").strip()
                _filtered: list[dict[str, Any]] = []
                _dropped = 0
                _sample_drop: list[str] = []
                for p in products:
                    _bid = str(p.get("repBrandId") or p.get("brandId") or "").strip()
                    _bname = str(p.get("brand") or "").strip()
                    if _bid:
                        _keep = _bid in _allowed
                    else:
                        _keep = (not _keyword_norm) or (_bname == _keyword_norm)
                    if _keep:
                        _filtered.append(p)
                    else:
                        _dropped += 1
                        if len(_sample_drop) < 3:
                            _sample_drop.append(f"{_bname}({_bid or 'no-id'})")
                if _dropped:
                    logger.info(
                        f"[SSG] 하위브랜드 drop {_dropped}건 "
                        f'keyword="{keyword}" ctgId={ctg_id} '
                        f"allowed={_allowed} samples={_sample_drop}"
                    )
                products = _filtered

            logger.info(
                f'[SSG] 검색 완료: "{keyword}" page={page} -> {len(products)}개'
            )
            return products

        try:
            if _shared_client:
                return await _run(_shared_client)
            async with httpx.AsyncClient(**_client_kwargs) as client:
                return await _run(client)

        except RateLimitError:
            raise
        except httpx.TimeoutException:
            logger.error(f"[SSG] 검색 타임아웃: {keyword}")
            return []
        except Exception as e:
            logger.error(f"[SSG] 검색 실패: {keyword} — {e}")
            return []

    def _extract_matching_brand_ids(self, html: str, keyword: str) -> list[str]:
        """__NEXT_DATA__에서 keyword로 시작하는 브랜드 ID 목록 추출.

        예) keyword='아디다스' → ['2000000507', '2000000509', '2000047294', '2000000510']
        """
        m = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not m:
            return []

        try:
            next_data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

        queries = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("dehydratedState", {})
            .get("queries", [])
        )

        brand_ids: list[str] = []
        seen: set[str] = set()

        for q in queries:
            if "useTemplateFilterQuery" not in (q.get("queryKey") or []):
                continue
            filters_data = q.get("state", {}).get("data") or []
            for f in filters_data:
                if f.get("filterType") != "brandFilter":
                    continue
                for unit in f.get("unitList", []):
                    for item in unit.get("dataList", []):
                        name = item.get("name", "")
                        value = item.get("value", "")
                        # keyword로 시작하는 브랜드 전부 선택
                        if name.startswith(keyword) and value and value not in seen:
                            brand_ids.append(value)
                            seen.add(value)

        return brand_ids

    # ------------------------------------------------------------------
    # 브랜드 탐색
    # ------------------------------------------------------------------

    async def discover_brands(self, keyword: str) -> dict[str, Any]:
        """키워드 검색 → brandFilter에서 브랜드 목록 + 개별 상품수 조회.

        1단계: __NEXT_DATA__의 brandFilter에서 브랜드명/ID 추출
        2단계: 각 브랜드별 repBrandId 파라미터로 검색 → PAGING_UNIT.itemCount 파싱

        Returns:
            {"brands": [{"name": "나이키", "count": 2601}, ...], "total": int}
        """
        import asyncio

        logger.info(f'[SSG] 브랜드 탐색 시작: "{keyword}"')

        try:
            _client_kwargs: dict[str, Any] = {
                "timeout": self._timeout,
                "follow_redirects": True,
            }
            if self.proxy_url:
                _client_kwargs["proxy"] = self.proxy_url

            async with httpx.AsyncClient(**_client_kwargs) as client:
                url = f"{self.SEARCH_URL}?query={quote(keyword)}&page=1"
                resp = await client.get(url, headers=self._headers())
                if resp.status_code in (429, 403):
                    raise RateLimitError(int(resp.status_code))
                if resp.status_code != 200:
                    logger.warning(f"[SSG] 브랜드 탐색 HTTP {resp.status_code}")
                    return {"brands": [], "total": 0}

                html = resp.text

                # 1단계: brandFilter에서 브랜드명/ID 추출
                brand_items = self._extract_brand_filter(html)
                if not brand_items:
                    return {"brands": [], "total": 0}

                # 2단계: 각 브랜드별 상품수 개별 조회
                brands: list[dict[str, Any]] = []
                for bi in brand_items:
                    await asyncio.sleep(0.5)
                    try:
                        brand_url = (
                            f"{self.SEARCH_URL}?query={quote(keyword)}"
                            f"&repBrandId={bi['value']}&page=1"
                        )
                        r = await client.get(brand_url, headers=self._headers())
                        count = (
                            self._parse_area_count(r.text)
                            if r.status_code == 200
                            else 0
                        )
                    except Exception:
                        count = 0
                    brands.append(
                        {"name": bi["name"], "count": count, "id": bi["value"]}
                    )
                    logger.info(f"[SSG] 브랜드 건수: {bi['name']} → {count}건")

        except RateLimitError:
            raise
        except Exception as e:
            logger.error(f"[SSG] 브랜드 탐색 실패: {keyword} — {e}")
            return {"brands": [], "total": 0}

        # count 내림차순 정렬
        brands.sort(key=lambda x: -x["count"])
        total = sum(b["count"] for b in brands)
        logger.info(
            f'[SSG] 브랜드 탐색 완료: "{keyword}" → {len(brands)}개 브랜드, 총 {total}건'
        )
        return {"brands": brands, "total": total}

    def _extract_brand_filter(self, html: str) -> list[dict[str, str]]:
        """__NEXT_DATA__의 brandFilter에서 브랜드 name/value 목록 추출."""
        m = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not m:
            return []
        try:
            next_data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

        queries = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("dehydratedState", {})
            .get("queries", [])
        )

        items: list[dict[str, str]] = []
        for q in queries:
            if "useTemplateFilterQuery" not in (q.get("queryKey") or []):
                continue
            for f in q.get("state", {}).get("data") or []:
                if f.get("filterType") != "brandFilter":
                    continue
                for unit in f.get("unitList", []):
                    for item in unit.get("dataList", []):
                        name = (item.get("name") or "").strip()
                        value = str(item.get("value") or "").strip()
                        if name and value:
                            items.append({"name": name, "value": value})
        return items

    # ------------------------------------------------------------------
    # 카테고리 스캔
    # ------------------------------------------------------------------

    async def scan_categories(
        self,
        keyword: str,
        *,
        selected_brands: list[str] | None = None,
        brand_ids: list[str] | None = None,
        brand_total: int = 0,
        log_fn: Callable[[str], None] | None = None,
        proxy_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        """키워드 검색 → 카테고리 분포 추출.

        brand_ids 제공 시: repBrandId 적용 검색 → 상품 detail 샘플링으로 실제 분포 추출.
        brand_ids 없을 시: categoryFilter 그대로 반환.

        SSG categoryFilter는 repBrandId를 반영하지 않으므로,
        브랜드가 선택된 경우 실제 상품의 dispCtgId를 샘플링해 정확한 분포를 구한다.

        Args:
            keyword: 검색 키워드 (예: "나이키")
            selected_brands: 사용자가 선택한 브랜드 이름 목록 (로그 용도)
            brand_ids: 선택된 브랜드의 repBrandId 목록
            brand_total: 선택된 브랜드의 총 상품수 (비례 스케일링용)
            log_fn: UI 로그창 콜백 (없으면 서버 로그만 출력)

        Returns:
            {"categories": [...], "total": int, "groupCount": int}
        """

        def _log(msg: str) -> None:
            logger.info(msg)
            if log_fn:
                from datetime import timezone as _tz

                _ts = (
                    datetime.now(_tz.utc)
                    .astimezone(
                        __import__("zoneinfo", fromlist=["ZoneInfo"]).ZoneInfo(
                            "Asia/Seoul"
                        )
                    )
                    .strftime("%H:%M:%S")
                )
                log_fn(f"[{_ts}] {msg}")

        logger.info(
            f'[SSG] 카테고리 스캔 시작: "{keyword}"'
            + (f" brand_ids={brand_ids}" if brand_ids else "")
        )

        try:
            _client_kwargs: dict[str, Any] = {
                "timeout": self._timeout,
                "follow_redirects": True,
            }
            if self.proxy_url:
                _client_kwargs["proxy"] = self.proxy_url

            async with httpx.AsyncClient(**_client_kwargs) as client:
                import asyncio as _asyncio

                # 1단계: 비브랜드 검색으로 전체 카테고리 트리 추출
                # 브랜드 검색 categoryFilter는 불안정(2개만 반환될 수 있음),
                # 비브랜드 검색은 항상 전체 15개 대분류 반환하여 안정적
                search_url = f"{self.SEARCH_URL}?query={quote(keyword)}&page=1"
                resp = await client.get(search_url, headers=self._headers())
                if resp.status_code in (429, 403):
                    raise RateLimitError(int(resp.status_code))
                if resp.status_code != 200:
                    logger.warning(f"[SSG] 카테고리 스캔 HTTP {resp.status_code}")
                    return {"categories": [], "total": 0, "groupCount": 0}

                html = resp.text
                all_categories, top_categories = self._extract_category_filters(html)

                if not all_categories:
                    logger.info("[SSG] 카테고리 필터 없음 — 빈 결과 반환")
                    return {"categories": [], "total": 0, "groupCount": 0}

                logger.info(
                    f"[SSG] 비브랜드 categoryFilter: {len(all_categories)}개 leaf, "
                    f"{len(top_categories)}개 대분류"
                )

                if brand_ids and top_categories:
                    brand_param = "|".join(brand_ids)

                    # 2단계: 비브랜드 검색 count로 valid 대분류 결정 (brand count 요청 제거)
                    # brand count 15회 요청 → rate limit 소진 → leaf 429 연쇄 문제 방지
                    valid_tops = {
                        top["name"] for top in top_categories if top.get("count", 0) > 0
                    }
                    candidate_leaves = [
                        c
                        for c in all_categories
                        if c.get("category1", "") in valid_tops
                    ]
                    _log(
                        f"[SSG] 후보 세분류: {len(candidate_leaves)}개 "
                        f"({len(valid_tops)}개 대분류)"
                    )

                    # 3단계: 각 leaf 직접 검증 (비브랜드 검색 1회 후 10s 쿨다운)
                    await _asyncio.sleep(10.0)
                    # ctgId만으로 검색 → brandFilter 사이드바에 keyword 브랜드 있으면 valid
                    # (하위브랜드 전용 leaf는 brandFilter에 keyword 브랜드 미등장 → 제외)
                    # 429 발생 시 60s 대기 후 재시도 (skip 금지 — skip하면 그룹 누락)
                    brand_leaf_ids: set[str] = set()
                    # 프록시 로테이션 클라이언트 생성
                    # 각 프록시당 ~30건으로 rate limit 분산
                    _proxy_list = [p.strip() for p in (proxy_urls or []) if p.strip()]
                    _proxy_clients: list[httpx.AsyncClient] = []
                    for _px in _proxy_list:
                        _proxy_clients.append(
                            httpx.AsyncClient(
                                proxy=_px,
                                timeout=self._timeout,
                                follow_redirects=True,
                            )
                        )
                    _leaf_delay = 1.0 if _proxy_clients else 3.0
                    _proxy_count = len(_proxy_clients)
                    _log(
                        f"[SSG] 세분류 검증 시작: {len(candidate_leaves)}개 "
                        f"(프록시 {_proxy_count}개 로테이션, {_leaf_delay:.0f}초 간격)"
                        if _proxy_clients
                        else f"[SSG] 세분류 검증 시작: {len(candidate_leaves)}개 (3초 간격)"
                    )
                    try:
                        for _leaf_idx, leaf in enumerate(candidate_leaves):
                            leaf_ctg_id = leaf.get("categoryCode", "")
                            if not leaf_ctg_id:
                                continue
                            # 프록시 로테이션: i번째 leaf는 i%N번 프록시 사용
                            _leaf_client = (
                                _proxy_clients[_leaf_idx % _proxy_count]
                                if _proxy_clients
                                else client
                            )
                            try:
                                leaf_url = (
                                    f"{self.SEARCH_URL}?query={quote(keyword)}"
                                    f"&ctgId={leaf_ctg_id}&repBrandId={brand_param}&page=1"
                                )
                                r = await _leaf_client.get(
                                    leaf_url, headers=self._headers()
                                )
                                # 429/403: 다음 프록시로 재시도 (최대 2회)
                                if r.status_code in (429, 403):
                                    _retry = 0
                                    while r.status_code in (429, 403) and _retry < 2:
                                        _next_client = (
                                            _proxy_clients[
                                                (_leaf_idx + _retry + 1) % _proxy_count
                                            ]
                                            if _proxy_clients
                                            else client
                                        )
                                        _log(
                                            f"[SSG] 세분류 검증 차단, "
                                            f"60초 대기 후 재시도 ({_retry + 1}/2)"
                                        )
                                        await _asyncio.sleep(60.0)
                                        try:
                                            r = await _next_client.get(
                                                leaf_url, headers=self._headers()
                                            )
                                        except Exception:
                                            break
                                        _retry += 1
                                    if r.status_code not in (200,):
                                        logger.warning(
                                            f"[SSG] leaf {leaf_ctg_id} 차단 지속, 제외"
                                        )
                                        await _asyncio.sleep(_leaf_delay)
                                        continue
                                if r.status_code == 200:
                                    # repBrandId 필터 결과의 area_count로 판정
                                    # brandFilter 사이드바는 ctgId와 무관하게 동일 반환되어 사용 불가
                                    count = self._parse_area_count(r.text)
                                    if count > 0:
                                        brand_leaf_ids.add(leaf_ctg_id)
                                        logger.debug(
                                            f"[SSG] leaf {leaf_ctg_id} valid "
                                            f"({leaf.get('path', '')}) count={count}"
                                        )
                            except Exception as exc:
                                logger.debug(
                                    f"[SSG] leaf 검증 실패 {leaf_ctg_id}: {exc}"
                                )
                            # 10개마다 진행률 로그
                            _done = _leaf_idx + 1
                            if _done % 10 == 0 or _done == len(candidate_leaves):
                                _log(
                                    f"[SSG] 세분류 검증 중 {_done}/{len(candidate_leaves)} "
                                    f"(확정 {len(brand_leaf_ids)}개)"
                                )
                            await _asyncio.sleep(_leaf_delay)
                    finally:
                        for _pc in _proxy_clients:
                            await _pc.aclose()

                    _log(
                        f"[SSG] 세분류 검증 완료: {len(candidate_leaves)}개 → "
                        f"{len(brand_leaf_ids)}개 그룹"
                    )

                    filtered_leaves = [
                        c
                        for c in all_categories
                        if c.get("categoryCode", "") in brand_leaf_ids
                    ]
                    logger.info(
                        f"[SSG] leaf 필터링: 전체 {len(all_categories)}개 → "
                        f"최종 {len(filtered_leaves)}개"
                    )

                    if filtered_leaves:
                        filtered_leaves.sort(key=lambda x: -x["count"])
                        raw_total = sum(c["count"] for c in filtered_leaves)
                        # brand_total로 비율 스케일링 (비브랜드 count 과대계산 보정)
                        if brand_total > 0 and raw_total > 0:
                            ratio = brand_total / raw_total
                            for c in filtered_leaves:
                                c["count"] = max(1, round(c["count"] * ratio))
                            total = brand_total
                        else:
                            total = raw_total
                        _log(
                            f"[SSG] 카테고리 스캔 완료: "
                            f"{len(filtered_leaves)}개 그룹, {total:,}건"
                        )
                        return {
                            "categories": filtered_leaves,
                            "total": total,
                            "groupCount": len(filtered_leaves),
                        }

                    # 브랜드 매칭 leaf 0개 — 빈 결과 반환 (전체 fallback 금지)
                    # 전체 leaf 폴백하면 하위브랜드(나이키키즈 등)가 그룹으로 생성됨
                    logger.warning(
                        f"[SSG] 브랜드 매칭 leaf 0개 — 빈 결과 반환 "
                        f"(brand_leaf_ids={len(brand_leaf_ids)}, valid_tops={len(valid_tops)})"
                    )
                    return {"categories": [], "total": 0, "groupCount": 0}

                # brand_ids 없거나 브랜드 프로빙 결과 없음: 전체 leaf 그대로 반환
                categories = [c for c in all_categories if c.get("count", 0) > 0]
                categories.sort(key=lambda x: -x["count"])
                total = sum(c["count"] for c in categories)
                logger.info(
                    f'[SSG] 카테고리 스캔 완료: "{keyword}" → '
                    f"{len(categories)}개 카테고리, {total}건"
                )
                return {
                    "categories": categories,
                    "total": total,
                    "groupCount": len(categories),
                }

        except RateLimitError:
            raise
        except Exception as e:
            logger.error(f"[SSG] 카테고리 스캔 실패: {keyword} — {e}")
            return {"categories": [], "total": 0, "groupCount": 0}

    def _extract_category_filters(
        self, html: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """__NEXT_DATA__에서 카테고리 필터 목록 추출.

        SSG categoryFilter는 트리 구조(dispCtgLvl 1→2→3)로 제공된다.
        각 노드에 dispCtgId(카테고리코드)와 itemCount(상품수)가 포함됨.
        leaf 카테고리까지 플래튼하여 롯데ON과 동일한 세분화 결과를 반환한다.

        Returns:
            tuple:
              - leaf_categories: [{"categoryCode": "6000206018", "path": "...",
                "count": 282, "category1": "스포츠웨어/슈즈", ...}]
              - top_categories: [{"name": "스포츠웨어/슈즈", "ctgId": "6000205962", "count": 1957}]
        """
        m = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not m:
            return [], []

        try:
            next_data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return [], []

        queries = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("dehydratedState", {})
            .get("queries", [])
        )

        # categoryFilter 데이터 추출
        _CATEGORY_KEYWORDS = ("ctg", "category", "카테고리")
        cat_filter_data: list[dict] = []

        for q in queries:
            if "useTemplateFilterQuery" not in (q.get("queryKey") or []):
                continue
            filters_data = q.get("state", {}).get("data") or []

            all_types = [f.get("filterType", "") for f in filters_data]
            logger.debug(f"[SSG] __NEXT_DATA__ filterTypes: {all_types}")

            for f in filters_data:
                ft = (f.get("filterType") or "").lower()
                if ft == "brandfilter":
                    continue
                if any(kw in ft for kw in _CATEGORY_KEYWORDS):
                    for unit in f.get("unitList", []):
                        cat_filter_data.extend(unit.get("dataList", []))
                    break

        if not cat_filter_data:
            return [], []

        # 대분류(level-1) 노드 목록 생성
        top_categories: list[dict[str, Any]] = []
        for item in cat_filter_data:
            top_name = (item.get("name") or "").strip()
            top_ctg_id = str(item.get("dispCtgId") or "").strip()
            top_count = int(float(item.get("itemCount") or 0))
            if top_name and top_ctg_id:
                top_categories.append(
                    {"name": top_name, "ctgId": top_ctg_id, "count": top_count}
                )

        # 트리 플래튼 — leaf 카테고리(자식 없는 노드)까지 재귀 탐색
        categories: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _add_category(ctg_id: str, current_path: list[str], count: int) -> None:
            """카테고리 결과에 추가 (중복/0건 제외)."""
            if not ctg_id or ctg_id in seen or count <= 0:
                return
            seen.add(ctg_id)
            c1 = current_path[0] if len(current_path) > 0 else ""
            c2 = current_path[1] if len(current_path) > 1 else ""
            c3 = current_path[2] if len(current_path) > 2 else ""
            categories.append(
                {
                    "categoryCode": ctg_id,
                    "path": " > ".join(current_path),
                    "count": count,
                    "category1": c1,
                    "category2": c2,
                    "category3": c3,
                }
            )

        def _flatten(node: dict, ancestors: list[str]) -> None:
            """카테고리 트리를 재귀적으로 플래튼."""
            name = (node.get("name") or "").strip()
            ctg_id = str(node.get("dispCtgId") or "").strip()
            count = int(float(node.get("itemCount") or 0))
            children = node.get("childList") or []

            if not name:
                return

            current_path = ancestors + [name]

            if children:
                # 자식이 있으면 재귀 탐색
                before = len(categories)
                for child in children:
                    _flatten(child, current_path)
                # 자식 재귀로 아무것도 추가되지 않았으면 현재 노드를 leaf로 추가
                if len(categories) == before:
                    _add_category(ctg_id, current_path, count)
            else:
                # leaf 노드
                _add_category(ctg_id, current_path, count)

        for item in cat_filter_data:
            _flatten(item, [])

        # leaf가 없는 경우(childList 구조가 아닐 때) — 1단계 카테고리로 폴백
        if not categories:
            for item in cat_filter_data:
                name = (item.get("name") or "").strip()
                ctg_id = str(item.get("dispCtgId") or "").strip()
                count = int(item.get("itemCount") or 0)
                if not ctg_id or ctg_id in seen or count <= 0:
                    continue
                seen.add(ctg_id)
                categories.append(
                    {
                        "categoryCode": ctg_id,
                        "path": name,
                        "count": count,
                        "category1": name,
                        "category2": "",
                        "category3": "",
                    }
                )

        return categories, top_categories

    def _extract_brand_filtered_item_ids(
        self,
        html: str,
        keyword: str,
        allowed_brand_ids: set[str],
        limit: int = 10,
    ) -> list[str]:
        """probe 응답에서 brand가 정확히 일치하는 상품의 itemId 목록 추출.

        SSG dataList에는 dispCtgId가 없으므로, brand가 일치하는 itemId만 뽑아
        호출 측에서 detail 조회로 실제 dispCtgId를 샘플링한다.

        매칭 규칙:
          - item의 brandId가 allowed_brand_ids에 포함 → 통과
          - brandId 없으면 brandName이 keyword와 정확 일치 → 통과
        """
        m = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not m:
            return set()
        try:
            next_data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return set()

        queries = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("dehydratedState", {})
            .get("queries", [])
        )

        keyword_norm = str(keyword or "").strip()
        result: list[str] = []

        for q in queries:
            qkey = q.get("queryKey") or []
            if "fetchSearchItemListArea" not in qkey:
                continue
            area_list = q.get("state", {}).get("data", {}).get("areaList", [])
            for area in area_list:
                if area.get("unitType") != "ITEM_UNIT_LIST":
                    continue
                for item in area.get("dataList") or []:
                    bid = str(
                        item.get("repBrandId")
                        or item.get("brandId")
                        or item.get("brdId")
                        or ""
                    ).strip()
                    bname = str(item.get("brandName") or "").strip()
                    if bid:
                        if bid not in allowed_brand_ids:
                            continue
                    else:
                        if not keyword_norm or bname != keyword_norm:
                            continue
                    item_id = str(item.get("itemId") or "").strip()
                    if item_id:
                        result.append(item_id)
                        if len(result) >= limit:
                            return result
                break
            if result:
                break

        return result

    def _extract_category_dist_from_items(self, html: str) -> dict[str, int]:
        """브랜드 필터 적용된 검색 결과의 상품 dataList에서 dispCtgId 분포 추출.

        SSG categoryFilter는 repBrandId를 반영하지 않으므로,
        실제 상품 item 데이터에서 dispCtgId를 추출해 카테고리별 분포를 샘플링한다.
        1페이지(40개) 샘플로 브랜드별 카테고리 분포 비율을 추정.

        Returns:
            {dispCtgId: count} 딕셔너리 (비어있으면 샘플링 불가)
        """
        m = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not m:
            return {}
        try:
            next_data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return {}

        queries = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("dehydratedState", {})
            .get("queries", [])
        )

        dist: dict[str, int] = {}
        for q in queries:
            qkey = q.get("queryKey") or []
            if "fetchSearchItemListArea" not in qkey:
                continue
            area_list = q.get("state", {}).get("data", {}).get("areaList", [])
            for area in area_list:
                if area.get("unitType") != "ITEM_UNIT_LIST":
                    continue
                for item in area.get("dataList") or []:
                    # SSG 검색결과 상품에 dispCtgId 필드가 있는지 시도
                    ctg_id = str(
                        item.get("dispCtgId")
                        or item.get("dispCtgCd")
                        or item.get("ctgId")
                        or item.get("categoryId")
                        or ""
                    ).strip()
                    if ctg_id:
                        dist[ctg_id] = dist.get(ctg_id, 0) + 1
                break
            if dist:
                break

        return dist

    async def _sample_category_dist(
        self,
        client: "httpx.AsyncClient",
        item_ids: list[str],
    ) -> dict[str, int]:
        """상품 ID 목록에서 detail 요청으로 dispCtgId 분포를 샘플링.

        SSG search result에 dispCtgId가 없으므로
        상위 N개 상품의 detail 페이지에서 dispCtgId를 직접 조회한다.
        요청 간 0.5초 대기 (차단 방지).

        Returns:
            {dispCtgId: count} 딕셔너리
        """
        import asyncio

        dist: dict[str, int] = {}
        for item_id in item_ids:
            try:
                det = await self.get_product_detail(item_id, _shared_client=client)
                ctg_id = str(det.get("dispCtgId") or "").strip()
                if ctg_id:
                    dist[ctg_id] = dist.get(ctg_id, 0) + 1
                    logger.debug(f"[SSG] 샘플 상품 {item_id} → dispCtgId={ctg_id}")
            except Exception as exc:
                logger.debug(f"[SSG] 샘플 detail 실패 {item_id}: {exc}")
            await asyncio.sleep(0.5)
        return dist

    def _parse_area_count(self, html: str) -> int:
        """__NEXT_DATA__에서 검색 결과 상품 수 파싱.

        PAGING_UNIT.itemCount 우선 사용,
        결과 0건이면 unitText[item_cnt]로 폴백.
        """
        m = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not m:
            return 0
        try:
            next_data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return 0

        queries = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("dehydratedState", {})
            .get("queries", [])
        )

        for q in queries:
            qk = q.get("queryKey") or []
            if "fetchSearchItemListArea" not in qk:
                continue
            data = q.get("state", {}).get("data")
            if not isinstance(data, dict):
                continue

            area_list = data.get("areaList", [])

            # 1순위: PAGING_UNIT.itemCount
            for area in area_list:
                if area.get("unitType") == "PAGING_UNIT":
                    return int(area.get("itemCount", 0))

            # 2순위: unitText에서 item_cnt 추출
            for area in area_list:
                for unit_text in area.get("unitText") or []:
                    if unit_text.get("type") == "item_cnt":
                        return int(unit_text.get("value", 0))

        return 0

    def _parse_search_html(self, html: str, keyword: str) -> list[dict[str, Any]]:
        """검색 결과 HTML에서 상품 정보 추출.

        1순위: Next.js script 태그 내 dataList JSON 파싱
        2순위: 상품 링크 + HTML 블록 파싱 폴백
        """
        # 1순위: dataList JSON 추출 (ITEM_UNIT_LIST 블록)
        products = self._parse_datalist_json(html)
        if products:
            return products

        # __NEXT_DATA__가 있으면 페이지 정상 로드 — 상품이 없는 것 뿐.
        # 폴백 파서(_parse_search_blocks)가 UI 요소를 상품으로 오인해
        # 무한루프 + CPU 100%를 유발했음 (이슈 #263, 2026-05-27).
        if re.search(r'<script[^>]+id="__NEXT_DATA__"', html):
            return []

        # 2순위: __NEXT_DATA__ 미존재 시에만 폴백 (페이지 구조 변경 대응)
        logger.warning(f"[SSG] dataList 파싱 실패, 폴백 파싱 시도: {keyword}")
        return self._parse_search_blocks(html)

    def _parse_datalist_json(self, html: str) -> list[dict[str, Any]]:
        """department.ssg.com 검색 HTML의 __NEXT_DATA__ script 태그에서 상품 목록 추출.

        구조: <script id="__NEXT_DATA__" type="application/json">{...}</script>
             → props.pageProps.dehydratedState.queries
             → [fetchSearchItemListArea].state.data.areaList
             → [unitType==ITEM_UNIT_LIST].dataList
        """
        # __NEXT_DATA__ script 태그 추출
        m = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not m:
            return []

        try:
            next_data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

        queries = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("dehydratedState", {})
            .get("queries", [])
        )

        data_list: list[dict] = []
        for q in queries:
            qkey = q.get("queryKey") or []
            if "fetchSearchItemListArea" not in qkey:
                continue
            area_list = q.get("state", {}).get("data", {}).get("areaList", [])
            for area in area_list:
                if area.get("unitType") == "ITEM_UNIT_LIST":
                    data_list = area.get("dataList") or []
                    break
            if data_list:
                break

        if not data_list:
            return []

        products: list[dict[str, Any]] = []
        seen: set[str] = set()
        skipped_other_site = 0
        skipped_deal_item = 0

        for item in data_list:
            item_id = str(item.get("itemId", ""))
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)

            # siteNo 필터: 신세계백화점(6009) 상품만 통과.
            # SSG 통합검색은 신세계몰(6004)/이마트몰 상품까지 섞어 반환하므로
            # 여기서 걸러내지 않으면 카테고리 체계가 달라 라우팅이 깨진다.
            raw_site_no = str(item.get("siteNo", "")).strip()
            if raw_site_no and raw_site_no != self.SITE_NO:
                skipped_other_site += 1
                continue

            # 모음전(기획전) 제외: itemDetailLink가 /dealItemView 경로인 경우만 제외.
            # salestrNo는 SSG가 일반 상품에도 채워 반환하기 시작했으므로 판별 기준에서 제거.
            _detail_link = str(
                item.get("itemDetailLink", "") or item.get("itemUrl", "") or ""
            )
            _is_deal_item = "dealItemView" in _detail_link
            if _is_deal_item:
                skipped_deal_item += 1
                continue

            # www.ssg.com(신세계몰 일반/개인 판매자 상품) 제외 — 백화점 상품(department.ssg.com)만 수집.
            # siteNo 필터가 비어있을 때를 대비한 URL 기반 추가 가드.
            if "www.ssg.com" in _detail_link:
                skipped_other_site += 1
                continue

            item_name = item.get("itemName", "").strip()
            if not item_name:
                continue

            # 가격 파싱 (문자열 "135,360" → 정수 135360)
            sale_price = self._safe_int(
                str(item.get("finalPrice", "") or item.get("sellprc", 0)).replace(
                    ",", ""
                )
            )
            original_price = (
                self._safe_int(
                    str(
                        item.get("strikeOutPrice", "") or item.get("norprc", 0)
                    ).replace(",", "")
                )
                or sale_price
            )

            # 할인율 (문자열 "20" 또는 "20%" → 정수 20)
            discount_rate_raw = str(item.get("discountRate", "0")).replace("%", "")
            discount_rate = self._safe_int(discount_rate_raw)

            # 이미지 URL
            image = self._normalize_image(item.get("itemImgUrl", ""))

            # 무료배송 여부
            shipping_list = (
                item.get("shippingCostInfo") or item.get("itemFeatureList") or []
            )
            free_shipping = any(
                "무료배송" in str(s.get("text", "")) for s in shipping_list
            )

            # 품절 여부
            is_sold_out = bool(item.get("soldOutMessage", "").strip())

            # itemUrl이 department.ssg.com 도메인인지 확인
            item_url = (
                item.get("itemDetailLink")
                or item.get("itemUrl")
                or (f"{self.ITEM_URL}?itemId={item_id}&siteNo={self.SITE_NO}")
            )

            # brandId 추출 (post-filter에서 하위 브랜드 제외용)
            rep_brand_id = str(
                item.get("repBrandId") or item.get("brandId") or item.get("brdId") or ""
            ).strip()

            products.append(
                {
                    "siteProductId": item_id,
                    "goodsNo": item_id,
                    "name": item_name,
                    "brand": item.get("brandName", ""),
                    "brandEngNm": item.get("brandEngNm", ""),
                    "repBrandId": rep_brand_id,
                    "brandId": rep_brand_id,
                    "salePrice": sale_price,
                    "originalPrice": original_price,
                    "discountRate": discount_rate,
                    "image": image,
                    "freeShipping": free_shipping,
                    "isSoldOut": is_sold_out,
                    "sourceUrl": item_url,
                    "siteNo": item.get("siteNo", self.SITE_NO),
                    "salestrNo": str(item.get("salestrNo", "")),
                }
            )

        if skipped_other_site:
            logger.info(
                f"[SSG] 타 사이트 상품 {skipped_other_site}건 제외 "
                f"(신세계백화점 siteNo={self.SITE_NO} 외)"
            )
        if skipped_deal_item:
            logger.info(
                f"[SSG] 모음전(기획전) {skipped_deal_item}건 제외 "
                f"(salestrNo/dealItemView)"
            )

        return products

    def _parse_search_blocks(self, html: str) -> list[dict[str, Any]]:
        """검색 결과 HTML 블록에서 상품 정보 추출 (폴백)."""
        products: list[dict[str, Any]] = []
        seen: set[str] = set()

        item_pattern = re.compile(
            r"/itemView\.ssg\?itemId=(\d{10,13})",
            re.IGNORECASE,
        )

        for item_id in item_pattern.findall(html):
            if item_id in seen:
                continue
            seen.add(item_id)
            products.append(
                {
                    "siteProductId": item_id,
                    "goodsNo": item_id,
                    "name": "",
                    "brand": "",
                    "salePrice": 0,
                    "originalPrice": 0,
                    "image": "",
                    "isSoldOut": False,
                    "sourceUrl": f"{self.ITEM_URL}?itemId={item_id}",
                }
            )

        return products

    # ------------------------------------------------------------------
    # 상세 조회
    # ------------------------------------------------------------------

    async def get_product_detail(
        self,
        item_id: str,
        refresh_only: bool = False,
        _shared_client: Optional[httpx.AsyncClient] = None,
    ) -> dict[str, Any]:
        """SSG 상품 상세 정보 조회.

        1순위: var resultItemObj JS 변수 파싱 (가장 안정적)
        2순위: og: 메타태그 + CSS 클래스 패턴 폴백

        Args:
            item_id: SSG 상품 ID (13자리 숫자)
            refresh_only: True이면 가격/재고만 빠르게 갱신
            _shared_client: TCP 연결 재사용용 공유 클라이언트 (대량 수집 성능 향상)

        Returns:
            표준 상품 상세 dict (무신사 프록시 반환 형식과 동일)

        Raises:
            RateLimitError: 429/403 응답 시
        """
        url = f"{self.ITEM_URL}?itemId={item_id}&siteNo={self.SITE_NO}"
        logger.info(f"[SSG] 상세 조회: {item_id}")

        _client_kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "follow_redirects": True,
        }
        if self.proxy_url:
            _client_kwargs["proxy"] = self.proxy_url

        async def _fetch(client: httpx.AsyncClient) -> str:
            resp = await client.get(url, headers=self._headers())
            if resp.status_code in (429, 403):
                retry_after = int(resp.headers.get("Retry-After", "60"))
                logger.warning(f"[SSG] 차단 감지 HTTP {resp.status_code}: {item_id}")
                raise RateLimitError(resp.status_code, retry_after)
            if resp.status_code != 200:
                logger.warning(f"[SSG] 상세 페이지 HTTP {resp.status_code}: {item_id}")
                return ""
            return resp.text

        try:
            if _shared_client:
                html = await _fetch(_shared_client)
            else:
                async with httpx.AsyncClient(**_client_kwargs) as client:
                    html = await _fetch(client)

            if not html:
                return {}

            # 임직원/사업자 회원 전용 상품 차단 — 일반 고객 구매 불가하여 마켓 등록·오토튠 무의미
            if _is_staff_only(html):
                logger.info(f"[SSG] 임직원 전용 상품 수집 차단: {item_id}")
                return {}

            # 1순위: resultItemObj JS 변수 파싱
            result = self._parse_result_item_obj(html, item_id, refresh_only)
            if result:
                logger.info(f"[SSG] resultItemObj 파싱 성공: {item_id}")
                return result

            # 2순위: 메타태그 + CSS 패턴 폴백
            logger.warning(f"[SSG] resultItemObj 없음, 폴백 파싱: {item_id}")
            return self._parse_detail_fallback(html, item_id)

        except RateLimitError:
            raise
        except httpx.TimeoutException:
            logger.error(f"[SSG] 상세 조회 타임아웃: {item_id}")
            return {}
        except Exception as e:
            logger.error(f"[SSG] 상세 조회 실패: {item_id} — {e}")
            return {}

    def _parse_result_item_obj(
        self,
        html: str,
        item_id: str,
        refresh_only: bool,
        dom_breadcrumb: list[str] | None = None,
    ) -> dict[str, Any]:
        """var resultItemObj JS 변수에서 상품 정보 추출 (1순위).

        SSG HTML의 resultItemObj는 parseInt() 등 JS 표현식이 포함된 객체 리터럴이므로
        JSON 파싱 대신 개별 필드 직접 추출 방식을 사용한다.
        """
        # 임직원/사업자 회원 전용 상품 차단 — 확장앱 경로(로그인 브라우저)에서도
        # 동일하게 alert 페이지(title=flagMsg, 본문에 "임직원 및 사업자 회원" 문구)가 내려오므로
        # 모든 호출자에서 일관 차단되도록 진입부에서 검사한다.
        if _is_staff_only(html):
            logger.info(f"[SSG] 임직원 전용 상품 수집 차단(parser): {item_id}")
            return {}
        # resultItemObj 블록 추출 (브라켓 카운터)
        start_marker = re.search(r"var\s+resultItemObj\s*=\s*\{", html)
        if not start_marker:
            return {}

        start = start_marker.end() - 1  # '{' 포함
        depth = 0
        end = start
        i = start
        while i < len(html):
            ch = html[i]
            if ch == "\\":
                i += 2
                continue
            if ch in ('"', "'"):
                q = ch
                i += 1
                while i < len(html) and html[i] != q:
                    if html[i] == "\\":
                        i += 1
                    i += 1
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            i += 1

        if end <= start:
            return {}

        js_block = html[start:end]

        # uitemObjList 원본 블록에서 먼저 추출 (옵션 목록)
        # uitemObjArr.push 패턴은 resultItemObj 블록 밖에 있으므로 js_block 실패 시 html 전체 재시도
        uitem_list = self._extract_uitem_list(js_block)
        if not uitem_list:
            uitem_list = self._extract_uitem_list(html)

        # 중첩된 객체/배열 제거 — 연관상품(itemAssocList), 비교상품(cmptItemObjMap) 등의
        # dispCtgLclsNm 등 내부 필드가 상위 필드보다 먼저 매칭되어 카테고리 오염되는
        # 버그 방지. 최상위(depth=1) 필드만 남긴 블록을 사용해 필드 추출.
        js_top = self._strip_nested_structures(js_block)

        def get_str(key: str) -> str:
            return self._extract_js_str_field(js_top, key)

        def get_num(key: str) -> int:
            return self._extract_js_num_field(js_top, key)

        # 필수 필드 확인
        name = get_str("itemNm")
        if not name:
            logger.warning(f"[SSG] resultItemObj에서 itemNm 추출 실패: {item_id}")
            return {}

        obj = {
            "itemNm": name,
            "repBrandNm": get_str("repBrandNm") or get_str("brandNm"),
            "repBrandId": get_str("repBrandId") or get_str("brandId"),
            "sellprc": get_num("sellprc"),
            "bestAmt": get_num("bestAmt"),
            "soldOut": get_str("soldOut"),
            "stdCtgLclsNm": get_str("stdCtgLclsNm"),
            "stdCtgMclsNm": get_str("stdCtgMclsNm"),
            "stdCtgSclsNm": get_str("stdCtgSclsNm"),
            "stdCtgDclsNm": get_str("stdCtgDclsNm"),
            # 전시카테고리 레벨명 — 있으면 stdCtg보다 우선 사용
            "dispCtgLclsNm": get_str("dispCtgLclsNm"),
            "dispCtgMclsNm": get_str("dispCtgMclsNm"),
            "dispCtgSclsNm": get_str("dispCtgSclsNm"),
            "dispCtgDclsNm": get_str("dispCtgDclsNm"),
            # dispCtgId는 JS에서 따옴표 없는 숫자 또는 parseInt() 형태이므로
            # get_str 실패 시 get_num으로 폴백하여 카테고리 매칭 보장
            "dispCtgId": get_str("dispCtgId") or str(get_num("dispCtgId") or ""),
            "dispCtgNm": get_str("dispCtgNm"),
            "itemImgUrl": get_str("itemImgUrl"),
            "shppTypeDtlCd": get_str("shppTypeDtlCd"),
            "deliType": get_str("deliType"),
            "uitemObjList": uitem_list,
        }

        now_iso = datetime.now(tz=timezone.utc).isoformat()
        timestamp = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

        # refresh_only + 전품절 여부 사전 판단 — 고시정보/이미지 파싱 스킵용
        _is_early_soldout = refresh_only and (
            str(obj.get("soldOut", "N")).upper() == "Y"
            or (
                uitem_list
                and all(str(u.get("usablInvQty", "0")) == "0" for u in uitem_list)
            )
        )

        # 기본 필드
        name = obj.get("itemNm", "").strip()
        brand = obj.get("repBrandNm") or obj.get("brandNm", "")
        brand_code = str(obj.get("repBrandId") or obj.get("brandId", ""))

        # department.ssg.com: resultItemObj.sellprc = 정상가 (할인 전 원가)
        # 실제 할인가(최적가)는 HTML cdtl_price point 클래스에 렌더링됨
        original_price = self._safe_int(obj.get("sellprc", 0))
        sale_price_html = self._extract_dept_sale_price(html)
        sell_price = sale_price_html if sale_price_html else original_price

        # 할인율 계산
        discount_rate = 0
        if original_price > 0 and sell_price < original_price:
            discount_rate = round((original_price - sell_price) / original_price * 100)

        # 카드혜택가 우선 (JS bestAmt는 일반 최적가이므로 카드혜택가보다 높을 수 있음)
        _card_price = self._extract_card_benefit_price(html)
        best_amt = _card_price or self._safe_int(obj.get("bestAmt", 0)) or sell_price

        # 품번(style_code): 상품명 패턴 우선(정확), 없으면 HTML 모델번호 폴백(부정확할 수 있음)
        _sc_match = re.search(r"[A-Za-z]{1,3}\d{3,}[-]\d{2,}", name)
        if _sc_match:
            _style_code = _sc_match.group(0)
        else:
            _mdl_match = re.search(r"모델번호\s*[:：]\s*(\S+)", html)
            _style_code = _mdl_match.group(1).strip() if _mdl_match else ""

        # 고시정보 파싱 (색상, 제조국, 재질 등)
        # refresh_only + 전품절이면 스킵 (HTML 전체 regex 비용 절감)
        _prod_info = {} if _is_early_soldout else self._parse_product_notice(html)

        # 품절 판단: soldOut 필드 (Y/N)
        is_sold_out = str(obj.get("soldOut", "N")).upper() == "Y"

        # 카테고리: 전시카테고리(dispCtg) 우선 — 카테고리 스캔과 동일 체계
        # stdCtg(표준 카테고리)는 스캔 결과와 불일치하므로 최후 폴백으로만 사용
        #
        # 우선순위:
        #   1순위: resultItemObj의 dispCtgLclsNm/Mcls/Scls/Dcls (전시카테고리 레벨명)
        #   2순위: HTML breadcrumb ("신세계백화점 / ..." 패턴)
        #   3순위: dispCtgNm 단일명
        #   최후: stdCtg (표준카테고리)
        _disp_lcls = obj.get("dispCtgLclsNm", "")
        _disp_mcls = obj.get("dispCtgMclsNm", "")
        _disp_scls = obj.get("dispCtgSclsNm", "")
        _disp_dcls = obj.get("dispCtgDclsNm", "")

        # 0순위: 확장앱이 직접 보낸 DOM breadcrumb (body HTML이 없어 backend regex가
        # 못 잡는 경우 대비). leaf-only 저장 사고 차단용.
        _dom_bc = [
            (p or "").strip()
            for p in (dom_breadcrumb or [])
            if (p or "").strip()
            and (p or "").strip() not in ("신세계백화점", "SSG", "SSG.COM")
        ]
        if _dom_bc and len(_dom_bc) >= 2:
            cat1 = _dom_bc[0] if len(_dom_bc) > 0 else ""
            cat2 = _dom_bc[1] if len(_dom_bc) > 1 else ""
            cat3 = _dom_bc[2] if len(_dom_bc) > 2 else ""
            cat4 = _dom_bc[3] if len(_dom_bc) > 3 else ""
            logger.debug(
                f"[SSG] 카테고리 확장앱 DOM breadcrumb 사용: {cat1}>{cat2}>{cat3}"
            )
        elif _disp_lcls:
            # 1순위: dispCtg 레벨명 (가장 정확한 전시카테고리)
            cat1 = _disp_lcls
            cat2 = _disp_mcls
            cat3 = _disp_scls
            cat4 = _disp_dcls
            logger.debug(f"[SSG] 카테고리 dispCtg 레벨명 사용: {cat1}>{cat2}>{cat3}")
        else:
            # 2순위: 상품 상세 HTML의 "카테고리 로케이션" 브레드크럼 DOM 파싱.
            # resultItemObj에는 dispCtgNm(리프)만 있고 상위 레벨이 없는 경우가 대부분이므로
            # UI로 렌더링되는 lo_depth_XX 링크에서 대/중/소/세 카테고리를 순차 추출한다.
            # 예: "남성패션 > 맨투맨/후드/티셔츠 > 반팔티셔츠"
            _loc_parts: list[str] = []
            if not _is_early_soldout:
                for _lv in ("대", "중", "소", "세"):
                    _m = re.search(
                        rf'data-react-tarea="[^"]*카테고리 로케이션\|{_lv}카테고리"'
                        r"[^>]*>\s*([^<]+?)\s*</a>",
                        html,
                    )
                    if _m:
                        _loc_parts.append(_m.group(1).strip())
                    else:
                        break

            if _loc_parts:
                cat1 = _loc_parts[0] if len(_loc_parts) > 0 else ""
                cat2 = _loc_parts[1] if len(_loc_parts) > 1 else ""
                cat3 = _loc_parts[2] if len(_loc_parts) > 2 else ""
                cat4 = _loc_parts[3] if len(_loc_parts) > 3 else ""
                logger.debug(f"[SSG] 카테고리 로케이션 DOM 사용: {cat1}>{cat2}>{cat3}")
            else:
                # 3순위: 구 버전 "신세계백화점 / ..." breadcrumb 정규식 (호환)
                if not _is_early_soldout:
                    _bc_match = re.search(
                        r"신세계백화점\s*[/>\s]+\s*(.+?)(?:<|$)",
                        html[:30000],
                    )
                    if _bc_match:
                        _bc_parts = [
                            p.strip()
                            for p in re.sub(r"<[^>]+>", "", _bc_match.group(1)).split(
                                "/"
                            )
                            if p.strip()
                        ]
                        if len(_bc_parts) == 1:
                            _bc_parts = [
                                p.strip() for p in _bc_parts[0].split(">") if p.strip()
                            ]
                    else:
                        _bc_parts = []
                else:
                    _bc_parts = []

                if _bc_parts:
                    cat1 = _bc_parts[0] if len(_bc_parts) > 0 else ""
                    cat2 = _bc_parts[1] if len(_bc_parts) > 1 else ""
                    cat3 = _bc_parts[2] if len(_bc_parts) > 2 else ""
                    cat4 = _bc_parts[3] if len(_bc_parts) > 3 else ""
                else:
                    # 4순위: dispCtgNm 단일명
                    _disp_nm = obj.get("dispCtgNm", "")
                    if _disp_nm:
                        cat1 = _disp_nm
                        cat2 = cat3 = cat4 = ""
                    else:
                        # 최후 폴백: stdCtg (표준카테고리)
                        cat1 = obj.get("stdCtgLclsNm", "")
                        cat2 = obj.get("stdCtgMclsNm", "")
                        cat3 = obj.get("stdCtgSclsNm", "")
                        cat4 = obj.get("stdCtgDclsNm", "")

        disp_ctg_id = str(obj.get("dispCtgId") or "")
        category_levels = [c for c in [cat1, cat2, cat3, cat4] if c]
        if not category_levels:
            disp_ctg_nm = obj.get("dispCtgNm", "")
            if disp_ctg_nm:
                category_levels = [disp_ctg_nm]
        category_str = " > ".join(category_levels)

        # 이미지: itemImgUrl 에서 _i1_36.jpg → _i{N}_1200.jpg 패턴으로 재구성
        # refresh_only + 전품절이면 이미지 파싱 스킵
        images = (
            []
            if _is_early_soldout
            else self._build_images_from_base_url(
                obj.get("itemImgUrl", ""), item_id, html
            )
        )

        # 상세 이미지 (갱신 모드에서는 스킵)
        detail_images: list[str] = []
        detail_html = ""
        if not refresh_only:
            detail_html, detail_images = self._parse_detail_content(html)

        # 옵션/재고: uitemObjList 파싱, 비어있으면 HTML <select> 태그 폴백
        # department.ssg.com: SSR의 uitemObjList는 낙관값(stock=99)이므로
        # <select id="ordOpt1">의 "(매진)" 텍스트로 실제 품절 상태 보완
        options = self._parse_uitem_options(obj)
        if not options:
            options = self._parse_layered_select_options(html, base_price=sell_price)
            # 가격 정보 없는 옵션(추가가=0 등)에 sell_price 채움
            for _opt in options:
                if not _opt.get("price"):
                    _opt["price"] = sell_price
        else:
            # uitemObjList가 있어도 select의 "(매진)" 정보로 실제 품절 상태 보완
            # (SSR HTML의 uitemObjList는 실시간 재고 미반영)
            _select_opts = self._parse_layered_select_options(html)
            if _select_opts:
                _soldout_names = {o["name"] for o in _select_opts if o.get("isSoldOut")}
                if _soldout_names:
                    for _opt in options:
                        if _opt.get("name") in _soldout_names:
                            _opt["isSoldOut"] = True
                            _opt["stock"] = 0
        # 옵션 재고가 있으면 상품 전체 품절 플래그보다 옵션 재고를 우선한다.
        # SSG soldOut/Y 또는 soldOutMessage가 stale인 경우가 있어 옵션 보정 후 재계산이 필요하다.
        if options:
            has_saleable_option = any(
                (not opt.get("isSoldOut", False)) and (opt.get("stock") or 0) > 0
                for opt in options
            )
            if has_saleable_option:
                is_sold_out = False
            elif all(opt.get("isSoldOut", False) for opt in options):
                is_sold_out = True

        # 배송 정보
        shpp_type = str(obj.get("shppTypeDtlCd", ""))
        deli_type = str(obj.get("deliType", ""))
        # shppTypeDtlCd: 22=무료배송, deliType: 10=일반, 20=당일
        free_shipping = shpp_type in ("22",) or bool(
            re.search(r"무료배송", html[:5000])
        )
        same_day_delivery = deli_type == "20" or bool(
            re.search(r"(?:당일배송|쓱배송|새벽배송)", html[:5000])
        )

        # 판매 상태
        sale_status = "sold_out" if is_sold_out else "in_stock"

        return {
            "id": f"col_ssg_{item_id}_{timestamp}",
            "sourceSite": "SSG",
            "siteProductId": str(item_id),
            "sourceUrl": f"{self.BASE}/item/itemView.ssg?itemId={item_id}&siteNo={self.SITE_NO}",
            "name": name,
            "nameEn": "",
            "nameJa": "",
            "brand": brand,
            "brandCode": brand_code,
            "category": category_str,
            "category1": cat1,
            "category2": cat2,
            "category3": cat3,
            "category4": cat4,
            # worker.py 라우팅 2순위가 참조하는 전시카테고리 레벨명 키.
            # 기존엔 원본 JSON 필드 기반이라 None이었지만 이제 브레드크럼 DOM
            # 파싱값으로 채워 라우팅이 경로명 매칭에 성공하도록 한다.
            "dispCtgLclsNm": cat1,
            "dispCtgMclsNm": cat2,
            "dispCtgSclsNm": cat3,
            "dispCtgDclsNm": cat4,
            "images": images[:9],
            "detailImages": detail_images,
            "detailHtml": detail_html,
            "options": options,
            "originalPrice": original_price,
            "salePrice": sell_price,
            "bestBenefitPrice": best_amt,
            "couponPrice": best_amt,
            "memberDiscountRate": 0,
            "discountRate": discount_rate,
            "origin": _prod_info.get("origin", ""),
            "material": _prod_info.get("material", ""),
            "manufacturer": _prod_info.get("manufacturer", ""),
            "color": _prod_info.get("color", ""),
            "sizeInfo": _prod_info.get("sizeInfo", ""),
            "care_instructions": _prod_info.get("care_instructions", ""),
            "quality_guarantee": "",
            "season": "",
            "style_code": _style_code,
            "sex": "",
            "brandNation": "",
            "kcCert": "",
            "dispCtgId": disp_ctg_id,
            "tags": [],
            "isOutOfStock": is_sold_out,
            "isSale": not is_sold_out,
            "saleStatus": sale_status,
            "freeShipping": free_shipping,
            "sameDayDelivery": same_day_delivery,
            "status": "collected",
            "appliedPolicyId": None,
            "marketPrices": {},
            "updateEnabled": True,
            "priceUpdateEnabled": True,
            "stockUpdateEnabled": True,
            "marketTransmitEnabled": True,
            "registeredAccounts": [],
            "collectedAt": now_iso,
            "updatedAt": now_iso,
        }

    def _parse_product_notice(self, html: str) -> dict[str, str]:
        """상세 HTML에서 고시정보(색상, 제조국, 재질, 제조사 등) 파싱.

        SSG 상세 페이지의 <table>(<th>/<td>) 또는 <dl>(<dt>/<dd>) 기반 고시정보에서
        주요 필드를 추출한다.
        """
        info: dict[str, str] = {}

        # th/td 쌍 + dt/dd 쌍 모두 처리
        pairs = re.findall(
            r"<(?:t[hd]|dt|dd)[^>]*>\s*(.*?)\s*</(?:t[hd]|dt|dd)>\s*"
            r"<(?:t[hd]|dt|dd)[^>]*>\s*(.*?)\s*</(?:t[hd]|dt|dd)>",
            html,
            re.DOTALL | re.IGNORECASE,
        )

        for label_raw, value_raw in pairs:
            label = re.sub(r"<[^>]+>", "", label_raw).strip()
            value = re.sub(r"<[^>]+>", "", value_raw).strip()
            if not label or not value:
                continue

            # SSG 고시정보 값에 라벨이 중복 포함됨 (예: "색상:엔트라시트/...")
            # "라벨:" 또는 "라벨 :" 접두어 제거
            _prefix_match = re.match(r"^[가-힣A-Za-z/\s]+[:：]\s*", value)
            if _prefix_match:
                value = value[_prefix_match.end() :].strip()
            if not value or value == "0":
                continue

            lbl = label.replace(" ", "")
            if "색상" in lbl and "color" not in info:
                info["color"] = value
            elif "제조국" in lbl:
                info["origin"] = value
            elif lbl in (
                "제품의주소재",
                "상품의주소재",
                "소재",
                "재질",
                "주소재",
                "제품소재",
            ):
                info["material"] = value
            elif "제조사" in lbl or "수입자" in lbl:
                info["manufacturer"] = value
            elif "치수" in lbl or "사이즈" in lbl:
                info["sizeInfo"] = value
            elif "세탁" in lbl or "취급방법" in lbl:
                info["care_instructions"] = value

        return info

    def _parse_uitem_options(self, obj: dict) -> list[dict[str, Any]]:
        """uitemObjList에서 옵션/재고 파싱."""
        options: list[dict[str, Any]] = []
        uitem_list = obj.get("uitemObjList") or []

        for uitem in uitem_list:
            uitem_id = uitem.get("_uitemId") or uitem.get("uitemId", "")
            # 00000은 옵션 없는 단일 상품 더미 — 실제 옵션이 있으면 스킵
            if uitem_id == "00000" and len(uitem_list) > 1:
                continue

            opt_name = uitem.get("name", "").strip()
            stock = self._safe_int(uitem.get("stock", 0))
            is_soldout = uitem.get("isSoldOut", False) or stock == 0
            sell_price = self._safe_int(uitem.get("price", 0))

            # _parse_raw_uitem_blocks가 이미 optionName1/2, optionDepth 세팅한 경우 그대로 전달
            opt_entry: dict[str, Any] = {
                "name": opt_name,
                "price": sell_price,
                "stock": stock,
                "isSoldOut": bool(is_soldout),
            }
            if uitem.get("optionDepth"):
                opt_entry["optionDepth"] = uitem["optionDepth"]
                if uitem.get("optionName1"):
                    opt_entry["optionName1"] = uitem["optionName1"]
                if uitem.get("optionName2"):
                    opt_entry["optionName2"] = uitem["optionName2"]
                if uitem.get("optionName3"):
                    opt_entry["optionName3"] = uitem["optionName3"]
            elif "/" in opt_name:
                # optionDepth 없이 "/" 포함된 name → 분리해서 depth 추론
                parts = opt_name.split("/", 2)
                opt_entry["optionDepth"] = len(parts)
                for i, p in enumerate(parts, start=1):
                    opt_entry[f"optionName{i}"] = p
            options.append(opt_entry)

        return options

    def _build_images_from_base_url(
        self, base_img_url: str, item_id: str, html: str
    ) -> list[str]:
        """resultItemObj.itemImgUrl에서 상품 이미지 목록 재구성.

        패턴: https://sitem.ssgcdn.com/{path}/item/{itemId}_i{N}_{size}.jpg
        base_img_url 예: .../1000626844250_i1_36.jpg  → _i1_1200.jpg 로 교체 후 i1~i9 시도
        """
        images: list[str] = []

        if base_img_url:
            # _i1_36.jpg → _i1_1200.jpg 변환
            high_res = re.sub(r"_i1_\d+\.jpg", "_i1_1200.jpg", base_img_url)
            if high_res:
                images.append(self._normalize_image(high_res))

        # HTML에서 sitem.ssgcdn.com 이미지 수집으로 보충
        ssgcdn_pattern = re.compile(
            r'["\']?(https://sitem\.ssgcdn\.com/[^"\']+_i\d+_(?:1200|500)\.jpg)["\']?',
            re.IGNORECASE,
        )
        seen = set(images)
        for m in ssgcdn_pattern.finditer(html):
            img = self._normalize_image(m.group(1))
            if img and img not in seen and f"/{item_id}_" in img:
                images.append(img)
                seen.add(img)
                if len(images) >= 9:
                    break

        return [i for i in images if i][:9]

    def _parse_detail_content(self, html: str) -> tuple[str, list[str]]:
        """상세 설명 영역 HTML 및 이미지 추출."""
        detail_html = ""
        detail_images: list[str] = []

        # 상세 설명 영역 추출
        detail_area = re.search(
            r'(?:id="cdtl_desc"|id="detail_cont"|class="[^"]*cdtl_desc[^"]*")[^>]*>(.*?)(?=<div[^>]+(?:id|class)="[^"]*(?:cdtl_review|cdtl_qna|cdtl_notice|footer)[^"]*")',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        if detail_area:
            detail_html = detail_area.group(1)
            img_pat = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)
            for m in img_pat.finditer(detail_html):
                img = self._normalize_image(m.group(1))
                if img and img not in detail_images:
                    detail_images.append(img)

        return detail_html, detail_images

    def _parse_detail_fallback(self, html: str, item_id: str) -> dict[str, Any]:
        """resultItemObj 없을 때 메타태그 + CSS 패턴 폴백."""
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        timestamp = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

        name = self._extract_meta(html, "og:title") or ""
        name = name.replace(" - SSG.COM", "").strip()
        thumbnail = self._normalize_image(self._extract_meta(html, "og:image") or "")

        sale_price = self._parse_sale_price(html)
        original_price = self._parse_original_price(html) or sale_price
        best_benefit_price = self._parse_best_benefit_price(html) or sale_price
        brand = self._parse_brand(html)
        category_levels = self._parse_category(html)

        # HTML에서 dispCtgId 추출 (카테고리 샘플링에 사용)
        _disp_ctg_m = re.search(r'"dispCtgId"\s*[=:]\s*["\']?(\d+)["\']?', html)
        disp_ctg_id = _disp_ctg_m.group(1) if _disp_ctg_m else ""

        images = [thumbnail] if thumbnail else []
        ssgcdn_pat = re.compile(
            r'https://sitem\.ssgcdn\.com/[^"\']+_i\d+_(?:1200|500)\.jpg',
            re.IGNORECASE,
        )
        seen_imgs = set(images)
        for m in ssgcdn_pat.finditer(html):
            img = m.group(0)
            if img not in seen_imgs and f"/{item_id}_" in img:
                images.append(img)
                seen_imgs.add(img)
                if len(images) >= 9:
                    break

        options = self._parse_options(html)
        is_out_of_stock = self._check_sold_out(html, options)

        return {
            "id": f"col_ssg_{item_id}_{timestamp}",
            "sourceSite": "SSG",
            "siteProductId": str(item_id),
            "sourceUrl": f"{self.BASE}/item/itemView.ssg?itemId={item_id}&siteNo={self.SITE_NO}",
            "name": name,
            "nameEn": "",
            "nameJa": "",
            "brand": brand,
            "brandCode": "",
            "category": " > ".join(category_levels),
            "category1": category_levels[0] if len(category_levels) > 0 else "",
            "category2": category_levels[1] if len(category_levels) > 1 else "",
            "category3": category_levels[2] if len(category_levels) > 2 else "",
            "category4": category_levels[3] if len(category_levels) > 3 else "",
            "dispCtgLclsNm": category_levels[0] if len(category_levels) > 0 else "",
            "dispCtgMclsNm": category_levels[1] if len(category_levels) > 1 else "",
            "dispCtgSclsNm": category_levels[2] if len(category_levels) > 2 else "",
            "dispCtgDclsNm": category_levels[3] if len(category_levels) > 3 else "",
            "images": images[:9],
            "detailImages": [],
            "detailHtml": "",
            "options": options,
            "originalPrice": original_price,
            "salePrice": sale_price,
            "bestBenefitPrice": best_benefit_price,
            "couponPrice": best_benefit_price,
            "memberDiscountRate": 0,
            "discountRate": 0,
            "origin": "",
            "material": "",
            "manufacturer": "",
            "color": "",
            "sizeInfo": "",
            "care_instructions": "",
            "quality_guarantee": "",
            "season": "",
            "style_code": "",
            "sex": "",
            "brandNation": "",
            "kcCert": "",
            "dispCtgId": disp_ctg_id,
            "tags": [],
            "isOutOfStock": is_out_of_stock,
            "isSale": not is_out_of_stock,
            "saleStatus": "sold_out" if is_out_of_stock else "in_stock",
            "freeShipping": bool(re.search(r"무료배송", html[:5000])),
            "sameDayDelivery": bool(
                re.search(r"(?:당일배송|쓱배송|새벽배송)", html[:5000])
            ),
            "status": "collected",
            "appliedPolicyId": None,
            "marketPrices": {},
            "updateEnabled": True,
            "priceUpdateEnabled": True,
            "stockUpdateEnabled": True,
            "marketTransmitEnabled": True,
            "registeredAccounts": [],
            "collectedAt": now_iso,
            "updatedAt": now_iso,
        }

    # ------------------------------------------------------------------
    # 폴백 가격/정보 파싱 헬퍼 (CSS 클래스 기반)
    # ------------------------------------------------------------------

    def _parse_sale_price(self, html: str) -> int:
        """판매가 추출 (폴백).

        우선순위:
          1순위: 카드혜택가 (존재할 때만)
          2순위: meta product:price:amount
          3순위: CSS 패턴 (ssg_price, sale_price, cdtl_price)
        """
        # 1순위: 카드혜택가
        card_price = self._extract_card_benefit_price(html)
        if card_price > 0:
            return card_price

        # 2순위: meta 태그
        price_meta = self._extract_meta(html, "product:price:amount")
        if price_meta:
            price = self._safe_int(re.sub(r"[^\d]", "", price_meta))
            if price > 0:
                return price

        # 3순위: CSS 패턴
        for pattern in [
            r'class="[^"]*ssg_price[^"]*"[^>]*>.*?(\d[\d,]+)',
            r'class="[^"]*sale[_-]?price[^"]*"[^>]*>.*?(\d[\d,]+)',
            r'class="[^"]*cdtl_price[^"]*"[^>]*>.*?(\d[\d,]+)',
        ]:
            price = self._extract_price(html, pattern)
            if price > 0:
                return price
        return 0

    def _parse_original_price(self, html: str) -> int:
        """정상가 추출 (폴백)."""
        for pattern in [
            r'class="[^"]*old[_-]?price[^"]*"[^>]*>.*?(\d[\d,]+)',
            r'class="[^"]*org[_-]?price[^"]*"[^>]*>.*?(\d[\d,]+)',
            r'class="[^"]*cdtl_old_price[^"]*"[^>]*>.*?(\d[\d,]+)',
        ]:
            price = self._extract_price(html, pattern)
            if price > 0:
                return price
        return 0

    def _parse_best_benefit_price(self, html: str) -> int:
        """최대혜택가 추출 (폴백)."""
        for pattern in [
            r'class="[^"]*best[_-]?benefit[^"]*"[^>]*>.*?(\d[\d,]+)',
            r'class="[^"]*coupon[_-]?price[^"]*"[^>]*>.*?(\d[\d,]+)',
            r"(?:최대혜택가|쿠폰적용가)[^<]*?(\d[\d,]+)",
        ]:
            price = self._extract_price(html, pattern)
            if price > 0:
                return price
        return 0

    def _parse_brand(self, html: str) -> str:
        """브랜드명 추출 (폴백)."""
        for pattern in [
            r'class="[^"]*cdtl_brand[^"]*"[^>]*>([^<]+)',
            r'class="[^"]*brand[_-]?name[^"]*"[^>]*>([^<]+)',
        ]:
            brand = self._extract_text(html, pattern)
            if brand:
                return brand.strip()
        return ""

    def _parse_category(self, html: str) -> list[str]:
        """카테고리 경로 추출 (폴백)."""
        # 브레드크럼에서 추출
        breadcrumb_pattern = re.compile(
            r'class="[^"]*(?:breadcrumb|location)[^"]*"[^>]*>(.*?)</(?:ul|ol|div|nav)',
            re.DOTALL | re.IGNORECASE,
        )
        bc = breadcrumb_pattern.search(html)
        if bc:
            cats = [
                t.strip()
                for t in re.findall(r"<a[^>]*>([^<]+)</a>", bc.group(1))
                if t.strip() and t.strip() not in ("홈", "HOME", "SSG.COM")
            ]
            if cats:
                return cats[:4]
        return []

    def _parse_options(self, html: str) -> list[dict[str, Any]]:
        """옵션 추출 (폴백)."""
        options: list[dict[str, Any]] = []

        # JSON 옵션 데이터
        opt_pattern = re.compile(
            r"(?:optionData|itemOptList|optionList)\s*[=:]\s*(\[.*?\]);",
            re.DOTALL,
        )
        j = opt_pattern.search(html)
        if j:
            try:
                opt_list = json.loads(j.group(1))
                for opt in opt_list:
                    opt_name = (opt.get("optNm", "") or opt.get("name", "")).strip()
                    if not opt_name:
                        continue
                    stock = self._safe_int(opt.get("stockQty", 0))
                    is_soldout = opt.get("soldOutYn", "N") == "Y" or stock == 0
                    options.append(
                        {
                            "name": opt_name,
                            "price": self._safe_int(opt.get("sellprc", 0)),
                            "stock": stock,
                            "isSoldOut": bool(is_soldout),
                        }
                    )
                return options
            except (json.JSONDecodeError, TypeError):
                pass

        # select 박스 폴백
        option_area = re.search(
            r'class="[^"]*option[_-]?select[^"]*"[^>]*>(.*?)</select>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        if option_area:
            for value, text in re.findall(
                r'<option[^>]+value="([^"]*)"[^>]*>([^<]+)</option>',
                option_area.group(1),
                re.IGNORECASE,
            ):
                text = text.strip()
                if not value or "선택" in text:
                    continue
                is_soldout = "매진" in text or "품절" in text
                options.append(
                    {
                        "name": text,
                        "price": 0,
                        "stock": 0 if is_soldout else 1,
                        "isSoldOut": is_soldout,
                    }
                )

        return options

    def _check_sold_out(self, html: str, options: list[dict]) -> bool:
        """품절 여부 판단 (폴백)."""
        if re.search(r'class="[^"]*sold[_-]?out[^"]*"', html, re.IGNORECASE):
            return True
        if options and all(opt.get("isSoldOut", False) for opt in options):
            return True
        return False

    # ------------------------------------------------------------------
    # 공통 헬퍼
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_nested_structures(js_block: str) -> str:
        """resultItemObj JS 블록에서 연관/비교 상품 배열·객체를 제거.

        itemAssocList, cmptItemObjMap, cmptItemList, imgAssoItemList,
        frebieList 등 내부에 dispCtgLclsNm 같은 필드가 있어 상위 필드보다
        먼저 매칭되어 카테고리가 오염되는 버그 방지용.

        해당 키 뒤의 [...] 또는 {...} 를 bracket 카운터로 찾아 제거.
        """
        SKIP_KEYS = [
            "itemAssocList",
            "cmptItemObjMap",
            "cmptItemList",
            "imgAssoItemList",
            "frebieList",
            "suMcoObjList",
        ]

        result = js_block
        for key in SKIP_KEYS:
            # key: [ ... ] 또는 key: { ... } 를 찾아 제거
            pattern = re.compile(rf"\b{re.escape(key)}\s*:\s*([\[\{{])")
            while True:
                m = pattern.search(result)
                if not m:
                    break
                open_ch = m.group(1)
                close_ch = "]" if open_ch == "[" else "}"
                start = m.end() - 1  # open bracket 위치
                depth = 0
                i = start
                n = len(result)
                while i < n:
                    c = result[i]
                    if c in ('"', "'"):
                        q = c
                        i += 1
                        while i < n and result[i] != q:
                            if result[i] == "\\":
                                i += 2
                                continue
                            i += 1
                        i += 1
                        continue
                    if c == open_ch:
                        depth += 1
                    elif c == close_ch:
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                    i += 1
                # 제거: m.start() 부터 i 까지 (뒤따르는 , 도 함께)
                j = i
                while j < n and result[j] in " \t\n\r,":
                    j += 1
                result = result[: m.start()] + result[j:]
        return result

    @staticmethod
    def _extract_js_str_field(js_block: str, key: str) -> str:
        """JS 객체 블록에서 문자열 필드 추출. 단일/이중따옴표 모두 지원."""
        pattern = rf"[,\{{]\s*{re.escape(key)}\s*:\s*['\"]([^'\"]*)['\"]"
        m = re.search(pattern, js_block)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _extract_js_num_field(js_block: str, key: str) -> int:
        """JS 객체 블록에서 숫자 필드 추출. parseInt() 표현식도 지원."""
        # parseInt('25480', 10) 또는 25480 또는 '25480'
        pattern = rf"[,\{{]\s*{re.escape(key)}\s*:\s*(?:parseInt\s*\(\s*['\"](\d+)['\"]|['\"](\d+)['\"]|(\d+))"
        m = re.search(pattern, js_block)
        if m:
            val = m.group(1) or m.group(2) or m.group(3)
            return int(val) if val else 0
        return 0

    def _extract_uitem_list(self, js_block: str) -> list[dict]:
        """uitemObjList 배열 또는 uitemObjArr push 패턴에서 옵션 목록 추출.

        department.ssg.com 서버사이드 스크립트는 uitemObjList: [] 초기 선언 후
        var uitemObjArr = []; uitemObj = {...}; uitemObjArr.push(uitemObj); ... 형태로
        개별 아이템을 push하고 마지막에 resultItemObj.uitemObjList = uitemObjArr 로 할당.
        기존 파서는 초기 빈 배열만 파싱해 0개 반환하므로 push 패턴도 지원.
        """
        # uitemObj = {...}; uitemObjArr.push 패턴 — department.ssg.com 서버사이드
        if "uitemObjArr" in js_block and "uitemObjArr.push" in js_block:
            raw_blocks = self._collect_uitem_obj_blocks(js_block)
            if raw_blocks:
                return self._parse_raw_uitem_blocks(raw_blocks)

        # 기존 패턴: uitemObjList : [...] 인라인 배열
        m = re.search(r"uitemObjList\s*:\s*\[", js_block)
        if not m:
            return []

        start = m.end() - 1  # '[' 포함
        depth = 0
        end = start
        i = start
        while i < len(js_block):
            ch = js_block[i]
            if ch == "\\":
                i += 2
                continue
            if ch in ('"', "'"):
                q = ch
                i += 1
                while i < len(js_block) and js_block[i] != q:
                    if js_block[i] == "\\":
                        i += 1
                    i += 1
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            i += 1

        if end <= start:
            return []

        array_block = js_block[start:end]
        raw_blocks = self._collect_brace_blocks(array_block)
        return self._parse_raw_uitem_blocks(raw_blocks)

    @staticmethod
    def _collect_uitem_obj_blocks(js_block: str) -> list[str]:
        """uitemObj = {...}; uitemObjArr.push(uitemObj) 패턴에서 객체 블록 수집."""
        raw_blocks: list[str] = []
        for m in re.finditer(r"uitemObj\s*=\s*\{", js_block):
            start = m.end() - 1  # '{' 위치
            depth, i = 1, m.end()
            while i < len(js_block) and depth > 0:
                ch = js_block[i]
                if ch == "\\":
                    i += 2
                    continue
                if ch in ('"', "'"):
                    q, i = ch, i + 1
                    while i < len(js_block) and js_block[i] != q:
                        if js_block[i] == "\\":
                            i += 1
                        i += 1
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                i += 1
            raw_blocks.append(js_block[start:i])
        return raw_blocks

    @staticmethod
    def _collect_brace_blocks(array_block: str) -> list[str]:
        """배열 텍스트에서 최상위 {..} 블록 목록 추출."""
        raw_blocks: list[str] = []
        _i = 0
        while _i < len(array_block):
            if array_block[_i] == "{":
                _depth, _obj_start = 1, _i
                _i += 1
                while _i < len(array_block) and _depth > 0:
                    _ch = array_block[_i]
                    if _ch == "\\":
                        _i += 2
                        continue
                    if _ch in ('"', "'"):
                        _q = _ch
                        _i += 1
                        while _i < len(array_block) and array_block[_i] != _q:
                            if array_block[_i] == "\\":
                                _i += 1
                            _i += 1
                    elif _ch == "{":
                        _depth += 1
                    elif _ch == "}":
                        _depth -= 1
                    _i += 1
                raw_blocks.append(array_block[_obj_start:_i])
            else:
                _i += 1
        return raw_blocks

    @staticmethod
    def _parse_raw_uitem_blocks(raw_blocks: list[str]) -> list[dict]:
        """uitemObj 블록 목록에서 옵션 정보 파싱."""
        items = []
        for block in raw_blocks:

            def gs(k: str, _b: str = block) -> str:
                pm = re.search(
                    rf"(?:^|[,\{{])\s*{re.escape(k)}\s*:\s*['\"]([^'\"]*)['\"]", _b
                )
                return pm.group(1).strip() if pm else ""

            def gn(k: str, _b: str = block) -> int:
                pm = re.search(
                    rf"(?:^|[,\{{])\s*{re.escape(k)}\s*:\s*(?:parseInt\s*\(\s*['\"](\d+)['\"]|['\"](\d+)['\"]|(\d+))",
                    _b,
                )
                if pm:
                    v = pm.group(1) or pm.group(2) or pm.group(3)
                    return int(v) if v else 0
                return 0

            soldout_m = re.search(r"isSoldout\s*:\s*(true|false)", block)
            is_soldout = soldout_m.group(1) == "true" if soldout_m else False

            uitem_id = gs("uitemId")
            stock = gn("usablInvQty") or gn("displInvQty")
            if stock == 0 and not is_soldout:
                is_soldout = True

            option_values = [
                p
                for p in [gs("uitemOptnNm1"), gs("uitemOptnNm2"), gs("uitemOptnNm3")]
                if p
            ]
            opt_name = "/".join(option_values) or gs("uitemNm")

            items.append(
                {
                    "_uitemId": uitem_id,
                    "name": opt_name,
                    **{
                        f"optionName{idx}": value
                        for idx, value in enumerate(option_values, start=1)
                    },
                    "optionDepth": len(option_values) if option_values else 1,
                    "price": gn("sellprc"),
                    "stock": stock,
                    "isSoldOut": is_soldout,
                }
            )

        return items

    @staticmethod
    def _parse_select_options(html: str, base_price: int = 0) -> list[dict]:
        """HTML <select id="ordOpt1"> 태그에서 옵션 추출.

        department.ssg.com은 uitemObjList가 항상 빈 배열 (JS 런타임 로드).
        대신 HTML에 <select id="ordOpt1"> 태그가 있으며, 매진 옵션도 렌더링되고
        옵션명에 "(매진)" 텍스트가 붙어 있다.

        base_price: 상품 판매가. data-add-price / (+X원) 추가가 합산에 사용.
        """
        m = re.search(
            r'<select[^>]+id=["\']ordOpt1["\'][^>]*>(.*?)</select>',
            html,
            re.DOTALL | re.IGNORECASE,
        )
        if not m:
            return []

        options: list[dict] = []
        select_html = m.group(1)

        for opt_m in re.finditer(
            r"<option([^>]*)>(.*?)</option>",
            select_html,
            re.DOTALL | re.IGNORECASE,
        ):
            attrs_raw = opt_m.group(1)
            text = re.sub(r"<[^>]+>", "", opt_m.group(2)).strip()

            # value 추출
            val_m = re.search(r'value=["\']([^"\']*)["\']', attrs_raw)
            val = val_m.group(1).strip() if val_m else ""
            # 빈 값(선택하세요 등) 건너뜀
            if not val:
                continue

            name = text or val

            # 가격 추출 우선순위:
            # 1) data-price (절대가) → 2) data-add-price (추가가, base_price+add) → 3) 텍스트 패턴 → 4) 0
            price = 0
            dp_m = re.search(r'data-price=["\'](\d+)["\']', attrs_raw)
            if dp_m:
                price = int(dp_m.group(1))
            else:
                dap_m = re.search(r'data-add-price=["\'](\d+)["\']', attrs_raw)
                if dap_m:
                    # 추가가: base_price + add (add=0이면 price=0 → 호출부에서 base_price로 채움)
                    add = int(dap_m.group(1))
                    price = base_price + add if add > 0 else 0
                else:
                    # 텍스트 내 "(+10,000원)" 추가가 또는 "(315,000원)" 절대가
                    plus_m = re.search(r"\(\+([\d,]+)원\)", text)
                    abs_m = re.search(r"\(([\d,]+)원\)", text)
                    if plus_m:
                        price = base_price + int(plus_m.group(1).replace(",", ""))
                    elif abs_m:
                        price = int(abs_m.group(1).replace(",", ""))

            is_soldout = "매진" in name or "품절" in name
            clean_name = re.sub(r"\s*[\(\[](매진|품절)[\)\]]", "", name).strip()
            options.append(
                {
                    "name": clean_name,
                    "optionName1": clean_name,
                    "optionDepth": 1,
                    "price": price,
                    "stock": 0 if is_soldout else 99,
                    "isSoldOut": is_soldout,
                }
            )

        return options

    @staticmethod
    def _parse_layered_select_options(html: str, base_price: int = 0) -> list[dict]:
        """2단 옵션이 있는 SSG select 구조를 최대한 보존해 옵션을 추출한다."""

        def _extract_select_options(select_id: str) -> list[dict]:
            m = re.search(
                rf'<select[^>]+id=["\']{re.escape(select_id)}["\'][^>]*>(.*?)</select>',
                html,
                re.DOTALL | re.IGNORECASE,
            )
            if not m:
                return []

            parsed: list[dict] = []
            for opt_m in re.finditer(
                r"<option([^>]*)>(.*?)</option>",
                m.group(1),
                re.DOTALL | re.IGNORECASE,
            ):
                attrs_raw = opt_m.group(1)
                text = re.sub(r"<[^>]+>", "", opt_m.group(2)).strip()
                val_m = re.search(r'value=["\']([^"\']*)["\']', attrs_raw)
                val = val_m.group(1).strip() if val_m else ""
                if not val:
                    continue

                price = 0
                dp_m = re.search(r'data-price=["\'](\d+)["\']', attrs_raw)
                if dp_m:
                    price = int(dp_m.group(1))
                else:
                    dap_m = re.search(r'data-add-price=["\'](\d+)["\']', attrs_raw)
                    if dap_m:
                        add = int(dap_m.group(1))
                        price = base_price + add if add > 0 else 0
                    else:
                        plus_m = re.search(r"\(\+([\d,]+)", text)
                        abs_m = re.search(r"\(([\d,]+)", text)
                        if plus_m:
                            price = base_price + int(plus_m.group(1).replace(",", ""))
                        elif abs_m:
                            price = int(abs_m.group(1).replace(",", ""))

                is_soldout = any(
                    token in text for token in ("매진", "품절", "留ㅼ쭊", "?덉젅")
                )
                clean_name = (
                    (text or val)
                    .replace("(매진)", "")
                    .replace("[매진]", "")
                    .replace("(품절)", "")
                    .replace("[품절]", "")
                    .replace("(留ㅼ쭊)", "")
                    .replace("[留ㅼ쭊]", "")
                    .replace("(?덉젅)", "")
                    .replace("[?덉젅]", "")
                    .strip()
                )
                parsed.append(
                    {
                        "name": clean_name,
                        "optionName1": clean_name,
                        "optionDepth": 1,
                        "price": price,
                        "stock": 0 if is_soldout else 99,
                        "isSoldOut": is_soldout,
                        "_selected": "selected" in attrs_raw.lower(),
                    }
                )
            return parsed

        level1 = _extract_select_options("ordOpt1")
        level2 = _extract_select_options("ordOpt2")
        if level2:
            active_level1 = [opt for opt in level1 if opt.get("_selected")]
            if not active_level1 and len(level1) == 1:
                active_level1 = [level1[0]]

            if len(active_level1) == 1:
                # 단일 선택 색상에 사이즈 붙이기 (기존 로직)
                prefix = active_level1[0]["name"]
                combined = [
                    {
                        "name": f"{prefix}/{opt['name']}",
                        "optionName1": prefix,
                        "optionName2": opt["name"],
                        "optionDepth": 2,
                        "price": opt["price"],
                        "stock": opt["stock"],
                        "isSoldOut": opt["isSoldOut"],
                    }
                    for opt in level2
                ]
            else:
                # 선택된 1차 옵션 없음(또는 복수) → level1 × level2 전체 크로스프로덕트
                # 예: 색상 3개 × 사이즈 4개 = 12개 옵션
                base_l1 = active_level1 if active_level1 else level1
                combined = []
                for l1 in base_l1:
                    for l2 in level2:
                        combined.append(
                            {
                                "name": f"{l1['name']}/{l2['name']}",
                                "optionName1": l1["name"],
                                "optionName2": l2["name"],
                                "optionDepth": 2,
                                "price": l2["price"] or l1["price"],
                                "stock": 0
                                if (l1["isSoldOut"] or l2["isSoldOut"])
                                else 99,
                                "isSoldOut": l1["isSoldOut"] or l2["isSoldOut"],
                            }
                        )

            if combined:
                return combined

        return [
            {
                "name": opt["name"],
                "optionName1": opt["name"],
                "optionDepth": 1,
                "price": opt["price"],
                "stock": opt["stock"],
                "isSoldOut": opt["isSoldOut"],
            }
            for opt in level1
        ]

    @staticmethod
    def _js_literal_to_json(js_str: str) -> str:
        """JS 객체 리터럴을 JSON으로 변환.

        SSG HTML의 resultItemObj는 단일따옴표와 미인용 키를 사용하는 JS 객체 리터럴이므로
        JSON으로 변환이 필요하다.

        처리 항목:
          - 단일따옴표 문자열 → 이중따옴표 (내부 이중따옴표 이스케이프)
          - 미인용 키 → 이중따옴표 키
          - 후행 콤마 제거
        """
        # Step 1: 단일따옴표 문자열 → 이중따옴표 변환
        result: list[str] = []
        i = 0
        length = len(js_str)
        while i < length:
            ch = js_str[i]
            if ch == "'":
                # 단일따옴표 문자열 시작
                j = i + 1
                chars: list[str] = ['"']
                while j < length:
                    c = js_str[j]
                    if c == "\\" and j + 1 < length:
                        nc = js_str[j + 1]
                        if nc == "'":
                            chars.append("'")  # \' → '
                        elif nc == '"':
                            chars.append('\\"')  # \" → \"
                        else:
                            chars.append(c)
                            chars.append(nc)
                        j += 2
                    elif c == "'":
                        chars.append('"')
                        j += 1
                        break
                    elif c == '"':
                        chars.append('\\"')  # 내부 " 이스케이프
                        j += 1
                    else:
                        chars.append(c)
                        j += 1
                result.append("".join(chars))
                i = j
            else:
                result.append(ch)
                i += 1

        converted = "".join(result)

        # Step 2: 미인용 키 → 이중따옴표 키 (예: itemId: → "itemId":)
        converted = re.sub(r"([{,]\s*)([a-zA-Z_]\w*)\s*:", r'\1"\2":', converted)

        # Step 3: 후행 콤마 제거 (예: {..., } → {...})
        converted = re.sub(r",\s*([}\]])", r"\1", converted)

        return converted

    @staticmethod
    def _extract_card_benefit_price(html: str) -> int:
        """카드혜택가 추출.

        <dt class="mndtl_dl_tit">카드혜택가</dt> 가 존재할 때만 해당 가격을 반환한다.
        카드혜택가는 매일 변동되므로 존재하지 않으면 0을 반환한다.
        """
        m = re.search(
            r'<dt[^>]+class="[^"]*mndtl_dl_tit[^"]*"[^>]*>\s*카드혜택가\s*</dt>'
            r'.*?<em[^>]+class="ssg_price"[^>]*>([\d,]+)</em>',
            html,
            re.DOTALL,
        )
        if m:
            return int(m.group(1).replace(",", ""))
        return 0

    @staticmethod
    def _extract_dept_sale_price(html: str) -> int:
        """department.ssg.com 상세 페이지에서 실질 판매가 추출.

        카드혜택가는 특정 카드 소지자만 적용되므로 salePrice에 포함하지 않음.
        카드혜택가는 _extract_card_benefit_price()로 별도 추출 → bestBenefitPrice에만 반영.

        우선순위:
          1순위: cdtl_new_price notranslate — 항상 현재 최저 비카드가격 표시
                 (최적가가 있으면 최적가, 없으면 세일가를 자동으로 표시)
          2순위: cdtl_price point — 최적가 tooltip fallback
        """
        # 1순위: 메인 표시 가격 (최적가 or 세일가 — 항상 현재 최저 비카드가격)
        m = re.search(
            r"cdtl_new_price\s+notranslate[^>]*>.*?ssg_price[^>]*>([\d,]+)",
            html,
            re.DOTALL,
        )
        if m:
            return int(m.group(1).replace(",", ""))

        # 3순위: 최적가 tooltip (cdtl_price point)
        m = re.search(
            r"cdtl_price\s+point[^>]*>.*?ssg_price[^>]*>([\d,]+)",
            html,
            re.DOTALL,
        )
        if m:
            return int(m.group(1).replace(",", ""))
        return 0

    def _normalize_image(self, url: str) -> str:
        """이미지 URL 정규화."""
        if not url:
            return ""
        url = url.strip()
        if url.startswith("//"):
            return f"https:{url}"
        if not url.startswith("http"):
            return ""
        return url

    @staticmethod
    def _extract_meta(html: str, prop: str) -> Optional[str]:
        """og/product 메타 태그에서 content 추출."""
        pattern = (
            rf'<meta[^>]+(?:property|name)="{re.escape(prop)}"[^>]+content="([^"]*)"'
        )
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return m.group(1)
        pattern2 = (
            rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{re.escape(prop)}"'
        )
        m2 = re.search(pattern2, html, re.IGNORECASE)
        return m2.group(1) if m2 else None

    @staticmethod
    def _extract_text(html: str, pattern: str) -> str:
        """정규식으로 텍스트 추출."""
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _extract_price(html: str, pattern: str) -> int:
        """정규식으로 가격(숫자) 추출."""
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if m:
            digits = re.sub(r"[^\d]", "", m.group(1))
            return int(digits) if digits else 0
        return 0

    @staticmethod
    def _safe_int(value: Any) -> int:
        """안전한 정수 변환."""
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            digits = re.sub(r"[^\d]", "", value)
            return int(digits) if digits else 0
        return 0

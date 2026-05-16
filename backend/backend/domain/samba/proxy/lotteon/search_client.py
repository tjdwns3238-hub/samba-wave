"""롯데ON 검색 클라이언트 믹스인.

검색 API 호출 및 브랜드/카테고리 스캔 메서드를 제공한다.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from backend.domain.samba.proxy.lotteon.category_map import _LOTTEON_SCAT_NAMES
from backend.utils.logger import logger


class RateLimitError(Exception):
    """롯데ON 차단 감지 (429/403)."""

    def __init__(self, status: int, retry_after: int = 0):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"HTTP {status} (retry_after={retry_after})")


class SearchClientMixin:
    """검색 HTTP 클라이언트 메서드 믹스인."""

    # 하위 클래스에서 정의되는 상수 (타입 힌트용)
    SEARCH_URL: str
    PRODUCT_URL: str
    HEADERS: dict[str, str]

    def _timeout_obj(self) -> httpx.Timeout:
        """타임아웃 객체 반환 (하위 클래스의 self._timeout 참조)."""
        return self._timeout  # type: ignore[attr-defined]

    def _httpx_kwargs(self, **extra: Any) -> dict[str, Any]:  # type: ignore[override]
        """LotteonSourcingClient에서 오버라이드 — 믹스인 단독 사용 시 폴백."""
        return dict(extra)

    @staticmethod
    def _is_transient_5xx_s(status: int) -> bool:
        return status in (502, 503, 504)

    # ------------------------------------------------------------------
    # qapi JSON 검색 API (페이지네이션 지원)
    # ------------------------------------------------------------------

    async def search_products(
        self,
        keyword: str,
        page: int = 1,
        size: int = 60,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        """롯데ON 상품 검색 — qapi JSON API 사용.

        csearch qapi 엔드포인트로 JSON 직접 호출. offset 기반 페이지네이션.

        Args:
          keyword: 검색 키워드 (브랜드명 등)
          page: 페이지 번호 (1부터, offset = (page-1)*size 로 변환)
          size: 페이지당 결과 수 (최대 60)
          **filters: 추가 필터

        Returns:
          표준 상품 dict 리스트

        Raises:
          RateLimitError: 429/403 응답 시
        """
        # URL이 keyword로 전달된 경우 q= 파라미터 추출 (collect_by_filter 호환)
        if keyword.startswith("http") and "lotteon.com" in keyword:
            from urllib.parse import urlparse as _up, parse_qs as _pq

            _qs = _pq(_up(keyword).query)
            keyword = _qs.get("q", [keyword])[0]

        offset = (page - 1) * min(size, 60)
        search_url = (
            f"{self.SEARCH_URL}?render=qapi&platform=pc"
            f"&collection_id=201&q={quote(keyword)}"
            f"&mallId=2&u2={offset}&u3={min(size, 60)}"
        )
        logger.info(
            f'[LOTTEON] qapi 검색: "{keyword}" (offset={offset}, size={min(size, 60)})'
        )

        try:
            qapi_headers = {**self.HEADERS, "Accept": "application/json, */*"}
            async with httpx.AsyncClient(
                **self._httpx_kwargs(
                    timeout=self._timeout_obj(), follow_redirects=True
                )
            ) as client:
                resp = await client.get(search_url, headers=qapi_headers)

                # 차단 감지
                if resp.status_code in (429, 403):
                    retry_after = int(resp.headers.get("Retry-After", "60"))
                    logger.warning(f"[LOTTEON] 차단 감지 HTTP {resp.status_code}")
                    raise RateLimitError(resp.status_code, retry_after)

                # WAF/엣지 일시 차단(502/503/504) — 재시도 대상으로 변환
                if self._is_transient_5xx_s(resp.status_code):
                    retry_after = int(resp.headers.get("Retry-After", "10"))
                    logger.warning(
                        f"[LOTTEON] qapi 5xx 감지 HTTP {resp.status_code} "
                        f"Server={resp.headers.get('Server', '')!r}"
                    )
                    raise RateLimitError(resp.status_code, max(retry_after, 5))

                if resp.status_code != 200:
                    logger.warning(f"[LOTTEON] qapi HTTP {resp.status_code}")
                    return []

            data = resp.json()
            items = data.get("itemList", [])
            total = data.get("total", 0)
            now_iso = datetime.now(tz=timezone.utc).isoformat()

            products = self._convert_qapi_items(items, now_iso)  # type: ignore[attr-defined]
            logger.info(
                f'[LOTTEON] qapi 완료: "{keyword}" -> {len(products)}개 (total={total})'
            )
            return products

        except RateLimitError:
            raise
        except httpx.TimeoutException:
            logger.error(f"[LOTTEON] qapi 타임아웃: {keyword}")
            return []
        except Exception as e:
            logger.error(f"[LOTTEON] qapi 실패: {keyword} — {e}")
            return []

    async def search_products_total(self, keyword: str) -> int:
        """키워드 검색 결과의 전체 상품 수(total)만 조회."""
        if keyword.startswith("http") and "lotteon.com" in keyword:
            from urllib.parse import urlparse as _up, parse_qs as _pq

            _qs = _pq(_up(keyword).query)
            keyword = _qs.get("q", [keyword])[0]

        url = (
            f"{self.SEARCH_URL}?render=qapi&platform=pc"
            f"&collection_id=201&q={quote(keyword)}&mallId=2&u2=0&u3=1"
        )
        try:
            qapi_headers = {**self.HEADERS, "Accept": "application/json, */*"}
            async with httpx.AsyncClient(
                **self._httpx_kwargs(
                    timeout=self._timeout_obj(), follow_redirects=True
                )
            ) as client:
                resp = await client.get(url, headers=qapi_headers)
                if resp.status_code == 200:
                    return resp.json().get("total", 0)
        except Exception as e:
            logger.error(f"[LOTTEON] total 조회 실패: {keyword} — {e}")
        return 0

    async def search(
        self, keyword: str, max_count: int = 100, **kwargs: Any
    ) -> dict[str, Any]:
        """worker.py 직접 API 패턴 호환 래퍼 — qapi offset 기반 멀티페이지 검색.

        max_count까지 u2 offset을 증가시키며 상품을 수집한다.
        qapi는 offset 2,100 이상 요청을 받지 않으므로 해당 지점에서 강제 종료하며,
        RateLimitError 발생 시 부분 결과를 반환하고 종료한다.
        """
        kwargs.pop("category_filter", None)
        kwargs.pop("dispCatNo", None)
        products: list[dict[str, Any]] = []
        seen: set[str] = set()
        offset = 0
        page_size = 60
        # qapi 응답이 유효한 offset 상한 (offset>=2100이면 빈 응답 반복)
        _MAX_QAPI_OFFSET = 2100

        while len(products) < max_count:
            # 하드 가드 — qapi 상한 초과 요청은 응답이 비어 무한루프 위험
            if offset >= _MAX_QAPI_OFFSET:
                logger.info(
                    f"[LOTTEON] search '{keyword}' qapi 상한 도달(offset={offset}) — "
                    f"수집 종료 {len(products)}건"
                )
                break

            page_num = (offset // page_size) + 1
            try:
                raw = await self.search_products(keyword, page=page_num, size=page_size)
            except RateLimitError as e:
                logger.warning(
                    f"[LOTTEON] search '{keyword}' page={page_num} "
                    f"RateLimit(status={e.status}, retry_after={e.retry_after}) — "
                    f"부분 종료 {len(products)}건"
                )
                break
            if not raw:
                break  # 더 이상 결과 없음

            new_count = 0
            for item in raw:
                if len(products) >= max_count:
                    break
                site_product_id = (
                    item.get("site_product_id")
                    or item.get("siteProductId")
                    or item.get("spdNo")
                    or ""
                )
                if not site_product_id or site_product_id in seen:
                    continue
                seen.add(site_product_id)
                new_count += 1
                thumbnail = (
                    item.get("thumbnailImageUrl")
                    or item.get("thumbnail_image_url")
                    or ""
                )
                products.append(
                    {
                        "site_product_id": site_product_id,
                        "name": item.get("name", ""),
                        "brand": item.get("brand", ""),
                        "sale_price": item.get("sale_price")
                        or item.get("salePrice")
                        or 0,
                        "original_price": item.get("original_price")
                        or item.get("originalPrice")
                        or 0,
                        "images": [thumbnail] if thumbnail else [],
                        "source_url": item.get("source_url")
                        or item.get("sourceUrl")
                        or f"{self.PRODUCT_URL}/{site_product_id}",
                        "free_shipping": item.get("free_shipping", False),
                        "options": item.get("options", []),
                        "scat_no": item.get("scat_no") or item.get("scatNo") or "",
                        "best_benefit_price": item.get("best_benefit_price")
                        or item.get("bestBenefitPrice")
                        or 0,
                    }
                )

            # 새로운 상품이 0개면 더 이상 가져올 게 없음
            if new_count == 0:
                break
            offset += page_size

        return {"products": products, "total": len(products)}

    async def search_popular(
        self,
        limit: int = 50,
        keyword: str = "패션",
    ) -> list[dict[str, Any]]:
        """롯데ON 인기상품 검색 (AI 소싱기 연동용).

        인기순 정렬(sortType=BEST)로 검색하여 인기상품 목록을 반환한다.
        """
        search_url = (
            f"{self.SEARCH_URL}?render=search&platform=pc"
            f"&q={quote(keyword)}&size={min(limit, 60)}&mallId=2&sortType=BEST"
        )
        logger.info(f"[LOTTEON] 인기상품 검색 시작: keyword={keyword}, limit={limit}")

        try:
            async with httpx.AsyncClient(
                **self._httpx_kwargs(
                    timeout=self._timeout_obj(), follow_redirects=True
                )
            ) as client:
                resp = await client.get(search_url, headers=self.HEADERS)

                if resp.status_code in (429, 403):
                    retry_after = int(resp.headers.get("Retry-After", "60"))
                    logger.warning(
                        f"[LOTTEON] 인기상품 검색 차단 HTTP {resp.status_code}"
                    )
                    raise RateLimitError(resp.status_code, retry_after)

                if self._is_transient_5xx_s(resp.status_code):
                    retry_after = int(resp.headers.get("Retry-After", "10"))
                    logger.warning(
                        f"[LOTTEON] 인기상품 5xx HTTP {resp.status_code} "
                        f"Server={resp.headers.get('Server', '')!r}"
                    )
                    raise RateLimitError(resp.status_code, max(retry_after, 5))

                if resp.status_code != 200:
                    logger.warning(f"[LOTTEON] 인기상품 검색 HTTP {resp.status_code}")
                    return []

            products = self._parse_search_html(resp.text, keyword)  # type: ignore[attr-defined]
            logger.info(f"[LOTTEON] 인기상품 검색 완료: {len(products)}개")
            return products
        except RateLimitError:
            raise
        except Exception as e:
            logger.error(f"[LOTTEON] 인기상품 검색 실패: {keyword} — {e}")
            return []

    async def discover_brands(
        self, keyword: str, *, max_pages: int = 100
    ) -> dict[str, Any]:
        """롯데ON 키워드 검색 → 발견된 브랜드 분포 반환.

        프론트에서 사용자가 어떤 브랜드를 카테고리 스캔 대상으로 선택할지
        결정하기 위한 1단계 조회.

        Returns:
            {
                "brands": [{"name": "나이키", "count": 599}, ...],
                "total": int,
            }
        """
        logger.info(f'[LOTTEON] 브랜드 탐색 시작: "{keyword}"')
        brand_counts: dict[str, int] = {}
        total = 0
        for page_num in range(1, max_pages + 1):
            try:
                items = await self.search_products(keyword, page=page_num, size=60)
                if not items:
                    break
                total += len(items)
                for item in items:
                    b = (item.get("brand") or "").strip()
                    if b:
                        brand_counts[b] = brand_counts.get(b, 0) + 1
            except Exception as e:
                logger.warning(f"[LOTTEON] 브랜드 탐색 p{page_num} 실패: {e}")
                break

        brands = [
            {"name": b, "count": c}
            for b, c in sorted(brand_counts.items(), key=lambda x: -x[1])
        ]
        logger.info(
            f'[LOTTEON] 브랜드 탐색 완료: "{keyword}" → {len(brands)}개 브랜드, 총 {total}건'
        )
        return {"brands": brands, "total": total}

    async def scan_categories(
        self,
        keyword: str,
        *,
        selected_brands: list[str] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """롯데ON 카테고리 스캔 — qapi 검색 결과의 BC코드(scat_no)로 카테고리 분포 집계.

        qapi 검색으로 상품을 가져온 뒤, data.category(BC코드)를 기준으로
        카테고리별 상품 수를 집계한다. _LOTTEON_SCAT_NAMES로 카테고리 경로를 매핑.

        Args:
            keyword: 검색 키워드
            selected_brands: 사용자가 선택한 브랜드 목록 (정확 일치 필터)
                비어있거나 None이면 전체 브랜드 집계.

        Returns:
            {
                "categories": [{"categoryCode", "path", "count", "category1", "category2", "category3"}],
                "total": int,
                "groupCount": int,
            }
        """
        logger.info(
            f'[LOTTEON] 카테고리 스캔 시작 (qapi BC코드): "{keyword}" (selected_brands={selected_brands!r})'
        )

        # 1차: 페이지 수집
        # selected_brands가 있으면 각 브랜드명을 keyword로 개별 검색해서 합침
        # (qapi 검색은 키워드 관련도 기반이라 "나이키"로 검색하면 "나이키 스윔" 등 서브브랜드가
        #  대부분 누락됨. "나이키 스윔"으로 직접 검색하면 1047건이 나오는 케이스 등을 보전)
        all_items: list[dict[str, Any]] = []
        scan_pages = 100  # 100페이지 × 60 = 6,000개
        search_keywords: list[str] = (
            list(selected_brands) if selected_brands else [keyword]
        )
        seen_pids: set[str] = set()
        for kw in search_keywords:
            kw_count = 0
            for page_num in range(1, scan_pages + 1):
                try:
                    items = await self.search_products(kw, page=page_num, size=60)
                    if not items:
                        break
                    for item in items:
                        pid = item.get("spdNo") or item.get("site_product_id") or ""
                        if pid and pid in seen_pids:
                            continue
                        if pid:
                            seen_pids.add(pid)
                        all_items.append(item)
                        kw_count += 1
                except Exception as e:
                    logger.warning(
                        f"[LOTTEON] 카테고리 스캔 kw={kw!r} p{page_num} 실패: {e}"
                    )
                    break
            logger.info(f"[LOTTEON] 카테고리 스캔 kw={kw!r} → {kw_count}건 수집")

        # 2차: 선택 브랜드 필터링은 생략한다.
        # 이미 1차에서 각 브랜드명을 키워드로 직접 검색했으므로, brand 필드가
        # 정확 일치하지 않아도(예: "나이키 스윔" 검색 결과에 brand="나이키"가 섞여도)
        # 키워드 검색 결과 자체를 신뢰한다. 정확 일치 필터를 적용하면 1차에서
        # 1000건을 가져와도 55건만 남는 문제가 발생함.
        filtered_items = all_items
        filtered_count = 0

        # BC코드 집계
        cat_counter: dict[str, int] = {}
        for item in filtered_items:
            scat = item.get("scatNo") or item.get("scat_no") or ""
            if scat:
                cat_counter[scat] = cat_counter.get(scat, 0) + 1

        if selected_brands and filtered_count:
            logger.info(
                f"[LOTTEON] 브랜드 필터링: {filtered_count}건 제외 (selected={selected_brands!r}, 통과={len(filtered_items)}건)"
            )

        # 미매핑 BC코드 자동 매핑: 상품 HTML breadcrumb에서 카테고리 경로 추출
        unmapped_codes = [bc for bc in cat_counter if bc not in _LOTTEON_SCAT_NAMES]
        if unmapped_codes:
            logger.info(
                f"[LOTTEON] 미매핑 BC코드 {len(unmapped_codes)}개 자동 매핑 시도"
            )
            # 미매핑 BC코드별 대표 상품 1개씩 — 1차에서 수집한 all_items 재활용
            bc_to_product: dict[str, str] = {}
            unmapped_set = set(unmapped_codes)
            for item in filtered_items:
                scat = item.get("scatNo") or item.get("scat_no") or ""
                pid = item.get("spdNo") or item.get("site_product_id") or ""
                if scat in unmapped_set and scat not in bc_to_product and pid:
                    bc_to_product[scat] = pid
                    if len(bc_to_product) >= len(unmapped_set):
                        break

            # 병렬로 HTML breadcrumb 추출
            async def _resolve_bc(bc_code: str, pid: str) -> tuple[str, str]:
                try:
                    detail = await self.get_product_detail(pid)  # type: ignore[attr-defined]
                    cats = detail.get("categories", [])
                    if not cats:
                        cat_str = detail.get("category", "")
                        if cat_str:
                            cats = [c.strip() for c in cat_str.split(">") if c.strip()]
                    if cats:
                        path = " > ".join(cats[:4])
                        return bc_code, path
                except Exception as e:
                    logger.debug(f"[LOTTEON] BC 자동매핑 실패 {bc_code}: {e}")
                return bc_code, ""

            resolve_tasks = [_resolve_bc(bc, pid) for bc, pid in bc_to_product.items()]
            if resolve_tasks:
                results = await asyncio.gather(*resolve_tasks, return_exceptions=True)
                mapped_count = 0
                for r in results:
                    if isinstance(r, Exception):
                        continue
                    bc_code, path = r
                    if path:
                        _LOTTEON_SCAT_NAMES[bc_code] = path
                        mapped_count += 1
                logger.info(
                    f"[LOTTEON] 자동 매핑 완료: {mapped_count}/{len(unmapped_codes)}개"
                )

        # BC코드 → 카테고리 경로 매핑 (같은 path 합산, 미매핑은 BC코드를 path로 표시)
        path_merged: dict[str, dict[str, Any]] = {}
        for bc_code, count in cat_counter.items():
            path = _LOTTEON_SCAT_NAMES.get(bc_code, "") or f"미매핑 ({bc_code})"
            if path in path_merged:
                path_merged[path]["count"] += count
                path_merged[path]["bc_codes"].append(bc_code)
            else:
                path_merged[path] = {
                    "bc_codes": [bc_code],
                    "count": count,
                }

        categories: list[dict[str, Any]] = []
        for path, info in sorted(path_merged.items(), key=lambda x: -x[1]["count"]):
            parts = path.split(" > ")
            # qapi 2,100 상한 우회용 서브키워드 ({브랜드} + 카테고리 리프명)
            # 예: keyword="나이키", path="스포츠 > 신발 > 운동화" → "나이키 운동화"
            _leaf = next((p for p in reversed(parts) if p), "")
            _sub_kw = f"{keyword} {_leaf}".strip() if _leaf else keyword
            categories.append(
                {
                    "categoryCode": info["bc_codes"][0],
                    "bc_codes": info["bc_codes"],
                    "path": path,
                    "count": info["count"],
                    "category1": parts[0] if len(parts) > 0 else "",
                    "category2": parts[1] if len(parts) > 1 else "",
                    "category3": parts[2] if len(parts) > 2 else "",
                    "subKeyword": _sub_kw,
                    "displayLabel": _sub_kw,
                }
            )

        total = sum(c["count"] for c in categories)
        logger.info(
            f"[LOTTEON] 카테고리 스캔 완료: keyword={keyword!r}, "
            f"카테고리={len(categories)}개, 총 상품={total}개"
        )

        return {
            "categories": categories,
            "total": total,
            "groupCount": len(categories),
        }

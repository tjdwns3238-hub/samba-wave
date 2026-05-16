"""롯데ON 클라이언트 패키지.

- LotteonSourcingClient: 소싱용 웹 스크래핑
- LotteonClient: Open API 상품 등록/수정
"""

from __future__ import annotations

from typing import Any

import httpx

from backend.domain.samba.proxy.lotteon.category_map import _LOTTEON_SCAT_NAMES
from backend.domain.samba.proxy.lotteon.detail_client import (
    DetailClientMixin,
    _lotteon_cookie_cache,
    set_lotteon_cookie,
)
from backend.domain.samba.proxy.lotteon.detail_parsers import DetailParsersMixin
from backend.domain.samba.proxy.lotteon.search_client import (
    RateLimitError,
    SearchClientMixin,
)
from backend.domain.samba.proxy.lotteon.search_parsers import SearchParsersMixin


def _filter_by_brands(items: list[dict], selected_brands: list[str]) -> list[dict]:
    """브랜드 필터링 — 선택된 브랜드 목록에 정확 일치하는 상품만 반환.

    공백 정규화 후 비교하여 "나이키 골프"와 "나이키골프"를 동일하게 처리.
    selected_brands가 비어있으면 필터링 없이 전체 반환.
    """
    if not items or not selected_brands:
        return items

    brand_set = {b.replace(" ", "").strip() for b in selected_brands if b}
    if not brand_set:
        return items

    return [
        it
        for it in items
        if (it.get("brand") or "").replace(" ", "").strip() in brand_set
    ]


class LotteonSourcingClient(
    SearchParsersMixin,
    SearchClientMixin,
    DetailParsersMixin,
    DetailClientMixin,
):
    """롯데ON 소싱용 웹 스크래핑 클라이언트 (검색, 상세).

    롯데ON 상품 페이지를 HTML 파싱하여 상품 검색/상세 정보를 추출한다.
    JSON-LD(schema.org Product) 마크업을 우선 파싱하고,
    없으면 __NEXT_DATA__ 또는 메타 태그에서 폴백한다.
    """

    BASE = "https://www.lotteon.com"
    SEARCH_URL = "https://www.lotteon.com/csearch/search/search"
    PRODUCT_URL = "https://www.lotteon.com/p/product"
    IMAGE_CDN = "contents.lotteon.com"
    PBF_BASE = "https://pbf.lotteon.com"

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
        "Referer": "https://www.lotteon.com/",
    }

    def __init__(self, proxy_url: str | None = None) -> None:
        self._timeout = httpx.Timeout(20.0, connect=10.0)
        # 롯데ON WAF가 데이터센터 IP에서 502 Bad Gateway로 소프트 차단하는 사례
        # 확인됨(2026-05-16) — 프록시 로테이션으로 회피. None이면 메인 IP 사용.
        self.proxy_url: str | None = proxy_url

    def _timeout_obj(self) -> httpx.Timeout:
        """타임아웃 객체 반환."""
        return self._timeout

    def _httpx_kwargs(self, **extra: Any) -> dict[str, Any]:
        """모든 httpx.AsyncClient 생성 시 공통으로 적용할 인자.

        proxy_url이 설정돼 있으면 proxy 키를 주입한다. detail/search/pbf 모든 호출이
        동일 IP로 나가도록 보장하기 위함.
        """
        kw: dict[str, Any] = dict(extra)
        if self.proxy_url:
            kw["proxy"] = self.proxy_url
        return kw


from backend.domain.samba.proxy.lotteon.api_client import (
    LotteonApiError,
    LotteonClient,
    _build_lotteon_intro,
    _build_lotteon_keywords,
    _get_lotteon_origin_code,
)

__all__ = [
    "LotteonSourcingClient",
    "RateLimitError",
    "set_lotteon_cookie",
    "_lotteon_cookie_cache",
    "_filter_by_brands",
    "_LOTTEON_SCAT_NAMES",
    "LotteonClient",
    "LotteonApiError",
    "_build_lotteon_intro",
    "_build_lotteon_keywords",
    "_get_lotteon_origin_code",
]

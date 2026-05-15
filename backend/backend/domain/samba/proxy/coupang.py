"""쿠팡 Wing API 클라이언트 - 상품 등록/수정.

인증 방식: HMAC-SHA256
- method, url, timestamp, accessKey → HMAC 서명 생성
- Authorization: CEA algorithm=HmacSHA256, access-key={accessKey}, signed-date={datetime}, signature={signature}
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from backend.core.config import settings
from backend.utils.logger import logger

# ------------------------------------------------------------------
# SEO 헬퍼 함수 (모듈 레벨)
# ------------------------------------------------------------------

# 노출상품명 불용어
_DISPLAY_NAME_STOPWORDS = {
    "무료배송",
    "당일발송",
    "특가",
    "할인",
    "세일",
    "SALE",
    "신상",
    "인기",
    "추천",
    "베스트",
    "HOT",
    "NEW",
    "한정",
    "사은품",
}

# 사이즈 패턴 (숫자 2~3자리, 알파벳 사이즈, FREE 등)
_SIZE_PATTERN = re.compile(
    r"^(\d{2,3}(?:\([A-Za-z]+\))?|(?:XX?[SL]|[SML]|FREE))$", re.IGNORECASE
)


def _build_display_product_name(product: dict[str, Any]) -> str:
    """쿠팡 노출상품명 생성 (최대 100자).

    우선순위: market_names["쿠팡"] → 자동생성
    자동생성: 브랜드 + 성별 + 카테고리키워드 + 특성 + 품번
    """
    # 수동 설정된 쿠팡 노출상품명 우선
    market_names = product.get("market_names") or {}
    if isinstance(market_names, dict) and market_names.get("쿠팡"):
        return str(market_names["쿠팡"])[:100]

    parts: list[str] = []

    # 브랜드
    brand = (product.get("brand") or "").strip()
    if brand:
        parts.append(brand)

    # 성별
    sex = (product.get("sex") or "").strip()
    sex_map = {
        "남성": "남성",
        "여성": "여성",
        "남": "남성",
        "여": "여성",
        "M": "남성",
        "F": "여성",
        "MALE": "남성",
        "FEMALE": "여성",
        "공용": "공용",
        "남녀공용": "남녀공용",
    }
    mapped_sex = sex_map.get(sex, "")
    if mapped_sex:
        parts.append(mapped_sex)

    # 카테고리 키워드 (category4 → 3 → 2 우선)
    for key in ("category4", "category3", "category2"):
        cat_val = (product.get(key) or "").strip()
        if cat_val and cat_val not in parts:
            parts.append(cat_val)
            break

    # 원본 상품명에서 특성 추출 (브랜드/카테고리 제외, 불용어 제외)
    original_name = product.get("name") or ""
    name_tokens = re.split(r"[\s/\-_#]+", original_name)
    existing_lower = {p.lower() for p in parts}
    for token in name_tokens:
        token_clean = token.strip()
        if (
            len(token_clean) >= 2
            and token_clean not in _DISPLAY_NAME_STOPWORDS
            and token_clean.lower() not in existing_lower
        ):
            parts.append(token_clean)
            existing_lower.add(token_clean.lower())
            # 특성은 최대 3개까지
            if len(parts) >= 7:
                break

    # 품번 (style_code)
    style_code = (product.get("style_code") or "").strip()
    if style_code and style_code.lower() not in existing_lower:
        parts.append(style_code)

    result = " ".join(parts)

    # 100자 초과 시 품번 유지하고 중간 잘라내기
    if len(result) > 100 and style_code:
        max_body = 100 - len(style_code) - 1  # 공백 1자
        body = " ".join(parts[:-1])
        result = body[:max_body].rstrip() + " " + style_code
    return result[:100]


def _build_search_tags(product: dict[str, Any]) -> str:
    """쿠팡 검색어 태그 생성 (최대 20개, 콤마 구분, 각 20자 이내)."""
    seen: set[str] = set()
    tags: list[str] = []

    def _add(keyword: str) -> None:
        kw = keyword.strip()
        if len(kw) < 2 or len(kw) > 20:
            return
        kw_lower = kw.lower()
        if kw_lower in seen or kw in _DISPLAY_NAME_STOPWORDS:
            return
        seen.add(kw_lower)
        tags.append(kw)

    brand = (product.get("brand") or "").strip()
    if brand:
        _add(brand)
    seo_keywords = product.get("seo_keywords") or []
    if isinstance(seo_keywords, list):
        for kw in seo_keywords:
            _add(str(kw))
    for key in ("category4", "category3", "category2", "category1"):
        cat_val = (product.get(key) or "").strip()
        if cat_val:
            _add(cat_val)
    original_name = product.get("name") or ""
    name_tokens = re.split(r"[\s/\-_,()]+", original_name)
    for token in name_tokens:
        _add(token)
    style_code = (product.get("style_code") or "").strip()
    if style_code:
        _add(style_code)
    material = (product.get("material") or "").strip()
    if material:
        _add(material)
    color = (product.get("color") or "").strip()
    if color:
        _add(color)
    return ",".join(tags[:20])


def _parse_option_color_size(opt_name: str, default_color: str) -> tuple[str, str]:
    """옵션명에서 색상/사이즈 분리."""
    opt_name = opt_name.strip()
    if not opt_name:
        return default_color, "FREE"
    parts = re.split(r"\s*/\s*|\s*,\s*", opt_name)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        last = parts[-1]
        if _SIZE_PATTERN.match(last):
            color_part = " ".join(parts[:-1])
            return color_part or default_color, last
        first = parts[0]
        if _SIZE_PATTERN.match(first):
            color_part = " ".join(parts[1:])
            return color_part or default_color, first
        return parts[0], parts[-1]
    single = parts[0] if parts else opt_name
    if _SIZE_PATTERN.match(single):
        return default_color, single
    return single, "FREE"


def _build_content_details(detail_html: str) -> list[dict[str, Any]]:
    """상세 HTML에서 IMAGE/TEXT 혼합 contentDetails 생성."""
    if not detail_html:
        return [{"content": "", "detailType": "TEXT"}]
    img_pattern = re.compile(
        r'<img\s+[^>]*?src=["\']([^"\']+)["\'][^>]*?>', re.IGNORECASE
    )
    segments = img_pattern.split(detail_html)
    if len(segments) <= 1:
        return [{"content": detail_html, "detailType": "TEXT"}]
    details: list[dict[str, Any]] = []
    for i, segment in enumerate(segments):
        segment = segment.strip()
        if not segment:
            continue
        if i % 2 == 0:
            details.append({"content": segment, "detailType": "TEXT"})
        else:
            url = segment
            if url.startswith("//"):
                url = "https:" + url
            details.append({"content": url, "detailType": "IMAGE"})
    return details if details else [{"content": detail_html, "detailType": "TEXT"}]


# GSShop 자사 CDN 화이트리스트 — 쿠팡 검증 거절(외부 호스트 이미지) 방지용
_GSSHOP_ALLOWED_DOMAINS: tuple[str, ...] = (
    "asset.m-gs.kr",
    "static.m-gs.kr",
    "static.gsshop.com",
)


def _filter_gsshop_domain(urls: list[str]) -> list[str]:
    """GSShop 자사 CDN 화이트리스트에 속한 URL만 통과시킨다.

    쿠팡 검증 거절(외부 호스트 / 비공개 CDN 이미지) 방지를 위해
    asset.m-gs.kr / static.m-gs.kr / static.gsshop.com 만 허용.
    빈 문자열·외부 호스트는 모두 제거된다.
    """
    result: list[str] = []
    for u in urls:
        if not u:
            continue
        if any(host in u for host in _GSSHOP_ALLOWED_DOMAINS):
            result.append(u)
    return result


def _filter_html_external_images(html: str) -> str:
    """detail_html의 <img> 태그에서 GSShop CDN 외 외부 호스트 src/data-src를 가진 태그를 제거.

    예: akplaza.com, speedgabia.com 등 외부 호스트 이미지는 태그 통째로 제거.
    화이트리스트(_GSSHOP_ALLOWED_DOMAINS)에 속한 src/data-src 만 남는다.
    """
    if not html:
        return html

    img_re = re.compile(
        r'<img[^>]*?(?:src|data-src)=["\']([^"\']+)["\'][^>]*?>',
        re.IGNORECASE,
    )

    def _sub(match: re.Match[str]) -> str:
        url = match.group(1)
        if any(host in url for host in _GSSHOP_ALLOWED_DOMAINS):
            return match.group(0)
        return ""

    return img_re.sub(_sub, html)


class CoupangClient:
    """쿠팡 Wing API 클라이언트."""

    BASE_URL = "https://api-gateway.coupang.com"

    # 카테고리별 notice 메타 캐시 (module-level, 모든 인스턴스 공유)
    # category_id → (data, timestamp)
    _notice_meta_cache: dict[str, tuple[dict, float]] = {}
    _NOTICE_META_CACHE_TTL = 3600  # 1시간
    _NOTICE_META_CACHE_MAX = (
        200  # 카테고리 200개 한도 (의류/신발/가방 등 메인 leaf 충분)
    )

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        vendor_id: str,
    ) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.vendor_id = vendor_id

    # ------------------------------------------------------------------
    # HMAC 서명 생성
    # ------------------------------------------------------------------

    def _generate_signature(
        self, method: str, path: str, query: str = ""
    ) -> tuple[str, str]:
        """HMAC-SHA256 서명 생성. (authorization_header, datetime) 반환."""
        dt = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")
        # 메시지: datetime + method + path + query (단순 연결, 구분자 없음)
        message = f"{dt}{method}{path}{query}"
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        authorization = (
            f"CEA algorithm=HmacSHA256, access-key={self.access_key}, "
            f"signed-date={dt}, signature={signature}"
        )
        return authorization, dt

    async def _call_api(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """공통 API 호출."""
        query = "&".join(f"{k}={v}" for k, v in (params or {}).items() if v)
        authorization, dt = self._generate_signature(method, path, query)

        url = f"{self.BASE_URL}{path}"
        if query:
            url = f"{url}?{query}"

        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json;charset=UTF-8",
            "X-Requested-By": "samba-wave",
        }

        async with httpx.AsyncClient(timeout=settings.http_timeout_default) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            elif method == "POST":
                resp = await client.post(url, headers=headers, json=body or {})
            elif method == "PUT":
                resp = await client.put(url, headers=headers, json=body or {})
            elif method == "PATCH":
                resp = await client.patch(url, headers=headers, json=body or {})
            elif method == "DELETE":
                resp = await client.delete(url, headers=headers)
            else:
                raise ValueError(f"지원하지 않는 HTTP 메서드: {method}")

            try:
                data = resp.json()
                _parsed = True
            except Exception:
                data = {"raw": resp.text}
                _parsed = False

            logger.info(f"[쿠팡] {method} {path} → {resp.status_code}")

            if not resp.is_success:
                msg = (
                    data.get("message", "") or data.get("reason", "") or resp.text[:200]
                )
                raise CoupangApiError(f"HTTP {resp.status_code}: {msg}")

            # 200 이지만 JSON 파싱 실패 → 응답을 신뢰할 수 없음
            if not _parsed:
                raise CoupangApiError(
                    f"응답 파싱 실패 (HTTP {resp.status_code}): {resp.text[:200]}"
                )

            # 쿠팡 API는 HTTP 200이지만 body에 code=ERROR 반환하는 경우 있음
            if isinstance(data, dict) and data.get("code") == "ERROR":
                msg = data.get("message", "") or "알 수 없는 오류"
                raise CoupangApiError(f"API ERROR: {msg}")

            return data

    # ------------------------------------------------------------------
    # 카테고리 조회
    # ------------------------------------------------------------------

    async def get_categories(self) -> dict[str, Any]:
        """전체 카테고리 조회 (display category 기반)."""
        return await self._call_api(
            "GET",
            "/v2/providers/seller_api/apis/api/v1/marketplace/meta/display-categories",
        )

    async def resolve_category_code(self, category_path: str) -> int:
        """카테고리 경로 문자열 → displayItemCategoryCode 변환.

        카테고리 트리를 조회하여 경로의 마지막 키워드와 가장 잘 매칭되는 리프 노드 반환.
        """
        try:
            result = await self.get_categories()
            root = result.get("data", result) if isinstance(result, dict) else {}
            if not isinstance(root, dict):
                return 0

            # 트리 평탄화: (경로, 코드) 리스트 생성
            def flatten(node: dict, path: str = "") -> list[tuple[str, int]]:
                code = node.get("displayItemCategoryCode", 0)
                name = node.get("name", "")
                current = f"{path} > {name}" if path else name
                entries: list[tuple[str, int]] = []
                children = node.get("child", [])
                if not children and code:
                    entries.append((current, code))
                for c in children:
                    entries.extend(flatten(c, current))
                return entries

            all_cats = flatten(root)

            # 경로에서 키워드 추출 (예: "패션의류 > 남성의류 > 아우터 > 코트" → ["패션의류", "남성의류", ...])
            keywords = [
                k.strip()
                for k in category_path.replace(">", "/").split("/")
                if k.strip()
            ]

            # 가중치 매칭: 상위 카테고리(성별 등)에 높은 가중치 부여
            # 예: ["패션의류", "남성의류", "아우터", "코트"] → 가중치 [4, 3, 2, 1]
            full_matches: list[tuple[str, int, int]] = []
            partial_matches: list[tuple[str, int, int]] = []
            for cat_path, code in all_cats:
                score = 0
                match_count = 0
                for i, kw in enumerate(keywords):
                    if kw in cat_path:
                        score += len(keywords) - i  # 상위 키워드일수록 높은 가중치
                        match_count += 1
                if match_count == len(keywords):
                    full_matches.append((cat_path, code, score))
                elif match_count > 0:
                    partial_matches.append((cat_path, code, score))

            # 전체 매칭 → 가중치 합계 높은 순, 동점이면 경로 짧은 순
            best_code = 0
            if full_matches:
                full_matches.sort(key=lambda x: (-x[2], len(x[0])))
                best_code = full_matches[0][1]
                logger.info(
                    f"[쿠팡] 카테고리 전체매칭: '{category_path}' → {best_code} ({full_matches[0][0]})"
                )
            elif partial_matches:
                partial_matches.sort(key=lambda x: (-x[2], len(x[0])))
                best_code = partial_matches[0][1]
                logger.info(
                    f"[쿠팡] 카테고리 부분매칭: '{category_path}' → {best_code} ({partial_matches[0][0]})"
                )

            if best_code:
                logger.info(f"[쿠팡] 카테고리 매핑: '{category_path}' → {best_code}")
            return best_code
        except Exception as exc:
            logger.warning(f"[쿠팡] 카테고리 코드 조회 실패: {exc}")
            return 0

    async def get_category_by_id(self, category_id: str) -> dict[str, Any]:
        """특정 카테고리 상세 조회."""
        return await self._call_api(
            "GET",
            f"/v2/providers/seller_api/apis/api/v1/marketplace/meta/display-categories/{category_id}",
        )

    async def get_notice_categories(self, category_id: str) -> dict[str, Any]:
        """카테고리별 정확한 noticeCategoryName/noticeCategoryDetailName 조회 (TTL 캐시).

        하드코딩된 한국어 매핑(_COUPANG_NOTICE_CATEGORY/_COUPANG_NOTICE_FIELDS)이
        쿠팡 API 표준 표기와 미스매치되어 의류/신발 등록 시 모든 옵션의 notice가
        거부되는 문제(2026-05 보고)를 동적 조회로 근본 해결.

        GET /v2/providers/seller_api/apis/api/v1/marketplace/meta/category-related-metas/display-category-codes/{displayCategoryCode}
        쿠팡 공식 path. data.noticeCategories[*].noticeCategoryDetailNames[*] 내려줌.
        # 직전 fix는 존재하지 않는 path(/notice-categories/{id})로 호출해
        # 모든 카테고리에서 404 → 정적 매핑 폴백 → 스포츠/레저 카테고리에 '의류' 전송 → 거부.

        캐시: module-level _notice_meta_cache, TTL 1시간. 카테고리당 1회 조회 후 재사용.
        """
        import time

        now = time.time()
        cached = self._notice_meta_cache.get(category_id)
        if cached:
            data, ts = cached
            if now - ts < self._NOTICE_META_CACHE_TTL:
                return data
            # 만료된 항목 제거
            del self._notice_meta_cache[category_id]

        result = await self._call_api(
            "GET",
            f"/v2/providers/seller_api/apis/api/v1/marketplace/meta/category-related-metas/display-category-codes/{category_id}",
        )
        self._notice_meta_cache[category_id] = (result, now)

        # 캐시 한도 초과 시 가장 오래된 절반 제거
        if len(self._notice_meta_cache) > self._NOTICE_META_CACHE_MAX:
            sorted_items = sorted(
                self._notice_meta_cache.items(), key=lambda x: x[1][1]
            )
            for k, _ in sorted_items[: self._NOTICE_META_CACHE_MAX // 2]:
                del self._notice_meta_cache[k]

        return result

    # ------------------------------------------------------------------
    # 출고지 / 반품지 조회
    # ------------------------------------------------------------------

    async def get_outbound_shipping_places(self) -> list[dict[str, Any]]:
        """쿠팡 출고지 목록 조회.

        GET /v2/providers/marketplace_openapi/apis/api/v1/vendor/shipping-place/outbound
        응답 구조: { content: [{ outboundShippingPlaceCode, shippingPlaceName, placeAddresses: [...], usable }] }
        """
        res = await self._call_api(
            "GET",
            "/v2/providers/marketplace_openapi/apis/api/v1/vendor/shipping-place/outbound",
            params={"pageNum": "1", "pageSize": "50"},
        )
        items = res.get("content") if isinstance(res, dict) else None
        items = items or []
        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # usable 기본값 True (키가 없으면 사용가능으로 간주)
            if not item.get("usable", True):
                continue
            addresses = item.get("placeAddresses") or []
            first_addr = (
                addresses[0] if addresses and isinstance(addresses[0], dict) else {}
            )
            result.append(
                {
                    "code": str(item.get("outboundShippingPlaceCode", "") or ""),
                    "name": item.get("shippingPlaceName", "") or "",
                    "address": first_addr.get("returnAddress", "")
                    or first_addr.get("placeAddress", "")
                    or "",
                }
            )
        return result

    async def get_return_shipping_centers(self) -> list[dict[str, Any]]:
        """쿠팡 반품지(회수지) 목록 조회.

        GET /v2/providers/openapi/apis/api/v4/vendors/{vendor_id}/returnShippingCenters
        응답 구조: { data: { content: [{ returnCenterCode, shippingPlaceName, placeAddresses: [...], usable }] } }
        """
        res = await self._call_api(
            "GET",
            f"/v2/providers/openapi/apis/api/v4/vendors/{self.vendor_id}/returnShippingCenters",
            params={"pageNum": "1", "pageSize": "50"},
        )
        data = res.get("data") if isinstance(res, dict) else None
        items: list[Any] = []
        if isinstance(data, dict):
            items = data.get("content") or []
        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if not item.get("usable", True):
                continue
            addresses = item.get("placeAddresses") or []
            first_addr = (
                addresses[0] if addresses and isinstance(addresses[0], dict) else {}
            )
            result.append(
                {
                    "code": item.get("returnCenterCode", "") or "",
                    "name": item.get("shippingPlaceName", "") or "",
                    "address": first_addr.get("returnAddress", "") or "",
                    "address_detail": first_addr.get("returnAddressDetail", "") or "",
                    "zipcode": first_addr.get("returnZipCode", "") or "",
                    "phone": first_addr.get("companyContactNumber", "") or "",
                }
            )
        return result

    # ------------------------------------------------------------------
    # 상품 등록/수정
    # ------------------------------------------------------------------

    async def register_product(self, product_data: dict[str, Any]) -> dict[str, Any]:
        """상품 등록.

        Coupang Wing API: POST /v2/providers/seller_api/apis/api/v1/marketplace/seller-products
        """
        result = await self._call_api(
            "POST",
            "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products",
            body=product_data,
        )
        return {"success": True, "data": result}

    async def update_product(
        self, seller_product_id: str, product_data: dict[str, Any]
    ) -> dict[str, Any]:
        """상품 수정."""
        result = await self._call_api(
            "PUT",
            f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{seller_product_id}",
            body=product_data,
        )
        return {"success": True, "data": result}

    async def approve_product(self, seller_product_id: str) -> dict[str, Any]:
        """상품 승인요청 — 임시저장 상태를 승인대기로 전환.

        쿠팡 Wing API: PUT /v2/.../marketplace/seller-products/{spid}/approvals
        body 없음.
        register/update API에서 requested=true 가 무시되는 케이스가 있어
        명시 호출로 보강 (확인된 사실: 2026-05-11).
        """
        return await self._call_api(
            "PUT",
            f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{seller_product_id}/approvals",
        )

    async def update_item_price(
        self, vendor_item_id: int | str, price: int
    ) -> dict[str, Any]:
        """옵션(vendorItemId) 단위 가격 변경 — 부분 업데이트.

        쿠팡 Wing API: PUT /v2/.../marketplace/vendor-items/{vendorItemId}/prices/{price}
        Path segment가 'vendor-items' (seller-products X). body 없음.
        forceSalePriceUpdate=true: 변경 비율 제한 우회 (오토튠 빈번 변동 대응).
        """
        path = (
            f"/v2/providers/seller_api/apis/api/v1/marketplace/"
            f"vendor-items/{vendor_item_id}/prices/{int(price)}"
        )
        return await self._call_api(
            "PUT", path, params={"forceSalePriceUpdate": "true"}
        )

    async def update_item_quantity(
        self, vendor_item_id: int | str, quantity: int
    ) -> dict[str, Any]:
        """옵션(vendorItemId) 단위 재고 변경 — 부분 업데이트.

        쿠팡 Wing API: PUT /v2/.../marketplace/vendor-items/{vendorItemId}/quantities/{quantity}
        Path segment가 'vendor-items'. body 없음.
        """
        path = (
            f"/v2/providers/seller_api/apis/api/v1/marketplace/"
            f"vendor-items/{vendor_item_id}/quantities/{int(quantity)}"
        )
        return await self._call_api("PUT", path)

    async def delete_product(self, seller_product_id: str) -> dict[str, Any]:
        """상품 삭제 (리스트에서 완전 제거)."""
        result = await self._call_api(
            "DELETE",
            f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{seller_product_id}",
        )
        return {"success": True, "data": result}

    async def get_product(self, seller_product_id: str) -> dict[str, Any]:
        """상품 조회."""
        return await self._call_api(
            "GET",
            f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{seller_product_id}",
        )

    async def list_seller_products(
        self,
        status: Optional[str] = None,
        max_per_page: int = 100,
        max_pages: int = 2000,
        throttle: float = 0.4,
    ) -> list[dict[str, Any]]:
        """등록된 모든 셀러상품 페이징 조회 (유령삭제 양방향 동기화용).

        쿠팡 Wing API: GET /v2/providers/seller_api/apis/api/v1/marketplace/seller-products
        - 페이징: nextToken (빈 문자열이면 마지막 페이지)
        - status (선택): IN_REVIEW/SAVED/APPROVING/APPROVED/PARTIAL_APPROVED/DENIED/DELETED
          미지정 시 전체 상태 반환. DELETED는 수집 후 호출측에서 필터.
        - maxPerPage: 1~100 (기본 100)
        - max_pages: 안전장치 (2000 페이지 = 최대 20만개)
        - throttle: 호출 간격 (rate limit 회피)
        """
        path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
        results: list[dict[str, Any]] = []
        next_token = ""
        page_count = 0

        while page_count < max_pages:
            params: dict[str, str] = {
                "vendorId": self.vendor_id,
                "maxPerPage": str(max_per_page),
            }
            if next_token:
                params["nextToken"] = next_token
            if status:
                params["status"] = status

            try:
                resp = await self._call_api("GET", path, params=params)
            except CoupangApiError as e:
                # 429 (rate limit) 대응 — 1회 재시도
                if "429" in str(e) or "TOO_MANY" in str(e).upper():
                    logger.warning(
                        "[쿠팡] list_seller_products 429 → 5초 대기 후 재시도"
                    )
                    await asyncio.sleep(5)
                    resp = await self._call_api("GET", path, params=params)
                else:
                    raise

            data = resp.get("data", []) if isinstance(resp, dict) else []
            if not isinstance(data, list):
                data = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                spid = item.get("sellerProductId")
                if not spid:
                    continue
                results.append(
                    {
                        "seller_product_id": str(spid),
                        "product_id": str(item.get("productId") or ""),
                        "product_name": str(item.get("sellerProductName") or ""),
                        "status_name": str(item.get("statusName") or ""),
                        "created_at": str(item.get("createdAt") or ""),
                    }
                )

            next_token = (
                str(resp.get("nextToken") or "") if isinstance(resp, dict) else ""
            )
            page_count += 1
            if not next_token:
                break
            await asyncio.sleep(throttle)

        logger.info(
            f"[쿠팡] list_seller_products 완료: {len(results)}개 "
            f"(페이지 {page_count}, status={status or 'ALL'})"
        )
        return results

    # ------------------------------------------------------------------
    # 상품 데이터 변환
    # ------------------------------------------------------------------

    @staticmethod
    def transform_product(
        product: dict[str, Any],
        category_id: str = "",
        return_center_code: str = "",
        outbound_shipping_place_code: str = "",
        notice_meta: Any = None,
    ) -> dict[str, Any]:
        """SambaCollectedProduct → 쿠팡 상품 등록 데이터 변환.

        쿠팡 Wing API 공식 스펙 기준 전체 필수필드 포함.
        SEO 최적화: 노출상품명 자동생성, 검색태그, 옵션별 색상분리, 상세이미지 분리.

        notice_meta: get_notice_categories(category_id) 결과 (선택). 있으면 동적 매핑,
        없으면 정적 매핑 폴백 (의류/신발 등록 시 옵션의 notice가 거부되는 미스매치 방지).
        """
        from datetime import datetime as dt, timezone as tz

        _is_gsshop = (product.get("source_site") or "").upper() == "GSSHOP"

        # 외부 호스트(GSShop 자사 CDN 화이트리스트 외) URL 제거 — 쿠팡 검증 거절 방지
        _images_orig = product.get("images") or []
        images_raw = _filter_gsshop_domain(_images_orig) if _is_gsshop else _images_orig
        # 필터링 후 빈 배열이면 원본 첫 URL 1개는 유지 (대표 이미지 보장)
        if not images_raw and _images_orig:
            images_raw = [_images_orig[0]]

        detail_images = (
            _filter_gsshop_domain(product.get("detail_images") or [])
            if _is_gsshop
            else (product.get("detail_images") or [])
        )

        _main_raw = product.get("coupang_main_image") or ""
        _main_filtered = (
            _filter_gsshop_domain([_main_raw])
            if (_is_gsshop and _main_raw)
            else ([_main_raw] if _main_raw else [])
        )
        coupang_main = _main_filtered[0] if _main_filtered else ""
        default_color = product.get("color", "") or "상세 이미지 참조"
        detail_html = (
            product.get("detail_html", "") or f"<p>{product.get('name', '')}</p>"
        )

        # 카테고리 코드 (숫자만 허용)
        display_category = (
            int(category_id) if category_id and str(category_id).isdigit() else 0
        )

        # 판매기간
        now = dt.now(tz.utc).strftime("%Y-%m-%dT%H:%M:%S")

        # 고시정보 — 카테고리별 동적 생성 (메타 API 결과 우선, 실패 시 정적 매핑 폴백)
        from backend.domain.samba.proxy.notice_utils import (
            build_coupang_notices,
            build_coupang_notices_with_meta,
        )

        notices = None
        if notice_meta is not None:
            try:
                notices = build_coupang_notices_with_meta(product, notice_meta)
            except Exception as _e:
                logger.warning(f"[쿠팡 고시정보] 동적 매핑 실패, 정적 매핑 폴백: {_e}")
                notices = None
        if not notices:
            notices = build_coupang_notices(product)

        # detail_html 안의 외부 호스트 <img> 태그 제거 (쿠팡 검증 거절 방지, GSShop 전용)
        detail_html = (
            _filter_html_external_images(detail_html) if _is_gsshop else detail_html
        )

        # 상세 컨텐츠 (IMAGE/TEXT 혼합)
        content_details = _build_content_details(detail_html)

        # 아이템별 공통 필드 생성 함수
        def _build_item(
            item_name: str,
            stock: int,
            size_val: str,
            item_color: str = "",
            add_price: int = 0,
            extra_attr_name: str = "",
            extra_attr_value: str = "",
        ) -> dict[str, Any]:
            rep_image = coupang_main or (images_raw[0] if images_raw else "")
            item_images: list[dict[str, Any]] = []
            if rep_image:
                item_images.append(
                    {
                        "imageOrder": 0,
                        "imageType": "REPRESENTATION",
                        "vendorPath": rep_image,
                    }
                )
                for idx, url in enumerate(images_raw[1:10], start=1):
                    item_images.append(
                        {
                            "imageOrder": idx,
                            "imageType": "DETAIL",
                            "vendorPath": url,
                        }
                    )

            # 아이템별 색상 (옵션에서 파싱된 개별 색상 우선)
            resolved_color = item_color or default_color

            # 옵션별 추가금액(add_price) 반영 — Gift box 같은 extra 옵션의 +N원
            base_sale = int(product.get("sale_price", 0))
            base_orig = (int(product.get("original_price", 0)) // 100) * 100
            opt_sale = base_sale + int(add_price or 0)
            opt_orig = base_orig + int(add_price or 0) if base_orig else 0

            return {
                "itemName": item_name,
                "originalPrice": opt_orig,
                "salePrice": opt_sale,
                "maximumBuyCount": min(stock, 99999),
                "maximumBuyForPerson": 0,
                "maximumBuyForPersonPeriod": 1,
                "outboundShippingTimeDay": 3,
                "unitCount": 1,
                "adultOnly": "EVERYONE",
                "taxType": "TAX",
                "parallelImported": "NOT_PARALLEL_IMPORTED",
                "overseasPurchased": "NOT_OVERSEAS_PURCHASED",
                "pccNeeded": False,
                "barcode": "",
                "emptyBarcode": True,
                "emptyBarcodeReason": "바코드 없음",
                "offerCondition": "NEW",
                # attributes — 색상축 + 옵션 그룹명 기반 자유 입력 attribute
                # 두 번째 그룹(예: "Gift box 추가") 이 있으면 사이즈축은 추가하지 않고
                # 색상 × extra 2축으로만 등록. 두 번째 그룹이 없을 때만 사이즈축 채움.
                "attributes": (
                    (
                        []
                        if extra_attr_name
                        else [
                            {
                                "attributeTypeName": "패션의류/잡화 사이즈",
                                "attributeValueName": (size_val or "")[:30],
                            },
                        ]
                    )
                    + [
                        {
                            "attributeTypeName": "색상",
                            "attributeValueName": (resolved_color or "")[:30],
                        },
                    ]
                    + (
                        [
                            {
                                "attributeTypeName": extra_attr_name[:25],
                                "attributeValueName": extra_attr_value[:30],
                            }
                        ]
                        if extra_attr_name and extra_attr_value
                        else []
                    )
                ),
                "contents": [
                    {
                        "contentsType": "HTML",
                        "contentDetails": content_details,
                    }
                ],
                "notices": notices,
                "images": item_images,
                "certifications": [
                    {"certificationType": "NOT_REQUIRED", "certificationCode": ""}
                ],
            }

        # 옵션 처리 — 색상/사이즈 분리
        options = product.get("options") or []
        _max_stock_cap = int(product.get("_max_stock") or 0)
        # 옵션별 추가금액(add_price) 기준치 — 최저가 옵션을 base 로 보고 차액 계산.
        # 무신사 cartesian 곱으로 만들어진 options 의 경우 price 만 합산돼 있고
        # add_price 필드가 누락된 과거 데이터에도 동일 fallback 으로 동작.
        _opt_prices = [int(o.get("price", 0) or 0) for o in options if o.get("price")]
        _base_opt_price = min(_opt_prices) if _opt_prices else 0
        # 옵션 그룹명 — 무신사 main × extra cartesian 곱일 때 두 번째 그룹명
        # (예: "Gift box 추가") 을 쿠팡 자유 입력 attribute 로 등록하기 위해 사용.
        _opt_groups = product.get("option_group_names") or []
        _second_group = ""
        if isinstance(_opt_groups, list) and len(_opt_groups) >= 2:
            _gname = str(_opt_groups[1] or "").strip()
            # 두 번째 그룹명이 "사이즈/size" 류면 자유 attribute 안 만들고 기존 size 매핑 사용
            if _gname and "사이즈" not in _gname and "size" not in _gname.lower():
                _second_group = _gname
        items = []
        if options:
            for opt in options:
                opt_name = opt.get("name", "") or opt.get("size", "") or "기본"
                _raw = opt.get("stock")
                if _raw is None:
                    opt_stock = _max_stock_cap if _max_stock_cap > 0 else 99
                elif int(_raw) <= 0:
                    opt_stock = 0
                else:
                    opt_stock = (
                        min(int(_raw), _max_stock_cap)
                        if _max_stock_cap > 0
                        else int(_raw)
                    )
                opt_color, size_val = _parse_option_color_size(opt_name, default_color)
                # add_price: 명시 필드 우선, 없으면 옵션 price - 최저가로 추출
                opt_add_price = int(opt.get("add_price", 0) or 0)
                if not opt_add_price and _base_opt_price:
                    _this_price = int(opt.get("price", 0) or 0)
                    if _this_price > _base_opt_price:
                        opt_add_price = _this_price - _base_opt_price
                # 멀티옵션 자유 attribute — _second_group 이 있을 때만 활성화
                # extra_attr_value 는 옵션명의 마지막 토큰 (예: "차콜 / Gift box" → "Gift box")
                _extra_name = ""
                _extra_value = ""
                if _second_group:
                    _parts = re.split(r"\s*/\s*|\s*,\s*", opt_name.strip())
                    if len(_parts) >= 2:
                        _extra_name = _second_group
                        _extra_value = _parts[-1].strip()
                items.append(
                    _build_item(
                        opt_name,
                        opt_stock,
                        size_val,
                        opt_color,
                        opt_add_price,
                        _extra_name,
                        _extra_value,
                    )
                )
        else:
            _no_opt_stock = _max_stock_cap if _max_stock_cap > 0 else 99
            items.append(
                _build_item(
                    product.get("name", "기본"), _no_opt_stock, "FREE", default_color
                )
            )

        # SEO 최적화: 노출상품명 + 검색태그
        display_name = _build_display_product_name(product)
        search_tags = _build_search_tags(product)

        result: dict[str, Any] = {
            "displayCategoryCode": display_category,
            "sellerProductName": re.sub(
                r"\s{2,}", " ", re.sub(r"[#_]+", " ", product.get("name", ""))
            ).strip()[:100],
            "vendorId": "",  # 런타임에 디스패처에서 채움
            "saleStartedAt": now,
            "saleEndedAt": "2099-01-01T23:59:59",
            "displayProductName": display_name,
            "brand": product.get("brand", ""),
            "generalProductName": display_name,
            "productGroup": "",
            "deliveryMethod": "SEQUENCIAL",
            "deliveryCompanyCode": "CJGLS",
            "deliveryChargeType": "FREE",
            "deliveryCharge": 0,
            "freeShipOverAmount": 0,
            "deliveryChargeOnReturn": 2500,
            "remoteAreaDeliverable": "N",
            "unionDeliveryType": "NOT_UNION_DELIVERY",
            "returnCenterCode": return_center_code,
            "returnChargeName": "반품지",
            "companyContactNumber": product.get("_as_phone", "") or "상세페이지 참조",
            "returnZipCode": "00000",
            "returnAddress": "상세페이지 참조",
            "returnAddressDetail": "상세페이지 참조",
            "returnCharge": 2500,
            "outboundShippingPlaceCode": (
                int(outbound_shipping_place_code) if outbound_shipping_place_code else 0
            ),
            "vendorUserId": "",  # 런타임에 디스패처에서 채움
            "requested": True,
            "items": items,
            "requiredDocuments": [],
            "extraInfoMessage": "",
            "manufacture": product.get("manufacturer", "") or product.get("brand", ""),
        }

        # 검색태그 추가
        if search_tags:
            result["searchTags"] = search_tags

        return result

    # ------------------------------------------------------------------
    # 주문 조회
    # ------------------------------------------------------------------

    async def get_orders(
        self,
        days: int = 7,
        status: str = "",
        max_per_page: int = 50,
    ) -> list[dict[str, Any]]:
        """최근 N일간 주문시트 조회.

        쿠팡 Wing API: GET /v2/providers/openapi/apis/api/v4/vendors/{vendorId}/ordersheets
        - 날짜 형식: yyyy-MM-dd (시간 X)
        - status 파라미터 필수 → status 미지정 시 6개 상태를 순회 후 shipmentBoxId 기준 dedup
        - 페이징: nextToken 기반 커서 방식
        """
        # 쿠팡 ordersheets API에서 인식되는 status 5개
        # CANCEL은 별도 API (취소 조회)에서 처리해야 하므로 제외
        STATUSES = [
            "ACCEPT",
            "INSTRUCT",
            "DEPARTURE",
            "DELIVERING",
            "FINAL_DELIVERY",
        ]
        targets = [status] if status else STATUSES

        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days)
        # createdAtTo는 exclusive로 처리되므로 +1일 추가 (당일 주문 누락 방지)
        until = now + timedelta(days=1)
        created_at_from = since.strftime("%Y-%m-%d")
        created_at_to = until.strftime("%Y-%m-%d")

        path = f"/v2/providers/openapi/apis/api/v4/vendors/{self.vendor_id}/ordersheets"
        seen_ids: set[int] = set()
        all_orders: list[dict[str, Any]] = []

        for idx, target_status in enumerate(targets):
            if idx > 0:
                await asyncio.sleep(1.5)  # 쿠팡 API rate limit 회피 (HTTP 429)
            next_token = ""
            for _ in range(100):  # 무한루프 방지
                params: dict[str, str] = {
                    "createdAtFrom": created_at_from,
                    "createdAtTo": created_at_to,
                    "status": target_status,
                    "maxPerPage": str(max_per_page),
                }
                if next_token:
                    params["nextToken"] = next_token

                result = await self._call_api("GET", path, params=params)

                data = result.get("data", []) if isinstance(result, dict) else []
                extracted: list[dict[str, Any]] = []
                if isinstance(data, list):
                    extracted = data
                elif isinstance(data, dict):
                    sheets = data.get("orderSheets", data.get("content", []))
                    if isinstance(sheets, list):
                        extracted = sheets

                for order in extracted:
                    box_id = order.get("shipmentBoxId")
                    if box_id and box_id not in seen_ids:
                        seen_ids.add(box_id)
                        all_orders.append(order)

                next_token = (
                    result.get("nextToken", "") if isinstance(result, dict) else ""
                )
                if not next_token:
                    break

        logger.info(
            f"[쿠팡] 주문 조회 완료: {len(all_orders)}건 "
            f"(최근 {days}일, status={status or 'ALL'})"
        )
        return all_orders

    async def confirm_orders(
        self,
        shipment_box_ids: list[int],
    ) -> list[dict[str, Any]]:
        """발주확인 (주문시트 확인).

        쿠팡 Wing API: PUT /v2/providers/openapi/apis/api/v4/vendors/{vendorId}/ordersheets/confirmation
        """
        results: list[dict[str, Any]] = []
        # 쿠팡은 shipmentBoxId 단위로 확인
        for box_id in shipment_box_ids:
            try:
                path = f"/v2/providers/openapi/apis/api/v4/vendors/{self.vendor_id}/ordersheets/{box_id}/confirmation"
                result = await self._call_api("PUT", path)
                results.append(
                    {"shipmentBoxId": box_id, "success": True, "data": result}
                )
            except CoupangApiError as e:
                logger.warning(f"[쿠팡] 발주확인 실패 (boxId={box_id}): {e}")
                results.append(
                    {"shipmentBoxId": box_id, "success": False, "error": str(e)}
                )
        logger.info(
            f"[쿠팡] 발주확인 요청: {len(shipment_box_ids)}건, 성공: {sum(1 for r in results if r['success'])}건"
        )
        return results

    # ------------------------------------------------------------------
    # 송장 전송
    # ------------------------------------------------------------------

    async def update_shipping(
        self,
        shipment_box_id: int,
        delivery_company_code: str,
        invoice_number: str,
    ) -> dict[str, Any]:
        """송장번호 입력 (배송 시작).

        쿠팡 Wing API: PUT /v2/.../vendors/{vendorId}/ordersheets/{shipmentBoxId}/invoices
        """
        path = f"/v2/providers/openapi/apis/api/v4/vendors/{self.vendor_id}/ordersheets/{shipment_box_id}/invoices"
        body = {
            "vendorId": self.vendor_id,
            "shipmentBoxId": shipment_box_id,
            "deliveryCompanyCode": delivery_company_code,
            "invoiceNumber": invoice_number,
        }
        result = await self._call_api("PUT", path, body=body)
        logger.info(
            f"[쿠팡] 송장 입력 완료: boxId={shipment_box_id}, 송장={invoice_number}"
        )
        return result

    # ------------------------------------------------------------------
    # 반품/교환
    # ------------------------------------------------------------------

    async def get_return_requests(
        self,
        days: int = 30,
        status: str = "",
        max_per_page: int = 50,
    ) -> list[dict[str, Any]]:
        """반품요청 목록 조회.

        쿠팡 Wing API: GET /v2/.../vendors/{vendorId}/returnRequests
        페이징: nextToken 기반 커서 방식
        """
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days)

        all_returns: list[dict[str, Any]] = []
        next_token = ""

        for _ in range(100):  # 무한루프 방지
            params: dict[str, str] = {
                "createdAtFrom": since.strftime("%Y-%m-%d"),
                "createdAtTo": now.strftime("%Y-%m-%d"),
                "maxPerPage": str(max_per_page),
            }
            if status:
                params["status"] = status
            if next_token:
                params["nextToken"] = next_token

            path = f"/v2/providers/openapi/apis/api/v4/vendors/{self.vendor_id}/returnRequests"
            result = await self._call_api("GET", path, params=params)

            data = result.get("data", []) if isinstance(result, dict) else []
            if isinstance(data, list):
                all_returns.extend(data)
            elif isinstance(data, dict):
                items = data.get("returnRequests", data.get("content", []))
                if isinstance(items, list):
                    all_returns.extend(items)

            next_token = result.get("nextToken", "") if isinstance(result, dict) else ""
            if not next_token:
                break

        logger.info(f"[쿠팡] 반품요청 조회 완료: {len(all_returns)}건 (최근 {days}일)")
        return all_returns

    async def approve_return(
        self,
        receipt_id: int,
    ) -> dict[str, Any]:
        """반품요청 승인.

        쿠팡 Wing API: PATCH /v2/.../vendors/{vendorId}/returnRequests/{receiptId}/approval
        """
        path = f"/v2/providers/openapi/apis/api/v4/vendors/{self.vendor_id}/returnRequests/{receipt_id}/approval"
        result = await self._call_api("PATCH", path)
        logger.info(f"[쿠팡] 반품 승인 완료: receiptId={receipt_id}")
        return result

    async def get_exchange_requests(
        self,
        days: int = 30,
        status: str = "",
        max_per_page: int = 50,
    ) -> list[dict[str, Any]]:
        """교환요청 목록 조회.

        쿠팡 Wing API: GET /v2/.../vendors/{vendorId}/exchangeRequests
        페이징: nextToken 기반 커서 방식
        """
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days)

        all_exchanges: list[dict[str, Any]] = []
        next_token = ""

        for _ in range(100):  # 무한루프 방지
            params: dict[str, str] = {
                "createdAtFrom": since.strftime("%Y-%m-%d"),
                "createdAtTo": now.strftime("%Y-%m-%d"),
                "maxPerPage": str(max_per_page),
            }
            if status:
                params["status"] = status
            if next_token:
                params["nextToken"] = next_token

            path = f"/v2/providers/openapi/apis/api/v4/vendors/{self.vendor_id}/exchangeRequests"
            result = await self._call_api("GET", path, params=params)

            data = result.get("data", []) if isinstance(result, dict) else []
            if isinstance(data, list):
                all_exchanges.extend(data)
            elif isinstance(data, dict):
                items = data.get("exchangeRequests", data.get("content", []))
                if isinstance(items, list):
                    all_exchanges.extend(items)

            next_token = result.get("nextToken", "") if isinstance(result, dict) else ""
            if not next_token:
                break

        logger.info(
            f"[쿠팡] 교환요청 조회 완료: {len(all_exchanges)}건 (최근 {days}일)"
        )
        return all_exchanges

    async def approve_exchange(
        self,
        receipt_id: int,
    ) -> dict[str, Any]:
        """교환요청 승인.

        쿠팡 Wing API: PATCH /v2/.../vendors/{vendorId}/exchangeRequests/{receiptId}/approval
        """
        path = f"/v2/providers/openapi/apis/api/v4/vendors/{self.vendor_id}/exchangeRequests/{receipt_id}/approval"
        result = await self._call_api("PATCH", path)
        logger.info(f"[쿠팡] 교환 승인 완료: receiptId={receipt_id}")
        return result

    # ------------------------------------------------------------------
    # CS 문의 수집
    # ------------------------------------------------------------------

    async def get_inquiries(
        self,
        days: int = 7,
        answered: Optional[bool] = None,
        max_per_page: int = 50,
    ) -> list[dict[str, Any]]:
        """CS 문의 목록 조회.

        쿠팡 Wing API v5: GET /v2/.../api/v5/vendors/{vendorId}/onlineInquiries
        페이징: pageNum 기반 offset 방식. 쿠팡 docs 조회 범위 최대 7일.
        """
        # docs 제약: 조회 기간 최대 7일 (쿠팡 inquiryStartAt/EndAt inclusive)
        if days > 7:
            logger.warning(
                f"[쿠팡] CS 문의 조회 days={days} 가 7 초과 — 7일로 clamp"
            )
            days = 7

        now = datetime.now(timezone.utc)
        # inclusive 양 끝 포함 → 7일 범위는 6일 차이로 보내야 함
        since = now - timedelta(days=max(days - 1, 0))

        # answered(bool) → answeredType(enum) 변환
        if answered is True:
            answered_type = "ANSWERED"
        elif answered is False:
            answered_type = "NOANSWER"
        else:
            answered_type = "ALL"

        all_inquiries: list[dict[str, Any]] = []

        for page_num in range(1, 101):  # 무한루프 방지 (최대 100페이지)
            params: dict[str, str] = {
                "inquiryStartAt": since.strftime("%Y-%m-%d"),
                "inquiryEndAt": now.strftime("%Y-%m-%d"),
                "vendorId": self.vendor_id,
                "answeredType": answered_type,
                "pageSize": str(max_per_page),
                "pageNum": str(page_num),
            }

            path = f"/v2/providers/openapi/apis/api/v5/vendors/{self.vendor_id}/onlineInquiries"
            result = await self._call_api("GET", path, params=params)

            data = result.get("data", []) if isinstance(result, dict) else []
            page_items: list[dict[str, Any]] = []
            if isinstance(data, list):
                page_items = data
            elif isinstance(data, dict):
                items = data.get("inquiries", data.get("content", []))
                if isinstance(items, list):
                    page_items = items

            all_inquiries.extend(page_items)

            # 페이지 종료 판단: 반환 건수가 pageSize 미만이면 마지막 페이지
            if len(page_items) < max_per_page:
                break

        logger.info(f"[쿠팡] CS 문의 조회 완료: {len(all_inquiries)}건 (최근 {days}일)")
        return all_inquiries

    async def reply_inquiry(
        self,
        inquiry_id: int,
        content: str,
        reply_by: str,
    ) -> dict[str, Any]:
        """CS 문의 답변 등록.

        쿠팡 Wing API v4: POST /v2/.../api/v4/vendors/{vendorId}/onlineInquiries/{inquiryId}/replies
        body 필수 3종: content, vendorId, replyBy(Wing 로그인 ID).
        중복 답변 금지 — 동일 inquiryId 에 이미 답변 있으면 HTTP 400.
        """
        path = f"/v2/providers/openapi/apis/api/v4/vendors/{self.vendor_id}/onlineInquiries/{inquiry_id}/replies"
        body = {
            "content": content,
            "vendorId": self.vendor_id,
            "replyBy": reply_by,
        }
        result = await self._call_api("POST", path, body=body)
        logger.info(f"[쿠팡] CS 문의 답변 완료: inquiryId={inquiry_id}")
        return result


class CoupangApiError(Exception):
    """쿠팡 API 에러."""

    pass

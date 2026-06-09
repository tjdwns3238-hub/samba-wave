"""ESM Plus 판매자 API 클라이언트 (지마켓/옥션 통합).

ESM Trading API v2 (sa2.esmplus.com) 기반.
JWT(HS256) 인증으로 상품 등록/수정/삭제/판매상태/이미지 관리.

지마켓(siteType=2, siteKey=Gmkt, ssiPrefix=G)과
옥션(siteType=1, siteKey=Iac, ssiPrefix=A)을 하나의 클라이언트로 처리.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any

import httpx
import jwt

from backend.domain.samba.proxy.notice_utils import detect_notice_group
from backend.utils.logger import logger


class _AsyncTokenBucket:
    """공유 토큰버킷 — ESM Plus API 호출 빈도 제한.

    ESM 정확한 한도는 미공개이나 PDF 가이드에 일부 API "분당 30회" 명시.
    보수적으로 30/min 기본 — 운영자 ESM 한도 조정 시 settings 에서 override 권장.
    """

    def __init__(self, rate_per_min: int = 30) -> None:
        self.rate = rate_per_min / 60.0  # tokens per second
        self.capacity = float(rate_per_min)
        self.tokens = float(rate_per_min)
        self.last_refill = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                logger.debug(f"[ESM rate-limit] waiting {wait:.2f}s for token")
                await asyncio.sleep(wait)
                self.tokens = 0.0
            else:
                self.tokens -= 1


# 모듈 전역 토큰버킷 — 모든 ESMPlusClient 인스턴스 공유. 호스팅 ID 와 무관.
# settings 에서 rate_per_min 동적 변경 시 _ESM_RATE_LIMITER.rate 갱신 가능.
_ESM_RATE_LIMITER = _AsyncTokenBucket(rate_per_min=30)


# 주문조회 전용 5초 인터벌 락 — ESM resultCode=3000 "주문 조회는 5초당 1회 호출 가능합니다"
# 토큰버킷 30/min(2초)으로는 4계정×5상태=20회 폭주 시 3000 응답.
# search_orders 호출 직전 마지막 호출 후 5.2초 경과 보장.
_ESM_ORDER_LOCK = asyncio.Lock()
_ESM_ORDER_LAST_CALL: float = 0.0
_ESM_ORDER_MIN_INTERVAL = 5.2


async def _esm_order_throttle() -> None:
    """search_orders 호출 직전 5.2초 글로벌 인터벌 보장."""
    global _ESM_ORDER_LAST_CALL
    async with _ESM_ORDER_LOCK:
        now = time.monotonic()
        elapsed = now - _ESM_ORDER_LAST_CALL
        if elapsed < _ESM_ORDER_MIN_INTERVAL:
            wait = _ESM_ORDER_MIN_INTERVAL - elapsed
            logger.debug(f"[ESM order throttle] waiting {wait:.2f}s")
            await asyncio.sleep(wait)
        _ESM_ORDER_LAST_CALL = time.monotonic()


# 옵션 그룹/값 TTL 캐시 — 옵션값 list 가 크고(색상 1.3K건) 자주 변하지 않음.
# 카테고리당 그룹은 거의 영구. 옵션값은 신규 색상/사이즈 등 가끔 추가.
_OPT_CACHE_TTL_SEC = 3600  # 1시간
_opt_cache: dict[tuple[str, str], tuple[float, Any]] = {}


def _opt_cache_get(key: tuple[str, str]) -> Any | None:
    """TTL 캐시 조회. 만료 시 None."""
    entry = _opt_cache.get(key)
    if entry is None:
        return None
    expire_at, value = entry
    if time.monotonic() > expire_at:
        _opt_cache.pop(key, None)
        return None
    return value


def _opt_cache_set(key: tuple[str, str], value: Any) -> None:
    _opt_cache[key] = (time.monotonic() + _OPT_CACHE_TTL_SEC, value)


class ESMPlusClient:
    """ESM Plus 판매자 API 클라이언트.

    Args:
      hosting_id: 호스팅사(셀링툴) 마스터 ID (JWT kid)
      secret_key: 호스팅사 시크릿 키 (JWT 서명용)
      seller_id: 판매자 ID (옥션 or 지마켓)
      site: 마켓 구분 ("gmarket" or "auction")
    """

    BASE = "https://sa2.esmplus.com"

    # siteType: 1=옥션, 2=지마켓
    SITE_CONFIG: dict[str, dict[str, Any]] = {
        "gmarket": {
            "siteType": 2,
            "siteKey": "Gmkt",
            "ssiPrefix": "G",
            "label": "지마켓",
        },
        "auction": {"siteType": 1, "siteKey": "Iac", "ssiPrefix": "A", "label": "옥션"},
    }

    def __init__(
        self,
        hosting_id: str,
        secret_key: str,
        seller_id: str,
        site: str = "gmarket",
    ) -> None:
        self.hosting_id = hosting_id
        self.secret_key = secret_key
        self.seller_id = seller_id
        self.site = site
        self.cfg = self.SITE_CONFIG[site]
        self._timeout = httpx.Timeout(30.0, connect=10.0)
        # 재사용 httpx client — 매 호출마다 새 TCP/TLS handshake 회피.
        # 첫 사용 시 lazy 생성. aclose() 명시 호출 또는 async-with 패턴 권장.
        self._http_client: httpx.AsyncClient | None = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        """공유 httpx.AsyncClient — connection pool + keep-alive 재사용."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=self._timeout,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._http_client

    async def aclose(self) -> None:
        """공유 client 종료. 운영자가 인스턴스 폐기 전 호출 권장."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> "ESMPlusClient":
        await self._get_http_client()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # JWT 토큰 생성
    # ------------------------------------------------------------------

    def _generate_token(self) -> str:
        """HS256 JWT 토큰 생성.

        Header: {"alg":"HS256","typ":"JWT","kid": hostingId}
        Payload: {"iss":"www.esmplus.com","sub":"sell","aud":"sa.esmplus.com","ssi":"G:판매자ID"}
        """
        header = {
            "alg": "HS256",
            "typ": "JWT",
            "kid": self.hosting_id,
        }
        payload = {
            "iss": "www.esmplus.com",
            "sub": "sell",
            "aud": "sa.esmplus.com",
            "iat": int(time.time()),
            "ssi": f"{self.cfg['ssiPrefix']}:{self.seller_id}",
        }
        return jwt.encode(payload, self.secret_key, algorithm="HS256", headers=header)

    def _headers(self) -> dict[str, str]:
        """API 요청 공통 헤더."""
        return {
            "Authorization": f"Bearer {self._generate_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # 공통 API 호출
    # ------------------------------------------------------------------

    # 재시도 가능한 상태 코드 — 429(rate limit) + 5xx(서버 일시 오류).
    # POST/PUT/DELETE 등 비-idempotent 메서드도 등록은 GoodsNo 중복 검증으로 안전,
    # sell-status/이미지 등 수정은 멱등 — 단순 재시도 허용.
    _RETRY_STATUS = {429, 500, 502, 503, 504}
    _MAX_RETRIES = 3

    async def _call_api(
        self,
        method: str,
        path: str,
        data: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        """ESM Plus API 호출 공통 메서드.

        - 토큰버킷으로 분당 호출 빈도 제한 (default 30/min, settings override).
        - 429/5xx 응답 시 지수 백오프 + 재시도 (최대 3회, 1s/2s/4s + jitter).
        """
        url = f"{self.BASE}{path}"
        label = self.cfg["label"]

        last_resp: httpx.Response | None = None
        for attempt in range(self._MAX_RETRIES + 1):
            # Rate limit — 분당 호출 빈도 제한
            await _ESM_RATE_LIMITER.acquire()

            try:
                client = await self._get_http_client()
                resp = await client.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=data,
                    params=params,
                )
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                # 네트워크 일시 오류 → 재시도. 마지막 시도에서도 실패 시 raise.
                if attempt >= self._MAX_RETRIES:
                    logger.error(
                        f"[{label}] API 연결 실패 {method} {path} (시도 {attempt + 1}): {exc}"
                    )
                    raise RuntimeError(
                        f"[{label}] API 연결 실패 ({type(exc).__name__}): {exc}"
                    ) from exc
                backoff = 2**attempt + random.uniform(0, 0.5)
                logger.warning(
                    f"[{label}] {method} {path} 연결 실패 (시도 {attempt + 1}/{self._MAX_RETRIES + 1}, {backoff:.1f}s 대기): {exc}"
                )
                await asyncio.sleep(backoff)
                continue

            last_resp = resp
            if resp.status_code not in self._RETRY_STATUS:
                break
            if attempt >= self._MAX_RETRIES:
                break

            backoff = 2**attempt + random.uniform(0, 0.5)
            logger.warning(
                f"[{label}] {method} {path} {resp.status_code} 응답 (시도 {attempt + 1}/{self._MAX_RETRIES + 1}, {backoff:.1f}s 백오프)"
            )
            await asyncio.sleep(backoff)

        assert last_resp is not None  # 위 루프 보장
        resp = last_resp

        # 204 No Content (DELETE 성공 등)
        if resp.status_code == 204:
            return {"resultCode": 0}

        raw: Any = {}
        try:
            raw = resp.json()
        except Exception:
            pass

        # 일부 endpoint (CS 등) 는 list 직접 반환 — dict wrapping.
        if isinstance(raw, list):
            body: dict[str, Any] = {"data": raw, "_list_response": True}
        elif isinstance(raw, dict):
            body = raw
        else:
            body = {}

        # ESM 응답 키 case mismatch — item API ('resultCode') vs shipping/v1 API ('ResultCode').
        # 양쪽 검사 + 0 이외(예: 1) 이면 에러로 raise. list 응답은 검증 skip.
        if body.get("_list_response"):
            result_code = 0
        else:
            result_code = body.get("resultCode")
            if result_code is None:
                result_code = body.get("ResultCode")
            if result_code is None:
                result_code = 0
        if resp.status_code >= 400 or (body and result_code != 0):
            msg = body.get("message") or body.get("Message") or resp.text[:500]
            logger.error(
                f"[{label}] API 에러 {method} {path}: {resp.status_code} / resultCode={result_code} / {msg}"
            )
            raise RuntimeError(f"[{label}] API 에러 (resultCode={result_code}): {msg}")

        return body

    # ------------------------------------------------------------------
    # 상품 CRUD
    # ------------------------------------------------------------------

    async def register_product(self, data: dict[str, Any]) -> dict[str, Any]:
        """상품 등록 — POST /item/v1/goods"""
        result = await self._call_api("POST", "/item/v1/goods", data=data)
        goods_no = result.get("goodsNo", "")
        site_detail = result.get("siteDetail", {})
        site_key_lower = self.cfg["siteKey"].lower()
        site_goods_no = ""
        for k, v in site_detail.items():
            if k.lower() == site_key_lower:
                site_goods_no = v.get("SiteGoodsNo", "")
                break
        logger.info(
            f"[{self.cfg['label']}] 상품 등록 성공: goodsNo={goods_no}, siteGoodsNo={site_goods_no}"
        )
        return {
            "goodsNo": str(goods_no),
            "siteGoodsNo": site_goods_no,
            **result,
        }

    async def update_product(
        self, goods_no: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """상품 수정 — PUT /item/v1/goods/{goodsNo}"""
        result = await self._call_api("PUT", f"/item/v1/goods/{goods_no}", data=data)
        logger.info(f"[{self.cfg['label']}] 상품 수정 성공: goodsNo={goods_no}")
        return result

    async def get_product(self, goods_no: str) -> dict[str, Any]:
        """상품 조회 — GET /item/v1/goods/{goodsNo}"""
        return await self._call_api("GET", f"/item/v1/goods/{goods_no}")

    async def delete_product(self, goods_no: str) -> dict[str, Any]:
        """상품 삭제 — DELETE /item/v1/goods/{goodsNo}

        주의:
        - 판매중지 상태에서만 삭제 가능 (그렇지 않으면 ESM 측에서 거부).
        - 등록 직후(<~15초) 즉시 삭제 시도 시 [F001000] cooldown 응답 발생 가능 —
          ESM 측 내부 lock. cooldown 회복 후 1회 재시도.
        """
        try:
            return await self._call_api("DELETE", f"/item/v1/goods/{goods_no}")
        except RuntimeError as exc:
            err = str(exc)
            # ESM 의 등록직후 cooldown 메시지 — 점진적 대기 (15s/30s/45s) 후 최대 2회 재시도.
            # 실 호출 검증: 등록+STOP+DELETE 직후 30s 이내 거부, 45s 안정.
            if "F001000" not in err and "다른 판매자의 주문" not in err:
                raise
            for wait in (30, 45):
                logger.warning(
                    f"[{self.cfg['label']}] 삭제 cooldown 감지 — {wait}초 대기 후 재시도 (goodsNo={goods_no})"
                )
                await asyncio.sleep(wait)
                try:
                    return await self._call_api("DELETE", f"/item/v1/goods/{goods_no}")
                except RuntimeError as inner:
                    if "F001000" not in str(inner) and "다른 판매자의 주문" not in str(
                        inner
                    ):
                        raise
            # 마지막 시도까지 cooldown 지속 — 운영자 수동 정리 필요
            logger.error(
                f"[{self.cfg['label']}] 삭제 cooldown 75초 후에도 지속 — 운영자 수동 정리 필요 (goodsNo={goods_no})"
            )
            raise

    # ------------------------------------------------------------------
    # 판매상태/가격/재고
    # ------------------------------------------------------------------

    async def update_sell_status(
        self, goods_no: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """판매상태/가격/재고 수정 — PUT /item/v1/goods/{goodsNo}/sell-status"""
        return await self._call_api(
            "PUT", f"/item/v1/goods/{goods_no}/sell-status", data=data
        )

    async def get_sell_status(self, goods_no: str) -> dict[str, Any]:
        """판매상태 조회 — GET /item/v1/goods/{goodsNo}/sell-status

        ESM 응답은 케이스 일관성 부족 — 'IsSell.gmkt'(camelCase) + 'Price.Gmkt'(PascalCase)
        mixed. ci_get() 헬퍼로 case-insensitive 조회 권장.
        """
        return await self._call_api("GET", f"/item/v1/goods/{goods_no}/sell-status")

    @staticmethod
    def ci_get(obj: dict[str, Any] | None, key: str, default: Any = None) -> Any:
        """ESM 응답 dict 의 case-insensitive 키 조회.

        ESM API 응답이 일관성 부족 — 등록 시 PascalCase 보내지만 조회 응답에서는
        필드별 case mixed (예: 'IsSell.gmkt' camelCase + 'Price.Gmkt' PascalCase).
        운영 코드는 ci_get() 으로 안전 접근.

        Example:
            >>> body = await client.get_sell_status(goods_no)
            >>> is_sell_gmkt = ESMPlusClient.ci_get(
            ...     ESMPlusClient.ci_get(body, "IsSell"), "Gmkt"
            ... )
        """
        if not isinstance(obj, dict):
            return default
        key_lower = key.lower()
        for k, v in obj.items():
            if k.lower() == key_lower:
                return v
        return default

    # ------------------------------------------------------------------
    # 이미지
    # ------------------------------------------------------------------

    async def update_images(
        self, goods_no: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """이미지 수정 — POST /item/v1/goods/{goodsNo}/images"""
        return await self._call_api(
            "POST", f"/item/v1/goods/{goods_no}/images", data=data
        )

    # ------------------------------------------------------------------
    # 배송 (출고지/반품지/발송정책)
    # ------------------------------------------------------------------

    async def get_places(self) -> list[dict[str, Any]]:
        """출고지/반품지 목록 — GET /item/v1/shipping/places

        응답 key 'shippingPlaces' (본진 'places' 추출은 잘못).
        """
        try:
            result = await self._call_api("GET", "/item/v1/shipping/places")
            return result.get("shippingPlaces", [])
        except Exception:
            return []

    async def resolve_return_addr_no(self, return_place_no: int) -> int:
        """반품지 placeNo → addrNo 해석 (#389).

        계정 config 의 returnPlaceNo 는 placeNo 값이지만 ESM 상품등록 API 의
        returnAndExchange.addrNo 는 주소번호(addrNo)를 요구한다. get_places 의
        place 객체에서 placeNo 가 일치하는 항목의 addrNo 를 반환.
        못 찾으면 0 (호출측에서 미주입 처리).
        """
        if not return_place_no:
            return 0
        try:
            for p in await self.get_places():
                if int(p.get("placeNo", 0) or 0) == return_place_no:
                    return int(p.get("addrNo", 0) or 0)
        except Exception:
            return 0
        return 0

    async def get_dispatch_policies(self) -> list[dict[str, Any]]:
        """발송정책 목록 — GET /item/v1/shipping/dispatch-policies"""
        try:
            result = await self._call_api("GET", "/item/v1/shipping/dispatch-policies")
            return result.get("dispatchPolicies", [])
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 카테고리
    # ------------------------------------------------------------------

    async def get_categories(self, cat_code: str = "") -> dict[str, Any]:
        """카테고리 조회.
        cat_code 미지정 시 전체 대분류, 지정 시 하위 카테고리.
        """
        path = "/item/v1/categories/site-cats"
        if cat_code:
            path = f"{path}/{cat_code}"
        return await self._call_api("GET", path)

    # ------------------------------------------------------------------
    # 추천옵션 (recommended-options)
    # ------------------------------------------------------------------
    # ESM 의 옵션 모델은 카테고리별 미리 정의된 옵션그룹/옵션값 사용.
    # 자유 텍스트 옵션은 별도 /order-options endpoint (권한 별도 필요).
    #
    # 등록 흐름:
    #   1. get_recommended_opt_groups(cat_code) — 카테고리의 옵션그룹 list
    #   2. get_recommended_opt_values(recommendedOptNo) — 그룹의 옵션값 list
    #   3. samba 옵션값 → ESM recommendedOptValueNo 매핑 (텍스트 매칭)
    #   4. set_recommended_options(goods_no, payload) — 상품 등록 후 옵션 추가
    # ------------------------------------------------------------------

    async def get_recommended_opt_groups(self, cat_code: str) -> list[dict[str, Any]]:
        """카테고리별 추천옵션그룹 — GET /item/v1/options/recommended-opts?catCode=...

        응답 key 'details' (응답 구조 확인: 색상/사이즈/직접입력 등).
        각 항목: { recommendedOptNo, recommendedOptName: {kor, eng, chi, jpn},
                  recommendedOptTypeName }
        TTL 캐시 적용 (모듈 전역). 카테고리당 그룹은 거의 변하지 않음 — 1시간 TTL.
        """
        cached = _opt_cache_get(("groups", cat_code))
        if cached is not None:
            return cached
        try:
            result = await self._call_api(
                "GET",
                "/item/v1/options/recommended-opts",
                params={"catCode": cat_code},
            )
            groups = result.get("details", []) or []
            _opt_cache_set(("groups", cat_code), groups)
            return groups
        except Exception as exc:
            logger.warning(
                f"[{self.cfg['label']}] 추천옵션그룹 조회 실패 cat={cat_code}: {exc}"
            )
            return []

    async def get_recommended_opt_values(
        self, recommended_opt_no: int | str
    ) -> list[dict[str, Any]]:
        """추천옵션그룹별 선택 항목 list — GET /item/v1/options/recommended-opts/{recommendedOptNo}

        recommendedOptValueNo=0 은 placeholder — 응답에 포함되지만 운영 매핑 시 제외.
        TTL 캐시 (1시간) — 옵션값 list 가 크고 (예: 색상 1,312건) 자주 변하지 않음.
        """
        cache_key = ("values", str(recommended_opt_no))
        cached = _opt_cache_get(cache_key)
        if cached is not None:
            return cached
        try:
            result = await self._call_api(
                "GET", f"/item/v1/options/recommended-opts/{recommended_opt_no}"
            )
            if isinstance(result, list):
                values = result
            else:
                values = []
                for key in ("details", "values", "recommendedOptValues"):
                    v = result.get(key)
                    if isinstance(v, list):
                        values = v
                        break
            _opt_cache_set(cache_key, values)
            return values
        except Exception as exc:
            logger.warning(
                f"[{self.cfg['label']}] 추천옵션값 조회 실패 optNo={recommended_opt_no}: {exc}"
            )
            return []

    async def set_recommended_options(
        self, goods_no: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """상품에 추천옵션 등록/수정 — PUT /item/v1/goods/{goodsNo}/recommended-options

        Payload 구조 (페이지 26, 16):
            {
                "type": 1,                     # 1=선택형, 2=2조합, 3=3조합, 5=텍스트형, ...
                "isStockManage": true,
                "independent": {               # type=1 (선택형)
                    "recommendedOptNo": <int>,
                    "details": [{ "recommendedOptValueNo": <int>, "addAmnt": 0,
                                  "qty": {"Gmkt": 10, "Iac": 10}, "isSoldOut": false,
                                  "isDisplay": true, "manageCode": "" }, ...]
                },
                "combination": null,
            }
        """
        return await self._call_api(
            "PUT", f"/item/v1/goods/{goods_no}/recommended-options", data=payload
        )

    async def get_recommended_options(self, goods_no: str) -> dict[str, Any]:
        """상품 추천옵션 조회 — GET /item/v1/goods/{goodsNo}/recommended-options"""
        return await self._call_api(
            "GET", f"/item/v1/goods/{goods_no}/recommended-options"
        )

    @staticmethod
    def detect_esm_option_group(
        samba_option_name: str, esm_groups: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """samba 옵션 이름 (예: '색상', '컬러', 'color') → ESM recommendedOpt 그룹 매칭.

        매칭 후보 — recommendedOptName.{kor,eng,korEng} 정확/부분 일치.
        '직접입력' (recommendedOptNo=0) 은 매칭 제외 (placeholder).
        """
        if not samba_option_name or not esm_groups:
            return None
        target = re.sub(r"\s+", "", samba_option_name.lower())

        def _norm(v: Any) -> str:
            return re.sub(r"\s+", "", v.lower()) if isinstance(v, str) else ""

        # 정확 일치 1차
        for g in esm_groups:
            if not g.get("recommendedOptNo"):
                continue
            name = g.get("recommendedOptName") or {}
            for key in ("kor", "eng", "korEng"):
                if _norm(name.get(key)) == target:
                    return g
        # 부분 포함 2차
        for g in esm_groups:
            if not g.get("recommendedOptNo"):
                continue
            name = g.get("recommendedOptName") or {}
            for key in ("kor", "eng", "korEng"):
                v = _norm(name.get(key))
                if v and (target in v or v in target):
                    return g
        return None

    @staticmethod
    def match_option_value(
        samba_text: str,
        esm_values: list[dict[str, Any]],
        fuzzy_threshold: float = 0.92,
    ) -> int | None:
        """samba 자유 텍스트 옵션값 → ESM recommendedOptValueNo 매칭.

        매칭 우선순위 (대소문자/공백 무시):
          1. kor / eng / korEng 정확 일치
          2. 부분 포함 (samba ⊆ ESM 또는 ESM ⊆ samba)
          3. difflib SequenceMatcher 비율 >= fuzzy_threshold (default 0.85)
        없으면 None.

        Args:
          samba_text: samba 옵션값 (예: "네이비", "GREEN", "Navy", "검정")
          esm_values: get_recommended_opt_values() 응답 list.
                      recommendedOptValueNo=0 placeholder 는 사전 제외 권장.
          fuzzy_threshold: 0.0~1.0. 낮을수록 관대 (오매칭 가능), 높을수록 엄격.
        """
        if not samba_text or not esm_values:
            return None
        target = re.sub(r"\s+", "", samba_text.lower())
        if not target:
            return None

        def _norm(value: Any) -> str:
            if not isinstance(value, str):
                return ""
            return re.sub(r"\s+", "", value.lower())

        # 1차: 정확 일치 (kor / eng / korEng)
        for v in esm_values:
            no = v.get("recommendedOptValueNo")
            if not no:
                continue
            name = v.get("recommendedOptValueName") or {}
            for key in ("kor", "eng", "korEng"):
                if _norm(name.get(key)) == target:
                    return int(no)

        # 2차: 부분 포함 — samba ⊆ ESM 방향만 (len>=2).
        # ESM ⊆ samba 방향은 제거: 1~2글자 ESM 사이즈값("L")이 임의 텍스트
        # ("Pearl Ribbon Keyring")의 부분문자열로 오매칭되는 손상 차단(#368 ④).
        if len(target) >= 2:
            for v in esm_values:
                no = v.get("recommendedOptValueNo")
                if not no:
                    continue
                name = v.get("recommendedOptValueName") or {}
                for key in ("kor", "eng", "korEng"):
                    normalized = _norm(name.get(key))
                    if not normalized:
                        continue
                    if target in normalized:
                        return int(no)

        # 3차: 유사도 매칭 (difflib SequenceMatcher) — threshold 이상 중 최대값.
        # 한글/영어 동시 비교. ESM 값 1,000건+ 일 수도 — O(n) 비교지만 한 번/그룹 호출.
        from difflib import SequenceMatcher

        best_no: int | None = None
        best_ratio = fuzzy_threshold
        for v in esm_values:
            no = v.get("recommendedOptValueNo")
            if not no:
                continue
            name = v.get("recommendedOptValueName") or {}
            for key in ("kor", "eng", "korEng"):
                normalized = _norm(name.get(key))
                if not normalized:
                    continue
                ratio = SequenceMatcher(None, target, normalized).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_no = int(no)
        return best_no

    # ------------------------------------------------------------------
    # 카테고리 트리 전체 수집
    # ------------------------------------------------------------------

    async def fetch_category_tree(
        self,
        delay: float = 0.5,
        exclude_global: bool = True,
    ) -> dict[str, str]:
        """전체 카테고리 트리를 수집하여 {이름경로: 코드} 딕셔너리 반환.

        Args:
          delay: API 호출 간 대기 시간 (초)
          exclude_global: 글로벌/해외 카테고리 제외 여부

        Returns:
          {"남성의류 > 니트 > 풀오버니트": "13290100", ...}
        """
        import asyncio as _aio

        global_keywords = ("글로벌", "Global", "global", "해외", "G로켓", "수출")
        result: dict[str, str] = {}
        api_calls = 0

        async def _walk(parent_code: str, path_prefix: str, depth: int = 0) -> None:
            nonlocal api_calls
            if depth > 5:
                return

            try:
                data = await self._call_api(
                    "GET", f"/item/v1/categories/site-cats/{parent_code}"
                )
                api_calls += 1
            except Exception as e:
                logger.warning(f"[ESM] 카테고리 조회 실패: {parent_code} — {e}")
                return

            subs = data.get("subCats", [])
            for cat in subs:
                name = cat.get("catName", "")
                code = cat.get("catCode", "")
                is_leaf = cat.get("isLeaf", False)

                if exclude_global and any(kw in name for kw in global_keywords):
                    continue

                cat_path = f"{path_prefix} > {name}" if path_prefix else name

                if is_leaf:
                    result[cat_path] = code
                else:
                    await _aio.sleep(delay)
                    await _walk(code, cat_path, depth + 1)

        # 대분류 조회
        try:
            top_data = await self._call_api("GET", "/item/v1/categories/site-cats")
            api_calls += 1
        except Exception as e:
            logger.error(f"[ESM] 대분류 조회 실패: {e}")
            return result

        top_cats = (
            top_data if isinstance(top_data, list) else top_data.get("subCats", [])
        )

        for cat in top_cats:
            name = cat.get("catName", "")
            code = cat.get("catCode", "")

            if exclude_global and any(kw in name for kw in global_keywords):
                continue

            if cat.get("isLeaf", False):
                result[name] = code
            else:
                import asyncio as _aio

                await _aio.sleep(delay)
                await _walk(code, name)

        label = self.cfg["label"]
        logger.info(
            f"[{label}] 카테고리 트리 수집 완료: {len(result)}개 leaf, API {api_calls}회 호출"
        )
        return result

    # ------------------------------------------------------------------
    # 상품 목록 조회
    # ------------------------------------------------------------------

    async def search_products(self, params: dict[str, Any]) -> dict[str, Any]:
        """상품 목록 조회 — POST /item/v1/goods/search (분당 30회 제한)"""
        return await self._call_api("POST", "/item/v1/goods/search", data=params)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 주문 / 배송 API (path prefix: /shipping/v1/)
    # ------------------------------------------------------------------
    # 응답 키가 PascalCase ('ResultCode', 'Data'). _call_api 는 'resultCode'
    # camelCase 만 검사 — 주문 API 호출 시 응답 직접 사용 + ci_get 권장.
    # rate limit: 5초당 1회 (주문번호 직접 조회 제외) — 토큰버킷 30/min 으로 충분히 안전.
    # ------------------------------------------------------------------

    async def search_orders(self, params: dict[str, Any]) -> dict[str, Any]:
        """주문 조회 — POST /shipping/v1/Order/RequestOrders.

        Required params:
          - siteType (int): 1=옥션, 2=G마켓
          - orderStatus (int): 0=주문번호, 1=결제완료, 2=배송준비, 3=배송중, 4=배송완료, 5=구매결정
          - requestDateFrom / requestDateTo (str YYYY-MM-DD): 기간 조회 시 필수
          - requestDateType (int): 1=주문일, 2=결제일, 3=발송마감일
          - orderNo (long): orderStatus=0 시 필수
        Optional: pageIndex, pageSize.
        조회 기간: G마켓 31일, 옥션 180일.
        rate limit: 5초당 1회 (orderStatus=0 직접 조회 제외) — `_esm_order_throttle()`로 보장.
        """
        await _esm_order_throttle()
        return await self._call_api(
            "POST", "/shipping/v1/Order/RequestOrders", data=params
        )

    async def confirm_order(
        self,
        order_no: int | str,
        seller_order_no: str | None = None,
        seller_item_no: str | None = None,
    ) -> dict[str, Any]:
        """주문확인 — POST /shipping/v1/Order/OrderCheck/{OrderNo}.

        주문확인 시 상태 '배송준비중' 으로 변경. 이후 취소는 판매자 승인 필요.
        """
        body: dict[str, Any] = {}
        if seller_order_no:
            body["SellerOrderNo"] = seller_order_no
        if seller_item_no:
            body["SellerItemNo"] = seller_item_no
        return await self._call_api(
            "POST", f"/shipping/v1/Order/OrderCheck/{order_no}", data=body
        )

    async def register_shipping(
        self,
        order_no: int | str,
        delivery_company_code: int,
        invoice_no: str,
        shipping_date: str,
        seller_order_no: str | None = None,
        seller_item_no: str | None = None,
    ) -> dict[str, Any]:
        """발송처리 (송장 입력) — POST /shipping/v1/Delivery/ShippingInfo.

        Args:
          order_no: 주문번호.
          delivery_company_code: deliveryCompCode (예: 10013 CJ택배).
          invoice_no: 송장번호.
          shipping_date: 발송일시 'YYYY-MM-DDThh:mm:ss'.
        """
        body: dict[str, Any] = {
            "OrderNo": int(order_no),
            "ShippingDate": shipping_date,
            "DeliveryCompanyCode": int(delivery_company_code),
            "InvoiceNo": invoice_no,
        }
        if seller_order_no:
            body["SellerOrderNo"] = seller_order_no
        if seller_item_no:
            body["SellerItemNo"] = seller_item_no
        return await self._call_api(
            "POST", "/shipping/v1/Delivery/ShippingInfo", data=body
        )

    async def get_order_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """주문 상태 조회 — POST /shipping/v1/Order/OrderStatus (조회기간 7일 이내).

        주요 응답: 주문상태 + 클레임 이력.
        """
        return await self._call_api(
            "POST", "/shipping/v1/Order/OrderStatus", data=params
        )

    # ------------------------------------------------------------------
    # 카탈로그 (catalogs): 브랜드/제조사/마니샵
    # 권한 OK (movestory1 검증): brands / makers
    # 권한 별도 (401): shop (마니샵)
    # ------------------------------------------------------------------

    async def search_brands(self, brand_name: str) -> dict[str, Any]:
        """브랜드 코드 조회 — GET /item/v1/catalogs/brands/{brandName}.

        상품 등록 시 brand 단순 string → ESM 브랜드 코드 매핑 시 사용.
        """
        return await self._call_api("GET", f"/item/v1/catalogs/brands/{brand_name}")

    async def search_makers(self, maker_name: str) -> dict[str, Any]:
        """제조사 코드 조회 — GET /item/v1/catalogs/makers/{makerName}."""
        return await self._call_api("GET", f"/item/v1/catalogs/makers/{maker_name}")

    async def get_mainshop_categories(self, shop_cat_code: str = "") -> dict[str, Any]:
        """마니샵 카테고리 조회 — GET /item/v1/catalogs/shop/{shopCatCode}.

        자체 쇼핑몰 매핑용. movestory1 권한 401 — 운영자 신청 후 사용.
        """
        path = "/item/v1/catalogs/shop"
        if shop_cat_code:
            path = f"{path}/{shop_cat_code}"
        return await self._call_api("GET", path)

    # ------------------------------------------------------------------
    # 안전인증 / 검색태그 (movestory1 권한 401 — 운영자 신청 후 사용)
    # ------------------------------------------------------------------

    async def get_safety_certs(self) -> dict[str, Any]:
        """안전인증 코드 조회 — GET /item/v1/catalogs/safety-certs.

        어린이/전기/생활/식품 등 인증 종류. 상품 등록 시 itemAddtionalInfo.safetyCerts 매핑.
        권한 별도 신청 필요.
        """
        return await self._call_api("GET", "/item/v1/catalogs/safety-certs")

    async def set_safety_certs(
        self, goods_no: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """안전인증 등록/수정 — POST /item/v1/goods/{goodsNo}/safety-certs.

        data: { safetyCerts: { child: "...", electric: "...", life: "...", food: "..." } }
        """
        return await self._call_api(
            "POST", f"/item/v1/goods/{goods_no}/safety-certs", data=data
        )

    async def set_search_tags(self, goods_no: str, tags: list[str]) -> dict[str, Any]:
        """검색태그 등록/수정 — POST /item/v1/goods/{goodsNo}/search-tags.

        상품 검색 노출 키워드. 권한 별도 신청 필요.
        """
        return await self._call_api(
            "POST",
            f"/item/v1/goods/{goods_no}/search-tags",
            data={"tags": tags},
        )

    # ------------------------------------------------------------------
    # 이벤트 홍보 (event-promotions)
    # ------------------------------------------------------------------

    async def create_event_promotion(self, data: dict[str, Any]) -> dict[str, Any]:
        """이벤트 홍보 등록 — POST /item/v1/event-promotions.

        data: { name, detail, isExposure, isApplyAll, exposureDate: {startDate, endDate} }
        Returns: { PromotionNo }
        """
        return await self._call_api("POST", "/item/v1/event-promotions", data=data)

    async def update_event_promotion(
        self, promotion_no: int | str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """이벤트 홍보 수정 — PUT /item/v1/event-promotions/{promotionNo}."""
        return await self._call_api(
            "PUT", f"/item/v1/event-promotions/{promotion_no}", data=data
        )

    async def get_event_promotion(self, promotion_no: int | str) -> dict[str, Any]:
        """이벤트 홍보 조회."""
        return await self._call_api("GET", f"/item/v1/event-promotions/{promotion_no}")

    async def delete_event_promotion(self, promotion_no: int | str) -> dict[str, Any]:
        """이벤트 홍보 삭제."""
        return await self._call_api(
            "DELETE", f"/item/v1/event-promotions/{promotion_no}"
        )

    async def add_event_promotion_goods(
        self, promotion_no: int | str, site_goods_nos: list[str]
    ) -> dict[str, Any]:
        """홍보에 상품 추가 — POST /item/v1/event-promotions/{promotionNo}/goods.

        최대 1,000 상품/홍보. G마켓 vs 옥션 separate.
        """
        return await self._call_api(
            "POST",
            f"/item/v1/event-promotions/{promotion_no}/goods",
            data={"siteGoodsNos": [str(x) for x in site_goods_nos]},
        )

    # ------------------------------------------------------------------
    # 고객혜택 (customer-benefit) — 상품 단위 할인/캐시백/광고
    # ------------------------------------------------------------------

    async def set_multiple_purchase_discount(
        self, goods_no: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """복수구매할인 등록/수정 — POST /item/v1/goods/{goodsNo}/customer-benefit/multiple-purchase-discount."""
        return await self._call_api(
            "POST",
            f"/item/v1/goods/{goods_no}/customer-benefit/multiple-purchase-discount",
            data=data,
        )

    async def set_special_discount(
        self, goods_no: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """특별할인 등록/수정 — POST .../customer-benefit/special-discount."""
        return await self._call_api(
            "POST",
            f"/item/v1/goods/{goods_no}/customer-benefit/special-discount",
            data=data,
        )

    async def set_cashback(self, goods_no: str, data: dict[str, Any]) -> dict[str, Any]:
        """판매자 스마일캐시 등록/수정 — POST .../customer-benefit/cashback."""
        return await self._call_api(
            "POST",
            f"/item/v1/goods/{goods_no}/customer-benefit/cashback",
            data=data,
        )

    # ------------------------------------------------------------------
    # 판매자 할인 (seller-discounts) — 상품 단위 할인
    # ------------------------------------------------------------------

    async def set_seller_discounts(
        self, goods_no: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """판매자할인 등록/수정 — POST /item/v1/goods/{goodsNo}/seller-discounts."""
        return await self._call_api(
            "POST", f"/item/v1/goods/{goods_no}/seller-discounts", data=data
        )

    async def delete_seller_discounts(self, goods_no: str) -> dict[str, Any]:
        """판매자할인 해제."""
        return await self._call_api(
            "DELETE", f"/item/v1/goods/{goods_no}/seller-discounts"
        )

    # ------------------------------------------------------------------
    # 그룹 상품 관리 (groups)
    # ------------------------------------------------------------------

    async def create_group(self, data: dict[str, Any]) -> dict[str, Any]:
        """그룹 생성 — POST /item/v1/groups."""
        return await self._call_api("POST", "/item/v1/groups", data=data)

    async def update_group(
        self, group_no: int | str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """그룹 수정 — PUT /item/v1/groups/{groupNo}."""
        return await self._call_api("PUT", f"/item/v1/groups/{group_no}", data=data)

    async def delete_group(self, group_no: int | str) -> dict[str, Any]:
        """그룹 삭제."""
        return await self._call_api("DELETE", f"/item/v1/groups/{group_no}")

    async def add_group_goods(
        self, group_no: int | str, site_goods_nos: list[str]
    ) -> dict[str, Any]:
        """그룹에 상품 등록 — POST /item/v1/groups/{groupNo}/goods."""
        return await self._call_api(
            "POST",
            f"/item/v1/groups/{group_no}/goods",
            data={"siteGoodsNos": [str(x) for x in site_goods_nos]},
        )

    # ------------------------------------------------------------------
    # 정산조회 (account/v1/settle/...)
    # SiteType: 'A'(옥션) 또는 'G'(G마켓) — 문자열.
    # 환불된 주문은 반대 부호.
    # ------------------------------------------------------------------

    async def search_settle_orders(self, params: dict[str, Any]) -> dict[str, Any]:
        """판매대금 정산조회 — POST /account/v1/settle/getsettleorder.

        params:
          - SiteType: 'A'/'G'
          - SrchType: D1~D10 (입금확인일/배송일/송금일 등)
          - SrchStartDate, SrchEndDate: YYYY-MM-DD
          - PageNo, PageRowCnt
        """
        return await self._call_api(
            "POST", "/account/v1/settle/getsettleorder", data=params
        )

    async def search_settle_delivery_fees(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """배송비 정산조회 — POST /account/v1/settle/getsettledeliveryfee."""
        return await self._call_api(
            "POST", "/account/v1/settle/getsettledeliveryfee", data=params
        )

    # ------------------------------------------------------------------
    # 클레임 (claim/v1/...): 취소/반품/교환/미수령
    # 응답 schema: PascalCase (ResultCode, Data: list).
    # ------------------------------------------------------------------

    async def search_cancels(self, params: dict[str, Any]) -> dict[str, Any]:
        """취소 조회 — POST /claim/v1/sa/Cancels.

        params:
          - SiteType: 1=옥션, 3=G마켓
          - CancelStatus: 0(전체)~6
          - Type: 0=주문번호, 1=장바구니, 2=신청일, 3=완료일, 4=결제일
          - StartDate / EndDate: 7일 이내 범위
        """
        return await self._call_api("POST", "/claim/v1/sa/Cancels", data=params)

    async def approve_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        """취소승인 — POST /claim/v1/sa/Cancels/Approval."""
        return await self._call_api(
            "POST", "/claim/v1/sa/Cancels/Approval", data=params
        )

    async def approve_cancel_by_orderno(
        self, order_no: str, site_type: int
    ) -> dict[str, Any]:
        """취소승인 (단건) — PUT /claim/v1/sa/Cancel/{OrderNo}.

        ESM Trading API 공식 문서. site_type: 1=옥션, 2=G마켓.
        성공: ResultCode == 0. 옥션 8668+BizRuleCode W8-2 = 이미 취소승인.
        이미 발송된 주문은 발송처리 API로 처리 → 자동 취소거부됨 (별도 거부 API 없음).
        """
        return await self._call_api(
            "PUT", f"/claim/v1/sa/Cancel/{order_no}", data={"SiteType": site_type}
        )

    async def seller_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        """판매취소 (품절 등) — POST /claim/v1/sa/Cancels/SellerCancel."""
        return await self._call_api(
            "POST", "/claim/v1/sa/Cancels/SellerCancel", data=params
        )

    async def search_exchanges(self, params: dict[str, Any]) -> dict[str, Any]:
        """교환 조회 — POST /claim/v1/sa/Exchanges."""
        return await self._call_api("POST", "/claim/v1/sa/Exchanges", data=params)

    async def search_non_receipts(self, params: dict[str, Any]) -> dict[str, Any]:
        """미수령 신고 조회 — POST /claim/v1/sa/NonReceipts."""
        return await self._call_api("POST", "/claim/v1/sa/NonReceipts", data=params)

    # ------------------------------------------------------------------
    # CS / 판매자 문의 (item/v1/communications/...)
    # ------------------------------------------------------------------

    async def search_customer_inquiries(self, params: dict[str, Any]) -> dict[str, Any]:
        """판매자 문의 조회 — POST /item/v1/communications/customer/bulletin-board.

        Required params:
          - qnaType (int): 1=옥션 일반, 2=옥션 비밀글, 3=G마켓 전체
          - status (int): 1=전체, 2=미처리, 3=처리완료, 4=처리중(G마켓), 5=중복
          - type (int): **서버 필수** — 누락 시 500 "Error getting value from 'Type'".
                         공식 문서에는 누락되어 있으나 raw probe로 확인. 1 사용 검증됨.
          - startDate / endDate (YYYY-MM-DD): 7일 단위
        """
        if "type" not in params:
            params = {**params, "type": 1}
        return await self._call_api(
            "POST",
            "/item/v1/communications/customer/bulletin-board",
            data=params,
        )

    async def answer_customer_inquiry(
        self,
        message_no: str,
        token: str,
        title: str,
        comments: str,
        answer_status: int = 2,
    ) -> dict[str, Any]:
        """판매자 문의 답변 — POST .../bulletin-board/qna.

        answer_status: 1=처리중, 2=처리완료.
        comments 1000byte 이내.
        SSG.COM 제휴 문의는 답변 후 수정 불가 — 운영자 주의.
        """
        body = {
            "messageNo": message_no,
            "token": token,
            "answerStatus": int(answer_status),
            "title": title[:200],
            "comments": comments,  # 1000byte 이내 호출자 보장
        }
        return await self._call_api(
            "POST",
            "/item/v1/communications/customer/bulletin-board/qna",
            data=body,
        )

    async def search_urgent_alerts(self, params: dict[str, Any]) -> dict[str, Any]:
        """긴급알리미 조회 — ESM 측 CS 긴급 요청 사항.

        정확한 경로는 etapi.gmarket.com 문서 기준
        `/assist/v1/Selling/GetEmergencyInformList`. 기존 `/item/v1/...` 경로는
        401 응답이며 권한 이슈로 오인되기 쉬워서 정정.
        """
        return await self._call_api(
            "POST",
            "/assist/v1/Selling/GetEmergencyInformList",
            data=params,
        )

    # ------------------------------------------------------------------
    # 데이터 변환 — 상품 dict → ESM Plus API 포맷
    # ------------------------------------------------------------------

    @staticmethod
    def transform_product(
        product: dict[str, Any],
        category_id: str,
        site: str = "gmarket",
    ) -> dict[str, Any]:
        """수집 상품 데이터를 ESM Plus 등록 API 포맷으로 변환.

        Args:
          product: 삼바웨이브 표준 상품 dict
          category_id: ESM Plus 최하위 카테고리 코드
          site: "gmarket" or "auction"
        """
        cfg = ESMPlusClient.SITE_CONFIG[site]
        site_type = cfg["siteType"]
        site_key = cfg["siteKey"]

        # 상품명 (100바이트 제한)
        market_names = product.get("market_names") or {}
        name = (
            market_names.get(cfg["label"])
            or market_names.get("G마켓")
            or market_names.get("옥션")
            or product.get("name", "")
        )
        # 100바이트 제한 — 한글 3바이트 계산
        encoded = name.encode("utf-8")
        if len(encoded) > 100:
            while len(name.encode("utf-8")) > 97:
                name = name[:-1]
            name = name.rstrip() + "..."

        # 가격
        sale_price = int(product.get("sale_price", 0) or 0)
        # 100원 단위 내림
        if sale_price % 100 != 0:
            sale_price = (sale_price // 100) * 100
        if sale_price < 10:
            sale_price = 10

        # 재고
        stock = int(
            product.get("_stock_quantity", 0) or product.get("stock_quantity", 0) or 99
        )
        max_stock = product.get("_max_stock")
        if max_stock:
            stock = min(stock, int(max_stock))
        stock = max(1, min(stock, 99999))

        # 이미지
        images = product.get("images") or []
        basic_img = images[0] if images else ""
        # 프로토콜 보정
        if basic_img and basic_img.startswith("//"):
            basic_img = f"https:{basic_img}"

        image_model: dict[str, Any] = {}
        if basic_img:
            image_model["BasicImage"] = {"URL": basic_img}
        for i, img_url in enumerate(images[1:15], start=1):
            if img_url.startswith("//"):
                img_url = f"https:{img_url}"
            image_model[f"AdditionalImage{i}"] = {"URL": img_url}

        # 상세 HTML
        detail_html = product.get("detail_html", "") or ""
        # 프로토콜 보정
        if detail_html:
            detail_html = re.sub(r'(src=["\'])\/\/', r"\1https://", detail_html)

        # 배송 정보
        delivery_fee_type = product.get("_delivery_fee_type", "FREE")
        delivery_base_fee = int(product.get("_delivery_base_fee", 0) or 0)
        shipping_type = 1  # 택배
        # 계정 설정에서 택배사/발송정책 가져오기
        company_no = int(product.get("_shipping_company_no", 0) or 0)
        dispatch_policy_no = int(product.get("_dispatch_policy_no", 0) or 0)
        place_no = int(product.get("_shipping_place_no", 0) or 0)
        # 반품/교환지 — ESM은 placeNo 가 아니라 addrNo 를 요구. 플러그인 execute 가
        # get_places 로 placeNo→addrNo 해석 후 _return_addr_no 주입 (#389)
        return_addr_no = int(product.get("_return_addr_no", 0) or 0)
        return_fee = int(product.get("_return_fee", 0) or 0)

        # 배송비 분류 (ESM API 가이드 etapi.gmarket.com/140 + 실 호출 검증):
        # - shipping.policy.feeType: 1=묶음(bundle 필수), 2=개별(each 필수)
        # - shipping.policy.each.feeType: 1=무료, 2=유료(fee 필수), 3=조건부
        # 묶음배송비 정책(bundle) 은 셀러 ESMplus 측 별도 권한 필요 — 일반 케이스는 개별(2) 사용.
        _EACH_FEE_TYPE_MAP = {"FREE": 1, "PAID": 2, "CONDITIONAL": 3}
        each_fee_type = _EACH_FEE_TYPE_MAP.get(delivery_fee_type.upper(), 1)

        shipping: dict[str, Any] = {
            "type": shipping_type,
        }
        if company_no:
            shipping["companyNo"] = company_no
        # 발송정책 번호 — ESM API 가 SiteInfoModel<Int64> 형태(사이트별 dict) 요구
        if dispatch_policy_no:
            shipping["dispatchPolicyNo"] = {site_key: dispatch_policy_no}

        # shipping.policy — 개별(each) 배송비 사용 (묶음 미사용 가정)
        policy_obj: dict[str, Any] = {"feeType": 2}
        if place_no:
            policy_obj["placeNo"] = place_no
        each_obj: dict[str, Any] = {"feeType": each_fee_type}
        if each_fee_type == 2 and delivery_base_fee > 0:
            each_obj["fee"] = delivery_base_fee
        policy_obj["each"] = each_obj
        shipping["policy"] = policy_obj

        # 반품/교환지 — ESM 공식 스펙: shipping.returnAndExchange.addrNo (#389).
        # policy.returnPlaceNo 는 ESM 미인식 필드라 무시됨 → addrNo 별도 객체로 전달.
        if return_addr_no:
            return_obj: dict[str, Any] = {"addrNo": return_addr_no}
            if return_fee > 0:
                return_obj["fee"] = return_fee
            shipping["returnAndExchange"] = return_obj

        # 판매기간 (-1=무제한)
        selling_period = int(product.get("_selling_period", -1) or -1)

        # 카테고리
        category_site = [{"siteType": site_type, "catCode": str(category_id)}]

        # 옵션 처리
        options_raw = product.get("options") or []
        option_type = 0  # 기본 미사용
        option_list: list[dict[str, Any]] = []

        if options_raw:
            option_type = 1  # 선택형 옵션
            for opt in options_raw:
                opt_name = opt.get("name", "") or opt.get("option_name", "")
                opt_values = opt.get("values") or opt.get("option_values") or []
                if isinstance(opt_values, str):
                    opt_values = [v.strip() for v in opt_values.split(",") if v.strip()]

                items: list[dict[str, Any]] = []
                for val in opt_values:
                    if isinstance(val, dict):
                        val_name = val.get("name", "") or val.get("value", "")
                        val_price = int(
                            val.get("priceAdjust", 0) or val.get("price_adjust", 0) or 0
                        )
                        val_stock = int(val.get("stock", stock) or stock)
                        val_sold_out = val.get("isSoldOut", False) or val.get(
                            "is_sold_out", False
                        )
                    else:
                        val_name = str(val)
                        val_price = 0
                        val_stock = stock
                        val_sold_out = False

                    items.append(
                        {
                            "optionValue": val_name,
                            "addPrice": val_price,
                            "stockQty": val_stock if not val_sold_out else 0,
                        }
                    )

                if items:
                    option_list.append(
                        {
                            "optionName": opt_name,
                            "optionValues": items,
                        }
                    )

        # 고시정보
        group = detect_notice_group(product)
        official_notice_no = _get_esm_notice_no(group)

        # 브랜드/제조사
        brand = product.get("brand", "")
        manufacturer = product.get("manufacturer", "") or brand

        # 원산지 (기본: 해외 → 상세설명 참조)
        origin = product.get("origin", "")

        # AS 전화번호
        as_phone = product.get("_as_phone", "")

        # API 데이터 구성
        # 주의: ESM Plus API 스펙상 필드명이 "itemAddtionalInfo" (오타 아님)
        # 등록/수정 API: PascalCase 키 (Gmkt, Iac)
        # sell-status API: camelCase 키 (gmkt, iac) — 스펙상 의도적 차이
        # 성인상품/면세 여부 — ESM API 등록 필수 필드 (대부분 False)
        is_adult_product = bool(product.get("is_adult_product", False))
        is_vat_free = bool(product.get("is_vat_free", False))

        data: dict[str, Any] = {
            "itemBasicInfo": {
                "goodsName": {
                    "kor": name,
                },
                "category": {
                    "site": category_site,
                },
                "brand": brand,
                "manufacturer": manufacturer,
            },
            "itemAddtionalInfo": {
                "price": {site_key: sale_price},
                "stock": {site_key: stock},
                "sellingPeriod": {site_key: selling_period},
                # 판매여부 — ESM 서버 필수. 누락 시 G마켓 등록 reject "판매 여부(isSell)를 입력해주세요"
                "isSell": {site_key: 1},
                "shipping": shipping,
                "images": {
                    "basicImgURL": basic_img,
                },
                # 상세 — type 2(HTML) 명시. 1=ContentsId, 2=HTML
                "descriptions": {
                    "kor": {
                        "type": 2,
                        "html": detail_html,
                    },
                },
                # 추천 옵션 — RecommendedOptI 단일 객체 필수.
                # type 0 = 추천옵션 미사용 (옵션 없는 상품). type 1+ = 별도 API
                # (POST /item/v1/{goodsNo}/recommended-options) 로 등록.
                "recommendedOpts": {"type": 0},
                "isAdultProduct": is_adult_product,
                "isVatFree": is_vat_free,
            },
        }

        # 추가 이미지 (이미지 모델은 등록 후 별도 API로 설정)
        if len(images) > 1:
            data["_pending_images"] = image_model

        # 옵션
        if option_list:
            data["itemAddtionalInfo"]["optionType"] = option_type
            data["itemAddtionalInfo"]["options"] = option_list

        # 고시정보 — 그룹 번호 + details (등록 필수 필드).
        # 응답 키: GET .../groups/{no}/codes 의 `codes` 리스트.
        # 요청 키: details 리스트 (POST /item/v1/goods 본문).
        # isExtraMark=true 항목 모두 채워야 ESM 등록 검증 통과.
        if official_notice_no:
            notice_items = _build_esm_notice_items(official_notice_no, product)
            notice_payload: dict[str, Any] = {
                "officialNoticeNo": official_notice_no,
            }
            if notice_items:
                notice_payload["details"] = notice_items
            data["itemAddtionalInfo"]["officialNotice"] = notice_payload

        # 원산지
        if origin:
            data["itemBasicInfo"]["origin"] = origin

        # AS 전화번호
        if as_phone:
            data["itemAddtionalInfo"]["asPhone"] = as_phone

        # 관리코드 (소싱처 상품 ID)
        source_product_id = product.get("source_product_id", "")
        if source_product_id:
            data["itemBasicInfo"]["managedCode"] = str(source_product_id)[:50]

        return data


# ------------------------------------------------------------------
# 고시정보 번호 매핑 (ESM Plus 전용)
# ------------------------------------------------------------------

# ESM Plus 고시정보 그룹 번호 (officialNoticeNo)
# 검증 출처: GET /item/v1/official-notice/groups (2026-05-15 movestory1 응답)
_ESM_NOTICE_MAP: dict[str, int] = {
    "wear": 1,  # 의류
    "shoes": 2,  # 구두/신발
    "bag": 3,  # 가방
    "accessories": 4,  # 패션잡화(모자/벨트/액세서리 등)
    "cosmetic": 18,  # 화장품
    "food": 20,  # 농수축산물 (가공식품은 21, 건강기능식품은 22)
    "electronics": 12,  # 소형전자 (휴대형 통신기기 13, 가정용 전기제품 8 등 세분 가능)
    "sports": 25,  # 스포츠 용품
    "etc": 35,  # 기타 재화
}


def _get_esm_notice_no(group: str) -> int:
    """고시정보 그룹 → ESM Plus 고시정보 번호."""
    return _ESM_NOTICE_MAP.get(group, 35)


# 그룹별 고시정보 항목 코드 매핑 (group → list[(itemelementCode, source_field)])
# 검증 출처: GET /item/v1/official-notice/groups/{no}/codes
# isExtraMark=true 항목 모두 채워야 ESM 등록 검증 통과. fallback="[상세설명참조]" 안전.
_ESM_NOTICE_ITEMS: dict[int, list[tuple[str, str]]] = {
    1: [  # 의류
        ("1-1", "material"),  # 제품소재
        ("1-2", "color"),  # 색상
        ("1-3", ""),  # 치수 (옵션/상세설명 참조)
        ("1-4", "manufacturer"),  # 제조자/수입자
        ("1-5", "origin"),  # 제조국
        ("1-6", "care_instructions"),  # 세탁방법
        ("1-7", ""),  # 제조연월
        ("1-8", "quality_guarantee"),  # 품질보증기준
        ("1-9", "_as_phone"),  # A/S 책임자/전화
        ("1-10", ""),  # 주문후 예상 배송기간
    ],
    2: [  # 구두/신발
        ("2-1", "material"),  # 제품의 주소재
        ("2-2", "color"),  # 색상
        ("2-3", ""),  # 치수
        ("2-4", "manufacturer"),  # 제조자/수입자
        ("2-5", "origin"),  # 제조국
        ("2-6", "care_instructions"),  # 취급시 주의사항
        ("2-7", "quality_guarantee"),  # 품질보증기준
        ("2-8", "_as_phone"),  # A/S
        ("2-9", ""),  # 배송기간
    ],
    3: [  # 가방
        ("3-1", ""),  # 종류
        ("3-2", "material"),  # 소재
        ("3-3", "color"),  # 색상
        ("3-4", ""),  # 크기
        ("3-5", "manufacturer"),  # 제조자/수입자
        ("3-6", "origin"),  # 제조국
        ("3-7", "care_instructions"),  # 취급시 주의사항
        ("3-8", "quality_guarantee"),  # 품질보증기준
        ("3-9", "_as_phone"),  # A/S
        ("3-10", ""),  # 배송기간
    ],
    4: [  # 패션잡화 (모자/벨트/액세서리 등)
        ("4-1", ""),  # 종류
        ("4-2", "material"),  # 소재
        ("4-3", ""),  # 치수
        ("4-4", "manufacturer"),  # 제조자/수입자
        ("4-5", "origin"),  # 제조국
        ("4-6", "care_instructions"),  # 취급시 주의사항
        ("4-7", "quality_guarantee"),  # 품질보증기준
        ("4-8", "_as_phone"),  # A/S
        ("4-9", ""),  # 배송기간
    ],
    35: [  # 기타 재화 — ESM 필수(isExtraMark=true) 항목 35-1~35-6 (항목명은 API 미제공).
        # 검증: GET /item/v1/official-notice/groups/35/codes (2026-05-24, 가디 응답)
        # 999-5 는 isExtraMark=false(비필수)라 단독 전송 시 "35-1 미입력" 오류 발생.
        # source_key 미지정 → 전부 fallback("[상세설명참조]")로 검증 통과.
        ("35-1", ""),
        ("35-2", ""),
        ("35-3", ""),
        ("35-4", ""),
        ("35-5", ""),
        ("35-6", ""),
    ],
}


def _build_esm_notice_items(
    group_no: int, product: dict[str, Any]
) -> list[dict[str, str]]:
    """ESM 고시정보 그룹에 따른 itemelement 리스트 생성.

    isExtraMark=true(필수) 항목 모두 채움. 값 없으면 fallback("[상세설명참조]") 으로
    검증 통과만 보장. 운영자는 실제 정확한 값 입력 권장.
    """
    fallback = "[상세설명참조]"
    fields = _ESM_NOTICE_ITEMS.get(group_no)
    if not fields:
        # 미정의 그룹 — ESM 의 official notice codes endpoint 호출 후 동적 매핑 가능.
        # 일단 빈 리스트 반환 (ESM 측에서 일부 그룹은 필수 필드 없을 수 있음).
        return []
    items: list[dict[str, str]] = []
    for code, source_key in fields:
        raw = (product.get(source_key) if source_key else "") or ""
        val = str(raw).strip() or fallback
        # ESM 고시 항목당 1,000byte 제한 (#368 ③) — 초과 시 등록 자체 거부.
        # 취급주의사항(세탁/관리)이 3,000byte+ 인 브랜드(나이키 등) 대응.
        encoded = val.encode("utf-8")
        if len(encoded) > 1000:
            val = encoded[:1000].decode("utf-8", "ignore")
        items.append({"officialNoticeItemelementCode": code, "value": val})
    return items


# ------------------------------------------------------------------
# 카테고리 매핑 캐시 및 조회
# ------------------------------------------------------------------

# 메모리 캐시 — 서버 기동 시 JSON 파일에서 로드
_cat_cache: dict[str, dict[str, str]] = {}


def _load_cat_mapping(name: str) -> dict[str, str]:
    """카테고리 매핑 JSON 파일 로드 (캐시 적용)."""
    if name in _cat_cache:
        return _cat_cache[name]

    import json
    from pathlib import Path

    mapping_dir = Path(__file__).resolve().parent.parent / "category"
    filepath = mapping_dir / f"esm_{name}.json"

    if not filepath.exists():
        logger.warning(f"[ESM] 카테고리 매핑 파일 없음: {filepath}")
        return {}

    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)
    _cat_cache[name] = data
    logger.info(f"[ESM] 카테고리 매핑 로드: {name} ({len(data)}개)")
    return data


def esm_map_category(cat_code: str, from_site: str, to_site: str) -> str:
    """옥션↔지마켓 카테고리 코드 변환.

    Args:
      cat_code: 원본 카테고리 코드
      from_site: "auction" or "gmarket"
      to_site: "auction" or "gmarket"

    Returns:
      변환된 카테고리 코드 (매핑 없으면 빈 문자열)
    """
    if from_site == to_site:
        return cat_code

    if from_site == "auction" and to_site == "gmarket":
        mapping = _load_cat_mapping("auction_to_gmarket")
    elif from_site == "gmarket" and to_site == "auction":
        mapping = _load_cat_mapping("gmarket_to_auction")
    else:
        return ""

    return mapping.get(cat_code, "")


def esm_find_category_by_path(path: str, site: str) -> str:
    """이름경로로 카테고리 코드 조회.

    Args:
      path: "남성의류 > 니트 > 풀오버니트"
      site: "auction" or "gmarket"

    Returns:
      카테고리 코드 (없으면 빈 문자열)
    """
    tree_name = "auction_cats" if site == "auction" else "gmarket_cats"
    tree = _load_cat_mapping(tree_name)
    return tree.get(path, "")


# ------------------------------------------------------------------
# 인증 정보 resolve 헬퍼
# ------------------------------------------------------------------


async def resolve_esm_credentials(
    session: Any,
    account: Any = None,
) -> tuple[str, str]:
    """ESM 인증 정보 조회 — 다단계 우선순위:

    1. account.additional_fields.esmHostingId/esmSecretKey (계정별 다중 hosting 지원)
    2. samba_settings.esm_credentials = {hosting_id, secret_key} (단일 hosting 다계정)
    3. env (settings.esmplus_hosting_id/esmplus_secret_key) — 1단계 (movestory1 검증)

    Returns:
      (hosting_id, secret_key). 모두 빈 문자열이면 미설정.
    """
    # 1) account.additional_fields
    if account is not None:
        extras = getattr(account, "additional_fields", None) or {}
        h = (extras.get("esmHostingId") or "").strip()
        s = (extras.get("esmSecretKey") or "").strip()
        if h and s:
            return h, s

    # 2) samba_settings.esm_credentials
    if session is not None:
        try:
            from backend.api.v1.routers.samba.proxy._helpers import _get_setting

            creds = await _get_setting(session, "esm_credentials") or {}
            if isinstance(creds, dict):
                h = (creds.get("hosting_id") or "").strip()
                s = (creds.get("secret_key") or "").strip()
                if h and s:
                    return h, s
        except Exception as exc:
            logger.debug(f"[ESM] settings esm_credentials 조회 실패: {exc}")

    # 3) env (settings.esmplus_*)
    try:
        from backend.core.config import settings

        return (
            (settings.esmplus_hosting_id or "").strip(),
            (settings.esmplus_secret_key or "").strip(),
        )
    except Exception:
        return "", ""


# ------------------------------------------------------------------
# samba options → ESM 추천옵션 흐름 헬퍼 (plugins 공유)
# ------------------------------------------------------------------


def _clamp_manage_code(natural: str, index_code: str) -> str:
    """ESM 옵션 관리코드 20byte 제한 (#368 ⑤).

    한글 조합형/직접입력은 자연코드가 20byte 초과 → ESM "옵션 관리코드는
    20byte를 초과" 등록거부. 초과 시 짧고 유일한 인덱스코드로 대체.
    """
    if len(natural.encode("utf-8")) <= 20:
        return natural
    return index_code


async def _build_independent(
    client: ESMPlusClient,
    cat_code: str,
    samba_opt: dict[str, Any],
    site_key: str,
    stock_per_value: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """type=1 선택형 payload.independent 생성. (combination 은 None)"""
    name, values = _normalize_samba_option(samba_opt)
    if not name or not values:
        return None, None
    group, pool = await _resolve_esm_group(client, cat_code, name)
    if not group:
        logger.warning(f"[ESM] 옵션 그룹 매칭 실패: samba='{name}' cat={cat_code}")
        return None, None

    rec_opt_no = group["recommendedOptNo"]
    details: list[dict[str, Any]] = []
    for i, v in enumerate(values):
        qty = stock_per_value if v["qty"] <= 0 else v["qty"]
        rec_val_no = ESMPlusClient.match_option_value(v["text"], pool)
        if rec_val_no:
            details.append(
                {
                    "recommendedOptValueNo": rec_val_no,
                    "addAmnt": v["add_amnt"],
                    "qty": {site_key: 0 if v["sold_out"] else qty},
                    "isSoldOut": v["sold_out"],
                    "isDisplay": True,
                    "manageCode": _clamp_manage_code(f"OPT{rec_val_no}", f"OPTF{i}"),
                }
            )
            continue
        # 직접입력 fallback — recommendedOptValueNo=0 + koreanText 자유 텍스트.
        # ESM 카테고리 권한에 따라 거부 가능 — 운영자 검증 후 활성화 권장.
        logger.warning(
            f"[ESM] 옵션값 매칭 실패 → 직접입력 fallback: cat={cat_code} group={rec_opt_no} text='{v['text']}'"
        )
        details.append(
            {
                "recommendedOptValueNo": 0,
                "recommendedOptValue": {"koreanText": v["text"][:50]},
                "addAmnt": v["add_amnt"],
                "qty": {site_key: 0 if v["sold_out"] else qty},
                "isSoldOut": v["sold_out"],
                "isDisplay": True,
                "manageCode": _clamp_manage_code(
                    f"OPT-FREE-{v['text'][:10]}", f"OPTF{i}"
                ),
            }
        )
    # recommendedOptValueNo 중복 제거 — 같은 optValueNo가 여러 번 등록되면 ESM 1000 에러
    seen_val_nos: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for d in details:
        val_no = d["recommendedOptValueNo"]
        if val_no == 0:
            dedup_key = f"0:{d.get('recommendedOptValue', {}).get('koreanText', '')}"
        else:
            dedup_key = str(val_no)
        if dedup_key in seen_val_nos:
            logger.warning(
                f"[ESM] 옵션값 중복 제거: recommendedOptValueNo={val_no} (key={dedup_key})"
            )
            continue
        seen_val_nos.add(dedup_key)
        deduped.append(d)
    return {
        "recommendedOptNo": rec_opt_no,
        "details": deduped,
        "_group_label": group.get("recommendedOptName"),
    }, None


async def _build_combination(
    client: ESMPlusClient,
    cat_code: str,
    samba_opts: list[dict[str, Any]],
    site_key: str,
    stock_per_value: int,
) -> tuple[dict[str, Any] | None, int, int, Any]:
    """type=2/3 조합형 payload.combination 생성. cartesian product.

    그룹 매칭 실패 축은 스킵 — 매칭된 축이 2개 이상이면 조합형 유지.
    매칭된 축이 1개면 None 반환 (caller 가 type=1 fallback 처리).
    """
    if len(samba_opts) < 2:
        return None, 0, 0, None
    logger.info(
        f"[ESM] 조합형 시작 cat={cat_code} "
        f"opts={[o.get('name') or o.get('option_name') for o in samba_opts]}"
    )
    # 각 축별 그룹/옵션값 매칭 — 그룹 미발견 축은 스킵 (전체 포기 X)
    axes: list[
        tuple[dict[str, Any], list[dict[str, Any]], list[int | None]]
    ] = []  # (group, samba_values_normalized, resolved_value_nos)
    for opt in samba_opts:
        name, samba_vals = _normalize_samba_option(opt)
        if not name or not samba_vals:
            logger.warning(f"[ESM] 조합형 축 스킵(빈값): name='{name}'")
            continue
        group, pool = await _resolve_esm_group(client, cat_code, name)
        if not group:
            logger.warning(
                f"[ESM] 조합형 축 스킵(그룹없음): samba='{name}' cat={cat_code}"
            )
            continue
        matched_count = sum(
            1 for v in samba_vals if ESMPlusClient.match_option_value(v["text"], pool)
        )
        logger.info(
            f"[ESM] 조합형 축 확정: '{name}' → optNo={group['recommendedOptNo']} "
            f"vals={len(samba_vals)}개 매칭={matched_count}개"
        )
        resolved = [
            ESMPlusClient.match_option_value(v["text"], pool) for v in samba_vals
        ]
        axes.append((group, samba_vals, resolved))

    if len(axes) < 2:
        logger.warning(
            f"[ESM] 조합형 포기 — 유효 축 {len(axes)}개 (최소 2개 필요) cat={cat_code}"
        )
        return None, 0, 0, None
    # 최대 3축
    axes = axes[:3]

    # cartesian product 생성 — 매칭 실패 값은 직접입력(recommendedOptValueNo=0) fallback 포함
    import itertools

    requested = 1
    for _, vs, _ in axes:
        requested *= len(vs)

    # _combo_stock_map: _split_multi_group_options가 첫 번째 그룹에 심어둔 조합별 재고 맵
    _combo_stock_map: dict[str, dict] = {}
    if samba_opts and isinstance(samba_opts[0], dict):
        _combo_stock_map = samba_opts[0].get("_combo_stock_map") or {}

    details: list[dict[str, Any]] = []
    # 매칭 여부와 무관하게 모든 인덱스 포함 (fallback 처리)
    indices_per_axis = [list(range(len(samba_vals))) for _, samba_vals, _ in axes]
    for ci, combo in enumerate(itertools.product(*indices_per_axis)):
        entry: dict[str, Any] = {
            "qty": {site_key: 0},
            "isSoldOut": False,
            "isDisplay": True,
            "manageCode": "",
            "addAmnt": 0,
        }
        manage_parts: list[str] = []
        any_sold_out = False
        first_axis_idx = combo[0]
        first_qty = axes[0][1][first_axis_idx]["qty"] or stock_per_value
        sum_add_amnt = 0
        for axis_idx, (group, samba_vals, resolved) in enumerate(axes):
            val_idx = combo[axis_idx]
            v = samba_vals[val_idx]
            rec_val_no = resolved[val_idx]
            if rec_val_no:
                entry[f"recommendedOptValueNo{axis_idx + 1}"] = rec_val_no
                manage_parts.append(str(rec_val_no))
            else:
                # 직접입력 fallback — ESM 카테고리 권한에 따라 거부 가능
                entry[f"recommendedOptValueNo{axis_idx + 1}"] = 0
                entry[f"recommendedOptValue{axis_idx + 1}"] = {
                    "koreanText": v["text"][:50]
                }
                logger.warning(
                    f"[ESM] 조합형 옵션값 매칭 실패 → 직접입력 fallback: "
                    f"cat={cat_code} axis={axis_idx + 1} text='{v['text']}'"
                )
                manage_parts.append(f"FREE-{v['text'][:10]}")
            if v["sold_out"]:
                any_sold_out = True
            sum_add_amnt += v["add_amnt"]
        # 조합별 재고 맵이 있으면 per-combination 재고 사용, 없으면 첫 축 재고로 fallback
        _combo_key = "/".join(axes[ai][1][combo[ai]]["text"] for ai in range(len(axes)))
        _combo_info = _combo_stock_map.get(_combo_key)
        if _combo_info is not None:
            _final_qty = int(_combo_info.get("stock") or 0) or stock_per_value
            _final_sold_out = bool(_combo_info.get("isSoldOut"))
        else:
            _final_qty = first_qty
            _final_sold_out = any_sold_out
        entry["qty"] = {site_key: 0 if _final_sold_out else _final_qty}
        entry["isSoldOut"] = _final_sold_out
        entry["addAmnt"] = sum_add_amnt
        entry["manageCode"] = _clamp_manage_code(
            "OPT" + "-".join(manage_parts), f"OPTC{ci}"
        )
        details.append(entry)

    # 조합형 중복 제거 — (optValueNo1, optValueNo2, ...) 튜플이 동일하면 ESM 1000 에러
    seen_combos: set[str] = set()
    deduped_combo: list[dict[str, Any]] = []
    for d in details:
        key_parts = []
        for ai in range(len(axes)):
            val_no = d.get(f"recommendedOptValueNo{ai + 1}", 0)
            if val_no == 0:
                free_text = d.get(f"recommendedOptValue{ai + 1}", {}).get(
                    "koreanText", ""
                )
                key_parts.append(f"0:{free_text}")
            else:
                key_parts.append(str(val_no))
        combo_key = "|".join(key_parts)
        if combo_key in seen_combos:
            logger.warning(f"[ESM] 조합형 옵션 중복 제거: key={combo_key}")
            continue
        seen_combos.add(combo_key)
        deduped_combo.append(d)
    details = deduped_combo

    if not details:
        return None, 0, requested, None

    combination_payload: dict[str, Any] = {"details": details, "_axis_count": len(axes)}
    for axis_idx, (group, _, _) in enumerate(axes):
        combination_payload[f"recommendedOptNo{axis_idx + 1}"] = group[
            "recommendedOptNo"
        ]
    group_label = [g.get("recommendedOptName") for g, _, _ in axes]
    return combination_payload, len(details), requested, group_label


def _normalize_samba_option(opt: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """samba option dict → (name, normalized_values list).

    각 value: {text, qty, add_amnt, sold_out}.
    """
    name = opt.get("name") or opt.get("option_name") or ""
    raw_values = opt.get("values") or opt.get("option_values") or []
    if isinstance(raw_values, str):
        raw_values = [v.strip() for v in raw_values.split(",") if v.strip()]

    def _to_int(v: Any) -> int | None:
        if v in (None, ""):
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    # 1차 패스: 절대 price 표현 감지 (#368 ②).
    # 일부 소싱처(마스마룰즈 등)는 추가금을 priceAdjust/addPrice 가 아니라
    # 옵션별 절대 price 로 표현 → base(min) 대비 차액을 add_amnt 로 환산.
    has_adjust = False
    abs_prices: list[int] = []
    for sv in raw_values:
        if isinstance(sv, dict):
            if sv.get("priceAdjust") or sv.get("addPrice"):
                has_adjust = True
            p = _to_int(sv.get("price"))
            if p is not None:
                abs_prices.append(p)
    base_price = min(abs_prices) if (abs_prices and not has_adjust) else None

    normalized: list[dict[str, Any]] = []
    for sv in raw_values:
        if isinstance(sv, dict):
            add_amnt = int(sv.get("priceAdjust", 0) or sv.get("addPrice", 0) or 0)
            if add_amnt == 0 and base_price is not None:
                p = _to_int(sv.get("price"))
                if p is not None:
                    add_amnt = max(0, p - base_price)
            normalized.append(
                {
                    "text": sv.get("name") or sv.get("value") or "",
                    "qty": int(sv.get("stock", 99) or 99),
                    "add_amnt": add_amnt,
                    "sold_out": bool(sv.get("isSoldOut") or sv.get("is_sold_out")),
                }
            )
        else:
            normalized.append(
                {"text": str(sv), "qty": 99, "add_amnt": 0, "sold_out": False}
            )
    return name, normalized


async def _resolve_esm_group(
    client: ESMPlusClient,
    cat_code: str,
    samba_opt_name: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """samba 옵션 이름 → ESM 그룹 + 옵션값 list (캐시 적용).

    이름 매칭 실패 시 사이즈 관련 키워드(사이즈/SIZE/치수/선택)로 재시도,
    그래도 없으면 첫 번째 유효 그룹(recommendedOptNo > 0) fallback.
    """
    groups = await client.get_recommended_opt_groups(cat_code)
    g = ESMPlusClient.detect_esm_option_group(samba_opt_name, groups)
    if not g:
        # 사이즈 관련 키워드로 재시도
        _size_keywords = ["사이즈", "size", "치수", "선택"]
        for kw in _size_keywords:
            g = ESMPlusClient.detect_esm_option_group(kw, groups)
            if g:
                logger.info(
                    f"[ESM] 옵션그룹 fallback: '{samba_opt_name}' → '{kw}' 매칭 "
                    f"(optNo={g.get('recommendedOptNo')} cat={cat_code})"
                )
                break
    if not g:
        # 마지막 fallback — 첫 번째 유효 그룹 사용
        valid = [gr for gr in groups if gr.get("recommendedOptNo")]
        if valid:
            g = valid[0]
            opt_name = (g.get("recommendedOptName") or {}).get("kor", "")
            logger.warning(
                f"[ESM] 옵션그룹 최종 fallback: '{samba_opt_name}' → '{opt_name}' "
                f"(optNo={g.get('recommendedOptNo')} cat={cat_code})"
            )
        else:
            logger.warning(
                f"[ESM] 카테고리 {cat_code}에 유효한 추천옵션그룹 없음 (samba='{samba_opt_name}')"
            )
            return None, []
    values = await client.get_recommended_opt_values(g["recommendedOptNo"])
    return g, [v for v in values if v.get("recommendedOptValueNo")]


# 한글 브랜드 → ESM 정식 검색어 alias.
# ESM이 일부 정식 브랜드를 영문명으로만 노출 → 한글 검색은 변종(키즈/플레이 등)만
# 반환하고 정식 브랜드가 누락됨. 검증된 케이스만 등재(추측 금지).
#   예: '엠엘비' 검색 → 엠엘비키즈/엠엘비(마스크)/엠엘비그루/엠엘비플레이 (정식 누락)
#       'MLB' 검색 → 'Mlb'(brandNo=23347, 정식) 노출 (2026-06-07 nanol06 실측)
_ESM_BRAND_ALIASES: dict[str, str] = {
    "엠엘비": "MLB",
}


async def _search_brand_exact(client: ESMPlusClient, query: str) -> int | None:
    """query 로 검색 후 brandName 정확일치(공백/대소문자 무시) brandNo 반환."""
    try:
        resp = await client.search_brands(query.strip())
    except Exception as e:
        logger.warning(f"[ESM] 브랜드 코드 조회 실패: '{query}' {e}")
        return None
    brands = (resp or {}).get("brands") or []
    target = re.sub(r"\s+", "", query).lower()
    for b in brands:
        bn = re.sub(r"\s+", "", (b.get("brandName") or "")).lower()
        if bn == target and b.get("brandNo"):
            return int(b["brandNo"])
    return None


async def resolve_esm_brand_no(client: ESMPlusClient, brand_name: str) -> int | None:
    """브랜드명 → ESM brandNo 매핑 (정확명 매칭).

    ESM 등록 시 itemBasicInfo.catalog.brandNo 가 실제 브랜드 필드.
    문자열 brand 만 보내면 ESM이 무시 → 마켓 리스팅 브랜드 빈칸.
    search_brands 응답: {"brands": [{"brandNo", "brandName", "makerNo", ...}]}.
    오매칭 방지 위해 공백/대소문자 무시 '정확 일치'만 채택. 없으면 None.

    1차 원본명 정확매칭 → 실패 시 한글→영문 alias 재검색(여전히 정확매칭).
    """
    if not brand_name or not brand_name.strip():
        return None

    # 1차: 원본명 정확매칭
    no = await _search_brand_exact(client, brand_name.strip())
    if no:
        return no

    # 2차: 한글→영문 alias 재검색 (ESM이 정식 브랜드를 영문으로만 노출하는 케이스)
    key = re.sub(r"\s+", "", brand_name).lower()
    alias = _ESM_BRAND_ALIASES.get(key) or _ESM_BRAND_ALIASES.get(brand_name.strip())
    if alias:
        no = await _search_brand_exact(client, alias)
        if no:
            logger.info(
                f"[ESM] 브랜드 alias 매칭: '{brand_name}' → '{alias}' brandNo={no}"
            )
            return no

    logger.warning(
        f"[ESM] 브랜드 정확매칭 없음 — catalog.brandNo 미설정 (brand='{brand_name}')"
    )
    return None


# 셀러+사이트별 {siteGoodsNo → master goodsNo} 맵 + master 집합 캐시 (TTL 10분).
# 수정/삭제 API는 마스터번호 필수인데 과거 저장값은 siteGoodsNo라 404 → 역매핑 필요.
_ESM_MASTER_MAP_CACHE: dict[str, tuple[float, tuple[dict[str, str], set[str]]]] = {}
_ESM_MASTER_MAP_TTL = 600.0


async def _build_esm_master_map(
    client: ESMPlusClient,
) -> tuple[dict[str, str], set[str]]:
    """셀러 전체 카탈로그 페이징 → ({siteGoodsNo: master}, {master...}).

    search_products 가 managedCode/siteGoodsNo 필터를 무시함(검증됨)이라
    전체 목록을 훑어 매칭. siteGoodsNo 는 {gmkt, iac} 구조 — 사이트별 키만 채택.
    """
    site_key = client.cfg["siteKey"].lower()  # "iac"(옥션) | "gmkt"(지마켓)
    site_map: dict[str, str] = {}
    masters: set[str] = set()
    for page in range(1, 41):  # 최대 40페이지(2,000건) 안전상한
        try:
            r = await client.search_products({"pageIndex": page, "pageSize": 50})
        except Exception as e:
            logger.warning(f"[ESM] 카탈로그 스캔 중단 page={page}: {e}")
            break
        items = r.get("items") or []
        if not items:
            break
        for it in items:
            master = str(it.get("goodsNo") or "").strip()
            if not master:
                continue
            masters.add(master)
            sno = str((it.get("siteGoodsNo") or {}).get(site_key) or "").strip()
            if sno:
                site_map[sno] = master
        if len(items) < 50:
            break
    return site_map, masters


async def _esm_targeted_resolve(
    client: ESMPlusClient, val: str, site_key: str
) -> str | None:
    """타깃 조회(#371 ②): 전체스캔 전 1~2콜로 master 변환 시도.

    1) query.goodsNo=[val] → 이미 master이면 그대로 반환 (0→1콜)
    2) query.siteGoodsNo=[val] → master 변환 (1→2콜)
    실패 시 None 반환 → 호출자가 전체스캔으로 폴백.
    """
    try:
        # ① val이 이미 master goodsNo인지 확인
        try:
            gno_int = int(val)
        except (ValueError, TypeError):
            gno_int = None
        if gno_int is not None:
            r = await client.search_products(
                {"query": {"goodsNo": [gno_int]}, "pageIndex": 1, "pageSize": 1}
            )
            for it in r.get("items") or []:
                if str(it.get("goodsNo") or "").strip() == val:
                    return val
        # ② siteGoodsNo → master 변환
        r2 = await client.search_products(
            {"query": {"siteGoodsNo": [val]}, "pageIndex": 1, "pageSize": 1}
        )
        for it in r2.get("items") or []:
            sno = str((it.get("siteGoodsNo") or {}).get(site_key) or "").strip()
            if sno == val:
                master = str(it.get("goodsNo") or "").strip()
                if master and master not in ("0", "0.0"):
                    return master
    except Exception as e:
        logger.debug(f"[ESM] 타깃조회 실패(폴백): {e}")
    return None


async def resolve_esm_master_goods_no(
    client: ESMPlusClient, goods_no: str
) -> str | None:
    """저장 상품번호(siteGoodsNo 또는 master) → 마스터 goodsNo.

    ESM 수정/삭제/판매상태 API(/goods/{goodsNo}/...)는 마스터번호 필수.
    과거 저장값은 siteGoodsNo(옥션 F.../지마켓 숫자)라 그대로 호출 시 404.

    순서(#371 ②): 캐시(0콜) → 타깃조회(1~2콜) → 전체스캔(폴백).
    """
    val = str(goods_no or "").strip()
    if not val:
        return None
    site_key = client.cfg["siteKey"].lower()
    cache_key = f"{client.site}:{client.seller_id}"
    now = time.time()
    cached = _ESM_MASTER_MAP_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _ESM_MASTER_MAP_TTL:
        site_map, masters = cached[1]
        if val in masters:
            return val
        if val in site_map:
            return site_map[val]

    # 캐시 없음/stale — 타깃조회 먼저 (rate-limit 절약)
    targeted = await _esm_targeted_resolve(client, val, site_key)
    if targeted is not None:
        return targeted

    # 전체스캔 폴백 (캐시 갱신 겸)
    fresh = await _build_esm_master_map(client)
    _ESM_MASTER_MAP_CACHE[cache_key] = (now, fresh)
    site_map, masters = fresh
    if val in masters:
        return val
    return site_map.get(val)


async def register_esm_options(
    client: ESMPlusClient,
    goods_no: str,
    cat_code: str,
    samba_options: list[dict[str, Any]],
    *,
    site: str = "gmarket",
    stock_per_value: int = 99,
    build_only: bool = False,
) -> dict[str, Any]:
    """samba options → ESM 추천옵션 매핑 + 등록.

    samba options 형식: [{"name": "색상", "values": [{"name": "네이비", "stock": 10}, ...]}, ...]

    옵션 갯수 → ESM type:
      - 1개  → type=1 선택형 (최대 50건)
      - 2개  → type=2 2조합형 (최대 500건, cartesian product)
      - 3개+ → type=3 (3 조합, 첫 3개만 사용)
    한계:
      - 매핑 실패 항목 = 스킵 + warning.
      - 조합형은 cartesian product (모든 조합) 생성. addAmnt = 각 값 합.
      - "직접입력" 그룹 (recommendedOptNo=0) fallback 미지원 (별도 PR).

    build_only=True (#368 ①): set_recommended_options PUT 을 호출하지 않고
    등록 POST 본문 itemAddtionalInfo.recommendedOpts 에 인라인 동봉할 payload 만
    빌드해서 반환. propagation polling/race 제거(atomic 등록). 반환 dict 에
    payload(빌드 성공 시) + multi_variant(총 옵션값 2개+ 여부) 포함.
    """
    if not samba_options:
        return {"success": False, "message": "samba_options 비어있음"}

    # flat list(값 나열) → 단일 그룹 구조 변환
    # collected_product.options: [{"name":"S","stock":2}, ...] 형태는 "values" 키 없음
    if not any(o.get("values") for o in samba_options):
        samba_options = [{"name": "옵션", "values": samba_options}]

    site_key = ESMPlusClient.SITE_CONFIG[site]["siteKey"]
    opt_count = min(len(samba_options), 3)

    # 총 옵션값(변형)수 — 미발행 가드 판단용 (#368 ①).
    # 축수가 아닌 총 변형수 기준: 구매자가 고를 변형이 둘 이상이면 multi_variant.
    total_variants = sum(
        len(_normalize_samba_option(o)[1]) for o in samba_options[:opt_count]
    )
    multi_variant = total_variants >= 2

    if opt_count == 1:
        opt_type = 1
        independent, combination = await _build_independent(
            client, cat_code, samba_options[0], site_key, stock_per_value
        )
        if not independent or not independent.get("details"):
            return {
                "success": False,
                "matched": 0,
                "requested": len(_normalize_samba_option(samba_options[0])[1]),
                "multi_variant": multi_variant,
                "message": "매칭된 옵션값 0건 (선택형)",
            }
        matched = len(independent["details"])
        requested = len(_normalize_samba_option(samba_options[0])[1])
        group_label = independent.get("_group_label")
        independent.pop("_group_label", None)
    else:
        opt_type = opt_count  # 2 또는 3
        independent = None
        combination, matched, requested, group_label = await _build_combination(
            client, cat_code, samba_options[:opt_count], site_key, stock_per_value
        )
        if combination and combination.get("details"):
            # 실제 매칭된 축 수로 opt_type 보정 (스킵된 축 있을 수 있음)
            actual_axes = combination.pop("_axis_count", opt_count)
            opt_type = max(2, actual_axes)
        else:
            # 조합형 완전 실패 → type=1 (선택형) fallback: 첫 번째로 매칭되는 축 사용
            logger.warning(
                f"[ESM] 조합형 실패 → type=1 선택형 fallback 시도 (cat={cat_code})"
            )
            combination = None
            opt_type = 1
            independent = None
            for fallback_opt in samba_options[:opt_count]:
                fb_ind, _ = await _build_independent(
                    client, cat_code, fallback_opt, site_key, stock_per_value
                )
                if fb_ind and fb_ind.get("details"):
                    independent = fb_ind
                    matched = len(fb_ind["details"])
                    requested = len(_normalize_samba_option(fallback_opt)[1])
                    group_label = fb_ind.get("_group_label")
                    fb_ind.pop("_group_label", None)
                    logger.info(
                        f"[ESM] type=1 fallback 성공: "
                        f"opt='{fallback_opt.get('name') or fallback_opt.get('option_name')}' "
                        f"matched={matched}/{requested}"
                    )
                    break
            if not independent or not independent.get("details"):
                return {
                    "success": False,
                    "matched": 0,
                    "requested": requested,
                    "multi_variant": multi_variant,
                    "message": f"조합형 및 선택형 모두 실패 (원본 {opt_count}축)",
                }
        if combination:
            combination.pop("_axis_count", None)

    async def _try_set_options(
        payload: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """set_recommended_options 호출 + 이미지 propagation 재시도. (성공, 에러메시지)"""
        last_exc: Exception | None = None
        for wait in (0, 30, 60):
            if wait > 0:
                logger.warning(
                    f"[ESM] 옵션 PUT 이미지 propagation 대기 {wait}s 후 재시도 (goods={goods_no})"
                )
                await asyncio.sleep(wait)
            try:
                await client.set_recommended_options(goods_no, payload)
                return True, None
            except RuntimeError as exc:
                msg = str(exc)
                # 이미지 propagation 미완 케이스 — 재시도 대상.
                # "이미지 다운로드…지연/작업 시간 초과"(resultCode=1000)는 ESM이 메인
                # 상품이미지 ingest 전 옵션 set 시 5초 타임아웃으로 반환되는 메시지.
                # 이게 빠져 있으면 옵션상품이 조용히 단일옵션으로 등록됨 (#361).
                if (
                    "잘못된 상품 이미지" in msg
                    or "404" in msg
                    or "이미지 다운로드" in msg
                    or "작업 시간이 초과" in msg
                ):
                    last_exc = exc
                    continue
                # 이미지 외 에러 — 재시도 없이 즉시 실패로 처리
                return False, msg
        return False, f"이미지 propagation 90s 후에도 실패: {last_exc}"

    payload = {
        "type": opt_type,
        "isStockManage": True,
        "independent": independent,
        "combination": combination,
    }

    # build_only — PUT 없이 인라인 동봉용 payload 반환 (#368 ①, atomic 등록)
    if build_only:
        return {
            "success": True,
            "type": opt_type,
            "matched": matched,
            "requested": requested,
            "group": group_label,
            "multi_variant": multi_variant,
            "payload": payload,
        }

    ok, err_msg = await _try_set_options(payload)
    if ok:
        return {
            "success": True,
            "type": opt_type,
            "matched": matched,
            "requested": requested,
            "group": group_label,
        }

    # 조합형 실패 시 type=1 선택형으로 재시도
    if opt_type >= 2 and err_msg:
        logger.warning(
            f"[ESM] type={opt_type} 옵션 등록 실패 ({err_msg}) → type=1 선택형 재시도 (goods={goods_no})"
        )
        for fallback_opt in samba_options[:opt_count]:
            fb_ind, _ = await _build_independent(
                client, cat_code, fallback_opt, site_key, stock_per_value
            )
            if not fb_ind or not fb_ind.get("details"):
                continue
            fb_ind.pop("_group_label", None)
            fb_payload = {
                "type": 1,
                "isStockManage": True,
                "independent": fb_ind,
                "combination": None,
            }
            ok2, err2 = await _try_set_options(fb_payload)
            if ok2:
                opt_name = fallback_opt.get("name") or fallback_opt.get(
                    "option_name", ""
                )
                logger.info(
                    f"[ESM] type=1 fallback 성공: opt='{opt_name}' goods={goods_no}"
                )
                return {
                    "success": True,
                    "type": 1,
                    "matched": len(fb_ind["details"]),
                    "requested": requested,
                    "group": None,
                    "note": f"type={opt_type} 실패 후 type=1 fallback",
                }
            logger.warning(
                f"[ESM] type=1 fallback 실패: opt='{fallback_opt.get('name')}' err={err2}"
            )

    return {
        "success": False,
        "type": opt_type,
        "matched": 0,
        "requested": requested,
        "message": err_msg or "옵션 등록 실패",
    }

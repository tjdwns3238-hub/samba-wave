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
                limits=httpx.Limits(
                    max_connections=20, max_keepalive_connections=10
                ),
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

        body: dict[str, Any] = {}
        try:
            body = resp.json()
        except Exception:
            pass

        result_code = body.get("resultCode", 0) if body else 0
        if resp.status_code >= 400 or (body and result_code != 0):
            msg = body.get("message") or resp.text[:500]
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
                    return await self._call_api(
                        "DELETE", f"/item/v1/goods/{goods_no}"
                    )
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

    async def get_recommended_opt_groups(
        self, cat_code: str
    ) -> list[dict[str, Any]]:
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

    async def get_recommended_options(
        self, goods_no: str
    ) -> dict[str, Any]:
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
        samba_text: str, esm_values: list[dict[str, Any]]
    ) -> int | None:
        """samba 자유 텍스트 옵션값 → ESM recommendedOptValueNo 매칭.

        매칭 우선순위 (대소문자/공백 무시):
          1. kor 정확 일치
          2. eng 정확 일치
          3. korEng (한+영) 정확 일치
          4. kor / eng 부분 포함 (substring)
        없으면 None — 운영자가 '직접입력' 그룹 또는 텍스트형(type=5) 으로 fallback.

        Args:
          samba_text: samba 옵션값 (예: "네이비", "GREEN", "Navy", "검정")
          esm_values: get_recommended_opt_values() 응답 list. recommendedOptValueNo=0
                      placeholder 는 호출자가 사전 제외 권장.
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

        # 2차: 부분 포함 (samba 가 ESM 값에 포함되거나 그 반대)
        for v in esm_values:
            no = v.get("recommendedOptValueNo")
            if not no:
                continue
            name = v.get("recommendedOptValueName") or {}
            for key in ("kor", "eng", "korEng"):
                normalized = _norm(name.get(key))
                if not normalized:
                    continue
                if target in normalized or normalized in target:
                    return int(no)
        return None

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
        return_place_no = int(product.get("_return_place_no", 0) or 0)

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
        if return_place_no:
            policy_obj["returnPlaceNo"] = return_place_no
        each_obj: dict[str, Any] = {"feeType": each_fee_type}
        if each_fee_type == 2 and delivery_base_fee > 0:
            each_obj["fee"] = delivery_base_fee
        policy_obj["each"] = each_obj
        shipping["policy"] = policy_obj

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
    "wear": 1,       # 의류
    "shoes": 2,      # 구두/신발
    "bag": 3,        # 가방
    "accessories": 4,  # 패션잡화(모자/벨트/액세서리 등)
    "cosmetic": 18,  # 화장품
    "food": 20,      # 농수축산물 (가공식품은 21, 건강기능식품은 22)
    "electronics": 12,  # 소형전자 (휴대형 통신기기 13, 가정용 전기제품 8 등 세분 가능)
    "sports": 25,    # 스포츠 용품
    "etc": 35,       # 기타 재화
}


def _get_esm_notice_no(group: str) -> int:
    """고시정보 그룹 → ESM Plus 고시정보 번호."""
    return _ESM_NOTICE_MAP.get(group, 35)


# 그룹별 고시정보 항목 코드 매핑 (group → list[(itemelementCode, source_field)])
# 검증 출처: GET /item/v1/official-notice/groups/{no}/codes
# isExtraMark=true 항목 모두 채워야 ESM 등록 검증 통과. fallback="[상세설명참조]" 안전.
_ESM_NOTICE_ITEMS: dict[int, list[tuple[str, str]]] = {
    1: [  # 의류
        ("1-1", "material"),         # 제품소재
        ("1-2", "color"),             # 색상
        ("1-3", ""),                  # 치수 (옵션/상세설명 참조)
        ("1-4", "manufacturer"),     # 제조자/수입자
        ("1-5", "origin"),            # 제조국
        ("1-6", "care_instructions"),  # 세탁방법
        ("1-7", ""),                  # 제조연월
        ("1-8", "quality_guarantee"),  # 품질보증기준
        ("1-9", "_as_phone"),         # A/S 책임자/전화
        ("1-10", ""),                 # 주문후 예상 배송기간
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
        items.append(
            {"officialNoticeItemelementCode": code, "value": val}
        )
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
# samba options → ESM 추천옵션 흐름 헬퍼 (plugins 공유)
# ------------------------------------------------------------------


async def register_esm_options(
    client: ESMPlusClient,
    goods_no: str,
    cat_code: str,
    samba_options: list[dict[str, Any]],
    *,
    site: str = "gmarket",
    stock_per_value: int = 99,
) -> dict[str, Any]:
    """samba options → ESM 추천옵션 매핑 + 등록.

    samba options 형식: [{"name": "색상", "values": [{"name": "네이비", "stock": 10}, ...]}]

    한계 (별도 PR 영역):
      - 첫 옵션 그룹만 처리 (type=1 선택형). 2-3 조합형(type=2/3) 미지원.
      - 매핑 실패한 옵션값 = 해당 항목 스킵 (운영자 수동 매핑 권장).
    """
    if not samba_options:
        return {"success": False, "message": "samba_options 비어있음"}
    first_opt = samba_options[0]
    samba_opt_name = first_opt.get("name") or first_opt.get("option_name") or ""
    samba_values = (
        first_opt.get("values") or first_opt.get("option_values") or []
    )
    if not samba_opt_name or not samba_values:
        return {"success": False, "message": "옵션 이름/값 누락"}

    groups = await client.get_recommended_opt_groups(cat_code)
    matched_group = ESMPlusClient.detect_esm_option_group(samba_opt_name, groups)
    if not matched_group:
        return {
            "success": False,
            "message": f"ESM 그룹 매칭 실패: samba='{samba_opt_name}', cat={cat_code}",
        }
    rec_opt_no = matched_group["recommendedOptNo"]
    values_pool = await client.get_recommended_opt_values(rec_opt_no)
    values_pool = [v for v in values_pool if v.get("recommendedOptValueNo")]

    site_key = ESMPlusClient.SITE_CONFIG[site]["siteKey"]
    details: list[dict[str, Any]] = []
    matched = 0
    for sv in samba_values:
        if isinstance(sv, dict):
            value_text = sv.get("name") or sv.get("value") or ""
            qty = int(sv.get("stock", stock_per_value) or stock_per_value)
            add_amnt = int(
                sv.get("priceAdjust", 0) or sv.get("addPrice", 0) or 0
            )
            sold_out = bool(sv.get("isSoldOut") or sv.get("is_sold_out"))
        else:
            value_text = str(sv)
            qty = stock_per_value
            add_amnt = 0
            sold_out = False
        rec_val_no = ESMPlusClient.match_option_value(value_text, values_pool)
        if not rec_val_no:
            logger.warning(
                f"[ESM] 옵션값 매칭 실패: cat={cat_code} group={rec_opt_no} text='{value_text}'"
            )
            continue
        details.append(
            {
                "recommendedOptValueNo": rec_val_no,
                "addAmnt": add_amnt,
                "qty": {site_key: 0 if sold_out else qty},
                "isSoldOut": sold_out,
                "isDisplay": True,
                "manageCode": f"OPT{rec_val_no}",
            }
        )
        matched += 1

    if not details:
        return {
            "success": False,
            "matched": 0,
            "requested": len(samba_values),
            "message": "매칭된 옵션값 0건",
        }

    payload = {
        "type": 1,
        "isStockManage": True,
        "independent": {"recommendedOptNo": rec_opt_no, "details": details},
        "combination": None,
    }
    await client.set_recommended_options(goods_no, payload)
    return {
        "success": True,
        "matched": matched,
        "requested": len(samba_values),
        "group": matched_group.get("recommendedOptName"),
    }

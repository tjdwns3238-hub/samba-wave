"""롯데ON Open API 클라이언트 - 상품 등록/수정.

인증 방식: Bearer {apiKey}
기본 URL: https://openapi.lotteon.com
카테고리/브랜드: https://onpick-api.lotteon.com (별도 도메인)

거래처 정보(trGrpCd, trNo)는 identity API에서 자동 획득.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.utils import now_kst

from backend.domain.samba.proxy.notice_utils import (
    build_lotteon_notice as _build_lot_notice,
)

import httpx

from backend.core.config import settings
from backend.utils.logger import logger


def _truncate_to_bytes(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


# ──────────────────────────────────────────────────────────────────────
# 원산지 → 롯데ON ISO alpha-2 코드 매핑
# ──────────────────────────────────────────────────────────────────────

_LOTTEON_ORIGIN_CODE: dict[str, str] = {
    # 국내
    "한국": "KR",
    "대한민국": "KR",
    "국내": "KR",
    "국산": "KR",
    "korea": "KR",
    # 아시아
    "중국": "CN",
    "china": "CN",
    "베트남": "VN",
    "vietnam": "VN",
    "일본": "JP",
    "japan": "JP",
    "인도": "IN",
    "india": "IN",
    "인도네시아": "ID",
    "indonesia": "ID",
    "태국": "TH",
    "thailand": "TH",
    "캄보디아": "KH",
    "cambodia": "KH",
    "방글라데시": "BD",
    "bangladesh": "BD",
    "미얀마": "MM",
    "myanmar": "MM",
    "필리핀": "PH",
    "philippines": "PH",
    "홍콩": "HK",
    "hong kong": "HK",
    "대만": "TW",
    "taiwan": "TW",
    "말레이시아": "MY",
    "malaysia": "MY",
    # 유럽
    "이탈리아": "IT",
    "italy": "IT",
    "프랑스": "FR",
    "france": "FR",
    "독일": "DE",
    "germany": "DE",
    "스페인": "ES",
    "spain": "ES",
    "영국": "GB",
    "uk": "GB",
    "포르투갈": "PT",
    "portugal": "PT",
    # 북미
    "미국": "US",
    "usa": "US",
    "us": "US",
    "캐나다": "CA",
    "canada": "CA",
}


def _get_lotteon_origin_code(origin: str) -> str:
    """원산지 텍스트 → 롯데ON ISO alpha-2 코드. 미매핑 시 KR 폴백."""
    if not origin:
        return "KR"
    lower = origin.lower().strip()
    if lower in _LOTTEON_ORIGIN_CODE:
        return _LOTTEON_ORIGIN_CODE[lower]
    for keyword, code in _LOTTEON_ORIGIN_CODE.items():
        if keyword in lower or keyword in origin:
            return code
    return "KR"


# ──────────────────────────────────────────────────────────────────────
# SEO 키워드 생성
# ──────────────────────────────────────────────────────────────────────


def _build_lotteon_keywords(product: dict[str, Any]) -> list[str]:
    """SEO 검색 키워드 빌드 — 최대 20개, 각 30자 이내.

    우선순위: seo_keywords → tags → 브랜드 → 카테고리 → 상품명 단어 분리
    """
    seen: set[str] = set()
    keywords: list[str] = []

    def _add(kw: str) -> None:
        kw = kw.strip()[:30]
        if kw and kw not in seen:
            seen.add(kw)
            keywords.append(kw)

    for kw in product.get("seo_keywords") or []:
        _add(str(kw))
    for tag in product.get("tags") or []:
        _add(str(tag))

    brand = product.get("brand", "")
    if brand:
        _add(brand)

    for cat_field in ("category1", "category2", "category3"):
        cat = product.get(cat_field, "")
        if cat:
            _add(cat)

    name = product.get("name", "")
    for word in re.split(r"[\s\[\]()（）,./·|]+", name):
        word = word.strip()
        if len(word) >= 2:
            _add(word)

    return keywords[:20]


# ──────────────────────────────────────────────────────────────────────
# 상품 소개문 자동 생성
# ──────────────────────────────────────────────────────────────────────


def _build_lotteon_intro(product: dict[str, Any]) -> str:
    """상품 소개문 자동 생성 — 최대 200자.

    "[브랜드] 상품명 | 카테고리 | 소재: OOO, 색상: OOO, 원산지: OOO"
    """
    parts: list[str] = []
    brand = product.get("brand", "")
    name = product.get("name", "")
    category = product.get("category2") or product.get("category1") or ""
    material = product.get("material", "")
    color = product.get("color", "")
    origin = product.get("origin", "")

    if brand:
        parts.append(f"[{brand}]")
    if name:
        parts.append(name)
    if category:
        parts.append(f"| {category}")

    details: list[str] = []
    if material:
        details.append(f"소재: {material}")
    if color:
        details.append(f"색상: {color}")
    if origin:
        details.append(f"원산지: {origin}")
    if details:
        parts.append("| " + ", ".join(details))

    return " ".join(parts)[:200]


# ──────────────────────────────────────────────────────────────────────
# 상품홍보문구 자동 생성
# ──────────────────────────────────────────────────────────────────────


class LotteonClient:
    """롯데ON Open API 클라이언트."""

    BASE_URL = "https://openapi.lotteon.com"
    # 카테고리/브랜드는 별도 도메인
    ONPICK_URL = "https://onpick-api.lotteon.com"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key.strip()
        self.tr_grp_cd: str = ""
        self.tr_no: str = ""
        # 인스턴스 수준 공유 클라이언트 — Cloud NAT 포트 소진 방지
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        """공유 httpx 클라이언트 반환. 닫혔으면 재생성."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=settings.http_timeout_default,
                limits=httpx.Limits(
                    max_connections=5,
                    max_keepalive_connections=3,
                    keepalive_expiry=30.0,
                ),
            )
        return self._client

    async def aclose(self) -> None:
        """클라이언트 명시적 종료 — 캐시 만료 시 호출."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "Accept-Language": "ko",
            "X-Timezone": "GMT+09:00",
        }

    async def _call_api(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, str]] = None,
        base_url: Optional[str] = None,
        _shared_client: Optional[Any] = None,
    ) -> dict[str, Any]:
        """공통 API 호출. _shared_client 제공 시 TCP 연결 재사용."""
        url = f"{base_url or self.BASE_URL}{path}"
        headers = self._headers()

        async def _do(c: Any) -> Any:
            if method == "GET":
                return await c.get(url, headers=headers, params=params)
            elif method == "POST":
                return await c.post(url, headers=headers, json=body or {})
            elif method == "PUT":
                return await c.put(url, headers=headers, json=body or {})
            elif method == "DELETE":
                return await c.delete(url, headers=headers, params=params)
            raise ValueError(f"지원하지 않는 HTTP 메서드: {method}")

        if _shared_client is not None:
            resp = await _do(_shared_client)
        else:
            resp = await _do(self._get_client())

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        logger.info(f"[롯데ON] {method} {path} → {resp.status_code}")

        if not resp.is_success:
            msg = data.get("message", "") or data.get("msg", "") or resp.text[:300]
            logger.warning(
                f"[롯데ON] HTTP {resp.status_code} 응답 body: {resp.text[:500]}"
            )
            raise LotteonApiError(f"HTTP {resp.status_code}: {msg}")

        # HTTP 200이어도 응답 body에 에러 코드가 있을 수 있음
        # returnCode: 요청 레벨 에러 (카테고리 누락 등)
        res_code = (
            data.get("returnCode")
            or data.get("code")
            or data.get("resultCode")
            or data.get("rspnCd")
            or ""
        )
        if res_code and res_code not in ("0000", "00", "SUCCESS"):
            msg = (
                data.get("message", "")
                or data.get("msg", "")
                or data.get("rspnMsgCntn", "")
                or str(data)
            )
            logger.warning(f"[롯데ON] 응답 에러 코드: {res_code} — {msg}")
            logger.warning(f"[롯데ON] 응답 전체 body: {data}")
            raise LotteonApiError(f"응답 에러 ({res_code}): {msg}")

        return data

    # ------------------------------------------------------------------
    # 인증
    # ------------------------------------------------------------------

    async def test_auth(self) -> dict[str, Any]:
        """거래처 정보 조회 (인증 테스트) — trGrpCd, trNo 자동 획득."""
        result = await self._call_api("GET", "/v1/openapi/common/v1/identity")
        logger.info(f"[롯데ON] identity 전체 응답: {result}")
        data = result.get("data", {})
        if data:
            self.tr_grp_cd = data.get("trGrpCd", "")
            self.tr_no = data.get("trNo", "")
        logger.info(
            f"[롯데ON] 추출값 — tr_grp_cd={self.tr_grp_cd!r}, tr_no={self.tr_no!r}"
        )
        return {"success": True, "message": "인증 성공", "data": data}

    async def get_delivery_policies(self) -> dict[str, Any]:
        """배송비정책 목록 조회 — test_auth() 선행 호출 필요.

        afflTrCd = 상위거래처번호 (identity API의 trNo, 예: LO10156909)
        """
        return await self._call_api(
            "POST",
            "/v1/openapi/contract/v1/dvl/getDvCstListSr",
            body={"afflTrCd": self.tr_no},
        )

    async def get_warehouses(self) -> dict[str, Any]:
        """출고지/회수지 목록 조회 — test_auth() 선행 호출 필요.

        afflTrCd = 상위거래처번호 (identity API의 trNo, 예: LO10156909)
        """
        return await self._call_api(
            "POST",
            "/v1/openapi/contract/v1/dvp/getDvpListSr",
            body={"afflTrCd": self.tr_no},
        )

    # ------------------------------------------------------------------
    # 상품 등록/수정/조회
    # ------------------------------------------------------------------

    async def register_product(self, product_data: dict[str, Any]) -> dict[str, Any]:
        """상품 등록.

        롯데ON은 returnCode=0000(요청 접수)이어도
        data[].resultCode=9999이면 개별 상품 등록 실패.

        유령상품 방지: 응답에 spdNo 없으면 epdNo로 list API 재조회 보강.
        spdNo 없이 epdNo만 저장하면 후속 update/status_change/get_product 가 모두 실패하므로,
        정상 처리에서는 반드시 진짜 spdNo 를 채워서 반환한다.
        """
        result = await self._call_api(
            "POST",
            "/v1/openapi/product/v1/product/registration/request",
            body=product_data,
        )
        # 개별 상품 결과 검증 (data는 리스트)
        data_list = result.get("data", [])
        if isinstance(data_list, list) and data_list:
            item = data_list[0]
            if isinstance(item, dict):
                item_code = item.get("resultCode", "")
                if item_code and item_code not in ("0000", "00", "SUCCESS"):
                    msg = item.get("resultMessage", "") or str(item)
                    logger.warning(f"[롯데ON] 상품 등록 실패: {item_code} — {msg}")
                    raise LotteonApiError(f"상품 등록 실패 ({item_code}): {msg}")
                # 성공 시 spdNo 추출 — 응답에 없으면 epdNo로 list API 보강
                spd_no = str(item.get("spdNo") or "").strip()
                epd_no = str(item.get("epdNo") or "").strip()
                if not epd_no:
                    # product_data 안 spdLst[0].epdNo 폴백
                    try:
                        epd_no = str(
                            (product_data.get("spdLst") or [{}])[0].get("epdNo") or ""
                        ).strip()
                    except Exception:
                        epd_no = ""
                if not spd_no and epd_no:
                    spd_no = await self._lookup_spd_no_by_epd_no(epd_no)
                    if spd_no:
                        logger.info(
                            f"[롯데ON] spdNo 보강 성공 — epdNo={epd_no} → spdNo={spd_no}"
                        )
                    else:
                        logger.warning(
                            f"[롯데ON] spdNo 보강 실패 — epdNo={epd_no} 조회 결과 없음 "
                            "(유령상품 위험, ghost_reconciler 가 다음 사이클에 회수)"
                        )
                return {
                    "success": True,
                    "data": result,
                    "spdNo": spd_no,
                    "epdNo": epd_no,
                }
        return {"success": True, "data": result}

    async def _lookup_spd_no_by_epd_no(
        self, epd_no: str, retries: int = 3, delay: float = 2.0
    ) -> str:
        """epdNo로 list API 재조회해 spdNo 회수 (등록 직후 인덱싱 지연 대응).

        retries 회 / delay 초 간격으로 시도. trGrpCd/trNo 가 비어있으면 빈 문자열 반환.
        """
        if not (self.tr_grp_cd and self.tr_no and epd_no):
            return ""
        import asyncio as _asyncio

        for attempt in range(retries):
            try:
                resp = await self._call_api(
                    "POST",
                    "/v1/openapi/product/v1/product/list",
                    body={
                        "trGrpCd": self.tr_grp_cd,
                        "trNo": self.tr_no,
                        "pageNo": 1,
                        "rowsPerPage": 10,
                        "regStrtDttm": "20200101000000",
                        "regEndDttm": "99991231235959",
                        "epdNo": [epd_no],
                    },
                )
                for it in resp.get("data") or []:
                    if not isinstance(it, dict):
                        continue
                    if str(it.get("epdNo") or "").strip() == epd_no:
                        spd = str(it.get("spdNo") or "").strip()
                        if spd:
                            return spd
            except Exception as e:
                logger.warning(f"[롯데ON] spdNo 보강 조회 예외(무시): {e}")
            if attempt < retries - 1:
                await _asyncio.sleep(delay)
        return ""

    async def update_product(self, product_data: dict[str, Any]) -> dict[str, Any]:
        """승인 상품 수정.

        등록과 동일하게 data[].resultCode 검증 필요.
        """
        result = await self._call_api(
            "POST",
            "/v1/openapi/product/v1/product/modification/request",
            body=product_data,
        )
        # 개별 상품 결과 검증
        data_list = result.get("data", [])
        if isinstance(data_list, list) and data_list:
            item = data_list[0]
            if isinstance(item, dict):
                item_code = item.get("resultCode", "")
                if item_code and item_code not in ("0000", "00", "SUCCESS"):
                    msg = item.get("resultMessage", "") or str(item)
                    logger.warning(f"[롯데ON] 상품 수정 실패: {item_code} — {msg}")
                    raise LotteonApiError(f"상품 수정 실패 ({item_code}): {msg}")
                spd_no = item.get("spdNo") or item.get("epdNo") or ""
                return {"success": True, "data": result, "spdNo": spd_no}
        return {"success": True, "data": result}

    async def register_publicity_sentence(self, spd_no: str, phrase: str) -> None:
        """상품 홍보문구 등록 — 등록 후 자동 호출.

        실패해도 상품 등록 자체는 롤백하지 않음 (best-effort).
        기간미설정(None)으로 등록 — 무기한 노출.
        """
        now = now_kst()
        # 시작일: 내일 00:00 — 즉시 등록 시 "과거일시" 에러 방지
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start_dt = tomorrow.strftime("%Y%m%d%H%M%S")
        # 종료일: 180일 — 1년은 판매기간 초과 에러, 기간미설정 API 미지원
        end_dt = (now + timedelta(days=180)).strftime("%Y%m%d235959")
        body = {
            "pblcStncLst": [
                {
                    "trGrpCd": self.tr_grp_cd or "SR",
                    "trNo": self.tr_no,
                    "lrtrNo": None,
                    "spdNo": spd_no,
                    "pblcStnc": phrase[:75],  # API 최대 75자
                    "pblcStncStrtDttm": start_dt,
                    "pblcStncEndDttm": end_dt,
                }
            ]
        }
        logger.debug(
            f"[롯데ON] 홍보문구 API 요청 — trGrpCd={self.tr_grp_cd!r} trNo={self.tr_no!r} spdNo={spd_no!r}"
        )
        result = await self._call_api(
            "POST",
            "/v1/openapi/product/v1/product/publicitysentence/registration/request",
            body=body,
        )
        rc_outer = result.get("returnCode", "")
        data_list = result.get("data", [])
        logger.debug(
            f"[롯데ON] 홍보문구 API 응답 — returnCode={rc_outer!r} data={data_list}"
        )
        if isinstance(data_list, list) and data_list:
            item = data_list[0]
            if isinstance(item, dict):
                rc = item.get("resultCode", "")
                if rc and rc not in ("0000", "00", "SUCCESS"):
                    logger.warning(
                        f"[롯데ON] 홍보문구 등록 실패: {rc} — {item.get('resultMessage', '')}"
                    )
                    return
        logger.info(f"[롯데ON] 홍보문구 등록 완료 — spdNo={spd_no!r}")

    async def get_product(self, spd_no: str) -> dict[str, Any]:
        """상품 단건 조회 (POST 방식)."""
        body = {
            "trGrpCd": self.tr_grp_cd or "SR",
            "trNo": self.tr_no,
            "spdNo": spd_no,
        }
        return await self._call_api(
            "POST",
            "/v1/openapi/product/v1/product/detail",
            body=body,
        )

    # ── 프로모션 API ───────────────────────────────────────────────────

    async def save_product_exception(
        self, spd_no: str, flags: dict[str, str]
    ) -> dict[str, Any]:
        """행사 제외 설정.

        flags 예시 (8개 필드 전부 필수):
          onerDcXcldAgrCd: AGR/NXCLD       오너스할인
          pcsDcXcldAgrCd: AGR/NXCLD        PCS할인
          ovlpCpnXcldAgrCd: AGR/NXCLD      중복쿠폰(상품단위쿠폰)
          dvCpnXcldAgrCd: AGR/NXCLD        배송쿠폰
          stffDcXcldAgrCd: AGR/NXCLD       임직원할인
          odCndCpnXcldAgrCd: AGR/NXCLD     장바구니쿠폰
          crdCmDcXcldAgrCd: AGR/NXCLD      카드즉시할인(CM+PCS)
          crdReqDcCashbXcldAgrCd: AGR/NXCLD 카드캐시백
        값: AGR=제외, NXCLD=제외안함
        """
        body = {"trNo": self.tr_no, "spdNo": spd_no, **flags}
        return await self._call_api(
            "POST",
            "/v1/openapi/promotion/v1/OpenApiService/saveProductException",
            body=body,
        )

    async def search_immediate_discount_list(self, spd_no: str) -> dict[str, Any]:
        """상품의 활성 즉시할인 행사 목록 조회 (마이그레이션용)."""
        return await self._call_api(
            "POST",
            "/v1/openapi/promotion/v1/OpenApiService/searchProductImmediateDiscountList",
            body={"spdNo": spd_no},
        )

    async def terminate_immediate_discount(
        self, spd_no: str, awy_dc_pd_reg_no: str
    ) -> dict[str, Any]:
        """즉시할인 행사 강제 종료 — saveDvsCd=D (마이그레이션용)."""
        return await self._call_api(
            "POST",
            "/v1/openapi/promotion/v1/OpenApiService/saveProductImmediateDiscount",
            body={
                "saveDvsCd": "D",
                "awyDcPdRegNo": awy_dc_pd_reg_no,
                "spdNo": spd_no,
                "trNo": self.tr_no,
            },
        )

    async def list_registered_products(
        self,
        page: int = 1,
        size: int = 100,
        reg_strt_dttm: str = "20260417000000",
        reg_end_dttm: str = "20261231235959",
    ) -> dict[str, Any]:
        """셀러 등록 상품 목록 페이지 조회 (마이그레이션용).

        reg_strt_dttm / reg_end_dttm: yyyymmdd 포맷, 즉시할인 도입 시점(2026-04-17) 이후로 기본 설정.
        """
        return await self._call_api(
            "POST",
            "/v1/openapi/product/v1/product/list",
            body={
                "trGrpCd": self.tr_grp_cd,
                "trNo": self.tr_no,
                "pageNo": page,
                "pageSize": size,
                "regStrtDttm": reg_strt_dttm,
                "regEndDttm": reg_end_dttm,
            },
        )

    async def save_lpoint_accumulation(
        self,
        spd_no: str,
        accm_val1: int = 0,
        accm_vp_knd_cd: str = "7",
        accm_val2: int = 0,
        accm_val3: int = 0,
        accm_val4: int = 0,
    ) -> dict[str, Any]:
        """L.POINT 추가적립 저장.

        API 스펙:
          cndAccmVal1: 구매확정시 L.POINT (>0이면 accmVpKndCd 필수)
          accmVpKndCd: 발송일로부터 N일 이내 구매확정 시 적립 (3~8 중 택1)
          cndAccmVal2/3/4: 리뷰/사진/동영상 포인트 (하이마트/홈쇼핑 전용, 일반은 0)
        """
        now = now_kst()
        spd_num = re.sub(r"[^0-9]", "", spd_no)[-12:]
        ts_suffix = str(int(now.timestamp()))[-8:]
        affil_pr_no = f"{spd_num}{ts_suffix}"
        start_dt = now.strftime("%Y%m%d%H%M%S")
        end_dt = (now + timedelta(days=365)).strftime("%Y%m%d235959")
        body = {
            "saveDvsCd": "C",
            "accmPdRegNo": "",
            "afflPrNo": affil_pr_no,
            "trNo": self.tr_no,
            "aplyStrtDttm": start_dt,
            "aplyEndDttm": end_dt,
            "spdNo": spd_no,
            "cndAccmVal1": accm_val1,
            "accmVpKndCd": accm_vp_knd_cd,
            "cndAccmVal2": accm_val2,
            "cndAccmVal3": accm_val3,
            "cndAccmVal4": accm_val4,
        }
        logger.info(f"[롯데ON] L.POINT 적립 요청 body: {body}")
        return await self._call_api(
            "POST",
            "/v1/openapi/promotion/v1/OpenApiService/saveProductLPoint",
            body=body,
        )

    async def search_quantity_discount_list(self, spd_no: str) -> dict[str, Any]:
        """살수록할인 목록 조회 — 기존 프로모션 prNo 확인용.

        반환 data 예시:
          {"prList": [{"prNo": "12345", "prNm": "...", ...}]}
        """
        return await self._call_api(
            "POST",
            "/v1/openapi/promotion/v1/OpenApiService/searchQuantityDiscountList",
            body={"spdNo": spd_no, "prKndCd": "PRD_MAM_BUY"},
        )

    async def insert_quantity_discount(
        self,
        spd_no: str,
        min_qty: int,
        discount_rate: float,
        eitm_nos: list[str] | None = None,
        pr_no: str = "",
    ) -> dict[str, Any]:
        """살수록할인(수량 기준 정율 할인) 등록/수정.

        API 스펙:
          saveDvsCd: C=신규, U=수정(pr_no 필수), D=삭제(pr_no 필수)
          prKndCd: PRD_MAM_BUY (살수록/배수할인)
          fvrOffrValDvsDtlCd: QTY_DC (수량 기준 할인)
          dcTypCd: FX=정율, FL=정액
          dcQtyList[].minPurQty: 최소 구매수량
          dcQtyList[].dcRt: 할인율 (정율일 때)
          dcQtyList[].dcAmt: 할인액 (정액일 때, 정율이면 0)
          spdList[].spdNo: 적용 상품번호
        """
        now = now_kst()
        spd_num = re.sub(r"[^0-9]", "", spd_no)[-12:]
        ts_suffix = str(int(now.timestamp()))[-8:]
        affil_pr_no = f"{spd_num}{ts_suffix}"  # 최대 20자
        start_dt = now.strftime("%Y%m%d%H%M%S")
        end_dt = (now + timedelta(days=365)).strftime("%Y%m%d235959")
        save_dvs_cd = "U" if pr_no else "C"
        body = {
            "saveDvsCd": save_dvs_cd,  # C=신규, U=수정
            "prKndCd": "PRD_MAM_BUY",  # 살수록/배수할인
            "prNo": pr_no,  # 수정 시 기존 prNo, 신규 시 빈값
            "prNm": "삼바 살수록할인",
            "afflPrNo": affil_pr_no,  # 셀러 자체 프로모션번호(PK, 최대 20자)
            "trNo": self.tr_no,
            "aplyStrtDttm": start_dt,
            "aplyEndDttm": end_dt,
            "fvrOffrValDvsDtlCd": "QTY_DC",  # 수량 기준 할인
            "dcTypCd": "FX",  # 정율 할인
            "dcQtyList": [
                {
                    "minPurQty": int(min_qty),  # 최소 구매수량
                    "dcAmt": 0,  # 정액할인액 (정율이므로 0)
                    "dcRt": float(discount_rate),  # 할인율 (%)
                }
            ],
            "spdList": (
                [{"spdNo": spd_no, "sitmNo": eitm_no} for eitm_no in eitm_nos]
                if eitm_nos
                else [{"spdNo": spd_no, "sitmNo": ""}]  # eitm_nos 없을 때 폴백
            ),
        }
        logger.info(f"[롯데ON] 살수록할인 요청 body: {body}")
        return await self._call_api(
            "POST",
            "/v1/openapi/promotion/v1/OpenApiService/insertQuantityDiscount",
            body=body,
        )

    async def update_stock(self, itm_stk_lst: list[dict[str, Any]]) -> dict[str, Any]:
        """단품 재고 변경.

        주의: 이 엔드포인트는 stkQty만 반영하고 slStatCd는 무시한다.
        재고 0으로 자동 SOUT 잠긴 옵션을 풀려면 별도 change_item_status 호출 필요.
        """
        return await self._call_api(
            "POST",
            "/v1/openapi/product/v1/item/stock/change",
            body={"itmStkLst": itm_stk_lst},
        )

    async def change_item_status(
        self, sitm_lst: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """단품(옵션) 판매상태 변경 — slStatCd: SALE | SOUT.

        재고 0으로 자동 SOUT 잠긴 옵션을 재고 회복 후 SALE로 복구할 때 사용.
        sitmLst 각 항목 필수 필드: sitmNo, spdNo, slStatCd
        (trGrpCd/trNo는 client 컨텍스트에서 자동 주입)
        """
        enriched = [
            {"trGrpCd": self.tr_grp_cd or "SR", "trNo": self.tr_no, **item}
            for item in sitm_lst
        ]
        body: dict[str, Any] = {"sitmLst": enriched}
        return await self._call_api(
            "POST",
            "/v1/openapi/product/v1/item/status/change",
            body=body,
        )

    async def update_price(self, itm_prc_lst: list[dict[str, Any]]) -> dict[str, Any]:
        """단품 가격 변경.

        itm_prc_lst 각 항목 필수 필드: sitmNo, spdNo, slPrc
        자동 주입: trGrpCd, trNo, hstStrtDttm(현재), hstEndDttm(now+365일 yyyymmdd235959)
        LOTTEON 스펙: itmPrcLst[].(trNo|trGrpCd|spdNo|sitmNo|hstStrtDttm|hstEndDttm|slPrc)
        """
        now = now_kst()
        start_dt = now.strftime("%Y%m%d%H%M%S")
        end_dt = (now + timedelta(days=365)).strftime("%Y%m%d235959")
        enriched = [
            {
                "trGrpCd": self.tr_grp_cd or "SR",
                "trNo": self.tr_no,
                "hstStrtDttm": start_dt,
                "hstEndDttm": end_dt,
                **item,
            }
            for item in itm_prc_lst
        ]
        return await self._call_api(
            "POST",
            "/v1/openapi/product/v1/item/price/change",
            body={"itmPrcLst": enriched},
        )

    async def change_status(self, spd_lst: list[dict[str, Any]]) -> dict[str, Any]:
        """상품 판매상태 변경 (slStatCd: SALE | SOUT | END).

        trGrpCd/trNo는 등록/수정 API와 동일하게 spdLst 각 아이템 안에 위치해야 함.
        """
        enriched = [
            {"trGrpCd": self.tr_grp_cd or "SR", "trNo": self.tr_no, **item}
            for item in spd_lst
        ]
        body: dict[str, Any] = {"spdLst": enriched}
        logger.info(f"[롯데ON] change_status 요청 body: {body}")
        return await self._call_api(
            "POST",
            "/v1/openapi/product/v1/product/status/change",
            body=body,
        )

    async def delete_product(self, spd_no: str) -> dict[str, Any]:
        """상품 삭제(판매종료 전환).

        롯데ON 공식 오픈API에는 상품 완전삭제 엔드포인트가 없다
        (`/product/delete`는 404). 공식 가이드상 `status/change`로
        `slStatCd=END`(판매종료) 전환 시 일정 기간 경과 후 시스템이
        자동 삭제하므로, 이를 삭제 액션으로 사용한다.

        spdLst 각 항목 필수 필드: trGrpCd, trNo, spdNo, slStatCd
        """
        result = await self.change_status(
            [
                {
                    "trGrpCd": self.tr_grp_cd or "SR",
                    "trNo": self.tr_no,
                    "lrtrNo": "",
                    "spdNo": spd_no,
                    "slStatCd": "END",
                }
            ]
        )
        # data 배열의 항목별 resultCode 검증
        if isinstance(result, dict):
            for item in result.get("data", []) or []:
                if not isinstance(item, dict):
                    continue
                item_code = item.get("resultCode", "")
                if item_code and item_code not in ("0000", "00", "SUCCESS"):
                    msg = item.get("resultMessage", "") or str(item)
                    raise LotteonApiError(f"롯데ON 판매종료 실패 ({item_code}): {msg}")
        return {"success": True, "data": result}

    # ------------------------------------------------------------------
    # 주문 조회
    # ------------------------------------------------------------------

    async def get_orders(self, days: int = 7) -> list[dict[str, Any]]:
        """최근 N일 주문 목록 조회.

        전략: getSROrderList(전체 주문) + SellerDeliveryOrdersSearch(배송처리 중 보완) 병행.
        getSROrderList 주의사항:
          - lrtrNo 는 빈 문자열 고정 (self.tr_no 넣으면 0건)
          - orderStatusList 미전송 및 빈 배열 전송 시 0건 → 명시 필수, 전체 상태 포함
          - 파서: 빈 list 필드는 건너뛰고 다음 키 확인 (orderItems:[] → orderList 확인)
        """
        import asyncio
        from datetime import timedelta

        now = now_kst()
        start = (now - timedelta(days=days)).strftime("%Y%m%d") + "000000"
        end = now.strftime("%Y%m%d") + "235959"

        # trGrpCd 제거: "SR" 고정 시 MO WEB 채널 주문(네이버 PCS 등) 필터링됨
        body: dict[str, Any] = {
            "trNo": self.tr_no,
            "lrtrNo": "",
            "srchStrtDttm": start,
            "srchEndDttm": end,
            "pageNo": 1,
            "pageSize": 100,
            "orderStatusList": ["10", "11", "12", "13", "14", "20", "30", "40", "50"],
        }
        logger.info(
            f"[롯데ON] 주문 조회 {start}~{end}, trNo={self.tr_no} (trGrpCd 제거)"
        )

        # getSROrderList + SellerDeliveryOrdersSearch 병행 조회 후 중복 제거
        async def _get_sr_orders() -> list[dict]:
            try:
                result = await self._call_api(
                    "POST", "/v1/openapi/order/v1/getSROrderList", body=body
                )
                import json as _json

                _preview = _json.dumps(result, ensure_ascii=False, default=str)[:500]
                logger.info(f"[롯데ON] getSROrderList raw(500): {_preview}")
                data = result.get("data") or {}
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    for key in (
                        "orderItems",
                        "orderList",
                        "list",
                        "content",
                        "items",
                        "orders",
                    ):
                        val = data.get(key)
                        if isinstance(val, list) and val:
                            logger.info(
                                f"[롯데ON] getSROrderList 키='{key}', {len(val)}건"
                            )
                            return val
                logger.warning(
                    f"[롯데ON] getSROrderList 구조 미파악: data 키={list(data.keys()) if isinstance(data, dict) else type(data)}"
                )
            except Exception as e:
                logger.warning(f"[롯데ON] getSROrderList 실패: {e}")
            return []

        sr_orders, delivery_orders, progress_orders = await asyncio.gather(
            _get_sr_orders(),
            self.get_delivery_orders(days=days),
            self.get_delivery_progress_states(days=days),
        )

        # 중복 제거: (odNo, odSeq) 기준 — procSeq는 API/상태에 따라 달라지므로 제외
        # progress_orders 우선: 같은 주문이 delivery에서 구 상태(11)로, progress에서 현재 상태(12)로 올 때
        # progress를 먼저 처리해야 최신 상태가 보존됨
        seen: set[tuple] = set()
        merged: list[dict] = []
        for item in progress_orders:
            key = (item.get("odNo"), item.get("odSeq"))
            if key not in seen:
                seen.add(key)
                merged.append(item)
        for item in sr_orders:
            key = (item.get("odNo"), item.get("odSeq"))
            if key not in seen:
                seen.add(key)
                merged.append(item)
        for item in delivery_orders:
            key = (item.get("odNo"), item.get("odSeq"))
            if key not in seen:
                seen.add(key)
                merged.append(item)

        logger.info(
            f"[롯데ON] 병행 조회 완료: getSROrderList={len(sr_orders)}건, delivery={len(delivery_orders)}건, progress={len(progress_orders)}건, 최종={len(merged)}건"
        )
        return merged

    async def get_claims(self, days: int = 7) -> list[dict[str, Any]]:
        """최근 N일 클레임(반품/교환/취소) 목록 조회.

        문서 확인된 경로:
        - 반품: returningOpenApi/returnRequestSearch
        - 교환: exchangeOpenApi/exchangeSearch
        - 취소: cancellationOpenApi/getCancellationRequestAndComplateList
             + cancellationOpenApi/purFvrCnclSearch (구매자 취소)
        """
        from datetime import timedelta

        now = now_kst()
        start = (now - timedelta(days=days)).strftime("%Y%m%d")
        end = now.strftime("%Y%m%d")

        body: dict[str, Any] = {
            "trGrpCd": self.tr_grp_cd or "SR",
            "trNo": self.tr_no,
            "srchStDt": start,
            "srchEdDt": end,
            "pageNo": 1,
            "pageSize": 100,
        }

        # (경로, 클레임 타입) 쌍 — 타입을 각 아이템에 주입해 구분
        claim_endpoints = [
            ("/v1/openapi/claim/v1/returningOpenApi/returnRequestSearch", "RETURN"),
            ("/v1/openapi/claim/v1/exchangeOpenApi/exchangeSearch", "EXCHANGE"),
            (
                "/v1/openapi/claim/v1/cancellationOpenApi/getCancellationRequestAndComplateList",
                "CANCEL",
            ),
            ("/v1/openapi/claim/v1/cancellationOpenApi/purFvrCnclSearch", "CANCEL"),
        ]
        all_claims: list[dict[str, Any]] = []
        for path, claim_type in claim_endpoints:
            try:
                r = await self._call_api("POST", path, body=body)
                logger.info(f"[롯데ON] 클레임 API 성공: {path}, 키: {list(r.keys())}")
                logger.info(f"[롯데ON] 클레임 응답 전체: {str(r)[:300]}")
                d = r.get("data") or r.get("list") or []
                if isinstance(d, dict):
                    d = (
                        d.get("list")
                        or d.get("content")
                        or d.get("claimList")
                        or d.get("items")
                        or []
                    )
                if isinstance(d, list) and d:
                    for item in d:
                        if isinstance(item, dict):
                            item.setdefault("_claimType", claim_type)
                    all_claims.extend(d)
                    logger.info(f"[롯데ON] {claim_type} 클레임 {len(d)}건")
            except LotteonApiError as e:
                err_str = str(e)
                if "404" in err_str or "403" in err_str:
                    logger.info(f"[롯데ON] 클레임 API {err_str[:20]} — 건너뜀: {path}")
                    continue
                raise

        logger.info(f"[롯데ON] 클레임 총 {len(all_claims)}건 수집")
        return all_claims

    async def get_cs_inquiries(self, days: int = 30) -> list[dict[str, Any]]:
        """CS 문의(QnA) 목록 조회 — 엔드포인트 자동 탐색."""
        from datetime import timedelta

        now = now_kst()
        start = (now - timedelta(days=days)).strftime("%Y%m%d")
        end = now.strftime("%Y%m%d")

        body: dict[str, Any] = {
            "trGrpCd": self.tr_grp_cd or "SR",
            "trNo": self.tr_no,
            "srchStDt": start,
            "srchEdDt": end,
            "pageNo": 1,
            "pageSize": 100,
        }
        candidate_paths = [
            "/v1/openapi/qna/v1/qna/list",
            "/v1/openapi/cs/v1/qna/list",
            "/v1/openapi/cs/v1/inquiry/list",
            "/v1/openapi/qna/v1/qnas",
        ]
        result: dict[str, Any] = {}
        for path in candidate_paths:
            try:
                result = await self._call_api("POST", path, body=body)
                logger.info(f"[롯데ON] CS문의 API 성공 경로: {path}")
                break
            except LotteonApiError as e:
                if "404" in str(e):
                    logger.info(f"[롯데ON] CS문의 API 404 — 다음 경로 시도: {path}")
                    continue
                raise
        if not result:
            logger.warning("[롯데ON] CS문의 API — 모든 후보 경로 404")
            return []
        data = result.get("data") or result.get("qnaList") or result.get("list") or []
        if isinstance(data, dict):
            data = data.get("qnaList") or data.get("list") or data.get("content") or []
        return data if isinstance(data, list) else []

    async def reply_cs_inquiry(self, qna_no: str, content: str) -> dict[str, Any]:
        """CS 문의 답변 등록.

        롯데ON QnA 답변 API: POST /v1/openapi/qna/v1/qna/answer
        """
        body: dict[str, Any] = {
            "trGrpCd": self.tr_grp_cd or "SR",
            "trNo": self.tr_no,
            "qnaNo": qna_no,
            "answerContent": content,
        }
        return await self._call_api(
            "POST",
            "/v1/openapi/qna/v1/qna/answer",
            body=body,
        )

    # ------------------------------------------------------------------
    # 카테고리 / 브랜드 (onpick-api 도메인)
    # ------------------------------------------------------------------

    async def get_categories(
        self,
        cat_id: str = "",
        depth: str = "",
        parent_id: str = "",
        skip: int = 0,
        limit: int = 500,
        _shared_client: Optional[Any] = None,
    ) -> dict[str, Any]:
        """표준카테고리 조회 (onpick-api 도메인).

        Args:
          cat_id: filter_1 — 특정 카테고리 ID 조회
          depth: filter_3 — 뎁스 레벨 (1~4)
          parent_id: filter_2 — 부모 카테고리 ID로 하위 목록 조회
          skip: 페이지네이션 시작 위치
          limit: 페이지당 건수 (최대 500)
          _shared_client: 대량 조회 시 TCP 연결 재사용용 httpx 클라이언트
        """
        params: dict[str, str] = {
            "job": "cheetahStandardCategory",
            "skip": str(skip),
            "limit": str(limit),
        }
        if cat_id:
            params["filter_1"] = cat_id
        if parent_id:
            params["filter_2"] = parent_id
        if depth:
            params["filter_3"] = depth
        return await self._call_api(
            "GET",
            "/cheetah/econCheetah.ecn",
            params=params,
            base_url=self.ONPICK_URL,
            _shared_client=_shared_client,
        )

    async def get_category_attributes(self, scat_no: str) -> dict[str, Any]:
        """표준카테고리 속성목록 조회 (onpick-api 도메인).

        scatAttrLst 구성에 필요한 optCd / optValCd 조회.
        """
        return await self._call_api(
            "GET",
            "/cheetah/econCheetah.ecn",
            params={"job": "cheetahScatAttr", "mf_1": scat_no},
            base_url=self.ONPICK_URL,
        )

    async def get_category_attribute_list(self, category_id: str) -> dict[str, Any]:
        """표준카테고리 속성목록 조회 — 메인 API 경로 시도."""
        return await self._call_api(
            "GET",
            "/v1/openapi/product/v1/category/attribute/list",
            params={"scatNo": category_id},
        )

    async def search_brand(self, keyword: str) -> dict[str, Any]:
        """브랜드 검색 (onpick-api 도메인)."""
        return await self._call_api(
            "GET",
            "/cheetah/econCheetah.ecn",
            params={"job": "cheetahBrnd", "mf_1": keyword},
            base_url=self.ONPICK_URL,
        )

    # ------------------------------------------------------------------
    # 상품 데이터 변환
    # ------------------------------------------------------------------

    @staticmethod
    def transform_product(
        product: dict[str, Any],
        category_id: str = "",
        tr_grp_cd: str = "SR",
        tr_no: str = "",
        disp_cat_id: str = "",
    ) -> dict[str, Any]:
        """SambaCollectedProduct → 롯데ON 상품 등록 데이터 변환.

        Args:
          category_id: 표준카테고리번호 (BC...)
          disp_cat_id: 전시카테고리번호 (FC...) — 없으면 category_id 사용
        """
        from backend.utils.logger import logger as _log

        # ── 이미지 URL 정규화 ──────────────────────────────────────
        import re as _re

        _IMG_EXT_RE = _re.compile(r"\.(jpe?g|png|gif|webp|bmp)", _re.IGNORECASE)

        def _normalize_url(url: str) -> str:
            if url.startswith("//"):
                return "https:" + url
            return url

        raw_images = product.get("images") or []
        images = [
            _normalize_url(u)
            for u in raw_images
            if u
            and (u.startswith("http") or u.startswith("//"))
            and _IMG_EXT_RE.search(u)  # 이미지 확장자 없는 추적/로거 URL 제외
        ][:10]
        _log.info(
            f"[롯데ON] 이미지: 원본 {len(raw_images)}개 → 정규화 {len(images)}개"
            f" (비이미지 URL {len(raw_images) - len(images)}개 제외)"
        )

        # ── 기본 상품 정보 ──────────────────────────────────────────
        sale_price = int(product.get("sale_price", 0))
        name = _truncate_to_bytes((product.get("name", "") or ""), 149)
        brand = product.get("brand", "") or ""
        # 제조사: manufacturer → brand → "제조사 미확인" 순 폴백
        manufacturer = product.get("manufacturer", "") or brand or "제조사 미확인"
        # 롯데ON mfcrNm 100Byte 제한 (UTF-8 기준)
        manufacturer = _truncate_to_bytes(manufacturer, 100)
        style_code = product.get("style_code", "") or product.get("styleCode", "") or ""
        origin = product.get("origin", "") or ""

        # 즉시할인 미적용 — sale_price 그대로 등록.
        # (이전: 25% + 이벤트 12% 역산으로 등록가를 부풀려 결제가가 의도와 어긋남.
        #  팀장 결정으로 즉시할인 자체 사용 안 함, 끝자리 1원 단위는 calc_market_price에서 100원 내림으로 정리.)

        # ── 재고 / 배송비 ───────────────────────────────────────────
        # _stock_quantity: 양수면 옵션별 상한(cap)으로만 동작.
        # 0/미설정이면 무신사 실재고(raw_stock) 그대로 사용.
        # default_stock은 옵션이 없는 단품(else 분기)에서만 폴백 용도.
        max_stock = int(product.get("_stock_quantity") or 0)
        default_stock = max_stock if max_stock > 0 else 999
        return_fee = product.get("_return_fee", 0) or 0
        exchange_fee = product.get("_exchange_fee", 0) or 0
        jeju_fee = product.get("_jeju_fee", 0) or 0

        # ── 판매 기간 ───────────────────────────────────────────────
        now = now_kst()
        sl_strt = now.strftime("%Y%m%d%H%M%S")
        sl_end = (now + timedelta(days=365)).strftime("%Y%m%d%H%M%S")

        # ── 옵션에서 사이즈/색상 추출 (고시정보용) ──────────────────
        options = product.get("options") or []
        sizes = [
            o.get("size", "") or o.get("name", "")
            for o in options
            if o.get("size") or o.get("name")
        ]
        size_text = (
            ", ".join(sorted(set(s for s in sizes if s)))[:200] or "상세페이지 참조"
        )
        db_color = product.get("color", "")
        color_part = ""
        if " - " in (product.get("name") or ""):
            color_part = product["name"].split(" - ", 1)[1].split("/")[0].strip()
        color_text = db_color or (color_part[:200] if color_part else "상세페이지 참조")

        # ── 이미지 파일 목록 (origFileNm, origImgFileNm 모두 URL 필수) ─
        pd_file_lst = [
            {
                "fileTypCd": "PD",
                "fileDvsCd": "WDTH",
                "origFileNm": url,
                "origImgFileNm": url,
            }
            for url in images
        ]

        # ── 단품 이미지 목록 ────────────────────────────────────────
        itm_img_lst = [
            {
                "epsrTypCd": "IMG",
                "epsrTypDtlCd": "IMG_SQRE",
                "origFileNm": url,
                "origImgFileNm": url,
                "rprtImgYn": "Y" if idx == 0 else "N",
            }
            for idx, url in enumerate(images)
        ]

        # ── 옵션 타입 감지 ──────────────────────────────────────────
        def _detect_opt_nm(opt: dict[str, Any]) -> str:
            """옵션 타입 자동 감지 (색상/사이즈/기타)."""
            keys = set(opt.keys())
            if "color" in keys or any("color" in str(k).lower() for k in keys):
                return "색상"
            if "size" in keys or any("size" in str(k).lower() for k in keys):
                return "사이즈"
            val = opt.get("name", "") or opt.get("value", "") or ""
            size_keywords = {
                "S",
                "M",
                "L",
                "XL",
                "XXL",
                "XS",
                "FREE",
                "프리",
                "스몰",
                "라지",
            }
            if val.strip().upper() in size_keywords or val.replace(".", "").isdigit():
                return "사이즈"
            return "옵션"

        # ── 단품(옵션) 목록 ─────────────────────────────────────────
        # 스마트스토어 `_build_combination_options` 패턴(proxy/smartstore.py:270~)을
        # 그대로 포팅 — isSoldOut 반영, 실재고 그대로, 상한 있을 때만 cap.
        # 2단 옵션(" / " 패턴): itmOptLst를 2개 항목으로 분리하고 slPrc를 옵션별 차등 적용.
        itm_lst: list[dict[str, Any]] = []
        opt_srt_lst: list[dict[str, Any]] = []
        if options:
            # 2단 옵션 판별 — " / "(공백+슬래시+공백) 패턴이 있어야 진짜 2단
            has_slash = any(" / " in (o.get("name") or "") for o in options)

            # 그룹명(차원명) 결정
            if has_slash:
                first_opt_nm = "옵션1"
                second_opt_nm = "옵션2"
            else:
                first_opt_nm = "옵션1"
                second_opt_nm = ""

            # 옵션별 추가금 산정용 base — 활성 옵션 중 최저가
            _active_opt_prices = [
                int(o.get("price") or 0)
                for o in options
                if int(o.get("price") or 0) > 0
                and not o.get("isSoldOut", False)
                and (o.get("stock") or 0) > 0
            ]
            _diff_base = (
                min(_active_opt_prices) if _active_opt_prices else int(sale_price or 0)
            )

            # optSrtLst 차원별 유니크 값(등장 순서 유지)
            _dim1_vals: list[str] = []
            _dim2_vals: list[str] = []

            for idx, opt in enumerate(options):
                opt_name = (
                    opt.get("name", "")
                    or opt.get("size", "")
                    or opt.get("value", "")
                    or f"옵션{idx + 1}"
                )

                # 2단 분리
                if has_slash and " / " in opt_name:
                    parts = [p.strip() for p in opt_name.split(" / ", 1)]
                    parts = [p for p in parts if p]
                    if len(parts) == 2:
                        opt_val_1, opt_val_2 = parts[0], parts[1]
                    elif len(parts) == 1:
                        opt_val_1, opt_val_2 = parts[0], ""
                    else:
                        opt_val_1, opt_val_2 = opt_name, ""
                else:
                    opt_val_1, opt_val_2 = opt_name, ""

                # ── 스마트스토어 패턴: isSoldOut → stock=0, 실재고 그대로 ──
                raw_stock = opt.get("stock", 0) or 0
                sold_out = bool(opt.get("isSoldOut", False))
                if sold_out:
                    raw_stock = 0

                if max_stock > 0:
                    opt_stock = min(max(int(raw_stock), 0), max_stock)
                else:
                    opt_stock = max(int(raw_stock), 0)

                # 품절/재고0 옵션은 미노출(dpYn=N) — smartstore의 usable=False 대응
                dp_yn = "N" if (sold_out or opt_stock == 0) else "Y"

                # 옵션별 가격 — 추가금만큼 가산 (price_diff = opt.price - _diff_base)
                opt_price = int(opt.get("price") or 0)
                price_diff = max(opt_price - _diff_base, 0) if opt_price > 0 else 0
                itm_sl_prc = int(sale_price) + price_diff

                itm_opt_lst: list[dict[str, str]] = [
                    {"optNm": first_opt_nm, "optVal": opt_val_1}
                ]
                if has_slash and opt_val_2:
                    itm_opt_lst.append({"optNm": second_opt_nm, "optVal": opt_val_2})

                itm_lst.append(
                    {
                        "eitmNo": f"OPT{idx}",
                        "dpYn": dp_yn,
                        "sortSeq": idx + 1,
                        "itmOptLst": itm_opt_lst,
                        "itmImgLst": itm_img_lst,
                        "slPrc": itm_sl_prc,
                        "stkQty": opt_stock,
                    }
                )

                # optSrtLst 누적
                if opt_val_1 and opt_val_1 not in _dim1_vals:
                    _dim1_vals.append(opt_val_1)
                if has_slash and opt_val_2 and opt_val_2 not in _dim2_vals:
                    _dim2_vals.append(opt_val_2)

            # optSrtLst 블록 구성 (itmOptLst 차원과 1:1 일치 필수)
            opt_srt_lst.append(
                {
                    "optSeq": 1,
                    "optNm": first_opt_nm,
                    "optValSrtLst": [
                        {"optValSeq": i + 1, "optVal": v}
                        for i, v in enumerate(_dim1_vals)
                    ],
                }
            )
            if has_slash and _dim2_vals:
                opt_srt_lst.append(
                    {
                        "optSeq": 2,
                        "optNm": second_opt_nm,
                        "optValSrtLst": [
                            {"optValSeq": i + 1, "optVal": v}
                            for i, v in enumerate(_dim2_vals)
                        ],
                    }
                )

            # 전 옵션 품절 가드 — 호출자가 sold_out 승격으로 차단해야 함
            if all((itm["stkQty"] == 0 or itm["dpYn"] == "N") for itm in itm_lst):
                _log.warning(
                    "[롯데ON] 전 옵션 품절 상태로 transform_product 호출됨 — 상위에서 sold_out 처리 필요"
                )

            if has_slash:
                _log.info(
                    f"[롯데ON] 2단 옵션 등록: {first_opt_nm}({len(_dim1_vals)}) × "
                    f"{second_opt_nm}({len(_dim2_vals)}) — 총 {len(itm_lst)}개 단품, "
                    f"슬프 범위 {min(i['slPrc'] for i in itm_lst):,}~{max(i['slPrc'] for i in itm_lst):,}"
                )
        else:
            # itmOptLst 키 생략 — 빈 배열 [] 은 롯데ON API 9999 에러 발생
            itm_lst.append(
                {
                    "eitmNo": "OPT0",
                    "dpYn": "Y",
                    "sortSeq": 1,
                    "itmImgLst": itm_img_lst,
                    "slPrc": sale_price,
                    "stkQty": default_stock,
                }
            )

        # ── 상세설명 ────────────────────────────────────────────────
        detail_html = product.get("detail_html", "") or f"<p>{name}</p>"

        # ── SEO: 검색 키워드 / 상품 소개문 ─────────────────────────
        keywords = _build_lotteon_keywords(product)
        intro = _build_lotteon_intro(product)

        # ── 고시정보 (실제 수집 데이터 주입) ───────────────────────
        notice = _build_lot_notice(
            product,
            size_text=size_text,
            color_text=color_text,
            mfr=manufacturer,
        )

        # ── 원산지 코드 동적 매핑 ───────────────────────────────────
        origin_code = _get_lotteon_origin_code(origin)

        spd: dict[str, Any] = {
            "trGrpCd": tr_grp_cd,
            "trNo": tr_no,
            "scatNo": category_id,
            # 전시카테고리(FC...) 있으면 사용, 없으면 표준카테고리 fallback
            "dcatLst": [{"mallCd": "LTON", "lfDcatNo": disp_cat_id or category_id}],
            "slTypCd": "GNRL",
            "pdTypCd": "GNRL_GNRL",
            "spdNm": name,
            # 브랜드번호 — 브랜드 API 검색 후 주입 (없으면 무브랜드)
            "brdNo": product.get("brand_no", ""),
            # 제조사: manufacturer 우선, 없으면 brand
            "mfcrNm": manufacturer,
            # 원산지: 무신사 origin 필드 기반 ISO alpha-2 코드
            "oplcCd": origin_code,
            "tdfDvsCd": "01",
            # 판매 기간
            "slStrtDttm": sl_strt,
            "slEndDttm": sl_end,
            # 출고지/배송비정책/회수지/도서산간추가배송정책 (응답 확인: adtnDvCstPolNo)
            "owhpNo": product.get("owhp_no", ""),
            "dvCstPolNo": product.get("dv_cst_pol_no", ""),
            "adtnDvCstPolNo": product.get("island_dv_cst_pol_no", "") or None,
            "rtrpNo": product.get("rtrp_no", ""),
            # 선물포장/메시지
            "prstPckPsbYn": "N",
            "prstMsgPsbYn": "N",
            "pdItmsInfo": notice,
            "purPsbQtyInfo": {
                "itmByMinPurYn": "N",
                "itmByMaxPurPsbQtyYn": "N",
                "maxPurLmtTypCd": "PERIOD",
            },
            "ageLmtCd": "0",
            "prcCmprEpsrYn": "Y",
            "pdStatCd": "NEW",
            "dpYn": "Y",
            "pdFileLst": pd_file_lst if pd_file_lst else None,
            # 상세설명 (A/S 안내는 고시정보 pdArtlCd=0090으로 전달)
            "epnLst": [
                {"pdEpnTypCd": "DSCRP", "cnts": detail_html},
            ],
            "cnclPsbYn": "Y",
            "dmstOvsDvDvsCd": "DMST",
            "impDvsCd": "NONE",  # 해당없음 (국내제작 기본값, 직수입 불허 카테고리 대응)
            "dvProcTypCd": "LO_ENTP",
            "dvPdTypCd": "GNRL",
            "dvRsvDvsCd": "GNRL_DV",
            "sndBgtNday": product.get("_dispatch_days", 2),
            "dvMnsCd": "DPCL",
            # 기본값 "N" — plugin에서 bundleDelivery 설정값 미주입 시에도 합배송 불가로 보수적 처리
            "cmbnDvPsbYn": product.get("cmbn_dv_psb_yn", "N"),
            "cmbnRtngPsbYn": product.get("cmbn_dv_psb_yn", "N"),
            "rtngPsbYn": "Y",
            "xchgPsbYn": "Y",
            **({"rtngFee": return_fee} if return_fee else {}),
            **({"xchgFee": exchange_fee} if exchange_fee else {}),
            **({"islandAddDlvFee": jeju_fee} if jeju_fee else {}),
            "stkMgtYn": "Y",
            "sitmYn": "Y" if options else "N",
            "itmLst": itm_lst,
            **({"optSrtLst": opt_srt_lst} if opt_srt_lst else {}),
            "rtrvTypCd": "ENTP_RTRV",
            "dvRgsprGrpCd": "GN000",
        }

        # ── SEO: 검색 키워드 (있을 때만) ────────────────────────────
        if keywords:
            spd["spdKeyword"] = keywords

        # ── SEO: 상품 소개문 (있을 때만) ────────────────────────────
        if intro:
            spd["pdIntrdCnts"] = intro

        # ── 판매자 상품코드 (품번 있을 때만) ────────────────────────
        if style_code:
            spd["selPrdNo"] = style_code[:50]

        # ── 기타정보 ─────────────────────────────────────────────────
        # 모델번호: 품번(style_code) 활용
        if style_code:
            spd["mdlNo"] = style_code[:50]
        # 온누리상품권 결제가능여부: 기본 사용안함 (응답 확인: onnuriPyPsbYn)
        spd["onnuriPyPsbYn"] = "N"
        # 임직원상품 여부: 기본 해당없음
        spd["empPrdYn"] = "N"
        # 출시년월(rlsYm): 롯데ON Open API 미지원 필드 — 파트너센터에서 수동 입력 필요
        # 제품/포장 사이즈: pdSzInfo 하나의 객체에 모두 포함 (API 응답 확인)
        spd["pdSzInfo"] = {
            "pdWdthSz": 29,  # 제품 가로 (cm)
            "pdLnthSz": 20,  # 제품 세로 (cm)
            "pdHghtSz": 16,  # 제품 높이 (cm)
            "pckWdthSz": 34,  # 포장 가로 (cm)
            "pckLnthSz": 25,  # 포장 세로 (cm)
            "pckHghtSz": 21,  # 포장 높이 (cm)
        }

        # ── 카테고리 속성정보 (scatAttrLst) ─────────────────────────
        # _scat_attr_lst: [{"optCd": attr_id, "optValCd": attr_val_id}, ...]
        # scatAttrChgYn: 수정 API에서 속성정보 변경 여부 명시 플래그 (필요 시)
        scat_attr_lst = product.get("_scat_attr_lst") or []
        if scat_attr_lst:
            spd["scatAttrLst"] = scat_attr_lst
            spd["scatAttrChgYn"] = "Y"

        # ── 상품홍보문구 — 자동 설정 불가
        # - OpenAPI 페이로드에 포함 시 무시됨 (200 OK 반환하지만 미반영)
        # - soapi updateProduct → 403 (API key 권한 없음, 브라우저 세션 전용)
        # 롯데ON 어드민에서 수동 설정 필요
        return {"spdLst": [spd]}

    # ------------------------------------------------------------------
    # 날짜 범위 helper
    # ------------------------------------------------------------------

    def _datetime_range(self, days: int) -> tuple[str, str]:
        """최근 N일 범위를 yyyymmddHHmmss 형식으로 반환 (최대 30일)."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=min(days, 30))
        fmt = "%Y%m%d%H%M%S"
        return start.strftime(fmt), now.strftime(fmt)

    # ------------------------------------------------------------------
    # 주문 조회
    # ------------------------------------------------------------------

    async def get_delivery_orders(self, days: int = 7) -> list[dict]:
        """최근 N일 배송 주문 조회 (SellerDeliveryOrdersSearch).

        API 제약: 조회 기간 1일 초과 불가 → 하루씩 병렬 조회 (동시 5건 제한).
        """
        import asyncio

        KST = timezone(timedelta(hours=9))
        now = datetime.now(KST)
        actual_days = min(days, 30)
        sem = asyncio.Semaphore(5)

        async def _fetch_day(offset: int) -> list[dict]:
            base_date = (now - timedelta(days=offset)).date()
            day_start = datetime(
                base_date.year, base_date.month, base_date.day, 0, 0, 0
            )
            day_end = datetime(
                base_date.year, base_date.month, base_date.day, 23, 59, 59
            )
            srch_strt = day_start.strftime("%Y%m%d%H%M%S")
            srch_end = day_end.strftime("%Y%m%d%H%M%S")
            async with sem:
                # ifCplYN 없음(신규 미연동) + "Y"(연동완료) 두 번 조회 후 합산
                # → 플레이오토 등 외부에서 연동완료 통보된 주문도 수집
                combined: list[dict] = []
                seen_keys: set[tuple] = set()
                for if_cpl in (None, "Y"):
                    body: dict = {"srchStrtDt": srch_strt, "srchEndDt": srch_end}
                    if self.tr_no:
                        body["trNo"] = (
                            self.tr_no
                        )  # 계정 trNo로 필터링 — 타 계정 주문 수집 차단
                    if if_cpl:
                        body["ifCplYN"] = if_cpl
                    try:
                        data = await self._call_api(
                            "POST",
                            "/v1/openapi/delivery/v1/SellerDeliveryOrdersSearch",
                            body=body,
                        )
                        inner = data.get("data") or {}
                        items = inner.get("deliveryOrderList") or []
                        if isinstance(items, list):
                            for item in items:
                                # procSeq는 ifCplYN에 따라 달라질 수 있으므로 제외
                                key = (item.get("odNo"), item.get("odSeq"))
                                if key not in seen_keys:
                                    seen_keys.add(key)
                                    combined.append(item)
                    except Exception as e:
                        logger.warning(
                            f"[롯데ON] 주문 조회 실패 ({srch_strt}~{srch_end}, ifCplYN={if_cpl}): {e}"
                        )
                logger.info(
                    f"[롯데ON][주문] {srch_strt}~{srch_end} deliveryOrderList={len(combined)}"
                )
                return combined

        day_results = await asyncio.gather(*[_fetch_day(i) for i in range(actual_days)])
        result: list[dict] = []
        for items in day_results:
            result.extend(items)
        return result

    async def get_delivery_progress_states(self, days: int = 7) -> list[dict]:
        """배송 진행 상태 조회 (SellerDeliveryProgressStateSearch).

        API 제약: 조회 기간 1일 초과 불가 → 하루씩 병렬 조회 (동시 5건 제한).
        get_orders() 병합 + 기존 주문 상태 갱신 두 용도로 사용.
        getSROrderList가 제외하는 상품준비(12) 이후 주문을 여기서 수집.
        """
        import asyncio

        KST = timezone(timedelta(hours=9))
        now = datetime.now(KST)
        actual_days = min(days, 30)
        sem = asyncio.Semaphore(5)

        async def _fetch_day(offset: int) -> list[dict]:
            base_date = (now - timedelta(days=offset)).date()
            day_start = datetime(
                base_date.year, base_date.month, base_date.day, 0, 0, 0
            )
            day_end = datetime(
                base_date.year, base_date.month, base_date.day, 23, 59, 59
            )
            srch_strt = day_start.strftime("%Y%m%d%H%M%S")
            srch_end = day_end.strftime("%Y%m%d%H%M%S")
            async with sem:
                try:
                    data = await self._call_api(
                        "POST",
                        "/v1/openapi/delivery/v1/SellerDeliveryProgressStateSearch",
                        body={"srchStrtDt": srch_strt, "srchEndDt": srch_end},
                    )
                    inner = data.get("data") or {}
                    items = inner.get("deliveryProgressStateList") or []
                    if isinstance(items, list) and items:
                        return items
                    return []
                except Exception as e:
                    logger.warning(
                        f"[롯데ON] 배송상태 조회 실패 ({srch_strt}~{srch_end}): {e}"
                    )
                    return []

        day_results = await asyncio.gather(*[_fetch_day(i) for i in range(actual_days)])
        result: list[dict] = []
        for items in day_results:
            result.extend(items)
        logger.info(f"[롯데ON] 배송상태 조회 완료: {len(result)}건")
        return result

    async def get_settlement_items(self, days: int = 7) -> list[dict]:
        """개별거래처 매출 정산 조회 (SettleItmdSales).

        pymtAmt(지급대상금액)가 실제 정산금액.
        주문과 (odNo, odSeq, procSeq) 키로 매칭하여 revenue/fee_rate 계산에 사용.
        """
        KST = timezone(timedelta(hours=9))
        now = datetime.now(KST)
        actual_days = min(days, 30)
        start_date = (now - timedelta(days=actual_days - 1)).strftime("%Y%m%d")
        end_date = now.strftime("%Y%m%d")
        try:
            resp = await self._call_api(
                "POST",
                "/v1/openapi/settle/v1/se/SettleItmdSales",
                body={"startDate": start_date, "endDate": end_date},
            )
            data = resp.get("data") or []
            if not isinstance(data, list):
                data = []
            logger.info(
                f"[롯데ON][정산] {start_date}~{end_date} SettleItmdSales={len(data)}건"
            )
            # 매칭 키 진단: 첫 1건의 키 형식 출력 (odNo/odSeq/procSeq 비교용)
            if data:
                _s = data[0]
                logger.info(
                    f"[롯데ON][정산] 샘플 키: odNo={_s.get('odNo')} "
                    f"odSeq={_s.get('odSeq')} procSeq={_s.get('procSeq')}"
                )
                logger.info(f"[롯데ON][정산] 샘플 전체 필드: {list(_s.keys())}")
            return data
        except Exception as e:
            logger.warning(f"[롯데ON][정산] 조회 실패 ({start_date}~{end_date}): {e}")
            return []

    # 판매자 취소 사유코드 (롯데ON 3자리 숫자 + 스마트스토어 영문코드 호환)
    SELLER_CANCEL_REASON_CODES: dict[str, str] = {
        # 롯데ON 내부 키 (레거시)
        "soldout": "111",
        "price": "132",
        "reseller": "133",
        "delivery": "137",
        "customer": "135",
        # 스마트스토어 호환 키 (프론트가 마켓 구분 없이 보내는 값)
        "SOLD_OUT": "111",  # 품절
        "PRICE_FLUCTUATION": "132",  # 가격오등록
        "INTENT_CHANGED": "135",  # 고객변심
        "WRONG_ORDER": "135",  # 잘못된주문 → 고객변심
        "DELAYED_DELIVERY": "137",  # 배송지연 → 택배불가
    }

    async def seller_cancel_order(
        self,
        od_no: str,
        reason_code: str = "111",
        reason_text: str = "",
        od_seq: int = 1,
        proc_seq: int = 1,
    ) -> tuple[bool, str]:
        """판매자 주도 주문 취소 (slrDirectCnclProc).

        Args:
            od_no: 주문번호
            reason_code: 판매자 사유코드 (111=품절, 132=가격오등록, 133=리셀러, 135=고객변심, 137=택배불가)
                         또는 스마트스토어 영문코드(SOLD_OUT/INTENT_CHANGED 등)도 허용
            reason_text: 판매자 사유 내용 (선택)
            od_seq: 주문순번 (기본 1)
            proc_seq: 처리순번 (기본 1)

        Returns:
            (성공여부, 메시지)
        """
        # 3자리 숫자가 아니면 매핑 딕셔너리로 변환 (에러 3073 방어)
        if not (reason_code.isdigit() and len(reason_code) == 3):
            mapped = self.SELLER_CANCEL_REASON_CODES.get(reason_code)
            if mapped:
                logger.info(
                    f"[롯데ON][판매자취소] 사유코드 매핑: {reason_code} → {mapped}"
                )
                reason_code = mapped
            else:
                logger.warning(
                    f"[롯데ON][판매자취소] 알 수 없는 사유코드 '{reason_code}', 기본값 111(품절) 사용"
                )
                reason_code = "111"
        payload = {
            "odNo": od_no,
            "itemList": [
                {
                    "odSeq": od_seq,
                    "procSeq": proc_seq,
                    "slrRsnCd": reason_code,
                    "slrRsnCnts": reason_text or "판매자 취소",
                    "lrtrNo": "",
                }
            ],
        }
        logger.info(
            f"[롯데ON][판매자취소] odNo={od_no} odSeq={od_seq} procSeq={proc_seq} "
            f"rsnCd={reason_code} rsnText={reason_text}"
        )
        try:
            resp = await self._call_api(
                "POST",
                "/v1/openapi/claim/v1/cancellationOpenApi/slrDirectCnclProc",
                body=payload,
            )
            data = resp.get("data") or resp
            return_code = str(data.get("returnCode") or data.get("rsltCd") or "")
            message = str(data.get("message") or data.get("rsltMsg") or "")
            success = return_code == "0000"
            logger.info(
                f"[롯데ON][판매자취소] 응답: odNo={od_no} returnCode={return_code} message={message}"
            )
            return success, message or ("정상 처리" if success else "실패")
        except Exception as e:
            err_msg = str(e)
            # 3006 = "주문의 상태를 확인해 주세요" — 같은 odNo의 다른 옵션이 먼저 취소되어
            # 롯데ON 쪽에서는 이미 전체 주문이 취소 상태. 삼바 DB 동기화를 위해 성공으로 처리.
            # _call_api가 "응답 에러 (3006): ..." 형식으로 LotteonApiError를 던지므로 메시지로 구분.
            if "(3006)" in err_msg:
                logger.info(
                    f"[롯데ON][판매자취소] 이미 취소된 주문 (3006): odNo={od_no}"
                )
                return True, "이미 취소된 주문"
            logger.warning(f"[롯데ON][판매자취소] 실패: odNo={od_no} / {err_msg}")
            return False, err_msg

    async def confirm_orders(self, order_items: list[dict]) -> bool:
        """주문확인 = 연동완료 처리 (SellerIfCompleteInform, ifCplYN=Y).

        롯데ON 판매자센터 "신규주문" 목록은 ifCplYN=N 기준.
        ifCplYN=Y 로 통보해야 신규주문에서 제거되고 주문확인 처리됨.
        """
        items = []
        for item in order_items:
            entry = {
                "dvRtrvDvsCd": "DV",
                "odNo": str(item.get("odNo", "")),
                "odSeq": int(item.get("odSeq", 1) or 1),
                "procSeq": int(item.get("procSeq", 1) or 1),
                "ifCplYN": "Y",
                "ifFlRsnCnts": "",
            }
            clm_no = item.get("clmNo") or ""
            if clm_no:
                entry["clmNo"] = str(clm_no)
                entry["orglProcSeq"] = int(item.get("orglProcSeq", 1) or 1)
            items.append(entry)
        try:
            resp = await self._call_api(
                "POST",
                "/v1/openapi/delivery/v1/SellerIfCompleteInform",
                body={"ifCompleteList": items},
            )
            rs = (resp.get("data") or {}).get("rsltCd", "") or resp.get(
                "returnCode", ""
            )
            logger.info(
                f"[롯데ON] 주문확인(SellerIfCompleteInform) 응답: rsltCd={rs} count={len(items)}"
            )
            return str(rs) == "0000"
        except Exception as e:
            logger.warning(f"[롯데ON] 주문확인 실패: {e}")
            return False

    # 택배사 한글명 → 롯데ON dvCoCd 매핑 (SellerDeliveryProgressStateInform 기준)
    DELIVERY_COMPANY_MAP: dict[str, str] = {
        "CJ대한통운": "0002",
        "한진택배": "0006",
        "롯데택배": "0001",
        "로젠택배": "0005",
        "우체국택배": "0004",
        "경동택배": "0019",
        "대신택배": "0011",
        "일양로지스": "0007",
        "편의점택배": "0016",
        "DHL": "0009",
        "딜리박스": "0159",
    }

    async def ship_order(
        self,
        od_no: str,
        sitm_no: str,
        spd_no: str,
        quantity: int,
        shipping_company: str,
        tracking_number: str,
        od_seq: str = "1",
        proc_seq: str = "1",
    ) -> bool:
        """발송처리 + 송장번호 등록 (SellerDeliveryProgressStateInform, odPrgsStepCd=13)."""
        KST = timezone(timedelta(hours=9))
        dv_co_cd = self.DELIVERY_COMPANY_MAP.get(shipping_company, shipping_company)
        dv_trc_stat_dttm = datetime.now(KST).strftime("%Y%m%d%H%M%S")

        # API 스펙: deliveryProgressStateList, odPrgsStepCd=13(발송완료), dvRtrvDvsCd=DV 필수
        payload = {
            "deliveryProgressStateList": [
                {
                    "dvRtrvDvsCd": "DV",
                    "odNo": od_no,
                    "odSeq": od_seq,
                    "procSeq": proc_seq,
                    "orglProcSeq": "",
                    "clmNo": "",
                    "odPrgsStepCd": "13",
                    "dvTrcStatDttm": dv_trc_stat_dttm,
                    "invcNbr": "1",
                    "dvCoCd": dv_co_cd,
                    "invcNo": tracking_number,
                    "spdNo": spd_no,
                    "spdNm": "",
                    "sitmNo": sitm_no,
                    "itmNm": "",
                    "itmSlPrc": "",
                    "slQty": str(quantity),
                    "dvTrcStatCd": "",
                    "dvArclNm": "",
                    "dvArclTelNo": "",
                    "dvLocCnts": "",
                    "eofcTelNo": "",
                }
            ]
        }
        logger.info(f"[롯데ON] 발송처리 요청: {payload}")
        try:
            resp_data = await self._call_api(
                "POST",
                "/v1/openapi/delivery/v1/SellerDeliveryProgressStateInform",
                body=payload,
            )
            rs = (resp_data.get("data") or {}).get("rsltCd", "")
            rm = (resp_data.get("data") or {}).get("rsltMsg", "")
            logger.info(f"[롯데ON] 발송처리 응답: rsltCd={rs} rsltMsg={rm}")
            if rs in ("0000", "00", "SUCCESS", ""):
                logger.info(
                    f"[롯데ON] 발송처리 완료: odNo={od_no} invcNo={tracking_number}"
                )
                return True
            logger.warning(f"[롯데ON] 발송처리 실패 rsltCd={rs}: {rm}")
            return False
        except Exception as e:
            logger.warning(f"[롯데ON] 발송처리 실패: odNo={od_no} / {e}")
            return False

    # ------------------------------------------------------------------
    # 취소/반품 클레임 조회
    # ------------------------------------------------------------------

    async def get_cancel_orders(self, days: int = 7) -> list[dict]:
        """최근 N일 취소 클레임 조회 (getCancellationRequestAndComplateList, clmTpCd=CCNL).

        응답: data[].itemList[] 중첩 구조 → odNo/clmNo 포함 flat list로 변환.
        """
        start_dt, end_dt = self._datetime_range(days)
        data = await self._call_api(
            "POST",
            "/v1/openapi/claim/v1/cancellationOpenApi/getCancellationRequestAndComplateList",
            body={
                "srchStrtDttm": start_dt,
                "srchEndDttm": end_dt,
                "clmTpCd": "CCNL",
            },
        )
        raw_list = data.get("data") or []
        if not isinstance(raw_list, list):
            raw_list = []
        result = []
        for claim in raw_list:
            od_no = claim.get("odNo", "")
            clm_no = claim.get("clmNo", "")
            step_cd = claim.get("odPrgsStepCd", "")
            logger.debug(
                f"[롯데ON][취소] odNo={od_no} clmNo={clm_no} stepCd={step_cd} "
                f"clmRsnCd={claim.get('clmRsnCd', '')} itemCount={len(claim.get('itemList') or [])}"
            )
            for item in claim.get("itemList") or []:
                item["odNo"] = od_no
                item["clmNo"] = clm_no
                logger.debug(
                    f"  └ sitmNo={item.get('sitmNo', '')} spdNm={item.get('spdNm', '')[:30]} "
                    f"cnclQty={item.get('cnclQty', '')} stepCd={item.get('odPrgsStepCd', '')}"
                )
                result.append(item)
        return result

    async def get_returns(self, days: int = 7) -> list[dict]:
        """최근 N일 반품 클레임 조회.

        1차: getCancellationRequestAndComplateList(clmTpCd=RETN)
        2차: getCancellationRequestAndComplateList(clmTpCd=EXCH)에서 clmRsnCd=300번대 건 보완 수집.

        롯데ON API 버그 대응: 반품 사유코드(300번대) 클레임이 EXCH API에는 나타나지만
        RETN API에는 누락되는 경우가 있어 EXCH API에서도 보완 수집한다.
        get_exchanges()에서는 이 건들을 이미 제외하므로 중복 처리 없음.
        """
        start_dt, end_dt = self._datetime_range(days)
        result: list[dict] = []
        seen_keys: set[str] = set()

        # 1차: RETN API
        data = await self._call_api(
            "POST",
            "/v1/openapi/claim/v1/cancellationOpenApi/getCancellationRequestAndComplateList",
            body={
                "srchStrtDttm": start_dt,
                "srchEndDttm": end_dt,
                "clmTpCd": "RETN",
            },
        )
        raw_list = data.get("data") or []
        if not isinstance(raw_list, list):
            raw_list = []
        for claim in raw_list:
            od_no = claim.get("odNo", "")
            clm_no = claim.get("clmNo", "")
            for item in claim.get("itemList") or []:
                item["odNo"] = od_no
                item["clmNo"] = clm_no
                key = f"{od_no}_{clm_no}_{item.get('odSeq', '')}"
                seen_keys.add(key)
                result.append(item)

        # 2차: EXCH API에서 clmRsnCd=300번대(반품 사유코드) 건 보완 수집
        # (롯데ON API 버그: 반품 사유 클레임이 EXCH 타입으로 잘못 분류되는 케이스 대응)
        try:
            data2 = await self._call_api(
                "POST",
                "/v1/openapi/claim/v1/cancellationOpenApi/getCancellationRequestAndComplateList",
                body={
                    "srchStrtDttm": start_dt,
                    "srchEndDttm": end_dt,
                    "clmTpCd": "EXCH",
                },
            )
            raw_list2 = data2.get("data") or []
            if not isinstance(raw_list2, list):
                raw_list2 = []
            for claim in raw_list2:
                od_no = claim.get("odNo", "")
                clm_no = claim.get("clmNo", "")
                for item in claim.get("itemList") or []:
                    clm_rsn_cd = str(item.get("clmRsnCd", "") or "")
                    if not clm_rsn_cd.startswith(("2", "3")):
                        continue  # 반품 사유코드(200/300번대)만 보완 대상
                    item["odNo"] = od_no
                    item["clmNo"] = clm_no
                    key = f"{od_no}_{clm_no}_{item.get('odSeq', '')}"
                    if key not in seen_keys:
                        seen_keys.add(key)
                        logger.warning(
                            f"[롯데ON][반품보완] EXCH API 반품 사유코드({clm_rsn_cd}) → 반품으로 재수집: "
                            f"odNo={od_no} clmNo={clm_no}"
                        )
                        result.append(item)
        except Exception as e:
            logger.warning(
                f"[롯데ON][get_returns] EXCH API 반품 보완 조회 실패 (무시): {e}"
            )

        # 3차: exchangeSearch API에서 clmRsnCd=300번대 건 보완 수집
        # (롯데ON API 버그: 반품 사유 클레임이 exchangeSearch에 포함되는 케이스 대응)
        try:
            data3 = await self._call_api(
                "POST",
                "/v1/openapi/claim/v1/exchangeOpenApi/exchangeSearch",
                body={
                    "srchStrtDttm": start_dt,
                    "srchEndDttm": end_dt,
                },
            )
            raw_list3 = data3.get("data") or []
            if not isinstance(raw_list3, list):
                raw_list3 = []
            for claim in raw_list3:
                od_no = claim.get("odNo", "")
                clm_no = claim.get("clmNo", "")
                for item in claim.get("itemList") or []:
                    clm_rsn_cd = str(item.get("clmRsnCd", "") or "")
                    if not clm_rsn_cd.startswith(("2", "3")):
                        continue  # 반품 사유코드(200/300번대)만 보완 대상
                    item["odNo"] = od_no
                    item["clmNo"] = clm_no
                    key = f"{od_no}_{clm_no}_{item.get('odSeq', '')}"
                    if key not in seen_keys:
                        seen_keys.add(key)
                        logger.warning(
                            f"[롯데ON][반품보완-exchangeSearch] 반품 사유코드({clm_rsn_cd}) → 반품으로 재수집: "
                            f"odNo={od_no} clmNo={clm_no}"
                        )
                        result.append(item)
        except Exception as e:
            logger.warning(
                f"[롯데ON][get_returns] exchangeSearch 반품 보완 조회 실패 (무시): {e}"
            )

        return result

    async def get_exchanges(self, days: int = 7) -> list[dict]:
        """최근 N일 교환 클레임 조회.

        1차: exchangeSearch API
        2차: getCancellationRequestAndComplateList(clmTpCd=EXCH)
        두 결과를 clmNo 기준으로 중복 제거 후 합산 반환.
        """
        start_dt, end_dt = self._datetime_range(days)
        result: list[dict] = []
        seen_clm_keys: set[str] = set()

        # 1차: exchangeSearch API (교환접수(03) 단계 — 접수 후 30분 내 건만 조회됨)
        try:
            data = await self._call_api(
                "POST",
                "/v1/openapi/claim/v1/exchangeOpenApi/exchangeSearch",
                body={
                    "srchStrtDttm": start_dt,
                    "srchEndDttm": end_dt,
                },
            )
            raw_list = data.get("data") or []
            if not isinstance(raw_list, list):
                raw_list = []
            for claim in raw_list:
                od_no = claim.get("odNo", "")
                clm_no = claim.get("clmNo", "")
                for item in claim.get("itemList") or []:
                    item["odNo"] = od_no
                    item["clmNo"] = clm_no
                    step_cd = str(item.get("odPrgsStepCd", "") or "")
                    dv_rtrv = item.get("dvRtrvDvsCd", "")
                    clm_rsn_cd_es = str(item.get("clmRsnCd", "") or "")
                    # 반품 사유 코드(300번대)가 exchangeSearch에 잘못 포함된 경우 제외
                    if clm_rsn_cd_es.startswith(("2", "3")):
                        logger.warning(
                            f"[롯데ON][교환-exchangeSearch] 반품 사유코드({clm_rsn_cd_es}) 교환 목록에서 제외: "
                            f"odNo={od_no} clmNo={clm_no}"
                        )
                        continue
                    logger.info(
                        f"[롯데ON][교환-exchangeSearch] odNo={od_no} clmNo={clm_no} stepCd={step_cd} "
                        f"dvRtrvDvsCd={dv_rtrv} clmRsnCd={clm_rsn_cd_es}"
                    )
                    key = f"{od_no}_{clm_no}_{item.get('odSeq', '')}"
                    if key not in seen_clm_keys:
                        seen_clm_keys.add(key)
                        result.append(item)
        except Exception as e:
            logger.warning(f"[롯데ON][교환-exchangeSearch] 조회 실패 (무시): {e}")

        # 2차: getCancellationRequestAndComplateList(clmTpCd=EXCH)
        try:
            data2 = await self._call_api(
                "POST",
                "/v1/openapi/claim/v1/cancellationOpenApi/getCancellationRequestAndComplateList",
                body={
                    "srchStrtDttm": start_dt,
                    "srchEndDttm": end_dt,
                    "clmTpCd": "EXCH",
                },
            )
            raw_list2 = data2.get("data") or []
            if not isinstance(raw_list2, list):
                raw_list2 = []
            for claim in raw_list2:
                od_no = claim.get("odNo", "")
                clm_no = claim.get("clmNo", "")
                for item in claim.get("itemList") or []:
                    item["odNo"] = od_no
                    item["clmNo"] = clm_no
                    step_cd = str(item.get("odPrgsStepCd", "") or "")
                    clm_rsn_cd = str(item.get("clmRsnCd", "") or "")
                    # 반품 사유 코드(300번대)가 교환 API에 잘못 포함된 경우 제외
                    if clm_rsn_cd.startswith(("2", "3")):
                        logger.warning(
                            f"[롯데ON][교환-EXCH] 반품 사유코드({clm_rsn_cd}) 교환 목록에서 제외: "
                            f"odNo={od_no} clmNo={clm_no}"
                        )
                        continue
                    logger.info(
                        f"[롯데ON][교환-EXCH] odNo={od_no} clmNo={clm_no} stepCd={step_cd} "
                        f"clmRsnCd={clm_rsn_cd}"
                    )
                    key = f"{od_no}_{clm_no}_{item.get('odSeq', '')}"
                    if key not in seen_clm_keys:
                        seen_clm_keys.add(key)
                        result.append(item)
        except Exception as e:
            logger.warning(f"[롯데ON][교환-EXCH] 조회 실패 (무시): {e}")

        # 3차: SellerDeliveryOrdersSearch(odTypCd=30) — 배송 모듈로 이동한 교환 회수 주문
        try:
            now = datetime.now(timezone.utc)
            actual_days = min(days, 30)
            for i in range(actual_days):
                day_end = now - timedelta(days=i)
                day_start = day_end - timedelta(days=1)
                srch_strt = day_start.strftime("%Y%m%d%H%M%S")
                srch_end = day_end.strftime("%Y%m%d%H%M%S")
                try:
                    ddata = await self._call_api(
                        "POST",
                        "/v1/openapi/delivery/v1/SellerDeliveryOrdersSearch",
                        body={
                            "srchStrtDt": srch_strt,
                            "srchEndDt": srch_end,
                        },
                    )
                    dl = (ddata.get("data") or {}).get("deliveryOrderList") or []
                    if not isinstance(dl, list):
                        dl = []
                    for item in dl:
                        # 교환 주문(odTypCd=30)만 처리
                        if str(item.get("odTypCd", "") or "") != "30":
                            continue
                        od_no = item.get("odNo", "")
                        clm_no = item.get("clmNo", "")
                        step_cd = str(item.get("odPrgsStepCd", "") or "")
                        logger.info(
                            f"[롯데ON][교환-배송모듈] odNo={od_no} clmNo={clm_no} "
                            f"stepCd={step_cd} dvRtrvDvsCd={item.get('dvRtrvDvsCd', '')}"
                        )
                        # 배송 API의 stepCd는 교환 클레임 단계와 다른 체계.
                        # 강제로 21(교환요청)을 찍으면 상품준비중 주문이 교환요청으로
                        # 잘못 바뀌므로 1·2차 클레임 API 결과만 신뢰하고 로그만 남김.
                except Exception as day_e:
                    logger.debug(
                        f"[롯데ON][교환-배송모듈] {srch_strt} 조회 실패: {day_e}"
                    )
        except Exception as e:
            logger.warning(f"[롯데ON][교환-배송모듈] 조회 실패 (무시): {e}")

        return result

    async def approve_exchange(
        self,
        od_no: str,
        clm_no: str,
        items: list[dict],
    ) -> bool:
        """교환 승인 처리 (exchangeRequestApproval) — 회수 지시.

        items: [{"odSeq": 1, "procSeq": 2, "orglProcSeq": 1, "slrRsnCd": "204"}]
        """
        payload = {
            "odNo": od_no,
            "clmNo": clm_no,
            "itemList": items,
        }
        logger.info(
            f"[롯데ON][교환승인] odNo={od_no} clmNo={clm_no} items={len(items)}건"
        )
        try:
            resp = await self._call_api(
                "POST",
                "/v1/openapi/claim/v1/exchangeOpenApi/exchangeRequestApproval",
                body=payload,
            )
            rc = str(resp.get("returnCode") or resp.get("rspnCd") or "")
            if rc == "0000":
                logger.info(f"[롯데ON][교환승인] 성공: odNo={od_no} clmNo={clm_no}")
                return True
            logger.warning(
                f"[롯데ON][교환승인] 실패: returnCode={rc} msg={resp.get('message', '')}"
            )
            return False
        except Exception as e:
            logger.warning(f"[롯데ON] 교환승인 실패: {e}")
            return False

    async def ship_order_exchange(
        self,
        od_no: str,
        sitm_no: str,
        spd_no: str,
        clm_no: str,
        quantity: int,
        shipping_company: str,
        tracking_number: str,
        od_seq: str = "1",
        proc_seq: str = "1",
    ) -> bool:
        """교환 재배송 처리 (SellerDeliveryProgressStateInform, odPrgsStepCd=13, clmNo 포함)."""
        KST = timezone(timedelta(hours=9))
        dv_co_cd = self.DELIVERY_COMPANY_MAP.get(shipping_company, shipping_company)
        dv_trc_stat_dttm = datetime.now(KST).strftime("%Y%m%d%H%M%S")

        payload = {
            "deliveryProgressStateList": [
                {
                    "dvRtrvDvsCd": "DV",
                    "odNo": od_no,
                    "odSeq": od_seq,
                    "procSeq": proc_seq,
                    "orglProcSeq": "",
                    "clmNo": clm_no,
                    "odPrgsStepCd": "13",
                    "dvTrcStatDttm": dv_trc_stat_dttm,
                    "invcNbr": "1",
                    "dvCoCd": dv_co_cd,
                    "invcNo": tracking_number,
                    "spdNo": spd_no,
                    "spdNm": "",
                    "sitmNo": sitm_no,
                    "itmNm": "",
                    "itmSlPrc": "",
                    "slQty": str(quantity),
                    "dvTrcStatCd": "",
                    "dvArclNm": "",
                    "dvArclTelNo": "",
                    "dvLocCnts": "",
                    "eofcTelNo": "",
                }
            ]
        }
        logger.info(
            f"[롯데ON][교환재배송] odNo={od_no} clmNo={clm_no} invcNo={tracking_number} dvCoCd={dv_co_cd}"
        )
        try:
            resp = await self._call_api(
                "POST",
                "/v1/openapi/delivery/v1/SellerDeliveryProgressStateInform",
                body=payload,
            )
            result_list = (
                resp.get("deliveryProgressStateList") or resp.get("data") or []
            )
            if isinstance(result_list, list) and result_list:
                item = result_list[0]
                rslt_cd = str(item.get("rsltCd", ""))
                rslt_msg = item.get("rsltMsg", "")
                if rslt_cd == "0000":
                    logger.info(
                        f"[롯데ON][교환재배송] 성공: odNo={od_no} clmNo={clm_no}"
                    )
                    return True
                else:
                    logger.warning(
                        f"[롯데ON][교환재배송] 실패: rsltCd={rslt_cd} rsltMsg={rslt_msg}"
                    )
                    return False
            logger.info(f"[롯데ON][교환재배송] 응답: {resp}")
            return True
        except Exception as e:
            logger.warning(
                f"[롯데ON] 교환재배송 실패: odNo={od_no} clmNo={clm_no} / {e}"
            )
            return False

    async def approve_return(self, od_no: str, clm_no: str, items: list[dict]) -> bool:
        """반품 승인 처리 (returnRequestApproval).

        Args:
          od_no: 주문번호
          clm_no: 클레임번호
          items: itemList — 각 항목에 odSeq, procSeq, orglProcSeq 필수
        """
        payload = {
            "odNo": od_no,
            "clmNo": clm_no,
            "itemList": items,
        }
        resp = await self._call_api(
            "POST",
            "/v1/openapi/claim/v1/returningOpenApi/returnRequestApproval",
            body=payload,
        )
        rc = str(resp.get("returnCode", ""))
        if rc != "0000":
            msg = resp.get("message", f"returnCode={rc}")
            raise Exception(f"롯데ON 반품 승인 오류: {msg}")
        return True

    async def reject_return(
        self, order_no: str, reason: str = "", clm_no: str = ""
    ) -> bool:
        """반품 거부 처리 (returnRequestReject).

        Args:
          order_no: 주문번호 (odNo)
          reason: 거부 사유
          clm_no: 클레임번호 (선택 — 전달 시 더 정확한 처리)
        """
        payload: dict[str, Any] = {
            "odNo": order_no,
            "clmRsnCnts": reason or "판매자 반품 거부",
        }
        if clm_no:
            payload["clmNo"] = clm_no
        try:
            await self._call_api(
                "POST",
                "/v1/openapi/claim/v1/returningOpenApi/returnRequestReject",
                body=payload,
            )
            logger.info(f"[롯데ON] 반품 거부 완료: odNo={order_no} clmNo={clm_no}")
            return True
        except Exception as e:
            logger.warning(f"[롯데ON] 반품 거부 실패: odNo={order_no} / {e}")
            return False

    # ------------------------------------------------------------------
    # CS 문의 (Q&A)
    # ------------------------------------------------------------------

    async def get_qna_list(self, days: int = 30) -> list[dict[str, Any]]:
        """판매자 문의 목록 조회.

        POST /v1/openapi/customer/v1/getSellerInquiryList

        Args:
          days: 조회 기간 (일 수, 기본 30일)

        Returns:
          rsltList 항목 리스트. 주요 필드:
            slrInqNo        — 판매자문의번호 (market_inquiry_no)
            vocLcsfCd       — 문의유형코드 (IC00000263~IC00000621)
            vocTypNm        — 문의유형명
            slrInqProcStatCd — 처리상태 (ANS=답변, UNANS=미답변)
            inqCnts         — 문의내용
            ansCnts         — 답변내용
            odNo            — 주문번호
            pdNo            — 상품번호 (market_product_no)
            pdNm            — 상품명
            spdNo           — 판매자상품번호
            accpDttm        — 접수일시 (yyyyMMddHHmmss)
            procDttm        — 처리일시
        """

        now = now_kst()
        end_dt = (now + timedelta(days=1)).strftime("%Y%m%d")
        start_dt = (now - timedelta(days=days)).strftime("%Y%m%d")

        body = {
            "scStrtDt": start_dt,
            "scEndDt": end_dt,
            "pageNo": "1",
            "rowsPerPage": "100",
        }

        try:
            result = await self._call_api(
                "POST", "/v1/openapi/customer/v1/getSellerInquiryList", body=body
            )
            items = result.get("rsltList") or []
            if not isinstance(items, list):
                items = []
            logger.info(
                f"[롯데ON][CS] 판매자문의 조회 완료: {len(items)}건 (기간: {start_dt}~{end_dt})"
            )
            return items
        except LotteonApiError as e:
            logger.warning(f"[롯데ON][CS] 판매자문의 목록 조회 실패: {e}")
            return []

    async def answer_qna(self, qna_no: str, content: str) -> dict[str, Any]:
        """판매자 문의 답변 등록/수정.

        POST /v1/openapi/customer/v1/updateSellerInquiry
        (등록과 수정 모두 동일 엔드포인트 사용)

        Args:
          qna_no: 판매자문의번호 (slrInqNo)
          content: 답변 내용 (ansCnts)

        Returns:
          응답 dict (rsltCd, rsltMsg)
        """
        body = {
            "slrInqNo": qna_no,
            "ansCnts": content,
        }
        result = await self._call_api(
            "POST", "/v1/openapi/customer/v1/updateSellerInquiry", body=body
        )
        logger.info(f"[롯데ON][CS] 판매자문의 답변 완료: slrInqNo={qna_no}")
        return result

    async def update_qna_answer(
        self, qna_no: str, answer_no: str, content: str
    ) -> dict[str, Any]:
        """판매자 문의 답변 수정 (등록과 동일 엔드포인트).

        POST /v1/openapi/customer/v1/updateSellerInquiry

        Args:
          qna_no: 판매자문의번호
          answer_no: 미사용 (롯데ON은 문의번호로만 식별)
          content: 수정할 답변 내용
        """
        return await self.answer_qna(qna_no, content)

    # ──────────────────────────────────────────────────────────────────────
    # CS — 상품 Q&A
    # ──────────────────────────────────────────────────────────────────────

    async def get_product_qna_list(self, days: int = 30) -> list[dict[str, Any]]:
        """상품 Q&A 목록 조회.

        GET /v1/openapi/product/v1/product/qna/list

        Args:
          days: 조회 기간 (일 수, 기본 30일)

        Returns:
          목록 항목 리스트. 주요 필드:
            qnaNo           — Q&A 번호 (market_inquiry_no: PQNA_{qnaNo})
            qnaCnts         — 질문 내용
            ansCnts         — 답변 내용
            ansStatCd       — 답변 상태 (ANS=답변완료, UNANS=미답변)
            pdNo            — 상품번호 (market_product_no)
            pdNm            — 상품명
            spdNo           — 판매자상품번호
            buyerId         — 구매자 ID
            regDttm         — 등록일시 (yyyyMMddHHmmss)
        """

        now = now_kst()
        end_dt = (now + timedelta(days=1)).strftime("%Y%m%d")
        start_dt = (now - timedelta(days=days)).strftime("%Y%m%d")

        try:
            result = await self._call_api(
                "GET",
                "/v1/openapi/product/v1/product/qna/list",
                params={
                    "scStrtDt": start_dt,
                    "scEndDt": end_dt,
                    "pageNo": "1",
                    "rowsPerPage": "100",
                },
            )
            # 응답 구조: rsltList 또는 content 또는 list
            items = (
                result.get("rsltList")
                or result.get("content")
                or result.get("list")
                or []
            )
            if not isinstance(items, list):
                items = []
            logger.info(
                f"[롯데ON][CS] 상품Q&A 조회 완료: {len(items)}건 (기간: {start_dt}~{end_dt})"
            )
            return items
        except LotteonApiError as e:
            logger.warning(f"[롯데ON][CS] 상품Q&A 목록 조회 실패: {e}")
            return []

    async def reply_product_qna(self, qna_no: str, content: str) -> dict[str, Any]:
        """상품 Q&A 답변 등록/수정.

        POST /v1/openapi/product/v1/product/qna/reply

        Args:
          qna_no: Q&A 번호 (qnaNo)
          content: 답변 내용 (ansCnts)

        Returns:
          응답 dict
        """
        body = {
            "qnaNo": qna_no,
            "ansCnts": content,
        }
        result = await self._call_api(
            "POST", "/v1/openapi/product/v1/product/qna/reply", body=body
        )
        logger.info(f"[롯데ON][CS] 상품Q&A 답변 완료: qnaNo={qna_no}")
        return result

    # ──────────────────────────────────────────────────────────────────────
    # CS — 판매자 연락 (Contact)
    # ──────────────────────────────────────────────────────────────────────

    async def get_contact_list(self, days: int = 30) -> list[dict[str, Any]]:
        """판매자 연락 목록 조회.

        POST /v1/openapi/customer/v1/getSellerContactList

        Returns:
          rsltList 항목 리스트. 주요 필드:
            cntcNo          — 연락번호 (market_inquiry_no)
            cntcCnts        — 연락 내용
            ansCnts         — 답변 내용
            procStatCd      — 처리상태 (ANS/UNANS)
            odNo            — 주문번호
            pdNo            — 상품번호
            pdNm            — 상품명
            accpDttm        — 접수일시 (yyyyMMddHHmmss)
            vocLcsfCd       — 문의유형코드
        """

        now = now_kst()
        end_dt = (now + timedelta(days=1)).strftime("%Y%m%d")
        start_dt = (now - timedelta(days=days)).strftime("%Y%m%d")

        body = {
            "scStrtDt": start_dt,
            "scEndDt": end_dt,
            "pageNo": "1",
            "rowsPerPage": "100",
        }
        try:
            result = await self._call_api(
                "POST", "/v1/openapi/customer/v1/getSellerContactList", body=body
            )
            items = result.get("rsltList") or []
            if not isinstance(items, list):
                items = []
            logger.info(
                f"[롯데ON][CS] 판매자연락 조회 완료: {len(items)}건 (기간: {start_dt}~{end_dt})"
            )
            return items
        except LotteonApiError as e:
            logger.warning(f"[롯데ON][CS] 판매자연락 목록 조회 실패: {e}")
            return []

    async def answer_contact(self, contact_no: str, content: str) -> dict[str, Any]:
        """판매자 연락 답변 등록.

        POST /v1/openapi/customer/v1/updateSellerContact

        Args:
          contact_no: 연락번호 (cntcNo)
          content: 답변 내용 (ansCnts)
        """
        body = {
            "cntcNo": contact_no,
            "ansCnts": content,
        }
        result = await self._call_api(
            "POST", "/v1/openapi/customer/v1/updateSellerContact", body=body
        )
        logger.info(f"[롯데ON][CS] 판매자연락 답변 완료: cntcNo={contact_no}")
        return result

    # ──────────────────────────────────────────────────────────────────────
    # CS — 보상 요청 (Compensate)
    # ──────────────────────────────────────────────────────────────────────

    async def get_compensate_list(self, days: int = 30) -> list[dict[str, Any]]:
        """보상 요청 목록 조회.

        POST /v1/openapi/customer/v1/getSellerCompensateList

        Returns:
          rsltList 항목 리스트. 주요 필드:
            compNo          — 보상요청번호 (market_inquiry_no)
            compCnts        — 보상 요청 내용
            ansCnts         — 답변/처리 내용
            procStatCd      — 처리상태
            odNo            — 주문번호
            pdNo            — 상품번호
            pdNm            — 상품명
            accpDttm        — 접수일시
        """

        now = now_kst()
        end_dt = (now + timedelta(days=1)).strftime("%Y%m%d")
        start_dt = (now - timedelta(days=days)).strftime("%Y%m%d")

        body = {
            "scStrtDt": start_dt,
            "scEndDt": end_dt,
            "pageNo": "1",
            "rowsPerPage": "100",
        }
        try:
            result = await self._call_api(
                "POST", "/v1/openapi/customer/v1/getSellerCompensateList", body=body
            )
            items = result.get("rsltList") or []
            if not isinstance(items, list):
                items = []
            logger.info(
                f"[롯데ON][CS] 보상요청 조회 완료: {len(items)}건 (기간: {start_dt}~{end_dt})"
            )
            return items
        except LotteonApiError as e:
            logger.warning(f"[롯데ON][CS] 보상요청 목록 조회 실패: {e}")
            return []


class LotteonApiError(Exception):
    """롯데ON API 에러."""

    pass

"""11번가 OpenAPI 클라이언트 - 상품 등록/수정.

인증 방식: 32자리 Open API Key (헤더 전달)
- openapikey: {apiKey}
- 상품 등록: POST /rest/prodservices/prod
- 상품 수정: PUT /rest/prodservices/prod/{prdNo}
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Optional
from xml.etree import ElementTree as ET

from backend.utils import now_kst

import httpx

from backend.core.config import settings

from backend.utils.logger import logger


class ElevenstRateLimitError(Exception):
    """11번가 API Rate Limit 초과 시 발생하는 예외"""

    def __init__(self, retry_after: int = 5):
        self.retry_after = retry_after
        super().__init__(f"11번가 Rate Limit 초과 (retry_after={retry_after}s)")


_elevenst_clients: dict[str, httpx.AsyncClient] = {}


def _get_elevenst_http_client(api_key: str) -> httpx.AsyncClient:
    """api_key별 httpx 클라이언트 재사용 — 연결 풀 유지로 SSL 핸드셰이크 반복 방지.

    닫힌 클라이언트가 캐시에 남아 있으면 새로 생성한다.
    """
    existing = _elevenst_clients.get(api_key)
    if existing is None or existing.is_closed:
        _elevenst_clients[api_key] = httpx.AsyncClient(
            timeout=settings.http_timeout_default,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=40),
        )
    return _elevenst_clients[api_key]


def _get_elevenst_stat_client(api_key: str) -> httpx.AsyncClient:
    """판매중지(prodstatservice) 전용 클라이언트.

    keepalive 풀 고갈로 인한 'All connection attempts failed' 방지를 위해
    매 호출마다 새 클라이언트를 생성한다 (context manager로 닫아야 함).
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(max_keepalive_connections=0, max_connections=1),
    )


class ElevenstClient:
    """11번가 셀러 API 클라이언트."""

    BASE_URL = "https://api.11st.co.kr/rest/prodservices"
    # 상품 등록: POST /rest/prodservices/product
    # 상품 조회: GET /rest/prodservices/product/{productCode}
    # 상품 수정: PUT /rest/prodservices/product/{productCode}

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {
            "openapikey": self.api_key,
            "Content-Type": "text/xml; charset=UTF-8",
            "Accept": "application/xml",
        }

    @staticmethod
    def _parse_xml(text: str) -> dict[str, Any]:
        """XML 응답 파싱."""
        try:
            root = ET.fromstring(text)
            result: dict[str, Any] = {}
            for child in root:
                tag = child.tag
                if list(child):
                    inner: dict[str, Any] = {}
                    for sub in child:
                        inner[sub.tag] = (sub.text or "").strip()
                    result[tag] = inner
                else:
                    result[tag] = (child.text or "").strip()
            return result
        except ET.ParseError:
            return {"raw": text}

    async def _do_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict,
        body: Optional[str],
    ) -> httpx.Response:
        if method == "GET":
            return await client.get(url, headers=headers)
        elif method == "POST":
            return await client.post(url, headers=headers, content=body)
        elif method == "PUT":
            return await client.put(url, headers=headers, content=body)
        elif method == "DELETE":
            return await client.delete(url, headers=headers)
        else:
            raise ValueError(f"지원하지 않는 HTTP 메서드: {method}")

    async def _call_api(
        self,
        method: str,
        path: str,
        body: Optional[str] = None,
    ) -> dict[str, Any]:
        """공통 API 호출 (XML 기반). ConnectError 시 클라이언트 재생성 후 1회 재시도."""
        url = f"{self.BASE_URL}{path}"
        headers = self._headers()

        client = _get_elevenst_http_client(self.api_key)
        try:
            resp = await self._do_request(client, method, url, headers, body)
        except (httpx.ConnectError, httpx.RemoteProtocolError) as conn_err:
            # 캐시된 클라이언트 연결이 끊겼을 수 있음 — 제거 후 새 클라이언트로 1회 재시도
            logger.warning(
                f"[11번가] 연결 오류, 클라이언트 재생성 후 재시도: {conn_err}"
            )
            _elevenst_clients.pop(self.api_key, None)
            client = _get_elevenst_http_client(self.api_key)
            resp = await self._do_request(client, method, url, headers, body)

        logger.info(f"[11번가] {method} {path} → {resp.status_code}")

        data = self._parse_xml(resp.text)

        # ⚠️ 반드시 "if not resp.is_success:" 블록보다 앞에 위치
        if resp.status_code == 429:
            try:
                retry_after = int(resp.headers.get("Retry-After", "5"))
            except ValueError:
                retry_after = 5
            raise ElevenstRateLimitError(retry_after=retry_after)
        if resp.status_code in (503, 504):
            raise ElevenstRateLimitError(retry_after=10)

        if not resp.is_success:
            msg = data.get("message", "") or data.get("raw", "") or resp.text[:300]
            raise ElevenstApiError(f"HTTP {resp.status_code}: {msg}")

        # 에러코드 체크
        result_code = data.get("resultCode", "") or data.get("ResultCode", "")
        if result_code and str(result_code) != "200" and str(result_code) != "0":
            msg = data.get("resultMessage", "") or data.get("message", "")
            raise ElevenstApiError(f"API 에러 ({result_code}): {msg}")

        return data

    # ------------------------------------------------------------------
    # 카테고리 조회
    # ------------------------------------------------------------------

    async def get_categories(self) -> dict[str, Any]:
        """전체 카테고리 조회. (cateservice 엔드포인트 사용)"""
        url = "https://api.11st.co.kr/rest/cateservice/category"
        headers = self._headers()
        client = _get_elevenst_http_client(self.api_key)
        resp = await client.get(url, headers=headers)
        logger.info(f"[11번가] GET /cateservice/category → {resp.status_code}")
        data = self._parse_xml(resp.text)
        if not resp.is_success:
            msg = data.get("message", "") or data.get("raw", "") or resp.text[:300]
            raise ElevenstApiError(f"HTTP {resp.status_code}: {msg}")
        return data

    async def get_category_by_id(self, category_id: str) -> dict[str, Any]:
        """특정 카테고리 하위 조회. (cateservice 엔드포인트 사용)"""
        url = f"https://api.11st.co.kr/rest/cateservice/category/{category_id}"
        headers = self._headers()
        client = _get_elevenst_http_client(self.api_key)
        resp = await client.get(url, headers=headers)
        logger.info(
            f"[11번가] GET /cateservice/category/{category_id} → {resp.status_code}"
        )
        data = self._parse_xml(resp.text)
        if not resp.is_success:
            msg = data.get("message", "") or data.get("raw", "") or resp.text[:300]
            raise ElevenstApiError(f"HTTP {resp.status_code}: {msg}")
        return data

    # ------------------------------------------------------------------
    # 상품 등록/수정
    # ------------------------------------------------------------------

    async def register_product(self, xml_data: str) -> dict[str, Any]:
        """상품 등록.

        11번가 셀러 API: POST /rest/prodservices/product
        """
        # KC인증 cert 필드(03/03/03/05) 검증용 — 응답 전체를 1회 로깅
        # 11번가가 echo하는 cert 필드 raw값 + 무시되는지 여부 확인 목적
        url = f"{self.BASE_URL}/product"
        headers = self._headers()
        client = _get_elevenst_http_client(self.api_key)
        try:
            resp = await self._do_request(client, "POST", url, headers, xml_data)
        except (httpx.ConnectError, httpx.RemoteProtocolError) as conn_err:
            logger.warning(
                f"[11번가] 연결 오류, 클라이언트 재생성 후 재시도: {conn_err}"
            )
            _elevenst_clients.pop(self.api_key, None)
            client = _get_elevenst_http_client(self.api_key)
            resp = await self._do_request(client, "POST", url, headers, xml_data)

        # cert 검증용 — ProductCertGroup 구조 echo 확인
        import re as _re

        req_groups = _re.findall(
            r"<ProductCertGroup>\s*<crtfGrpTypCd>([^<]+)</crtfGrpTypCd>\s*<crtfGrpObjClfCd>([^<]+)</crtfGrpObjClfCd>\s*</ProductCertGroup>",
            xml_data,
        )
        resp_groups = _re.findall(
            r"<ProductCertGroup>\s*<crtfGrpTypCd>([^<]+)</crtfGrpTypCd>\s*<crtfGrpObjClfCd>([^<]+)</crtfGrpObjClfCd>\s*</ProductCertGroup>",
            resp.text,
        )
        logger.info(f"[11번가] 등록요청 cert groups: {req_groups}")
        logger.info(f"[11번가] 등록응답 cert groups: {resp_groups}")
        logger.info(
            f"[11번가] 등록응답 raw (앞 1500자): {resp.text[:1500].replace(chr(10), ' ')}"
        )

        # 기존 _call_api 후처리(에러코드/예외) 재현
        data = self._parse_xml(resp.text)
        if resp.status_code == 429:
            try:
                retry_after = int(resp.headers.get("Retry-After", "5"))
            except ValueError:
                retry_after = 5
            raise ElevenstRateLimitError(retry_after=retry_after)
        if resp.status_code in (503, 504):
            raise ElevenstRateLimitError(retry_after=10)
        if not resp.is_success:
            msg = data.get("message", "") or data.get("raw", "") or resp.text[:300]
            raise ElevenstApiError(f"HTTP {resp.status_code}: {msg}")
        result_code = data.get("resultCode", "") or data.get("ResultCode", "")
        if result_code and str(result_code) != "200" and str(result_code) != "0":
            msg = data.get("resultMessage", "") or data.get("message", "")
            raise ElevenstApiError(f"API 에러 ({result_code}): {msg}")

        result = data
        prd_no = result.get("productNo", "") if isinstance(result, dict) else ""
        logger.info(
            f"[11번가] 상품 등록 완료 — prdNo={prd_no}, keys={list(result.keys()) if isinstance(result, dict) else type(result)}"
        )
        return {"success": True, "prd_no": prd_no, "data": result}

    async def update_product(self, prd_no: str, xml_data: str) -> dict[str, Any]:
        """상품 수정."""
        result = await self._call_api("PUT", f"/product/{prd_no}", body=xml_data)
        return {"success": True, "data": result}

    async def delete_product(self, prd_no: str) -> dict[str, Any]:
        """상품 판매중지(전시중지).

        11번가 공식 API: PUT /rest/prodstatservice/stat/stopdisplay/{prdNo}
        성공 시 resultCode=200, message에 STAT 코드 포함.
        """
        import asyncio as _asyncio

        url = f"https://api.11st.co.kr/rest/prodstatservice/stat/stopdisplay/{prd_no}"
        headers = self._headers()

        status_code: int = 0
        resp_text: str = ""
        resp_headers: dict = {}

        for attempt in range(3):
            try:
                async with _get_elevenst_stat_client(self.api_key) as client:
                    resp = await client.put(url, headers=headers)
                    status_code = resp.status_code
                    resp_text = resp.text
                    resp_headers = dict(resp.headers)
                break
            except httpx.HTTPError as conn_err:
                logger.warning(
                    f"[11번가] 판매중지 연결 오류 (attempt {attempt + 1}/3): {conn_err}"
                )
                if attempt < 2:
                    await _asyncio.sleep(2**attempt)
                else:
                    raise ElevenstApiError(
                        f"판매중지 연결 실패 (3회 시도): {conn_err}"
                    ) from conn_err

        logger.info(
            f"[11번가] PUT /prodstatservice/stat/stopdisplay/{prd_no} → {status_code}"
        )
        data = self._parse_xml(resp_text)

        if status_code == 429:
            try:
                retry_after = int(resp_headers.get("retry-after", "5"))
            except ValueError:
                retry_after = 5
            raise ElevenstRateLimitError(retry_after=retry_after)

        result_code = str(data.get("resultCode", "") or data.get("ResultCode", ""))
        if result_code != "200":
            msg = data.get("message", "") or data.get("raw", "") or resp_text[:300]
            raise ElevenstApiError(
                f"HTTP {status_code} / resultCode={result_code}: {msg}"
            )

        return {"success": True, "data": data}

    async def get_product(self, prd_no: str) -> dict[str, Any]:
        """상품 조회."""
        return await self._call_api("GET", f"/product/{prd_no}")

    async def find_by_seller_code(self, seller_prd_cd: str) -> dict[str, Any]:
        """판매자상품코드(sellerPrdCd)로 11번가 상품 조회.

        11번가 공식 API: GET /rest/prodmarketservice/sellerprodcode/{sellerprdcd}
        성공 시 ns2:product 내부의 prdNo, selStatCd, selStatNm, prdNm 등을 반환한다.

        Returns:
            {
                "found": bool,        # 11번가에 등록 존재 여부
                "prd_no": str,        # 11번가 상품번호
                "sel_stat_cd": str,   # 판매상태 코드 (103=판매중, 104=품절, 105=전시중지, 106=정상종료, 108=금지)
                "sel_stat_nm": str,
                "prd_nm": str,
                "raw_text": str,      # 디버그용 응답 앞부분
            }
        """
        import re as _re

        url = f"https://api.11st.co.kr/rest/prodmarketservice/sellerprodcode/{seller_prd_cd}"
        headers = self._headers()
        client = _get_elevenst_http_client(self.api_key)

        try:
            resp = await client.get(url, headers=headers)
        except (httpx.ConnectError, httpx.RemoteProtocolError) as conn_err:
            _elevenst_clients.pop(self.api_key, None)
            client = _get_elevenst_http_client(self.api_key)
            resp = await client.get(url, headers=headers)

        logger.info(
            f"[11번가] GET /sellerprodcode/{seller_prd_cd} → {resp.status_code}"
        )

        # Rate limit 우선 처리
        if resp.status_code == 429:
            try:
                retry_after = int(resp.headers.get("Retry-After", "5"))
            except ValueError:
                retry_after = 5
            raise ElevenstRateLimitError(retry_after=retry_after)
        if resp.status_code in (503, 504):
            raise ElevenstRateLimitError(retry_after=10)

        # euc-kr 인코딩 응답을 명시적 디코딩
        try:
            raw_bytes = resp.content
            text = raw_bytes.decode("euc-kr", errors="replace")
        except Exception:
            text = resp.text

        # 404 또는 본문에 product 노드 없음 → 미존재
        if resp.status_code == 404 or "<prdNo>" not in text:
            return {
                "found": False,
                "prd_no": "",
                "sel_stat_cd": "",
                "sel_stat_nm": "",
                "prd_nm": "",
                "raw_text": text[:300],
            }

        if not resp.is_success:
            raise ElevenstApiError(f"HTTP {resp.status_code}: {text[:300]}")

        # ns2:* prefix XML — 정규식으로 첫 번째 상품 정보만 추출 (일치하는 sellerPrdCd 단일 케이스)
        def _first(tag: str) -> str:
            m = _re.search(rf"<{tag}>([^<]*)</{tag}>", text)
            return m.group(1).strip() if m else ""

        prd_no = _first("prdNo")
        return {
            "found": bool(prd_no),
            "prd_no": prd_no,
            "sel_stat_cd": _first("selStatCd"),
            "sel_stat_nm": _first("selStatNm"),
            "prd_nm": _first("prdNm"),
            "raw_text": text[:300],
        }

    async def test_auth(self) -> bool:
        """API 키 유효성 확인 — 카테고리 조회로 인증 테스트."""
        try:
            await self.get_categories()
            return True
        except Exception:
            return False

    async def list_seller_products(
        self,
        sel_stat_cd: str = "103",
        page_size: int = 500,
        max_pages: int = 200,
        throttle: float = 0.4,
    ) -> list[dict[str, Any]]:
        """등록된 셀러 상품 페이징 조회 (유령삭제 양방향 동기화용).

        11번가 공식 API: POST /rest/prodmarketservice/prodmarket
        - XML body: <SearchProduct><limit>500</limit><start>1</start><end>500</end><selStatCd>103</selStatCd></SearchProduct>
        - 페이징: start/end offset 증가 (1~500, 501~1000, ...)
        - limit 최대 500
        - selStatCd: 101=승인대기, 102=승인전, 103=판매중, 104=품절, 105=전시중지,
          106=정상종료, 108=판매금지. 빈문자열이면 전체.
        - max_pages: 안전장치 (200 * 500 = 최대 10만개)
        - throttle: 호출 간격 (rate limit 회피)

        Returns:
            list[{"prd_no": str, "name": str, "seller_code": str,
                  "sel_stat_cd": str, "sel_stat_nm": str}]
        """
        import re as _re

        url = "https://api.11st.co.kr/rest/prodmarketservice/prodmarket"
        results: list[dict[str, Any]] = []
        offset = 1
        page_count = 0

        # SearchProduct XML body 생성
        def _build_body(start: int, end: int) -> str:
            stat_xml = f"<selStatCd>{sel_stat_cd}</selStatCd>" if sel_stat_cd else ""
            return (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<SearchProduct>"
                f"<limit>{page_size}</limit>"
                f"<start>{start}</start>"
                f"<end>{end}</end>"
                f"{stat_xml}"
                "</SearchProduct>"
            )

        client = _get_elevenst_http_client(self.api_key)

        while page_count < max_pages:
            start = offset
            end = offset + page_size - 1
            body = _build_body(start, end)
            headers = self._headers()

            try:
                resp = await client.post(
                    url, headers=headers, content=body.encode("utf-8")
                )
            except (httpx.ConnectError, httpx.RemoteProtocolError):
                _elevenst_clients.pop(self.api_key, None)
                client = _get_elevenst_http_client(self.api_key)
                resp = await client.post(
                    url, headers=headers, content=body.encode("utf-8")
                )

            logger.info(
                f"[11번가] POST /prodmarket start={start} end={end} → {resp.status_code}"
            )

            if resp.status_code == 429:
                try:
                    retry_after = int(resp.headers.get("Retry-After", "5"))
                except ValueError:
                    retry_after = 5
                raise ElevenstRateLimitError(retry_after=retry_after)
            if resp.status_code in (503, 504):
                raise ElevenstRateLimitError(retry_after=10)

            try:
                text = resp.content.decode("euc-kr", errors="replace")
            except Exception:
                text = resp.text

            if not resp.is_success:
                raise ElevenstApiError(f"HTTP {resp.status_code}: {text[:300]}")

            # ns2:product 노드 추출 — 정규식으로 블록 단위 분리
            product_blocks = _re.findall(
                r"<ns2:product>(.*?)</ns2:product>", text, flags=_re.DOTALL
            )
            if not product_blocks:
                # prefix 없는 형태 폴백
                product_blocks = _re.findall(
                    r"<product>(.*?)</product>", text, flags=_re.DOTALL
                )

            if not product_blocks:
                break

            def _extract(block: str, tag: str) -> str:
                m = _re.search(rf"<{tag}>([^<]*)</{tag}>", block)
                return m.group(1).strip() if m else ""

            page_results: list[dict[str, Any]] = []
            for blk in product_blocks:
                prd_no = _extract(blk, "prdNo")
                if not prd_no:
                    continue
                page_results.append(
                    {
                        "prd_no": prd_no,
                        "name": _extract(blk, "prdNm"),
                        "seller_code": _extract(blk, "sellerPrdCd"),
                        "sel_stat_cd": _extract(blk, "selStatCd"),
                        "sel_stat_nm": _extract(blk, "selStatNm"),
                    }
                )

            results.extend(page_results)
            page_count += 1

            # 페이지 size 미만이면 마지막 페이지
            if len(page_results) < page_size:
                break

            offset += page_size
            await asyncio.sleep(throttle)

        logger.info(
            f"[11번가] list_seller_products 완료: {len(results)}개 "
            f"(페이지 {page_count}, selStatCd={sel_stat_cd or 'ALL'})"
        )
        return results

    # ------------------------------------------------------------------
    # 주문 조회 / 처리
    # ------------------------------------------------------------------

    async def get_orders(self, start_time: str, end_time: str) -> list[dict[str, Any]]:
        """기간별 결제완료 주문 목록 조회.

        Args:
            start_time: 검색시작일 YYYYMMDDhhmm (예: 202603010000)
            end_time:   검색종료일 YYYYMMDDhhmm (예: 202603071200)
            최대 조회 기간: 7일 제한 → 초과 시 자동 분할 조회
        """
        from datetime import timedelta

        fmt = "%Y%m%d%H%M"
        start_dt = datetime.strptime(start_time, fmt)
        end_dt = datetime.strptime(end_time, fmt)

        # 7일 단위로 청크 분할
        all_orders: list[dict[str, Any]] = []
        chunk_start = start_dt
        while chunk_start < end_dt:
            chunk_end = min(chunk_start + timedelta(days=7), end_dt)
            chunk_orders = await self._fetch_orders_chunk(
                chunk_start.strftime(fmt), chunk_end.strftime(fmt)
            )
            all_orders.extend(chunk_orders)
            chunk_start = chunk_end

        logger.info("[11번가] 전체 주문 조회 완료: %d건", len(all_orders))
        return all_orders

    async def get_packaging_orders(
        self, start_time: str, end_time: str
    ) -> list[dict[str, Any]]:
        """기간별 배송준비중(발주확인 완료) 주문 목록 조회.

        Args:
            start_time: 검색시작일 YYYYMMDDhhmm (예: 202603010000)
            end_time:   검색종료일 YYYYMMDDhhmm (예: 202603071200)
            최대 조회 기간: 7일 제한 → 초과 시 자동 분할 조회

        Returns:
            ordPrdStat=301 (발주확인 완료 = 배송준비중) 주문 목록
        """
        from datetime import timedelta

        fmt = "%Y%m%d%H%M"
        start_dt = datetime.strptime(start_time, fmt)
        end_dt = datetime.strptime(end_time, fmt)

        # 7일 단위로 청크 분할
        all_orders: list[dict[str, Any]] = []
        chunk_start = start_dt
        while chunk_start < end_dt:
            chunk_end = min(chunk_start + timedelta(days=7), end_dt)
            chunk_orders = await self._fetch_packaging_chunk(
                chunk_start.strftime(fmt), chunk_end.strftime(fmt)
            )
            all_orders.extend(chunk_orders)
            chunk_start = chunk_end

        logger.info("[11번가] 배송준비중 주문 조회 완료: %d건", len(all_orders))
        return all_orders

    async def _fetch_packaging_chunk(
        self, start_time: str, end_time: str
    ) -> list[dict[str, Any]]:
        """7일 이내 단일 구간 배송준비중 주문 조회."""
        import re as _re

        url = (
            f"https://api.11st.co.kr/rest/ordservices/packaging/{start_time}/{end_time}"
        )
        headers = self._headers()

        client = _get_elevenst_http_client(self.api_key)
        try:
            resp = await client.get(url, headers=headers)
        except (httpx.ConnectError, httpx.RemoteProtocolError) as conn_err:
            logger.warning(f"[11번가] 배송준비중 주문 연결 오류, 재시도: {conn_err}")
            _elevenst_clients.pop(self.api_key, None)
            client = _get_elevenst_http_client(self.api_key)
            resp = await client.get(url, headers=headers)
        logger.info(
            "[11번가] GET /ordservices/packaging/%s/%s → %s",
            start_time,
            end_time,
            resp.status_code,
        )

        # EUC-KR 인코딩 처리 (HTTP 에러 여부와 무관하게 먼저 디코딩)
        try:
            text = resp.content.decode("euc-kr")
        except Exception:
            text = resp.text

        # 네임스페이스 + XML 선언 제거 (ET가 euc-kr 멀티바이트 인코딩 미지원)
        xml_text = text.replace("ns2:", "")
        xml_text = _re.sub(r"<\?xml[^?]*\?>", "", xml_text, count=1).strip()

        if not resp.is_success:
            try:
                _err_root = ET.fromstring(xml_text)
                _rc = _err_root.findtext("resultCode", "") or _err_root.findtext(
                    "result_code", ""
                )
                _rt = _err_root.findtext("resultText", "") or _err_root.findtext(
                    "result_text", ""
                )
                raise ElevenstApiError(
                    f"API 오류 ({_rc}): {_rt}"
                    if _rc
                    else f"HTTP {resp.status_code}: {text[:200]}"
                )
            except ET.ParseError:
                raise ElevenstApiError(f"HTTP {resp.status_code}: {text[:200]}")

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.error("[11번가] 배송준비중 주문 XML 파싱 실패: %s", e)
            return []

        # result_code 확인 (0=결과없음 정상, 음수=에러)
        result_code = root.findtext("result_code", "")
        if result_code:
            if result_code == "0":
                return []
            result_text = root.findtext("result_text", "")
            raise ElevenstApiError(
                f"배송준비중 주문 조회 에러 ({result_code}): {result_text}"
            )

        orders: list[dict[str, Any]] = []
        for order_el in root.findall("order"):
            order_dict: dict[str, Any] = {}
            for child in order_el:
                order_dict[child.tag] = (child.text or "").strip()
            orders.append(order_dict)

        return orders

    async def _fetch_orders_chunk(
        self, start_time: str, end_time: str
    ) -> list[dict[str, Any]]:
        """7일 이내 단일 구간 주문 조회."""
        import re as _re

        url = (
            f"https://api.11st.co.kr/rest/ordservices/complete/{start_time}/{end_time}"
        )
        headers = self._headers()

        client = _get_elevenst_http_client(self.api_key)
        try:
            resp = await client.get(url, headers=headers)
        except (httpx.ConnectError, httpx.RemoteProtocolError) as conn_err:
            logger.warning(f"[11번가] 주문 조회 연결 오류, 재시도: {conn_err}")
            _elevenst_clients.pop(self.api_key, None)
            client = _get_elevenst_http_client(self.api_key)
            resp = await client.get(url, headers=headers)
        logger.info(
            "[11번가] GET /ordservices/complete/%s/%s → %s",
            start_time,
            end_time,
            resp.status_code,
        )

        # EUC-KR 인코딩 처리 (HTTP 에러 여부와 무관하게 먼저 디코딩)
        try:
            text = resp.content.decode("euc-kr")
        except Exception:
            text = resp.text

        # 네임스페이스 + XML 선언 제거 (ET가 euc-kr 멀티바이트 인코딩 미지원)
        xml_text = text.replace("ns2:", "")
        xml_text = _re.sub(r"<\?xml[^?]*\?>", "", xml_text, count=1).strip()

        if not resp.is_success:
            # HTTP 에러이더라도 XML 본문에서 실제 오류 메시지 추출 시도
            logger.error("[11번가] 오류 응답 원문: %s", text[:500])
            try:
                _err_root = ET.fromstring(xml_text)
                _rc = _err_root.findtext("resultCode", "") or _err_root.findtext(
                    "result_code", ""
                )
                _rt = (
                    _err_root.findtext("resultText", "")
                    or _err_root.findtext("result_text", "")
                    or _err_root.findtext("resultMessage", "")
                    or text[:300]
                )
                raise ElevenstApiError(
                    f"API 오류 ({_rc}): {_rt}"
                    if _rc
                    else f"HTTP {resp.status_code}: {text[:200]}"
                )
            except ET.ParseError:
                raise ElevenstApiError(f"HTTP {resp.status_code}: {text[:200]}")

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.error("[11번가] 주문 XML 파싱 실패: %s", e)
            return []

        # result_code 확인 (0=결과없음 정상, 음수=에러)
        result_code = root.findtext("result_code", "")
        if result_code:
            if result_code == "0":
                return []
            result_text = root.findtext("result_text", "")
            raise ElevenstApiError(f"주문 조회 에러 ({result_code}): {result_text}")

        orders: list[dict[str, Any]] = []
        for order_el in root.findall("order"):
            order_dict: dict[str, Any] = {}
            for child in order_el:
                order_dict[child.tag] = (child.text or "").strip()
            orders.append(order_dict)

        return orders

    async def confirm_order(
        self,
        ord_no: str,
        ord_prd_seq: str,
        dlv_no: str,
        add_prd_yn: str = "N",
        add_prd_no: str = "null",
    ) -> bool:
        """발주확인처리.

        Args:
            ord_no:      주문번호 (ordNo)
            ord_prd_seq: 주문순번 (ordPrdSeq)
            dlv_no:      배송번호 (dlvNo)
            add_prd_yn:  추가구성상품 여부 (Y/N, 기본 N)
            add_prd_no:  추가구성상품 번호 (없으면 null)

        Returns:
            True if 발주확인 성공
        """
        import re as _re

        url = (
            f"https://api.11st.co.kr/rest/ordservices/reqpackaging"
            f"/{ord_no}/{ord_prd_seq}/{add_prd_yn}/{add_prd_no}/{dlv_no}"
        )
        headers = self._headers()

        client = _get_elevenst_http_client(self.api_key)
        resp = await client.get(url, headers=headers)
        logger.info(
            "[11번가] 발주확인 ordNo=%s ordPrdSeq=%s → %s",
            ord_no,
            ord_prd_seq,
            resp.status_code,
        )

        if not resp.is_success:
            raise ElevenstApiError(
                f"발주확인 HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            text = resp.content.decode("euc-kr")
        except Exception:
            text = resp.text

        xml_text = _re.sub(r"<\?xml[^?]*\?>", "", text, count=1).strip()
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            raise ElevenstApiError(f"발주확인 응답 XML 파싱 실패: {text[:200]}")

        result_code = root.findtext("result_code", "")
        result_text = root.findtext("result_text", "")
        logger.info(
            "[11번가] 발주확인 결과: code=%s, text=%s", result_code, result_text
        )

        if result_code != "0":
            raise ElevenstApiError(f"발주확인 에러 ({result_code}): {result_text}")

        return True

    async def ship_order(
        self,
        dlv_no: str,
        invc_no: str,
        dlv_etprs_cd: str,
        dlv_mthd_cd: str = "01",
        send_dt: Optional[str] = None,
    ) -> bool:
        """발송처리 (배송중 처리).

        Args:
            dlv_no:       배송번호 (주문 응답의 dlvNo)
            invc_no:      송장번호
            dlv_etprs_cd: 택배사 코드 (예: 00034=CJ대한통운, 00012=롯데, 00011=한진)
            dlv_mthd_cd:  배송방식 (01=택배, 03=직접, 04=퀵, 05=배송없음, 기본 01)
            send_dt:      발송일 YYYYMMDDhhmm (미입력 시 현재 시각)

        Returns:
            True if 발송처리 성공
        """
        import re as _re

        if not send_dt:
            send_dt = now_kst().strftime("%Y%m%d%H%M")

        url = (
            f"https://api.11st.co.kr/rest/ordservices/reqdelivery"
            f"/{send_dt}/{dlv_mthd_cd}/{dlv_etprs_cd}/{invc_no}/{dlv_no}"
        )
        headers = self._headers()

        client = _get_elevenst_http_client(self.api_key)
        resp = await client.get(url, headers=headers)
        logger.info(
            "[11번가] 발송처리 dlvNo=%s invcNo=%s → %s",
            dlv_no,
            invc_no,
            resp.status_code,
        )

        if not resp.is_success:
            raise ElevenstApiError(
                f"발송처리 HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            text = resp.content.decode("euc-kr")
        except Exception:
            text = resp.text

        xml_text = _re.sub(r"<\?xml[^?]*\?>", "", text, count=1).strip()
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            raise ElevenstApiError(f"발송처리 응답 XML 파싱 실패: {text[:200]}")

        result_code = root.findtext("result_code", "")
        result_text = root.findtext("result_text", "")
        logger.info(
            "[11번가] 발송처리 결과: code=%s, text=%s", result_code, result_text
        )

        if result_code != "0":
            # -3313: 이미 배송중 상태 → 성공으로 처리
            if result_code == "-3313":
                logger.info(
                    "[11번가] 발송처리: 이미 배송중 상태 (dlvNo=%s) — 성공 처리", dlv_no
                )
                return True
            raise ElevenstApiError(f"발송처리 에러 ({result_code}): {result_text}")

        return True

    async def get_order_status(self, ord_no: str) -> dict[str, Any]:
        """주문번호별 배송/상태 조회.

        Args:
            ord_no: 주문번호

        Returns:
            ordPrdStat 포함 주문 상태 dict
            상태 코드: 202=결제완료, 301=발주확인, 401=발송완료,
                       501=배송완료, 901=수취확인, A01=반품완료, B01=주문취소
        """
        import re as _re

        url = f"https://api.11st.co.kr/rest/claimservice/orderlistalladdr/{ord_no}"
        headers = self._headers()

        client = _get_elevenst_http_client(self.api_key)
        resp = await client.get(url, headers=headers)
        logger.info("[11번가] 주문상태 조회 ordNo=%s → %s", ord_no, resp.status_code)

        if not resp.is_success:
            raise ElevenstApiError(
                f"주문상태 조회 HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            text = resp.content.decode("euc-kr")
        except Exception:
            text = resp.text

        xml_text = text.replace("ns2:", "")
        xml_text = _re.sub(r"<\?xml[^?]*\?>", "", xml_text, count=1).strip()
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.error("[11번가] 주문상태 XML 파싱 실패: %s", e)
            return {}

        result: dict[str, Any] = {}
        for child in root:
            result[child.tag] = (child.text or "").strip()
        return result

    async def get_outbound_addresses(self) -> list[dict[str, str]]:
        """출고지 주소 목록 조회. GET /rest/areaservice/outboundarea"""
        return await self._get_area_addresses("outboundarea")

    async def get_inbound_addresses(self) -> list[dict[str, str]]:
        """반품/교환지 주소 목록 조회. GET /rest/areaservice/inboundarea"""
        return await self._get_area_addresses("inboundarea")

    async def get_dispatch_templates(self) -> list[dict[str, str]]:
        """발송마감 템플릿 목록 조회.

        11번가 OpenAPI 공식 스펙:
          GET /rest/prodservices/sendCloseList
          응답: <productInformationTemplateList><templateBOList>...

        반환 항목 구조 (호출부 호환을 위해 키명 유지):
            - tmpltNo: 템플릿번호 (prdInfoTmpltNo)
            - tmpltNm: 템플릿명 (prdInfoTmpltNm)
            - reprYn:  대표마감시간 설정 유무 (repCloseTimeYn, Y/N)
        """
        from xml.etree import ElementTree as ET

        url = "https://api.11st.co.kr/rest/prodservices/sendCloseList"
        headers = self._headers()
        client = _get_elevenst_http_client(self.api_key)

        try:
            resp = await client.get(url, headers=headers)
        except Exception as exc:
            logger.warning("[11번가] 발송마감 템플릿 호출 실패: %s", exc)
            return []

        logger.info(
            "[11번가] GET /rest/prodservices/sendCloseList → %s", resp.status_code
        )
        if not resp.is_success:
            return []

        xml_text = resp.text.replace("ns2:", "")
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.warning("[11번가] 발송마감 템플릿 XML 파싱 실패: %s", exc)
            return []

        templates: list[dict[str, str]] = []
        for el in root.findall(".//templateBOList"):
            tmplt_no = (el.findtext("prdInfoTmpltNo") or "").strip()
            if not tmplt_no:
                continue
            templates.append(
                {
                    "tmpltNo": tmplt_no,
                    "tmpltNm": (el.findtext("prdInfoTmpltNm") or "").strip(),
                    "reprYn": (el.findtext("repCloseTimeYn") or "N").strip(),
                }
            )
        if templates:
            logger.info("[11번가] 발송마감 템플릿 %d건", len(templates))
        else:
            logger.info(
                "[11번가] 발송마감 템플릿 0건 — 셀러오피스에 등록된 템플릿 없음"
            )
        return templates

    async def _get_area_addresses(self, area_type: str) -> list[dict[str, str]]:
        """출고지/반품지 주소 조회 공통 메서드."""
        from xml.etree import ElementTree as ET

        url = f"https://api.11st.co.kr/rest/areaservice/{area_type}"
        headers = self._headers()
        client = _get_elevenst_http_client(self.api_key)
        resp = await client.get(url, headers=headers)
        logger.info("[11번가] GET /areaservice/%s → %s", area_type, resp.status_code)

        if not resp.is_success:
            logger.warning(
                "[11번가] %s 조회 실패: HTTP %s", area_type, resp.status_code
            )
            return []

        # XML 파싱 (네임스페이스 제거)
        xml_text = resp.text.replace("ns2:", "")
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            logger.error("[11번가] %s XML 파싱 실패", area_type)
            return []

        # result_message 확인
        result_msg = root.findtext("result_message", "")
        if result_msg and result_msg != "SUCCESS":
            logger.warning("[11번가] %s 결과: %s", area_type, result_msg)
            return []

        addresses = []
        for addr_el in root.findall("inOutAddress"):
            addr = {
                "addr": (addr_el.findtext("addr") or "").strip(),
                "addrNm": (addr_el.findtext("addrNm") or "").strip(),
                "addrSeq": (addr_el.findtext("addrSeq") or "").strip(),
                "rcvrNm": (addr_el.findtext("rcvrNm") or "").strip(),
                "gnrlTlphnNo": (addr_el.findtext("gnrlTlphnNo") or "").strip(),
                "prtblTlphnNo": (addr_el.findtext("prtblTlphnNo") or "").strip(),
            }
            if addr["addr"]:
                addresses.append(addr)

        logger.info("[11번가] %s 조회 완료: %d건", area_type, len(addresses))
        return addresses

    # ------------------------------------------------------------------
    # 취소 처리
    # ------------------------------------------------------------------

    async def get_cancel_requests(
        self, start_time: str, end_time: str
    ) -> list[dict[str, Any]]:
        """기간별 취소 요청 목록 조회.

        Args:
            start_time: 검색시작일 YYYYMMDDhhmm
            end_time:   검색종료일 YYYYMMDDhhmm
            최대 조회 기간: 30일 제한 → 초과 시 자동 분할 조회
        """
        from datetime import timedelta

        fmt = "%Y%m%d%H%M"
        start_dt = datetime.strptime(start_time, fmt)
        end_dt = datetime.strptime(end_time, fmt)

        all_items: list[dict[str, Any]] = []
        chunk_start = start_dt
        while chunk_start < end_dt:
            chunk_end = min(chunk_start + timedelta(days=30), end_dt)
            chunk_items = await self._fetch_claim_list(
                "cancelorders", chunk_start.strftime(fmt), chunk_end.strftime(fmt)
            )
            all_items.extend(chunk_items)
            chunk_start = chunk_end

        logger.info("[11번가] 취소 요청 목록 조회 완료: %d건", len(all_items))
        return all_items

    async def confirm_cancel(
        self,
        ord_prd_cn_seq: str,
        ord_no: str,
        ord_prd_seq: str,
    ) -> bool:
        """취소 승인 처리.

        Args:
            ord_prd_cn_seq: 클레임번호 (취소요청코드)
            ord_no:         주문번호
            ord_prd_seq:    주문순번

        Returns:
            True if 취소승인 성공
        """
        import re as _re

        url = (
            f"https://api.11st.co.kr/rest/claimservice/cancelreqconf"
            f"/{ord_prd_cn_seq}/{ord_no}/{ord_prd_seq}"
        )
        headers = self._headers()

        client = _get_elevenst_http_client(self.api_key)
        resp = await client.get(url, headers=headers)
        logger.info(
            "[11번가] 취소승인 ordPrdCnSeq=%s ordNo=%s → %s",
            ord_prd_cn_seq,
            ord_no,
            resp.status_code,
        )

        if not resp.is_success:
            raise ElevenstApiError(
                f"취소승인 HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            text = resp.content.decode("euc-kr")
        except Exception:
            text = resp.text

        xml_text = text.replace("ns2:", "").replace("s2:", "")
        xml_text = _re.sub(r"<\?xml[^?]*\?>", "", xml_text, count=1).strip()
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            raise ElevenstApiError(f"취소승인 응답 XML 파싱 실패: {text[:200]}")

        result_code = root.findtext("result_code", "")
        result_text = root.findtext("result_text", "")
        logger.info(
            "[11번가] 취소승인 결과: code=%s, text=%s", result_code, result_text
        )

        if result_code and result_code != "0":
            raise ElevenstApiError(f"취소승인 에러 ({result_code}): {result_text}")

        return True

    async def reject_cancel(
        self,
        ord_prd_cn_seq: str,
        ord_no: str,
        ord_prd_seq: str,
        dlv_mthd_cd: str = "01",
        dlv_etprs_cd: str = "00034",
        invc_no: str = "0000000000",
        reject_reason_cd: str = "02",
        reject_reason: str = "배송 준비 중",
    ) -> bool:
        """취소 거절 처리.

        Args:
            ord_prd_cn_seq:  클레임번호 (취소요청코드)
            ord_no:          주문번호
            ord_prd_seq:     주문상품순번
            dlv_mthd_cd:     배송방식코드 (01=택배)
            dlv_etprs_cd:    택배사코드 (00034=CJ대한통운)
            invc_no:         송장번호
            reject_reason_cd: 거절사유코드 (01=이미발송, 02=배송준비중, 03=기타)
            reject_reason:   거절사유 텍스트

        Returns:
            True if 취소거절 성공
        """
        import re as _re

        import urllib.parse

        send_dt = now_kst().strftime("%Y%m%d")
        encoded_reason = urllib.parse.quote(reject_reason, safe="")
        # 구버전 GET (ordNo/ordPrdSeq/ordPrdCnSeq/dlvMthdCd/sendDt/dlvEtprsCd/invcNo)
        url = (
            f"https://api.11st.co.kr/rest/claimservice/cancelreqreject"
            f"/{ord_no}/{ord_prd_seq}/{ord_prd_cn_seq}"
            f"/{dlv_mthd_cd}/{send_dt}/{dlv_etprs_cd}/{invc_no}"
        )
        headers = self._headers()

        logger.info("[11번가] 취소거절 URL: %s", url)
        client = _get_elevenst_http_client(self.api_key)
        resp = await client.get(url, headers=headers)
        logger.info(
            "[11번가] 취소거절 ordPrdCnSeq=%s ordNo=%s → %s",
            ord_prd_cn_seq,
            ord_no,
            resp.status_code,
        )

        if not resp.is_success:
            logger.warning("[11번가] 취소거절 응답: %s", resp.text[:500])
            raise ElevenstApiError(
                f"취소거절 HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            text = resp.content.decode("euc-kr")
        except Exception:
            text = resp.text

        xml_text = text.replace("ns2:", "").replace("s2:", "")
        xml_text = _re.sub(r"<\?xml[^?]*\?>", "", xml_text, count=1).strip()
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            raise ElevenstApiError(f"취소거절 응답 XML 파싱 실패: {text[:200]}")

        result_code = root.findtext("result_code", "")
        result_text = root.findtext("result_text", "")
        logger.info(
            "[11번가] 취소거절 결과: code=%s, text=%s", result_code, result_text
        )

        if result_code and result_code != "0":
            # 이미 취소거부 상태이면 성공으로 처리
            if "취소거부" in (result_text or ""):
                logger.info(
                    "[11번가] 취소거절: 이미 취소거부 상태 — 성공 처리 (ordNo=%s)",
                    ord_no,
                )
                return True
            raise ElevenstApiError(f"취소거절 에러 ({result_code}): {result_text}")

        return True

    async def reject_order(
        self,
        ord_no: str,
        ord_prd_seq: str,
        ord_cn_rsn_cd: str = "10",
        ord_cn_dtls_rsn: str = "구매자 요청으로 취소 처리",
    ) -> bool:
        """판매불가처리 (판매자 주도 주문 취소).

        공식 API: GET /rest/claimservice/reqrejectorder/{ordNo}/{ordPrdSeq}/{ordCnRsnCd}/{ordCnDtlsRsn}

        Args:
            ord_no:          주문번호
            ord_prd_seq:     주문상품순번
            ord_cn_rsn_cd:   취소사유코드
                             06=배송지연예상, 07=상품/가격정보오류,
                             08=상품품절(전체옵션), 09=옵션품절(해당옵션),
                             10=고객변심(신용점수 차감 없음, 기본값), 99=기타
            ord_cn_dtls_rsn: 사유 텍스트

        Returns:
            True if 판매불가처리 성공

        Note:
            사유코드 10(고객변심) 외에는 신용점수 -1점 차감.
            구매자 동의 없이 악의적 취소 시 -5점 + 고객센터 제재.
        """
        import re as _re
        import urllib.parse

        encoded_reason = urllib.parse.quote(ord_cn_dtls_rsn, safe="")
        url = (
            f"https://api.11st.co.kr/rest/claimservice/reqrejectorder"
            f"/{ord_no}/{ord_prd_seq}/{ord_cn_rsn_cd}/{encoded_reason}"
        )
        headers = self._headers()

        logger.info(
            "[11번가] 판매불가처리 ordNo=%s ordPrdSeq=%s rsnCd=%s",
            ord_no,
            ord_prd_seq,
            ord_cn_rsn_cd,
        )
        client = _get_elevenst_http_client(self.api_key)
        resp = await client.get(url, headers=headers)

        if not resp.is_success:
            logger.warning("[11번가] 판매불가처리 응답: %s", resp.text[:500])
            raise ElevenstApiError(
                f"판매불가처리 HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            text = resp.content.decode("euc-kr")
        except Exception:
            text = resp.text

        xml_text = text.replace("ns2:", "").replace("s2:", "")
        xml_text = _re.sub(r"<\?xml[^?]*\?>", "", xml_text, count=1).strip()
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            raise ElevenstApiError(f"판매불가처리 응답 XML 파싱 실패: {text[:200]}")

        result_code = root.findtext("result_code", "")
        result_text = root.findtext("result_text", "")
        logger.info(
            "[11번가] 판매불가처리 결과: code=%s, text=%s", result_code, result_text
        )

        if result_code and result_code != "0":
            # 이미 취소된 주문이면 성공 처리
            if any(
                k in (result_text or "")
                for k in ["이미", "취소완료", "처리 가능한 주문이 아닙니다"]
            ):
                logger.info(
                    "[11번가] 판매불가처리: 이미 처리된 주문 — 성공 처리 (ordNo=%s)",
                    ord_no,
                )
                return True
            raise ElevenstApiError(f"판매불가처리 에러 ({result_code}): {result_text}")

        return True

    # ------------------------------------------------------------------
    # 반품 처리
    # ------------------------------------------------------------------

    async def get_return_requests(
        self, start_time: str, end_time: str
    ) -> list[dict[str, Any]]:
        """기간별 반품 요청 목록 조회.

        Args:
            start_time: 검색시작일 YYYYMMDDhhmm
            end_time:   검색종료일 YYYYMMDDhhmm
            최대 조회 기간: 30일 제한 → 초과 시 자동 분할 조회
        """
        from datetime import timedelta

        fmt = "%Y%m%d%H%M"
        start_dt = datetime.strptime(start_time, fmt)
        end_dt = datetime.strptime(end_time, fmt)

        all_items: list[dict[str, Any]] = []
        chunk_start = start_dt
        while chunk_start < end_dt:
            chunk_end = min(chunk_start + timedelta(days=30), end_dt)
            chunk_items = await self._fetch_claim_list(
                "returnorders", chunk_start.strftime(fmt), chunk_end.strftime(fmt)
            )
            all_items.extend(chunk_items)
            chunk_start = chunk_end

        logger.info("[11번가] 반품 요청 목록 조회 완료: %d건", len(all_items))
        return all_items

    async def confirm_return(
        self,
        clm_req_seq: str,
        ord_no: str,
        ord_prd_seq: str,
    ) -> bool:
        """반품 승인 처리.

        Args:
            clm_req_seq:  클레임번호 (반품요청코드)
            ord_no:       주문번호
            ord_prd_seq:  주문순번

        Returns:
            True if 반품승인 성공
        """
        import re as _re

        url = (
            f"https://api.11st.co.kr/rest/claimservice/returnreqconf"
            f"/{clm_req_seq}/{ord_no}/{ord_prd_seq}"
        )
        headers = self._headers()

        client = _get_elevenst_http_client(self.api_key)
        resp = await client.get(url, headers=headers)
        logger.info(
            "[11번가] 반품승인 clmReqSeq=%s ordNo=%s → %s",
            clm_req_seq,
            ord_no,
            resp.status_code,
        )

        if not resp.is_success:
            raise ElevenstApiError(
                f"반품승인 HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            text = resp.content.decode("euc-kr")
        except Exception:
            text = resp.text

        xml_text = text.replace("ns2:", "").replace("s2:", "")
        xml_text = _re.sub(r"<\?xml[^?]*\?>", "", xml_text, count=1).strip()
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            raise ElevenstApiError(f"반품승인 응답 XML 파싱 실패: {text[:200]}")

        result_code = root.findtext("result_code", "")
        result_text = root.findtext("result_text", "")
        logger.info(
            "[11번가] 반품승인 결과: code=%s, text=%s", result_code, result_text
        )

        if result_code and result_code != "0":
            raise ElevenstApiError(f"반품승인 에러 ({result_code}): {result_text}")

        return True

    async def reject_return(
        self,
        clm_req_seq: str,
        ord_no: str,
        ord_prd_seq: str,
        refs_rsn_cd: str = "104",
        refs_rsn: str = "기타",
    ) -> bool:
        """반품 거부 처리.

        11번가 API: GET /rest/claimservice/returnreqreject/{ordNo}/{ordPrdSeq}/{clmReqSeq}/{refsRsnCd}/{refsRsn}

        Args:
            clm_req_seq:  클레임번호
            ord_no:       주문번호
            ord_prd_seq:  주문순번
            refs_rsn_cd:  사유코드 (101=반품상품미입고, 102=고객반품청취대행, 103=반품불가상품, 104=기타)
            refs_rsn:     사유텍스트

        Returns:
            True if 반품거부 성공
        """
        import re as _re
        import urllib.parse

        encoded_rsn = urllib.parse.quote(refs_rsn, safe="")
        url = (
            f"https://api.11st.co.kr/rest/claimservice/returnreqreject"
            f"/{ord_no}/{ord_prd_seq}/{clm_req_seq}/{refs_rsn_cd}/{encoded_rsn}"
        )
        headers = self._headers()

        client = _get_elevenst_http_client(self.api_key)
        resp = await client.get(url, headers=headers)
        logger.info(
            "[11번가] 반품거부 clmReqSeq=%s ordNo=%s → %s",
            clm_req_seq,
            ord_no,
            resp.status_code,
        )

        if not resp.is_success:
            raise ElevenstApiError(
                f"반품거부 HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            text = resp.content.decode("euc-kr")
        except Exception:
            text = resp.text

        xml_text = text.replace("ns2:", "").replace("s2:", "")
        xml_text = _re.sub(r"<\?xml[^?]*\?>", "", xml_text, count=1).strip()
        logger.info("[11번가] 반품거부 원시 응답: %s", xml_text[:300])
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            raise ElevenstApiError(f"반품거부 응답 XML 파싱 실패: {text[:200]}")

        # 응답 루트가 ResultOrder인 경우 처리
        result_node = root if root.tag != "ResultOrder" else root
        result_code = result_node.findtext("result_code", "") or result_node.findtext(
            "ResultCode", ""
        )
        result_text = result_node.findtext("result_text", "") or result_node.findtext(
            "ResultText", ""
        )
        logger.info(
            "[11번가] 반품거부 결과: code=%s, text=%s", result_code, result_text
        )

        if result_code and result_code != "0":
            raise ElevenstApiError(f"반품거부 에러 ({result_code}): {result_text}")

        return True

    # ------------------------------------------------------------------
    # 상품 Q&A (고객 문의)
    # ------------------------------------------------------------------

    async def get_qna_list(
        self, start_dt: Optional[str] = None, end_dt: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """상품 Q&A 목록 조회.

        Args:
            start_dt: 검색시작일 YYYYMMDD (기본: 7일 전)
            end_dt:   검색종료일 YYYYMMDD (기본: 오늘)

        Returns:
            Q&A 항목 리스트 (brdInfoNo, brdInfoCont, answerCont, answerYn, prdNm 등)
        """
        import re as _re
        from datetime import timedelta

        if not end_dt:
            end_dt = now_kst().strftime("%Y%m%d")
        if not start_dt:
            start_dt = (now_kst() - timedelta(days=7)).strftime("%Y%m%d")

        # answerStatus: 00=전체, 01=답변완료, 02=미답변
        url = f"https://api.11st.co.kr/rest/prodqnaservices/prodqnalist/{start_dt}/{end_dt}/00"
        headers = self._headers()

        client = _get_elevenst_http_client(self.api_key)
        resp = await client.get(url, headers=headers)
        logger.info("[11번가] Q&A 목록 조회 → %s", resp.status_code)

        if not resp.is_success:
            raise ElevenstApiError(
                f"Q&A 목록 HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            text = resp.content.decode("euc-kr")
        except Exception:
            text = resp.text

        # ns2: 네임스페이스 제거
        xml_text = text.replace("ns2:", "").replace("s2:", "")
        xml_text = _re.sub(r"<\?xml[^?]*\?>", "", xml_text, count=1).strip()
        logger.info("[11번가] Q&A 원시 응답(500자): %s", xml_text[:500])

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.error("[11번가] Q&A XML 파싱 실패: %s", e)
            return []

        result_code = root.findtext("result_code", "")
        if result_code:
            result_text = root.findtext("result_text", "")
            logger.info(
                "[11번가] Q&A result_code=%s, result_text=%s", result_code, result_text
            )
            # "0" 또는 "-1"은 정상 응답(결과 없음 포함) → 빈 리스트 반환
            if result_code in ("0", "-1"):
                return []
            raise ElevenstApiError(f"Q&A 조회 에러 ({result_code}): {result_text}")

        items: list[dict[str, Any]] = []
        for el in root.findall("productQna"):
            item: dict[str, Any] = {}
            for child in el:
                item[child.tag] = (child.text or "").strip()
            items.append(item)

        logger.info("[11번가] Q&A %d건 조회", len(items))
        return items

    async def get_urgent_inquiry_list(
        self, start_dt: Optional[str] = None, end_dt: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """긴급알림(긴급문의) 목록 조회.

        Args:
            start_dt: 검색시작일 YYYYMMDD
            end_dt:   검색종료일 YYYYMMDD

        Returns:
            긴급알림 항목 리스트
        """
        import re as _re
        from datetime import timedelta

        if not end_dt:
            end_dt = now_kst().strftime("%Y%m%d")
        if not start_dt:
            start_dt = (now_kst() - timedelta(days=7)).strftime("%Y%m%d")

        # 미확인(01) + 답변대기(02) 상태 수집
        # API: GET /rest/alimi/getalimilist/{startTime}/{endTime}/{emerNtceCrntCd}/
        headers = self._headers()
        items: list[dict[str, Any]] = []

        for status_cd in ("01", "02"):
            url = f"https://api.11st.co.kr/rest/alimi/getalimilist/{start_dt}/{end_dt}/{status_cd}"
            try:
                client = _get_elevenst_http_client(self.api_key)
                resp = await client.get(url, headers=headers)
                logger.info(
                    "[11번가] 긴급알리미(%s) 조회 → %s", status_cd, resp.status_code
                )
            except Exception as e:
                logger.warning("[11번가] 긴급알리미 API 요청 실패: %s", e)
                continue

            if not resp.is_success:
                logger.warning(
                    "[11번가] 긴급알리미 HTTP %s: %s", resp.status_code, resp.text[:300]
                )
                continue

            try:
                text = resp.content.decode("euc-kr")
            except Exception:
                text = resp.text

            xml_text = text.replace("ns2:", "").replace("s2:", "")
            xml_text = _re.sub(r"<\?xml[^?]*\?>", "", xml_text, count=1).strip()
            logger.info(
                "[11번가] 긴급알리미(%s) 원시 응답(500자): %s",
                status_cd,
                xml_text[:500],
            )

            try:
                root = ET.fromstring(xml_text)
            except ET.ParseError as e:
                logger.error("[11번가] 긴급알리미 XML 파싱 실패: %s", e)
                continue

            # result_code 존재 시 결과 없음(0) 또는 에러 → 건너뜀
            result_code = root.findtext("result_code", "")
            if result_code:
                logger.info(
                    "[11번가] 긴급알리미(%s) result_code=%s", status_cd, result_code
                )
                continue

            for el in root.findall("alimListInfo"):
                item: dict[str, Any] = {}
                for child in el:
                    val = (child.text or "").strip()
                    if child.tag == "emerCtnt":
                        val = _clean_alimi_content(val)
                    item[child.tag] = val
                items.append(item)

        logger.info("[11번가] 긴급알리미 총 %d건 조회", len(items))
        return items

    async def reply_qna(self, brd_info_no: str, prd_no: str, answer: str) -> bool:
        """Q&A 답변 등록.

        Args:
            brd_info_no: QnA 글번호 (brdInfoNo)
            prd_no:      상품번호 (brdInfoClfNo)
            answer:      답변 내용

        Returns:
            True if 성공
        """
        import re as _re

        url = f"https://api.11st.co.kr/rest/prodqnaservices/prodqnaanswer/{brd_info_no}/{prd_no}"
        headers = self._headers()
        xml_body = f"<?xml version='1.0' encoding='UTF-8'?><ProductQna><answerCont>{answer}</answerCont></ProductQna>"

        client = _get_elevenst_http_client(self.api_key)
        resp = await client.put(url, headers=headers, content=xml_body.encode("utf-8"))
        logger.info(
            "[11번가] Q&A 답변 brdInfoNo=%s → %s", brd_info_no, resp.status_code
        )

        if not resp.is_success:
            raise ElevenstApiError(
                f"Q&A 답변 HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            text = resp.content.decode("euc-kr")
        except Exception:
            text = resp.text

        xml_text = _re.sub(r"<\?xml[^?]*\?>", "", text, count=1).strip()
        logger.info("[11번가] Q&A 답변 원시 응답: %s", xml_text[:300])

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            raise ElevenstApiError(f"Q&A 답변 응답 파싱 실패: {text[:200]}")

        result_code = root.findtext("resultCode", "")
        if result_code and result_code != "200":
            message = root.findtext("message", "")
            raise ElevenstApiError(f"Q&A 답변 에러 ({result_code}): {message}")

        return True

    async def confirm_alimi(self, emer_ntce_seq: str) -> bool:
        """긴급알리미 확인처리.

        API: PUT https://api.11st.co.kr/rest/alimi/alimianswer
        result_code 100(공지확인) / 200(답변확인) = 성공
        """
        import re as _re2

        url = "https://api.11st.co.kr/rest/alimi/alimianswer"
        headers = {**self._headers(), "Content-Type": "application/xml; charset=UTF-8"}
        xml_body = (
            f"<?xml version='1.0' encoding='UTF-8'?>"
            f"<request><confirmYn>Y</confirmYn><emerNtceSeq>{emer_ntce_seq}</emerNtceSeq></request>"
        )
        client = _get_elevenst_http_client(self.api_key)
        resp = await client.put(url, headers=headers, content=xml_body.encode("utf-8"))
        logger.info(
            "[11번가] 긴급알리미 확인처리 seq=%s → %s", emer_ntce_seq, resp.status_code
        )

        try:
            resp_text = resp.content.decode("euc-kr")
        except Exception:
            resp_text = resp.text

        if not resp.is_success:
            raise ElevenstApiError(
                f"긴급알리미 확인처리 HTTP {resp.status_code}: {resp_text[:300]}"
            )

        xml_clean = _re2.sub(r"<\?xml[^?]*\?>", "", resp_text, count=1).strip()
        try:
            root = ET.fromstring(xml_clean)
            result_code = root.findtext("result_code", "")
            # 100(공지확인성공), 200(답변확인성공), -10005(이미처리됨) → 모두 성공으로 간주
            if result_code and result_code not in ("100", "200", "-10005"):
                result_text = root.findtext("result_text", "")
                raise ElevenstApiError(
                    f"긴급알리미 확인처리 에러 ({result_code}): {result_text}"
                )
        except ElevenstApiError:
            raise
        except Exception:
            pass

        return True

    async def _fetch_claim_list(
        self, claim_type: str, start_time: str, end_time: str
    ) -> list[dict[str, Any]]:
        """취소/반품 목록 단일 구간 조회 공통 메서드.

        Args:
            claim_type: 'cancelorders' 또는 'returnorders'
            start_time: YYYYMMDDhhmm
            end_time:   YYYYMMDDhhmm
        """
        import re as _re

        url = f"https://api.11st.co.kr/rest/claimservice/{claim_type}/{start_time}/{end_time}"
        headers = self._headers()

        client = _get_elevenst_http_client(self.api_key)
        resp = await client.get(url, headers=headers)
        logger.info(
            "[11번가] GET /claimservice/%s/%s/%s → %s",
            claim_type,
            start_time,
            end_time,
            resp.status_code,
        )

        if not resp.is_success:
            raise ElevenstApiError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            text = resp.content.decode("euc-kr")
        except Exception:
            text = resp.text

        # 네임스페이스 + XML 선언 제거
        xml_text = text.replace("ns2:", "").replace("s2:", "")
        xml_text = _re.sub(r"<\?xml[^?]*\?>", "", xml_text, count=1).strip()
        logger.info("[11번가] %s 원시 응답(500자): %s", claim_type, xml_text[:500])

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.error("[11번가] %s XML 파싱 실패: %s", claim_type, e)
            return []

        # result_code 확인 (0=결과없음 정상, 음수=에러)
        result_code = root.findtext("result_code", "")
        if result_code:
            if result_code in ("0", "-1"):
                # 0 또는 -1 + "해당 건이 없습니다" → 결과 없음 (정상)
                result_text = root.findtext("result_text", "")
                if result_code == "-1" and "없습니다" not in (result_text or ""):
                    raise ElevenstApiError(
                        f"{claim_type} 조회 에러 ({result_code}): {result_text}"
                    )
                return []
            result_text = root.findtext("result_text", "")
            raise ElevenstApiError(
                f"{claim_type} 조회 에러 ({result_code}): {result_text}"
            )

        items: list[dict[str, Any]] = []
        for order_el in root.findall("order"):
            item: dict[str, Any] = {}
            for child in order_el:
                item[child.tag] = (child.text or "").strip()
            items.append(item)

        return items

    # ------------------------------------------------------------------
    # 상품 데이터 변환 (수집 상품 → 11번가 XML 형식)
    # ------------------------------------------------------------------

    @staticmethod
    def transform_product(
        product: dict[str, Any],
        category_code: str = "",
        settings: Optional[dict[str, Any]] = None,
    ) -> str:
        """SambaCollectedProduct → 11번가 상품 등록 XML 변환.

        settings: 계정의 additional_fields (배송비, 출고지, 반품지 등)
        """
        cfg = settings or {}
        name = _clean_product_name(product.get("name", ""))
        sale_price = (int(product.get("sale_price", 0)) // 100) * 100
        _orig = int(product.get("original_price", 0) or 0)
        # maktPrc(정가)는 판매가 이상이어야 함 — 100원 내림
        makt_prc = (_orig // 100) * 100 if _orig > sale_price else sale_price
        images = product.get("images") or []
        detail_images = product.get("detail_images") or []

        # 11번가 이미지 필터: 3MB 초과 가능성 있는 이미지 제외
        # - notice/공지 이미지: 300x300 미만으로 거부되거나 용량 초과
        # - old.millet.co.kr/data/goods_set: 원본 고해상도 이미지로 3MB 초과 가능
        # msscdn.net 썸네일(_500.jpg 등)만 안정적으로 사용
        def _is_valid_detail_image(url: str) -> bool:
            """3MB 초과 가능성 있는 이미지 제외."""
            lower = url.lower()
            if "/notice/" in lower or "notice" in lower.split("/")[-1]:
                return False
            # 밀레 원본 고해상도 이미지 (goods_set) 제외
            if "old.millet.co.kr" in lower and "/data/goods_set/" in lower:
                return False
            return True

        _img_tag = '<div style="text-align:center;"><img src="{url}" style="max-width:860px;width:100%;" /></div>'
        # 정책 기반으로 shipment service가 사전 생성한 detail_html을 최우선 사용
        # (상단/하단 템플릿 이미지·순서·체크옵션 반영). 없을 때만 이미지로 폴백.
        _policy_detail_html = (product.get("detail_html") or "").strip()
        if _policy_detail_html:
            detail_html = _policy_detail_html
        else:
            _html_parts = [
                _img_tag.format(url=u) for u in images if _is_valid_detail_image(u)
            ]
            _html_parts += [
                _img_tag.format(url=u)
                for u in detail_images
                if _is_valid_detail_image(u)
            ]
            detail_html = "\n".join(_html_parts) if _html_parts else f"<p>{name}</p>"
        brand = product.get("brand", "")

        # 아동 의류 여부 판별 (KC인증 분기용)
        _kids_keywords = {
            "키즈",
            "kids",
            "kid",
            "아동",
            "유아",
            "베이비",
            "baby",
            "주니어",
            "junior",
            "jr",
            "어린이",
            "infant",
            "toddler",
        }
        _check_text = " ".join(
            filter(
                None,
                [
                    brand,
                    product.get("category", ""),
                    product.get("category1", ""),
                    product.get("category2", ""),
                    product.get("name", ""),
                ],
            )
        ).lower()
        is_kids = any(kw in _check_text for kw in _kids_keywords)
        kc_kids_code = "01" if is_kids else "03"

        # 계정 설정값 (없으면 기본값)
        tax_type = cfg.get("taxType", "01")
        # 배송비 종류 코드 변환 (구 문자열 → 11번가 공식 숫자 코드)
        # 01=무료, 02=고정, 03=상품조건부무료, 05=1개당, 07=판매자조건부, 08=출고지조건부, 09=통합출고지
        _dlv_code_map = {"DV_FREE": "01", "DV_FIXED": "02", "DV_COND": "03"}
        raw_dlv = cfg.get("deliveryType", "01")
        delivery_type = _dlv_code_map.get(raw_dlv, raw_dlv) or "01"
        delivery_fee = int(cfg.get("deliveryFee", 0) or 0)
        return_fee = int(cfg.get("returnFee", 4000) or 4000)
        exchange_fee = int(cfg.get("exchangeFee", 8000) or 8000)
        # 제주/도서산간 추가배송비 — 11번가 등록 XML 필수 항목
        jeju_fee = int(cfg.get("jejuFee", 0) or 0)
        island_fee = int(cfg.get("islandFee", 0) or 0)
        ship_from = cfg.get("shipFromAddress", "")
        return_addr = cfg.get("returnAddress", "")
        # 계정 설정 origin이 '기타'이면 무시하고 상품 실제 원산지 우선 사용
        cfg_origin = cfg.get("origin") or ""
        if cfg_origin == "기타":
            cfg_origin = ""
        origin_raw = cfg_origin or product.get("origin") or ""
        orgn_typ_cd, orgn_dtls_cd, orgn_nm_val = _resolve_origin(origin_raw)
        as_phone = product.get("_as_phone") or cfg.get("asPhone") or ""
        _cfg_as_msg = cfg.get("asMessage", "") or ""
        # "상세페이지 참조" 는 기본값으로 간주 → 전화번호 우선
        if _cfg_as_msg and _cfg_as_msg != "상세페이지 참조":
            as_message = _cfg_as_msg
        elif as_phone:
            as_message = f"A/S 문의: {as_phone}"
        else:
            as_message = "상세페이지 참조"
        return_exchange = cfg.get("returnExchangeGuide", "") or "상세페이지 참조"
        minor_restrict = cfg.get("minorRestrict", "Y")

        # 11Pay 포인트 적립 설정 (계정별 on/off)
        # 셀러오피스 form 필드명 확인: pay11YN, pay11Value, pay11WyCd
        # pay11WyCd=02: 정액(원), pay11WyCd=01: 정률(%)
        llpay_pnt_yn = (
            "Y"
            if cfg.get("llpayPointEnabled")
            and str(cfg.get("llpayPointEnabled")) not in ("", "false", "0")
            else "N"
        )
        llpay_pnt_type = str(
            cfg.get("llpayPointType", "02") or "02"
        )  # 02=정액, 01=정률
        llpay_pnt_value = int(cfg.get("llpayPointValue", 100) or 100)

        # 복수구매 할인 설정 (계정별 on/off)
        # 셀러오피스 form 필드명: pluYN, pluDscCd(기준유형), pluDscMthdCd(할인방식), pluDscBasis(기준값), pluDscAmtPercnt(할인값)
        # pluDscCd: 01=수량기준, 02=금액기준
        # pluDscMthdCd: 02=정액(원), 01=정률(%)
        mnp_buy_basis_type = str(cfg.get("multiPurchaseBasisType", "01") or "01")
        mnp_buy_dsc_method = str(cfg.get("multiPurchaseDiscountMethod", "02") or "02")
        mnp_buy_qty = int(float(cfg.get("multiPurchaseQty") or 2))
        mnp_buy_amt = int(float(cfg.get("multiPurchaseAmt") or 0))
        # 복수구매할인(PLU): 스토어 설정 multiPurchaseDiscount='true' 시 활성화
        # 활성 조건: 기준값(qty) >= 1, 할인값(amt) > 0 충족 시에만 ON. 미충족이면 등록 실패 방지 위해 OFF
        _mnp_enabled_raw = cfg.get("multiPurchaseDiscount")
        _mnp_enabled = bool(_mnp_enabled_raw) and str(_mnp_enabled_raw).lower() not in (
            "",
            "false",
            "0",
            "n",
        )
        if _mnp_enabled and mnp_buy_qty >= 1 and mnp_buy_amt > 0:
            mnp_buy_yn = "Y"
        else:
            mnp_buy_yn = "N"
            if _mnp_enabled:
                # '설정함'인데 값 누락 — silent disable 가시화 (UX 함정 방어)
                logger.warning(
                    "[11번가] 복수구매할인 '설정함'이지만 값 누락 — PLU OFF로 등록 "
                    f"(qty={mnp_buy_qty}, amt={mnp_buy_amt}). 스토어 설정에서 'N개 이상'과 '개당 할인값'을 입력하세요."
                )
        mnp_period_yn = (
            "Y"
            if cfg.get("multiPurchasePeriodEnabled")
            and str(cfg.get("multiPurchasePeriodEnabled")) not in ("", "false", "0")
            else "N"
        )
        mnp_start_dy = str(cfg.get("multiPurchaseStartDate", "") or "")
        mnp_end_dy = str(cfg.get("multiPurchaseEndDate", "") or "")
        # 종료일이 오늘 이전이면 기간 설정 비활성화
        if mnp_period_yn == "Y" and mnp_end_dy:
            from datetime import date

            today_str = date.today().strftime("%Y%m%d")
            if mnp_end_dy < today_str:
                mnp_period_yn = "N"

        # 즉시할인 (쿠폰) 설정 — discountRate(%) 값이 있으면 정률 쿠폰 적용
        # 11번가 API 필드: cuponcheck, dscAmtPercnt, cupnDscMthdCd(02=정률)
        _discount_rate = int(cfg.get("discountRate", 0) or 0)
        instant_dsc_yn = "Y" if _discount_rate > 0 else "N"

        # 이미지 XML — 공식 필드명: prdImage01~04 (imageUrl 아님)
        # 11번가 요구사항: 300x300 이상, jpg/png/gif (webp 거부), URL fetch 가능해야 함.
        #
        # 무신사 케이스 처리(2026-05-01):
        #   1) `_1100.jpg` 업스케일은 msscdn에 해당 사이즈 부재(404) → "잘못된 이미지" 에러
        #      → /images/.../IMG_500.jpg → /thumbnails/images/.../IMG_500.jpg?w=1100 으로 전환
        #        (thumbnails 엔드포인트가 1100px 리사이즈를 정상 제공)
        #   2) goodsImages가 부족하면 musinsa proxy가 detail_images(상세 desc_html에서 추출한
        #      배너/사이즈표 등)로 `images` 슬롯을 9개까지 채움. 이 중 작은 배너는 300x300 미달
        #      → "300x300 이상" 에러
        #      → prdImage01~04는 정품 상품 이미지(/images/goods_img/)만 허용
        #   3) webp는 11번가가 거부하므로 jpg로 정규화.
        import re as _re_img

        def _normalize_msscdn(u: str) -> str:
            if "msscdn.net" not in u:
                return u
            # webp → jpg (11번가는 webp 거부)
            u = _re_img.sub(r"(_\d+)\.webp(\?|$)", r"\1.jpg\2", u)
            # 2026-05-12: thumbnails?w=1100 치환은 msscdn 정책 변경으로 404 발생 → 원본 _500.jpg 그대로 사용
            return u

        def _ext_ok(url: str) -> bool:
            """확장자 검증 (svg/webp/gif 등 제외 — 11번가는 jpg/png 우선)."""
            if not url or not url.lower().startswith("https://"):
                return False
            path = url.lower().split("?", 1)[0]
            return any(path.endswith(ext) for ext in (".jpg", ".jpeg", ".png"))

        def _host_of(url: str) -> str:
            try:
                from urllib.parse import urlparse

                return (urlparse(url).hostname or "").lower()
            except Exception:
                return ""

        def _is_msscdn_product(url: str) -> bool:
            """msscdn의 상품 이미지 path만 인정 (배너/공지 제외).

            - /images/goods_img/ : 상품 메인 이미지
            - /images/prd_img/detail_ : 상품 상세컷 (배너 아닌 실제 상품 사진)
            - /images/prd_img/<hash>.jpg, /display/images/common/ 등 배너성은 제외
            """
            lower = url.lower()
            if "msscdn.net" not in lower:
                return False
            if "/images/goods_img/" in lower:
                return True
            # 상세컷은 detail_<상품ID>_ 패턴으로 식별 (배너성 해시 파일명 제외)
            if "/images/prd_img/" in lower and "/detail_" in lower:
                return True
            return False

        # prdImage 후보: 1차로 _is_valid_detail_image + 확장자 OK
        # 무신사(msscdn)인 경우 detail_images에 cafe24 등 hotlink 차단 호스트 배너가 섞이므로
        # /images/goods_img/ 경로만 허용 (배너/사이즈표는 detail_html에만 노출)
        # 그 외 소싱처는 첫 이미지 호스트와 일치하는 URL만 허용 (호스트 일치 = 동일 CDN의 정품 이미지일 확률 높음)
        _candidates = [u for u in images if _is_valid_detail_image(u) and _ext_ok(u)]
        if _candidates:
            _first_host = _host_of(_candidates[0])
            _first_is_msscdn = "msscdn.net" in _first_host
            if _first_is_msscdn:
                _filtered = [u for u in _candidates if _is_msscdn_product(u)]
            else:
                # 비-무신사 소싱처: 첫 이미지 호스트와 일치하는 것만 (Referer 없이 fetch 가능 가정)
                _filtered = [u for u in _candidates if _host_of(u) == _first_host]
            # 안전 폴백: 필터 결과가 비면 최소한 첫 이미지 1장은 살림
            product_images = [
                _normalize_msscdn(u) for u in (_filtered or _candidates[:1])
            ]
        else:
            product_images = []
        image_xml = ""
        if product_images:
            image_xml += f"<prdImage01>{_escape_xml(product_images[0])}</prdImage01>"
            for i, url in enumerate(product_images[1:4], start=2):
                image_xml += f"<prdImage0{i}>{_escape_xml(url)}</prdImage0{i}>"

        # 옵션 처리 — 2D(슬래시) 감지 시 멀티옵션, 그 외 싱글옵션
        # 공식 예제: singleOption1.txt / multiOption1-1.txt
        options = product.get("options") or []
        # 스토어설정 재고수량 상한: _stock_quantity(계정설정) > _max_stock(정책) 우선
        _max_stock_cap = int(cfg.get("stockQuantity") or product.get("_max_stock") or 0)
        option_xml = _build_elevenst_option_xml(
            options,
            max_stock_cap=_max_stock_cap,
            option_group_names=product.get("option_group_names"),
        )

        # 홍보문구 — 스토어설정 값 우선, 없으면 카테고리 기반 자동 생성
        # (advrtStmt 제한: 한글 14자/영문 28자)
        promo_text = _generate_promo_text(
            product, name, (cfg.get("promotionMessage") or "").strip()
        )

        # 상품정보 제공고시 XML (카테고리별 동적 생성)
        notice_xml = _build_elevenst_notice_xml(product)

        # 판매자상품코드 — 삼바 내부 product.id로 통일 (주문 역매칭 키)
        seller_prd_cd = str(product.get("id") or "").strip()

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Product>
  <sellerPrdCd>{_escape_xml(seller_prd_cd)}</sellerPrdCd>
  <prdNm>{_escape_xml(name)}</prdNm>
  <advrtStmt>{_escape_xml(promo_text)}</advrtStmt>
  <prdStatCd>01</prdStatCd>
  <prdTypCd>01</prdTypCd>
  <dispCtgrNo>{category_code}</dispCtgrNo>
  <brand>{_escape_xml(brand)}</brand>
  {f"<modelCd>{_escape_xml(_resolve_model_nm(product))}</modelCd>" if _resolve_model_nm(product) else ""}
  <maktPrc>{makt_prc}</maktPrc>
  <selPrc>{sale_price}</selPrc>
  <selMthdCd>01</selMthdCd>
  <selTermUseYn>N</selTermUseYn>
  <prdWeight>0</prdWeight>
  <rmaterialTypCd>04</rmaterialTypCd>
  <orgnTypCd>{orgn_typ_cd}</orgnTypCd>
  {f"<orgnTypDtlsCd>{orgn_dtls_cd}</orgnTypDtlsCd>" if orgn_dtls_cd else ""}
  {f"<orgnNmVal>{_escape_xml(orgn_nm_val)}</orgnNmVal>" if orgn_nm_val else ""}
  <dlvCnFee>{delivery_fee}</dlvCnFee>
  <dlvGrntYn>Y</dlvGrntYn>
  {f"<dlvSendCloseTmpltNo>{_escape_xml(str(cfg.get('dispatchTemplateNo', '')).strip())}</dlvSendCloseTmpltNo>" if str(cfg.get("dispatchTemplateNo", "")).strip() else ""}
  <dlvCstInstBasiCd>{delivery_type}</dlvCstInstBasiCd>
  <jejuDlvCst>{jeju_fee}</jejuDlvCst>
  <islandDlvCst>{island_fee}</islandDlvCst>
  <rtngdDlvCst>{return_fee}</rtngdDlvCst>
  <exchDlvCst>{exchange_fee}</exchDlvCst>
  <dlvBsPlc>{_escape_xml(ship_from)}</dlvBsPlc>
  <rtngBsPlc>{_escape_xml(return_addr)}</rtngBsPlc>
  <taxTypCd>{tax_type}</taxTypCd>
  <minorSelCnYn>{minor_restrict}</minorSelCnYn>
  <pay11YN>{llpay_pnt_yn}</pay11YN>
  {f"<pay11Value>{llpay_pnt_value}</pay11Value><pay11WyCd>{llpay_pnt_type}</pay11WyCd>" if llpay_pnt_yn == "Y" else ""}
  <pluYN>{mnp_buy_yn}</pluYN>
  {f"<pluDscCd>{mnp_buy_basis_type}</pluDscCd><pluDscMthdCd>{mnp_buy_dsc_method}</pluDscMthdCd><pluDscBasis>{mnp_buy_qty}</pluDscBasis><pluDscAmtPercnt>{mnp_buy_amt}</pluDscAmtPercnt>" if mnp_buy_yn == "Y" else ""}
  {f"<pluUseLmtDyYn>Y</pluUseLmtDyYn><pluIssStartDy>{mnp_start_dy}</pluIssStartDy><pluIssEndDy>{mnp_end_dy}</pluIssEndDy>" if mnp_buy_yn == "Y" and mnp_period_yn == "Y" and mnp_start_dy and mnp_end_dy else ""}
  {f"<cuponcheck>Y</cuponcheck><dscAmtPercnt>{_discount_rate}</dscAmtPercnt><cupnDscMthdCd>02</cupnDscMthdCd>" if instant_dsc_yn == "Y" else ""}
  <ProductCertGroup>
    <crtfGrpTypCd>01</crtfGrpTypCd>
    <crtfGrpObjClfCd>03</crtfGrpObjClfCd>
  </ProductCertGroup>
  <ProductCertGroup>
    <crtfGrpTypCd>02</crtfGrpTypCd>
    <crtfGrpObjClfCd>03</crtfGrpObjClfCd>
  </ProductCertGroup>
  <ProductCertGroup>
    <crtfGrpTypCd>03</crtfGrpTypCd>
    <crtfGrpObjClfCd>03</crtfGrpObjClfCd>
  </ProductCertGroup>
  <ProductCertGroup>
    <crtfGrpTypCd>04</crtfGrpTypCd>
    <crtfGrpObjClfCd>05</crtfGrpObjClfCd>
  </ProductCertGroup>
  <ProductCert>
    <certTypeCd>131</certTypeCd>
    <certKey></certKey>
  </ProductCert>
  {image_xml}
  <htmlDetail><![CDATA[{detail_html.replace("]]>", "]]]]><![CDATA[>")}]]></htmlDetail>
  {option_xml}
  {notice_xml}
  <asDetail>{_escape_xml(as_message)}</asDetail>
  <rtngExchDetail>{_escape_xml(return_exchange)}</rtngExchDetail>
</Product>"""
        return xml


# ──────────────────────────────────────────────────────────────
# 원산지 코드 매핑 (셀러오피스 신규상품등록 페이지 실측값 기준)
# 국내(01): orgnTypDtlsCd 불필요 (비농산물 기준)
# 해외(02): orgnTypDtlsCd = 국가코드
# 기타(03): orgnNmVal = 텍스트
# ──────────────────────────────────────────────────────────────
_ORIGIN_COUNTRY_MAP: dict[str, str] = {
    # 아시아
    "그루지야": "1254",
    "georgia": "1254",
    "네팔": "1255",
    "nepal": "1255",
    "동티모르": "1256",
    "east timor": "1256",
    "timor-leste": "1256",
    "라오스": "1257",
    "laos": "1257",
    "레바논": "1258",
    "lebanon": "1258",
    "말레이시아": "1259",
    "malaysia": "1259",
    "몰디브": "1260",
    "maldives": "1260",
    "몽골": "1261",
    "mongolia": "1261",
    "미얀마": "1262",
    "myanmar": "1262",
    "burma": "1262",
    "바레인": "1263",
    "bahrain": "1263",
    "방글라데시": "1264",
    "bangladesh": "1264",
    "베트남": "1265",
    "vietnam": "1265",
    "viet nam": "1265",
    "부탄": "1266",
    "bhutan": "1266",
    "브루나이": "1267",
    "brunei": "1267",
    "사우디아라비아": "1268",
    "saudi arabia": "1268",
    "스리랑카": "1269",
    "sri lanka": "1269",
    "시리아": "1270",
    "syria": "1270",
    "싱가포르": "1271",
    "singapore": "1271",
    "아랍에미리트": "1272",
    "uae": "1272",
    "u.a.e.": "1272",
    "아르메니아": "1273",
    "armenia": "1273",
    "아제르바이잔": "1274",
    "azerbaijan": "1274",
    "아프가니스탄": "1275",
    "afghanistan": "1275",
    "예멘": "1276",
    "yemen": "1276",
    "오만": "1277",
    "oman": "1277",
    "요르단": "1278",
    "jordan": "1278",
    "우즈베키스탄": "1279",
    "uzbekistan": "1279",
    "이라크": "1280",
    "iraq": "1280",
    "이란": "1281",
    "iran": "1281",
    "이스라엘": "1282",
    "israel": "1282",
    "인도": "1283",
    "india": "1283",
    "인도네시아": "1284",
    "indonesia": "1284",
    "일본": "1285",
    "japan": "1285",
    "중국": "1287",
    "china": "1287",
    "카자흐스탄": "1288",
    "kazakhstan": "1288",
    "카타르": "1289",
    "qatar": "1289",
    "캄보디아": "1290",
    "cambodia": "1290",
    "쿠웨이트": "1291",
    "kuwait": "1291",
    "키르기스스탄": "1292",
    "kyrgyzstan": "1292",
    "태국": "1293",
    "thailand": "1293",
    "타이완": "1294",
    "taiwan": "1294",
    "타지키스탄": "1295",
    "tajikistan": "1295",
    "투르크메니스탄": "1296",
    "turkmenistan": "1296",
    "파키스탄": "1297",
    "pakistan": "1297",
    "필리핀": "1298",
    "philippines": "1298",
    # 아프리카
    "가나": "1299",
    "ghana": "1299",
    "가봉": "1300",
    "gabon": "1300",
    "감비아": "1301",
    "gambia": "1301",
    "기니": "1302",
    "guinea": "1302",
    "나미비아": "1304",
    "namibia": "1304",
    "나이지리아": "1305",
    "nigeria": "1305",
    "남아프리카공화국": "1306",
    "남아공": "1306",
    "south africa": "1306",
    "르완다": "1310",
    "rwanda": "1310",
    "모로코": "1315",
    "morocco": "1315",
    "에티오피아": "1334",
    "ethiopia": "1334",
    "이집트": "1336",
    "egypt": "1336",
    "케냐": "1345",
    "kenya": "1345",
    "탄자니아": "1350",
    "tanzania": "1350",
    "튀니지": "1352",
    "tunisia": "1352",
    # 유럽
    "그리스": "1353",
    "greece": "1353",
    "네덜란드": "1354",
    "netherlands": "1354",
    "holland": "1354",
    "노르웨이": "1355",
    "norway": "1355",
    "덴마크": "1356",
    "denmark": "1356",
    "독일": "1357",
    "germany": "1357",
    "러시아": "1359",
    "russia": "1359",
    "루마니아": "1360",
    "romania": "1360",
    "룩셈부르크": "1361",
    "luxembourg": "1361",
    "벨기에": "1370",
    "belgium": "1370",
    "불가리아": "1373",
    "bulgaria": "1373",
    "스웨덴": "1376",
    "sweden": "1376",
    "스위스": "1377",
    "switzerland": "1377",
    "스페인": "1378",
    "spain": "1378",
    "슬로바키아": "1379",
    "slovakia": "1379",
    "아일랜드": "1382",
    "ireland": "1382",
    "알바니아": "1384",
    "albania": "1384",
    "에스토니아": "1385",
    "estonia": "1385",
    "영국": "1386",
    "uk": "1386",
    "united kingdom": "1386",
    "britain": "1386",
    "오스트리아": "1387",
    "austria": "1387",
    "우크라이나": "1388",
    "ukraine": "1388",
    "이탈리아": "1389",
    "italy": "1389",
    "체코": "1390",
    "czech": "1390",
    "czechia": "1390",
    "크로아티아": "1391",
    "croatia": "1391",
    "튀르키예": "1393",
    "turkey": "1393",
    "터키": "1393",
    "포르투갈": "1394",
    "portugal": "1394",
    "폴란드": "1395",
    "poland": "1395",
    "프랑스": "1396",
    "france": "1396",
    "핀란드": "1397",
    "finland": "1397",
    "헝가리": "1398",
    "hungary": "1398",
    # 북아메리카
    "과테말라": "1399",
    "guatemala": "1399",
    "멕시코": "1404",
    "mexico": "1404",
    "미국": "1405",
    "usa": "1405",
    "us": "1405",
    "united states": "1405",
    "캐나다": "1417",
    "canada": "1417",
    "코스타리카": "1418",
    "costa rica": "1418",
    "쿠바": "1419",
    "cuba": "1419",
    "파나마": "1421",
    "panama": "1421",
    # 남아메리카
    "브라질": "1425",
    "brazil": "1425",
    "아르헨티나": "1427",
    "argentina": "1427",
    "에콰도르": "1428",
    "ecuador": "1428",
    "우루과이": "1429",
    "uruguay": "1429",
    "칠레": "1430",
    "chile": "1430",
    "콜롬비아": "1431",
    "colombia": "1431",
    "페루": "1433",
    "peru": "1433",
    # 오세아니아
    "뉴질랜드": "1435",
    "new zealand": "1435",
    "오스트레일리아": "1441",
    "호주": "1441",
    "australia": "1441",
    "파푸아뉴기니": "1445",
    "papua new guinea": "1445",
    "피지": "1447",
    "fiji": "1447",
}

# 국내로 인식할 키워드
_ORIGIN_DOMESTIC_KEYWORDS = {"한국", "국내", "korea", "south korea", "대한민국"}


def _resolve_origin(origin: str) -> tuple[str, str, str]:
    """원산지 문자열 → (orgnTypCd, orgnTypDtlsCd, orgnNmVal).

    Returns:
        orgnTypCd: 01=국내, 02=해외, 03=기타
        orgnTypDtlsCd: 해외일 때 국가코드 (셀러오피스 실측값)
        orgnNmVal: 기타일 때 원산지명 텍스트
    """
    if not origin:
        return ("03", "", "기타")

    normalized = origin.strip().lower()

    if normalized in _ORIGIN_DOMESTIC_KEYWORDS:
        return ("01", "", "")

    country_code = _ORIGIN_COUNTRY_MAP.get(normalized)
    if country_code:
        return ("02", country_code, "")

    # 매핑 없는 국가명은 기타로 처리
    return ("03", "", origin.strip())


def _clean_alimi_content(raw: str) -> str:
    """긴급알리미 emerCtnt 필드 정제: 이중 HTML 엔티티 디코딩 → 줄바꿈 보존 → 나머지 태그 제거."""
    import html as _html
    import re as _re2

    # 이중 인코딩 디코딩 (&amp;lt; → &lt; → <)
    text = _html.unescape(_html.unescape(raw))
    # <br>, <BR>, <p>, </p> → 줄바꿈으로 변환 (가독성 보존)
    text = _re2.sub(r"<br\s*/?>|<BR\s*/?>", "\n", text, flags=_re2.IGNORECASE)
    text = _re2.sub(r"</?p\s*>", "\n", text, flags=_re2.IGNORECASE)
    # 나머지 HTML 태그 제거
    text = _re2.sub(r"<[^>]+>", "", text)
    # javascript: 링크 텍스트 제거
    text = _re2.sub(r"javascript:[^\s\"']+", "", text)
    # 3줄 이상 연속 빈줄 → 2줄로 정리
    text = _re2.sub(r"\n{3,}", "\n\n", text)
    # 각 줄 앞뒤 공백 제거
    text = "\n".join(line.strip() for line in text.splitlines())
    return text.strip()


def _escape_xml(text: str) -> str:
    """XML 특수문자 이스케이프."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


# 11번가 옵션값/제목에서 금지된 특수문자 제거 (공식 가이드 명시)
# 금지: [ ] & ; " % < > # † |  (단 &는 XML escape 후 허용되므로 strip 대상에서 제외)
_ELEVENST_FORBIDDEN_OPT_RE = re.compile(r'[\[\];"%<>#†|]')


def _sanitize_elevenst_option_text(s: str, max_len: int = 25) -> str:
    """11번가 옵션값/제목 정제: 금지문자 제거 + 중복 공백 압축 + 길이 컷."""
    s = _ELEVENST_FORBIDDEN_OPT_RE.sub("", s or "")
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s[:max_len]


def _build_elevenst_option_xml(
    options: list[dict],
    max_stock_cap: int,
    option_group_names: list | None,
) -> str:
    """11번가 옵션 XML 빌드.

    - 옵션명에 ' / ' 패턴 감지 시 2D 멀티옵션(optMixYn=N + ProductRootOption + ProductOptionExt)
    - 그 외엔 1D 싱글옵션
    - 1차 그룹명 폴백: "선택", 2차 폴백: "옵션"
    - colOptPrice = max(opt.price - 활성옵션최저가, 0) — 옵션별 추가요금 반영
    - 11번가 정책: 옵션가 0원 옵션 최소 1개 필수 (base 옵션이 자동 0원)
    """
    if not options:
        no_stock = max_stock_cap if max_stock_cap > 0 else 99
        return (
            "<optSelectYn>Y</optSelectYn>"
            "<txtColCnt>1</txtColCnt>"
            "<colTitle>옵션</colTitle>"
            "<prdExposeClfCd>00</prdExposeClfCd>"
            "<ProductOption>"
            "<useYn>Y</useYn>"
            "<colOptPrice>0</colOptPrice>"
            "<colValue0>기본</colValue0>"
            f"<colCount>{no_stock}</colCount>"
            "</ProductOption>"
        )

    has_slash = any(" / " in (o.get("name") or "") for o in options)

    # 활성 옵션 최저가를 옵션가 base 로 사용 (옵션 간 상대 차이만 추출)
    _active_prices = [
        int(o.get("price") or 0)
        for o in options
        if int(o.get("price") or 0) > 0
        and not o.get("isSoldOut", False)
        and (o.get("stock") or 0) > 0
    ]
    diff_base = min(_active_prices) if _active_prices else 0

    def _stock_of(opt: dict) -> tuple[int, str]:
        raw = opt.get("stock")
        sold_out = opt.get("isSoldOut", False)
        if raw is None:
            qty = max_stock_cap if max_stock_cap > 0 else 99
            use = "Y"
        elif int(raw) <= 0 or sold_out:
            qty = 0
            use = "N"
        else:
            real = int(raw)
            qty = min(real, max_stock_cap) if max_stock_cap > 0 else real
            use = "Y"
        return qty, use

    groups = [g for g in (option_group_names or []) if g]

    if has_slash:
        # 2D 멀티옵션 (선택형: optMixYn=N + ProductOptionExt)
        title1 = "옵션1"
        title2 = "옵션2"

        axis1_values: list[str] = []
        axis2_values: list[str] = []
        parsed: list[tuple[str, str, dict]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for opt in options:
            name = (opt.get("name") or "").strip()
            if " / " in name:
                a_raw, b_raw = (p.strip() for p in name.split(" / ", 1))
            else:
                a_raw, b_raw = name, ""
            a = _sanitize_elevenst_option_text(a_raw) or "기본"
            b = _sanitize_elevenst_option_text(b_raw) or "기본"
            if (a, b) in seen_pairs:
                continue
            seen_pairs.add((a, b))
            if a not in axis1_values:
                axis1_values.append(a)
            if b not in axis2_values:
                axis2_values.append(b)
            parsed.append((a, b, opt))

        root1_items = "".join(
            f"<ProductOption><colOptPrice>0</colOptPrice><colValue0>{_escape_xml(v)}</colValue0></ProductOption>"
            for v in axis1_values
        )
        root2_items = "".join(
            f"<ProductOption><colOptPrice>0</colOptPrice><colValue0>{_escape_xml(v)}</colValue0></ProductOption>"
            for v in axis2_values
        )
        root_xml = (
            f"<ProductRootOption><colTitle>{_escape_xml(title1)}</colTitle>{root1_items}</ProductRootOption>"
            f"<ProductRootOption><colTitle>{_escape_xml(title2)}</colTitle>{root2_items}</ProductRootOption>"
        )

        ext_items = ""
        for a, b, opt in parsed:
            qty, use = _stock_of(opt)
            opt_price = int(opt.get("price", 0) or 0)
            diff = max(opt_price - diff_base, 0) if opt_price > 0 else 0
            stock_code = _escape_xml(opt.get("managedCode", "") or "")
            mapping_key = f"{title1}:{a}†{title2}:{b}"
            ext_items += (
                "<ProductOption>"
                f"<useYn>{use}</useYn>"
                f"<colOptPrice>{diff}</colOptPrice>"
                f"<colOptCount>{qty}</colOptCount>"
                f"<colCount>{qty}</colCount>"
                f"<colSellerStockCd>{stock_code}</colSellerStockCd>"
                f"<optionMappingKey>{_escape_xml(mapping_key)}</optionMappingKey>"
                "</ProductOption>"
            )

        return (
            "<optUpdateYn>Y</optUpdateYn>"
            "<optSelectYn>Y</optSelectYn>"
            "<txtColCnt>1</txtColCnt>"
            "<prdExposeClfCd>00</prdExposeClfCd>"
            "<optMixYn>N</optMixYn>"
            f"{root_xml}"
            f"<ProductOptionExt>{ext_items}</ProductOptionExt>"
        )

    # 1D 싱글옵션
    title = "옵션1"
    xml = (
        "<optUpdateYn>Y</optUpdateYn>"
        "<optSelectYn>Y</optSelectYn>"
        "<txtColCnt>1</txtColCnt>"
        f"<colTitle>{_escape_xml(title)}</colTitle>"
        "<prdExposeClfCd>00</prdExposeClfCd>"
    )
    seen_names: set[str] = set()
    for opt in options:
        raw_name = opt.get("name", "") or opt.get("size", "") or "기본"
        opt_name = _sanitize_elevenst_option_text(raw_name) or "기본"
        if opt_name in seen_names:
            continue
        seen_names.add(opt_name)
        qty, use = _stock_of(opt)
        opt_price = int(opt.get("price", 0) or 0)
        diff = max(opt_price - diff_base, 0) if opt_price > 0 else 0
        stock_code = _escape_xml(opt.get("managedCode", "") or "")
        xml += (
            "<ProductOption>"
            f"<useYn>{use}</useYn>"
            f"<colOptPrice>{diff}</colOptPrice>"
            f"<colValue0>{_escape_xml(opt_name)}</colValue0>"
            f"<colCount>{qty}</colCount>"
            f"<colSellerStockCd>{stock_code}</colSellerStockCd>"
            "</ProductOption>"
        )
    return xml


# ──────────────────────────────────────────────────────────────
# 상품명 정제 + 홍보문구 자동 생성
# 공식 필드: prdNm(99바이트), advrtStmt(한글 14자/영문 28자)
# ──────────────────────────────────────────────────────────────

# 세부 카테고리 키워드 → 홍보문구 템플릿 (한글 14자 이내)
_PROMO_CATEGORY_TEMPLATES: list[tuple[str, str]] = [
    # 아우터
    ("패딩", "시즌 필수 패딩 아이템"),
    ("코트", "프리미엄 클래식 코트"),
    ("야상", "스트릿 감성 아우터"),
    ("점퍼", "스타일 업 아우터"),
    ("바람막이", "경량 스포티 아우터"),
    ("아우터", "시즌 트렌드 아우터"),
    # 상의
    ("후드티", "캐주얼 스트릿 무드"),
    ("후드", "캐주얼 스트릿 무드"),
    ("맨투맨", "베이직 캐주얼 완성"),
    ("스웨트", "베이직 캐주얼 완성"),
    ("니트", "시즌 감성 니트"),
    ("블라우스", "페미닌 감성 스타일"),
    ("셔츠", "클래식 셔츠 스타일"),
    ("티셔츠", "베이직 데일리 티"),
    ("상의", "데일리 무드 완성"),
    # 하의
    ("청바지", "데님 데일리 스타일"),
    ("데님", "데님 데일리 스타일"),
    ("슬랙스", "세련된 보텀 스타일"),
    ("레깅스", "액티브 데일리 룩"),
    ("스커트", "페미닌 스타일 완성"),
    ("치마", "페미닌 스타일 완성"),
    ("반바지", "시즌 데일리 쇼츠"),
    ("바지", "트렌디 보텀 스타일"),
    ("하의", "트렌디 보텀 스타일"),
    # 원피스/세트
    ("원피스", "페미닌 감성 드레스"),
    ("드레스", "페미닌 감성 드레스"),
    ("세트", "코디 완성 세트"),
    # 신발
    ("스니커즈", "데일리 스타일 완성"),
    ("운동화", "데일리 스타일 완성"),
    ("구두", "클래식 포멀 슈즈"),
    ("부츠", "시즌 트렌드 부츠"),
    ("샌들", "시즌 감성 샌들"),
    ("슬리퍼", "편안한 데일리 슈즈"),
    ("신발", "데일리 스타일 완성"),
    # 가방
    ("백팩", "실용 데일리 백팩"),
    ("크로스백", "데일리 크로스 스타일"),
    ("숄더백", "시즌 트렌드 숄더백"),
    ("토트백", "실용 데일리 토트백"),
    ("가방", "프리미엄 패션 백"),
    # 잡화/액세서리
    ("모자", "데일리 포인트 모자"),
    ("벨트", "스타일 포인트 벨트"),
    ("지갑", "슬림 패션 지갑"),
    ("시계", "세련된 패션 시계"),
    ("주얼리", "감성 패션 주얼리"),
    ("액세서리", "패션 감성 완성"),
]

# 그룹별 기본 템플릿 (세부 키워드 미매칭 시)
_PROMO_GROUP_TEMPLATES: dict[str, list[str]] = {
    "wear": ["데일리 무드 컬렉션", "시즌 트렌드 패션", "프리미엄 패션 아이템"],
    "shoes": ["데일리 스타일 완성", "트렌디 슈즈 컬렉션", "시즌 인기 슈즈"],
    "bag": ["프리미엄 패션 백", "시즌 트렌드 가방", "데일리 스타일 백"],
    "accessories": ["패션 감성 완성", "시즌 트렌드 잡화", "데일리 패션 잡화"],
    "etc": ["프리미엄 품질 보장", "시즌 트렌드 아이템", "데일리 추천 상품"],
}


def _resolve_model_nm(product: dict) -> str:
    """11번가 modelCd/modelNm용 품번 반환.

    11번가 카탈로그 자동 매칭 키이므로 정확한 품번 추출이 중요.
    우선순위:
      1) style_code (수집기가 채운 품번)
      2) 상품명 끝 토큰 — 하이픈 포함 품번 (예: IF0217-010, 623869-01)
      3) 상품명 끝 토큰 — 6자 이상 영숫자 코드 (예: SQBAB9401, GZ8950)
    매칭 실패 시 빈 문자열 반환 → 호출부에서 modelCd 엘리먼트 자체 생략.
    ('없음' 같은 더미값 전송 시 11번가 카탈로그 매칭이 차단됨)
    """
    style_code = (product.get("style_code", "") or "").strip()
    if style_code:
        return style_code
    raw_name = product.get("name", "") or ""
    # 하이픈 포함 품번 (예: "/ IF0217-010", " 623869-01")
    m = re.search(r"(?:^|[\s/(\[])([A-Za-z0-9]+-[A-Za-z0-9]+)\b", raw_name)
    if m:
        return m.group(1)
    # 하이픈 없는 6자 이상 영숫자 코드 (예: "GZ8950", "AT5301")
    m2 = re.search(r"(?:^|[\s/(\[])([A-Z][A-Z0-9]{5,})\b", raw_name)
    if m2:
        return m2.group(1)
    return ""


# 상품명 금지 패턴 (배송/이벤트/할인 관련)
_NAME_REMOVE_PATTERNS = [
    r"무료\s*배송",
    r"배송\s*무료",
    r"당일\s*발송",
    r"오늘\s*발송",
    r"\d+\s*%\s*할인",
    r"할인\s*\d+\s*%",
    r"할인가",
    r"특가",
    r"이벤트",
    r"사은품",
    r"증정품",
    r"기간\s*한정",
    r"한정\s*수량",
    r"\[세일\]",
    r"\[특가\]",
    r"\[할인\]",
    r"\[행사\]",
    r":?\s*세일",
    r"세일\s*:?",
]


def _truncate_to_bytes(text: str, max_bytes: int) -> str:
    """UTF-8 바이트 기준 안전 절단 (멀티바이트 경계 보존)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _limit_repeated_name_words(name: str) -> str:
    """동일 단어가 3번 이상 등장하면 3번째부터 자르기.

    복합어("데님팬츠")에 포함된 부분어("팬츠")도 출현 횟수에 포함.
    """
    words = name.split()
    result: list[str] = []
    for word in words:
        count = sum(1 for w in result if word in w)
        if count >= 2:
            break
        result.append(word)
    return " ".join(result)


def _clean_product_name(name: str) -> str:
    """11번가 등록용 상품명 정제 (금지어 제거 + 반복단어 제한 + 99바이트 제한)."""
    for pattern in _NAME_REMOVE_PATTERNS:
        name = re.sub(pattern, "", name, flags=re.IGNORECASE)
    # 연속 공백 정리
    name = re.sub(r"\s+", " ", name).strip()
    # 동일 단어 3회 이상 반복 차단
    name = _limit_repeated_name_words(name)
    # 99바이트 제한 (UTF-8)
    return _truncate_to_bytes(name, 99).strip()


def _validate_promo_clean(promo: str, name: str) -> bool:
    """홍보문구 클린체크 6가지 기준 검증.

    1) 상품명 단어 2개 이상 중복 → 부적합
    2) 숫자/특수문자만 → 부적합
    3) 3글자 이하 → 부적합
    4) 배송 정보 포함 → 부적합
    5) 인사말/부적합 단어 → 부적합
    6) 전화번호 포함 → 부적합
    + 한글 14자 초과 → 부적합
    """
    promo = promo.strip()
    if not promo:
        return False
    # 길이 체크 (한글 기준 14자)
    if len(promo) > 14:
        return False
    # 3글자 이하
    if len(promo) <= 3:
        return False
    # 숫자/특수문자만
    if re.match(r"^[0-9\W]+$", promo):
        return False
    # 배송 정보
    if re.search(r"무료배송|오늘발송|당일배송|빠른배송|익일배송|오늘출발", promo):
        return False
    # 인사말/부적합 단어
    if re.search(r"상세설명참조|감사합니다|안녕하세요|문의주세요", promo):
        return False
    # 전화번호
    if re.search(r"\d{2,4}[-\s]?\d{3,4}[-\s]?\d{4}", promo):
        return False
    # 상품명 단어 2개 이상 겹치면 부적합 (2자 이상 한글/영문)
    name_words = set(re.findall(r"[가-힣a-zA-Z]{2,}", name))
    promo_words = set(re.findall(r"[가-힣a-zA-Z]{2,}", promo))
    if len(name_words & promo_words) >= 2:
        return False
    return True


def _generate_promo_text(product: dict, name: str, custom: str = "") -> str:
    """홍보문구 결정 (한글 14자 이내).

    우선순위: 사용자 지정(custom) → 세부 카테고리 키워드 → 그룹 기본 템플릿 → fallback
    """
    # 사용자가 스토어설정에서 지정한 값이 있으면 14자 절단 후 그대로 사용
    # (clean 검증은 사용자 책임 — 자동 매칭과 달리 의도된 문구이므로 강제 검증 X)
    if custom:
        trimmed = custom.strip()[:14]
        if trimmed:
            return trimmed

    from backend.domain.samba.proxy.notice_utils import detect_notice_group

    group = detect_notice_group(product)
    search_text = " ".join(
        filter(
            None,
            [
                product.get("category") or "",
                product.get("category1") or "",
                product.get("name") or "",
            ],
        )
    ).lower()

    # 세부 키워드 매핑 시도
    for keyword, template in _PROMO_CATEGORY_TEMPLATES:
        if keyword in search_text:
            if _validate_promo_clean(template, name):
                return template

    # 그룹 기본 템플릿
    for template in _PROMO_GROUP_TEMPLATES.get(group, _PROMO_GROUP_TEMPLATES["etc"]):
        if _validate_promo_clean(template, name):
            return template

    return "프리미엄 브랜드 상품"


# ──────────────────────────────────────────────────────────────
# 상품정보 제공고시 (11번가 공식 API 문서 + 셀러오피스 UI 실측값 기준)
# 출처: openapi.11st.co.kr 상품관리 > 상품등록 파라미터 및
#       soffice.11st.co.kr/product/BulkProductReg.tmall?method=goProductNotiPop
# ──────────────────────────────────────────────────────────────

# detect_notice_group() 반환값 → 11번가 유형 코드
_ELEVENST_NOTICE_TYPE_CODE: dict[str, str] = {
    "wear": "891011",  # 의류
    "shoes": "891012",  # 구두/신발
    "bag": "891013",  # 가방
    "accessories": "891014",  # 패션잡화 (모자/벨트/액세서리 등)
}

# 유형별 항목 코드 목록 (code, 항목명) — 공식 문서 실측값
# API XML: <item><code>코드</code><name>값</name></item>
_ELEVENST_NOTICE_ITEMS: dict[str, list[tuple[str, str]]] = {
    "wear": [
        ("11835", "색상"),
        ("23756520", "세탁방법 및 취급시 주의사항"),
        ("23759095", "제조국"),
        ("23760437", "A/S 책임자와 전화번호"),
        ("23759468", "제품 소재"),
        ("23760034", "치수"),
        ("23760386", "품질보증기준"),
        ("11905", "제조자/수입자"),
        ("23759308", "제조연월"),
    ],
    "shoes": [
        ("11835", "색상"),
        ("11905", "제조자/수입자"),
        ("23759095", "제조국"),
        ("40748371", "제품 주소재"),
        ("23760034", "치수"),
        ("23760386", "품질보증기준"),
        ("23760437", "A/S 책임자와 전화번호"),
        ("23759972", "취급시 주의사항"),
    ],
    "bag": [
        ("11835", "색상"),
        ("11848", "소재"),
        ("11905", "제조자/수입자"),
        ("11908", "종류"),
        ("23760437", "A/S 책임자와 전화번호"),
        ("23759095", "제조국"),
        ("23759972", "취급시 주의사항"),
        ("23760386", "품질보증기준"),
        ("11932", "크기,용량,형태"),
    ],
    "accessories": [
        ("11848", "소재"),
        ("11905", "제조자/수입자"),
        ("11908", "종류"),
        ("23760437", "A/S 책임자와 전화번호"),
        ("23759972", "취급시 주의사항"),
        ("23760034", "치수"),
        ("23760386", "품질보증기준"),
        ("23759095", "제조국"),
    ],
}

# 카테고리별 취급주의사항 기본 문구
_ELEVENST_CAUTION_DEFAULTS: dict[str, str] = {
    "wear": "세탁 시 뒤집어서 단독 손세탁, 표백제 사용 금지, 직사광선을 피해 그늘에서 건조",
    "shoes": "물세탁 불가, 직사광선 및 고온 다습한 곳 보관 금지, 벤젠/신나 등 화학제품 사용 금지",
    "bag": "직사광선 및 고온 다습한 환경을 피해 보관, 마찰에 의한 색 이염 주의",
    "accessories": "직사광선 및 습기를 피해 보관, 화학제품 접촉 주의",
}


def _build_elevenst_notice_xml(product: dict[str, Any]) -> str:
    """상품 카테고리에 맞는 11번가 상품정보 제공고시 XML 블록 생성.

    - XML 태그: <ProductNotification> (공식 API 문서 기준)
    - 항목 구조: <item><code>항목코드</code><name>값</name></item>
    - 항목 코드: 11번가 공식 문서(goProductNotiPop) 실측값
    - 무신사 수집 데이터(material/color/manufacturer 등) 동적 매핑
    """
    from backend.domain.samba.proxy.notice_utils import detect_notice_group

    group = detect_notice_group(product)
    type_code = _ELEVENST_NOTICE_TYPE_CODE.get(group, "891011")
    items = _ELEVENST_NOTICE_ITEMS.get(group, _ELEVENST_NOTICE_ITEMS["wear"])

    fallback = "상세페이지 참조"

    # 취급 주의사항: 수집값 우선, 없으면 카테고리별 기본문구
    # HTML 태그 및 이스케이프된 태그 제거
    def _strip_html(text: str) -> str:
        import re as _re

        text = _re.sub(
            r"&lt;[^&]*&gt;", " ", text
        )  # &lt;br&gt; 등 이스케이프된 태그 제거
        text = _re.sub(r"<[^>]+>", " ", text)  # <br> 등 일반 HTML 태그 제거
        text = _re.sub(r"\s+", " ", text).strip()
        return text

    raw_caution = (
        product.get("care_instructions", "")
        or product.get("careInstructions", "")
        or ""
    )
    caution = (
        _strip_html(raw_caution)
        if raw_caution
        else _ELEVENST_CAUTION_DEFAULTS.get(group, fallback)
    )

    # 옵션에서 사이즈 텍스트 생성
    options = product.get("options") or []
    sizes = [opt.get("name") or opt.get("size", "") for opt in options]
    size_text = ", ".join(filter(None, sizes)) if sizes else fallback

    # 항목 코드 → 값 매핑
    code_value_map: dict[str, str] = {
        "11835": product.get("color", "") or fallback,  # 색상
        "23756520": caution,  # 세탁방법 및 취급시 주의사항
        "23759972": caution,  # 취급시 주의사항
        "23759095": product.get("origin", "") or fallback,  # 제조국
        "23760437": product.get("_as_phone") or fallback,  # A/S 책임자와 전화번호
        "23759468": product.get("material", "") or fallback,  # 제품 소재
        "40748371": product.get("material", "") or fallback,  # 제품 주소재
        "11848": product.get("material", "") or fallback,  # 소재
        "23760034": size_text,  # 치수
        "23760386": "제품 이상 시 공정거래위원회 고시 소비자분쟁해결기준에 의거 보상합니다.",  # 품질보증기준
        "11905": product.get("manufacturer", "")
        or product.get("brand", "")
        or fallback,  # 제조자/수입자
        "23759308": fallback,  # 제조연월
        "11932": fallback,  # 크기,용량,형태
        "11908": fallback,  # 종류
    }

    items_xml = ""
    for code, _ in items:
        value = code_value_map.get(code, fallback)
        items_xml += f"""
  <item>
    <code>{code}</code>
    <name>{_escape_xml(value)}</name>
  </item>"""

    # company: 제조사/수입사 (API 문서 별도 필드)
    company = product.get("manufacturer", "") or product.get("brand", "") or "없음"

    return f"""<ProductNotification>
  <type>{type_code}</type>{items_xml}
  <company>{_escape_xml(company)}</company>
  <modelNm>{_escape_xml(_resolve_model_nm(product) or "없음")}</modelNm>
</ProductNotification>"""


class ElevenstApiError(Exception):
    """11번가 API 에러."""

    pass

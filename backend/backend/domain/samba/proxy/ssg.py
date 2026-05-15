"""SSG(신세계몰) Open API 클라이언트 - 상품 등록/수정.

인증 방식: Authorization 헤더에 업체 인증키
기본 URL: https://eapi.ssgadm.com

API 버전:
- 조회(GET): 0.1 (브랜드, 카테고리 등)
- 등록/수정(POST): 0.5 (insertItem, updateItem)
- 주소: 0.3
- 상품관리속성: 0.1~0.2

사이트번호:
- 6001: 이마트몰
- 6004: 신세계몰
- 6009: 신세계백화점몰

API 경로 패턴:
- 업체정보: /venInfo/{version}/xxx.ssg
- 상품관리: /item/{version}/xxx.ssg
- 공통정보: /common/{version}/xxx.ssg

JSON 구조 주의: SSG는 XStream 기반이므로 배열을
요소명 래퍼로 감싸야 함.
  예) "sites": [{"siteNo":"6004"}]  (X)
      "sites": {"site": {"siteNo":"6004"}}  (O)
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from backend.core.config import settings
from backend.utils.logger import logger

# fetch_infra() 결과 캐시 — api_key 기준, TTL 15분
_infra_cache: dict[str, tuple[dict[str, Any], float]] = {}
_INFRA_CACHE_TTL = 900  # 15분

# httpx 클라이언트 풀 — api_key별 재사용 (TCP 커넥션 재활용으로 SSL handshake 제거)
# 값: (클라이언트, 마지막 사용 시간)
_client_pool: dict[str, tuple[httpx.AsyncClient, float]] = {}
_CLIENT_STALE_TTL = 1800  # 30분 미사용 시 정리


async def _cleanup_stale_clients() -> None:
    """30분 이상 미사용 클라이언트를 닫고 풀에서 제거."""
    now = time.time()
    stale_keys = [
        k
        for k, (_, last_used) in _client_pool.items()
        if now - last_used > _CLIENT_STALE_TTL
    ]
    for k in stale_keys:
        client, _ = _client_pool.pop(k)
        try:
            await client.aclose()
        except Exception:
            pass
    if stale_keys:
        logger.debug(f"[SSG] stale 클라이언트 {len(stale_keys)}개 정리 완료")


def _get_ssg_client(api_key: str) -> httpx.AsyncClient:
    """api_key별 재사용 클라이언트 반환. 없으면 생성, 사용 시간 갱신."""
    now = time.time()
    if api_key in _client_pool:
        client, _ = _client_pool[api_key]
        _client_pool[api_key] = (client, now)
        return client
    client = httpx.AsyncClient(
        timeout=settings.http_timeout_default,
        limits=httpx.Limits(
            max_keepalive_connections=10,
            max_connections=5,
        ),
    )
    _client_pool[api_key] = (client, now)
    return client


async def invalidate_infra_cache(api_key: str) -> None:
    """SSG 설정 변경 시 인프라 캐시 + stale 클라이언트 정리.

    설정 저장 엔드포인트에서 호출하여
    변경된 자격증명이 즉시 반영되도록 한다.
    """
    _infra_cache.pop(api_key, None)
    # 해당 api_key 클라이언트도 닫고 제거 (자격증명 변경 대응)
    entry = _client_pool.pop(api_key, None)
    if entry is not None:
        try:
            await entry[0].aclose()
        except Exception:
            pass
    # stale 클라이언트 일괄 정리
    await _cleanup_stale_clients()
    logger.info(f"[SSG] 인프라 캐시 무효화 완료 (api_key=...{api_key[-6:]})")


class SSGClient:
    """SSG Open API 클라이언트."""

    BASE_URL = "https://eapi.ssgadm.com"

    def __init__(self, api_key: str, site_no: str = "6004") -> None:
        """api_key: 업체 인증키, site_no: 사이트번호 (기본 신세계몰)."""
        self.api_key = api_key
        self.site_no = site_no

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        return {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
            "Accept": accept,
        }

    async def _call_api(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """공통 API 호출.

        SSG는 에러 시 HTTP 500을 반환하면서도 JSON body에 에러 내용을 담으므로
        500 응답도 JSON 파싱 후 반환한다.
        """
        url = f"{self.BASE_URL}{path}"
        headers = self._headers()

        # stale 클라이언트 정리 후 커넥션 풀 재사용
        await _cleanup_stale_clients()
        client = _get_ssg_client(self.api_key)
        if method == "GET":
            resp = await client.get(url, headers=headers, params=params)
        elif method == "POST":
            resp = await client.post(
                url, headers=headers, json=body or {}, params=params
            )
        elif method == "PUT":
            resp = await client.put(url, headers=headers, json=body or {})
        elif method == "DELETE":
            resp = await client.delete(url, headers=headers, params=params)
        else:
            raise ValueError(f"지원하지 않는 HTTP 메서드: {method}")

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        logger.info(f"[SSG] {method} {path} → {resp.status_code}")

        # SSG는 500 응답에도 JSON 에러를 담아 보냄 — 에러 내용 추출
        if not resp.is_success:
            # 에러 응답 전체 로깅 (디버깅용)
            logger.error(f"[SSG ERROR] {method} {path} 응답 전체: {resp.text[:2000]}")
            # SSG JSON 에러 응답에서 상세 메시지 추출
            result_obj = data.get("result", {}) if isinstance(data, dict) else {}
            desc = ""
            if isinstance(result_obj, dict):
                desc = result_obj.get("resultDesc", "") or result_obj.get(
                    "resultMessage", ""
                )
            msg = (
                desc
                or data.get("message", "")
                or data.get("msg", "")
                or resp.text[:300]
            )
            raise SSGApiError(f"HTTP {resp.status_code}: {msg}")

        return data

    async def _call_api_xml(
        self,
        method: str,
        path: str,
        xml_body: str,
    ) -> dict[str, Any]:
        """/api/postng/ 등 XStream 기반 XML POST 엔드포인트 전용 호출.

        SSG의 일부 API(/api/postng/)는 JSON body가 아닌 XML body를 요구한다.
        응답은 Accept: application/json으로 JSON을 받는다.
        """
        url = f"{self.BASE_URL}{path}"
        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/xml; charset=UTF-8",
            "Accept": "application/json",
        }

        # XML 전송은 Content-Type이 달라 별도 클라이언트 생성
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_default
        ) as xml_client:
            resp = await xml_client.post(
                url, headers=headers, content=xml_body.encode("utf-8")
            )

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        logger.info(f"[SSG] XML POST {path} → {resp.status_code}")

        if not resp.is_success:
            raw_bytes = resp.content
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            logger.error(
                f"[SSG ERROR XML] {path} status={resp.status_code} encoding={resp.encoding} body={raw_text[:2000]}"
            )
            result_obj = data.get("result", {}) if isinstance(data, dict) else {}
            desc = ""
            if isinstance(result_obj, dict):
                desc = result_obj.get("resultDesc", "") or result_obj.get(
                    "resultMessage", ""
                )
            msg = desc or raw_text[:300]
            raise SSGApiError(f"HTTP {resp.status_code}: {msg}")

        return data

    # ------------------------------------------------------------------
    # 인증 테스트
    # ------------------------------------------------------------------

    async def test_auth(self) -> dict[str, Any]:
        """인증 테스트 — 브랜드 목록 조회로 키 유효성 확인."""
        result = await self._call_api("GET", "/venInfo/0.1/listBrand.ssg")
        return {"success": True, "message": "인증 성공", "data": result}

    # ------------------------------------------------------------------
    # 상품 등록/수정/조회
    # ------------------------------------------------------------------

    async def register_product(self, product_data: dict[str, Any]) -> dict[str, Any]:
        """상품 전체 등록.

        SSG Open API v0.1: POST /item/0.1/online
        SSG POST API는 JSON이 아닌 XML body를 요구함 (XStream 기반).
        """
        xml_body = '<?xml version="1.0" encoding="UTF-8"?>' + self._to_xml(
            product_data, "insertItem"
        )
        logger.info(f"[SSG DEBUG] insertItem XML (총 {len(xml_body.encode())}bytes):\n{xml_body[:2000]}")
        result = await self._call_api_xml("POST", "/item/0.5/insertItem.ssg", xml_body)
        return {"success": True, "data": result}

    async def update_product(self, product_data: dict[str, Any]) -> dict[str, Any]:
        """상품 전체 수정.

        SSG Open API v0.1: POST /item/0.1/online/{itemId}
        SSG POST API는 JSON이 아닌 XML body를 요구함 (XStream 기반).
        """
        item_id = product_data.pop("itemId", "") or ""
        product_data["itemId"] = item_id  # XStream updateItem.ssg는 itemId를 XML 본문에 포함
        xml_body = '<?xml version="1.0" encoding="UTF-8"?>' + self._to_xml(
            product_data, "updateItem"
        )
        logger.info(f"[SSG DEBUG] updateItem XML (itemId={item_id}):\n{xml_body[:2000]}")
        result = await self._call_api_xml(
            "POST", "/item/0.5/updateItem.ssg", xml_body
        )
        return {"success": True, "data": result}

    async def delete_product(self, item_id: str) -> dict[str, Any]:
        """상품 삭제 — 영구판매중지(sellStatCd=90)로 처리.

        SSG는 실제 삭제 API(deleteItem.ssg)를 지원하지 않으므로
        판매상태를 영구판매중지(90)로 변경하여 사실상 삭제 처리.
        sellFrmCd 등 필수 필드를 유지하기 위해 현재 상태를 먼저 조회한 후 수정.
        """
        logger.info(f"[SSG] 영구판매중지 처리: itemId={item_id}")

        # 현재 판매상태 조회 — sellFrmCd 등 필수 필드 유지용
        current_status: dict[str, Any] = {}
        try:
            status_resp = await self.get_item_sales_status(item_id)
            logger.info(f"[SSG] 판매상태 조회 응답 전문: {status_resp}")
            res_obj = status_resp.get("result", {})
            sales_status = (
                (res_obj.get("salesStatus") if isinstance(res_obj, dict) else None)
                or status_resp.get("salesStatus")
                or {}
            )
            if isinstance(sales_status, dict):
                current_status = sales_status
                logger.info(f"[SSG] 현재 판매상태: {current_status}")
        except Exception as e:
            logger.warning(f"[SSG] 판매상태 조회 실패 (계속 진행): {e}")

        # 기존 필드를 유지하면서 sellStatCd만 영구판매중지(90)로 변경
        # optionInventories 유무로 옵션 여부를 판단하면 API가 빈 배열/null 반환 시 오판할 수 있음.
        # SSG 서버는 itemSellTypeCd="20"(옵션상품)으로 등록된 상품에 usablInvQty 전송을 거부하므로
        # 판매상태 변경에 불필요한 usablInvQty는 항상 제외한다.
        option_inventories = current_status.get("optionInventories")
        has_option_inventories = (
            isinstance(option_inventories, list) and len(option_inventories) > 0
        )

        payload: dict[str, Any] = {}
        for field in ("invMngYn", "invQtyMarkgYn", "dispStrtDt", "dispEndDt"):
            if field in current_status and current_status[field] is not None:
                payload[field] = current_status[field]
        # usablInvQty는 전송하지 않음 — 옵션상품(itemSellTypeCd=20)에서 에러 발생하며
        # 영구판매중지 처리에 재고수량은 필수값이 아님
        if "sellFrmCd" in current_status and current_status["sellFrmCd"] is not None:
            payload["sellFrmCd"] = str(current_status["sellFrmCd"])
        payload["sellStatCd"] = "90"
        if has_option_inventories:
            payload["optionInventories"] = [
                {
                    **opt,
                    "sellStatCd": str(opt.get("sellStatCd", 20)),
                    # sellFrmCd가 없거나 None이면 기본값 "10"(일반판매) 적용 — 빈값 전송 시 API 오류
                    "sellFrmCd": str(opt["sellFrmCd"])
                    if opt.get("sellFrmCd") is not None
                    else "10",
                }
                for opt in option_inventories
                if isinstance(opt, dict)
            ]

        result = await self.update_item_sales_status(item_id, payload)
        logger.info(f"[SSG] 영구판매중지 응답: {result}")

        res = result.get("result", {})
        if isinstance(res, dict):
            code = res.get("resultCode")
            if code is not None and str(code) != "00" and str(code) != "0":
                msg = (
                    res.get("resultDesc", "")
                    or res.get("resultMessage", "")
                    or f"resultCode={code}"
                )
                # IV2-OP: 이미 영구판매중지된 상품 → 삭제 완료 상태이므로 성공으로 처리
                if "IV2-OP" in msg or "영구판매중지(90)" in msg:
                    logger.info(
                        f"[SSG] 이미 영구판매중지된 상품 — 성공으로 처리: itemId={item_id}"
                    )
                    return {"success": True, "data": result}
                raise SSGApiError(f"SSG 영구판매중지 실패: {msg}")

        return {"success": True, "data": result}

    async def get_settlement_items(self, days: int = 7) -> list[dict[str, Any]]:
        """위수탁 마감리스트 조회 (정산 API).

        SSG Open API: GET /api/settle/v1/ven/sales/list.ssg
        critnDt(정산일자) 기준으로 days만큼 반복 조회하여 합산 반환.
        settIAmt(정산금액), sellFeeRt(판매수수료율), ordNo+ordItemSeq 로 주문 매칭.
        """
        KST = timezone(timedelta(hours=9))
        now = datetime.now(KST)
        result_items: list[dict[str, Any]] = []
        actual_days = min(days, 30)

        for i in range(actual_days):
            critn_dt = (now - timedelta(days=i)).strftime("%Y%m%d")
            page = 1
            while True:
                try:
                    resp = await self._call_api(
                        "GET",
                        "/api/settle/v1/ven/sales/list.ssg",
                        params={
                            "critnDt": critn_dt,
                            "page": str(page),
                            "pageSize": "1000",
                        },
                    )
                    res = resp.get("result", {})
                    if not isinstance(res, dict):
                        break
                    data = res.get("resultData") or []
                    if not isinstance(data, list):
                        data = [data] if isinstance(data, dict) else []
                    result_items.extend(data)
                    # 다음 페이지 없으면 종료
                    total_cnt = int(res.get("totalCnt", 0) or 0)
                    if page * 1000 >= total_cnt:
                        break
                    page += 1
                except Exception as e:
                    logger.warning(f"[SSG][정산] {critn_dt} page={page} 조회 실패: {e}")
                    break

        logger.info(f"[SSG][정산] {actual_days}일 총 {len(result_items)}건")
        return result_items

    async def get_item_sales_status(self, item_id: str) -> dict[str, Any]:
        """상품 판매상태 조회.

        SSG Open API: GET /item/0.1/online/{itemId}/sales-status
        """
        return await self._call_api("GET", f"/item/0.1/online/{item_id}/sales-status")

    async def update_item_sales_status(
        self,
        item_id: str,
        sales_status: dict[str, Any],
    ) -> dict[str, Any]:
        """상품 판매상태 수정.

        SSG Open API: POST /item/0.1/online/{itemId}/sales-status
        sellStatCd: 20=판매중, 80=일시판매중지, 90=영구판매중지
        """
        body = {"online_updateSalesStatus": {"salesStatus": sales_status}}
        return await self._call_api(
            "POST", f"/item/0.1/online/{item_id}/sales-status", body=body
        )

    async def get_product(self, item_id: str) -> dict[str, Any]:
        """상품 조회."""
        return await self._call_api(
            "GET",
            "/item/0.1/getItemList.ssg",
            params={"itemId": item_id},
        )

    async def get_product_list(
        self, keyword: str = "", page_size: int = 10
    ) -> dict[str, Any]:
        """상품 목록 조회 (상품명 키워드 검색).

        SSG Open API: GET /item/0.1/getItemList.ssg
        """
        params: dict[str, Any] = {"page": "1", "pageSize": str(page_size)}
        if keyword:
            params["itemNm"] = keyword
        return await self._call_api("GET", "/item/0.1/getItemList.ssg", params=params)

    async def get_product_count(self, site_no: str = "6004") -> int:
        """전체 등록 상품 수 조회 (페이지네이션으로 전체 카운트).

        SSG API는 totalCnt를 제공하지 않으므로 items 배열 길이를 집계.
        XStream 특성상 상품 1개 → dict, 여러 개 → list로 반환되므로 양쪽 처리.
        """
        page = 1
        page_size = 100
        total = 0
        while True:
            params: dict[str, Any] = {
                "page": str(page),
                "pageSize": str(page_size),
                "siteNo": site_no,
            }
            resp = await self._call_api(
                "GET", "/item/0.1/getItemList.ssg", params=params
            )
            result_obj = resp.get("result", {})
            items_raw = result_obj.get("items", {})
            # XStream 응답: items는 dict 래퍼, item은 1개면 dict, 여러 개면 list
            if isinstance(items_raw, dict):
                item_val = items_raw.get("item", [])
                if isinstance(item_val, dict):
                    items_list: list[Any] = [item_val]
                elif isinstance(item_val, list):
                    items_list = item_val
                else:
                    items_list = []
            elif isinstance(items_raw, list):
                items_list = items_raw
            else:
                items_list = []
            count = len(items_list)
            total += count
            if count < page_size:
                break
            page += 1
        return total

    # ------------------------------------------------------------------
    # 업체정보 조회
    # ------------------------------------------------------------------

    async def get_brands(self, keyword: str = "") -> dict[str, Any]:
        """브랜드 목록 조회."""
        params: dict[str, str] = {}
        if keyword:
            params["brandNm"] = keyword
        return await self._call_api(
            "GET", "/venInfo/0.1/listBrand.ssg", params=params or None
        )

    async def get_categories(
        self,
        std_ctg_id: str = "",
        item_reg_div_cd: str = "",
        site_no: str = "",
        std_cg_grp_cd: str = "",
        std_ctg_lcls_id: str = "",
        std_ctg_mcls_id: str = "",
        std_ctg_scls_id: str = "",
        std_ctg_srch_wrd: str = "",
        page: int = 1,
        page_size: int = 500,
    ) -> dict[str, Any]:
        """표준카테고리 조회 (키패스 포함).

        SSG Open API: GET /venInfo/0.2/listStdCtgKeyPath.ssg
        item_reg_div_cd: 상품등록구분 (10=온라인, 20=점포, 30=백화점, 미지정시 10)
        site_no: 사이트 번호 (6001=이마트몰, 6004=신세계몰, 6009=신세계백화점몰)
        std_ctg_srch_wrd: 표준 카테고리 소/세분류 명 검색어
        """
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if std_ctg_id:
            params["stdCtgDclsId"] = std_ctg_id
        if item_reg_div_cd:
            params["itemRegDivCd"] = item_reg_div_cd
        if site_no:
            params["siteNo"] = site_no
        if std_cg_grp_cd:
            params["stdCgGrpCd"] = std_cg_grp_cd
        if std_ctg_lcls_id:
            params["stdCtgLclsId"] = std_ctg_lcls_id
        if std_ctg_mcls_id:
            params["stdCtgMclsId"] = std_ctg_mcls_id
        if std_ctg_scls_id:
            params["stdCtgSclsId"] = std_ctg_scls_id
        if std_ctg_srch_wrd:
            params["stdCtgSrchWrd"] = std_ctg_srch_wrd
        return await self._call_api(
            "GET", "/venInfo/0.2/listStdCtgKeyPath.ssg", params=params
        )

    async def get_categories_v2(
        self,
        page: int = 1,
        page_size: int = 500,
    ) -> dict[str, Any]:
        """표준카테고리 조회 v2 (get_categories alias).

        category/service.py에서 호출하는 메서드명과 일치시키기 위한 래퍼.
        """
        return await self.get_categories(page=page, page_size=page_size)

    async def get_display_categories(
        self,
        std_ctg_dcls_id: str = "",
        site_no: str = "",
        disp_ctg_nm: str = "",
        page: int = 1,
        page_size: int = 500,
    ) -> dict[str, Any]:
        """전시카테고리 조회.

        SSG Open API: GET /common/0.2/listDispCtg.ssg
        std_ctg_dcls_id: 표준카테고리 세분류ID
        disp_ctg_nm: 전시카테고리명 검색어 (키워드 검색)
        """
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if std_ctg_dcls_id:
            params["stdCtgDclsId"] = std_ctg_dcls_id
        if site_no:
            params["siteNo"] = site_no
        if disp_ctg_nm:
            params["dispCtgNm"] = disp_ctg_nm
        return await self._call_api("GET", "/common/0.2/listDispCtg.ssg", params=params)

    async def get_display_categories_all(
        self,
        site_no: str = "",
        disp_ctg_nm: str = "",
        page: int = 1,
        page_size: int = 500,
    ) -> dict[str, Any]:
        """전시카테고리 전체 조회.

        SSG Open API: GET /common/0.1/displayCategory.ssg
        dispCtgId, dispCtgNm, siteNo 중 1개 이상 필수.
        siteNo만으로 사이트 전체 전시카테고리 조회 가능.
        응답: result.displayCategorys[].category → dispCtgId, dispCtgNm, dispCtgPathNm
        """
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if site_no:
            params["siteNo"] = site_no
        if disp_ctg_nm:
            params["dispCtgNm"] = disp_ctg_nm
        return await self._call_api(
            "GET", "/common/0.1/displayCategory.ssg", params=params
        )

    async def search_display_categories(
        self,
        keyword: str,
        site_no: str = "6005",
    ) -> dict[str, Any]:
        """전시카테고리명 키워드 검색 — displayCategory.ssg (v0.1).

        dispCtgNm 파라미터로 전시카테고리 명 검색.
        """
        params: dict[str, Any] = {
            "dispCtgNm": keyword,
            "siteNo": site_no,
        }
        return await self._call_api(
            "GET", "/common/0.1/displayCategory.ssg", params=params
        )

    async def list_origins(
        self,
        orplc_nm: str = "",
        manuf_cntry_yn: str = "Y",
        page: int = 1,
        page_size: int = 500,
    ) -> dict[str, Any]:
        """원산지/제조국 코드 목록 조회.

        SSG Open API: GET /common/0.1/listOrplc.ssg
        orplc_nm: 원산지명 검색어 (부분 매칭)
        manuf_cntry_yn: 제조국 사용 가능 여부 필터 (Y=사용가능만)
        """
        params: dict[str, Any] = {"page": page, "pageSize": page_size}
        if orplc_nm:
            params["orplcNm"] = orplc_nm
        if manuf_cntry_yn:
            params["manufCntryYn"] = manuf_cntry_yn
        return await self._call_api("GET", "/common/0.1/listOrplc.ssg", params=params)

    async def get_shipping_policies(self) -> dict[str, Any]:
        """배송비정책 목록 조회."""
        return await self._call_api("GET", "/venInfo/0.1/listShppcstPlcy.ssg")

    async def get_addresses(self) -> dict[str, Any]:
        """출고/반품 주소 목록 조회 (v0.3 필수)."""
        return await self._call_api("GET", "/venInfo/0.3/listVenAddr.ssg")

    async def fetch_infra(self) -> dict[str, Any]:
        """상품 등록에 필요한 인프라 ID 자동 조회.

        반환: whoutShppcstId, retShppcstId, whoutAddrId, snbkAddrId, origin_code_map

        결과는 api_key 기준 15분간 인메모리 캐싱 — 상품마다 반복 호출하는 비용 제거.
        """
        now = time.time()
        cached = _infra_cache.get(self.api_key)
        if cached is not None:
            data, ts = cached
            if now - ts < _INFRA_CACHE_TTL:
                logger.debug(
                    f"[SSG] fetch_infra 캐시 hit (잔여 {int(_INFRA_CACHE_TTL - (now - ts))}초)"
                )
                return data

        infra: dict[str, Any] = {}

        # 원산지 응답 파싱 헬퍼
        def _parse_origin_resp(resp: dict) -> dict[str, str]:
            orplc_list_wrap = resp.get("result", {}).get("orplcs", [{}])
            if isinstance(orplc_list_wrap, dict):
                orplc_list_wrap = [orplc_list_wrap]
            orplc_raw = orplc_list_wrap[0].get("orplc", []) if orplc_list_wrap else []
            if isinstance(orplc_raw, dict):
                orplc_items = [orplc_raw]
            else:
                orplc_items = orplc_raw
            result: dict[str, str] = {}
            for item in orplc_items:
                nm = (item.get("orplcNm") or "").strip()
                oid = str(item.get("orplcId") or "")
                if nm and oid:
                    result[nm.lower()] = oid
            return result

        # 배송비정책 / 주소 / 원산지 3개 API 병렬 조회 (순차 → 동시)
        import asyncio as _asyncio

        sp_raw, addr_raw, origin_resp_raw = await _asyncio.gather(
            self.get_shipping_policies(),
            self.get_addresses(),
            self.list_origins(manuf_cntry_yn="Y", page_size=500),
            return_exceptions=True,
        )

        # 배송비정책 파싱
        if isinstance(sp_raw, Exception):
            logger.warning(f"[SSG] 배송비정책 조회 실패: {sp_raw}")
        else:
            try:
                policies = sp_raw.get("result", {}).get("shppcstPlcys", [{}])
                policy_list = policies[0].get("shppcstPlcy", []) if policies else []
                for p in policy_list:
                    div = p.get("shppcstPlcyDivCd")
                    sid = p.get("shppcstId", "")
                    # 10=출고(일반배송), 20=반품
                    if div == 10 and "whoutShppcstId" not in infra:
                        infra["whoutShppcstId"] = sid
                    elif div == 20 and "retShppcstId" not in infra:
                        infra["retShppcstId"] = sid
            except Exception as exc:
                logger.warning(f"[SSG] 배송비정책 파싱 실패: {exc}")

        # 주소 파싱
        if isinstance(addr_raw, Exception):
            logger.warning(f"[SSG] 주소 조회 실패: {addr_raw}")
        else:
            try:
                logger.info(f"[SSG DEBUG] 주소 조회 원본 응답: {addr_raw}")
                addr_result = addr_raw.get("result", {})
                addr_list = addr_result.get("venAddrDelInfo", [])
                # XStream 래핑: list 또는 dict(단일 항목) 모두 처리
                if isinstance(addr_list, dict):
                    addr_list = [addr_list]
                addrs_raw = (
                    addr_list[0].get("venAddrDelInfoDto", []) if addr_list else []
                )
                # venAddrDelInfoDto도 단일 항목이면 dict로 올 수 있음
                if isinstance(addrs_raw, dict):
                    addrs = [addrs_raw]
                else:
                    addrs = addrs_raw

                # 기본주소(bascAddrYn=Y) 우선, 없으면 첫 번째 사용
                base_addr = next((a for a in addrs if a.get("bascAddrYn") == "Y"), None)
                if not base_addr and addrs:
                    base_addr = addrs[0]

                if base_addr:
                    # doroAddrId 우선 사용 (SSG API whoutAddrId 유효값), 없으면 grpAddrId 폴백
                    addr_id = base_addr.get("doroAddrId", "") or base_addr.get(
                        "grpAddrId", ""
                    )
                    logger.info(
                        f"[SSG DEBUG] 선택된 주소: doroAddrId={base_addr.get('doroAddrId')}, grpAddrId={base_addr.get('grpAddrId')}, 사용={addr_id}, 전체={base_addr}"
                    )
                    infra["whoutAddrId"] = addr_id
                    infra["snbkAddrId"] = addr_id
            except Exception as exc:
                logger.warning(f"[SSG] 주소 파싱 실패: {exc}")

        # 원산지 파싱 (실패 시 필터 없이 재시도)
        try:
            if isinstance(origin_resp_raw, Exception):
                logger.warning(f"[SSG] 원산지 1차 조회 실패: {origin_resp_raw}")
                origin_code_map: dict[str, str] = {}
            else:
                origin_code_map = _parse_origin_resp(origin_resp_raw)

            # 빈 경우 필터 없이 재시도
            if not origin_code_map:
                logger.warning("[SSG] 원산지 코드 0개 — 필터 없이 재조회 시도")
                origin_resp2 = await self.list_origins(manuf_cntry_yn="", page_size=500)
                origin_code_map = _parse_origin_resp(origin_resp2)
            infra["origin_code_map"] = origin_code_map
            logger.info(f"[SSG] 원산지 코드 {len(origin_code_map)}개 조회 완료")
        except Exception as exc:
            logger.warning(f"[SSG] 원산지 코드 조회 실패: {exc}")
            infra["origin_code_map"] = {}

        # 결과 캐싱
        _infra_cache[self.api_key] = (infra, time.time())
        logger.debug("[SSG] fetch_infra 결과 캐싱 완료 (TTL 15분)")
        return infra

    # ------------------------------------------------------------------
    # 계약 브랜드 매핑 (brandNm → brandId)
    # ------------------------------------------------------------------

    # SSG 계약 브랜드 목록 — 키: 매칭용 소문자, 값: (brandId, 표시명)
    CONTRACTED_BRANDS: dict[str, tuple[str, str]] = {
        "게스": ("2000002737", "게스"),
        "guess": ("2000002737", "게스"),
        "나이키": ("2000004827", "나이키"),
        "nike": ("2000004827", "나이키"),
        "노스페이스": ("2000006637", "노스페이스"),
        "the north face": ("2000006637", "노스페이스"),
        "northface": ("2000006637", "노스페이스"),
        "뉴발란스": ("2011015410", "뉴발란스"),
        "new balance": ("2011015410", "뉴발란스"),
        "스노우피크": ("2011000375", "스노우피크"),
        "snow peak": ("2011000375", "스노우피크"),
        "snowpeak": ("2011000375", "스노우피크"),
        "스케쳐스": ("2000006059", "스케쳐스"),
        "skechers": ("2000006059", "스케쳐스"),
        "아디다스": ("2000000507", "아디다스"),
        "adidas": ("2000000507", "아디다스"),
        "에코": ("2011012514", "에코"),
        "ecco": ("2011012514", "에코"),
        "잔스포츠": ("2000020559", "잔스포츠"),
        "jansport": ("2000020559", "잔스포츠"),
        "지포어": ("3000020249", "지포어"),
        "g/fore": ("3000020249", "지포어"),
        "gfore": ("3000020249", "지포어"),
        "코오롱스포츠": ("2000003676", "코오롱스포츠"),
        "kolon sport": ("2000003676", "코오롱스포츠"),
        "크레모아": ("3000006049", "크레모아"),
        "claymore": ("3000006049", "크레모아"),
        "푸마": ("2000005405", "푸마"),
        "puma": ("2000005405", "푸마"),
        "휠라": ("2000002338", "휠라"),
        "fila": ("2000002338", "휠라"),
    }

    @classmethod
    def match_brand(cls, brand_name: str) -> tuple[str, str]:
        """상품 브랜드명으로 SSG 계약 브랜드 매칭.

        반환: (brandId, 표시명). 매칭 실패 시 ("9999999999", "").
        """
        if not brand_name:
            return "9999999999", ""

        lower = brand_name.strip().lower()

        # 정확 매칭
        if lower in cls.CONTRACTED_BRANDS:
            return cls.CONTRACTED_BRANDS[lower]

        # 부분 매칭 (브랜드명이 상품 브랜드에 포함)
        for key, (bid, display) in cls.CONTRACTED_BRANDS.items():
            if key in lower or lower in key:
                return bid, display

        return "9999999999", ""

    @staticmethod
    def remove_brand_from_name(name: str, brand_display: str) -> str:
        """상품명에서 브랜드명 제거 (SSG 정책: 상품명에 브랜드명 포함 불가)."""
        if not brand_display or not name:
            return name

        # 한글 브랜드명 제거
        cleaned = re.sub(re.escape(brand_display), "", name, flags=re.IGNORECASE)
        # CONTRACTED_BRANDS에서 영문명도 찾아서 제거
        for key, (_, disp) in SSGClient.CONTRACTED_BRANDS.items():
            if disp == brand_display and key != brand_display.lower():
                cleaned = re.sub(re.escape(key), "", cleaned, flags=re.IGNORECASE)

        # 연속 공백 정리
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        # 앞뒤 특수문자 정리
        cleaned = re.sub(r"^[\s\-/]+|[\s\-/]+$", "", cleaned)
        return cleaned or name  # 빈 문자열이면 원래 이름 유지

    # ------------------------------------------------------------------
    # 원산지 코드 매핑
    # ------------------------------------------------------------------

    # 영문 국가명 → 한국어 국가명 (SSG listOrplc.ssg는 한국어 국가명 기반)
    _ORIGIN_KO_MAP: dict[str, str] = {
        "korea": "대한민국",
        "south korea": "대한민국",
        "한국": "대한민국",
        "국내": "대한민국",
        "china": "중국",
        "중국": "중국",
        "vietnam": "베트남",
        "viet nam": "베트남",
        "베트남": "베트남",
        "indonesia": "인도네시아",
        "인도네시아": "인도네시아",
        "cambodia": "캄보디아",
        "캄보디아": "캄보디아",
        "italy": "이탈리아",
        "이탈리아": "이탈리아",
        "france": "프랑스",
        "프랑스": "프랑스",
        "usa": "미국",
        "united states": "미국",
        "미국": "미국",
        "portugal": "포르투갈",
        "포르투갈": "포르투갈",
        "germany": "독일",
        "독일": "독일",
        "spain": "스페인",
        "스페인": "스페인",
        "bangladesh": "방글라데시",
        "방글라데시": "방글라데시",
        "myanmar": "미얀마",
        "미얀마": "미얀마",
        "thailand": "태국",
        "태국": "태국",
        "india": "인도",
        "인도": "인도",
        "taiwan": "대만",
        "대만": "대만",
        "japan": "일본",
        "일본": "일본",
        "philippines": "필리핀",
        "필리핀": "필리핀",
    }

    @classmethod
    def _resolve_origin_code(
        cls,
        origin_text: str,
        origin_code_map: dict[str, str],
    ) -> str:
        """원산지 텍스트를 SSG 원산지 코드로 변환.

        origin_text: 수집된 원산지 텍스트 (예: "Vietnam", "베트남")
        origin_code_map: fetch_infra()에서 조회한 {orplcNm(소문자): orplcId} 딕셔너리
        반환: SSG orplcId. 매핑 실패 시 빈 문자열
        """
        if not origin_text or not origin_code_map:
            return ""

        lower = origin_text.strip().lower()

        # 1단계: 직접 매핑 (소문자 일치)
        if lower in origin_code_map:
            return origin_code_map[lower]

        # 2단계: 영문 → 한국어 변환 후 재시도
        ko_name = cls._ORIGIN_KO_MAP.get(lower, "")
        if ko_name and ko_name.lower() in origin_code_map:
            return origin_code_map[ko_name.lower()]

        # 3단계: 부분 매칭 (origin_code_map 키에 lower가 포함되거나 그 반대)
        for key, code in origin_code_map.items():
            if lower in key or key in lower:
                return code

        logger.warning(f"[SSG] 원산지 코드 매핑 실패: {origin_text!r}")
        return ""

    # ------------------------------------------------------------------
    # XStream XML 변환 헬퍼
    # ------------------------------------------------------------------

    @staticmethod
    def _to_xml(data: Any, tag: str) -> str:
        """Python dict를 XStream 호환 XML 엘리먼트로 변환.

        {"sites": {"site": [{"siteNo":"6004"}]}}
        → <sites><site><siteNo>6004</siteNo></site></sites>
        """
        import re as _re
        from xml.sax.saxutils import escape as _esc

        _invalid_xml_chars = _re.compile(
            r"[^\x09\x0A\x0D\x20-퟿-�\U00010000-\U0010FFFF]"
        )

        def _clean(s: str) -> str:
            return _invalid_xml_chars.sub("", s)

        def _serialize(value: Any) -> str:
            if isinstance(value, dict):
                return "".join(
                    _elem(k, v) for k, v in value.items() if v is not None and v != ""
                )
            return _esc(_clean(str(value))) if value is not None else ""

        def _elem(key: str, value: Any) -> str:
            if value is None or value == "":
                return ""
            if isinstance(value, list):
                return "".join(f"<{key}>{_serialize(item)}</{key}>" for item in value)
            return f"<{key}>{_serialize(value)}</{key}>"

        return f"<{tag}>{_serialize(data)}</{tag}>"

    @staticmethod
    def _wrap_list(items: list[dict[str, Any]], element_name: str) -> dict[str, Any]:
        """XStream 호환: 배열을 요소명 래퍼로 감싸기.

        단일 항목이면 객체로, 복수 항목이면 배열로.
        예) [{"a":1}] → {"item": {"a":1}}
            [{"a":1},{"a":2}] → {"item": [{"a":1},{"a":2}]}
        """
        if len(items) == 1:
            return {element_name: items[0]}
        return {element_name: items}

    @staticmethod
    def _wrap_list_always_array(
        items: list[dict[str, Any]], element_name: str
    ) -> dict[str, Any]:
        """XStream 호환: 항상 배열로 감싸기 (uitems, uitemPluralPrcs 등 배열 강제 필드용).

        SSG API v0.5에서 uitems, uitemPluralPrcs는 항목 수에 상관없이 배열이어야 함.
        예) [{"a":1}] → {"item": [{"a":1}]}
            [{"a":1},{"a":2}] → {"item": [{"a":1},{"a":2}]}
        """
        return {element_name: items}

    # ------------------------------------------------------------------
    # 상품 데이터 변환
    # ------------------------------------------------------------------

    def transform_product(
        self,
        product: dict[str, Any],
        category_id: str = "",
        brand_id: str = "",
        infra: Optional[dict[str, str]] = None,
        std_category_id: str = "",
        main_category_id: str = "",
        margin_rate: int = 0,
        shpp_rqrm_dcnt: int = 3,
        day_max_qty: int = 5,
        once_min_qty: int = 1,
        once_max_qty: int = 5,
        brand_mappings: Optional[list[dict]] = None,
    ) -> dict[str, Any]:
        """SambaCollectedProduct → SSG insertItem 요청 데이터 변환.

        XStream JSON 형식으로 변환하며, infra 딕셔너리에서
        배송비/주소 ID를 가져온다.

        Args:
          category_id: 전시카테고리 ID (dispCtgId) — 사용자가 매핑한 "ssg" 카테고리
          std_category_id: 표준카테고리 ID (stdCtgId) — 사용자가 매핑한 "ssg_std" 카테고리
          margin_rate: 설정 페이지 마진율(%) — 0이면 판매가/원가로 역산
          shpp_rqrm_dcnt: 배송소요일 — 설정 페이지 값
          day_max_qty: 1일 최대 주문수량
          once_min_qty: 1회 최소 주문수량
          once_max_qty: 1회 최대 주문수량
        """
        import re as _re

        inf = infra or {}
        sale_price = int(product.get("sale_price", 0) or 0)
        cost = int(product.get("cost", 0) or 0) or int(sale_price * 0.7)
        _raw_detail_html = (
            product.get("detail_html", "")
            or f"<p>{(product.get('name', '') or '')[:200]}</p>"
        )
        # SSG XML 바디 크기 제한 — 전체 HTML이 너무 크면 Tomcat 400 발생
        _detail_bytes = _raw_detail_html.encode("utf-8")
        detail_html = (
            _detail_bytes[:50000].decode("utf-8", errors="ignore")
            if len(_detail_bytes) > 50000
            else _raw_detail_html
        )
        images = product.get("images") or []
        brand = product.get("brand", "")
        material = product.get("material", "") or "상세설명참조"
        color = product.get("color", "") or "상세설명참조"
        _raw_manufacturer = product.get("manufacturer", "") or brand or "상세설명참조"
        # "제조사: Nike inc. / 수입처 : 나이키코리아(유)" 형태 → 첫 번째 값만 추출
        # SSG manufcoNm 필드는 단순 회사명이어야 함 (콜론/슬래시 포함 시 파싱 실패 가능)
        if "/" in _raw_manufacturer:
            _raw_manufacturer = _raw_manufacturer.split("/")[0].strip()
        if ":" in _raw_manufacturer:
            _raw_manufacturer = _raw_manufacturer.split(":", 1)[1].strip()
        manufacturer = _raw_manufacturer[:100] if _raw_manufacturer else (brand or "상세설명참조")
        style_no = product.get("style_no", "") or product.get("styleNo", "") or ""

        # 브랜드 매칭 — 정책 브랜드 매핑 우선, 없으면 CONTRACTED_BRANDS fallback
        def _match_from_mappings(name: str, mappings: list[dict]) -> tuple[str, str]:
            if not name or not mappings:
                return "", ""
            lower = name.strip().lower()
            lower_ns = lower.replace(" ", "")
            for m in mappings:
                nm = (m.get("brandNm") or "").strip().lower()
                nm_ns = nm.replace(" ", "")
                if nm_ns and nm_ns == lower_ns:
                    return m["brandId"], m["brandNm"]
            for m in mappings:
                nm = (m.get("brandNm") or "").strip().lower()
                nm_ns = nm.replace(" ", "")
                if nm_ns and (nm_ns in lower_ns or lower_ns in nm_ns):
                    return m["brandId"], m["brandNm"]
            return "", ""

        _mappings = brand_mappings or []
        matched_brand_id, matched_brand_name = _match_from_mappings(brand, _mappings)
        if not matched_brand_id and manufacturer:
            matched_brand_id, matched_brand_name = _match_from_mappings(manufacturer, _mappings)
        if not matched_brand_id:
            matched_brand_id, matched_brand_name = self.match_brand(brand)
            if matched_brand_id == "9999999999" and manufacturer:
                matched_brand_id, matched_brand_name = self.match_brand(manufacturer)
        if brand_id:
            matched_brand_id = brand_id  # 명시적 지정 우선

        # ── 상품명 처리 ──
        # 1) 계약 브랜드명 제거 (CONTRACTED_BRANDS 매칭 결과)
        raw_name = product.get("name", "") or ""
        cleaned_name = self.remove_brand_from_name(raw_name, matched_brand_name)
        # 1-2) 소싱처 brand 필드 직접 제거 (CONTRACTED_BRANDS 매칭 여부와 무관하게)
        if brand and brand != matched_brand_name:
            cleaned_name = self.remove_brand_from_name(cleaned_name, brand)
        # 2) 특수문자 제거 (한글·영문·숫자·공백만 허용, 언더스코어도 제거)
        cleaned_name = _re.sub(r"[^가-힣a-zA-Z0-9\s]", " ", cleaned_name)
        cleaned_name = _re.sub(r"\s{2,}", " ", cleaned_name).strip()
        # 3) 90byte 제한 (공백 포함)
        encoded = cleaned_name.encode("utf-8")
        if len(encoded) > 90:
            # 90byte 이내로 잘라냄 (멀티바이트 경계 보호)
            truncated = encoded[:90]
            cleaned_name = truncated.decode("utf-8", errors="ignore").strip()
        name = cleaned_name or raw_name[:30]

        logger.info(
            f"[SSG] 브랜드 매칭: {brand} → {matched_brand_id}({matched_brand_name}), 상품명: {name!r}"
        )

        now = datetime.now(timezone.utc)
        disp_start = now.strftime("%Y%m%d")
        disp_end = "29991231"

        options = product.get("options") or []

        # ── 마진율 결정 ──
        # 설정 페이지 마진율이 있으면 우선 사용, 없으면 판매가/원가 역산
        if margin_rate > 0:
            margin_pct = margin_rate
        elif sale_price > 0 and cost > 0:
            margin_pct = round((sale_price - cost) / sale_price * 100)
        else:
            margin_pct = 30

        # 배송/주소 ID
        whout_shppcst_id = inf.get("whoutShppcstId", "")
        ret_shppcst_id = inf.get("retShppcstId", "")
        whout_addr_id = inf.get("whoutAddrId", "")
        snbk_addr_id = inf.get("snbkAddrId", "")
        add_shppcst_jeju = inf.get("addShppcstIdJeju", "")
        add_shppcst_island = inf.get("addShppcstIdIsland", "")

        # ── 이미지 (XStream: itemImgs → imgInfo) ──
        item_imgs_list = []
        for idx, url in enumerate(images[:10]):
            item_imgs_list.append(
                {
                    "dataSeq": idx + 1,
                    "dataFileNm": url,
                    "rplcTextNm": name[:50] if idx == 0 else f"{name[:40]}_{idx + 1}",
                }
            )

        # ── 상품관리속성 — 원산지 계산 이후 호출 (수입여부 일관성 보장) ──
        # build_ssg_notice 호출은 아래 원산지 코드 결정 블록 이후로 이동됨

        # std_category_id: 표준카테고리 ID (ssg_std 매핑값)
        # category_id: 전시카테고리 ID (ssg 매핑값)
        # stdCtgId는 반드시 표준카테고리여야 함 — 전시카테고리 ID를 넣으면 API 파싱 오류 발생
        effective_std_cat = std_category_id or product.get("_std_category_id", "") or ""

        # ── 검색어 (태그 기반, 최대 10개, 500byte) ──
        tags = product.get("tags") or product.get("keywords") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        # 상품명/브랜드/모델명과 중복 제거 후 최대 10개
        name_lower = name.lower()
        brand_lower = (matched_brand_name or brand or "").lower()
        model_lower = style_no.lower()
        filtered_tags: list[str] = []
        total_bytes = 0
        for tag in tags:
            tag = tag.strip()
            if not tag:
                continue
            # 메타 마커 태그 제거 (__ai_tagged__ 등 언더스코어로 감싼 마커)
            if tag.startswith("__") and tag.endswith("__"):
                continue
            tag_lower = tag.lower()
            # 상품명/브랜드/모델명에 포함된 키워드는 중복 제외
            if (
                tag_lower in name_lower
                or tag_lower in brand_lower
                or tag_lower in model_lower
            ):
                continue
            tag_bytes = len(tag.encode("utf-8")) + 1  # 콤마 포함
            if total_bytes + tag_bytes > 500:
                break
            if len(filtered_tags) >= 10:
                break
            filtered_tags.append(tag)
            total_bytes += tag_bytes
        search_keyword = ",".join(filtered_tags) if filtered_tags else ""

        # 재고 수량 (단품 미래 활용 대비 보존)
        stock_qty = int(product.get("stock", 999) or 999)

        # 원산지 코드 결정
        # 원산지 정보 없으면 베트남 폴백 (실제 제조 다발국 기준)
        origin_map = inf.get("origin_code_map", {})
        korea_origin_code = self._resolve_origin_code("한국", origin_map)
        raw_origin = product.get("origin", "") or ""
        direct_origin_code = self._resolve_origin_code(raw_origin, origin_map)
        # fallback 순서: 상품 원산지 → 베트남 → 중국 → origin_map 첫 번째 값
        resolved_origin = (
            direct_origin_code
            or self._resolve_origin_code("베트남", origin_map)
            or self._resolve_origin_code("중국", origin_map)
            or (next(iter(origin_map.values()), None) if origin_map else None)
        )
        # 수입여부: 실제 전송되는 제조국 코드(resolved_origin)가 한국 코드와 다르면 수입품
        # prodManufCntryId에 들어가는 값과 반드시 일치해야 함 (불일치 시 SSG API 거부)
        is_imported = (
            (resolved_origin != korea_origin_code)
            if (resolved_origin and korea_origin_code)
            else True
        )
        # notice_utils가 동일한 수입여부를 쓰도록 product에 주입
        product = {**product, "_ssg_import_yn": "Y" if is_imported else "N"}

        # ── 상품관리속성 (카테고리별 동적 생성) — 원산지/_ssg_import_yn 주입 후 호출 ──
        from backend.domain.samba.proxy.notice_utils import build_ssg_notice

        item_mng_prop_cls_id, item_mng_attrs_list = build_ssg_notice(product)

        data: dict[str, Any] = {
            "itemNm": name,
            "brandId": matched_brand_id,
            "stdCtgId": effective_std_cat,
            "mdlNm": style_no or None,
            "manufcoNm": manufacturer,
            **({"prodManufCntryId": resolved_origin} if resolved_origin else {}),
            "sites": self._wrap_list_always_array(
                [{"siteNo": self.site_no, "sellStatCd": "20"}],
                "site",
            ),
            "b2eAplRngCd": "10",
            "b2cAplRngCd": "10",
            "b2bAplRngCd": "10",
            "itemMngPropClsId": item_mng_prop_cls_id,
            "itemMngAttrs": self._wrap_list(item_mng_attrs_list, "itemMngAttr"),
            "dispCtgs": self._wrap_list_always_array(
                [e for e in [
                    {"siteNo": self.site_no, "dispCtgId": category_id} if category_id else None,
                    {"siteNo": "6005", "dispCtgId": main_category_id} if main_category_id else None,
                ] if e],
                "dispCtg",
            )
            if (category_id or main_category_id)
            else None,
            "dispStrtDts": disp_start,
            "dispEndDts": disp_end,
            "srchPsblYn": "Y",
            "itemSrchwdNm": search_keyword
            or (matched_brand_name or brand or "")[:50]
            or None,
            "minOnetOrdPsblQty": once_min_qty,
            "maxOnetOrdPsblQty": once_max_qty,
            "max1dyOrdPsblQty": day_max_qty,
            "adultItemTypeCd": "90",
            "hriskItemYn": "N",
            "nitmAplYn": "N",
            "buyFrmCd": "60",
            "txnDivCd": "10",
            "prcMngMthd": "1",
            "salesPrcInfos": self._wrap_list_always_array(
                [{"splprc": cost, "sellprc": sale_price, "mrgrt": margin_pct}],
                "uitemPrc",
            ),
            "itemSellTypeCd": "10",
            "itemSellTypeDtlCd": "10",
            "itemChrctDivCd": "10",
            "itemChrctDtlCd": "10",
            "exusItemDivCd": "10",
            "exusItemDtlCd": "10",
            "shppItemDivCd": "01",
            "retExchPsblYn": "Y",
            "ssgstrSellYn": "N",
            "giftPsblYn": "N",
            "palimpItemYn": "N",
            "itemShppCritns": self._wrap_list_always_array(
                [
                    {
                        "shppMainCd": "41",
                        "shppMthdCd": "20",
                        "tdShppPsblYn": "N",
                        "jejuShppDisabYn": "N",
                        "ismtarShppDisabYn": "N",
                        "whoutAddrId": whout_addr_id,
                        "snbkAddrId": snbk_addr_id,
                        "whoutShppcstId": whout_shppcst_id,
                        "retShppcstId": ret_shppcst_id,
                        "mareaShppYn": "N",
                        **({"jejuAddShppcstId": add_shppcst_jeju} if add_shppcst_jeju else {}),
                        **({"ismtarAddShppcstId": add_shppcst_island} if add_shppcst_island else {}),
                    }
                ],
                "itemShppCritn",
            ),
            "shppRqrmDcnt": shpp_rqrm_dcnt,
            "itemImgs": self._wrap_list_always_array(item_imgs_list, "imgInfo")
            if item_imgs_list
            else None,
            "itemDesc": detail_html,
            "invMngYn": "Y",
            "invQtyMarkgYn": "N",
            "itemSellWayCd": "10",
            "itemStatTypeCd": "10",
            "whinNotiYn": "Y",
        }

        if not effective_std_cat:
            logger.warning("[SSG] stdCtgId(표준카테고리 ID)가 없습니다. API 등록 실패할 수 있습니다.")

        if not data.get("itemNm"):
            data["itemNm"] = raw_name[:49] or "상품명없음"

        data = {k: v for k, v in data.items() if v is not None and v != ""}

        # 단품(옵션) 추가
        if options:
            uitems_list = []
            uitem_prices_list = []
            for idx, opt in enumerate(options):
                opt_name = (
                    opt.get("name", "") or opt.get("size", "") or f"옵션{idx + 1}"
                )
                _raw_stock = opt.get("stock")
                _max_stock_cap = int(product.get("_max_stock") or 0)
                if _raw_stock is None or _raw_stock == "":
                    opt_stock = _max_stock_cap if _max_stock_cap > 0 else 99
                elif int(_raw_stock) <= 0:
                    opt_stock = 0
                else:
                    opt_stock = (
                        min(int(_raw_stock), _max_stock_cap)
                        if _max_stock_cap > 0
                        else int(_raw_stock)
                    )
                is_sold_out = opt.get("isSoldOut", False)
                temp_id = str(idx + 1)

                uitems_list.append(
                    {
                        "tempUitemId": temp_id,
                        "uitemOptnTypeNm1": "사이즈",
                        "uitemOptnNm1": opt_name,
                        "baseInvQty": 0 if is_sold_out else opt_stock,
                        "useYn": "Y",
                    }
                )
                uitem_prices_list.append(
                    {
                        "tempUitemId": temp_id,
                        "splprc": cost,
                        "sellprc": sale_price,
                        "mrgrt": margin_pct,
                    }
                )

            data["itemSellTypeCd"] = "20"
            data["uitemAttr"] = {
                "uitemCacOptnYn": "N",
                "uitemOptnChoiTypeCd1": "10",
                "uitemOptnExpsrTypeCd1": "10",
            }
            data["uitems"] = self._wrap_list_always_array(uitems_list, "uitem")
            data["uitemPluralPrcs"] = self._wrap_list_always_array(
                uitem_prices_list, "uitemPrc"
            )

        return data

    # ------------------------------------------------------------------
    # 주문/반품 관련
    # ------------------------------------------------------------------

    async def get_orders(self, days: int = 7) -> list[dict[str, Any]]:
        """주문 목록 조회 — 최근 days일 이내 (최대 180일)."""
        KST = timezone(timedelta(hours=9))
        now = datetime.now(KST)
        days = min(days, 180)
        start_dt = (now - timedelta(days=days)).strftime("%Y%m%d")
        end_dt = now.strftime("%Y%m%d")
        body = {
            "requestShppDirection": {
                "perdType": "02",
                "perdStrDts": start_dt,
                "perdEndDts": end_dt,
            }
        }
        data = await self._call_api(
            "POST", "/api/pd/1/listShppDirection.ssg", body=body
        )
        result = data.get("result", {})
        if not isinstance(result, dict):
            logger.warning(
                f"[SSG 주문] result가 dict가 아님: {type(result)} — {str(result)[:500]}"
            )
            return []

        directions = result.get("shppDirections", [])
        # XStream 단일 항목: list가 아닌 dict로 올 수 있음
        if isinstance(directions, dict):
            directions = [directions]
        elif not isinstance(directions, list):
            logger.warning(
                f"[SSG 주문] shppDirections 타입 이상: {type(directions)} — {str(directions)[:500]}"
            )
            return []

        # SSG XStream 래핑 해제
        # 실제 구조: [{"shppDirection": [{주문1}, {주문2}, ...]}, ...]
        # shppDirection 값이 list(여러 주문) 또는 dict(단일 주문)일 수 있음
        unwrapped: list[dict[str, Any]] = []
        unwrap_list_count = 0
        unwrap_dict_count = 0
        unwrap_invalid_count = 0
        for index, direction in enumerate(directions):
            if not isinstance(direction, dict):
                unwrap_invalid_count += 1
                logger.warning(
                    f"[SSG 주문] shppDirections[{index}]가 dict가 아님: "
                    f"type={type(direction).__name__}, value={str(direction)[:300]}"
                )
                continue
            inner = direction.get("shppDirection")
            if isinstance(inner, list):
                unwrap_list_count += 1
                # shppDirection이 배열 — 각 항목이 개별 주문
                for item in inner:
                    if isinstance(item, dict):
                        unwrapped.append(item)
                    else:
                        unwrap_invalid_count += 1
                        logger.warning(
                            f"[SSG 주문] shppDirection 리스트 내부 항목이 dict가 아님: "
                            f"type={type(item).__name__}, value={str(item)[:300]}"
                        )
            elif isinstance(inner, dict):
                unwrap_dict_count += 1
                # shppDirection이 단일 dict — 주문 1건
                unwrapped.append(inner)
            else:
                unwrap_invalid_count += 1
                # shppDirection이 None이거나 비정상 타입 — 건너뜀
                logger.warning(
                    f"[SSG 주문] shppDirection 언래핑 실패: "
                    f"index={index}, type={type(inner).__name__}, "
                    f"direction_keys={list(direction.keys())}, value={str(inner)[:300]}"
                )

        logger.info(
            f"[SSG 주문] 언래핑 결과: directions={len(directions)}, "
            f"list_wrapper={unwrap_list_count}, dict_wrapper={unwrap_dict_count}, "
            f"invalid={unwrap_invalid_count}, orders={len(unwrapped)}"
        )
        if unwrapped:
            first = unwrapped[0]
            logger.info(
                f"[SSG 주문] {len(unwrapped)}건 파싱, 첫 주문 키 샘플: "
                f"{list(first.keys())[:20]}"
            )
            logger.info(
                f"[SSG 주문] 첫 주문 실제 필드명: "
                f"{', '.join(sorted(map(str, first.keys()))[:30])}"
            )
            logger.info(
                f"[SSG 주문] 샘플 값 — ordNo={first.get('ordNo')}, "
                f"orordNo={first.get('orordNo')}, "
                f"shppNo={first.get('shppNo')}, ordItemSeq={first.get('ordItemSeq')}, "
                f"itemNm={first.get('itemNm')}, siteNo={first.get('siteNo')}, "
                f"rlordAmt={first.get('rlordAmt')}"
            )
            # 전체 주문 번호 목록 로그 (중복 진단용)
            for _i, _o in enumerate(unwrapped):
                logger.info(
                    f"[SSG 주문] [{_i}] ordNo={_o.get('ordNo')}, orordNo={_o.get('orordNo')}, "
                    f"shppNo={_o.get('shppNo')}, ordItemSeq={_o.get('ordItemSeq')}, "
                    f"siteNo={_o.get('siteNo')}, itemId={_o.get('itemId')}"
                )
        else:
            logger.info(
                f"[SSG 주문] 언래핑 후 0건: raw directions={str(directions)[:500]}"
            )
        return unwrapped

    async def get_cancel_requests(self, days: int = 7) -> list[dict[str, Any]]:
        """취소신청 목록 조회 — 최근 days일 이내 (최대 7일).

        API: GET /api/claim/v2/cancel/requests
        listShppDirection과 달리 취소신청 상태 주문만 반환한다.
        """
        KST = timezone(timedelta(hours=9))
        now = datetime.now(KST)
        # 취소신청 API는 7일 이내만 허용
        days = min(days, 7)
        start_dt = (now - timedelta(days=days)).strftime("%Y%m%d")
        end_dt = now.strftime("%Y%m%d")
        data = await self._call_api(
            "GET",
            "/api/claim/v2/cancel/requests",
            params={"perdStrDts": start_dt, "perdEndDts": end_dt},
        )
        result_obj = data.get("result", {})
        if not isinstance(result_obj, dict):
            result_obj = {}
        result_list = result_obj.get("resultData", [])
        if isinstance(result_list, dict):
            result_list = [result_list]
        elif not isinstance(result_list, list):
            logger.warning(
                f"[SSG 취소신청] resultData 타입 이상: {type(result_list)} — {str(result_list)[:300]}"
            )
            return []
        logger.info(f"[SSG 취소신청] {len(result_list)}건 조회")
        return result_list

    def parse_cancel_request(
        self,
        raw: dict[str, Any],
        account_id: str,
        label: str,
        fee_rate: float,
    ) -> dict[str, Any]:
        """취소신청 목록 응답 1건을 SambaOrder insert 형식으로 변환."""
        ord_no = str(raw.get("ordNo", "") or "")
        ord_item_seq = str(raw.get("ordItemSeq", "") or "")
        or_ord_no = str(raw.get("orordNo", "") or "")
        item_id_str = str(raw.get("itemId", "") or "")
        product_image = ""
        if len(item_id_str) >= 6:
            d1, d2, d3 = item_id_str[-2:], item_id_str[-4:-2], item_id_str[-6:-4]
            product_image = (
                f"https://sitem.ssgcdn.com/{d1}/{d2}/{d3}/item/{item_id_str}_i1_250.jpg"
            )
        return {
            "order_number": ord_no,
            # 형식: "|ordItemSeq|orordNo" (shppNo 없음, 취소신청에는 배송번호 불필요)
            "shipment_id": f"|{ord_item_seq}|{or_ord_no}",
            "channel_id": account_id,
            "channel_name": label,
            "product_id": item_id_str,
            "product_name": str(raw.get("itemNm", "") or ""),
            "product_image": product_image,
            "customer_name": "",
            "customer_phone": "",
            "customer_address": "",
            "quantity": int(raw.get("procOrdQty", 1) or 1),
            "sale_price": 0.0,
            "cost": 0,
            "fee_rate": fee_rate,
            "revenue": 0.0,
            "source": "ssg",
            "status": "cancel_requested",
            "shipping_status": "취소요청",
        }

    async def confirm_order(self, shpp_no: str, shpp_seq: str) -> dict[str, Any]:
        """발주확인 처리."""
        body = {
            "requestOrderSubjectManage": {
                "shppNo": shpp_no,
                "shppSeq": shpp_seq,
            }
        }
        return await self._call_api(
            "POST", "/api/pd/1/updateOrderSubjectManage.ssg", body=body
        )

    async def approve_cancel(self, ord_no: str, ord_item_seq: str) -> dict[str, Any]:
        """취소 요청 승인.

        SSG 클레임 API는 POST이지만 파라미터를 query string으로 전달해야 한다.
        """
        data = await self._call_api(
            "POST",
            "/api/claim/v2/cancel/request/approve",
            params={"ordNo": ord_no, "ordItemSeq": ord_item_seq},
        )
        # result 래핑 또는 최상위 모두 대응
        result_obj = data.get("result", data) if isinstance(data, dict) else {}
        if not isinstance(result_obj, dict):
            result_obj = {}
        result_code = result_obj.get("resultCode", "")
        # 91 = SSG 서버에서 취소처리는 완료되었으나 Server Exception 코드를 반환하는 케이스
        # 실제 취소가 완료된 경우이므로 성공으로 처리
        if result_code not in ("00", "91"):
            raise SSGApiError(
                f"취소 승인 실패: resultCode={result_code}, "
                f"msg={result_obj.get('resultMessage', '')}"
            )
        if result_code == "91":
            logger.warning(
                f"[취소승인] SSG resultCode=91(Server Exception) — 취소는 완료된 것으로 간주: ordNo={ord_no}"
            )
        return data

    def parse_order(
        self,
        raw: dict[str, Any],
        account_id: str,
        label: str,
        fee_rate: float,
    ) -> dict[str, Any]:
        """listShppDirection 응답 1건을 SambaOrder insert 형식으로 변환."""
        ord_item_div = str(raw.get("ordItemDiv", ""))
        shpp_prog = str(raw.get("shppProgStatDtlCd", ""))

        # 상태 매핑
        if ord_item_div == "021":
            status, shipping_status = "cancel_requested", "취소요청"
        elif ord_item_div == "031":
            status, shipping_status = "return_requested", "반품요청"
        elif ord_item_div in ("041", "042"):
            status, shipping_status = "return_requested", "교환요청"
        elif shpp_prog == "11":
            status, shipping_status = "pending", "상품준비중"
        elif shpp_prog in ("21", "22", "31", "41"):
            status, shipping_status = "pending", "상품준비중"
        elif shpp_prog == "43":
            status, shipping_status = "shipped", "국내배송중"
        elif shpp_prog == "51":
            status, shipping_status = "delivered", "배송완료"
        else:
            status, shipping_status = "pending", "상품준비중"

        rl_ord_amt = float(raw.get("rlordAmt", 0) or 0)
        # 수령인 우선, 없으면 주문자 fallback (str 정규화)
        customer_name = str(raw.get("rcptpeNm", "") or raw.get("ordpeNm", "") or "")
        # 수령인 연락처 우선 (휴대폰 → 집전화 → 주문자 휴대폰)
        customer_phone = str(
            raw.get("rcptpeHpno", "")
            or raw.get("rcptpeTelno", "")
            or raw.get("ordpeHpno", "")
        )
        # 도로명+상세주소 우선, 없으면 지번주소
        bsc = raw.get("shpplocBascAddr", "") or raw.get("ordpeRoadAddr", "")
        dtl = raw.get("shpplocDtlAddr", "")
        customer_address = str(
            (f"{bsc} {dtl}".strip() if bsc else "") or raw.get("shpplocAddr", "") or ""
        )

        # SSG CDN 이미지 URL 생성 (itemId 끝 6자리 역순 2글자씩)
        item_id_str = str(raw.get("itemId", "") or "")
        product_image = ""
        if len(item_id_str) >= 6:
            d1, d2, d3 = item_id_str[-2:], item_id_str[-4:-2], item_id_str[-6:-4]
            product_image = (
                f"https://sitem.ssgcdn.com/{d1}/{d2}/{d3}/item/{item_id_str}_i1_250.jpg"
            )

        item_nm = str(raw.get("itemNm", "") or "")
        raw_keys = list(raw.keys())
        ord_no = str(raw.get("ordNo", "") or "")
        ord_item_seq = str(raw.get("ordItemSeq", "") or "")
        shpp_no = str(raw.get("shppNo", "") or "")
        # orordNo: 원주문번호 (신세계몰 주문관리 페이지의 '원주문번호' 항목)
        or_ord_no = str(raw.get("orordNo", "") or "")

        # ordNo가 비어있으면 orordNo → shppNo|ordItemSeq 복합키 순으로 fallback
        if not ord_no:
            if or_ord_no:
                ord_no = or_ord_no
                logger.warning(
                    f"[SSG 주문] ordNo 누락으로 orordNo fallback 사용: "
                    f"order_number={ord_no}, raw_keys={raw_keys}"
                )
            else:
                ord_no = f"{shpp_no}|{ord_item_seq}"
                logger.warning(
                    f"[SSG 주문] ordNo/orordNo 모두 누락으로 복합키 fallback 사용: "
                    f"order_number={ord_no}, raw_keys={raw_keys}"
                )
        logger.info(
            f"[SSG 주문 파싱] order_number={ord_no}, shipment_id_parts=({shpp_no}|{ord_item_seq}|{or_ord_no}), "
            f"product_id={item_id_str}, status={status}, shppProgStatDtlCd={shpp_prog}"
        )

        # shipment_id에 shppNo|ordItemSeq|orordNo 형식으로 저장
        # - shppNo: 배송번호 (발주확인 시 필요)
        # - ordItemSeq: 주문상품순번 (취소승인 시 필요)
        # - orordNo: 원주문번호 (프론트 '상품주문번호' 란 표시용)
        shipment_id = f"{shpp_no}|{ord_item_seq}|{or_ord_no}"

        return {
            "order_number": ord_no,
            "shipment_id": shipment_id,
            "customer_note": str(raw.get("ordMemoCntt", "") or ""),
            "channel_id": account_id,
            "channel_name": label,
            "product_id": item_id_str,
            "product_name": item_nm,
            "product_image": product_image,
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "customer_address": customer_address,
            "quantity": raw.get("ordQty", 1) or 1,
            "sale_price": rl_ord_amt,
            "cost": 0,
            "fee_rate": fee_rate,
            "revenue": rl_ord_amt * (1 - fee_rate / 100),
            "source": "ssg",
            "status": status,
            "shipping_status": shipping_status,
        }

    async def confirm_rcov(
        self, shpp_no: str, shpp_seq: str, proc_item_qty: int = 1
    ) -> dict[str, Any]:
        """반품 회수확인 처리."""
        body = {
            "requestConfirmRcov": {
                "shppNo": shpp_no,
                "shppSeq": shpp_seq,
                "procItemQty": proc_item_qty,
                "shppTypeDtlCd": "22",
                "delicoVenId": "0000033012",
                "wblNo": "0000000000",
                "resellPsblYn": "N",
                "retImptMainCd": "10",
                "shppMainCd": "32",
            }
        }
        resp = await self._call_api("POST", "/api/pd/1/saveConfirmRcov.ssg", body=body)
        result_code = resp.get("resultCode") or resp.get("result_code", "")
        if result_code and result_code != "00":
            raise SSGApiError(
                f"반품 회수확인 실패 (resultCode={result_code}): {resp.get('resultMessage', '')}"
            )
        return resp

    async def complete_rcov(
        self, shpp_no: str, shpp_seq: str, proc_item_qty: int = 1
    ) -> dict[str, Any]:
        """반품 완료 처리."""
        body = {
            "requestConfirmRcov": {
                "shppNo": shpp_no,
                "shppSeq": shpp_seq,
                "procItemQty": proc_item_qty,
                "shppTypeDtlCd": "22",
                "resellPsblYn": "N",
                "retImptMainCd": "10",
                "shppMainCd": "32",
            }
        }
        resp = await self._call_api("POST", "/api/pd/1/saveCompleteRcov.ssg", body=body)
        result_code = resp.get("resultCode") or resp.get("result_code", "")
        if result_code and result_code != "00":
            raise SSGApiError(
                f"반품 완료 실패 (resultCode={result_code}): {resp.get('resultMessage', '')}"
            )
        return resp

    async def refuse_return(
        self,
        shpp_no: str,
        shpp_seq: str,
        memo: str = "",
        reason_cd: str = "11",
        proc_item_qty: int = 1,
    ) -> dict[str, Any]:
        """반품 거부 처리."""
        body = {
            "requestRefusualReturn": {
                "shppNo": shpp_no,
                "shppSeq": shpp_seq,
                "procItemQty": proc_item_qty,
                "retRefusRsnCd": reason_cd,
                "retProcMemoCntt": memo,
            }
        }
        resp = await self._call_api(
            "POST", "/api/pd/1/saveRefusualReturn.ssg", body=body
        )
        result_code = resp.get("resultCode") or resp.get("result_code", "")
        if result_code and result_code != "00":
            raise SSGApiError(
                f"반품 거부 실패 (resultCode={result_code}): {resp.get('resultMessage', '')}"
            )
        return resp

    async def close(self) -> None:
        """리소스 정리 — 매 호출마다 httpx 클라이언트를 생성/해제하므로 no-op."""
        pass

    # ------------------------------------------------------------------
    # CS 연동 — 쪽지 / Q&A
    # ------------------------------------------------------------------

    async def get_notes(
        self,
        start_date: str,
        end_date: str,
        page: int = 1,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """쪽지 목록 조회 (최대 6개월). start_date/end_date: YYYYMMDD 형식."""
        params = {
            "modDtsStart": start_date,
            "modDtsEnd": end_date,
            "page": str(page),
            "pageSize": str(page_size),
            "ntRcvYn": "N",  # 마지막 수신쪽지가 읽지않음(미답변)인 쪽지만 수집
        }
        res = await self._call_api("GET", "/api/cm/0.1/notes.ssg", params=params)
        logger.debug(f"[SSG] 쪽지 목록 응답: {res}")
        result = res.get("result", {})
        if not isinstance(result, dict) or result.get("resultCode") not in (
            "00",
            "SUCCESS",
        ):
            logger.warning(f"[SSG] 쪽지 목록 조회 실패 (전체응답): {res}")
            return []
        data = result.get("resultData", {})
        note_list = data.get("noteList", {})
        # noteList 자체가 list로 내려오는 경우 처리
        if isinstance(note_list, list):
            notes = note_list
        else:
            notes = note_list.get("note", [])
        if isinstance(notes, dict):
            notes = [notes]
        return notes

    async def get_note_detail_no_recv(self, bo_nt_id: str) -> dict[str, Any]:
        """쪽지 상세 조회 (수신처리 없음). 대화 스레드 확인용 — 읽음 처리 불필요할 때 사용."""
        try:
            res = await self._call_api(
                "POST",
                f"/api/cm/0.1/note/detail/{bo_nt_id}.ssg",
                body={},
            )
        except Exception as e:
            logger.debug(f"[SSG] 쪽지 상세(no-recv) 조회 실패 ({bo_nt_id}): {e}")
            return {}
        result = res.get("result", {})
        if result.get("resultCode") != "00":
            logger.debug(
                f"[SSG] 쪽지 상세(no-recv) 조회 실패 ({bo_nt_id}): {result.get('resultMessage')}"
            )
            return {}
        return result.get("resultData", {})

    async def get_note_detail(self, bo_nt_id: str) -> dict[str, Any]:
        """쪽지 상세 조회 + 수신 처리. 답장 전 호출 — 실패해도 답장은 계속 시도."""
        try:
            res = await self._call_api(
                "POST",
                f"/api/cm/0.1/note/{bo_nt_id}.ssg",
                body={},
            )
        except Exception as e:
            logger.warning(
                f"[SSG] 쪽지 수신처리 실패 ({bo_nt_id}): {e} — 답장은 계속 시도"
            )
            return {}
        result = res.get("result", {})
        if result.get("resultCode") != "00":
            logger.warning(
                f"[SSG] 쪽지 상세 조회 실패 ({bo_nt_id}): {result.get('resultMessage')}"
            )
            return {}
        return result.get("resultData", {})

    async def reply_note(self, bo_nt_id: str, content: str) -> bool:
        """쪽지 답장. 반드시 get_note_detail 호출 후 사용."""
        body = {"note": {"boNtId": bo_nt_id, "ntCntt": content}}
        res = await self._call_api("POST", "/api/cm/0.1/note/reply.ssg", body=body)
        result = res.get("result", {})
        if result.get("resultCode") != "00":
            logger.warning(
                f"[SSG] 쪽지 답장 실패 ({bo_nt_id}): {result.get('resultMessage')}"
            )
            return False
        return True

    @staticmethod
    def _xml_escape(s: str) -> str:
        """XML 특수문자 이스케이프."""
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )

    async def get_qna_list(
        self, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """미답변 상품 Q&A 목록 조회.

        start_date/end_date: YYYYMMDD 형식(8자). 내부적으로 YYYYMMDDHHMM(12자)으로 보정하여 전달.

        SSG 서버 SQL 분석:
          AND A.REG_DTS >= TO_DATE(? || '00', 'YYYYMMDDHH24MISS')  -- 시작: SS='00' 붙임
          AND A.REG_DTS <= TO_DATE(? || '59', 'YYYYMMDDHH24MISS')  -- 종료: SS='59' 붙임
        포맷 'YYYYMMDDHH24MISS'(14자) - SS(2자) = '?'는 12자 = YYYYMMDDHHMM 을 기대.
        따라서 시작은 '000000'시, 종료는 '235959'시로 맞춰서 하루 전체 범위 커버.
        """

        # 8자(YYYYMMDD) 입력을 12자(YYYYMMDDHHMM)로 보정
        def _to_12(d: str, is_end: bool) -> str:
            s = (d or "").replace("-", "").strip()
            if len(s) == 8:
                return s + ("2359" if is_end else "0000")
            if len(s) == 12:
                return s
            # 예외: 길이가 다르면 그대로 넘겨 서버 에러 메시지 받기
            return s

        start_param = _to_12(start_date, is_end=False)
        end_param = _to_12(end_date, is_end=True)

        # SSG Q&A API: XML body + YYYYMMDDHHMM(12자) 형식 (Oracle TO_DATE('YYYYMMDDHH24MISS') 기준)
        xml_body = (
            f"<ssg.eapi.dp.postng.dto.PostngReqDto>"
            f"<qnaStartDt>{self._xml_escape(start_param)}</qnaStartDt>"
            f"<qnaEndDt>{self._xml_escape(end_param)}</qnaEndDt>"
            f"</ssg.eapi.dp.postng.dto.PostngReqDto>"
        )
        res = await self._call_api_xml("POST", "/api/postng/qnaList.ssg", xml_body)
        logger.debug(f"[SSG] Q&A 목록 응답: {res}")
        result = res.get("result", {}) if isinstance(res, dict) else {}
        if not isinstance(result, dict) or result.get("resultCode") not in (
            "00",
            "SUCCESS",
        ):
            logger.warning(f"[SSG] Q&A 목록 조회 실패 (전체응답): {res}")
            return []
        data = result.get("resultData", {})
        qna_list = data.get("qnaList") or {}
        # 실제 응답 구조: {"qnaList": [{"qna": [{...}, {...}]}]}
        # qnaList가 list → 첫 번째 원소의 "qna" 키에서 실제 목록 추출
        if isinstance(qna_list, list):
            items: list = []
            for entry in qna_list:
                if isinstance(entry, dict):
                    qna_items = entry.get("qna") or []
                    if isinstance(qna_items, dict):
                        qna_items = [qna_items]
                    if isinstance(qna_items, list):
                        items.extend(qna_items)
        elif isinstance(qna_list, dict):
            items = qna_list.get("qna") or []
            if isinstance(items, dict):
                items = [items]
        else:
            items = []
        return [x for x in items if isinstance(x, dict)]

    async def reply_qna(self, postng_id: str, content: str) -> bool:
        """상품 Q&A 답변. 미답변 Q&A에만 가능."""
        xml_body = (
            f"<ssg.eapi.dp.postng.dto.PostngReqDto>"
            f"<postngId>{self._xml_escape(postng_id)}</postngId>"
            f"<postngCntt>{self._xml_escape(content)}</postngCntt>"
            f"</ssg.eapi.dp.postng.dto.PostngReqDto>"
        )
        try:
            res = await self._call_api_xml("POST", "/api/postng/ansQna.ssg", xml_body)
        except SSGApiError as e:
            logger.warning(f"[SSG] Q&A 답변 실패 ({postng_id}): {e}")
            return False
        result = res.get("result", {}) if isinstance(res, dict) else {}
        if result.get("resultCode") != "00":
            logger.warning(
                f"[SSG] Q&A 답변 실패 ({postng_id}): {result.get('resultMessage')}"
            )
            return False
        return True


class SSGApiError(Exception):
    """SSG API 에러."""

    pass

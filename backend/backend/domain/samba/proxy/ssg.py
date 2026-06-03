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
            import json as _json

            logger.info(
                f"[SSG REQ] POST {path} body={_json.dumps(body or {}, ensure_ascii=False)[:500]}"
            )
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

        SSG Open API v0.5: POST /item/0.5/insertItem.ssg (XStream XML)
        """
        xml_body = '<?xml version="1.0" encoding="UTF-8"?>' + self._to_xml(
            product_data, "insertItem"
        )
        logger.debug(
            f"[SSG] insertItem XML (총 {len(xml_body.encode())}bytes):\n{xml_body[:2000]}"
        )
        result = await self._call_api_xml("POST", "/item/0.5/insertItem.ssg", xml_body)
        return {"success": True, "data": result}

    async def update_product(self, product_data: dict[str, Any]) -> dict[str, Any]:
        """상품 전체 수정.

        SSG Open API v0.4: POST /item/0.4/updateItem.ssg (XStream XML)
        v0.5 updateItem.ssg는 SSG 미지원 (resultCode=99 "Latest Ver. 0.4").

        tempUitemId는 insertItem 전용 임시 ID로, updateItem에서는 SSG가 무시한다.
        sales-status API로 실제 uitemId를 조회한 뒤 교체해야 옵션 가격이 반영됨.

        가격 인상 시 SSG 검증 순서: 새 대표가 vs 기존 옵션최저가
        → 1단계로 대표가만 기존 옵션가로 맞춘 뒤, 2단계에서 전체 업데이트.
        """
        import asyncio as _asyncio
        import copy as _copy
        import re as _re

        item_id = product_data.pop("itemId", "") or ""
        product_data["itemId"] = item_id

        # updateItem에서는 tempUitemId 대신 실제 uitemId를 사용해야 SSG가 옵션 가격을 인식.
        # sales-status API로 현재 uitemId 목록 조회 → 등록 순서(tempUitemId 1,2,3...)와 매핑.
        uitem_id_map: dict[str, str] = {}
        try:
            status_resp = await self.get_item_sales_status(item_id)
            option_invs = (
                status_resp.get("result", {})
                .get("salesStatus", {})
                .get("optionInventories", [])
            )
            if isinstance(option_invs, dict):
                option_invs = [option_invs]
            for idx, inv in enumerate(option_invs, start=1):
                uid = str(inv.get("uitemId", "")).strip()
                if uid:
                    uitem_id_map[str(idx)] = uid
            if uitem_id_map:
                logger.info(
                    f"[SSG] uitemId 매핑 완료 ({len(uitem_id_map)}개): {uitem_id_map}"
                )
        except Exception as _e:
            logger.warning(
                f"[SSG] uitemId 조회 실패 — tempUitemId로 전송 (옵션가 미반영 위험): {_e}"
            )

        def _replace_temp_ids_in_prices(data: dict) -> dict:
            """tempUitemId를 실제 uitemId로 교체 (uitemPluralPrcs + uitems 모두).

            uitemPluralPrcs 구조: {"uitemPrc": [...]}  (_wrap_list_always_array(..., "uitemPrc"))
            uitems 구조: {"uitem": [...]}               (_wrap_list_always_array(..., "uitem"))

            uitems 업데이트 시 uitemOptnNm1 등 옵션명 필드를 제거해야 SSG '중복 에러' 방지.
            uitemAttr는 등록 시 전용이므로 수정 모드에서는 제거.
            """
            if not uitem_id_map:
                return data
            data = _copy.deepcopy(data)

            # uitemPluralPrcs: tempUitemId → uitemId
            prcs_wrap = data.get("uitemPluralPrcs")
            if isinstance(prcs_wrap, dict):
                items = prcs_wrap.get("uitemPrc") or prcs_wrap.get("item") or []
                if isinstance(items, dict):
                    items = [items]
                for entry in items:
                    t = str(entry.get("tempUitemId", "")).strip()
                    if t in uitem_id_map:
                        entry.pop("tempUitemId", None)
                        entry["uitemId"] = uitem_id_map[t]

            # uitems: tempUitemId → uitemId, 옵션명 관련 필드 제거 (재등록 방지)
            uitems_wrap = data.get("uitems")
            if isinstance(uitems_wrap, dict):
                items = uitems_wrap.get("uitem") or uitems_wrap.get("item") or []
                if isinstance(items, dict):
                    items = [items]
                for entry in items:
                    t = str(entry.get("tempUitemId", "")).strip()
                    if t in uitem_id_map:
                        entry.pop("tempUitemId", None)
                        entry["uitemId"] = uitem_id_map[t]
                        entry.pop("uitemOptnTypeNm1", None)
                        entry.pop("uitemOptnNm1", None)

            # uitemAttr는 옵션 신규 등록 전용 — 수정 모드에서는 제거
            data.pop("uitemAttr", None)

            return data

        product_data = _replace_temp_ids_in_prices(product_data)

        xml_body = '<?xml version="1.0" encoding="UTF-8"?>' + self._to_xml(
            product_data, "updateItem"
        )
        logger.debug(f"[SSG] updateItem XML (itemId={item_id}):\n{xml_body[:2000]}")
        result = await self._call_api_xml("POST", "/item/0.4/updateItem.ssg", xml_body)

        # 가격 인상 감지: SSG가 '새 대표가 vs 기존 옵션최저가'를 먼저 검증하므로
        # 대표가 인상 시 오류 → 1단계로 대표가만 기존 옵션가로 낮춰 검증 통과 후 옵션가 올리기
        # → 2단계에서 새 대표가 + 새 옵션가로 전체 업데이트
        desc = result.get("result", {}).get("resultDesc", "") or ""
        m = _re.search(r"옵션최저가격\s*([\d,]+)원", desc)
        if not m:
            m = _re.search(r"옵션판매가[는은]\s*([\d,]+)원", desc)
        if m and product_data.get("salesPrcInfos"):
            cur_min = int(m.group(1).replace(",", ""))
            logger.info(f"[SSG] 가격 인상 감지 → 1단계: 대표가={cur_min}원으로 맞추기")

            def _patch_sell(prc_wrap, new_sell):
                w = _copy.deepcopy(prc_wrap)
                items = w.get("uitemPrc") or w.get("list", [])
                if isinstance(items, dict):
                    items = [items]
                for p in items:
                    p["sellprc"] = new_sell
                return w

            # 1단계: 대표가만 기존 옵션가로 맞추기 (uitems 제외 — 가격 조정만 목적)
            step1_data = {
                "itemId": item_id,
                "salesPrcInfos": _patch_sell(product_data["salesPrcInfos"], cur_min),
            }
            if product_data.get("uitemPluralPrcs"):
                step1_data["uitemPluralPrcs"] = product_data["uitemPluralPrcs"]
            xml_step1 = '<?xml version="1.0" encoding="UTF-8"?>' + self._to_xml(
                step1_data, "updateItem"
            )
            r1 = await self._call_api_xml("POST", "/item/0.4/updateItem.ssg", xml_step1)
            r1_code = r1.get("result", {}).get("resultCode")
            logger.info(
                f"[SSG] updateItem 1단계(대표가={cur_min}) resultCode={r1_code}"
            )
            await _asyncio.sleep(1.5)
            # 2단계: 원래 데이터(새 대표가)로 재시도 — 이제 옵션가도 올라갔으므로 통과
            result = await self._call_api_xml(
                "POST", "/item/0.4/updateItem.ssg", xml_body
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
                    "uitemId": opt.get("uitemId"),
                    "sellStatCd": str(opt.get("sellStatCd", "20")),
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

    async def get_item_detail(self, item_id: str) -> dict[str, Any]:
        """상품 상세 조회 — getItemList(요약) 와 달리 전체 필드 반환.

        SSG Open API: GET /item/0.1/online/{itemId} (프로덕션 실측 확인).
        응답: result.{itemBase, sites, mainDisplayCategories[...], ...}
          mainDisplayCategories: [{siteNo, dispCtgId, ...}] — 6005(SSG.COM)/6004 전시카테고리.
        """
        return await self._call_api("GET", f"/item/0.1/online/{item_id}")

    def extract_main_disp_ctg(self, detail: dict[str, Any], site_no: str) -> str:
        """상세조회 응답에서 특정 siteNo 의 메인 전시카테고리 dispCtgId 추출.

        b21b361d(SSG.COM opt-in) 이전 등록분은 6005 전시카테고리를 이미 보유 →
        수정 시 그대로 보존하지 않으면 SSG 가 "SSG.COM몰 메인매장 카테고리 필수" 거부.
        반환: dispCtgId 문자열 (없으면 빈 문자열).
        """
        if not isinstance(detail, dict):
            return ""
        result_obj = detail.get("result", detail)
        if not isinstance(result_obj, dict):
            return ""
        # mainDisplayCategories 는 result.itemBase 하위에 중첩됨(프로덕션 실측).
        # 방어적으로 result 직하도 함께 확인.
        item_base = result_obj.get("itemBase")
        mdc = None
        if isinstance(item_base, dict):
            mdc = item_base.get("mainDisplayCategories")
        if mdc is None:
            mdc = result_obj.get("mainDisplayCategories")
        if isinstance(mdc, dict):
            mdc = [mdc]
        if not isinstance(mdc, list):
            return ""
        for ent in mdc:
            if not isinstance(ent, dict):
                continue
            if str(ent.get("siteNo") or "") == str(site_no):
                cid = ent.get("dispCtgId")
                if cid:
                    return str(cid)
        return ""

    async def get_item_approval_status(
        self, item_id: str, div_cd: str = "00"
    ) -> list[dict[str, Any]]:
        """신상품 MD 승인 상태 조회.

        SSG Open API: GET /item/0.1/getItemChngDemandList.ssg
        chngDemndProcStatCd: 10=MD승인요청(대기), 20=승인완료, 30=MD반려
        itemrChngDemndDivCd: 00=신상품등록, 01=상품수정
        """
        params: dict[str, Any] = {"itemId": item_id}
        if div_cd:
            params["itemrChngDemndDivCd"] = div_cd
        resp = await self._call_api(
            "GET", "/item/0.1/getItemChngDemandList.ssg", params=params
        )
        result_obj = resp.get("responseItemChngDemndList", resp.get("result", {}))
        raw = result_obj.get("itemChngDemndList", {})
        if isinstance(raw, dict):
            item_val = raw.get("itemChngDemnd", [])
            if isinstance(item_val, dict):
                return [item_val]
            if isinstance(item_val, list):
                return item_val
            return []
        if isinstance(raw, list):
            result = []
            for item in raw:
                if isinstance(item, dict):
                    demnd = item.get("itemChngDemnd")
                    if isinstance(demnd, dict):
                        result.append(demnd)
                    elif isinstance(demnd, list):
                        result.extend(demnd)
            return result
        return []

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

    async def find_live_item_id_by_spl_ven(self, spl_ven_item_id: str) -> str:
        """splVenItemId(공급업체상품ID=수집상품 id)로 기존 live 등록 itemId 검색 (#321).

        insertItem 은 비멱등(호출마다 새 itemId) → itemNm 포맷이 바뀌면 동일 상품이
        2개 itemId 로 중복등록됨. 안정키 splVenItemId 로 미리 찾아 update 전환.

        - getItemList splVenItemId 검색 지원 (공식문서 + 프로덕션 실측 확인).
        - 방어: SSG 가 미지원 param 을 무시하고 전체를 반환하는 경우 대비
          splVenItemId 정확일치만 채택.
        - sellStatCd=90(삭제) 은 제외, 살아있는 등록만 채택.
        반환: 매칭 itemId (없으면 빈 문자열).
        """
        if not spl_ven_item_id:
            return ""
        resp = await self._call_api(
            "GET",
            "/item/0.1/getItemList.ssg",
            params={
                "page": "1",
                "pageSize": "20",
                "splVenItemId": str(spl_ven_item_id),
            },
        )
        result_obj = resp.get("result", resp) if isinstance(resp, dict) else {}
        items_raw = result_obj.get("items") if isinstance(result_obj, dict) else None
        if isinstance(items_raw, dict):
            iv = items_raw.get("item")
        else:
            iv = items_raw
        if isinstance(iv, dict):
            items = [iv]
        elif isinstance(iv, list):
            items = [x for x in iv if isinstance(x, dict)]
        else:
            items = []
        for it in items:
            # 정확일치 방어 (param 무시 시 오adopt 차단)
            if str(it.get("splVenItemId") or "") != str(spl_ven_item_id):
                continue
            if str(it.get("sellStatCd") or "") == "90":  # 삭제 제외
                continue
            iid = it.get("itemId")
            if iid:
                return str(iid)
        return ""

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
        if len(_detail_bytes) > 50000:
            detail_html = _detail_bytes[:50000].decode("utf-8", errors="ignore")
            logger.warning(
                "[SSG] detail_html %d bytes → 50,000 bytes로 절단 (상품: %s)",
                len(_detail_bytes),
                (product.get("name") or "")[:50],
            )
        else:
            detail_html = _raw_detail_html
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
        manufacturer = (
            _raw_manufacturer[:100] if _raw_manufacturer else (brand or "상세설명참조")
        )
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
            matched_brand_id, matched_brand_name = _match_from_mappings(
                manufacturer, _mappings
            )
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
        # notice_utils가 동일한 수입여부/제조국 코드를 쓰도록 product에 주입
        product = {
            **product,
            "_ssg_import_yn": "Y" if is_imported else "N",
            "_ssg_origin_code": resolved_origin or "",
        }

        # ── 상품관리속성 (카테고리별 동적 생성) — 원산지/_ssg_import_yn 주입 후 호출 ──
        from backend.domain.samba.proxy.notice_utils import build_ssg_notice

        item_mng_prop_cls_id, item_mng_attrs_list = build_ssg_notice(product)

        data: dict[str, Any] = {
            "itemNm": name,
            "brandId": matched_brand_id,
            "stdCtgId": effective_std_cat,
            "mdlNm": style_no or None,
            # 공급업체상품ID = 수집상품 id (안정 멱등키, #321). insertItem 비멱등 대응 —
            # itemNm 포맷이 바뀌어도 splVenItemId 로 기존 등록을 찾아 중복등록 차단.
            # getItemList splVenItemId 검색 지원(공식문서+프로덕션 실측 확인).
            **({"splVenItemId": str(product.get("id"))} if product.get("id") else {}),
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
                [
                    e
                    for e in [
                        {"siteNo": self.site_no, "dispCtgId": category_id}
                        if category_id
                        else None,
                        {"siteNo": "6005", "dispCtgId": main_category_id}
                        if main_category_id
                        else None,
                    ]
                    if e
                ],
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
                        **(
                            {"jejuAddShppcstId": add_shppcst_jeju}
                            if add_shppcst_jeju
                            else {}
                        ),
                        **(
                            {"ismtarAddShppcstId": add_shppcst_island}
                            if add_shppcst_island
                            else {}
                        ),
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
            logger.warning(
                "[SSG] stdCtgId(표준카테고리 ID)가 없습니다. API 등록 실패할 수 있습니다."
            )

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

    async def get_warehouse_out_orders(self, days: int = 7) -> list[dict[str, Any]]:
        """출고처리 목록 조회 — 주문확인처리(피킹완료) 된 주문 (최대 180일).

        listShppDirection은 배송지시(11) 상태만 반환하므로,
        이미 발주확인된 출고대기 주문은 이 API로 별도 조회.
        """
        KST = timezone(timedelta(hours=9))
        now = datetime.now(KST)
        days = min(days, 180)
        start_dt = (now - timedelta(days=days)).strftime("%Y%m%d")
        end_dt = now.strftime("%Y%m%d")
        body = {
            "requestWarehouseOut": {
                "perdType": "03",  # 주문완료일 기준
                "perdStrDts": start_dt,
                "perdEndDts": end_dt,
            }
        }
        try:
            data = await self._call_api(
                "POST", "/api/pd/1/listWarehouseOut.ssg", body=body
            )
        except Exception as e:
            logger.warning(f"[SSG 출고대기] listWarehouseOut 조회 실패: {e}")
            return []

        result = data.get("result", {})
        if not isinstance(result, dict):
            return []

        warehouse_outs = result.get("warehouseOuts", [])
        if isinstance(warehouse_outs, dict):
            warehouse_outs = [warehouse_outs]
        elif not isinstance(warehouse_outs, list):
            return []

        orders: list[dict[str, Any]] = []
        for wo in warehouse_outs:
            if not isinstance(wo, dict):
                continue
            inner = wo.get("warehouseOut")
            if isinstance(inner, list):
                orders.extend(i for i in inner if isinstance(i, dict))
            elif isinstance(inner, dict):
                orders.append(inner)

        logger.info(f"[SSG 출고대기] listWarehouseOut {len(orders)}건 조회")
        return orders

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
                f"rlordAmt={first.get('rlordAmt')}, "
                f"shpplocZpCd={first.get('shpplocZpCd')}, "
                f"rcptpeZpCd={first.get('rcptpeZpCd')}, "
                f"shpplocZipCd={first.get('shpplocZipCd')}, "
                f"shpplocZpNo={first.get('shpplocZpNo')}"
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
        # 미등록 주문에 부정확한 사진이 매칭되던 문제로 product_image 자동 합성 제거.
        # source_url(소싱처 원문)에는 SSG 판매페이지를 넣지 않는다. itemView.ssg?itemId=
        # 는 신세계몰 '판매' 리스팅이지 소싱처 원문이 아니므로, 화면 소싱처 배지/원문링크/
        # 원주문링크가 실제 소싱처(예: ABCmart)를 SSG로 오인하게 만든다. 판매링크는 프론트
        # handleMarketLink가 product_id로 따로 생성하므로 source_url 불필요.
        source_url = ""
        return {
            "order_number": ord_no,
            # 형식: "|ordItemSeq" (shppNo 없음, 취소신청에는 배송번호 불필요; orordNo는 order_number에 존재)
            "shipment_id": f"|{ord_item_seq}",
            "channel_id": account_id,
            "channel_name": label,
            "product_id": item_id_str,
            "product_name": str(raw.get("itemNm", "") or ""),
            "product_option": str(raw.get("uitemNm", "") or ""),
            "product_image": "",
            "source_url": source_url,
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

    async def get_return_requests(self, days: int = 7) -> list[dict[str, Any]]:
        """반품/교환 회수 대상 조회 — 최근 days일 이내 (최대 7일).

        API: POST /api/pd/1/listExchangeTarget.ssg
        shppDivDtlCds=21(반품), 22(교환) 모두 조회.
        """
        KST = timezone(timedelta(hours=9))
        now = datetime.now(KST)
        days = min(days, 7)
        start_dt = (now - timedelta(days=days)).strftime("%Y%m%d")
        end_dt = now.strftime("%Y%m%d")
        body = {
            "requestExchangeTarget": {
                "perdType": "01",
                "perdStrDts": start_dt,
                "perdEndDts": end_dt,
                "shppDivDtlCds": "21,22",
            }
        }
        data = await self._call_api(
            "POST", "/api/pd/1/listExchangeTarget.ssg", body=body
        )
        logger.info(f"[SSG 반품조회] 응답 최상위 키: {list(data.keys())[:10]}")
        # 응답 구조가 다른 API와 다를 수 있음 — result 래퍼 있는 경우와 없는 경우 모두 처리
        result = data.get("result") or data
        if not isinstance(result, dict):
            return []
        raw_targets = result.get("exchangeTargets") or data.get("exchangeTargets") or []
        # XStream 래핑: exchangeTargets가 dict이면 exchangeTarget 꺼내기
        if isinstance(raw_targets, dict):
            raw_targets = raw_targets.get("exchangeTarget", [])
        if isinstance(raw_targets, dict):
            raw_targets = [raw_targets]
        elif not isinstance(raw_targets, list):
            logger.warning(
                f"[SSG 반품조회] exchangeTargets 타입 이상: {type(raw_targets)} — {str(raw_targets)[:200]}"
            )
            return []
        unwrapped = []
        for t in raw_targets:
            if not isinstance(t, dict):
                continue
            inner = t.get("exchangeTarget")
            if inner is None:
                unwrapped.append(t)
            elif isinstance(inner, list):
                unwrapped.extend([i for i in inner if isinstance(i, dict)])
            elif isinstance(inner, dict):
                unwrapped.append(inner)
        logger.info(f"[SSG 반품조회] {len(unwrapped)}건 조회")
        return unwrapped

    async def get_order_detail(self, or_ord_no: str) -> list[dict[str, Any]]:
        """원주문번호로 주문 상세 조회 — ordItemDiv(021=취소) 등 현재 상태 확인용.

        API: GET /api/claim/v2/order/{orordNo}
        """
        data = await self._call_api("GET", f"/api/claim/v2/order/{or_ord_no}")
        result = data.get("result", {})
        if not isinstance(result, dict):
            return []
        result_data = result.get("resultData", [])
        if isinstance(result_data, dict):
            result_data = [result_data]
        elif not isinstance(result_data, list):
            return []
        return result_data

    _COURIER_CODE_MAP: dict[str, str] = {
        "CJ대한통운": "0000033011",
        "대한통운": "0000033032",
        "한진택배": "0000033071",
        "한진": "0000033071",
        "롯데택배": "0000033073",
        "롯데글로벌": "0010326677",
        "롯데글로벌로지스": "0010326677",
        "우체국택배": "0000033052",
        "우체국": "0000033052",
        "우체국EMS": "0000033050",
        "로젠택배": "0000033036",
        "로젠": "0000033036",
        "경동택배": "0000033027",
        "GS편의점택배": "0000033013",
        "GSPostbox택배": "0000033013",
        "CU편의점택배": "0008369131",
        "합동택배": "0000038977",
        "한국택배": "0000033069",
        "일양로지스": "0000033057",
        "천일택배": "0000033062",
        "대신택배": "0000033030",
        "KT로지스": "0000033021",
        "동원로엑스": "0020089384",
        "쿠팡로지스틱스": "0024803687",
        "딜리박스": "0024850579",
    }

    def get_courier_code(self, courier_name: str) -> str:
        """한글 택배사명 → SSG delicoVenId 변환. 매핑 없으면 빈 문자열 반환."""
        return self._COURIER_CODE_MAP.get(courier_name.strip(), "")

    async def send_invoice(
        self,
        shpp_no: str,
        shpp_seq: str,
        wbl_no: str,
        delico_ven_id: str,
        shpp_type_cd: str = "20",
        shpp_type_dtl_cd: str = "22",
    ) -> dict[str, Any]:
        """운송장 등록 — /api/pd/1/saveWblNo.ssg."""
        body = {
            "requestWhOutCompleteProcess": {
                "shppNo": shpp_no,
                "shppSeq": shpp_seq,
                "wblNo": wbl_no,
                "delicoVenId": delico_ven_id,
                "shppTypeCd": shpp_type_cd,
                "shppTypeDtlCd": shpp_type_dtl_cd,
            }
        }
        data = await self._call_api("POST", "/api/pd/1/saveWblNo.ssg", body=body)
        result = data.get("result", {})
        result_code = (
            (result.get("resultCode") or "") if isinstance(result, dict) else ""
        )
        if result_code != "00":
            desc = (
                (result.get("resultDesc") or result.get("resultMessage") or str(data))
                if isinstance(result, dict)
                else str(data)
            )
            raise RuntimeError(f"SSG 운송장 등록 실패 ({result_code}): {desc}")
        return data

    async def process_outbound(
        self,
        shpp_no: str,
        shpp_seq: str,
        qty: int = 1,
    ) -> dict[str, Any]:
        """출고처리 — /api/pd/1/saveWhOutCompleteProcess.ssg.

        운송장 등록(saveWblNo) 후 반드시 호출해야 배송중으로 상태 변경됨.
        """
        body = {
            "requestWhOutCompleteProcess": {
                "shppNo": shpp_no,
                "shppSeq": shpp_seq,
                "procItemQty": qty,
            }
        }
        data = await self._call_api(
            "POST", "/api/pd/1/saveWhOutCompleteProcess.ssg", body=body
        )
        result = data.get("result", {})
        result_code = (
            (result.get("resultCode") or "") if isinstance(result, dict) else ""
        )
        if result_code != "00":
            desc = (
                (result.get("resultDesc") or result.get("resultMessage") or str(data))
                if isinstance(result, dict)
                else str(data)
            )
            raise RuntimeError(f"SSG 출고처리 실패 ({result_code}): {desc}")
        return data

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

    @staticmethod
    def _parse_ssg_dts(raw_val: Any) -> Optional[datetime]:
        """SSG 일시 문자열을 UTC datetime으로 변환.

        지원 포맷: YYYYMMDDHH24MISS(14), YYYYMMDDHHMM(12), YYYY-MM-DD HH:MM:SS,
        YYYYMMDD(8). SSG 응답은 KST 기준이므로 UTC로 변환해 반환.
        """
        s = str(raw_val or "").strip()
        if not s:
            return None
        KST = timezone(timedelta(hours=9))
        # 숫자만 추출 — 구분자 제거(예: "2026-05-18 15:21:40" → "20260518152140")
        digits = "".join(ch for ch in s if ch.isdigit())
        try:
            if len(digits) >= 14:
                dt = datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
            elif len(digits) >= 12:
                dt = datetime.strptime(digits[:12], "%Y%m%d%H%M")
            elif len(digits) >= 8:
                dt = datetime.strptime(digits[:8], "%Y%m%d")
            else:
                return None
        except ValueError:
            return None
        return dt.replace(tzinfo=KST).astimezone(timezone.utc)

    def parse_order(
        self,
        raw: dict[str, Any],
        account_id: str,
        label: str,
        fee_rate: float,
    ) -> dict[str, Any]:
        """listShppDirection 응답 1건을 SambaOrder insert 형식으로 변환."""
        ord_item_div = str(raw.get("ordItemDiv", ""))
        # listWarehouseOut은 lastShppProgStatDtlCd, listShppDirection은 shppProgStatDtlCd
        shpp_prog = str(
            raw.get("lastShppProgStatDtlCd", "") or raw.get("shppProgStatDtlCd", "")
        )

        # 상태 매핑
        if ord_item_div == "021":
            status, shipping_status = "cancel_requested", "취소요청"
        elif ord_item_div == "031":
            status, shipping_status = "return_requested", "반품요청"
        elif ord_item_div in ("041", "042"):
            status, shipping_status = "return_requested", "교환요청"
        elif shpp_prog == "11":
            status, shipping_status = "pending", "상품준비중"
        elif shpp_prog in ("21", "22", "31"):
            status, shipping_status = "pending", "주문접수"
        elif shpp_prog == "41":
            status, shipping_status = "pending", "주문접수"
        elif shpp_prog == "42":
            status, shipping_status = "pending", "출고보류"
        elif shpp_prog == "43":
            status, shipping_status = "shipped", "국내배송중"
        elif shpp_prog == "51":
            status, shipping_status = "delivered", "배송완료"
        else:
            status, shipping_status = "pending", "상품준비중"

        rl_ord_amt = float(raw.get("rlordAmt", 0) or 0)
        dc_amt = float(raw.get("dcAmt", 0) or 0)
        sell_price = float(raw.get("sellprc", 0) or 0) or (rl_ord_amt + dc_amt)
        spl_prc = float(
            raw.get("splprc", 0) or raw.get("splPrc", 0) or 0
        )  # listWarehouseOut은 splPrc
        # 수령인 우선, 없으면 주문자 fallback (str 정규화)
        customer_name = str(raw.get("rcptpeNm", "") or raw.get("ordpeNm", "") or "")
        # 주문자명 — SSG ordpeNm (수령인 rcptpeNm과 다를 수 있음: 선물하기 등)
        orderer_name = str(raw.get("ordpeNm", "") or raw.get("rcptpeNm", "") or "")
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
        # 우편번호 — SSG 필드명 후보 fallback chain
        _zip = str(
            raw.get("shpplocZpCd", "")
            or raw.get("rcptpeZpCd", "")
            or raw.get("shpplocZipCd", "")
            or raw.get("shpplocZpNo", "")
            or ""
        ).strip()
        if not _zip:
            logger.info(
                f"[SSG 주문] 우편번호 필드 미발견 — ordNo={raw.get('ordNo')}, "
                f"zip후보키={[k for k in raw if 'zp' in k.lower() or 'zip' in k.lower()]}"
            )

        item_id_str = str(raw.get("itemId", "") or "")
        # 미등록 주문에 부정확한 사진이 매칭되던 문제로 product_image 자동 합성 제거.
        # source_url(소싱처 원문)에는 SSG 판매페이지를 넣지 않는다. itemView.ssg?itemId=
        # 는 신세계몰 '판매' 리스팅이지 소싱처 원문이 아니므로, 화면 소싱처 배지/원문링크/
        # 원주문링크가 실제 소싱처(예: ABCmart)를 SSG로 오인하게 만든다. 판매링크는 프론트
        # handleMarketLink가 product_id로 따로 생성하므로 source_url 불필요.
        source_url = ""

        item_nm = str(raw.get("itemNm", "") or "")
        raw_keys = list(raw.keys())
        ord_no = str(raw.get("ordNo", "") or "")
        ord_item_seq = str(raw.get("ordItemSeq", "") or "")
        shpp_no = str(raw.get("shppNo", "") or "")
        shpp_seq = str(
            raw.get("shppSeq", "") or ord_item_seq
        )  # 배송순번 (운송장등록/발주확인에 사용)
        # orordNo: 원주문번호 (신세계몰 주문관리 페이지의 '원주문번호' 항목)
        or_ord_no = str(raw.get("orordNo", "") or "")

        # ordNo가 비어있으면 orordNo → shppNo|shppSeq 복합키 순으로 fallback
        if not ord_no:
            if or_ord_no:
                ord_no = or_ord_no
                logger.warning(
                    f"[SSG 주문] ordNo 누락으로 orordNo fallback 사용: "
                    f"order_number={ord_no}, raw_keys={raw_keys}"
                )
            else:
                ord_no = f"{shpp_no}|{shpp_seq}"
                logger.warning(
                    f"[SSG 주문] ordNo/orordNo 모두 누락으로 복합키 fallback 사용: "
                    f"order_number={ord_no}, raw_keys={raw_keys}"
                )
        logger.info(
            f"[SSG 주문 파싱] order_number={ord_no}, shppNo={shpp_no}, shppSeq={shpp_seq}, ordItemSeq={ord_item_seq}, "
            f"product_id={item_id_str}, status={status}, shppProgStatDtlCd={shpp_prog}"
        )

        # shipment_id에 shppNo|shppSeq 형식으로 저장
        # - shppNo: 배송번호
        # - shppSeq: 배송순번 (발주확인·운송장등록 시 필요, ordItemSeq와 다를 수 있음)
        # ordItemSeq는 ord_prd_seq에 별도 저장 (취소승인 시 필요)
        shipment_id = f"{shpp_no}|{shpp_seq}"

        # 결제일(paid_at) — 주문 목록 화면이 paid_at IS NOT NULL 로 필터링하므로
        # 누락되면 SSG 주문이 목록에서 통째로 사라짐. 결제완료>주문일시 우선순위.
        paid_at = (
            self._parse_ssg_dts(raw.get("pymtCmplDts"))
            or self._parse_ssg_dts(raw.get("pymtDts"))
            or self._parse_ssg_dts(raw.get("ordDts"))
            or self._parse_ssg_dts(raw.get("ordDt"))
            or self._parse_ssg_dts(raw.get("ordCmplDts"))
            or self._parse_ssg_dts(raw.get("ordRcpDts"))
        )
        if paid_at is None:
            logger.warning(
                f"[SSG 주문 파싱] paid_at 추출 실패 — ordNo={ord_no}, "
                f"date_keys={[k for k in raw.keys() if 'Dt' in str(k) or 'dt' in str(k)]}"
            )

        # 고객메모 앞 [태그] 제거 (예: "[고객배송메모]부재 시..." → "부재 시...")
        _raw_note = str(raw.get("ordMemoCntt", "") or "")
        customer_note = re.sub(r"^\[.*?\]", "", _raw_note).strip()

        return {
            "order_number": ord_no,
            "shipment_id": shipment_id,
            "customer_note": customer_note,
            "customer_postal_code": _zip or None,
            "channel_id": account_id,
            "channel_name": label,
            "product_id": item_id_str,
            "product_name": item_nm,
            "product_option": str(raw.get("uitemNm", "") or ""),
            "product_image": "",
            "source_url": source_url,
            "customer_name": customer_name,
            "orderer_name": orderer_name,
            "customer_phone": customer_phone,
            "customer_address": customer_address,
            "quantity": raw.get("ordQty", 1) or 1,
            "sale_price": sell_price,
            "cost": 0,
            "fee_rate": fee_rate,
            "revenue": round(spl_prc * 1.1)
            if spl_prc > 0
            else round(sell_price / 1.1 * (1 - fee_rate / 100)),
            "source": "ssg",
            "status": status,
            "shipping_status": shipping_status,
            "paid_at": paid_at,
            "ord_prd_seq": ord_item_seq,  # 취소승인 API(approve_cancel)에서 ordItemSeq로 사용
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
        data = result.get("resultData") or {}
        if not isinstance(data, dict):
            return []
        note_list = data.get("noteList", {})
        if isinstance(note_list, list):
            notes = note_list
        elif isinstance(note_list, dict):
            notes = note_list.get("note", [])
        else:
            notes = []
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
        # qnaList 위치: result.qnaList (직접) 또는 result.resultData.qnaList (레거시)
        data = result.get("resultData") or {}
        qna_list = (
            result.get("qnaList")
            or (data.get("qnaList") if isinstance(data, dict) else None)
            or {}
        )
        # 실제 응답 구조: {"qnaList": [{"qna": {...}}]} — qna가 dict(단건) 또는 list
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

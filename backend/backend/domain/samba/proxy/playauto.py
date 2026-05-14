"""플레이오토 EMP API 클라이언트."""

from typing import Any

import httpx

from backend.utils.logger import logger

# EMP API 기본 URL
EMP_BASE_URL = "https://playauto-api.playauto.co.kr/emp/v1"
# 공통 API 기본 URL
COMMON_BASE_URL = "https://playapi.api.plto.com/restApi/empapi"


class PlayAutoApiError(Exception):
    """플레이오토 API 에러."""

    def __init__(self, message: str, status: int = 0, data: Any = None):
        self.message = message
        self.status = status
        self.data = data
        super().__init__(message)


class PlayAutoClient:
    """플레이오토 EMP API 클라이언트.

    인증: X-API-KEY 헤더
    상품 등록/수정/품절, 주문 조회, 송장 입력, 문의 조회/답변
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None

    @staticmethod
    def _get_proxy_url() -> str:
        # 우선순위: PLAYAUTO_PROXY_URL env → DB transmit → DB collect
        # GCP/클라우드 VM에서 PlayAuto 호스트 차단 회피용 — env 우선
        import os as _os

        env_proxy = _os.environ.get("PLAYAUTO_PROXY_URL", "").strip()
        if env_proxy:
            if "://" not in env_proxy:
                env_proxy = f"http://{env_proxy}"
            return env_proxy
        try:
            from backend.domain.samba.collector.refresher import (
                get_collect_proxy_url,
                get_transmit_proxy_url,
            )

            proxy = (get_transmit_proxy_url() or "").strip()
            if proxy:
                return proxy
            return (get_collect_proxy_url() or "").strip()
        except Exception as e:
            logger.warning(f"[플레이오토] 프록시 설정 로드 실패: {e}")
            return ""

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # PlayAuto EMP API는 공개 REST API — 수집 프록시 불필요, 직접 연결
            proxy = self._get_proxy_url()
            if proxy:
                logger.info(
                    f"[플레이오토] 프록시 사용: {proxy.split('@')[-1] if '@' in proxy else 'on'}"
                )
            else:
                logger.warning("[플레이오토] 프록시 미설정 — 직접 연결")
            # POST /prods(상품등록)는 옵션 다수/대용량 상세 페이로드 처리에
            # PlayAuto 서버가 30초 이상 걸리는 케이스가 존재 → read timeout 90초로 확대.
            # connect=15초는 그대로(차단 IP 감지 빠르게).
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(90.0, connect=15.0),
                follow_redirects=True,
                proxy=proxy if proxy else None,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json",
        }

    async def _call_api(
        self,
        method: str,
        url: str,
        body: dict | list | None = None,
        params: dict | None = None,
    ) -> Any:
        """API 호출 공통 메서드."""
        client = self._get_client()
        headers = self._headers()

        kwargs: dict[str, Any] = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        if params is not None:
            kwargs["params"] = params

        # 연결 단계 실패(ConnectError/ConnectTimeout/PoolTimeout)는 서버 도달 전이라
        # 재시도 안전 → 1회 재시도. ReadTimeout 등 응답 단계 실패는 등록 중복 우려로 재시도 안 함.
        async def _send_once():
            return await client.request(method, url, **kwargs)

        try:
            try:
                resp = await _send_once()
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.PoolTimeout,
            ) as e:
                logger.warning(f"[플레이오토] 연결 단계 실패 → 1회 재시도: {e}")
                resp = await _send_once()
        except httpx.TimeoutException as e:
            raise PlayAutoApiError(
                f"[플레이오토] 타임아웃 — GCP/클라우드 환경에서 PlayAuto 호스트 직접 도달 불가 시 "
                f"settings 전송(transmit) 프록시 또는 PLAYAUTO_PROXY_URL 설정 필요: {e}"
            ) from e
        except httpx.ConnectError as e:
            raise PlayAutoApiError(
                f"[플레이오토] 연결 실패 — GCP/클라우드 환경에서 PlayAuto 호스트가 차단됩니다. "
                f"국내 ISP 정적 IP 프록시(전송 용도)를 설정하세요: {e}"
            ) from e

        # 응답 파싱
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        # HTTP 에러 체크
        if resp.status_code >= 400:
            msg = ""
            if isinstance(data, dict):
                msg = data.get("message", data.get("msg", str(data)[:200]))
            else:
                msg = str(data)[:200]
            logger.warning(
                "[플레이오토] HTTP 오류 응답: method=%s url=%s status=%s body=%s params=%s resp=%s",
                method,
                url,
                resp.status_code,
                str(body)[:300] if body is not None else "",
                params,
                str(data)[:500],
            )
            raise PlayAutoApiError(
                f"[플레이오토] HTTP {resp.status_code}: {method} {url} - {msg or resp.reason_phrase}",
                status=resp.status_code,
                data=data,
            )

        return data

    # ── 상품 API ──

    async def register_product(self, products: list[dict]) -> list[dict]:
        """상품 일괄 등록 (POST /prods).

        Args:
            products: 상품 데이터 리스트 (transform_product 결과)

        Returns:
            [{code, status, msg}, ...]
        """
        url = f"{EMP_BASE_URL}/prods"
        body = {"data": products}
        result = await self._call_api("POST", url, body=body)
        logger.info(f"[플레이오토] 상품 등록 응답: {result}")
        return result if isinstance(result, list) else [result]

    async def update_product(
        self, products: list[dict], use_no_edit_slave: bool = False
    ) -> list[dict]:
        """상품 일괄 수정 (PATCH /prods).

        Args:
            products: 수정할 상품 데이터 리스트 (MasterCode 필수)
            use_no_edit_slave: True면 슬레이브 정보 수정 안 함

        Returns:
            [{code, status, msg}, ...]
        """
        url = f"{EMP_BASE_URL}/prods"
        body: dict[str, Any] = {"data": products}
        if use_no_edit_slave:
            body["UseNoEditSlave"] = True
        result = await self._call_api("PATCH", url, body=body)
        logger.info(f"[플레이오토] 상품 수정 응답: {result}")
        return result if isinstance(result, list) else [result]

    async def soldout_product(self, master_codes: list[str]) -> list[dict]:
        """상품 품절 처리 (PATCH /prods/soldout).

        '판매중', '수정대기', '종료대기' → '취소대기'로 변경.
        """
        url = f"{EMP_BASE_URL}/prods/soldout"
        body = {"data": ",".join(master_codes)}
        result = await self._call_api("PATCH", url, body=body)
        logger.info(f"[플레이오토] 상품 품절 응답: {result}")
        return result if isinstance(result, list) else [result]

    async def get_product(self, master_code: str) -> dict:
        """상품 한건 조회 (GET /prods)."""
        url = f"{EMP_BASE_URL}/prods"
        return await self._call_api("GET", url, params={"MasterCode": master_code})

    async def get_products(self, my_cate_name: str = "") -> list[dict]:
        """상품 다중 조회 (GET /prods/info/lookupProd)."""
        url = f"{EMP_BASE_URL}/prods/info/lookupProd"
        params = {}
        if my_cate_name:
            params["MyCateName"] = my_cate_name
        result = await self._call_api("GET", url, params=params or None)
        return result if isinstance(result, list) else [result]

    # ── 주문 API ──

    async def get_orders(
        self,
        malls: list[str] | None = None,
        states: str = "",
        start_date: str = "",
        end_date: str = "",
        page: int = 1,
        count: int = 100,
        master_code: str = "",
        tel: str = "",
        customer: str = "",
    ) -> list[dict]:
        """주문 일괄 조회 (GET /orders)."""
        url = f"{EMP_BASE_URL}/orders"
        params: dict[str, Any] = {"page": page, "count": count}
        if malls:
            for i, mall in enumerate(malls):
                params[f"malls[{i}]"] = mall
        if states:
            params["states"] = states
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        if master_code:
            params["MasterCode"] = master_code
        if tel:
            params["tel"] = tel
        if customer:
            params["customer"] = customer

        result = await self._call_api("GET", url, params=params)
        return result if isinstance(result, list) else [result]

    async def get_order(self, number: int) -> dict:
        """주문 한건 조회 (GET /orders/{number})."""
        url = f"{EMP_BASE_URL}/orders"
        return await self._call_api("GET", url, params={"number": number})

    async def get_order_count(
        self,
        malls: list[str] | None = None,
        states: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> int:
        """주문 총 수량 조회."""
        url = f"{EMP_BASE_URL}/orders/count"
        params: dict[str, Any] = {}
        if malls:
            for i, mall in enumerate(malls):
                params[f"malls[{i}]"] = mall
        if states:
            params["states"] = states
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date

        result = await self._call_api("GET", url, params=params)
        if isinstance(result, dict):
            return int(result.get("count", 0))
        return 0

    async def get_order_cs(self, number: int) -> list[dict]:
        """주문 CS 로그 조회."""
        url = f"{EMP_BASE_URL}/orders/cs"
        result = await self._call_api("GET", url, params={"number": number})
        return result if isinstance(result, list) else [result]

    # ── 송장 API ──

    async def send_invoice(
        self,
        invoices: list[dict],
        change_state: bool = False,
        overwrite: bool = True,
    ) -> list[dict]:
        """송장 입력 (PATCH /senders).

        EMP API는 신규주문 → 송장입력 전이만 공식 지원.
        change_state=False로 호출하면 신규주문 → 송장입력으로 상태 자동 변경.

        Args:
            invoices: [{number, sender(택배사코드 T-code), senderno(송장번호)}, ...]
            change_state: False=송장입력으로 변경(기본), True=출고로 변경
            overwrite: True=기존 송장 덮어쓰기
        """
        url = f"{EMP_BASE_URL}/senders"
        body = {
            "changeState": change_state,
            "overWrite": overwrite,
            "data": invoices,
        }
        result = await self._call_api("PATCH", url, body=body)
        logger.info(f"[플레이오토] 송장 입력 응답: {result}")
        return result if isinstance(result, list) else [result]

    # ── 문의 API ──

    async def get_qnas(
        self,
        malls: list[str] | None = None,
        states: str = "",
        start_date: str = "",
        end_date: str = "",
        page: int = 1,
        count: int = 100,
    ) -> list[dict]:
        """문의 일괄 조회 (GET /qnas)."""
        url = f"{EMP_BASE_URL}/qnas"
        params: dict[str, Any] = {"page": page, "count": count}
        if malls:
            for i, mall in enumerate(malls):
                params[f"malls[{i}]"] = mall
        if states:
            params["states"] = states
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date

        result = await self._call_api("GET", url, params=params)
        if isinstance(result, dict) and "rows" in result:
            rows = result["rows"]
            return rows if isinstance(rows, list) else [rows]
        return result if isinstance(result, list) else [result]

    async def answer_qna(
        self, answers: list[dict], overwrite: bool = False
    ) -> list[dict]:
        """문의 답변 등록 (PATCH /qnas).

        Args:
            answers: [{number, Asubject, AContent}, ...]
            overwrite: 답변 덮어쓰기 여부
        """
        url = f"{EMP_BASE_URL}/qnas"
        body = {"overWrite": overwrite, "data": answers}
        result = await self._call_api("PATCH", url, body=body)
        logger.info(f"[플레이오토] 문의 답변 응답: {result}")
        if isinstance(result, dict) and "rows" in result:
            rows = result["rows"]
            return rows if isinstance(rows, list) else [rows]
        return result if isinstance(result, list) else [result]

    # ── 공통 API ──

    async def get_market_list(self) -> list[dict]:
        """쇼핑몰 정보 조회 (GET /getMarketList)."""
        url = f"{COMMON_BASE_URL}/getMarketList"
        result = await self._call_api("GET", url)
        if isinstance(result, dict) and result.get("success") == "true":
            rows = result.get("rows", [])
            return rows if isinstance(rows, list) else [rows]
        return []

    async def get_deliv_codes(self) -> list[dict]:
        """택배사 코드 조회 (GET /getDelivCode)."""
        url = f"{COMMON_BASE_URL}/getDelivCode"
        result = await self._call_api("GET", url)
        if isinstance(result, dict) and result.get("success") == "true":
            rows = result.get("rows", [])
            return rows if isinstance(rows, list) else [rows]
        return []

    async def get_match_categories(self) -> list[dict]:
        """표준 카테고리 조회 (GET /getMatchCate)."""
        url = f"{COMMON_BASE_URL}/getMatchCate"
        result = await self._call_api("GET", url)
        if isinstance(result, dict) and result.get("success") == "true":
            rows = result.get("rows", [])
            return rows if isinstance(rows, list) else [rows]
        return []

    async def get_mall_sites(self) -> list[dict]:
        """사용중인 쇼핑몰 조회 (GET /get-mall-site)."""
        url = f"{EMP_BASE_URL}/members/get-mall-site"
        result = await self._call_api("GET", url)
        return result if isinstance(result, list) else [result]

    # ── 인증 테스트 ──

    async def test_connection(self) -> bool:
        """API 키 유효성 확인 — 상품 다중 조회로 테스트."""
        try:
            await self.get_products()
            return True
        except PlayAutoApiError:
            return False
        except Exception:
            return False

    # ── 상품 데이터 변환 ──

    @staticmethod
    def transform_product(
        product: dict,
        category_id: str = "",
        stock_qty: int = 999,
        deliv_method: str = "무료",
        deliv_price: str = "0",
    ) -> dict[str, Any]:
        """삼바웨이브 상품 → 플레이오토 EMP API 포맷 변환.

        Args:
            product: 삼바웨이브 수집 상품 dict
            category_id: 플레이오토 카테고리 코드
            stock_qty: 기본 재고수량
            deliv_method: 배송방법 (착불/무료/선결제)
            deliv_price: 배송비

        Returns:
            EMP 상품등록 API에 맞는 dict
        """
        # 기본 정보
        data: dict[str, Any] = {
            "MasterCode": "__AUTO__",
            "ProdName": str(product.get("name", ""))[:200],
            "Price": str(int(product.get("sale_price", 0))),
            "Count": str(stock_qty),
            "MadeIn": _normalize_origin(product.get("origin")),
            "TaxType": "Y",
        }

        # 원가는 절대 전송 금지 — 신규/오토튠 모두 0으로 강제하여 기존 등록값도 초기화
        data["CostPrice"] = "0"

        # 시중가: 정책의 streetPriceRate(%) 적용, 0이면 판매가와 동일
        sale_price = int(product.get("sale_price", 0))
        street_rate = product.get("_street_price_rate", 0)
        if street_rate and sale_price:
            data["StreetPrice"] = str(int(sale_price * (1 + street_rate / 100)))
        else:
            data["StreetPrice"] = str(sale_price)

        # 카테고리
        if category_id:
            data["CateCode"] = str(category_id)

        # 브랜드/제조사/모델명
        brand = product.get("brand", "")
        if brand:
            data["Brand"] = str(brand)
        maker = product.get("maker", "") or product.get("manufacturer", "")
        if maker:
            data["Maker"] = str(maker)
        # 모델명 = 품번 (site_product_id 또는 product_code)
        model = (
            product.get("product_code")
            or product.get("site_product_id")
            or product.get("sku_code")
            or ""
        )
        if model:
            data["Model"] = str(model)

        # 이미지 (최대 10개, 빈 항목 건너뛰고 순차 배치)
        # EMP는 JPG/JPEG/PNG/GIF/BMP 확장자만 허용
        images = product.get("images") or []
        img_idx = 1
        if isinstance(images, list):
            for img_url in images:
                if img_idx > 5:
                    break
                url = img_url if isinstance(img_url, str) else img_url.get("url", "")
                if not url:
                    continue
                # 프로토콜 보정
                if url.startswith("//"):
                    url = f"https:{url}"
                # 확장자 없는 URL에 .jpg 추가 (R2/CDN URL 대응)
                if not any(
                    url.lower().endswith(ext)
                    for ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")
                ):
                    url = url + ".jpg"
                # WebP → JPG 변환 (EMP 미지원)
                if url.lower().endswith(".webp"):
                    url = url[:-5] + ".jpg"
                data[f"Image{img_idx}"] = url
                img_idx += 1
        # 미사용 슬롯을 대표이미지로 채움 (EMP가 Content에서 자동추출하는 것 방지)
        # 최대 5장 상한 (리소스 절약)
        main_img = data.get("Image1", "")
        for fill_idx in range(img_idx, 6):
            data[f"Image{fill_idx}"] = main_img

        # 상세설명 HTML
        content = product.get("detail_html", "") or product.get("description", "")
        if content:
            data["Content"] = str(content)

        # 배송 정보
        data["DelivMethod"] = deliv_method
        data["DelivPrice"] = str(deliv_price)

        # 정책에서 주입된 배송비 적용
        if product.get("_delivery_fee_type") == "PAID":
            base_fee = product.get("_delivery_base_fee", 0)
            data["DelivMethod"] = "선결제"
            data["DelivPrice"] = str(base_fee)
        elif product.get("_delivery_fee_type") == "FREE":
            data["DelivMethod"] = "무료"
            data["DelivPrice"] = "0"

        # 재고수량 정책 반영
        max_stock = product.get("_max_stock")
        if max_stock:
            stock_qty = int(max_stock)
            data["Count"] = str(stock_qty)

        # 키워드 (태그에서 내부태그 제외, 최대 10개)
        tags = product.get("tags") or []
        if isinstance(tags, list):
            clean_tags = [t for t in tags if t and not str(t).startswith("__")]
            for i, tag in enumerate(clean_tags[:10], 1):
                tag_str = str(tag).strip()
                if tag_str:
                    data[f"Keyword{i}"] = tag_str

        # 옵션 변환
        options = product.get("options") or []
        if options and isinstance(options, list):
            emp_opts = _build_options(options, stock_qty)
            if emp_opts:
                data["Opts"] = emp_opts
                # 옵션 타입 결정 (옵션축 개수에 따라)
                has_two_axes = any(o.get("title2") for o in emp_opts)
                data["OptSelectType"] = "SM" if has_two_axes else "SS"

        # 품목정보고시 — 카테고리별로 code 분기 (01의류/02구두신발/03가방/04패션잡화/35기타)
        # GS샵 등 품목 분류 필수 마켓 대응. 35(기타재화)로는 등록하지 않는다 — 차단.
        siil_entry = _build_siil_entry(product, data)
        if siil_entry.get("code") == "35":
            raise PlayAutoApiError(
                "[플레이오토] 의류/신발/가방/잡화가 아닌 카테고리는 등록 차단 "
                "(GS샵 품목 4분류 미해당, 기타재화 35 등록 방지). "
                f"category={product.get('category1', '')} / "
                f"{product.get('category2', '')} / {product.get('category3', '')}"
            )
        data["SiilData"] = [siil_entry]

        # 인증정보 (기본: 해당없음)
        data["CertType"] = "C"

        # 사용자 임의분류 (검색필터명 기반)
        # EMP MyCateName은 '/'가 트리 구분자 — 필터명에 '/' 있으면 _로 치환
        # (신규 필터는 worker.py에서 차단하나 레거시 데이터 방어)
        filter_name = product.get("_search_filter_name", "")
        if filter_name:
            safe_name = filter_name.replace("/", "_")
            data["MyCateName"] = f"SAMBA-WAVE/{safe_name}"

        return data


# ────────────────────────────────────────────────────────────
# SiilData (품목정보고시) 빌더
# ────────────────────────────────────────────────────────────

# 카테고리 그룹 → EMP 품목정보코드
# 01: 의류, 02: 구두/신발, 03: 가방, 04: 패션잡화, 35: 기타재화
_SIIL_CODE_MAP: dict[str, str] = {
    "wear": "01",
    "shoes": "02",
    "bag": "03",
    "accessories": "04",
    # cosmetic/food/electronics 등은 GS샵 품목 4분류에 없으므로 기타재화로 유지
    "cosmetic": "35",
    "food": "35",
    "electronics": "35",
    # sports/etc는 의류·신발·가방 아닌 패션 관련 용품 → 잡화(04)로 등록
    "sports": "04",
    "etc": "04",
}


def _build_siil_entry(product: dict, data: dict) -> dict:
    """상품 카테고리에 맞는 품목정보고시 엔트리를 생성한다.

    GS샵 등은 품목정보가 의류/구두·신발/가방/패션잡화/기타재화 중 하나로
    정확히 분류되어야 등록 가능하다. category1 기반으로 code를 판별하고
    data 필드는 수집값(소재/색상/치수/원산지/제조사/AS) + [상세설명참조]로 채운다.

    주의: EMP SiilData의 data1~data24 각 필드 의미는 code별로 다르지만,
    playauto 공식 스펙(openapi.json)에는 위치별 매핑이 명시되어 있지 않고
    외부 가이드 페이지에서만 안내된다. 의류/신발/가방/잡화는 롯데ON·스마트스토어의
    품목정보고시 표준 필드 순서(소재-색상-치수-제조자-제조국-취급-제조년월-보증-AS)를 참고한다.
    """
    from backend.domain.samba.proxy.notice_utils import detect_notice_group

    group = detect_notice_group(product)
    code = _SIIL_CODE_MAP.get(group, "35")

    fallback = "[상세설명참조]"
    as_phone = (product.get("_as_phone", "") or "").strip() or fallback
    raw_origin = (product.get("origin") or "").strip()
    siil_origin = (
        raw_origin if raw_origin and raw_origin not in ("기타", "국내") else fallback
    )
    maker = data.get("Maker", "") or product.get("brand", "") or fallback
    material = (product.get("material") or "").strip() or fallback
    # 플레이오토 품목정보 소재는 1글자만 등록 시 에러 — 한 글자면 "X 소재" 형태로 보정
    if material != fallback and len(material) == 1:
        material = f"{material} 소재"
    color = (product.get("color") or "").strip() or fallback
    care = (
        product.get("care_instructions") or product.get("careInstructions") or ""
    ).strip() or fallback
    is_imported_y = "Y" if data.get("MadeIn", "").startswith("해외") else "N"
    quality = (
        product.get("quality_guarantee") or product.get("qualityGuarantee") or ""
    ).strip() or "관련 법 및 소비자 분쟁해결 규정에 따름"

    entry: dict[str, str] = {"code": code}

    # 누락 방지: 모든 의류/신발/가방/잡화 코드는 data1~data20을 fallback으로 베이스 채움.
    # 플레이오토 EMP는 code별 필요한 위치만 사용하므로 초과 키는 무시되고, 누락(빈 값)만 차단.
    # GS샵 등 품목정보 필수 마켓에서 (01)/(02)/(03)/(04) 누락 거부 대응.
    if code in ("01", "02", "03", "04"):
        for i in range(1, 21):
            entry[f"data{i}"] = fallback

    if code == "01":
        # 의류: 소재/색상/치수/제조자/제조국/세탁방법/제조년월/품질보증/AS
        entry.update(
            {
                "data1": material,
                "data2": color,
                "data4": maker,
                "data5": siil_origin,
                "data6": care,
                "data8": quality,
                "data9": as_phone,
            }
        )
    elif code == "02":
        # 구두/신발: 소재(겉/안감)/색상/치수/제조자/제조국/취급시주의/제조년월/품질보증/AS
        entry.update(
            {
                "data1": material,
                "data2": color,
                "data4": maker,
                "data5": siil_origin,
                "data6": care,
                "data8": quality,
                "data9": as_phone,
            }
        )
    elif code == "03":
        # 가방: 종류/소재/색상/크기/제조자/제조국/취급시주의/제조년월/품질보증/AS
        entry.update(
            {
                "data2": material,
                "data3": color,
                "data5": maker,
                "data6": siil_origin,
                "data7": care,
                "data9": quality,
                "data10": as_phone,
            }
        )
    elif code == "04":
        # 패션잡화: 종류/소재/치수/제조자/제조국/취급시주의/제조년월/품질보증/AS
        entry.update(
            {
                "data2": material,
                "data4": maker,
                "data5": siil_origin,
                "data6": care,
                "data8": quality,
                "data9": as_phone,
            }
        )
    else:
        # 35 기타재화: 품명/모델/제조자/제조국/수입여부/제조자/AS — 기존 매핑 유지
        entry.update(
            {
                "data1": str(product.get("name", ""))[:100] or fallback,
                "data2": data.get("Model", fallback),
                "data3": fallback,
                "data4": siil_origin,
                "data5": maker,
                "data6": is_imported_y,
                "data7": maker,
                "data8": as_phone,
            }
        )

    return entry


def _build_options(options: list[dict], default_stock: int = 999) -> list[dict]:
    """삼바웨이브 옵션 → EMP 옵션 변환.

    삼바웨이브 옵션 형식:
        소싱처 형식: [{name: "WHITE / M", ...}, ...]  (value 필드 없음)
        명시적 형식: [{option_name: "색상/사이즈", option_value: "빨강/M", ...}, ...]
    """
    emp_opts: list[dict] = []
    seen_keys: set[tuple] = set()

    for opt in options:
        emp_opt: dict[str, str] = {"type": "SELECT"}

        # 옵션명 파싱 — 다양한 형식 대응
        opt_name = opt.get("option_name", "") or opt.get("name", "")
        opt_value = opt.get("option_value", "") or opt.get("value", "")

        if opt_value:
            # 명시적 형식: option_name=제목, option_value=값 (cafe24 등)
            names = opt_name.split("/") if "/" in opt_name else [opt_name]
            values = opt_value.split("/") if "/" in opt_value else [opt_value]
            for i, (n, v) in enumerate(zip(names[:3], values[:3]), 1):
                emp_opt[f"title{i}"] = n.strip()
                emp_opt[f"opt{i}"] = v.strip()
        else:
            # 소싱처 형식: name이 옵션값 역할 (MUSINSA, ABCmart, Nike 등)
            # " / " 구분자로 다축 분리 (예: "WHITE / M" → 옵션1=WHITE, 옵션2=M)
            parts = (
                [p.strip() for p in opt_name.split(" / ")]
                if " / " in opt_name
                else [opt_name.strip()]
            )
            for i, p in enumerate(parts[:3], 1):
                emp_opt[f"title{i}"] = f"옵션{i}" if len(parts) > 1 else "옵션"
                emp_opt[f"opt{i}"] = p

        # 중복 옵션 제거 (EMP API "독립형옵션 중복오류" 방지)
        key = (
            emp_opt.get("title1", ""),
            emp_opt.get("opt1", ""),
            emp_opt.get("title2", ""),
            emp_opt.get("opt2", ""),
            emp_opt.get("title3", ""),
            emp_opt.get("opt3", ""),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)

        # 옵션 가격
        opt_price = opt.get("option_price", 0) or opt.get("add_price", 0)
        emp_opt["price"] = str(int(opt_price))

        # 옵션 재고 (max_stock 제한 적용)
        opt_stock = opt.get("stock", opt.get("quantity", default_stock))
        if default_stock > 0:
            opt_stock = min(int(opt_stock), default_stock)
        if opt.get("is_sold_out") or opt.get("sold_out"):
            emp_opt["soldout"] = "1"
            emp_opt["stock"] = "0"
        else:
            emp_opt["soldout"] = "0"
            emp_opt["stock"] = str(int(opt_stock))

        emp_opt["weight"] = "0"
        emp_opt["manage_code"] = ""
        emp_opt["barcode_user"] = ""

        emp_opts.append(emp_opt)

    return emp_opts


_DOMESTIC_KEYWORDS = {"국내", "한국", "대한민국", "Korea"}
_ETC_KEYWORDS = {"기타", "해당없음", "없음", "미상"}

# EMP 원산지 — 해외 국가→대륙 매핑 (해외=대륙=국가)
_COUNTRY_TO_CONTINENT: dict[str, str] = {
    # 아시아
    "중국": "아시아",
    "일본": "아시아",
    "베트남": "아시아",
    "인도": "아시아",
    "인도네시아": "아시아",
    "태국": "아시아",
    "대만": "아시아",
    "방글라데시": "아시아",
    "캄보디아": "아시아",
    "미얀마": "아시아",
    "파키스탄": "아시아",
    "필리핀": "아시아",
    "말레이시아": "아시아",
    "싱가포르": "아시아",
    "스리랑카": "아시아",
    "터키": "아시아",
    "네팔": "아시아",
    "라오스": "아시아",
    # 유럽
    "이탈리아": "유럽",
    "프랑스": "유럽",
    "독일": "유럽",
    "스페인": "유럽",
    "영국": "유럽",
    "포르투갈": "유럽",
    "폴란드": "유럽",
    "루마니아": "유럽",
    "체코": "유럽",
    "스웨덴": "유럽",
    "네덜란드": "유럽",
    "벨기에": "유럽",
    "스위스": "유럽",
    "오스트리아": "유럽",
    "덴마크": "유럽",
    # 북아메리카
    "미국": "북아메리카",
    "캐나다": "북아메리카",
    "멕시코": "북아메리카",
    # 남아메리카
    "브라질": "남아메리카",
    "아르헨티나": "남아메리카",
    "칠레": "남아메리카",
    "페루": "남아메리카",
    "콜롬비아": "남아메리카",
    # 아프리카
    "이집트": "아프리카",
    "남아공": "아프리카",
    "모로코": "아프리카",
    "에티오피아": "아프리카",
    "튀니지": "아프리카",
    # 오세아니아
    "호주": "오세아니아",
    "뉴질랜드": "오세아니아",
}


def _normalize_origin(origin: str | None) -> str:
    """원산지 값을 EMP 포맷으로 정규화.

    EMP 포맷:
        국내: "국내=시도=시군구" (예: "국내=강원=강릉시")
        해외: "해외=대륙=국가" (예: "해외=아시아=중국")
        기타: "기타=기타=기타"
    """
    if not origin or not origin.strip():
        return "기타=기타=기타"
    origin = origin.strip()

    # 이미 "=" 포함된 완전한 형식
    parts = origin.split("=")
    if len(parts) >= 3:
        return origin
    if len(parts) == 2:
        return f"{parts[0]}={parts[1]}={parts[1]}"

    # 단일 값 — 국내/기타/해외 자동 판별
    if origin in _DOMESTIC_KEYWORDS:
        return "국내=서울=서울"
    if origin in _ETC_KEYWORDS:
        return "기타=기타=기타"
    # 해외 국가 → 대륙 매핑
    continent = _COUNTRY_TO_CONTINENT.get(origin, "아시아")
    return f"해외={continent}={origin}"

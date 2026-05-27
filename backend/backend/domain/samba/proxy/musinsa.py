"""무신사 API 클라이언트 - httpx 기반.

proxy-server.mjs의 무신사 관련 로직을 Python으로 포팅.
상품 상세, 옵션/재고, 고시정보, 쿠폰, 혜택가, 검색 API를 지원한다.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from backend.core.config import settings
from backend.utils.logger import logger


def _is_future_sell_start(d: dict) -> bool:
    """sellStartDate가 현재 시각보다 미래이면 True (신규 발매 카운트다운 상품)."""
    raw = d.get("sellStartDate") or d.get("saleStartDate")
    if not raw or not isinstance(raw, str):
        return False
    try:
        if raw.endswith("Z") or "+" in raw[10:] or "-" in raw[10:]:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            # 무신사 응답은 timezone 없는 KST 시각 반환
            dt = datetime.fromisoformat(raw).replace(
                tzinfo=timezone(timedelta(hours=9))
            )
    except (ValueError, TypeError):
        return False
    return dt > datetime.now(tz=timezone.utc)


class RateLimitError(Exception):
    """소싱처 차단 감지 (429/403)."""

    def __init__(self, status: int, retry_after: int = 0):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"HTTP {status} (retry_after={retry_after})")


class MusinsaClient:
    """무신사 API 클라이언트 (상품 상세, 검색, 로그인 상태 확인)."""

    BASE_DETAIL = "https://goods-detail.musinsa.com/api2/goods"
    BASE_SEARCH = "https://api.musinsa.com/api2/dp/v1/plp/goods"
    BASE_COUPON = (
        "https://api.musinsa.com/api2/coupon/coupons/getUsableCouponsByGoodsNo"
    )
    BASE_MEMBER = "https://api.musinsa.com/api2/member/v1/me"

    HEADERS: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": "https://www.musinsa.com/",
        "Origin": "https://www.musinsa.com",
    }

    def __init__(self, cookie: str = "", *, proxy_url: str | None = None) -> None:
        self.cookie = cookie
        self.proxy_url = proxy_url

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        h = {**self.HEADERS}
        if self.cookie:
            h["Cookie"] = self.cookie
        if extra:
            h.update(extra)
        return h

    async def _check_product_pre_point(
        self, client: httpx.AsyncClient, goods_no: str
    ) -> Optional[bool]:
        """비인증 호출로 상품 본연의 isPrePoint 확인 (계정 설정 영향 배제)."""
        try:
            headers = {**self.HEADERS}  # 쿠키 미포함
            resp = await client.get(f"{self.BASE_DETAIL}/{goods_no}", headers=headers)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                return data.get("isPrePoint") is True
        except Exception:
            pass
        return None  # 실패 시 None → 호출부에서 auth 값 유지

    @staticmethod
    def _to_image_url(path: str) -> str:
        if not path:
            return ""
        if path.startswith("http"):
            return path
        if path.startswith("//"):
            return f"https:{path}"
        return f"https://image.msscdn.net{path}"

    # 추적 픽셀/트래커/외부 분석 도메인 차단 — 롯데홈쇼핑 [1036] 등 확장자 검증 마켓 보호
    # (셀러가 desc HTML 혹은 goodsImages에 트래커 URL을 박아넣는 케이스 다수)
    _TRACKER_PATTERNS = (
        "trace.smartwiz",
        "trace.",
        "pixel.",
        "/logger/",
        "doubleclick.net",
        "google-analytics.com",
        "googletagmanager.com",
        "facebook.com/tr",
        "/track?",
        "/track/",
    )
    _IMG_EXT_RE = re.compile(
        r"\.(?:jpg|jpeg|png|gif|webp|bmp)(?:[?#].*)?$", re.IGNORECASE
    )

    @classmethod
    def _is_valid_image_url(cls, url: str) -> bool:
        """이미지 등록 가능 URL 판별 — 트래커 차단 + 이미지 확장자 화이트리스트."""
        if not url:
            return False
        low = url.lower()
        if any(p in low for p in cls._TRACKER_PATTERNS):
            return False
        return bool(cls._IMG_EXT_RE.search(url.split("?", 1)[0]))

    @staticmethod
    def _floor_to_10(amount: float) -> int:
        return int(amount / 10) * 10

    @classmethod
    def _calculate_display_benefit_price(
        cls,
        *,
        benefit_base: int,
        grade_discount_rate: float,
        is_point_restricted: bool,
        point_rate_pct: float,
    ) -> tuple[int, int, int]:
        """Calculate the product-page max benefit price shown by Musinsa."""
        grade_discount = (
            cls._floor_to_10(benefit_base * grade_discount_rate / 100)
            if grade_discount_rate > 0
            else 0
        )
        point_base = benefit_base - grade_discount
        point_usage = 0
        if not is_point_restricted and point_rate_pct > 0:
            point_usage = cls._floor_to_10(point_base * point_rate_pct / 100)
        display_price = benefit_base - grade_discount - point_usage
        return grade_discount, point_usage, display_price

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_goods_detail(
        self,
        goods_no: str,
        *,
        member_grade_rate: Optional[float] = None,
        refresh_only: bool = False,
        _shared_client: Optional[httpx.AsyncClient] = None,
    ) -> dict[str, Any]:
        """상품 상세 조회 - 상세 + 옵션 + 재고 + 고시정보 + 쿠폰 + 혜택가.

        proxy-server.mjs ``fetchMusinsaProduct()`` 전체 로직 포팅.
        _shared_client: 외부에서 공유 클라이언트를 넘기면 연결 재사용 (병렬 수집 성능 향상)
        """
        # 무신사는 로그인(쿠키) 필수
        if not self.cookie:
            raise ValueError(
                "무신사 수집은 로그인(쿠키)이 필요합니다. "
                "확장앱에서 무신사 로그인 후 다시 시도하세요."
            )
        timeout = httpx.Timeout(settings.http_timeout_default, connect=10.0)
        # 공유 클라이언트 재사용 (TCP 연결 풀링) 또는 새로 생성
        _own_client = None
        if _shared_client:
            client = _shared_client
        else:
            _client_kwargs: dict[str, Any] = {"timeout": timeout}
            if self.proxy_url:
                _client_kwargs["proxy"] = self.proxy_url
            _own_client = httpx.AsyncClient(**_client_kwargs)
            client = _own_client
        try:
            # 방어적 초기화 — 모든 코드 경로에서 UnboundLocalError 방지
            desc_html = ""
            unique_images: list[str] = []
            detail_images: list[str] = []

            # 1) 상품 상세 API
            detail_resp = await client.get(
                f"{self.BASE_DETAIL}/{goods_no}",
                headers=self._headers(),
            )
            # 429/403 차단 감지
            if detail_resp.status_code in (429, 403):
                retry_after = int(detail_resp.headers.get("Retry-After", "30"))
                raise RateLimitError(detail_resp.status_code, retry_after)
            detail_resp.raise_for_status()
            detail_json = detail_resp.json()
            meta = detail_json.get("meta", {})
            if meta.get("result") != "SUCCESS" or not detail_json.get("data"):
                raise ValueError("상품 데이터 없음")

            d = detail_json["data"]
            gp = d.get("goodsPrice") or {}
            cat = d.get("category") or {}  # None 방지

            # 2) 옵션 API + 재고 API (메인 / 추가옵션 / 그룹명 분리)
            (
                options,
                option_value_no_map,
                addon_options,
                option_group_names,
            ) = await self._fetch_options(client, goods_no, gp)

            # 3) 상품고시정보 API (갱신 모드에서는 스킵)
            essential = (
                {} if refresh_only else await self._fetch_essential(client, goods_no)
            )

            # 카테고리
            category_levels = [cat.get(f"categoryDepth{i}Name") for i in range(1, 5)]
            category_levels = [c for c in category_levels if c]

            # 이미지 파싱 (갱신 모드에서는 스킵 — 가격/재고만 필요)
            unique_images = []
            original_image_count = 0
            detail_images = []
            desc_html = ""
            if not refresh_only:
                desc_html = self._sanitize_desc_html(d.get("goodsContents", ""))
                detail_images = self._extract_detail_images(desc_html)

                thumbnail_url = d.get("thumbnailImageUrl", "")
                goods_images_raw = d.get("goodsImages") or []
                logger.info(
                    f"[무신사 이미지] {goods_no}: "
                    f"thumbnail={thumbnail_url!r}, "
                    f"goodsImages={len(goods_images_raw)}개, "
                    f"goodsContents길이={len(desc_html)}, "
                    f"detailImages={len(detail_images)}개"
                )
                if goods_images_raw:
                    logger.info(
                        f"[무신사 이미지 상세] goodsImages 샘플: {goods_images_raw[:3]}"
                    )

                all_images = [self._to_image_url(thumbnail_url)]
                for img in goods_images_raw:
                    all_images.append(
                        self._to_image_url(img.get("imageUrl") or img.get("url", ""))
                    )
                # 트래커/확장자 없는 URL drop — 마켓 등록 [1036] 등 차단 방지
                all_images = [
                    i for i in all_images if i and self._is_valid_image_url(i)
                ]
                unique_images = list(dict.fromkeys(all_images))[:9]
                logger.info(
                    f"[무신사 이미지 최종] {goods_no}: images={len(unique_images)}개, detail_images={len(detail_images)}개"
                )

            # 소재 정보
            materials = (d.get("goodsMaterial") or {}).get("materials", [])
            material_str = ", ".join(
                (
                    f"{m.get('materialName') or m.get('name', '')} "
                    f"{m.get('rate') or m.get('ratio', '')}%"
                ).strip()
                if (m.get("rate") or m.get("ratio"))
                else (m.get("materialName") or m.get("name", ""))
                for m in materials
                if (m.get("materialName") or m.get("name"))
            )

            # 시즌 정보 — 코드 → 텍스트 변환
            _SEASON_MAP = {
                "1": "SS",
                "2": "FW",
                "3": "ALL SS",
                "4": "ALL FW",
                "0": "ALL",
            }
            season_year = d.get("seasonYear", "")
            if season_year == "0000":
                season_year = "ALL"
            season_code = str(d.get("season", ""))
            season_text = _SEASON_MAP.get(season_code, season_code)
            if not season_text and season_code not in ("0", ""):
                season_text = season_code
            season = " ".join(filter(None, [season_year, season_text]))

            # 4) 가격 계산
            normal_p = gp.get("normalPrice", 0) or 0
            raw_sale = gp.get("immediateDiscountedPrice") or gp.get("salePrice", 0) or 0
            # 무신사 신규 "최저가 도전"/이벤트 할인 — salePrice 에는 미반영, extraDiscountAmount 에 별도 차감액
            # finalPrice = salePrice - extraDiscountAmount - couponPrice 일체 적용된 최종 노출가
            # 우리는 쿠폰 분은 _fetch_coupons 에서 별도 계산하므로 extraDiscountAmount 만 차감 (이중 차감 방지)
            extra_disc = gp.get("extraDiscountAmount", 0) or 0
            if raw_sale > 0 and extra_disc > 0:
                raw_sale = max(0, raw_sale - extra_disc)
            s_price = (
                raw_sale
                if (raw_sale > 0 and (normal_p == 0 or raw_sale <= normal_p))
                else (normal_p or raw_sale)
            )
            # 최대혜택가 = 할인가 - 쿠폰 - 등급 - 적립금 - 선할인
            # 1단계: 쿠폰 할인
            # goodsPrice.couponPrice는 카드/결제 쿠폰(AG 타입)을 포함하므로 최대혜택가 계산에 사용하지 않음
            # 최대혜택가 쿠폰의 올바른 판단 기준: 쿠폰 API의 bestSalePriceYn=Y (SG/SB 타입)
            coupon_price_raw = (
                gp.get("couponPrice", 0) or 0
            )  # price_uncertain 판단용으로만 사용
            (
                benefit_coupon_discount,
                _coupon_api_failed,
                _coupons_total,
                _sg_y_total,
            ) = await self._fetch_coupons(
                client,
                goods_no,
                d,
                s_price,
                0,  # 초기값 0 고정 — goodsPrice.couponPrice 미사용
            )
            # 인증 의심: 쿠키 보유 + couponPrice 있는데 쿠폰 응답이 0건이면
            # 비로그인 응답 가능성 (회원전용 SG/SB 쿠폰 누락 → cost 부정확)
            _auth_suspect = (
                bool(self.cookie)
                and _coupons_total == 0
                and coupon_price_raw > 0
                and coupon_price_raw < s_price
            )
            if _auth_suspect:
                logger.warning(
                    f"[무신사 인증 의심] {goods_no}: 쿠키 보유했으나 쿠폰 0건 "
                    f"(couponPrice={coupon_price_raw}, salePrice={s_price}) — "
                    f"DB musinsa_cookies 만료/복호화 실패 가능성. "
                    f"price_uncertain=True 마킹하여 잘못된 cost 갱신 차단."
                )
            benefit_base = s_price - benefit_coupon_discount

            # ── 등급 할인 & 선할인 ──
            # 등급할인 조건: isLimitedDc=False (등급할인 제한 아닌 상품만)
            #   → goodsPrice.memberDiscountRate 사용 (memberGrade.discountRate는 항상 0)
            # 선할인 조건: isPrePoint=True
            #   → 등급적립(memberSavePointRate) + 구매적립(savePoint)
            is_limited_dc = d.get("isLimitedDc") is True
            grade_discount_rate = (
                (gp.get("memberDiscountRate", 0) or 0) if not is_limited_dc else 0
            )
            grade_save_point_rate = gp.get("memberSavePointRate", 0) or 0
            save_point_value = gp.get("savePoint", 0) or 0

            # 보유 적립금 5,000원 이상일 때만 적립금 사용 반영
            member_point = (d.get("point") or {}).get("memberPoint", 0) or 0
            can_use_point = member_point >= 5000

            raw_point_rate = d.get("maxUsePointRate", 0) or 0
            point_rate_pct = (
                raw_point_rate * 100 if 0 < raw_point_rate < 1 else raw_point_rate
            )

            # 2단계: 등급할인 (benefit_base 기준, 10원 절사)
            grade_discount, point_usage, display_benefit_price = (
                self._calculate_display_benefit_price(
                    benefit_base=benefit_base,
                    grade_discount_rate=grade_discount_rate,
                    is_point_restricted=d.get("isRestictedUsePoint") is True,
                    point_rate_pct=point_rate_pct if can_use_point else 0,
                )
            )

            # 3단계: 적립금 사용 (benefit_base - 등급할인 기준, 10원 절사)
            is_point_restricted = d.get("isRestictedUsePoint") is True
            point_base = benefit_base - grade_discount
            point_usage = 0
            if can_use_point and not is_point_restricted and point_rate_pct > 0:
                point_usage = (
                    int(point_base * point_rate_pct / 100 / 10) * 10
                )  # 10원 절사

            # 4단계: 적립 선할인 (isPrePoint=True일 때)
            # 선할인 = 등급적립(remaining × memberSavePointRate) + 구매적립(savePoint)
            # isPrePoint 교정: 인증 결과 True → 비인증으로 상품 본연 값 확인
            is_pre_point = d.get("isPrePoint") is True
            if is_pre_point:
                product_pre_point = await self._check_product_pre_point(
                    client, goods_no
                )
                if product_pre_point is False:
                    is_pre_point = False
                    logger.info(
                        f"[무신사 선할인 교정] {goods_no}: "
                        f"계정 설정 영향 → isPrePoint=False로 교정"
                    )
                # product_pre_point=None(실패) → auth 값(True) 유지
            remaining = display_benefit_price
            pre_discount = 0
            if is_pre_point:
                grade_point = (
                    self._floor_to_10(remaining * grade_save_point_rate / 100)
                    if grade_save_point_rate > 0
                    else 0
                )
                pre_discount = grade_point + save_point_value

            # 선할인(isPrePoint=True)일 때만 pre_discount 차감.
            # isPrePoint=False(예: 5180810)면 위 단계에서 pre_discount=0 → 차감 영향 없음.
            # isPrePoint=True(예: 4988437)면 무신사 화면 "적립금 선할인" 라디오 즉시 차감 가격에 맞춤.
            # b1e64568에서 무조건 제거했으나 isPrePoint 분기가 이미 보호하므로 차감 부활.
            best_benefit_price = display_benefit_price - pre_discount

            # 보유 적립금(point_usage) 제외 버전 — 정책 토글용
            # point_usage만 0으로 재계산. 등급할인/선할인은 유지
            # 선할인은 remaining(=display_benefit_price)에 의존하므로 동일 로직 재적용
            display_benefit_price_excl_held = benefit_base - grade_discount
            pre_discount_excl = 0
            if is_pre_point:
                grade_point_excl = (
                    self._floor_to_10(
                        display_benefit_price_excl_held * grade_save_point_rate / 100
                    )
                    if grade_save_point_rate > 0
                    else 0
                )
                pre_discount_excl = grade_point_excl + save_point_value
            # 동일 정책: isPrePoint=True면 pre_discount_excl 차감
            best_benefit_price_excl_held_point = (
                display_benefit_price_excl_held - pre_discount_excl
            )

            # 추가 비로그인 검출 신호: 쿠키 있는데 회원 혜택(등급할인/적립금/선할인)이 전부 0
            # 5259516 사례 — 쿠폰은 적용됐지만 등급할인/적립금만 누락된 비로그인 응답
            _anon_zero_benefits = (
                bool(self.cookie)
                and grade_discount_rate == 0
                and raw_point_rate == 0
                and not is_pre_point
                and not is_limited_dc
            )
            if _anon_zero_benefits:
                logger.warning(
                    f"[무신사 인증 의심2] {goods_no}: 쿠키 보유했으나 "
                    f"등급할인/적립금/선할인 모두 0 — 비로그인 응답 가능성. "
                    f"price_uncertain=True 마킹하여 잘못된 cost 갱신 차단."
                )
                _auth_suspect = True

            logger.info(
                f"[무신사 혜택가] {goods_no}: "
                f"할인가={s_price}, 쿠폰=-{benefit_coupon_discount}, "
                f"benefit_base={benefit_base}, "
                f"등급할인({grade_discount_rate}%,limitedDc={is_limited_dc})=-{grade_discount}, "
                f"적립금({point_rate_pct}%,보유={member_point},사용가능={can_use_point})=-{point_usage}(base={point_base}), "
                f"선할인(savePtRate={grade_save_point_rate}%+savePt={save_point_value})=-{pre_discount}, "
                f"혜택가={best_benefit_price}, 보유적립금제외={best_benefit_price_excl_held_point}"
            )

            # 배송 정보: 무료배송(플러스배송) / 당일발송(플러스배송 OR isTodayReleaseGoods)
            is_plus = d.get("isPlusDelivery", False) is True
            lpi = d.get("logisticsPrioritizedInventory") or {}
            is_free_shipping = is_plus
            is_same_day = is_plus or lpi.get("isTodayReleaseGoods", False) is True
            logger.info(
                f"[무신사 배송] {goods_no}: "
                f"isPlusDelivery={is_plus}, "
                f"isTodayReleaseGoods={lpi.get('isTodayReleaseGoods')}, "
                f"freeShipping={is_free_shipping}, sameDayDelivery={is_same_day}"
            )

            now_iso = datetime.now(tz=timezone.utc).isoformat()

            # 판매 상태 관련 필드 디버그 로그
            logger.info(
                f"[무신사 상태 디버그] {goods_no}: "
                f"goodsSaleType={d.get('goodsSaleType')!r}, "
                f"goodsSaleTypeText={d.get('goodsSaleTypeText')!r}, "
                f"isSale={gp.get('isSale')!r}, "
                f"isSoldOut_gp={gp.get('isSoldOut')!r}, "
                f"isSoldOut_d={d.get('isSoldOut')!r}, "
                f"isOutOfStock={d.get('isOutOfStock')!r}, "
                f"canBuy={d.get('canBuy')!r}, "
                f"isOfflineGoods={d.get('isOfflineGoods')!r}, "
                f"goodsTypeCode={d.get('goodsTypeCode')!r}, "
                f"saleState={gp.get('saleState') or d.get('saleState')!r}, "
                f"timeSale={d.get('timeSale')!r}, "
                f"isTimeSale={d.get('isTimeSale')!r}, "
                f"saleReserveYmdt={gp.get('saleReserveYmdt') or d.get('saleReserveYmdt')!r}"
            )

            brand_info = d.get("brandInfo") or {}
            _result = {
                "id": f"col_musinsa_{goods_no}_{int(datetime.now(tz=timezone.utc).timestamp() * 1000)}",
                "sourceSite": "MUSINSA",
                "siteProductId": str(d.get("goodsNo") or goods_no),
                "sourceUrl": f"https://www.musinsa.com/products/{goods_no}",
                "searchFilterId": None,
                "name": d.get("goodsNm", ""),
                "nameEn": d.get("goodsNmEng", ""),
                "nameJa": "",
                "brand": brand_info.get("brandName") or d.get("brand", ""),
                "brandCode": d.get("brand", ""),
                "category": " > ".join(category_levels),
                "category1": cat.get("categoryDepth1Name", ""),
                "category2": cat.get("categoryDepth2Name", ""),
                "category3": cat.get("categoryDepth3Name", ""),
                "category4": cat.get("categoryDepth4Name", ""),
                "categoryCode": (
                    cat.get("categoryDepth4Code")
                    or cat.get("categoryDepth3Code")
                    or cat.get("categoryDepth2Code")
                    or cat.get("categoryDepth1Code")
                    or ""
                ),
                "images": unique_images,
                "originalImageCount": original_image_count,
                "detailImages": detail_images,
                "detailHtml": desc_html,
                "options": options,
                "addonOptions": addon_options,
                "optionGroupNames": option_group_names,
                "originalPrice": gp.get("normalPrice") or raw_sale or 0,
                "salePrice": s_price,
                "couponPrice": benefit_base,
                "bestBenefitPrice": best_benefit_price,
                "bestBenefitPriceExclHeldPoint": best_benefit_price_excl_held_point,
                "memberDiscountRate": grade_discount_rate,
                "isLoggedIn": bool(self.cookie),
                "discountRate": gp.get("discountRate", 0),
                "origin": essential.get("origin", ""),
                "material": essential.get("material") or material_str,
                "manufacturer": essential.get("manufacturer", ""),
                "color": essential.get("color", ""),
                "sizeInfo": essential.get("size", ""),
                "care_instructions": essential.get("careInstructions", ""),
                "quality_guarantee": essential.get("qualityGuarantee", ""),
                "brandNation": brand_info.get("brandNationName", ""),
                "season": season,
                "style_code": d.get("styleNo", ""),
                "kcCert": "",
                "tags": [],
                "status": "collected",
                "appliedPolicyId": None,
                "marketPrices": {},
                "updateEnabled": True,
                "priceUpdateEnabled": True,
                "stockUpdateEnabled": True,
                "marketTransmitEnabled": True,
                "registeredAccounts": [],
                # 성별: 배열 → 문자열 (예: ["남성", "여성"] → "남녀공용", ["남성"] → "남성")
                "sex": (lambda s: "남녀공용" if len(s) != 1 else s[0])(
                    d.get("sex") or []
                ),
                "storeCodes": d.get("storeCodes", []),
                "isOutlet": d.get("isOutlet", False),
                # 부티끄 판별: goodsTypeCode 또는 saleType
                "isBoutique": (
                    str(d.get("goodsTypeCode", "")).upper() == "BOUTIQUE"
                    or "부티크" in str(d.get("goodsTypeName", ""))
                    or "부티끄" in str(d.get("goodsTypeName", ""))
                    or any(
                        str(sc).upper() in ("BOUTIQUE", "BTQSHOP")
                        for sc in (d.get("storeCodes") or [])
                    )
                ),
                # 품절 판단: isSale=False(판매안함/판매예정) + soldOut + 모든옵션품절
                # STAND_BY_SALE: 재입고 알림 전체 품절 상태 (isRestock=True이지만 SALE 상태인 부분품절과 구분)
                "isOutOfStock": bool(
                    str(d.get("goodsSaleType", "")).upper()
                    in ("STOP_SALE", "PROHIBITED", "CLOSE", "SOLD_OUT", "STAND_BY_SALE")
                    or d.get("isSoldOut")
                    or (d.get("goodsPrice") or {}).get("isSoldOut")
                    or d.get("isOutOfStock", False)
                    or (
                        bool(options)
                        and all(opt.get("isSoldOut", False) for opt in options)
                    )
                ),
                "isSale": gp.get("isSale", False),
                # 판매 상태: sold_out(품절) → preorder(판매예정) → in_stock 순서로 판단
                # sold_out을 먼저 체크해야 preorder 상태였다가 품절된 경우를 올바르게 처리
                # canBuy=False / isOfflineGoods=True: 오프라인 전용 상품 sold_out 처리
                # STAND_BY_SALE: goodsSaleType 기준 전체품절 (부분품절 SALE 상태와 구분)
                "saleStatus": (
                    "sold_out"
                    if bool(
                        str(d.get("goodsSaleType", "")).upper()
                        in (
                            "STOP_SALE",
                            "PROHIBITED",
                            "CLOSE",
                            "SOLD_OUT",
                            "STAND_BY_SALE",
                        )
                        or d.get("canBuy") is False
                        or d.get("isOfflineGoods") is True
                        or d.get("isSoldOut")
                        or (d.get("goodsPrice") or {}).get("isSoldOut")
                        or d.get("isOutOfStock", False)
                        or (
                            bool(options)
                            and all(opt.get("isSoldOut", False) for opt in options)
                        )
                    )
                    else "preorder"
                    # 판매 예약 날짜가 설정된 경우 (판매예정)
                    if (
                        bool(gp.get("saleReserveYmdt") or d.get("saleReserveYmdt"))
                        # 예약/사전주문 배송 타입 옵션이 있는 경우
                        or bool(
                            options
                            and any(
                                str(opt.get("deliveryType", "")).upper()
                                in ("RESERVATION", "PREORDER", "RESERVE", "SCHEDULED")
                                for opt in options
                            )
                        )
                        # sellStartDate가 미래 시각인 경우 — 신규 발매 카운트다운 상품
                        or _is_future_sell_start(d)
                        # isSale=False 조건 제거 — 무배당발 상품도 isSale=False일 수 있음
                    )
                    else "in_stock"
                ),
                "freeShipping": is_free_shipping,
                "sameDayDelivery": is_same_day,
                "collectedAt": now_iso,
                "updatedAt": now_iso,
                # 가격 불확실 케이스:
                #   1) 쿠폰 API 실패 + goodsPrice.couponPrice도 0
                #   2) 인증 의심 (쿠키 있는데 쿠폰 0건이고 couponPrice>0 — 비로그인 응답)
                "price_uncertain": (_coupon_api_failed and coupon_price_raw == 0)
                or _auth_suspect,
                # 적립금 사용 제한 여부 (True=불가, False=가능)
                "isPointRestricted": bool(d.get("isRestictedUsePoint")),
            }
            # saleStatus=sold_out이면 모든 옵션 재고 강제 0 (API가 outOfStock=False로 내려와도)
            if _result.get("saleStatus") == "sold_out" and _result.get("options"):
                for _opt in _result["options"]:
                    _opt["stock"] = 0
                    _opt["isSoldOut"] = True
            return _result
        finally:
            if _own_client:
                await _own_client.aclose()

    async def search_products(
        self,
        keyword: str,
        page: int = 1,
        size: int = 30,
        sort: str = "POPULAR",
        category: str = "",
        brand: str = "",
        min_price: int | None = None,
        max_price: int | None = None,
        gf: str = "A",
    ) -> dict[str, Any]:
        """상품 검색 (API 방식) - proxy-server.mjs /api/musinsa/search-api 포팅."""
        size = min(size, 200)
        params: dict[str, str] = {
            "caller": "SEARCH",
            "keyword": keyword,
            "page": str(page),
            "size": str(size),
            "sort": sort,
            "gf": gf,
        }
        if category:
            params["category"] = category
        if brand:
            params["brand"] = brand
        if min_price is not None:
            params["minPrice"] = str(min_price)
        if max_price is not None:
            params["maxPrice"] = str(max_price)

        timeout = httpx.Timeout(settings.http_timeout_default, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                self.BASE_SEARCH,
                params=params,
                headers=self._headers(),
            )
            # 검색 API도 429/403 차단 감지
            if resp.status_code in (429, 403):
                retry_after = int(resp.headers.get("Retry-After", "30"))
                raise RateLimitError(resp.status_code, retry_after)
            resp.raise_for_status()
            api_data = resp.json()

            meta = api_data.get("meta", {})
            if meta.get("result") != "SUCCESS":
                raise ValueError("무신사 API 결과 실패")

            item_list = (api_data.get("data") or {}).get("list", [])
            pagination = (api_data.get("data") or {}).get("pagination", {})
            now_iso = datetime.now(tz=timezone.utc).isoformat()

            products = []
            for item in item_list:
                goods_no = item.get("goodsNo", "")
                products.append(
                    {
                        "id": f"col_musinsa_{goods_no}_{int(datetime.now(tz=timezone.utc).timestamp() * 1000)}",
                        "sourceSite": "MUSINSA",
                        "siteProductId": str(goods_no),
                        "sourceUrl": (
                            item.get("goodsLinkUrl")
                            or f"https://www.musinsa.com/products/{goods_no}"
                        ),
                        "searchFilterId": None,
                        "name": item.get("goodsName", ""),
                        "nameEn": "",
                        "nameJa": "",
                        "brand": item.get("brandName") or item.get("brand", ""),
                        "brandCode": item.get("brand", ""),
                        "category": "",
                        "images": [item["thumbnail"]] if item.get("thumbnail") else [],
                        "detailImages": [],
                        "detailHtml": "",
                        "options": [],
                        "originalPrice": item.get("normalPrice")
                        or item.get("price", 0),
                        "salePrice": item.get("price") or item.get("normalPrice", 0),
                        "discountRate": item.get("saleRate", 0),
                        "origin": "",
                        "material": "",
                        "manufacturer": "",
                        "season": "",
                        "styleCode": "",
                        "kcCert": "",
                        "tags": [],
                        "status": "collected",
                        "isSoldOut": item.get("isSoldOut", False),
                        "appliedPolicyId": None,
                        "marketPrices": {},
                        "updateEnabled": True,
                        "priceUpdateEnabled": True,
                        "stockUpdateEnabled": True,
                        "marketTransmitEnabled": True,
                        "registeredAccounts": [],
                        "collectedAt": now_iso,
                        "updatedAt": now_iso,
                    }
                )

            return {
                "success": True,
                "count": len(products),
                "totalCount": pagination.get("totalCount", 0),
                "totalPages": pagination.get("totalPages", 0),
                "page": pagination.get("page", page),
                "data": products,
            }

    async def search_by_url(self, url: str) -> dict[str, Any]:
        """URL 기반 검색/리다이렉트 처리 - proxy-server.mjs /api/musinsa/search 포팅."""
        timeout = httpx.Timeout(settings.http_timeout_default, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            # onelink.me 단축 URL
            if "musinsa.onelink.me" in url:
                resp = await client.get(
                    url, headers={"User-Agent": self.HEADERS["User-Agent"]}
                )
                final_url = str(resp.url)
                match = re.search(r"/(?:app/)?(?:goods|products)/(\d{4,8})", final_url)
                if match:
                    return {
                        "success": True,
                        "count": 1,
                        "goodsNos": [match.group(1)],
                        "source": "redirect",
                    }

            # URL에서 키워드 추출 시도
            try:
                from urllib.parse import parse_qs, urlparse

                parsed = urlparse(url)
                qs = parse_qs(parsed.query)
                kw = (qs.get("keyword") or qs.get("q") or qs.get("query") or [""])[0]
                if kw:
                    params = {
                        "caller": "SEARCH",
                        "keyword": kw,
                        "page": "1",
                        "size": "50",
                        "sort": "POPULAR",
                        "gf": "A",
                    }
                    api_resp = await client.get(
                        self.BASE_SEARCH,
                        params=params,
                        headers={k: v for k, v in self.HEADERS.items()},
                    )
                    if api_resp.status_code == 200:
                        api_data = api_resp.json()
                        if (api_data.get("meta") or {}).get("result") == "SUCCESS":
                            goods_nos = [
                                str(item.get("goodsNo"))
                                for item in (api_data.get("data") or {}).get("list", [])
                            ]
                            return {
                                "success": True,
                                "count": len(goods_nos),
                                "goodsNos": goods_nos,
                                "source": "api",
                            }
            except Exception:
                pass

            # URL에서 상품번호 직접 추출
            match = re.search(r"/(?:app/)?(?:goods|products)/(\d{4,8})", url)
            if match:
                return {
                    "success": True,
                    "count": 1,
                    "goodsNos": [match.group(1)],
                    "source": "url-pattern",
                }

            return {"success": True, "count": 0, "goodsNos": [], "source": "none"}

    async def check_login_status(self, cookie: Optional[str] = None) -> dict[str, Any]:
        """로그인 상태 확인 - proxy-server.mjs /api/musinsa/check-login 포팅."""
        cookie_to_check = cookie or self.cookie
        if not cookie_to_check:
            return {"isLoggedIn": False}

        timeout = httpx.Timeout(settings.http_timeout_short, connect=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    self.BASE_MEMBER,
                    headers={**self.HEADERS, "Cookie": cookie_to_check},
                )
                me_json = resp.json()
                data = me_json.get("data") or {}
                is_logged_in = bool(data.get("memberId"))
                return {
                    "isLoggedIn": is_logged_in,
                    "memberId": data.get("memberId", ""),
                    "gradeName": data.get("gradeName", ""),
                }
        except Exception:
            return {"isLoggedIn": False}

    async def set_cookie_and_verify(self, cookie: str) -> dict[str, Any]:
        """쿠키 설정 및 검증 - proxy-server.mjs /api/musinsa/set-cookie 포팅."""
        if not cookie:
            return {"success": False, "message": "쿠키가 없습니다"}

        self.cookie = cookie

        timeout = httpx.Timeout(settings.http_timeout_short, connect=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    self.BASE_MEMBER,
                    headers={**self.HEADERS, "Cookie": cookie},
                )
                me_json = resp.json()
                data = me_json.get("data") or {}
                if data.get("memberId"):
                    return {
                        "success": True,
                        "isLoggedIn": True,
                        "memberId": data["memberId"],
                        "gradeName": data.get("gradeName", ""),
                        "message": (
                            f"{data['memberId']} 로그인 성공 "
                            f"({data.get('gradeName') or '등급미확인'})"
                        ),
                    }
        except Exception as exc:
            logger.warning(f"[무신사] 쿠키 검증 API 실패 (쿠키는 저장됨): {exc}")

        return {
            "success": True,
            "isLoggedIn": True,
            "message": "쿠키가 설정되었습니다. 수집 시 로그인 여부가 확인됩니다.",
        }

    async def check_stock(self, goods_nos: list[str]) -> dict[str, Any]:
        """재고 소진 감지 - proxy-server.mjs /api/agents/stock-check 포팅."""
        results = []
        timeout = httpx.Timeout(settings.http_timeout_default, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for goods_no in goods_nos:
                try:
                    resp = await client.get(
                        f"{self.BASE_DETAIL}/{goods_no}",
                        headers=self._headers(),
                    )
                    if resp.status_code != 200:
                        results.append(
                            {
                                "goodsNo": goods_no,
                                "error": f"API {resp.status_code}",
                                "isSoldOut": None,
                            }
                        )
                        continue
                    d = resp.json().get("data")
                    if not d:
                        results.append(
                            {
                                "goodsNo": goods_no,
                                "error": "데이터 없음",
                                "isSoldOut": None,
                            }
                        )
                        continue
                    is_sold_out = bool(
                        str(d.get("goodsSaleType", "")).upper()
                        in (
                            "STOP_SALE",
                            "PROHIBITED",
                            "CLOSE",
                            "SOLD_OUT",
                            "STAND_BY_SALE",
                        )
                        or d.get("isSoldOut")
                        or (d.get("goodsPrice") or {}).get("isSoldOut")
                    )
                    _gp_p = d.get("goodsPrice") or {}
                    price = (
                        _gp_p.get("immediateDiscountedPrice")
                        or _gp_p.get("salePrice")
                        or 0
                    )
                    _extra_p = _gp_p.get("extraDiscountAmount", 0) or 0
                    if price > 0 and _extra_p > 0:
                        price = max(0, price - _extra_p)
                    results.append(
                        {
                            "goodsNo": goods_no,
                            "isSoldOut": is_sold_out,
                            "price": price,
                            "name": d.get("goodsName", ""),
                        }
                    )
                except Exception as exc:
                    results.append(
                        {"goodsNo": goods_no, "error": str(exc), "isSoldOut": None}
                    )

        sold_out_count = sum(1 for r in results if r.get("isSoldOut") is True)
        return {"success": True, "results": results, "soldOutCount": sold_out_count}

    async def monitor_prices(self, products: list[dict[str, Any]]) -> dict[str, Any]:
        """가격 변동 감지 - proxy-server.mjs /api/agents/price-monitor 포팅."""
        results = []
        timeout = httpx.Timeout(settings.http_timeout_default, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for p in products:
                goods_no = p.get("goodsNo", "")
                try:
                    resp = await client.get(
                        f"{self.BASE_DETAIL}/{goods_no}",
                        headers=self._headers(),
                    )
                    if resp.status_code != 200:
                        results.append(
                            {
                                "goodsNo": goods_no,
                                "productId": p.get("productId"),
                                "error": f"API {resp.status_code}",
                                "changed": False,
                            }
                        )
                        continue
                    d = resp.json().get("data")
                    if not d:
                        results.append(
                            {
                                "goodsNo": goods_no,
                                "productId": p.get("productId"),
                                "error": "데이터 없음",
                                "changed": False,
                            }
                        )
                        continue
                    gp_inner = d.get("goodsPrice") or {}
                    current_price = (
                        gp_inner.get("immediateDiscountedPrice")
                        or gp_inner.get("salePrice")
                        or 0
                    )
                    _extra_cp = gp_inner.get("extraDiscountAmount", 0) or 0
                    if current_price > 0 and _extra_cp > 0:
                        current_price = max(0, current_price - _extra_cp)
                    stored_price = p.get("storedPrice", 0)
                    diff = current_price - stored_price
                    diff_rate = (
                        round(diff / stored_price * 100) if stored_price > 0 else 0
                    )
                    results.append(
                        {
                            "goodsNo": goods_no,
                            "productId": p.get("productId"),
                            "storedPrice": stored_price,
                            "currentPrice": current_price,
                            "changed": current_price != stored_price,
                            "diff": diff,
                            "diffRate": diff_rate,
                            "name": d.get("goodsName", ""),
                            "isSoldOut": bool(
                                str(d.get("goodsSaleType", "")).upper()
                                in (
                                    "STOP_SALE",
                                    "PROHIBITED",
                                    "CLOSE",
                                    "SOLD_OUT",
                                    "STAND_BY_SALE",
                                )
                                or d.get("isSoldOut")
                                or gp_inner.get("isSoldOut")
                            ),
                        }
                    )
                except Exception as exc:
                    results.append(
                        {
                            "goodsNo": goods_no,
                            "productId": p.get("productId"),
                            "error": str(exc),
                            "changed": False,
                        }
                    )

        changed_count = sum(1 for r in results if r.get("changed"))
        return {"success": True, "results": results, "changedCount": changed_count}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_options(
        self,
        client: httpx.AsyncClient,
        goods_no: str,
        gp: dict[str, Any],
    ) -> tuple[
        list[dict[str, Any]],
        dict[int, int],
        list[dict[str, Any]],
        list[str],
    ]:
        """옵션 + 재고 API 호출.

        반환: (options, option_value_no_map, addon_options, option_group_names)
          - options: 메인 SKU 옵션 (1단 또는 색상×사이즈 같은 다단 매트릭스)
          - addon_options: 추가구성상품(스마트스토어 productAddItems 등) — 메인과 별개 차원
          - option_group_names: 메인 옵션의 그룹명 목록 (예: ["색상"], ["색상","사이즈"])
        """
        option_value_no_map: dict[int, int] = {}
        options: list[dict[str, Any]] = []
        addon_options: list[dict[str, Any]] = []
        option_group_names: list[str] = []

        try:
            opt_resp = await client.get(
                f"{self.BASE_DETAIL}/{goods_no}/options",
                headers=self._headers(),
            )
            if opt_resp.status_code != 200:
                logger.warning(
                    f"[옵션] {goods_no} 옵션 API 비정상 응답: HTTP {opt_resp.status_code}"
                )
                return options, option_value_no_map, addon_options, option_group_names

            opt_json = opt_resp.json()
            opt_meta = opt_json.get("meta", {})
            if opt_meta.get("result") != "SUCCESS" or not opt_json.get("data"):
                logger.warning(
                    f"[옵션] {goods_no} 옵션 API 실패: result={opt_meta.get('result')}, data={bool(opt_json.get('data'))}"
                )
                return options, option_value_no_map, addon_options, option_group_names

            items = opt_json["data"].get("optionItems", [])

            # 메인 옵션 그룹명 — 무신사 응답의 basic[*].name을 차원 순서대로 사용
            # data.basic: [{ no, name(예: "색상"), sequence, optionValues:[...] }, ...]
            main_groups_meta = opt_json["data"].get("basic", []) or []
            # sequence 오름차순으로 정렬 — 차원 순서 보장
            for grp in sorted(
                main_groups_meta, key=lambda g: g.get("sequence", 0) or 0
            ):
                if grp.get("isDeleted"):
                    continue
                gn = (grp.get("name") or "").strip()
                if gn:
                    option_group_names.append(gn)

            # optionValueNo 목록 수집
            all_option_value_nos: list[int] = []
            for item in items:
                for v in item.get("optionValues", []):
                    if v.get("no"):
                        all_option_value_nos.append(v["no"])
                        option_value_no_map[v["no"]] = item.get("no", 0)

            # 재고 API
            inventory_map: dict[int, dict[str, Any]] = {}
            if all_option_value_nos:
                try:
                    inv_resp = await client.post(
                        f"{self.BASE_DETAIL}/{goods_no}/options/v2/prioritized-inventories",
                        headers=self._headers({"Content-Type": "application/json"}),
                        json={"optionValueNos": all_option_value_nos},
                    )
                    if inv_resp.status_code == 200:
                        inv_json = inv_resp.json()
                        if (inv_json.get("meta") or {}).get(
                            "result"
                        ) == "SUCCESS" and isinstance(inv_json.get("data"), list):
                            for inv in inv_json["data"]:
                                opt_item_no = inv.get("productVariantId")
                                if opt_item_no:
                                    _dd = inv.get("domesticDelivery") or {}
                                    inventory_map[opt_item_no] = {
                                        "remainQuantity": inv.get("remainQuantity"),
                                        "outOfStock": inv.get("outOfStock", False),
                                        "isRedirect": inv.get("isRedirect", False),
                                        "deliveryType": _dd.get("deliveryType", ""),
                                        "willReleaseDate": _dd.get(
                                            "willReleaseDate", ""
                                        ),
                                    }
                except Exception as inv_err:
                    logger.warning(
                        f"[재고] {goods_no} 재고 API 실패 (무시): {type(inv_err).__name__}: {inv_err}"
                    )

            # 옵션 정리 — preorder/품절 등 salePrice=0인 경우 normalPrice 폴백
            base_price = (
                gp.get("immediateDiscountedPrice")
                or gp.get("salePrice")
                or gp.get("normalPrice", 0)
            )
            _extra_bp = gp.get("extraDiscountAmount", 0) or 0
            if base_price > 0 and _extra_bp > 0:
                base_price = max(0, base_price - _extra_bp)
            # 품절 상품이라도 normalPrice가 있으면 가격 보존
            if not base_price and gp.get("normalPrice"):
                base_price = gp["normalPrice"]
                logger.info(
                    f"[옵션] {goods_no} base_price=0 → normalPrice {base_price:,} 폴백"
                )
            for item in items:
                if not item.get("activated") or item.get("isDeleted"):
                    continue
                vals = [
                    v.get("name", "")
                    for v in item.get("optionValues", [])
                    if v.get("name")
                ]
                inv = inventory_map.get(item.get("no", 0))

                stock: Optional[int] = 99  # 재고 불명 기본값
                is_sold_out = False
                is_brand_delivery = False

                if inv:
                    is_brand_delivery = inv.get("isRedirect") is True
                    if inv.get("outOfStock") and not is_brand_delivery:
                        stock = 0
                        is_sold_out = True
                    elif is_brand_delivery:
                        stock = 99  # 브랜드직배: 재고 불명 → 99
                        is_sold_out = False
                    elif inv.get("remainQuantity") is not None:
                        stock = inv["remainQuantity"]
                    else:
                        stock = 99  # 재고 수량 불명 → 99

                # 예약배송(MANUAL): 출고일 3일 초과 → 품절 처리
                if not is_sold_out:
                    _dt = (inv or {}).get("deliveryType", "")
                    _wr = (inv or {}).get("willReleaseDate", "")
                    if _dt == "MANUAL" and _wr:
                        try:
                            _KST = timezone(timedelta(hours=9))
                            _today = datetime.now(tz=_KST).date()
                            _release = date.fromisoformat(_wr)
                            _days = (_release - _today).days
                            if _days > 3:
                                stock = 0
                                is_sold_out = True
                                logger.info(
                                    f"[옵션] {goods_no} 예약배송 품절: "
                                    f"출고일={_wr}({_days}일 후)"
                                )
                        except ValueError:
                            pass

                options.append(
                    {
                        "no": item.get("no"),
                        "name": " / ".join(vals) or item.get("managedCode", ""),
                        "price": (base_price or 0) + (item.get("price") or 0),
                        "stock": stock,
                        "isSoldOut": is_sold_out,
                        "isBrandDelivery": is_brand_delivery,
                        "deliveryType": (inv or {}).get("deliveryType", ""),
                        "managedCode": item.get("managedCode", ""),
                    }
                )

            # 의미없는 단일값 축 제거 — FREE/ONE COLOR/ONESIZE 등 한 가지 값만 있는 축
            # 예: [컬러=ONE COLOR, 사이즈=FREE] × 엑스트라 → 불필요한 축 누적으로 마켓 전송 에러
            # 모든 옵션 행에서 값이 동일(unique 1개)인 컬럼을 메인 옵션명/그룹명에서 드롭
            if options and option_group_names:
                n_cols = len(option_group_names)
                vals_per_item: list[list[str]] = []
                for opt in options:
                    parts = (opt.get("name") or "").split(" / ")
                    vals_per_item.append(parts if len(parts) == n_cols else [])
                aligned = [v for v in vals_per_item if v]
                if aligned:
                    drop_idx = [
                        i
                        for i in range(n_cols)
                        if len({v[i].strip().upper() for v in aligned}) == 1
                    ]
                    keep_idx = [i for i in range(n_cols) if i not in drop_idx]
                    # 남는 축이 1개 이상일 때만 드롭 (전부 단일값 축인 상품은 유지)
                    if drop_idx and keep_idx:
                        dropped_names = [option_group_names[i] for i in drop_idx]
                        for opt, parts in zip(options, vals_per_item):
                            if parts:
                                opt["name"] = " / ".join(parts[i] for i in keep_idx)
                        option_group_names = [option_group_names[i] for i in keep_idx]
                        logger.info(
                            f"[옵션] {goods_no} 무의미한 단일값 축 {len(drop_idx)}개 제거 "
                            f"(드롭={dropped_names}) → 그룹={option_group_names}"
                        )

            # extra(추가) 옵션 처리 — 메인 × 엑스트라 2D 조합 SKU로 통합
            # Naver Commerce v2 의 productAddItems 는 inline 등록 안 되므로,
            # 메인×엑스트라를 cartesian 곱으로 2D optionCombinations 만들어
            # 마켓에 색상×스트랩 두 드롭다운으로 노출 (선택안함은 엑스트라의 첫 행)
            extra_groups = opt_json["data"].get("extra", [])
            extra_values: list[dict[str, Any]] = []
            extra_group_name = ""
            for grp in extra_groups:
                if grp.get("isDeleted"):
                    continue
                if not extra_group_name:
                    extra_group_name = (grp.get("name") or "추가").strip()
                is_stock_managed = grp.get("isStockManaged", False)
                # "선택안함" 항목 자동 생성 — Musinsa 응답에 명시 안 돼 있을 수도 있으므로 보장
                has_none = False
                for ev in grp.get("optionValues", []):
                    if not ev.get("activated") or ev.get("isDeleted"):
                        continue
                    ev_name = (ev.get("name") or "").strip()
                    if "선택안함" in ev_name or "선택없음" in ev_name:
                        has_none = True
                        extra_values.insert(
                            0,
                            {
                                "no": ev.get("no"),
                                "name": "선택안함",
                                "add_price": 0,
                                "stock": 99,
                            },
                        )
                        continue
                    add_price = int(ev.get("price") or 0)
                    if not add_price:
                        m = re.search(r"\(\+(\d+)\)", ev_name)
                        if m:
                            add_price = int(m.group(1))
                    ev_stock = (ev.get("quantity") or 99) if is_stock_managed else 99
                    if ev_stock and ev_stock > 9999:
                        ev_stock = 9999
                    extra_values.append(
                        {
                            "no": ev.get("no"),
                            "name": ev_name,
                            "add_price": add_price,
                            "stock": ev_stock,
                        }
                    )
                # 선택안함이 응답에 없으면 0번째로 추가 (필수 아님일 때만)
                is_required = grp.get("isRequired", False) is True
                if not has_none and not is_required:
                    extra_values.insert(
                        0,
                        {
                            "no": None,
                            "name": "선택안함",
                            "add_price": 0,
                            "stock": 99,
                        },
                    )

            if extra_values and options:
                main_options = list(options)
                options = []
                for main in main_options:
                    main_name = main.get("name") or ""
                    for ev in extra_values:
                        ev_name = ev["name"]
                        combo_name = f"{main_name} / {ev_name}"
                        main_stock = main.get("stock")
                        if main_stock is None:
                            combo_stock = ev["stock"]
                        elif ev["stock"] is None:
                            combo_stock = main_stock
                        else:
                            combo_stock = min(main_stock, ev["stock"])
                        options.append(
                            {
                                "no": ev["no"] or main.get("no"),
                                "name": combo_name,
                                "price": (main.get("price") or 0) + ev["add_price"],
                                # 옵션별 추가금액 — 마켓 전송 시 salePrice 보정용
                                "add_price": int(ev["add_price"] or 0),
                                "stock": combo_stock,
                                "isSoldOut": main.get("isSoldOut", False),
                                "isBrandDelivery": main.get("isBrandDelivery", False),
                                "deliveryType": main.get("deliveryType", ""),
                                "managedCode": main.get("managedCode", ""),
                            }
                        )
                # 그룹명에 엑스트라 그룹 추가 (마켓 변환에서 optionGroupName2 로 사용)
                if extra_group_name:
                    option_group_names.append(extra_group_name)
                logger.info(
                    f"[옵션] {goods_no} 2D 조합 생성: "
                    f"메인 {len(main_options)} × 엑스트라 {len(extra_values)} = {len(options)}개, "
                    f"그룹={option_group_names}"
                )

        except Exception as exc:
            logger.warning(
                f"[옵션] {goods_no} 옵션 수집 실패: {type(exc).__name__}: {exc}"
            )

        return options, option_value_no_map, addon_options, option_group_names

    async def _fetch_essential(
        self, client: httpx.AsyncClient, goods_no: str
    ) -> dict[str, str]:
        """상품고시정보 API 호출."""
        essential: dict[str, str] = {}
        try:
            resp = await client.get(
                f"{self.BASE_DETAIL}/{goods_no}/essential",
                headers=self._headers(),
            )
            if resp.status_code != 200:
                logger.warning(
                    f"[고시] {goods_no} essential API {resp.status_code}: {resp.text[:200]}"
                )
                return essential
            ess_json = resp.json()
            if (ess_json.get("meta") or {}).get("result") != "SUCCESS":
                logger.warning(
                    f"[고시] {goods_no} essential API result 실패: {ess_json.get('meta')}"
                )
                return essential

            # 키 이름 변경 대비 — essentials / essentialItems 모두 시도
            raw_data = ess_json.get("data") or {}
            items = (
                raw_data.get("essentials")
                or raw_data.get("essentialItems")
                or raw_data.get("essentialInformations")
                or []
            )
            if not items:
                logger.debug(
                    f"[고시] {goods_no} essential 항목 없음. keys={list(raw_data.keys())}"
                )
                return essential

            for item in items:
                # name/title 키 모두 허용
                name = (item.get("name") or item.get("title") or "").strip()
                # value/content 키 모두 허용
                value = (item.get("value") or item.get("content") or "").strip()
                if not value:
                    continue
                if "소재" in name or "재질" in name:
                    essential["material"] = value
                elif "색상" in name:
                    essential["color"] = value
                elif (
                    ("치수" in name or "사이즈" in name)
                    and "취급" not in name
                    and "주의" not in name
                ):
                    essential["size"] = value
                elif "제조사" in name or "제조자" in name:
                    essential["manufacturer"] = value
                elif "제조국" in name or "원산지" in name:
                    essential["origin"] = value
                elif (
                    ("세탁" in name or "취급" in name or "주의사항" in name)
                    and "치수" not in name
                    and "사이즈" not in name
                ):
                    essential["careInstructions"] = value
                elif "품질보증" in name:
                    essential["qualityGuarantee"] = value

        except Exception as exc:
            logger.warning(
                f"[고시] {goods_no} 고시정보 수집 실패: {exc!r}", exc_info=True
            )

        return essential

    async def _fetch_coupons(
        self,
        client: httpx.AsyncClient,
        goods_no: str,
        d: dict[str, Any],
        s_price: int,
        best_coupon_discount: int,
    ) -> tuple[int, bool, int, int]:
        """쿠폰 API 호출.

        Returns (할인액, API실패여부, 응답_쿠폰갯수, bestSalePriceYn=Y_갯수).
        호출자는 cookie 보유 + couponPrice>0인데 응답 쿠폰 0건이면 비로그인 응답
        의심 신호로 사용 가능.
        """
        coupons_total = 0
        sg_y_total = 0
        try:
            specialty = d.get("specialtyCodes") or []
            params_dict: dict[str, Any] = {
                "goodsNo": goods_no,
                "brand": d.get("brand", ""),
                "comId": d.get("comId", ""),
                "salePrice": s_price,
            }
            if specialty:
                params_dict["specialtyCodes"] = (
                    ",".join(specialty) if isinstance(specialty, list) else specialty
                )
            params = urlencode(params_dict)
            coupon_url = f"{self.BASE_COUPON}?{params}"
            resp = await client.get(coupon_url, headers=self._headers())
            if resp.status_code == 200:
                coupon_json = resp.json()
                coupons = (coupon_json.get("data") or {}).get(
                    "list"
                ) or coupon_json.get("data", [])
                if isinstance(coupons, list):
                    coupons_total = len(coupons)
                    sg_y_total = sum(
                        1 for c in coupons if c.get("bestSalePriceYn") == "Y"
                    )
                    logger.info(
                        f"[쿠폰 응답] {goods_no}: total={coupons_total}, "
                        f"bestSalePriceYn=Y={sg_y_total}, "
                        f"hasCookie={bool(self.cookie)}"
                    )
                    for c in coupons:
                        logger.info(
                            f"[쿠폰 상세] {goods_no}: salePrice={c.get('salePrice')}, "
                            f"discountPrice={c.get('discountPrice')}, "
                            f"couponApply={c.get('couponApply')}, "
                            f"maxLimitQty={c.get('maxLimitQty')}, "
                            f"bestSalePriceYn={c.get('bestSalePriceYn')}, "
                            f"couponNm={c.get('couponNm', '')[:30]}"
                        )
                        # 조건 필터링: 사용 불가 쿠폰 제외
                        if (c.get("maxLimitQty", 0) or 0) > 1:
                            logger.info(
                                f"[쿠폰 스킵] {goods_no}: maxLimitQty={c.get('maxLimitQty')} — 2개 이상 구매 조건"
                            )
                            continue
                        if (c.get("lowPrice", 0) or 0) > s_price:
                            logger.info(
                                f"[쿠폰 스킵] {goods_no}: lowPrice={c.get('lowPrice')} > {s_price} — 최소 금액 미달"
                            )
                            continue
                        best_yn = c.get("bestSalePriceYn")
                        # bestSalePriceYn=Y인 쿠폰만 허용 (타입 무관)
                        # AG Y = 해당 계정 등급에 맞는 최대 회원쿠폰 (무신사가 최대혜택가에 반영 표시)
                        if best_yn != "Y":
                            logger.info(
                                f"[쿠폰 스킵] {goods_no}: bestSalePriceYn={best_yn} — 최대혜택가 미반영 쿠폰"
                            )
                            continue
                        actual_discount = 0
                        c_sale_price = c.get("salePrice", 0) or 0
                        # salePrice 우선 처리
                        if 0 < c_sale_price < s_price:
                            if c_sale_price < s_price * 0.5:
                                actual_discount = c_sale_price  # 작은 값 = 할인금액
                            else:
                                actual_discount = (
                                    s_price - c_sale_price
                                )  # 큰 값 = 적용가
                        elif c.get("discountPrice", 0) and c["discountPrice"] > 0:
                            dp = c["discountPrice"]
                            # discountPrice도 적용가일 수 있으므로 가드 추가
                            if dp < s_price * 0.5:
                                actual_discount = dp  # 작은 값 = 할인금액
                            elif dp < s_price:
                                actual_discount = s_price - dp  # 큰 값 = 적용가
                        if actual_discount > best_coupon_discount:
                            best_coupon_discount = actual_discount
        except Exception as exc:
            logger.warning(f"[쿠폰] {goods_no} API 호출 실패: {exc}")
            return best_coupon_discount, True, coupons_total, sg_y_total

        return best_coupon_discount, False, coupons_total, sg_y_total

    @staticmethod
    def _is_msscdn_banner_url(url: str) -> bool:
        """msscdn URL 중 상품 이미지가 아닌 광고/공지 배너인지 판단.

        상품 이미지 패턴(허용):
          - /images/goods_img/  : 상품 메인 이미지
          - /images/prd_img/.../detail_  : 상품 상세컷
        그 외 msscdn URL(예: /display/images/common/, /images/prd_img/<hash>.jpg)은
        배너성으로 간주.
        """
        if not url:
            return False
        lower = url.lower()
        if "msscdn.net" not in lower:
            return False
        if "/images/goods_img/" in lower:
            return False
        if "/images/prd_img/" in lower and "/detail_" in lower:
            return False
        return True

    @staticmethod
    def _sanitize_desc_html(desc_html: str) -> str:
        """무신사 goodsContents에서 msscdn 광고 배너 <img>를 제거.

        - 타 마켓(11번가/쿠팡 등) 상세설명에 무신사 자사 광고 배너 노출 방지
        - msscdn 외 호스트(cafe24 등)는 다른 경로(이미지 미러)에서 처리하므로 건드리지 않음
        - 배너 <img>를 감싸기만 한 빈 <a>도 함께 제거
        """
        if not desc_html:
            return desc_html
        try:
            from bs4 import BeautifulSoup
        except Exception as e:  # pragma: no cover
            logger.warning(f"[무신사 sanitize] bs4 로드 실패, 원본 반환: {e}")
            return desc_html

        try:
            soup = BeautifulSoup(desc_html, "html.parser")
        except Exception as e:
            logger.warning(f"[무신사 sanitize] HTML 파싱 실패, 원본 반환: {e}")
            return desc_html

        removed = 0
        for img in list(soup.find_all("img")):
            src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy")
                or img.get("data-original")
                or ""
            )
            if not MusinsaClient._is_msscdn_banner_url(src):
                continue
            parent = img.parent
            img.decompose()
            removed += 1
            # 배너만 감쌌던 <a>는 함께 제거
            if (
                parent is not None
                and getattr(parent, "name", None) == "a"
                and not parent.find(True)
                and not (parent.get_text() or "").strip()
            ):
                parent.decompose()

        if removed:
            logger.info(f"[무신사 sanitize] msscdn 배너 {removed}개 제거")
        return str(soup)

    @staticmethod
    def _extract_detail_images(desc_html: str) -> list[str]:
        """상세 HTML에서 이미지 URL 추출.

        무신사 desc_html은 API 응답으로 대체로 정규화되어 있지만,
        일부 상품은 lazy-load(data-src/data-lazy)가 섞여있어 함께 처리.
        """
        detail_images: list[str] = []
        pattern = re.compile(
            r'<img[^>]+(?:src|data-src|data-lazy|data-original)=["\']([^"\']+)["\']',
            re.I,
        )
        for match in pattern.finditer(desc_html):
            src = MusinsaClient._to_image_url(match.group(1))
            if (
                src
                and "icon" not in src
                and "btn_" not in src
                and MusinsaClient._is_valid_image_url(src)
            ):
                if src not in detail_images:
                    detail_images.append(src)
        return detail_images

    # ------------------------------------------------------------------
    # 주문 관련 (소비자 원주문 취소)
    # ------------------------------------------------------------------

    async def _get_order_option_nos(self, order_no: str) -> list[str]:
        """주문의 orderOptionNo 목록 추출 (API → HTML 순서)."""
        import json as _json

        timeout = httpx.Timeout(15.0, connect=10.0)
        headers = self._headers()

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            # 1) order 도메인 API로 주문 상세 조회
            _DETAIL_APIS = [
                f"https://order.musinsa.com/api2/order/v1/orders/{order_no}",
                f"https://order.musinsa.com/api2/order/v1/order-detail/{order_no}",
                f"https://order.musinsa.com/api2/order/v1/{order_no}",
                f"https://api.musinsa.com/api2/order/store/mypage/{order_no}",
                f"https://api.musinsa.com/api2/claim/store/mypage/order/{order_no}",
            ]
            for url in _DETAIL_APIS:
                try:
                    resp = await client.get(url, headers=headers)
                    logger.info(f"[무신사 옵션조회] GET {url} → {resp.status_code}")
                    if resp.status_code in (400, 500):
                        logger.info(f"[무신사 옵션조회] 응답 body: {resp.text[:300]}")
                    if resp.status_code == 200:
                        data = resp.json()
                        logger.info(
                            f"[무신사 옵션조회] 응답 키: {list(data.keys()) if isinstance(data, dict) else type(data)}"
                        )
                        # JSON 전체에서 orderOptionNo 재귀 탐색
                        nos: set[str] = set()

                        def _find(obj: Any) -> None:
                            if isinstance(obj, dict):
                                for k, v in obj.items():
                                    if (
                                        k
                                        in (
                                            "orderOptionNo",
                                            "orderOptionId",
                                            "optionNo",
                                        )
                                        and v
                                    ):
                                        nos.add(str(v))
                                    else:
                                        _find(v)
                            elif isinstance(obj, list):
                                for item in obj:
                                    _find(item)

                        _find(data)
                        if nos:
                            logger.info(f"[무신사 옵션조회] API에서 추출: {nos}")
                            return list(nos)
                except Exception as e:
                    logger.warning(f"[무신사 옵션조회] {url} 실패: {e}")

            # 2) 주문 상세 HTML 페이지에서 추출
            try:
                resp = await client.get(
                    f"https://www.musinsa.com/order/order-detail/{order_no}",
                    headers=self._headers({"Accept": "text/html"}),
                )
                if resp.status_code == 200:
                    html = resp.text
                    logger.info(
                        f"[무신사 옵션조회] HTML 길이: {len(html)}, __NEXT_DATA__ 포함: {'__NEXT_DATA__' in html}"
                    )
                    # __NEXT_DATA__ 파싱
                    match = re.search(
                        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
                    )
                    if match:
                        try:
                            next_data = _json.loads(match.group(1))
                            logger.info(
                                f"[무신사 옵션조회] __NEXT_DATA__ props 키: {list(next_data.get('props', {}).get('pageProps', {}).keys())}"
                            )
                            logger.info(
                                f"[무신사 옵션조회] __NEXT_DATA__ query: {next_data.get('query', {})}"
                            )
                            nos2: set[str] = set()

                            def _find2(obj: Any) -> None:
                                if isinstance(obj, dict):
                                    for k, v in obj.items():
                                        if (
                                            k
                                            in (
                                                "orderOptionNo",
                                                "orderOptionId",
                                                "optionNo",
                                            )
                                            and v
                                        ):
                                            nos2.add(str(v))
                                        else:
                                            _find2(v)
                                elif isinstance(obj, list):
                                    for item in obj:
                                        _find2(item)

                            _find2(next_data)
                            if nos2:
                                logger.info(
                                    f"[무신사 옵션조회] __NEXT_DATA__에서 추출: {nos2}"
                                )
                                return list(nos2)
                        except Exception as e:
                            logger.warning(
                                f"[무신사 옵션조회] __NEXT_DATA__ 파싱 실패: {e}"
                            )
                    # fallback: HTML에서 숫자 패턴
                    option_nos = re.findall(rf"/{order_no}/(\d{{6,12}})", html)
                    if option_nos:
                        logger.info(
                            f"[무신사 옵션조회] HTML 패턴에서 추출: {set(option_nos)}"
                        )
                        return list(set(option_nos))
            except Exception as e:
                logger.warning(f"[무신사 옵션조회] HTML 페이지 실패: {e}")

        logger.warning(f"[무신사 옵션조회] orderOptionNo를 찾을 수 없음: {order_no}")
        return []

    async def cancel_order(
        self, order_no: str, reason: str = "단순변심"
    ) -> dict[str, Any]:
        """무신사 원주문 취소 (소비자 주문취소).

        확정 API: GET /api2/claim/store/mypage/order/cancel/voucher/refund/complete/{주문번호}?orderOptionNoList={옵션번호}
        일반상품: GET /api2/claim/store/mypage/order/cancel/refund/complete/{주문번호}?orderOptionNoList={옵션번호}
        """
        if not self.cookie:
            raise ValueError("무신사 로그인(쿠키)이 필요합니다.")

        # 1) orderOptionNo 추출
        option_nos = await self._get_order_option_nos(order_no)
        if not option_nos:
            raise ValueError(
                f"주문 {order_no}의 상품옵션번호를 찾을 수 없습니다. 주문 상세 페이지를 확인해주세요."
            )

        option_list = ",".join(option_nos)
        logger.info(f"[무신사 주문취소] 주문={order_no}, 옵션={option_list}")

        # 2) 취소 API 호출 (바우처/일반 순서로 시도)
        _CANCEL_URLS = [
            f"https://api.musinsa.com/api2/claim/store/mypage/order/cancel/voucher/refund/complete/{order_no}?orderOptionNoList={option_list}",
            f"https://api.musinsa.com/api2/claim/store/mypage/order/cancel/refund/complete/{order_no}?orderOptionNoList={option_list}",
        ]

        timeout = httpx.Timeout(15.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for url in _CANCEL_URLS:
                try:
                    resp = await client.get(url, headers=self._headers())
                    logger.info(f"[무신사 주문취소] GET {url} → {resp.status_code}")
                    if resp.status_code == 200:
                        data = resp.json() if resp.text else {}
                        logger.info(f"[무신사 주문취소] 성공: {data}")
                        return {
                            "ok": True,
                            "message": "무신사 주문취소 완료",
                            "data": data,
                        }
                    elif resp.status_code == 400:
                        body = resp.text[:500]
                        logger.warning(f"[무신사 주문취소] 400 응답: {body}")
                        return {"ok": False, "message": f"취소 요청 거부: {body}"}
                    else:
                        logger.info(f"[무신사 주문취소] {resp.status_code} → 다음 시도")
                except Exception as e:
                    logger.warning(f"[무신사 주문취소] {url} 실패: {e}")
                    continue

        raise ValueError(f"무신사 주문취소 실패: {order_no} (모든 API 시도 실패)")

    # ------------------------------------------------------------------
    # 송장(배송) 정보 조회 — POST /order-service/my/delivery/deliveryInfo
    # ------------------------------------------------------------------

    async def fetch_tracking(
        self, ord_no: str, ord_opt_no: str, is_return: str = "0"
    ) -> dict[str, Any]:
        """무신사 마이페이지 deliveryInfo API 직접 호출.

        쿠키 + ord_no + ord_opt_no 만 있으면 백엔드에서 송장 정보 fetch 가능
        (확장앱 탭 폴링 불필요).

        Returns:
          {
            "ok": True,
            "courier": "우체국택배",
            "courierCode": "EPOST",
            "trackingNumber": "6062613923874",
            "state": "ongoing",
            "lastTraceText": "안성우체국에서 이동 중입니다",
          }
          또는 실패 시 {"ok": False, "error": "..."}
        """
        url = "https://www.musinsa.com/order-service/my/delivery/deliveryInfo"
        headers = self._headers(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://www.musinsa.com",
                "Referer": (
                    f"https://www.musinsa.com/order-service/my/delivery/trace"
                    f"?ord_no={ord_no}&ord_opt_no={ord_opt_no}&is_return={is_return}"
                ),
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
        )
        payload = {
            "ord_no": ord_no,
            "ord_opt_no": ord_opt_no,
            "is_return": is_return,
        }
        timeout = httpx.Timeout(15.0, connect=10.0)
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True
            ) as client:
                resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                return {
                    "ok": False,
                    "error": f"HTTP {resp.status_code}",
                    "body": resp.text[:200],
                }
            data = resp.json()
            meta = data.get("meta") or {}
            if meta.get("result") != "SUCCESS":
                return {
                    "ok": False,
                    "error": meta.get("message") or "deliveryInfo FAIL",
                    "errorCode": meta.get("errorCode"),
                }
            d = data.get("data") or {}
            tracking = (d.get("dlvNo") or "").strip()
            courier = (d.get("deliverName") or "").strip()
            if not tracking:
                return {
                    "ok": False,
                    "error": "no_tracking",
                    "courier": courier,
                    "state": d.get("state"),
                }
            return {
                "ok": True,
                "courier": courier,
                "courierCode": (d.get("deliverCode") or "").strip(),
                "trackingNumber": tracking,
                "state": d.get("state"),
                "lastTraceText": d.get("titleText"),
            }
        except Exception as exc:
            logger.warning(
                f"[무신사 송장fetch] order={ord_no} opt={ord_opt_no} 실패: {exc}"
            )
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # 브랜드 카테고리 스캔
    # ------------------------------------------------------------------

    async def scan_brand_categories(
        self,
        brand: str,
        gf: str = "A",
        keyword: str = "",
        include_sold_out: bool = False,
    ) -> list[dict[str, Any]]:
        """브랜드의 최하위 카테고리 목록 + 상품 수 반환.

        무신사 필터 API로 대>중분류를 가져온 뒤,
        각 중분류에 대해 소분류를 재귀 탐색하여 최하위 카테고리별 상품 수를 집계한다.
        """
        timeout = httpx.Timeout(30.0, connect=10.0)
        base_params: dict[str, str] = {
            "caller": "SEARCH",
            "keyword": keyword or brand,
            "brand": brand,
            "gf": gf,
        }
        if include_sold_out:
            base_params["includeSoldOut"] = "1"

        async with httpx.AsyncClient(timeout=timeout) as client:
            # 1) 필터 API로 대>중분류 가져오기
            resp = await client.get(
                "https://api.musinsa.com/api2/dp/v1/plp/filter",
                params=base_params,
                headers=self._headers(),
            )
            resp.raise_for_status()
            cats = (
                resp.json()
                .get("data", {})
                .get("detail", {})
                .get("category", {})
                .get("list", [])
            )

            results: list[dict[str, Any]] = []

            # 1단계에서 받은 모든 중분류 코드 수집 (형제 판별용)
            all_mid_codes: set[str] = set()
            for cat in cats:
                for sub in cat.get("categoryList", []):
                    code = sub.get("value", "")
                    if code:
                        all_mid_codes.add(code)

            for cat in cats:
                big_name = cat.get("displayText", "")
                big_code = cat.get("value", "")
                subs = cat.get("categoryList", [])

                for sub in subs:
                    mid_name = sub.get("displayText", "")
                    mid_code = sub.get("value", "")

                    # 2) 중분류 선택 후 소분류 필터 확인
                    resp2 = await client.get(
                        "https://api.musinsa.com/api2/dp/v1/plp/filter",
                        params={**base_params, "category": mid_code},
                        headers=self._headers(),
                    )
                    await asyncio.sleep(0.3)
                    sub_cats = []
                    if resp2.status_code == 200:
                        for d1 in (
                            resp2.json()
                            .get("data", {})
                            .get("detail", {})
                            .get("category", {})
                            .get("list", [])
                        ):
                            sub_cats.extend(d1.get("categoryList", []))

                    # 형제 카테고리 제거 → 진짜 소분류만 남김
                    real_sub_cats = [
                        s for s in sub_cats if s.get("value", "") not in all_mid_codes
                    ]

                    if real_sub_cats:
                        # 진짜 소분류별 상품 수 조회
                        for small in real_sub_cats:
                            small_name = small.get("displayText", "")
                            small_code = small.get("value", "")
                            resp3 = await client.get(
                                "https://api.musinsa.com/api2/dp/v1/plp/goods",
                                params={
                                    **base_params,
                                    "category": small_code,
                                    "page": "1",
                                    "size": "1",
                                },
                                headers=self._headers(),
                            )
                            await asyncio.sleep(0.3)
                            cnt = 0
                            if resp3.status_code == 200:
                                cnt = (
                                    resp3.json()
                                    .get("data", {})
                                    .get("pagination", {})
                                    .get("totalCount", 0)
                                )
                            if cnt > 0:
                                actual_cat3 = (
                                    "" if small_name == mid_name else small_name
                                )
                                path = (
                                    f"{big_name} > {mid_name} > {small_name}"
                                    if actual_cat3
                                    else f"{big_name} > {mid_name}"
                                )
                                results.append(
                                    {
                                        "category1": big_name,
                                        "category2": mid_name,
                                        "category3": actual_cat3,
                                        "categoryCode": small_code,
                                        "path": path,
                                        "count": cnt,
                                    }
                                )
                    else:
                        # 소분류 없음(형제만 있었거나 빈 응답) → 중분류 직접 조회
                        resp3 = await client.get(
                            "https://api.musinsa.com/api2/dp/v1/plp/goods",
                            params={
                                **base_params,
                                "category": mid_code,
                                "page": "1",
                                "size": "1",
                            },
                            headers=self._headers(),
                        )
                        await asyncio.sleep(0.3)
                        cnt = 0
                        if resp3.status_code == 200:
                            cnt = (
                                resp3.json()
                                .get("data", {})
                                .get("pagination", {})
                                .get("totalCount", 0)
                            )
                        if cnt > 0:
                            results.append(
                                {
                                    "category1": big_name,
                                    "category2": mid_name,
                                    "category3": "",
                                    "categoryCode": mid_code,
                                    "path": f"{big_name} > {mid_name}",
                                    "count": cnt,
                                }
                            )

            # 상품 수 내림차순 정렬
            results.sort(key=lambda x: -x["count"])
            total = sum(r["count"] for r in results)
            logger.info(
                f"[무신사 브랜드스캔] {brand}: {len(results)}개 카테고리, 총 {total}건"
            )
            return results

    async def search_brands(
        self,
        keyword: str,
        gf: str = "A",
    ) -> list[dict[str, str]]:
        """키워드로 무신사 브랜드 코드/이름 검색.

        필터 API를 호출하여 매칭되는 브랜드 목록을 반환한다.
        """
        timeout = httpx.Timeout(15.0, connect=10.0)
        params = {
            "caller": "SEARCH",
            "keyword": keyword,
            "gf": gf,
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                "https://api.musinsa.com/api2/dp/v1/plp/filter",
                params=params,
                headers=self._headers(),
            )
            resp.raise_for_status()
            detail = resp.json().get("data", {}).get("detail", {})
            brand_data = detail.get("brand", {}).get("list", [])
            results = []
            for b in brand_data:
                code = b.get("value", "")
                name = b.get("displayText", "")
                if code:
                    results.append({"brandCode": code, "brandName": name})
            logger.info(f"[무신사 브랜드검색] '{keyword}': {len(results)}개 브랜드")
            return results

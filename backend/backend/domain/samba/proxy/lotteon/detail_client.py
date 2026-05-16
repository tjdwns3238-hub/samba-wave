"""롯데ON 상세 클라이언트 믹스인.

상세 페이지 조회 및 PBF API 관련 메서드를 제공한다.
쿠키 캐시(최대혜택가 API용)도 이 모듈에서 관리한다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from backend.domain.samba.proxy.lotteon.search_client import RateLimitError
from backend.utils.logger import logger


# ── 롯데ON 쿠키 캐시 (확장앱→서버 동기화 후 benefits API에 사용) ──
_lotteon_cookie_cache: str = ""


def set_lotteon_cookie(cookie: str) -> None:
    """확장앱에서 수신한 롯데ON 쿠키를 모듈 캐시에 설정."""
    global _lotteon_cookie_cache
    _lotteon_cookie_cache = cookie


class DetailClientMixin:
    """상세 HTTP 클라이언트 메서드 믹스인."""

    # 하위 클래스에서 정의되는 상수 (타입 힌트용)
    PRODUCT_URL: str
    PBF_BASE: str
    HEADERS: dict[str, str]
    proxy_url: Optional[str]

    def _timeout_obj(self) -> httpx.Timeout:
        """타임아웃 객체 반환 (하위 클래스의 self._timeout 참조)."""
        return self._timeout  # type: ignore[attr-defined]

    def _httpx_kwargs(self, **extra: Any) -> dict[str, Any]:  # type: ignore[override]
        """LotteonSourcingClient에서 오버라이드 — 믹스인 단독 사용 시 폴백."""
        return dict(extra)

    @staticmethod
    def _is_transient_5xx(status: int) -> bool:
        """롯데ON WAF/엣지의 일시적 차단/장애 응답 — 재시도 대상."""
        return status in (502, 503, 504)

    @staticmethod
    def _log_5xx_headers(resp: "httpx.Response", ctx: str) -> None:
        """502/503/504 응답 헤더 1줄 로깅 — WAF 식별용 (Server/Akamai 등)."""
        try:
            _server = resp.headers.get("Server", "")
            _akamai = resp.headers.get("X-Akamai-Request-ID", "") or resp.headers.get(
                "Akamai-True-Client-IP", ""
            )
            _ray = resp.headers.get("CF-RAY", "")
            logger.warning(
                f"[LOTTEON] 5xx 헤더 {ctx}: status={resp.status_code} "
                f"Server={_server!r} Akamai={_akamai!r} CF-RAY={_ray!r}"
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 상세 조회
    # ------------------------------------------------------------------

    async def get_product_detail(
        self, product_no: str, refresh_only: bool = False
    ) -> dict[str, Any]:
        """롯데ON 상품 상세 정보 조회.

        1단계: 상품 페이지 HTML → JSON-LD로 기본 정보 파싱
        2단계: HTML에서 sitmNo 추출 → pbf.lotteon.com API로 옵션/재고/이미지 보완

        Args:
          product_no: 롯데ON 상품 번호 (LO/PD/LI/LE prefix)
          refresh_only: True이면 가격/재고만 빠르게 갱신

        Returns:
          표준 상품 상세 dict

        Raises:
          RateLimitError: 429/403 응답 시
        """
        url = f"{self.PRODUCT_URL}/{product_no}"
        logger.info(f"[LOTTEON] 상세 조회: {product_no}")

        try:
            async with httpx.AsyncClient(
                **self._httpx_kwargs(
                    timeout=self._timeout_obj(), follow_redirects=True
                )
            ) as client:
                resp = await client.get(url, headers=self.HEADERS)

                # 차단 감지
                if resp.status_code in (429, 403):
                    retry_after = int(resp.headers.get("Retry-After", "60"))
                    logger.warning(
                        f"[LOTTEON] 차단 감지 HTTP {resp.status_code}: {product_no}"
                    )
                    raise RateLimitError(resp.status_code, retry_after)

                # WAF/엣지 일시 차단(502/503/504) — 재시도 대상으로 변환
                if self._is_transient_5xx(resp.status_code):
                    self._log_5xx_headers(resp, f"detail/{product_no}")
                    retry_after = int(resp.headers.get("Retry-After", "10"))
                    raise RateLimitError(resp.status_code, max(retry_after, 5))

                if resp.status_code != 200:
                    logger.warning(
                        f"[LOTTEON] 상세 페이지 HTTP {resp.status_code}: {product_no}"
                    )
                    return {}

                html = resp.text
                now_iso = datetime.now(tz=timezone.utc).isoformat()
                timestamp = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

                # 방법 1: JSON-LD(schema.org Product) 우선 파싱
                detail = self._parse_json_ld_detail(  # type: ignore[attr-defined]
                    html, product_no, now_iso, timestamp
                )
                if detail:
                    self._enrich_from_html(detail, html)  # type: ignore[attr-defined]
                else:
                    # 방법 2: __NEXT_DATA__에서 파싱
                    detail = self._parse_next_data_detail(  # type: ignore[attr-defined]
                        html, product_no, now_iso, timestamp
                    )
                    if not detail:
                        # 방법 3: 메타 태그 + HTML 폴백
                        detail = self._parse_meta_detail(  # type: ignore[attr-defined]
                            html, product_no, now_iso, timestamp
                        )

                if not detail:
                    return {}

                # 2단계: pbf API로 옵션/재고/이미지 보완 + artlInfo 파싱
                sitm_no = self._extract_sitmno_from_html(html)  # type: ignore[attr-defined]
                # sitmNo를 반환 dict에 포함 (소싱 플러그인 캐시 저장용)
                if sitm_no:
                    detail["sitmNo"] = sitm_no
                pd_no_from_pbf = ""
                if sitm_no:
                    pbf_data = await self._fetch_pbf_detail(sitm_no, client)
                    if pbf_data:
                        self._enrich_from_pbf(detail, pbf_data)  # type: ignore[attr-defined]
                        # sitm 응답에도 artlInfo가 포함됨 — 고시정보 파싱
                        self._enrich_from_pbf_pd(detail, pbf_data)  # type: ignore[attr-defined]
                        # pdNo 추출 (3단계 폴백용)
                        pd_no_from_pbf = str(
                            (pbf_data.get("basicInfo") or {}).get("pdNo", "")
                        ).strip()
                        logger.info(
                            f"[LOTTEON] pbf 보완 완료: {product_no} (sitmNo={sitm_no}, pdNo={pd_no_from_pbf})"
                        )

                        # ── 최대혜택가 보강: refresh 경로와 동일하게 benefits + qapi 적용 ──
                        # 수집 시점에도 AG쿠폰/카드 즉시할인 등 실제 최대혜택가를 cost에 반영하기 위함.
                        # refresh의 _fetch_pbf_refresh / qapi 보정 로직 미러링.
                        import asyncio as _asyncio

                        _spd_no = sitm_no.split("_")[0] if "_" in sitm_no else sitm_no
                        try:
                            _benefit, _qapi = await _asyncio.gather(
                                self.fetch_benefit_price(  # type: ignore[attr-defined]
                                    pbf_data, spd_no=_spd_no, sitm_no=sitm_no
                                ),
                                self.fetch_qapi_price(_spd_no),  # type: ignore[attr-defined]
                                return_exceptions=True,
                            )
                            if isinstance(_benefit, Exception):
                                logger.debug(
                                    f"[LOTTEON] benefits 예외: {product_no} — {_benefit}"
                                )
                                _benefit = None
                            if isinstance(_qapi, Exception):
                                logger.debug(
                                    f"[LOTTEON] qapi 예외: {product_no} — {_qapi}"
                                )
                                _qapi = None

                            if _benefit and _benefit > 0:
                                detail["bestBenefitPrice"] = int(_benefit)
                                logger.info(
                                    f"[LOTTEON] benefits 최대혜택가 적용: {product_no} → {int(_benefit):,}"
                                )

                            if _qapi:
                                _final = int(_qapi.get("final", 0) or 0)
                                _original = int(_qapi.get("original", 0) or 0)
                                _pbf_sale = int(detail.get("salePrice") or 0)
                                if _final > 0 and _final < _pbf_sale:
                                    detail["salePrice"] = _final
                                    _existing_benefit = int(
                                        detail.get("bestBenefitPrice") or 0
                                    )
                                    if (
                                        _existing_benefit <= 0
                                        or _existing_benefit >= _final
                                    ):
                                        detail["bestBenefitPrice"] = _final
                                    if _original > 0:
                                        detail["originalPrice"] = _original
                                    logger.info(
                                        f"[LOTTEON] qapi 프로모션가 보정: {product_no} "
                                        f"pbf={_pbf_sale:,} → final={_final:,}, "
                                        f"bestBenefit={detail.get('bestBenefitPrice', 0):,}"
                                    )

                            # 옵션 가격을 실제 판매가/혜택가로 통일 (sl_prc 정가 대신)
                            _effective = int(
                                detail.get("bestBenefitPrice") or 0
                            ) or int(detail.get("salePrice") or 0)
                            if _effective > 0 and detail.get("options"):
                                for _opt in detail["options"]:
                                    _opt["price"] = _effective
                        except Exception as _be:
                            logger.debug(
                                f"[LOTTEON] 최대혜택가 보강 실패: {product_no} — {_be}"
                            )
                    else:
                        logger.debug(f"[LOTTEON] pbf 데이터 없음: {sitm_no}")
                else:
                    logger.debug(f"[LOTTEON] sitmNo 추출 실패: {product_no}")

                # 3단계: artlInfo가 아직 비어있으면 pd API로 재시도
                # sitm에서 이미 artlInfo를 파싱했으면 스킵
                if not detail.get("origin") and not detail.get("manufacturer"):
                    # pdNo: sitm basicInfo에서 추출 또는 PD 접두사 상품번호
                    pd_no = pd_no_from_pbf or (
                        product_no if product_no.startswith("PD") else ""
                    )
                    if pd_no:
                        pd_data = await self._fetch_pbf_pd_detail(pd_no, client)
                        if pd_data:
                            self._enrich_from_pbf_pd(detail, pd_data)  # type: ignore[attr-defined]

                # 4단계: pbf API에 없는 필드(품번/시즌/성별)는 상품명·브랜드·카테고리에서 폴백 추출
                _name = detail.get("name") or ""
                if not detail.get("style_code"):
                    _sc = self._extract_style_code_from_name(_name)  # type: ignore[attr-defined]
                    if _sc:
                        detail["style_code"] = _sc
                if not detail.get("season"):
                    _ss = self._extract_season_from_name(_name)  # type: ignore[attr-defined]
                    if _ss:
                        detail["season"] = _ss
                if not detail.get("sex"):
                    _sx = self._infer_sex(  # type: ignore[attr-defined]
                        _name,
                        detail.get("brand", ""),
                        detail.get("category1", ""),
                        detail.get("category", ""),
                    )
                    if _sx:
                        detail["sex"] = _sx

                # 임시 폴백 키 정리
                detail.pop("_scatCategoryFallback", None)

                # 최종 salePrice/freeShipping 기반 배송비 재계산 (롯데온 임시 정책 폴백)
                # - pbf 보완으로 salePrice가 갱신된 뒤 계산해야 정확
                # - Follow-up: pbf/__NEXT_DATA__에서 실제 배송비 필드 파싱으로 대체 예정
                from backend.domain.samba.proxy.lotteon.detail_parsers import (
                    _lotteon_shipping_fee,
                )

                detail["shipping_fee"] = _lotteon_shipping_fee(
                    detail.get("salePrice"),
                    bool(detail.get("freeShipping", False)),
                )

                return detail

        except RateLimitError:
            raise
        except httpx.TimeoutException:
            logger.error(f"[LOTTEON] 상세 조회 타임아웃: {product_no}")
            return {}
        except Exception as e:
            logger.error(f"[LOTTEON] 상세 조회 실패: {product_no} — {e}")
            return {}

    async def get_detail(self, product_id: str) -> dict[str, Any]:
        """worker.py get_detail 패턴 호환 래퍼 — get_product_detail() 결과 반환."""
        return await self.get_product_detail(product_id)

    # 공유 httpx 클라이언트 — refresh 빠른경로에서 커넥션 풀 재사용
    # proxy_url별로 별도 클라이언트를 캐시 (None = 메인 IP). 무신사
    # _get_musinsa_shared_client 패턴 미러.
    _pbf_shared_clients: Dict[Optional[str], httpx.AsyncClient] = {}

    async def _get_pbf_client(self) -> httpx.AsyncClient:
        """pbf refresh용 공유 클라이언트 (proxy_url별 커넥션 풀 재사용)."""
        key = self.proxy_url
        existing = DetailClientMixin._pbf_shared_clients.get(key)
        if existing is not None and not existing.is_closed:
            return existing
        kw = self._httpx_kwargs(
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        new_client = httpx.AsyncClient(**kw)
        DetailClientMixin._pbf_shared_clients[key] = new_client
        return new_client

    async def fetch_pbf_standalone(self, sitm_no: str) -> Optional[dict[str, Any]]:
        """pbf.lotteon.com API — 공유 클라이언트로 커넥션 풀 재사용."""
        client = await self._get_pbf_client()
        return await self._fetch_pbf_detail(sitm_no, client)

    async def fetch_qapi_price(self, spd_no: str) -> Optional[dict[str, Any]]:
        """qapi 검색으로 프로모션 최종가 조회 — productId 매칭.

        pbf API의 slPrc는 정가(할인 전)를 반환하므로,
        qapi의 priceInfo[type=final]로 실제 프로모션가를 가져온다.

        Returns:
          {"original": 81750, "final": 65400, "card_discount": 5} 또는 None
        """
        # 상품번호에서 검색 키워드 추출 불가 → 상품번호 직접 검색
        try:
            url = (
                f"{self.SEARCH_URL}?render=qapi&platform=pc"  # type: ignore[attr-defined]
                f"&collection_id=201&q={spd_no}&mallId=2&u2=0&u3=5"
            )
            qapi_headers = {**self.HEADERS, "Accept": "application/json, */*"}
            async with httpx.AsyncClient(
                **self._httpx_kwargs(
                    timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True
                )
            ) as client:
                resp = await client.get(url, headers=qapi_headers)
                if self._is_transient_5xx(resp.status_code):
                    self._log_5xx_headers(resp, f"qapi/{spd_no}")
                if resp.status_code != 200:
                    return None
                data = resp.json()
                items = data.get("itemList", [])
                # productId로 정확 매칭
                for item in items:
                    pid = item.get("productId", "")
                    if pid == spd_no:
                        price_map: dict[str, int] = {}
                        for p in item.get("priceInfo", []):
                            price_map[p.get("type", "")] = p.get("num", 0)
                        return {
                            "original": price_map.get("original", 0),
                            "final": price_map.get("final", 0),
                        }
        except Exception as e:
            logger.debug(f"[LOTTEON] qapi 가격 조회 실패: {spd_no} — {e}")
        return None

    async def fetch_option_stock(
        self,
        pbf_data: dict[str, Any],
        spd_no: str = "",
        sitm_no: str = "",
    ) -> Optional[list[dict[str, Any]]]:
        """option/mapping API로 옵션별 실재고 조회 (탭/DOM 불필요).

        Returns:
          [{"name": "250", "stock": 6, "isSoldOut": False}, ...] 또는 None
        """
        basic = pbf_data.get("basicInfo") or {}
        spd_no = spd_no or str(basic.get("spdNo", "") or "").strip()
        sitm_no = sitm_no or str(basic.get("sitmNo", "") or "").strip()
        tr_no = str(basic.get("trNo", "") or "").strip()
        tr_grp_cd = str(basic.get("trGrpCd", "") or "").strip()
        lrtr_no = str(basic.get("lrtrNo", "") or "").strip()
        pd_no = str(basic.get("pdNo", "") or spd_no).strip()
        if not spd_no or not sitm_no:
            return None

        url = (
            f"{self.PBF_BASE}/product/v2/detail/option/mapping"
            f"/{spd_no}/{sitm_no}"
            f"?trNo={tr_no}&trGrpCd={tr_grp_cd}"
            f"&lrtrNo={lrtr_no}&pdNo={pd_no}"
        )
        try:
            client = await self._get_pbf_client()
            resp = await client.get(
                url,
                headers={
                    **self.HEADERS,
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://www.lotteon.com",
                },
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
            if str(body.get("returnCode")) != "200":
                return None

            data = body.get("data") or {}
            opt_info = data.get("optionInfo") or {}
            opt_list = opt_info.get("optionList") or []
            mapping = opt_info.get("optionMappingInfo") or {}
            if not opt_list or not mapping:
                return None

            # optionList → mapping에서 stkQty 조회
            # 1D: key = value ("100(M)100(M)")
            # 2D: key = "v1_v2" ("블랙블랙_100(M)100(M)")  ← 2차원 옵션(색상×사이즈)
            #   실측 확인 2026-04-24: LE1219878538 기준 optionMappingInfo 키가
            #   단순 flat 순회(각 group value 단독)로는 miss → 모든 옵션 stkQty=0으로
            #   떨어져 리프레시 시 전 옵션 품절 승격 버그 발생. 조합 key 경로 분기.
            options: list[dict[str, Any]] = []
            price_info = pbf_data.get("priceInfo") or {}
            sl_prc = self._safe_int(price_info.get("slPrc", 0))  # type: ignore[attr-defined]

            if len(opt_list) >= 2:
                g1_opts = opt_list[0].get("options", [])
                g2_opts = opt_list[1].get("options", [])
                for g1 in g1_opts:
                    for g2 in g2_opts:
                        v1 = str(g1.get("value", ""))
                        v2 = str(g2.get("value", ""))
                        combined_key = f"{v1}_{v2}"
                        # optionMappingInfo는 sparse 가능 — 없는 키는
                        # "판매하지 않는 조합"이지 품절이 아니므로 스킵.
                        # full Cartesian을 전부 방출하면 팔지도 않는 유령 품절
                        # 옵션이 DB에 박힌다.
                        if combined_key not in mapping:
                            continue
                        m = mapping.get(combined_key) or {}
                        l1 = g1.get("label", "").strip()
                        l2 = g2.get("label", "").strip()
                        disabled = bool(g1.get("disabled", False)) or bool(
                            g2.get("disabled", False)
                        )
                        combined_label = f"{l1} / {l2}".strip(" /")
                        stk_qty = int(m.get("stkQty", 0) or 0)
                        is_sold_out = disabled or stk_qty == 0
                        options.append(
                            {
                                "name": combined_label,
                                "price": sl_prc,
                                "stock": 0 if is_sold_out else stk_qty,
                                "isSoldOut": is_sold_out,
                            }
                        )
            else:
                for group in opt_list:
                    for opt in group.get("options", []):
                        label = opt.get("label", "").strip()
                        value = str(opt.get("value", ""))
                        disabled = bool(opt.get("disabled", False))
                        m = mapping.get(value, {})
                        stk_qty = int(m.get("stkQty", 0) or 0)
                        is_sold_out = disabled or stk_qty == 0
                        options.append(
                            {
                                "name": label,
                                "price": sl_prc,
                                "stock": 0 if is_sold_out else stk_qty,
                                "isSoldOut": is_sold_out,
                            }
                        )

            if options:
                logger.info(
                    f"[LOTTEON] option/mapping 재고: {spd_no} → "
                    f"{len(options)}개 옵션 "
                    f"(재고: {[o['stock'] for o in options]})"
                )
            return options if options else None
        except Exception as e:
            logger.debug(f"[LOTTEON] option/mapping 실패: {spd_no} — {e}")
        return None

    async def fetch_benefit_price(
        self,
        pbf_data: dict[str, Any],
        spd_no: str = "",
        sitm_no: str = "",
    ) -> Optional[int]:
        """favorBox/benefits API로 최대혜택가(totAmt) 조회.

        Returns:
          최대혜택가(int) 또는 None (실패 시)
        """
        basic = pbf_data.get("basicInfo") or {}
        price = pbf_data.get("priceInfo") or {}
        spd_no = spd_no or str(basic.get("spdNo", "") or "").strip()
        sitm_no = sitm_no or str(basic.get("sitmNo", "") or "").strip()
        sl_prc = self._safe_int(price.get("slPrc", 0))  # type: ignore[attr-defined]
        if not spd_no or not sitm_no or sl_prc <= 0:
            logger.info(
                f"[LOTTEON] benefits API 스킵: spd={spd_no}, sitm={sitm_no}, slPrc={sl_prc}"
            )
            return None

        logger.info(f"[LOTTEON] benefits API basicInfo keys: {sorted(basic.keys())}")

        body = {
            "spdNo": spd_no,
            "sitmNo": sitm_no,
            "slPrc": sl_prc,
            "slQty": 1,
            "trGrpCd": str(basic.get("trGrpCd", "") or "LE"),
            "trNo": str(basic.get("trNo", "") or ""),
            "lrtrNo": str(basic.get("lrtrNo", "") or ""),
            "brdNo": str(basic.get("brdNo", "") or ""),
            "scatNo": str(basic.get("scatNo", "") or ""),
            "strCd": str(basic.get("strCd", "") or ""),
            # 채널 정보 — PC 웹 고정값 (basicInfo에 없을 수 있음)
            "chCsfCd": str(basic.get("chCsfCd", "") or "PA"),
            "chDtlNo": str(basic.get("chDtlNo", "") or "1025188"),
            "chNo": str(basic.get("chNo", "") or "100994"),
            "chTypCd": str(basic.get("chTypCd", "") or "PA07"),
            "ctrtTypCd": str(basic.get("ctrtTypCd", "") or "A"),
            "afflPdMrgnRt": basic.get("afflPdMrgnRt"),
            "afflPdLwstMrgnRt": basic.get("afflPdLwstMrgnRt"),
            "sfcoPdMrgnRt": self._safe_int(basic.get("sfcoPdMrgnRt", 0)),  # type: ignore[attr-defined]
            "sfcoPdLwstMrgnRt": self._safe_int(basic.get("sfcoPdLwstMrgnRt", 0)),  # type: ignore[attr-defined]
            "pcsLwstMrgnRt": self._safe_int(basic.get("pcsLwstMrgnRt", 0)),  # type: ignore[attr-defined]
            "dmstOvsDvDvsCd": str(basic.get("dmstOvsDvDvsCd", "") or "DMST"),
            "dvPdTypCd": str(basic.get("dvPdTypCd", "") or "GNRL"),
            "dvCst": self._safe_int(basic.get("dvCst", 0)),  # type: ignore[attr-defined]
            "dvCstStdQty": self._safe_int(basic.get("dvCstStdQty", 0)),  # type: ignore[attr-defined]
            "stkMgtYn": str(basic.get("stkMgtYn", "") or "Y"),
            "thdyPdYn": str(basic.get("thdyPdYn", "") or "N"),
            "fprdDvPdYn": str(basic.get("fprdDvPdYn", "") or "N"),
            "mallNo": str(basic.get("mallNo", "") or "1"),
            "cartDvsCd": "01",
            "infwMdiaCd": "PC",
            "screenType": "PRODUCT",
            "maxPurQty": 999999,
            "aplyBestPrcChk": "Y",
            "aplyStdDttm": datetime.now().strftime("%Y%m%d%H%M%S"),
            "pyMnsExcpLst": [],
            "discountApplyProductList": [],
        }

        url = f"{self.PBF_BASE}/product/v2/extlmsa/promotion/favorBox/benefits"
        _cookie_len = (
            len(_lotteon_cookie_cache.split(";")) if _lotteon_cookie_cache else 0
        )
        logger.info(
            f"[LOTTEON] benefits API 호출: {spd_no}, "
            f"쿠키={'있음(' + str(_cookie_len) + '개)' if _lotteon_cookie_cache else '없음'}, "
            f"slPrc={sl_prc:,}"
        )
        try:
            client = await self._get_pbf_client()
            _benefit_headers = {
                **self.HEADERS,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://www.lotteon.com",
            }
            if _lotteon_cookie_cache:
                _benefit_headers["Cookie"] = _lotteon_cookie_cache
            resp = await client.post(
                url,
                json=body,
                headers=_benefit_headers,
            )
            if resp.status_code != 200:
                logger.warning(
                    f"[LOTTEON] benefits API HTTP {resp.status_code}: {resp.text[:200]}"
                )
                return None
            result = resp.json()
            if str(result.get("returnCode")) != "200":
                logger.warning(
                    f"[LOTTEON] benefits API 실패: {result.get('message', '')[:100]}"
                )
                return None
            data = result.get("data") or {}
            tot_amt = data.get("totAmt")
            if tot_amt is not None and float(tot_amt) > 0:
                benefit = int(float(tot_amt))
                # 롯데멤버스카드는 1회성 혜택이므로 최대혜택가 계산에서 제외.
                # 롯데카드/삼성카드 즉시할인은 유지 (상시 카드 결제 할인).
                # 식별: discountGroups[].discountApplyPromotionList[] 중
                #   dispTitle에 "롯데멤버스" 포함 + 적용됨(bestPrAplyYn=Y, prAplyYn=Y)
                # 진단용: 전체 프로모션 dispTitle 로깅 (운영에서 실제 값 확인)
                member_dc = 0
                _promo_titles: list[str] = []
                for _group in data.get("discountGroups") or []:
                    for _pr in _group.get("discountApplyPromotionList") or []:
                        _disp = str(_pr.get("dispTitle", "") or "")
                        _pr_typ = str(_pr.get("prTypCd", "")).upper()
                        _applied = (
                            str(_pr.get("bestPrAplyYn", "")).upper() == "Y"
                            and str(_pr.get("prAplyYn", "")).upper() == "Y"
                        )
                        _dc = int(float(_pr.get("dcAmt", 0) or 0))
                        _promo_titles.append(
                            f"{_disp}({_pr_typ},{'A' if _applied else '-'},{_dc})"
                        )
                        if _applied and "롯데멤버스" in _disp:
                            member_dc += _dc
                if _promo_titles:
                    logger.info(
                        f"[LOTTEON] benefits promotions: {spd_no} → {_promo_titles}"
                    )
                if member_dc > 0:
                    benefit += member_dc
                    logger.info(
                        f"[LOTTEON] 롯데멤버스카드 제외(1회성): {spd_no} → "
                        f"{member_dc:,} 복원 → {benefit:,}"
                    )
                logger.info(
                    f"[LOTTEON] benefits API 혜택가: {spd_no} → {benefit:,}"
                    f" (정가={sl_prc:,}, 할인={int(float(data.get('totDcAmt', 0))):,})"
                )
                return benefit
        except Exception as e:
            logger.debug(f"[LOTTEON] benefits API 실패: {spd_no} — {e}")
        return None

    async def _fetch_pbf_detail(
        self, sitm_no: str, client: httpx.AsyncClient
    ) -> Optional[dict[str, Any]]:
        """pbf.lotteon.com API로 옵션/재고/이미지 데이터 조회."""
        url = f"{self.PBF_BASE}/product/v2/detail/search/base/sitm/{sitm_no}"
        pbf_headers = {
            **self.HEADERS,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.lotteon.com",
        }
        try:
            resp = await client.get(url, headers=pbf_headers)
            if resp.status_code != 200:
                return None
            body = resp.json()
            if body.get("returnCode") != "200" and body.get("returnCode") != 200:
                return None
            return body.get("data")
        except Exception as e:
            logger.debug(f"[LOTTEON] pbf API 실패: {sitm_no} — {e}")
            return None

    async def _fetch_pbf_pd_detail(
        self, pd_no: str, client: httpx.AsyncClient
    ) -> Optional[dict[str, Any]]:
        """pbf /base/pd/ API — artlInfo(고시정보), dispCategoryInfo 포함."""
        url = (
            f"{self.PBF_BASE}/product/v2/detail/search/base/pd"
            f"/{pd_no}?isNotContainOptMapping=true"
        )
        pbf_headers = {
            **self.HEADERS,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.lotteon.com",
        }
        try:
            resp = await client.get(url, headers=pbf_headers)
            if resp.status_code != 200:
                return None
            body = resp.json()
            if body.get("returnCode") != "200" and body.get("returnCode") != 200:
                return None
            return body.get("data")
        except Exception as e:
            logger.debug(f"[LOTTEON] pbf pd API 실패: {pd_no} — {e}")
            return None

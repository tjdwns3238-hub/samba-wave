"""스마트스토어(네이버 커머스) API 클라이언트 - 상품 등록/수정.

인증 방식: OAuth2 (bcrypt 서명)
- client_id + timestamp → bcrypt hash → Base64 = client_secret_sign
- POST /external/v1/oauth2/token → access_token 발급
- 이후 Bearer 토큰으로 API 호출
"""

from __future__ import annotations

import asyncio

import base64
import math
import re
import time
from typing import Any, Optional

import bcrypt

from backend.domain.samba.proxy.notice_utils import (
    build_smartstore_notice as _build_ss_notice,
)
import httpx

from backend.core.config import settings
from backend.utils.logger import logger

# 한국/대한민국 등 국내산 키워드
_DOMESTIC_KEYWORDS = {"한국", "대한민국", "korea", "국내", "국산"}

# 나라 → 네이버 원산지 코드 매핑 (GET /v1/product-origin-areas 기반)
_COUNTRY_ORIGIN_CODE: dict[str, str] = {
    # 아시아 (0200)
    "베트남": "0200014",
    "중국": "0200037",
    "일본": "0200036",
    "인도": "0200033",
    "인도네시아": "0200034",
    "태국": "0200044",
    "대만": "0200002",
    "방글라데시": "0200013",
    "캄보디아": "0200040",
    "미얀마": "0200011",
    "필리핀": "0200048",
    "말레이시아": "0200008",
    "파키스탄": "0200047",
    "스리랑카": "0200019",
    "싱가포르": "0200021",
    "홍콩": "0200049",
    "몽골": "0200010",
    "네팔": "0200001",
    "라오스": "0200004",
    "브루나이": "0200017",
    "우즈베키스탄": "0200028",
    "카자흐스탄": "0200038",
    "카타르": "0200039",
    "쿠웨이트": "0200041",
    "바레인": "0200012",
    "사우디아라비아": "0200018",
    "아랍에미리트": "0200022",
    "이스라엘": "0200032",
    "이란": "0200031",
    # 유럽 (0201)
    "이탈리아": "0201038",
    "프랑스": "0201046",
    "독일": "0201005",
    "스페인": "0201025",
    "영국": "0201035",
    "포르투갈": "0201044",
    "루마니아": "0201049",
    "폴란드": "0201045",
    "체코": "0201040",
    "헝가리": "0201048",
    "네덜란드": "0201002",
    "스위스": "0201024",
    "스웨덴": "0201023",
    "노르웨이": "0201003",
    "덴마크": "0201004",
    "핀란드": "0201047",
    "벨기에": "0201017",
    "오스트리아": "0201036",
    "그리스": "0201000",
    "아일랜드공화국": "0201029",
    "러시아연방": "0201007",
    "터키": "0201042",
    "불가리아": "0201021",
    "크로아티아": "0201041",
    "세르비아": "0201050",
    # 북아메리카 (0204)
    "미국": "0204000",
    "캐나다": "0204006",
    # 라틴아메리카 (0205)
    "멕시코": "0205007",
    "브라질": "0205015",
    "아르헨티나": "0205020",
    "칠레": "0205029",
    "콜롬비아": "0205031",
    "페루": "0205036",
    # 오세아니아 (0203)
    "호주": "0203024",
    "뉴질랜드": "0203003",
    # 아프리카 (0202)
    "이집트": "0202039",
    "남아프리카공화국": "0202008",
    "모로코": "0202017",
    "에티오피아": "0202036",
    "케냐": "0202049",
    # 영문 → 한글 매핑
    "vietnam": "0200014",
    "china": "0200037",
    "japan": "0200036",
    "india": "0200033",
    "indonesia": "0200034",
    "thailand": "0200044",
    "taiwan": "0200002",
    "cambodia": "0200040",
    "bangladesh": "0200013",
    "myanmar": "0200011",
    "italy": "0201038",
    "france": "0201046",
    "germany": "0201005",
    "spain": "0201025",
    "uk": "0201035",
    "portugal": "0201044",
    "usa": "0204000",
    "us": "0204000",
    "canada": "0204006",
    "australia": "0203024",
    "new zealand": "0203003",
}


def _format_phone(phone: str) -> str:
    """전화번호 포맷팅 — 010-95940674 → 010-9594-0674."""
    digits = re.sub(r"[^0-9]", "", phone)
    if not digits:
        return phone
    # 010-xxxx-xxxx
    if len(digits) == 11 and digits.startswith("01"):
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    # 02-xxxx-xxxx (서울)
    if digits.startswith("02"):
        if len(digits) == 10:
            return f"02-{digits[2:6]}-{digits[6:]}"
        if len(digits) == 9:
            return f"02-{digits[2:5]}-{digits[5:]}"
    # 0xx-xxxx-xxxx (지역번호 3자리)
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    # 050x-xxxx-xxxx (안심번호)
    if len(digits) == 12 and digits.startswith("05"):
        return f"{digits[:4]}-{digits[4:8]}-{digits[8:]}"
    return phone


def _validate_as_phone(phone: str) -> str:
    """AS 전화번호 검증 — 전화번호 형식이 아니면 기본값 반환."""
    formatted = _format_phone(phone)
    if re.match(r"^\d{2,4}-\d{3,4}-\d{4}$", formatted):
        return formatted
    return "02-0000-0000"


def _build_origin_area(origin: str) -> dict:
    """원산지 값에 따라 originAreaInfo를 동적 생성.

    originAreaCode: "00"=국내산, "01"=원양산(해산물), "02"=수입산, "03"=기타
    """
    origin = (origin or "").strip()
    lower = (origin or "").lower()
    if origin and any(kw in lower for kw in _DOMESTIC_KEYWORDS):
        return {"originAreaCode": "00", "content": origin, "plural": False}

    # 수입산: 나라→네이버 고유코드 매핑
    if origin:
        code = _COUNTRY_ORIGIN_CODE.get(origin) or _COUNTRY_ORIGIN_CODE.get(lower, "")
        if not code:
            # 부분 매칭 시도 (예: "베트남산" → "베트남")
            for country, c in _COUNTRY_ORIGIN_CODE.items():
                if country in origin or country in lower:
                    code = c
                    origin = country
                    break
        if code:
            return {
                "originAreaCode": code,
                "content": origin,
                "importer": "판매자 문의",
                "plural": False,
            }

    # 매핑 안 되는 경우 기타(03)
    return {
        "originAreaCode": "03",
        "content": origin or "상세설명에 표기",
        "plural": False,
    }


def _build_certification_infos(cert_infos: list[dict] | None) -> dict:
    """카테고리 인증정보 → productCertificationInfos + 인증대상 제외 변환.

    카테고리 API 조회 결과 중 필수(nonEssential=false)만 선택.
    네이버 API 제한: 최대 5개.
    KC/어린이제품 인증 → certificationTargetExcludeContent로 면제 선언.
    GREEN_PRODUCTS → 인증번호 숫자+하이픈만 허용.
    """
    if not cert_infos:
        return {}
    # 필수 인증만 필터 (nonEssential이 false이거나 없는 것)
    required = [c for c in cert_infos if not c.get("nonEssential", False)]
    if not required:
        return {}
    items = []
    exclude_content: dict[str, object] = {}
    # KC/어린이제품 인증 면제 대상 kindType
    KC_CHILD_TYPES = {"KC_CERTIFICATION", "CHILD_CERTIFICATION"}
    for info in required[:5]:  # 최대 5개
        cert_id = info.get("id")
        if cert_id is None:
            continue
        kind_types = info.get("kindTypes") or []
        kind_type = kind_types[0] if kind_types else "ETC"
        # KC/어린이제품 인증 → 인증대상 제외 선언 (실제 인증서 없음)
        if kind_type in KC_CHILD_TYPES:
            exclude_content["childCertifiedProductExclusionYn"] = True
            exclude_content["kcCertifiedProductExclusionYn"] = "TRUE"
            continue
        # 친환경인증: 인증번호 숫자+하이픈만 허용
        if info.get("green") or kind_type == "GREEN_PRODUCTS":
            cert_number = "0000-0000"
        else:
            cert_number = "해당사항없음"
        # companyName=true면 인증상호 필수
        name = "자가인증" if info.get("companyName") else "해당사항없음"
        items.append(
            {
                "certificationInfoId": cert_id,
                "certificationKindType": kind_type,
                "name": name,
                "certificationNumber": cert_number,
            }
        )
    result: dict[str, object] = {}
    if items:
        result["productCertificationInfos"] = items
    if exclude_content:
        result["certificationTargetExcludeContent"] = exclude_content
    return result


def _build_combination_options(
    options: list[dict],
    sale_price: int,
    max_stock_per_option: int = 0,
    option_deletion_words: list[str] | None = None,
    option_group_names: list[str] | None = None,
) -> dict:
    """수집 옵션 → 스마트스토어 combinationOption 변환.

    옵션명에서 사이즈/색상을 분리하고, 재고·품절 상태를 반영한다.
    옵션가는 sale_price 기준 0원이 최소 1개 보장되도록 처리.
    max_stock_per_option: 계정 재고수량 설정 (옵션별 재고 상한)
    option_deletion_words: 옵션명에서 제거할 단어 목록
    option_group_names: 소싱처 응답의 그룹명 (예: ["색상","사이즈"]) — 있으면 우선 사용
    """
    import re as _re

    _opt_del = option_deletion_words or []
    # 옵션명 패턴 분석: "02(235)" → 사이즈, "Black / 270" → 색상+사이즈
    # 2단 옵션 판별: " / " (공백+슬래시+공백) 패턴이 있어야 진짜 색상/사이즈 구분
    # "A/XS", "A/M" 같은 사이즈 코드는 1단 옵션으로 처리
    has_slash = any(" / " in (o.get("name") or "") for o in options)

    # 그룹명 결정: 1차 옵션그룹명은 항상 "선택"으로 강제 (소싱처가 코드성 명칭(A/B/C 등)을
    # 내려보내 1차옵션명이 단일 문자로 노출되는 문제 방지).
    # 2차 그룹명은 소싱처 응답(option_group_names[1])을 우선, 없으면 "사이즈" 폴백.
    _src_groups = [g for g in (option_group_names or []) if g]
    if has_slash:
        option_groups = ["옵션1", "옵션2"]
    else:
        option_groups = ["옵션1"]

    def _clean_option_name(n: str) -> str:
        """옵션명에서 삭제어 제거."""
        for w in _opt_del:
            n = n.replace(w, "")
        return _re.sub(r"\s{2,}", " ", n).strip() or n

    # 옵션 가격 차이 base 결정 — sale_price(정상가) 가 옵션의 ABS 가격보다 높으면
    # max(opt-sale_price,0) 가 전부 0으로 clamp되어 옵션 간 가격 차이가 소실됨.
    # 옵션의 활성 가격 중 최저가를 base 로 사용하여 옵션 간 상대 차이만 추출.
    _active_opt_prices = [
        int(o.get("price") or 0)
        for o in options
        if int(o.get("price") or 0) > 0
        and not o.get("isSoldOut", False)
        and (o.get("stock") or 0) > 0
    ]
    _diff_base = min(_active_opt_prices) if _active_opt_prices else int(sale_price or 0)

    combinations = []
    for idx, opt in enumerate(options):
        name = opt.get("name") or opt.get("size") or f"옵션{idx + 1}"
        if _opt_del:
            name = _clean_option_name(name)
        # 빈 옵션명 방어 — 공백/특수문자만 남은 경우
        name = name.strip()
        if not name or len(name.replace(" ", "")) == 0:
            name = f"옵션{idx + 1}"

        # 2D 옵션은 split 먼저 — 결합 이름 길이로 자르면 뒷부분 짤림 (예: "Cotton beige / Cotton beige"
        # 가 25자 cut → "Cotton beige / Cotton bei" → optionName2 = "Cotton bei")
        if has_slash and " / " in name:
            parts = [p.strip() for p in name.split(" / ", 1)]
            parts = [p for p in parts if p]
            if not parts:
                parts = [name.replace("/", "").strip() or f"옵션{idx + 1}"]
            # 각 차원별 25자 제한 (전체가 아닌 차원별)
            option_values = [p[:25] for p in parts]
        else:
            # 1D — 전체 이름에 25자 제한
            if len(name) > 25:
                name = name[:25]
            option_values = [name]

        stock = opt.get("stock", 0) or 0
        sold_out = opt.get("isSoldOut", False)

        if sold_out:
            stock = 0

        # 옵션 가격 차이 — 옵션 활성가 중 최저가를 base 로 사용 (음수 0 클램핑)
        opt_price = int(opt.get("price", 0) or 0)
        price_diff = max(opt_price - _diff_base, 0) if opt_price > 0 else 0

        combinations.append(
            {
                "optionName1": option_values[0],
                **({"optionName2": option_values[1]} if len(option_values) > 1 else {}),
                "stockQuantity": min(max(stock, 0), max_stock_per_option)
                if max_stock_per_option > 0
                else max(stock, 0),
                "price": price_diff,
                "usable": not sold_out,
            }
        )

    # ── 동일 옵션명 중복 감지 → US 사이즈 접미사로 구분 (나이키 등) ──
    # 2D(색상 / 사이즈, 색상 / 스트랩 등) 모드에서는 optionName1 이 여러 optionName2 와
    # 짝지어져 자연스럽게 반복되므로 중복 제거 로직 건너뜀
    # (1D 단독 옵션 — has_slash=False — 에서만 의미 있음)
    from collections import Counter as _Counter

    def _append_within_25(base: str, suffix: str) -> str:
        # suffix 포함 25자 초과 시 base 를 먼저 잘라서 합산이 25자 이하가 되도록 보장
        if len(base) + len(suffix) <= 25:
            return base + suffix
        cut = max(25 - len(suffix), 0)
        return base[:cut] + suffix

    if not has_slash:
        _name_counts = _Counter(c["optionName1"] for c in combinations)
        _dup_names = {n for n, cnt in _name_counts.items() if cnt > 1}
        if _dup_names:
            _dup_seq: dict[str, int] = {}
            for _c, _opt in zip(combinations, options):
                if _c["optionName1"] in _dup_names:
                    _us = _opt.get("us_label", "")
                    if _us:
                        _c["optionName1"] = _append_within_25(
                            _c["optionName1"], f" US{_us}"
                        )
                    else:
                        _base = _c["optionName1"]
                        _seq = _dup_seq.get(_base, 1)
                        _dup_seq[_base] = _seq + 1
                        if _seq > 1:
                            _c["optionName1"] = _append_within_25(_base, f"({_seq})")

    # 최종 안전망 — 어떤 경로로 들어와도 옵션값은 25자 이하 보장 (스마트스토어 MaxLength 정책)
    for _c in combinations:
        if isinstance(_c.get("optionName1"), str) and len(_c["optionName1"]) > 25:
            _c["optionName1"] = _c["optionName1"][:25]
        if isinstance(_c.get("optionName2"), str) and len(_c["optionName2"]) > 25:
            _c["optionName2"] = _c["optionName2"][:25]

    # 스마트스토어 필수조건: 옵션가 0원 + 재고 1개 이상 + 사용여부 Y 가 최소 1개
    has_base = any(
        c["price"] == 0 and c["stockQuantity"] > 0 and c["usable"] for c in combinations
    )
    if not has_base and combinations:
        # 가격 0원인 옵션이 없으면, 최저가 옵션을 기준(0원)으로 재조정
        min_price = min(c["price"] for c in combinations)
        if min_price > 0:
            for c in combinations:
                c["price"] = c["price"] - min_price
        # 재조정 후에도 조건 미충족 (전 옵션 품절/재고0) → 빈 combinations 반환하여 전송 스킵 유도
        has_base = any(
            c["price"] == 0 and c["stockQuantity"] > 0 and c["usable"]
            for c in combinations
        )
        if not has_base:
            return None  # 전 옵션 품절 → 등록 불가

    return {
        "optionCombinationSortType": "CREATE",
        "optionCombinationGroupNames": {
            "optionGroupName1": option_groups[0],
            **(
                {"optionGroupName2": option_groups[1]} if len(option_groups) > 1 else {}
            ),
        },
        "optionCombinations": combinations,
        "useStockManagement": True,
    }


def _build_product_add_items(addon_options: list[dict]) -> list[dict]:
    """addon_options → 스마트스토어 productAddItems 변환.

    Naver Commerce API 스키마 (그룹/아이템 중첩):
      productAddItems = [
        { "groupName": "추가옵션 그룹명",
          "items": [
            {"itemName": "값명", "price": 추가금액, "stockQuantity": 재고, "usable": bool},
            ...
          ]
        },
        ...
      ]

    addon_options 입력 포맷:
      [{no, group, name, add_price, stock, is_required, is_none_choice}, ...]
    """
    if not addon_options:
        return []

    # 그룹별로 묶기 (group 키 없으면 "추가옵션" 으로 모음)
    by_group: dict[str, list[dict]] = {}
    for ao in addon_options:
        name = (ao.get("name") or "").strip()
        if not name:
            continue
        # "선택안함"/"선택없음"은 productAddItems에 의미 없음 — 미선택이 기본
        if ao.get("is_none_choice") or "선택안함" in name or "선택없음" in name:
            continue
        if len(name) > 25:
            name = name[:25]
        group_raw = (ao.get("group") or "추가옵션").strip()
        group = group_raw[:25] if group_raw else "추가옵션"
        add_price = max(int(ao.get("add_price") or 0), 0)
        stock = int(ao.get("stock") or 0)
        if stock < 0:
            stock = 0
        # 스마트스토어 stockQuantity 보정 — 소싱처 99999+ 같은 큰 값은 9999로 캡
        if stock > 9999:
            stock = 9999
        by_group.setdefault(group, []).append(
            {
                "itemName": name,
                "price": add_price,
                "stockQuantity": stock,
                "usable": stock > 0,
            }
        )

    return [
        {"groupName": group_name, "items": items}
        for group_name, items in by_group.items()
        if items
    ]


class SmartStoreClient:
    """네이버 커머스 API 클라이언트."""

    BASE_URL = "https://api.commerce.naver.com/external"

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token: str = ""
        self._token_expires_at: float = 0

    # ------------------------------------------------------------------
    # 인증
    # ------------------------------------------------------------------

    async def _ensure_token(self) -> str:
        """유효한 토큰이 없으면 새로 발급."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        timestamp = int(time.time() * 1000)
        password = f"{self.client_id}_{timestamp}"
        hashed = bcrypt.hashpw(
            password.encode("utf-8"),
            self.client_secret.encode("utf-8"),
        )
        client_secret_sign = base64.standard_b64encode(hashed).decode("utf-8")

        async with httpx.AsyncClient(timeout=settings.http_timeout_default) as client:
            resp = await client.post(
                f"{self.BASE_URL}/v1/oauth2/token",
                data={
                    "client_id": self.client_id,
                    "timestamp": timestamp,
                    "client_secret_sign": client_secret_sign,
                    "grant_type": "client_credentials",
                    "type": "SELF",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                err = (
                    resp.json()
                    if "json" in resp.headers.get("content-type", "")
                    else {}
                )
                raise SmartStoreApiError(
                    f"토큰 발급 실패: {err.get('message', resp.status_code)}"
                )
            data = resp.json()
            self._access_token = data["access_token"]
            self._token_expires_at = time.time() + data.get("expires_in", 3600)
            return self._access_token

    async def _call_api(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> dict[str, Any]:
        """공통 API 호출."""
        token = await self._ensure_token()
        url = f"{self.BASE_URL}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        effective_timeout = (
            timeout if timeout is not None else settings.http_timeout_default
        )

        async with httpx.AsyncClient(timeout=effective_timeout) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers, params=params)
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

            text = resp.text
            try:
                data = resp.json()
            except Exception:
                data = {"raw": text}

            logger.info(f"[스마트스토어] {method} {path} → {resp.status_code}")

            # claim 관련 API는 응답 body 전체 로깅 (디버깅용)
            if "/claim/" in path:
                logger.info(f"[스마트스토어] claim 응답 body: {data}")

            if not resp.is_success:
                # 네이버 API는 invalidInputs 배열로 상세 에러 제공
                msg = data.get("message", "") or data.get("reason", "") or text[:200]
                invalid_inputs = data.get("invalidInputs") or []
                if invalid_inputs:
                    logger.error(f"[스마트스토어] invalidInputs 원본: {invalid_inputs}")
                    details = "; ".join(
                        f"{iv.get('name', iv.get('field', '?'))}: {iv.get('message', '')}"
                        for iv in invalid_inputs
                        if isinstance(iv, dict)
                    )
                    # 어린이제품 인증 에러 시 명확한 가이드
                    if any(
                        "인증" in str(iv.get("message", ""))
                        for iv in invalid_inputs
                        if isinstance(iv, dict)
                    ):
                        msg = f"이 카테고리는 어린이제품 인증정보(KC인증 등)가 필요합니다. 카테고리를 변경하거나 인증정보를 등록해주세요. [{details}]"
                    else:
                        msg = f"{msg} [{details}]"
                raise SmartStoreApiError(f"HTTP {resp.status_code}: {msg}")

            return data

    # ------------------------------------------------------------------
    # 채널(스토어) 정보 조회
    # ------------------------------------------------------------------

    async def get_channel_info(self) -> dict[str, Any]:
        """채널(스토어) 정보를 조회하여 스토어 슬러그 등 반환."""
        result = await self._call_api("GET", "/v1/seller/channels")
        logger.info(f"[스마트스토어] 채널 조회 raw: {result}")

        # 다양한 응답 구조 대응
        channels: list[Any] = []
        if isinstance(result, list):
            channels = result
        elif isinstance(result, dict):
            for key in ("contents", "channels", "data", "result"):
                val = result.get(key)
                if isinstance(val, list) and val:
                    channels = val
                    break
            # 단일 객체 응답 (channelNo가 최상위에 있는 경우)
            if not channels and result.get("channelNo"):
                channels = [result]

        if not channels:
            logger.warning("[스마트스토어] 채널 목록이 비어있음")
            return {}

        ch = channels[0]
        # channel이 nested일 수 있음
        if isinstance(ch.get("channel"), dict):
            ch = ch["channel"]

        # URL 필드 다양한 키 시도
        url = ch.get("url") or ch.get("channelUrl") or ch.get("storeUrl") or ""
        slug = url.rstrip("/").split("/")[-1] if url else ""

        logger.info(f"[스마트스토어] 채널 파싱 결과 — url={url}, slug={slug}")

        return {
            "channelNo": ch.get("channelNo", ""),
            "channelName": ch.get("name", ch.get("channelName", "")),
            "storeSlug": slug,
            "url": url,
        }

    async def get_store_slug_fallback(self) -> str:
        """채널 API 실패 시 — 등록된 상품에서 스토어 슬러그 추출."""
        try:
            result = await self._call_api(
                "POST",
                "/v1/products/search",
                body={
                    "page": 1,
                    "size": 1,
                },
            )
            logger.info(f"[스마트스토어] 슬러그 fallback 상품검색 raw: {result}")

            # 응답에서 상품 목록 추출
            contents = []
            if isinstance(result, dict):
                contents = result.get("contents", result.get("data", []))
            if isinstance(result, list):
                contents = result

            if not contents:
                return ""

            product = contents[0]
            # 상품의 smartStoreUrl 또는 channelProducts에서 URL 추출
            store_url = product.get("smartStoreUrl", "")
            if not store_url:
                channel_products = product.get("channelProducts", [])
                for cp in channel_products:
                    cp_url = cp.get("url") or cp.get("channelProductUrl") or ""
                    if "smartstore.naver.com" in cp_url:
                        store_url = cp_url
                        break

            if store_url and "smartstore.naver.com" in store_url:
                # https://smartstore.naver.com/슬러그/products/... → 슬러그 추출
                parts = store_url.split("smartstore.naver.com/")
                if len(parts) > 1:
                    slug = parts[1].split("/")[0]
                    logger.info(f"[스마트스토어] fallback 슬러그 추출: {slug}")
                    return slug

            return ""
        except Exception as e:
            logger.warning(f"[스마트스토어] 슬러그 fallback 실패: {e}")
            return ""

    # ------------------------------------------------------------------
    # 카테고리 조회
    # ------------------------------------------------------------------

    async def get_categories(self, last_only: bool = True) -> list[dict[str, Any]]:
        """네이버 커머스 카테고리 전체 조회.

        GET /v1/categories?last={true|false}
        응답: [{wholeCategoryName, id, name, last}, ...]
        """
        params = {"last": str(last_only).lower()}
        return await self._call_api("GET", "/v1/categories", params=params)

    # ------------------------------------------------------------------
    # 브랜드 검색
    # ------------------------------------------------------------------

    async def search_catalog(
        self, style_code: str, category_id: str = ""
    ) -> Optional[dict[str, Any]]:
        """품번(스타일코드)으로 네이버 카탈로그 검색. 매칭되면 modelId/brandId/manufacturerId 반환."""
        if not style_code:
            return None
        try:
            result = await self._call_api(
                "GET", "/v1/product-models", params={"name": style_code}
            )
            contents = (
                result.get("contents", [])
                if isinstance(result, dict)
                else result
                if isinstance(result, list)
                else []
            )
            if contents:
                # 같은 카테고리 카탈로그 우선 선택
                c = contents[0]
                if category_id:
                    for item in contents:
                        if str(item.get("categoryId", "")) == str(category_id):
                            c = item
                            break
                logger.info(
                    f"[스마트스토어] 카탈로그 매칭: {style_code} → {c.get('name', '')[:40]} (catId={c.get('categoryId')}, 상품catId={category_id})"
                )
                return {
                    "modelId": c.get("id"),
                    "brandId": c.get("brandCode"),
                    "brandName": c.get("brandName", ""),
                    "manufacturerId": c.get("manufacturerCode"),
                    "manufacturerName": c.get("manufacturerName", ""),
                    "categoryId": c.get("categoryId", ""),
                }
        except Exception as e:
            logger.warning(f"[스마트스토어] 카탈로그 검색 실패 ({style_code}): {e}")
        return None

    @staticmethod
    def _brand_name_variants(name: str) -> list[str]:
        """브랜드/제조사명의 검색 변형 목록 생성.

        시도 순서: 원본 → 접미사 제거 → 법인명 제거 → 첫 단어만
        """
        import re

        seen: set[str] = set()
        variants: list[str] = []
        for candidate in [
            name,
            # 카테고리 접미사 제거
            re.sub(
                r"\s*(키즈|kids|kid|주니어|junior|jr|아동|유아|베이비|baby|우먼|women|맨즈|men|골프|golf|스포츠|sports|아웃도어|outdoor)\s*$",
                "",
                name,
                flags=re.IGNORECASE,
            ).strip(),
            # 법인 접미사 제거
            re.sub(
                r"\s*(AG|Inc\.?|Corp\.?|Ltd\.?|Co\.?,?\s*Ltd\.?|LLC|GmbH|S\.?A\.?)\s*$",
                "",
                name,
                flags=re.IGNORECASE,
            ).strip(),
            re.sub(r"\(주\)|\(유\)|\(합\)|주식회사|㈜", "", name).strip(),
            # 첫 단어만 (예: "아디다스 골프" → "아디다스")
            name.split()[0] if " " in name else "",
        ]:
            c = candidate.strip()
            if c and c.lower() not in seen:
                seen.add(c.lower())
                variants.append(c)
        return variants

    async def search_brand(self, brand_name: str) -> Optional[tuple[int, str]]:
        """브랜드명으로 네이버 브랜드 (ID, 정확한 이름) 검색 — 자동 fallback 체인."""
        if not brand_name or brand_name in ("상세설명 참조", "상세 이미지 참조"):
            return None
        for name in self._brand_name_variants(brand_name):
            try:
                result = await self._call_api(
                    "GET", "/v1/product-brands", params={"name": name}
                )
                brands = result
                if isinstance(result, dict):
                    brands = (
                        result.get("contents")
                        or result.get("brands")
                        or result.get("data")
                        or []
                    )
                if not isinstance(brands, list):
                    brands = []
                if not brands:
                    continue
                logger.info(f"[스마트스토어] 브랜드 검색: {name} → {len(brands)}건")
                # 정확 매치 우선
                for b in brands:
                    if b.get("name") == name:
                        return (b.get("id"), b.get("name", name))
                # 첫 번째 결과 — 네이버가 반환한 정확한 이름 사용
                b = brands[0]
                return (b.get("id"), b.get("name", name))
            except Exception:
                continue
        logger.warning(
            f"[스마트스토어] 브랜드 검색 실패 (모든 변형 시도): {brand_name}"
        )
        return None

    async def search_manufacturer(self, mfr_name: str) -> Optional[int]:
        """제조사명으로 네이버 제조사 ID 검색 — 자동 fallback 체인."""
        if not mfr_name:
            return None
        for name in self._brand_name_variants(mfr_name):
            try:
                result = await self._call_api(
                    "GET", "/v1/product-manufacturers", params={"name": name}
                )
                if isinstance(result, list) and result:
                    for m in result:
                        if m.get("name") == name:
                            return m.get("id")
                    return result[0].get("id")
            except Exception:
                continue
        logger.warning(f"[스마트스토어] 제조사 검색 실패 (모든 변형 시도): {mfr_name}")
        return None

    async def get_category_certification_infos(
        self, category_id: str
    ) -> list[dict[str, Any]]:
        """카테고리별 필수 인증정보 조회. certificationInfos[].id/name/kindTypes 반환."""
        if not category_id:
            return []
        try:
            result = await self._call_api(
                "GET",
                f"/v1/categories/{category_id}",
            )
            cert_infos = []
            if isinstance(result, dict):
                cert_infos = result.get("certificationInfos") or []
            logger.info(
                f"[스마트스토어] 카테고리 {category_id} 인증정보: {len(cert_infos)}개"
            )
            return cert_infos
        except Exception as e:
            logger.warning(
                f"[스마트스토어] 카테고리 인증정보 조회 실패 ({category_id}): {e}"
            )
            return []

    async def get_category_attributes(self, category_id: str) -> list[dict[str, Any]]:
        """카테고리별 상품속성 값 목록 조회."""
        if not category_id:
            return []
        try:
            result = await self._call_api(
                "GET",
                "/v1/product-attributes/attribute-values",
                params={"categoryId": category_id},
            )
            # 네이버 API 응답: 리스트 또는 {"contents": [...]}
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return (
                    result.get("contents")
                    or result.get("data")
                    or result.get("attributeValues")
                    or []
                )
            return []
        except Exception as e:
            logger.warning(
                f"[스마트스토어] 카테고리 속성 조회 실패 ({category_id}): {e}"
            )
            return []

    # ------------------------------------------------------------------
    # 태그 사전 검색
    # ------------------------------------------------------------------

    async def search_tags(self, keyword: str) -> list[dict[str, Any]]:
        """(v2) 추천 태그 검색 목록 조회. 태그사전에 등록된 태그만 반환."""
        if not keyword:
            return []
        try:
            result = await self._call_api(
                "GET",
                "/v2/tags/recommend-tags",
                params={"keyword": keyword},
            )
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return (
                    result.get("tags")
                    or result.get("contents")
                    or result.get("data")
                    or []
                )
            return []
        except Exception as e:
            logger.warning(f"[스마트스토어] 태그 검색 실패 ({keyword}): {e}")
            return []

    async def check_restricted_tags(self, tags: list[str]) -> dict[str, bool]:
        """(v2) 제한 태그 여부 조회. {태그: 제한여부} 딕셔너리 반환."""
        if not tags:
            return {}
        try:
            result = await self._call_api(
                "GET",
                "/v2/tags/restricted-tags",
                params={"tags": tags},
            )
            if isinstance(result, list):
                return {r.get("tag", ""): r.get("restricted", False) for r in result}
            return {}
        except Exception as e:
            logger.warning(f"[스마트스토어] 제한 태그 조회 실패: {e}")
            return {}

    async def validate_tags(
        self, tags: list[str], max_count: int = 10
    ) -> list[dict[str, str]]:
        """태그를 태그사전 검색 + 제한태그 필터링하여 사용 가능한 태그만 반환.

        1) 각 태그를 recommend-tags API로 정확 매치 확인
        2) 응답에 들어온 다른 등재 태그를 풀로 누적 (정확매치가 부족할 때 보충)
        3) 정확매치 우선 + 풀 보충 후 restricted-tags API로 제한 여부 확인
        4) 모두 통과한 태그만 max_count개까지 반환
        """
        import asyncio

        token = await self._ensure_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        # 1단계: 태그사전 순차 매치 (동시 1개 + 429 재시도)
        sem = asyncio.Semaphore(1)

        async def _match_one(
            client: httpx.AsyncClient, tag: str
        ) -> tuple[dict[str, str] | None, list[dict[str, str]]]:
            """반환: (정확매치 dict 또는 None, 응답에 함께 들어온 등재 태그 풀)."""
            async with sem:
                # 태그사전 캐시 조회 (TTL 10분)
                from backend.domain.samba.cache import cache

                cached = await cache.get(f"tags:search:{tag}")
                if cached is not None:
                    # 캐시 히트 시 풀은 비움 (응답 본문 미보존)
                    return (cached if cached else None), []

                # 429 재시도: 최대 3회, 지수 백오프 (1초 → 2초 → 4초)
                for attempt in range(3):
                    try:
                        if attempt > 0:
                            await asyncio.sleep(1.0 * (2 ** (attempt - 1)))
                        resp = await client.get(
                            f"{self.BASE_URL}/v2/tags/recommend-tags",
                            headers=headers,
                            params={"keyword": tag},
                        )
                        if resp.status_code == 429:
                            if attempt < 2:
                                logger.info(
                                    f"[스마트스토어] 태그 429 재시도 {attempt + 1}/3: {tag}"
                                )
                                continue
                            logger.warning(f"[스마트스토어] 태그 429 최종 실패: {tag}")
                            # 429 에러 시 캐싱하지 않음 (재시도 필요)
                            return None, []
                        if resp.status_code != 200:
                            logger.warning(
                                f"[스마트스토어] 태그 검색 HTTP {resp.status_code}: {tag}"
                            )
                            return None, []
                        data = resp.json()
                        results = (
                            data
                            if isinstance(data, list)
                            else (
                                data.get("tags")
                                or data.get("contents")
                                or data.get("data")
                                or []
                            )
                        )
                        matched: dict[str, str] | None = None
                        discovered: list[dict[str, str]] = []
                        for r in results:
                            text = r.get("text") or r.get("tag")
                            if not text:
                                continue
                            item = {"code": str(r.get("code", 0)), "text": text}
                            if matched is None and text == tag:
                                matched = item
                            else:
                                discovered.append(item)
                        if matched is not None:
                            # 정확매치만 캐싱 — 풀은 호출별 누적
                            await cache.set(f"tags:search:{tag}", matched, ttl=600)
                        else:
                            logger.info(
                                f"[스마트스토어] 태그사전 미등록: {tag} "
                                f"(결과 {len(results)}건, 풀 {len(discovered)}개 발굴)"
                            )
                            # 미등록 태그도 캐싱 (False로 구분)
                            await cache.set(f"tags:search:{tag}", False, ttl=600)
                        return matched, discovered
                    except Exception as e:
                        logger.warning(f"[스마트스토어] 태그 검색 실패: {tag} — {e}")
                        return None, []
                return None, []

        # 후보 풀 확대 (429 탈락 대비)
        search_tags = tags[: max_count * 3]
        matched_tags: list[dict[str, str]] = []
        discovered_pool: list[dict[str, str]] = []
        pool_seen: set[str] = set()
        async with httpx.AsyncClient(timeout=60) as client:
            for i, t in enumerate(search_tags):
                if i > 0:
                    await asyncio.sleep(0.3)
                exact, discovered = await _match_one(client, t)
                if exact is not None:
                    matched_tags.append(exact)
                    pool_seen.add(exact["text"])
                for d in discovered:
                    if d["text"] in pool_seen:
                        continue
                    pool_seen.add(d["text"])
                    discovered_pool.append(d)
                # 정확매치 + 풀이 충분히 모이면 조기 종료
                if len(matched_tags) + len(discovered_pool) >= max_count * 3:
                    break

        # 정확매치 우선, 부족 시 풀로 보충
        combined: list[dict[str, str]] = list(matched_tags) + list(discovered_pool)

        if not combined:
            logger.warning(
                f"[스마트스토어] 태그사전 매치 0건 (후보 {len(search_tags)}개)"
            )
            return []

        # 2단계: 제한 태그 필터링 (정확매치 + 풀 통합 검사)
        tag_texts = [t["text"] for t in combined]
        restricted = await self.check_restricted_tags(tag_texts)
        valid_tags: list[dict[str, str]] = []
        for t in combined:
            if restricted.get(t["text"], False):
                logger.info(f"[스마트스토어] 제한 태그 제외: {t['text']}")
                continue
            valid_tags.append(t)
            if len(valid_tags) >= max_count:
                break

        logger.info(
            f"[스마트스토어] 태그 검증 완료: {len(tags)}개 후보 → "
            f"정확매치 {len(matched_tags)}개 + 풀 {len(discovered_pool)}개 "
            f"→ {len(valid_tags)}개 사용가능"
        )
        return valid_tags

    # ------------------------------------------------------------------
    # 상품 등록
    # ------------------------------------------------------------------

    async def upload_image_from_url(
        self,
        image_url: str,
        _dl_client: httpx.AsyncClient | None = None,
        _ul_client: httpx.AsyncClient | None = None,
    ) -> str:
        """외부 이미지 URL을 네이버 커머스에 업로드하고 네이버 URL을 반환."""
        token = await self._ensure_token()
        from urllib.parse import urlparse

        parsed = urlparse(image_url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        if "msscdn.net" in (parsed.netloc or ""):
            referer = "https://www.musinsa.com/"

        # 이미지 다운로드 (클라이언트 재사용)
        dl = _dl_client or httpx.AsyncClient(
            timeout=settings.http_timeout_default, follow_redirects=True
        )
        try:
            try:
                img_resp = await dl.get(
                    image_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Referer": referer,
                        "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                    },
                )
            except httpx.ConnectTimeout:
                logger.error(
                    f"[이미지] 다운로드 연결 타임아웃 — CDN 차단 가능성: {image_url[:80]}"
                )
                raise SmartStoreApiError(
                    "이미지 다운로드 연결 타임아웃 — CDN 차단 가능성"
                )
            except httpx.ReadTimeout:
                logger.error(
                    f"[이미지] 다운로드 읽기 타임아웃 — CDN 차단 가능성: {image_url[:80]}"
                )
                raise SmartStoreApiError(
                    "이미지 다운로드 읽기 타임아웃 — CDN 차단 가능성"
                )
            except Exception as dl_err:
                logger.error(
                    f"[이미지] 다운로드 실패: {type(dl_err).__name__}: {dl_err}"
                )
                raise SmartStoreApiError(
                    f"이미지 다운로드 실패: {type(dl_err).__name__}"
                )
            if not img_resp.is_success:
                logger.warning(
                    f"[이미지] 다운로드 HTTP {img_resp.status_code}: {image_url[:80]}"
                )
                raise SmartStoreApiError(
                    f"이미지 다운로드 실패: HTTP {img_resp.status_code}"
                )
            img_bytes = img_resp.content
            content_type = img_resp.headers.get("content-type", "image/jpeg")
            if len(img_bytes) < 1000:
                logger.warning(
                    f"[이미지] 비정상 크기({len(img_bytes)}B) — CDN 차단 가능성: {image_url[:80]}"
                )
                raise SmartStoreApiError(
                    f"이미지가 비정상적으로 작음({len(img_bytes)}B) — CDN 차단 가능성"
                )
        finally:
            if not _dl_client:
                await dl.aclose()

        # EXIF 제거 — GS샵 등 EXIF 포함 소싱처만 적용 (무신사 등 CDN은 스킵, OOM 방지)
        _is_gsshop_image = "gsshop" in image_url or "gs.kr" in image_url
        if _is_gsshop_image:
            try:
                from backend.domain.samba.image.exif import strip_exif

                img_bytes = strip_exif(img_bytes)
            except Exception:
                pass  # EXIF 제거 실패해도 업로드 진행

        # content_type 불명확 시 바이트 시그니처로 감지
        if not content_type.startswith("image/"):
            if img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                content_type = "image/png"
            elif img_bytes[:2] == b"\xff\xd8":
                content_type = "image/jpeg"
            elif img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
                content_type = "image/webp"
            elif img_bytes[:6] in (b"GIF87a", b"GIF89a"):
                content_type = "image/gif"
            else:
                content_type = "image/jpeg"  # 기본 폴백

        # webp → PNG 변환
        ext = "jpg"
        upload_type = content_type
        if "png" in content_type:
            ext = "png"
        elif "gif" in content_type:
            ext = "gif"
        elif "webp" in content_type or image_url.endswith(".webp"):
            # 네이버 API는 WebP 미지원 → PNG 변환 (전 소싱처 공통)
            try:
                from PIL import Image as _PILImage
                import io as _io

                pil_img = _PILImage.open(_io.BytesIO(img_bytes)).convert("RGB")
                buf = _io.BytesIO()
                pil_img.save(buf, format="PNG")
                img_bytes = buf.getvalue()
                ext = "png"
                upload_type = "image/png"
                del pil_img, buf
            except Exception:
                ext = "webp"
                upload_type = "image/webp"

        # 네이버 업로드 (클라이언트 재사용 + 429 재시도)
        import asyncio as _aio_retry

        ul = _ul_client or httpx.AsyncClient(timeout=settings.http_timeout_default)
        try:
            for attempt in range(4):
                resp = await ul.post(
                    f"{self.BASE_URL}/v1/product-images/upload",
                    headers={"Authorization": f"Bearer {token}"},
                    files={"imageFiles": (f"image.{ext}", img_bytes, upload_type)},
                )
                if resp.status_code == 429:
                    wait = 2**attempt
                    logger.warning(
                        f"[스마트스토어] 이미지 업로드 429 → {wait}초 후 재시도 ({attempt + 1}/3)"
                    )
                    await _aio_retry.sleep(wait)
                    continue
                if not resp.is_success:
                    raise SmartStoreApiError(
                        f"이미지 업로드 실패: {resp.status_code} {resp.text[:200]}"
                    )
                del img_bytes  # OOM 방지: 업로드 완료 후 bytes 즉시 해제
                data = resp.json()
                images = data.get("images", [])
                if not images:
                    raise SmartStoreApiError("이미지 업로드 응답에 URL 없음")
                return images[0].get("url", "")
        finally:
            # OOM 방지: 예외 시에도 bytes 해제
            img_bytes = None  # noqa: F841
            if not _ul_client:
                await ul.aclose()
        raise SmartStoreApiError(
            "이미지 업로드 실패: 429 Rate Limit 초과 (재시도 3회 실패)"
        )

    async def upload_images_batch(self, image_urls: list[str]) -> list[str]:
        """외부 이미지 URL 최대 4장을 1회 API 호출로 업로드. 네이버 URL 리스트 반환."""
        if not image_urls:
            return []
        token = await self._ensure_token()
        from urllib.parse import urlparse

        # 이미지 다운로드 + 전처리
        def _mem_mb():
            try:
                with open("/proc/self/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            return int(line.split()[1]) // 1024
            except Exception:
                return -1

        logger.info(
            f"[메모리] 이미지다운로드 전: {_mem_mb()}MB, urls={len(image_urls)}장"
        )
        files_list: list[tuple[str, bytes, str]] = []
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_default, follow_redirects=True
        ) as dl:
            for url in image_urls[:4]:
                try:
                    parsed = urlparse(url)
                    referer = f"{parsed.scheme}://{parsed.netloc}/"
                    if "msscdn.net" in (parsed.netloc or ""):
                        referer = "https://www.musinsa.com/"
                    elif "fashionplus" in (parsed.netloc or ""):
                        referer = "https://www.fashionplus.co.kr/"
                    resp = await dl.get(
                        url,
                        headers={
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            "Referer": referer,
                            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                        },
                    )
                    if not resp.is_success or len(resp.content) < 1000:
                        logger.warning(
                            f"[스마트스토어] 이미지 다운로드 실패: {url[:60]}"
                        )
                        continue
                    img_bytes = resp.content
                    content_type = resp.headers.get("content-type", "image/jpeg")

                    # content_type 불명확 시 바이트 시그니처로 감지
                    if not content_type.startswith("image/"):
                        if img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                            content_type = "image/png"
                        elif img_bytes[:2] == b"\xff\xd8":
                            content_type = "image/jpeg"
                        elif img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
                            content_type = "image/webp"
                        elif img_bytes[:6] in (b"GIF87a", b"GIF89a"):
                            content_type = "image/gif"
                        else:
                            content_type = "image/jpeg"
                    ext = "jpg"
                    if "png" in content_type:
                        ext = "png"
                    elif "gif" in content_type:
                        ext = "gif"
                    elif "webp" in content_type or url.endswith(".webp"):
                        ext = "webp"
                        content_type = "image/webp"
                    files_list.append(
                        (f"image_{len(files_list)}.{ext}", img_bytes, content_type)
                    )
                except Exception as e:
                    logger.warning(f"[스마트스토어] 이미지 다운로드 실패: {e}")

        logger.info(
            f"[메모리] 이미지다운로드 후: {_mem_mb()}MB, files={len(files_list)}장, total={sum(len(f[1]) for f in files_list) // 1024}KB"
        )
        if not files_list:
            return []

        # 네이버 업로드 (4장 동시, 429 재시도)
        import asyncio as _aio_batch

        async with httpx.AsyncClient(timeout=settings.http_timeout_default) as ul:
            for attempt in range(4):
                resp = await ul.post(
                    f"{self.BASE_URL}/v1/product-images/upload",
                    headers={"Authorization": f"Bearer {token}"},
                    files=[("imageFiles", (f[0], f[1], f[2])) for f in files_list],
                )
                if resp.status_code == 429:
                    wait = 2**attempt
                    logger.warning(
                        f"[스마트스토어] 배치 이미지 업로드 429 → {wait}초 후 재시도 ({attempt + 1}/4)"
                    )
                    await _aio_batch.sleep(wait)
                    continue
                if not resp.is_success:
                    raise SmartStoreApiError(
                        f"이미지 업로드 실패: {resp.status_code} {resp.text[:200]}"
                    )
                # OOM 방지: 업로드 완료 후 bytes 즉시 해제
                for _f in files_list:
                    del _f
                files_list.clear()
                logger.info(f"[메모리] 네이버업로드 후: {_mem_mb()}MB")
                data = resp.json()
                return [img.get("url", "") for img in data.get("images", [])]
        # OOM 방지: 예외 시에도 bytes 해제
        for _f in files_list:
            del _f
        files_list.clear()
        raise SmartStoreApiError("이미지 업로드 실패: 429 Rate Limit 초과")

    async def register_product(self, product_data: dict[str, Any]) -> dict[str, Any]:
        """상품 등록. POST /v2/products는 Naver 처리 시간이 길어 90초 타임아웃 적용."""
        result = await self._call_api(
            "POST", "/v2/products", body=product_data, timeout=90
        )
        return {"success": True, "data": result}

    async def find_by_management_code(
        self, management_code: str
    ) -> dict[str, Any] | None:
        """sellerManagementCode로 기등록 상품 조회. 없으면 None 반환."""
        try:
            result = await self._call_api(
                "POST",
                "/v1/products/search",
                body={"page": 1, "size": 5, "sellerManagementCode": management_code},
            )
            contents = result.get("contents") or result.get("data") or []
            if isinstance(result, list):
                contents = result
            for item in contents:
                code = item.get("sellerManagementCode") or item.get(
                    "originProduct", {}
                ).get("sellerCodeInfo", {}).get("sellerManagementCode", "")
                if code == management_code:
                    return item
            return None
        except Exception as e:
            logger.warning(f"[스마트스토어] sellerManagementCode 조회 실패 (무시): {e}")
            return None

    async def update_product(
        self, product_no: str, product_data: dict[str, Any]
    ) -> dict[str, Any]:
        """상품 수정. origin-products로 PUT."""
        result = await self._call_api(
            "PUT", f"/v2/products/origin-products/{product_no}", body=product_data
        )
        return {"success": True, "data": result}

    async def delete_product(self, product_no: str) -> dict[str, Any]:
        """상품 삭제 (리스트에서 완전 제거). 404는 이미 삭제된 상품이므로 성공 처리."""
        try:
            result = await self._call_api(
                "DELETE", f"/v2/products/origin-products/{product_no}"
            )
            return {"success": True, "data": result}
        except SmartStoreApiError as e:
            if "HTTP 404" in str(e):
                logger.info(
                    f"[스마트스토어] 상품 {product_no} 이미 삭제됨 (404) → 성공 처리"
                )
                return {"success": True, "data": {}, "already_deleted": True}
            raise

    async def get_product(self, product_no: str) -> dict[str, Any]:
        """상품 조회."""
        return await self._call_api("GET", f"/v2/products/origin-products/{product_no}")

    # ------------------------------------------------------------------
    # 주문 조회
    # ------------------------------------------------------------------

    async def get_orders(
        self,
        days: int = 7,
        order_status: str = "",
    ) -> list[dict[str, Any]]:
        """최근 N일간 주문 조회.

        Commerce API: GET /v1/pay-order/seller/product-orders/last-changed-statuses
        """
        from datetime import datetime, timedelta, timezone

        # KST 기준으로 시작 시간 계산 (스마트스토어 API 최대 90일 제한)
        kst = timezone(timedelta(hours=9))
        effective_days = min(days, 89)
        since = datetime.now(kst) - timedelta(days=effective_days)
        since_str = since.strftime("%Y-%m-%dT%H:%M:%S.000+09:00")

        params: dict[str, Any] = {
            "lastChangedFrom": since_str,
        }
        if order_status:
            params["lastChangedType"] = order_status

        # 1단계: 변경된 주문 ID 목록 조회
        # 과거: lastChangedType 13개 루프 호출 — 11개 타입이 400 에러("정확한 타입 아님")로 죽고
        #       살아있는 PAYED·PURCHASE_DECIDED 2개만 동작. 배송지변경(DELIVERY_ADDRESS_CHANGED)된
        #       신규주문은 마지막 이벤트가 비-PAYED라 영영 누락되는 사고가 있었다(2026-05-12 이종영 주문).
        # 현재: lastChangedType 파라미터 생략 — 모든 변경 유형을 한 호출로 받는다.
        #       네이버 공식 답변(commerce-api Discussion #1646)도 type 생략을 권고.
        # 요청 기간 + 최근 1일 두 시점 호출은 그대로 유지 — 응답 누락 방지 안전장치.
        logger.info(f"[스마트스토어] 주문 조회 시작 lastChangedFrom={since_str}")

        all_statuses: list[dict[str, Any]] = []
        seen_po_ids: set[str] = set()

        # 페이지네이션: 네이버 last-changed-statuses는 응답이 lastChangedDate 오름차순으로
        # 정렬되며 응답 limit이 있어 since가 멀수록 잘림(2026-05-12 검증: since=5/7 → 1건만,
        # since=5/10 → 9건). 응답이 잘리면 마지막 lastChangedDate를 새 cursor로 써서
        # 더 이상 새 productOrderId가 안 나올 때까지 반복 호출.
        async def _fetch_with_pagination(initial_from: str) -> None:
            cursor = initial_from
            for _page in range(20):  # 안전 상한: 20페이지
                qparams = dict(params)
                qparams["lastChangedFrom"] = cursor
                qparams.pop("lastChangedType", None)
                result = None
                for _retry in range(3):
                    try:
                        result = await self._call_api(
                            "GET",
                            "/v1/pay-order/seller/product-orders/last-changed-statuses",
                            params=qparams,
                        )
                        break
                    except Exception as _api_err:
                        if "429" in str(_api_err):
                            wait = 2.0 * (_retry + 1)
                            logger.info(
                                f"[스마트스토어] 429 재시도 {_retry + 1}/3 ({wait}초 대기)"
                            )
                            await asyncio.sleep(wait)
                            continue
                        logger.warning(
                            f"[스마트스토어] last-changed-statuses 호출 실패 (from={cursor}): {_api_err}"
                        )
                        break
                if result is None:
                    return
                await asyncio.sleep(1.0)
                data = result.get("data", result) if isinstance(result, dict) else {}
                statuses_list = (
                    (
                        data.get("lastChangeStatuses", [])
                        or data.get("lastChangedStatuses", [])
                    )
                    if isinstance(data, dict)
                    else []
                )
                new_count = 0
                last_changed_date = None
                for s in statuses_list:
                    pid = s.get("productOrderId", "")
                    lcd = s.get("lastChangedDate")
                    if lcd and (last_changed_date is None or lcd > last_changed_date):
                        last_changed_date = lcd
                    if pid and pid not in seen_po_ids:
                        seen_po_ids.add(pid)
                        all_statuses.append(s)
                        new_count += 1
                # 종료 조건: 응답 비었거나 새 ID 0건이거나 커서 진전 없음
                if not statuses_list or new_count == 0 or not last_changed_date:
                    return
                if last_changed_date <= cursor:
                    return
                cursor = last_changed_date

        # since_str 시작 + 최근 1일 보강 호출 (이미 잡힌 건은 dedup으로 skip)
        await _fetch_with_pagination(since_str)
        recent = datetime.now(kst) - timedelta(days=1)
        recent_str = recent.strftime("%Y-%m-%dT%H:%M:%S.000+09:00")
        if recent_str > since_str:
            await _fetch_with_pagination(recent_str)

        logger.info(f"[스마트스토어] 주문 변경 조회 완료 — 총 {len(all_statuses)}건")

        if not all_statuses:
            return []

        statuses = all_statuses

        # 2단계: 주문 상세 조회
        po_ids = [s.get("productOrderId") for s in statuses if s.get("productOrderId")]
        if not po_ids:
            return []

        logger.info(f"[스마트스토어] 상세 조회 대상: {len(po_ids)}건")
        details_result = await self._call_api(
            "POST",
            "/v1/pay-order/seller/product-orders/query",
            body={"productOrderIds": po_ids[:300]},
        )

        details_data = (
            details_result.get("data", details_result)
            if isinstance(details_result, dict)
            else details_result
        )
        # data가 리스트이면 그대로 사용, 딕셔너리면 productOrders 키에서 추출
        if isinstance(details_data, list):
            orders_data = details_data
        elif isinstance(details_data, dict):
            orders_data = details_data.get("productOrders", [])
        else:
            orders_data = []
        logger.info(f"[스마트스토어] 주문 상세 결과: {len(orders_data)}건")
        # 디버그: 클레임 주문 응답 구조 확인용 (취소/반품/교환 있는 첫 건 덤프)
        for _dbg in orders_data:
            _po = _dbg.get("productOrder", _dbg) if isinstance(_dbg, dict) else _dbg
            _claim_top = (_po or {}).get("claimStatus") or (_po or {}).get("claimType")
            _claim_sub = (_dbg.get("claim") or {}) if isinstance(_dbg, dict) else {}
            if _claim_top or _claim_sub:
                logger.info(
                    f"[스마트스토어][클레임디버그] productOrderId={(_po or {}).get('productOrderId')} "
                    f"po.claimStatus={(_po or {}).get('claimStatus')} "
                    f"po.claimType={(_po or {}).get('claimType')} "
                    f"claim_sub={_claim_sub}"
                )
                break
        return orders_data

    async def get_product_orders_by_ids(
        self, po_ids: list[str]
    ) -> list[dict[str, Any]]:
        """productOrderId 목록으로 주문 상세 직접 조회 (last-changed 우회)."""
        if not po_ids:
            return []
        results: list[dict[str, Any]] = []
        for i in range(0, len(po_ids), 50):
            batch = po_ids[i : i + 50]
            data = await self._call_api(
                "POST",
                "/v1/pay-order/seller/product-orders/query",
                body={"productOrderIds": batch},
            )
            # 응답: data 키 안에 리스트
            inner = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(inner, list):
                results.extend(inner)
            elif isinstance(inner, dict):
                results.extend(inner.get("productOrders", []))
            if i + 50 < len(po_ids):
                await asyncio.sleep(1.0)
        return results

    async def confirm_product_orders(
        self, product_order_ids: list[str]
    ) -> dict[str, Any]:
        """발주확인 (placeOrderStatus: NOT_YET → OK).

        Commerce API: POST /v1/pay-order/seller/product-orders/confirm
        """
        result = await self._call_api(
            "POST",
            "/v1/pay-order/seller/product-orders/confirm",
            body={"productOrderIds": product_order_ids},
        )
        logger.info(f"[스마트스토어] 발주확인 {len(product_order_ids)}건 요청")
        return result

    # 택배사 코드 매핑 (한글 → 네이버 코드)
    # 네이버 커머스 API 공식 DeliveryCompanyType enum
    # 롯데택배 = HYUNDAI (구 현대택배 인수 → 레거시 코드 유지)
    DELIVERY_COMPANY_MAP: dict[str, str] = {
        "CJ대한통운": "CJGLS",
        "한진택배": "HANJIN",
        "롯데택배": "HYUNDAI",  # 롯데글로벌로지스 (구 현대택배)
        "로젠택배": "KGB",
        "우체국택배": "EPOST",
        "경동택배": "KDEXP",
        "대신택배": "DAESIN",
        "일양로지스": "ILYANG",
        "편의점택배": "CVSNET",
        "딜리박스": "JMNP",
        "DHL": "DHL",
        "기타": "DLV_COM_ETC",
    }

    # 한글 라벨 → 네이버 deliveryMethod enum
    # 매핑되지 않으면 기본값 "DELIVERY" 사용 (택배/등기/소포)
    DELIVERY_METHOD_MAP: dict[str, str] = {
        "직접배송": "DIRECT_DELIVERY",
        "직접전달": "DIRECT_DELIVERY",
        "방문수령": "VISIT_RECEIPT",
        "퀵서비스": "QUICK_SVC",
        "배송없음": "NOTHING",
    }

    async def ship_product_order(
        self,
        product_order_id: str,
        delivery_company: str,
        tracking_number: str,
    ) -> dict[str, Any]:
        """발송처리 (송장번호 전송).

        Commerce API: POST /v1/pay-order/seller/product-orders/dispatch

        Args:
          product_order_id: 상품주문번호
          delivery_company: 택배사 (한글 또는 네이버 코드)
          tracking_number: 송장번호
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # 한글 라벨에서 공백 제거 후 deliveryMethod 우선 판단
        # (택배 외 흐름은 deliveryCompanyCode를 보내지 않음)
        normalized = (delivery_company or "").replace(" ", "")
        delivery_method = self.DELIVERY_METHOD_MAP.get(normalized, "DELIVERY")
        now = datetime.now(ZoneInfo("Asia/Seoul")).strftime(
            "%Y-%m-%dT%H:%M:%S.000+09:00"
        )

        dispatch_item: dict[str, Any] = {
            "productOrderId": product_order_id,
            "deliveryMethod": delivery_method,
            "dispatchDate": now,
        }
        if delivery_method == "DELIVERY":
            # 택배 흐름: 회사 코드 + 송장번호 필수
            dispatch_item["deliveryCompanyCode"] = self.DELIVERY_COMPANY_MAP.get(
                delivery_company, delivery_company
            )
            dispatch_item["trackingNumber"] = tracking_number
        else:
            # 직접배송/방문수령/퀵서비스/배송없음: 송장 필드 자체를 보내지 않음
            # (송장번호가 들어와도 의미가 없으므로 생략)
            pass

        body = {"dispatchProductOrders": [dispatch_item]}
        result = await self._call_api(
            "POST",
            "/v1/pay-order/seller/product-orders/dispatch",
            body=body,
        )
        # 응답에서 성공/실패 확인
        data = result.get("data", {})
        success_ids = data.get("successProductOrderIds", [])
        fail_infos = data.get("failProductOrderInfos", [])

        if fail_infos:
            fail_msg = fail_infos[0].get("message", "알 수 없는 오류")
            # 이미 발송처리된 주문은 마켓 입장에서 송장이 등록된 상태 → silent success
            # (cafe24 422 silent 패턴과 동일, 재시도/재큐잉으로 인한 중복 호출 방어)
            if any(k in fail_msg for k in ("이미 발송", "이미 처리")):
                logger.info(
                    f"[스마트스토어] 발송처리 {product_order_id} 이미 처리됨 → 성공 처리: {fail_msg}"
                )
                return result
            raise SmartStoreApiError(f"발송처리 실패: {fail_msg}")

        logger.info(
            f"[스마트스토어] 발송처리 {product_order_id} → method={delivery_method} "
            f"company={dispatch_item.get('deliveryCompanyCode', '-')} tracking={tracking_number or '-'}"
        )
        return result

    async def approve_cancel(self, product_order_id: str) -> dict[str, Any]:
        """취소요청 승인.

        Commerce API: POST /v1/pay-order/seller/product-orders/{id}/claim/cancel/approve
        """
        result = await self._call_api(
            "POST",
            f"/v1/pay-order/seller/product-orders/{product_order_id}/claim/cancel/approve",
        )
        logger.info(f"[스마트스토어] 취소승인 완료: {product_order_id}")
        return result

    async def request_cancel(
        self,
        product_order_id: str,
        cancel_reason: str = "SOLD_OUT",
        cancel_detailed_reason: str = "",
    ) -> dict[str, Any]:
        """판매자 주도 취소 요청.

        Commerce API: POST /v1/pay-order/seller/product-orders/{id}/claim/cancel/request
        cancelReason: SOLD_OUT / INTENT_CHANGED / COLOR_AND_SIZE /
                      WRONG_ORDER / DELAYED_DELIVERY / INCORRECT_INFO
        """
        body: dict[str, Any] = {"cancelReason": cancel_reason}
        if cancel_detailed_reason:
            body["cancelDetailedReason"] = cancel_detailed_reason
        result = await self._call_api(
            "POST",
            f"/v1/pay-order/seller/product-orders/{product_order_id}/claim/cancel/request",
            body=body,
        )
        logger.info(
            f"[스마트스토어] 판매자취소 완료: {product_order_id} (사유={cancel_reason})"
        )
        return result

    async def reject_exchange(
        self, product_order_id: str, reason: str = "판매자 교환 거부"
    ) -> dict[str, Any]:
        """교환요청 거부.

        Commerce API: POST /v1/pay-order/seller/product-orders/{id}/claim/exchange/reject
        rejectExchangeReason 필수: PRODUCT_INSUFFICIENT, ETC 등
        """
        result = await self._call_api(
            "POST",
            f"/v1/pay-order/seller/product-orders/{product_order_id}/claim/exchange/reject",
            body={"rejectExchangeReason": reason},
        )
        logger.info(f"[스마트스토어] 교환거부 완료: {product_order_id}")
        return result

    async def approve_exchange(self, product_order_id: str) -> dict[str, Any]:
        """교환 재배송 승인.

        Commerce API: POST /v1/pay-order/seller/product-orders/{id}/claim/exchange/approve
        """
        result = await self._call_api(
            "POST",
            f"/v1/pay-order/seller/product-orders/{product_order_id}/claim/exchange/approve",
        )
        logger.info(f"[스마트스토어] 교환재배송 승인 완료: {product_order_id}")
        return result

    async def convert_exchange_to_return(self, product_order_id: str) -> dict[str, Any]:
        """교환 → 반품 변경 (교환 거부 처리).

        교환 거부 사유에 '반품으로 변경'을 명시.
        반품 요청은 구매자가 직접 진행해야 하므로 교환 거부까지만 API 호출.
        """
        result = await self._call_api(
            "POST",
            f"/v1/pay-order/seller/product-orders/{product_order_id}/claim/exchange/reject",
            body={"rejectExchangeReason": "반품으로 변경"},
        )
        logger.info(
            f"[스마트스토어] 교환→반품 변경 완료 (교환거부): {product_order_id}"
        )
        return result

    async def approve_return(self, product_order_id: str) -> dict[str, Any]:
        """반품 승인.

        Commerce API: POST /v1/pay-order/seller/product-orders/{id}/claim/return/approve
        """
        result = await self._call_api(
            "POST",
            f"/v1/pay-order/seller/product-orders/{product_order_id}/claim/return/approve",
        )
        # 응답 검증: data 안에 실패 정보가 있을 수 있음
        data = result.get("data", result) if isinstance(result, dict) else result
        if isinstance(data, dict):
            fail_infos = (
                data.get("failProductOrderInfos") or data.get("failReturns") or []
            )
            if fail_infos:
                fail_msg = (
                    fail_infos[0].get("message", "")
                    if isinstance(fail_infos[0], dict)
                    else str(fail_infos[0])
                )
                logger.error(f"[스마트스토어] 반품승인 실패 응답: {fail_infos}")
                raise SmartStoreApiError(f"반품승인 실패: {fail_msg}")
        logger.info(f"[스마트스토어] 반품승인 완료: {product_order_id} 응답: {result}")
        return result

    async def release_return_hold(self, product_order_id: str) -> dict[str, Any]:
        """반품 환불보류 해제.

        Commerce API: POST /v1/pay-order/seller/product-orders/{id}/claim/return/holdback/release
        """
        result = await self._call_api(
            "POST",
            f"/v1/pay-order/seller/product-orders/{product_order_id}/claim/return/holdback/release",
        )
        logger.info(f"[스마트스토어] 반품 보류해제 완료: {product_order_id}")
        return result

    async def reject_return(
        self, product_order_id: str, reason: str = "판매자 반품 거부"
    ) -> dict[str, Any]:
        """반품 거부.

        Commerce API: POST /v1/pay-order/seller/product-orders/{id}/claim/return/reject
        """
        result = await self._call_api(
            "POST",
            f"/v1/pay-order/seller/product-orders/{product_order_id}/claim/return/reject",
            body={"rejectReturnReason": reason},
        )
        logger.info(f"[스마트스토어] 반품거부 완료: {product_order_id}")
        return result

    # ------------------------------------------------------------------
    # 상품 데이터 변환 (수집 상품 → 스마트스토어 형식)
    # ------------------------------------------------------------------

    @staticmethod
    def transform_product(
        product: dict[str, Any],
        category_id: str = "",
    ) -> dict[str, Any]:
        """SambaCollectedProduct → 스마트스토어 상품 등록 데이터 변환."""
        # 스마트스토어 상품명: market_names 우선, 없으면 name에서 49자 슬라이스
        # 스마트스토어 금지 특수문자 제거: \ * ? " < >
        _re_ss_special = re.compile(r'[\\*?"<>]')
        market_names = product.get("market_names") or {}
        ss_name = market_names.get("스마트스토어", "")
        if ss_name:
            product_name = _re_ss_special.sub("", ss_name).strip()[:49]
        else:
            raw_name = product.get("name", "")
            product_name = _re_ss_special.sub("", raw_name).strip()[:49]

        images_raw = product.get("images") or []

        representative = {"url": images_raw[0]} if images_raw else {}
        # 수집 시 이미 상세이미지로 보충됨 — images[1:5] 그대로 사용
        optional = [{"url": u} for u in images_raw[1:5]]

        desired_price = int(product.get("sale_price", 0))
        if desired_price <= 0:
            desired_price = int(product.get("original_price", 0)) or 10000

        # 300원 올림 → 25% 역산(÷0.75)이 항상 100원 단위로 정확히 나눠짐
        # 예) 89,600 → 89,700 / 0.75 = 119,600 (100원 단위 정확)
        desired_price = math.ceil(desired_price / 300) * 300
        discount_rate = 25
        sale_price = desired_price * 4 // 3
        immediate_discount = True

        # (주) 제거 함수
        def _clean_company(name: str) -> str:
            if not name:
                return name
            return re.sub(r"\(주\)|㈜|\(株\)", "", name).strip()

        brand = product.get("brand", "") or "상세설명 참조"
        # 제조사: manufacturer → brand → "상세설명 참조" 순으로 폴백
        raw_mfr = _clean_company(product.get("manufacturer", ""))
        mfr = (
            raw_mfr
            if (
                raw_mfr and raw_mfr != "상세설명 참조" and raw_mfr != "상세 이미지 참조"
            )
            else ""
        )
        if not mfr:
            raw_brand = _clean_company(product.get("brand", ""))
            mfr = (
                raw_brand
                if (
                    raw_brand
                    and raw_brand != "상세설명 참조"
                    and raw_brand != "상세 이미지 참조"
                )
                else "상세설명 참조"
            )

        # 옵션에서 사이즈 정보 추출
        options = product.get("options") or []
        sizes = [
            o.get("size", "") or o.get("name", "")
            for o in options
            if o.get("size") or o.get("name")
        ]
        size_text = (
            ", ".join(sorted(set(s for s in sizes if s)))[:200] or "상세설명 참조"
        )

        # 색상: DB 필드 우선, 상품명에서 추출
        color_part = ""
        if " - " in product.get("name", ""):
            color_part = product["name"].split(" - ", 1)[1].split("/")[0].strip()
        db_color = product.get("color", "")
        color_text = db_color or (
            color_part[:200] if color_part else "상세 이미지 참조"
        )

        # 재고수량: 설정값 > 정책 제한 > 실재고 순 우선
        setting_stock = product.get("_stock_quantity", 0)
        max_stock = product.get("_max_stock", 0)
        real_stock = (
            sum((o.get("stock") or 0) for o in options if not o.get("isSoldOut"))
            if options
            else 999
        )
        if setting_stock and setting_stock > 0:
            stock_qty = setting_stock
        elif max_stock and max_stock > 0:
            stock_qty = min(max_stock, real_stock) if real_stock > 0 else max_stock
        else:
            stock_qty = real_stock if real_stock > 0 else 999

        # 모델명/품번 — 공통 컬럼 우선, kream_data 폴백
        style_code = product.get("style_code", "") or product.get("styleCode", "")
        if not style_code:
            kream = product.get("kream_data") or {}
            if isinstance(kream, dict):
                style_code = kream.get("styleCode", "")

        # 성별 — 수집 데이터에 명시된 경우만 사용, 없으면 남녀공용
        sex_raw = product.get("sex", "")
        sex_list: list[str] = []
        if sex_raw:
            sex_list = [sex_raw] if isinstance(sex_raw, str) else list(sex_raw)

        # 시즌 — 공통 컬럼
        season = product.get("season", "") or ""

        # 상품 속성 구성 (카테고리별 API 속성 기반)
        product_attributes: list[dict[str, Any]] = []
        cat_attrs = product.get("_category_attributes") or []

        # INPUT/RANGE 타입(단위 있는 측정값) 식별 — attributeRealValue 필요한 속성은 매칭 제외
        def _is_input_type(a: dict[str, Any]) -> bool:
            atype = (a.get("attributeType") or a.get("type") or "").upper()
            if atype in {"INPUT", "RANGE", "REAL", "TEXT", "NUMBER"}:
                return True
            # 단위(unitName/unitCode/usableUnitCodes) 보유 → 측정값 입력형
            if a.get("unitName") or a.get("unitCode") or a.get("usableUnitCodes"):
                return True
            return False

        # 성별 속성 — API 속성에서 성별 seq 찾기, 기본값 남녀공용
        _GENDER_KEYWORDS = {"남성용", "여성용", "남녀공용", "공용", "유니섹스"}
        gender_seq = None
        gender_values: dict[str, int] = {}
        for a in cat_attrs:
            if _is_input_type(a):
                continue
            val = a.get("minAttributeValue", "") or a.get("attributeValueName", "")
            vseq = a.get("attributeValueSeq", 0) or 0
            if val in _GENDER_KEYWORDS and vseq > 0:
                gender_seq = a["attributeSeq"]
                gender_values[val] = vseq

        if gender_seq:
            # 수집 데이터에서 성별 판단
            if sex_list:
                sex_val = sex_list[0] if isinstance(sex_list, list) else str(sex_list)
                if "공용" in sex_val or "남녀" in sex_val or "유니" in sex_val:
                    target = "남녀공용"
                elif "남" in sex_val:
                    target = "남성용"
                elif "여" in sex_val:
                    target = "여성용"
                else:
                    target = "남녀공용"
            else:
                target = "남녀공용"
            if target in gender_values:
                product_attributes.append(
                    {
                        "attributeSeq": gender_seq,
                        "attributeValueSeq": gender_values[target],
                    }
                )

        # 사용계절 속성 — 수집된 season 값 기반 매핑
        _SEASON_KEYWORDS = {"봄", "여름", "가을", "겨울"}
        _SEASON_MAP: dict[str, list[str]] = {
            "SS": ["봄", "여름"],
            "ALL SS": ["봄", "여름"],
            "FW": ["가을", "겨울"],
            "ALL FW": ["가을", "겨울"],
            "ALL": ["봄", "여름", "가을", "겨울"],
        }
        # 연도 접두어 제거: "ALL ALL FW" → "ALL FW", "2025 FW" → "FW"
        season_key = season.strip().upper()
        parts = season_key.split(None, 1)
        if len(parts) == 2 and (parts[0].isdigit() or parts[0] == "ALL"):
            season_key = parts[1]
        target_seasons = _SEASON_MAP.get(season_key, ["봄", "여름", "가을", "겨울"])
        season_seq = None
        season_values: dict[str, int] = {}
        for a in cat_attrs:
            if _is_input_type(a):
                continue
            val = a.get("minAttributeValue", "") or a.get("attributeValueName", "")
            vseq = a.get("attributeValueSeq", 0) or 0
            if val in _SEASON_KEYWORDS and vseq > 0:
                season_seq = a["attributeSeq"]
                season_values[val] = vseq
        if season_seq:
            for s in target_seasons:
                if s in season_values:
                    product_attributes.append(
                        {
                            "attributeSeq": season_seq,
                            "attributeValueSeq": season_values[s],
                        }
                    )

        # 종류 속성 — 카테고리(category1~4)로 추정하여 매칭
        type_seq = None
        type_values: dict[str, int] = {}
        _TYPE_SKIP = _GENDER_KEYWORDS | _SEASON_KEYWORDS | {"기타", "해당없음"}
        for a in cat_attrs:
            if _is_input_type(a):
                continue
            val = a.get("minAttributeValue", "") or a.get("attributeValueName", "")
            seq = a.get("attributeSeq", 0)
            vseq = a.get("attributeValueSeq", 0) or 0
            if (
                val
                and vseq > 0
                and val not in _TYPE_SKIP
                and seq != gender_seq
                and seq != season_seq
            ):
                if type_seq is None:
                    type_seq = seq
                if seq == type_seq:
                    type_values[val] = vseq

        if type_seq and type_values:
            cat_keywords = [
                product.get("category1", ""),
                product.get("category2", ""),
                product.get("category3", ""),
                product.get("category4", ""),
            ]
            cat_text = " ".join(c for c in cat_keywords if c)
            matched_type = None
            for type_name in type_values:
                if type_name in cat_text:
                    matched_type = type_name
                    break
            # 카테고리 텍스트와 매칭 실패 시 종류 속성 미전송 (오매칭 방지)
            if matched_type:
                product_attributes.append(
                    {
                        "attributeSeq": type_seq,
                        "attributeValueSeq": type_values[matched_type],
                    }
                )

        data: dict[str, Any] = {
            "originProduct": {
                "statusType": "SALE",
                "saleType": "NEW",
                "leafCategoryId": category_id or "50000803",
                "name": product_name,
                # 판매자상품코드 — 삼바 내부 product.id로 통일 (주문 역매칭 키)
                "sellerCodeInfo": {
                    "sellerManagementCode": str(product.get("id") or style_code or "")
                },
                "detailContent": product.get("detail_html", "")
                or f'<div style="text-align:center; padding:30px 0;"><p style="font-size:18px; font-weight:bold;">{product_name}</p><p style="margin-top:10px; color:#666;">상세 정보는 상품 이미지를 참조해주세요.</p></div>',
                "images": {
                    "representativeImage": representative,
                    "optionalImages": optional,
                },
                "salePrice": sale_price,
                "stockQuantity": stock_qty,
                "deliveryInfo": {
                    "deliveryType": "DELIVERY",
                    "deliveryAttributeType": "NORMAL",
                    "deliveryCompany": "CJGLS",
                    "deliveryFee": {
                        "deliveryFeeType": product.get("_delivery_fee_type", "FREE"),
                        "deliveryFeePayType": "PREPAID",
                        "baseFee": product.get("_delivery_base_fee", 0),
                        "deliveryFeeByArea": {
                            "deliveryAreaType": "AREA_2",
                            "area2extraFee": product.get("_jeju_fee", 3000),
                        },
                    },
                    "claimDeliveryInfo": {
                        "returnDeliveryFee": product.get("_return_fee", 3000),
                        "exchangeDeliveryFee": product.get("_exchange_fee", 6000),
                        **(
                            {"freeReturnInsuranceYn": True}
                            if product.get("_return_safeguard")
                            else {}
                        ),
                    },
                },
                "detailAttribute": {
                    "afterServiceInfo": {
                        "afterServiceTelephoneNumber": _format_phone(
                            product.get("_as_phone", "") or "상세페이지 참조"
                        ),
                        "afterServiceGuideContent": product.get("_as_message", "")
                        or "상세페이지 참조",
                    },
                    "originAreaInfo": _build_origin_area(product.get("origin", "")),
                    "minorPurchasable": True,
                    "productInfoProvidedNotice": _build_ss_notice(
                        product,
                        color_text=color_text,
                        size_text=(f"발길이(mm): {size_text}")[:200]
                        if sizes
                        else "FREE (상세 이미지 참조)",
                        mfr=mfr,
                        brand=brand,
                        ss_category_id=category_id,
                    ),
                    **(
                        {"optionInfo": _opt_result}
                        if options
                        and (
                            _opt_result := _build_combination_options(
                                options,
                                sale_price,
                                max_stock_per_option=stock_qty,
                                option_deletion_words=product.get(
                                    "_option_deletion_words"
                                ),
                                option_group_names=product.get("option_group_names"),
                            )
                        )
                        else {}
                    ),
                    **_build_certification_infos(product.get("_certification_infos")),
                },
            },
            "smartstoreChannelProduct": {
                "channelProductName": product_name,
                "storeKeepExclusiveProduct": False,
                "naverShoppingRegistration": product.get("_naver_shopping", True),
                "channelProductDisplayStatusType": "ON",
            },
        }

        # NOTE: Naver Commerce v2 API 는 inline productAddItems 미지원
        # → addon_options 은 Musinsa collector 에서 메인×엑스트라 2D 조합 SKU로 통합되어
        #   options 에 들어있음 (optionCombinations 로 등록됨). 별도 productAddItems 빌드 불필요.

        # 즉시할인 적용
        if immediate_discount:
            data["originProduct"]["customerBenefit"] = {
                "immediateDiscountPolicy": {
                    "discountMethod": {
                        "value": discount_rate,
                        "unitType": "PERCENT",
                    },
                },
            }

        # 모델명/품번 입력 — style_code 없으면 상품명에서 추출 시도
        if not style_code:
            # 상품명에서 영숫자 품번 패턴 추출 (예: DUF24G03R2)
            code_match = re.search(r"[A-Z]{2,}[\dA-Z]{4,}", product.get("name", ""))
            if code_match:
                style_code = code_match.group()
        if style_code:
            # 품번 = manufactureDefineNo (셀러센터 "품번" 필드)
            # 허용 문자만 유지: 영문·숫자·-_./ (공백·+·한글 등 제거)
            sanitized_style = re.sub(r"[^A-Za-z0-9\-_./]", "", style_code)
            if sanitized_style:
                data["originProduct"]["detailAttribute"]["manufactureDefineNo"] = (
                    sanitized_style
                )

        # 브랜드명 정제 — brandId가 이미 있으면(카탈로그 매칭 완료) 정제 스킵
        # brandId 없을 때만 접미사 제거하여 검색 성공률 높임
        if not product.get("_brand_id"):
            import re as _re_brand

            _brand_suffixes = r"\s*(키즈|kids|kid|주니어|junior|jr|아동|유아|베이비|baby|우먼|women|맨즈|men|골프|golf|스포츠|sports|아웃도어|outdoor)\s*$"
            if brand:
                brand = (
                    _re_brand.sub(
                        _brand_suffixes, "", brand, flags=_re_brand.IGNORECASE
                    ).strip()
                    or brand
                )
            if mfr:
                mfr = (
                    _re_brand.sub(
                        _brand_suffixes, "", mfr, flags=_re_brand.IGNORECASE
                    ).strip()
                    or mfr
                )

        # 브랜드/제조사 — naverShoppingSearchInfo에 설정 (스마트스토어 상품주요정보)
        # brandName은 네이버에 등록된 브랜드만 허용 — brandId 있을 때만 전송
        # 네이버 금지문자(\ * ? " < >) 및 URL 패턴 최종 방어 — 소싱처 파서 누락 대비
        def _sanitize_naver_name(v: str) -> str:
            if not v:
                return ""
            v = re.split(r"https?://|www\.", v, maxsplit=1)[0].strip()
            v = re.sub(r'[\\*?"<>]', "", v).strip()
            return v

        brand = _sanitize_naver_name(brand)
        mfr = _sanitize_naver_name(mfr)

        naver_search_info: dict[str, Any] = {}
        brand_id = product.get("_brand_id")
        mfr_id = product.get("_manufacturer_id")
        if brand_id:
            naver_search_info["brandId"] = brand_id
            naver_search_info["brandName"] = brand
        # brandId 없으면 brandName 전송하지 않음 (미등록 브랜드 에러 방지)
        if mfr_id:
            naver_search_info["manufacturerId"] = mfr_id
            naver_search_info["manufacturerName"] = mfr[:50]
        elif mfr and mfr != "상세설명 참조":
            naver_search_info["manufacturerName"] = mfr[:50]
        # 카탈로그 모델 ID — 설정하면 모델명/브랜드/제조사/상품속성 자동 매칭
        catalog_model_id = product.get("_catalog_model_id")
        if catalog_model_id:
            naver_search_info["modelId"] = catalog_model_id
        else:
            # 모델명 ← 원상품명(product.name), 50자 제한
            origin_name = _sanitize_naver_name(product.get("name") or "")
            if origin_name:
                naver_search_info["modelName"] = origin_name[:50]
            # 제조사 모델명 ← 품번(style_code), 50자 제한
            if style_code:
                clean_code = _sanitize_naver_name(style_code)[:50].strip()
                if clean_code:
                    naver_search_info["manufacturerModelName"] = clean_code
        if naver_search_info:
            logger.info(f"[스마트스토어] naverShoppingSearchInfo: {naver_search_info}")
            data["originProduct"]["detailAttribute"]["naverShoppingSearchInfo"] = (
                naver_search_info
            )

        # 상품속성 (성별, 시즌 등)
        if product_attributes:
            data["originProduct"]["detailAttribute"]["productAttributes"] = (
                product_attributes
            )

        # 복수구매할인
        if product.get("_multi_purchase"):
            multi_qty = product.get("_multi_purchase_qty", 2)
            multi_rate = product.get("_multi_purchase_rate", 1)
            benefit = data["originProduct"].get("customerBenefit", {})
            benefit["multiPurchaseDiscountPolicy"] = {
                "discountMethod": {
                    "value": multi_rate,
                    "unitType": "PERCENT",
                },
                "orderValue": multi_qty,
                "orderValueUnitType": "COUNT",
            }
            data["originProduct"]["customerBenefit"] = benefit

        # 포인트/리뷰 정책 (customerBenefit 하위)
        if product.get("_purchase_point"):
            benefit = data["originProduct"].get("customerBenefit", {})
            rate = product.get("_purchase_point_rate", 1)
            benefit["purchasePointPolicy"] = {
                "pointPayYn": True,
                "value": rate,
                "unitType": "PERCENT",
            }
            data["originProduct"]["customerBenefit"] = benefit

        if product.get("_review_point"):
            benefit = data["originProduct"].get("customerBenefit", {})
            review_policy: dict[str, Any] = {"reviewPointPayYn": True}
            text_pt = product.get("_review_text_point", 0)
            photo_pt = product.get("_review_photo_point", 0)
            month_text_pt = product.get("_review_month_text_point", 0)
            month_photo_pt = product.get("_review_month_photo_point", 0)
            if text_pt:
                review_policy["textReviewPoint"] = text_pt
            if photo_pt:
                review_policy["photoVideoReviewPoint"] = photo_pt
            if month_text_pt:
                review_policy["afterUseTextReviewPoint"] = month_text_pt
            if month_photo_pt:
                review_policy["afterUsePhotoVideoReviewPoint"] = month_photo_pt
            benefit["reviewPointPolicy"] = review_policy
            data["originProduct"]["customerBenefit"] = benefit

        # 알림받기 동의고객 포인트: Commerce API v2 미지원 → 셀러센터에서 직접 설정

        # 태그 → originProduct.detailAttribute.seoInfo.sellerTags (네이버 커머스 API v2.67)
        tags = product.get("tags") or []
        # 시스템 마커 + 브랜드 + 상품명 + 카테고리 포함 태그 제외
        brand_lower = brand.lower() if brand else ""
        name_lower = (product.get("name", "") or "").lower()
        # 카테고리 단어 추출 (스마트스토어는 카테고리명을 태그 금지어 처리)
        import re as _re

        _cat_words: set[str] = set()
        for ck in ("category1", "category2", "category3", "category4", "category"):
            cv = product.get(ck, "")
            if cv:
                for w in _re.split(r"[\s>/\-]+", cv):
                    cw = w.strip().lower()
                    if len(cw) >= 2:
                        _cat_words.add(cw)
        seller_tags = []
        seen: set[str] = set()
        for t in tags:
            if t.startswith("__"):
                continue
            tl = t.lower()
            tl_nospace = tl.replace(" ", "")
            # 중복 제거 (공백 무시)
            if tl_nospace in seen:
                continue
            seen.add(tl_nospace)
            if brand_lower and brand_lower in tl:
                continue
            if tl in name_lower:
                continue
            # 카테고리 단어와 정확 일치하면 제외 (부분 포함은 네이버 API가 판단)
            if tl in _cat_words:
                continue
            seller_tags.append(t)
            if len(seller_tags) >= 10:
                break
        if seller_tags:
            data["originProduct"]["detailAttribute"]["seoInfo"] = {
                "sellerTags": [{"text": t} for t in seller_tags],
            }
            logger.info(
                f"[스마트스토어] sellerTags {len(seller_tags)}개 전송: {seller_tags[:3]}..."
            )

        review_photo = product.get("_review_photo_url")
        if review_photo:
            benefit = data["originProduct"].get("customerBenefit", {})
            if "reviewPointPolicy" not in benefit:
                benefit["reviewPointPolicy"] = {}
            benefit["reviewPointPolicy"]["reviewPhotoBenefitImageUrl"] = review_photo
            data["originProduct"]["customerBenefit"] = benefit

        return data

    # ------------------------------------------------------------------
    # 그룹상품 API
    # ------------------------------------------------------------------

    async def get_purchase_option_guides(self, category_id: str) -> list:
        """카테고리별 표준 판매옵션 가이드 조회.
        빈 리스트 반환 시 해당 카테고리는 그룹상품 미지원.
        """
        try:
            data = await self._call_api(
                "GET",
                "/v2/standard-purchase-option-guides",
                params={"categoryId": category_id},
            )
            return data.get("contents", [])
        except SmartStoreApiError:
            return []  # API 에러 시 그룹상품 미지원으로 간주

    async def register_group_product(self, payload: dict) -> dict:
        """그룹상품 등록 (비동기). 결과는 poll_group_status로 확인."""
        return await self._call_api("POST", "/v2/standard-group-products", body=payload)

    async def poll_group_status(self, max_wait: int = 120) -> dict:
        """그룹상품 등록/수정 결과 폴링. 최대 max_wait초 대기. 지수백오프."""
        import asyncio as _asyncio

        start = time.time()
        attempt = 0
        while time.time() - start < max_wait:
            result = await self._call_api("GET", "/v2/standard-group-products/status")
            state = result.get("progress", {}).get("state", "")
            if state == "COMPLETED":
                return result
            elif state in ("ERROR", "FAILED"):
                error_msg = result.get("errorMessage", "알 수 없는 오류")
                raise SmartStoreApiError(f"그룹상품 등록 실패: {state} - {error_msg}")
            # 지수백오프: 0.5s → 1s → 2s → 3s (최대)
            wait = min(0.5 * (2**attempt), 3)
            await _asyncio.sleep(wait)
            attempt += 1
        raise TimeoutError("그룹상품 등록 타임아웃 (2분 초과)")

    @staticmethod
    def transform_group_product(
        products: list[dict],
        category_id: str,
        guide_id: int,
        account_settings: dict,
    ) -> dict:
        """수집 상품 리스트 → 그룹상품 API 페이로드 변환.

        Args:
            products: 같은 group_key를 가진 상품 리스트 (이미지 업로드 완료 상태)
            category_id: 스마트스토어 리프 카테고리 ID
            guide_id: 판매옵션 가이드 ID
            account_settings: 계정 설정 (A/S, 배송, 할인 등)
        """
        first = products[0]
        brand = first.get("brand", "")

        # 그룹 상품명: market_names 우선, 없으면 모델명(색상 제거) → 50자 슬라이스
        # 스마트스토어 금지 특수문자 제거: \ * ? " < >
        _re_ss_special = re.compile(r'[\\*?"<>]')
        first_market_names = first.get("market_names") or {}
        ss_group_name = first_market_names.get("스마트스토어", "")
        if ss_group_name:
            group_name = _re_ss_special.sub("", ss_group_name).strip()[:50]
        else:
            name = first.get("name", "")
            name_base = name.split(" - ", 1)[0].strip() if " - " in name else name
            group_name = _re_ss_special.sub("", name_base).strip()[:50]

        # A/S 정보
        as_phone = account_settings.get("asPhone", "") or "상세페이지 참조"
        as_message = account_settings.get("asMessage", "") or "상세페이지 참조"

        # 고시정보 (첫 상품 기준) - 기존 _build_ss_notice 사용
        mfr = first.get("manufacturer", "") or brand or "상세 이미지 참조"
        notice = _build_ss_notice(
            first,
            color_text="상세 이미지 참조",
            size_text="상세 이미지 참조",
            mfr=mfr,
            brand=brand,
        )

        # 공통 상세 HTML
        common_detail = first.get("detail_html", "")

        # 원산지 정보
        origin_area = _build_origin_area(first.get("origin", ""))

        # 개별 상품(specificProducts) 구성
        specific_products = []
        for p in products:
            color = p.get("color", "") or "기본"
            _desired_price = (
                p.get("_final_sale_price")
                or p.get("sale_price")
                or p.get("original_price", 0)
            )
            # 300원 올림 + 25% 역산: 정가·결제가 모두 100원 단위 보장
            _dp = math.ceil(int(_desired_price) / 300) * 300
            sale_price = _dp * 4 // 3
            stock = int(account_settings.get("stockQuantity", 0)) or 999

            # 옵션에서 재고 계산
            options = p.get("options") or []
            if options:
                total_stock = sum(
                    o.get("stock", 0) for o in options if not o.get("isSoldOut", False)
                )
                if total_stock > 0:
                    stock = min(stock, total_stock)

            # 이미지
            images_list = p.get("images") or []
            representative = {"url": images_list[0]} if images_list else {"url": ""}
            optional_imgs = [{"url": url} for url in images_list[1:5]]

            # 배송정보
            delivery_info = {
                "deliveryType": "DELIVERY",
                "deliveryAttributeType": "NORMAL",
                "deliveryCompany": "CJGLS",
                "deliveryFee": {
                    "deliveryFeeType": p.get("_delivery_fee_type", "FREE"),
                    "deliveryFeePayType": "PREPAID",
                    "baseFee": p.get("_delivery_base_fee", 0),
                    "deliveryFeeByArea": {
                        "deliveryAreaType": "AREA_2",
                        "area2extraFee": int(account_settings.get("jejuFee", 3000)),
                    },
                },
                "claimDeliveryInfo": {
                    "returnDeliveryFee": int(account_settings.get("returnFee", 3000)),
                    "exchangeDeliveryFee": int(
                        account_settings.get("exchangeFee", 6000)
                    ),
                },
            }
            if account_settings.get("returnSafeguard"):
                delivery_info["claimDeliveryInfo"]["freeReturnInsuranceYn"] = True

            sp = {
                "standardPurchaseOptions": [{"valueName": color}],
                "salePrice": int(sale_price),
                "stockQuantity": stock,
                "images": {
                    "representativeImage": representative,
                    "optionalImages": optional_imgs,
                },
                "deliveryInfo": delivery_info,
                "originAreaInfo": origin_area,
                "smartstoreChannelProduct": {
                    "naverShoppingRegistration": account_settings.get(
                        "naverShopping", True
                    ),
                    "channelProductDisplayStatusType": "ON",
                },
            }

            # 판매자상품코드 — 삼바 내부 product.id로 통일 (주문 역매칭 키)
            style_code = p.get("style_code", "")
            sp["sellerCodeInfo"] = {
                "sellerManagementCode": str(p.get("id") or style_code or "")
            }

            # 기존 상품번호 (수정용)
            existing_no = p.get("_origin_product_no")
            if existing_no:
                sp["originProductNo"] = int(existing_no)

            # 즉시할인: 25% 고정 (SmartStore 판매가 10원 단위 제약)
            sp["immediateDiscountPolicy"] = {
                "discountMethod": {
                    "value": 25,
                    "unitType": "PERCENT",
                }
            }

            specific_products.append(sp)

        payload = {
            "groupProduct": {
                "leafCategoryId": category_id,
                "name": group_name,
                "guideId": guide_id,
                "brandName": brand,
                "minorPurchasable": True,
                "saleType": "NEW",
                "productInfoProvidedNotice": notice,
                "afterServiceInfo": {
                    "afterServiceTelephoneNumber": _format_phone(as_phone),
                    "afterServiceGuideContent": as_message,
                },
                "commonDetailContent": common_detail,
                "specificProducts": specific_products,
                "smartstoreGroupChannel": {},
            }
        }

        # 브랜드 ID
        brand_id = first.get("_brand_id")
        if brand_id:
            payload["groupProduct"]["brandId"] = int(brand_id)

        # SEO 태그
        tags = first.get("tags") or []
        seller_tags = [{"text": t} for t in tags[:10] if t and not t.startswith("__")]
        if seller_tags:
            payload["groupProduct"]["seoInfo"] = {"sellerTags": seller_tags}

        return payload

    # ------------------------------------------------------------------
    # CS 문의 관리
    # ------------------------------------------------------------------

    async def get_inquiries(
        self,
        from_date: str = "",
        to_date: str = "",
        page: int = 1,
        size: int = 100,
        answered: Optional[bool] = None,
    ) -> dict[str, Any]:
        """고객 문의(Q&A) 목록 조회.

        GET /v1/contents/qnas

        Args:
          from_date: 조회 시작일시 (ISO 8601, 예: 2026-03-01T00:00:00.000+09:00)
          to_date: 조회 종료일시 (ISO 8601)
          page: 페이지 번호 (1부터)
          size: 페이지 크기 (최대 100)
          answered: 답변 여부 (None=전체, True=답변완료, False=미답변)
        """
        params: dict[str, Any] = {
            "page": page,
            "size": size,
        }
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date
        if answered is not None:
            params["answered"] = str(answered).lower()

        return await self._call_api("GET", "/v1/contents/qnas", params=params)

    async def get_purchase_inquiries(
        self,
        start_date: str = "",
        end_date: str = "",
        page: int = 1,
        size: int = 100,
        answered: Optional[bool] = None,
    ) -> dict[str, Any]:
        """고객문의(구매 후 1:1 문의) 목록 조회.

        GET /v1/pay-user/inquiries

        Args:
          start_date: 조회 시작일 (LocalDate, 예: 2026-03-01)
          end_date: 조회 종료일 (LocalDate, 예: 2026-03-26)
          page: 페이지 번호 (1부터)
          size: 페이지 크기 (최대 100)
          answered: 답변 여부 필터
        """
        params: dict[str, Any] = {
            "page": page,
            "size": size,
        }
        if start_date:
            params["startSearchDate"] = start_date
        if end_date:
            params["endSearchDate"] = end_date
        if answered is not None:
            params["answered"] = str(answered).lower()

        return await self._call_api("GET", "/v1/pay-user/inquiries", params=params)

    async def answer_product_qna(
        self,
        question_id: int,
        comment_content: str,
    ) -> dict[str, Any]:
        """상품문의(Q&A) 답변 등록/수정.

        PUT /v1/contents/qnas/{questionId}
        응답: 204 No Content (성공 시 빈 body)
        """
        return await self._call_api(
            "PUT",
            f"/v1/contents/qnas/{question_id}",
            body={"commentContent": comment_content},
        )

    async def answer_inquiry(
        self,
        inquiry_no: int,
        answer_comment: str,
    ) -> dict[str, Any]:
        """고객문의(1:1) 답변 등록.

        POST /v1/pay-merchant/inquiries/{inquiryNo}/answer
        """
        return await self._call_api(
            "POST",
            f"/v1/pay-merchant/inquiries/{inquiry_no}/answer",
            body={"answerComment": answer_comment},
        )

    async def update_inquiry_answer(
        self,
        inquiry_no: int,
        answer_content_id: int,
        answer_comment: str,
    ) -> dict[str, Any]:
        """고객문의(1:1) 답변 수정.

        PUT /v1/pay-merchant/inquiries/{inquiryNo}/answer/{answerContentId}
        """
        return await self._call_api(
            "PUT",
            f"/v1/pay-merchant/inquiries/{inquiry_no}/answer/{answer_content_id}",
            body={"answerComment": answer_comment},
        )


class SmartStoreApiError(Exception):
    """스마트스토어 API 에러."""

    pass

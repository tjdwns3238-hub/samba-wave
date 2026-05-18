"""상품정보제공고시 — 카테고리별 동적 분기 유틸리티.

상품의 category1 값을 기반으로 각 마켓 API가 요구하는
고시정보 타입과 필드를 자동 결정한다.
"""

from __future__ import annotations

from typing import Any


# ────────────────────────────────────────────
# 1. 카테고리 → 고시정보 타입 판별 (공통)
# ────────────────────────────────────────────

# category1 → 고시정보 그룹 매핑
_CATEGORY_GROUP: dict[str, str] = {
    # 의류
    "상의": "wear",
    "하의": "wear",
    "아우터": "wear",
    "원피스": "wear",
    "니트": "wear",
    "셔츠": "wear",
    "스커트": "wear",
    "팬츠": "wear",
    "의류": "wear",
    "패션의류": "wear",
    "남성의류": "wear",
    "여성의류": "wear",
    "속옷": "wear",
    "잠옷": "wear",
    "정장": "wear",
    # 아웃도어/스포츠 의류 — sports 그룹이 아닌 wear로 분류 (쿠팡 "기타 재화" fallback 방지)
    "남성등산의류": "wear",
    "여성등산의류": "wear",
    "등산의류": "wear",
    "스포츠의류": "wear",
    "남성스포츠의류": "wear",
    "여성스포츠의류": "wear",
    "아웃도어의류": "wear",
    "골프웨어": "wear",
    "유니폼": "wear",
    "레플리카": "wear",
    "져지": "wear",
    "트레이닝복": "wear",
    "트레이닝웨어": "wear",
    "트랙수트": "wear",
    "축구의류": "wear",
    "농구의류": "wear",
    "야구의류": "wear",
    "바람막이": "wear",
    "자켓": "wear",
    # 신발
    "신발": "shoes",
    "스니커즈": "shoes",
    "부츠": "shoes",
    "샌들": "shoes",
    "슬리퍼": "shoes",
    "스포츠화": "shoes",
    "구두": "shoes",
    "로퍼": "shoes",
    "운동화": "shoes",
    "러닝화": "shoes",
    "축구화": "shoes",
    "농구화": "shoes",
    # 아웃도어 신발 — 등산화/트레킹화
    "등산화": "shoes",
    "트레킹화": "shoes",
    "등산화/트레킹화": "shoes",
    # 스포츠 신발 복합 카테고리 (SSG 등 소싱처)
    "스포츠 슈즈": "shoes",
    "슈즈": "shoes",
    "워킹화": "shoes",
    # 가방
    "가방": "bag",
    "백팩": "bag",
    "크로스백": "bag",
    "숄더백": "bag",
    "토트백": "bag",
    "클러치": "bag",
    "에코백": "bag",
    "캐리어": "bag",
    "등산가방": "bag",
    "등산배낭": "bag",
    # 잡화/액세서리
    "모자": "accessories",
    "벨트": "accessories",
    "지갑": "accessories",
    "시계": "accessories",
    "주얼리": "accessories",
    "안경": "accessories",
    "액세서리": "accessories",
    "패션잡화": "accessories",
    # GSShop 패션잡화 (양산/장갑/스카프 등)
    "우산": "accessories",
    "양산": "accessories",
    "우산/양산": "accessories",
    "장갑": "accessories",
    "스카프": "accessories",
    "머플러": "accessories",
    "숄": "accessories",
    "스카프/머플러": "accessories",
    "스카프/머플러/숄": "accessories",
    "손수건": "accessories",
    "보석/잡화": "accessories",
    "등산모자": "accessories",
    "양말/패션소품": "accessories",
    "기타패션소품": "accessories",
    "필드용품": "accessories",
    # 데스크/PC 주변 잡화 (마우스패드/키보드 등) — "디지털" 부분매칭으로 electronics에 빠지지 않도록 명시
    "마우스패드": "accessories",
    "마우스": "accessories",
    "키보드": "accessories",
    "데스크": "accessories",
    "데스크테리어": "accessories",
    "문구": "accessories",
    "잡화": "accessories",
    # 화장품/뷰티
    "화장품": "cosmetic",
    "뷰티": "cosmetic",
    "스킨케어": "cosmetic",
    "메이크업": "cosmetic",
    "향수": "cosmetic",
    "헤어": "cosmetic",
    "바디": "cosmetic",
    # 식품
    "식품": "food",
    "음료": "food",
    "건강식품": "food",
    "과일": "food",
    "채소": "food",
    "수산물": "food",
    "축산물": "food",
    "농산물": "food",
    # 전자제품
    "전자": "electronics",
    "가전": "electronics",
    "디지털": "electronics",
    "컴퓨터": "electronics",
    "모바일": "electronics",
    # 스포츠/레저
    "스포츠": "sports",
    "레저": "sports",
    "스포츠/레저": "sports",
    "등산": "sports",
    "아웃도어": "sports",
    "골프": "sports",
    "헬스": "sports",
    "피트니스": "sports",
    "캠핑": "sports",
    "낚시": "sports",
    "자전거": "sports",
    "수영": "sports",
}


def detect_notice_group(product: dict[str, Any]) -> str:
    """상품의 category1 기반으로 고시정보 그룹을 판별한다.

    Returns: "wear" | "shoes" | "bag" | "accessories" | "cosmetic" | "food" | "electronics" | "sports" | "etc"
    """
    cat1 = (product.get("category1") or "").strip()
    cat2 = (product.get("category2") or "").strip()
    cat3 = (product.get("category3") or "").strip()

    # 의류/신발/가방/잡화는 GS샵 등 품목 필수 마켓에서 정확히 분기되어야 한다.
    # cat1이 "스포츠웨어/슈즈" 같은 복합 카테고리면 cat1 부분키워드 매칭에서 "스포츠"로
    # sports(=35 기타재화)에 빠지는 문제 → cat2/cat3 정확 매칭을 cat1 부분 매칭보다 먼저.
    PRIORITY_GROUPS = ("wear", "shoes", "bag", "accessories")

    # 1) cat2 / cat3 정확 매칭 (가장 구체적인 분류)
    for cat in (cat2, cat3):
        if cat and cat in _CATEGORY_GROUP:
            return _CATEGORY_GROUP[cat]

    # 2) cat1 정확 매칭
    if cat1 in _CATEGORY_GROUP:
        return _CATEGORY_GROUP[cat1]

    # 3) "스포츠/레저", "소품" 등 복합 cat1: cat2 키워드 부분 매칭 후 etc 폴백
    if cat1 in ("스포츠/레저", "소품"):
        for keyword, group in _CATEGORY_GROUP.items():
            if cat2 and keyword in cat2:
                return group
        return "etc"

    # 4) cat2/cat3에서 의류/신발/가방/잡화 키워드 우선 부분 매칭 (sports로 빠지지 않도록)
    for cat in (cat2, cat3):
        if not cat:
            continue
        for keyword, group in _CATEGORY_GROUP.items():
            if group in PRIORITY_GROUPS and keyword in cat:
                return group

    # 5) cat1 키워드 부분 매칭 (의류/신발/가방/잡화 우선, 그 외는 그다음)
    for keyword, group in _CATEGORY_GROUP.items():
        if group in PRIORITY_GROUPS and keyword in cat1:
            return group
    for keyword, group in _CATEGORY_GROUP.items():
        if keyword in cat1:
            return group

    # category (전체 경로) 에서도 시도
    full_cat = (product.get("category") or "").strip()
    for keyword, group in _CATEGORY_GROUP.items():
        if keyword in full_cat:
            return group

    # GS샵 등 미분류 카테고리 폴백
    if full_cat == "기타 재화":
        return "wear"

    # 상품명에서 카테고리 추론 (카테고리 미설정 소싱처 대응)
    name = (product.get("name") or "").lower()
    name_hints = {
        "shoes": [
            "운동화",
            "신발",
            "스니커즈",
            "부츠",
            "샌들",
            "슬리퍼",
            "러닝화",
            "로퍼",
            "구두",
            "플라이",
            "에어맥스",
            "에어포스",
            "덩크",
            "조던",
            "트레이너",
            "베이퍼",
            "등산화",
            "트레킹화",
            "경등산화",
        ],
        "wear": [
            "티셔츠",
            "셔츠",
            "자켓",
            "재킷",
            "팬츠",
            "후드",
            "맨투맨",
            "바지",
            "코트",
            "조거",
            "트레이닝",
            "베스트",
            "조끼",
            "레깅스",
            "패딩",
            "점퍼",
            "윈드",
            "바람막이",
            "방풍",
            "아노락",
            "블루종",
            "GTX",
            "고어텍스",
            "플리스",
            "후리스",
            "다운",
            "레인자켓",
        ],
        "bag": ["가방", "백팩", "크로스백", "토트백", "숄더백"],
        "accessories": [
            "모자",
            "벨트",
            "지갑",
            "시계",
            "양말",
            "장갑",
            "팔토시",
            "토시",
            "넥워머",
            "게이터",
            "마스크",
            "워머",
            "아대",
            "밴드",
            # 데스크/PC 주변 잡화 — 무신사 디지털 카테고리 오분류 방지
            "마우스패드",
            "키보드",
            "텀블러",
            "노트",
            "파우치",
            "키링",
            "스티커",
        ],
    }
    for group, hints in name_hints.items():
        for h in hints:
            if h in name:
                return group

    # 신발 브랜드 + 카테고리 미설정 → 기본 shoes 추론
    brand = (product.get("brand") or "").lower()
    shoe_brands = {
        "나이키",
        "nike",
        "아디다스",
        "adidas",
        "뉴발란스",
        "new balance",
        "퓨마",
        "puma",
        "리복",
        "reebok",
        "아식스",
        "asics",
        "컨버스",
        "converse",
        "반스",
        "vans",
    }
    if brand in shoe_brands:
        return "shoes"

    return "etc"


# ────────────────────────────────────────────
# 2. 쿠팡 고시정보
# ────────────────────────────────────────────

# 쿠팡 noticeCategoryName 매핑
_COUPANG_NOTICE_CATEGORY: dict[str, str] = {
    "wear": "의류",
    "shoes": "구두/신발",
    "bag": "가방",
    "accessories": "패션잡화(모자/벨트/액세서리 등)",
    "cosmetic": "화장품(기능성화장품 포함)",
    "food": "식품(일반식품)",
    "electronics": "전자제품",
    # 스포츠 용품 fallback (의류는 cat3에서 wear로 매칭됨)
    "sports": "기타 재화",
    "etc": "기타 재화",
}

# 쿠팡 카테고리별 고시정보 상세 필드
_COUPANG_NOTICE_FIELDS: dict[str, list[str]] = {
    "의류": [
        "제품 소재",
        "색상",
        "치수",
        "제조자(수입자)",
        "제조국",
        "세탁방법 및 취급시 주의사항",
        "제조연월",
        "품질보증기준",
        "A/S 책임자와 전화번호",
    ],
    "구두/신발": [
        "제품 소재",
        "색상",
        "치수",
        "제조자(수입자)",
        "제조국",
        "세탁방법 및 취급시 주의사항",
        "제조연월",
        "품질보증기준",
        "A/S 책임자와 전화번호",
    ],
    "가방": [
        "종류",
        "소재",
        "색상",
        "크기",
        "제조자(수입자)",
        "제조국",
        "세탁방법 및 취급시 주의사항",
        "제조연월",
        "품질보증기준",
        "A/S 책임자와 전화번호",
    ],
    "패션잡화(모자/벨트/액세서리 등)": [
        "종류",
        "소재",
        "치수",
        "제조자(수입자)",
        "제조국",
        "취급시 주의사항",
        "품질보증기준",
        "A/S 책임자와 전화번호",
    ],
    "화장품(기능성화장품 포함)": [
        "용량 또는 중량",
        "제품 주요 사양",
        "사용기한 또는 개봉 후 사용기간",
        "사용방법",
        "제조자 및 제조판매업자",
        "제조국",
        "주요 성분",
        "기능성 화장품 심사필 유무",
        "사용할 때 주의사항",
        "품질보증기준",
        "소비자상담관련 전화번호",
    ],
    "식품(일반식품)": [
        "식품의 유형",
        "생산자 및 소재지",
        "제조연월일/유통기한/품질유지기한",
        "포장단위별 내용물의 용량(중량),수량",
        "원재료명 및 함량",
        "영양성분",
        "유전자변형식품 여부",
        "소비자상담관련 전화번호",
    ],
    "전자제품": [
        "품명 및 모델명",
        "KC 인증 필 유무",
        "정격전압/소비전력",
        "에너지소비효율등급",
        "동일모델의 출시년월",
        "제조자(수입자)",
        "제조국",
        "크기/무게",
        "주요 사양",
        "품질보증기준",
        "A/S 책임자와 전화번호",
    ],
    "기타 재화": [
        "품명 및 모델명",
        "제조자(수입자)",
        "제조국",
        "A/S 책임자와 전화번호",
    ],
}


def _build_value_map(
    product: dict[str, Any], cat_name: str
) -> tuple[dict[str, str], str]:
    """notice value 매핑 사전 + fallback 생성 (build_coupang_notices와 _with_meta 공용)."""
    fallback = "상세페이지 참조"
    _caution_defaults: dict[str, str] = {
        "의류": "세탁 시 뒤집어서 단독 손세탁, 표백제 사용 금지, 직사광선을 피해 그늘에서 건조",
        "구두/신발": "물세탁 불가, 직사광선 및 고온 다습한 곳 보관 금지, 벤젠/신나 등 화학제품 사용 금지",
        "가방": "직사광선 및 고온 다습한 환경을 피해 보관, 마찰에 의한 색 이염 주의",
    }
    caution_text = (
        product.get("care_instructions", "")
        or product.get("careInstructions", "")
        or _caution_defaults.get(cat_name, fallback)
    )
    value_map: dict[str, str] = {
        "제품 소재": product.get("material", "") or fallback,
        "소재": product.get("material", "") or fallback,
        "색상": product.get("color", "") or fallback,
        "치수": fallback,
        "크기": fallback,
        "종류": fallback,
        "제조자(수입자)": product.get("manufacturer", "")
        or product.get("brand", "")
        or fallback,
        "제조자 및 제조판매업자": product.get("manufacturer", "")
        or product.get("brand", "")
        or fallback,
        "제조국": product.get("origin", "") or fallback,
        "세탁방법 및 취급시 주의사항": caution_text,
        "취급시 주의사항": caution_text,
        "사용할 때 주의사항": caution_text,
        "제조연월": fallback,
        "품질보증기준": "제품 이상 시 공정거래위원회 고시 소비자분쟁해결기준에 의거 보상합니다.",
        "A/S 책임자와 전화번호": fallback,
        "소비자상담관련 전화번호": fallback,
    }
    return value_map, fallback


def _normalize_notice_meta(meta: Any) -> list[dict[str, Any]] | None:
    """쿠팡 메타 API 응답에서 notice 그룹 리스트 추출 (응답 구조 다양성 대응).

    응답 구조 후보:
      a) {"data": [{"noticeCategoryName": ..., "noticeCategoryDetailNames": [{"noticeCategoryDetailName": ...}]}]}
      b) {"data": {"noticeCategoryGroups": [...]}}
      c) [...]  (data 키 없이 list 직접)
    하나라도 매칭되면 정규화된 list[dict] 반환, 못 찾으면 None.
    """
    if meta is None:
        return None
    raw = meta.get("data") if isinstance(meta, dict) else meta
    if isinstance(raw, dict):
        # 후보 b
        for k in ("noticeCategoryGroups", "noticeCategories", "items"):
            v = raw.get(k)
            if isinstance(v, list):
                return v
        return None
    if isinstance(raw, list):
        return raw
    return None


def build_coupang_notices_with_meta(
    product: dict[str, Any],
    meta: Any,
) -> list[dict[str, str]] | None:
    """쿠팡 메타 API 응답을 기반으로 notices 배열을 생성한다.

    하드코딩 매핑(_COUPANG_NOTICE_CATEGORY/_COUPANG_NOTICE_FIELDS) 대신 카테고리별
    실제 noticeCategoryName/Detail을 사용 — 의류/신발 등록 시 옵션의 notice가
    "Cannot enter '...'" 로 거부되는 미스매치 근본 해결.

    응답이 비정상이거나 그룹이 비어있으면 None 반환 → 호출자가 build_coupang_notices() 폴백.
    """
    groups = _normalize_notice_meta(meta)
    if not groups:
        return None

    notices: list[dict[str, str]] = []
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        cat_name = (
            grp.get("noticeCategoryName")
            or grp.get("name")
            or grp.get("category")
            or ""
        )
        if not cat_name:
            continue
        details_raw = (
            grp.get("noticeCategoryDetailNames")
            or grp.get("noticeCategoryDetails")
            or grp.get("details")
            or []
        )
        if not isinstance(details_raw, list):
            continue
        value_map, fallback = _build_value_map(product, cat_name)
        for det in details_raw:
            if isinstance(det, dict):
                detail_name = (
                    det.get("noticeCategoryDetailName")
                    or det.get("name")
                    or det.get("detail")
                    or ""
                )
            elif isinstance(det, str):
                detail_name = det
            else:
                continue
            if not detail_name:
                continue
            notices.append(
                {
                    "noticeCategoryName": cat_name,
                    "noticeCategoryDetailName": detail_name,
                    "content": value_map.get(detail_name, fallback),
                }
            )
        # 첫 번째 유효 그룹만 사용 (한 카테고리에 한 그룹)
        if notices:
            break
    return notices or None


def build_coupang_notices(product: dict[str, Any]) -> list[dict[str, str]]:
    """상품 카테고리에 맞는 쿠팡 고시정보 notices 배열을 생성한다 (정적 매핑 폴백).

    가능하면 build_coupang_notices_with_meta(product, meta)를 우선 사용하고,
    이 함수는 메타 조회 실패 시 fallback으로 호출된다.
    """
    group = detect_notice_group(product)
    cat_name = _COUPANG_NOTICE_CATEGORY.get(group, "기타 재화")
    fields = _COUPANG_NOTICE_FIELDS.get(cat_name, _COUPANG_NOTICE_FIELDS["기타 재화"])

    # 상품 데이터에서 값 추출 (없으면 카테고리별 기본값)
    value_map, fallback = _build_value_map(product, cat_name)

    notices = []
    for field in fields:
        notices.append(
            {
                "noticeCategoryName": cat_name,
                "noticeCategoryDetailName": field,
                "content": value_map.get(field, fallback),
            }
        )
    return notices


# ────────────────────────────────────────────
# 3. 스마트스토어 고시정보
# ────────────────────────────────────────────

# 스마트스토어 카테고리 ID → 고시정보 그룹 매핑
# 50000000 대역: 패션의류/잡화
_SS_CATEGORY_GROUP: dict[str, str] = {
    # 신발 카테고리 (50003xxx 대역)
    "50003822": "shoes",  # 운동화 > 스니커즈
    "50003835": "shoes",  # 운동화 > 런닝화
    "50003801": "shoes",  # 신발
    "50003802": "shoes",  # 남성신발
    "50003803": "shoes",  # 여성신발
    "50003804": "shoes",  # 아동신발
    "50003820": "shoes",  # 운동화
    "50003821": "shoes",  # 워킹화
    "50003830": "shoes",  # 구두
    "50003840": "shoes",  # 부츠
    "50003850": "shoes",  # 샌들/슬리퍼
}


def _detect_group_from_ss_category(category_id: str) -> str | None:
    """스마트스토어 카테고리 ID로 고시정보 그룹을 판별.
    직접 매핑이 없으면 상위 카테고리 대역으로 추론.
    """
    if not category_id:
        return None
    # 1. 직접 매핑
    if category_id in _SS_CATEGORY_GROUP:
        return _SS_CATEGORY_GROUP[category_id]
    # 2. 대역 추론 (5000380x~5000389x = 신발)
    try:
        cid = int(category_id)
        if 50003800 <= cid <= 50003899:
            return "shoes"
        if 50000100 <= cid <= 50002999:
            return "wear"
        if 50004000 <= cid <= 50004099:
            return "bag"
        if 50004100 <= cid <= 50004299:
            return "accessories"
    except (ValueError, TypeError):
        pass
    return None


# 스마트스토어 고시정보 타입 매핑
_SMARTSTORE_NOTICE_TYPE: dict[str, str] = {
    "wear": "WEAR",
    "shoes": "SHOES",
    "bag": "BAG",
    "accessories": "FASHION_ITEMS",
    "cosmetic": "COSMETIC",
    "food": "FOOD",
    # 디지털콘텐츠(eBook/음원/SW) 전용 타입 — 물리 가전/잡화에 사용 시 필수 필드 누락 400 발생
    # 우리는 디지털콘텐츠 상품을 취급하지 않으므로 영구적으로 ETC로 매핑
    "electronics": "ETC",
    "etc": "ETC",
}


_SMARTSTORE_NOTICE_FIELD_MAX_LENGTH = 1500


def _normalize_smartstore_notice_fields(
    value: Any,
    *,
    field_path: str,
    max_length: int = _SMARTSTORE_NOTICE_FIELD_MAX_LENGTH,
) -> Any:
    """SmartStore 고시정보 문자열 필드를 안전 길이로 정규화한다."""
    if isinstance(value, dict):
        return {
            key: _normalize_smartstore_notice_fields(
                item,
                field_path=f"{field_path}.{key}" if field_path else str(key),
                max_length=max_length,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _normalize_smartstore_notice_fields(
                item,
                field_path=f"{field_path}[{idx}]",
                max_length=max_length,
            )
            for idx, item in enumerate(value)
        ]
    if isinstance(value, str) and len(value) > max_length:
        from backend.utils.logger import logger as _notice_logger

        _notice_logger.warning(
            "[스마트스토어 고시정보] 문자열 길이 초과로 잘림: "
            f"{field_path} {len(value)}->{max_length}"
        )
        return value[:max_length].rstrip()
    return value


def build_smartstore_notice(product: dict[str, Any], **kwargs: str) -> dict[str, Any]:
    """상품 카테고리에 맞는 스마트스토어 고시정보를 생성한다.

    kwargs: color_text, size_text, mfr, brand, ss_category_id 등 transform_product에서 가공된 값
    """
    # 매핑된 스마트스토어 카테고리 ID로 고시정보 타입 우선 판별
    ss_cat_id = kwargs.get("ss_category_id", "")
    group = _detect_group_from_ss_category(ss_cat_id) if ss_cat_id else None
    if not group:
        group = detect_notice_group(product)
    notice_type = _SMARTSTORE_NOTICE_TYPE.get(group, "ETC")

    fallback = "상세 이미지 참조"
    # 스마트스토어 금지 특수문자 제거: \ * ? " < >
    # + 보이지 않는 유니코드(zero-width, NBSP, NNBSP 등) 정규화
    # — 네이버 고시정보는 NARROW NO-BREAK SPACE(U+202F) 등을 DisallowedCharacters로 거부함
    import re as _re_special

    def _clean_special(text: str) -> str:
        if not text:
            return text
        # zero-width 및 라인 구분자 제거
        s = _re_special.sub(r"[​-‍  ﻿]", "", text)
        # 비표준 공백을 일반 공백으로 치환 (NBSP, NNBSP, FIGURE SPACE, WORD JOINER)
        s = _re_special.sub(r"[   ⁠]", " ", s)
        # 스마트스토어 금지 특수문자 제거
        s = _re_special.sub(r'[\\*?"<>]', "", s)
        return s.strip()

    material = _clean_special(product.get("material", "") or fallback)
    color_text = kwargs.get("color_text", fallback)
    size_text = kwargs.get("size_text", fallback)
    mfr = kwargs.get(
        "mfr", product.get("manufacturer", "") or product.get("brand", "") or fallback
    )
    brand = kwargs.get("brand", product.get("brand", "") or fallback)

    # 카테고리별 기본 취급주의사항
    _DEFAULT_CAUTION: dict[str, str] = {
        "wear": "세탁 시 뒤집어서 단독 손세탁, 표백제 사용 금지, 직사광선을 피해 그늘에서 건조",
        "shoes": "물세탁 불가, 직사광선 및 고온 다습한 곳 보관 금지, 벤젠/신나 등 화학제품 사용 금지",
        "bag": "직사광선 및 고온 다습한 환경을 피해 보관, 마찰에 의한 색 이염 주의, 물에 젖었을 경우 마른 천으로 닦아 그늘에서 건조",
        "accessories": "직사광선 및 습기를 피해 보관, 화학제품 접촉 주의",
        "cosmetic": "사용 후 뚜껑을 꼭 닫아 보관, 직사광선을 피해 서늘한 곳에 보관, 이상 증상 발생 시 사용 중지",
        "food": "직사광선을 피해 서늘한 곳에 보관, 개봉 후 빠른 시일 내 섭취",
        "electronics": "물기에 주의, 직사광선 및 고온 다습한 곳 보관 금지",
        "etc": "상세페이지 참조",
    }

    caution = (
        product.get("care_instructions", "")
        or product.get("careInstructions", "")
        or _DEFAULT_CAUTION.get(group, _DEFAULT_CAUTION["etc"])
    )

    # 소비자보호 가이드 5항목 — "0"=법정기준, "1"=상품상세 참조
    _GUIDE_FIELDS = {
        "returnCostReason": "0",
        "noRefundReason": "0",
        "qualityAssuranceStandard": "0",
        "compensationProcedure": "0",
        "troubleShootingContents": "0",
    }

    # 제조사에서 (주) 제거
    import re as _re

    mfr = _re.sub(r"\(주\)|㈜|\(株\)", "", mfr).strip() if mfr else mfr

    # 공통 필드 (의류/신발/가방은 필드가 거의 동일) — 수집 데이터 우선 사용
    caution = _clean_special(
        product.get("care_instructions", "")
        or product.get("careInstructions", "")
        or _DEFAULT_CAUTION.get(group, _DEFAULT_CAUTION["etc"])
    )
    common_fields = {
        **_GUIDE_FIELDS,
        "material": material,
        "color": _clean_special(color_text),
        "size": _clean_special(size_text),
        "manufacturer": _clean_special(mfr or fallback),
        "caution": caution,
        "packDateText": "주문 후 개별포장 발송",
        "warrantyPolicy": _clean_special(
            product.get("quality_guarantee", "")
            or product.get("qualityGuarantee", "")
            or "제품 하자 시 소비자분쟁해결기준(공정거래위원회 고시)에 따라 보상"
        ),
        "afterServiceDirector": _clean_special(f"{brand} 고객센터"),
    }

    # 타입별 필드 키 이름
    type_key_map: dict[str, str] = {
        "WEAR": "wear",
        "SHOES": "shoes",
        "BAG": "bag",
        "FASHION_ITEMS": "fashionItems",
        "COSMETIC": "cosmetic",
        "FOOD": "food",
        "DIGITAL_CONTENTS": "digitalContents",
        "ETC": "etc",
    }

    field_key = type_key_map.get(notice_type, "etc")

    # 화장품/식품은 필드가 다름
    if notice_type == "COSMETIC":
        notice_data = {
            "capacity": product.get("material", "") or fallback,
            "manufacturer": mfr,
            "expirationDateText": fallback,
            "mainIngredient": fallback,
            "caution": fallback,
            "warrantyPolicy": common_fields["warrantyPolicy"],
            "afterServiceDirector": common_fields["afterServiceDirector"],
        }
    elif notice_type == "FOOD":
        notice_data = {
            "foodType": fallback,
            "manufacturer": mfr,
            "location": fallback,
            "packDateText": fallback,
            "expirationDateText": fallback,
            "weight": fallback,
            "amount": fallback,
            "ingredients": fallback,
            "nutritionFacts": fallback,
            "geneticallyModified": "해당 없음",
            "consumerSafetyCaution": fallback,
            "importDeclaration": "해당 없음",
            "customerServicePhoneNumber": "상세페이지 참조",
        }
    elif notice_type == "ETC":
        notice_data = {
            "itemName": (product.get("name", "") or fallback)[:50],
            "modelName": fallback,
            "manufacturer": mfr,
            "afterServiceDirector": common_fields["afterServiceDirector"],
        }
    elif notice_type == "FASHION_ITEMS":
        # 패션잡화 — type 필드 필수 (모자/벨트/지갑 등 세부 분류)
        _cat_parts = [
            p.strip() for p in (product.get("category") or "").split(">") if p.strip()
        ]
        _fashion_type = (
            _cat_parts[1]
            if len(_cat_parts) > 1
            else (_cat_parts[0] if _cat_parts else "패션잡화")
        )
        if not _fashion_type:
            _fashion_type = "패션잡화"
        from backend.utils.logger import logger as _notice_logger

        _notice_logger.info(
            f"[고시정보] FASHION_ITEMS type={_fashion_type!r}, "
            f"category={product.get('category')!r}, "
            f"category1={product.get('category1')!r}, "
            f"category_levels={product.get('category_levels')!r}"
        )
        notice_data = {
            **common_fields,
            "type": _clean_special(_fashion_type),
        }
    elif notice_type == "SHOES":
        # 신발 — height 필드 필수
        notice_data = {
            **common_fields,
            "height": _clean_special(size_text) or "상세 이미지 참조",
        }
    elif notice_type == "BAG":
        # 가방 — type 필드 필수 (종류: 백팩/크로스백/토트백 등)
        _cat_parts = [
            p.strip() for p in (product.get("category") or "").split(">") if p.strip()
        ]
        _bag_type = (
            _cat_parts[1]
            if len(_cat_parts) > 1
            else (_cat_parts[0] if _cat_parts else "가방")
        )
        if not _bag_type:
            _bag_type = "가방"
        notice_data = {
            **common_fields,
            "type": _clean_special(_bag_type),
        }
    elif notice_type == "DIGITAL_CONTENTS":
        # 전자제품/디지털콘텐츠 — 의류와 다른 필드 구조
        notice_data = {
            "itemName": (product.get("name", "") or fallback)[:50],
            "modelName": fallback,
            "manufacturer": _clean_special(mfr or fallback),
            "caution": fallback,
            "warrantyPolicy": common_fields["warrantyPolicy"],
            "afterServiceDirector": common_fields["afterServiceDirector"],
        }
    else:
        # WEAR — 공통 필드 사용
        notice_data = common_fields

    # 화장품/식품/ETC에도 가이드 필드 추가
    if isinstance(notice_data, dict):
        for gk, gv in _GUIDE_FIELDS.items():
            if gk not in notice_data:
                notice_data[gk] = gv

    notice_payload = {
        "productInfoProvidedNoticeType": notice_type,
        field_key: notice_data,
    }
    return _normalize_smartstore_notice_fields(
        notice_payload,
        field_path="productInfoProvidedNotice",
    )


# ────────────────────────────────────────────
# 4. 롯데ON 고시정보
# ────────────────────────────────────────────

# 롯데ON pdItmsCd 매핑
_LOTTEON_NOTICE_CODE: dict[str, str] = {
    "wear": "01",  # [01]의류
    "shoes": "02",  # [02]구두/신발
    "bag": "03",  # [03]가방
    "accessories": "04",  # [04]패션잡화
    "cosmetic": "18",  # [18]화장품
    "food": "21",  # [21]가공식품
    "electronics": "10",  # [10]사무용기기(컴퓨터/노트북 등)
    "sports": "25",  # [25]스포츠용품
    "etc": "38",  # [38]기타(재화)
}


def build_lotteon_notice(product: dict[str, Any], **kwargs: str) -> dict[str, Any]:
    """상품 카테고리에 맞는 롯데ON 고시정보를 생성한다.

    pdItmsCd별 유효 pdArtlCd 코드 (롯데ON 어드민 확인):
      01(의류): 0010소재 0020색상 0030치수 0040제조년월 0050세탁취급 0060제조국 0070제조자 0080품질보증 0090A/S
      02(신발): 01과 동일 구조로 추정 — 어드민 재확인 필요
      03/04:    01과 동일 구조로 추정 — 어드민 재확인 필요
      35(기타): 0060 0070 0080 0090 사용
    """
    from datetime import datetime as _dt
    import logging as _logging

    _log = _logging.getLogger(__name__)

    group = detect_notice_group(product)
    code = _LOTTEON_NOTICE_CODE.get(group, "38")
    _log.info(
        f"[롯데ON 고시정보] category1={product.get('category1')} group={group} pdItmsCd={code}"
    )

    fallback = "상세페이지 참조"
    brand = product.get("brand", "") or ""
    material = product.get("material", "") or fallback
    color_text = kwargs.get("color_text") or product.get("color", "") or fallback
    size_text = kwargs.get("size_text") or fallback
    mfr = (
        kwargs.get("mfr")
        or product.get("manufacturer", "")
        or brand
        or "제조자 정보 없음"
    )
    origin = product.get("origin", "") or fallback
    quality = product.get("quality_guarantee", "") or "소비자 기본법에 따름"
    as_message = (product.get("_as_message", "") or "").strip()
    as_phone = (product.get("_as_phone", "") or "").strip()
    as_contact = (
        as_message or as_phone or (f"{brand} 고객센터" if brand else "판매자 문의")
    )
    care = product.get("care_instructions", "") or fallback
    manufacture_ym = _dt.now().strftime("%Y%m")  # 등록일 기준 yyyyMM

    if code == "01":
        # 의류: 0010소재 0020색상 0030치수 0040제조년월 0050세탁취급 0060제조국 0070제조자 0080품질보증 0090A/S
        articles: list[dict[str, str]] = [
            {"pdArtlCd": "0010", "pdArtlCnts": material},
            {"pdArtlCd": "0020", "pdArtlCnts": color_text},
            {"pdArtlCd": "0030", "pdArtlCnts": size_text},
            {"pdArtlCd": "0040", "pdArtlCnts": manufacture_ym},
            {"pdArtlCd": "0050", "pdArtlCnts": care},
            {"pdArtlCd": "0060", "pdArtlCnts": origin},
            {"pdArtlCd": "0070", "pdArtlCnts": mfr},
            {"pdArtlCd": "0080", "pdArtlCnts": quality},
            {"pdArtlCd": "0090", "pdArtlCnts": as_contact},
        ]
    elif code == "02":
        # 신발: 0100소재 0020색상 0030치수 0060제조국 0070제조자 0110취급주의 0080품질보증 0090A/S
        articles = [
            {"pdArtlCd": "0100", "pdArtlCnts": material},
            {"pdArtlCd": "0020", "pdArtlCnts": color_text},
            {"pdArtlCd": "0030", "pdArtlCnts": size_text},
            {"pdArtlCd": "0060", "pdArtlCnts": origin},
            {"pdArtlCd": "0070", "pdArtlCnts": mfr},
            {"pdArtlCd": "0110", "pdArtlCnts": care},
            {"pdArtlCd": "0080", "pdArtlCnts": quality},
            {"pdArtlCd": "0090", "pdArtlCnts": as_contact},
        ]
    elif code == "03":
        # 가방: 0130종류 0120소재 0020색상 0140크기 0060제조국 0070제조자 0110취급주의 0080품질보증 0090A/S
        articles = [
            {"pdArtlCd": "0130", "pdArtlCnts": fallback},
            {"pdArtlCd": "0120", "pdArtlCnts": material},
            {"pdArtlCd": "0020", "pdArtlCnts": color_text},
            {"pdArtlCd": "0140", "pdArtlCnts": size_text},
            {"pdArtlCd": "0060", "pdArtlCnts": origin},
            {"pdArtlCd": "0070", "pdArtlCnts": mfr},
            {"pdArtlCd": "0110", "pdArtlCnts": care},
            {"pdArtlCd": "0080", "pdArtlCnts": quality},
            {"pdArtlCd": "0090", "pdArtlCnts": as_contact},
        ]
    elif code == "04":
        # 패션잡화: 0130종류 0120소재 0030치수 0060제조국 0070제조자 0110취급주의 0080품질보증 0090A/S
        articles = [
            {"pdArtlCd": "0130", "pdArtlCnts": fallback},
            {"pdArtlCd": "0120", "pdArtlCnts": material},
            {"pdArtlCd": "0030", "pdArtlCnts": size_text},
            {"pdArtlCd": "0060", "pdArtlCnts": origin},
            {"pdArtlCd": "0070", "pdArtlCnts": mfr},
            {"pdArtlCd": "0110", "pdArtlCnts": care},
            {"pdArtlCd": "0080", "pdArtlCnts": quality},
            {"pdArtlCd": "0090", "pdArtlCnts": as_contact},
        ]
    elif code == "25":
        # 스포츠용품: 공식 고시코드표 기준 (롯데ON 품목고시 현행화 PDF)
        # 0210품명 0780크기/중량 0220출시년월 0150제품구성 0020색상 0410재질
        # 0810세부사양 0060제조국 0070제조자 0080품질보증 0090A/S 0200안전인증
        articles = [
            {"pdArtlCd": "0210", "pdArtlCnts": fallback},
            {"pdArtlCd": "0780", "pdArtlCnts": size_text},
            {"pdArtlCd": "0220", "pdArtlCnts": manufacture_ym},
            {"pdArtlCd": "0150", "pdArtlCnts": fallback},
            {"pdArtlCd": "0020", "pdArtlCnts": color_text},
            {"pdArtlCd": "0410", "pdArtlCnts": material},
            {"pdArtlCd": "0810", "pdArtlCnts": fallback},
            {"pdArtlCd": "0060", "pdArtlCnts": origin},
            {"pdArtlCd": "0070", "pdArtlCnts": mfr},
            {"pdArtlCd": "0080", "pdArtlCnts": quality},
            {"pdArtlCd": "0090", "pdArtlCnts": as_contact},
            {"pdArtlCd": "0200", "pdArtlCnts": fallback},
        ]
    elif code == "38":
        # 기타재화: 공식 고시코드표 기준 (롯데ON 품목고시 현행화 PDF)
        # 0210품명 1400인증/허가 1420제조국/원산지 0070제조자 1440A/S 0200안전인증
        articles = [
            {"pdArtlCd": "0210", "pdArtlCnts": fallback},
            {"pdArtlCd": "1400", "pdArtlCnts": fallback},
            {"pdArtlCd": "1420", "pdArtlCnts": origin},
            {"pdArtlCd": "0070", "pdArtlCnts": mfr},
            {"pdArtlCd": "1440", "pdArtlCnts": as_contact},
            {"pdArtlCd": "0200", "pdArtlCnts": fallback},
        ]
    else:
        # 기타 미분류(10/18/21 등 전용 분기 없는 코드 포함)
        # → pdItmsCd 도 "38"(기타재화)로 통일해야 articleCd와 itemCd 가 일치.
        # 미통일 시: pdItmsCd=10 + articleCd=[0210,1420,0070,1440] 미스매치로
        # 롯데ON 9999 "[상품품목고시정보] 반드시 입력" 에러 발생.
        code = "38"
        articles = [
            {"pdArtlCd": "0210", "pdArtlCnts": fallback},
            {"pdArtlCd": "1420", "pdArtlCnts": origin},
            {"pdArtlCd": "0070", "pdArtlCnts": mfr},
            {"pdArtlCd": "1440", "pdArtlCnts": as_contact},
        ]

    return {"pdItmsCd": code, "pdItmsArtlLst": articles}


# ────────────────────────────────────────────
# 5. SSG 고시정보 (상품관리속성)
# ────────────────────────────────────────────

_DOMESTIC_ORIGIN_KEYWORDS = {"한국", "대한민국", "국내", "korea", "south korea"}
_UNKNOWN_ORIGIN_KEYWORDS = {"없음", "미상", "미확인", "알수없음", "상세설명참조", ""}

# SSG 고시정보 타입 매핑 (카테고리 그룹 → SSG itemMngPropClsId)
_SSG_NOTICE_TYPE_MAP: dict[str, str] = {
    "wear": "0000000001",
    "shoes": "0000000002",
    "bag": "0000000004",
    "accessories": "0000000004",  # 패션잡화 — bag과 동일 클래스
    "cosmetic": "0000000005",
    "food": "0000000006",
    "electronics": "0000000007",
    "etc": "0000000035",
}


def _is_domestic_origin(origin: str) -> bool:
    """원산지가 국내산인지 판별한다."""
    stripped = origin.strip().lower()
    return stripped in _DOMESTIC_ORIGIN_KEYWORDS or stripped in _UNKNOWN_ORIGIN_KEYWORDS


def build_ssg_notice(
    product: dict[str, Any],
) -> tuple[str, list[dict[str, str]]]:
    """상품 카테고리에 맞는 SSG 상품관리속성을 생성한다.

    Returns:
        (itemMngPropClsId, itemMngAttrs 배열)
    """
    fallback = "상세페이지 참조"
    # 정책 기본값 (_ssg_notice_* 키로 주입, 소싱 데이터 우선)
    _pol_material = product.get("_ssg_notice_material", "") or ""
    _pol_color = product.get("_ssg_notice_color", "") or ""

    material = product.get("material", "") or _pol_material or fallback
    color = product.get("color", "") or _pol_color or fallback
    origin = product.get("origin", "") or ""

    # 치수 및 굽높이
    size_heel = product.get("_ssg_notice_size", "") or fallback

    # 수입여부 — 정책값 우선, 없으면 원산지로 자동 판별
    if "_ssg_import_yn" in product:
        import_yn = product["_ssg_import_yn"]
    else:
        import_yn = "N" if (origin and _is_domestic_origin(origin)) else "Y"

    # 제조사 — 정책값 우선, 없으면 소싱 데이터
    _pol_manufacturer = product.get("_ssg_notice_manufacturer", "") or ""
    manufacturer = (
        _pol_manufacturer
        or product.get("manufacturer", "")
        or product.get("brand", "")
        or fallback
    )

    # 제조국 — 정책값 우선, 없으면 소싱 데이터
    _pol_origin = product.get("_ssg_notice_origin", "") or ""
    origin = _pol_origin or origin or fallback

    # 수입자 — 정책값 우선, 없으면 제조사 자동
    _pol_importer = product.get("_ssg_notice_importer", "") or ""
    importer = _pol_importer or (manufacturer if import_yn == "Y" else fallback)

    # 취급시 주의사항 — 정책값 우선
    _pol_caution = product.get("_ssg_notice_caution", "") or ""
    caution = _pol_caution or fallback

    # A/S 책임자 및 전화번호 — 통합 연락처 우선, 없으면 개별 phone/message
    _pol_as_contact = product.get("_ssg_notice_as_contact", "") or ""
    as_phone = product.get("_as_phone", "") or ""
    as_message = product.get("_as_message", "") or ""
    if _pol_as_contact:
        as_info = _pol_as_contact
    elif as_phone:
        as_info = as_phone
        if as_message:
            as_info += f" | {as_message}"
    elif as_message:
        as_info = as_message
    else:
        as_info = fallback

    _group_map = {"의류": "wear", "신발": "shoes", "가방/잡화": "bag", "기타": "etc"}
    _pol_group = product.get("_ssg_notice_group", "") or ""
    group = _group_map.get(_pol_group) or detect_notice_group(product)
    cls_id = _SSG_NOTICE_TYPE_MAP.get(group, "0000000035")

    if group == "wear":
        attrs: list[dict[str, str]] = [
            {"itemMngPropId": "0000000001", "itemMngCntt": material},
            {"itemMngPropId": "0000000002", "itemMngCntt": color},
            {"itemMngPropId": "0000000003", "itemMngCntt": fallback},
            {"itemMngPropId": "0000000008", "itemMngCntt": import_yn},
            {"itemMngPropId": "0000000009", "itemMngCntt": importer},
            {"itemMngPropId": "0000000005", "itemMngCntt": fallback},
            {"itemMngPropId": "0000000004", "itemMngCntt": fallback},
            {
                "itemMngPropId": "0000000006",
                "itemMngCntt": "관련 법 및 소비자 분쟁해결 규정에 따름",
            },
            {"itemMngPropId": "0000000011", "itemMngCntt": product.get("_ssg_origin_code") or fallback},
            {"itemMngPropId": "0000000012", "itemMngCntt": as_info},
        ]
    elif group == "shoes":
        attrs = [
            {"itemMngPropId": "0000000184", "itemMngCntt": material},
            {"itemMngPropId": "0000000002", "itemMngCntt": color},
            {"itemMngPropId": "0000000170", "itemMngCntt": size_heel},
            {"itemMngPropId": "0000000008", "itemMngCntt": import_yn},
            {"itemMngPropId": "0000000009", "itemMngCntt": importer},
            {"itemMngPropId": "0000000013", "itemMngCntt": caution},
            {
                "itemMngPropId": "0000000006",
                "itemMngCntt": "관련 법 및 소비자 분쟁해결 규정에 따름",
            },
            {"itemMngPropId": "0000000012", "itemMngCntt": as_info},
        ]
    elif group in ("bag", "accessories"):
        attrs = [
            {"itemMngPropId": "0000000014", "itemMngCntt": fallback},
            {"itemMngPropId": "0000000001", "itemMngCntt": material},
            {"itemMngPropId": "0000000003", "itemMngCntt": fallback},
            {"itemMngPropId": "0000000008", "itemMngCntt": import_yn},
            {"itemMngPropId": "0000000009", "itemMngCntt": importer},
            {"itemMngPropId": "0000000013", "itemMngCntt": fallback},
            {
                "itemMngPropId": "0000000006",
                "itemMngCntt": "관련 법 및 소비자 분쟁해결 규정에 따름",
            },
            {"itemMngPropId": "0000000012", "itemMngCntt": as_info},
        ]
    else:
        attrs = [
            {"itemMngPropId": "0000000001", "itemMngCntt": material},
            {"itemMngPropId": "0000000002", "itemMngCntt": color},
            {"itemMngPropId": "0000000003", "itemMngCntt": fallback},
            {
                "itemMngPropId": "0000000006",
                "itemMngCntt": "관련 법 및 소비자 분쟁해결 규정에 따름",
            },
            {"itemMngPropId": "0000000007", "itemMngCntt": manufacturer},
            {"itemMngPropId": "0000000008", "itemMngCntt": "N"},
            {"itemMngPropId": "0000000011", "itemMngCntt": "1000000001"},
            {"itemMngPropId": "0000000012", "itemMngCntt": as_info},
        ]

    return cls_id, attrs

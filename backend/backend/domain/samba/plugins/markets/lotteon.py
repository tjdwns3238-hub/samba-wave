"""롯데ON 마켓 플러그인.

기존 dispatcher._handle_lotteon 로직을 플러그인 구조로 추출.
인증 로드는 base._load_auth 가 처리하므로 execute 에서는 creds dict 사용.
"""

from __future__ import annotations

import re
import time
from typing import Any

from backend.domain.samba.plugins.market_base import MarketPlugin
from backend.utils.logger import logger

# ── test_auth 캐싱 (60초 TTL) — 오토튠 품절 배치 시 인증 API 중복 호출 방지 ──
_auth_cache: dict[str, tuple[float, Any]] = {}  # {api_key: (timestamp, client)}
_AUTH_TTL = 60  # 초

# ── 카테고리 캐싱 (10분 TTL) — 동일 카테고리 반복 전송 시 API 3회 호출 방지 ──
_category_cache: dict[
    str, tuple[float, dict]
] = {}  # {category_id: (timestamp, cache_data)}
_CAT_TTL = 600  # 초 (10분)


async def _get_cached_client(api_key: str):
    """test_auth 결과를 60초간 캐싱하여 재사용."""
    from backend.domain.samba.proxy.lotteon import LotteonClient

    now = time.time()
    if api_key in _auth_cache:
        ts, client = _auth_cache[api_key]
        if now - ts < _AUTH_TTL:
            return client
        # 만료된 클라이언트 연결 정리
        await client.aclose()
    client = LotteonClient(api_key)
    await client.test_auth()
    _auth_cache[api_key] = (now, client)
    return client


def _pick_lotteon_itm_label(itm: dict) -> str:
    """롯데ON product/detail 응답의 itm 객체에서 옵션 라벨 후보 선택.

    우선순위: itmNm > sitmNm > itmOptLst[0].optVal > optNm
    각 후보가 None/빈/공백 문자열이면 다음 후보로 폴백 (공백 하드닝).
    optNm은 축 이름("사이즈" 등)이라 값으로 쓰기 부적절 → 마지막 폴백.

    배경: 2026-04-26 LO2664562602 외 다수에서 itmNm/optNm 빈 문자열 → 매칭 0건 →
    stkQty=0 강제 → 전 옵션 SOUT_STK 잠김. sitmNm 또는 itmOptLst[0].optVal에
    실제 라벨이 들어있어 폴백 필요.
    """
    candidates: list[Any] = [itm.get("itmNm"), itm.get("sitmNm")]
    opt_lst = itm.get("itmOptLst") or []
    # 2단 옵션 대응: itmOptLst에 여러 차원이 있으면 " / "로 조합한 라벨도 후보로
    # (transform_product가 2단 옵션을 [{optNm:색상,optVal:차콜},{optNm:Gift box,optVal:선택안함}] 형태로 등록 →
    # 매칭 키는 "차콜 / 선택안함" 이어야 product.options의 name과 매칭됨)
    if opt_lst:
        opt_vals = [
            str(o.get("optVal") or "").strip()
            for o in opt_lst
            if isinstance(o, dict) and o.get("optVal")
        ]
        if len(opt_vals) >= 2:
            candidates.append(" / ".join(opt_vals))
        if opt_vals:
            candidates.append(opt_vals[0])
    candidates.append(itm.get("optNm"))

    for c in candidates:
        if c is None:
            continue
        s = str(c).strip()
        if s:
            return s
    return ""


def _norm_opt_label(name: str) -> str:
    """옵션 라벨 정규화 — 공백/슬래시 차이 흡수.

    경량 분기의 nested ``_norm_opt``와 동일 규칙을 모듈 레벨로 추출.
    """
    s = (name or "").strip()
    s = re.sub(r"\s*/\s*", "/", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _build_lotteon_price_payload(
    saved_itm_lst: list[dict],
    target_itm_lst: list[dict],
    spd_no: str,
) -> list[dict]:
    """수정 API(update_product) 후 itm 가격 보정용 itm_prc_lst 생성.

    update_product는 spd 헤더만 반영하고 itm 가격(slPrc)은 무시한다.
    별도 update_price(itm_prc_lst) 호출이 필요한데, sitmNo는 기존 옵션
    번호(target_itm_lst)에서, slPrc는 새 가격(saved_itm_lst)에서 가져와야 한다.

    매칭 키: itmOptLst[0].optVal (옵션 라벨, 정규화 적용)
    - 옵션 있는 itm: 정규화된 optVal 매칭 실패 시 **스킵** (다른 variant 가격으로
      덮어쓰는 위험 방지 — codex P1 지적). 정규화로 "240 / Beige" vs "240/Beige"
      같은 포맷 차이는 흡수.
    - 옵션 없는 단품(itmOptLst 생략): fallback_slprc 사용.
    """
    new_prc_map: dict[str, int] = {}
    fallback_slprc = 0
    for new_itm in saved_itm_lst or []:
        new_opts = new_itm.get("itmOptLst") or []
        opt_val = (
            new_opts[0].get("optVal", "")
            if new_opts and isinstance(new_opts[0], dict)
            else ""
        )
        try:
            slprc = int(new_itm.get("slPrc") or 0)
        except (TypeError, ValueError):
            slprc = 0
        if slprc <= 0:
            continue
        opt_val_norm = _norm_opt_label(opt_val)
        if opt_val_norm:
            new_prc_map[opt_val_norm] = slprc
        if slprc > fallback_slprc:
            fallback_slprc = slprc

    itm_prc_lst: list[dict] = []
    for old_itm in target_itm_lst or []:
        sitm_no = old_itm.get("sitmNo") or old_itm.get("itmNo")
        if not sitm_no:
            continue
        old_opts = old_itm.get("itmOptLst") or []
        opt_val = (
            old_opts[0].get("optVal", "")
            if old_opts and isinstance(old_opts[0], dict)
            else ""
        )
        opt_val_norm = _norm_opt_label(opt_val)
        if opt_val_norm:
            slprc = new_prc_map.get(opt_val_norm) or 0
            # 옵션 있는 itm은 매칭 실패 시 스킵 — fallback으로 다른 variant 가격을
            # silently 덮어쓰는 위험 방지 (codex P1).
            if slprc <= 0:
                continue
        else:
            # 옵션 없는 단품: fallback_slprc 사용
            slprc = fallback_slprc
            if slprc <= 0:
                continue
        itm_prc_lst.append(
            {
                "sitmNo": str(sitm_no),
                "spdNo": spd_no,
                "slPrc": slprc,
            }
        )
    return itm_prc_lst


# 브랜드명 접미사 목록 — 검색 전 자동 제거
_BRAND_SUFFIXES = [
    "키즈",
    "주니어",
    "주니어즈",
    "골프",
    "Kids",
    "Junior",
    "Juniors",
    "Golf",
]

# ──────────────────────────────────────────────────────────────────────────
# scatAttrLst 매핑 테이블
# attr_id: 롯데ON 속성코드 (카테고리 attr_list에서 조회)
# attr_val_id: 롯데ON 속성값코드 (cheetahAttr API에서 확인)
# ──────────────────────────────────────────────────────────────────────────

# 알려진 attr_id → 의미 매핑 (카테고리별로 다를 수 있음)
_ATTR_SEASON_ID = "10378"  # 사용계절
_ATTR_SEX_ID = "11337"  # 성별
_ATTR_PANTS_FIT_ID = "11933"  # 팬츠 핏 (카테고리 BC160501xx)
_ATTR_MATERIAL_ID = "11974"  # 의류 주요소재
_ATTR_COLOR_ID = "12438"  # 통합색상
_ATTR_SIZE_BOTTOM_ID = "12442"  # 성인 하의 사이즈
_ATTR_CLOTHES_TYPE_ID = "776739"  # 의류 종류
_ATTR_ITEM_TYPE_ID = "779690"  # 품목
_ATTR_BOTTOM_LENGTH_ID = "11780"  # 하의기장
_ATTR_LOOK_STYLE_ID = "11809"  # 룩/스타일
_ATTR_SHOES_MATERIAL_ID = "10265"  # 신발 소재 (의류 11974와 별도)
_ATTR_PRINT_ID = "11330"  # 프린트 (신발/의류 공통)
_ATTR_SHOES_FUNCTION_ID = "725056"  # 신발 부가기능
_ATTR_SKIRT_STYLE_ID = "11810"  # 스커트 스타일
_ATTR_CLOTH_LENGTH_ID = "11606"  # 의류 기장 (하의기장 11780과 별도)

# 사용계절 매핑 (무신사 season → attr_val_id)
_SEASON_MAP: dict[str, str] = {
    "사계절": "102421",
    "봄": "102422",
    "가을": "102422",
    "spring": "102422",
    "autumn": "102422",
    "fall": "102422",
    "여름": "102423",
    "summer": "102423",
    "겨울": "102424",
    "winter": "102424",
}

# 성별 매핑 (무신사 sex → attr_val_id)
_SEX_MAP: dict[str, str] = {
    "남성": "109487",
    "남자": "109487",
    "male": "109487",
    "men": "109487",
    "man": "109487",
    "여성": "109488",
    "여자": "109488",
    "female": "109488",
    "women": "109488",
    "woman": "109488",
    "공용": "109489",
    "남녀": "109489",
    "unisex": "109489",
}

# 의류 주요소재 매핑 (무신사 material 텍스트 키워드 → attr_val_id)
_MATERIAL_MAP: dict[str, str] = {
    "면": "112206",
    "코튼": "112206",
    "cotton": "112206",
    "폴리에스터": "716573347",
    "폴리에스텔": "716573347",
    "polyester": "716573347",
    "폴리": "716573347",
    "기모": "632861291",
    "나일론": "112203",
    "nylon": "112203",
    "리넨": "112204",
    "linen": "112204",
    "실크": "112205",
    "silk": "112205",
    "레이온": "718490456",
    "rayon": "718490456",
    "모달": "876098952",
    "modal": "876098952",
    "아크릴": "733156070",
    "acrylic": "733156070",
    "데님": "112190",
    "청": "112190",
    "denim": "112190",
    "벨벳": "112196",
    "velvet": "112196",
    "모": "112208",
    "울": "112208",
    "wool": "112208",
    "캐시미어": "591014974",
    "cashmere": "591014974",
    "코듀로이": "632861290",
    "corduroy": "632861290",
    "플리스": "876098953",
    "fleece": "876098953",
    "가죽": "547696592",
    "leather": "547696592",
    "인조가죽": "788761126",
    "폴리우레탄": "752495629",
    "새틴": "876098955",
    "satin": "876098955",
    "트위드": "835835046",
    "tweed": "835835046",
    "텐셀": "112207",
    "tencel": "112207",
    "퍼": "547696582",
    "fur": "547696582",
    "앙고라": "636940692",
    "angora": "636940692",
}

# 통합색상 매핑 (무신사 color 텍스트 키워드 → attr_val_id)
_COLOR_MAP: dict[str, str] = {
    "블랙": "114835",
    "검정": "114835",
    "black": "114835",
    "화이트": "114794",
    "흰": "114794",
    "white": "114794",
    "네이비": "114833",
    "navy": "114833",
    "그레이": "114830",
    "회색": "114830",
    "gray": "114830",
    "grey": "114830",
    "그레": "114830",
    "베이지": "114772",
    "beige": "114772",
    "브라운": "114782",
    "갈색": "114782",
    "brown": "114782",
    "카키": "114816",
    "khaki": "114816",
    "레드": "114822",
    "빨강": "114822",
    "red": "114822",
    "블루": "114804",
    "파랑": "114804",
    "blue": "114804",
    "그린": "114811",
    "초록": "114811",
    "green": "114811",
    "핑크": "114796",
    "분홍": "114796",
    "pink": "114796",
    "옐로우": "114836",
    "노랑": "114836",
    "yellow": "114836",
    "오렌지": "114818",
    "orange": "114818",
    "퍼플": "91478236",
    "보라": "91478236",
    "purple": "91478236",
    "아이보리": "114773",
    "ivory": "114773",
    "차콜": "114831",
    "charcoal": "114831",
    "올리브": "114814",
    "olive": "114814",
    "와인": "114823",
    "wine": "114823",
    "버건디": "114824",
    "burgundy": "114824",
    "멀티": "114839",
    "multi": "114839",
    "코랄": "114820",
    "coral": "114820",
    "스카이": "114806",
    "sky": "114806",
    "라벤다": "114802",
    "lavender": "114802",
    "민트": "114812",
    "mint": "114812",
    "머스타드": "114837",
    "mustard": "114837",
    "골드": "114778",
    "gold": "114778",
    "실버": "114828",
    "silver": "114828",
    "연두": "114813",
    "데님": "114810",
    "아쿠아": "114807",
    "aqua": "114807",
}

# 의류 종류 매핑 (category 텍스트 키워드 → attr_val_id)
_CLOTHES_TYPE_MAP: dict[str, str] = {
    "바지": "625980564",
    "팬츠": "625980564",
    "레깅스": "625980564",
    "자켓": "625980557",
    "코트": "625980557",
    "점퍼": "625980558",
    "패딩": "625980558",
    "야상": "625980558",
    "가디건": "625980559",
    "니트": "625980561",
    "조끼": "625980561",
    "셔츠": "625980562",
    "블라우스": "625980562",
    "티셔츠": "625980563",
    "맨투맨": "625980563",
    "후디": "625980563",
    "후드": "625980563",
    "스커트": "625980566",
    "원피스": "625980567",
    "점프수트": "625980567",
    "수영복": "628783835",
    "래시가드": "628783835",
    "정장": "628785579",
    "트레이닝": "628785581",
}

# 성인 하의 사이즈 매핑 (옵션명 → attr_val_id)
_SIZE_BOTTOM_MAP: dict[str, str] = {
    "XS": "114990",
    "S": "114991",
    "M": "114992",
    "L": "114993",
    "XL": "114994",
    "2XL": "114995",
    "XXL": "114995",
    "3XL": "114996",
    "4XL": "114997",
    "5XL": "114998",
    "22": "803338603",
    "23": "114969",
    "24": "114970",
    "25": "114971",
    "26": "114972",
    "27": "114973",
    "28": "114974",
    "29": "114975",
    "30": "114976",
    "31": "114977",
    "32": "114978",
    "33": "114979",
    "34": "114980",
    "35": "114981",
    "36": "114982",
    "38": "114984",
    "40": "114986",
    "42": "91478294",
    "44": "91478295",
    "FREE": "114968",
    "free": "114968",
    "one size": "114968",
    "1size": "114968",
    "프리": "114968",
}

# 팬츠 핏 매핑 (상품명/태그 키워드 → attr_val_id)
_PANTS_FIT_MAP: dict[str, str] = {
    "배기": "112026",
    "baggy": "112026",
    "부츠컷": "112027",
    "boot": "112027",
    "와이드": "112028",
    "wide": "112028",
    "스트레이트": "112029",
    "일자": "112029",
    "straight": "112029",
    "슬림": "112030",
    "스키니": "112030",
    "slim": "112030",
    "skinny": "112030",
    "테이퍼드": "547916208",
    "tapered": "547916208",
    "조거": "610443195",
    "jogger": "610443195",
    "핀턱": "773234579",
    "pintuck": "773234579",
    "루즈": "112028",
    "loose": "112028",  # 루즈핏 → 와이드 계열
    "카고": "112029",
    "cargo": "112029",  # 카고 → 스트레이트 계열
    "레귤러": "112029",
    "regular": "112029",  # 레귤러 → 스트레이트 계열
    "릴랙스": "112028",
    "relaxed": "112028",  # 릴랙스 → 와이드 계열
}

# 하의기장 매핑 (상품명/카테고리 키워드 → attr_val_id, 기본: 긴바지)
_BOTTOM_LENGTH_MAP: dict[str, str] = {
    "반바지": "111194",
    "숏": "111194",
    "short": "111194",
    "숏팬츠": "558495938",
    "3부": "558495938",
    "7부": "111198",
    "9부": "111200",
}

# 신발 소재 매핑 (무신사 material 키워드 → attr_val_id) — optCd "10265"
_SHOES_MATERIAL_MAP: dict[str, str] = {
    "가죽": "101543",
    "leather": "101543",
    "에나멜": "101544",
    "enamel": "101544",
    "스웨이드": "101545",
    "suede": "101545",
    "패브릭": "101546",
    "fabric": "101546",
    "벨벳": "101548",
    "velvet": "101548",
    "퍼": "101550",
    "fur": "101550",
    "면": "101551",
    "cotton": "101551",
    "eva": "101553",
    "EVA": "101553",
    "폴리에스테르": "101554",
    "폴리에스텔": "101554",
    "polyester": "101554",
    "폴리": "101554",
    "폴리우레탄": "101555",
    "polyurethane": "101555",
    "pu": "101555",
    "인조가죽": "101556",
    "합성피혁": "101556",
    "synthetic leather": "101556",
    "네오프렌": "101557",
    "neoprene": "101557",
    "젤리": "101549",
    "고무": "101549",
    "rubber": "101549",
    "매쉬": "547916352",
    "메시": "547916352",
    "mesh": "547916352",
    "크로슬라이트": "593337873",
    "croslite": "593337873",
    "나일론": "593692932",
    "nylon": "593692932",
    "폴리아미드": "598879479",
    "polyamide": "598879479",
    "pvc": "718157948",
    "PVC": "718157948",
    "합성섬유": "722574414",
    "합성 섬유": "722574414",
    "synthetic fiber": "722574414",
    "스판덱스": "753156392",
    "spandex": "753156392",
    "elastane": "753156392",
    "코르크": "835832335",
    "cork": "835832335",
}

# 스커트 스타일 매핑 (category/상품명 키워드 → attr_val_id) — optCd "11810"
_SKIRT_STYLE_MAP: dict[str, str] = {
    "A라인": "111349",
    "에이라인": "111349",
    "a-line": "111349",
    "플리츠": "111350",
    "pleated": "111350",
    "H라인": "111351",
    "에이치라인": "111351",
    "h-line": "111351",
    "타이트": "111351",
    "머메이드": "111352",
    "mermaid": "111352",
    "언밸런스": "111353",
    "unbalance": "111353",
    "비대칭": "111353",
    "랩스커트": "111354",
    "랩": "111354",
    "wrap": "111354",
    "벌룬": "111355",
    "balloon": "111355",
    "티어드": "111356",
    "캉캉": "111356",
    "tiered": "111356",
    "플레어": "856854512",
    "flare": "856854512",
}

# 의류 기장 매핑 (category/상품명 키워드 → attr_val_id) — optCd "11606"
_CLOTH_LENGTH_MAP: dict[str, str] = {
    "미니": "110631",
    "mini": "110631",  # 추정값 — 캡처로 확인 필요
    "미디": "110632",
    "midi": "110632",
    "맥시": "110633",
    "maxi": "110633",  # 추정값 — 캡처로 확인 필요
    "롱": "110633",
    "long": "110633",
}

# 의류핏 매핑 (상품명/카테고리 키워드 → attr_val_id) — attr_id는 카테고리 API에서 동적 조회
_CLOTH_FIT_MAP: dict[str, str] = {
    "오버핏": "112024",
    "오버사이즈": "112024",
    "oversize": "112024",
    "oversized": "112024",
    "루즈핏": "112024",
    "루즈": "112024",
    "슬림핏": "112025",
    "슬림": "112025",
    "slim": "112025",
    "skinny": "112025",
    "레귤러핏": "112023",
    "레귤러": "112023",
    "regular": "112023",
    # 스탠다드는 기본값으로 처리 (아래 로직에서)
}
_CLOTH_FIT_DEFAULT = "112022"  # 스탠다드

# 네크라인 매핑 (소싱처 데이터 키워드 → attr_val_id) — attr_id는 카테고리 API에서 동적 조회
_NECKLINE_MAP: dict[str, str] = {
    "라운드": "111311",
    "라운드넥": "111311",
    "round": "111311",
    "크루넥": "111311",
    "브이넥": "111312",
    "v넥": "111312",
    "v-neck": "111312",
    "터틀넥": "111313",
    "목폴라": "111313",
    "turtle": "111313",
    "폴라": "111313",
    "하이넥": "111314",
    "하프넥": "111314",
    "반폴라": "111314",
    "mock": "111314",
    "후드": "111315",
    "hood": "111315",
    "hooded": "111315",
    "카라": "111316",
    "폴로": "111316",
    "collar": "111316",
    "polo": "111316",
    "오프숄더": "111317",
    "off shoulder": "111317",
    "스퀘어넥": "111318",
    "square": "111318",
    "보트넥": "111319",
    "boat": "111319",
}

# 소매기장 매핑 — attr_id는 카테고리 API에서 동적 조회
_SLEEVE_LENGTH_MAP: dict[str, str] = {
    "민소매": "111257",
    "나시": "111257",
    "sleeveless": "111257",
    "탱크탑": "111257",
    "반팔": "111258",
    "short sleeve": "111258",
    "숏슬리브": "111258",
    "7부소매": "111259",
    "칠부": "111259",
    # 긴소매는 기본값으로 처리
}
_SLEEVE_LENGTH_DEFAULT = "111260"  # 긴소매

# optValCd → 롯데ON 표시명 (scatAttrLst의 optVal 필드 — 필수, null 불가)
_OPT_VAL_LABELS: dict[str, str] = {
    # 사용계절
    "102421": "사계절용",
    "102422": "봄/가을용",
    "102423": "여름용",
    "102424": "겨울용",
    # 성별
    "109487": "남성",
    "109488": "여성",
    "109489": "공용",
    # 의류 주요소재
    "112206": "면",
    "716573347": "폴리에스테르",
    "632861291": "기모",
    "112203": "나일론",
    "112204": "리넨",
    "112205": "실크",
    "718490456": "레이온",
    "876098952": "모달",
    "733156070": "아크릴",
    "112190": "데님",
    "112196": "벨벳",
    "112208": "모",
    "591014974": "캐시미어",
    "632861290": "코듀로이",
    "876098953": "플리스",
    "547696592": "가죽",
    "788761126": "인조가죽",
    "752495629": "폴리우레탄",
    "876098955": "새틴",
    "835835046": "트위드",
    "112207": "텐셀",
    "547696582": "퍼(FUR)",
    "636940692": "앙고라",
    # 통합색상
    "114835": "블랙",
    "114794": "화이트",
    "114833": "네이비",
    "114830": "그레이",
    "114772": "베이지",
    "114782": "브라운",
    "114816": "카키",
    "114822": "레드",
    "114804": "블루",
    "114811": "그린",
    "114796": "핑크",
    "114836": "옐로우",
    "114818": "오렌지",
    "91478236": "퍼플",
    "114773": "아이보리",
    "114831": "차콜",
    "114814": "올리브",
    "114823": "와인",
    "114824": "버건디",
    "114839": "멀티",
    "114820": "코랄",
    "114806": "스카이블루",
    "114802": "라벤더",
    "114812": "민트",
    "114837": "머스타드",
    "114778": "골드",
    "114828": "실버",
    "114813": "연두",
    "114810": "데님",
    "114807": "아쿠아",
    # 의류 종류
    "625980564": "바지/레깅스",
    "625980557": "자켓/코트",
    "625980558": "점퍼/패딩",
    "625980559": "가디건",
    "625980561": "니트/조끼",
    "625980562": "셔츠/블라우스",
    "625980563": "티셔츠/맨투맨/후드",
    "625980566": "스커트",
    "625980567": "원피스/점프수트",
    "628783835": "수영복/래시가드",
    "628785579": "정장",
    "628785581": "트레이닝",
    # 품목 (779690) — 카테고리별 단일값
    "628662010": "의류",
    # 하의기장
    "111194": "반바지",
    "558495938": "숏팬츠/3부",
    "111198": "7부",
    "111200": "9부",
    "111202": "긴바지",
    # 룩/스타일
    "111334": "캐주얼",
    "111338": "오피스",
    "111340": "글램/섹시",
    "111342": "펑크",
    "111344": "빈티지/히피",
    "111345": "힙합/스트릿",
    "111346": "페미닌",
    "547698709": "마린",
    "547698710": "아웃도어",
    "547698711": "파티",
    "547698712": "프레피",
    "547698713": "리조트",
    "547698714": "웨딩",
    "547698715": "컨트리",
    "547919177": "레트로",
    "604509736": "로맨틱",
    "604509737": "큐트",
    "629429090": "에스닉",
    "629485501": "밀리터리",
    # 성인 하의 사이즈
    "114968": "FREE",
    "114990": "XS",
    "114991": "S",
    "114992": "M",
    "114993": "L",
    "114994": "XL",
    "114995": "2XL",
    "114996": "3XL",
    "114997": "4XL",
    "114998": "5XL",
    "803338603": "22",
    "114969": "23",
    "114970": "24",
    "114971": "25",
    "114972": "26",
    "114973": "27",
    "114974": "28",
    "114975": "29",
    "114976": "30",
    "114977": "31",
    "114978": "32",
    "114979": "33",
    "114980": "34",
    "114981": "35",
    "114982": "36",
    "114984": "38",
    "114986": "40",
    "91478294": "42",
    "91478295": "44",
    # 팬츠 핏
    "112026": "배기",
    "112027": "부츠컷",
    "112028": "와이드",
    "112029": "스트레이트",
    "112030": "슬림",
    "547916208": "테이퍼드",
    "610443195": "조거",
    "773234579": "핀턱",
    # 신발 소재 (optCd 10265)
    "101543": "가죽",
    "101544": "에나멜",
    "101545": "스웨이드",
    "101546": "패브릭",
    "101548": "벨벳",
    "101549": "젤리/고무",
    "101550": "퍼(FUR)",
    "101551": "면/면혼방",
    "101553": "EVA",
    "101554": "폴리에스테르",
    "101555": "폴리우레탄",
    "101556": "인조가죽",
    "101557": "네오프렌",
    "547916352": "매쉬",
    "593337873": "크로슬라이트",
    "593692932": "나일론",
    "598879479": "폴리아미드",
    "718157948": "PVC",
    "722574414": "합성 섬유",
    "753156392": "스판덱스",
    "835832335": "코르크",
    # 프린트 (optCd 11330 — 신발/의류 공통)
    "605647945": "로고",
    # 스커트 스타일 (optCd 11810)
    "111349": "A라인",
    "111350": "플리츠",
    "111351": "H라인",
    "111352": "머메이드",
    "111353": "언밸런스",
    "111354": "랩스커트",
    "111355": "벌룬",
    "111356": "티어드/캉캉",
    "856854512": "플레어",
    # 의류 기장 (optCd 11606)
    "110631": "미니",
    "110632": "미디",
    "110633": "맥시",
    # 의류핏
    "112022": "스탠다드",
    "112023": "레귤러",
    "112024": "오버사이즈",
    "112025": "슬림",
    # 네크라인
    "111311": "라운드넥",
    "111312": "브이넥",
    "111313": "터틀넥",
    "111314": "하이넥",
    "111315": "후드",
    "111316": "카라",
    "111317": "오프숄더",
    "111318": "스퀘어넥",
    "111319": "보트넥",
    # 소매기장
    "111257": "민소매",
    "111258": "반팔",
    "111259": "7부소매",
    "111260": "긴소매",
    # 신발 부가기능 (optCd 725056)
    "609276717": "키높이",
    "609276718": "통풍",
    "609276719": "충격흡수",
    "609276720": "경량",
    "609276721": "에어",
}


_BOTTOM_KW = frozenset(
    {
        "바지",
        "팬츠",
        "청바지",
        "레깅스",
        "스커트",
        "치마",
        "반바지",
        "쇼츠",
        "shorts",
        "pants",
        "skirt",
        "leggings",
        "trousers",
    }
)

# 사용계절 기본값 판별용 키워드 (자켓/코트 → 봄가을, 패딩/점퍼 → 겨울)
_JACKET_KW = frozenset(
    {"자켓", "코트", "jacket", "coat", "블레이저", "blazer", "트렌치", "trench"}
)
_PADDING_KW = frozenset({"패딩", "점퍼", "다운", "padding", "jumper", "puffer", "down"})

# 남성 BC4104 → 여성 BC4110 카테고리 매핑
_BC_M_TO_F: dict[str, str] = {
    "BC41040100": "BC41100100",  # 긴바지
    "BC41040200": "BC41100200",  # 긴팔티셔츠
    "BC41040300": "BC41100300",  # 반팔티셔츠
    "BC41040900": "BC41100900",  # 반바지
    "BC41041000": "BC41101000",  # 맨투맨
    "BC41041200": "BC41101200",  # 후드
    "BC41041300": "BC41101400",  # 집업 (불규칙 오프셋)
    "BC41041400": "BC41101500",  # 트레이닝복 (불규칙 오프셋)
    "BC41041500": "BC41101600",  # 바람막이/재킷
    "BC41041600": "BC41101700",  # 점퍼
    "BC41041800": "BC41101900",  # 니트
}

# FC05 패션의류 → FC08 스포츠의류 경로 변환
_FASHION_TO_SPORTS: dict[str, str] = {
    "패션의류 > 여성의류 > 스커트": "스포츠의류/운동화 > 여성스포츠의류 > 스커트",
    "패션의류 > 여성의류 > 원피스": "스포츠의류/운동화 > 여성스포츠의류 > 원피스",
    "패션의류 > 여성의류 > 점프수트/오버올": "스포츠의류/운동화 > 여성스포츠의류 > 원피스",
    "패션의류 > 여성의류 > 원피스/점프수트": "스포츠의류/운동화 > 여성스포츠의류 > 원피스",
    "패션의류 > 여성의류 > 점프슈트": "스포츠의류/운동화 > 여성스포츠의류 > 원피스",
    "패션의류 > 여성의류 > 바지": "스포츠의류/운동화 > 여성스포츠의류 > 긴바지",
    "패션의류 > 여성의류 > 청바지": "스포츠의류/운동화 > 여성스포츠의류 > 긴바지",
    "패션의류 > 여성의류 > 티셔츠": "스포츠의류/운동화 > 여성스포츠의류 > 반팔티셔츠",
    "패션의류 > 여성의류 > 맨투맨": "스포츠의류/운동화 > 여성스포츠의류 > 맨투맨",
    "패션의류 > 여성의류 > 후드": "스포츠의류/운동화 > 여성스포츠의류 > 후드",
    "패션의류 > 여성의류 > 트레이닝복": "스포츠의류/운동화 > 여성스포츠의류 > 트레이닝복",
    "패션의류 > 남성의류 > 티셔츠": "스포츠의류/운동화 > 남성스포츠의류 > 반팔티셔츠",
    "패션의류 > 남성의류 > 바지": "스포츠의류/운동화 > 남성스포츠의류 > 긴바지",
    "패션의류 > 남성의류 > 청바지": "스포츠의류/운동화 > 남성스포츠의류 > 긴바지",
    "패션의류 > 남성의류 > 맨투맨": "스포츠의류/운동화 > 남성스포츠의류 > 맨투맨",
    "패션의류 > 남성의류 > 후드": "스포츠의류/운동화 > 남성스포츠의류 > 후드",
    "패션의류 > 남성의류 > 트레이닝복": "스포츠의류/운동화 > 남성스포츠의류 > 트레이닝복",
    "패션의류 > 남성의류 > 아우터": "스포츠의류/운동화 > 남성스포츠의류 > 점퍼",
}

# 거래처 미허용 카테고리 → 대체 카테고리 강제 변환 (경로형 category_id 대상)
_BLOCKED_PATHS: dict[str, str] = {
    "스포츠의류/운동화 > 남성스포츠의류 > 다운/패딩": "스포츠의류/운동화 > 남성스포츠의류 > 점퍼",
    "스포츠의류/운동화 > 여성스포츠의류 > 다운/패딩": "스포츠의류/운동화 > 여성스포츠의류 > 점퍼",
}

# 거래처 미허용 전시카테고리(FC) 코드 — disp_cat_id 해석 결과가 여기 들어가면 폴백 필요
# FC08090202: (구) 다운/패딩 계열 미허용
# FC08030602: 2026-05-16 moonol06 여성 점퍼 등록 시 9999 차단 확인
_BLOCKED_DISP_CAT_IDS: frozenset[str] = frozenset({"FC08090202", "FC08030602"})

# 미허용 FC 발생 시 폴백할 표준 BC 코드 (집업) — 점퍼와 가까운 안전 카테고리
_FALLBACK_BC_FOR_BLOCKED_DISP = {
    "여성": "BC41101400",  # 여성스포츠의류 > 집업
    "남성": "BC41041300",  # 남성스포츠의류 > 집업
}

# BC23 패션의류 → BC41 스포츠의류 BC코드 변환
_BC23_TO_BC41: dict[str, str] = {
    "BC23110400": "BC41101800",  # 여성의류>스커트 → 여성스포츠의류>스커트
    "BC23110100": "BC41100100",  # 여성의류>긴바지 → 여성스포츠의류>긴바지
    "BC23110200": "BC41101000",  # 여성의류>티셔츠 → 여성스포츠의류>반팔티셔츠
    "BC23110300": "BC41101100",  # 여성의류>원피스 → 여성스포츠의류>원피스
    "BC23110500": "BC41101500",  # 여성의류>트레이닝복 → 여성스포츠의류>트레이닝복
}

# 소싱 원본 BC코드 허용 범위 (스포츠의류/신발/패션잡화)
_ALLOWED_BC_PREFIXES = ("BC4103", "BC4104", "BC4109", "BC4110", "BC47")

# 등록 후 API 안정화 대기 시간 (초)
_POST_REGISTER_DELAY = 5


def _is_bottom_product(product: dict[str, Any]) -> bool:
    """상품이 하의(바지/스커트류)인지 판별."""
    text = " ".join(
        filter(
            None,
            [
                product.get("name", ""),
                product.get("category2", ""),
                product.get("category3", ""),
                product.get("category4", ""),
            ],
        )
    ).lower()
    return any(kw in text for kw in _BOTTOM_KW)


def _build_scat_attr_lst(
    product: dict[str, Any], attr_ids: list[str], attr_raw: list[dict] | None = None
) -> list[dict[str, str]]:
    """무신사 소싱 데이터 → 롯데ON scatAttrLst 변환.

    Args:
      product: CollectedProduct dict (product_copy)
      attr_ids: 카테고리에서 지원하는 attr_id 목록
      attr_raw: 카테고리 attr_list 원시 데이터 (attr_id + attr_nm 포함)

    Returns:
      [{"optCd": attr_id, "optValCd": attr_val_id}, ...]
    """
    result: list[dict[str, str]] = []
    attr_id_set = set(attr_ids)
    is_bottom = _is_bottom_product(product)

    # attr_nm → attr_id 동적 매핑 (의류핏/네크라인 등 하드코딩 불가한 속성용)
    attr_nm_to_id: dict[str, str] = {}
    if attr_raw:
        for a in attr_raw:
            nm = (a.get("attr_nm") or "").strip()
            aid = str(a.get("attr_id", ""))
            if nm and aid:
                attr_nm_to_id[nm] = aid

    def _add(attr_id: str, val_id: str) -> None:
        if not (attr_id in attr_id_set and val_id):
            return
        opt_val = _OPT_VAL_LABELS.get(val_id, "")
        if not opt_val:
            logger.warning(
                f"[롯데ON] optVal 라벨 없음 — optCd={attr_id} optValCd={val_id} (속성 제외)"
            )
            return
        result.append({"optCd": attr_id, "optValCd": val_id, "optVal": opt_val})

    def _keyword_match(text: str, mapping: dict[str, str]) -> str:
        """텍스트에서 첫 번째 매칭 키워드의 attr_val_id 반환."""
        text_lower = text.lower()
        for key, val in mapping.items():
            if key.lower() in text_lower:
                return val
        return ""

    # ── 카테고리 텍스트 (여러 속성에서 공통 사용) ────────────────────
    cat_text = " ".join(
        filter(
            None,
            [
                product.get("category1") or "",
                product.get("category2") or "",
                product.get("category3") or "",
                product.get("category4") or "",
            ],
        )
    )

    # ── 사용계절 ──────────────────────────────────────────────────────
    # "2026 SS", "FW", "AW" 등 패션 시즌 코드 지원
    season_raw = product.get("season") or []
    if isinstance(season_raw, str):
        season_raw = [
            s.strip() for s in season_raw.replace(",", "/").split("/") if s.strip()
        ]
    if season_raw:
        combined = " ".join(season_raw).lower()
        season_set = {s for s in season_raw if s in {"봄", "여름", "가을", "겨울"}}
        if len(season_set) >= 3 or "사계절" in combined:
            _add(_ATTR_SEASON_ID, "102421")  # 사계절용
        elif re.search(r"\b(fw|aw|fall|autumn|winter|겨울)\b", combined):
            _add(_ATTR_SEASON_ID, "102424")  # 겨울용 (FW/AW)
        elif re.search(r"\b(ss|spring|봄|가을)\b", combined):
            _add(_ATTR_SEASON_ID, "102422")  # 봄/가을용 (SS)
        elif re.search(r"\b(summer|여름)\b", combined):
            _add(_ATTR_SEASON_ID, "102423")  # 여름용
    # 사용계절 매핑 없으면 카테고리별 기본값 적용
    if _ATTR_SEASON_ID in attr_id_set and not any(
        r.get("optCd") == _ATTR_SEASON_ID for r in result
    ):
        cat_and_name = (cat_text + " " + (product.get("name") or "")).lower()
        if any(kw in cat_and_name for kw in _PADDING_KW):
            _add(_ATTR_SEASON_ID, "102424")  # 겨울용
        elif any(kw in cat_and_name for kw in _JACKET_KW):
            _add(_ATTR_SEASON_ID, "102422")  # 봄/가을용
        else:
            _add(_ATTR_SEASON_ID, "102421")  # 사계절용

    # ── 성별 ─────────────────────────────────────────────────────────
    sex = (product.get("sex") or "").lower()
    val = _keyword_match(sex, _SEX_MAP)
    if val:
        _add(_ATTR_SEX_ID, val)

    # ── 의류 주요소재 ─────────────────────────────────────────────────
    material = (product.get("material") or "").lower()
    val = _keyword_match(material, _MATERIAL_MAP)
    if val:
        _add(_ATTR_MATERIAL_ID, val)

    # ── 통합색상 ─────────────────────────────────────────────────────
    color = (product.get("color") or "").lower()
    val = _keyword_match(color, _COLOR_MAP)
    if val:
        _add(_ATTR_COLOR_ID, val)

    # ── 의류 종류 (category + 상품명에서 추출) ──────────────────────
    val = _keyword_match(cat_text, _CLOTHES_TYPE_MAP)
    if not val:
        # 카테고리에서 못 찾으면 상품명에서 재시도
        val = _keyword_match(product.get("name") or "", _CLOTHES_TYPE_MAP)
    if val:
        _add(_ATTR_CLOTHES_TYPE_ID, val)

    # ── 품목 → 신발/잡화 카테고리는 스킵 (의류 val_id만 알고 있음) ──────
    _is_shoes_cat = (product.get("category1") or "").strip() in {
        "신발",
        "스포츠신발",
        "운동화",
    }
    if not _is_shoes_cat:
        _add(_ATTR_ITEM_TYPE_ID, "628662010")

    # ── 신발 전용 속성 ─────────────────────────────────────────────────
    if _is_shoes_cat:
        # 프린트: 브랜드 신발은 항상 로고 고정
        _add(_ATTR_PRINT_ID, "605647945")
        # 부가기능: 키높이 제외 전부 (통풍/충격흡수/경량/에어)
        for _func_val in ("609276718", "609276719", "609276720", "609276721"):
            _add(_ATTR_SHOES_FUNCTION_ID, _func_val)
        # 신발 소재: 상품 정보에서 매핑, 없으면 빈값 유지
        shoes_material = (product.get("material") or "").lower()
        shoes_mat_val = _keyword_match(shoes_material, _SHOES_MATERIAL_MAP)
        if shoes_mat_val:
            _add(_ATTR_SHOES_MATERIAL_ID, shoes_mat_val)

    # ── 의류 전용 속성 ─────────────────────────────────────────────────
    if not _is_shoes_cat:
        # 프린트: 의류는 항상 로고 고정
        _add(_ATTR_PRINT_ID, "605647945")
        # 스커트 스타일: category/상품명 키워드에서 추출
        style_text = (product.get("name") or "") + " " + cat_text
        skirt_style_val = _keyword_match(style_text, _SKIRT_STYLE_MAP)
        if skirt_style_val:
            _add(_ATTR_SKIRT_STYLE_ID, skirt_style_val)
        # 의류 기장: category/상품명 키워드에서 추출
        cloth_len_val = _keyword_match(style_text, _CLOTH_LENGTH_MAP)
        if cloth_len_val:
            _add(_ATTR_CLOTH_LENGTH_ID, cloth_len_val)

    # ── 의류핏 (동적 attr_id — 상품명/카테고리에서 추출, 없으면 스탠다드) ──
    _fit_attr_id = attr_nm_to_id.get("의류핏", "")
    if _fit_attr_id and _fit_attr_id in attr_id_set:
        name_and_cat = (product.get("name") or "") + " " + cat_text
        fit_val = _keyword_match(name_and_cat, _CLOTH_FIT_MAP)
        _add(_fit_attr_id, fit_val or _CLOTH_FIT_DEFAULT)

    # ── 네크라인 (동적 attr_id — 소싱처 정보 있으면 입력, 없으면 생략) ──
    _neck_attr_id = attr_nm_to_id.get("네크라인", "")
    if _neck_attr_id and _neck_attr_id in attr_id_set:
        neck_src = " ".join(
            filter(
                None,
                [
                    product.get("name") or "",
                    product.get("neckline") or "",
                    " ".join(product.get("tags") or []),
                ],
            )
        )
        neck_val = _keyword_match(neck_src, _NECKLINE_MAP)
        if neck_val:
            _add(_neck_attr_id, neck_val)

    # ── 소매기장 (동적 attr_id — 상품명에서 추출, 없으면 긴소매) ──────
    _sleeve_attr_id = attr_nm_to_id.get("소매기장", "")
    if _sleeve_attr_id and _sleeve_attr_id in attr_id_set:
        sleeve_src = (product.get("name") or "") + " " + cat_text
        sleeve_val = _keyword_match(sleeve_src, _SLEEVE_LENGTH_MAP)
        _add(_sleeve_attr_id, sleeve_val or _SLEEVE_LENGTH_DEFAULT)

    # ── 성인 하의 사이즈 (하의 상품만, options 에서 추출) ──────────
    if is_bottom and _ATTR_SIZE_BOTTOM_ID in attr_id_set:
        options = product.get("options") or []
        added_sizes: set[str] = set()
        for opt in options:
            size_nm = (opt.get("name") or opt.get("size") or "").strip()
            val = _SIZE_BOTTOM_MAP.get(size_nm, "")
            if val and val not in added_sizes:
                opt_val_nm = _OPT_VAL_LABELS.get(val, "")
                if opt_val_nm:
                    result.append(
                        {
                            "optCd": _ATTR_SIZE_BOTTOM_ID,
                            "optValCd": val,
                            "optVal": opt_val_nm,
                        }
                    )
                    added_sizes.add(val)

    # ── 팬츠 핏 (하의 상품만, 상품명 + 태그에서 키워드 추출) ────────
    if is_bottom and _ATTR_PANTS_FIT_ID in attr_id_set:
        name_and_tags = (
            (product.get("name") or "") + " " + " ".join(product.get("tags") or [])
        )
        val = _keyword_match(name_and_tags, _PANTS_FIT_MAP)
        if val:
            _add(_ATTR_PANTS_FIT_ID, val)

    # ── 하의기장 (하의 상품만, 기본: 긴바지) ──────────────────────
    if is_bottom and _ATTR_BOTTOM_LENGTH_ID in attr_id_set:
        search_text = (product.get("name") or "") + " " + cat_text
        val = _keyword_match(search_text, _BOTTOM_LENGTH_MAP)
        _add(_ATTR_BOTTOM_LENGTH_ID, val or "111202")  # 키워드 없으면 긴바지

    # ── 룩/스타일 (항상 캐주얼 고정) ───────────────────────────────
    if _ATTR_LOOK_STYLE_ID in attr_id_set:
        _add(_ATTR_LOOK_STYLE_ID, "111334")

    return result


def _strip_brand_suffix(name: str) -> str:
    """브랜드명 뒤에 붙은 접미사 제거. 예: '나이키 키즈' → '나이키'"""
    for suffix in _BRAND_SUFFIXES:
        if name.endswith(" " + suffix):
            return name[: -(len(suffix) + 1)].strip()
    return name


async def _search_brand_no(client: Any, brand_name: str) -> str:
    """접미사 제거 → 첫 단어만 → 스킵 순서로 브랜드 검색. brdNo 반환."""
    stripped = _strip_brand_suffix(brand_name)
    candidates = [stripped]

    # 첫 단어만 시도 (접미사 제거 결과와 다를 때만)
    first_word = stripped.split()[0] if stripped else ""
    if first_word and first_word != stripped:
        candidates.append(first_word)

    for candidate in candidates:
        try:
            result = await client.search_brand(candidate)
            items = result.get("itemList") or result.get("data") or []
            if isinstance(items, list) and items:
                item = items[0]
                d = item.get("data", item) if isinstance(item, dict) else item
                # 브랜드 검색 응답(cheetahBrnd)의 실제 키: brnd_id
                brd_no = (
                    d.get("brnd_id", "") or d.get("brnd_no", "") or d.get("brdNo", "")
                )
                if brd_no:
                    logger.info(
                        f"[롯데ON] 브랜드 검색 성공: {brand_name!r} → {candidate!r} brdNo={brd_no}"
                    )
                    return str(brd_no)
        except Exception as e:
            logger.warning(f"[롯데ON] 브랜드 검색 실패 ({candidate!r}): {e}")

    logger.info(
        f"[롯데ON] 브랜드 검색 스킵 — 브랜드 공란으로 등록 진행: {brand_name!r}"
    )
    return ""


class LotteonPlugin(MarketPlugin):
    market_type = "lotteon"
    policy_key = "롯데ON"
    required_fields = ["name", "sale_price"]

    def _validate_category(self, category_id: str) -> str:
        """롯데ON은 BC 접두사 카테고리 코드 허용 (BC41030100 형식)."""
        return category_id or ""

    def transform(self, product: dict, category_id: str, **kwargs) -> dict:
        """상품 데이터 → 롯데ON API 포맷 변환."""
        from backend.domain.samba.proxy.lotteon import LotteonClient

        tr_grp_cd = kwargs.get("tr_grp_cd", "SR")
        tr_no = kwargs.get("tr_no", "")
        return LotteonClient.transform_product(product, category_id, tr_grp_cd, tr_no)

    async def execute(
        self,
        session,
        product: dict,
        creds: dict,
        category_id: str,
        account,
        existing_no: str,
    ) -> dict[str, Any]:
        """롯데ON 상품 등록/수정 — 전체 로직."""
        from backend.domain.samba.proxy.lotteon import LotteonClient

        # market_base.handle()이 execute() 호출 전에 session.commit()을 수행하므로
        # ORM account 객체의 속성은 expired 상태. commit 이후 account.* 직접 접근은
        # async lazy-refresh를 유발해 "greenlet_spawn has not been called" 에러를 낸다.
        # 따라서 진입 시점에 필요한 필드를 dict/스칼라로 스냅샷하여 이후엔 ORM 접근 금지.
        _account_extras_snapshot: dict[str, Any] = {}
        _account_api_key_snapshot: str = ""
        if account:
            try:
                _account_extras_snapshot = (
                    getattr(account, "additional_fields", None) or {}
                )
            except Exception:
                _account_extras_snapshot = {}
            try:
                _account_api_key_snapshot = getattr(account, "api_key", "") or ""
            except Exception:
                _account_api_key_snapshot = ""

        api_key = creds.get("apiKey", "")

        # account 필드에서 보완
        if not api_key and account:
            extras = _account_extras_snapshot
            api_key = extras.get("apiKey", "") or _account_api_key_snapshot or ""

        if not api_key:
            return {
                "success": False,
                "message": "롯데ON API Key가 비어있습니다. 설정에서 해당 계정을 수정 후 저장해주세요.",
            }

        # ── 경량 가격/재고 업데이트 (오토튠 최적화) ──────────────────────
        # _skip_image_upload=True → price/stock만 변경된 경우
        # 카테고리/브랜드/속성 재계산 없이 경량 API로 직접 업데이트
        if product.get("_skip_image_upload") and existing_no:
            try:
                client = await _get_cached_client(api_key)

                # 기존 상품에서 단품번호(itmNo) 조회
                prod_resp = await client.get_product(existing_no)
                inner = prod_resp.get("data", prod_resp)
                if isinstance(inner, dict):
                    spd_info = inner.get("spdLst") or inner.get("spdInfo") or inner
                    if isinstance(spd_info, list) and spd_info:
                        spd_info = spd_info[0]
                else:
                    spd_info = {}

                itm_lst = (
                    (spd_info.get("itmLst") or []) if isinstance(spd_info, dict) else []
                )
                if not itm_lst:
                    logger.warning(
                        f"[롯데ON] 경량 업데이트 실패 — 단품 목록 없음, 전체 수정으로 폴백: {existing_no}"
                    )
                    # 폴백: 아래 전체 로직으로 진행
                else:
                    _raw_price = int(product.get("sale_price") or 0)
                    new_options = product.get("options") or []

                    # 계정 재고수량 상한 (transform_product와 동일 정책)
                    # 주의: 경량 분기는 1410 라인의 product_copy 주입보다 먼저 실행되므로
                    # account.additional_fields에서 직접 로드해야 함.
                    # (commit으로 expired된 ORM 직접 접근 금지 — 스냅샷 사용)
                    _acc_extras = _account_extras_snapshot

                    # 즉시할인 미적용 — sale_price 그대로 등록 (팀장 결정).
                    new_price = _raw_price
                    try:
                        _max_stock_per_opt = int(_acc_extras.get("stockQuantity") or 0)
                    except (ValueError, TypeError):
                        _max_stock_per_opt = 0

                    def _apply_stock_cap(raw: int, sold: bool) -> int:
                        """스마트스토어 _build_combination_options 패턴과 동일."""
                        if sold:
                            return 0
                        r = max(int(raw or 0), 0)
                        if _max_stock_per_opt > 0:
                            return min(r, _max_stock_per_opt)
                        return r

                    def _norm_opt(name: str) -> str:
                        """옵션명 정규화 — 공백/슬래시 차이 흡수."""
                        s = (name or "").strip()
                        s = re.sub(r"\s*/\s*", "/", s)
                        s = re.sub(r"\s+", " ", s)
                        return s

                    def _make_match_keys(name: str) -> set[str]:
                        """매칭 키 양방향 생성 — 전체 라벨 + 마지막 '/' 뒤 토큰.

                        회귀 배경(2026-05-11 939b89ed): _pick_lotteon_itm_label이
                        2단 옵션에서 ' / ' 결합 라벨('D/NAVY / 085')을 우선 후보로
                        쓰면서, 소싱처(무신사 등) new_options의 name='085'(사이즈만)와
                        매칭이 깨졌다. 양방향 키 등록·조회로 단일/결합 라벨 둘 다 흡수.
                        """
                        s = _norm_opt(name)
                        keys = {s}
                        if "/" in s:
                            keys.add(s.rsplit("/", 1)[-1].strip())
                        return keys

                    # 옵션명 → (stock, isSoldOut) 매핑 — 양방향 키 등록
                    opt_info_map: dict[str, tuple[int, bool]] = {}
                    for o in new_options:
                        info = (
                            o.get("stock", 0) or 0,
                            bool(o.get("isSoldOut", False)),
                        )
                        for k in _make_match_keys(o.get("name") or ""):
                            opt_info_map[k] = info

                    # 가격 변경 요청
                    # LOTTEON update_price 스펙: sitmNo + spdNo + slPrc 필수
                    # (trNo/trGrpCd/hstStrtDttm/hstEndDttm은 client 래퍼에서 자동 주입)
                    itm_prc_lst = []
                    itm_stk_lst = []
                    for itm in itm_lst:
                        itm_no = itm.get("itmNo") or itm.get("sitmNo")
                        if not itm_no:
                            continue
                        # 가격 업데이트
                        if new_price > 0:
                            itm_prc_lst.append(
                                {
                                    "sitmNo": str(itm.get("sitmNo") or itm_no),
                                    "spdNo": existing_no,
                                    "slPrc": new_price,
                                }
                            )
                        # 재고 업데이트 (양방향 키 매칭) — 라벨 후보 선택은 헬퍼 참조
                        itm_label = _pick_lotteon_itm_label(itm)
                        _itm_keys = _make_match_keys(itm_label)
                        _matched_key = next(
                            (k for k in _itm_keys if k in opt_info_map), None
                        )
                        if _matched_key:
                            raw_s, sold = opt_info_map[_matched_key]
                            stk = _apply_stock_cap(raw_s, sold)
                        else:
                            # 매칭 실패 시: 기존 min() 폴백은 품절 옵션이 섞이면 0이 되는
                            # 위험한 로직이었음. 이제는 명시적 0 + 경고 로그 (임의 재고 주입 금지).
                            logger.warning(
                                f"[롯데ON] 경량 업데이트 — 옵션명 매칭 실패: "
                                f"라벨='{itm_label}' keys={_itm_keys}, stkQty=0 강제"
                            )
                            stk = 0
                        itm_stk_lst.append(
                            {
                                "sitmNo": str(itm.get("sitmNo") or itm_no),
                                "spdNo": existing_no,
                                "trNo": client.tr_no,
                                "trGrpCd": client.tr_grp_cd or "SR",
                                "stkQty": max(0, int(stk)),
                            }
                        )

                    _updated = []
                    # 가격 + 재고 병렬 업데이트
                    # 회귀 방지(2026-04-30 092af2ead): return_exceptions=True 결과를
                    # 받지 않고 폐기하면 API 실패가 무음 성공으로 둔갑한다. 반드시
                    # 결과를 받아 예외 발생 시 위 try/except 폴백으로 전파해야 한다.
                    import asyncio as _asyncio

                    _tasks: list = []
                    _task_kinds: list[str] = []  # 결과 인덱싱용 ("price"/"stock")
                    if itm_prc_lst:
                        _tasks.append(client.update_price(itm_prc_lst))
                        _task_kinds.append("price")
                    if itm_stk_lst:
                        _tasks.append(client.update_stock(itm_stk_lst))
                        _task_kinds.append("stock")
                    if _tasks:
                        _results = await _asyncio.gather(
                            *_tasks, return_exceptions=True
                        )
                        _errs: list[tuple[str, Exception]] = [
                            (_task_kinds[i], r)
                            for i, r in enumerate(_results)
                            if isinstance(r, Exception)
                        ]
                        if _errs:
                            # 첫 예외를 위 try/except로 전파 → 전체 수정 폴백 진입
                            _kind, _exc = _errs[0]
                            logger.warning(
                                f"[롯데ON] 경량 업데이트 {_kind} API 실패: {_exc}"
                            )
                            raise _exc
                    if itm_prc_lst:
                        _updated.append(f"가격({new_price:,}원)")
                    if itm_stk_lst:
                        _updated.append(f"재고({len(itm_stk_lst)}건)")
                        # 재고 양수인데 SOUT_STK로 잠긴 옵션을 SALE로 자동 복구
                        await self._restore_sout_to_sale(
                            client, existing_no, itm_stk_lst
                        )

                    logger.info(
                        f"[롯데ON] 경량 업데이트 완료: {existing_no} — {', '.join(_updated)}"
                    )
                    return {
                        "success": True,
                        "product_no": existing_no,
                        "message": f"경량 업데이트: {', '.join(_updated)}",
                        "data": {"spdNo": existing_no},
                    }

            except Exception as e:
                logger.warning(
                    f"[롯데ON] 경량 업데이트 실패, 전체 수정으로 폴백: {existing_no} — {e}"
                )
                # 폴백: 아래 전체 로직으로 계속 진행

        # ── 성별 오버라이드: sex에 따라 남성/여성 카테고리 강제 변환 ──
        # sex='여성' → 여성스포츠의류, 그 외(남성/유니섹스/라이프 등) → 남성스포츠의류
        _sex_val = (product.get("sex") or "").strip()
        if _sex_val == "여성" and category_id:
            from backend.domain.samba.category.rules import _LOTTEON_M_TO_F

            if ">" in category_id:
                female_cat = _LOTTEON_M_TO_F.get(category_id)
                if female_cat:
                    logger.info(
                        f"[롯데ON] 성별 오버라이드: {category_id!r} → {female_cat!r}"
                    )
                    category_id = female_cat
                elif "남성스포츠의류" in category_id:
                    female_cat = category_id.replace("남성스포츠의류", "여성스포츠의류")
                    logger.info(
                        f"[롯데ON] 성별 보정(스포츠의류): {category_id!r} → {female_cat!r}"
                    )
                    category_id = female_cat
            elif category_id.startswith("BC4104"):
                female_bc = _BC_M_TO_F.get(category_id)
                if female_bc:
                    logger.info(f"[롯데ON] 성별 보정(BC): {category_id} → {female_bc}")
                    category_id = female_bc
                else:
                    candidate = "BC4110" + category_id[6:]
                    logger.info(
                        f"[롯데ON] 성별 보정(BC fallback): {category_id} → {candidate}"
                    )
                    category_id = candidate
        elif _sex_val != "여성" and category_id:
            # 여성이 아닌 경우 → 남성스포츠의류로 강제 변환
            if ">" in category_id and "여성스포츠의류" in category_id:
                male_cat = category_id.replace("여성스포츠의류", "남성스포츠의류")
                logger.info(
                    f"[롯데ON] 남성 강제 변환: {category_id!r} → {male_cat!r} (sex={_sex_val})"
                )
                category_id = male_cat
            elif category_id.startswith("BC4110"):
                candidate = "BC4104" + category_id[6:]
                logger.info(
                    f"[롯데ON] 남성 강제 변환(BC): {category_id} → {candidate} (sex={_sex_val})"
                )
                category_id = candidate

        # ── 거래처 미허용 카테고리 강제 대체 ─────────────────────────────────────────
        if category_id and category_id in _BLOCKED_PATHS:
            alt = _BLOCKED_PATHS[category_id]
            logger.info(f"[롯데ON] 미허용 카테고리 대체: {category_id!r} → {alt!r}")
            category_id = alt

        # ── 패딩/다운 키워드 기반 차단(거래처 FC08090202 등 미허용 대응) ─────────────
        # 스포츠의류 경로 마지막 세그먼트에 다운/패딩 키워드가 있으면 점퍼로 강제 변환
        if category_id and ">" in category_id and "스포츠의류" in category_id:
            _segs = [s.strip() for s in category_id.split(">")]
            _last = _segs[-1] if _segs else ""
            if any(kw in _last for kw in ("패딩", "다운")):
                if "여성스포츠의류" in category_id:
                    _alt = "스포츠의류/운동화 > 여성스포츠의류 > 점퍼"
                else:
                    _alt = "스포츠의류/운동화 > 남성스포츠의류 > 점퍼"
                logger.info(
                    f"[롯데ON] 패딩/다운 거래처미허용 회피: {category_id!r} → {_alt!r}"
                )
                category_id = _alt

        # ── FC05 권한없음 방지: 패션의류 경로/BC23코드 → 스포츠의류 강제 변환 ──────────
        if category_id and category_id in _FASHION_TO_SPORTS:
            mapped = _FASHION_TO_SPORTS[category_id]
            logger.info(f"[롯데ON] FC05→FC08 경로변환: {category_id!r} → {mapped!r}")
            category_id = mapped
        # category_id가 이미 BC코드로 변환된 경우: BC23xxx → BC41xxx 강제 변환
        if category_id and category_id in _BC23_TO_BC41:
            mapped = _BC23_TO_BC41[category_id]
            logger.info(f"[롯데ON] FC05→FC08 BC코드변환: {category_id} → {mapped}")
            category_id = mapped
        # 알 수 없는 BC23xxx: 성별 기반 기본값으로 폴백
        elif category_id and category_id.startswith("BC23"):
            sex_val = (product.get("sex") or "").strip()
            fallback = "BC41101000" if sex_val == "여성" else "BC41041000"  # 반팔티셔츠
            logger.info(
                f"[롯데ON] BC23 알 수 없는 코드→폴백: {category_id} → {fallback} (sex={sex_val})"
            )
            category_id = fallback

        # ── 소싱된 롯데ON 상품: _lotteonScatNo 원본 BC코드 직접 사용 ──────────
        _scat_no = str(product.get("_lotteonScatNo", "") or "").strip()
        if (
            _scat_no
            and _scat_no.startswith(_ALLOWED_BC_PREFIXES)
            and category_id
            and ">" in category_id
        ):
            logger.info(
                f"[롯데ON] 소싱 원본 BC코드 사용 (fuzzy match 스킵): {_scat_no}"
            )
            category_id = _scat_no
        elif (
            _scat_no
            and _scat_no.startswith("BC")
            and not _scat_no.startswith(_ALLOWED_BC_PREFIXES)
        ):
            logger.info(
                f"[롯데ON] 소싱 원본 BC코드 허용 범위 밖, 무시하고 매핑 사용: {_scat_no}"
            )

        # 트레이닝복 BC코드: 상품명 키워드로 집업/긴바지/반바지/상의로 세분화
        # BC41041400(남성 트레이닝복), BC41101500(여성 트레이닝복) → 키워드 기반 분류
        if category_id in ("BC41041400", "BC41101500"):
            _name_lower = (product.get("name") or "").lower()
            _sex_val = (product.get("sex") or "").strip()
            _is_female = _sex_val == "여성"
            _orig_cat = category_id
            if any(
                k in _name_lower
                for k in [
                    "재킷",
                    "jacket",
                    "집업",
                    "zip",
                    "트랙탑",
                    "트랩",
                    "track top",
                    "tracktop",
                    "track-top",
                    "tracktop",
                ]
            ):
                # 재킷/집업/트랙탑 → 집업 카테고리
                category_id = "BC41101400" if _is_female else "BC41041300"
            elif any(k in _name_lower for k in ["숏팬츠", "shorts", "반바지"]):
                # 반바지 (긴바지보다 먼저 체크)
                category_id = "BC41100900" if _is_female else "BC41040900"
            elif any(
                k in _name_lower
                for k in ["팬츠", "pants", "레깅스", "leggings", "슬랙스"]
            ):
                # 팬츠/레깅스 → 긴바지
                category_id = "BC41100100" if _is_female else "BC41040100"
            elif any(
                k in _name_lower
                for k in [
                    "맨투맨",
                    "sweatshirt",
                    "후드",
                    "hood",
                    "티셔츠",
                    "t-shirt",
                    "tshirt",
                    "top",
                ]
            ):
                # 상의 키워드 → 맨투맨 or 반팔티셔츠
                if any(k in _name_lower for k in ["후드", "hood"]):
                    category_id = "BC41101200" if _is_female else "BC41041200"  # 후드
                else:
                    category_id = "BC41101000" if _is_female else "BC41041000"  # 맨투맨
            if category_id != _orig_cat:
                logger.info(
                    f"[롯데ON] 트레이닝복→세분화: {_orig_cat} → {category_id} (name={product.get('name')})"
                )

        # category_id가 경로 문자열(">" 포함)이면 DB 코드맵에서 변환 시도
        if category_id and ">" in category_id:
            from backend.domain.samba.category.repository import (
                SambaCategoryMappingRepository,
                SambaCategoryTreeRepository,
            )
            from backend.domain.samba.category.service import SambaCategoryService

            _cat_svc = SambaCategoryService(
                SambaCategoryMappingRepository(session),
                SambaCategoryTreeRepository(session),
            )
            resolved = await _cat_svc.resolve_category_code("lotteon", category_id)
            if resolved:
                logger.info(
                    f"[롯데ON] 카테고리 코드 변환: '{category_id}' → {resolved}"
                )
                category_id = resolved
            else:
                return {
                    "success": False,
                    "message": (
                        f"롯데ON 카테고리 코드를 찾을 수 없습니다. "
                        f"카테고리 설정에서 '롯데ON 동기화'를 실행한 뒤 "
                        f"AI 자동 매핑을 다시 실행해주세요. "
                        f"(현재 값: {category_id})"
                    ),
                }

        logger.info(
            f"[롯데ON] 최종 카테고리 코드: {category_id} (상품: {product.get('name', '')[:30]})"
        )
        # 거래처 정보 자동 획득 (trGrpCd, trNo) — 캐싱으로 중복 호출 방지
        client = await _get_cached_client(api_key)

        product_copy = dict(product)

        # ── 1. 계정 additional_fields 주입 ──────────────────────────────
        # commit 이후 ORM account 직접 접근 금지 — 진입 시점 스냅샷 사용
        extras: dict[str, Any] = dict(_account_extras_snapshot)

        # 글로벌 설정을 항상 base로 읽고, 계정 설정으로 오버라이드
        # (owhpNo 유무와 무관하게 shippingType 등 발송 설정도 반영되어야 함)
        from backend.domain.samba.forbidden.model import SambaSettings
        from sqlmodel import select

        stmt = select(SambaSettings).where(SambaSettings.key == "store_lotteon")
        result = await session.execute(stmt)
        row = result.scalars().first()
        _lotteon_setting_val = row.value if row else None
        try:
            await session.commit()
        except Exception:
            pass
        if _lotteon_setting_val and isinstance(_lotteon_setting_val, dict):
            extras = {**_lotteon_setting_val, **extras}

        product_copy["owhp_no"] = extras.get("owhpNo", "")
        product_copy["dv_cst_pol_no"] = extras.get("dvCstPolNo", "")
        product_copy["island_dv_cst_pol_no"] = extras.get("dvIslandCstPolNo", "")
        product_copy["rtrp_no"] = extras.get("rtrpNo", "")
        # 기본값 "N" (합배송 불가) — 설정에서 명시적으로 "Y"를 선택한 경우에만 합배송 허용
        # 과거 "Y" 기본값 버그: DB에 bundleDelivery 키 없으면 의도치 않게 합배송 가능으로 등록됨
        product_copy["cmbn_dv_psb_yn"] = extras.get("bundleDelivery", "N")
        # 계정 추가 설정 주입
        if extras.get("asPhone"):
            # 설정의 A/S 전화번호를 그대로 사용 (브랜드명 불포함 — 다브랜드 운영)
            product_copy["_as_phone"] = extras["asPhone"]
        if extras.get("asMessage"):
            product_copy["_as_message"] = extras["asMessage"]
        # 스토어 즉시할인: UI 입력값 무시 — 팀장 결정으로 즉시할인 자체 사용 안 함.
        # (이전: 25% 하드코딩 + 12% 이벤트 할인 역산으로 결제가가 의도와 어긋남.
        #  복잡도 대비 효과 부족 + 끝자리 1원 단위 발생으로 단순화.)
        if extras.get("returnFee"):
            product_copy["_return_fee"] = int(extras["returnFee"])
        if extras.get("exchangeFee"):
            product_copy["_exchange_fee"] = int(extras["exchangeFee"])
        if extras.get("jejuFee"):
            product_copy["_jeju_fee"] = int(extras["jejuFee"])
        if extras.get("stockQuantity"):
            product_copy["_stock_quantity"] = int(extras["stockQuantity"])
        # 발송완료일 주입
        if extras.get("dispatchDays"):
            product_copy["_dispatch_days"] = int(extras["dispatchDays"])

        # ── 2. 정책 설정 주입 ────────────────────────────────────────────
        policy_id = product.get("applied_policy_id")
        if policy_id:
            from backend.domain.samba.policy.repository import SambaPolicyRepository

            policy_repo = SambaPolicyRepository(session)
            _policy = await policy_repo.get_async(policy_id)
            if _policy:
                mp = (_policy.market_policies or {}).get("롯데ON", {})
                pr = _policy.pricing or {}
                # 배송비
                shipping = int(mp.get("shippingCost") or pr.get("shippingCost") or 0)
                if shipping > 0:
                    product_copy["_delivery_fee_type"] = "PAID"
                    product_copy["_delivery_base_fee"] = shipping
                # 최대 재고
                if mp.get("maxStock"):
                    product_copy["_max_stock"] = int(mp["maxStock"])

        # ── 3. 브랜드 검색 (접미사 제거 → 첫 단어 → 스킵 폴백) ──────────
        brand_name = product_copy.get("brand", "")
        if brand_name and not product_copy.get("brand_no"):
            brd_no = await _search_brand_no(client, brand_name)
            if brd_no:
                product_copy["brand_no"] = brd_no

        # ── 비리프 카테고리 자동 보정 (leaf_yn="Y" 될 때까지 최대 4단계 반복 탐색) ──
        if category_id and category_id.endswith("0000"):
            logger.info(
                f"[롯데ON] 비리프 카테고리 감지 — 하위 탐색 시작: {category_id}"
            )
            for _step in range(4):
                try:
                    child_result = await client.get_categories(parent_id=category_id)
                    child_items = child_result.get("itemList") or []
                    logger.info(
                        f"[롯데ON] 하위 카테고리 조회 결과: {len(child_items)}개 (step={_step + 1})"
                    )
                    if not child_items:
                        logger.warning(
                            f"[롯데ON] 비리프 보정 중단 — 하위 없음 (parent={category_id})"
                        )
                        break
                    d = child_items[0].get("data", child_items[0])
                    child_id = (
                        d.get("std_cat_id", "")
                        or d.get("cat_id", "")
                        or d.get("id", "")
                    )
                    leaf_yn = d.get("leaf_yn", "")
                    if not child_id:
                        logger.warning(
                            f"[롯데ON] 비리프 보정 중단 — std_cat_id 없음. 키: {list(d.keys())[:10]}"
                        )
                        break
                    logger.info(
                        f"[롯데ON] 비리프 자동 보정: {category_id} → {child_id} (leaf_yn={leaf_yn})"
                    )
                    category_id = child_id
                    if leaf_yn == "Y":
                        break  # 최하위 도달
                    # leaf_yn이 "N"이거나 불분명하면 한 번 더 탐색
                except Exception as e:
                    logger.warning(f"[롯데ON] 하위 카테고리 조회 실패 (무시): {e}")
                    break

        # 전시카테고리(FC...) + attr_list 자동 조회 (10분 캐시)
        disp_cat_id = ""
        category_attr_ids: list[str] = []
        _attr_raw: list = []
        _cat_cache_key = category_id
        _cat_cached = _category_cache.get(_cat_cache_key)
        if _cat_cached and (time.time() - _cat_cached[0] < _CAT_TTL):
            _cached_data = _cat_cached[1]
            disp_cat_id = _cached_data["disp_cat_id"]
            category_attr_ids = _cached_data["category_attr_ids"]
            _attr_raw = _cached_data["_attr_raw"]
            # 폴백으로 BC 코드가 갈아끼워졌으면 캐시된 최종 BC도 복원
            _cached_bc = _cached_data.get("resolved_category_id")
            if _cached_bc and _cached_bc != category_id:
                logger.info(
                    f"[롯데ON] 캐시된 폴백 BC 적용: {category_id} → {_cached_bc}"
                )
                category_id = _cached_bc
            logger.info(
                f"[롯데ON] 카테고리 캐시 히트: {category_id} → disp={disp_cat_id}, attr_ids={len(category_attr_ids)}개"
            )
        else:
            try:
                cat_result = await client.get_categories(cat_id=category_id)
                items = cat_result.get("itemList") or []
                if items:
                    d = items[0].get("data", {})
                    disp_list = d.get("disp_list", [])
                    if disp_list:
                        disp_cat_id = disp_list[0].get("disp_cat_id", "")
                    _attr_raw = d.get("attr_list") or []
                    category_attr_ids = [
                        str(a.get("attr_id", "")) for a in _attr_raw if a.get("attr_id")
                    ]
                    logger.info(
                        f"[롯데ON] attr_list 상세: "
                        f"{[(str(a.get('attr_id', '')), a.get('attr_nm', '')) for a in _attr_raw]}"
                    )
                    if _attr_raw:
                        logger.info(
                            f"[롯데ON] attr_list[0] 원시키: {list(_attr_raw[0].keys())}"
                        )
                        logger.info(
                            f"[롯데ON] attr_list pi_type: "
                            f"{[(str(a.get('attr_id', '')), a.get('attr_pi_type', '')) for a in _attr_raw]}"
                        )
                logger.info(
                    f"[롯데ON] 전시카테고리 조회: {category_id} → {disp_cat_id}, attr_ids={len(category_attr_ids)}개"
                )

                # ── 거래처 미허용 disp_cat_id 폴백 ──
                # FC08030602/FC08090202 등 거래처가 못 쓰는 전시카테고리로 해석된 경우
                # 집업 카테고리로 BC 코드를 갈아끼우고 disp/attr 재조회.
                if disp_cat_id and disp_cat_id in _BLOCKED_DISP_CAT_IDS:
                    _sex_for_fb = (product.get("sex") or "").strip()
                    _fb_bc = _FALLBACK_BC_FOR_BLOCKED_DISP.get(
                        _sex_for_fb, _FALLBACK_BC_FOR_BLOCKED_DISP["남성"]
                    )
                    logger.info(
                        f"[롯데ON] 거래처 미허용 FC 감지 — 집업 폴백: "
                        f"{category_id}({disp_cat_id}) → {_fb_bc}"
                    )
                    category_id = _fb_bc
                    disp_cat_id = ""
                    category_attr_ids = []
                    _attr_raw = []
                    try:
                        cat_result2 = await client.get_categories(cat_id=category_id)
                        items2 = cat_result2.get("itemList") or []
                        if items2:
                            d2 = items2[0].get("data", {})
                            disp_list2 = d2.get("disp_list", [])
                            if disp_list2:
                                disp_cat_id = disp_list2[0].get("disp_cat_id", "")
                            _attr_raw = d2.get("attr_list") or []
                            category_attr_ids = [
                                str(a.get("attr_id", ""))
                                for a in _attr_raw
                                if a.get("attr_id")
                            ]
                        logger.info(
                            f"[롯데ON] 폴백 재조회: {category_id} → {disp_cat_id}, "
                            f"attr_ids={len(category_attr_ids)}개"
                        )
                    except Exception as _e:
                        logger.warning(f"[롯데ON] 폴백 카테고리 재조회 실패: {_e}")

                _category_cache[_cat_cache_key] = (
                    time.time(),
                    {
                        "disp_cat_id": disp_cat_id,
                        "category_attr_ids": category_attr_ids,
                        "_attr_raw": _attr_raw,
                        "resolved_category_id": category_id,
                    },
                )
                # 속성값 목록 상세 조회 (디버그 로깅 전용 — 캐시 미적용)
                for _scat_key in [category_id, disp_cat_id]:
                    if not _scat_key:
                        continue
                    try:
                        _attr_detail = await client.get_category_attributes(
                            scat_no=_scat_key
                        )
                        logger.info(
                            f"[롯데ON] cheetahScatAttr({_scat_key}) 응답: {_attr_detail}"
                        )
                        break
                    except Exception as _e:
                        logger.debug(
                            f"[롯데ON] cheetahScatAttr({_scat_key}) 조회 실패: {_e}"
                        )
                try:
                    _attr_detail2 = await client.get_category_attribute_list(
                        category_id=category_id
                    )
                    logger.info(
                        f"[롯데ON] openapi attr_list({category_id}) 응답: {_attr_detail2}"
                    )
                except Exception as _e:
                    logger.debug(f"[롯데ON] openapi attr_list 조회 실패: {_e}")
            except Exception as e:
                logger.warning(f"[롯데ON] 전시카테고리 조회 실패 (무시): {e}")

        # 속성정보(scatAttrLst) 생성 — 무신사 소싱 데이터 → 롯데ON 속성값 매핑
        if category_attr_ids:
            # 소스 필드 디버그 (성별/계절 오매핑 원인 파악용)
            logger.info(
                f"[롯데ON][속성소스] sex={product_copy.get('sex')!r} "
                f"season={product_copy.get('season')!r} "
                f"material={product_copy.get('material')!r} "
                f"color={product_copy.get('color')!r} "
                f"category1={product_copy.get('category1')!r} "
                f"name={product_copy.get('name', '')[:40]!r}"
            )
            scat_attr_lst = _build_scat_attr_lst(
                product_copy, category_attr_ids, attr_raw=_attr_raw
            )
            product_copy["_scat_attr_lst"] = scat_attr_lst
            logger.info(
                f"[롯데ON] scatAttrLst 생성: {len(scat_attr_lst)}개 — {[a['optVal'] for a in scat_attr_lst]}"
            )

        logger.info(
            f"[롯데ON] 발송 설정 진단 — "
            f"_shipping_type={product_copy.get('_shipping_type')!r} "
            f"_dispatch_days={product_copy.get('_dispatch_days')!r} "
            f"_order_cutoff_hour={product_copy.get('_order_cutoff_hour')!r} "
            f"extras.shippingType={extras.get('shippingType')!r}"
        )

        # ── 차단 CDN 사전 R2 미러링 (HEAD 검증 0장 회피) ──
        # GS샵(asset.m-gs.kr / static.m-gs.kr), 무신사(msscdn.net),
        # 롯데온(contents.lotteon.com) 등 핫링크 차단 도메인은 일반 httpx HEAD에
        # 403/거절을 내려 filter_alive_urls가 전부 드롭 → "유효한 이미지 0장" 가드 발동.
        # 롯데홈쇼핑(lottehome.py)과 동일하게 R2로 선미러해 R2 URL로 교체한다.
        # _HOTLINK_BLOCKED_HOSTS에 등록된 도메인만 미러 — 다른 소싱처는 원본 유지.
        try:
            from backend.domain.samba.image.service import ImageTransformService

            _img_svc = ImageTransformService()
            if product_copy.get("images"):
                _mirrored, _ = await _img_svc.mirror_external_to_r2(
                    product_copy["images"]
                )
                product_copy["images"] = _mirrored
        except Exception as e:
            logger.warning(f"[롯데ON] 차단 CDN R2 미러링 실패 — 원본 유지: {e}")

        # ── 외부 이미지 URL 사전 검증 (9999 회피) ──
        # 죽은 URL 또는 거부 확장자가 origImgFileNm에 들어가면 롯데ON이
        # "URL 형식이 올바르지 않습니다(9999)"로 응답한다.
        # image_validator로 HEAD 사전 검증 — R2 등 외부 인프라 의존 없음.
        from backend.domain.samba.image.image_validator import filter_alive_urls

        if product_copy.get("images"):
            _orig_imgs = product_copy["images"]
            _alive_imgs = await filter_alive_urls(_orig_imgs)
            # 경로에 괄호/공백/한글 등이 들어있으면 롯데ON URL 형식 검증에서
            # "URL 형식이 올바르지 않습니다(9999)"로 거부됨 (예: yswholesale
            # `..._1000px(1).jpg`). HEAD 200이라도 등록 API는 별도로 strict 파싱하므로
            # 경로 부분만 퍼센트 인코딩 (이미 인코딩된 %XX 시퀀스는 보존).
            from urllib.parse import quote, urlsplit, urlunsplit

            def _normalize_for_lotteon(u: str) -> str:
                if not u or not u.startswith(("http://", "https://")):
                    return u
                try:
                    p = urlsplit(u)
                    return urlunsplit(
                        (
                            p.scheme,
                            p.netloc,
                            quote(p.path, safe="/%-._~"),
                            p.query,
                            p.fragment,
                        )
                    )
                except Exception:
                    return u

            product_copy["images"] = [_normalize_for_lotteon(u) for u in _alive_imgs]
            # transform_product 단계의 확장자 필터와 동일 기준 적용 — 확장자 없는
            # CDN URL이 가드를 통과한 뒤 transform에서 0장으로 떨어져 9999가 나는
            # 사고 방지(GS샵 등). 동일 정규식을 미리 통과시켜 가드에서 잡는다.
            import re as _re_lot

            _LOT_IMG_EXT_RE = _re_lot.compile(
                r"\.(jpe?g|png|gif|webp|bmp)", _re_lot.IGNORECASE
            )
            _before_ext = len(product_copy["images"])
            product_copy["images"] = [
                u for u in product_copy["images"] if _LOT_IMG_EXT_RE.search(u)
            ][:10]
            _after_ext = len(product_copy["images"])
            _kept_count = _after_ext
            _excluded = len(_orig_imgs) - _kept_count
            _changed = sum(
                1 for o, n in zip(_alive_imgs, product_copy["images"]) if o != n
            )
            logger.info(
                f"[롯데ON] 이미지 사전검증: 원본 {len(_orig_imgs)}장 → "
                f"통과 {_kept_count}장 (제외 {_excluded}장, URL 정규화 {_changed}장, "
                f"확장자 필터 제외 {_before_ext - _after_ext}장)"
            )

        # ── 이미지 0장 사전 차단 가드 ──────────────────────────────────
        # itmImgLst 빈 배열로 호출하면 롯데ON이 9999/itmImgLst 입력 필수 에러를 낸다.
        # 사전검증 후 0장이면 여기서 차단해 더 명확한 메시지로 실패시킨다.
        if not (product_copy.get("images") or []):
            return {
                "success": False,
                "message": (
                    "롯데ON 등록 실패: 유효한 이미지 0장 "
                    "(원본 URL 만료/거부 가능성 — 재수집 또는 R2 업로드 후 재시도 필요)"
                ),
            }

        data = LotteonClient.transform_product(
            product_copy,
            category_id,
            client.tr_grp_cd or "SR",
            client.tr_no,
            disp_cat_id,
        )

        # ── 4. 등록 / 수정 ───────────────────────────────────────────────
        try:
            if existing_no:
                # ── 기존 단품 eitmNo 조회 (수정 시 중복 방지) ───────────────
                existing_eitm_nos: list[str] = []
                existing_sitm_nos: list[
                    str
                ] = []  # 통합EC판매자단품번호 — 살수록할인 API에서 사용
                try:
                    prod_resp = await client.get_product(existing_no)
                    inner = prod_resp.get("data", prod_resp)
                    if isinstance(inner, dict):
                        spd_info = inner.get("spdLst") or inner.get("spdInfo") or inner
                        if isinstance(spd_info, list) and spd_info:
                            spd_info = spd_info[0]
                        if isinstance(spd_info, dict):
                            itm_lst_raw = spd_info.get("itmLst") or []
                            existing_eitm_nos = [
                                str(itm.get("eitmNo"))
                                for itm in itm_lst_raw
                                if itm.get("eitmNo")
                            ]
                            # sitmNo = 롯데ON 내부 단품번호 (예: LO2643843825_2643843826)
                            existing_sitm_nos = [
                                str(itm.get("sitmNo"))
                                for itm in itm_lst_raw
                                if itm.get("sitmNo")
                            ]
                    logger.info(
                        f"[롯데ON] 기존 단품 eitmNo: {existing_eitm_nos}, sitmNo: {existing_sitm_nos}"
                    )
                except Exception as e:
                    logger.warning(f"[롯데ON] 기존 단품 조회 실패 (무시): {e}")

                # spdNo + selPrdNo 모두 주입 (롯데ON 수정 API 필수값)
                if data.get("spdLst") and isinstance(data["spdLst"], list):
                    data["spdLst"][0]["spdNo"] = existing_no
                    data["spdLst"][0]["selPrdNo"] = existing_no
                    # 수정 API는 itmLst를 "새 단품 추가"로 처리 → 기존 옵션과 중복 에러 발생
                    # 상품 헤더만 업데이트하고 itmLst는 제거 (재고는 수정 후 update_stock으로 별도 반영)
                    _saved_itm_lst = data["spdLst"][0].get("itmLst", [])
                    data["spdLst"][0].pop("itmLst", None)
                    data["spdLst"][0].pop("sitmYn", None)
                _spd0 = data["spdLst"][0] if data.get("spdLst") else {}
                logger.info(
                    f"[롯데ON] 수정 모드 — 기존 spdNo={existing_no!r} "
                    f"dvRsvDvsCd={_spd0.get('dvRsvDvsCd')!r} "
                    f"sndBgtNday={_spd0.get('sndBgtNday')!r} "
                    f"ordCutHh={_spd0.get('ordCutHh')!r}"
                )
                # impDvsCd fallback: 직수입 불허 카테고리 대응 (등록과 동일 전략)
                _upd_imp_fallbacks = [
                    ("NONE", "DMST"),  # 1. 해당없음 + 국내
                    ("NONE", "OVRS"),  # 2. 해당없음 + 해외
                    ("PRL_IMP", "DMST"),  # 3. 병행수입 + 국내
                    (None, "DMST"),  # 4. impDvsCd 제거 + 국내
                    ("_REMOVE_BOTH_", ""),  # 5. 둘 다 제거
                ]
                _upd_exception: Exception | None = None
                api_result = None
                try:
                    api_result = await client.update_product(data)
                except Exception as _ue:
                    if "수입구분코드" in str(_ue):
                        _upd_exception = _ue
                        import copy as _copy_upd

                        _orig_pd_itms_u = (
                            _copy_upd.deepcopy(data["spdLst"][0].get("pdItmsInfo"))
                            if data.get("spdLst")
                            else None
                        )
                        for _imp_code, _dmst_code in _upd_imp_fallbacks:
                            # 매 시도마다 pdItmsInfo 원본 복원
                            if _orig_pd_itms_u is not None and data.get("spdLst"):
                                data["spdLst"][0]["pdItmsInfo"] = _copy_upd.deepcopy(
                                    _orig_pd_itms_u
                                )
                            if data.get("spdLst") and isinstance(data["spdLst"], list):
                                _spd = data["spdLst"][0]
                                if _imp_code == "_REMOVE_BOTH_":
                                    _spd.pop("impDvsCd", None)
                                    _spd.pop("dmstOvsDvDvsCd", None)
                                elif _imp_code is None:
                                    _spd.pop("impDvsCd", None)
                                else:
                                    _spd["impDvsCd"] = _imp_code
                                if _imp_code != "_REMOVE_BOTH_":
                                    _spd["dmstOvsDvDvsCd"] = _dmst_code
                            logger.info(
                                f"[롯데ON] 수정 impDvsCd fallback: impDvsCd={_imp_code!r} dmst={_dmst_code}"
                            )
                            try:
                                api_result = await client.update_product(data)
                                _upd_exception = None
                                break
                            except Exception as _ue2:
                                # 항목코드/품목항목코드 에러 처리
                                if "항목코드" in str(_ue2):
                                    _upd_exception = _ue2
                                    _artl_err = _ue2
                                    _artl_resolved = False
                                    for _artl_try in range(10):
                                        import re as _re_a

                                        _am = _re_a.search(
                                            r"항목코드\D{0,3}(\d{3,4})", str(_artl_err)
                                        )
                                        _bad = _am.group(1).zfill(4) if _am else None
                                        if not _bad:
                                            break
                                        logger.info(
                                            f"[롯데ON] 수정 impDvsCd fallback 중 항목코드 제거 ({_artl_try + 1}회) — 코드={_bad}"
                                        )
                                        _spd3 = data["spdLst"][0]
                                        _ntc3 = _spd3.get("pdItmsInfo")
                                        if isinstance(_ntc3, dict):
                                            _al3 = _ntc3.get("pdItmsArtlLst", [])
                                            _ntc3["pdItmsArtlLst"] = [
                                                a
                                                for a in _al3
                                                if str(a.get("pdArtlCd", "")).zfill(4)
                                                != _bad
                                            ]
                                        try:
                                            api_result = await client.update_product(
                                                data
                                            )
                                            _upd_exception = None
                                            _artl_resolved = True
                                            break
                                        except Exception as _ea:
                                            _upd_exception = _ea
                                            if "항목코드" in str(_ea):
                                                _artl_err = _ea
                                                continue
                                            else:
                                                break
                                    # pdItmsCd=38(기타재화)로 교체 시도
                                    if not _artl_resolved and _upd_exception:
                                        _spd_f = data["spdLst"][0]
                                        _pdi = _spd_f.get("pdItmsInfo")
                                        if (
                                            isinstance(_pdi, dict)
                                            and _pdi.get("pdItmsCd") != "38"
                                        ):
                                            logger.info(
                                                f"[롯데ON] 수정 pdItmsCd={_pdi.get('pdItmsCd')} 실패 → 38(기타재화)로 교체"
                                            )
                                            _pdi["pdItmsCd"] = "38"
                                            # 롯데ON 공식 품목고시 PDF 기준 기타재화 항목코드
                                            _pdi["pdItmsArtlLst"] = [
                                                {
                                                    "pdArtlCd": "0210",
                                                    "pdArtlCnts": "상세페이지 참조",
                                                },
                                                {
                                                    "pdArtlCd": "1400",
                                                    "pdArtlCnts": "상세페이지 참조",
                                                },
                                                {
                                                    "pdArtlCd": "1420",
                                                    "pdArtlCnts": "상세페이지 참조",
                                                },
                                                {
                                                    "pdArtlCd": "0070",
                                                    "pdArtlCnts": "상세페이지 참조",
                                                },
                                                {
                                                    "pdArtlCd": "1440",
                                                    "pdArtlCnts": "상세페이지 참조",
                                                },
                                                {
                                                    "pdArtlCd": "0200",
                                                    "pdArtlCnts": "상세페이지 참조",
                                                },
                                            ]
                                            # pdItmsCd=38 교체 후 항목코드 제거 루프
                                            for _artl38_try in range(15):
                                                try:
                                                    api_result = (
                                                        await client.update_product(
                                                            data
                                                        )
                                                    )
                                                    _upd_exception = None
                                                    _artl_resolved = True
                                                    break
                                                except Exception as _ea2:
                                                    _upd_exception = _ea2
                                                    if "항목코드" in str(_ea2):
                                                        import re as _re38

                                                        _m38 = _re38.search(
                                                            r"항목코드\D{0,3}(\d{3,4})",
                                                            str(_ea2),
                                                        )
                                                        _bad38 = (
                                                            _m38.group(1).zfill(4)
                                                            if _m38
                                                            else None
                                                        )
                                                        if _bad38:
                                                            _artl_lst = _pdi.get(
                                                                "pdItmsArtlLst", []
                                                            )
                                                            _artl_lst[:] = [
                                                                a
                                                                for a in _artl_lst
                                                                if str(
                                                                    a.get(
                                                                        "pdArtlCd", ""
                                                                    )
                                                                ).zfill(4)
                                                                != _bad38
                                                            ]
                                                            logger.info(
                                                                f"[롯데ON] 수정 pdItmsCd=38 항목코드({_bad38}) 제거 → 남은: {[a.get('pdArtlCd') for a in _artl_lst]}"
                                                            )
                                                            if not _artl_lst:
                                                                break
                                                            continue
                                                        else:
                                                            break
                                                    else:
                                                        break
                                    if _artl_resolved:
                                        break
                                    if _upd_exception and "항목코드" not in str(
                                        _upd_exception
                                    ):
                                        continue
                                logger.warning(
                                    f"[롯데ON] 수정 fallback 실패: impDvsCd={_imp_code!r} dmst={_dmst_code} — {_ue2}"
                                )
                    elif "항목코드" in str(_ue):
                        # 항목코드 에러 (수입구분코드 문제 없음): 반복 제거 루프
                        _artl_err = _ue
                        for _artl_try in range(10):
                            import re as _re_a

                            _am = _re_a.search(r"항목코드\((\d+)\)", str(_artl_err))
                            _bad_code2 = _am.group(1) if _am else None
                            if not _bad_code2:
                                break
                            logger.info(
                                f"[롯데ON] 수정 항목코드 fallback ({_artl_try + 1}회) — 코드={_bad_code2}"
                            )
                            if data.get("spdLst") and isinstance(data["spdLst"], list):
                                _spd = data["spdLst"][0]
                                _ntc = _spd.get("pdItmsInfo")
                                if isinstance(_ntc, dict):
                                    _artl_lst = _ntc.get("pdItmsArtlLst", [])
                                    _ntc["pdItmsArtlLst"] = [
                                        a
                                        for a in _artl_lst
                                        if a.get("pdArtlCd") != _bad_code2
                                    ]
                            try:
                                api_result = await client.update_product(data)
                                _upd_exception = None
                                break
                            except Exception as _ue3:
                                if "항목코드" in str(_ue3):
                                    _artl_err = _ue3
                                    _upd_exception = _ue3
                                    continue
                                else:
                                    logger.warning(
                                        f"[롯데ON] 수정 항목코드 fallback 실패: {_ue3}"
                                    )
                                    _upd_exception = _ue3
                                    break
                        if _upd_exception and "항목코드" in str(_upd_exception):
                            logger.info(
                                "[롯데ON] 수정 항목코드 전부 실패 → pdItmsInfo 제거 후 재시도"
                            )
                            data["spdLst"][0].pop("pdItmsInfo", None)
                            try:
                                api_result = await client.update_product(data)
                                _upd_exception = None
                            except Exception as _ea_final:
                                logger.warning(
                                    f"[롯데ON] 수정 pdItmsInfo 제거 후에도 실패: {_ea_final}"
                                )
                                _upd_exception = _ea_final
                    else:
                        raise
                if _upd_exception is not None:
                    raise _upd_exception
                # 수정 API가 새 spdNo를 반환하는 경우 (수정본 별도 상품번호 발급)
                new_spd_no = api_result.get("spdNo", "") or ""
                effective_no = (
                    new_spd_no
                    if new_spd_no and new_spd_no != existing_no
                    else existing_no
                )
                if new_spd_no and new_spd_no != existing_no:
                    logger.info(
                        f"[롯데ON] 수정 후 새 spdNo 발급: {existing_no} → {new_spd_no}"
                    )
                # ── 수정 후 재고 동기화 (수정 API는 itmLst 무시하므로 별도 호출) ──
                if _saved_itm_lst and itm_lst_raw:
                    try:
                        # transform_product 결과의 옵션값 → 재고 매핑
                        _new_stk_map: dict[str, tuple[int, str]] = {}
                        for _new_itm in _saved_itm_lst:
                            _new_opts = _new_itm.get("itmOptLst") or []
                            if _new_opts:
                                _opt_val = _new_opts[0].get("optVal", "").strip()
                                _new_stk_map[_opt_val] = (
                                    _new_itm.get("stkQty", 0),
                                    _new_itm.get("dpYn", "Y"),
                                )

                        _itm_stk_lst = []
                        for _old_itm in itm_lst_raw:
                            _sitm_no = _old_itm.get("sitmNo") or _old_itm.get("itmNo")
                            if not _sitm_no:
                                continue
                            _old_opts = _old_itm.get("itmOptLst") or []
                            if _old_opts:
                                _opt_val = _old_opts[0].get("optVal", "").strip()
                                _stk_dp = _new_stk_map.get(_opt_val)
                                _stk = (
                                    _stk_dp[0] if _stk_dp else _old_itm.get("stkQty", 0)
                                )
                            else:
                                _stk = 0
                            _itm_stk_lst.append(
                                {
                                    "sitmNo": str(_sitm_no),
                                    "spdNo": effective_no,
                                    "trNo": client.tr_no,
                                    "trGrpCd": client.tr_grp_cd or "SR",
                                    "stkQty": max(0, int(_stk)),
                                }
                            )

                        if _itm_stk_lst:
                            await client.update_stock(_itm_stk_lst)
                            logger.info(
                                f"[롯데ON] 수정 후 재고 동기화 완료: {effective_no} — "
                                f"{len(_itm_stk_lst)}건"
                            )
                            # 재고 양수인데 SOUT_STK로 잠긴 옵션을 SALE로 자동 복구
                            await self._restore_sout_to_sale(
                                client, effective_no, _itm_stk_lst
                            )
                    except Exception as _stk_e:
                        logger.warning(
                            f"[롯데ON] 수정 후 재고 동기화 실패 (무시): {_stk_e}"
                        )

                # ── 수정 후 가격 동기화 ───────────────────────────────────
                # update_product는 spd 헤더만 반영하고 itm 가격(slPrc)은 무시한다.
                # 별도 update_price 호출 없이는 정상가/판매가 변경이 셀러 페이지에
                # 반영되지 않아 sale_price와 실제 노출가가 어긋나는 사고가 발생.
                # 경량 분기(line ~1217)는 이미 update_price를 호출하지만 일반 수정
                # 경로에는 빠져있어 같은 패턴으로 보강 (collector_autotune.py:828-830
                # 의 동일 사고 주석 참조).
                if _saved_itm_lst:
                    try:
                        # 새 spdNo가 발급된 경우 itm_lst_raw의 sitmNo는 old spd
                        # 소속이라 update_price가 no-op이 되므로 effective_no로
                        # 재조회 (codex P2 지적).
                        _itm_for_price = itm_lst_raw
                        if new_spd_no and new_spd_no != existing_no:
                            try:
                                _new_prod = await client.get_product(effective_no)
                                _new_inner = _new_prod.get("data", _new_prod)
                                _new_spd = (
                                    _new_inner.get("spdLst")
                                    or _new_inner.get("spdInfo")
                                    or _new_inner
                                )
                                if isinstance(_new_spd, list) and _new_spd:
                                    _new_spd = _new_spd[0]
                                if isinstance(_new_spd, dict):
                                    _itm_for_price = (
                                        _new_spd.get("itmLst") or itm_lst_raw
                                    )
                            except Exception as _refetch_e:
                                logger.warning(
                                    f"[롯데ON] 새 spdNo itm 재조회 실패, 기존 itm으로 시도: {_refetch_e}"
                                )

                        _itm_prc_lst = _build_lotteon_price_payload(
                            _saved_itm_lst, _itm_for_price, effective_no
                        )
                        if _itm_prc_lst:
                            await client.update_price(_itm_prc_lst)
                            logger.info(
                                f"[롯데ON] 수정 후 가격 동기화 완료: {effective_no} — "
                                f"{len(_itm_prc_lst)}건 (slPrc={_itm_prc_lst[0].get('slPrc'):,})"
                            )
                    except Exception as _prc_e:
                        logger.warning(
                            f"[롯데ON] 수정 후 가격 동기화 실패 (무시): {_prc_e}"
                        )

                # ── 수정 후 프로모션 재설정 ──────────────────────────────
                await self._apply_promotions(
                    client,
                    effective_no,
                    extras,
                    is_update=True,
                    eitm_nos=existing_sitm_nos,
                )
                # ── 홍보문구 갱신 (180일 자동 연장) ────────────────────
                publicity_phrase = (
                    extras.get("promotionMessage", "")
                    or extras.get("publicityPhrase", "").strip()
                )
                if publicity_phrase:
                    try:
                        await client.register_publicity_sentence(
                            effective_no, publicity_phrase
                        )
                    except Exception as e:
                        logger.warning(f"[롯데ON] 홍보문구 갱신 실패 (무시): {e}")
                ret: dict[str, Any] = {
                    "success": True,
                    "message": "롯데ON 수정 성공",
                    "data": api_result,
                }
                if effective_no != existing_no:
                    # service.py가 market_product_nos를 새 번호로 갱신하도록 반환
                    ret["spdNo"] = effective_no
                return ret
            else:
                # impDvsCd + dmstOvsDvDvsCd fallback 전략:
                # (impDvsCd, dmstOvsDvDvsCd) 조합을 순차 시도
                # - DRC_IMP+OVRS: 해외브랜드(아디다스 등) 직수입 → dmstOvsDvDvsCd를 OVRS로 변경
                # - None: impDvsCd 필드 제거 (카테고리 기본값 사용)
                # 이미 유효하지 않은 코드: NATN_MFR, DOM_MFR, IND_IMP (롯데ON이 인식 못함)
                # impDvsCd + dmstOvsDvDvsCd fallback 전략 (6단계):
                # None = 필드 제거(pop), "" = 빈문자열, "_REMOVE_BOTH_" = 둘 다 제거
                _imp_dvs_fallbacks = [
                    ("NONE", "DMST"),  # 1. 해당없음 + 국내
                    ("NONE", "OVRS"),  # 2. 해당없음 + 해외
                    ("PRL_IMP", "DMST"),  # 3. 병행수입 + 국내
                    (None, "DMST"),  # 4. impDvsCd 제거 + 국내
                    ("_REMOVE_BOTH_", ""),  # 5. 둘 다 제거
                ]
                _reg_exception: Exception | None = None
                api_result = None
                try:
                    api_result = await client.register_product(data)
                except Exception as _e:
                    if "수입구분코드" in str(_e):
                        _reg_exception = _e
                        import copy as _copy_imp

                        _orig_pd_itms = (
                            _copy_imp.deepcopy(data["spdLst"][0].get("pdItmsInfo"))
                            if data.get("spdLst")
                            else None
                        )
                        for _imp_code, _dmst_code in _imp_dvs_fallbacks:
                            # 매 시도마다 pdItmsInfo 원본 복원
                            if _orig_pd_itms is not None and data.get("spdLst"):
                                data["spdLst"][0]["pdItmsInfo"] = _copy_imp.deepcopy(
                                    _orig_pd_itms
                                )
                            if data.get("spdLst") and isinstance(data["spdLst"], list):
                                _spd = data["spdLst"][0]
                                if _imp_code == "_REMOVE_BOTH_":
                                    _spd.pop("impDvsCd", None)
                                    _spd.pop("dmstOvsDvDvsCd", None)
                                elif _imp_code is None:
                                    _spd.pop("impDvsCd", None)
                                else:
                                    _spd["impDvsCd"] = _imp_code
                                if _imp_code != "_REMOVE_BOTH_":
                                    _spd["dmstOvsDvDvsCd"] = _dmst_code
                            logger.info(
                                f"[롯데ON] impDvsCd fallback: impDvsCd={_imp_code!r} dmst={_dmst_code} (원인: {_e})"
                            )
                            try:
                                api_result = await client.register_product(data)
                                _reg_exception = None
                                break
                            except Exception as _e2:
                                # 항목코드/품목항목코드 에러 처리
                                if "항목코드" in str(_e2):
                                    _reg_exception = _e2  # 실제 에러로 갱신
                                    _artl_err = _e2
                                    _artl_resolved = False
                                    # 개별 항목코드 제거 루프
                                    for _artl_try in range(10):
                                        import re as _re_a

                                        _am = _re_a.search(
                                            r"항목코드\((\d+)\)", str(_artl_err)
                                        )
                                        _bad = _am.group(1) if _am else None
                                        if not _bad:
                                            break
                                        logger.info(
                                            f"[롯데ON] impDvsCd fallback 중 항목코드 제거 ({_artl_try + 1}회) — 코드={_bad}"
                                        )
                                        _spd2 = data["spdLst"][0]
                                        _ntc2 = _spd2.get("pdItmsInfo")
                                        if isinstance(_ntc2, dict):
                                            _al = _ntc2.get("pdItmsArtlLst", [])
                                            _ntc2["pdItmsArtlLst"] = [
                                                a
                                                for a in _al
                                                if a.get("pdArtlCd") != _bad
                                            ]
                                        try:
                                            api_result = await client.register_product(
                                                data
                                            )
                                            _reg_exception = None
                                            _artl_resolved = True
                                            break
                                        except Exception as _ea:
                                            _reg_exception = _ea
                                            if "항목코드" in str(_ea):
                                                _artl_err = _ea
                                                continue
                                            else:
                                                break
                                    # 개별 제거 실패 → pdItmsCd=38(기타재화)로 교체 시도
                                    if not _artl_resolved and _reg_exception:
                                        _spd_f = data["spdLst"][0]
                                        _pdi = _spd_f.get("pdItmsInfo")
                                        if (
                                            isinstance(_pdi, dict)
                                            and _pdi.get("pdItmsCd") != "38"
                                        ):
                                            logger.info(
                                                f"[롯데ON] pdItmsCd={_pdi.get('pdItmsCd')} 실패 → 38(기타재화)로 교체"
                                            )
                                            _pdi["pdItmsCd"] = "38"
                                            # 롯데ON 공식 품목고시 PDF 기준 기타재화 항목코드
                                            _pdi["pdItmsArtlLst"] = [
                                                {
                                                    "pdArtlCd": "0210",
                                                    "pdArtlCnts": "상세페이지 참조",
                                                },
                                                {
                                                    "pdArtlCd": "1400",
                                                    "pdArtlCnts": "상세페이지 참조",
                                                },
                                                {
                                                    "pdArtlCd": "1420",
                                                    "pdArtlCnts": "상세페이지 참조",
                                                },
                                                {
                                                    "pdArtlCd": "0070",
                                                    "pdArtlCnts": "상세페이지 참조",
                                                },
                                                {
                                                    "pdArtlCd": "1440",
                                                    "pdArtlCnts": "상세페이지 참조",
                                                },
                                                {
                                                    "pdArtlCd": "0200",
                                                    "pdArtlCnts": "상세페이지 참조",
                                                },
                                            ]
                                            # pdItmsCd=38 교체 후 항목코드 제거 루프
                                            for _artl38_try in range(15):
                                                try:
                                                    api_result = (
                                                        await client.register_product(
                                                            data
                                                        )
                                                    )
                                                    _reg_exception = None
                                                    _artl_resolved = True
                                                    break
                                                except Exception as _ea2:
                                                    _reg_exception = _ea2
                                                    if "항목코드" in str(_ea2):
                                                        import re as _re38

                                                        _m38 = _re38.search(
                                                            r"항목코드\D{0,3}(\d{3,4})",
                                                            str(_ea2),
                                                        )
                                                        _bad38 = (
                                                            _m38.group(1).zfill(4)
                                                            if _m38
                                                            else None
                                                        )
                                                        if _bad38:
                                                            _artl_lst = _pdi.get(
                                                                "pdItmsArtlLst", []
                                                            )
                                                            _artl_lst[:] = [
                                                                a
                                                                for a in _artl_lst
                                                                if str(
                                                                    a.get(
                                                                        "pdArtlCd", ""
                                                                    )
                                                                ).zfill(4)
                                                                != _bad38
                                                            ]
                                                            logger.info(
                                                                f"[롯데ON] pdItmsCd=38 항목코드({_bad38}) 제거 → 남은: {[a.get('pdArtlCd') for a in _artl_lst]}"
                                                            )
                                                            if not _artl_lst:
                                                                break
                                                            continue
                                                        else:
                                                            break
                                                    else:
                                                        break
                                    if _artl_resolved:
                                        break  # impDvsCd loop 탈출
                                    if _reg_exception and "항목코드" not in str(
                                        _reg_exception
                                    ):
                                        continue  # 다른 에러 → 다음 impDvsCd 조합
                                logger.warning(
                                    f"[롯데ON] fallback impDvsCd={_imp_code!r} dmst={_dmst_code} 실패: {_e2}"
                                )
                    elif "항목코드" in str(_e):
                        # 항목코드 에러 (수입구분코드 문제 없음): 반복 제거 루프
                        _artl_err = _e
                        for _artl_try in range(10):
                            import re as _re_a

                            _am = _re_a.search(r"항목코드\((\d+)\)", str(_artl_err))
                            _bad_code = _am.group(1) if _am else None
                            if not _bad_code:
                                break
                            logger.info(
                                f"[롯데ON] 항목코드 fallback ({_artl_try + 1}회) — 코드={_bad_code}"
                            )
                            if data.get("spdLst") and isinstance(data["spdLst"], list):
                                _spd = data["spdLst"][0]
                                _ntc = _spd.get("pdItmsInfo")
                                if isinstance(_ntc, dict):
                                    _artl_lst = _ntc.get("pdItmsArtlLst", [])
                                    _ntc["pdItmsArtlLst"] = [
                                        a
                                        for a in _artl_lst
                                        if a.get("pdArtlCd") != _bad_code
                                    ]
                            try:
                                api_result = await client.register_product(data)
                                _reg_exception = None
                                break
                            except Exception as _e3:
                                if "항목코드" in str(_e3):
                                    _artl_err = _e3
                                    _reg_exception = _e3
                                    continue
                                else:
                                    logger.warning(
                                        f"[롯데ON] 항목코드 fallback 실패: {_e3}"
                                    )
                                    _reg_exception = _e3
                                    break
                        # 루프 다 돌아도 항목코드 에러 → pdItmsInfo 자체 제거
                        if _reg_exception and "항목코드" in str(_reg_exception):
                            logger.info(
                                "[롯데ON] 항목코드 전부 실패 → pdItmsInfo 제거 후 재시도"
                            )
                            data["spdLst"][0].pop("pdItmsInfo", None)
                            try:
                                api_result = await client.register_product(data)
                                _reg_exception = None
                            except Exception as _ea_final:
                                logger.warning(
                                    f"[롯데ON] pdItmsInfo 제거 후에도 실패: {_ea_final}"
                                )
                                _reg_exception = _ea_final
                    elif "not supported type" in str(_e):
                        # scatAttrLst optValCd 미지원 → scatAttrLst 제거 후 재시도
                        _reg_exception = _e
                        if data.get("spdLst") and isinstance(data["spdLst"], list):
                            for _spd_s in data["spdLst"]:
                                _spd_s.pop("scatAttrLst", None)
                                _spd_s.pop("scatAttrChgYn", None)
                        logger.info(
                            f"[롯데ON] scatAttrLst 미지원 optValCd fallback — scatAttrLst 제거 재시도 (원인: {_e})"
                        )
                        try:
                            api_result = await client.register_product(data)
                            _reg_exception = None
                        except Exception as _es:
                            logger.warning(
                                f"[롯데ON] scatAttrLst 제거 재시도 실패: {_es}"
                            )
                            _reg_exception = _es
                    elif "등록이 불가한 브랜드" in str(_e):
                        # 거래처-브랜드-전시카테고리 계약 누락: brdNo 공란으로 1회 재시도
                        _reg_exception = _e
                        _orig_brd_no = ""
                        if data.get("spdLst") and isinstance(data["spdLst"], list):
                            _spd_b = data["spdLst"][0]
                            _orig_brd_no = _spd_b.get("brdNo", "") or ""
                            _spd_b["brdNo"] = ""
                        logger.info(
                            f"[롯데ON] 브랜드 불가 fallback — brdNo={_orig_brd_no!r} → '' 재시도 (원인: {_e})"
                        )
                        try:
                            api_result = await client.register_product(data)
                            _reg_exception = None
                        except Exception as _eb:
                            logger.warning(f"[롯데ON] 브랜드 공란 재시도 실패: {_eb}")
                            _reg_exception = _eb
                    else:
                        raise
                if _reg_exception is not None:
                    raise _reg_exception
                # proxy.register_product 가 spdNo를 최상위로 반환 (service.py가 api_result.get("spdNo")로 읽음)
                spd_no = api_result.get("spdNo", "") or api_result.get("epdNo", "")
                logger.info(f"[롯데ON] 등록 완료 — spdNo={spd_no!r}")

                # ── 등록 후 프로모션 설정: sitmNo 조회 후 전달 ────────────
                if spd_no:
                    # 롯데ON 상품 처리 대기 — 즉시 호출 시 9000/9999 에러 발생
                    import asyncio

                    await asyncio.sleep(_POST_REGISTER_DELAY)
                    new_sitm_nos: list[str] = []
                    try:
                        prod_resp = await client.get_product(spd_no)
                        inner = prod_resp.get("data", prod_resp)
                        if isinstance(inner, dict):
                            spd_info = (
                                inner.get("spdLst") or inner.get("spdInfo") or inner
                            )
                            if isinstance(spd_info, list) and spd_info:
                                spd_info = spd_info[0]
                            if isinstance(spd_info, dict):
                                new_sitm_nos = [
                                    str(itm.get("sitmNo"))
                                    for itm in (spd_info.get("itmLst") or [])
                                    if itm.get("sitmNo")
                                ]
                    except Exception as e:
                        logger.warning(
                            f"[롯데ON] 신규 단품 sitmNo 조회 실패 (무시): {e}"
                        )
                    await self._apply_promotions(
                        client, spd_no, extras, is_update=False, eitm_nos=new_sitm_nos
                    )

                # ── 홍보문구 등록 ────────────────────────────────────────────
                if spd_no:
                    publicity_phrase = (
                        extras.get("promotionMessage", "")
                        or extras.get("publicityPhrase", "").strip()
                    )
                    if publicity_phrase:
                        logger.info(
                            f"[롯데ON] 홍보문구 등록 시도 — spdNo={spd_no!r} phrase={publicity_phrase!r}"
                        )
                        try:
                            await client.register_publicity_sentence(
                                spd_no, publicity_phrase
                            )
                        except Exception as e:
                            logger.warning(f"[롯데ON] 홍보문구 등록 실패 (무시): {e}")
                    else:
                        logger.debug(
                            "[롯데ON] 홍보문구 미설정 (설정 > 롯데ON > 상품 홍보문구 입력 필요)"
                        )

                return {
                    "success": True,
                    "message": "롯데ON 등록 성공",
                    "data": api_result,
                    "spdNo": spd_no,
                }
        except Exception as e:
            import traceback as _tb

            action = "수정" if existing_no else "등록"
            # 광범위 except가 진짜 위치를 숨겨 'NoneType has no attribute' 같은 메시지의
            # 발생 라인 추적이 안 되던 문제 — traceback 전체를 로그에 남긴다.
            logger.error(f"[롯데ON] {action} 실패: {e}\n{_tb.format_exc()}")
            return {"success": False, "message": f"롯데ON {action} 실패: {e}"}

    @staticmethod
    def _parse_lotteon_spd_info(resp: dict | None) -> dict:
        """get_product 응답을 envelope 변형(data.itmLst / spdLst[0] / spdInfo) 모두 폴백 파싱.

        다른 경로(경량 분기 L1224, 일반 update L1758, sweep 스크립트)는 spdLst/spdInfo
        폴백을 하는데 _restore_sout_to_sale만 data.itmLst만 봤음. 통일.
        """
        if not isinstance(resp, dict):
            return {}
        inner = resp.get("data", resp)
        if isinstance(inner, dict):
            spd_info = inner.get("spdLst") or inner.get("spdInfo") or inner
            if isinstance(spd_info, list) and spd_info:
                spd_info = spd_info[0]
            if isinstance(spd_info, dict):
                return spd_info
        return {}

    @staticmethod
    def _verify_change_status_response(result: dict | None) -> tuple[bool, str]:
        """change_status 응답의 outer code + data[].resultCode 직접 검증.

        api_client.change_status는 outer-code 검사만 하고 item-level resultCode는 보지 않음.
        delete_product가 별도로 검증하는 패턴 재사용.

        Returns:
            (success, message): 성공 여부와 실패 시 사유.
        """
        if not isinstance(result, dict):
            return False, f"non-dict response: {type(result).__name__}"
        ok_codes = ("0000", "00", "SUCCESS", "")
        outer_code = (
            result.get("returnCode")
            or result.get("code")
            or result.get("resultCode")
            or ""
        )
        if outer_code not in ok_codes:
            return (
                False,
                f"outer code {outer_code!r}: {result.get('message') or result}",
            )
        items = result.get("data") or []
        if isinstance(items, list):
            for itm in items:
                if not isinstance(itm, dict):
                    continue
                code = itm.get("resultCode", "")
                if code and code not in ok_codes:
                    return (
                        False,
                        f"item code {code!r}: {itm.get('resultMessage') or itm}",
                    )
        return True, ""

    async def _restore_items_to_sale(
        self,
        client: Any,
        spd_no: str,
        spd_info: dict,
        itm_stk_lst: list[dict],
    ) -> bool:
        """ITEM phase — stkQty>0 + slStatRsnCd=SOUT_STK 옵션을 SALE로 복구.

        update_stock(item/stock/change)는 stkQty만 반영하고 slStatCd는 무시하므로,
        재고 0→양수 회복 시 옵션 slStatCd가 SOUT 고착. item/status/change로 명시적 SALE 전환.

        Returns:
            bool: 1건 이상 복구되면 True (호출자가 spd_info 재조회 여부 판단용).
        """
        positive_stk_sitms = {
            str(it.get("sitmNo"))
            for it in itm_stk_lst or []
            if it.get("sitmNo") and int(it.get("stkQty") or 0) > 0
        }
        if not positive_stk_sitms:
            return False

        cur_itm = spd_info.get("itmLst") or []
        to_recover: list[dict] = []
        for itm in cur_itm:
            if not isinstance(itm, dict):
                continue
            sitm_no = str(itm.get("sitmNo") or "")
            if sitm_no not in positive_stk_sitms:
                continue
            if itm.get("slStatCd") != "SOUT":
                continue
            # SOUT_STK만 복구 — 사람이 수동 잠근 다른 사유는 보존
            if itm.get("slStatRsnCd") != "SOUT_STK":
                continue
            to_recover.append(
                {
                    "sitmNo": sitm_no,
                    "spdNo": spd_no,
                    "slStatCd": "SALE",
                }
            )

        if not to_recover:
            return False

        await client.change_item_status(to_recover)
        logger.info(
            f"[롯데ON] item SOUT→SALE 복구: {spd_no} — "
            f"{len(to_recover)}건 ({[t['sitmNo'] for t in to_recover]})"
        )
        return True

    async def _restore_spd_to_sale(
        self,
        client: Any,
        spd_no: str,
        spd_info: dict,
    ) -> None:
        """SPD phase — slStatRsnCd=SOUT_ITM 잠긴 SPD 헤더를 SALE로 복구.

        롯데ON은 옵션이 모두 stkQty=0이 되면 SPD를 SOUT/SOUT_ITM으로 자동 escalate한다.
        옵션을 SALE+재고 양수로 복구해도 SPD 헤더는 자동 해제되지 않아 소비자 페이지에서
        '품절된 상품입니다'가 유지된다(2026-04-30 LO2665417627 사례). product/status/change로
        SPD 단위 SALE 전환이 별도 필요.

        가드:
        - SPD slStatCd=='SOUT' AND slStatRsnCd=='SOUT_ITM' (셀러 수동 SOUT 등 다른 사유는 보존)
        - itmLst 중 ≥1개가 slStatCd=='SALE' AND stkQty>0 (실제 판매 가능한 옵션 존재)

        검증: change_status 응답의 data[].resultCode를 직접 검사 (래퍼는 outer만 봄).
        """
        if spd_info.get("slStatCd") != "SOUT":
            return
        if spd_info.get("slStatRsnCd") != "SOUT_ITM":
            return
        cur_itm = spd_info.get("itmLst") or []
        sellable = any(
            isinstance(itm, dict)
            and itm.get("slStatCd") == "SALE"
            and int(itm.get("stkQty") or 0) > 0
            for itm in cur_itm
        )
        if not sellable:
            return

        # 최소 페이로드 — trGrpCd/trNo는 client가 자동 prepend
        result = await client.change_status([{"spdNo": spd_no, "slStatCd": "SALE"}])
        ok, msg = self._verify_change_status_response(result)
        if ok:
            logger.info(f"[롯데ON] SPD SOUT_ITM→SALE 복구: {spd_no}")
        else:
            logger.warning(f"[롯데ON] SPD SOUT_ITM→SALE 복구 실패: {spd_no} — {msg}")

    async def _restore_sout_to_sale(
        self,
        client: Any,
        spd_no: str,
        itm_stk_lst: list[dict],
    ) -> None:
        """재고 회복 후 SOUT 잠금 해제 — item phase + SPD phase orchestrator.

        item phase:
        - stkQty>0 + slStatRsnCd=SOUT_STK 옵션을 SALE로 (item/status/change)

        SPD phase:
        - SPD slStatRsnCd=SOUT_ITM이고 sellable item이 있으면 SPD SALE로 (product/status/change)
        - item phase가 옵션을 살린 직후이므로 race 방지를 위해 spd_info 재조회

        실패해도 결과에 영향 없음 (warning 로그만 — 등록/수정 흐름은 정상 종료).
        """
        try:
            spd_info = self._parse_lotteon_spd_info(await client.get_product(spd_no))
            if not spd_info:
                return

            items_changed = await self._restore_items_to_sale(
                client, spd_no, spd_info, itm_stk_lst
            )

            # item 복구가 있었다면 spd_info를 다시 받아 SPD phase 가드를 갱신된 상태로 평가
            if items_changed:
                spd_info = self._parse_lotteon_spd_info(
                    await client.get_product(spd_no)
                )
                if not spd_info:
                    return

            await self._restore_spd_to_sale(client, spd_no, spd_info)
        except Exception as e:
            logger.warning(f"[롯데ON] SOUT→SALE 복구 실패 (무시): {spd_no} — {e}")

    async def _apply_promotions(
        self,
        client: Any,
        spd_no: str,
        extras: dict,
        is_update: bool = False,
        eitm_nos: list[str] | None = None,
    ) -> None:
        """등록/수정 후 프로모션 설정 — 실패해도 결과에 영향 없음."""

        # ── 즉시할인 ───────────────────────────────────────────────────
        # 팀장 결정으로 즉시할인 미사용 (sale_price 그대로 등록, 추가 할인 없음).

        # ── 행사 제외 설정 ──────────────────────────────────────────────
        # 설정값 Y/N → API값 AGR(제외)/NXCLD(제외안함) 변환
        # 8개 필드 전부 필수, 미설정 항목은 NXCLD 기본값
        _flag_map = {
            "ownerDiscountExclude": "onerDcXcldAgrCd",
            "unitCouponExclude": "ovlpCpnXcldAgrCd",
            "deliveryCouponExclude": "dvCpnXcldAgrCd",
            "cmPcsExclude": "crdCmDcXcldAgrCd",
            "pcsExclude": "pcsDcXcldAgrCd",
        }
        _yn_to_agr = {"Y": "AGR", "N": "NXCLD"}
        exception_flags: dict[str, str] = {}
        has_any = False
        for settings_key, api_key in _flag_map.items():
            val = extras.get(settings_key, "")
            if val in ("Y", "N"):
                exception_flags[api_key] = _yn_to_agr[val]
                has_any = True
            else:
                exception_flags[api_key] = "NXCLD"
        # 나머지 3개 필드는 NXCLD 기본값
        exception_flags.setdefault("stffDcXcldAgrCd", "NXCLD")
        exception_flags.setdefault("odCndCpnXcldAgrCd", "NXCLD")
        exception_flags.setdefault("crdReqDcCashbXcldAgrCd", "NXCLD")

        if has_any:
            try:
                resp = await client.save_product_exception(spd_no, exception_flags)
                logger.info(f"[롯데ON] 행사제외 설정 완료: {exception_flags} -> {resp}")
            except Exception as e:
                logger.warning(f"[롯데ON] 행사제외 설정 실패 (무시): {e}")

        # ── L.POINT 추가적립 ────────────────────────────────────────────
        # 설정 페이지 필드 매핑:
        #   reviewTextPoint      → 구매확정 적립 LPOINT (accm_val1)
        #   reviewPhotoPoint     → 리뷰작성시 LPOINT    (accm_val2)
        #   reviewMonthTextPoint → 사진첨부시 LPOINT    (accm_val3)
        #   reviewMonthPhotoPoint→ 동영상첨부시 LPOINT  (accm_val4)
        lpoint_accm = int(
            extras.get("lpointAccm") or extras.get("reviewTextPoint") or 0
        )
        if lpoint_accm > 0:
            try:
                accm_days = str(extras.get("lpointAccmDays") or "7")
                lpoint_review = int(
                    extras.get("lpointReview") or extras.get("reviewPhotoPoint") or 0
                )
                lpoint_photo = int(
                    extras.get("lpointPhoto") or extras.get("reviewMonthTextPoint") or 0
                )
                lpoint_video = int(
                    extras.get("lpointVideo")
                    or extras.get("reviewMonthPhotoPoint")
                    or 0
                )
                resp = await client.save_lpoint_accumulation(
                    spd_no,
                    accm_val1=lpoint_accm,
                    accm_vp_knd_cd=accm_days,
                    accm_val2=lpoint_review,
                    accm_val3=lpoint_photo,
                    accm_val4=lpoint_video,
                )
                logger.info(
                    f"[롯데ON] L.POINT 적립 설정 완료: {lpoint_accm}P (D+{accm_days}) → {resp}"
                )
            except Exception as e:
                logger.warning(f"[롯데ON] L.POINT 적립 설정 실패 (무시): {e}")

        # ── 살수록할인 ───────────────────────────────────────────────────
        # UI 필드: multiPurchaseDiscount(설정안함/설정함) + multiPurchaseQty(수량) + multiPurchaseRate(할인율%)
        multi_enabled = extras.get("multiPurchaseDiscount") in (
            "설정함",
            "true",
            True,
            "Y",
        )
        multi_qty = int(extras.get("multiPurchaseQty") or 0)
        multi_rate = float(extras.get("multiPurchaseRate") or 0)
        if multi_enabled and multi_qty > 0 and multi_rate > 0:
            try:
                # 기존 살수록할인 prNo 조회 → 있으면 update(U), 없으면 create(C)
                existing_pr_no = ""
                try:
                    search_resp = await client.search_quantity_discount_list(spd_no)
                    pr_list = (search_resp.get("data") or {}).get("prList") or []
                    if pr_list:
                        existing_pr_no = str(pr_list[0].get("prNo", ""))
                        logger.info(
                            f"[롯데ON] 기존 살수록할인 발견: prNo={existing_pr_no} → 수정 모드(U)"
                        )
                except Exception as se:
                    logger.info(
                        f"[롯데ON] 살수록할인 목록 조회 실패 (신규 등록 진행): {se}"
                    )

                resp = await client.insert_quantity_discount(
                    spd_no,
                    min_qty=multi_qty,
                    discount_rate=multi_rate,
                    eitm_nos=eitm_nos or [],
                    pr_no=existing_pr_no,
                )
                logger.info(
                    f"[롯데ON] 살수록할인 설정 완료: {multi_qty}개 이상 {multi_rate}% → {resp}"
                )
            except Exception as e:
                err_str = str(e)
                if "3000" in err_str:
                    # 3000 = 행사기간 중복 → 이미 등록된 살수록할인이 활성 상태
                    logger.info(
                        f"[롯데ON] 살수록할인 이미 등록됨 — 기존 설정 유지 ({err_str[:80]})"
                    )
                else:
                    logger.warning(f"[롯데ON] 살수록할인 설정 실패 (무시): {e}")

    async def delete(self, session, product_no: str, account) -> dict[str, Any]:
        """롯데ON 상품 판매종료 (END 전환 → 판매페이지 즉시 차단 + 시스템 자동 삭제).

        _get_cached_client로 test_auth 캐싱 — 오토튠 품절 배치 시 인증 API 1회만 호출.
        """
        creds = await self._load_auth(session, account)
        if not creds:
            return {"success": False, "message": "롯데ON 인증정보 없음"}

        api_key = creds.get("apiKey", "")
        if not api_key:
            return {"success": False, "message": "롯데ON API Key 없음"}

        try:
            client = await _get_cached_client(api_key)
            # END = 판매종료. 판매페이지가 "현재 판매 중인 상품이 아닙니다"로 즉시 차단되고,
            # 롯데ON 시스템이 일정 기간 경과 후 자동 삭제한다. (SOUT은 품절 배지만 달고
            # 판매페이지는 계속 노출되어 삼바 "품절→삭제" 원칙을 만족시키지 못함.)
            await client.delete_product(product_no)
            return {"success": True, "message": "롯데ON 판매종료 완료"}
        except Exception as e:
            err = str(e)
            # 이미 종료/삭제된 상품 — 마켓 측 정리는 끝났으므로 우리도 success로 간주해
            # registered_accounts에서 정리되도록 한다 (테트리스 브랜드 블럭 삭제 차단 방지).
            # 8888 = 판매종료된 상품, 9999 = 존재하지 않는 상품 등
            _ghost_signals = ("8888", "판매종료 된 상품", "존재하지 않", "이미 삭제")
            if any(sig in err for sig in _ghost_signals):
                logger.warning(
                    f"[롯데ON] 이미 종료/삭제된 상품 — 정리 완료로 처리: {err}"
                )
                return {
                    "success": True,
                    "message": f"롯데ON 이미 종료됨(자동정리): {err}",
                    "ghost_cleanup": True,
                }
            logger.error(f"[롯데ON] 판매종료 실패: {e}")
            return {"success": False, "message": f"롯데ON 판매종료 실패: {e}"}

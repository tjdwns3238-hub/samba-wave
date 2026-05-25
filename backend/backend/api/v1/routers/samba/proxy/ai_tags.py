"""AI 태그 생성/미리보기/적용 엔드포인트 및 관련 헬퍼."""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_write_session_dependency
from backend.domain.samba.cache import cache
from backend.domain.samba.tenant.middleware import get_optional_tenant_id
from backend.utils.logger import logger

from ._helpers import _get_setting

router = APIRouter(tags=["samba-proxy"])

# ── AI 태그 공통 상수 ──

_AI_TAG_GROUP_KEY_PREFIX = "gk:"
_AI_TAG_PRODUCT_PREFIX = "pid:"

_SOURCING_SITE_BANNED: frozenset[str] = frozenset(
    {
        "musinsa",
        "무신사",
        "kream",
        "크림",
        "abcmart",
        "abc마트",
        "올리브영",
        "oliveyoung",
        "ssg",
        "신세계",
        "롯데온",
        "lotteon",
        "gsshop",
        "gs샵",
        "ebay",
        "이베이",
        "zara",
        "자라",
        "fashionplus",
        "패션플러스",
        "grandstage",
        "그랜드스테이지",
        "rexmonde",
        "elandmall",
        "이랜드몰",
        "ssf",
        "ssf샵",
    }
)

_BRAND_BANNED: frozenset[str] = frozenset(
    {
        "nike",
        "나이키",
        "adidas",
        "아디다스",
        "뉴발란스",
        "new balance",
        "푸마",
        "puma",
        "리복",
        "reebok",
        "아식스",
        "asics",
        "컨버스",
        "converse",
        "반스",
        "vans",
        "휠라",
        "fila",
        "스케쳐스",
        "skechers",
        "노스페이스",
        "the north face",
        "코오롱",
        "kolon",
        "아이더",
        "eider",
        "블랙야크",
        "blackyak",
        "k2",
        "네파",
        "nepa",
        "밀레",
        "millet",
        "살로몬",
        "salomon",
        "메렐",
        "merrell",
        "콜롬비아",
        "columbia",
        "호카",
        "hoka",
        "온러닝",
        "on running",
        "라코스테",
        "lacoste",
        "폴로",
        "polo",
        "구찌",
        "gucci",
        "프라다",
        "prada",
        "버버리",
        "burberry",
        "발렌시아가",
        "balenciaga",
        "디올",
        "dior",
    }
)

_BRAND_PARTIAL_MATCH: frozenset[str] = frozenset(
    {
        "나이키",
        "아디다스",
        "뉴발란스",
        "푸마",
        "리복",
        "아식스",
        "컨버스",
        "반스",
        "휠라",
        "스케쳐스",
        "노스페이스",
        "코오롱",
        "아이더",
        "블랙야크",
        "네파",
        "밀레",
        "살로몬",
        "메렐",
        "콜롬비아",
        "호카",
        "라코스테",
        "폴로",
        "구찌",
        "프라다",
        "버버리",
        "발렌시아가",
        "디올",
        "nike",
        "adidas",
        "puma",
        "reebok",
        "asics",
        "converse",
        "vans",
        "fila",
        "skechers",
        "salomon",
        "merrell",
        "columbia",
        "hoka",
        "lacoste",
        "gucci",
        "prada",
        "burberry",
    }
)


# ── 헬퍼 함수 ──


def _make_ai_tag_group_key_id(search_filter_id: str, group_key: str) -> str:
    return f"{_AI_TAG_GROUP_KEY_PREFIX}{search_filter_id}:{group_key}"


def _make_ai_tag_product_id(product_id: str) -> str:
    return f"{_AI_TAG_PRODUCT_PREFIX}{product_id}"


def _parse_ai_tag_group_id(group_id: str) -> tuple[str, str, str]:
    if group_id.startswith(_AI_TAG_GROUP_KEY_PREFIX):
        payload = group_id[len(_AI_TAG_GROUP_KEY_PREFIX) :]
        search_filter_id, _, group_key = payload.partition(":")
        if search_filter_id and group_key:
            return ("group_key", search_filter_id, group_key)
    if group_id.startswith(_AI_TAG_PRODUCT_PREFIX):
        return ("product", group_id[len(_AI_TAG_PRODUCT_PREFIX) :], "")
    return ("legacy", group_id, "")


async def _build_ai_tag_groups(
    repo,
    session: AsyncSession,
    product_ids: list[str],
    req_group_ids: list[str],
) -> tuple[dict[str, list[Any]], dict[str, str]]:
    from backend.domain.samba.collector.model import SambaSearchFilter as _SF_tag

    groups: dict[str, list[Any]] = {}
    group_names: dict[str, str] = {}
    filter_products_cache: dict[str, list[Any]] = {}
    filter_names_cache: dict[str, str] = {}

    async def _get_filter_name(search_filter_id: str) -> str:
        if search_filter_id not in filter_names_cache:
            sf = await session.get(_SF_tag, search_filter_id)
            filter_names_cache[search_filter_id] = (
                (sf.name or search_filter_id) if sf else search_filter_id
            )
        return filter_names_cache[search_filter_id]

    async def _get_filter_products(search_filter_id: str) -> list[Any]:
        if search_filter_id not in filter_products_cache:
            filter_products_cache[search_filter_id] = list(
                await repo.filter_by_async(
                    search_filter_id=search_filter_id, limit=10000
                )
            )
        return filter_products_cache[search_filter_id]

    async def _register_group(
        group_id: str,
        products: list[Any],
        *,
        search_filter_id: str | None = None,
        label: str | None = None,
    ) -> None:
        if not products or group_id in groups:
            return
        groups[group_id] = products
        if label:
            group_names[group_id] = label
        elif search_filter_id:
            group_names[group_id] = await _get_filter_name(search_filter_id)
        else:
            rep = products[0]
            group_names[group_id] = rep.name or rep.id

    for gid in req_group_ids:
        filter_products = await _get_filter_products(gid)
        if not filter_products:
            continue
        # 그룹(사이트+브랜드+카테고리) 단위로 전체 상품을 1개 그룹으로 처리
        # group_key(모델코드)별 분리 불필요 — AI 태그는 그룹 단위로 동일하게 적용
        await _register_group(gid, filter_products, search_filter_id=gid)

    for pid in product_ids:
        product = await repo.get_async(pid)
        if not product:
            continue

        search_filter_id = getattr(product, "search_filter_id", None) or ""
        group_key = (getattr(product, "group_key", None) or "").strip()

        if search_filter_id and group_key:
            products = list(
                await repo.filter_by_async(
                    search_filter_id=search_filter_id,
                    group_key=group_key,
                    limit=10000,
                )
            )
            await _register_group(
                _make_ai_tag_group_key_id(search_filter_id, group_key),
                products,
                search_filter_id=search_filter_id,
                label=f"{await _get_filter_name(search_filter_id)} / {product.name or group_key}",
            )
            continue

        if search_filter_id:
            filter_products = await _get_filter_products(search_filter_id)
            has_grouped_products = any(
                (getattr(item, "group_key", None) or "").strip()
                for item in filter_products
            )
            if not has_grouped_products:
                await _register_group(
                    search_filter_id,
                    filter_products,
                    search_filter_id=search_filter_id,
                )
                continue

        await _register_group(
            _make_ai_tag_product_id(product.id),
            [product],
            search_filter_id=search_filter_id or None,
            label=product.name or product.id,
        )

    return groups, group_names


async def _resolve_ai_tag_apply_products(
    repo, group: dict[str, Any]
) -> tuple[str, list[Any]]:
    product_ids = [pid for pid in group.get("product_ids", []) if pid]
    if product_ids:
        resolved: list[Any] = []
        for pid in product_ids:
            product = await repo.get_async(pid)
            if product:
                resolved.append(product)
        return str(group.get("group_id", "") or ""), resolved

    gid = str(group.get("group_id", "") or "")
    if not gid:
        return "", []

    group_type, first, second = _parse_ai_tag_group_id(gid)
    if group_type == "group_key":
        products = await repo.filter_by_async(
            search_filter_id=first,
            group_key=second,
            limit=10000,
        )
        return gid, list(products)
    if group_type == "product":
        product = await repo.get_async(first)
        return gid, [product] if product else []

    products = await repo.filter_by_async(search_filter_id=gid, limit=10000)
    if products:
        return gid, list(products)
    product = await repo.get_async(gid)
    return gid, [product] if product else []


async def _load_tag_filter_data(session) -> tuple[set[str], set[str]]:
    """DB에서 금지태그/미등록태그 + 전체 브랜드 목록을 1회 로드."""
    ss_banned: set[str] = set()
    db_brands: set[str] = set()
    try:
        from backend.domain.samba.forbidden.repository import SambaSettingsRepository

        repo = SambaSettingsRepository(session)
        for key in ("smartstore_banned_tags", "smartstore_unregistered_tags"):
            row = await repo.find_by_async(key=key)
            if row and isinstance(row.value, list):
                ss_banned.update(w.lower().replace(" ", "") for w in row.value)
    except Exception:
        pass
    try:
        from sqlmodel import select as _sel
        from backend.domain.samba.collector.model import SambaCollectedProduct as _CP

        result = await session.exec(_sel(_CP.brand).distinct())
        for b in result.all():
            if b and len(b) >= 2:
                db_brands.add(b.lower())
    except Exception:
        pass
    return ss_banned, db_brands


def _build_banned_set(
    source_site: str,
    brand: str,
    cats: list,
    rep_name: str,
    ss_banned: set[str],
    db_brands: set[str],
) -> tuple[set[str], set[str], set[str], set[str]]:
    """상품 정보 기반 금지어 집합 (_banned, _name_words, _brand_words, _ss_banned) 생성."""
    _banned = set(_SOURCING_SITE_BANNED | _BRAND_BANNED | ss_banned)
    if source_site:
        _banned.add(source_site.lower())
    for cat_part in cats:
        if cat_part:
            for w in re.split(r"[\s>/\-]+", cat_part):
                clean = w.strip().lower()
                if len(clean) >= 2:
                    _banned.add(clean)
    if brand:
        _banned.add(brand.lower())
        for w in brand.split():
            if len(w) >= 2:
                _banned.add(w.lower())

    _name_words: set[str] = set()
    for w in re.split(r"[\s/\-_()]+", rep_name):
        clean = re.sub(r"[^가-힣a-zA-Z0-9]", "", w).lower()
        if len(clean) >= 2:
            _name_words.add(clean)

    _brand_words = set(_BRAND_PARTIAL_MATCH | db_brands)
    if brand and len(brand) >= 2:
        _brand_words.add(brand.lower())

    return _banned, _name_words, _brand_words, ss_banned


def _is_valid_tag(
    tag: str,
    banned: set[str],
    name_words: set[str],
    ss_banned: set[str],
    brand_words: set[str],
) -> bool:
    """태그 유효성 검사."""
    t = tag.strip().lower()
    if not t:
        return False
    if t in banned or t in name_words:
        return False
    if t.replace(" ", "") in ss_banned:
        return False
    for bw in brand_words:
        if bw in t:
            return False
    return True


def _has_overlap_suffix(word: str, existing: list[str], min_suffix: int = 2) -> bool:
    """기존 SEO 키워드와 접미어가 겹치는지 확인.

    예: 기존에 '로고티셔츠'가 있으면 '그래픽티셔츠'는 '티셔츠' 접미어 중복 → True
    """
    wl = word.lower()
    for e in existing:
        el = e.lower()
        # 공통 접미어 검사 (뒤에서부터 매칭)
        common = 0
        for i in range(1, min(len(wl), len(el)) + 1):
            if wl[-i] == el[-i]:
                common = i
            else:
                break
        if common >= min_suffix and wl != el:
            return True
    return False


def _extract_seo_keywords(
    candidates: list[str],
    cats: list,
    banned: set[str],
    name_words: set[str],
    final_tags: list[str] | None = None,
    max_count: int = 3,
) -> list[str]:
    """최종 검증 태그와 겹치지 않는 SEO 키워드 3개 추출.

    최종 태그에 포함된 키워드는 SEO에서 제외하여 중복을 방지한다.
    태그에 선정되지 않은 후보 중에서 SEO에 적합한 키워드를 추출한다.
    """
    seo: list[str] = []
    # 최종 태그 집합 (소문자, 공백 제거)
    tag_set = {t.lower().replace(" ", "") for t in (final_tags or [])}
    # 태그에 포함되지 않은 후보 우선, 그 다음 전체 후보
    non_tag_candidates = [
        c for c in candidates if c.lower().replace(" ", "") not in tag_set
    ]
    pool = non_tag_candidates + [c for c in candidates if c not in non_tag_candidates]
    for kw in pool:
        cleaned = kw
        for cat_part in cats:
            if cat_part:
                cleaned = cleaned.replace(cat_part, "").strip()
        words = cleaned.split() if " " in cleaned else [cleaned]
        for word in words:
            w = word.strip()
            wl = w.lower().replace(" ", "")
            if len(w) < 2 or wl in banned or wl in name_words or w in seo:
                continue
            # 태그와 겹치면 SEO에서 제외
            if wl in tag_set:
                continue
            if _has_overlap_suffix(w, seo):
                continue
            seo.append(w)
            if len(seo) >= max_count:
                break
        if len(seo) >= max_count:
            break
    return seo


# 모듈 레벨 SmartStore 클라이언트 캐시 — 같은 계정 키면 인스턴스를 재사용해 토큰 재발급 최소화
_ss_client_cache: dict[tuple[str, str], object] = {}


async def _get_smartstore_tag_client(session: AsyncSession):
    """활성 스마트스토어 계정으로 태그사전 검증용 클라이언트 반환.

    동일 (client_id, client_secret)이면 캐시된 인스턴스를 반환하므로
    SmartStore OAuth 토큰(1시간 유효)이 요청 간 재사용된다.
    """
    try:
        from backend.domain.samba.account.repository import SambaMarketAccountRepository
        from backend.domain.samba.proxy.smartstore import SmartStoreClient

        account_repo = SambaMarketAccountRepository(session)
        ss_accounts = await account_repo.filter_by_async(
            market_type="smartstore", is_active=True
        )
        if ss_accounts:
            acc = ss_accounts[0]
            additional = acc.additional_fields or {}
            _cid = additional.get("clientId") or acc.api_key
            _csec = additional.get("clientSecret") or acc.api_secret
            if _cid and _csec:
                cache_key = (_cid, _csec)
                if cache_key not in _ss_client_cache:
                    _ss_client_cache[cache_key] = SmartStoreClient(_cid, _csec)
                    logger.info("[AI태그] 스마트스토어 클라이언트 신규 생성")
                else:
                    logger.info("[AI태그] 스마트스토어 클라이언트 캐시 재사용")
                return _ss_client_cache[cache_key]
    except Exception as e:
        logger.warning(
            f"[AI태그] 스마트스토어 클라이언트 초기화 실패 (태그사전 검증 비활성): {e}"
        )
    return None


# ── 엔드포인트 ──


@router.post("/ai-tags/generate")
async def generate_ai_tags(
    request: dict[str, Any],
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> dict[str, Any]:
    """선택 상품을 그룹별로 묶어 대표 1개로 AI 태그 생성 후 태그사전 검증 → 그룹 전체에 적용."""
    from backend.domain.samba.collector.repository import (
        SambaCollectedProductRepository,
    )

    product_ids = request.get("product_ids", [])
    req_group_ids = request.get("group_ids", [])
    method: str = request.get("method", "gemini")  # gemini | gemma | claude
    logger.info(
        f"[AI태그] 요청: product_ids={len(product_ids)}개, group_ids={req_group_ids}, method={method}"
    )

    if not product_ids and not req_group_ids:
        return {"success": False, "message": "상품 또는 그룹을 선택해주세요"}

    # API 키 조회 (method에 따라 분기)
    if method in ("gemma", "gemini"):
        creds = await _get_setting(session, "gemini", tenant_id=tenant_id)
        if not creds or not isinstance(creds, dict) or not creds.get("apiKey"):
            return {"success": False, "message": "Gemini API 설정이 없습니다"}
        api_key = str(creds["apiKey"]).strip()
        if method == "gemma":
            model = "gemma-4-26b-a4b-it"
        else:
            model = str(creds.get("model", "gemini-2.5-flash"))
    else:
        creds = await _get_setting(session, "claude", tenant_id=tenant_id)
        if not creds or not isinstance(creds, dict) or not creds.get("apiKey"):
            return {"success": False, "message": "Claude API 설정이 없습니다"}
        api_key = str(creds["apiKey"]).strip()
        model = str(creds.get("model", "claude-sonnet-4-6"))

    repo = SambaCollectedProductRepository(session)

    # 그룹 ID 직접 전달 시 바로 사용
    group_products, group_names = await _build_ai_tag_groups(
        repo, session, product_ids, req_group_ids
    )

    # 상품 ID로 전달 시 그룹 추출

    # 그룹별 전체 상품 조회 (샘플링용)

    if not group_products:
        return {"success": False, "message": "상품을 찾을 수 없습니다"}

    total_tagged = 0
    total_groups = len(group_products)
    failed_groups = 0
    shortage_groups = 0  # 등재 태그 10개 미달 그룹 수
    api_calls = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_tag_dict_validated = 0
    total_tag_dict_rejected = 0

    # 금지태그/브랜드 목록 1회 로드
    ss_banned_cache, db_brands_cache = await _load_tag_filter_data(session)

    # 스마트스토어 클라이언트 초기화 (태그사전 검증용)
    ss_client = await _get_smartstore_tag_client(session)

    async with httpx.AsyncClient(timeout=30) as http_client:
        for gid, products in group_products.items():
            rep = products[0]
            rep_name = rep.name or ""
            cats = [rep.category1, rep.category2, rep.category3, rep.category4]
            category = " > ".join(c for c in cats if c) or rep.category or ""
            brand = rep.brand or ""
            source_site = rep.source_site or ""

            # 그룹 내 다양한 상품명 샘플링 (최대 10개, 컬러 제거)
            seen_names: set[str] = set()
            sample_names: list[str] = []
            for p in products:
                n = p.name or ""
                if " - " in n:
                    n = n.split(" - ")[0].strip()
                if n and n not in seen_names:
                    seen_names.add(n)
                    sample_names.append(n)
                    if len(sample_names) >= 10:
                        break
            sample_str = "\n".join(f"  · {n}" for n in sample_names)

            # Claude API 호출
            prompt = (
                f"그룹 상품 정보 ({len(products)}개 상품):\n"
                f"- 브랜드: {brand}\n"
                f"- 카테고리: {category}\n"
                f"- 대표 상품명 (샘플 {len(sample_names)}개):\n{sample_str}\n\n"
                f"이 그룹의 모든 상품에 공통 적용할 검색용 태그를 50개 생성해주세요.\n"
                f"규칙:\n"
                f"1. 소비자가 네이버에서 실제로 검색할 만한 인기 키워드\n"
                f"2. 브랜드명('{brand}')은 제외\n"
                f"3. 한글로 작성\n"
                f"4. 쉼표로 구분하여 태그만 출력 (번호/설명 없이)\n"
                f"5. 수집사이트 이름(MUSINSA, 무신사, KREAM 등)은 제외\n"
                f"6. 브랜드명(나이키, 아디다스, 뉴발란스 등 모든 브랜드)은 절대 포함하지 마세요\n"
                f"7. 복합어보다 실제 검색에 사용되는 단순 키워드 위주 (예: 등산스니커즈(X) → 경량등산화(O))\n"
                f"8. 다양한 관점의 태그 필수 — 다음 카테고리별로 골고루 생성:\n"
                f"   - 용도/상황 (출근용, 데일리, 등산용, 캠핑, 여행)\n"
                f"   - 소재/기능 (고어텍스, 방수, 경량, 쿠션, 통기성)\n"
                f"   - 스타일/느낌 (캐주얼, 클래식, 빈티지, 트렌디)\n"
                f"   - 대상/성별 (남성, 여성, 남녀공용, 커플)\n"
                f"   - 시즌 (봄신발, 겨울신발, 사계절)\n"
                f"9. 같은 의미 단어 조합을 반복하지 마세요 (남성/남자, 여성/여자, 경량/가벼운 등 동의어는 하나만 사용)\n"
                f"10. 색상명(블랙, 화이트, 네이비, 그레이, 베이지, 카키, 레드, 블루 등)은 절대 포함하지 마세요 — 그룹 전체에 공통 적용됩니다\n"
            )

            try:
                # AI API 호출 (429 rate limit 대비 최대 3회 재시도)
                resp = None
                text = ""
                for _attempt in range(3):
                    if method in ("gemma", "gemini"):
                        resp = await http_client.post(
                            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
                            headers={"content-type": "application/json"},
                            json={
                                "contents": [{"parts": [{"text": prompt}]}],
                                "generationConfig": {
                                    "maxOutputTokens": 800,
                                    **(
                                        {"thinkingConfig": {"thinkingBudget": 0}}
                                        if not model.endswith("-image")
                                        else {}
                                    ),
                                },
                            },
                        )
                    else:
                        resp = await http_client.post(
                            "https://api.anthropic.com/v1/messages",
                            headers={
                                "x-api-key": api_key,
                                "anthropic-version": "2023-06-01",
                                "content-type": "application/json",
                            },
                            json={
                                "model": model,
                                "max_tokens": 800,
                                "messages": [{"role": "user", "content": prompt}],
                            },
                        )
                    api_calls += 1
                    if resp.status_code == 429 and _attempt < 2:
                        import asyncio as _aio_tag

                        logger.warning(
                            f"[AI태그] {method} 429 rate limit — {30 * (_attempt + 1)}초 대기"
                        )
                        await _aio_tag.sleep(30 * (_attempt + 1))
                        continue
                    break
                if not resp or resp.status_code != 200:
                    logger.warning(
                        f"[AI태그] {method} 호출 실패: {resp.status_code if resp else 'no response'}"
                    )
                    failed_groups += 1
                    continue

                data = resp.json()
                if method in ("gemma", "gemini"):
                    usage = data.get("usageMetadata", {})
                    total_input_tokens += usage.get("promptTokenCount", 0)
                    total_output_tokens += usage.get("candidatesTokenCount", 0)
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        text = parts[0].get("text", "") if parts else ""
                else:
                    usage = data.get("usage", {})
                    total_input_tokens += usage.get("input_tokens", 0)
                    total_output_tokens += usage.get("output_tokens", 0)
                    text = data.get("content", [{}])[0].get("text", "")

                # 금지어 집합 생성
                banned, name_words, brand_words, ss_banned = _build_banned_set(
                    source_site,
                    brand,
                    cats,
                    rep_name,
                    ss_banned_cache,
                    db_brands_cache,
                )

                # 쉼표 구분 태그 파싱 + 금지어 필터링
                ai_tags = [
                    t.strip()
                    for t in text.split(",")
                    if _is_valid_tag(t, banned, name_words, ss_banned, brand_words)
                ]

                # AI 태그 중복 제거 (후보 전체 보존 — 태그사전 검증에서 탈락 대비)
                seen: set[str] = set()
                candidate_tags: list[str] = []
                for t in ai_tags:
                    tl = t.lower().replace(" ", "")
                    if tl not in seen:
                        seen.add(tl)
                        candidate_tags.append(t)

                if not candidate_tags:
                    continue

                # 태그사전 검증: 등재 태그만 사용 (미등록 보충 금지)
                # 12개 확보 시 상위 2개 SEO + 나머지 10개 태그
                top12: list[str] = []
                if ss_client and candidate_tags:
                    try:
                        validated = await ss_client.validate_tags(
                            candidate_tags, max_count=15
                        )
                        # 안전망: 검증 결과에도 브랜드/사이트/사용자금지어 재필터
                        for v in validated:
                            text = v.get("text", "")
                            if _is_valid_tag(
                                text, banned, name_words, ss_banned, brand_words
                            ):
                                top12.append(text)
                                if len(top12) >= 12:
                                    break
                        total_tag_dict_validated += len(top12)
                        total_tag_dict_rejected += max(
                            0, len(candidate_tags) - len(top12)
                        )
                        logger.info(
                            f"[AI태그] 그룹 {gid}: 후보 {len(candidate_tags)}개 → 등재 {len(top12)}개"
                        )
                    except Exception as ve:
                        logger.error(
                            f"[AI태그] 태그사전 검증 예외 — 등재 태그 0개로 처리: {ve}"
                        )
                        top12 = []
                else:
                    if not ss_client:
                        logger.warning(
                            "[AI태그] 스마트스토어 클라이언트 없음 — 태그사전 검증 불가, 태그 미적용"
                        )
                    top12 = []

                if not top12:
                    failed_groups += 1
                    continue

                # 상위 2개 = SEO, 접미어 중복 시 앞 단어에서 공통 접미어 제거
                seo_kws = top12[:2]
                if len(seo_kws) == 2:
                    a, b = seo_kws[0], seo_kws[1]
                    # 공통 접미어 찾기 (뒤에서부터)
                    common = 0
                    for i in range(1, min(len(a), len(b)) + 1):
                        if a[-i] == b[-i]:
                            common = i
                        else:
                            break
                    if common >= 2:
                        prefix = a[:-common].strip()
                        if len(prefix) >= 1:
                            seo_kws[0] = prefix
                tags = top12[2:12]
                if len(tags) < 10:
                    shortage_groups += 1
                    logger.warning(
                        f"[AI태그] 그룹 {gid}: 등재 태그 부족 — 태그 {len(tags)}개/10개"
                    )

                # 태그 생성 후 그룹 전체 상품 조회 → 벌크 적용
                # 기존 태그는 __센티넬만 보존하고 새 태그로 교체 (누적 방지)
                for p in products:
                    preserved = [
                        t
                        for t in (p.tags or [])
                        if isinstance(t, str) and t.startswith("__")
                    ]
                    merged = list(dict.fromkeys([*preserved, "__ai_tagged__", *tags]))
                    update_data: dict = {"tags": merged}
                    if seo_kws:
                        update_data["seo_keywords"] = seo_kws
                    await repo.update_async(p.id, **update_data)
                    total_tagged += 1

            except Exception as e:
                logger.error(f"[AI태그] 그룹 {gid} 실패: {e}")
                failed_groups += 1
                continue

    await session.commit()
    await cache.clear_pattern("filters:tree:counts:*")
    from backend.domain.samba.tetris.service import clear_board_cache as _cbc  # noqa: F811

    _cbc()
    # 실비 계산 (Claude Sonnet 4.6: 입력 $3/1M, 출력 $15/1M, 환율 1400원)
    input_cost = total_input_tokens * 3 / 1_000_000 * 1400
    output_cost = total_output_tokens * 15 / 1_000_000 * 1400
    total_cost = round(input_cost + output_cost, 1)
    validated_msg = (
        f", 태그사전 통과 {total_tag_dict_validated}개/제외 {total_tag_dict_rejected}개"
        if ss_client
        else ""
    )
    fail_msg = f", 실패 {failed_groups}개 그룹" if failed_groups else ""
    shortage_msg = (
        f", 등재태그 부족 {shortage_groups}개 그룹" if shortage_groups else ""
    )
    return {
        "success": True,
        "message": f"태그 생성 완료 — {total_groups}개 그룹, {total_tagged}개 상품에 복사{fail_msg}{shortage_msg} (₩{total_cost}{validated_msg})",
        "total_tagged": total_tagged,
        "failed_groups": failed_groups,
        "shortage_groups": shortage_groups,
        "api_calls": api_calls,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "cost_krw": total_cost,
        "tag_dict_validated": total_tag_dict_validated,
        "tag_dict_rejected": total_tag_dict_rejected,
    }


@router.post("/ai-tags/preview")
async def preview_ai_tags(
    request: dict[str, Any],
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> dict[str, Any]:
    """선택 상품의 그룹별 대표 1개로 AI 태그 25개 생성 → 적용하지 않고 미리보기 반환."""
    from backend.domain.samba.collector.repository import (
        SambaCollectedProductRepository,
    )

    product_ids = request.get("product_ids", [])
    req_group_ids = request.get("group_ids", [])
    method: str = request.get("method", "gemini")  # gemini | gemma | claude
    logger.info(
        f"[AI태그 미리보기] 요청: product_ids={len(product_ids)}개, group_ids={req_group_ids}, method={method}"
    )

    if not product_ids and not req_group_ids:
        return {"success": False, "message": "상품 또는 그룹을 선택해주세요"}

    # API 키 조회 (method에 따라 분기)
    if method in ("gemma", "gemini"):
        creds = await _get_setting(session, "gemini", tenant_id=tenant_id)
        if not creds or not isinstance(creds, dict) or not creds.get("apiKey"):
            return {"success": False, "message": "Gemini API 설정이 없습니다"}
        api_key = str(creds["apiKey"]).strip()
        if method == "gemma":
            model = "gemma-4-26b-a4b-it"
        else:
            model = str(creds.get("model", "gemini-2.5-flash"))
    else:
        creds = await _get_setting(session, "claude", tenant_id=tenant_id)
        if not creds or not isinstance(creds, dict) or not creds.get("apiKey"):
            return {"success": False, "message": "Claude API 설정이 없습니다"}
        api_key = str(creds["apiKey"]).strip()
        model = str(creds.get("model", "claude-sonnet-4-6"))

    repo = SambaCollectedProductRepository(session)

    # 그룹 ID 수집
    groups, group_names = await _build_ai_tag_groups(
        repo, session, product_ids, req_group_ids
    )

    # 그룹별 상품 조회

    if not groups:
        return {"success": False, "message": "상품을 찾을 수 없습니다"}

    # 그룹명 조회
    # 그룹별 태그 미리보기 결과
    preview_results: list[dict[str, Any]] = []
    failed_groups = 0
    api_calls = 0
    total_input_tokens = 0
    total_output_tokens = 0

    # 금지태그/브랜드 목록 1회 로드
    ss_banned_cache, db_brands_cache = await _load_tag_filter_data(session)

    # 스마트스토어 클라이언트 초기화 (태그사전 검증용)
    ss_client_preview = await _get_smartstore_tag_client(session)

    async with httpx.AsyncClient(timeout=90) as http_client:
        logger.info(
            f"[AI태그 미리보기] {method} model={model} key={api_key[:10]}... groups={len(groups)}"
        )
        for gid, products in groups.items():
            rep = products[0]
            rep_name = rep.name or ""
            cats = [rep.category1, rep.category2, rep.category3, rep.category4]
            category = " > ".join(c for c in cats if c) or rep.category or ""
            brand = rep.brand or ""
            source_site = rep.source_site or ""

            # 그룹 내 다양한 상품명 샘플링 (최대 10개, 컬러 제거)
            seen_names: set[str] = set()
            sample_names: list[str] = []
            for p in products:
                n = p.name or ""
                if " - " in n:
                    n = n.split(" - ")[0].strip()
                if n and n not in seen_names:
                    seen_names.add(n)
                    sample_names.append(n)
                    if len(sample_names) >= 10:
                        break
            sample_str = "\n".join(f"  · {n}" for n in sample_names)

            # Claude API 호출 (25개 요청 — 태그사전 검증 탈락 대비 여유분)
            prompt = (
                f"그룹 상품 정보 ({len(products)}개 상품):\n"
                f"- 브랜드: {brand}\n"
                f"- 카테고리: {category}\n"
                f"- 대표 상품명 (샘플 {len(sample_names)}개):\n{sample_str}\n\n"
                f"이 그룹의 모든 상품에 공통 적용할 검색용 태그를 50개 생성해주세요.\n"
                f"규칙:\n"
                f"1. 소비자가 네이버에서 실제로 검색할 만한 인기 키워드\n"
                f"2. 브랜드명('{brand}')은 제외\n"
                f"3. 한글로 작성\n"
                f"4. 쉼표로 구분하여 태그만 출력 (번호/설명 없이)\n"
                f"5. 수집사이트 이름(MUSINSA, 무신사, KREAM 등)은 제외\n"
                f"6. 브랜드명(나이키, 아디다스, 뉴발란스 등 모든 브랜드)은 절대 포함하지 마세요\n"
                f"7. 복합어보다 실제 검색에 사용되는 단순 키워드 위주 (예: 등산스니커즈(X) → 경량등산화(O))\n"
                f"8. 다양한 관점의 태그 필수 — 다음 카테고리별로 골고루 생성:\n"
                f"   - 용도/상황 (출근용, 데일리, 등산용, 캠핑, 여행)\n"
                f"   - 소재/기능 (고어텍스, 방수, 경량, 쿠션, 통기성)\n"
                f"   - 스타일/느낌 (캐주얼, 클래식, 빈티지, 트렌디)\n"
                f"   - 대상/성별 (남성, 여성, 남녀공용, 커플)\n"
                f"   - 시즌 (봄신발, 겨울신발, 사계절)\n"
                f"9. 같은 의미 단어 조합을 반복하지 마세요 (남성/남자, 여성/여자, 경량/가벼운 등 동의어는 하나만 사용)\n"
                f"10. 색상명(블랙, 화이트, 네이비, 그레이, 베이지, 카키, 레드, 블루 등)은 절대 포함하지 마세요 — 그룹 전체에 공통 적용됩니다\n"
            )

            try:
                # AI API 호출 (429 rate limit 대비 최대 3회 재시도)
                resp = None
                text = ""
                for _attempt in range(3):
                    if method in ("gemma", "gemini"):
                        resp = await http_client.post(
                            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
                            headers={"content-type": "application/json"},
                            json={
                                "contents": [{"parts": [{"text": prompt}]}],
                                "generationConfig": {
                                    "maxOutputTokens": 800,
                                    **(
                                        {"thinkingConfig": {"thinkingBudget": 0}}
                                        if not model.endswith("-image")
                                        else {}
                                    ),
                                },
                            },
                        )
                    else:
                        resp = await http_client.post(
                            "https://api.anthropic.com/v1/messages",
                            headers={
                                "x-api-key": api_key,
                                "anthropic-version": "2023-06-01",
                                "content-type": "application/json",
                            },
                            json={
                                "model": model,
                                "max_tokens": 800,
                                "messages": [{"role": "user", "content": prompt}],
                            },
                        )
                    api_calls += 1
                    if resp.status_code == 429 and _attempt < 2:
                        import asyncio as _aio_tag

                        logger.warning(
                            f"[AI태그 미리보기] {method} 429 rate limit — {30 * (_attempt + 1)}초 대기"
                        )
                        await _aio_tag.sleep(30 * (_attempt + 1))
                        continue
                    break
                if not resp or resp.status_code != 200:
                    logger.warning(
                        f"[AI태그 미리보기] {method} 호출 실패: {resp.status_code if resp else 'no response'}"
                        f" — {resp.text[:300] if resp else ''}"
                    )
                    failed_groups += 1
                    continue

                data = resp.json()
                if method in ("gemma", "gemini"):
                    usage = data.get("usageMetadata", {})
                    total_input_tokens += usage.get("promptTokenCount", 0)
                    total_output_tokens += usage.get("candidatesTokenCount", 0)
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        text = parts[0].get("text", "") if parts else ""
                else:
                    usage = data.get("usage", {})
                    total_input_tokens += usage.get("input_tokens", 0)
                    total_output_tokens += usage.get("output_tokens", 0)
                    text = data.get("content", [{}])[0].get("text", "")

                # 금지어 집합 생성
                banned, name_words, brand_words, ss_banned = _build_banned_set(
                    source_site,
                    brand,
                    cats,
                    rep_name,
                    ss_banned_cache,
                    db_brands_cache,
                )

                # 중복 제거 후 후보 전체 보존
                seen: set[str] = set()
                candidate_tags: list[str] = []
                for t in text.split(","):
                    t = t.strip()
                    if not _is_valid_tag(t, banned, name_words, ss_banned, brand_words):
                        continue
                    tl = t.lower().replace(" ", "")
                    if tl not in seen:
                        seen.add(tl)
                        candidate_tags.append(t)

                # 태그사전 검증: 등재 태그만 사용 (미등록 보충 금지)
                # 12개 확보 시 상위 2개 SEO + 나머지 10개 태그
                top12_preview: list[str] = []
                rejected_tags: list[str] = []
                tag_validation_error = ""
                if ss_client_preview and candidate_tags:
                    try:
                        validated = await ss_client_preview.validate_tags(
                            candidate_tags, max_count=15
                        )
                        # 안전망: 검증 결과에도 브랜드/사이트/사용자금지어 재필터
                        for v in validated:
                            text = v.get("text", "")
                            if _is_valid_tag(
                                text, banned, name_words, ss_banned, brand_words
                            ):
                                top12_preview.append(text)
                                if len(top12_preview) >= 12:
                                    break
                        accepted_set = set(top12_preview)
                        rejected_tags = [
                            t for t in candidate_tags if t not in accepted_set
                        ]
                    except Exception as ve:
                        tag_validation_error = str(ve)
                        logger.error(
                            f"[AI태그] 태그사전 검증 예외 — 등재 태그 0개로 처리: {ve}"
                        )
                        top12_preview = []
                        rejected_tags = list(candidate_tags)
                else:
                    if not ss_client_preview:
                        tag_validation_error = (
                            "스마트스토어 계정 미연동 — 태그사전 검증 불가"
                        )
                        logger.warning(
                            "[AI태그 미리보기] 스마트스토어 클라이언트 없음 — 태그 미적용"
                        )
                    top12_preview = []
                    rejected_tags = list(candidate_tags)

                # 상위 2개 = SEO, 접미어 중복 시 앞 단어에서 공통 접미어 제거
                seo_preview = top12_preview[:2]
                if len(seo_preview) == 2:
                    _a, _b = seo_preview[0], seo_preview[1]
                    _common = 0
                    for _i in range(1, min(len(_a), len(_b)) + 1):
                        if _a[-_i] == _b[-_i]:
                            _common = _i
                        else:
                            break
                    if _common >= 2:
                        _prefix = _a[:-_common].strip()
                        if len(_prefix) >= 1:
                            seo_preview[0] = _prefix
                validated_tags = top12_preview[2:12]
                tag_shortage = len(validated_tags) < 10

                preview_results.append(
                    {
                        "group_id": gid,
                        "group_name": group_names.get(gid, rep_name),
                        "product_count": len(products),
                        "rep_name": rep_name,
                        "product_ids": [p.id for p in products],
                        "group_key": rep.group_key or "",
                        "tags": validated_tags,
                        "rejected_tags": rejected_tags,
                        "seo_keywords": seo_preview,
                        "candidate_count": len(candidate_tags),
                        "candidates": candidate_tags[:15],
                        "validation_error": tag_validation_error,
                        "tag_shortage": tag_shortage,
                        "tag_count": len(validated_tags),
                        "tag_target": 10,
                    }
                )

            except Exception as e:
                logger.error(
                    f"[AI태그 미리보기] 그룹 {gid} 실패: {type(e).__name__}: {e}"
                )
                failed_groups += 1
                continue

    # 비용 계산
    input_cost = total_input_tokens * 3 / 1_000_000 * 1400
    output_cost = total_output_tokens * 15 / 1_000_000 * 1400
    total_cost = round(input_cost + output_cost, 1)

    fail_msg = f", 실패 {failed_groups}개 그룹" if failed_groups else ""
    shortage_count = sum(1 for r in preview_results if r.get("tag_shortage"))
    shortage_msg = f", 등재태그 부족 {shortage_count}개 그룹" if shortage_count else ""
    return {
        "success": True,
        "message": f"{len(preview_results)}개 그룹 태그 미리보기 생성 완료{fail_msg}{shortage_msg} (₩{total_cost})",
        "previews": preview_results,
        "failed_groups": failed_groups,
        "shortage_groups": shortage_count,
        "api_calls": api_calls,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "cost_krw": total_cost,
    }


@router.post("/ai-tags/apply")
async def apply_ai_tags(
    request: dict[str, Any],
    session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """사용자가 확정한 태그를 그룹 전체 상품에 적용."""
    from backend.domain.samba.collector.repository import (
        SambaCollectedProductRepository,
    )

    # groups: [{ group_id, tags: [...] }]
    groups_data = request.get("groups", [])
    removed_tags = request.get("removed_tags", [])
    if not groups_data:
        return {"success": False, "message": "적용할 태그 데이터가 없습니다"}

    # 삭제된 태그를 금지태그(smartstore_banned_tags)에 추가
    banned_added = 0
    if removed_tags:
        try:
            from backend.domain.samba.forbidden.repository import (
                SambaSettingsRepository,
            )

            settings_repo = SambaSettingsRepository(session)
            row = await settings_repo.find_by_async(key="smartstore_banned_tags")
            existing_banned: list[str] = (
                row.value if row and isinstance(row.value, list) else []
            )
            existing_lower = {w.lower() for w in existing_banned}
            for tag in removed_tags:
                if tag.lower() not in existing_lower:
                    existing_banned.append(tag)
                    existing_lower.add(tag.lower())
                    banned_added += 1
            if banned_added > 0:
                await settings_repo.upsert_async(
                    key="smartstore_banned_tags", value=existing_banned
                )
                logger.info(
                    f"[AI태그] 금지태그 {banned_added}개 추가: {removed_tags[:5]}"
                )
        except Exception as e:
            logger.warning(f"[AI태그] 금지태그 저장 실패: {e}")

    repo = SambaCollectedProductRepository(session)
    total_tagged = 0

    for group in groups_data:
        gid, products = await _resolve_ai_tag_apply_products(repo, group)
        tags = group.get("tags", [])
        if not gid or not tags:
            continue

        # 그룹 상품 조회
        if not products:
            continue
            # 개별 상품 (그룹 없는 경우)

        # SEO 키워드: 프론트에서 수정한 값 우선, 없으면 자동 추출 (태그와 중복 방지)
        seo_kws: list[str] = list(group.get("seo_keywords", []) or [])

        # 브랜드/소싱처 금지어 필터 — 프론트 우회·구버전 preview 값으로
        # 경쟁 브랜드명("나이키","뉴발란스" 등)이 SEO에 섞이는 사고 방지.
        # 사용자 정의 스마트스토어 금지태그(ss_banned)+DB 등록 브랜드도 함께 차단.
        try:
            _ss_banned_set, _db_brands = await _load_tag_filter_data(session)
        except Exception:
            _ss_banned_set, _db_brands = set(), set()
        _self_brand_lower = ""
        try:
            for _p in products:
                _b = getattr(_p, "brand", None)
                if _b:
                    _self_brand_lower = str(_b).lower().replace(" ", "")
                    break
        except Exception:
            pass
        _brand_block_lower = {b.lower().replace(" ", "") for b in _BRAND_BANNED}
        _site_block_lower = {s.lower().replace(" ", "") for s in _SOURCING_SITE_BANNED}
        _db_brand_block_lower = {b.lower().replace(" ", "") for b in _db_brands if b}
        _blocked_lower = (
            _brand_block_lower
            | _site_block_lower
            | _ss_banned_set
            | _db_brand_block_lower
        )
        # 자기 브랜드는 차단 대상에서 제외 (자기 브랜드 SEO는 정상 허용)
        if _self_brand_lower:
            _blocked_lower.discard(_self_brand_lower)

        def _seo_allowed(word: str) -> bool:
            wl = (word or "").lower().replace(" ", "")
            if not wl:
                return False
            if wl in _blocked_lower:
                return False
            # 부분일치 차단 (예: "나이키신발" 같은 결합어) — 자기 브랜드 토큰은 허용
            for token in _BRAND_PARTIAL_MATCH:
                tl = token.lower().replace(" ", "")
                if not tl:
                    continue
                if _self_brand_lower and tl == _self_brand_lower:
                    continue
                if tl in wl:
                    return False
            return True

        seo_kws = [w for w in seo_kws if _seo_allowed(w)]

        if not seo_kws:
            tag_lower_set = {t.lower().replace(" ", "") for t in tags}
            ordered = list(tags[10:]) + list(tags[:10])
            for kw in ordered:
                for word in kw.split():
                    w = word.strip()
                    wl = w.lower().replace(" ", "")
                    if (
                        len(w) >= 2
                        and wl not in tag_lower_set
                        and w not in seo_kws
                        and _seo_allowed(w)
                    ):
                        seo_kws.append(w)
                        if len(seo_kws) >= 2:
                            break
                if len(seo_kws) >= 2:
                    break

        # 그룹 내 모든 상품에 적용 (개별 커밋 없이 일괄 처리)
        from sqlalchemy.orm.attributes import flag_modified as _fm
        from datetime import datetime as _dt, UTC as _utc

        for p in products:
            # 기존 태그는 __센티넬만 보존하고 새 태그로 교체 (누적 방지)
            preserved = [
                t for t in (p.tags or []) if isinstance(t, str) and t.startswith("__")
            ]
            merged = list(dict.fromkeys([*preserved, "__ai_tagged__", *tags]))
            p.tags = merged
            _fm(p, "tags")
            if seo_kws:
                p.seo_keywords = seo_kws
                _fm(p, "seo_keywords")
            if hasattr(p, "updated_at"):
                p.updated_at = _dt.now(_utc)
            session.add(p)
            total_tagged += 1

    await session.commit()
    await cache.clear_pattern("filters:tree:counts:*")
    from backend.domain.samba.tetris.service import clear_board_cache as _cbc  # noqa: F811

    _cbc()
    return {
        "success": True,
        "message": f"{len(groups_data)}개 그룹, {total_tagged}개 상품에 태그 적용 완료"
        + (f" (금지태그 {banned_added}개 추가)" if banned_added else ""),
        "total_tagged": total_tagged,
        "banned_added": banned_added,
    }


@router.post("/ai-tags/clear")
async def clear_ai_tags(
    request: dict[str, Any],
    session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """그룹 전체 상품의 AI 태그(tags, seo_keywords)를 초기화한다."""
    from backend.domain.samba.collector.repository import (
        SambaCollectedProductRepository,
    )
    from datetime import UTC as _utc
    from datetime import datetime as _dt

    from sqlalchemy.orm.attributes import flag_modified as _fm

    group_ids = request.get("group_ids", [])
    if not group_ids:
        return {"success": False, "message": "대상 그룹이 없습니다"}

    repo = SambaCollectedProductRepository(session)
    total_cleared = 0

    for gid in group_ids:
        _, products = await _resolve_ai_tag_apply_products(repo, {"group_id": gid})
        for p in products:
            p.tags = None
            p.seo_keywords = None
            _fm(p, "tags")
            _fm(p, "seo_keywords")
            if hasattr(p, "updated_at"):
                p.updated_at = _dt.now(_utc)
            session.add(p)
            total_cleared += 1

    await session.commit()
    await cache.clear_pattern("filters:tree:counts:*")
    from backend.domain.samba.tetris.service import clear_board_cache as _cbc  # noqa: F811

    _cbc()
    logger.info(f"[AI태그] 태그 삭제: {len(group_ids)}개 그룹, {total_cleared}개 상품")
    return {
        "success": True,
        "message": f"{len(group_ids)}개 그룹, {total_cleared}개 상품의 AI 태그 삭제 완료",
        "total_cleared": total_cleared,
    }

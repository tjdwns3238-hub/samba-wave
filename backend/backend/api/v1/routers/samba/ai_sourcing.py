"""AI 소싱기 API 라우터.

근거데이터 분석, 브랜드 IP검증, 검색그룹 일괄 생성 엔드포인트.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from backend.domain.user.auth_service import get_user_id

from backend.db.orm import get_write_session
from backend.domain.samba.ai_sourcing.service import (
    AISourcingService,
    MAIN_CATEGORIES,
    MUSINSA_CATEGORIES,
    NAVER_DL_CATEGORIES,
)
from backend.domain.samba.collector.repository import SambaSearchFilterRepository
from backend.domain.samba.forbidden.repository import (
    SambaForbiddenWordRepository,
    SambaSettingsRepository,
)
from backend.utils.logger import logger

router = APIRouter(prefix="/ai-sourcing", tags=["AI Sourcing"])


# ── Request / Response DTOs ──


class AnalyzeRequest(BaseModel):
    use_musinsa: bool = True
    use_naver: bool = True
    musinsa_categories: list[str] | None = None
    naver_categories: list[str] | None = None
    target_count: int = 10000


class CreateGroupsRequest(BaseModel):
    combinations: list[dict[str, Any]]


# ── 카테고리 목록 ──


@router.get("/categories")
async def get_categories():
    """AI 소싱기에서 사용 가능한 대카테고리 + 하위 매핑 반환."""
    return {
        "main": [
            {
                "name": name,
                "musinsa": info["musinsa"],
                "naver": info["naver"],
            }
            for name, info in MAIN_CATEGORIES.items()
        ],
        "musinsa": [
            {"id": code, "name": name} for name, code in MUSINSA_CATEGORIES.items()
        ],
        "naver": [
            {"id": cid, "name": name} for name, cid in NAVER_DL_CATEGORIES.items()
        ],
    }


# ── 엑셀 포함 통합 분석 (SSE) ──


@router.post("/analyze-full")
async def analyze_full(
    month: int = Form(0),
    main_category: str = Form("패션의류"),
    target_count: int = Form(10000),
    file: Optional[UploadFile] = File(None),
):
    """월 + 대카테고리 기반 AI 소싱 분석 (SSE 스트리밍).

    month: 1~12 (작년 해당월 데이터). 0이면 최근 1개월.
    main_category: 패션의류 / 패션잡화 / 스포츠/레저 / 패션전체
    """
    excel_content: bytes | None = None
    if file:
        excel_content = await file.read()

    cat_map = MAIN_CATEGORIES.get(main_category, MAIN_CATEGORIES["패션의류"])
    m_cats = cat_map["musinsa"]
    n_cats = cat_map["naver"]
    month_label = f"작년 {month}월" if month > 0 else "최근 1개월"

    async def stream():
        try:
            # 무신사 쿠키 + 금지어 로드
            async with get_write_session() as session:
                settings_repo = SambaSettingsRepository(session)
                cookie_row = await settings_repo.find_by_async(key="musinsa_cookie")
                musinsa_cookie = cookie_row.value if cookie_row else ""

                word_repo = SambaForbiddenWordRepository(session)
                forbidden_rows = await word_repo.list_active("forbidden")
                forbidden_words = [fw.word for fw in forbidden_rows]

            svc = AISourcingService(musinsa_cookie=musinsa_cookie or "")

            yield _sse(
                "log",
                f"[설정] {main_category} / {month_label} / 목표 {target_count:,}개",
            )
            yield _sse("log", f"[설정] 무신사: {', '.join(m_cats)}")
            yield _sse("log", f"[설정] 네이버: {', '.join(n_cats)}")
            if excel_content:
                yield _sse("log", f"[설정] 엑셀: {len(excel_content):,} bytes")

            # ── 0단계: 확장앱으로 무신사 랭킹 + 검색키워드 수집 ──
            import uuid as _uuid

            # 작년 해당월 date 파라미터 생성
            from datetime import datetime as _dt

            if month > 0:
                rank_date = f"{_dt.now().year - 1}{month:02d}"
            else:
                # 최근 1개월: 이번 달 또는 지난 달
                now = _dt.now()
                prev_month = now.month - 1 if now.month > 1 else 12
                prev_year = now.year if now.month > 1 else now.year - 1
                rank_date = f"{prev_year}{prev_month:02d}"

            yield _sse("log", "")
            yield _sse("log", f"━━ 0/4 확장앱 무신사 데이터 수집 ({rank_date}) ━━")

            # 랭킹 수집 요청
            rank_id = str(_uuid.uuid4())[:8]
            _collect_queue.append(
                {
                    "requestId": rank_id,
                    "type": "ranking",
                    "date": rank_date,
                    "categoryCode": "000",
                }
            )
            _collect_events[rank_id] = _asyncio.Event()
            yield _sse("log", f"[확장앱] 랭킹 아카이브 수집 요청 (date={rank_date})")

            # 검색 키워드 수집 요청
            kw_id = str(_uuid.uuid4())[:8]
            _collect_queue.append(
                {
                    "requestId": kw_id,
                    "type": "keywords",
                }
            )
            _collect_events[kw_id] = _asyncio.Event()
            yield _sse("log", "[확장앱] 인기/급상승 검색어 수집 요청")

            # 결과 대기 (각 최대 45초)
            ranking_items: list[dict] = []
            search_keywords_raw: list[dict] = []

            try:
                yield _sse("log", "[확장앱] 랭킹 데이터 대기 중...")
                await _asyncio.wait_for(_collect_events[rank_id].wait(), timeout=45.0)
                rank_result = _collect_results.pop(rank_id, {})
                rank_data = rank_result.get("data", {})
                ranking_items = rank_data.get("items", [])
                yield _sse(
                    "log", f"[확장앱] 랭킹: {len(ranking_items)}개 상품 수집 완료"
                )
                if ranking_items:
                    for it in ranking_items[:3]:
                        yield _sse(
                            "log",
                            f"  [{it.get('rank')}] {it.get('brand', '')} - {it.get('name', '')[:40]}",
                        )
            except _asyncio.TimeoutError:
                yield _sse(
                    "log",
                    "[확장앱] 랭킹 타임아웃 (확장앱 미연결?) — 기존 방식으로 대체",
                )
            finally:
                _collect_events.pop(rank_id, None)

            try:
                yield _sse("log", "[확장앱] 검색 키워드 대기 중...")
                await _asyncio.wait_for(_collect_events[kw_id].wait(), timeout=45.0)
                kw_result = _collect_results.pop(kw_id, {})
                kw_data = kw_result.get("data", {})
                search_keywords_raw = kw_data.get("keywordItems", [])
                yield _sse(
                    "log",
                    f"[확장앱] 검색 키워드: {len(search_keywords_raw)}개 수집 완료",
                )
                for kw in search_keywords_raw:
                    yield _sse("log", f"  [{kw.get('rank')}] {kw.get('keyword')}")
            except _asyncio.TimeoutError:
                yield _sse("log", "[확장앱] 키워드 타임아웃 — 건너뜀")
            finally:
                _collect_events.pop(kw_id, None)

            # ── 1단계: 랭킹 데이터에서 브랜드×키워드 추출 ──
            yield _sse("log", "")
            yield _sse("log", "━━ 1/4 무신사 인기상품 분석 ━━")

            # 확장앱 랭킹 데이터가 있으면 그것을 사용, 없으면 API 검색 fallback
            if ranking_items:
                musinsa_brands, musinsa_pairs = svc.extract_brands_from_ranking(
                    ranking_items
                )
                yield _sse(
                    "log",
                    f"랭킹에서 {len(musinsa_brands)}개 브랜드, {len(musinsa_pairs)}개 쌍 추출",
                )
            else:
                musinsa_brands, musinsa_pairs = await svc.fetch_musinsa_popular(
                    categories=m_cats
                )
                yield _sse(
                    "log",
                    f"API 검색으로 {len(musinsa_brands)}개 브랜드, {len(musinsa_pairs)}개 쌍 추출",
                )

            top5 = [bs.brand for bs in musinsa_brands[:5]]
            if top5:
                yield _sse("log", f"TOP5 브랜드: {', '.join(top5)}")
            top_pairs = [f"{p.brand}×{p.keyword}" for p in musinsa_pairs[:5]]
            if top_pairs:
                yield _sse("log", f"TOP5 조합: {', '.join(top_pairs)}")

            # 검색어에서 브랜드×키워드 쌍 추가 추출
            if search_keywords_raw:
                from backend.domain.samba.ai_sourcing.service import (
                    BrandKeywordPair,
                    _ALL_CATEGORY_KEYWORDS,
                )

                # 브랜드/키워드 목록은 정적이므로 정렬 결과를 재사용
                _known_brands = [
                    "나이키",
                    "아디다스",
                    "뉴발란스",
                    "푸마",
                    "크록스",
                    "스케쳐스",
                    "반스",
                    "컨버스",
                    "노스페이스",
                    "파타고니아",
                    "아식스",
                    "살로몬",
                    "호카",
                    "MLB",
                    "디스커버리",
                    "내셔널지오그래픽",
                    "무신사스탠다드",
                    "무신사 스탠다드",
                    "탑텐",
                    "스파오",
                    "필라",
                    "리바이스",
                    "칼하트",
                    "커버낫",
                    "마르디메크르디",
                ]
                if not hasattr(analyze_ai_sourcing, "_cached_sorted_brands"):  # noqa: F821
                    analyze_ai_sourcing._cached_sorted_brands = sorted(  # noqa: F821
                        _known_brands, key=len, reverse=True
                    )
                    analyze_ai_sourcing._cached_sorted_kws = sorted(  # noqa: F821
                        _ALL_CATEGORY_KEYWORDS, key=len, reverse=True
                    )
                _sorted_brands = analyze_ai_sourcing._cached_sorted_brands  # noqa: F821
                _sorted_kws = analyze_ai_sourcing._cached_sorted_kws  # noqa: F821
                kw_pairs_added = 0
                for kw_item in search_keywords_raw:
                    kw_text = kw_item.get("keyword", "")
                    rank = kw_item.get("rank", 999)
                    kw_lower = kw_text.lower()
                    for b in _sorted_brands:
                        if b.lower() in kw_lower:
                            remaining = kw_text.replace(b, "").strip()
                            remaining_lower = remaining.lower().replace(" ", "")
                            for cat_kw in _sorted_kws:
                                if cat_kw.lower().replace(" ", "") in remaining_lower:
                                    # musinsa_pairs에 추가
                                    musinsa_pairs.append(
                                        BrandKeywordPair(
                                            brand=b,
                                            keyword=cat_kw,
                                            count=1,
                                            score=max(501 - rank, 1),
                                            source="search_keyword",
                                        )
                                    )
                                    kw_pairs_added += 1
                                    break
                            break
                if kw_pairs_added:
                    yield _sse(
                        "log",
                        f"검색어에서 {kw_pairs_added}개 브랜드×키워드 쌍 추가 추출",
                    )

            # ── 2단계: 네이버 데이터랩 ──
            yield _sse("log", "")
            yield _sse("log", f"━━ 2/4 네이버 데이터랩 ({month_label}) ━━")
            naver_brands: list[Any] = []
            naver_pairs: list[Any] = []
            try:
                naver_brands, naver_pairs = await svc.fetch_naver_datalab(
                    naver_categories=n_cats, month=month
                )
                ntop5 = [bs.brand for bs in naver_brands[:5]]
                yield _sse(
                    "log",
                    f"네이버 {len(naver_brands)}개 브랜드, {len(naver_pairs)}개 브랜드×키워드 쌍 추출",
                )
                if ntop5:
                    yield _sse("log", f"TOP5 브랜드: {', '.join(ntop5)}")
                ntop_pairs = [f"{p.brand}×{p.keyword}" for p in naver_pairs[:5]]
                if ntop_pairs:
                    yield _sse("log", f"TOP5 조합: {', '.join(ntop_pairs)}")
            except Exception as e:
                yield _sse("log", f"네이버 조회 실패 (무시): {e}")

            # ── 2.5단계: 엑셀 분석 ──
            excel_brands = []
            if excel_content:
                yield _sse("log", "")
                month_filter_label = f" ({month}월 필터)" if month > 0 else ""
                yield _sse("log", f"━━ 엑셀 판매이력 분석{month_filter_label} ━━")
                excel_brands = svc.parse_sales_excel(excel_content, month=month)
                etop5 = [f"{bs.brand}({bs.count}건)" for bs in excel_brands[:5]]
                yield _sse(
                    "log",
                    f"엑셀 {len(excel_brands)}개 브랜드 추출 (TOP5: {', '.join(etop5)})",
                )

            # ── 3단계: 브랜드 통합 + IP검증 ──
            yield _sse("log", "")
            yield _sse("log", "━━ 3/4 브랜드 통합 + IP안전 검증 ━━")

            # 한/영 브랜드 정규화 (Nike→나이키, Adidas→아디다스)
            from backend.domain.samba.ai_sourcing.service import normalize_brand

            for bs in musinsa_brands:
                bs.brand = normalize_brand(bs.brand)
            for bs in naver_brands:
                bs.brand = normalize_brand(bs.brand)
            for bs in excel_brands:
                bs.brand = normalize_brand(bs.brand)
            for p in musinsa_pairs:
                p.brand = normalize_brand(p.brand)
            for p in naver_pairs:
                p.brand = normalize_brand(p.brand)

            # 브랜드 통합
            all_brands: dict[str, Any] = {}
            for bs in musinsa_brands:
                if bs.brand in all_brands:
                    existing = all_brands[bs.brand]
                    existing.count += bs.count
                    existing.score += bs.score
                    for kw in bs.keywords:
                        if kw not in existing.keywords:
                            existing.keywords.append(kw)
                else:
                    all_brands[bs.brand] = bs
            for bs in naver_brands:
                if bs.brand in all_brands:
                    existing = all_brands[bs.brand]
                    existing.count += bs.count
                    existing.score += bs.score * 0.5
                    for kw in bs.keywords:
                        if kw not in existing.keywords:
                            existing.keywords.append(kw)
                else:
                    all_brands[bs.brand] = bs
            for bs in excel_brands:
                if bs.brand in all_brands:
                    existing = all_brands[bs.brand]
                    existing.count += bs.count
                    existing.score += bs.score * 2
                    existing.total_sales += bs.total_sales
                    existing.avg_profit_rate = bs.avg_profit_rate
                    for kw in bs.keywords:
                        if kw not in existing.keywords:
                            existing.keywords.append(kw)
                else:
                    bs.score *= 2
                    all_brands[bs.brand] = bs

            # 브랜드×키워드 쌍 통합
            all_pairs: dict[str, Any] = {}
            for p in musinsa_pairs:
                key = f"{p.brand}|{p.keyword}"
                all_pairs[key] = p
            for p in naver_pairs:
                key = f"{p.brand}|{p.keyword}"
                if key in all_pairs:
                    all_pairs[key].count += p.count
                    all_pairs[key].score += p.score * 0.5
                else:
                    all_pairs[key] = p

            sorted_brands = sorted(
                all_brands.values(), key=lambda x: x.score, reverse=True
            )
            brand_names = [bs.brand for bs in sorted_brands]
            safety = await svc.check_brand_safety(brand_names, forbidden_words)
            safe_brands = [
                bs
                for bs in sorted_brands
                if safety.get(bs.brand, {}).get("is_safe", True)
            ]
            unsafe = len(sorted_brands) - len(safe_brands)
            yield _sse(
                "log",
                f"총 {len(sorted_brands)}개 → 안전 {len(safe_brands)}개 / 위험 {unsafe}개",
            )
            yield _sse("log", f"총 브랜드×키워드 쌍: {len(all_pairs)}개")

            # ── 4단계: 조합 생성 — 브랜드×키워드 쌍 기반 ──
            yield _sse("log", "")
            yield _sse(
                "log",
                f"━━ 4/4 조합 생성 ({len(sorted_brands)}개 브랜드, IP위험 포함) ━━",
            )

            # 기존 검색그룹 조회 → 이미 존재하는 조합 제외
            existing_combos: set[str] = set()
            async with get_write_session() as filter_session:
                filter_repo = SambaSearchFilterRepository(filter_session)
                existing_filters = await filter_repo.filter_by_async(limit=10000)
                for f in existing_filters:
                    # 그룹명 패턴: "{소싱처}_{브랜드}_{키워드}" 또는 keyword 필드에서 추출
                    fname = (f.name or "").upper()
                    fkw = (f.keyword or "").lower()
                    existing_combos.add(fname)
                    # keyword 필드: "브랜드 키워드" 형태
                    if fkw:
                        existing_combos.add(fkw)
            if existing_combos:
                yield _sse(
                    "log",
                    f"기존 검색그룹 {len(existing_filters)}개 로드 → 중복 조합 제외",
                )

            pair_list = sorted(all_pairs.values(), key=lambda x: x.score, reverse=True)
            combinations = await svc.generate_combinations(
                sorted_brands,
                pair_list,
                existing_combos=existing_combos,
            )
            for combo in combinations:
                b_safety = safety.get(combo.brand, {})
                combo.is_safe = b_safety.get("is_safe", True)
                combo.safety_reason = b_safety.get("reason", "")

            total_est = sum(c.estimated_count for c in combinations)
            yield _sse(
                "log", f"완료: {len(combinations)}개 그룹, 예상 {total_est:,}개 상품"
            )

            # ── 최종 결과 ──
            result = {
                "brands": [
                    {
                        "brand": bs.brand,
                        "count": bs.count,
                        "score": round(bs.score, 1),
                        "total_sales": bs.total_sales,
                        "avg_profit_rate": round(bs.avg_profit_rate * 100, 1),
                        "categories": getattr(bs, "categories", []),
                        "keywords": getattr(bs, "keywords", []),
                        "source": bs.source,
                        "is_safe": safety.get(bs.brand, {}).get("is_safe", True),
                        "safety_reason": safety.get(bs.brand, {}).get("reason", ""),
                    }
                    for bs in sorted_brands[:100]
                ],
                "combinations": [
                    {
                        "source_site": c.source_site,
                        "brand": c.brand,
                        "keyword": c.keyword,
                        "category": c.category,
                        "category_code": c.category_code,
                        "estimated_count": c.estimated_count,
                        "search_url": c.search_url,
                        "is_safe": c.is_safe,
                        "safety_reason": c.safety_reason,
                    }
                    for c in combinations
                ],
                "summary": {
                    "total_brands_found": len(all_brands),
                    "safe_brands": len(safe_brands),
                    "unsafe_brands": unsafe,
                    "total_combinations": len(combinations),
                    "total_estimated_products": total_est,
                    "total_pairs": len(all_pairs),
                },
                "forbidden_words": forbidden_words,
            }
            yield _sse("result", result)
            yield _sse("done", {"message": "AI 소싱 분석 완료"})

        except Exception as e:
            logger.error(f"[AI소싱] 통합분석 오류: {e}", exc_info=True)
            yield _sse("error", {"message": str(e)})

    return StreamingResponse(stream(), media_type="text/event-stream")


# ── 검색그룹 일괄 생성 ──


@router.post("/create-groups")
async def create_groups(req: CreateGroupsRequest, user_id: str = Depends(get_user_id)):
    """선택된 조합으로 검색그룹(SambaSearchFilter) 일괄 생성."""
    created_ids: list[str] = []

    async with get_write_session() as session:
        repo = SambaSearchFilterRepository(session)

        for combo in req.combinations:
            brand = combo.get("brand", "")
            keyword = combo.get("keyword", "")
            source_site = combo.get("source_site", "MUSINSA")
            estimated_count = combo.get("estimated_count", 100)
            search_url = combo.get("search_url", "")

            # 그룹명: "브랜드_키워드" (사이트 접두사 제거 — 트리에서 source_site로 구분)
            group_name = f"{brand}_{keyword}" if keyword else brand

            # keyword: "브랜드 키워드" 형태로 검색그룹에 저장
            search_keyword = f"{brand} {keyword}" if keyword else brand

            created = await repo.create_async(
                source_site=source_site,
                name=group_name,
                keyword=search_keyword,
                category_filter=search_url,  # 소싱 URL 저장
                requested_count=estimated_count,
                exclude_sold_out=True,
                is_active=True,
                created_by=user_id,
            )
            created_ids.append(created.id)

        await session.commit()

    logger.info(f"[AI소싱] 검색그룹 {len(created_ids)}개 생성 완료")
    return {"created": len(created_ids), "ids": created_ids}


# ── 랭킹 캐시 확인 ──


@router.get("/ranking-cache")
async def get_ranking_cache():
    """DB에 캐싱된 무신사 랭킹 데이터 존재 여부 확인."""
    async with get_write_session() as session:
        settings_repo = SambaSettingsRepository(session)
        cache_row = await settings_repo.find_by_async(key="ai_sourcing_ranking_cache")
        if cache_row and isinstance(cache_row.value, dict):
            data = cache_row.value
            return {
                "success": True,
                "brands": len(data.get("brands", {})),
                "pairs": len(data.get("pairs", {})),
                "keywords": len(data.get("search_keywords", [])),
                "date": data.get("date", ""),
                "collected_at": data.get("collected_at", ""),
            }
    return {"success": False, "brands": 0, "pairs": 0, "keywords": 0}


# ── 확장앱 랭킹 데이터 수신 ──


@router.post("/ranking-data")
async def receive_ranking_data(request: dict[str, Any]):
    """확장앱에서 수집한 무신사 랭킹 + 인기검색어 데이터 수신 → 저장."""
    from backend.domain.samba.ai_sourcing.service import (
        CATEGORY_KEYWORDS,
    )

    date = request.get("date", "")
    ranking_items = request.get("ranking_items", [])
    search_keywords = request.get("search_keywords", {})

    # 카테고리코드 → 카테고리명 매핑
    code_to_name = {v: k for k, v in MUSINSA_CATEGORIES.items()}

    # 모든 카테고리 키워드 통합 (상품명에서 키워드 추출용)
    all_cat_keywords: list[str] = []
    for kws in CATEGORY_KEYWORDS.values():
        all_cat_keywords.extend(kws)
    all_cat_keywords = list(set(all_cat_keywords))

    # 브랜드 집계 + 브랜드×키워드 쌍 추출
    brand_map: dict[str, dict[str, Any]] = {}
    pair_map: dict[str, dict[str, Any]] = {}

    for item in ranking_items:
        if item.get("fallback"):
            continue  # rawText 항목은 스킵

        brand = item.get("brand", "").strip()
        name = item.get("name", "").strip()
        price = item.get("price", 0)
        cat_code = item.get("categoryCode", "000")
        cat_name = code_to_name.get(cat_code, "")

        if not brand:
            continue

        # 브랜드 집계
        if brand not in brand_map:
            brand_map[brand] = {
                "count": 0,
                "total_sales": 0,
                "categories": [],
                "keywords": [],
            }
        bm = brand_map[brand]
        bm["count"] += 1
        bm["total_sales"] += price
        if cat_name and cat_name not in bm["categories"]:
            bm["categories"].append(cat_name)

        # 상품명에서 키워드 추출
        name_lower = name.lower().replace(" ", "")
        found_keywords = []
        for kw in all_cat_keywords:
            if kw.lower().replace(" ", "") in name_lower:
                found_keywords.append(kw)

        for kw in found_keywords:
            if kw not in bm["keywords"]:
                bm["keywords"].append(kw)
            pair_key = f"{brand}|{kw}"
            if pair_key not in pair_map:
                pair_map[pair_key] = {
                    "brand": brand,
                    "keyword": kw,
                    "count": 0,
                    "score": 0,
                }
            pair_map[pair_key]["count"] += 1
            pair_map[pair_key]["score"] += price / 10000

    # 인기검색어에서 브랜드×키워드 쌍 추출
    known_brands = [
        "나이키",
        "아디다스",
        "뉴발란스",
        "푸마",
        "크록스",
        "스케쳐스",
        "반스",
        "컨버스",
        "노스페이스",
        "파타고니아",
        "아식스",
        "살로몬",
        "호카",
        "MLB",
        "디스커버리",
        "내셔널지오그래픽",
        "무신사스탠다드",
        "무신사 스탠다드",
        "탑텐",
        "스파오",
        "필라",
        "리바이스",
        "칼하트",
        "커버낫",
        "마르디메크르디",
    ]

    search_kw_list = []
    for kw_type in ["popular", "trending"]:
        for kw_item in search_keywords.get(kw_type, []):
            kw_text = kw_item.get("keyword", "")
            rank = kw_item.get("rank", 999)
            search_kw_list.append({"keyword": kw_text, "rank": rank, "type": kw_type})

            # 검색어에서 브랜드+키워드 분리
            kw_lower = kw_text.lower()
            for b in sorted(known_brands, key=len, reverse=True):
                if b.lower() in kw_lower:
                    remaining = kw_text.replace(b, "").strip()
                    remaining_lower = remaining.lower().replace(" ", "")
                    for cat_kw in sorted(all_cat_keywords, key=len, reverse=True):
                        if cat_kw.lower().replace(" ", "") in remaining_lower:
                            pair_key = f"{b}|{cat_kw}"
                            if pair_key not in pair_map:
                                pair_map[pair_key] = {
                                    "brand": b,
                                    "keyword": cat_kw,
                                    "count": 0,
                                    "score": 0,
                                }
                            pair_map[pair_key]["count"] += 1
                            pair_map[pair_key]["score"] += max(501 - rank, 1)
                            break
                    break

    # DB에 캐싱 (설정 테이블에 JSON으로 저장)
    async with get_write_session() as session:
        settings_repo = SambaSettingsRepository(session)
        cache_data = {
            "date": date,
            "brands": brand_map,
            "pairs": pair_map,
            "search_keywords": search_kw_list,
            "ranking_count": len(ranking_items),
            "collected_at": __import__("datetime").datetime.now().isoformat(),
        }
        await settings_repo.upsert_async(
            key="ai_sourcing_ranking_cache",
            value=cache_data,
        )
        await session.commit()

    logger.info(
        f"[AI소싱] 랭킹 데이터 수신: {len(ranking_items)}개 상품, "
        f"{len(brand_map)}개 브랜드, {len(pair_map)}개 쌍, "
        f"{len(search_kw_list)}개 검색어"
    )

    return {
        "success": True,
        "brands": len(brand_map),
        "pairs": len(pair_map),
        "search_keywords": len(search_kw_list),
        "ranking_items": len(ranking_items),
    }


# ── 확장앱 큐 기반 수집 (KREAM/소싱 큐와 동일 패턴) ──

import asyncio as _asyncio
from collections import deque as _deque

_collect_queue: _deque[dict[str, Any]] = _deque()
_collect_results: dict[str, dict[str, Any]] = {}
_collect_events: dict[str, _asyncio.Event] = {}


@router.get("/collect-queue")
async def get_collect_queue(request: Request):
    """확장앱 폴링 — 수집 작업 큐."""
    if getattr(request.app.state, "is_shutting_down", False):
        return JSONResponse(
            status_code=503,
            content={"hasJob": False, "shuttingDown": True},
            headers={"Connection": "close"},
        )
    if _collect_queue:
        job = _collect_queue.popleft()
        return {"hasJob": True, **job}
    return {"hasJob": False}


@router.post("/collect-result")
async def receive_collect_result(request: dict[str, Any]):
    """확장앱에서 수집 결과 수신."""
    req_id = request.get("requestId", "")
    _collect_results[req_id] = request
    if req_id in _collect_events:
        _collect_events[req_id].set()

    # 로깅
    job_type = request.get("type", "?")
    error = request.get("error")
    data = request.get("data", {})

    if error:
        logger.info(f"[AI소싱] {job_type} 오류: {error}")
    elif job_type == "ranking":
        items = data.get("items", [])
        debug = data.get("debug", {})
        logger.info(
            f"[AI소싱] 랭킹: {len(items)}개 상품, 링크={debug.get('productLinks', 0)}"
        )
        for it in items[:5]:
            logger.info(
                f"  상품: [{it.get('rank')}] {it.get('brand', '')} - {it.get('name', '')[:40]} ₩{it.get('price', 0)}"
            )
        if not items:
            logger.info(f"  본문: {debug.get('bodyPreview', '')[:800]}")
    elif job_type == "keywords":
        kws = data.get("keywordItems", [])
        logger.info(f"[AI소싱] 키워드: {len(kws)}개")
        for kw in kws:
            logger.info(f"  [{kw.get('rank')}] {kw.get('keyword')}")
        if not kws:
            logger.info(f"  본문: {data.get('debug', {}).get('bodyPreview', '')[:500]}")

    return {"success": True}


@router.post("/test-collect")
async def test_collect(request: dict[str, Any]):
    """테스트용: 큐에 수집 작업을 넣고 결과를 대기."""
    import uuid

    req_id = str(uuid.uuid4())[:8]
    job_type = request.get("type", "ranking")
    job = {
        "requestId": req_id,
        "type": job_type,
        "date": request.get("date", "202503"),
        "categoryCode": request.get("categoryCode", "000"),
    }
    _collect_queue.append(job)
    _collect_events[req_id] = _asyncio.Event()

    logger.info(f"[AI소싱] 테스트 큐 추가: {job_type} (id={req_id})")

    # 최대 60초 대기
    try:
        await _asyncio.wait_for(_collect_events[req_id].wait(), timeout=60.0)
    except _asyncio.TimeoutError:
        return {"success": False, "message": "타임아웃 (60초)", "requestId": req_id}
    finally:
        _collect_events.pop(req_id, None)

    result = _collect_results.pop(req_id, {})
    return {"success": True, "requestId": req_id, **result}


# ── SSE 헬퍼 ──


def _sse(event: str, data: Any) -> str:
    """SSE 포맷 문자열 생성."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"

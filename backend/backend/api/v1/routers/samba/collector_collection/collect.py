"""수집 엔드포인트 — collect_by_url, collect_by_filter, collect_by_keyword, collect_single_musinsa."""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_read_session_dependency, get_write_session_dependency
from backend.domain.samba.collector.grouping import (
    generate_group_key,
    parse_color_from_name,
)
from backend.domain.samba.collector.refresher import _site_intervals
from backend.domain.samba.proxy.musinsa import RateLimitError

from backend.api.v1.routers.samba.collector_common import (
    _invalidate_blacklist_cache,
    _is_blacklisted,
    _build_product_data,
    _trim_history,
    _build_kream_price_snapshot,
    _get_services,
    get_musinsa_cookie,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["samba-collector"])


# ── Inline DTOs ──


class CollectByUrlRequest(BaseModel):
    url: str
    source_site: Optional[str] = None  # auto-detect if not provided


class CollectByKeywordRequest(BaseModel):
    source_site: str = "MUSINSA"
    keyword: str
    page: int = 1
    size: int = 30


class BlockProductRequest(BaseModel):
    product_ids: list[str]


class CollectSingleMusinsaRequest(BaseModel):
    url: str


# ── 블랙리스트 ──


@router.get("/blacklist")
async def get_collection_blacklist(
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """수집 블랙리스트 조회."""
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository

    repo = SambaSettingsRepository(session)
    row = await repo.find_by_async(key="collection_blacklist")
    return row.value if row and isinstance(row.value, list) else []


@router.post("/blacklist/unblock")
async def unblock_products(
    body: BlockProductRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """블랙리스트에서 해제."""
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository

    repo = SambaSettingsRepository(session)
    row = await repo.find_by_async(key="collection_blacklist")
    if not row or not isinstance(row.value, list):
        return {"ok": True, "removed": 0}
    remove_set = set(body.product_ids)  # site_product_id 목록
    before = len(row.value)
    row.value = [b for b in row.value if b.get("site_product_id") not in remove_set]
    session.add(row)
    await session.commit()
    _invalidate_blacklist_cache()
    return {"ok": True, "removed": before - len(row.value)}


# ── 실제 수집 (프록시 통합) ──


@router.post("/collect-by-url", status_code=201)
async def collect_by_url(
    body: CollectByUrlRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """URL로 소싱사이트에서 상품 수집 → DB 저장."""
    from backend.domain.samba.proxy.musinsa import MusinsaClient
    from backend.domain.samba.proxy.kream import KreamClient

    url = body.url.strip()
    site = body.source_site

    # 사이트 자동 감지
    if not site:
        if "musinsa.com" in url:
            site = "MUSINSA"
        elif "kream.co.kr" in url:
            site = "KREAM"
        elif "ssg.com" in url:
            site = "SSG"
        elif "lotteon.com" in url:
            site = "LOTTEON"
        else:
            raise HTTPException(
                400, "지원하지 않는 URL입니다. source_site를 지정해주세요."
            )

    svc = _get_services(session)

    if site == "MUSINSA":
        from urllib.parse import urlparse, parse_qs

        # 무신사 로그인(쿠키) 필수 체크
        cookie_check = await get_musinsa_cookie(session)
        if not cookie_check:
            raise HTTPException(
                400,
                "무신사 수집은 로그인(쿠키)이 필요합니다. "
                "확장앱에서 무신사 로그인 후 다시 시도하세요.",
            )

        parsed = urlparse(url)
        is_search_url = "/search" in parsed.path or "keyword" in parsed.query

        if is_search_url:
            # ── 검색 URL → 키워드 추출 → 검색그룹 자동 생성 → 검색 API → 전체 일괄 저장 ──
            qs = parse_qs(parsed.query)
            keyword = qs.get("keyword", [""])[0]
            if not keyword:
                raise HTTPException(400, "검색 URL에서 키워드를 찾을 수 없습니다")

            # 카테고리 필터 추출
            category_filter = qs.get("category", [""])[0]

            # 검색 필터 파라미터 추출 (브랜드, 가격 범위, 성별 등)
            brand_filter = qs.get("brand", [""])[0]
            min_price_raw = qs.get("minPrice", [""])[0]
            max_price_raw = qs.get("maxPrice", [""])[0]
            gf_filter = qs.get("gf", ["A"])[0]
            min_price = int(min_price_raw) if min_price_raw.isdigit() else None
            max_price = int(max_price_raw) if max_price_raw.isdigit() else None

            # 수집 제외 옵션
            exclude_preorder = qs.get("excludePreorder", [""])[0] == "1"
            exclude_boutique = qs.get("excludeBoutique", [""])[0] == "1"
            # 최대혜택가 사용 여부 (체크 시 cost=bestBenefitPrice, 미체크 시 cost=salePrice)
            use_max_discount = qs.get("maxDiscount", [""])[0] == "1"
            # 품절상품 포함 여부 (체크 시 품절도 수집)
            include_sold_out = qs.get("includeSoldOut", [""])[0] == "1"

            # 검색그룹(SearchFilter) 자동 생성
            requested_count = 100  # 기본값
            search_filter = await svc.create_filter(
                {
                    "source_site": "MUSINSA",
                    "name": keyword,
                    "keyword": url,
                    "category_filter": category_filter or None,
                    "requested_count": requested_count,
                }
            )
            filter_id = search_filter.id

            cookie = await get_musinsa_cookie(session)
            client = MusinsaClient(cookie=cookie)

            # 기존 수집 상품 수 확인
            from backend.domain.samba.collector.model import (
                SambaCollectedProduct as CPModel,
            )

            existing_count = await svc.product_repo.count_async(
                filters={"search_filter_id": filter_id}
            )
            remaining = max(0, requested_count - existing_count)
            if remaining <= 0:
                raise HTTPException(
                    status_code=200,
                    detail=f"이미 {existing_count}개 수집됨 (요청: {requested_count}개)",
                )

            # 필요한 만큼만 검색 (페이지당 100개)

            all_items = []
            max_pages = max(1, (remaining // 100) + 1)
            for page in range(1, min(max_pages + 1, 11)):  # 최대 10페이지
                try:
                    data = await client.search_products(
                        keyword=keyword,
                        page=page,
                        size=100,
                        category=category_filter,
                        brand=brand_filter,
                        min_price=min_price,
                        max_price=max_price,
                        gf=gf_filter,
                    )
                    items = data.get("data", [])
                    if not items:
                        break
                    all_items.extend(items)
                    await asyncio.sleep(
                        _site_intervals.get("MUSINSA", 1.0)
                    )  # 적응형 인터벌
                except Exception:
                    break

            if not all_items:
                raise HTTPException(502, f"'{keyword}' 검색 결과가 없습니다")

            # 기존 상품 ID 일괄 조회 (중복 체크 — 단일 쿼리)
            candidate_ids = [
                str(item.get("siteProductId", item.get("goodsNo", "")))
                for item in all_items
            ]
            existing_stmt = select(CPModel.site_product_id).where(
                CPModel.source_site == "MUSINSA",
                CPModel.site_product_id.in_(candidate_ids),  # type: ignore[union-attr]
            )
            existing_result = await session.execute(existing_stmt)
            existing_ids = {row[0] for row in existing_result.all()}

            # 중복/품절 필터링 → 수집 대상 상품번호 추출
            skipped_sold_out = 0
            collected_sold_out = 0
            targets = []
            for item in all_items:
                if len(targets) >= remaining:
                    break
                site_pid = str(item.get("siteProductId", item.get("goodsNo", "")))
                if site_pid in existing_ids:
                    continue
                if item.get("isSoldOut", False):
                    if not include_sold_out:
                        skipped_sold_out += 1
                        continue
                    collected_sold_out += 1
                targets.append(site_pid)

            # 각 상품 상세 수집 → 배치 저장 (10건씩 flush)
            saved = 0
            skipped_preorder = 0
            skipped_boutique = 0
            _batch_buf: list[dict] = []
            _BATCH_SIZE = 10

            async def _flush_batch() -> int:
                """버퍼에 쌓인 상품을 한번에 DB 저장."""
                if not _batch_buf:
                    return 0
                cnt = await svc.bulk_create_products(list(_batch_buf))
                _batch_buf.clear()
                return cnt

            for goods_no in targets:
                # 블랙리스트 체크
                if await _is_blacklisted(session, "MUSINSA", goods_no):
                    logger.info(f"[수집] 블랙리스트 스킵: MUSINSA/{goods_no}")
                    continue
                try:
                    detail = await client.get_goods_detail(goods_no)
                    if not detail or not detail.get("name"):
                        await asyncio.sleep(_site_intervals.get("MUSINSA", 1.0))
                        continue
                    # 긴 상세이미지 분할 (추가이미지 보충분)
                    orig_cnt = detail.get(
                        "originalImageCount", len(detail.get("images", []))
                    )
                    if orig_cnt < len(detail.get("images", [])):
                        from backend.domain.samba.image.service import split_long_images

                        detail["images"] = await split_long_images(
                            detail["images"], orig_cnt, session
                        )

                    if exclude_preorder and detail.get("saleStatus") == "preorder":
                        skipped_preorder += 1
                        await asyncio.sleep(_site_intervals.get("MUSINSA", 1.0))
                        continue
                    if exclude_boutique and detail.get("isBoutique"):
                        skipped_boutique += 1
                        await asyncio.sleep(_site_intervals.get("MUSINSA", 1.0))
                        continue

                    # 최대혜택가 체크(use_max_discount=True) 시 bestBenefitPrice 추출 실패
                    # → cost=None (정가 폴백 금지). 다음 사이클 정상 수집 시 채움.
                    # 미체크 시는 사용자가 명시적으로 salePrice를 cost로 선택한 모드.
                    if use_max_discount:
                        _raw_cost = detail.get("bestBenefitPrice")
                        new_cost = (
                            _raw_cost
                            if (_raw_cost is not None and _raw_cost > 0)
                            else None
                        )
                    else:
                        new_cost = detail.get("salePrice") or 0

                    raw_cat = detail.get("category", "") or ""
                    cat_parts = (
                        [c.strip() for c in raw_cat.split(">") if c.strip()]
                        if raw_cat
                        else []
                    )
                    _sale_price = detail.get("salePrice", 0)
                    _original_price = detail.get("originalPrice", 0)

                    raw_detail_html = detail.get("detailHtml", "")
                    if not raw_detail_html:
                        detail_imgs = detail.get("detailImages") or []
                        if detail_imgs:
                            raw_detail_html = "\n".join(
                                f'<div style="text-align:center;"><img src="{img}" style="max-width:860px;width:100%;" /></div>'
                                for img in detail_imgs
                            )

                    product_data = _build_product_data(
                        detail,
                        goods_no,
                        filter_id,
                        "MUSINSA",
                        new_cost,
                        _sale_price,
                        _original_price,
                        raw_cat,
                        cat_parts,
                        raw_detail_html,
                    )
                    await svc._inherit_group_attributes(product_data)
                    _batch_buf.append(svc.prepare_product_data(product_data))
                    saved += 1
                    if len(_batch_buf) >= _BATCH_SIZE:
                        await _flush_batch()
                except RateLimitError:
                    logger.warning(
                        f"[무신사] 요청 제한 감지 — 수집 중단 (수집완료: {saved}/{len(targets)})"
                    )
                    break
                except Exception as e:
                    logger.warning(f"[수집 실패] {goods_no}: {e}")
                await asyncio.sleep(_site_intervals.get("MUSINSA", 1.0))

            # 잔여 버퍼 flush
            await _flush_batch()

            # 검색그룹에 최근수집일 업데이트
            await svc.update_filter(
                filter_id,
                {
                    "last_collected_at": datetime.now(timezone.utc),
                },
            )

            return {
                "type": "search",
                "keyword": keyword,
                "filter_id": filter_id,
                "filter_name": keyword,
                "total_found": len(all_items),
                "saved": saved,
                "enriched": saved,
                "skipped_duplicates": len(all_items) - len(targets) - skipped_sold_out,
                "skipped_sold_out": skipped_sold_out,
                "skipped_preorder": skipped_preorder,
                "skipped_boutique": skipped_boutique,
                "in_stock_count": saved - collected_sold_out,
                "sold_out_count": collected_sold_out,
            }

        else:
            # ── 단일 상품 URL → 상품번호 추출 → 상세 API ──
            match = (
                re.search(r"/products/(\d+)", url)
                or re.search(r"goodsNo=(\d+)", url)
                or re.search(r"/(\d+)", url)
            )
            if not match:
                raise HTTPException(
                    400, "무신사 상품 URL에서 상품번호를 찾을 수 없습니다"
                )
            goods_no = match.group(1)

            # 블랙리스트 체크 — 수집차단된 상품 스킵
            if await _is_blacklisted(session, "MUSINSA", goods_no):
                raise HTTPException(400, f"수집차단된 상품입니다 ({goods_no})")

            cookie = await get_musinsa_cookie(session)
            client = MusinsaClient(cookie=cookie)
            data = await client.get_goods_detail(goods_no)
            if not data or not data.get("name"):
                raise HTTPException(502, "무신사 상품 조회 실패")
            # 긴 상세이미지 분할 (추가이미지 보충분)
            orig_cnt = data.get("originalImageCount", len(data.get("images", [])))
            if orig_cnt < len(data.get("images", [])):
                from backend.domain.samba.image.service import split_long_images

                data["images"] = await split_long_images(
                    data["images"], orig_cnt, session
                )

            # 가격이력 초기 스냅샷
            initial_snapshot = {
                "date": datetime.now(timezone.utc).isoformat(),
                "sale_price": data.get("salePrice", 0),
                "original_price": data.get("originalPrice", 0),
                "options": data.get("options", []),
            }
            sale_status = data.get("saleStatus", "in_stock")
            # 상세 HTML: 수집 데이터의 detailHtml 사용
            raw_detail_html = data.get("detailHtml", "")
            if not raw_detail_html:
                # 상세 이미지가 있으면 이미지로 HTML 생성
                detail_imgs = data.get("detailImages") or []
                if detail_imgs:
                    raw_detail_html = "\n".join(
                        f'<div style="text-align:center;"><img src="{img}" style="max-width:860px;width:100%;" /></div>'
                        for img in detail_imgs
                    )

            # 중복 체크: 기존 상품이 있으면 업데이트 (upsert)
            from backend.domain.samba.collector.model import (
                SambaCollectedProduct as CPModel,
            )

            existing_stmt = select(CPModel).where(
                CPModel.source_site == "MUSINSA",
                CPModel.site_product_id == goods_no,
            )
            existing_row = (await session.execute(existing_stmt)).scalar_one_or_none()

            # 그룹상품용 similarNo 추출
            similar_no = str(data.get("similarNo", "0"))

            product_data = {
                "source_site": "MUSINSA",
                "site_product_id": goods_no,
                "source_url": data.get("sourceUrl", "") or url,
                "name": data.get("name", ""),
                "brand": data.get("brand", ""),
                "original_price": data.get("originalPrice", 0),
                "sale_price": data.get("salePrice", 0),
                "cost": data.get("bestBenefitPrice") or None,
                "images": data.get("images", []),
                "detail_images": data.get("detailImages") or [],
                "options": data.get("options", []),
                "addon_options": data.get("addonOptions") or None,
                "option_group_names": data.get("optionGroupNames") or None,
                "category": data.get("category", ""),
                "category1": data.get("category1", ""),
                "category2": data.get("category2", ""),
                "category3": data.get("category3", ""),
                "category4": data.get("category4", ""),
                "manufacturer": data.get("manufacturer", "") or data.get("brand", ""),
                "origin": data.get("origin", ""),
                "material": data.get("material", ""),
                "color": data.get("color", "")
                or parse_color_from_name(data.get("name", "")),
                "sex": data.get("sex", "") or "남녀공용",
                "season": data.get("season", "") or "사계절",
                "care_instructions": data.get("care_instructions", ""),
                "quality_guarantee": data.get("quality_guarantee", ""),
                "similar_no": similar_no,
                "style_code": data.get("styleNo", "") or data.get("style_code", ""),
                "group_key": generate_group_key(
                    brand=data.get("brand", ""),
                    similar_no=similar_no,
                    style_code=data.get("styleNo", "") or data.get("style_code", ""),
                    name=data.get("name", ""),
                ),
                "detail_html": raw_detail_html,
                "status": "collected",
                "sale_status": sale_status,
                "free_shipping": data.get("freeShipping", False),
                "same_day_delivery": data.get("sameDayDelivery", False),
                "is_point_restricted": data.get("isPointRestricted"),
                "price_history": [initial_snapshot],
            }

            if existing_row:
                # 기존 상품 → 가격이력 누적 후 업데이트
                history = list(existing_row.price_history or [])
                history.insert(0, initial_snapshot)
                product_data["price_history"] = _trim_history(history)
                # 재수집 시 기존 태그 보존 (확장앱은 tags를 보내지 않음)
                if "tags" not in product_data or not product_data.get("tags"):
                    product_data.pop("tags", None)
                collected = await svc.update_collected_product(
                    existing_row.id, product_data
                )
                return {
                    "type": "single",
                    "saved": 1,
                    "updated": True,
                    "product": collected,
                }
            else:
                collected = await svc.create_collected_product(product_data)
                return {"type": "single", "saved": 1, "product": collected}

    elif site == "KREAM":
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(url)
        is_search_url = "/search" in parsed.path or "keyword" in parsed.query

        if is_search_url:
            qs = parse_qs(parsed.query)
            keyword = qs.get("keyword", qs.get("tab", [""]))[0]
            if not keyword:
                raise HTTPException(400, "KREAM 검색 URL에서 키워드를 찾을 수 없습니다")

            # 검색그룹(SearchFilter) 자동 생성
            search_filter = await svc.create_filter(
                {
                    "source_site": "KREAM",
                    "name": keyword,
                    "keyword": url,
                }
            )
            filter_id = search_filter.id

            client = KreamClient()
            try:
                items = await client.search(keyword, 100)
            except Exception as e:
                raise HTTPException(
                    504,
                    f"KREAM 검색 타임아웃: {str(e)}. "
                    "웨일 브라우저 확장앱이 실행 중인지 확인하세요.",
                )

            if not items:
                raise HTTPException(502, f"'{keyword}' 검색 결과가 없습니다")

            items_list = items if isinstance(items, list) else []

            # 기존 상품 ID 일괄 조회
            from backend.domain.samba.collector.model import (
                SambaCollectedProduct as CPModel,
            )

            candidate_ids = [
                str(item.get("siteProductId") or item.get("id") or "")
                for item in items_list
            ]
            existing_stmt = select(CPModel.site_product_id).where(
                CPModel.source_site == "KREAM",
                CPModel.site_product_id.in_(candidate_ids),  # type: ignore[union-attr]
            )
            existing_result = await session.execute(existing_stmt)
            existing_ids = {row[0] for row in existing_result.all()}

            bulk_items = []
            for item in items_list:
                # 확장앱 검색결과: siteProductId / id 둘 다 지원
                site_pid = str(item.get("siteProductId") or item.get("id") or "")
                if not site_pid or site_pid in existing_ids:
                    continue
                bulk_items.append(
                    {
                        "source_site": "KREAM",
                        "site_product_id": site_pid,
                        "search_filter_id": filter_id,
                        "name": item.get("name", ""),
                        "brand": item.get("brand", ""),
                        "original_price": item.get(
                            "originalPrice", item.get("retailPrice", 0)
                        ),
                        "sale_price": item.get("salePrice", item.get("retailPrice", 0)),
                        "images": item.get("images", [item.get("imageUrl", "")])
                        if (item.get("images") or item.get("imageUrl"))
                        else [],
                        "similar_no": None,
                        "group_key": generate_group_key(
                            brand=item.get("brand", ""),
                            similar_no=None,
                            style_code=item.get("styleCode", ""),
                            name=item.get("name", ""),
                        ),
                        "status": "collected",
                    }
                )

            created_count = 0
            if bulk_items:
                created_count = await svc.bulk_create_products(bulk_items)

            # 검색그룹에 최근수집일 업데이트
            await svc.update_filter(
                filter_id,
                {
                    "last_collected_at": datetime.now(timezone.utc),
                },
            )

            return {
                "type": "search",
                "keyword": keyword,
                "filter_id": filter_id,
                "filter_name": keyword,
                "total_found": len(items_list),
                "saved": created_count,
                "skipped_duplicates": len(items_list) - created_count,
            }

        else:
            match = re.search(r"/products/(\d+)", url)
            if not match:
                raise HTTPException(
                    400, "KREAM 상품 URL에서 상품번호를 찾을 수 없습니다"
                )
            product_id = match.group(1)

            client = KreamClient()
            try:
                data = await client.get_product(product_id)
            except Exception as e:
                raise HTTPException(
                    504,
                    f"KREAM 상품 조회 타임아웃: {str(e)}. "
                    "웨일 브라우저 확장앱이 실행 중인지 확인하세요.",
                )

            if not data:
                raise HTTPException(502, "KREAM 상품 조회 실패")

            # 확장앱 수집 결과: { success, product: { ... } }
            product_data = data.get("product", data)

            _sp = product_data.get("salePrice", product_data.get("retailPrice", 0))
            _op = product_data.get("originalPrice", product_data.get("retailPrice", 0))
            _opts = product_data.get("options", [])
            _snapshot = _build_kream_price_snapshot(_sp, _op, _sp, _opts)

            # 중복 체크: 기존 상품이 있으면 업데이트 (upsert)
            from backend.domain.samba.collector.model import (
                SambaCollectedProduct as CPModel,
            )

            existing_stmt = select(CPModel).where(
                CPModel.source_site == "KREAM",
                CPModel.site_product_id == product_id,
            )
            existing_row = (await session.execute(existing_stmt)).scalar_one_or_none()

            kream_product_data = {
                "source_site": "KREAM",
                "site_product_id": product_id,
                "name": product_data.get("name", ""),
                "brand": product_data.get("brand", ""),
                "original_price": _op,
                "sale_price": _sp,
                "images": product_data.get("images", []),
                "options": _opts,
                "category": product_data.get("category", ""),
                "category1": product_data.get("category1", ""),
                "category2": product_data.get("category2", ""),
                "category3": product_data.get("category3", ""),
                "similar_no": None,
                "color": parse_color_from_name(product_data.get("name", "")),
                "group_key": generate_group_key(
                    brand=product_data.get("brand", ""),
                    similar_no=None,
                    style_code=product_data.get("styleCode", ""),
                    name=product_data.get("name", ""),
                ),
                "status": "collected",
                "price_history": [_snapshot],
            }

            if existing_row:
                # 기존 상품 → 가격이력 누적 후 업데이트
                history = list(existing_row.price_history or [])
                history.insert(0, _snapshot)
                kream_product_data["price_history"] = _trim_history(history)
                # 재수집 시 기존 태그 보존
                if "tags" not in kream_product_data or not kream_product_data.get(
                    "tags"
                ):
                    kream_product_data.pop("tags", None)
                collected = await svc.update_collected_product(
                    existing_row.id, kream_product_data
                )
                return {
                    "type": "single",
                    "saved": 1,
                    "updated": True,
                    "product": collected,
                }
            else:
                collected = await svc.create_collected_product(kream_product_data)
                return {"type": "single", "saved": 1, "product": collected}

    # ── SSG 수집 ──
    elif site == "SSG":
        from urllib.parse import urlparse, parse_qs
        from backend.domain.samba.proxy.ssg_sourcing import SSGSourcingClient

        if "dealItemView" in url:
            raise HTTPException(
                400,
                "모음전(기획전) 상품은 수집할 수 없습니다. 개별 상품 URL을 입력해주세요.",
            )

        parsed = urlparse(url)
        is_search_url = "/search" in parsed.path or "query" in parsed.query

        if is_search_url:
            qs = parse_qs(parsed.query)
            keyword = qs.get("query", [""])[0]
            if not keyword:
                raise HTTPException(400, "검색 URL에서 키워드를 찾을 수 없습니다")

            use_max_discount = qs.get("maxDiscount", [""])[0] == "1"
            include_sold_out = qs.get("includeSoldOut", [""])[0] == "1"

            # 검색그룹 자동 생성
            search_filter = await svc.create_filter(
                {
                    "source_site": "SSG",
                    "name": keyword,
                    "keyword": url,
                    "requested_count": 100,
                }
            )
            filter_id = search_filter.id

            client = SSGSourcingClient()

            # 기존 수집 수 확인
            from backend.domain.samba.collector.model import (
                SambaCollectedProduct as CPModel,
            )

            existing_count = await svc.product_repo.count_async(
                filters={"search_filter_id": filter_id}
            )
            remaining = max(0, 100 - existing_count)
            if remaining <= 0:
                return {
                    "type": "search",
                    "keyword": keyword,
                    "filter_id": filter_id,
                    "message": f"이미 {existing_count}개 수집됨",
                    "saved": 0,
                    "enriched": 0,
                }

            # 검색

            all_items = []
            max_pages = max(1, (remaining // 40) + 1)
            for page in range(1, min(max_pages + 1, 11)):
                try:
                    items = await client.search_products(
                        keyword=keyword, page=page, size=40
                    )
                    if not items:
                        break
                    all_items.extend(items)
                    await asyncio.sleep(_site_intervals.get("SSG", 1.0))
                except Exception:
                    break

            if not all_items:
                raise HTTPException(502, f"'{keyword}' 검색 결과가 없습니다")

            # 중복 필터
            candidate_ids = [
                str(item.get("siteProductId", item.get("goodsNo", "")))
                for item in all_items
            ]
            existing_stmt = select(CPModel.site_product_id).where(
                CPModel.source_site == "SSG",
                CPModel.site_product_id.in_(candidate_ids),
            )
            existing_result = await session.execute(existing_stmt)
            existing_ids = {row[0] for row in existing_result.all()}

            targets = []
            skipped_sold_out = 0
            collected_sold_out = 0
            for item in all_items:
                if len(targets) >= remaining:
                    break
                site_pid = str(item.get("siteProductId", item.get("goodsNo", "")))
                if site_pid in existing_ids:
                    continue
                if item.get("isSoldOut", False):
                    if not include_sold_out:
                        skipped_sold_out += 1
                        continue
                    collected_sold_out += 1
                targets.append(site_pid)

            # 상세 수집 + 배치 저장
            saved = 0
            _batch_buf: list[dict] = []
            _BATCH_SIZE = 10

            async def _flush_batch_ssg() -> int:
                if not _batch_buf:
                    return 0
                cnt = await svc.bulk_create_products(list(_batch_buf))
                _batch_buf.clear()
                return cnt

            for item_id in targets:
                try:
                    detail = await client.get_product_detail(item_id)
                    if not detail or not detail.get("name"):
                        await asyncio.sleep(_site_intervals.get("SSG", 1.0))
                        continue

                    # 최대혜택가 체크(use_max_discount=True) 시 bestBenefitPrice 추출 실패
                    # → cost=None (정가 폴백 금지). 다음 사이클 정상 수집 시 채움.
                    # 미체크 시는 사용자가 명시적으로 salePrice를 cost로 선택한 모드.
                    if use_max_discount:
                        _raw_cost = detail.get("bestBenefitPrice")
                        new_cost = (
                            _raw_cost
                            if (_raw_cost is not None and _raw_cost > 0)
                            else None
                        )
                    else:
                        new_cost = detail.get("salePrice") or 0

                    raw_cat = detail.get("category", "") or ""
                    cat_parts = (
                        [c.strip() for c in raw_cat.split(">") if c.strip()]
                        if raw_cat
                        else []
                    )
                    _sale_price = detail.get("salePrice", 0)
                    _original_price = detail.get("originalPrice", 0)

                    raw_detail_html = ""
                    detail_imgs = detail.get("detailImages") or []
                    if detail_imgs:
                        raw_detail_html = "\n".join(
                            f'<div style="text-align:center;"><img src="{img}" style="max-width:860px;width:100%;" /></div>'
                            for img in detail_imgs
                        )

                    product_data = _build_product_data(
                        detail,
                        item_id,
                        filter_id,
                        "SSG",
                        new_cost,
                        _sale_price,
                        _original_price,
                        raw_cat,
                        cat_parts,
                        raw_detail_html,
                    )
                    await svc._inherit_group_attributes(product_data)
                    _batch_buf.append(svc.prepare_product_data(product_data))
                    saved += 1
                    if len(_batch_buf) >= _BATCH_SIZE:
                        await _flush_batch_ssg()
                except Exception as e:
                    logger.warning(f"[SSG 수집 실패] {item_id}: {e}")
                await asyncio.sleep(_site_intervals.get("SSG", 1.0))

            await _flush_batch_ssg()
            await svc.update_filter(
                filter_id, {"last_collected_at": datetime.now(timezone.utc)}
            )

            return {
                "type": "search",
                "keyword": keyword,
                "filter_id": filter_id,
                "total_found": len(all_items),
                "saved": saved,
                "enriched": saved,
                "skipped_sold_out": skipped_sold_out,
                "in_stock_count": saved - collected_sold_out,
                "sold_out_count": collected_sold_out,
            }

        else:
            # 단일 상품 URL
            match = re.search(r"itemId=(\d+)", url) or re.search(r"/item/(\d+)", url)
            if not match:
                raise HTTPException(400, "SSG 상품 URL에서 상품번호를 찾을 수 없습니다")
            item_id = match.group(1)

            client = SSGSourcingClient()
            data = await client.get_product_detail(item_id)
            if not data or not data.get("name"):
                raise HTTPException(502, "SSG 상품 조회 실패")

            initial_snapshot = {
                "date": datetime.now(timezone.utc).isoformat(),
                "sale_price": data.get("salePrice", 0),
                "original_price": data.get("originalPrice", 0),
                "options": data.get("options", []),
            }
            sale_status = data.get("saleStatus", "in_stock")
            raw_detail_html = ""
            detail_imgs = data.get("detailImages") or []
            if detail_imgs:
                raw_detail_html = "\n".join(
                    f'<div style="text-align:center;"><img src="{img}" style="max-width:860px;width:100%;" /></div>'
                    for img in detail_imgs
                )

            from backend.domain.samba.collector.model import (
                SambaCollectedProduct as CPModel,
            )

            existing_stmt = select(CPModel).where(
                CPModel.source_site == "SSG",
                CPModel.site_product_id == item_id,
            )
            existing_row = (await session.execute(existing_stmt)).scalar_one_or_none()

            product_data = {
                "source_site": "SSG",
                "site_product_id": item_id,
                "source_url": data.get("sourceUrl", "") or url,
                "name": data.get("name", ""),
                "brand": data.get("brand", ""),
                "original_price": data.get("originalPrice", 0),
                "sale_price": data.get("salePrice", 0),
                "cost": data.get("bestBenefitPrice") or None,
                "images": data.get("images", []),
                "detail_images": data.get("detailImages") or [],
                "options": data.get("options", []),
                "addon_options": data.get("addonOptions") or None,
                "option_group_names": data.get("optionGroupNames") or None,
                "category": data.get("category", ""),
                "category1": data.get("category1", ""),
                "category2": data.get("category2", ""),
                "category3": data.get("category3", ""),
                "category4": data.get("category4", ""),
                "manufacturer": data.get("manufacturer", "") or data.get("brand", ""),
                "origin": data.get("origin", ""),
                "material": data.get("material", ""),
                "color": data.get("color", "")
                or parse_color_from_name(data.get("name", "")),
                "sex": data.get("sex", "") or "남녀공용",
                "season": data.get("season", "") or "사계절",
                "care_instructions": data.get("care_instructions", ""),
                "quality_guarantee": data.get("quality_guarantee", ""),
                "similar_no": str(data.get("similarNo", "0")),
                "style_code": data.get("styleNo", "") or data.get("style_code", ""),
                "detail_html": raw_detail_html,
                "status": "collected",
                "sale_status": sale_status,
                "free_shipping": data.get("freeShipping", False),
                "same_day_delivery": data.get("sameDayDelivery", False),
                "price_history": [initial_snapshot],
            }

            if existing_row:
                history = list(existing_row.price_history or [])
                history.insert(0, initial_snapshot)
                product_data["price_history"] = _trim_history(history)
                if "tags" not in product_data or not product_data.get("tags"):
                    product_data.pop("tags", None)
                collected = await svc.update_collected_product(
                    existing_row.id, product_data
                )
                return {
                    "type": "single",
                    "saved": 1,
                    "updated": True,
                    "product": collected,
                }
            else:
                collected = await svc.create_collected_product(product_data)
                return {"type": "single", "saved": 1, "product": collected}

    # ── 롯데ON 수집 ──
    elif site == "LOTTEON":
        from urllib.parse import urlparse, parse_qs
        from backend.domain.samba.proxy.lotteon_sourcing import LotteonSourcingClient

        parsed = urlparse(url)
        is_search_url = "/search/" in parsed.path or "q=" in parsed.query

        if is_search_url:
            qs = parse_qs(parsed.query)
            keyword = qs.get("q", [""])[0]
            if not keyword:
                raise HTTPException(400, "검색 URL에서 키워드를 찾을 수 없습니다")

            use_max_discount = qs.get("maxDiscount", [""])[0] == "1"
            include_sold_out = qs.get("includeSoldOut", [""])[0] == "1"

            # 검색그룹 자동 생성
            search_filter = await svc.create_filter(
                {
                    "source_site": "LOTTEON",
                    "name": keyword,
                    "keyword": url,
                    "requested_count": 100,
                }
            )
            filter_id = search_filter.id

            from backend.domain.samba.collector.refresher import (
                get_collect_proxy_url as _get_collect_proxy_url_lot,
            )

            client = LotteonSourcingClient(proxy_url=_get_collect_proxy_url_lot())

            # 기존 수집 수 확인
            from backend.domain.samba.collector.model import (
                SambaCollectedProduct as CPModel,
            )

            existing_count = await svc.product_repo.count_async(
                filters={"search_filter_id": filter_id}
            )
            remaining = max(0, 100 - existing_count)
            if remaining <= 0:
                return {
                    "type": "search",
                    "keyword": keyword,
                    "filter_id": filter_id,
                    "message": f"이미 {existing_count}개 수집됨",
                    "saved": 0,
                    "enriched": 0,
                }

            # 검색

            all_items = []
            max_pages = max(1, (remaining // 40) + 1)
            for page in range(1, min(max_pages + 1, 11)):
                try:
                    items = await client.search_products(
                        keyword=keyword, page=page, size=40
                    )
                    if not items:
                        break
                    all_items.extend(items)
                    await asyncio.sleep(_site_intervals.get("LOTTEON", 0.5))
                except Exception:
                    break

            if not all_items:
                raise HTTPException(502, f"'{keyword}' 검색 결과가 없습니다")

            # 중복 필터
            candidate_ids = [
                str(item.get("siteProductId", item.get("goodsNo", "")))
                for item in all_items
            ]
            existing_stmt = select(CPModel.site_product_id).where(
                CPModel.source_site == "LOTTEON",
                CPModel.site_product_id.in_(candidate_ids),
            )
            existing_result = await session.execute(existing_stmt)
            existing_ids = {row[0] for row in existing_result.all()}

            targets = []
            skipped_sold_out = 0
            collected_sold_out = 0
            for item in all_items:
                if len(targets) >= remaining:
                    break
                site_pid = str(item.get("siteProductId", item.get("goodsNo", "")))
                if site_pid in existing_ids:
                    continue
                if item.get("isSoldOut", False):
                    if not include_sold_out:
                        skipped_sold_out += 1
                        continue
                    collected_sold_out += 1
                targets.append(site_pid)

            # 상세 수집 + 배치 저장
            saved = 0
            _batch_buf_lt: list[dict] = []
            _BATCH_SIZE = 10

            async def _flush_batch_lt() -> int:
                if not _batch_buf_lt:
                    return 0
                cnt = await svc.bulk_create_products(list(_batch_buf_lt))
                _batch_buf_lt.clear()
                return cnt

            for item_id in targets:
                try:
                    detail = await client.get_product_detail(item_id)
                    if not detail or not detail.get("name"):
                        await asyncio.sleep(_site_intervals.get("LOTTEON", 0.5))
                        continue

                    _sale_price = detail.get("salePrice", 0)

                    # qapi 프로모션가 보정 (pbf slPrc는 정가 → qapi final이 실제 판매가)
                    try:
                        _qapi_price = await client.fetch_qapi_price(item_id)
                        if _qapi_price:
                            _qapi_final = _qapi_price.get("final", 0)
                            if _qapi_final > 0 and _qapi_final < _sale_price:
                                logger.info(
                                    f"[LOTTEON] 수집 qapi 보정: {item_id} "
                                    f"{_sale_price:,} → {_qapi_final:,}"
                                )
                                _sale_price = _qapi_final
                                detail["salePrice"] = _qapi_final
                                _bbp = detail.get("bestBenefitPrice", 0)
                                if not _bbp or _bbp >= _sale_price:
                                    detail["bestBenefitPrice"] = _qapi_final
                    except Exception as _qe:
                        logger.debug(
                            f"[LOTTEON] 수집 qapi 보정 실패: {item_id} — {_qe}"
                        )

                    if use_max_discount:
                        # 확장앱 DOM에서 실제 "나의 혜택가" 수집
                        # 추출 실패 시 cost=None (정가 폴백 금지). 다음 사이클 정상 수집 시 채움.
                        new_cost = None
                        try:
                            from backend.domain.samba.proxy.sourcing_queue import (
                                SourcingQueue,
                            )

                            _req_id, _future = SourcingQueue.add_detail_job(
                                "LOTTEON",
                                item_id,
                                sitm_no=detail.get("sitmNo", ""),
                            )
                            _ext_result = await asyncio.wait_for(_future, timeout=25)
                            if isinstance(_ext_result, dict) and _ext_result.get(
                                "success"
                            ):
                                _ext_benefit = int(
                                    _ext_result.get("best_benefit_price", 0) or 0
                                )
                                if _ext_benefit > 0:
                                    new_cost = _ext_benefit
                                    logger.info(
                                        f"[LOTTEON] 수집 확장앱 혜택가: {item_id} → {_ext_benefit:,}"
                                    )
                        except asyncio.TimeoutError:
                            logger.info(
                                f"[LOTTEON] 수집 확장앱 타임아웃: {item_id} — cost=None 저장 (다음 사이클 채움)"
                            )
                        except Exception as _ext_err:
                            logger.debug(
                                f"[LOTTEON] 수집 확장앱 실패: {item_id} — {_ext_err}"
                            )
                    else:
                        new_cost = detail.get("salePrice") or 0

                    raw_cat = detail.get("category", "") or ""
                    cat_parts = (
                        [c.strip() for c in raw_cat.split(">") if c.strip()]
                        if raw_cat
                        else []
                    )
                    _original_price = detail.get("originalPrice", 0)

                    raw_detail_html = ""
                    detail_imgs = detail.get("detailImages") or []
                    if detail_imgs:
                        raw_detail_html = "\n".join(
                            f'<div style="text-align:center;"><img src="{img}" style="max-width:860px;width:100%;" /></div>'
                            for img in detail_imgs
                        )

                    product_data = _build_product_data(
                        detail,
                        item_id,
                        filter_id,
                        "LOTTEON",
                        new_cost,
                        _sale_price,
                        _original_price,
                        raw_cat,
                        cat_parts,
                        raw_detail_html,
                    )
                    await svc._inherit_group_attributes(product_data)
                    _batch_buf_lt.append(svc.prepare_product_data(product_data))
                    saved += 1
                    if len(_batch_buf_lt) >= _BATCH_SIZE:
                        await _flush_batch_lt()
                except Exception as e:
                    logger.warning(f"[LOTTEON 수집 실패] {item_id}: {e}")
                await asyncio.sleep(_site_intervals.get("LOTTEON", 0.5))

            await _flush_batch_lt()
            await svc.update_filter(
                filter_id, {"last_collected_at": datetime.now(timezone.utc)}
            )

            return {
                "type": "search",
                "keyword": keyword,
                "filter_id": filter_id,
                "total_found": len(all_items),
                "saved": saved,
                "enriched": saved,
                "skipped_sold_out": skipped_sold_out,
                "in_stock_count": saved - collected_sold_out,
                "sold_out_count": collected_sold_out,
            }

        else:
            # 단일 상품 URL
            match = re.search(r"/product/(LO\d+)", url) or re.search(
                r"/product/(\d+)", url
            )
            if not match:
                raise HTTPException(
                    400, "롯데ON 상품 URL에서 상품번호를 찾을 수 없습니다"
                )
            item_id = match.group(1)

            use_max_discount = False  # 단일 수집 시 기본값

            from backend.domain.samba.collector.refresher import (
                get_collect_proxy_url as _get_collect_proxy_url_lot,
            )

            client = LotteonSourcingClient(proxy_url=_get_collect_proxy_url_lot())
            data = await client.get_product_detail(item_id)
            if not data or not data.get("name"):
                raise HTTPException(502, "롯데ON 상품 조회 실패")

            # 최대혜택가: 확장앱 DOM 파싱으로 실제 혜택가 수집
            _sale_price = data.get("salePrice", 0)

            # qapi 프로모션가 보정 (pbf slPrc는 정가 → qapi final이 실제 판매가)
            try:
                _qapi_price = await client.fetch_qapi_price(item_id)
                if _qapi_price:
                    _qapi_final = _qapi_price.get("final", 0)
                    if _qapi_final > 0 and _qapi_final < _sale_price:
                        logger.info(
                            f"[LOTTEON] 단일수집 qapi 보정: {item_id} "
                            f"{_sale_price:,} → {_qapi_final:,}"
                        )
                        _sale_price = _qapi_final
                        data["salePrice"] = _qapi_final
                        _bbp = data.get("bestBenefitPrice", 0)
                        if not _bbp or _bbp >= _sale_price:
                            data["bestBenefitPrice"] = _qapi_final
            except Exception as _qe:
                logger.debug(f"[LOTTEON] 단일수집 qapi 보정 실패: {item_id} — {_qe}")

            _cost = _sale_price
            if use_max_discount:
                try:
                    from backend.domain.samba.proxy.sourcing_queue import SourcingQueue

                    _req_id, _future = SourcingQueue.add_detail_job(
                        "LOTTEON", item_id, sitm_no=data.get("sitmNo", "")
                    )
                    _ext_result = await asyncio.wait_for(_future, timeout=25)
                    if isinstance(_ext_result, dict) and _ext_result.get("success"):
                        _ext_benefit = int(
                            _ext_result.get("best_benefit_price", 0) or 0
                        )
                        if _ext_benefit > 0:
                            _cost = _ext_benefit
                            logger.info(
                                f"[LOTTEON] 단일수집 확장앱 혜택가: {item_id} → {_ext_benefit:,}"
                            )
                except asyncio.TimeoutError:
                    logger.info(
                        f"[LOTTEON] 단일수집 확장앱 타임아웃: {item_id} — 판매가({_sale_price:,}) 사용"
                    )
                except Exception as _ext_err:
                    logger.debug(
                        f"[LOTTEON] 단일수집 확장앱 실패: {item_id} — {_ext_err}"
                    )

            initial_snapshot = {
                "date": datetime.now(timezone.utc).isoformat(),
                "sale_price": _sale_price,
                "original_price": data.get("originalPrice", 0),
                "options": data.get("options", []),
            }
            sale_status = data.get("saleStatus", "in_stock")
            raw_detail_html = ""
            detail_imgs = data.get("detailImages") or []
            if detail_imgs:
                raw_detail_html = "\n".join(
                    f'<div style="text-align:center;"><img src="{img}" style="max-width:860px;width:100%;" /></div>'
                    for img in detail_imgs
                )

            from backend.domain.samba.collector.model import (
                SambaCollectedProduct as CPModel,
            )

            existing_stmt = select(CPModel).where(
                CPModel.source_site == "LOTTEON",
                CPModel.site_product_id == item_id,
            )
            existing_row = (await session.execute(existing_stmt)).scalar_one_or_none()

            product_data = {
                "source_site": "LOTTEON",
                "site_product_id": item_id,
                "source_url": data.get("sourceUrl", "") or url,
                "name": data.get("name", ""),
                "brand": data.get("brand", ""),
                "original_price": data.get("originalPrice", 0),
                "sale_price": _sale_price,
                "cost": _cost,
                "images": data.get("images", []),
                "detail_images": data.get("detailImages") or [],
                "options": data.get("options", []),
                "addon_options": data.get("addonOptions") or None,
                "option_group_names": data.get("optionGroupNames") or None,
                "category": data.get("category", ""),
                "category1": data.get("category1", ""),
                "category2": data.get("category2", ""),
                "category3": data.get("category3", ""),
                "category4": data.get("category4", ""),
                "manufacturer": data.get("manufacturer", "") or data.get("brand", ""),
                "origin": data.get("origin", ""),
                "material": data.get("material", ""),
                "color": data.get("color", "")
                or parse_color_from_name(data.get("name", "")),
                "sex": data.get("sex", "") or "남녀공용",
                "season": data.get("season", "") or "사계절",
                "care_instructions": data.get("care_instructions", ""),
                "quality_guarantee": data.get("quality_guarantee", ""),
                "similar_no": str(data.get("similarNo", "0")),
                "style_code": data.get("styleNo", "") or data.get("style_code", ""),
                "detail_html": raw_detail_html,
                "status": "collected",
                "sale_status": sale_status,
                "free_shipping": data.get("freeShipping", False),
                "same_day_delivery": data.get("sameDayDelivery", False),
                "price_history": [initial_snapshot],
            }

            if existing_row:
                history = list(existing_row.price_history or [])
                history.insert(0, initial_snapshot)
                product_data["price_history"] = _trim_history(history)
                if "tags" not in product_data or not product_data.get("tags"):
                    product_data.pop("tags", None)
                collected = await svc.update_collected_product(
                    existing_row.id, product_data
                )
                return {
                    "type": "single",
                    "saved": 1,
                    "updated": True,
                    "product": collected,
                }
            else:
                collected = await svc.create_collected_product(product_data)
                return {"type": "single", "saved": 1, "product": collected}

    raise HTTPException(400, f"'{site}' 사이트 수집은 아직 지원하지 않습니다")


@router.post("/collect-filter/{filter_id}", status_code=200)
async def collect_by_filter(
    filter_id: str,
    group_index: int | None = None,
    group_total: int | None = None,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """검색그룹 기반 수집 — Job 큐에 등록하여 백그라운드 실행."""
    from backend.domain.samba.job.repository import SambaJobRepository
    from backend.domain.samba.job.service import SambaJobService

    svc = _get_services(session)
    search_filter = await svc.filter_repo.get_async(filter_id)
    if not search_filter:
        raise HTTPException(404, "필터를 찾을 수 없습니다")

    job_svc = SambaJobService(SambaJobRepository(session))
    payload: dict = {
        "filter_id": filter_id,
        "source_site": search_filter.source_site,
    }
    if group_index is not None and group_total is not None:
        payload["group_index"] = group_index
        payload["group_total"] = group_total

    job = await job_svc.create_job({"job_type": "collect", "payload": payload})
    await session.commit()
    return {"job_id": job.id, "status": job.status, "filter_id": filter_id}


@router.post("/collect-by-keyword", status_code=201)
async def collect_by_keyword(
    body: CollectByKeywordRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """키워드로 소싱사이트 검색 → 결과 반환 (저장은 별도)."""
    from backend.domain.samba.proxy.musinsa import MusinsaClient
    from backend.domain.samba.proxy.kream import KreamClient

    if body.source_site == "MUSINSA":
        from backend.domain.samba.forbidden.repository import SambaSettingsRepository

        settings_repo = SambaSettingsRepository(session)
        cookie_setting = await settings_repo.get_async("musinsa_cookie")
        cookie = (
            cookie_setting.value
            if cookie_setting and hasattr(cookie_setting, "value")
            else ""
        )

        if not cookie:
            raise HTTPException(
                400,
                "무신사 수집은 로그인(쿠키)이 필요합니다. "
                "확장앱에서 무신사 로그인 후 다시 시도하세요.",
            )

        client = MusinsaClient(cookie=cookie)
        data = await client.search_products(
            keyword=body.keyword, page=body.page, size=body.size
        )
        return data

    elif body.source_site == "KREAM":
        client = KreamClient()
        data = await client.search(body.keyword, body.size)
        return {"success": True, "data": data}

    elif body.source_site == "LOTTEON":
        from backend.domain.samba.collector.refresher import get_collect_proxy_url
        from backend.domain.samba.proxy.lotteon_sourcing import LotteonSourcingClient

        client = LotteonSourcingClient(proxy_url=get_collect_proxy_url())
        data = await client.search_products(
            keyword=body.keyword, page=body.page, size=body.size
        )
        # 브랜드 필터링: 키워드와 브랜드명이 일치하는 상품만 반환
        # (롯데ON은 URL에 브랜드 파라미터가 없어 다른 브랜드 상품이 섞임)
        keyword_lower = body.keyword.strip().lower()
        filtered = [
            p for p in data if keyword_lower in (p.get("brand", "") or "").lower()
        ]
        return {"success": True, "data": filtered if filtered else data}

    raise HTTPException(
        400, f"'{body.source_site}' 키워드 검색은 아직 지원하지 않습니다"
    )


@router.post("/collect-single-musinsa", status_code=201)
async def collect_single_musinsa(
    body: CollectSingleMusinsaRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """무신사 상품 URL 1개 수집 → 상품번호가 카테고리로 표시되는 그룹 자동 생성."""
    from urllib.parse import urlparse

    from backend.domain.samba.collector.model import SambaCollectedProduct as CPModel
    from backend.domain.samba.collector.model import SambaSearchFilter as SFModel
    from backend.domain.samba.proxy.musinsa import MusinsaClient

    url = body.url.strip()

    # 무신사 로그인(쿠키) 체크
    cookie = await get_musinsa_cookie(session)
    if not cookie:
        raise HTTPException(
            400,
            "무신사 수집은 로그인(쿠키)이 필요합니다. 확장앱에서 무신사 로그인 후 다시 시도하세요.",
        )

    # URL에서 상품번호 추출
    parsed_url = urlparse(url)
    match = re.search(r"/products/(\d+)", parsed_url.path) or re.search(
        r"goodsNo=(\d+)", parsed_url.query
    )
    if not match:
        raise HTTPException(400, "무신사 상품 URL에서 상품번호를 찾을 수 없습니다")
    goods_no = match.group(1)

    # 블랙리스트 체크
    if await _is_blacklisted(session, "MUSINSA", goods_no):
        raise HTTPException(400, f"수집차단된 상품입니다 ({goods_no})")

    # 상품 상세 조회
    client = MusinsaClient(cookie=cookie)
    data = await client.get_goods_detail(goods_no)
    if not data or not data.get("name"):
        raise HTTPException(502, "무신사 상품 조회 실패")

    brand = (data.get("brand") or "unknown").strip()

    svc = _get_services(session)

    # SearchFilter 중복 체크 (같은 상품번호로 이미 그룹 생성된 경우 재사용)
    filter_name = f"MUSINSA_{brand}_{goods_no}"
    existing_filter_stmt = select(SFModel).where(
        SFModel.source_site == "MUSINSA",
        SFModel.name == filter_name,
    )
    existing_filter = (await session.execute(existing_filter_stmt)).scalar_one_or_none()

    if existing_filter:
        search_filter = existing_filter
    else:
        search_filter = await svc.create_filter(
            {
                "source_site": "MUSINSA",
                "name": filter_name,
                "keyword": url,
                "requested_count": 1,
            }
        )

    filter_id = search_filter.id

    # 가격이력 초기 스냅샷
    initial_snapshot = {
        "date": datetime.now(timezone.utc).isoformat(),
        "sale_price": data.get("salePrice", 0),
        "original_price": data.get("originalPrice", 0),
        "options": data.get("options", []),
    }

    sale_status = data.get("saleStatus", "in_stock")
    raw_detail_html = data.get("detailHtml", "")
    if not raw_detail_html:
        detail_imgs = data.get("detailImages") or []
        if detail_imgs:
            raw_detail_html = "\n".join(
                f'<div style="text-align:center;"><img src="{img}" style="max-width:860px;width:100%;" /></div>'
                for img in detail_imgs
            )

    similar_no = str(data.get("similarNo", "0"))

    product_data = {
        "source_site": "MUSINSA",
        "site_product_id": goods_no,
        "search_filter_id": filter_id,
        "name": data.get("name", ""),
        "brand": brand,
        "original_price": data.get("originalPrice", 0),
        "sale_price": data.get("salePrice", 0),
        "cost": data.get("bestBenefitPrice") or None,
        "images": data.get("images", []),
        "detail_images": data.get("detailImages") or [],
        "options": data.get("options", []),
        "addon_options": data.get("addonOptions") or None,
        "option_group_names": data.get("optionGroupNames") or None,
        "category": data.get("category", ""),
        "category1": data.get("category1", ""),
        "category2": data.get("category2", ""),
        "category3": data.get("category3", ""),
        "category4": data.get("category4", ""),
        "manufacturer": data.get("manufacturer", "") or brand,
        "origin": data.get("origin", ""),
        "material": data.get("material", ""),
        "color": data.get("color", "") or parse_color_from_name(data.get("name", "")),
        "sex": data.get("sex", "") or "남녀공용",
        "season": data.get("season", "") or "사계절",
        "care_instructions": data.get("care_instructions", ""),
        "quality_guarantee": data.get("quality_guarantee", ""),
        "similar_no": similar_no,
        # 프록시는 'style_code' 키로 노출 — 'styleNo'는 없으므로 두 키 모두 폴백
        "style_code": data.get("style_code") or data.get("styleNo", ""),
        "group_key": generate_group_key(
            brand=brand,
            similar_no=similar_no,
            style_code=data.get("style_code") or data.get("styleNo", ""),
            name=data.get("name", ""),
        ),
        "detail_html": raw_detail_html,
        "status": "collected",
        "sale_status": sale_status,
        "free_shipping": data.get("freeShipping", False),
        "same_day_delivery": data.get("sameDayDelivery", False),
        "is_point_restricted": data.get("isPointRestricted"),
        "price_history": [initial_snapshot],
    }

    # 기존 상품 체크 (upsert)
    existing_stmt = select(CPModel).where(
        CPModel.source_site == "MUSINSA",
        CPModel.site_product_id == goods_no,
    )
    existing_row = (await session.execute(existing_stmt)).scalar_one_or_none()

    if existing_row:
        history = list(existing_row.price_history or [])
        history.insert(0, initial_snapshot)
        product_data["price_history"] = _trim_history(history)
        if "tags" not in product_data or not product_data.get("tags"):
            product_data.pop("tags", None)
        # search_filter_id 갱신 (그룹 연결 보장)
        product_data["search_filter_id"] = filter_id
        collected = await svc.update_collected_product(existing_row.id, product_data)
        updated = True
    else:
        collected = await svc.create_collected_product(product_data)
        updated = False

    # SearchFilter last_collected_at 갱신
    search_filter.last_collected_at = datetime.now(timezone.utc)
    session.add(search_filter)
    await session.commit()

    return {
        "saved": 1,
        "updated": updated,
        "product_no": goods_no,
        "brand": brand,
        "filter_name": filter_name,
        "product": collected,
    }

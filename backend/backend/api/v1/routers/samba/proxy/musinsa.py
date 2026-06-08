"""무신사 관련 엔드포인트."""

from typing import Any, Optional

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.core.rate_limit import RATE_SET_COOKIE, limiter
from backend.core.url_safe import validate_url_host
from backend.db.orm import get_read_session_dependency, get_write_session_dependency
from backend.domain.samba.proxy.musinsa import MusinsaClient
from backend.utils.logger import logger

from ._helpers import _get_musinsa_client, _get_setting, _set_setting

# SSRF 방지 — `/musinsa/search?url=...` 가 받을 수 있는 host allowlist.
# substring 검사 (``"musinsa.com" in url``) 는 ``https://attacker.com/musinsa.com``
# 우회에 취약하므로 host 정확/서브도메인 매칭으로 강화.
_MUSINSA_ALLOWED_URL_HOSTS = frozenset({"musinsa.com", "musinsa.onelink.me"})

router = APIRouter(tags=["samba-proxy"])

# 확장앱 전용 라우터 — JWT(samba_auth) 면제. X-Api-Key만으로 호출 가능.
# 라우터 레벨 samba_auth가 적용된 main router에 두면 'Missing authentication token'
# 401로 차단되어 settings.musinsa_cookie 갱신 경로가 막힘 (2026-04-09 사고).
extension_router = APIRouter(tags=["samba-proxy-extension"])


@router.get("/musinsa/goods/{goods_no}")
async def musinsa_goods_detail(
    goods_no: str,
    session: AsyncSession = Depends(get_read_session_dependency),
) -> dict[str, Any]:
    """무신사 상품 상세 조회."""
    if not goods_no or not goods_no.isdigit():
        raise HTTPException(status_code=400, detail="유효하지 않은 상품번호입니다.")

    client = await _get_musinsa_client(session)
    try:
        product = await client.get_goods_detail(goods_no)
        return {"success": True, "data": product}
    except Exception as exc:
        logger.error(f"[무신사] {goods_no} 수집 실패: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/brand-search")
async def brand_search(
    keyword: str = Query(...),
    gf: str = Query("A"),
    session: AsyncSession = Depends(get_read_session_dependency),
) -> dict[str, Any]:
    """무신사 키워드로 브랜드 코드 검색."""
    try:
        client = await _get_musinsa_client(session)
        brands = await client.search_brands(keyword, gf)
        return {"brands": brands}
    except Exception as exc:
        logger.error(f"[무신사 브랜드검색] 실패: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/search-count")
async def search_count(
    source_site: str = Query(...),
    keyword: str = Query(""),
    url: str = Query(""),
    session: AsyncSession = Depends(get_read_session_dependency),
) -> dict[str, Any]:
    """소싱처별 검색 총 상품수 조회."""
    try:
        if source_site == "MUSINSA":
            client = await _get_musinsa_client(session)
            params: dict[str, Any] = {"keyword": keyword, "size": 1}
            if url:
                from urllib.parse import urlparse, parse_qs

                parsed = parse_qs(urlparse(url).query)
                if "brand" in parsed:
                    params["brand"] = parsed["brand"][0]
                if "category" in parsed:
                    params["category"] = parsed["category"][0]
                if "gf" in parsed:
                    params["gf"] = parsed["gf"][0]
                if "minPrice" in parsed:
                    params["min_price"] = int(parsed["minPrice"][0])
                if "maxPrice" in parsed:
                    params["max_price"] = int(parsed["maxPrice"][0])
                if not keyword and "keyword" in parsed:
                    params["keyword"] = parsed["keyword"][0]
            result = await client.search_products(**params)
            return {"totalCount": result.get("totalCount", 0)}

        elif source_site == "FashionPlus":
            search_word = keyword
            if not search_word and url:
                from urllib.parse import urlparse, parse_qs

                parsed = parse_qs(urlparse(url).query)
                search_word = parsed.get("searchWord", [""])[0]
            if not search_word:
                return {"totalCount": 0}
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(
                    "https://www.fashionplus.co.kr/search/goods/fetch",
                    params={
                        "searchWord": search_word,
                        "page": 1,
                        "pageSize": 1,
                        "sort": "recommend",
                    },
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                data = r.json()
                return {
                    "totalCount": data.get("goodsPaginator", {}).get("totalCount", 0)
                }

        elif source_site == "KREAM":
            # KREAM은 확장앱 기반 수집 — 카운트 조회 불가
            return {"totalCount": 0}

        elif source_site in ("ABCmart", "Nike", "Adidas", "OliveYoung"):
            # 이 소싱처들은 서버사이드 렌더링/확장앱 기반 — 카운트 조회 불가
            return {"totalCount": 0}

        else:
            return {"totalCount": 0}

    except Exception as e:
        logger.warning(f"[검색카운트] {source_site} 실패: {e}")
        return {"totalCount": 0}


@router.get("/musinsa/search-api")
async def musinsa_search_api(
    keyword: str = Query("", description="검색 키워드"),
    page: int = Query(1, ge=1),
    size: int = Query(30, ge=1, le=200),
    sort: str = Query("POPULAR"),
    category: str = Query(""),
    brand: str = Query(""),
    min_price: Optional[int] = Query(None, alias="minPrice"),
    max_price: Optional[int] = Query(None, alias="maxPrice"),
    gf: str = Query("A"),
    session: AsyncSession = Depends(get_read_session_dependency),
) -> dict[str, Any]:
    """무신사 상품 검색 API."""
    if not keyword:
        raise HTTPException(status_code=400, detail="검색 키워드를 입력해주세요.")

    client = await _get_musinsa_client(session)
    try:
        return await client.search_products(
            keyword=keyword,
            page=page,
            size=size,
            sort=sort,
            category=category,
            brand=brand,
            min_price=min_price,
            max_price=max_price,
            gf=gf,
        )
    except Exception as exc:
        logger.error(f"[무신사] 검색 실패: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/musinsa/search")
async def musinsa_search_by_url(
    url: str = Query("", description="무신사 URL"),
    session: AsyncSession = Depends(get_read_session_dependency),
) -> dict[str, Any]:
    """URL 기반 검색/리다이렉트 처리."""
    if not validate_url_host(url, _MUSINSA_ALLOWED_URL_HOSTS):
        raise HTTPException(status_code=400, detail="무신사 URL을 입력해주세요.")

    client = await _get_musinsa_client(session)
    try:
        return await client.search_by_url(url)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class MusinsaSetCookieRequest(BaseModel):
    cookie: str


@extension_router.post("/musinsa/set-cookie")
@limiter.limit(RATE_SET_COOKIE)
async def musinsa_set_cookie(
    request: Request,
    body: MusinsaSetCookieRequest = Body(...),
    write_session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """브라우저 확장에서 쿠키 직접 전달.

    인증: X-Api-Key (확장앱 공통). extension_router로 분리 — main router의
    라우터 레벨 samba_auth(JWT)가 확장앱 호출(X-Api-Key만 전송)을 401로 차단해
    2026-04-09부터 settings.musinsa_cookie 갱신 정지가 발생함. JWT 면제 라우터로
    옮겨 X-Api-Key만으로 호출 가능하게 한다. refresher가 사용하는 복수 쿠키 풀
    musinsa_cookies도 함께 갱신해 잔액 페이지 미진입 시에도 풀이 살아있도록 한다.

    (2026-05-20) owner_device_ids 가드 적용 — 포크 확장앱이 원본 백엔드로
    쿠키 미러 전송하던 누수 차단.
    """
    import json

    from backend.api.v1.routers.samba.sourcing_account import _check_owner_device

    _check_owner_device(request)

    client = MusinsaClient(cookie=body.cookie)
    result = await client.set_cookie_and_verify(body.cookie)
    # 단수 쿠키 저장 (기존 호출지점 호환)
    await _set_setting(write_session, "musinsa_cookie", body.cookie)

    # refresher가 우선 참조하는 복수 쿠키 풀에도 머지 (중복 제거 + 최신 맨 앞)
    try:
        raw = await _get_setting(write_session, "musinsa_cookies")
        existing: list[str] = []
        if raw:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, list):
                existing = [c for c in parsed if isinstance(c, str) and c]
        merged = [body.cookie] + [c for c in existing if c != body.cookie]
        await _set_setting(write_session, "musinsa_cookies", json.dumps(merged))
    except Exception as exc:  # pragma: no cover — fallback 실패해도 단수는 저장됨
        logger.warning(f"[set-cookie] musinsa_cookies 풀 갱신 실패 (무시): {exc}")

    # (2026-05-27) hashId 기반 row 동기화 — sync-balance 미발생 시(마이페이지 미진입)에도
    # 본인 계정 row 에 쿠키 박힘. JWT(mss_mac).sub 매칭이라 Chrome 프로필 이메일 불일치/
    # Whale 환경 chrome.identity 빈값에 무관.
    # 매칭 0건이면 row 안 건드림 → 오염 차단.
    try:
        from backend.api.v1.routers.samba.proxy._musinsa_jwt import (
            musinsa_hash_id,
        )
        from backend.domain.samba.sourcing_account.repository import (
            SambaSourcingAccountRepository,
        )
        from backend.domain.samba.sourcing_account.service import (
            SambaSourcingAccountService,
        )

        _hash = musinsa_hash_id(body.cookie or "")
        if _hash:
            _svc = SambaSourcingAccountService(
                SambaSourcingAccountRepository(write_session)
            )
            _accs = await _svc.list_accounts(site_name="MUSINSA")
            _match = next(
                (
                    a
                    for a in _accs
                    if (a.additional_fields or {}).get("musinsa_hash_id") == _hash
                ),
                None,
            )
            if _match:
                from datetime import datetime, timezone

                _extra = dict(_match.additional_fields or {})
                _extra["musinsa_cookie"] = body.cookie
                _extra["cookie_expired"] = False
                _extra["cookie_updated_at"] = datetime.now(timezone.utc).isoformat()
                await _svc.repo.update_async(_match.id, additional_fields=_extra)
                logger.info(
                    f"[set-cookie] hashId 매칭 → {_match.account_label} row 갱신"
                )
            else:
                logger.info(f"[set-cookie] hashId={_hash} 매칭 row 없음 — pool 만 저장")
    except Exception as exc:
        logger.warning(f"[set-cookie] hashId row 동기화 실패 (무시): {exc}")

    return result


class MusinsaCheckLoginRequest(BaseModel):
    cookie: Optional[str] = None


@router.post("/musinsa/check-login")
async def musinsa_check_login(
    body: MusinsaCheckLoginRequest = MusinsaCheckLoginRequest(),
    session: AsyncSession = Depends(get_read_session_dependency),
) -> dict[str, Any]:
    """무신사 로그인 상태 확인."""
    client = await _get_musinsa_client(session)
    return await client.check_login_status(cookie=body.cookie)


@router.get("/musinsa/auth/status")
async def musinsa_auth_status(
    session: AsyncSession = Depends(get_read_session_dependency),
) -> dict[str, Any]:
    """무신사 인증 상태 확인."""
    cookie = await _get_setting(session, "musinsa_cookie") or ""
    return {"isLoggedIn": bool(cookie), "cookieLength": len(str(cookie))}


@router.delete("/musinsa/auth")
async def musinsa_auth_delete(
    write_session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """무신사 쿠키 초기화 (로그아웃)."""
    await _set_setting(write_session, "musinsa_cookie", "")
    return {"success": True, "isLoggedIn": False, "message": "로그아웃 완료"}


class MusinsaOptNoMapping(BaseModel):
    ord_no: str
    ord_opt_no: str


class MusinsaSaveOptNosRequest(BaseModel):
    mappings: list[MusinsaOptNoMapping]


@extension_router.post("/musinsa/save-opt-nos")
async def musinsa_save_opt_nos(
    body: MusinsaSaveOptNosRequest = Body(...),
    write_session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """확장앱이 무신사 마이페이지 API에서 추출한 ord_no→ord_opt_no 매핑 일괄 저장.

    인증: X-Api-Key (확장앱 공통). extension_router에 등록 — JWT 면제.

    동작:
      - sourcing_order_number == ord_no AND source_site = 'MUSINSA' 인 주문에
        musinsa_ord_opt_no 컬럼 채우기 (이미 채워진 row는 덮어쓰기 OK — 갱신 케이스)
      - 매칭 안 되는 매핑은 카운트만 (정상 — 아직 동기화 안 된 주문 등)
    """
    from sqlalchemy import update
    from backend.domain.samba.order.model import SambaOrder

    updated = 0
    not_matched = 0
    for m in body.mappings:
        ord_no = (m.ord_no or "").strip()
        ord_opt_no = (m.ord_opt_no or "").strip()
        if not ord_no or not ord_opt_no:
            continue
        stmt = (
            update(SambaOrder)
            .where(
                SambaOrder.sourcing_order_number == ord_no,
                SambaOrder.source_site == "MUSINSA",
            )
            .values(musinsa_ord_opt_no=ord_opt_no)
        )
        res = await write_session.execute(stmt)
        n = res.rowcount or 0
        if n > 0:
            updated += n
        else:
            not_matched += 1
    await write_session.commit()
    logger.info(
        f"[무신사 옵션번호] 매핑 저장: updated={updated} not_matched={not_matched} "
        f"total_in={len(body.mappings)}"
    )
    return {
        "ok": True,
        "received": len(body.mappings),
        "updated": updated,
        "notMatched": not_matched,
    }


class MusinsaReturnTracking(BaseModel):
    orderNo: str
    courier: Optional[str] = None
    trackingNo: str


class MusinsaSaveReturnTrackingRequest(BaseModel):
    items: list[MusinsaReturnTracking]


@extension_router.post("/musinsa/save-return-tracking")
async def musinsa_save_return_tracking(
    body: MusinsaSaveReturnTrackingRequest = Body(...),
    write_session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """확장앱이 무신사 get_claim_list API 에서 추출한 회수송장 일괄 저장.

    인증: X-Api-Key (확장앱 공통). extension_router 등록 — JWT 면제.

    동작:
      - sourcing_order_number == orderNo AND source_site = 'MUSINSA' 인 주문에
        return_collect_courier/tracking/at 저장 (CS 답변용, 마켓 전송 안 함)
      - trackingNo 비어있는 항목은 스킵 (아직 회수 미발송)
      - 이미 같은 송장이 저장된 경우는 갱신 카운트에서 제외(불필요 write 방지)
    """
    from datetime import datetime, timezone

    from sqlalchemy import update

    from backend.domain.samba.order.model import SambaOrder
    from backend.domain.samba.tracking_sync.service import normalize_courier_name

    updated = 0
    not_matched = 0
    skipped = 0
    now = datetime.now(timezone.utc)
    for it in body.items:
        ord_no = (it.orderNo or "").strip()
        tracking = (it.trackingNo or "").strip()
        if not ord_no or not tracking:
            skipped += 1
            continue
        courier = normalize_courier_name((it.courier or "").strip())
        stmt = (
            update(SambaOrder)
            .where(
                SambaOrder.sourcing_order_number == ord_no,
                SambaOrder.source_site == "MUSINSA",
                # 같은 송장이 이미 박혀 있으면 갱신 안 함(불필요 write/at 갱신 방지)
                (SambaOrder.return_collect_tracking.is_(None))
                | (SambaOrder.return_collect_tracking != tracking),
            )
            .values(
                return_collect_courier=courier,
                return_collect_tracking=tracking,
                return_collect_at=now,
            )
        )
        res = await write_session.execute(stmt)
        n = res.rowcount or 0
        if n > 0:
            updated += n
        else:
            not_matched += 1
    await write_session.commit()
    logger.info(
        f"[무신사 회수송장] 저장: updated={updated} not_matched={not_matched} "
        f"skipped={skipped} total_in={len(body.items)}"
    )
    return {
        "ok": True,
        "received": len(body.items),
        "updated": updated,
        "notMatched": not_matched,
        "skipped": skipped,
    }


class MusinsaCookiesRequest(BaseModel):
    cookies: list[str]


@router.post("/musinsa/cookies")
async def set_musinsa_cookies(
    body: MusinsaCookiesRequest,
    write_session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """무신사 쿠키 로테이션 목록 저장."""
    import json

    await _set_setting(write_session, "musinsa_cookies", json.dumps(body.cookies))
    return {"ok": True, "count": len(body.cookies)}


@router.get("/musinsa/cookies")
async def get_musinsa_cookies(
    session: AsyncSession = Depends(get_read_session_dependency),
) -> dict[str, Any]:
    """무신사 쿠키 로테이션 목록 조회."""
    import json

    raw = await _get_setting(session, "musinsa_cookies")
    if raw:
        cookies = json.loads(raw) if isinstance(raw, str) else raw
        return {"cookies": cookies, "count": len(cookies)}
    return {"cookies": [], "count": 0}


class StockCheckRequest(BaseModel):
    goodsNos: list[str]


@router.post("/musinsa/stock-check")
async def musinsa_stock_check(
    body: StockCheckRequest,
    session: AsyncSession = Depends(get_read_session_dependency),
) -> dict[str, Any]:
    """재고 소진 감지 (서브에이전트)."""
    if not body.goodsNos:
        raise HTTPException(status_code=400, detail="goodsNos 배열이 필요합니다.")

    client = await _get_musinsa_client(session)
    return await client.check_stock(body.goodsNos)


class PriceMonitorProduct(BaseModel):
    goodsNo: str
    storedPrice: int = 0
    productId: Optional[str] = None


class PriceMonitorRequest(BaseModel):
    products: list[PriceMonitorProduct]


@router.post("/musinsa/price-monitor")
async def musinsa_price_monitor(
    body: PriceMonitorRequest,
    session: AsyncSession = Depends(get_read_session_dependency),
) -> dict[str, Any]:
    """가격 변동 감지 (서브에이전트)."""
    if not body.products:
        raise HTTPException(status_code=400, detail="products 배열이 필요합니다.")

    client = await _get_musinsa_client(session)
    products_dicts = [p.model_dump() for p in body.products]
    return await client.monitor_prices(products_dicts)

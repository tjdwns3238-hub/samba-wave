"""통합 소싱 큐 — 확장앱 기반 상품 수집 큐 관리.

KREAM 패턴과 동일: 백엔드가 큐에 작업 추가 → 확장앱이 폴링 → 탭 열어 DOM 파싱 → 결과 전송.
ABCmart, GrandStage, REXMONDE, 롯데ON, GSShop 5개 사이트 지원.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.db.orm import get_write_session
from backend.domain.samba.sourcing_job.model import SambaSourcingJob
from backend.shutdown_state import is_shutting_down
from backend.utils.logger import logger

_UTC = timezone.utc
_JOB_TTL_SEC: dict[str, int] = {"search": 600, "detail": 180, "tracking": 3600}


async def _db_insert_job(
    job: dict[str, Any], job_type: str, *, priority: bool = False
) -> None:
    try:
        async with get_write_session() as session:
            now = datetime.now(_UTC)
            record = SambaSourcingJob(
                request_id=job["requestId"],
                site=job["site"],
                job_type=job_type,
                status="pending",
                owner_device_id=job.get("ownerDeviceId") or None,
                payload=job,
                # priority 잡은 created_at을 과거로 밀어 ORDER BY created_at 우선순위 확보
                created_at=now - timedelta(seconds=3600) if priority else now,
                expires_at=now + timedelta(seconds=_JOB_TTL_SEC.get(job_type, 180)),
            )
            session.add(record)
            await session.commit()
    except Exception as exc:
        logger.warning(f"[소싱큐-DB] INSERT 실패 (무시): {exc}")


async def _db_update_dispatched(request_id: str) -> None:
    try:
        async with get_write_session() as session:
            record = await session.get(SambaSourcingJob, request_id)
            if record:
                record.status = "dispatched"
                record.dispatched_at = datetime.now(_UTC)
                session.add(record)
                await session.commit()
    except Exception as exc:
        logger.warning(f"[소싱큐-DB] dispatched UPDATE 실패 (무시): {exc}")


async def _db_update_completed(request_id: str, data: dict[str, Any]) -> None:
    try:
        async with get_write_session() as session:
            record = await session.get(SambaSourcingJob, request_id)
            if record:
                success = bool(data.get("success"))
                record.status = "completed" if success else "failed"
                record.result = data
                record.error = data.get("error") or None
                record.completed_at = datetime.now(_UTC)
                session.add(record)
                await session.commit()
    except Exception as exc:
        logger.warning(f"[소싱큐-DB] completed UPDATE 실패 (무시): {exc}")


# 오토튠 잡 owner는 PC별 인스턴스 모델로 전환 (2026-05-12).
# 발행자 PC 컨텍스트(collector_autotune.current_pc_owner)를 읽어 owner_device_id로 박는다.
# 글로벌 owner state는 더 이상 유지하지 않음.


def get_autotune_owner(site: str | None = None) -> str:
    """현재 잡 발행 컨텍스트의 owner device_id 반환.

    site 인자는 historical compatibility 용도(무신사 등 호출처 시그니처 유지).
    실제로는 contextvar 값만 사용한다.
    """
    try:
        from backend.api.v1.routers.samba.collector_autotune import (
            current_pc_owner,
        )

        return current_pc_owner.get() or ""
    except Exception:
        return ""


def get_autotune_owner_mapping() -> dict[str, str]:
    """historical compatibility — 빈 매핑 반환."""
    return {"default": "", "by_site": {}}


# 사이트별 검색 URL 템플릿
SITE_SEARCH_URLS: dict[str, str] = {
    "ABCmart": "https://www.a-rt.com/display/search-word/result?searchWord={keyword}",
    "GrandStage": "https://www.a-rt.com/display/search-word/result?searchWord={keyword}&channel=10002",
    "REXMONDE": "https://www.okmall.com/products/list?keyword={keyword}",
    "LOTTEON": "https://www.lotteon.com/csearch/search/search?render=search&platform=pc&mallId=2&q={keyword}",
    "GSShop": "https://www.gsshop.com/shop/search/main.gs?tq={keyword}",
    "SSG": "https://department.ssg.com/search?query={keyword}",
    "ElandMall": "https://www.elandmall.com/search/search.action?kwd={keyword}",
    "SSF": "https://www.ssfshop.com/search?keyword={keyword}",
}

# 사이트별 상품 상세 URL 템플릿
SITE_DETAIL_URLS: dict[str, str] = {
    # /product/new 신형 URL 사용 필수 — 구형 /product?prdtNo=X 는 최대혜택가를
    # 멤버십+쿠폰 전체 적용 전 값으로 표시해 DOM 파싱 시 잘못된 원가가 수집됨.
    "ABCmart": "https://abcmart.a-rt.com/product/new?prdtNo={product_id}",
    "GrandStage": "https://grandstage.a-rt.com/product/new?prdtNo={product_id}&tChnnlNo=10002",
    "REXMONDE": "https://www.okmall.com/products/detail/{product_id}",
    "LOTTEON": "https://www.lotteon.com/p/product/{product_id}",
    "GSShop": "https://www.gsshop.com/prd/prd.gs?prdid={product_id}",
    "SSG": "https://department.ssg.com/item/itemView.ssg?itemId={product_id}&siteNo=6009",
    "ElandMall": "https://www.elandmall.com/goods/goods.action?goodsNo={product_id}",
    "SSF": "https://www.ssfshop.com/goods/{product_id}",
    "FashionPlus": "https://www.fashionplus.co.kr/goods/detail/{product_id}",
}


class SourcingQueue:
    """통합 소싱 수집 큐 (싱글턴, 클래스 변수).

    단계 3(2026-05-09~): DB가 단일 진실의 원천. queue 리스트 제거.
    잡 발행 → DB INSERT (create_task fire-and-forget)
    잡 수신 → DB SELECT FOR UPDATE SKIP LOCKED
    """

    # 결과 대기: {requestId: asyncio.Future}
    resolvers: dict[str, asyncio.Future[Any]] = {}

    @classmethod
    def _ensure_accepting_jobs(cls) -> None:
        if is_shutting_down():
            raise RuntimeError("server is shutting down")

    @classmethod
    def add_search_job(
        cls,
        site: str,
        keyword: str,
        url: str | None = None,
        max_count: int | None = None,
        *,
        owner_device_id: str | None = None,
    ) -> tuple[str, asyncio.Future[Any]]:
        """검색 작업 큐에 추가. (requestId, future) 반환.

        url: 호출자가 원본 검색 URL(파라미터 포함)을 직접 넘길 수 있음.
             없으면 SITE_SEARCH_URLS 템플릿에 keyword만 치환해서 사용.
        max_count: 확장앱에 최대 수집 건수 힌트 전달.
        owner_device_id: 작업을 집어가야 할 확장앱 deviceId. None이면 오토튠 전역값을 사용.
        """
        cls._ensure_accepting_jobs()
        request_id = str(uuid.uuid4())[:8]
        if not url:
            url_template = SITE_SEARCH_URLS.get(site, "")
            if not url_template:
                raise ValueError(f"지원하지 않는 소싱처: {site}")
            url = url_template.replace("{keyword}", keyword)

        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()

        if owner_device_id is None:
            # 사이트별 매핑 우선 → 없으면 기본 owner (PC 분산 지원)
            owner_device_id = get_autotune_owner(site)

        job: dict[str, Any] = {
            "requestId": request_id,
            "site": site,
            "type": "search",
            "url": url,
            "keyword": keyword,
            "ownerDeviceId": owner_device_id or "",
        }
        if max_count is not None:
            job["maxCount"] = max_count
        cls.resolvers[request_id] = future
        asyncio.create_task(_db_insert_job(job, "search"))
        _owner_tag = f" owner={owner_device_id[:8]}" if owner_device_id else ""
        logger.info(
            f"[소싱큐] 검색 추가: {site} '{keyword}' (id={request_id}){_owner_tag}"
        )
        return request_id, future

    @classmethod
    def add_detail_job(
        cls,
        site: str,
        product_id: str,
        *,
        sitm_no: str = "",
        url: str = "",
        extra: dict[str, Any] | None = None,
        owner_device_id: str | None = None,
        priority: bool = False,
    ) -> tuple[str, asyncio.Future[Any]]:
        """상세조회 작업 큐에 추가. (requestId, future) 반환.

        sitm_no: LOTTEON sitmNo — 전달 시 확장앱이 탭 없이 pbf API 직접 호출.
        url: 비어있지 않으면 SITE_DETAIL_URLS 템플릿 대신 직접 사용 (NAVERSTORE 등 템플릿만으로 부족한 경우).
        extra: job dict에 병합할 추가 필드 (channelUid, storeName 등).
        owner_device_id: 작업을 집어가야 할 확장앱 deviceId. None이면 오토튠 전역값을 사용.
        priority: True면 큐 맨 앞에 삽입 (수동 enrich 등 긴급 요청용).
        """
        cls._ensure_accepting_jobs()
        request_id = str(uuid.uuid4())[:8]
        if not url:
            url_template = SITE_DETAIL_URLS.get(site, "")
            if not url_template:
                raise ValueError(f"지원하지 않는 소싱처: {site}")
            url = url_template.replace("{product_id}", product_id)
        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()

        if owner_device_id is None:
            # 사이트별 매핑 우선 → 없으면 기본 owner (PC 분산 지원)
            owner_device_id = get_autotune_owner(site)

        job: dict[str, Any] = {
            "requestId": request_id,
            "site": site,
            "type": "detail",
            "url": url,
            "productId": product_id,
            "ownerDeviceId": owner_device_id or "",
        }
        if sitm_no:
            job["sitmNo"] = sitm_no
        if extra:
            job.update(extra)
        cls.resolvers[request_id] = future
        asyncio.create_task(_db_insert_job(job, "detail", priority=priority))
        _owner_tag = f" owner={owner_device_id[:8]}" if owner_device_id else ""
        _prio_tag = " [우선]" if priority else ""
        logger.info(
            f"[소싱큐] 상세 추가: {site} #{product_id} (id={request_id}){_owner_tag}{_prio_tag}"
        )
        return request_id, future

    @classmethod
    def add_tracking_job(
        cls,
        site: str,
        url: str,
        order_id: str,
        sourcing_order_number: str,
        *,
        owner_device_id: str | None = None,
        sourcing_account_id: str | None = None,
    ) -> tuple[str, asyncio.Future[Any]]:
        """송장 추출 작업 큐에 추가 (소싱처 배송조회 페이지 → 운송장 스크래핑).

        결과는 별도 라우터 `/proxy/sourcing/tracking-result` 로 수신되어
        tracking_sync_service.apply_tracking_result()로 라우팅됨.
        """
        cls._ensure_accepting_jobs()
        request_id = str(uuid.uuid4())[:8]
        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()
        if owner_device_id is None:
            owner_device_id = get_autotune_owner(site)

        job: dict[str, Any] = {
            "requestId": request_id,
            "site": site,
            "type": "tracking",
            "url": url,
            "orderId": order_id,
            "sourcingOrderNumber": sourcing_order_number,
            "ownerDeviceId": owner_device_id or "",
            "sourcingAccountId": sourcing_account_id or "",
        }
        cls.resolvers[request_id] = future
        asyncio.create_task(_db_insert_job(job, "tracking"))
        _owner_tag = f" owner={owner_device_id[:8]}" if owner_device_id else ""
        logger.info(
            f"[소싱큐] 송장조회 추가: {site} ord={sourcing_order_number} "
            f"(id={request_id}){_owner_tag}"
        )
        return request_id, future

    @classmethod
    async def get_next_job(
        cls,
        device_id: str | None = None,
        allowed_sites: list[str] | None = None,
    ) -> dict[str, Any]:
        """DB에서 다음 작업 가져오기 (확장앱 폴링용).

        SELECT FOR UPDATE SKIP LOCKED — 멀티 PC 동시 폴링 안전.

        device_id: 해당 deviceId 소유 잡 또는 소유자 미지정 잡만 반환.
        allowed_sites:
          - None  = 전체 처리 (단일 PC 디폴트)
          - []    = 아무것도 처리 안 함
          - [...] = 해당 사이트만
        """
        if is_shutting_down():
            return {"hasJob": False, "shuttingDown": True}

        device_id = (device_id or "").strip()

        # 명시적 빈 배열 → 이 PC는 작업 안 받음
        if allowed_sites is not None and len(allowed_sites) == 0:
            return {"hasJob": False}

        from sqlalchemy import text

        try:
            conditions = [
                "status = 'pending'",
                "expires_at > now()",
            ]
            params: dict[str, Any] = {}

            # owner 필터
            if device_id:
                conditions.append(
                    "(owner_device_id IS NULL OR owner_device_id = '' "
                    "OR owner_device_id = :device_id)"
                )
                params["device_id"] = device_id
            else:
                conditions.append("(owner_device_id IS NULL OR owner_device_id = '')")

            # site 필터
            if allowed_sites is not None:
                site_list = [s.strip() for s in allowed_sites if s.strip()]
                placeholders = ", ".join(f":site_{i}" for i in range(len(site_list)))
                conditions.append(f"site IN ({placeholders})")
                for i, s in enumerate(site_list):
                    params[f"site_{i}"] = s

            where = " AND ".join(conditions)
            sql = text(
                f"SELECT request_id, payload FROM samba_sourcing_job "
                f"WHERE {where} "
                f"ORDER BY created_at ASC LIMIT 1 "
                f"FOR UPDATE SKIP LOCKED"
            )

            async with get_write_session() as session:
                row = (await session.execute(sql, params)).fetchone()
                if not row:
                    return {"hasJob": False}

                request_id, payload = row
                await session.execute(
                    text(
                        "UPDATE samba_sourcing_job "
                        "SET status = 'dispatched', dispatched_at = now() "
                        "WHERE request_id = :rid"
                    ),
                    {"rid": request_id},
                )
                await session.commit()

            job = dict(payload or {})
            job.setdefault("requestId", request_id)
            return {"hasJob": True, **job}

        except Exception as exc:
            logger.warning(f"[소싱큐] DB dequeue 실패: {exc}")
            return {"hasJob": False}

    @classmethod
    def resolve_job(cls, request_id: str, data: dict[str, Any]) -> bool:
        """작업 결과 전달 (확장앱 → 백엔드).

        Future가 워커 스레드의 이벤트 루프에서 생성되었을 수 있으므로
        call_soon_threadsafe로 안전하게 resolve한다.
        """
        future = cls.resolvers.pop(request_id, None)
        if future and not future.done():
            try:
                loop = future.get_loop()
                loop.call_soon_threadsafe(future.set_result, data)
            except RuntimeError:
                # 루프가 닫혔으면 직접 set (같은 스레드일 수도 있음)
                if not future.done():
                    future.set_result(data)
            asyncio.create_task(_db_update_completed(request_id, data))
            _prods = data.get("products") or []
            _err = data.get("error") or ""
            logger.info(
                f"[소싱큐] 결과 수신: id={request_id}, success={data.get('success')}, "
                f"products={len(_prods)}, error={_err[:100]}"
            )
            return True
        return False

    @classmethod
    def cancel_all(cls, reason: str = "server is shutting down") -> None:
        """인-메모리 Future 전부 취소. DB pending 잡은 TTL로 자연 소멸."""
        futures = list(cls.resolvers.items())
        cls.resolvers.clear()
        for request_id, future in futures:
            if future.done():
                continue
            exc = RuntimeError(reason)
            try:
                loop = future.get_loop()
                loop.call_soon_threadsafe(future.set_exception, exc)
            except RuntimeError:
                if not future.done():
                    future.set_exception(exc)
            logger.info(f"[sourcing queue] shutdown cancel: {request_id}")

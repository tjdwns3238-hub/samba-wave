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
_JOB_TTL_SEC: dict[str, int] = {
    "search": 600,
    "detail": 180,
    "tracking": 3600,
    "reward": 3600,
    "cancel_order": 1800,
}

# 데몬 전용 사이트 — 확장앱(브라우저) 처리 불가. 데몬 없으면 잡 발행 자체 skip.
# 사용자 룰 (2026-05-25, feedback_daemon_sites_routing): SSG/ABC/LOTTEON 은 데몬으로만.
# 확장앱 fallback 으로 라우팅 시 60초 미응답 차단 → 무의미한 timeout 누적.
DAEMON_ONLY_SITES: set[str] = {"SSG", "ABCmart", "GrandStage", "LOTTEON"}


def _resolve_owner_with_daemon_policy(site: str) -> str | None:
    """데몬 전용 사이트 라우팅 정책 — 데몬 매칭 우선, 확장앱 fallback 차단.

    SSG/ABC/LOTTEON 은 데몬 풀에 매칭되는 데몬이 없으면 None(잡 발행 skip).
    기타 사이트는 데몬 → 확장앱 fallback (기존 동작).
    """
    from backend.domain.samba.proxy.daemon_pool import pick_daemon_owner

    d = pick_daemon_owner(site)
    if d:
        return d
    if (site or "").upper() in {s.upper() for s in DAEMON_ONLY_SITES}:
        return None  # 데몬 없으면 확장앱 fallback 차단
    return get_autotune_owner(site)


# 적립 액션별 진입 URL — 확장앱이 잡 받으면 이 URL의 탭을 열고 content script 주입.
REWARD_ACTION_URLS: dict[str, str] = {
    "musinsa_attendance": "https://www.musinsa.com/events/attendance",
    "musinsa_snap_like": "https://www.musinsa.com/mission?tab=snap-daily-like",
    "musinsa_balance": "https://www.musinsa.com/mypage",
    "musinsa_review": "https://www.musinsa.com/mypage/myreview",
    "abcmart_attendance": "https://member.a-rt.com/p/attendance-check",
    "abcmart_review": "https://abcmart.a-rt.com/mypage/claim/claim-order-main?orderPrdtStatCodeClick=10007",
    "ssg_review": "https://www.ssg.com/myssg/activityMng/pdtEvalList.ssg?quick=pdtEvalList",
    "gs_review": "https://www.gsshop.com/ord/dlvcursta/ordList.gs",
    "lotteon_review": "https://www.lotteon.com/p/review/myLotte/reviewWriteListTab",
    "naver_review": "https://shopping.naver.com/my/writable-reviews",
    "kream_review": "https://kream.co.kr/my/reviews?tab=to_write",
}

# 사이트별 지원 액션 (자동 적립 매트릭스)
SITE_REWARD_ACTIONS: dict[str, list[str]] = {
    "MUSINSA": [
        "musinsa_attendance",
        "musinsa_snap_like",
        "musinsa_balance",
        "musinsa_review",
    ],
    "ABCmart": ["abcmart_attendance", "abcmart_review"],
    "SSG": ["ssg_review"],
    "GSShop": ["gs_review"],
    "LOTTEON": ["lotteon_review"],
    "NAVERSTORE": ["naver_review"],
    "KREAM": ["kream_review"],
}


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
        _owner_tag = f" owner={owner_device_id}" if owner_device_id else ""
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
            # 데몬 전용 사이트 정책 적용 — SSG/ABC/LOTTEON 등은 데몬 없으면 None(skip)
            owner_device_id = _resolve_owner_with_daemon_policy(site)

        # 데몬 전용 사이트에 owner 없음 → 확장앱 fallback 차단, 잡 발행 skip
        if owner_device_id is None and (site or "").upper() in {
            s.upper() for s in DAEMON_ONLY_SITES
        }:
            logger.warning(
                f"[소싱큐] {site} 데몬 미등록 — 잡 발행 skip (확장앱 fallback 차단): {product_id}"
            )
            raise RuntimeError(f"{site} 데몬 미등록 — 잡 발행 불가")

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
        _owner_tag = f" owner={owner_device_id}" if owner_device_id else ""
        _prio_tag = " [우선]" if priority else ""
        logger.info(
            f"[소싱큐] 상세 추가: {site} #{product_id} (id={request_id}){_owner_tag}{_prio_tag}"
        )
        return request_id, future

    @classmethod
    async def add_tracking_job(
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

        [통일 2026-05-16] async + await — 이전 asyncio.create_task background 로
        N건 적재 시 INSERT 순서가 호출 순서와 달라져 created_at 뒤섞임 → ORDER BY
        그룹화 깨지던 회귀 차단. 단건씩 sequential 적재로 같은 계정 잡 연속 보장.
        """
        cls._ensure_accepting_jobs()
        request_id = str(uuid.uuid4())[:8]
        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()
        if owner_device_id is None:
            # 데몬 전용 사이트 정책 적용
            owner_device_id = _resolve_owner_with_daemon_policy(site)
        if owner_device_id is None and (site or "").upper() in {
            s.upper() for s in DAEMON_ONLY_SITES
        }:
            logger.warning(
                f"[소싱큐] {site} 송장 데몬 미등록 — 잡 발행 skip: ord={sourcing_order_number}"
            )
            raise RuntimeError(f"{site} 데몬 미등록 — 송장 잡 발행 불가")

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
        await _db_insert_job(job, "tracking")
        _owner_tag = f" owner={owner_device_id}" if owner_device_id else ""
        logger.info(
            f"[소싱큐] 송장조회 추가: {site} ord={sourcing_order_number} "
            f"(id={request_id}){_owner_tag}"
        )
        return request_id, future

    @classmethod
    async def add_reward_job(
        cls,
        site: str,
        action: str,
        sourcing_account_id: str,
        *,
        owner_device_id: str | None = None,
    ) -> tuple[str, asyncio.Future[Any]]:
        """적립금 자동 적립 작업 큐에 추가.

        action: 'musinsa_attendance' | 'musinsa_snap_like' | 'musinsa_balance' | 'abcmart_attendance'
        결과는 라우터 `/sourcing-accounts/extension/reward-result` 로 수신되어
        `additional_fields.last_{action}_at` / balance 갱신에 사용된다.

        송장조회와 동일하게 sequential 적재로 같은 계정 잡 연속 처리 보장.
        """
        cls._ensure_accepting_jobs()
        url = REWARD_ACTION_URLS.get(action)
        if not url:
            raise ValueError(f"지원하지 않는 적립 액션: {action}")
        request_id = str(uuid.uuid4())[:8]
        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()
        if owner_device_id is None:
            owner_device_id = get_autotune_owner(site)
        # reward 잡은 owner 안 박음 — 대신 get_next_job 에서 X-Ext-Version 헤더로
        # 옛 확장앱(reward 분기 모름) 필터링. "가장 최근 폴링 PC = 사용자 PC" 가정은
        # 멀티PC 환경에서 다른 PC가 폴링 더 자주 하면 깨짐 → 검증 실패.

        job: dict[str, Any] = {
            "requestId": request_id,
            "site": site,
            "type": "reward",
            "action": action,
            "url": url,
            "sourcingAccountId": sourcing_account_id,
            "ownerDeviceId": owner_device_id or "",
        }
        cls.resolvers[request_id] = future
        await _db_insert_job(job, "reward")
        _owner_tag = f" owner={owner_device_id}" if owner_device_id else ""
        logger.info(
            f"[소싱큐] 적립 추가: {site} action={action} acct={sourcing_account_id} "
            f"(id={request_id}){_owner_tag}"
        )
        return request_id, future

    @classmethod
    async def add_cancel_order_job(
        cls,
        site: str,
        sourcing_order_number: str,
        order_id: str,
        *,
        sourcing_account_id: str = "",
        url: str = "",
        owner_device_id: str | None = None,
    ) -> tuple[str, asyncio.Future[Any]]:
        """소싱처 발주 취소 작업 큐에 추가 (헤드리스 데몬 처리).

        - tracking 잡과 동일 패턴 — 데몬 우선 라우팅, 없으면 확장앱 폴백.
        - 결과 라우터: POST /api/v1/samba/proxy/sourcing/cancel-result
        - 결과 스키마: {success, cancelled, alreadyShipped?, reason?, error?}
        - 사이트별 cancel_js 미정의면 데몬이 "미지원" 실패 회신 — 부작용 없음.
        """
        cls._ensure_accepting_jobs()
        if not sourcing_order_number:
            raise ValueError("sourcing_order_number 필수")
        request_id = str(uuid.uuid4())[:8]
        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()
        if owner_device_id is None:
            from backend.domain.samba.proxy.daemon_pool import pick_daemon_owner

            owner_device_id = pick_daemon_owner(site) or get_autotune_owner(site)

        job: dict[str, Any] = {
            "requestId": request_id,
            "site": site,
            "type": "cancel_order",
            "url": url or "",
            "orderId": order_id,
            "sourcingOrderNumber": sourcing_order_number,
            "sourcingAccountId": sourcing_account_id or "",
            "ownerDeviceId": owner_device_id or "",
        }
        cls.resolvers[request_id] = future
        await _db_insert_job(job, "cancel_order")
        _owner_tag = f" owner={owner_device_id}" if owner_device_id else ""
        logger.info(
            f"[소싱큐] 발주취소 추가: {site} ord={sourcing_order_number} "
            f"(id={request_id}){_owner_tag}"
        )
        return request_id, future

    @classmethod
    async def get_next_job(
        cls,
        device_id: str | None = None,
        allowed_sites: list[str] | None = None,
        ext_version: str | None = None,
    ) -> dict[str, Any]:
        """DB에서 다음 작업 가져오기 (확장앱 폴링용).

        SELECT FOR UPDATE SKIP LOCKED — 멀티 PC 동시 폴링 안전.

        device_id: 해당 deviceId 소유 잡 또는 소유자 미지정 잡만 반환.
        allowed_sites:
          - None  = 전체 처리 (단일 PC 디폴트)
          - []    = 아무것도 처리 안 함
          - [...] = 해당 사이트만
        ext_version: 확장앱 버전. reward 잡은 v2.13.27 미만 PC에 안 줌
          (옛 PC는 reward 분기 모름 → '파싱 실패' 반환 회피).
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

            # reward 잡은 v2.13.27+ 확장앱만 처리 — 옛 확장앱은 type=reward 분기 코드
            # 자체가 없어서 일반 가격수집 잡으로 잘못 라우팅 → '파싱 실패' 사고
            def _ver_tuple(v: str) -> tuple[int, ...]:
                try:
                    return tuple(int(x) for x in v.split(".") if x.isdigit())
                except Exception:
                    return (0, 0, 0)

            min_reward_ver = (2, 13, 27)
            client_ver = _ver_tuple(ext_version or "")
            if client_ver < min_reward_ver:
                conditions.append("job_type != 'reward'")

            # owner 필터
            if device_id:
                conditions.append(
                    "(owner_device_id IS NULL OR owner_device_id = '' "
                    "OR owner_device_id = :device_id)"
                )
                params["device_id"] = device_id
            else:
                conditions.append("(owner_device_id IS NULL OR owner_device_id = '')")

            # 데몬 전용 사이트 가드 — LOTTEON/SSG/ABCmart/GrandStage 의 detail(상세) 잡만
            # 헤드리스 데몬이 처리. 확장앱(non-daemon)엔 detail 잡 발행 안 함 → 확장앱 팝업 0.
            # (이 사이트 상세가는 AJAX 로 늦게 채워져 확장앱은 팝업창을 띄워야만 읽힘. 데몬 headless 만
            #  팝업 없이 처리 가능.) 단 목록 수집(search)은 데몬 미지원이므로 확장앱이 계속 처리 →
            #  데몬사이트 'detail' 만 차단하고 search/tracking 등은 확장앱 허용(적체 방지).
            _DAEMON_ONLY_SITES = ("LOTTEON", "SSG", "ABCmart", "GrandStage")
            if not device_id.startswith("samba-daemon-"):
                _dph = ", ".join(f":dsite_{i}" for i in range(len(_DAEMON_ONLY_SITES)))
                conditions.append(f"NOT (site IN ({_dph}) AND job_type = 'detail')")
                for i, s in enumerate(_DAEMON_ONLY_SITES):
                    params[f"dsite_{i}"] = s

            # site 필터 — 케이싱 무관 매칭.
            # detail 잡 site='ABCmart'(혼합)인데 tracking 잡 site='ABCMART'(대문자)라
            # 데몬 폴링(X-Poll-Site='ABCmart')이 ABCMART tracking 잡을 dequeue 하려면
            # UPPER 양쪽 비교 필요. 사이트명 충돌 없어 안전.
            if allowed_sites is not None:
                site_list = [s.strip() for s in allowed_sites if s.strip()]
                placeholders = ", ".join(f":site_{i}" for i in range(len(site_list)))
                conditions.append(f"UPPER(site) IN ({placeholders})")
                for i, s in enumerate(site_list):
                    params[f"site_{i}"] = s.upper()

            where = " AND ".join(conditions)
            # [중요] 모달 리스트 정렬과 동일 순서 — site → sourcing_account_id → created_at.
            # 같은 사이트/계정 잡을 연속으로 dequeue 해서 자동 로그인 스왑 횟수 = 계정 수로 최소화.
            # 사용자가 모달 1번부터 본 순서 그대로 처리됨 (예측 가능).
            # NULL/빈 값은 가장 뒤로 (NULLS LAST). 같은 계정 안에서는 created_at ASC FIFO.
            sql = text(
                f"SELECT request_id, payload FROM samba_sourcing_job "
                f"WHERE {where} "
                f"ORDER BY "
                f"  site ASC NULLS LAST, "
                f"  NULLIF(payload->>'sourcingAccountId', '') ASC NULLS LAST, "
                f"  created_at ASC "
                f"LIMIT 1 "
                f"FOR UPDATE SKIP LOCKED"
            )

            async with get_write_session() as session:
                row = (await session.execute(sql, params)).fetchone()
                if not row:
                    return {"hasJob": False}

                request_id, payload = row
                # owner_device_id가 NULL/빈 잡(reward 등)은 클레이밍 device_id를 기록 —
                # 어느 PC가 잡 가져갔는지 추적용. 기존 소유자 잡은 owner 유지.
                await session.execute(
                    text(
                        "UPDATE samba_sourcing_job "
                        "SET status = 'dispatched', dispatched_at = now(), "
                        "    owner_device_id = COALESCE(NULLIF(owner_device_id, ''), :did) "
                        "WHERE request_id = :rid"
                    ),
                    {"rid": request_id, "did": device_id or None},
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

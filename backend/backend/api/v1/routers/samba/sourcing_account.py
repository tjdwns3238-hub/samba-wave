"""소싱처 계정 API 라우터."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from sqlmodel import select

from backend.db.orm import get_read_session_dependency, get_write_session_dependency
from backend.domain.samba.tenant.middleware import (
    get_optional_tenant_id,
    check_sourcing_limit,
)
from backend.dtos.samba.sourcing_account import (
    SourcingAccountCreate,
    SourcingAccountUpdate,
)
from backend.utils.logger import logger
from backend.utils.masking import mask_model_secrets

router = APIRouter(prefix="/sourcing-accounts", tags=["samba-sourcing-accounts"])

# 확장앱 전용 라우터 — JWT 인증 불필요 (X-Api-Key 헤더만 사용)
extension_router = APIRouter(
    prefix="/sourcing-accounts", tags=["samba-sourcing-accounts-extension"]
)


def _normalize_sourcing_site_name(site_name: str | None) -> str:
    raw = (site_name or "").strip()
    if not raw:
        return ""

    compact = raw.replace(" ", "").replace("_", "").replace("-", "").upper()
    alias_map = {
        "LOTTEON": "LOTTEON",
        "롯데ON": "LOTTEON",
        "롯데온": "LOTTEON",
        "GSSHOP": "GSShop",
        "GS샵": "GSShop",
        "ABCMART": "ABCmart",
        "ABC마트": "ABCmart",
        "SSG": "SSG",
        "MUSINSA": "MUSINSA",
        "무신사": "MUSINSA",
        "KREAM": "KREAM",
        "크림": "KREAM",
        "NIKE": "Nike",
        "나이키": "Nike",
        "ADIDAS": "Adidas",
        "아디다스": "Adidas",
        "FASHIONPLUS": "FashionPlus",
        "패션플러스": "FashionPlus",
        "OLIVEYOUNG": "OliveYoung",
        "올리브영": "OliveYoung",
        "DANAWA": "DANAWA",
        "다나와": "DANAWA",
        "NAVERSTORE": "NAVERSTORE",
        "네이버스토어": "NAVERSTORE",
    }
    return alias_map.get(compact, raw)


def _read_service(session: AsyncSession):
    from backend.domain.samba.sourcing_account.repository import (
        SambaSourcingAccountRepository,
    )
    from backend.domain.samba.sourcing_account.service import (
        SambaSourcingAccountService,
    )

    return SambaSourcingAccountService(SambaSourcingAccountRepository(session))


def _write_service(session: AsyncSession):
    from backend.domain.samba.sourcing_account.repository import (
        SambaSourcingAccountRepository,
    )
    from backend.domain.samba.sourcing_account.service import (
        SambaSourcingAccountService,
    )

    return SambaSourcingAccountService(SambaSourcingAccountRepository(session))


@router.get("")
async def list_sourcing_accounts(
    site_name: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    from backend.domain.samba.sourcing_account.model import SambaSourcingAccount

    # tenant_id가 있으면 해당 테넌트 + 기존(NULL) 소싱처 계정 모두 조회
    if tenant_id is not None:
        from sqlalchemy import or_

        stmt = select(SambaSourcingAccount).order_by(
            SambaSourcingAccount.created_at.desc()
        )
        stmt = stmt.where(
            or_(
                SambaSourcingAccount.tenant_id == tenant_id,
                SambaSourcingAccount.tenant_id == None,  # noqa: E711
            )
        )
        if site_name:
            stmt = stmt.where(SambaSourcingAccount.site_name == site_name)
        result = await session.execute(stmt)
        accounts = result.scalars().all()
        return [mask_model_secrets(a.model_dump()) for a in accounts]
    accounts = await _read_service(session).list_accounts(site_name=site_name)
    return [mask_model_secrets(a.model_dump()) for a in accounts]


@router.get("/sites")
async def get_supported_sites():
    from backend.domain.samba.sourcing_account.service import (
        SambaSourcingAccountService,
    )

    return SambaSourcingAccountService.get_supported_sites()


@router.get("/chrome-profiles")
async def get_chrome_profiles(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """DB에 동기화된 크롬 프로필 목록 반환 (확장앱이 시작 시 자동 등록)."""
    from backend.domain.samba.sourcing_account.model import SambaChromProfile

    if tenant_id is not None:
        from sqlalchemy import or_

        stmt = (
            select(SambaChromProfile)
            .where(
                or_(
                    SambaChromProfile.tenant_id == tenant_id,
                    SambaChromProfile.tenant_id == None,  # noqa: E711
                )
            )
            .order_by(SambaChromProfile.email)
        )
    else:
        stmt = select(SambaChromProfile).order_by(SambaChromProfile.email)

    result = await session.execute(stmt)
    profiles = result.scalars().all()

    return [
        {
            # 기존 인터페이스 호환 유지 (directory/name/gaia_name)
            "directory": p.email,
            "name": p.display_name or p.email.split("@")[0],
            "gaia_name": p.display_name or "",
            # 신규 필드
            "email": p.email,
            "display_name": p.display_name or p.email.split("@")[0],
        }
        for p in profiles
    ]


# 잔액 체크 요청 플래그 (확장앱이 폴링으로 확인)
_balance_check_requested = False
_CHROME_PROFILE_SYNC_REQUEST_KEY = "__chrome_profile_sync_requested__"


@router.post("/request-balance-check")
async def request_balance_check():
    """프론트에서 잔액 체크 요청 → 확장앱이 폴링으로 확인 후 실행."""
    global _balance_check_requested
    _balance_check_requested = True
    return {"ok": True}


@router.get("/balance-check-requested")
async def get_balance_check_requested():
    """확장앱이 폴링으로 확인하는 잔액 체크 요청 플래그."""
    global _balance_check_requested
    if _balance_check_requested:
        _balance_check_requested = False
        return {"requested": True}
    return {"requested": False}


@router.post("/request-chrome-profile-sync")
async def request_chrome_profile_sync(
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """확장앱에 크롬 프로필 동기화를 요청한다."""
    from backend.domain.samba.forbidden.model import SambaSettings

    stmt = select(SambaSettings).where(
        SambaSettings.key == _CHROME_PROFILE_SYNC_REQUEST_KEY
    )
    result = await session.execute(stmt)
    existing = result.scalars().first()
    now = datetime.now(timezone.utc)

    if existing:
        existing.value = {"requested": True, "requested_at": now.isoformat()}
        existing.updated_at = now
        session.add(existing)
    else:
        session.add(
            SambaSettings(
                key=_CHROME_PROFILE_SYNC_REQUEST_KEY,
                value={"requested": True, "requested_at": now.isoformat()},
                updated_at=now,
            )
        )
    await session.commit()
    return {"ok": True}


@router.get("/chrome-profile-sync-requested")
async def get_chrome_profile_sync_requested(
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """확장앱이 소비할 크롬 프로필 동기화 요청 여부."""
    from backend.domain.samba.forbidden.model import SambaSettings

    stmt = select(SambaSettings).where(
        SambaSettings.key == _CHROME_PROFILE_SYNC_REQUEST_KEY
    )
    result = await session.execute(stmt)
    existing = result.scalars().first()
    if (
        existing
        and isinstance(existing.value, dict)
        and existing.value.get("requested")
    ):
        existing.value = {
            "requested": False,
            "consumed_at": datetime.now(timezone.utc).isoformat(),
        }
        existing.updated_at = datetime.now(timezone.utc)
        session.add(existing)
        await session.commit()
        return {"requested": True}
    return {"requested": False}


@router.get("/{account_id}")
async def get_sourcing_account(
    account_id: str,
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    svc = _read_service(session)
    account = await svc.get_account(account_id)
    if not account:
        raise HTTPException(404, "소싱처 계정을 찾을 수 없습니다")
    # IDOR 방지: 테넌트 소유권 검증
    if tenant_id is not None and account.tenant_id != tenant_id:
        raise HTTPException(403, "해당 계정에 대한 권한이 없습니다")
    return mask_model_secrets(account.model_dump())


@router.post("", status_code=201)
async def create_sourcing_account(
    body: SourcingAccountCreate,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    # 티어 제한 체크 — 소싱 계정 수
    if tenant_id:
        await check_sourcing_limit(tenant_id, session)
    data = body.model_dump(exclude_unset=True)
    # tenant_id가 있으면 신규 소싱처 계정에 테넌트 정보 설정
    if tenant_id is not None:
        data["tenant_id"] = tenant_id
    return await _write_service(session).create_account(data)


@router.put("/{account_id}")
async def update_sourcing_account(
    account_id: str,
    body: SourcingAccountUpdate,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    svc = _write_service(session)
    # tenant_id가 있으면 소유권 검증
    if tenant_id is not None:
        existing = await svc.get_account(account_id)
        if not existing:
            raise HTTPException(404, "소싱처 계정을 찾을 수 없습니다")
        if existing.tenant_id != tenant_id:
            raise HTTPException(403, "해당 계정에 대한 권한이 없습니다")
    result = await svc.update_account(account_id, body.model_dump(exclude_unset=True))
    if not result:
        raise HTTPException(404, "소싱처 계정을 찾을 수 없습니다")
    return result


@router.put("/{account_id}/toggle")
async def toggle_sourcing_account(
    account_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    svc = _write_service(session)
    # tenant_id가 있으면 소유권 검증
    if tenant_id is not None:
        existing = await svc.get_account(account_id)
        if not existing:
            raise HTTPException(404, "소싱처 계정을 찾을 수 없습니다")
        if existing.tenant_id != tenant_id:
            raise HTTPException(403, "해당 계정에 대한 권한이 없습니다")
    result = await svc.toggle_active(account_id)
    if not result:
        raise HTTPException(404, "소싱처 계정을 찾을 수 없습니다")
    return result


@router.put("/{account_id}/set-login-default")
async def set_login_default_account(
    account_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    """자동로그인 기본 계정 지정 — 사이트당 1개 라디오 동작.
    같은 site_name의 다른 계정은 자동으로 is_login_default=false 처리됨.
    """
    svc = _write_service(session)
    # tenant_id가 있으면 소유권 검증
    if tenant_id is not None:
        existing = await svc.get_account(account_id)
        if not existing:
            raise HTTPException(404, "소싱처 계정을 찾을 수 없습니다")
        if existing.tenant_id != tenant_id:
            raise HTTPException(403, "해당 계정에 대한 권한이 없습니다")
    result = await svc.set_login_default(account_id, tenant_id=tenant_id)
    if not result:
        raise HTTPException(404, "소싱처 계정을 찾을 수 없습니다")
    return mask_model_secrets(result.model_dump())


@router.delete("/{account_id}")
async def delete_sourcing_account(
    account_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    svc = _write_service(session)
    # tenant_id가 있으면 소유권 검증
    if tenant_id is not None:
        existing = await svc.get_account(account_id)
        if not existing:
            raise HTTPException(404, "소싱처 계정을 찾을 수 없습니다")
        if existing.tenant_id != tenant_id:
            raise HTTPException(403, "해당 계정에 대한 권한이 없습니다")
    if not await svc.delete_account(account_id):
        raise HTTPException(404, "소싱처 계정을 찾을 수 없습니다")
    return {"ok": True}


class SyncMembershipRequest(BaseModel):
    site_name: str
    membership_rate: float
    membership_grade: str = ""
    # 확장앱이 추출한 a-rt.com 로그인 쿠키 (옵션)
    # 'k1=v1; k2=v2' 형식. 잡 시작 시 ABCmart 호출에 주입되어 alwaysDscntAmt 등 정확값 수신
    cookie: Optional[str] = None
    expired: bool = False


async def _sync_abcmart_cookie_to_settings(
    session: AsyncSession,
    accounts: list,
) -> None:
    """ABCmart 모든 계정의 만료되지 않은 쿠키 → SambaSettings.abcmart_cookies 동기화.

    proxy/abcmart.py의 prepare_abcmart_cache()가 SambaSettings만 읽으므로,
    확장앱이 sync한 쿠키가 잡에 반영되려면 이 동기화가 필요.
    """
    import json

    from backend.domain.samba.forbidden.model import SambaSettings

    cookies: list[str] = []
    for a in accounts:
        af = a.additional_fields or {}
        cookie_val = af.get("abcmart_cookie", "")
        if cookie_val and not af.get("cookie_expired"):
            cookies.append(cookie_val)

    try:
        result = await session.execute(
            select(SambaSettings).where(SambaSettings.key == "abcmart_cookies")
        )
        row = result.scalar_one_or_none()
        if row:
            row.value = json.dumps(cookies)
        else:
            session.add(SambaSettings(key="abcmart_cookies", value=json.dumps(cookies)))
        logger.info(
            f"[ABCmart쿠키동기화] SambaSettings.abcmart_cookies 업데이트: {len(cookies)}개"
        )
    except Exception as e:
        logger.warning(f"[ABCmart쿠키동기화] SambaSettings 업데이트 실패 (무시): {e}")


@extension_router.post("/sync-membership")
async def sync_membership_from_extension(
    body: SyncMembershipRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """확장앱에서 멤버십 등급 + 로그인 쿠키 수신 → 소싱처 계정에 저장.

    멤버십 rate는 더 이상 곱셈 계산에 쓰지 않음 (참고용 메타데이터).
    실제 cost 계산은 잡 시작 시 로딩한 쿠키로 API 호출 → alwaysDscntAmt 사용.
    """
    svc = _write_service(session)
    accounts = await svc.list_accounts(site_name=body.site_name)

    for account in accounts:
        extra = dict(account.additional_fields or {})
        extra["membership_rate"] = body.membership_rate
        extra["membership_grade"] = body.membership_grade

        if body.expired:
            extra["cookie_expired"] = True
            extra["cookie_expired_at"] = datetime.now(timezone.utc).isoformat()
        elif body.cookie:
            extra["abcmart_cookie"] = body.cookie
            extra["cookie_expired"] = False
            extra["cookie_updated_at"] = datetime.now(timezone.utc).isoformat()

        await svc.repo.update_async(account.id, additional_fields=extra)

    # ABCmart 쿠키가 있으면 SambaSettings에도 동기화 (잡 캐시가 읽음)
    if body.site_name == "ABCmart" and (body.cookie or body.expired):
        # 최신 상태 다시 읽어서 동기화
        accounts = await svc.list_accounts(site_name=body.site_name)
        await _sync_abcmart_cookie_to_settings(session, accounts)

    logger.info(
        f"[멤버십동기화] {body.site_name}: {body.membership_grade} "
        f"({body.membership_rate}%) cookie={'expired' if body.expired else ('set' if body.cookie else 'none')}"
    )
    return {
        "ok": True,
        "rate": body.membership_rate,
        "grade": body.membership_grade,
        "cookie_synced": bool(body.cookie),
        "expired": body.expired,
    }


class SyncBalanceRequest(BaseModel):
    money: float = 0
    mileage: float = 0
    profileEmail: Optional[str] = None
    username: Optional[str] = None
    cookie: Optional[str] = None
    expired: bool = False


async def _sync_musinsa_cookie_to_settings(
    session: AsyncSession,
    new_cookie: str,
    all_accounts: list,
) -> None:
    """SambaSourcingAccount 쿠키를 SambaSettings.musinsa_cookies 배열에 동기화.

    refresher.py의 _get_musinsa_cookies()는 SambaSettings를 읽으므로,
    확장앱이 자동 갱신한 쿠키가 오토튠에 반영되려면 이 동기화가 필요.
    반드시 _set_setting을 통해 저장해 암호화 키 자동 적용 (직접 SQL 금지 —
    암호화/복호화 경로 불일치로 무신사가 쿠키를 못 읽는 이슈 방지).
    """
    import json

    from backend.api.v1.routers.samba.proxy._helpers import _set_setting

    # 모든 활성 무신사 계정의 쿠키 수집 (만료되지 않은 것만)
    cookies: list[str] = []
    for a in all_accounts:
        af = a.additional_fields or {}
        cookie_val = af.get("musinsa_cookie", "")
        if cookie_val and not af.get("cookie_expired"):
            cookies.append(cookie_val)

    # 새 쿠키가 목록에 없으면 맨 앞에 추가
    if new_cookie not in cookies:
        cookies.insert(0, new_cookie)

    if not cookies:
        return

    try:
        await _set_setting(session, "musinsa_cookies", json.dumps(cookies))
        logger.info(
            f"[쿠키동기화] SambaSettings.musinsa_cookies 업데이트: {len(cookies)}개"
        )
    except Exception as e:
        logger.warning(f"[쿠키동기화] SambaSettings 업데이트 실패 (무시): {e}")


@extension_router.post("/sync-balance")
async def sync_balance_from_extension(
    body: SyncBalanceRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """확장앱에서 잔액 수신 → 크롬 프로필 Gmail로 계정 매칭 → 저장."""
    svc = _write_service(session)
    accounts = await svc.list_accounts(site_name="MUSINSA")
    matched = None

    # 1순위: 크롬 프로필 Gmail(memo 필드)로 매칭
    if body.profileEmail:
        matched = next(
            (
                a
                for a in accounts
                if a.memo and a.memo.lower() == body.profileEmail.lower()
            ),
            None,
        )

    # 2순위: 쿠키 문자열에 아이디가 포함되어 있는지 확인
    if not matched and body.cookie:
        for a in accounts:
            if a.username and a.username in body.cookie:
                matched = a
                break

    if not matched:
        logger.warning(
            f"[잔액동기화] 매칭 실패: email={body.profileEmail}, username={body.username}"
        )
        # 매칭 실패해도 쿠키는 refresher 풀(SambaSettings.musinsa_cookies)에 저장
        # — 소싱처 계정 미등록 상태(포크/신규 인스턴스)에서도 최대혜택가 계산 가능하도록
        if body.cookie and not body.expired:
            await _sync_musinsa_cookie_to_settings(session, body.cookie, accounts)
            logger.info(
                "[잔액동기화] 매칭 실패 — 쿠키만 SambaSettings.musinsa_cookies에 저장"
            )
        return {
            "ok": False,
            "cookie_saved": bool(body.cookie and not body.expired),
            "message": f"계정을 찾을 수 없습니다: {body.profileEmail or body.username}",
        }

    from datetime import datetime, timezone

    extra = dict(matched.additional_fields or {})

    if body.expired:
        # 쿠키 만료 처리
        extra["cookie_expired"] = True
        extra["cookie_expired_at"] = datetime.now(timezone.utc).isoformat()
        await svc.repo.update_async(matched.id, additional_fields=extra)
        logger.warning(
            f"[잔액동기화] {matched.account_label}: 쿠키 만료 — 재로그인 필요"
        )
        return {"ok": True, "account_label": matched.account_label, "expired": True}

    # 잔액 + 쿠키 저장
    extra["mileage"] = body.mileage
    extra["cookie_expired"] = False
    if body.cookie:
        extra["musinsa_cookie"] = body.cookie
        extra["cookie_updated_at"] = datetime.now(timezone.utc).isoformat()
    await svc.repo.update_async(
        matched.id,
        balance=body.money,
        balance_updated_at=datetime.now(timezone.utc),
        additional_fields=extra,
    )
    # refresher가 읽는 SambaSettings.musinsa_cookies에도 동기화
    if body.cookie:
        await _sync_musinsa_cookie_to_settings(session, body.cookie, accounts)
    logger.info(
        f"[잔액동기화] {matched.account_label}: 머니 {body.money:,.0f} / 적립금 {body.mileage:,.0f}"
    )
    return {
        "ok": True,
        "account_label": matched.account_label,
        "money": body.money,
        "mileage": body.mileage,
    }


@router.get("/{account_id}/balance")
async def get_balance(
    account_id: str,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """계정의 저장된 잔액 조회 (확장앱이 수집한 데이터)."""
    svc = _read_service(session)
    account = await svc.get_account(account_id)
    if not account:
        raise HTTPException(404, "소싱처 계정을 찾을 수 없습니다")
    extra = account.additional_fields or {}
    return {
        "balance": account.balance,
        "mileage": extra.get("mileage"),
        "balance_updated_at": account.balance_updated_at,
        "cookie_updated_at": extra.get("cookie_updated_at"),
        "has_cookie": bool(extra.get("musinsa_cookie")),
    }


# ==================== 확장앱 전용 엔드포인트 (extension_router) ====================


class SyncChromeProfileRequest(BaseModel):
    email: str
    gaia_id: Optional[str] = None
    display_name: Optional[str] = None


class ExtensionKeyRequest(BaseModel):
    gaia_id: str = ""
    email: str = ""


# 확장앱 키 발급 IP 레이트리밋 — 무인증 부트스트랩 엔드포인트의 1차 방어선.
# 정식 해결책(테넌트별 키, 사용자 JWT 발급) 도입 전 한시 보강.
_EXT_KEY_RATE_LIMIT_WINDOW_S = 60  # 초
_EXT_KEY_RATE_LIMIT_COUNT = 10  # 분당 IP당 허용 횟수
_ext_key_rate_log: dict[str, list[float]] = {}
_ext_key_rate_lock_inited = False


@extension_router.post("/extension-key")
async def get_extension_key(request: Request):
    """확장앱 API 키 발급 — 키 자체가 인증 수단이므로 사용자 검증 불필요.

    보안 보강 (2026-05-09):
      - IP당 1분 10회 레이트리밋
      - User-Agent / Origin 누락 시 경고 로그 (차단은 안 함 — 정식 확장앱 호환 유지)
      - 정식 해결책(JWT 또는 tenant-scoped 키) 도입 전 한시 조치
    """
    import time as _time
    import logging as _logging

    from backend.core.config import settings
    from backend.core.rate_limit import _client_key

    _logger = _logging.getLogger(__name__)
    # Caddy 리버스 프록시 뒤라 request.client.host는 docker bridge IP로 뭉침 →
    # X-Forwarded-For 마지막 IP 사용 (rate_limit._client_key와 동일 정책).
    client_ip = _client_key(request)
    now = _time.time()
    cutoff = now - _EXT_KEY_RATE_LIMIT_WINDOW_S
    bucket = _ext_key_rate_log.setdefault(client_ip, [])
    # 윈도우 밖 타임스탬프 정리
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= _EXT_KEY_RATE_LIMIT_COUNT:
        _logger.warning(
            f"[extension-key] IP {client_ip} 레이트리밋 차단 ({len(bucket)}/{_EXT_KEY_RATE_LIMIT_COUNT})"
        )
        raise HTTPException(429, "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.")
    bucket.append(now)

    # 부트스트랩 출처 흔적 — 비정상 호출 패턴 감지용 (차단은 하지 않음)
    ua = request.headers.get("User-Agent", "")
    origin = request.headers.get("Origin", "")
    if "chrome-extension" not in origin and "Mozilla" not in ua:
        _logger.warning(
            f"[extension-key] 비정상 호출 의심 ip={client_ip} ua={ua[:80]} origin={origin[:80]}"
        )

    return {"api_key": settings.api_gateway_key}


@extension_router.get("/login-credential")
async def get_login_credential(
    site_name: str | None = Query(
        None,
        description="사이트 ID (예: LOTTEON, ABCmart, SSG) — account_id 없을 때 라디오 기본 계정 조회",
    ),
    account_id: str | None = Query(
        None,
        description="특정 계정 ID — 주문 매칭 계정으로 로그인할 때 사용 (우선 적용)",
    ),
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """확장앱 자동로그인 전용 — 계정 자격증명(평문) 반환.

    조회 우선순위:
      1. account_id 제공 시 → 해당 계정 단건 조회 (is_active 무관, 만료 계정도 시도 허용)
      2. account_id 없으면 → site_name + is_active=true + is_login_default=true 라디오 기본 계정

    찾지 못하면 404 반환.

    인증/테넌트 정책:
    - extension_router는 X-Api-Key 헤더 인증만 사용 (JWT 토큰 없음)
    - tenant_id Depends 제거 — 다른 extension 엔드포인트(sync-membership 등)와 동일 패턴
    - 단일 사용자 환경 또는 NULL tenant 범위에서 작동.
      멀티테넌트 환경에서는 X-Api-Key가 동일하게 글로벌이므로 모든 활성 default 계정 중
      첫 번째를 가져옴 (운영상 site_name당 1개만 존재한다는 전제).

    보안 메모:
    - 평문 username/password 노출 (Chrome 자동완성 불가능한 SPA 사이트의 직접 .value 설정용)
    - DB 자체에 평문 저장된 자격증명을 그대로 전달 — 새로운 보안 위험 추가 없음.
    """
    from sqlalchemy import select as sa_select
    from backend.domain.samba.sourcing_account.model import SambaSourcingAccount

    account = None

    # 1) account_id 우선 — 주문 매칭 계정으로 단건 조회
    if account_id:
        account = await session.get(SambaSourcingAccount, account_id)
        if not account:
            raise HTTPException(
                404,
                f"계정을 찾을 수 없습니다: account_id={account_id}",
            )
        return {
            "id": account.id,
            "site_name": account.site_name,
            "account_label": account.account_label,
            "username": account.username,
            "password": account.password,
        }

    # 2) site_name 기반 라디오 기본 계정 조회 (legacy)
    if not site_name:
        raise HTTPException(400, "site_name 또는 account_id 중 하나는 필수입니다")

    normalized_site_name = _normalize_sourcing_site_name(site_name)
    site_candidates = [
        candidate
        for candidate in dict.fromkeys(
            [
                site_name,
                normalized_site_name,
                site_name.upper(),
                site_name.lower(),
            ]
        )
        if candidate
    ]

    stmt = (
        sa_select(SambaSourcingAccount)
        .where(SambaSourcingAccount.site_name.in_(site_candidates))
        .where(SambaSourcingAccount.is_active.is_(True))
        .where(SambaSourcingAccount.is_login_default.is_(True))
        .order_by(
            SambaSourcingAccount.updated_at.desc(),
            SambaSourcingAccount.created_at.desc(),
        )
    )
    result = await session.execute(stmt)
    account = result.scalars().first()

    if not account:
        raise HTTPException(
            404,
            f"{normalized_site_name or site_name} 자동로그인 기본 계정 없음 — 설정 페이지에서 기본 계정을 지정해 주세요.",
        )
    return {
        "id": account.id,
        "site_name": account.site_name,
        "account_label": account.account_label,
        "username": account.username,
        "password": account.password,
    }


@extension_router.get("/find-by-username")
async def find_account_by_username(
    site_name: str = Query(..., description="사이트 ID (예: MUSINSA, LOTTEON)"),
    username: str = Query(..., description="현재 브라우저에 로그인된 사용자 식별자 (아이디/이메일/닉네임)"),
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """확장앱 전용 — 현재 로그인된 username을 SambaSourcingAccount의 account_id로 매핑.

    송장수집 시 확장앱이 현재 로그인 계정을 식별 후 매칭 잡을 우선 처리하려면
    "현재 로그인된 username 문자열" → "백엔드 account_id" 변환이 필요.
    이 엔드포인트는 site_name + username 으로 단건 매칭 후 account_id를 반환.

    조회 우선순위:
      1. 정확 매칭 (account.username == username)
      2. account_label 매칭 (사용자가 라벨에 username을 적어두는 케이스 대응)

    찾지 못하면 404. is_active 무관 (만료 계정도 매칭 허용 — 일단 식별만이 목적).
    """
    from sqlalchemy import select as sa_select, or_
    from backend.domain.samba.sourcing_account.model import SambaSourcingAccount

    if not username.strip():
        raise HTTPException(400, "username 비어있음")

    normalized_site_name = _normalize_sourcing_site_name(site_name)
    site_candidates = [
        candidate
        for candidate in dict.fromkeys(
            [
                site_name,
                normalized_site_name,
                site_name.upper(),
                site_name.lower(),
            ]
        )
        if candidate
    ]

    stmt = (
        sa_select(SambaSourcingAccount)
        .where(SambaSourcingAccount.site_name.in_(site_candidates))
        .where(
            or_(
                SambaSourcingAccount.username == username,
                SambaSourcingAccount.account_label == username,
            )
        )
        .order_by(
            SambaSourcingAccount.is_active.desc(),
            SambaSourcingAccount.updated_at.desc(),
        )
        .limit(1)
    )
    account = (await session.execute(stmt)).scalars().first()
    if not account:
        raise HTTPException(
            404,
            f"매칭 계정 없음: site={site_name} username={username}",
        )
    return {
        "id": account.id,
        "site_name": account.site_name,
        "account_label": account.account_label,
        "username": account.username,
    }


@extension_router.post("/sync-chrome-profile")
async def sync_chrome_profile(
    body: SyncChromeProfileRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """확장앱에서 크롬 프로필 동기화 — email 기반 upsert."""
    from backend.domain.samba.sourcing_account.model import SambaChromProfile

    if not body.email:
        return {"ok": False, "message": "이메일이 비어 있습니다"}

    # email로 기존 레코드 조회
    stmt = select(SambaChromProfile).where(SambaChromProfile.email == body.email)
    result = await session.execute(stmt)
    existing = result.scalars().first()

    now = datetime.now(timezone.utc)

    if existing:
        # 기존 레코드 업데이트
        existing.last_seen_at = now
        if body.gaia_id:
            existing.gaia_id = body.gaia_id
        if body.display_name:
            existing.display_name = body.display_name
        session.add(existing)
        await session.commit()
        logger.info(f"[크롬프로필] 갱신: {body.email}")
        return {"ok": True, "email": existing.email, "action": "updated"}
    else:
        # 새 레코드 생성
        profile = SambaChromProfile(
            email=body.email,
            gaia_id=body.gaia_id,
            display_name=body.display_name or body.email.split("@")[0],
            last_seen_at=now,
        )
        session.add(profile)
        await session.commit()
        logger.info(f"[크롬프로필] 신규 등록: {body.email}")
        return {"ok": True, "email": body.email, "action": "created"}

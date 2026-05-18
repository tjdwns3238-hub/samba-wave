"""삼바웨이브 사용자(로그인 계정) 관리 API."""

import os
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.core.rate_limit import RATE_LOGIN, limiter
from backend.db.orm import get_read_session_dependency, get_write_session_dependency
from backend.domain.samba.user.model import SambaLoginHistory, SambaUser
from backend.domain.samba.user.repository import SambaUserRepository
from backend.domain.samba.tenant.middleware import require_admin
from backend.utils.logger import logger
from backend.utils.password import hash_password, verify_password

router = APIRouter(prefix="/users", tags=["samba-users"])


# ── DTO ──

INVITE_CODE = os.environ.get("SAMBA_INVITE_CODE", "samba_wave")


class UserCreateDto(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6)
    name: str = Field(..., min_length=1, max_length=50)
    invite_code: str = Field("", description="초대 코드")


class UserLoginDto(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)


class UserUpdateDto(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = Field(None, min_length=6)
    status: Optional[str] = None


class UserOut(BaseModel):
    id: str
    email: Optional[str] = None
    name: Optional[str] = None
    is_admin: bool = False
    status: str = "active"
    created_at: str
    updated_at: str
    access_token: Optional[str] = None


# ── 엔드포인트 ──


@router.get("", response_model=list[UserOut])
async def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_read_session_dependency),
    _admin_id: str = Depends(require_admin),
):
    """활성 사용자 목록 조회 (삭제된 사용자 제외)."""
    stmt = (
        select(SambaUser)
        .where(SambaUser.deleted_at.is_(None))
        .order_by(SambaUser.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await session.execute(stmt)
    users = result.scalars().all()
    return [
        UserOut(
            id=u.id,
            email=u.email,
            name=u.name,
            is_admin=u.is_admin,
            status=u.status,
            created_at=u.created_at.isoformat(),
            updated_at=u.updated_at.isoformat(),
        )
        for u in users
    ]


@router.post("", response_model=UserOut, status_code=201)
async def create_user(
    body: UserCreateDto,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """새 사용자 계정 생성 — 초대코드 검증 + 테넌트 자동 생성."""
    # 초대 코드 검증 (프로덕션 보호는 초대 코드로 대체)
    if body.invite_code != INVITE_CODE:
        raise HTTPException(status_code=403, detail="초대 코드가 올바르지 않습니다")

    repo = SambaUserRepository(session)
    hashed = hash_password(body.password)

    # 이메일 중복 검사 (삭제된 계정 포함)
    any_existing = await repo.find_by_email_any(body.email)
    if any_existing and any_existing.deleted_at is None:
        raise HTTPException(status_code=409, detail="이미 등록된 이메일입니다")

    from backend.domain.samba.tenant.model import SambaTenant

    if any_existing and any_existing.deleted_at is not None:
        # 탈퇴한 계정 — 비밀번호·이름 재설정 + 새 테넌트로 부활
        user = any_existing
        user.deleted_at = None
        user.password_hash = hashed
        user.name = body.name
        user.status = "active"
        user.is_admin = False
        logger.info(f"[사용자관리] 탈퇴 계정 복구: {user.email}")
    else:
        user = await repo.create_async(
            email=body.email,
            name=body.name,
            password_hash=hashed,
            is_admin=False,
            status="active",
        )

    # 신규 테넌트 자동 생성 → 데이터 격리 보장
    tenant = SambaTenant(
        name=body.name,
        owner_user_id=user.id,
        plan="free",
    )
    session.add(tenant)
    await session.flush()  # tenant.id 확정

    # 사용자에 tenant_id 연결 + owner 역할 부여
    user.tenant_id = tenant.id
    user.role = "owner"
    session.add(user)
    await session.commit()
    await session.refresh(user)

    logger.info(f"[사용자관리] 계정 생성: {user.email} / 테넌트 {tenant.id} 자동 생성")

    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        is_admin=user.is_admin,
        status=user.status,
        created_at=user.created_at.isoformat(),
        updated_at=user.updated_at.isoformat(),
    )


_KR_REGION = {
    "Seoul": "서울",
    "Busan": "부산",
    "Daegu": "대구",
    "Incheon": "인천",
    "Gwangju": "광주",
    "Daejeon": "대전",
    "Ulsan": "울산",
    "Sejong": "세종",
    "Gyeonggi-do": "경기",
    "Gangwon-do": "강원",
    "Chungcheongbuk-do": "충북",
    "Chungcheongnam-do": "충남",
    "Jeollabuk-do": "전북",
    "Jeollanam-do": "전남",
    "Gyeongsangbuk-do": "경북",
    "Gyeongsangnam-do": "경남",
    "Jeju-do": "제주",
    "North Chungcheong": "충북",
    "South Chungcheong": "충남",
    "North Jeolla": "전북",
    "South Jeolla": "전남",
    "North Gyeongsang": "경북",
    "South Gyeongsang": "경남",
}
_KR_CITY = {
    "Suwon": "수원",
    "Seongnam": "성남",
    "Goyang": "고양",
    "Yongin": "용인",
    "Bucheon": "부천",
    "Ansan": "안산",
    "Anyang": "안양",
    "Namyangju": "남양주",
    "Hwaseong": "화성",
    "Uijeongbu": "의정부",
    "Gimpo": "김포",
    "Gwangmyeong": "광명",
    "Hanam": "하남",
    "Siheung": "시흥",
    "Gunpo": "군포",
    "Osan": "오산",
    "Icheon": "이천",
    "Paju": "파주",
    "Pyeongtaek": "평택",
    "Yangju": "양주",
    "Changwon": "창원",
    "Gimhae": "김해",
    "Jinju": "진주",
    "Yangsan": "양산",
    "Geoje": "거제",
    "Tongyeong": "통영",
    "Sacheon": "사천",
    "Miryang": "밀양",
    "Pohang": "포항",
    "Gumi": "구미",
    "Gimcheon": "김천",
    "Andong": "안동",
    "Yeongju": "영주",
    "Sangju": "상주",
    "Gyeongju": "경주",
    "Gyeongsan": "경산",
    "Cheonan": "천안",
    "Asan": "아산",
    "Seosan": "서산",
    "Dangjin": "당진",
    "Cheongju": "청주",
    "Chungju": "충주",
    "Jecheon": "제천",
    "Jeonju": "전주",
    "Gunsan": "군산",
    "Iksan": "익산",
    "Namwon": "남원",
    "Yeosu": "여수",
    "Suncheon": "순천",
    "Mokpo": "목포",
    "Gwangyang": "광양",
    "Chuncheon": "춘천",
    "Wonju": "원주",
    "Gangneung": "강릉",
    "Sokcho": "속초",
    "Donghae": "동해",
    "Samcheok": "삼척",
    "Taebaek": "태백",
    "Jeju City": "제주시",
    "Seogwipo": "서귀포",
}


async def _resolve_ip_region(ip: str) -> str:
    """IP 주소로 접속 지역 조회 (ip-api.com).

    주의: ip-api.com 무료 플랜은 HTTP만 지원. HTTPS는 유료.
    프라이버시를 위해 HTTPS 사용하되, 실패 시 graceful degradation.
    """
    if not ip or ip in ("127.0.0.1", "::1", "localhost"):
        return "로컬"
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            # HTTPS 우선 시도 (유료), 실패 시 지역정보 생략
            resp = await client.get(
                f"https://ip-api.com/json/{ip}?fields=status,country,countryCode,regionName,city"
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") != "success":
                    return "알 수 없음"
                code = data.get("countryCode", "")
                region = data.get("regionName", "")
                city = data.get("city", "")
                if code == "KR":
                    kr_region = _KR_REGION.get(region, region)
                    # 광역시: city와 region이 같으면 한글 지역명만 반환
                    if city == region or not city:
                        return kr_region
                    kr_city = _KR_CITY.get(city, "")
                    if kr_city:
                        return f"{kr_region} {kr_city}"
                    # 매핑 없는 도시명 → 시/도만 반환
                    return kr_region
                country = data.get("country", "")
                return f"{country} {city}".strip() or "알 수 없음"
    except Exception:
        pass
    return "알 수 없음"


def _get_client_ip(request: Request) -> str:
    """클라이언트 IP 추출 (프록시 헤더 우선)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.post("/login", response_model=UserOut)
@limiter.limit(RATE_LOGIN)
async def login_user(
    request: Request,
    body: UserLoginDto,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """이메일/비밀번호 로그인."""
    repo = SambaUserRepository(session)
    user = await repo.find_by_email(body.email)
    if not user:
        raise HTTPException(
            status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다"
        )

    if user.status != "active":
        raise HTTPException(status_code=403, detail="비활성 계정입니다")

    if not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다"
        )

    # JWT 토큰 발급 — tenant_id를 tid 클레임에 포함 (자동 격리 활성화)
    from backend.domain.user.auth_service import AuthService

    auth_svc = AuthService(session)
    access_token = auth_svc._create_access_token(user.id, tenant_id=user.tenant_id)

    # 로그인 이력 저장 (실패해도 로그인은 정상 진행)
    ip = _get_client_ip(request)
    try:
        region = await _resolve_ip_region(ip)
        from backend.db.orm import get_write_session

        async with get_write_session() as log_session:
            history = SambaLoginHistory(
                user_id=user.id,
                email=user.email,
                ip_address=ip,
                region=region,
                user_agent=request.headers.get("user-agent", ""),
            )
            log_session.add(history)
            await log_session.commit()
        logger.info(f"[사용자관리] 로그인: {user.email} IP={ip} 지역={region}")
    except Exception as e:
        logger.warning(f"[사용자관리] 로그인 이력 저장 실패: {e}")

    logger.info(f"[사용자관리] 로그인 성공: {user.email}")
    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        is_admin=user.is_admin,
        status=user.status,
        created_at=user.created_at.isoformat(),
        updated_at=user.updated_at.isoformat(),
        access_token=access_token,
    )


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    body: UserUpdateDto,
    session: AsyncSession = Depends(get_write_session_dependency),
    _admin_id: str = Depends(require_admin),
):
    """사용자 정보 수정."""
    repo = SambaUserRepository(session)
    user = await repo.get_async(user_id)
    if not user or user.deleted_at is not None:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")

    update_data: dict[str, Any] = {}
    if body.name is not None:
        update_data["name"] = body.name
    if body.email is not None:
        # 이메일 변경 시 중복 검사
        if body.email != user.email:
            dup = await repo.find_by_email(body.email)
            if dup:
                raise HTTPException(status_code=409, detail="이미 등록된 이메일입니다")
        update_data["email"] = body.email
    if body.password is not None:
        update_data["password_hash"] = hash_password(body.password)
    if body.status is not None:
        update_data["status"] = body.status

    if update_data:
        updated = await repo.update_async(user_id, **update_data)
        if updated:
            user = updated

    logger.info(f"[사용자관리] 계정 수정: {user.email}")

    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        is_admin=user.is_admin,
        status=user.status,
        created_at=user.created_at.isoformat(),
        updated_at=user.updated_at.isoformat(),
    )


class LoginHistoryOut(BaseModel):
    id: str
    email: str
    ip_address: Optional[str] = None
    region: Optional[str] = None
    created_at: str


@router.get("/login-history", response_model=list[LoginHistoryOut])
async def get_login_history(
    start: Optional[str] = Query(None, description="시작일 YYYY-MM-DD"),
    end: Optional[str] = Query(None, description="종료일 YYYY-MM-DD"),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_read_session_dependency),
    _admin_id: str = Depends(require_admin),
):
    """로그인 이력 조회 (날짜 범위 필터)."""
    stmt = select(SambaLoginHistory).order_by(SambaLoginHistory.created_at.desc())

    if start and end:
        from backend.utils import kst_date_range_to_utc

        start_dt, end_dt = kst_date_range_to_utc(start, end)
        stmt = stmt.where(SambaLoginHistory.created_at >= start_dt)
        stmt = stmt.where(SambaLoginHistory.created_at <= end_dt)
    elif start:
        from backend.utils import kst_date_range_to_utc

        start_dt, _ = kst_date_range_to_utc(start, start)
        stmt = stmt.where(SambaLoginHistory.created_at >= start_dt)
    elif end:
        from backend.utils import kst_date_range_to_utc

        _, end_dt = kst_date_range_to_utc(end, end)
        stmt = stmt.where(SambaLoginHistory.created_at <= end_dt)

    stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    rows = result.scalars().all()

    return [
        LoginHistoryOut(
            id=r.id,
            email=r.email,
            ip_address=r.ip_address,
            region=r.region,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
    _admin_id: str = Depends(require_admin),
):
    """사용자 계정 삭제 (소프트 삭제)."""
    repo = SambaUserRepository(session)
    success = await repo.soft_delete(user_id)
    if not success:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    logger.info(f"[사용자관리] 계정 삭제: {user_id}")
    return {"ok": True}

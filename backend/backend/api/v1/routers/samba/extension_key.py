"""확장앱 테넌트별 API 키 발급/조회/revoke."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.core.config import settings
from backend.db.orm import get_write_session_dependency
from backend.domain.samba.extension_key.model import SambaExtensionKey

router = APIRouter(prefix="/extension-keys", tags=["extension-keys"])
_security = HTTPBearer(auto_error=False)
_UTC = timezone.utc


@dataclass
class _UserCtx:
    user_id: str
    tenant_id: Optional[str]


async def _get_user_ctx(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> _UserCtx:
    if settings.mock_auth_enabled:
        return _UserCtx(user_id="mock-user-001", tenant_id=None)
    if not credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing token")
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("type") != "access":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token type")
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
        return _UserCtx(user_id=user_id, tenant_id=payload.get("tid"))
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _new_ulid() -> str:
    from ulid import ULID

    return str(ULID())


class _KeyIssueRequest(BaseModel):
    label: Optional[str] = None


class _KeyResponse(BaseModel):
    id: str
    label: Optional[str]
    created_at: datetime
    last_used_at: Optional[datetime]
    revoked_at: Optional[datetime]


class _KeyIssueResponse(_KeyResponse):
    key: str  # 평문 키 — 발급 시 1회만 노출


@router.post("", status_code=201, response_model=_KeyIssueResponse)
async def issue_key(
    body: _KeyIssueRequest,
    request: Request,
    ctx: _UserCtx = Depends(_get_user_ctx),
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """테넌트별 확장앱 키 발급. 평문 키는 이 응답에서만 노출.

    X-Device-Id 헤더가 있으면 device_id 컬럼에 저장(2026-05-20) — 오토튠
    status API의 본인 device 매칭에 사용.
    """
    raw = secrets.token_hex(32)
    _device_id = (request.headers.get("X-Device-Id") or "").strip() or None
    record = SambaExtensionKey(
        id=_new_ulid(),
        key_hash=_hash_key(raw),
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        label=body.label,
        created_at=datetime.now(_UTC),
        device_id=_device_id,
    )
    session.add(record)
    await session.commit()
    return _KeyIssueResponse(
        id=record.id,
        label=record.label,
        created_at=record.created_at,
        last_used_at=None,
        revoked_at=None,
        key=raw,
    )


@router.get("", response_model=list[_KeyResponse])
async def list_keys(
    ctx: _UserCtx = Depends(_get_user_ctx),
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """본인이 발급한 키 목록."""
    stmt = (
        select(SambaExtensionKey)
        .where(SambaExtensionKey.user_id == ctx.user_id)
        .order_by(SambaExtensionKey.created_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        _KeyResponse(
            id=r.id,
            label=r.label,
            created_at=r.created_at,
            last_used_at=r.last_used_at,
            revoked_at=r.revoked_at,
        )
        for r in rows
    ]


@router.delete("/{key_id}", status_code=204)
async def revoke_key(
    key_id: str,
    ctx: _UserCtx = Depends(_get_user_ctx),
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """키 revoke — 이후 해당 키로 API 접근 불가."""
    result = await session.execute(
        update(SambaExtensionKey)
        .where(
            SambaExtensionKey.id == key_id,
            SambaExtensionKey.user_id == ctx.user_id,
            SambaExtensionKey.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(_UTC))
    )
    await session.commit()
    if result.rowcount == 0:
        raise HTTPException(404, "키를 찾을 수 없거나 이미 revoke됨")


# 데몬 설치 exe 원본 (GitHub Release). 다운로드 프록시가 가져와 파일명에 install-token 박아 스트림.
# asset 명은 upload.ps1 이 'lotteon-daemon-setup.exe' 로 고정(autotune 아님).
_DAEMON_EXE_URL = (
    "https://github.com/sbk0674-web/samba-wave/releases/latest/download/"
    "lotteon-daemon-setup.exe"
)
_INSTALL_TOKEN_TTL_HOURS = 1


@router.get("/daemon-installer")
async def daemon_installer(
    request: Request,
    ctx: _UserCtx = Depends(_get_user_ctx),
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """데몬 설치 exe 다운로드 (JWT 필요).

    로그인 사용자의 테넌트로 1시간 만료 install-token 을 발급해 exe 파일명에 박아
    스트림한다(`autotune-daemon-setup_apikey=<token>_did=<device>.exe`). 데몬은 첫 실행 시
    파일명에서 토큰을 추출해 /extension-keys/exchange 로 long-lived 키와 교환한다.
    파일명 유출되어도 1시간 후 자동 만료 → 피해 최소화.
    """
    now = datetime.now(_UTC)
    raw_token = secrets.token_hex(32)
    device_id = (request.query_params.get("device_id") or "").strip() or None
    record = SambaExtensionKey(
        id=_new_ulid(),
        key_hash=_hash_key(raw_token),
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        label="install-token",
        device_id=device_id,
        is_install_token=True,
        created_at=now,
        expires_at=now + timedelta(hours=_INSTALL_TOKEN_TTL_HOURS),
    )
    session.add(record)
    await session.commit()

    # 파일명에 '=' 쓰면 브라우저 Content-Disposition 파싱에서 잘려 토큰 유실됨.
    # '=' 없는 '_it-<token>' 형식으로 박고 데몬이 정규식으로 추출한다.
    fname = f"autotune-daemon-setup_it-{raw_token}.exe"

    # 350MB exe 를 메모리에 통째로 올리지 않고 청크 스트리밍 — 메모리 안전 + 즉시 응답 시작.
    # (r.content 통짜 로드 시 fetch+로드 동안 응답이 안 와 브라우저 다운로드가 멈춤)
    client = httpx.AsyncClient(follow_redirects=True, timeout=300.0)
    try:
        req = client.build_request("GET", _DAEMON_EXE_URL)
        upstream = await client.send(req, stream=True)
        if upstream.status_code != 200:
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(
                502, f"데몬 설치 파일 응답 오류: {upstream.status_code}"
            )
    except HTTPException:
        raise
    except Exception as exc:
        await client.aclose()
        raise HTTPException(502, f"데몬 설치 파일 다운로드 실패: {exc}")

    async def _stream():
        try:
            async for chunk in upstream.aiter_bytes(1 << 16):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {"Content-Disposition": f'attachment; filename="{fname}"'}
    _clen = upstream.headers.get("content-length")
    if _clen:
        headers["Content-Length"] = _clen
    return StreamingResponse(
        _stream(), media_type="application/octet-stream", headers=headers
    )


# ── install-token 교환 (JWT 면제, install-token 자체로 인증) ──────────────
# 데몬은 사람 로그인 불가(헤드리스)이므로, 다운로드 시 박힌 1시간 만료 install-token
# 을 첫 실행 때 long-lived 키와 교환한다. install-token 은 api_gateway 가 검증해
# request.state.tenant_id 를 주입하고, exchange 경로에서만 통과시킨다(일반 API 차단).
public_router = APIRouter(prefix="/extension-keys", tags=["extension-keys-public"])


class _ExchangeRequest(BaseModel):
    device_id: Optional[str] = None
    label: Optional[str] = None


class _ExchangeResponse(BaseModel):
    key: str


@public_router.post("/exchange", response_model=_ExchangeResponse)
async def exchange_install_token(
    body: _ExchangeRequest,
    request: Request,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """install-token → long-lived 테넌트 키 교환 + install-token 즉시 revoke.

    인증: X-Api-Key 헤더의 install-token (api_gateway 가 검증·tenant 주입).
    데몬 첫 실행 시 1회 호출. 교환 후 install-token 은 폐기되어 재사용 불가.
    """
    raw_token = (request.headers.get("X-Api-Key") or "").strip()
    if not raw_token:
        raise HTTPException(401, "install-token(X-Api-Key) 필요")
    token_hash = _hash_key(raw_token)
    now = datetime.now(_UTC)
    row = (
        (
            await session.execute(
                select(SambaExtensionKey).where(
                    SambaExtensionKey.key_hash == token_hash,
                    SambaExtensionKey.is_install_token.is_(True),
                    SambaExtensionKey.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if row is None or (row.expires_at is not None and row.expires_at < now):
        raise HTTPException(403, "install-token 이 유효하지 않거나 만료되었습니다")

    new_raw = secrets.token_hex(32)
    device_id = (body.device_id or row.device_id or "").strip() or None
    new_key = SambaExtensionKey(
        id=_new_ulid(),
        key_hash=_hash_key(new_raw),
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        label=body.label or "데몬",
        device_id=device_id,
        is_install_token=False,
        created_at=now,
    )
    session.add(new_key)
    row.revoked_at = now  # install-token 일회용 — 교환 즉시 폐기
    session.add(row)
    await session.commit()
    return _ExchangeResponse(key=new_raw)

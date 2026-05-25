"""확장앱 테넌트별 API 키 발급/조회/revoke."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
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


# 데몬 설치 exe 원본 (GitHub Release). 단일 파일명 `samba.exe` — 모든 PC 동일 파일.
# 토큰/PC명 임베드 제거(2026-05-25, v1.3.0): 유상 판매용 깔끔한 파일명. 인증은 별도 키 발급
# 엔드포인트(`/extension-keys/daemon-key/issue`)로 분리 — 사용자 UI에서 키 발급/복사 후
# 데몬 첫 실행 시 입력. 멀티PC = 같은 파일 + 같은 키 사용 가능 (Datadog Agent 패턴).
_DAEMON_EXE_URL = (
    "https://github.com/sbk0674-web/samba-wave/releases/latest/download/samba.exe"
)


@router.get("/daemon-installer")
async def daemon_installer(
    request: Request,
    ctx: _UserCtx = Depends(_get_user_ctx),
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """데몬 설치 exe 다운로드 (JWT 필요) — 자동 등록형.

    SaaS 1클릭 자동화 (2026-05-25): 다운로드 시 1시간 짜리 install-token 발급 → 토큰
    마커를 exe 파일 끝에 append (`#SAMBA_TOKEN=<token>#`). 데몬 첫 실행 시 자기 exe
    파일 끝을 읽어 토큰 추출 → `/exchange` 호출해 long-lived 키 받고 캐시. 사용자
    수동 키 입력·붙여넣기 불필요.
    """
    _ = request
    now = datetime.now(_UTC)
    # 1) install-token 발급
    raw_token = secrets.token_hex(32)
    install_record = SambaExtensionKey(
        id=_new_ulid(),
        key_hash=_hash_key(raw_token),
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        label="install-token",
        device_id=None,
        is_install_token=True,
        created_at=now,
        expires_at=now + _install_token_ttl(),
    )
    session.add(install_record)
    await session.commit()

    fname = "samba.exe"
    # 토큰 마커 — 데몬이 exe 파일 마지막에서 찾는다. 충돌 회피용 prefix/suffix.
    token_marker = f"\n#SAMBA_TOKEN={raw_token}#\n".encode()

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
            # exe 끝에 토큰 마커 append — 데몬이 자기 exe 끝에서 추출
            yield token_marker
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {"Content-Disposition": f'attachment; filename="{fname}"'}
    # 토큰 append 로 길이 늘어남 — 원본 content-length 사용 시 truncate 발생
    _clen = upstream.headers.get("content-length")
    if _clen:
        headers["Content-Length"] = str(int(_clen) + len(token_marker))
    return StreamingResponse(
        _stream(), media_type="application/octet-stream", headers=headers
    )


def _install_token_ttl():
    from datetime import timedelta

    return timedelta(hours=1)


# ── 데몬 API 키 발급 (v1.3.0+) ──────────────────────────────────────────
# 단일 파일 `samba.exe` + 별도 키 발급 모델. 로그인 사용자가 UI에서 키 발급 → 복사 →
# 데몬 첫 실행 시 입력. 같은 키 여러 PC에서 사용 가능 (long-lived tenant key).


class _DaemonKeyResponse(BaseModel):
    api_key: str
    id: str
    label: str


@router.post("/daemon-key/issue", response_model=_DaemonKeyResponse)
async def issue_daemon_key(
    request: Request,
    ctx: _UserCtx = Depends(_get_user_ctx),
    session: AsyncSession = Depends(get_write_session_dependency),
) -> _DaemonKeyResponse:
    """로그인 사용자 테넌트의 long-lived 데몬 키 발급 + 평문 반환.

    UI '데몬 키 발급' 버튼에서 호출 → 응답의 api_key 를 사용자에게 표시 + 복사 버튼 제공.
    사용자가 각 PC 데몬 첫 실행 시 입력하거나 api_key.txt 에 저장.
    install-token (1시간 만료) 과 달리 long-lived (revoke 전까지 유효).
    같은 키를 여러 PC에서 사용 가능 — hostname 기반 device_id 로 자동 구분됨.
    """
    _ = request  # 미사용 (인증 게이트만)
    now = datetime.now(_UTC)
    raw_token = secrets.token_hex(32)
    label = "데몬 키"
    record = SambaExtensionKey(
        id=_new_ulid(),
        key_hash=_hash_key(raw_token),
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        label=label,
        device_id=None,
        is_install_token=False,
        created_at=now,
        expires_at=None,
    )
    session.add(record)
    await session.commit()
    return _DaemonKeyResponse(api_key=raw_token, id=record.id, label=label)


# ── 데몬 self-update (X-Api-Key 인증, JWT 면제) — SaaS 1클릭 자동 갱신 ─────────
# 데몬이 자동 업데이트할 때 backend 경유로 받아 새 install-token 박힌 exe 획득 → 다음
# 실행 시 새 토큰 추출 → 새 long-lived 키 캐시. 옛 키 invalid 케이스 자동 복구.
public_router = APIRouter(prefix="/extension-keys", tags=["extension-keys-public"])


@public_router.get("/daemon-self-update")
async def daemon_self_update(
    request: Request,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """데몬 self-update 전용 — X-Api-Key 로 인증된 데몬에 새 토큰 박힌 exe 제공.

    api-gateway 가 X-Api-Key 검증 → tenant_id 주입. 이 endpoint 는 그 tenant_id 로
    새 install-token 발급 + 표준 daemon-installer 와 동일 로직으로 exe 끝에 토큰 append.
    데몬 swap 후 자동으로 새 토큰 추출 → 새 long-lived 키 갱신 → 옛 키 자연 폐기.
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(401, "유효한 X-Api-Key 필요")
    now = datetime.now(_UTC)
    raw_token = secrets.token_hex(32)
    install_record = SambaExtensionKey(
        id=_new_ulid(),
        key_hash=_hash_key(raw_token),
        tenant_id=tenant_id,
        user_id=None,
        label="install-token (self-update)",
        device_id=None,
        is_install_token=True,
        created_at=now,
        expires_at=now + _install_token_ttl(),
    )
    session.add(install_record)
    await session.commit()

    token_marker = f"\n#SAMBA_TOKEN={raw_token}#\n".encode()
    client = httpx.AsyncClient(follow_redirects=True, timeout=300.0)
    try:
        upstream_req = client.build_request("GET", _DAEMON_EXE_URL)
        upstream = await client.send(upstream_req, stream=True)
        if upstream.status_code != 200:
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(
                502, f"self-update 다운로드 실패: {upstream.status_code}"
            )
    except HTTPException:
        raise
    except Exception as exc:
        await client.aclose()
        raise HTTPException(502, f"self-update 다운로드 실패: {exc}")

    async def _stream():
        try:
            async for chunk in upstream.aiter_bytes(1 << 16):
                yield chunk
            yield token_marker
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {"Content-Disposition": 'attachment; filename="samba.exe"'}
    _clen = upstream.headers.get("content-length")
    if _clen:
        headers["Content-Length"] = str(int(_clen) + len(token_marker))
    return StreamingResponse(
        _stream(), media_type="application/octet-stream", headers=headers
    )


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

"""TenantContextMiddleware — JWT의 tid 클레임을 contextvar에 세팅.

ORM 자동 필터는 backend/core/tenant_context.py의 current_tenant_id를 읽는다.
미들웨어 자체는 인증을 강제하지 않음 (인증 없는 endpoint는 그대로 통과).
"""

import logging
from typing import Optional

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from backend.core.config import settings
from backend.core.tenant_context import current_tenant_id

logger = logging.getLogger(__name__)


_USER_TENANT_CACHE: dict[str, str] = {}  # user_id → tenant_id (프로세스 캐시)


async def _resolve_tenant_id(request: Request) -> Optional[str]:
    """Authorization Bearer JWT에서 tenant_id 해석.

    우선순위:
    1. JWT tid 클레임 (신규 토큰)
    2. JWT sub(user_id)로 DB 조회 → SambaUser.tenant_id (구 토큰 폴백, 캐시됨)
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split(" ", 1)[1]
    try:
        payload = jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
    except Exception:
        return None

    tid = payload.get("tid")
    if tid:
        return tid

    user_id = payload.get("sub", "")
    if not user_id:
        return None

    cached = _USER_TENANT_CACHE.get(user_id)
    if cached:
        return cached

    # DB 폴백 — sync session으로 짧게 조회 (event loop 위에서 async도 OK)
    try:
        from backend.db.orm import get_read_session
        from sqlmodel import select
        from backend.domain.samba.user.model import SambaUser

        async with get_read_session() as sess:
            stmt = select(SambaUser.tenant_id).where(SambaUser.id == user_id)
            result = await sess.execute(stmt)
            tenant_id = result.scalar_one_or_none()
            if tenant_id:
                _USER_TENANT_CACHE[user_id] = tenant_id
            return tenant_id
    except Exception as e:
        logger.warning(f"[tenant_context] DB 폴백 실패 user_id={user_id}: {e}")
        return None


class TenantContextMiddleware(BaseHTTPMiddleware):
    """모든 HTTP 요청에 대해 contextvar 세팅 → ORM 자동 필터 활성."""

    async def dispatch(self, request: Request, call_next):
        tenant_id = await _resolve_tenant_id(request)
        token = current_tenant_id.set(tenant_id)
        try:
            return await call_next(request)
        finally:
            current_tenant_id.reset(token)

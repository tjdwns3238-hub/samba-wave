"""API Gateway Key 검증 미들웨어 — 외부 앱의 무단 API 접근 차단.

Stage 2: 테넌트 키 우선 체크 → 글로벌 키 폴백.
X-Api-Key 가 DB samba_extension_key 에 등록된 키이면 tenant_id 를 주입,
없으면 기존 글로벌 키로 검증.
"""

import hashlib
import logging
import time
from typing import Optional

from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# 키 검증을 건너뛸 경로 (health check, 루트, 인증 진입점)
# 회원가입·로그인 엔드포인트는 X-Api-Key 없이도 호출 가능해야 함
# (invite_code + rate_limit + role-based 가드로 보호)
_EXEMPT_PATHS = {
    "/",
    "/api/v1/health",
    "/api/v1/samba/sourcing-accounts/extension-key",
    "/api/v1/license/verify",
    "/api/v1/samba/users",  # 회원가입 POST (목록조회 GET은 라우터에서 require_admin)
    "/api/v1/samba/users/login",  # 로그인
    "/api/v1/auth/email/sign-up",
    "/api/v1/auth/email/login",
    "/api/v1/auth/refresh",
}

# 키 검증을 건너뛸 prefix (정적 자산 — 모델 프리셋 PNG 등)
# 화이트리스트 누락 시 프론트가 무한 재요청하며 워커 event loop 소모 → health timeout 유발
_EXEMPT_PREFIXES = (
    "/static/",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/v1/samba/proxy/bg-jobs/",  # bg-worker 내부 호출 — 워커 토큰으로 자체 인증
)

# 테넌트 키 캐시: key_hash → (tenant_id, cached_until_monotonic)
# tenant_id = None 은 테넌트 미설정 키 (사용자 발급 후 JWT 에 tid 없는 경우)
_KEY_CACHE: dict[str, tuple[Optional[str], float]] = {}
_KEY_CACHE_TTL = 60.0  # 1분


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _lookup_tenant_key(key_hash: str) -> tuple[bool, Optional[str]]:
    """DB 에서 테넌트 키 조회. (found, tenant_id) 반환. 1분 캐시."""
    now = time.monotonic()
    cached = _KEY_CACHE.get(key_hash)
    if cached is not None:
        tenant_id, expires = cached
        if now < expires:
            return True, tenant_id

    try:
        from backend.db.orm import get_read_session

        async with get_read_session() as session:
            result = await session.execute(
                text(
                    "SELECT tenant_id FROM samba_extension_key "
                    "WHERE key_hash = :kh AND revoked_at IS NULL "
                    "LIMIT 1"
                ),
                {"kh": key_hash},
            )
            row = result.fetchone()

        if row is not None:
            tenant_id = row[0]
            _KEY_CACHE[key_hash] = (tenant_id, now + _KEY_CACHE_TTL)
            return True, tenant_id

        return False, None
    except Exception as exc:
        logger.warning("[api-gateway] 테넌트 키 조회 실패: %s", exc)
        return False, None


class ApiGatewayMiddleware(BaseHTTPMiddleware):
    """X-Api-Key 헤더를 검증하여 허가된 클라이언트만 API 접근 허용.

    검증 순서:
    1. 테넌트 키 DB 조회 (1분 캐시) → 유효하면 request.state.tenant_id 주입 후 통과
    2. 글로벌 키 일치 여부 확인 → 일치하면 통과
    3. 둘 다 실패 → 403
    """

    def __init__(self, app, api_key: str):
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request: Request, call_next):
        # CORS preflight는 통과
        if request.method == "OPTIONS":
            return await call_next(request)

        # 면제 경로는 통과
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)
        if request.url.path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        # 키가 설정되지 않은 경우(개발환경) 통과
        if not self.api_key:
            return await call_next(request)

        request_key = request.headers.get("X-Api-Key", "")

        # 1단계: 테넌트 키 체크 (DB 조회, 1분 캐시)
        if request_key:
            key_hash = _hash_key(request_key)
            found, tenant_id = await _lookup_tenant_key(key_hash)
            if found:
                request.state.tenant_id = tenant_id
                return await call_next(request)

        # 2단계: 글로벌 키 폴백 (DEPRECATE_GLOBAL_KEY=true 시 건너뜀)
        from backend.core.config import settings as _settings

        if not _settings.deprecate_global_key and request_key == self.api_key:
            request.state.tenant_id = None
            return await call_next(request)

        logger.warning(
            "[api-gateway] 차단: %s %s (IP: %s)",
            request.method,
            request.url.path,
            request.client.host if request.client else "unknown",
        )
        return JSONResponse(
            status_code=403,
            content={"detail": "유효하지 않은 API 키입니다."},
        )

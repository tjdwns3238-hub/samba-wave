import os
import re
from typing import Literal

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

_CHROME_EXT_ID_RE = re.compile(r"^[a-z]{32}$")


def _is_valid_chrome_id(value: str) -> bool:
    return bool(_CHROME_EXT_ID_RE.fullmatch(value))


class BackendSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ===========================================
    # Environment Configuration
    # ===========================================
    environment: Literal["development", "staging", "production"] = "development"
    """Deployment environment: development, staging, production."""

    # ===========================================
    # Database Configuration
    # ===========================================
    write_db_user: str
    write_db_password: str
    write_db_host: str
    write_db_port: int
    write_db_name: str
    read_db_user: str
    read_db_password: str
    read_db_host: str
    read_db_port: int
    read_db_name: str

    # Database SSL configuration
    db_ssl_required: bool | None = None

    @computed_field
    @property
    def use_db_ssl(self) -> bool:
        """Compute actual SSL requirement based on explicit setting or environment."""
        if self.db_ssl_required is not None:
            return self.db_ssl_required
        return self.environment != "development"

    # ===========================================
    # JWT Configuration
    # ===========================================
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"

    # Token expiration settings
    access_token_expire_minutes: int = 43200  # 30일
    refresh_token_expire_days: int = 30

    # ===========================================
    # Authentication Configuration
    # ===========================================
    mock_auth_enabled: bool = False
    """Enable mock authentication for development."""

    # ===========================================
    # CORS Configuration
    # ===========================================

    # ===========================================
    # API Gateway Key (외부 앱 차단용)
    # ===========================================
    api_gateway_key: str = ""
    """API 게이트웨이 키 — 프론트엔드·확장앱만 허용, 외부 앱 차단."""

    deprecate_global_key: bool = False
    """True 시 글로벌 키 폴백 비활성화 — 테넌트 키만 허용. 모든 유저가 웹 로그인 후 전환."""

    owner_device_ids: str = ""
    """[deprecated 2026-05-25] 키-디바이스 TOFU 바인딩으로 대체됨.
    samba_extension_key.device_id 컬럼이 첫 사용 시 자동 백필되어 동일 보안 효과 제공.
    필드는 pydantic env 호환을 위해 유지하되 더 이상 참조되지 않음."""

    autotune_daemon_device_id: str = ""
    """[deprecated] 단일 데몬 deviceId — 하위호환용. 신규는 autotune_daemon_device_ids 사용.
    값 있으면 autotune_daemon_device_ids 의 1번째 원소로 자동 승격된다."""

    autotune_daemon_device_ids: str = ""
    """LOTTEON 헤드리스 데몬 deviceId 풀 (콤마 구분).
    예) "samba-daemon-pc1,samba-daemon-pc2,samba-daemon-pc3"
    설정 시 LOTTEON DOM 위임 잡을 풀에서 round-robin 으로 1개 데몬에 라우팅.
    비어있으면 기존 흐름(오토튠 시작 PC 확장앱) 유지 — 즉시 롤백 가능.
    각 데몬은 본인 deviceId 만 picking — 백엔드 get_next_job 가 owner 매칭."""

    daemon_public_backend_url: str = ""
    """데몬 설치본이 가리킬 백엔드 공개 URL (포크 운영자용).
    설정 시 /daemon-installer 가 파일명에 `_be-<hex>` 로 박아 데몬이 본인 백엔드를 향하게 한다.
    비어있으면 미박음 → 데몬 기본값(https://api.samba-wave.co.kr) 사용. 메인 운영은 비워둬도 동일.
    예) "https://api.myfork.com" """

    # ===========================================
    # AI / Anthropic Configuration
    # ===========================================
    anthropic_api_key: str = ""
    """Claude API 키 (카테고리 AI 매핑 등)."""

    # ===========================================
    # Redis 설정
    # ===========================================
    redis_url: str | None = None  # 환경변수: REDIS_URL

    # ===========================================
    # 네이버 API 설정 (스마트스토어 소싱용)
    # ===========================================
    naver_client_id: str = ""
    """네이버 검색 API Client ID."""
    naver_client_secret: str = ""
    """네이버 검색 API Client Secret."""

    # ===========================================
    # HTTP 타임아웃 설정 (초)
    # ===========================================
    http_timeout_short: int = 10  # 빠른 API (검색, 조회)
    http_timeout_default: int = 30  # 기본 API (등록, 수정)
    http_timeout_upload: int = 60  # 이미지 업로드 등 느린 작업

    # [DEPRECATED] 환경변수 프록시 설정. 더 이상 참조되지 않음 —
    # 수집/전송/오토튠 프록시는 `/samba/settings` 페이지에서 DB에 등록한다.
    # 필드 자체는 .env 변수 호환성을 위해 남겨두되 코드 어디에서도 읽지 않는다.
    proxy_urls: str = ""
    collect_proxy_url: str = ""

    # ===========================================
    # eBay 통합 설정
    # ===========================================
    ebay_deletion_notification_url: str = ""
    """eBay 마켓플레이스 계정 삭제 알림 endpoint 전체 URL.
       Developer Portal에 등록한 URL과 100% 동일해야 SHA-256 challenge 검증 통과.
       환경별로 다르므로 코드 기본값은 빈 문자열 — VM의 .env로 주입."""

    ebay_verification_token: str = ""
    """eBay endpoint 검증용 token (32자 이상).
       Developer Portal에 등록한 값과 동일해야 challenge 응답이 일치.
       시크릿이므로 코드/PR에 노출 금지 — VM의 .env로만 주입."""

    cs_internal_token: str = ""
    """CS 자동화 내부 API(/api/v1/internal/cs/*) 인증 토큰.
       Claude 클라우드 스케줄잡이 X-Internal-Token 헤더로 호출.
       samba_auth(JWT) 우회 경로이므로 이 토큰이 유일 방어선 — 빈 값이면 전체 차단.
       시크릿이므로 코드/PR 노출 금지 — VM의 .env로만 주입."""

    # ===========================================
    # ESMPlus 호스팅 인증정보 (셀링툴업체 고정값)
    # ===========================================
    esmplus_hosting_id: str = ""
    """ESMPlus 호스팅 마스터 ID — 삼바웨이브 셀링툴 고정값."""
    esmplus_secret_key: str = ""
    """ESMPlus 호스팅 시크릿 키 — 삼바웨이브 셀링툴 고정값."""

    # 추가 허용 origin (콤마 구분, Railway 환경변수로 주입)
    cors_extra_origins: str = ""

    # 명시적으로 허용할 chrome 확장앱 ID 목록 (콤마 구분, 32자 [a-z]).
    # env 가 비어있으면 fallback 으로 모든 32자 [a-z] 확장 ID 를 허용 (배포 직후 회귀 방지).
    # 운영에서는 env 주입 권장 — 임의 확장이 origin 헤더만 위조하면 CORS 통과하는 위험을 차단.
    chrome_extension_ids: str = ""

    @computed_field
    @property
    def cors_origins(self) -> list[str]:
        """Get allowed CORS origins based on environment."""
        origins = [
            "http://localhost:3000",
            "http://localhost:3001",
            "http://localhost:3002",
            "http://localhost:3003",
            "http://127.0.0.1:3000",
        ]
        if self.cors_extra_origins:
            extras = [
                o.strip() for o in self.cors_extra_origins.split(",") if o.strip()
            ]
            origins.extend(extras)
        return origins

    @computed_field
    @property
    def cors_origin_regex(self) -> str | None:
        """localhost + 프로젝트 vercel.app + chrome 확장앱 origin 허용.

        chrome 확장앱 부분:
        - chrome_extension_ids 가 주입되면 명시 ID allowlist 만 통과
        - 비어있으면 [a-z]{32} fallback (운영 env 주입 전 일시적 호환)
        """
        ids_raw = (self.chrome_extension_ids or "").strip()
        if ids_raw:
            valid_ids = [_id.strip() for _id in ids_raw.split(",") if _id.strip()]
            valid_ids = [_id for _id in valid_ids if _is_valid_chrome_id(_id)]
            if valid_ids:
                # _is_valid_chrome_id 가 [a-z]{32} 만 통과시키지만, defense-in-depth
                # 로 정규식 메타 문자를 escape — 검증 함수에 회귀 발생 시에도 보호.
                ext_pattern = "|".join(re.escape(_id) for _id in valid_ids)
                ext_part = rf"chrome-extension://({ext_pattern})"
            else:
                # 잘못된 형식만 들어있으면 어떤 확장도 허용 안 함
                ext_part = r"chrome-extension://__never_match__"
        else:
            ext_part = r"chrome-extension://[a-z]{32}"

        return (
            r"^(https?://(localhost(:\d+)?|127\.0\.0\.1(:\d+)?"
            r"|samba-wave[a-z0-9-]*\.vercel\.app"
            r"|([a-z0-9-]+\.)?samba-wave\.co\.kr)"
            r"|" + ext_part + r")$"
        )

    # ===========================================
    # Computed Properties
    # ===========================================
    @computed_field
    @property
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.environment == "development"

    @computed_field
    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return self.environment == "production"

    @computed_field
    @property
    def debug_enabled(self) -> bool:
        """Enable debug mode in non-production environments."""
        return self.environment != "production"


_settings = BackendSettings()

# ── 개발 환경에서 운영 DB 접속 차단 ──
# 운영 DB 호스트 식별자는 PRODUCTION_DB_HOSTS env 콤마 분리로 분리 — public repo
# leak 차단. 빈값이면 차단 로직 비활성(개발자 책임).
# 예: PRODUCTION_DB_HOSTS="34.47.96.236,/cloudsql/<project>,/cloudsql/<other>"
_PRODUCTION_DB_HOSTS = [
    h.strip() for h in os.getenv("PRODUCTION_DB_HOSTS", "").split(",") if h.strip()
]
if _settings.is_development:
    for _h in _PRODUCTION_DB_HOSTS:
        if _h in _settings.write_db_host or _h in _settings.read_db_host:
            raise RuntimeError(
                f"[보안 차단] 개발 환경(APP_ENV=development)에서 운영 DB 호스트({_h}) 접속이 감지되었습니다. "
                "운영 DB는 Cloud Run 배포를 통해서만 접근해야 합니다."
            )

settings = _settings

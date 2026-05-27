import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator
from weakref import WeakKeyDictionary

from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.core.config import settings

# non-checked-in connection = 연결이 GC에 의해 회수됨 → 진짜 누수. 경고 억제 대신 로깅으로 가시화
import warnings as _w

_orig_showwarning = _w.showwarning


def _log_non_checked_in(msg, category, filename, lineno, file=None, line=None):
    if "non-checked-in" in str(msg):
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "[DB누수] non-checked-in connection 감지: %s (%s:%s)", msg, filename, lineno
        )
        return
    _orig_showwarning(msg, category, filename, lineno, file, line)


_w.showwarning = _log_non_checked_in

logger = logging.getLogger(__name__)

_sessionmaker_cache: "WeakKeyDictionary[Any, Any]" = (
    WeakKeyDictionary()
)  # engine ↔ sessionmaker
_write_engine_cache: "WeakKeyDictionary[asyncio.AbstractEventLoop, Any]" = (
    WeakKeyDictionary()
)
_read_engine_cache: "WeakKeyDictionary[asyncio.AbstractEventLoop, Any]" = (
    WeakKeyDictionary()
)

write_db_url = URL.create(
    "postgresql",
    username=settings.write_db_user,
    password=settings.write_db_password,
    host=settings.write_db_host,
    port=settings.write_db_port,
    database=settings.write_db_name,
)

read_db_url = URL.create(
    "postgresql",
    username=settings.read_db_user,
    password=settings.read_db_password,
    host=settings.read_db_host,
    port=settings.read_db_port,
    database=settings.read_db_name,
)


def _build_db_url(user: str, password: str, host: str, port: int, name: str) -> URL:
    """Build database URL with optional SSL parameter."""
    # Cloud SQL Auth Proxy Unix 소켓 연결 (Cloud Run 환경)
    if host.startswith("/cloudsql/"):
        return URL.create(
            "postgresql+asyncpg",
            username=user,
            password=password,
            database=name,
            query={"host": host},
        )
    query = {"ssl": "require"} if settings.use_db_ssl else {}
    return URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password,
        host=host,
        port=port,
        database=name,
        query=query,
    )


def _create_write_async_engine() -> AsyncEngine:
    eng = _build_write_engine()
    try:
        from backend.db.pool_monitor import attach_pool_monitor

        attach_pool_monitor(eng, "write")
    except Exception:
        pass
    return eng


def _build_write_engine() -> AsyncEngine:
    return create_async_engine(
        _build_db_url(
            settings.write_db_user,
            settings.write_db_password,
            settings.write_db_host,
            settings.write_db_port,
            settings.write_db_name,
        ),
        future=True,
        echo=False,  # Disable SQL echo to reduce noise
        pool_pre_ping=False,  # asyncpg 버그: SELECT 1이 idle in transaction 좀비 누적 → pool_recycle로 대체
        # (2026-05-27 PM) 풀 상한 축소: write 30+30=60 → 20+20=40.
        # 사용자 캡쳐 Cloud SQL 최대 연결 97/100 위험. 백엔드 단일 워커가 풀 상한
        # 90개(write 60 + read 30) 까지 차오를 수 있어 Cloud SQL max=100 한계 임박.
        # 축소 후 합 max=60 (write 40 + read 20) — Cloud SQL 여유 35 확보.
        pool_size=20,
        max_overflow=20,
        pool_recycle=45,  # idle 커넥션 45초 후 재활용 — 좀비 회수 가속
        pool_timeout=10,  # 빠른 실패 — 30s 대기 중 ASGI 워커 타임아웃 방지
        connect_args={
            "timeout": 10,
            "server_settings": {
                # 좀비 차단 — IIT 초과 시 PostgreSQL 자동 종료. 120s 타협안:
                # 60s = transmit/worker 마켓 HTTP 60s 초과 시 connection-closed 발생,
                # 180s = 좀비 회수 늦어져 풀 idle 누적 → Cloud SQL 97/100 위험.
                # 120s = transmit 평균 30~45s 대비 2~4배 마진 + 좀비 회수 가속.
                "idle_in_transaction_session_timeout": "120000",
            },
        },
    )


def _create_read_async_engine() -> AsyncEngine:
    eng = _build_read_engine()
    try:
        from backend.db.pool_monitor import attach_pool_monitor

        attach_pool_monitor(eng, "read")
    except Exception:
        pass
    return eng


def _build_read_engine() -> AsyncEngine:
    return create_async_engine(
        _build_db_url(
            settings.read_db_user,
            settings.read_db_password,
            settings.read_db_host,
            settings.read_db_port,
            settings.read_db_name,
        ),
        future=True,
        echo=False,
        pool_pre_ping=False,  # asyncpg 버그: SELECT 1이 idle in transaction 좀비 누적 → pool_recycle로 대체
        # (2026-05-27 PM) read 풀 상한 축소: 15+15=30 → 10+10=20.
        # Cloud SQL 97/100 위험 대응. 오토튠 코디네이터 read 전환으로 read 부하 ↑ 이지만
        # scroll_products 병렬화 진입당 ~3 세션 + 백그라운드 sync 루프 합산해도 20 안.
        pool_size=10,
        max_overflow=10,
        pool_recycle=45,  # idle 커넥션 45초 후 재활용 — 좀비 회수 가속
        pool_timeout=10,
        connect_args={
            "timeout": 10,
            "server_settings": {
                # read 트랜잭션 짧음 — 30s 유지 (좀비 빠른 회수 우선).
                "idle_in_transaction_session_timeout": "30000",
            },
        },
    )


def get_write_engine() -> Any:
    loop = asyncio.get_running_loop()
    if loop not in _write_engine_cache:
        _write_engine_cache[loop] = _create_write_async_engine()
    return _write_engine_cache[loop]


def get_read_engine() -> Any:
    loop = asyncio.get_running_loop()
    if loop not in _read_engine_cache:
        _read_engine_cache[loop] = _create_read_async_engine()
    return _read_engine_cache[loop]


def get_write_sessionmaker() -> Any:
    engine = get_write_engine()
    if engine not in _sessionmaker_cache:
        _sessionmaker_cache[engine] = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _sessionmaker_cache[engine]


def get_read_sessionmaker() -> Any:
    engine = get_read_engine()
    if engine not in _sessionmaker_cache:
        _sessionmaker_cache[engine] = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _sessionmaker_cache[engine]


@asynccontextmanager
async def get_write_session() -> AsyncGenerator[AsyncSession, None]:
    Session = get_write_sessionmaker()
    async with Session() as sess:
        try:
            yield sess
        except (
            BaseException
        ):  # CancelledError는 BaseException 상속 — Exception으로는 못 잡음
            try:
                await sess.rollback()
            except BaseException:  # rollback 중 2차 CancelledError도 억제
                pass
            raise


@asynccontextmanager
async def get_read_session() -> AsyncGenerator[AsyncSession, None]:
    Session = get_read_sessionmaker()
    async with Session() as sess:
        try:
            yield sess
        except (
            BaseException
        ):  # CancelledError 포함 — rollback으로 idle in transaction 방지
            try:
                await sess.rollback()
            except BaseException:  # rollback 중 2차 CancelledError도 억제
                pass
            raise


# Non-decorator versions for dependency injection
async def get_write_session_dependency() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for write sessions with guaranteed cleanup.

    BaseException까지 잡아야 함 — asyncio.CancelledError는 Exception이 아닌 BaseException 상속.
    클라이언트 끊김/ASGI 타임아웃 시 CancelledError가 발생하는데 except Exception이면
    rollback이 호출되지 않아 idle in transaction 좀비가 풀에 쌓인다(2026-05-10 사고).
    """
    Session = get_write_sessionmaker()
    session = Session()
    try:
        yield session
    except BaseException:
        try:
            await session.rollback()
        except BaseException:
            pass
        raise
    finally:
        try:
            await session.close()
        except BaseException:
            pass


async def get_read_session_dependency() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for read sessions with guaranteed cleanup.

    BaseException까지 잡아야 함 — write 세션과 동일 이유 (CancelledError 안 잡힘 → rollback 누락).
    """
    Session = get_read_sessionmaker()
    session = Session()
    try:
        yield session
    except BaseException:
        try:
            await session.rollback()
        except BaseException:
            pass
        raise
    finally:
        try:
            await session.close()
        except BaseException:
            pass

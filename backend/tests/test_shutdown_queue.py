import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.domain.samba.proxy.sourcing_queue import SourcingQueue
from backend.shutdown_state import clear_shutting_down, mark_shutting_down


@pytest.fixture(autouse=True)
def reset_shutdown_queue_state():
    clear_shutting_down()
    SourcingQueue.resolvers.clear()
    yield
    clear_shutting_down()
    SourcingQueue.resolvers.clear()


def _make_db_session_mock(request_id: str, payload: dict):
    """get_next_job DB 조회를 mock하는 헬퍼.

    get_write_session은 @asynccontextmanager 데코레이터로 감싼
    async generator 함수 — 호출 시 async context manager 객체를 반환.
    """
    mock_row = MagicMock()
    mock_row.__iter__ = lambda s: iter([request_id, payload])

    mock_result = MagicMock()
    mock_result.fetchone.return_value = mock_row

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    return MagicMock(return_value=mock_cm)


def test_sourcing_queue_add_and_resolve_job():
    async def scenario():
        with patch(
            "backend.domain.samba.proxy.sourcing_queue._db_insert_job",
            new_callable=lambda: lambda *a, **kw: asyncio.sleep(0),
        ):
            request_id, future = SourcingQueue.add_search_job("ABCmart", "nike")

        payload = {
            "requestId": request_id,
            "site": "ABCmart",
            "keyword": "nike",
            "type": "search",
            "url": "https://example.com",
            "ownerDeviceId": "",
        }
        mock_get_write_session = _make_db_session_mock(request_id, payload)

        with patch(
            "backend.domain.samba.proxy.sourcing_queue.get_write_session",
            mock_get_write_session,
        ):
            job = await SourcingQueue.get_next_job()

        assert job["hasJob"] is True
        assert job["requestId"] == request_id
        assert job["site"] == "ABCmart"
        assert job["keyword"] == "nike"

        assert (
            SourcingQueue.resolve_job(
                request_id,
                {"success": True, "products": []},
            )
            is True
        )
        assert await future == {"success": True, "products": []}

    asyncio.run(scenario())


def test_sourcing_queue_rejects_new_jobs_while_shutting_down():
    mark_shutting_down()

    with pytest.raises(RuntimeError, match="server is shutting down"):
        SourcingQueue.add_search_job("ABCmart", "nike")

    async def check():
        result = await SourcingQueue.get_next_job()
        assert result == {"hasJob": False, "shuttingDown": True}

    asyncio.run(check())


def test_sourcing_queue_cancel_all_fails_waiters():
    async def scenario():
        with patch(
            "backend.domain.samba.proxy.sourcing_queue._db_insert_job",
            new_callable=lambda: lambda *a, **kw: asyncio.sleep(0),
        ):
            _, future = SourcingQueue.add_detail_job(
                "ABCmart", "12345", owner_device_id="test-device"
            )

        SourcingQueue.cancel_all("shutdown for deploy")

        with pytest.raises(RuntimeError, match="shutdown for deploy"):
            await future

        assert SourcingQueue.resolvers == {}

    asyncio.run(scenario())

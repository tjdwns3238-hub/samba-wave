"""BaseRepository 청크 commit opt-in 회귀 테스트 (issue #401).

대량 주문 persist 시 건당 commit → 청크 commit 으로 묶기 위한 commit 파라미터 검증.
- commit=True(기본): session.commit + refresh 호출 → 기존 모든 호출부 동작 불변
- commit=False: session.flush 만 호출, commit/refresh 미호출 → 호출부가 일괄 commit

검증 게이트: 로컬에서 300초 timeout 은 재현 불가하므로, "기본 경로가 오늘과
동일(다른 도메인 호출부 영향 없음)" + "opt-out 시 flush-only" 두 contract 를 단언한다.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from backend.domain.shared.base_repository import BaseRepository


class _FakeModel:
    """SQLModel 대용 — self.model(**kwargs) 호출과 setattr 만 흉내낸다."""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_repo() -> tuple[BaseRepository, AsyncMock]:
    session = AsyncMock()
    session.add = MagicMock()  # add 는 동기 메서드
    repo: BaseRepository = BaseRepository(session, _FakeModel)
    return repo, session


# ── create_async ──────────────────────────────────────────────


async def test_create_default_commits_and_refreshes():
    repo, session = _make_repo()
    await repo.create_async(name="x")
    session.commit.assert_awaited_once()
    session.refresh.assert_awaited_once()
    session.flush.assert_not_awaited()


async def test_create_commit_false_flushes_only():
    repo, session = _make_repo()
    await repo.create_async(commit=False, name="x")
    session.flush.assert_awaited_once()
    session.commit.assert_not_awaited()
    session.refresh.assert_not_awaited()


# ── update_async ──────────────────────────────────────────────


async def test_update_default_commits_and_refreshes():
    repo, session = _make_repo()
    entity = _FakeModel(id=5, name="old")
    repo.get_async = AsyncMock(return_value=entity)  # type: ignore[method-assign]
    await repo.update_async(5, name="new")
    assert entity.name == "new"  # 변경 반영
    session.commit.assert_awaited_once()
    session.refresh.assert_awaited_once()
    session.flush.assert_not_awaited()


async def test_update_commit_false_flushes_only():
    repo, session = _make_repo()
    entity = _FakeModel(id=5, name="old")
    repo.get_async = AsyncMock(return_value=entity)  # type: ignore[method-assign]
    await repo.update_async(5, commit=False, name="new")
    assert entity.name == "new"
    session.flush.assert_awaited_once()
    session.commit.assert_not_awaited()
    session.refresh.assert_not_awaited()


async def test_update_missing_entity_returns_none_no_write():
    repo, session = _make_repo()
    repo.get_async = AsyncMock(return_value=None)  # type: ignore[method-assign]
    result = await repo.update_async(99, commit=False, name="x")
    assert result is None
    session.flush.assert_not_awaited()
    session.commit.assert_not_awaited()

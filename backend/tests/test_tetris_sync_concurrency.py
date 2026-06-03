"""테트리스 sync_all 동시 실행 중복 잡 방지 회귀 테스트.

배경: 인터벌 루프(_tetris_sync_loop)와 수동 /sync(또는 더블클릭)가 시간상 겹치면
두 sync_all 코루틴이 별도 트랜잭션에서 atomic 가드(_exists_pending_transmit, DB 읽기)를
서로 commit 전에 통과 → 같은 (소싱처, 브랜드, 계정) 전송잡을 양쪽이 INSERT.
프로덕션 실측(2026-06-03): 16개 조합이 gap 0.001~0.8초로 중복 생성됨.

수정: 모듈 레벨 _sync_all_lock 으로 직렬화. 이미 실행 중이면 비차단 스킵.
"""

import asyncio
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.domain.samba.tetris import service as tetris_service
from backend.domain.samba.tetris.service import SambaTetrisService


@pytest.mark.asyncio
async def test_sync_all_concurrent_runs_serialized(monkeypatch) -> None:
    """동시에 sync_all 두 번 호출 시 본문(_sync_all_impl)은 한 번만 실행되고,
    겹친 호출은 skipped_running=True 로 스킵된다."""

    # 토글 ON 가드 통과
    async def _fake_get_setting(session, key):  # noqa: ANN001
        return "1"

    monkeypatch.setattr(
        "backend.api.v1.routers.samba.proxy._helpers._get_setting",
        _fake_get_setting,
    )

    # 일시정지 아님 가드 통과
    monkeypatch.setattr(
        "backend.domain.samba.shipment.service.is_cancel_requested",
        lambda marker: False,
    )

    impl_calls = {"count": 0}

    async def _slow_impl(self, tenant_id):  # noqa: ANN001
        impl_calls["count"] += 1
        # 본문이 도는 동안 두번째 호출이 들어오도록 양보
        await asyncio.sleep(0.05)
        return {"assignments": 1, "jobs": 1, "triggered": 1}

    monkeypatch.setattr(SambaTetrisService, "_sync_all_impl", _slow_impl)

    # 락이 이전 테스트 잔여로 잠겨있지 않은지 확인
    assert not tetris_service._sync_all_lock.locked()

    svc_a = SambaTetrisService(repo=None, session=None)  # type: ignore[arg-type]
    svc_b = SambaTetrisService(repo=None, session=None)  # type: ignore[arg-type]

    results = await asyncio.gather(
        svc_a.sync_all(None),
        svc_b.sync_all(None),
    )

    # 본문은 정확히 1회만 실행
    assert impl_calls["count"] == 1, (
        f"_sync_all_impl 가 {impl_calls['count']}회 실행됨 (중복)"
    )

    # 정확히 한쪽은 정상 실행, 다른 한쪽은 skipped_running
    skipped = [r for r in results if r.get("skipped_running")]
    ran = [r for r in results if not r.get("skipped_running")]
    assert len(skipped) == 1
    assert len(ran) == 1
    assert ran[0]["jobs"] == 1

    # 락이 정상 해제됐는지 확인
    assert not tetris_service._sync_all_lock.locked()

"""백그라운드 잡 워커 — FastAPI lifespan에서 실행.

재시작 시 stuck running 잡 자동 복구 포함.
"""

import asyncio
import ctypes
import gc
import json
import logging
import re
import time as _time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from backend.domain.samba.collector.model import (
    FIXED_REQUESTED_COUNT,
    generate_search_cache_id,
)

logger = logging.getLogger(__name__)
UTC = timezone.utc


def _fmt_num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


_LOG_NUMBER_PATTERN = re.compile(r"(?<![\d/])(\d{4,})(?=(건|개|원|회|토큰|페이지))")
_LOG_FRACTION_PATTERN = re.compile(r"\[(\d+)/(\d+)\]")
_LOG_UNIT_NUMBER_PATTERN = re.compile(
    r"(?<![\d,])(\d{4,})(?=(건|개|원|회|토큰|페이지))"
)
_LOG_CONTEXT_NUMBER_PATTERN = re.compile(
    r"(?P<prefix>(?:원가|판매가|정상가|계산가|성공|스킵|실패|상품|옵션|선택|총|전체|대기|완료|남은|중단|재고변동)\s*)(?P<num>\d{4,})(?=(?:\D|$))"
)


def _normalize_job_log_numbers(msg: str) -> str:
    def _fmt_fraction(match: re.Match[str]) -> str:
        return f"[{_fmt_num(match.group(1))}/{_fmt_num(match.group(2))}]"

    msg = _LOG_FRACTION_PATTERN.sub(_fmt_fraction, msg)
    msg = _LOG_NUMBER_PATTERN.sub(lambda m: _fmt_num(m.group(1)), msg)
    msg = _LOG_UNIT_NUMBER_PATTERN.sub(lambda m: _fmt_num(m.group(1)), msg)
    return _LOG_CONTEXT_NUMBER_PATTERN.sub(
        lambda m: f"{m.group('prefix')}{_fmt_num(m.group('num'))}",
        msg,
    )


# 수집 잡 진행 트래커 — job_id → 마지막 저장 시각 (UNIX timestamp)
# 저장 루프에서 갱신, 스레드 래퍼에서 polling하여 진행 기반 타임아웃 판단
# CPython dict read/write는 GIL로 thread-safe
_collect_last_progress: dict[str, float] = {}


def _force_free_memory():
    """gc.collect() + glibc malloc_trim으로 해제된 메모리를 OS에 반환."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass  # Windows/macOS에서는 무시


# Job별 실시간 로그 버퍼 (인메모리, 최근 500줄)
_job_logs: dict[str, list[str]] = {}
_MAX_JOB_LOGS = 5000  # 인덱스 기반 since 폴링이므로 리스트 사용 (deque는 인덱스 어긋남)

# 수집 로그 주기적 DB 플러시 카운터 (크로스 인스턴스 동기화용)
_collect_log_flush_counter: dict[str, int] = {}

# job_id별 플러시 진행 중 여부 — 동시 UPDATE tuple lock 방지
_flush_in_progress: dict[str, bool] = {}

# ── 전송 로그 전용 링 버퍼 (오토튠과 동일 방식) ──
_shipment_log_buffer: deque[str] = deque(maxlen=300)
_shipment_log_total: int = 0  # 누적 카운터
# 사용자가 명시적으로 초기화했는지 여부 — DB fallback 억제용
# (clear 직후 폴링이 옛 DB 로그를 다시 끌어오는 버그 방지)
_shipment_log_cleared: bool = False


def get_shipment_logs(since_idx: int = 0) -> tuple[list[str], int]:
    """전송 로그 링 버퍼 조회 (since_idx 이후). (logs, current_idx) 반환."""
    global _shipment_log_total
    buf_len = len(_shipment_log_buffer)
    buf_start = _shipment_log_total - buf_len
    if since_idx >= _shipment_log_total:
        return [], _shipment_log_total
    if since_idx <= buf_start:
        logs = list(_shipment_log_buffer)
    else:
        offset = since_idx - buf_start
        logs = list(_shipment_log_buffer)[offset:]
    return logs, _shipment_log_total


def is_shipment_log_cleared() -> bool:
    """사용자가 명시적으로 로그 초기화한 직후인지 여부."""
    return _shipment_log_cleared


def _add_shipment_log(msg: str):
    """전송 로그를 링 버퍼에 추가."""
    global _shipment_log_total, _shipment_log_cleared
    _shipment_log_buffer.append(msg)
    _shipment_log_total += 1
    # 새 로그가 들어오면 cleared 플래그 자동 해제
    _shipment_log_cleared = False


def clear_shipment_logs():
    """전송 로그 링 버퍼 초기화 (사용자 요청 시만)."""
    global _shipment_log_total, _shipment_log_cleared
    _shipment_log_buffer.clear()
    _shipment_log_total = 0
    _shipment_log_cleared = True


# ── 수집 로그 전용 링 버퍼 (전송과 동일 방식) ──
_collect_log_buffer: deque[str] = deque(maxlen=300)
_collect_log_total: int = 0

# 수집 잡 컨텍스트 추적 — _add_job_log 호출 시 자동으로 collect 링 버퍼에 추가하기 위함
from contextvars import ContextVar  # noqa: E402

_current_collect_job_id: ContextVar[str] = ContextVar(
    "current_collect_job_id", default=""
)
_current_transmit_job_id: ContextVar[str] = ContextVar(
    "current_transmit_job_id", default=""
)
_current_order_sync_job_id: ContextVar[str] = ContextVar(
    "current_order_sync_job_id", default=""
)


def get_collect_logs(since_idx: int = 0) -> tuple[list[str], int]:
    """수집 로그 링 버퍼 조회 (since_idx 이후). (logs, current_idx) 반환."""
    global _collect_log_total
    buf_len = len(_collect_log_buffer)
    buf_start = _collect_log_total - buf_len
    if since_idx >= _collect_log_total:
        return [], _collect_log_total
    if since_idx <= buf_start:
        logs = list(_collect_log_buffer)
    else:
        offset = since_idx - buf_start
        logs = list(_collect_log_buffer)[offset:]
    return logs, _collect_log_total


def _add_collect_log(msg: str):
    """수집 로그를 링 버퍼에 추가."""
    global _collect_log_total
    _collect_log_buffer.append(msg)
    _collect_log_total += 1


def clear_collect_logs():
    """수집 로그 링 버퍼 초기화."""
    global _collect_log_total
    _collect_log_buffer.clear()
    _collect_log_total = 0


async def _cancellable_sleep(seconds: float) -> bool:
    """취소 가능한 sleep — 1초 단위로 취소 여부 체크. 취소되면 True 반환."""
    from backend.domain.samba.emergency import (
        is_collect_cancel_requested,
        is_emergency_stopped,
    )

    for _ in range(max(1, int(seconds))):
        if is_collect_cancel_requested() or is_emergency_stopped():
            return True
        await asyncio.sleep(1)
    return False


async def _flush_job_logs(job_id: str, logs: list[str], job_type: str) -> None:
    """잡 로그를 DB에 영속화 — 서버 재시작 후 복원용."""
    if not logs:
        return
    # 이미 플러시 진행 중이면 스킵 — 동시 UPDATE tuple lock 방지
    if _flush_in_progress.get(job_id):
        return
    _flush_in_progress[job_id] = True
    try:
        from sqlalchemy import text as _text
        from backend.db.orm import get_write_session

        async with get_write_session() as session:
            await session.execute(
                _text(
                    "UPDATE samba_jobs SET logs = CAST(:logs AS jsonb) WHERE id = :jid"
                ),
                {"logs": json.dumps(logs, ensure_ascii=False), "jid": job_id},
            )
            await session.commit()
        logger.info(f"[잡워커] {job_type} 로그 DB 저장: {job_id} ({len(logs)}줄)")
    except Exception as le:
        logger.warning(f"[잡워커] {job_type} 로그 DB 저장 실패: {job_id} — {le}")
    finally:
        _flush_in_progress[job_id] = False


def get_job_logs(job_id: str, since: int = 0) -> list[str]:
    """Job 로그 조회 (since 인덱스 이후)."""
    buf = _job_logs.get(job_id)
    if not buf:
        return []
    return buf[since:]


def _add_job_log(job_id: str, msg: str, job_type: str = ""):
    """Job 로그 추가 (최대 _MAX_JOB_LOGS 유지) + 링 버퍼에도 저장."""
    # 백엔드 타임스탬프 (KST) — 프론트 폴링 시각이 아닌 실제 처리 시각 기록
    from datetime import datetime as _dt, timezone, timedelta

    msg = _normalize_job_log_numbers(msg)
    msg = f"[{(_dt.now(timezone.utc) + timedelta(hours=9)).strftime('%H:%M:%S')}] {msg}"
    if job_id not in _job_logs:
        _job_logs[job_id] = []
    buf = _job_logs[job_id]
    buf.append(msg)
    if len(buf) > _MAX_JOB_LOGS:
        _job_logs[job_id] = buf[-_MAX_JOB_LOGS:]
    # 수집/전송 링 버퍼 분기 — job_type 미지정 시 ContextVar로 자동 감지
    effective_type = job_type
    if not effective_type:
        if _current_collect_job_id.get() == job_id:
            effective_type = "collect"
        elif _current_transmit_job_id.get() == job_id:
            effective_type = "transmit"
        elif _current_order_sync_job_id.get() == job_id:
            effective_type = "order_sync"
    if effective_type == "collect":
        _add_collect_log(msg)
        # 20줄마다 DB 플러시 — Cloud Run 멀티 인스턴스에서도 로그 조회 가능하도록
        _collect_log_flush_counter[job_id] = (
            _collect_log_flush_counter.get(job_id, 0) + 1
        )
        if _collect_log_flush_counter[job_id] % 50 == 0:
            import asyncio as _asyncio

            try:
                _loop = _asyncio.get_running_loop()
                _cur_logs = list(_job_logs.get(job_id, []))
                _loop.create_task(_flush_job_logs(job_id, _cur_logs, "수집"))
            except RuntimeError:
                pass
    elif effective_type == "transmit":
        _add_shipment_log(msg)
        # 50줄마다 DB 플러시 — Cloud Run 멀티 인스턴스에서도 로그 조회 가능하도록
        _collect_log_flush_counter[job_id] = (
            _collect_log_flush_counter.get(job_id, 0) + 1
        )
        if _collect_log_flush_counter[job_id] % 50 == 0:
            import asyncio as _asyncio

            try:
                _loop = _asyncio.get_running_loop()
                _cur_logs = list(_job_logs.get(job_id, []))
                _loop.create_task(_flush_job_logs(job_id, _cur_logs, "전송"))
            except RuntimeError:
                pass

    elif effective_type == "order_sync":
        _collect_log_flush_counter[job_id] = (
            _collect_log_flush_counter.get(job_id, 0) + 1
        )
        if _collect_log_flush_counter[job_id] % 10 == 0:
            import asyncio as _asyncio

            try:
                _loop = _asyncio.get_running_loop()
                _cur_logs = list(_job_logs.get(job_id, []))
                _loop.create_task(_flush_job_logs(job_id, _cur_logs, "order_sync"))
            except RuntimeError:
                pass


def clear_job_logs(job_id: str):
    """완료된 잡 로그 삭제 — 메모리 해제 (링 버퍼는 유지)."""
    _job_logs.pop(job_id, None)


# 워커 상태 추적 (health 엔드포인트용)
_worker_status: dict[str, str | None] = {
    "alive": "false",
    "last_poll": None,
    "started_at": None,
    "restarts": "0",
}


def get_worker_status() -> dict[str, str | None]:
    """현재 워커 상태 반환."""
    return dict(_worker_status)


async def _fail_job_safe(job_id: str, error_msg: str) -> None:
    """스레드 크래시 시 안전하게 잡을 FAILED로 마킹 (RUNNING 고착 방지)."""
    from backend.db.orm import get_write_session
    from backend.domain.samba.job.repository import SambaJobRepository

    async with get_write_session() as session:
        repo = SambaJobRepository(session)
        await repo.fail_job(job_id, error_msg)
        await session.commit()
    _add_job_log(job_id, f"수집 실패: {error_msg}", job_type="collect")


def _run_transmit_in_thread(worker: "JobWorker", job_id: str, payload: dict):
    """별도 스레드에서 독립 이벤트 루프로 전송 실행 — API 요청과 I/O 완전 격리."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(worker._execute_transmit_isolated(job_id, payload))
    except Exception as e:
        logger.error(f"[잡워커] 전송 스레드 에러: {job_id} — {e}")
        # 잡 상태를 FAILED로 업데이트 — 미처리 시 RUNNING 고착 방지
        _err_msg = f"전송 스레드 에러: {e}"
        try:
            from backend.db.orm import get_write_session
            from backend.domain.samba.job.repository import SambaJobRepository

            async def _fail_transmit():
                async with get_write_session() as session:
                    repo = SambaJobRepository(session)
                    await repo.fail_job(job_id, _err_msg)
                    await session.commit()

            loop.run_until_complete(_fail_transmit())
        except Exception as fe:
            logger.error(
                f"[잡워커] 전송 스레드 에러 후 잡 상태 갱신 실패: {job_id} — {fe}"
            )
    finally:
        # 스레드 전용 엔진 dispose — 풀의 TCP 커넥션을 Cloud SQL에 즉시 반납
        # 생략 시 loop.close() 만으로는 asyncpg 소켓이 GC까지 살아있어 좀비 누적 → max_connections 고갈
        try:
            from backend.db.orm import _write_engine_cache, _read_engine_cache

            for _cache in (_write_engine_cache, _read_engine_cache):
                _eng = _cache.get(loop)
                if _eng is not None:
                    try:
                        loop.run_until_complete(_eng.dispose())
                    except Exception as de:
                        logger.warning(f"[잡워커] 전송 엔진 dispose 실패: {de}")
        except Exception:
            pass
        loop.close()


class JobWorker:
    """pending 잡을 폴링하여 병렬 실행 (전송 무제한 동시)."""

    POLL_INTERVAL = 5  # 초

    STUCK_CHECK_INTERVAL = 2  # 2회 폴링마다 stuck 체크 (≒10초)
    STUCK_THRESHOLD_SEC = 1800  # 30분 이상 RUNNING 상태면 stuck 판정 (ABCmart 대량 수집 정상 소요시간 수용)

    def __init__(self):
        self._running = True
        self._shutting_down = False  # SIGTERM 수신 시 True — 전송 루프가 체크
        self._active_job_ids: set[str] = set()  # 현재 실행 중인 잡 ID 집합
        self._active_tasks: dict[
            str, asyncio.Task
        ] = {}  # job_id → Task (수집+전송 병렬용)
        # 소싱처별 동시 실행 제어 — 같은 소싱처는 순차, 다른 소싱처는 병렬
        self._active_collect_sources: set[str] = set()
        # 마켓 계정별 transmit 동시 실행 제어 — 같은 계정은 순차, 다른 계정은 병렬
        # job_id → list[account_id]
        self._active_transmit_accounts: dict[str, list[str]] = {}
        # 동일 계정 transmit 잡 직렬화 — 스케줄러 방어가 새더라도 실제 실행은 1개만 허용
        self._transmit_account_locks: dict[str, asyncio.Lock] = {}
        # 동일 계정 delete_market 잡 직렬화 — transmit 락과 독립 (전송/삭제 별개 실행)
        self._delete_account_locks: dict[str, asyncio.Lock] = {}
        # transmit 글로벌 동시 실행 한도 — write pool 여유 확보 (오토튠 점유분 고려)
        self._transmit_semaphore = asyncio.Semaphore(5)
        # delete_market 전용 세마포어 — transmit 세마포어와 분리하여 전송 포화 시에도 즉시 실행
        self._delete_semaphore = asyncio.Semaphore(2)
        # brand_all 잡 직렬화 — SSG+MUSINSA 동시 실행 시 DB/메모리 고갈 방지
        self._brand_all_running: bool = False
        self._poll_count = 0
        # 검색 결과 캐시: {(site, keyword): (items_list, timestamp)}
        # 동일 브랜드 그룹 수집 시 전수 검색 1회만 실행
        self._search_cache: dict[tuple[str, str], tuple[list, float]] = {}

    @staticmethod
    def _extract_transmit_account_ids(payload: dict[str, Any] | None) -> list[str]:
        """전송 잡 payload에서 계정 ID 목록을 정규화해 추출."""
        payload = payload or {}
        account_ids: list[str] = []

        raw_ids = payload.get("target_account_ids") or []
        if isinstance(raw_ids, list):
            for value in raw_ids:
                account_id = str(value or "").strip()
                if account_id:
                    account_ids.append(account_id)

        for key in ("account_id", "target_account_id"):
            value = str(payload.get(key) or "").strip()
            if value:
                account_ids.append(value)

        deduped: list[str] = []
        seen: set[str] = set()
        for account_id in account_ids:
            if account_id in seen:
                continue
            seen.add(account_id)
            deduped.append(account_id)
        return deduped

    def _get_transmit_account_lock(self, account_id: str) -> asyncio.Lock:
        if account_id not in self._transmit_account_locks:
            self._transmit_account_locks[account_id] = asyncio.Lock()
        return self._transmit_account_locks[account_id]

    def _get_delete_account_lock(self, account_id: str) -> asyncio.Lock:
        if account_id not in self._delete_account_locks:
            self._delete_account_locks[account_id] = asyncio.Lock()
        return self._delete_account_locks[account_id]

    async def start(self):
        """무한 루프: pending 잡 조회 → 전송 잡 병렬 실행 (무제한)."""
        logger.info("[잡워커] 시작 (병렬 모드: 전송 무제한 동시 실행)")
        _worker_status["alive"] = "true"
        _worker_status["started_at"] = datetime.now(UTC).isoformat()
        _worker_status["restarts"] = str(int(_worker_status.get("restarts") or 0) + 1)
        # 부팅 시 이전 프로세스의 잔류 세마포어 1회 클리어
        try:
            from backend.domain.samba.shipment.service import clear_account_semaphores

            clear_account_semaphores()
        except Exception:
            pass
        # 배포/재시작으로 stuck된 running 잡 자동 복구 — 시작 시 threshold 없이 전부 복구
        await self._recover_stuck_jobs(force=True)
        while self._running:
            try:
                # 주기적 stuck 잡 복구 (배포/DB 끊김 후 running 상태로 남은 잡)
                self._poll_count += 1
                if self._poll_count % self.STUCK_CHECK_INTERVAL == 0:
                    await self._recover_stuck_jobs()
                executed = await self._poll_once()
                if not executed:
                    await asyncio.sleep(self.POLL_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[잡워커] 폴링 에러: {e}")
                await asyncio.sleep(self.POLL_INTERVAL)
        _worker_status["alive"] = "false"
        logger.info("[잡워커] 종료")

    async def _recover_stuck_jobs(self, force: bool = False):
        """stuck running 잡을 pending으로 복구 — 현재 워커가 실행 중인 잡은 제외.

        force=True: threshold 없이 전체 복구 (재시작 직후 전용).
        force=False: STUCK_THRESHOLD_SEC 초과 잡만 복구 (주기적 체크).
        """
        try:
            from backend.db.orm import get_write_session
            from backend.domain.samba.job.repository import SambaJobRepository

            threshold = 0 if force else self.STUCK_THRESHOLD_SEC
            async with get_write_session() as session:
                repo = SambaJobRepository(session)
                recovered = await repo.recover_stuck_running(
                    exclude_ids=self._active_job_ids,
                    threshold_sec=threshold,
                )
                if recovered:
                    await session.commit()
                    logger.info(
                        f"[잡워커] stuck running 잡 {recovered}건 → pending 복구"
                        + (" (강제 복구)" if force else "")
                    )
        except Exception as e:
            logger.warning(f"[잡워커] stuck 잡 복구 실패: {e}")

    def stop(self):
        self._running = False

    async def graceful_stop(self, timeout: int = 30):
        """배포 시 호출 — 전송 루프에 종료 신호 보내고 대기.

        1) _shutting_down 플래그 세팅 → 전송 루프가 현재 건 완료 후 탈출
        2) 최대 timeout초 대기 → 모든 전송 Task 종료 확인
        3) running Job → pending으로 전환 (current 보존)
        """
        self._shutting_down = True
        self._running = False
        logger.info(
            f"[잡워커] graceful_stop — {len(self._active_job_ids)}개 잡 종료 대기"
        )

        # 모든 활성 Task가 종료될 때까지 대기
        for _ in range(timeout):
            if not self._active_tasks and not self._active_collect_sources:
                break
            await asyncio.sleep(1)

        # 모든 running transmit Job → pending 복구 (current 보존)
        # _execute_job().finally가 먼저 _active_job_ids를 비우므로
        # remaining_ids에 의존하지 않고 DB를 직접 조회한다
        try:
            from backend.db.orm import get_write_session
            from sqlalchemy import text

            async with get_write_session() as session:
                r = await session.execute(
                    text(
                        "UPDATE samba_jobs SET status = 'pending', "
                        "started_at = NULL "
                        "WHERE status = 'running' AND job_type IN ('transmit', 'delete_market')"
                    )
                )
                await session.commit()
                if r.rowcount > 0:
                    logger.info(
                        f"[잡워커] 배포 종료 — {r.rowcount}개 잡 → pending 복구"
                    )
        except Exception as e:
            logger.error(f"[잡워커] 배포 종료 잡 복구 실패: {e}")

    async def _poll_once(self) -> bool:
        """전송 잡 병렬 실행 — 빈 슬롯만큼 배치 픽업.

        FOR UPDATE SKIP LOCKED로 원자적 잡 획득 — 멀티 worker 중복 실행 방지.
        호출 1회당 transmit 빈 슬롯(=5 - 활성 transmit Task)만큼 연속 클레임하여
        다른 계정 잡을 즉시 병렬 시작 (큐 맨 앞이 같은 계정으로 채워져 있어도
        SQL NOT EXISTS + 인메모리 exclude로 자연스럽게 다른 계정으로 점프).
        """
        _worker_status["last_poll"] = datetime.now(UTC).isoformat()

        # 완료된 Task 정리
        done_ids = [jid for jid, task in self._active_tasks.items() if task.done()]
        for jid in done_ids:
            task = self._active_tasks.pop(jid)
            self._active_job_ids.discard(jid)
            self._active_transmit_accounts.pop(jid, None)
            if task.cancelled():
                # 취소된 Task — CancelledError는 BaseException이므로 exception() 호출 시 재발생
                # start() 루프의 except CancelledError: break에 걸려 워커 사망 방지
                logger.warning(f"[잡워커] 전송 Task 취소됨: {jid}")
                continue
            exc = task.exception()
            if exc:
                logger.error(f"[잡워커] 전송 Task 예외: {jid} — {exc}")

        from backend.db.orm import get_write_session
        from backend.domain.samba.job.repository import SambaJobRepository
        from backend.domain.samba.shipment.service import is_cancel_requested

        # transmit 빈 슬롯 계산 (글로벌 세마포어 한도 5 기준)
        transmit_running = sum(
            1 for jid in self._active_tasks if jid in self._active_transmit_accounts
        )
        transmit_slots = max(0, 5 - transmit_running)
        # 한 폴링 사이클 최대 픽업 개수 = transmit 슬롯 + 비-transmit 여유분(2)
        # 비-transmit(collect/delete/기타)이 들어와도 큐 진행이 막히지 않도록 여유 둠
        max_picks = transmit_slots + 2
        picked = 0

        for _ in range(max_picks):
            # 매 iteration마다 fresh write session — claim → commit 후 즉시 닫아
            # 다음 iteration의 _excl_accounts 계산이 최신 _active_transmit_accounts 반영
            async with get_write_session() as session:
                repo = SambaJobRepository(session)
                # 현재 실행 중인 소싱처/계정 — 같은 소싱처 순차, 같은 계정 순차
                _excl_sources = set(self._active_collect_sources)
                _excl_accounts: set[str] = set()
                for _aids in self._active_transmit_accounts.values():
                    _excl_accounts.update(_aids)
                # 일시정지 중이면 transmit 클레임 스킵 — PENDING 잡 대기 유지
                _excl_types: set[str] = {"bg_remove"}
                if is_cancel_requested("__all__"):
                    _excl_types.add("transmit")
                job = await repo.claim_pending_job(
                    exclude_sources=_excl_sources or None,
                    exclude_brand_all=self._brand_all_running,
                    exclude_types=_excl_types,
                    exclude_accounts=_excl_accounts or None,
                )
                if not job:
                    break
                self._active_job_ids.add(job.id)
                await session.commit()

            # 전송/마켓삭제: asyncio.Task로 백그라운드 병렬 실행 (동일 계정 순차 보장)
            if job.job_type in ("transmit", "delete_market"):
                _tx_accounts = self._extract_transmit_account_ids(job.payload)
                # transmit 잡만 등록 — delete_market은 별도 세마포어/락으로 관리하므로
                # _active_transmit_accounts에 포함하면 같은 계정의 transmit 잡을 불필요하게 차단함
                if job.job_type == "transmit":
                    self._active_transmit_accounts[job.id] = _tx_accounts

                if job.job_type == "delete_market":

                    async def _run_with_limit(_j=job):
                        async with self._delete_semaphore:
                            await self._execute_job(_j)
                else:

                    async def _run_with_limit(_j=job):  # type: ignore[misc]
                        async with self._transmit_semaphore:
                            await self._execute_job(_j)

                task = asyncio.create_task(
                    _run_with_limit(),
                    name=f"{job.job_type}-{job.id}",
                )
                self._active_tasks[job.id] = task
                logger.info(
                    f"[잡워커] {job.job_type} Task 생성: {job.id} "
                    f"(동시 실행: {len(self._active_tasks)}개, "
                    f"계정={_tx_accounts})"
                )
                picked += 1
                continue

            # 수집: 소싱처별 병렬 Task (같은 소싱처는 exclude_sources로 순차 보장)
            if job.job_type == "collect":
                _site = (job.payload or {}).get("source_site", "?")
                # Task 생성 전에 즉시 등록 — 폴링 루프가 sleep 없이 연속 호출될 때
                # _execute_job 내부에서 add()하면 Task 실행 전까지 반영 안 됨 (race condition)
                if _site and _site != "?":
                    self._active_collect_sources.add(_site)
                task = asyncio.create_task(
                    self._execute_job(job),
                    name=f"collect-{job.id}",
                )
                self._active_tasks[job.id] = task
                logger.info(
                    f"[잡워커] 수집 Task 생성: {job.id} (site={_site}, "
                    f"활성 소싱처={sorted(self._active_collect_sources)})"
                )
                picked += 1
                continue

            # 기타: 기존 방식 (동기 대기) — 사이클 점유하므로 즉시 종료
            await self._execute_job(job)
            picked += 1
            break

        return picked > 0 or bool(self._active_tasks)

    async def _execute_job(self, job):
        """개별 잡 실행 — 수집만 별도 스레드, 전송+기타는 메인 루프."""
        from backend.db.orm import get_write_session
        from backend.domain.samba.job.repository import SambaJobRepository

        try:
            # 수집: 별도 스레드 + 독립 이벤트 루프 (전송과 I/O 격리)
            _job_id = job.id
            _job_type = job.job_type
            _job_payload = job.payload or {}
            if _job_type == "collect":
                _collect_site = (_job_payload or {}).get("source_site") or ""
                _is_brand_all = bool((_job_payload or {}).get("brand_all"))
                if _collect_site:
                    self._active_collect_sources.add(_collect_site)
                if _is_brand_all:
                    self._brand_all_running = True
                    logger.info(
                        f"[잡워커] brand_all 시작 — 직렬 실행 플래그 set: {_job_id} site={_collect_site}"
                    )
                logger.info(
                    f"[잡워커] 수집 실행 (메인 루프 task): {_job_id} site={_collect_site}"
                )
                # 메인 이벤트 루프에서 task로 실행 — 글로벌 AsyncEngine과 동일 루프 사용
                # (별도 스레드 격리 시 SQLAlchemy greenlet_spawn 에러 발생)
                _collect_task = asyncio.create_task(
                    self._execute_collect_isolated(_job_id, _job_payload),
                    name=f"collect-exec-{_job_id}",
                )
                _NO_PROGRESS_SEC = 600  # 10분 동안 새 저장 없으면 타임아웃
                _collect_last_progress[_job_id] = _time.time()  # 시작 기준점 초기화
                _cancel_reason: str | None = None
                while not _collect_task.done():
                    if self._shutting_down:
                        _cancel_reason = "shutdown"
                        logger.info(f"[잡워커] 배포 종료 — 수집 task 취소: {_job_id}")
                        break
                    idle_sec = _time.time() - _collect_last_progress.get(
                        _job_id, _time.time()
                    )
                    if idle_sec > _NO_PROGRESS_SEC:
                        _cancel_reason = "no_progress"
                        break  # 진행 없음 → 타임아웃
                    await asyncio.sleep(2)
                _collect_last_progress.pop(_job_id, None)

                if _cancel_reason:
                    _collect_task.cancel()
                    try:
                        await _collect_task
                    except (asyncio.CancelledError, Exception):
                        pass

                    if _cancel_reason == "shutdown":
                        # 배포/재시작 중단 — pending으로 복구 (다음 인스턴스에서 재실행)
                        logger.info(
                            f"[잡워커] 수집 중 배포 중단 → pending 복구: {_job_id}"
                        )
                        _add_job_log(
                            _job_id,
                            "배포 중단 — 재시작 후 자동 재실행",
                            job_type="collect",
                        )
                        try:
                            async with get_write_session() as shutdown_session:
                                from sqlalchemy import text as _text

                                await shutdown_session.execute(
                                    _text(
                                        "UPDATE samba_jobs SET status='pending' WHERE id=:jid AND status='running'"
                                    ),
                                    {"jid": _job_id},
                                )
                                await shutdown_session.commit()
                        except Exception as se:
                            logger.error(f"[잡워커] 배포 중단 pending 복구 실패: {se}")
                    else:
                        # 진행 없음 타임아웃 → pending 복구 (재시작 시 이어서 수집)
                        logger.warning(
                            f"[잡워커] 수집 진행 없음 {_NO_PROGRESS_SEC}초 → pending 복구: {_job_id}"
                        )
                        _add_job_log(
                            _job_id,
                            f"수집 진행 없음 ({_NO_PROGRESS_SEC // 60}분) — 자동 재시도 예정",
                            job_type="collect",
                        )
                        try:
                            async with get_write_session() as timeout_session:
                                from sqlalchemy import text as _text2

                                await timeout_session.execute(
                                    _text2(
                                        "UPDATE samba_jobs SET status='pending', started_at=NULL WHERE id=:jid AND status='running'"
                                    ),
                                    {"jid": _job_id},
                                )
                                await timeout_session.commit()
                        except Exception as te:
                            logger.error(f"[잡워커] 진행없음 pending 복구 실패: {te}")
                else:
                    # 정상 완료 — task 내부에서 finish_job/fail_job 처리 완료
                    # 단, task 자체 예외는 여기서 catch 후 잡 상태 갱신
                    try:
                        await _collect_task
                    except Exception as e:
                        logger.error(f"[잡워커] 수집 task 예외: {_job_id} — {e}")
                        try:
                            await _fail_job_safe(_job_id, f"수집 예외: {e}")
                        except Exception as fe:
                            logger.error(
                                f"[잡워커] 수집 예외 후 잡 상태 갱신 실패: {_job_id} — {fe}"
                            )
                return

            # 전송 + 기타: 직접 실행 (인메모리 로그 공유)
            _job_id = job.id
            _job_type = job.job_type
            async with get_write_session() as session:
                repo = SambaJobRepository(session)
                # detached 객체 대신 현재 세션에서 job 재조회
                from backend.domain.samba.job.model import SambaJob as _SJ

                fresh_job = await session.get(_SJ, _job_id)
                if not fresh_job:
                    logger.error(f"[잡워커] 잡 재조회 실패: {_job_id}")
                    return
                logger.info(f"[잡워커] 실행: {_job_id} ({_job_type})")

                try:
                    if _job_type == "transmit":
                        _tx_token = _current_transmit_job_id.set(_job_id)
                        _tx_accounts = sorted(
                            set(self._extract_transmit_account_ids(_job_payload))
                        )
                        _tx_locks = [
                            self._get_transmit_account_lock(account_id)
                            for account_id in _tx_accounts
                        ]
                        try:
                            for _lock in _tx_locks:
                                await _lock.acquire()
                            await self._run_transmit(fresh_job, repo, session)
                        finally:
                            for _lock in reversed(_tx_locks):
                                if _lock.locked():
                                    _lock.release()
                            _current_transmit_job_id.reset(_tx_token)
                    elif _job_type == "delete_market":
                        _dm_token = _current_transmit_job_id.set(_job_id)
                        _dm_accounts = sorted(
                            set(self._extract_transmit_account_ids(_job_payload))
                        )
                        _dm_locks = [
                            self._get_delete_account_lock(account_id)
                            for account_id in _dm_accounts
                        ]
                        try:
                            for _lock in _dm_locks:
                                await _lock.acquire()
                            await self._run_delete_market(fresh_job, repo, session)
                        finally:
                            for _lock in reversed(_dm_locks):
                                if _lock.locked():
                                    _lock.release()
                            _current_transmit_job_id.reset(_dm_token)
                    elif _job_type == "refresh":
                        await self._run_stub(fresh_job, repo, "갱신")
                    elif _job_type == "ai_tag":
                        await self._run_stub(fresh_job, repo, "AI태그")
                    elif _job_type == "order_sync":
                        from backend.domain.samba.job.handlers.order_sync import (
                            run as run_order_sync,
                        )

                        _os_token = _current_order_sync_job_id.set(_job_id)
                        try:
                            await run_order_sync(fresh_job, repo, session, self)
                        finally:
                            _current_order_sync_job_id.reset(_os_token)
                    elif _job_type == "cs_sync":
                        from backend.domain.samba.job.handlers.cs_sync import (
                            run as run_cs_sync,
                        )

                        await run_cs_sync(fresh_job, repo, session, self)
                    else:
                        await repo.fail_job(_job_id, f"알 수 없는 잡 타입: {_job_type}")

                    await session.commit()
                except Exception as e:
                    logger.error(f"[잡워커] 잡 실행 실패: {_job_id} — {e}")
                    try:
                        await repo.fail_job(_job_id, str(e))
                        await session.commit()
                    except Exception as fail_exc:
                        logger.error(
                            f"[잡워커] 잡 상태 갱신 실패 (running 고착 가능): {_job_id} — {fail_exc}"
                        )
        finally:
            self._active_job_ids.discard(_job_id)
            self._active_tasks.pop(_job_id, None)
            self._active_transmit_accounts.pop(_job_id, None)
            if _job_type == "collect":
                _collect_site = (_job_payload or {}).get("source_site") or ""
                if _collect_site:
                    self._active_collect_sources.discard(_collect_site)
                if (_job_payload or {}).get("brand_all"):
                    self._brand_all_running = False
                    logger.info(
                        f"[잡워커] brand_all 완료 — 직렬 실행 플래그 clear: {_job_id}"
                    )
            # 프론트 폴링이 로그를 읽을 시간 확보 후 삭제 (60초)
            try:
                asyncio.get_running_loop().call_later(60, clear_job_logs, _job_id)
            except RuntimeError:
                pass  # 루프 종료 중이면 로그 정리 스킵

    async def _execute_collect_isolated(self, job_id: str, payload: dict):
        """격리된 이벤트 루프에서 수집 잡 실행 — 자체 DB 세션 관리."""
        from backend.db.orm import get_write_session
        from backend.domain.samba.job.repository import SambaJobRepository
        from backend.domain.samba.job.model import SambaJob
        from backend.domain.samba.emergency import clear_collect_cancel

        # 새 수집 시작 시 이전 취소 플래그 초기화 (이전 수집의 잔여 플래그 방지)
        clear_collect_cancel()

        # 수집 잡 컨텍스트 설정 — _add_job_log 호출 시 자동으로 collect 링 버퍼에 추가
        _ctx_token = _current_collect_job_id.set(job_id)
        try:
            async with get_write_session() as session:
                repo = SambaJobRepository(session)
                job = await session.get(SambaJob, job_id)
                if not job:
                    logger.error(f"[잡워커] 수집 잡 없음: {job_id}")
                    return
                try:
                    await self._run_collect(job, repo, session)
                    await session.commit()
                except Exception as e:
                    logger.error(f"[잡워커] 수집 실행 실패: {job_id} — {e}")
                    try:
                        # 세션이 InFailedSQLTransactionError 로 aborted 상태일 수 있으므로
                        # fail_job 호출 전 반드시 rollback 하여 트랜잭션 초기화
                        try:
                            await session.rollback()
                        except Exception as rb_exc:
                            logger.warning(
                                f"[잡워커] 세션 rollback 실패(무시): {job_id} — {rb_exc}"
                            )
                        await repo.fail_job(job_id, str(e))
                        await session.commit()
                    except Exception as fail_exc:
                        logger.error(
                            f"[잡워커] 잡 상태 갱신 실패 (running 고착 가능): {job_id} — {fail_exc}"
                        )
        except Exception as e:
            logger.error(f"[잡워커] 수집 세션 에러: {job_id} — {e}")
        finally:
            _current_collect_job_id.reset(_ctx_token)
            await _flush_job_logs(job_id, list(_collect_log_buffer), "수집")

    async def _execute_transmit_isolated(self, job_id: str, payload: dict):
        """격리된 이벤트 루프에서 전송 잡 실행 — 자체 DB 세션 관리."""
        from backend.db.orm import get_write_session
        from backend.domain.samba.job.repository import SambaJobRepository
        from backend.domain.samba.job.model import SambaJob

        # 별도 이벤트 루프이므로 이전 루프의 세마포어 정리
        from backend.domain.samba.shipment.service import clear_account_semaphores

        clear_account_semaphores()
        _ctx_token = _current_transmit_job_id.set(job_id)

        try:
            async with get_write_session() as session:
                repo = SambaJobRepository(session)
                job = await session.get(SambaJob, job_id)
                if not job:
                    logger.error(f"[잡워커] 전송 잡 없음: {job_id}")
                    return
                try:
                    await self._run_transmit(job, repo, session)
                    await session.commit()
                except Exception as e:
                    logger.error(f"[잡워커] 전송 실행 실패: {job_id} — {e}")
                    try:
                        # 세션이 InFailedSQLTransactionError 로 aborted 상태일 수 있으므로
                        # fail_job 호출 전 반드시 rollback 하여 트랜잭션 초기화
                        try:
                            await session.rollback()
                        except Exception as rb_exc:
                            logger.warning(
                                f"[잡워커] 세션 rollback 실패(무시): {job_id} — {rb_exc}"
                            )
                        await repo.fail_job(job_id, str(e))
                        await session.commit()
                    except Exception as fail_exc:
                        logger.error(
                            f"[잡워커] 잡 상태 갱신 실패 (running 고착 가능): {job_id} — {fail_exc}"
                        )
        except Exception as e:
            logger.error(f"[잡워커] 전송 세션 에러: {job_id} — {e}")
        finally:
            await _flush_job_logs(job_id, list(_shipment_log_buffer), "전송")

            _current_transmit_job_id.reset(_ctx_token)

    async def _run_transmit(self, job, repo, session):
        """전송 잡 실행 — 기존 shipment_service 호출."""
        from backend.domain.samba.shipment.service import (
            SambaShipmentService,
            is_cancel_requested,
            clear_cancel_transmit,
        )
        from backend.domain.samba.shipment.repository import SambaShipmentRepository
        from backend.domain.samba.emergency import clear_emergency_stop

        # 새 잡 시작 — 이 잡의 잔존 플래그만 해제 (__all__ 유지 — 일시정지 중 다음 잡 클레임 차단)
        clear_cancel_transmit(job.id)
        clear_emergency_stop()
        # 이전 잡 로그 잔재가 새 잡 화면에 흘러나오지 않도록 ring buffer 격리
        clear_shipment_logs()

        payload = job.payload or {}
        product_ids = payload.get("product_ids", [])
        update_items = payload.get("update_items", [])
        target_account_ids = payload.get("target_account_ids", [])
        skip_unchanged = payload.get("skip_unchanged", False)
        # 프론트에서 테트리스 배치 기반으로 직접 target_account_ids 구성한 경우 True
        _payload_tetris_flag = bool(payload.get("skip_policy_account_filter", False))

        if not product_ids:
            await repo.fail_job(job.id, "product_ids 없음")
            return

        from backend.domain.samba.collector.repository import (
            SambaCollectedProductRepository,
        )
        from backend.domain.samba.account.repository import SambaMarketAccountRepository
        from backend.db.orm import get_write_session

        # tetris 매칭 사전 로드
        # (source_site_norm, brand_norm) → list[market_account_id] (브랜드당 여러 마켓 배정 가능)
        _tetris_account_map: dict[tuple[str, str], list[str]] = {}
        # market_account_id → market_type (오버라이드 시 마켓별 교체 판정용)
        _tetris_acc_market: dict[str, str] = {}
        try:
            from backend.domain.samba.forbidden.model import SambaSettings
            from backend.domain.samba.tetris.repository import SambaTetrisRepository
            from backend.domain.samba.tetris.service import (
                _norm_site_key as _ts_norm_site,
                _norm_tetris_key as _ts_norm_brand,
            )
            from sqlmodel import select as _select

            _tenant_id = getattr(job, "tenant_id", None)
            _setting_key = (
                f"{_tenant_id}:tetris_matching_enabled"
                if _tenant_id
                else "tetris_matching_enabled"
            )
            async with get_write_session() as _cfg_sess:
                _setting_row = (
                    (
                        await _cfg_sess.execute(
                            _select(SambaSettings).where(
                                SambaSettings.key == _setting_key
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                _tetris_enabled = bool(_setting_row.value) if _setting_row else False
                # payload 플래그가 True이면 DB 설정과 무관하게 테트리스 오버라이드 활성화
                if _payload_tetris_flag:
                    _tetris_enabled = True
                if _tetris_enabled:
                    _tet_repo = SambaTetrisRepository(_cfg_sess)
                    _assignments = await _tet_repo.list_by_tenant(_tenant_id)
                    _acc_repo_pre = SambaMarketAccountRepository(_cfg_sess)
                    for _a in _assignments:
                        _norm_key = (
                            _ts_norm_site(_a.source_site),
                            _ts_norm_brand(_a.brand_name),
                        )
                        _tetris_account_map.setdefault(_norm_key, []).append(
                            _a.market_account_id
                        )
                        if _a.market_account_id not in _tetris_acc_market:
                            _acc_obj = await _acc_repo_pre.get_async(
                                _a.market_account_id
                            )
                            if _acc_obj:
                                _tetris_acc_market[_a.market_account_id] = (
                                    _acc_obj.market_type
                                )
                    logger.info(
                        f"[잡워커] tetris 매칭 활성 — {len(_tetris_account_map)}개 브랜드 배치 로드"
                    )
        except Exception as _te:
            logger.warning(f"[잡워커] tetris 매칭 로드 실패(무시): {_te}")

        total = len(product_ids)

        # 이어하기: 이전 진행 위치를 먼저 읽은 후 진행률 갱신
        # (update_progress가 identity map으로 job.current를 덮어쓰기 때문)
        start_from = job.current or 0
        # 이어하기 방어: start_from이 total 이상이면 이미 완료된 잡 → complete 처리
        if start_from >= total:
            logger.warning(
                f"[잡워커] start_from({start_from}) >= total({total}) — 이미 완료된 잡"
            )
            await repo.complete_job(
                job.id,
                job.result or {"success": 0, "skipped": 0, "failed": 0},
            )
            await session.commit()
            return
        await repo.update_progress(job.id, start_from, total)

        # 이어하기: 이전 실행의 카운트 복원
        prev_result = job.result or {}
        success_count = prev_result.get("success", 0) if start_from > 0 else 0
        fail_count = prev_result.get("failed", 0) if start_from > 0 else 0
        skip_count = prev_result.get("skipped", 0) if start_from > 0 else 0

        # 상품별 전송 루프 (단건 순차 처리)
        if start_from > 0:
            _add_job_log(job.id, f"이전 진행 {start_from}/{total}건 이후부터 재개")
            logger.info(f"[잡워커] 전송 재개: {job.id} — {start_from}/{total}건부터")

        # 잡 단위 계정 차단 셋 — 등록갯수 한도 초과 등 "계정 자체가 더 이상 등록 불가"인 경우
        # 즉시 해당 계정의 후속 시도를 건너뜀
        blocked_account_ids: set[str] = set()
        blocked_account_reasons: dict[str, str] = {}

        def _is_account_blocking_error(err: str) -> bool:
            if not err:
                return False
            # 11번가: "판매 중인 상품은 최대 5,000개까지 등록할 수 있습니다"
            # 기타 마켓에서도 등록 한도/판매자 상태 차단 메시지 추가 시 여기에 보강
            patterns = (
                "판매 중인 상품은 최대",
                "최대 5,000개",
                "최대 5000개",
                "상품을 판매중지",
            )
            return any(p in err for p in patterns)

        async def _process_one(i: int, pid: str) -> tuple[int, int, int, str | None]:
            """상품 1건 처리 → (success_delta, skip_delta, fail_delta, failed_pid)"""
            prod_name = pid[-8:]
            try:
                async with get_write_session() as item_session:
                    cp_repo = SambaCollectedProductRepository(item_session)
                    acc_repo = SambaMarketAccountRepository(item_session)
                    prod = await cp_repo.get_async(pid)
                    site_pid = prod.site_product_id if prod else ""
                    _brand = (prod.brand or "") if prod else ""
                    _style = (prod.style_code or "") if prod else ""
                    _raw_name = (prod.name or "") if prod else pid[-8:]
                    _source = (prod.source_site or "").upper() if prod else ""
                    prod_name = f"{_brand} {_raw_name}".strip()[:35]
                    if _style:
                        prod_name = f"{prod_name} {_style}"
                    if site_pid:
                        prod_name = f"{prod_name} ({site_pid})"
                    if _source:
                        prod_name = f"[{_source}] {prod_name}"

                    # tetris 매칭이 활성화된 경우 — 매칭된 계정으로만 전송
                    # 매칭 없거나 선택 마켓 범위 밖이면 전송 자체 스킵 (사용자 의도)
                    effective_account_ids = list(target_account_ids)
                    if _tetris_enabled and prod:
                        _norm_k = (
                            _ts_norm_site(prod.source_site),
                            _ts_norm_brand(prod.brand),
                        )
                        _assigned_all = _tetris_account_map.get(_norm_k) or []
                        if not _assigned_all:
                            # 테트리스 매칭 없음 → 전송 스킵
                            effective_account_ids = []
                        elif target_account_ids:
                            # 선택된 마켓 범위 내의 매칭 계정만 사용
                            _selected_markets: set[str] = set()
                            for _tid in target_account_ids:
                                _tacc = await acc_repo.get_async(_tid)
                                if _tacc:
                                    _selected_markets.add(_tacc.market_type)
                            _assigned_list = [
                                a
                                for a in _assigned_all
                                if _tetris_acc_market.get(a) in _selected_markets
                            ]
                            # 매칭됐지만 선택 마켓 범위 밖 → 전송 스킵
                            effective_account_ids = list(_assigned_list)
                        else:
                            # target_account_ids 미지정 시 매칭된 계정 전부 사용
                            effective_account_ids = list(_assigned_all)

                    # 테트리스 매칭 없음/범위 밖 → 전송 스킵
                    if _tetris_enabled and not effective_account_ids:
                        _add_job_log(
                            job.id,
                            f"[{i + 1}/{total:,}] {prod_name}: 스킵 (테트리스 매칭 없음)",
                        )
                        await item_session.commit()
                        return 0, 1, 0, None

                    # 잡 단위 차단 계정 제거 — 등록 한도 초과 등으로 더 이상 시도 불가
                    if blocked_account_ids and effective_account_ids:
                        _before = list(effective_account_ids)
                        effective_account_ids = [
                            a
                            for a in effective_account_ids
                            if a not in blocked_account_ids
                        ]
                        _removed = [a for a in _before if a in blocked_account_ids]
                        if _removed:
                            for _ra in _removed:
                                _reason = blocked_account_reasons.get(_ra, "등록 차단")
                                _add_job_log(
                                    job.id,
                                    f"[{i + 1}/{total:,}] {prod_name} → 계정 {_ra}: 스킵 (잡 차단: {_reason[:80]})",
                                )
                        if not effective_account_ids:
                            await item_session.commit()
                            return 0, 1, 0, None

                    item_svc = SambaShipmentService(
                        SambaShipmentRepository(item_session), item_session
                    )
                    result = await item_svc.start_update(
                        [pid],
                        update_items,
                        effective_account_ids,
                        skip_unchanged=skip_unchanged,
                        skip_policy_account_filter=_tetris_enabled,
                    )
                    results_list = result.get("results", [])
                    r = results_list[0] if results_list else {}
                    status = r.get("status", "unknown")
                    tx_result = r.get("transmit_result", {})
                    tx_error = r.get("transmit_error", {})
                    any_success = False
                    _s = _sk = _f = 0
                    for acc_id, acc_status in tx_result.items():
                        acc = await acc_repo.get_async(acc_id)
                        acc_label = (
                            f"{acc.market_name}({acc.seller_id or acc.business_name or '-'})"
                            if acc
                            else acc_id
                        )
                        ur = r.get("update_result", {})
                        rl = (
                            f" [{ur.get('refresh', '')}]"
                            if isinstance(ur, dict) and ur.get("refresh")
                            else ""
                        )
                        if acc_status in ("success", "completed"):
                            any_success = True
                            _s += 1
                            label = "품절삭제" if acc_status == "completed" else "전송"
                            _add_job_log(
                                job.id,
                                f"[{i + 1}/{total:,}] {prod_name} → {acc_label}: {label}{rl}",
                            )
                        elif acc_status == "skipped":
                            _sk += 1
                            _skip_reason = str(tx_error.get(acc_id, "") or "")[:200]
                            _reason_suffix = (
                                f" ({_skip_reason})" if _skip_reason else ""
                            )
                            _add_job_log(
                                job.id,
                                f"[{i + 1}/{total:,}] {prod_name} → {acc_label}: 스킵{_reason_suffix}{rl}",
                            )
                        else:
                            _f += 1
                            err = str(tx_error.get(acc_id, "실패"))[:500]
                            if "<asyncio" in err or "Semaphore" in err:
                                err = "전송 동시성 오류"
                            _add_job_log(
                                job.id,
                                f"[{i + 1}/{total:,}] {prod_name} → {acc_label}: {err}{rl}",
                            )
                            # 계정 등록 한도 초과 등 — 이후 상품에서 이 계정 자동 스킵
                            if (
                                _is_account_blocking_error(err)
                                and acc_id not in blocked_account_ids
                            ):
                                blocked_account_ids.add(acc_id)
                                blocked_account_reasons[acc_id] = err
                                _add_job_log(
                                    job.id,
                                    f"[잡차단] {acc_label} 계정 등록 차단 — 이후 상품에서 이 계정은 자동 스킵 (사유: {err[:120]})",
                                )
                                logger.warning(
                                    f"[잡워커] 계정 등록 차단: job={job.id} account={acc_id} reason={err[:200]}"
                                )
                    if not tx_result:
                        if status == "skipped":
                            _sk += 1
                            refresh_info = r.get("update_result", {})
                            rl = (
                                refresh_info.get("refresh", "")
                                if isinstance(refresh_info, dict)
                                else ""
                            )
                            _add_job_log(
                                job.id,
                                f"[{i + 1}/{total}] {prod_name}: 스킵 [{rl}]",
                            )
                        elif r.get("error") or tx_error.get("_all"):
                            _f += 1
                            err_msg = r.get("error") or tx_error.get("_all", "실패")
                            _add_job_log(
                                job.id,
                                f"[{i + 1}/{total}] {prod_name}: {str(err_msg)[:500]}",
                            )
                        else:
                            _f += 1
                            _add_job_log(job.id, f"[{i + 1}/{total}] {prod_name}: 실패")
                    _failed_pid = (
                        pid
                        if not any_success and status not in ("skipped", "completed")
                        else None
                    )
                    await item_session.commit()
                    return _s, _sk, _f, _failed_pid
            except Exception as e:
                _add_job_log(job.id, f"[{i + 1}/{total}] {prod_name}: {e}")
                return 0, 0, 1, pid

        BATCH_SIZE = 1
        all_indices = list(range(start_from, total))
        for batch_start in range(0, len(all_indices), BATCH_SIZE):
            batch = all_indices[batch_start : batch_start + BATCH_SIZE]
            i_first = batch[0]
            i_last = batch[-1]

            # 비상정지 + Job 취소 + 전송중단 플래그 체크 (배치별)
            from backend.domain.samba.emergency import is_emergency_stopped

            try:
                _is_cancelled = await repo.is_cancelled(job.id)
            except Exception as exc:
                logger.warning(f"[잡워커] 취소 체크 중 DB 에러: {job.id} — {exc}")
                _is_cancelled = False

            # 배포 종료 감지 — progress 저장 + 즉시 pending 전환 후 탈출
            if self._shutting_down:
                remaining = total - i_first
                _add_job_log(
                    job.id,
                    f"배포 종료 — {i_first}건 완료, {remaining}건 남음 (다음 인스턴스에서 재개)",
                )
                logger.info(
                    f"[잡워커] 배포 종료 감지: {job.id} — {i_first}/{total}건, pending 전환"
                )
                try:
                    from sqlalchemy import text

                    await repo.update_progress(job.id, i_first, total)
                    # 정상 배포 중단 → 즉시 pending + attempt 리셋 (OOM 아님)
                    await session.execute(
                        text(
                            "UPDATE samba_jobs SET status = 'pending', "
                            "started_at = NULL, attempt = 0 "
                            "WHERE id = :jid AND status = 'running'"
                        ),
                        {"jid": job.id},
                    )
                    await session.commit()
                except Exception as exc:
                    logger.warning(
                        f"[잡워커] 배포 종료 진행 저장 실패: {job.id} — {exc}"
                    )
                return  # fail 아닌 정상 리턴

            if is_emergency_stopped() or is_cancel_requested(job.id) or _is_cancelled:
                cancelled = total - i_first
                reason = "비상정지" if is_emergency_stopped() else "취소"
                _add_job_log(job.id, f"{reason} — {i_first}건 완료, {cancelled}건 중단")
                logger.info(
                    f"[잡워커] 전송 {reason}: {job.id} — {i_first}건 완료, {cancelled}건 중단"
                )
                await repo.fail_job(
                    job.id, f"{reason}: {i_first}건 완료, {cancelled}건 중단"
                )
                clear_cancel_transmit(job.id)  # 이 잡 플래그만 해제, __all__ 유지
                clear_emergency_stop()
                return

            # 배치 내 병렬 처리
            batch_results = await asyncio.gather(
                *[_process_one(i, product_ids[i]) for i in batch],
                return_exceptions=True,
            )

            for idx, res in zip(batch, batch_results):
                if isinstance(res, BaseException):
                    fail_count += 1
                else:
                    _s, _sk, _f, _fp = res
                    success_count += _s
                    skip_count += _sk
                    fail_count += _f

            # OOM 방지: 50건마다 gc + malloc_trim으로 RSS 회수
            if (i_last + 1) % 50 < BATCH_SIZE:
                _force_free_memory()
                logger.info(f"[잡워커] 메모리 회수 ({i_last + 1}/{total}건)")

            # 잡 progress 업데이트 (배치 완료 후)
            try:
                await repo.update_progress(job.id, i_last + 1, total)
                _job = await repo.get_async(job.id)
                if _job:
                    _job.result = {
                        "success": success_count,
                        "skipped": skip_count,
                        "failed": fail_count,
                    }
                await session.commit()
            except Exception as pg_err:
                logger.error(f"[잡워커] progress 업데이트 실패: {job.id} — {pg_err}")
                _add_job_log(
                    job.id,
                    f"[{i_last + 1}/{total}] DB 세션 오류 — 다음 건 계속 진행",
                )
                try:
                    await session.rollback()
                except Exception as exc:
                    logger.warning(f"[잡워커] 세션 롤백 실패: {job.id} — {exc}")

        final_fail = fail_count
        _add_job_log(
            job.id,
            f"전송 완료 — 성공 {success_count}건, 스킵 {skip_count}건, 실패 {final_fail}건",
        )
        await repo.complete_job(
            job.id,
            {"success": success_count, "skipped": skip_count, "failed": final_fail},
        )
        logger.info(
            f"[잡워커] 전송 완료: {job.id} (성공 {success_count}, 스킵 {skip_count}, 실패 {final_fail}/{total}건)"
        )

    async def _run_collect(self, job, repo, session):
        """수집 잡 실행 — collector_collection의 _stream_musinsa 로직 이식."""
        from urllib.parse import urlparse, parse_qs
        from sqlmodel import select, func as _func
        from backend.domain.samba.collector.model import SambaSearchFilter
        from backend.domain.samba.collector.model import (
            SambaCollectedProduct as CPModel,
        )
        from backend.domain.samba.proxy.musinsa import MusinsaClient, RateLimitError
        from backend.api.v1.routers.samba.collector_common import _build_product_data
        from backend.domain.samba.collector.refresher import (
            _site_intervals,
            _site_consecutive_errors,
            get_interval_key,
        )

        _ik = get_interval_key("MUSINSA", "collect")  # 수집 전용 인터벌 키

        payload = job.payload or {}

        # 브랜드 전체수집 모드 분기
        if payload.get("brand_all"):
            _ba_site = payload.get("source_site", "MUSINSA")
            if _ba_site == "ABCmart":
                await self._run_brand_collect_all_abc(job, repo, session)
            elif _ba_site == "SSG":
                await self._run_brand_collect_all_ssg(job, repo, session)
            elif _ba_site == "GSShop":
                await self._run_brand_collect_all_gs(job, repo, session)
            else:
                await self._run_brand_collect_all(job, repo, session)
            return

        filter_id = payload.get("filter_id")
        if not filter_id:
            await repo.fail_job(job.id, "filter_id 없음")
            return

        # 필터 조회
        sf = await session.get(SambaSearchFilter, filter_id)
        if not sf:
            await repo.fail_job(job.id, f"필터 없음: {filter_id}")
            return

        site = sf.source_site
        _gi = payload.get("group_index")
        _gt = payload.get("group_total")
        _prefix = f"({_gi}/{_gt})" if _gi and _gt else f"[{site}]"
        _add_job_log(job.id, f"{_prefix} [{sf.name}] 수집 시작", job_type="collect")

        # 직접 API 소싱처 (서버 HTTP)
        DIRECT_API_SITES = {
            "FashionPlus",
            "Nike",
            "Adidas",
            "LOTTEON",
            "SSG",
            "NAVERSTORE",
            "SNKRDUNK",
        }
        # 확장앱 기반 소싱처 (소싱큐)
        EXTENSION_SITES = {
            "ABCmart",
            "GrandStage",
            "REXMONDE",
            "GSShop",
            "ElandMall",
            "SSF",
        }

        # 타임아웃은 스레드 래퍼의 진행 기반 체크(_collect_last_progress)가 담당
        if site in DIRECT_API_SITES:
            await self._collect_direct_api(job, sf, session, repo)
            return

        if site in EXTENSION_SITES:
            await self._collect_direct_api(job, sf, session, repo)
            return

        if site != "MUSINSA":
            await repo.fail_job(job.id, f"미지원 소싱처: {site}")
            return

        # 쿠키 로드 — 암호화 저장값 자동 복호화 헬퍼 사용
        from backend.api.v1.routers.samba.collector_common import (
            get_musinsa_cookie as _get_musinsa_cookie,
        )

        cookie = await _get_musinsa_cookie(session)
        if not cookie:
            await repo.fail_job(job.id, "무신사 로그인(쿠키) 필요")
            return

        # 수집용 프록시 적용 — DB 설정 페이지(/samba/settings)에 등록된 collect 프록시만 사용
        from backend.domain.samba.collector.refresher import get_collect_proxy_url

        _collect_proxy = get_collect_proxy_url()
        client = MusinsaClient(cookie=cookie, proxy_url=_collect_proxy)
        if _collect_proxy:
            logger.info(
                f"[잡워커] 수집 프록시: {_collect_proxy.split('@')[-1] if '@' in _collect_proxy else 'on'}"
            )

        # 키워드/옵션 추출
        keyword_or_url = sf.keyword or ""
        keyword = keyword_or_url
        _exclude_preorder = False
        _exclude_boutique = False
        _use_max_discount = False
        _include_sold_out = False

        _brand_filter = ""
        _min_price = None
        _max_price = None
        _gf_filter = "A"
        _category_filter = ""

        try:
            parsed = urlparse(keyword_or_url)
            if parsed.scheme:
                qs = parse_qs(parsed.query)
                keyword = qs.get("keyword", [keyword])[0]
                _exclude_preorder = qs.get("excludePreorder", [""])[0] == "1"
                _exclude_boutique = qs.get("excludeBoutique", [""])[0] == "1"
                _use_max_discount = qs.get("maxDiscount", [""])[0] == "1"
                _include_sold_out = qs.get("includeSoldOut", [""])[0] == "1"
                _brand_filter = qs.get("brand", [""])[0]
                _min_price_raw = qs.get("minPrice", [""])[0]
                _max_price_raw = qs.get("maxPrice", [""])[0]
                _gf_filter = qs.get("gf", ["A"])[0]
                _category_filter = qs.get("category", [""])[0]
                _min_price = int(_min_price_raw) if _min_price_raw.isdigit() else None
                _max_price = int(_max_price_raw) if _max_price_raw.isdigit() else None
        except Exception as exc:
            logger.warning(f"[잡워커] 검색 URL 파싱 실패: {exc}")

        # 1상품 직접 URL 감지 (/products/{goods_no} 패턴 — collect_single_musinsa에서 생성)
        _product_url_match = re.search(r"/products/(\d+)", keyword_or_url)
        if _product_url_match:
            _direct_goods_no = _product_url_match.group(1)
            _add_job_log(
                job.id,
                f"{_prefix} [{sf.name}] 상품 직접 URL 감지 → goods_no={_direct_goods_no}",
                job_type="collect",
            )
            # 이미 수집된 상품 체크
            _existing_direct_count = (
                await session.execute(
                    select(_func.count()).where(
                        CPModel.search_filter_id == filter_id,
                        CPModel.site_product_id == _direct_goods_no,
                    )
                )
            ).scalar() or 0
            if _existing_direct_count > 0:
                _add_job_log(
                    job.id,
                    f"{_prefix} [{sf.name}] 수집 완료: 이미 수집됨 (신규 0건)",
                    job_type="collect",
                )
                await repo.complete_job(job.id, "이미 수집됨")
                return
            # 상품 상세 API 직접 호출
            try:
                _direct_detail = await client.get_goods_detail(_direct_goods_no)
            except Exception as _de:
                await repo.fail_job(job.id, f"상품 상세 조회 실패: {_de}")
                return
            if not _direct_detail or not _direct_detail.get("name"):
                await repo.fail_job(job.id, "상품 상세 조회 실패: 데이터 없음")
                return
            # 상품 저장
            from backend.api.v1.routers.samba.collector_common import (
                _get_services as _get_services_direct,
                _build_product_data as _build_product_data_direct,
            )

            _d_svc = _get_services_direct(session)
            _d_raw_cat = _direct_detail.get("category", "") or ""
            _d_cat_parts = (
                [c.strip() for c in _d_raw_cat.split(">") if c.strip()]
                if _d_raw_cat
                else []
            )
            _d_sale = _direct_detail.get("salePrice", 0)
            _d_orig = _direct_detail.get("originalPrice", 0)
            _d_cost = _direct_detail.get("bestBenefitPrice") or _d_sale
            _d_raw_html = _direct_detail.get("detailHtml", "")
            if not _d_raw_html:
                _d_dimgs = _direct_detail.get("detailImages") or []
                if _d_dimgs:
                    _d_raw_html = "\n".join(
                        f'<div style="text-align:center;"><img src="{img}" style="max-width:860px;width:100%;" /></div>'
                        for img in _d_dimgs
                    )
            _d_pdata = _build_product_data_direct(
                _direct_detail,
                _direct_goods_no,
                filter_id,
                "MUSINSA",
                _d_cost,
                _d_sale,
                _d_orig,
                _d_raw_cat,
                _d_cat_parts,
                _d_raw_html,
            )
            await _d_svc.create_collected_product(_d_pdata)
            # SearchFilter last_collected_at 갱신
            from sqlalchemy import update as _sa_upd_direct

            await session.execute(
                _sa_upd_direct(SambaSearchFilter)
                .where(SambaSearchFilter.id == filter_id)
                .values(last_collected_at=datetime.now(UTC))
            )
            await session.commit()
            _add_job_log(
                job.id,
                f"{_prefix} [{sf.name}] 수집 완료: 신규 1건",
                job_type="collect",
            )
            await repo.complete_job(job.id, "수집 완료: 신규 1건")
            return

        # LOTTEON 서브키워드 모드 감지: q="{브랜드} {카테고리}"면 qapi total 기준 전수 수집
        # (스캔 단계의 샘플 분포 count로 requested_count가 작게 잡혀도 cap에 걸리지 않도록)
        # 수집 완료 시점에 실제 수집수로 requested_count가 자동 갱신되어 이후엔 정확해짐.
        _lotteon_subkw_mode = False
        if sf.source_site == "LOTTEON":
            try:
                _subkw_q = parse_qs(urlparse(sf.keyword or "").query).get("q", [""])[0]
                if _subkw_q and " " in _subkw_q:
                    _lotteon_subkw_mode = True
            except Exception:
                pass

        # 기존 수집 수 확인 — sf.requested_count(사용자 수정값)가 있으면 우선, 없으면 기본 1000
        requested_count = sf.requested_count or FIXED_REQUESTED_COUNT
        count_stmt = select(_func.count()).where(CPModel.search_filter_id == filter_id)
        existing_count = (await session.execute(count_stmt)).scalar() or 0
        remaining = (
            99999 if _lotteon_subkw_mode else max(0, requested_count - existing_count)
        )

        if remaining <= 0:
            _add_job_log(
                job.id,
                f"{_prefix} 이미 {existing_count}개 수집됨 (요청: {requested_count}개)",
                job_type="collect",
            )
            await repo.complete_job(
                job.id,
                {
                    "saved": 0,
                    "message": f"이미 {existing_count}개 수집됨 (요청: {requested_count}개)",
                },
            )
            return

        _add_job_log(
            job.id,
            f"{_prefix} [{sf.name}] 잔여 {remaining}건 수집 시작 (기존 {existing_count}건)",
            job_type="collect",
        )
        await repo.update_progress(job.id, existing_count, requested_count)

        # 수집 루프
        total_saved = 0
        total_skipped = 0
        search_page = 1
        empty_pages = 0  # 연속 신규 0건 페이지 카운터 (잡 간 오염 방지용 로컬 변수)
        max_pages = 100  # API totalPages 기반으로 동적 조정 (초기값)
        _collected_sold_out = 0

        while total_saved < remaining and search_page <= max_pages:
            # 취소 확인 — 인메모리 플래그 우선(빠름), DB 조회는 최후(멀티인스턴스 대비)
            from backend.domain.samba.emergency import (
                clear_collect_cancel,
                is_collect_cancel_requested,
                is_emergency_stopped,
            )

            if (
                is_collect_cancel_requested()
                or is_emergency_stopped()
                or await repo.is_cancelled(job.id)
            ):
                logger.info(f"[잡워커] 수집 취소됨: {job.id}")
                # DB 상태 확실히 CANCELLED — stuck recovery 재시작 방지
                try:
                    await repo.cancel_job(job.id)
                    await session.commit()
                except Exception as _e:
                    logger.warning(f"[잡워커] 취소 상태 저장 실패: {job.id} — {_e}")
                _add_job_log(job.id, "수집 취소됨", job_type="collect")
                clear_collect_cancel()  # 다음 수집을 위해 해제
                return

            # 검색
            try:
                data = await client.search_products(
                    keyword=keyword,
                    page=search_page,
                    size=100,
                    category=_category_filter,
                    brand=_brand_filter,
                    min_price=_min_price,
                    max_price=_max_price,
                    gf=_gf_filter,
                )
                search_items = data.get("data", [])
                # 첫 페이지에서 totalPages로 최대 페이지 동적 설정
                if search_page == 1:
                    api_total_pages = data.get("totalPages", 0)
                    api_total_count = data.get("totalCount", 0)
                    if api_total_pages > 0:
                        max_pages = api_total_pages
                    else:
                        logger.warning(
                            f"[잡워커] totalPages={api_total_pages}, totalCount={api_total_count} → 초기값({max_pages}) 유지"
                        )
                    logger.info(
                        f"[잡워커] API 총 {api_total_count}건, {api_total_pages}페이지 → max_pages={max_pages}"
                    )
                    _add_job_log(
                        job.id,
                        f"{_prefix} [{sf.name}] API totalCount={api_total_count}건, totalPages={api_total_pages} → max_pages={max_pages}",
                        job_type="collect",
                    )
                logger.info(
                    f"[잡워커] 검색 p{search_page}: {len(search_items)}건 (kw={keyword}, brand={_brand_filter})"
                )
                if not search_items:
                    break
                await asyncio.sleep(_site_intervals.get(_ik, 0))
            except Exception as e:
                logger.error(f"[잡워커] 검색 실패: {e}")
                break

            # 중복 필터링 (전역 기준 — unique constraint와 동일한 범위)
            candidate_ids = [
                str(item.get("siteProductId", item.get("goodsNo", "")))
                for item in search_items
            ]
            existing_result = await session.execute(
                select(CPModel.site_product_id).where(
                    CPModel.source_site == site,
                    CPModel.site_product_id.in_(candidate_ids),
                )
            )
            existing_ids = {row[0] for row in existing_result.all()}

            targets = []
            for item in search_items:
                if total_saved + len(targets) >= remaining:
                    break
                site_pid = str(item.get("siteProductId", item.get("goodsNo", "")))
                if site_pid in existing_ids:
                    continue
                # 품절 판단은 상세 수집 단계에서 정확하게 수행 (검색 API의 isSoldOut은 부정확)
                targets.append(site_pid)

            logger.info(
                f"[잡워커] 중복={len(existing_ids)}, 타겟={len(targets)}, 스킵={total_skipped}"
            )
            if not targets:
                # 중복만 있는 페이지 — 다른 그룹이 먼저 수집했을 수 있으므로
                # max_pages까지 계속 탐색 (조기 종료 없음)
                empty_pages += 1
                logger.info(
                    f"[잡워커] p{search_page}: 신규 0건 (중복 {len(existing_ids)}건) — 계속 탐색"
                )
                _add_job_log(
                    job.id,
                    f"{_prefix} [{sf.name}] p{search_page}: 중복 {len(existing_ids)}건, 다음 페이지 탐색",
                    job_type="collect",
                )
                search_page += 1
                continue
            empty_pages = 0  # 신규 상품 발견 시 카운터 리셋

            # 상세 수집 (병렬 — SITE_CONCURRENCY + 공유 HTTP 클라이언트)
            from backend.domain.samba.collector.refresher import SITE_CONCURRENCY
            import httpx as _httpx

            _collect_sem = asyncio.Semaphore(SITE_CONCURRENCY.get("MUSINSA", 5))
            _collect_results: list[dict | None] = []
            _rate_limited = False
            _shared_http = _httpx.AsyncClient(timeout=_httpx.Timeout(30, connect=5.0))

            async def _fetch_detail(goods_no: str) -> dict | None:
                nonlocal total_skipped, _rate_limited, _collected_sold_out
                if _rate_limited:
                    return None
                async with _collect_sem:
                    try:
                        detail = await client.get_goods_detail(
                            goods_no, _shared_client=_shared_http
                        )
                        if not detail or not detail.get("name"):
                            return None
                        _is_sold = detail.get("saleStatus") == "sold_out" or detail.get(
                            "isOutOfStock"
                        )
                        if _is_sold:
                            if not _include_sold_out:
                                total_skipped += 1
                                return None
                            _collected_sold_out += 1
                        if _exclude_preorder and detail.get("saleStatus") == "preorder":
                            total_skipped += 1
                            return None
                        if _exclude_boutique and detail.get("isBoutique"):
                            total_skipped += 1
                            return None
                        return {"goods_no": goods_no, "detail": detail}
                    except RateLimitError as rle:
                        current = _site_intervals.get(_ik, 1.0)
                        _site_intervals[_ik] = min(30.0, current * 2)
                        _site_consecutive_errors[_ik] = (
                            _site_consecutive_errors.get("MUSINSA", 0) + 1
                        )
                        if _site_consecutive_errors[_ik] >= 5:
                            _rate_limited = True
                        if rle.retry_after > 0:
                            if await _cancellable_sleep(rle.retry_after):
                                return None
                        return None
                    except Exception as e:
                        logger.warning(
                            f"[잡워커] 수집 실패 {goods_no}: {type(e).__name__}: {e}"
                        )
                        return None

            _collect_results = await asyncio.gather(
                *[_fetch_detail(gn) for gn in targets]
            )
            await _shared_http.aclose()

            if _rate_limited:
                await repo.fail_job(job.id, "소싱처 차단 (연속 rate limit)")
                return

            # 수집된 상세 순차 저장 (DB 쓰기는 순차)
            from backend.api.v1.routers.samba.collector_common import _get_services

            svc = _get_services(session)
            for item in _collect_results:
                if item is None:
                    continue
                goods_no = item["goods_no"]
                detail = item["detail"]

                if _use_max_discount:
                    _raw_cost = detail.get("bestBenefitPrice")
                    new_cost = (
                        _raw_cost
                        if (_raw_cost is not None and _raw_cost > 0)
                        else (detail.get("salePrice") or 0)
                    )
                else:
                    new_cost = detail.get("salePrice") or 0

                raw_cat = detail.get("category", "") or ""
                cat_parts = (
                    [c.strip() for c in raw_cat.split(">") if c.strip()]
                    if raw_cat
                    else []
                )
                _sale_price = detail.get("salePrice", 0)
                _original_price = detail.get("originalPrice", 0)

                raw_detail_html = detail.get("detailHtml", "")
                if not raw_detail_html:
                    detail_imgs = detail.get("detailImages") or []
                    if detail_imgs:
                        raw_detail_html = "\n".join(
                            f'<div style="text-align:center;"><img src="{img}" style="max-width:860px;width:100%;" /></div>'
                            for img in detail_imgs
                        )

                product_data = _build_product_data(
                    detail,
                    goods_no,
                    filter_id,
                    "MUSINSA",
                    new_cost,
                    _sale_price,
                    _original_price,
                    raw_cat,
                    cat_parts,
                    raw_detail_html,
                )
                await svc.create_collected_product(product_data)
                total_saved += 1
                _collect_last_progress[job.id] = _time.time()  # 진행 갱신
                await repo.update_progress(
                    job.id, existing_count + total_saved, requested_count
                )
                _p_brand = detail.get("brand", "") or ""
                _p_name = detail.get("name", "") or ""
                _add_job_log(
                    job.id,
                    f"{_prefix} [{existing_count + total_saved:,}/{requested_count:,}] {_p_brand} {_p_name} {goods_no}",
                    job_type="collect",
                )

                if total_saved >= remaining:
                    break

            search_page += 1

        # 수집 완료 → last_collected_at 갱신 + 요청수를 실제 수집수로 보정
        from sqlalchemy import update as _sa_upd

        _actual = (
            await session.execute(
                select(_func.count()).where(CPModel.search_filter_id == filter_id)
            )
        ).scalar() or 0
        _upd_vals: dict = {"last_collected_at": datetime.now(UTC)}
        # requested_count는 실제 수집수가 더 클 때만 갱신 (축소 방지)
        if _actual > requested_count:
            _upd_vals["requested_count"] = _actual
            logger.info(f"[잡워커] requested_count 갱신: {requested_count} → {_actual}")
        elif _actual < requested_count:
            logger.info(
                f"[잡워커] 실제 {_actual}건 < 요청 {requested_count}건 (축소 방지로 유지)"
            )
        await session.execute(
            _sa_upd(SambaSearchFilter)
            .where(SambaSearchFilter.id == filter_id)
            .values(**_upd_vals)
        )

        # 정책 자동 적용
        policy_msg = ""
        if sf.applied_policy_id and total_saved > 0:
            try:
                from backend.domain.samba.policy.repository import SambaPolicyRepository
                from backend.api.v1.routers.samba.collector_common import _get_services

                svc = _get_services(session)
                policy_repo = SambaPolicyRepository(session)
                policy = await policy_repo.get_async(sf.applied_policy_id)
                policy_data = None
                if policy and policy.pricing:
                    pr = policy.pricing if isinstance(policy.pricing, dict) else {}
                    policy_data = {
                        "margin_rate": pr.get("marginRate", 15),
                        "shipping_cost": pr.get("shippingCost", 0),
                        "extra_charge": pr.get("extraCharge", 0),
                        "use_range_margin": pr.get("useRangeMargin", False),
                        "range_margins": pr.get("rangeMargins", []),
                        "source_site_margins": pr.get("sourceSiteMargins", {}),
                    }
                count = await svc.apply_policy_to_filter_products(
                    filter_id, sf.applied_policy_id, policy_data
                )
                policy_msg = f"정책 적용: {count}개"
            except Exception as e:
                logger.error(f"[잡워커] 정책 전파 실패: {e}")

        _in_stock = total_saved - _collected_sold_out
        _parts = [f"신규 {total_saved}건"]
        if _in_stock > 0 or _collected_sold_out > 0:
            _parts.append(f"재고 {_in_stock}건 | 품절 {_collected_sold_out}건")
        if total_skipped > 0:
            _parts.append(f"중복/스킵 {total_skipped}건")
        if policy_msg:
            _parts.append(policy_msg)
        _add_job_log(
            job.id,
            f"{_prefix} [{sf.name}] 수집 완료: {' | '.join(_parts)}",
            job_type="collect",
        )

        await repo.complete_job(
            job.id,
            {
                "saved": total_saved,
                "skipped": total_skipped,
                "policy": policy_msg,
                "in_stock_count": _in_stock,
                "sold_out_count": _collected_sold_out,
            },
        )
        logger.info(f"[잡워커] 수집 완료: {job.id} ({total_saved}건)")

    async def _run_brand_collect_all(self, job, repo, session):
        """무신사 브랜드 전체 상품 수집 후 카테고리별 SearchFilter 배분.

        기존 카테고리별 순차 수집의 두 문제 해결:
        - 페이지 이탈 = 수집 중단 → 단일 백엔드 Job으로 완전 독립
        - 글로벌 dedup 누락 → 상품당 1개 filter에만 저장, 중복 없음
        """
        import random as _random
        from urllib.parse import urlparse, parse_qs
        from sqlmodel import select, func as _func
        from backend.domain.samba.collector.model import SambaSearchFilter
        from backend.domain.samba.collector.model import (
            SambaCollectedProduct as CPModel,
        )
        from backend.domain.samba.proxy.musinsa import MusinsaClient, RateLimitError
        from backend.api.v1.routers.samba.collector_common import (
            _build_product_data,
            _get_services,
        )
        from backend.domain.samba.collector.refresher import (
            _site_intervals,
            _site_consecutive_errors,
            get_interval_key,
        )
        import httpx as _httpx
        from sqlalchemy import update as _sa_upd

        _ik = get_interval_key("MUSINSA", "collect")
        payload = job.payload or {}
        filter_ids: list[str] = payload.get("filter_ids", [])
        keyword: str = payload.get("keyword", "")
        brand: str = payload.get("brand", "")
        gf: str = payload.get("gf", "A")
        _exclude_preorder: bool = payload.get("exclude_preorder", True)
        _exclude_boutique: bool = payload.get("exclude_boutique", True)
        _use_max_discount: bool = payload.get("use_max_discount", False)
        _include_sold_out: bool = payload.get("include_sold_out", False)

        if not filter_ids or not keyword or not brand:
            await repo.fail_job(job.id, "brand_all: filter_ids/keyword/brand 필요")
            return

        _add_job_log(
            job.id,
            f"[브랜드전체수집] '{keyword}' 시작 — {len(filter_ids):,}개 그룹 대상",
            job_type="collect",
        )

        # 쿠키 로드 — 암호화 저장값 자동 복호화 헬퍼 사용
        from backend.api.v1.routers.samba.collector_common import (
            get_musinsa_cookie as _get_musinsa_cookie,
        )

        cookie = await _get_musinsa_cookie(session)
        if not cookie:
            await repo.fail_job(job.id, "무신사 로그인(쿠키) 필요")
            return

        from backend.domain.samba.collector.refresher import get_collect_proxy_url

        _collect_proxy = get_collect_proxy_url()
        client = MusinsaClient(cookie=cookie, proxy_url=_collect_proxy)

        # SearchFilter 목록 로드 + category_code → filter_id 맵 빌드
        filters_result = await session.execute(
            select(SambaSearchFilter).where(SambaSearchFilter.id.in_(filter_ids))
        )
        filters: list[SambaSearchFilter] = list(filters_result.scalars().all())

        cat_filter_map: dict[str, str] = {}  # {category_code: filter_id}
        cat_name_map: dict[
            str, str
        ] = {}  # {category_path: filter_id} — name 기반 fallback
        for f in filters:
            if f.keyword:
                try:
                    _qs = parse_qs(urlparse(f.keyword).query)
                    cat = _qs.get("category", [""])[0]
                    if cat:
                        cat_filter_map[cat] = f.id
                except Exception:
                    pass
            # f.name = "MUSINSA_브랜드_대분류_중분류_소분류" → "대분류 > 중분류 > 소분류"
            if f.name:
                _nm_parts = f.name.split("_")
                if len(_nm_parts) > 2:
                    cat_name_map[" > ".join(_nm_parts[2:])] = f.id

        _add_job_log(
            job.id,
            f"[브랜드전체수집] 카테고리 맵 {len(cat_filter_map):,}개 구성",
            job_type="collect",
        )

        # 이미 수집된 site_product_id 전체 로드 (dedup용)
        existing_result = await session.execute(
            select(CPModel.site_product_id).where(
                CPModel.source_site == "MUSINSA",
                CPModel.search_filter_id.in_(filter_ids),
            )
        )
        existing_ids: set[str] = {row[0] for row in existing_result.all()}

        # 브랜드 전체 검색 (카테고리 필터 없음)
        total_saved = 0
        total_skipped = 0
        total_unmatched = 0
        _collected_sold_out = 0
        _total_count = 0  # 전체 건수 (1페이지 응답에서 채워짐)
        search_page = 1
        max_pages = 100
        _rate_limited = False
        svc = _get_services(session)

        # 검색+상세수집 인터리빙 — 페이지마다 상세수집 후 다음 검색
        # 병렬도 5 고정 (오토튠 검증선과 동일 — 안전 우선)
        _collect_sem = asyncio.Semaphore(5)
        _shared_http = _httpx.AsyncClient(timeout=_httpx.Timeout(30, connect=5.0))

        async def _fetch_detail_brand(goods_no: str) -> dict | None:
            nonlocal total_skipped, _rate_limited, _collected_sold_out
            if _rate_limited:
                return None
            async with _collect_sem:
                try:
                    detail = await client.get_goods_detail(
                        goods_no, _shared_client=_shared_http
                    )
                    if not detail or not detail.get("name"):
                        return None
                    _is_sold = detail.get("saleStatus") == "sold_out" or detail.get(
                        "isOutOfStock"
                    )
                    if _is_sold:
                        if not _include_sold_out:
                            total_skipped += 1
                            return None
                        _collected_sold_out += 1
                    if _exclude_preorder and detail.get("saleStatus") == "preorder":
                        total_skipped += 1
                        return None
                    if _exclude_boutique and detail.get("isBoutique"):
                        total_skipped += 1
                        return None
                    return {"goods_no": goods_no, "detail": detail}
                except RateLimitError as rle:
                    current = _site_intervals.get(_ik, 1.0)
                    _site_intervals[_ik] = min(30.0, current * 2)
                    _site_consecutive_errors[_ik] = (
                        _site_consecutive_errors.get("MUSINSA", 0) + 1
                    )
                    if _site_consecutive_errors[_ik] >= 5:
                        _rate_limited = True
                    if rle.retry_after > 0:
                        if await _cancellable_sleep(rle.retry_after):
                            return None
                    return None
                except Exception as e:
                    logger.warning(f"[잡워커] 브랜드전체수집 상세 실패 {goods_no}: {e}")
                    return None

        while search_page <= max_pages:
            from backend.domain.samba.emergency import (
                is_collect_cancel_requested,
                is_emergency_stopped,
                clear_collect_cancel,
            )

            if (
                is_collect_cancel_requested()
                or is_emergency_stopped()
                or await repo.is_cancelled(job.id)
            ):
                _add_job_log(job.id, "[브랜드전체수집] 수집 취소됨", job_type="collect")
                try:
                    await repo.cancel_job(job.id)
                    await session.commit()
                except Exception:
                    pass
                clear_collect_cancel()
                await _shared_http.aclose()
                return

            # 검색 요청 — 최대 3회 재시도
            search_items = []
            _page_fail = False
            for _retry in range(4):
                try:
                    data = await client.search_products(
                        keyword=keyword,
                        page=search_page,
                        size=100,
                        brand=brand,
                        gf=gf,
                    )
                    search_items = data.get("data", [])
                    if search_page == 1:
                        max_pages = data.get("totalPages", 1) or 1
                        _total_count = data.get("totalCount", 0) or 0
                        _add_job_log(
                            job.id,
                            f"[브랜드전체수집] 총 {_total_count:,}건 / {max_pages}페이지",
                            job_type="collect",
                        )
                        await repo.update_progress(
                            job.id,
                            0,
                            data.get("totalCount", 0) or len(filter_ids) * 100,
                        )
                    break  # 성공
                except Exception as e:
                    logger.error(
                        f"[잡워커] 브랜드전체수집 검색 실패 p{search_page} (재시도 {_retry + 1}/3): {e!r}",
                        exc_info=True,
                    )
                    if _retry >= 3:
                        _page_fail = True
                        break
                    await asyncio.sleep(5 * (_retry + 1))

            if _page_fail:
                _add_job_log(
                    job.id,
                    f"[브랜드전체수집] p{search_page} 재시도 초과, 검색 중단",
                    job_type="collect",
                )
                break

            if not search_items:
                if search_page == 1:
                    break
                search_page += 1
                continue

            # goodsNo 추출 + dedup
            _page_targets = []
            for item in search_items:
                spid = str(item.get("siteProductId", item.get("goodsNo", "")))
                if spid and spid not in existing_ids:
                    _page_targets.append(spid)

            if not _page_targets:
                await asyncio.sleep(1.0)
                search_page += 1
                continue

            # 이 페이지 상세수집 — as_completed로 완료 순서대로 즉시 저장
            tasks = [
                asyncio.create_task(_fetch_detail_brand(gn)) for gn in _page_targets
            ]
            for _fut in asyncio.as_completed(tasks):
                item = await _fut
                if item is None:
                    continue
                goods_no = item["goods_no"]
                detail = item["detail"]

                cat_code = detail.get("categoryCode", "")
                filter_id = cat_filter_map.get(cat_code)
                # 1차 fallback: Depth 코드
                if not filter_id:
                    _cat_raw = detail.get("category_raw") or {}
                    for _depth in [
                        "categoryDepth3Code",
                        "categoryDepth2Code",
                        "categoryDepth1Code",
                    ]:
                        _c = _cat_raw.get(_depth, "")
                        if _c and _c in cat_filter_map:
                            filter_id = cat_filter_map[_c]
                            break
                # 2차 fallback: Depth Name 경로 깊이별 매칭
                _cat_raw = detail.get("category_raw") or {}
                _name_parts = [
                    (_cat_raw.get("categoryDepth1Name") or "").strip(),
                    (_cat_raw.get("categoryDepth2Name") or "").strip(),
                    (_cat_raw.get("categoryDepth3Name") or "").strip(),
                    (_cat_raw.get("categoryDepth4Name") or "").strip(),
                ]
                _name_parts = [p for p in _name_parts if p]
                if not _name_parts:
                    _raw_cat_str = detail.get("category", "") or ""
                    _name_parts = [
                        p.strip() for p in _raw_cat_str.split(">") if p.strip()
                    ]
                if not filter_id:
                    for _d in range(len(_name_parts), 0, -1):
                        _sub = " > ".join(_name_parts[:_d])
                        filter_id = cat_filter_map.get(_sub) or cat_name_map.get(_sub)
                        if filter_id:
                            break

                # 3차 fallback: 자동 카테고리 filter 생성 (미매핑 0 보장)
                if not filter_id and filters and _name_parts:
                    _parent = filters[0]
                    _brand_nm = _parent.source_brand_name or keyword
                    _cat_path_str = " > ".join(_name_parts)
                    # 플레이오토 MyCateName은 '/'가 트리 구분자 — 필터명에 '/' 금지
                    _new_name = (
                        f"MUSINSA_{_brand_nm}_" + "_".join(_name_parts)
                    ).replace("/", "_")
                    # keyword URL: 기존 filter에서 category param만 교체
                    _new_keyword = None
                    try:
                        _parsed = urlparse(_parent.keyword or "")
                        _q = parse_qs(_parsed.query)
                        if cat_code:
                            _q["category"] = [cat_code]
                        _q_str = "&".join(f"{k}={v[0]}" for k, v in _q.items() if v)
                        _new_keyword = f"{_parsed.scheme}://{_parsed.netloc}{_parsed.path}?{_q_str}"
                    except Exception:
                        _new_keyword = _parent.keyword

                    _new_filter = SambaSearchFilter(
                        source_site="MUSINSA",
                        name=_new_name,
                        parent_id=_parent.parent_id,
                        tenant_id=_parent.tenant_id,
                        keyword=_new_keyword,
                        source_brand_name=_brand_nm,
                        requested_count=0,
                    )
                    session.add(_new_filter)
                    await session.flush()
                    if cat_code:
                        cat_filter_map[cat_code] = _new_filter.id
                    cat_name_map[_cat_path_str] = _new_filter.id
                    filter_id = _new_filter.id
                    filters.append(_new_filter)
                    _add_job_log(
                        job.id,
                        f"[자동생성] 신규 카테고리: {_cat_path_str} (code={cat_code})",
                        job_type="collect",
                    )

                if not filter_id:
                    total_unmatched += 1
                    _p_name = (detail.get("name") or "")[:20]
                    _cat_str = detail.get("category", "") or cat_code
                    _add_job_log(
                        job.id,
                        f"[미매핑] {_p_name} ({goods_no}) cat={_cat_str[:40]}",
                        job_type="collect",
                    )
                    continue

                if _use_max_discount:
                    _raw_cost = detail.get("bestBenefitPrice")
                    new_cost = (
                        _raw_cost
                        if (_raw_cost is not None and _raw_cost > 0)
                        else (detail.get("salePrice") or 0)
                    )
                else:
                    new_cost = detail.get("salePrice") or 0

                raw_cat = detail.get("category", "") or ""
                cat_parts = (
                    [c.strip() for c in raw_cat.split(">") if c.strip()]
                    if raw_cat
                    else []
                )
                _sale_price = detail.get("salePrice", 0)
                _original_price = detail.get("originalPrice", 0)
                raw_detail_html = detail.get("detailHtml", "")
                if not raw_detail_html:
                    detail_imgs = detail.get("detailImages") or []
                    if detail_imgs:
                        raw_detail_html = "\n".join(
                            f'<div style="text-align:center;"><img src="{img}" style="max-width:860px;width:100%;" /></div>'
                            for img in detail_imgs
                        )

                product_data = _build_product_data(
                    detail,
                    goods_no,
                    filter_id,
                    "MUSINSA",
                    new_cost,
                    _sale_price,
                    _original_price,
                    raw_cat,
                    cat_parts,
                    raw_detail_html,
                )
                await svc.create_collected_product(product_data)
                existing_ids.add(goods_no)
                total_saved += 1
                _collect_last_progress[job.id] = _time.time()

                _m_brand = detail.get("brand", "") or ""
                _m_name = detail.get("name", "") or ""
                _m_style = detail.get("style_code", "") or ""
                _m_log = f"[{total_saved:,}/{_total_count:,}] {_m_brand} {_m_name}"
                if _m_style:
                    _m_log += f" {_m_style}"
                _m_log += f" {goods_no}"
                _add_job_log(job.id, _m_log, job_type="collect")
                if total_saved % 10 == 0:
                    await repo.update_progress(job.id, total_saved, _total_count or 1)

            if _rate_limited:
                await _shared_http.aclose()
                await repo.fail_job(job.id, "소싱처 차단 (연속 rate limit)")
                return

            # 지터 0.3~0.8초 — 고정 인터벌 봇 지문 회피
            await asyncio.sleep(_random.uniform(0.3, 0.8))
            search_page += 1

        await _shared_http.aclose()

        # 각 SearchFilter의 requested_count를 실제 수집수로 갱신
        for f in filters:
            actual = (
                await session.execute(
                    select(_func.count()).where(CPModel.search_filter_id == f.id)
                )
            ).scalar() or 0
            await session.execute(
                _sa_upd(SambaSearchFilter)
                .where(SambaSearchFilter.id == f.id)
                .values(last_collected_at=datetime.now(UTC))
            )

        _add_job_log(
            job.id,
            f"[브랜드전체수집] 완료: 저장 {total_saved:,}건 | 품절스킵 {total_skipped:,}건 | 카테고리미매핑 {total_unmatched:,}건",
            job_type="collect",
        )
        await repo.complete_job(
            job.id,
            {
                "saved": total_saved,
                "skipped": total_skipped,
                "unmatched": total_unmatched,
                "in_stock_count": total_saved - _collected_sold_out,
                "sold_out_count": _collected_sold_out,
            },
        )
        logger.info(f"[잡워커] 브랜드전체수집 완료: {job.id} ({total_saved:,}건)")

    async def _run_brand_collect_all_abc(self, job, repo, session):
        """ABCmart+GrandStage 브랜드 전체 상품을 단일 Job으로 수집 후 카테고리별 배분.

        무신사(_run_brand_collect_all)와 동일 목적이나 ABCmart 전용 흐름:
        - cat_filter_map: sf.category_filter 직접 사용 (URL category 파라미터 아님)
        - 검색: ARTSourcingClient로 ABC+GS 병렬 전체 검색
        - 상세: 3건 병렬 배치 선취합
        - 배분: category_code → filter_id 매핑
        """
        from urllib.parse import parse_qs, urlparse
        from sqlalchemy import select, update as _sa_upd, func as _func
        from backend.domain.samba.collector.model import SambaSearchFilter
        from backend.domain.samba.collector.model import (
            SambaCollectedProduct as CPModel,
        )
        from backend.domain.samba.proxy.abcmart import ARTSourcingClient
        from backend.api.v1.routers.samba.collector_common import (
            _build_product_data,
            _get_services,
        )
        from datetime import datetime, timezone as _tz

        UTC = _tz.utc
        payload = job.payload or {}
        filter_ids: list[str] = payload.get("filter_ids", [])
        keyword: str = payload.get("keyword", "")
        _use_max_discount: bool = payload.get("use_max_discount", False)
        _include_sold_out: bool = payload.get("include_sold_out", False)

        if not filter_ids:
            await repo.fail_job(job.id, "brand_all_abc: filter_ids 필요")
            return

        _add_job_log(
            job.id,
            f"[ABC브랜드전체수집] '{keyword}' 시작 — {len(filter_ids):,}개 그룹 대상",
            job_type="collect",
        )

        # SearchFilter 로드 + category_filter → filter_id 맵 구성
        filters_result = await session.execute(
            select(SambaSearchFilter).where(SambaSearchFilter.id.in_(filter_ids))
        )
        filters: list[SambaSearchFilter] = list(filters_result.scalars().all())

        cat_filter_map: dict[str, str] = {}  # {category_code: filter_id}
        cat_name_map: dict[
            str, str
        ] = {}  # {category_path: filter_id} — 코드 불일치 fallback
        for f in filters:
            if f.category_filter:
                cat_filter_map[f.category_filter] = f.id
            # f.name = "ABCmart_아디다스_신발_스니커즈" → "신발 > 스니커즈"
            if f.name:
                _nm_parts = f.name.split("_")
                if len(_nm_parts) > 2:
                    cat_name_map[" > ".join(_nm_parts[2:])] = f.id

        # 자동생성 시 사용할 사이트 폴더 ID — 기존 leaf의 parent_id 우선,
        # 없으면 source_site 사이트 폴더(is_folder=true) 직접 조회
        _auto_parent_id: Optional[str] = None
        for _f in filters:
            if _f.parent_id:
                _auto_parent_id = _f.parent_id
                break
        if not _auto_parent_id:
            _site_folder_row = (
                await session.execute(
                    select(SambaSearchFilter.id)
                    .where(
                        SambaSearchFilter.source_site.in_(["ABCmart", "GrandStage"]),
                        SambaSearchFilter.is_folder == True,  # noqa: E712
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if _site_folder_row:
                _auto_parent_id = _site_folder_row

        # 자동생성 시 사용할 tenant_id — job.tenant_id 우선, 없으면 NOT NULL인 filter
        # (멀티테넌시 격리 화면에서 NULL tenant 행이 누락되는 문제 방지)
        _auto_tenant_id: Optional[str] = getattr(job, "tenant_id", None)
        if not _auto_tenant_id:
            for _f in filters:
                if _f.tenant_id:
                    _auto_tenant_id = _f.tenant_id
                    break

        if not cat_filter_map:
            await repo.fail_job(
                job.id,
                "brand_all_abc: category_filter가 없습니다 (그룹 스캔 후 다시 시도)",
            )
            return

        # 검색 키워드: 첫 번째 filter URL의 searchWord 파라미터
        _abc_kw = keyword
        if filters:
            try:
                _qs_kw = parse_qs(urlparse(filters[0].keyword or "").query)
                _abc_kw = _qs_kw.get("searchWord", [keyword])[0] or keyword
            except Exception:
                pass

        _add_job_log(
            job.id,
            f"[ABC브랜드전체수집] 카테고리 맵 {len(cat_filter_map)}개 | 키워드: '{_abc_kw}'",
            job_type="collect",
        )

        # ABC + GrandStage 병렬 전체 검색 (카테고리 필터 없이)
        _add_job_log(
            job.id,
            f"[ABC브랜드전체수집] '{_abc_kw}' ABC+GrandStage 병렬 검색 중...",
            job_type="collect",
        )
        abc_client = ARTSourcingClient(channel=None)
        gs_client = ARTSourcingClient(channel="10002")
        abc_res, gs_res = await asyncio.gather(
            abc_client.search(_abc_kw, max_count=9999),
            gs_client.search(_abc_kw, max_count=9999),
            return_exceptions=True,
        )

        # 중복 제거 병합 (ABC 우선)
        _seen_spids: set[str] = set()
        all_items: list[dict] = []
        for _res in [abc_res, gs_res]:
            if isinstance(_res, Exception):
                logger.warning(f"[잡워커] ABC브랜드전체수집 검색 실패: {_res}")
                continue
            for _it in _res.get("products", []):
                _spid = str(_it.get("site_product_id", ""))
                if _spid and _spid not in _seen_spids:
                    _seen_spids.add(_spid)
                    all_items.append(_it)

        _add_job_log(
            job.id,
            f"[ABC브랜드전체수집] {len(all_items):,}건 검색 완료 — 상세조회 시작",
            job_type="collect",
        )
        await repo.update_progress(job.id, 0, max(len(all_items), 1))

        # 이미 수집된 상품 제외
        existing_result = await session.execute(
            select(CPModel.site_product_id).where(
                CPModel.source_site.in_(["ABCmart", "GrandStage"]),
                CPModel.search_filter_id.in_(filter_ids),
            )
        )
        existing_ids: set[str] = {row[0] for row in existing_result.all()}

        new_items = [
            it
            for it in all_items
            if str(it.get("site_product_id", "")) not in existing_ids
        ]
        _add_job_log(
            job.id,
            f"[ABC브랜드전체수집] 신규 {len(new_items):,}건 (기존 {len(existing_ids):,}건 스킵)",
            job_type="collect",
        )

        # 3건 병렬 조회 → 배치 완료 즉시 1건씩 저장 → 건별 로그
        svc = _get_services(session)
        total_saved = 0
        total_skipped = 0
        total_unmatched = 0
        _ABC_BATCH = 5

        for _bs in range(0, len(new_items), _ABC_BATCH):
            from backend.domain.samba.emergency import (
                is_collect_cancel_requested,
                is_emergency_stopped,
            )

            if (
                is_collect_cancel_requested()
                or is_emergency_stopped()
                or await repo.is_cancelled(job.id)
            ):
                await repo.cancel_job(job.id)
                await session.commit()
                return

            _batch = new_items[_bs : _bs + _ABC_BATCH]
            _gs_client = ARTSourcingClient(channel="10002")
            _details = await asyncio.gather(
                *(
                    (
                        _gs_client
                        if it.get("source_site") == "GrandStage"
                        else abc_client
                    ).get_product_detail(str(it.get("site_product_id", "")))
                    for it in _batch
                ),
                return_exceptions=True,
            )

            # 배치 완료 즉시 1건씩 저장 + 로그
            for _bi, (it, det) in enumerate(zip(_batch, _details)):
                spid = str(it.get("site_product_id", ""))
                detail = det if (det and not isinstance(det, Exception)) else {}

                is_sold_out = bool(
                    detail.get("isOutOfStock") or it.get("is_sold_out", False)
                )
                if is_sold_out and not _include_sold_out:
                    total_skipped += 1
                    continue

                cat_code = (
                    it.get("category_code", "")
                    or detail.get("categoryCode", "")
                    or detail.get("category_code", "")
                )
                filter_id = cat_filter_map.get(cat_code)
                _item_cat = it.get("category", "") or detail.get("category", "")
                _parts = [p.strip() for p in _item_cat.split(">") if p.strip()]
                if not filter_id:
                    # 코드 불일치 → 카테고리 경로 깊이별 매칭
                    for _d in range(len(_parts), 0, -1):
                        _sub = " > ".join(_parts[:_d])
                        filter_id = cat_filter_map.get(_sub) or cat_name_map.get(_sub)
                        if filter_id:
                            break
                # 자동 카테고리 filter 생성 (미매핑 0 보장)
                if not filter_id and filters and _parts:
                    _parent = filters[0]
                    _brand_nm = _parent.source_brand_name or keyword
                    _cat_path_str = " > ".join(_parts)
                    # 플레이오토 MyCateName은 '/'가 트리 구분자 — 필터명에 '/' 금지
                    _new_name = (f"ABCmart_{_brand_nm}_" + "_".join(_parts)).replace(
                        "/", "_"
                    )
                    _new_filter = SambaSearchFilter(
                        source_site=_parent.source_site or "ABCmart",
                        name=_new_name,
                        parent_id=_parent.parent_id or _auto_parent_id,
                        tenant_id=_parent.tenant_id or _auto_tenant_id,
                        keyword=_parent.keyword,
                        category_filter=cat_code or None,
                        source_brand_name=_brand_nm,
                        requested_count=0,
                    )
                    session.add(_new_filter)
                    await session.flush()
                    if cat_code:
                        cat_filter_map[cat_code] = _new_filter.id
                    cat_name_map[_cat_path_str] = _new_filter.id
                    filter_id = _new_filter.id
                    filters.append(_new_filter)
                    _add_job_log(
                        job.id,
                        f"[자동생성] 신규 카테고리: {_cat_path_str} (code={cat_code})",
                        job_type="collect",
                    )
                if not filter_id:
                    total_unmatched += 1
                    _p_name = (detail.get("name") or it.get("name", ""))[:20]
                    _add_job_log(
                        job.id,
                        f"[미매핑] {_p_name} ({spid}) cat={_item_cat[:40] or cat_code}",
                        job_type="collect",
                    )
                    continue

                _sale_price = int(
                    detail.get("salePrice", 0) or it.get("sale_price", 0) or 0
                )
                _original_price = int(
                    detail.get("originalPrice", 0)
                    or it.get("original_price", 0)
                    or _sale_price
                )
                if _use_max_discount:
                    _bbp = int(detail.get("bestBenefitPrice", 0) or 0)
                    _cost = _bbp if _bbp > 0 else _sale_price
                else:
                    _cost = int(it.get("cost", 0) or _sale_price)

                _is_free_ship = it.get("free_shipping", False) or detail.get(
                    "freeShipping", False
                )
                if not _is_free_ship:
                    _cost += int(detail.get("shippingFee", 0) or 0)

                # 원가 수집 실패 시 100,000원 sentinel — 배송비만 남는 사고 방지
                if _cost <= 0:
                    _add_job_log(
                        job.id,
                        f"[원가수집실패] ABCmart spid={spid} → 100,000원 fallback 적용",
                        job_type="collect",
                    )
                    _cost = 100000

                raw_cat = detail.get("category", "") or it.get("category", "")
                cat_parts = [
                    it.get("category1", "") or "",
                    it.get("category2", "") or "",
                    it.get("category3", "") or "",
                ]
                cat_parts = [c for c in cat_parts if c]
                source_site = "ABCmart"  # GrandStage 상품도 ABCmart로 통합 저장

                detail_for_build: dict = {
                    "name": detail.get("name") or it.get("name", ""),
                    "brand": detail.get("brand") or it.get("brand", ""),
                    "images": (detail.get("images") or []) or it.get("images", []),
                    "detailImages": detail.get("detailImages") or [],
                    "options": detail.get("options") or [],
                    "sourceUrl": (
                        detail.get("sourceUrl")
                        or f"https://www.a-rt.com/product?prdtNo={spid}"
                    ),
                    "category": raw_cat,
                    "manufacturer": detail.get("manufacturer") or it.get("brand", ""),
                    "origin": detail.get("origin", ""),
                    "material": detail.get("material", ""),
                    "color": detail.get("color", ""),
                    "saleStatus": "sold_out" if is_sold_out else "in_stock",
                    "freeShipping": _is_free_ship,
                    "styleNo": detail.get("styleCode")
                    or detail.get("style_code")
                    or it.get("style_code", ""),
                }
                raw_detail_html = detail.get("detailHtml", "") or detail.get(
                    "detail_html", ""
                )

                product_data = _build_product_data(
                    detail_for_build,
                    spid,
                    filter_id,
                    source_site,
                    _cost,
                    _sale_price,
                    _original_price,
                    raw_cat,
                    cat_parts,
                    raw_detail_html,
                )
                await svc.create_collected_product(product_data)
                existing_ids.add(spid)
                total_saved += 1
                _collect_last_progress[job.id] = _time.time()

                _log_brand = (detail_for_build.get("brand") or "").strip()
                _log_name = (detail_for_build.get("name") or "").strip()
                _add_job_log(
                    job.id,
                    f"[{total_saved:,}/{len(new_items):,}] {_log_brand} {_log_name} {spid}",
                    job_type="collect",
                )

            _done = min(_bs + _ABC_BATCH, len(new_items))
            await repo.update_progress(job.id, _done, len(new_items))
            if _bs + _ABC_BATCH < len(new_items):
                await asyncio.sleep(0.5)

        # 각 SearchFilter의 requested_count를 실제 수집수로 갱신
        for f in filters:
            actual = (
                await session.execute(
                    select(_func.count()).where(CPModel.search_filter_id == f.id)
                )
            ).scalar() or 0
            await session.execute(
                _sa_upd(SambaSearchFilter)
                .where(SambaSearchFilter.id == f.id)
                .values(last_collected_at=datetime.now(UTC))
            )

        _add_job_log(
            job.id,
            f"[ABC브랜드전체수집] 완료: 저장 {total_saved:,}건 | 품절스킵 {total_skipped:,}건 | 카테고리미매핑 {total_unmatched:,}건",
            job_type="collect",
        )
        await repo.complete_job(
            job.id,
            {
                "saved": total_saved,
                "skipped": total_skipped,
                "unmatched": total_unmatched,
            },
        )
        logger.info(f"[잡워커] ABC브랜드전체수집 완료: {job.id} ({total_saved:,}건)")

    async def _run_brand_collect_all_ssg(self, job, repo, session):
        """SSG 브랜드 전체 상품을 단일 Job으로 수집 후 카테고리별 SearchFilter 배분.

        무신사 인터리빙 패턴 준용:
        - 검색 1페이지 → 즉시 상세조회+저장 → 검색 2페이지 → 즉시 상세조회+저장 ...
        - cat_filter_map: filter.category_filter (= dispCtgId) → filter_id
        - 상세: get_product_detail() → dispCtgId로 카테고리 배분
        """
        from urllib.parse import parse_qs, urlparse
        from sqlalchemy import select, update as _sa_upd, func as _func
        from backend.domain.samba.collector.model import SambaSearchFilter
        from backend.domain.samba.collector.model import (
            SambaCollectedProduct as CPModel,
        )
        from backend.domain.samba.proxy.ssg_sourcing import SSGSourcingClient
        from backend.domain.samba.proxy.ssg_sourcing import (
            RateLimitError as SSGSearchRL,
        )
        from backend.api.v1.routers.samba.collector_common import (
            _build_product_data,
            _get_services,
            _is_blacklisted,
        )
        from datetime import datetime, timezone as _tz

        UTC = _tz.utc
        payload = job.payload or {}
        filter_ids: list[str] = payload.get("filter_ids", [])
        keyword: str = payload.get("keyword", "")
        _use_max_discount: bool = payload.get("use_max_discount", False)
        _include_sold_out: bool = payload.get("include_sold_out", False)

        if not filter_ids:
            await repo.fail_job(job.id, "brand_all_ssg: filter_ids 필요")
            return

        _add_job_log(
            job.id,
            f"[SSG브랜드전체수집] '{keyword}' 시작 — {len(filter_ids):,}개 그룹 대상",
            job_type="collect",
        )

        # SearchFilter 로드 + category_filter → filter_id 맵 구성
        filters_result = await session.execute(
            select(SambaSearchFilter).where(SambaSearchFilter.id.in_(filter_ids))
        )
        filters: list[SambaSearchFilter] = list(filters_result.scalars().all())

        cat_filter_map: dict[str, str] = {}  # {dispCtgId: filter_id}
        cat_name_map: dict[str, str] = {}  # {category_path: filter_id} — fallback
        _brand_ids_from_filter: list[str] = []  # repBrandId 목록
        for f in filters:
            if f.category_filter:
                cat_filter_map[f.category_filter] = f.id
            # f.name = "SSG_브랜드_대분류_중분류_소분류" → "대분류 > 중분류 > 소분류"
            # 추가: leaf 단일 토큰("소분류")도 alias 키로 등록 — SSG 크론 수집 시
            # 검색결과 detail이 풀 path를 못 가져와 leaf만 알 때(가장 흔한 케이스)
            # 기존 카테고리 매핑(스캔으로 만든 풀 path 필터)을 재사용해 leaf 자동생성
            # 무한증식을 차단한다. leaf 충돌(여러 필터가 같은 leaf) 시 먼저 등록된 것을
            # 우선해 후속 등록은 무시 — UI 매핑현황의 정의 순서 따름.
            if f.name:
                _nm_parts = f.name.split("_")
                if len(_nm_parts) > 2:
                    cat_name_map[" > ".join(_nm_parts[2:])] = f.id
                    _leaf = _nm_parts[-1].strip()
                    if _leaf and _leaf not in cat_name_map:
                        cat_name_map[_leaf] = f.id
            # repBrandId 추출 (keyword URL)
            if f.keyword and "repBrandId=" in f.keyword:
                try:
                    _qs = parse_qs(urlparse(f.keyword).query)
                    for _bid in (_qs.get("repBrandId", [""])[0] or "").split("|"):
                        if _bid and _bid not in _brand_ids_from_filter:
                            _brand_ids_from_filter.append(_bid)
                except Exception:
                    pass

        if not cat_filter_map:
            await repo.fail_job(
                job.id,
                "brand_all_ssg: category_filter가 없습니다 (그룹 스캔 후 다시 시도)",
            )
            return

        _add_job_log(
            job.id,
            f"[SSG브랜드전체수집] 카테고리 맵 {len(cat_filter_map)}개 | 브랜드: {keyword}",
            job_type="collect",
        )

        # 메인 IP 단일 클라이언트 — 프록시 미사용
        client = SSGSourcingClient()

        if not _brand_ids_from_filter:
            _brand_ids_from_filter = await client._fetch_brand_ids(keyword)
            _add_job_log(
                job.id,
                f"[SSG브랜드전체수집] brand_ids 자동추출: {_brand_ids_from_filter}",
                job_type="collect",
            )

        # 시작 시 기존 수집 ID 로드 (전 페이지 dedup)
        existing_result = await session.execute(
            select(CPModel.site_product_id).where(
                CPModel.source_site == "SSG",
                CPModel.search_filter_id.in_(filter_ids),
            )
        )
        existing_ids: set[str] = {row[0] for row in existing_result.all()}
        _seen_spids: set[str] = set(existing_ids)

        svc = _get_services(session)
        total_saved = 0
        total_skipped = 0
        total_unmatched = 0
        # 재시도 큐 — 상세조회/매핑 실패 상품 누수 방지
        _failed_queue: list[dict] = []
        # 병렬 배치 처리 — SSG rate-limit 완화를 위해 2개 동시로 제한
        _SSG_BATCH = 2
        _ssg_page = 1
        # 필터 requested_count 합산 → 총 예상 건수 (진행률 표시용)
        _ssg_total_est = sum(f.requested_count or 0 for f in filters) or 1

        while True:
            from backend.domain.samba.emergency import (
                is_collect_cancel_requested as _icc_s,
                is_emergency_stopped as _ies_s,
            )

            if _icc_s() or _ies_s() or await repo.is_cancelled(job.id):
                await repo.cancel_job(job.id)
                await session.commit()
                return

            # 1단계: 해당 페이지 검색 — 확장앱 소싱큐로 위임 (하이브리드)
            from urllib.parse import quote as _qs_quote

            _brand_q = (
                "|".join(_brand_ids_from_filter) if _brand_ids_from_filter else ""
            )
            _ssg_search_url = (
                f"https://department.ssg.com/search?query={_qs_quote(keyword)}"
                f"&page={_ssg_page}"
            )
            if _brand_q:
                _ssg_search_url += f"&repBrandId={_brand_q}"

            _add_job_log(
                job.id,
                f"[SSG브랜드전체수집] {_ssg_page}페이지 검색 중...",
                job_type="collect",
            )
            _raw: list[dict] = []
            for _attempt in range(3):
                try:
                    _raw = await client.search_products(
                        keyword,
                        page=_ssg_page,
                        size=40,
                        brand_ids=_brand_ids_from_filter,
                    )
                    break
                except SSGSearchRL as _rl:
                    _wait = _rl.retry_after or min(15, 3 * (_attempt + 1))
                    _add_job_log(
                        job.id,
                        f"[SSG브랜드전체수집] 검색 속도제한 {_wait}초 대기 (p{_ssg_page})",
                        job_type="collect",
                    )
                    if await _cancellable_sleep(_wait):
                        await repo.cancel_job(job.id)
                        await session.commit()
                        return
                except Exception as _se:
                    _add_job_log(
                        job.id,
                        f"[SSG브랜드전체수집] 검색 오류: {type(_se).__name__} (p{_ssg_page})",
                        job_type="collect",
                    )
                    break

            if not _raw:
                break

            # 2단계: 이 페이지 신규 상품 추출 (확장앱 반환은 이미 정규화된 형태)
            # [중요] SSG 검색 API는 repBrandId+ctgId 동시 사용 시 ctgId 무시 → repBrandId 제거 상태.
            # query 키워드 매칭이 하위 브랜드(나이키키즈/스윔/골프)까지 반환하므로 클라이언트 post-filter 필수.
            # 2중 방어:
            #   (1) brandId 정확 매칭 — _brand_ids_from_filter set 기준
            #   (2) brand 이름 정확 매칭 — keyword(=선택 브랜드명)와 item.brand 비교
            _allowed_brand_ids: set[str] = {
                str(b).strip() for b in (_brand_ids_from_filter or []) if str(b).strip()
            }
            _keyword_norm = str(keyword or "").strip()
            _brand_dropped = 0
            page_new: list[dict] = []
            for item in _raw:
                pid = str(
                    item.get("site_product_id")
                    or item.get("siteProductId")
                    or item.get("goodsNo")
                    or ""
                )
                if not pid or pid in _seen_spids:
                    continue
                # 브랜드 post-filter
                _item_bid = str(
                    item.get("repBrandId") or item.get("brandId") or ""
                ).strip()
                _item_bname = str(item.get("brand") or "").strip()
                _match_id = (not _allowed_brand_ids) or (
                    _item_bid and _item_bid in _allowed_brand_ids
                )
                _match_name = (not _keyword_norm) or (_item_bname == _keyword_norm)
                # brandId가 있으면 id 매칭으로 하위브랜드 필터링
                # brandId가 없으면 어느 브랜드인지 알 수 없으므로 통과 (SSG는 brandId 미제공)
                if _item_bid:
                    _keep = _match_id
                else:
                    _keep = True
                if not _keep:
                    _brand_dropped += 1
                    continue
                _seen_spids.add(pid)
                page_new.append(
                    {
                        "site_product_id": pid,
                        "name": item.get("name", ""),
                        "brand": item.get("brand", ""),
                        "sale_price": item.get("salePrice")
                        or item.get("sale_price", 0),
                        "original_price": item.get("originalPrice")
                        or item.get("original_price", 0),
                        "images": [item.get("image")]
                        if item.get("image")
                        else item.get("images", []),
                        "is_sold_out": item.get("isSoldOut", False),
                    }
                )

            if _brand_dropped:
                _add_job_log(
                    job.id,
                    f"[SSG브랜드전체수집] 하위브랜드 drop {_brand_dropped}건 (p{_ssg_page})",
                    job_type="collect",
                )

            # 3단계: 신규 상품 즉시 상세조회+저장 (1건 순차, 배치당 2초)

            _page_cancelled = False
            for _bs in range(0, len(page_new), _SSG_BATCH):
                if _page_cancelled:
                    break
                _batch = page_new[_bs : _bs + _SSG_BATCH]

                # 상세 조회 — 확장앱 소싱큐 병렬 배치 처리 (배치당 5개 동시)
                # 1단계: 블랙리스트 사전 필터링 (순차)
                _non_bl: list[dict] = []
                for _bl_it in _batch:
                    if _page_cancelled:
                        break
                    _spid_bl = _bl_it["site_product_id"]
                    if await _is_blacklisted(session, "SSG", _spid_bl):
                        logger.info(f"[SSG수집] 블랙리스트 스킵: SSG/{_spid_bl}")
                        total_skipped += 1
                    else:
                        _non_bl.append(_bl_it)

                # 2단계: 병렬 상세조회
                from backend.domain.samba.proxy.sourcing_queue import SourcingQueue

                _ssg_ext_cache: dict[str, Any] = {}
                if _non_bl and not _page_cancelled:
                    _bl_futs = [
                        SourcingQueue.add_detail_job("SSG", _it["site_product_id"])
                        for _it in _non_bl
                    ]
                    _gathered_ext = await asyncio.gather(
                        *[asyncio.wait_for(f, timeout=55) for _, f in _bl_futs],
                        return_exceptions=True,
                    )
                    for _bl_it2, _bl_ext in zip(_non_bl, _gathered_ext):
                        _ssg_ext_cache[_bl_it2["site_product_id"]] = _bl_ext

                # 3단계: 결과 처리 및 DB 저장
                for it in _non_bl:
                    if _page_cancelled:
                        break
                    spid = it["site_product_id"]
                    _ext_result = _ssg_ext_cache.get(spid)
                    detail: dict = {}
                    if isinstance(_ext_result, asyncio.TimeoutError):
                        _add_job_log(
                            job.id,
                            f"[SSG] 상세 타임아웃: {spid} (확장앱 미응답)",
                            job_type="collect",
                        )
                    elif isinstance(_ext_result, Exception):
                        logger.debug(f"[SSG] 확장앱 상세 실패: {spid} — {_ext_result}")
                    elif isinstance(_ext_result, dict) and _ext_result.get("success"):
                        _html = _ext_result.get("html", "")
                        _dom_bc = _ext_result.get("domBreadcrumb", []) or []
                        if _html:
                            _loop = asyncio.get_event_loop()
                            detail = await _loop.run_in_executor(
                                None,
                                lambda: (
                                    client._parse_result_item_obj(
                                        _html, spid, False, dom_breadcrumb=_dom_bc
                                    )
                                    or {}
                                ),
                            )
                        # 확장앱 detailHtml 머지 — _ext_result에 있으면 detail에 주입해
                        # 이후 _build_product_data 의 detail.get("detailHtml", "") 폴백이
                        # 정상 작동. 백엔드 html 필드는 script 태그만이라 cdtl_desc DOM
                        # 추출 불가하므로 확장앱이 보내준 것을 그대로 사용.
                        _ext_detail_html = _ext_result.get("detailHtml", "")
                        if _ext_detail_html and detail is not None:
                            detail["detailHtml"] = _ext_detail_html
                        # 확장앱 DOM 썸네일(domImages) 머지 — 추가이미지 백필.
                        # html 필드가 script 태그만이라 _build_images_from_base_url 정규식이
                        # body의 <img.zoom_thumb>를 못 잡아 i2~iN이 누락되는 문제 해결.
                        _dom_imgs = (
                            _ext_result.get("domImages", [])
                            if isinstance(_ext_result, dict)
                            else []
                        )
                        if _dom_imgs and detail:
                            _cur_imgs = list(detail.get("images") or [])
                            _seen_imgs = set(_cur_imgs)
                            for _di in _dom_imgs:
                                if _di and _di not in _seen_imgs:
                                    _cur_imgs.append(_di)
                                    _seen_imgs.add(_di)
                                    if len(_cur_imgs) >= 9:
                                        break
                            detail["images"] = _cur_imgs[:9]
                        # _parse_result_item_obj 실패 시 (dept.ssg.com AJAX 로드):
                        # 확장앱 safeObj의 itemNm + HTML select 직접 파싱으로 폴백
                        if not detail:
                            _ext_obj = _ext_result.get("resultItemObj", {})
                            _item_nm = _ext_obj.get("itemNm", "")
                            if _item_nm and _html:
                                _opts = await _loop.run_in_executor(
                                    None,
                                    lambda: client._parse_layered_select_options(_html),
                                )
                                _sold = (
                                    all(o.get("isSoldOut", False) for o in _opts)
                                    if _opts
                                    else False
                                )
                                detail = {
                                    "itemNm": _item_nm,
                                    "name": _item_nm,
                                    "brand": _ext_obj.get("repBrandNm")
                                    or _ext_obj.get("brandNm", ""),
                                    "options": _opts,
                                    "soldOut": "Y" if _sold else "N",
                                    "dispCtgLclsNm": "",
                                    "dispCtgMclsNm": "",
                                    "dispCtgSclsNm": "",
                                    "dispCtgId": "",
                                }
                        # 확장앱 uitemOptions(AJAX 후 실재고+이중옵션)로 옵션 교체 또는 보정
                        # ssg.py refresh와 동일 로직: 이중옵션이면 전체 교체, 아니면 품절만 보정
                        _uitem_opts = _ext_result.get("uitemOptions", [])
                        if _uitem_opts:
                            _detail_opts = detail.get("options") or []
                            _has_layered_uitem = any(
                                "/" in str(o.get("name", "")) for o in _uitem_opts
                            )
                            _has_layered_detail = any(
                                "/" in str(o.get("name", "")) for o in _detail_opts
                            )
                            if _has_layered_uitem and (
                                not _detail_opts
                                or not _has_layered_detail
                                or len(_detail_opts) < len(_uitem_opts)
                            ):
                                # 이중옵션(색상/사이즈) 전체 교체
                                _price_fallback = int(detail.get("salePrice", 0) or 0)
                                detail["options"] = [
                                    {
                                        "name": _uo.get("name", ""),
                                        "price": int(_uo.get("price", 0) or 0)
                                        or _price_fallback,
                                        "stock": _uo.get("usablInvQty", 0)
                                        if not _uo.get("isSoldOut")
                                        else 0,
                                        "isSoldOut": _uo.get("isSoldOut", False),
                                    }
                                    for _uo in _uitem_opts
                                    if _uo.get("name")
                                ]
                            elif _detail_opts:
                                # 단일 옵션: 품절 상태만 보정
                                _soldout_names = {
                                    o["name"] for o in _uitem_opts if o.get("isSoldOut")
                                }
                                if _soldout_names:
                                    for _opt in _detail_opts:
                                        if _opt.get("name") in _soldout_names:
                                            _opt["isSoldOut"] = True
                                            _opt["stock"] = 0

                    if not detail or not (detail.get("itemNm") or detail.get("name")):
                        _failed_queue.append(it)
                        continue

                    is_sold_out = bool(
                        detail.get("soldOut") == "Y" or it.get("is_sold_out", False)
                    )
                    if is_sold_out and not _include_sold_out:
                        total_skipped += 1
                        continue

                    # 카테고리 매핑 (3단계 + 최후 fallback)
                    # 1순위: dispCtgId → cat_filter_map
                    disp_ctg_id = detail.get("dispCtgId", "")
                    filter_id = cat_filter_map.get(disp_ctg_id) if disp_ctg_id else None

                    # 2순위: dispCtg 레벨명 경로 → cat_filter_map / cat_name_map
                    _cat_parts = [
                        (detail.get("dispCtgLclsNm", "") or "").strip(),
                        (detail.get("dispCtgMclsNm", "") or "").strip(),
                        (detail.get("dispCtgSclsNm", "") or "").strip(),
                    ]
                    _cat_parts = [p for p in _cat_parts if p]
                    if not filter_id:
                        for _d in range(len(_cat_parts), 0, -1):
                            _sub = " > ".join(_cat_parts[:_d])
                            filter_id = cat_filter_map.get(_sub) or cat_name_map.get(
                                _sub
                            )
                            if filter_id:
                                break

                    # 3순위: detail["category"] (4단계 폴백 적용된 최종 경로)
                    if not filter_id:
                        _full_cat = (detail.get("category") or "").strip()
                        if _full_cat:
                            _fc_parts = [
                                p.strip() for p in _full_cat.split(" > ") if p.strip()
                            ]
                            if not _cat_parts:
                                _cat_parts = _fc_parts
                            for _d in range(len(_fc_parts), 0, -1):
                                _sub = " > ".join(_fc_parts[:_d])
                                filter_id = cat_filter_map.get(
                                    _sub
                                ) or cat_name_map.get(_sub)
                                if filter_id:
                                    break

                    # 3.5순위: leaf 단일 토큰으로 cat_name_map 룩업 (카테고리 스캔으로
                    # 만든 기존 풀 path 필터의 leaf alias 매칭). 검색결과 detail이
                    # dispCtgLclsNm/Mcls/Scls를 비워 보내고 dispCtgNm(leaf)만 가져오는
                    # 케이스를 위한 폴백 — leaf 1개로 새 필터 자동생성하는 무한증식을 차단.
                    if not filter_id:
                        _leaf_candidates: list[str] = []
                        if _cat_parts:
                            _leaf_candidates.append(_cat_parts[-1])
                        _disp_nm_leaf = (detail.get("dispCtgNm") or "").strip()
                        if _disp_nm_leaf and _disp_nm_leaf not in _leaf_candidates:
                            _leaf_candidates.append(_disp_nm_leaf)
                        _full_cat_leaf = (detail.get("category") or "").strip()
                        if _full_cat_leaf:
                            _fc_leaf = _full_cat_leaf.split(" > ")[-1].strip()
                            if _fc_leaf and _fc_leaf not in _leaf_candidates:
                                _leaf_candidates.append(_fc_leaf)
                        for _leaf_key in _leaf_candidates:
                            filter_id = cat_name_map.get(_leaf_key)
                            if filter_id:
                                # 풀 path 필터에 매칭됐으니 leaf-only 기록 방지를 위해
                                # _cat_parts도 매칭 필터명에서 복원해 product.category가
                                # 풀 path로 저장되도록 한다.
                                _f_match = next(
                                    (f for f in filters if f.id == filter_id), None
                                )
                                if _f_match and _f_match.name:
                                    _name_parts = _f_match.name.split("_")
                                    if len(_name_parts) > 2:
                                        _restored = [p for p in _name_parts[2:] if p]
                                        if len(_restored) > len(_cat_parts):
                                            _cat_parts = _restored
                                _add_job_log(
                                    job.id,
                                    f"[필터leaf매칭] '{_leaf_key}' → 기존 매핑 재사용",
                                    job_type="collect",
                                )
                                break

                    # 3순위도 실패 시 필터 자동 생성 — 누수 0 보장
                    if not filter_id:
                        # stdCtg 경로도 시도
                        _std_parts = [
                            (detail.get("stdCtgLclsNm", "") or "").strip(),
                            (detail.get("stdCtgMclsNm", "") or "").strip(),
                            (detail.get("stdCtgSclsNm", "") or "").strip(),
                        ]
                        _std_parts = [p for p in _std_parts if p]
                        _brand_nm = keyword or "브랜드"
                        _cat_path_final = (
                            " > ".join(_cat_parts)
                            or " > ".join(_std_parts)
                            or detail.get("dispCtgNm", "")
                            or "미분류"
                        )
                        _cat_parts_for_name = (
                            _cat_parts
                            or _std_parts
                            or [detail.get("dispCtgNm", "기타")]
                        )
                        # 플레이오토 MyCateName은 '/'가 트리 구분자 — 필터명에 '/' 금지
                        _new_name = (
                            f"SSG_{_brand_nm}_" + "_".join(_cat_parts_for_name)
                        ).replace("/", "_")
                        # 동일 이름 필터 중복 방지
                        _existing = next(
                            (f for f in filters if f.name == _new_name), None
                        )
                        if _existing:
                            filter_id = _existing.id
                        else:
                            _parent = filters[0] if filters else None
                            _new_filter = SambaSearchFilter(
                                source_site="SSG",
                                name=_new_name,
                                parent_id=_parent.parent_id if _parent else None,
                                tenant_id=_parent.tenant_id if _parent else None,
                                keyword=_parent.keyword if _parent else "",
                                category_filter=disp_ctg_id or None,
                                source_brand_name=keyword,
                                requested_count=0,
                            )
                            session.add(_new_filter)
                            await session.flush()
                            if disp_ctg_id:
                                cat_filter_map[disp_ctg_id] = _new_filter.id
                            cat_name_map[_cat_path_final] = _new_filter.id
                            filters.append(_new_filter)
                            filter_id = _new_filter.id
                            _add_job_log(
                                job.id,
                                f"[필터자동생성] {_new_name} (cat={_cat_path_final[:40]})",
                                job_type="collect",
                            )

                    _sale_price = int(
                        detail.get("sellprc", 0) or it.get("sale_price", 0) or 0
                    )
                    _original_price = int(
                        detail.get("originalPrice", 0)
                        or it.get("original_price", 0)
                        or _sale_price
                    )
                    _bbp = int(detail.get("bestAmt", 0) or 0)
                    _cost = (
                        (_bbp if _bbp > 0 else _sale_price)
                        if _use_max_discount
                        else _sale_price
                    )
                    _is_free = detail.get("freeShipping", False) or it.get(
                        "free_shipping", False
                    )
                    if not _is_free:
                        _cost += int(detail.get("shippingFee", 0) or 0)

                    # 원가 수집 실패 시 100,000원 sentinel — 배송비만 남는 사고 방지
                    if _cost <= 0:
                        _add_job_log(
                            job.id,
                            f"[원가수집실패] SSG spid={spid} → 100,000원 fallback 적용",
                            job_type="collect",
                        )
                        _cost = 100000

                    # 크론잡(brand_all) 분기에서 SSG 검색결과의 dispCtgLclsNm/Mcls/Scls가
                    # 비어 있는 케이스가 다수 — _cat_parts가 1개 이하면 검색그룹명(filter.name)
                    # 의 풀 path("SSG_브랜드_대_중_소") 에서 카테고리 단계를 복원해
                    # product.category 가 leaf 1단계로 굳는 사고를 방지.
                    if len(_cat_parts) <= 1 and filter_id:
                        _f_match = next((f for f in filters if f.id == filter_id), None)
                        if _f_match and _f_match.name:
                            _name_parts = _f_match.name.split("_")
                            # 형식: "SSG_<브랜드>_<대>_<중>_<소>..." → brand 다음 토큰들이 path
                            if len(_name_parts) > 2:
                                _restored = [p for p in _name_parts[2:] if p]
                                if len(_restored) > len(_cat_parts):
                                    _cat_parts = _restored
                    _raw_cat = " > ".join(_cat_parts)
                    detail_for_build: dict = {
                        "name": detail.get("itemNm")
                        or detail.get("name")
                        or it.get("name", ""),
                        "brand": detail.get("repBrandNm")
                        or detail.get("brand")
                        or it.get("brand", ""),
                        "images": detail.get("images")
                        or ([it["images"][0]] if it.get("images") else []),
                        "detailImages": detail.get("detailImages") or [],
                        "options": detail.get("options") or [],
                        "sourceUrl": detail.get("sourceUrl")
                        or f"https://www.ssg.com/item/itemView.ssg?itemId={spid}",
                        "category": _raw_cat,
                        "manufacturer": detail.get("repBrandNm") or it.get("brand", ""),
                        "origin": detail.get("origin", ""),
                        "material": detail.get("material", ""),
                        "color": detail.get("color", ""),
                        "care_instructions": detail.get("care_instructions", ""),
                        "saleStatus": "sold_out" if is_sold_out else "in_stock",
                        "freeShipping": _is_free,
                        "styleNo": detail.get("style_code", "")
                        or detail.get("modelNo", ""),
                    }
                    product_data = _build_product_data(
                        detail_for_build,
                        spid,
                        filter_id,
                        "SSG",
                        _cost,
                        _sale_price,
                        _original_price,
                        _raw_cat,
                        _cat_parts,
                        detail.get("detailHtml", ""),
                    )
                    await svc.create_collected_product(product_data)
                    total_saved += 1
                    _collect_last_progress[job.id] = _time.time()
                    _log_brand = (detail_for_build.get("brand") or "").strip()
                    _log_name = (detail_for_build.get("name") or "").strip()
                    _log_style = (detail_for_build.get("style_code") or "").strip()
                    _ssg_log = (
                        f"[{total_saved:,}/{_ssg_total_est:,}] {_log_brand} {_log_name}"
                    )
                    if _log_style:
                        _ssg_log += f" {_log_style}"
                    _ssg_log += f" {spid}"
                    _add_job_log(job.id, _ssg_log, job_type="collect")

                # 배치 간 1초 딜레이
                await asyncio.sleep(1.0)

            await repo.update_progress(job.id, total_saved, total_saved + 1)
            _add_job_log(
                job.id,
                f"[SSG브랜드전체수집] {_ssg_page}페이지 완료 — 저장 누적 {total_saved:,}건 (신규 {len(page_new)}건)",
                job_type="collect",
            )

            if _page_cancelled:
                await repo.cancel_job(job.id)
                await session.commit()
                return

            # 페이지 전체 dupe여도 다음 페이지 계속 시도 — 누수 방지
            # _raw 자체가 비면 break (search 결과 소진)
            _ssg_page += 1
            # 페이지 간 딜레이 없음 — 최대 속도

        # 4단계: 재시도 큐 처리 — 메인 루프에서 실패한 상품을 긴 대기 후 재시도
        if _failed_queue:
            _add_job_log(
                job.id,
                f"[SSG브랜드전체수집] 재시도 큐 {len(_failed_queue):,}건 (누수 방지)",
                job_type="collect",
            )
            _retry_waits = [60, 120, 300, 600, 600]  # 5 라운드
            for _round_idx, _wait_sec in enumerate(_retry_waits, 1):
                if not _failed_queue:
                    break
                _add_job_log(
                    job.id,
                    f"[재시도 R{_round_idx}] {_wait_sec}초 대기 후 {len(_failed_queue):,}건 재시도",
                    job_type="collect",
                )
                # 대기 중에도 heartbeat 갱신 — 메인 스레드 타임아웃(600s) 방지
                _collect_last_progress[job.id] = _time.time()
                if await _cancellable_sleep(_wait_sec):
                    break
                _collect_last_progress[job.id] = _time.time()
                _current = _failed_queue
                _failed_queue = []
                for _fit in _current:
                    from backend.domain.samba.emergency import (
                        is_collect_cancel_requested as _icc_r,
                        is_emergency_stopped as _ies_r,
                    )

                    if _icc_r() or _ies_r() or await repo.is_cancelled(job.id):
                        await repo.cancel_job(job.id)
                        await session.commit()
                        return
                    _spid = _fit["site_product_id"]
                    # 확장앱 소싱큐 경유 (직접 HTTP 차단 우회)
                    from backend.domain.samba.proxy.sourcing_queue import (
                        SourcingQueue as _SQ_r,
                    )

                    _det: dict = {}
                    try:
                        _, _r_fut = _SQ_r.add_detail_job("SSG", _spid)
                        _r_ext = await asyncio.wait_for(_r_fut, timeout=45)
                        if isinstance(_r_ext, dict) and _r_ext.get("success"):
                            _r_html = _r_ext.get("html", "")
                            _r_dom_bc = _r_ext.get("domBreadcrumb", []) or []
                            if _r_html:
                                _r_loop = asyncio.get_event_loop()
                                _det = await _r_loop.run_in_executor(
                                    None,
                                    lambda: (
                                        client._parse_result_item_obj(
                                            _r_html,
                                            _spid,
                                            False,
                                            dom_breadcrumb=_r_dom_bc,
                                        )
                                        or {}
                                    ),
                                )
                            if not _det:
                                _r_obj = _r_ext.get("resultItemObj", {})
                                _r_nm = _r_obj.get("itemNm", "")
                                if _r_nm and _r_html:
                                    _r_loop = asyncio.get_event_loop()
                                    _r_opts = await _r_loop.run_in_executor(
                                        None,
                                        lambda: client._parse_layered_select_options(
                                            _r_html
                                        ),
                                    )
                                    _det = {
                                        "itemNm": _r_nm,
                                        "name": _r_nm,
                                        "brand": _r_obj.get("repBrandNm")
                                        or _r_obj.get("brandNm", ""),
                                        "options": _r_opts,
                                        "soldOut": "N",
                                        "dispCtgLclsNm": "",
                                        "dispCtgMclsNm": "",
                                        "dispCtgSclsNm": "",
                                        "dispCtgId": "",
                                    }
                    except Exception:
                        _det = {}
                    if not _det or not (_det.get("itemNm") or _det.get("name")):
                        _failed_queue.append(_fit)
                        continue
                    # 카테고리 매핑 (간단 버전 — 메인 로직과 동일)
                    _disp = _det.get("dispCtgId", "")
                    _fid = cat_filter_map.get(_disp) if _disp else None
                    if not _fid:
                        _cps = [
                            (_det.get("dispCtgLclsNm", "") or "").strip(),
                            (_det.get("dispCtgMclsNm", "") or "").strip(),
                            (_det.get("dispCtgSclsNm", "") or "").strip(),
                        ]
                        _cps = [p for p in _cps if p]
                        for _d in range(len(_cps), 0, -1):
                            _sub = " > ".join(_cps[:_d])
                            _fid = cat_filter_map.get(_sub) or cat_name_map.get(_sub)
                            if _fid:
                                break
                    if not _fid:
                        total_unmatched += 1
                        continue
                    # 저장
                    _sp = int(_det.get("sellprc", 0) or 0)
                    _op = int(_det.get("originalPrice", 0) or _sp)
                    _bbp = int(_det.get("bestAmt", 0) or 0)
                    _co = (_bbp if _bbp > 0 else _sp) if _use_max_discount else _sp
                    _fs = _det.get("freeShipping", False)
                    if not _fs:
                        _co += int(_det.get("shippingFee", 0) or 0)
                    # 원가 수집 실패 시 100,000원 sentinel
                    if _co <= 0:
                        _add_job_log(
                            job.id,
                            "[원가수집실패] SSG refresh → 100,000원 fallback 적용",
                            job_type="collect",
                        )
                        _co = 100000
                    _cat_parts_r = [
                        _det.get("dispCtgLclsNm", "") or "",
                        _det.get("dispCtgMclsNm", "") or "",
                        _det.get("dispCtgSclsNm", "") or "",
                    ]
                    _cat_parts_r = [c for c in _cat_parts_r if c]
                    # 키 fallback — _parse_result_item_obj 산출물은 최상위 키가
                    # "name"/"brand" 이고, 폴백 fallback dict 는 itemNm/repBrandNm 도 포함.
                    # 두 형태 모두 안전하게 받기 위해 메인 경로(2978~)와 동일하게 확장.
                    _d4build = {
                        "name": _det.get("itemNm") or _det.get("name", ""),
                        "brand": _det.get("repBrandNm") or _det.get("brand", ""),
                        "images": _det.get("images") or [],
                        "detailImages": _det.get("detailImages") or [],
                        "options": _det.get("options") or [],
                        "sourceUrl": _det.get("sourceUrl")
                        or f"https://www.ssg.com/item/itemView.ssg?itemId={_spid}",
                        "category": " > ".join(_cat_parts_r),
                        "manufacturer": _det.get("repBrandNm") or _det.get("brand", ""),
                        "origin": _det.get("origin", ""),
                        "material": _det.get("material", ""),
                        "color": _det.get("color", ""),
                        "care_instructions": _det.get("care_instructions", ""),
                        "saleStatus": "in_stock",
                        "freeShipping": _fs,
                        "styleNo": _det.get("styleNo")
                        or _det.get("style_code", "")
                        or _det.get("modelNo", ""),
                    }
                    _pd = _build_product_data(
                        _d4build,
                        _spid,
                        _fid,
                        "SSG",
                        _co,
                        _sp,
                        _op,
                        " > ".join(_cat_parts_r),
                        _cat_parts_r,
                        _det.get("detailHtml", ""),
                    )
                    await svc.create_collected_product(_pd)
                    total_saved += 1
                    _r_brand = (_d4build.get("brand") or "").strip()
                    _r_name = (_d4build.get("name") or "").strip()
                    _r_style = (_d4build.get("style_code") or "").strip()
                    _r_log = f"[재시도 R{_round_idx}][{total_saved:,}/{_ssg_total_est:,}] {_r_brand} {_r_name}"
                    if _r_style:
                        _r_log += f" {_r_style}"
                    _r_log += f" {_spid}"
                    _add_job_log(job.id, _r_log, job_type="collect")
                _add_job_log(
                    job.id,
                    f"[재시도 R{_round_idx}] 완료 — 남은 실패 {len(_failed_queue):,}건",
                    job_type="collect",
                )

            if _failed_queue:
                _add_job_log(
                    job.id,
                    f"[SSG브랜드전체수집] 최종 실패 {len(_failed_queue):,}건 (재시도 한도 초과)",
                    job_type="collect",
                )

        # 각 SearchFilter의 requested_count를 실제 수집수로 갱신 + 0건 그룹 자동 삭제
        from sqlalchemy import delete as _sa_del

        _empty_filter_ids: list[str] = []
        for f in filters:
            actual = (
                await session.execute(
                    select(_func.count()).where(CPModel.search_filter_id == f.id)
                )
            ).scalar() or 0
            if actual == 0:
                _empty_filter_ids.append(f.id)
            else:
                await session.execute(
                    _sa_upd(SambaSearchFilter)
                    .where(SambaSearchFilter.id == f.id)
                    .values(last_collected_at=datetime.now(UTC))
                )

        if _empty_filter_ids:
            await session.execute(
                _sa_del(SambaSearchFilter).where(
                    SambaSearchFilter.id.in_(_empty_filter_ids)
                )
            )
            _add_job_log(
                job.id,
                f"[SSG브랜드전체수집] 0건 그룹 {len(_empty_filter_ids):,}개 자동 삭제",
                job_type="collect",
            )

        _add_job_log(
            job.id,
            f"[SSG브랜드전체수집] 완료: 저장 {total_saved:,}건 | 품절스킵 {total_skipped:,}건 | 카테고리미매핑 {total_unmatched:,}건",
            job_type="collect",
        )
        await repo.complete_job(
            job.id,
            {
                "saved": total_saved,
                "skipped": total_skipped,
                "unmatched": total_unmatched,
            },
        )
        logger.info(f"[잡워커] SSG브랜드전체수집 완료: {job.id} ({total_saved:,}건)")

    async def _run_brand_collect_all_gs(self, job, repo, session):
        """GS샵 브랜드 전체 상품을 단일 Job으로 수집 후 카테고리별 SearchFilter 배분.

        ABCmart 패턴 준용:
        - cat_filter_map: filter.category_filter (= GNB 경로 path) → filter_id
        - 검색: GsShopSourcingClient.search_products() — 전체 상품 ID 크롤링
        - 상세: get_product_detail() → category(GNB 경로)로 카테고리 배분
        - name_map fallback: filter.name에서 카테고리 경로 추출 + 깊이별 재시도
        """
        from sqlalchemy import select, update as _sa_upd, func as _func
        from backend.domain.samba.collector.model import SambaSearchFilter
        from backend.domain.samba.collector.model import (
            SambaCollectedProduct as CPModel,
        )
        from backend.domain.samba.proxy.gsshop_sourcing import GsShopSourcingClient
        from backend.api.v1.routers.samba.collector_common import (
            _build_product_data,
            _get_services,
        )
        from datetime import datetime, timezone as _tz

        UTC = _tz.utc
        payload = job.payload or {}
        filter_ids: list[str] = payload.get("filter_ids", [])
        keyword: str = payload.get("keyword", "")
        _use_max_discount: bool = payload.get("use_max_discount", False)
        _include_sold_out: bool = payload.get("include_sold_out", False)

        if not filter_ids:
            await repo.fail_job(job.id, "brand_all_gs: filter_ids 필요")
            return

        _add_job_log(
            job.id,
            f"[GS브랜드전체수집] '{keyword}' 시작 — {len(filter_ids):,}개 그룹 대상",
            job_type="collect",
        )

        # SearchFilter 로드 + category_filter → filter_id 맵 구성
        filters_result = await session.execute(
            select(SambaSearchFilter).where(SambaSearchFilter.id.in_(filter_ids))
        )
        filters: list[SambaSearchFilter] = list(filters_result.scalars().all())

        cat_filter_map: dict[str, str] = {}  # {GNB경로: filter_id}
        cat_name_map: dict[str, str] = {}  # {경로후반부: filter_id} — fallback
        for f in filters:
            if f.category_filter:
                cat_filter_map[f.category_filter] = f.id
            # f.name = "GSShop_브랜드_대분류_중분류_소분류" → "대분류 > 중분류 > 소분류"
            if f.name:
                _nm_parts = f.name.split("_")
                if len(_nm_parts) > 2:
                    cat_name_map[" > ".join(_nm_parts[2:])] = f.id

        if not cat_filter_map:
            await repo.fail_job(
                job.id,
                "brand_all_gs: category_filter가 없습니다 (그룹 스캔 후 다시 시도)",
            )
            return

        _add_job_log(
            job.id,
            f"[GS브랜드전체수집] 카테고리 맵 {len(cat_filter_map)}개",
            job_type="collect",
        )

        # GS 전체 상품 검색 — DB 설정 페이지의 collect 프록시 풀 사용
        from backend.domain.samba.collector.refresher import get_collect_proxies

        _gs_proxies2 = get_collect_proxies()
        gs_client = GsShopSourcingClient(proxy_pool=_gs_proxies2 or None)
        _add_job_log(
            job.id,
            f"[GS브랜드전체수집] '{keyword}' 전체 상품 검색 중 (백화점탭 크롤링)...",
            job_type="collect",
        )
        all_items: list[dict] = await gs_client.search_products(keyword, size=9999)

        _add_job_log(
            job.id,
            f"[GS브랜드전체수집] 전체 {len(all_items):,}건 검색 완료",
            job_type="collect",
        )
        await repo.update_progress(job.id, 0, max(len(all_items), 1))

        # 이미 수집된 상품 제외
        existing_result = await session.execute(
            select(CPModel.site_product_id).where(
                CPModel.source_site == "GSShop",
                CPModel.search_filter_id.in_(filter_ids),
            )
        )
        existing_ids: set[str] = {row[0] for row in existing_result.all()}

        new_items = [
            it
            for it in all_items
            if str(it.get("site_product_id", "")) not in existing_ids
        ]
        _add_job_log(
            job.id,
            f"[GS브랜드전체수집] 신규 {len(new_items):,}건 (기존 {len(existing_ids):,}건 스킵)",
            job_type="collect",
        )

        # 5건 배치 상세 조회 → 카테고리 배분 → 저장
        svc = _get_services(session)
        total_saved = 0
        total_skipped = 0
        total_unmatched = 0
        _GS_BATCH = 5

        for _bs in range(0, len(new_items), _GS_BATCH):
            from backend.domain.samba.emergency import (
                is_collect_cancel_requested,
                is_emergency_stopped,
            )

            if (
                is_collect_cancel_requested()
                or is_emergency_stopped()
                or await repo.is_cancelled(job.id)
            ):
                await repo.cancel_job(job.id)
                await session.commit()
                return

            _batch = new_items[_bs : _bs + _GS_BATCH]
            _details = await asyncio.gather(
                *(
                    gs_client.get_product_detail(str(it.get("site_product_id", "")))
                    for it in _batch
                ),
                return_exceptions=True,
            )

            for _bi, (it, det) in enumerate(zip(_batch, _details)):
                spid = str(it.get("site_product_id", ""))
                detail = det if (det and not isinstance(det, Exception)) else {}

                is_sold_out = bool(
                    detail.get("isOutOfStock") or it.get("is_sold_out", False)
                )
                if is_sold_out and not _include_sold_out:
                    total_skipped += 1
                    continue

                # 브랜드 검증 — GSShop은 키워드 검색이라 무관 브랜드 상품도 매칭됨
                # 필터의 source_brand_name과 prefix 일치하지 않으면 스킵 (키즈/베이비 라인 보존)
                _target_brand = (
                    filters[0].source_brand_name if filters else keyword
                ) or ""
                _detail_brand = (detail.get("brand") or it.get("brand") or "").strip()
                if (
                    _target_brand
                    and _detail_brand
                    and not _detail_brand.startswith(_target_brand)
                ):
                    total_skipped += 1
                    continue

                # GS 상세 응답의 category 필드 = GNB_MAP 포함 전체 경로
                _cat_str = detail.get("category", "")
                filter_id = cat_filter_map.get(_cat_str) if _cat_str else None

                # category1~4 조합으로 깊이별 매핑 재시도
                _c_parts = [
                    (detail.get("category1", "") or "").strip(),
                    (detail.get("category2", "") or "").strip(),
                    (detail.get("category3", "") or "").strip(),
                    (detail.get("category4", "") or "").strip(),
                ]
                _c_parts = [c for c in _c_parts if c]
                if not filter_id:
                    for _depth in range(len(_c_parts), 0, -1):
                        _sub_path = " > ".join(_c_parts[:_depth])
                        filter_id = cat_filter_map.get(_sub_path) or cat_name_map.get(
                            _sub_path
                        )
                        if filter_id:
                            break

                # 자동 카테고리 filter 생성 (미매핑 0 보장)
                if not filter_id and filters and _c_parts:
                    _parent = filters[0]
                    _brand_nm = _parent.source_brand_name or keyword
                    _cat_path_str = " > ".join(_c_parts)
                    # 플레이오토 MyCateName은 '/'가 트리 구분자 — 필터명에 '/' 금지
                    _new_name = (f"GSShop_{_brand_nm}_" + "_".join(_c_parts)).replace(
                        "/", "_"
                    )
                    _new_filter = SambaSearchFilter(
                        source_site="GSShop",
                        name=_new_name,
                        parent_id=_parent.parent_id,
                        tenant_id=_parent.tenant_id,
                        keyword=_parent.keyword,
                        category_filter=_cat_str or _cat_path_str,
                        source_brand_name=_brand_nm,
                        requested_count=0,
                    )
                    session.add(_new_filter)
                    await session.flush()
                    cat_filter_map[_cat_path_str] = _new_filter.id
                    if _cat_str:
                        cat_filter_map[_cat_str] = _new_filter.id
                    cat_name_map[_cat_path_str] = _new_filter.id
                    filter_id = _new_filter.id
                    filters.append(_new_filter)
                    _add_job_log(
                        job.id,
                        f"[자동생성] 신규 카테고리: {_cat_path_str}",
                        job_type="collect",
                    )

                if not filter_id:
                    total_unmatched += 1
                    _p_name = (detail.get("name") or it.get("name", ""))[:20]
                    _add_job_log(
                        job.id,
                        f"[미매핑] {_p_name} ({spid}) cat={_cat_str[:30]}",
                        job_type="collect",
                    )
                    continue

                _sale_price = int(
                    detail.get("salePrice", 0) or it.get("sale_price", 0) or 0
                )
                _original_price = int(
                    detail.get("originalPrice", 0)
                    or it.get("original_price", 0)
                    or _sale_price
                )
                if _use_max_discount:
                    _bbp = int(detail.get("bestBenefitPrice", 0) or 0)
                    _cost = _bbp if _bbp > 0 else _sale_price
                else:
                    _cost = _sale_price

                _is_free_ship = detail.get("freeShipping", False) or it.get(
                    "free_shipping", False
                )
                if not _is_free_ship:
                    _cost += int(detail.get("shippingFee", 0) or 0)

                # 원가 수집 실패 시 100,000원 sentinel
                if _cost <= 0:
                    _add_job_log(
                        job.id,
                        f"[원가수집실패] GSShop spid={spid} → 100,000원 fallback 적용",
                        job_type="collect",
                    )
                    _cost = 100000

                _cat_parts_clean = [
                    detail.get("category1", "") or "",
                    detail.get("category2", "") or "",
                    detail.get("category3", "") or "",
                    detail.get("category4", "") or "",
                ]
                _cat_parts_clean = [c for c in _cat_parts_clean if c]

                detail_for_build: dict = {
                    "name": detail.get("name") or it.get("name", ""),
                    "brand": detail.get("brand") or it.get("brand", ""),
                    "images": detail.get("images") or [],
                    "detailImages": detail.get("detailImages") or [],
                    "options": detail.get("options") or [],
                    "sourceUrl": (
                        detail.get("sourceUrl")
                        or f"https://www.gsshop.com/prd/prd.gs?prdid={spid}"
                    ),
                    "category": _cat_str,
                    "manufacturer": detail.get("manufacturer") or it.get("brand", ""),
                    "origin": detail.get("origin", ""),
                    "material": detail.get("material", ""),
                    "color": detail.get("color", ""),
                    "saleStatus": "sold_out" if is_sold_out else "in_stock",
                    "freeShipping": _is_free_ship,
                    "styleNo": detail.get("modelName", ""),
                }
                raw_detail_html = detail.get("detailHtml", "")

                product_data = _build_product_data(
                    detail_for_build,
                    spid,
                    filter_id,
                    "GSShop",
                    _cost,
                    _sale_price,
                    _original_price,
                    _cat_str,
                    _cat_parts_clean,
                    raw_detail_html,
                )
                await svc.create_collected_product(product_data)
                existing_ids.add(spid)
                total_saved += 1
                _collect_last_progress[job.id] = _time.time()

                _log_brand = (detail_for_build.get("brand") or "").strip()
                _log_name = (detail_for_build.get("name") or "").strip()
                _add_job_log(
                    job.id,
                    f"[{total_saved:,}/{len(new_items):,}] {_log_brand} {_log_name} {spid}",
                    job_type="collect",
                )

            _done = min(_bs + _GS_BATCH, len(new_items))
            await repo.update_progress(job.id, _done, len(new_items))
            if _bs + _GS_BATCH < len(new_items):
                await asyncio.sleep(0.5)

        # 각 SearchFilter의 requested_count를 실제 수집수로 갱신
        for f in filters:
            actual = (
                await session.execute(
                    select(_func.count()).where(CPModel.search_filter_id == f.id)
                )
            ).scalar() or 0
            await session.execute(
                _sa_upd(SambaSearchFilter)
                .where(SambaSearchFilter.id == f.id)
                .values(last_collected_at=datetime.now(UTC))
            )

        _add_job_log(
            job.id,
            f"[GS브랜드전체수집] 완료: 저장 {total_saved:,}건 | 품절스킵 {total_skipped:,}건 | 카테고리미매핑 {total_unmatched:,}건",
            job_type="collect",
        )
        await repo.complete_job(
            job.id,
            {
                "saved": total_saved,
                "skipped": total_skipped,
                "unmatched": total_unmatched,
            },
        )
        logger.info(f"[잡워커] GS브랜드전체수집 완료: {job.id} ({total_saved:,}건)")

    async def _collect_direct_api(self, job, sf, session, repo):
        """FashionPlus/Nike/Adidas 등 직접 API 소싱처 수집."""
        from sqlalchemy import func as _func, select
        from backend.domain.samba.collector.model import (
            SambaCollectedProduct as CPModel,
        )
        from backend.api.v1.routers.samba.collector_common import (
            _get_services,
            generate_group_key,
        )

        site = sf.source_site
        filter_id = sf.id
        keyword = sf.keyword or ""
        _original_url = keyword  # URL 원본 보존 (카테고리 필터 포함)
        requested_count = sf.requested_count or FIXED_REQUESTED_COUNT
        _payload = job.payload or {}
        _dgi = _payload.get("group_index")
        _dgt = _payload.get("group_total")
        _dprefix = f"({_dgi}/{_dgt})" if _dgi and _dgt else f"[{site}]"

        # URL에서 키워드/필터 추출
        _search_kwargs: dict = {}
        _use_max_discount = False
        _include_sold_out = False
        try:
            from urllib.parse import urlparse, parse_qs

            parsed = urlparse(keyword)
            if parsed.scheme:
                qs = parse_qs(parsed.query)
                _use_max_discount = qs.get("maxDiscount", [""])[0] == "1"
                _include_sold_out = qs.get("includeSoldOut", [""])[0] == "1"
                # 소싱처별 키워드 파라미터: LOTTEON=q, GSShop=tq, SSG=query, FashionPlus=searchWord
                # NAVERSTORE: URL 전체(스토어명 + /category/ 또는 /search?q= path)가 필요 —
                # ?q= 추출로 치환하면 list_mixin에서 store_name 파싱 실패 → 수집 0건
                if site != "NAVERSTORE":
                    keyword = qs.get(
                        "q",
                        qs.get(
                            "tq",
                            qs.get(
                                "query",
                                qs.get("keyword", qs.get("searchWord", [keyword])),
                            ),
                        ),
                    )[0]
                # 패션플러스 필터 파라미터
                for k in (
                    "category1Id",
                    "category2Id",
                    "category3Id",
                    "sort",
                    "minPrice",
                    "maxPrice",
                    "maxDiscount",
                ):
                    v = qs.get(k, [""])[0]
                    if v:
                        _search_kwargs[k] = v
                # brands 파라미터
                brand_ids = qs.get("brands[][id]", [])
                brand_names = qs.get("brands[][name]", [])
                if brand_ids:
                    _search_kwargs["brand_id"] = brand_ids[0]
                if brand_names:
                    _search_kwargs["brand_name"] = brand_names[0]
                # SSG repBrandId 파라미터 → brand_ids 리스트로 전달
                _rep_brand_id = qs.get("repBrandId", [""])[0]
                if _rep_brand_id:
                    _search_kwargs["brand_ids"] = _rep_brand_id.split("|")
                # SSG ctgId 파라미터 → 검색 URL에 카테고리 필터 전달
                # 하위호환: 기존 dispCtgId 그룹도 지원
                _ctg_id = qs.get("ctgId", [""])[0] or qs.get("dispCtgId", [""])[0]
                if _ctg_id:
                    _search_kwargs["ctg_id"] = _ctg_id
                _ctg_lv = qs.get("ctgLv", [""])[0]
                if _ctg_lv:
                    _search_kwargs["ctg_lv"] = _ctg_lv
                # SSG ctgPath 파라미터 → 전시카테고리 전체 경로 (그룹 생성 시 저장)
                _ctg_path = qs.get("ctgPath", [""])[0]
                if _ctg_path:
                    _search_kwargs["ctgPath"] = _ctg_path
                # skipDetail 옵션
                if qs.get("skipDetail", [""])[0] == "1":
                    _search_kwargs["_skip_detail"] = True
        except Exception as exc:
            logger.warning(f"[잡워커] 검색 URL 파싱 실패: {exc}")

        # LOTTEON 서브키워드 모드 감지: q에 공백이 있으면 qapi total 기준 전수 수집
        # (스캔 단계의 샘플 분포 count로 requested_count가 작게 잡혀도 cap에 걸리지 않도록.
        # 수집 완료 시점에 실제 수집수로 requested_count가 자동 갱신되어 이후엔 정확해짐.)
        _use_subkw_mode = False
        if site == "LOTTEON":
            try:
                _sq_v = parse_qs(urlparse(sf.keyword or "").query).get("q", [""])[0]
                if _sq_v and " " in _sq_v:
                    _use_subkw_mode = True
            except Exception:
                pass

        # 기존 수집 수 확인
        count_stmt = select(_func.count()).where(CPModel.search_filter_id == filter_id)
        existing_count = (await session.execute(count_stmt)).scalar() or 0
        remaining = (
            99999 if _use_subkw_mode else max(0, requested_count - existing_count)
        )
        if remaining <= 0:
            _add_job_log(
                job.id,
                f"{_dprefix} [{sf.name}] 이미 {existing_count}개 수집됨",
                job_type="collect",
            )
            await repo.complete_job(
                job.id, {"saved": 0, "message": f"이미 {existing_count}개 수집됨"}
            )
            return

        # 클라이언트 생성 — 직접 API 소싱처
        client = None
        if site == "FashionPlus":
            from backend.domain.samba.proxy.fashionplus import FashionPlusClient

            client = FashionPlusClient()
        elif site == "Nike":
            from backend.domain.samba.proxy.nike import NikeClient

            client = NikeClient()
        elif site == "Adidas":
            from backend.domain.samba.proxy.adidas import AdidasClient

            client = AdidasClient()
        elif site == "LOTTEON":
            from backend.domain.samba.collector.refresher import get_collect_proxy_url
            from backend.domain.samba.proxy.lotteon_sourcing import (
                LotteonSourcingClient,
            )

            # 롯데ON WAF가 데이터센터 IP에서 502로 소프트 차단 — collect 프록시 적용
            _lotteon_proxy = get_collect_proxy_url()
            client = LotteonSourcingClient(proxy_url=_lotteon_proxy)
            if _lotteon_proxy:
                logger.info(
                    f"[잡워커] 롯데ON 수집 프록시: "
                    f"{_lotteon_proxy.split('@')[-1] if '@' in _lotteon_proxy else 'on'}"
                )
        elif site == "ABCmart":
            from backend.domain.samba.collector.refresher import get_collect_proxies
            from backend.domain.samba.proxy.abcmart import ARTSourcingClient

            # Cloud Run IP가 a-rt.com에 차단되는 현상 우회 — DB 설정 페이지의 collect 프록시 풀 사용
            _abc_proxies = get_collect_proxies()
            client = ARTSourcingClient(proxy_pool=_abc_proxies or None)
        elif site == "GSShop":
            from backend.domain.samba.collector.refresher import get_collect_proxies
            from backend.domain.samba.proxy.gsshop_sourcing import (
                GsShopSourcingClient,
            )

            _gs_proxies = get_collect_proxies()
            client = GsShopSourcingClient(proxy_pool=_gs_proxies or None)
        elif site == "SSG":
            from backend.domain.samba.proxy.ssg_sourcing import SSGSourcingClient

            client = SSGSourcingClient()
        elif site == "NAVERSTORE":
            from backend.domain.samba.proxy.naverstore_sourcing import (
                NaverStoreSourcingClient,
            )

            client = NaverStoreSourcingClient()
        elif site == "SNKRDUNK":
            from backend.domain.samba.proxy.snkrdunk import SnkrdunkClient

            client = SnkrdunkClient()

        # 확장앱 소싱큐 기반 사이트 — 소싱큐로 검색 요청
        if not client:
            from backend.domain.samba.proxy.sourcing_queue import (
                SourcingQueue,
                SITE_SEARCH_URLS,
            )

            if site not in SITE_SEARCH_URLS:
                await repo.fail_job(job.id, f"미지원 소싱처: {site}")
                return
            try:
                # sf.keyword가 이미 URL이면 SourcingQueue에 직접 전달 (템플릿 이중 치환 방지)
                # 상대 URL(/shop/...)도 절대 URL로 변환하여 전달
                _kw_raw = sf.keyword or ""
                if _kw_raw.startswith("http"):
                    _sq_url = _kw_raw
                elif _kw_raw.startswith("/"):
                    # 상대 URL → 소싱처 도메인 붙여서 절대 URL 변환
                    _site_domains = {
                        "GSShop": "https://www.gsshop.com",
                        "ABCmart": "https://www.a-rt.com",
                        "GrandStage": "https://www.a-rt.com",
                        "REXMONDE": "https://www.okmall.com",
                        "ElandMall": "https://www.elandmall.com",
                        "SSF": "https://www.ssfshop.com",
                        "SSG": "https://www.ssg.com",
                    }
                    _domain = _site_domains.get(site, "")
                    _sq_url = f"{_domain}{_kw_raw}" if _domain else ""
                else:
                    _sq_url = ""
                _req_id, _future = SourcingQueue.add_search_job(
                    site, keyword, url=_sq_url
                )
                ext_result = await asyncio.wait_for(_future, timeout=180)
                items_list = ext_result.get("products", [])
                logger.info(
                    f"[잡워커] {site} 확장앱 검색 '{keyword}' → {len(items_list)}건"
                )
            except asyncio.TimeoutError:
                SourcingQueue.resolvers.pop(_req_id, None)
                await repo.fail_job(
                    job.id, "확장앱 응답 타임아웃. 확장앱이 실행 중인지 확인하세요."
                )
                return
            except Exception as e:
                await repo.fail_job(job.id, f"확장앱 검색 실패: {e}")
                return
            # 확장앱 결과는 검색 API와 동일 포맷으로 처리 (아래 중복필터+저장 로직 공유)
            result = {"products": items_list, "total": len(items_list)}

        else:
            # 직접 API 검색
            # LOTTEON: 두 가지 모드 지원
            #   1) 서브키워드 모드 (신): q="{브랜드} {카테고리}" 형태 (공백 포함)
            #      → qapi 2,100 상한을 카테고리 단위로 회피
            #   2) 브랜드별 모드 (구/하위호환): brands 파라미터로 각 브랜드 개별 검색
            _per_brand_keywords: list[str] = []
            _use_subkw_mode = False
            if site == "LOTTEON":
                try:
                    parsed_kw = urlparse(sf.keyword or "")
                    if parsed_kw.scheme:
                        _qs_kw = parse_qs(parsed_kw.query)
                        _q_val = _qs_kw.get("q", [""])[0]
                        _bp = _qs_kw.get("brands", [""])[0]
                        if _q_val and " " in _q_val:
                            _use_subkw_mode = True
                            _per_brand_keywords = [_q_val]
                        elif _bp:
                            _per_brand_keywords = [
                                b.strip() for b in _bp.split(",") if b.strip()
                            ]
                except Exception as exc:
                    logger.warning(
                        f"[잡워커] LOTTEON 브랜드/서브키워드 파라미터 파싱 실패: {exc}"
                    )

            try:
                if _per_brand_keywords:
                    items_list = []
                    seen_pids: set[str] = set()
                    # LOTTEON 전수 페이징: 브랜드당 qapi 상한 2,100건 전체 수집
                    # BC코드 사후 필터링 특성상 수집 모수가 많아야 실제 카테고리 상품 확보 가능
                    # (search() 내부에서 _MAX_QAPI_OFFSET=2100 하드캡 처리 중)
                    per_max = (
                        2100
                        if (site == "LOTTEON" and sf.category_filter)
                        else max(remaining * 2, 100)
                    )
                    for _kw in _per_brand_keywords:
                        try:
                            _r = await client.search(
                                _kw, max_count=per_max, **_search_kwargs
                            )
                            _items = _r.get("products", [])
                            for _it in _items:
                                _pid = str(_it.get("site_product_id", ""))
                                if _pid and _pid in seen_pids:
                                    continue
                                if _pid:
                                    seen_pids.add(_pid)
                                items_list.append(_it)
                            _mode = "서브키워드" if _use_subkw_mode else "브랜드별"
                            logger.info(
                                f"[잡워커] LOTTEON {_mode} 검색 '{_kw}' → {len(_items)}건"
                            )
                        except Exception as _be:
                            logger.warning(f"[잡워커] LOTTEON 검색 실패 '{_kw}': {_be}")
                    result = {"products": items_list, "total": len(items_list)}
                    _mode = "서브키워드" if _use_subkw_mode else "브랜드별"
                    logger.info(
                        f"[잡워커] LOTTEON {_mode} 검색 합계 → {len(items_list)}건"
                    )
                else:
                    # 카테고리필터가 있는 소싱처: 전체 검색 후 사후 필터링
                    # SSG: 검색 URL에 dispCtgId가 이미 포함되므로 상세 재검증 불필요
                    # 중복 제거 여유분 5건만 추가해서 검색
                    if site == "SSG" and sf.category_filter:
                        _max = remaining + 5
                    else:
                        _max = (
                            9999
                            if (
                                site in ("Nike", "ABCmart", "GSShop", "SSG", "LOTTEON")
                                and sf.category_filter
                            )
                            else max(remaining * 2, 100)
                        )
                    # 검색 캐시: 동일 브랜드 그룹 수집 시 전수 검색 1회만 실행
                    # ABCmart: DB 캐시 (다중 Cloud Run 인스턴스 공유), 나머지: 인메모리 캐시
                    # (module-level `import time as _time` 재활용 — 지역 재import 시 함수 전체의 _time이
                    # 로컬로 shadow되어 상단 _time.time() 호출이 UnboundLocalError 발생)
                    _cache_key = (site, keyword)
                    _cached = self._search_cache.get(_cache_key)
                    _cache_ttl = 300  # 5분 (인메모리)
                    _abc_db_cache_hit = False

                    # ABCmart: DB 캐시 우선 조회 (인스턴스 간 공유)
                    if (
                        site == "ABCmart"
                        and sf.category_filter
                        and not (_cached and _time.time() - _cached[1] < _cache_ttl)
                    ):
                        from backend.domain.samba.collector.model import (
                            SambaSearchCache as _SCache,
                        )
                        from datetime import timedelta as _td

                        _db_cache = await session.execute(
                            select(_SCache)
                            .where(
                                _SCache.source_site == site,
                                _SCache.keyword == keyword,
                                _SCache.created_at
                                > datetime.now(tz=timezone.utc) - _td(minutes=60),
                            )
                            .order_by(_SCache.created_at.desc())
                            .limit(1)
                        )
                        _db_cache_row = _db_cache.scalars().first()
                        if _db_cache_row and _db_cache_row.products:
                            items_list = list(_db_cache_row.products)
                            _abc_db_cache_hit = True
                            logger.info(
                                f"[잡워커] ABCmart DB 캐시 히트 '{keyword}' → {len(items_list)}건"
                            )
                            _add_job_log(
                                job.id,
                                f"{_dprefix} [{sf.name}] 검색 완료: {len(items_list):,}건 (캐시)",
                                job_type="collect",
                            )
                            # 인메모리 캐시에도 복사 (같은 인스턴스 내 후속 잡 최적화)
                            self._search_cache[_cache_key] = (
                                items_list,
                                _time.time(),
                            )

                    if not _abc_db_cache_hit and (
                        _cached
                        and _time.time() - _cached[1] < _cache_ttl
                        and (
                            site in ("Nike", "ABCmart", "GSShop", "LOTTEON")
                            and sf.category_filter
                        )
                    ):
                        items_list = list(_cached[0])
                        logger.info(
                            f"[잡워커] {site} 검색 캐시 히트 '{keyword}' → {len(items_list)}건"
                        )
                        _add_job_log(
                            job.id,
                            f"{_dprefix} [{sf.name}] 검색 완료: {len(items_list):,}건 (캐시)",
                            job_type="collect",
                        )
                    elif not _abc_db_cache_hit:
                        # GSShop: 원본 URL(카테고리 필터 포함) 전달
                        if site == "GSShop" and _original_url.startswith("http"):
                            _search_kwargs["url"] = _original_url
                        # ABCmart: ABC + GS 동시 검색 (로컬 테스트: 순차 8.4s → 병렬 6.0s)
                        if site == "ABCmart" and sf.category_filter:
                            # 검색 직전 취소 체크 (병렬 검색 6초 걸림)
                            from backend.domain.samba.emergency import (
                                clear_collect_cancel as _clear_cc2,
                                is_collect_cancel_requested as _is_cc2,
                                is_emergency_stopped as _is_es2,
                            )

                            if _is_cc2() or _is_es2():
                                logger.info(f"[잡워커] {site} 검색 취소: {job.id}")
                                _clear_cc2()
                                return
                            from backend.domain.samba.collector.refresher import (
                                get_collect_proxies,
                            )
                            from backend.domain.samba.proxy.abcmart import (
                                ARTSourcingClient as _ART,
                            )

                            # GrandStage도 DB 설정 페이지의 collect 프록시 풀 공유 (a-rt.com 차단 우회)
                            _gs_proxies2 = get_collect_proxies()
                            _gs = _ART("10002", proxy_pool=_gs_proxies2 or None)
                            # 프로덕션(Cloud Run IP)에서 a-rt.com이 응답을 씹는 경우 대비 120초 가드
                            try:
                                _abc_res, _gs_res = await asyncio.wait_for(
                                    asyncio.gather(
                                        client.search(
                                            keyword,
                                            max_count=_max,
                                            **_search_kwargs,
                                        ),
                                        _gs.search(
                                            keyword,
                                            max_count=_max,
                                            **_search_kwargs,
                                        ),
                                        return_exceptions=True,
                                    ),
                                    timeout=120,
                                )
                            except asyncio.TimeoutError:
                                logger.warning(
                                    f"[잡워커] ABCmart+GS 검색 120초 타임아웃: {keyword}"
                                )
                                await repo.fail_job(
                                    job.id,
                                    "ABCmart 응답 지연 (120초 타임아웃) — a-rt.com 응답 없음",
                                )
                                return
                            # gather 내부 예외는 개별적으로 처리
                            if isinstance(_abc_res, Exception):
                                logger.warning(
                                    f"[잡워커] ABCmart 검색 예외: {_abc_res}"
                                )
                                _abc_res = {"products": [], "total": 0}
                            if isinstance(_gs_res, Exception):
                                logger.warning(
                                    f"[잡워커] GrandStage 검색 예외: {_gs_res}"
                                )
                                _gs_res = {"products": [], "total": 0}
                            result = _abc_res
                            items_list = result.get("products", [])
                            _gs_products = _gs_res.get("products", [])
                            if _gs_products:
                                _seen = {
                                    p.get("site_product_id", "")
                                    for p in items_list
                                    if p.get("site_product_id")
                                }
                                for p in _gs_products:
                                    pid = p.get("site_product_id", "")
                                    if pid and pid not in _seen:
                                        _seen.add(pid)
                                        items_list.append(p)
                                logger.info(
                                    f"[잡워커] ABCmart+GS 병렬 병합: ABC {len(result.get('products', []))}건 "
                                    f"+ GS {len(_gs_products)}건 → 총 {len(items_list)}건"
                                )
                        else:
                            # 단일 검색에도 120초 가드 — 카테고리필터 없는 경로 hang 방지
                            try:
                                result = await asyncio.wait_for(
                                    client.search(
                                        keyword, max_count=_max, **_search_kwargs
                                    ),
                                    timeout=120,
                                )
                            except asyncio.TimeoutError:
                                logger.warning(
                                    f"[잡워커] {site} 검색 120초 타임아웃: {keyword}"
                                )
                                await repo.fail_job(
                                    job.id,
                                    f"{site} 검색 응답 지연 (120초 타임아웃)",
                                )
                                return
                            items_list = result.get("products", [])
                        logger.info(
                            f"[잡워커] {site} 검색 '{keyword}' → {len(items_list)}건"
                        )
                        _add_job_log(
                            job.id,
                            f"{_dprefix} [{sf.name}] 검색 완료: {len(items_list):,}건",
                            job_type="collect",
                        )
                        # 전수 검색 결과 캐시 저장
                        # ABCmart: GS 병합이 실패(0개)한 경우 캐시 저장 금지
                        # → GS 실패 캐시가 전파되어 이후 모든 SF 잡이 GS 아이템 누락하는 현상 방지
                        _abc_only_count = len(result.get("products", []))
                        _gs_merged_count = len(items_list) - _abc_only_count
                        _gs_was_attempted = site == "ABCmart" and sf.category_filter
                        _cache_ok = not _gs_was_attempted or _gs_merged_count > 0
                        if not _cache_ok:
                            logger.warning(
                                f"[잡워커] ABCmart GS 검색 실패로 캐시 저장 스킵 "
                                f"(ABC {_abc_only_count}건, GS 0건) — 다음 잡에서 재시도"
                            )
                        if (
                            (
                                site in ("Nike", "ABCmart", "GSShop", "LOTTEON")
                                and sf.category_filter
                            )
                            and items_list
                            and _cache_ok
                        ):
                            self._search_cache[_cache_key] = (
                                items_list,
                                _time.time(),
                            )
                            # ABCmart: DB 캐시에도 저장 (다중 인스턴스 공유)
                            # 기존 항목을 먼저 삭제하고 새로 저장 (stale 데이터 방지)
                            if site == "ABCmart":
                                from backend.domain.samba.collector.model import (
                                    SambaSearchCache as _SCache,
                                )
                                from sqlalchemy import delete as _sa_delete

                                _cache_data = {
                                    "id": generate_search_cache_id(),
                                    "tenant_id": getattr(sf, "tenant_id", None),
                                    "source_site": site,
                                    "keyword": keyword,
                                    "products": items_list,
                                    "ttl_minutes": 60,
                                    "created_at": datetime.now(tz=timezone.utc),
                                }
                                try:
                                    # 동일 (source_site, keyword) 기존 캐시 전부 삭제
                                    await session.execute(
                                        _sa_delete(_SCache).where(
                                            _SCache.source_site == site,
                                            _SCache.keyword == keyword,
                                        )
                                    )
                                    session.add(_SCache(**_cache_data))
                                    await session.flush()
                                    logger.info(
                                        f"[잡워커] ABCmart DB 캐시 갱신: '{keyword}' {len(items_list)}건"
                                    )
                                except Exception as _ce:
                                    await session.rollback()
                                    logger.warning(
                                        f"[잡워커] ABCmart DB 캐시 저장 실패 (무시): {_ce}"
                                    )
            except Exception as e:
                await repo.fail_job(job.id, f"검색 실패: {e}")
                return

        # Nike: category_filter("성별_세분류")로 검색 결과 사후 필터링
        if site == "Nike" and sf.category_filter:
            # "남성_러닝화" → c2="남성", c3="러닝화"
            # "가방" (언더스코어 없음) → c2="", c3="가방" (성별 없는 카테고리)
            _parts = sf.category_filter.split("_", 1)
            if len(_parts) == 2:
                _filter_c2, _filter_c3 = _parts[0], _parts[1]
            else:
                # 언더스코어 없으면 세분류만 (성별 없는 카테고리: 가방, 모자, 양말 등)
                _filter_c2, _filter_c3 = "", _parts[0]
            before = len(items_list)
            filtered = []
            for item in items_list:
                ic2 = item.get("category2", "")
                ic3 = item.get("category3", "")
                # 성별+세분류 모두 일치해야 통과
                if _filter_c2 and ic2 != _filter_c2:
                    continue
                if _filter_c3 and ic3 != _filter_c3:
                    continue
                filtered.append(item)
            items_list = filtered
            logger.info(
                f"[잡워커] Nike 카테고리 필터 {sf.category_filter}: {before}→{len(items_list)}건"
            )

        # ABCmart: category_filter(카테고리 코드+이름) 로 검색 결과 사후 필터링
        # ABC-MART/GrandStage 카테고리 코드가 채널별로 다르므로
        # 코드 매칭 + 카테고리명(path) 매칭 병행
        if site == "ABCmart" and sf.category_filter:
            # [DIAG] 필터 진입 전 source_site × category_code 분포 확인
            from collections import Counter as _Ctr  # noqa: PLC0415

            _diag = _Ctr(
                (
                    item.get("source_site", ""),
                    item.get("category_code", "") or "(empty)",
                )
                for item in items_list
            )
            logger.info(
                f"[잡워커][DIAG] ABCmart filter 진입 sf.category_filter={sf.category_filter!r} "
                f"items={len(items_list)} 분포 TOP10={_diag.most_common(10)}"
            )
            _gs_samples = [
                (item.get("category_code", ""), item.get("category", ""))
                for item in items_list
                if item.get("source_site") == "GrandStage"
            ][:3]
            logger.info(f"[잡워커][DIAG] GS 샘플: {_gs_samples}")
        if site == "ABCmart" and sf.category_filter:
            before = len(items_list)
            # ABC-MART 코드에 대응하는 카테고리 이름(path) 수집
            _target_cat_names: set[str] = set()
            for item in items_list:
                if (item.get("category_code") or "") == sf.category_filter:
                    _cn = item.get("category") or ""
                    if _cn:
                        _target_cat_names.add(_cn)
            # 코드 일치 OR 같은 카테고리명의 GS 상품 포함
            items_list = [
                item
                for item in items_list
                if (item.get("category_code") or "") == sf.category_filter
                or (item.get("category") or "") in _target_cat_names
            ]
            logger.info(
                f"[잡워커] ABCmart 카테고리 필터 {sf.category_filter}: {before}→{len(items_list)}건"
                f" (카테고리명 매칭: {_target_cat_names})"
            )

        # LOTTEON: category_filter(BC코드, 콤마 구분)로 검색 결과 사후 필터링
        if site == "LOTTEON" and sf.category_filter:
            bc_set = set(sf.category_filter.split(","))
            before = len(items_list)
            items_list = [
                item for item in items_list if (item.get("scat_no") or "") in bc_set
            ]
            logger.info(
                f"[잡워커] LOTTEON BC코드 필터 {sf.category_filter}: {before}→{len(items_list)}건"
            )

        # LOTTEON: 선택된 브랜드 목록으로 정확 일치 필터링
        # URL 파라미터 brands=나이키,나이키 키즈 형태 (콤마 구분)
        # brands 파라미터 없으면 keyword 단일 브랜드로 사용 (하위 호환)
        if site == "LOTTEON":
            from backend.domain.samba.proxy.lotteon_sourcing import _filter_by_brands

            _selected_brands: list[str] = []
            try:
                parsed2 = urlparse(sf.keyword or "")
                if parsed2.scheme:
                    _qs2 = parse_qs(parsed2.query)
                    _brands_param = _qs2.get("brands", [""])[0]
                    if _brands_param:
                        _selected_brands = [
                            b.strip() for b in _brands_param.split(",") if b.strip()
                        ]
            except Exception as exc:
                logger.warning(f"[잡워커] LOTTEON 브랜드 필터 파싱 실패: {exc}")

            if not _selected_brands and keyword:
                _selected_brands = [keyword]

            if _selected_brands:
                before = len(items_list)
                items_list = _filter_by_brands(items_list, _selected_brands)
                if before != len(items_list):
                    logger.info(
                        f"[잡워커] LOTTEON 브랜드 필터 {_selected_brands}: {before}→{len(items_list)}건"
                    )

        await repo.update_progress(job.id, 0, remaining)

        # 카테고리 매핑 (패션플러스)
        # URL의 categoryName 파라미터 우선 사용 — _CATEGORY_MAP은 ID와 이름이 불일치할 수 있음
        _category1_name = ""
        _fp_cat1 = ""
        _fp_cat2 = ""
        _fp_cat3 = ""
        # SSG: ctgPath URL 파라미터에서 전시카테고리 전체 경로 복원
        _ssg_cat = ""
        _ssg_cat1 = ""
        _ssg_cat2 = ""
        _ssg_cat3 = ""
        _ssg_cat4 = ""
        if site == "SSG":
            _ctg_path_ssg = _search_kwargs.get("ctgPath", "")
            if _ctg_path_ssg:
                _ssg_parts = _ctg_path_ssg.split(" > ")
                _ssg_cat = _ctg_path_ssg
                _ssg_cat1 = _ssg_parts[0] if _ssg_parts else ""
                _ssg_cat2 = _ssg_parts[1] if len(_ssg_parts) > 1 else ""
                _ssg_cat3 = _ssg_parts[2] if len(_ssg_parts) > 2 else ""
                _ssg_cat4 = _ssg_parts[3] if len(_ssg_parts) > 3 else ""
                logger.debug(f"[잡워커] SSG ctgPath 카테고리: {_ssg_cat}")
        if site == "FashionPlus":
            _fp_cat1 = qs.get("category1Name", [""])[0]
            _fp_cat2 = qs.get("category2Name", [""])[0]
            _fp_cat3 = qs.get("category3Name", [""])[0]
            _fp_path_parts = [n for n in [_fp_cat1, _fp_cat2, _fp_cat3] if n]
            if _fp_path_parts:
                # URL에 이름 파라미터가 있으면 경로 재구성 (예: "잡화 > 가방 > 백팩")
                _category1_name = " > ".join(_fp_path_parts)
            elif _search_kwargs.get("category1Id"):
                # 구 URL(이름 파라미터 없음) 폴백 — _CATEGORY_MAP 사용
                from backend.domain.samba.proxy.fashionplus import _CATEGORY_MAP

                _category1_name = _CATEGORY_MAP.get(_search_kwargs["category1Id"], "")

        # 중복 필터링
        candidate_ids = [
            str(item.get("site_product_id", ""))
            for item in items_list
            if item.get("site_product_id")
        ]
        existing_ids: set[str] = set()
        if candidate_ids:
            existing_result = await session.execute(
                select(CPModel.site_product_id).where(
                    CPModel.source_site == site,
                    CPModel.site_product_id.in_(candidate_ids),
                )
            )
            existing_ids = {row[0] for row in existing_result.all()}

        svc = _get_services(session)
        total_saved = 0

        # LOTTEON: 저장 전 10건 병렬로 상세 정보 선취합 (1단계 통합 수집)
        _lotteon_details: dict[str, dict[str, Any]] = {}
        if site == "LOTTEON" and client:
            # LOTTEON 서브키워드 모드: 다른 필터에 이미 수집된 상품도 현재 필터로 소유권 교체
            # (create_collected_product의 IntegrityError upsert 경로가 search_filter_id 갱신)
            _lt_takeover = bool(locals().get("_use_subkw_mode", False))
            _skip_ids = set() if _lt_takeover else existing_ids
            # 중복 제외한 신규 상품만 상세 조회
            new_items = [
                it
                for it in items_list
                if str(it.get("site_product_id", "")) not in _skip_ids
            ][:remaining]
            if new_items:
                logger.info(
                    f"[잡워커] LOTTEON 상세 선취합 시작: {len(new_items)}건 (10건 병렬)"
                )
                BATCH_SIZE = 10
                for batch_start in range(0, len(new_items), BATCH_SIZE):
                    from backend.domain.samba.emergency import (
                        is_collect_cancel_requested as _icc_lt,
                        is_emergency_stopped as _ies_lt,
                    )

                    if _icc_lt() or _ies_lt() or await repo.is_cancelled(job.id):
                        await repo.cancel_job(job.id)
                        await session.commit()
                        return
                    batch = new_items[batch_start : batch_start + BATCH_SIZE]
                    details = await asyncio.gather(
                        *(
                            client.get_detail(str(it.get("site_product_id", "")))
                            for it in batch
                        ),
                        return_exceptions=True,
                    )
                    for it, det in zip(batch, details):
                        pid = str(it.get("site_product_id", ""))
                        if isinstance(det, Exception):
                            logger.warning(
                                f"[잡워커] LOTTEON 상세 선취합 실패 {pid}: {det}"
                            )
                            continue
                        if det:
                            _lotteon_details[pid] = det
                    done = min(batch_start + BATCH_SIZE, len(new_items))
                    await repo.update_progress(job.id, done, len(new_items))
                    logger.info(
                        f"[잡워커] LOTTEON 상세 선취합 [{done}/{len(new_items)}]"
                    )
                    _add_job_log(
                        job.id,
                        f"[{site}] [{sf.name}] 상세 조회 [{done:,}/{len(new_items):,}]",
                        job_type="collect",
                    )
                    await asyncio.sleep(0.3)
                logger.info(
                    f"[잡워커] LOTTEON 상세 선취합 완료: {len(_lotteon_details)}/{len(new_items)}건 성공"
                )

        # Nike: 저장 전 10건 병렬로 상세 정보 선취합
        _nike_details: dict[str, dict[str, Any]] = {}
        if site == "Nike" and client:
            new_items = [
                it
                for it in items_list
                if str(it.get("site_product_id", "")) not in existing_ids
            ][:remaining]
            if new_items:
                logger.info(
                    f"[잡워커] Nike 상세 선취합 시작: {len(new_items)}건 (10건 병렬)"
                )
                _NK_BATCH = 10
                for batch_start in range(0, len(new_items), _NK_BATCH):
                    from backend.domain.samba.emergency import (
                        is_collect_cancel_requested as _icc_nk,
                        is_emergency_stopped as _ies_nk,
                    )

                    if _icc_nk() or _ies_nk() or await repo.is_cancelled(job.id):
                        await repo.cancel_job(job.id)
                        await session.commit()
                        return
                    batch = new_items[batch_start : batch_start + _NK_BATCH]
                    details = await asyncio.gather(
                        *(
                            client.get_detail(
                                str(it.get("site_product_id", "")),
                                pdp_url=it.get("url") or it.get("source_url"),
                                base_info=it,
                            )
                            for it in batch
                        ),
                        return_exceptions=True,
                    )
                    for it, det in zip(batch, details):
                        pid = str(it.get("site_product_id", ""))
                        if isinstance(det, Exception):
                            logger.warning(
                                f"[잡워커] Nike 상세 선취합 실패 {pid}: {det}"
                            )
                            continue
                        if det:
                            _nike_details[pid] = det
                    done = min(batch_start + _NK_BATCH, len(new_items))
                    await repo.update_progress(job.id, done, len(new_items))
                    logger.info(f"[잡워커] Nike 상세 선취합 [{done}/{len(new_items)}]")
                    _add_job_log(
                        job.id,
                        f"[{site}] [{sf.name}] 상세 조회 [{done:,}/{len(new_items):,}]",
                        job_type="collect",
                    )
                    await asyncio.sleep(0.15)
                logger.info(
                    f"[잡워커] Nike 상세 선취합 완료: {len(_nike_details)}/{len(new_items)}건 성공"
                )

        # GSShop: 선취합 + 카테고리 필터 (검색 결과에 이름/카테고리 없으므로 상세 조회 필수)
        _gsshop_details: dict[str, dict[str, Any]] = {}
        if site == "GSShop" and client:
            new_items = [
                it
                for it in items_list
                if str(it.get("site_product_id", "")) not in existing_ids
            ][:remaining]
            if new_items:
                logger.info(
                    f"[잡워커] GSShop 상세 선취합 시작: {len(new_items)}건 (20건 병렬)"
                )
                _GS_BATCH = 20
                _gs_cat_filter = sf.category_filter or ""
                # 카테고리 필터: "카테고리명" 또는 "대>중>소" 형태
                _gs_filter_parts = [
                    p.strip()
                    for p in _gs_cat_filter.replace(" > ", "_").split("_")
                    if p.strip()
                ]
                for batch_start in range(0, len(new_items), _GS_BATCH):
                    from backend.domain.samba.emergency import (
                        is_collect_cancel_requested as _icc_gs,
                        is_emergency_stopped as _ies_gs,
                    )

                    if _icc_gs() or _ies_gs() or await repo.is_cancelled(job.id):
                        await repo.cancel_job(job.id)
                        await session.commit()
                        return
                    batch = new_items[batch_start : batch_start + _GS_BATCH]
                    details = await asyncio.gather(
                        *(
                            client.get_detail(str(it.get("site_product_id", "")))
                            for it in batch
                        ),
                        return_exceptions=True,
                    )
                    for it, det in zip(batch, details):
                        pid = str(it.get("site_product_id", ""))
                        if isinstance(det, Exception):
                            logger.debug(
                                f"[잡워커] GSShop 상세 선취합 실패 {pid}: {det}"
                            )
                            continue
                        if not det or not det.get("name"):
                            continue
                        # 카테고리 필터 적용
                        if _gs_filter_parts:
                            _det_cats = [
                                det.get("category1", ""),
                                det.get("category2", ""),
                                det.get("category3", ""),
                                det.get("category4", ""),
                            ]
                            _det_cat_str = " ".join(c for c in _det_cats if c).lower()
                            _matched = all(
                                fp.lower() in _det_cat_str for fp in _gs_filter_parts
                            )
                            if not _matched:
                                continue
                        _gsshop_details[pid] = det
                    done = min(batch_start + _GS_BATCH, len(new_items))
                    await repo.update_progress(job.id, done, len(new_items))
                    logger.info(
                        f"[잡워커] GSShop 상세 선취합 [{done}/{len(new_items)}]"
                        f" 카테고리 통과: {len(_gsshop_details)}건"
                    )
                    _add_job_log(
                        job.id,
                        f"[{site}] [{sf.name}] 상세 조회 [{done:,}/{len(new_items):,}]",
                        job_type="collect",
                    )
                logger.info(
                    f"[잡워커] GSShop 상세 선취합 완료:"
                    f" {len(_gsshop_details)}/{len(new_items)}건"
                    f" (카테고리 필터: {_gs_cat_filter or '없음'})"
                )
            # GSShop: 선취합 결과로 items_list 교체 (카테고리 통과 상품만)
            if _gsshop_details:
                items_list = [
                    it
                    for it in items_list
                    if str(it.get("site_product_id", "")) in _gsshop_details
                ]

        # SSG: 저장 전 상세 정보 선취합 (카테고리/원가/고시정보 보충 필수)
        _ssg_details: dict[str, dict[str, Any]] = {}
        if site == "SSG" and client:
            _ssg_cat_filter = sf.category_filter or None
            new_items = [
                it
                for it in items_list
                if str(it.get("site_product_id", "")) not in existing_ids
            ]
            # 카테고리 필터 유무 관계없이 remaining개로 제한
            # SSG 검색 URL에 dispCtgId가 이미 포함되므로 추가 재검증 불필요
            new_items = new_items[:remaining]
            if new_items:
                logger.info(
                    f"[잡워커] SSG 상세 선취합 시작: {len(new_items)}건 (페이지 순서 기준)"
                    + (
                        f" | 카테고리 필터: {_ssg_cat_filter}"
                        if _ssg_cat_filter
                        else ""
                    )
                )
                _add_job_log(
                    job.id,
                    f"[{site}] [{sf.name}] 상세 조회 시작: {len(new_items):,}건",
                    job_type="collect",
                )
                # 확장앱 소싱큐 위임 (SSG 서버사이드 차단 우회)
                from backend.domain.samba.proxy.sourcing_queue import (
                    SourcingQueue as _SSGQueue,
                )

                from backend.domain.samba.emergency import (
                    is_collect_cancel_requested as _icc_ssg,
                    is_emergency_stopped as _ies_ssg,
                )

                _SSG_PREFETCH_BATCH = 5
                _ssg_done = 0
                for _pb_i in range(0, len(new_items), _SSG_PREFETCH_BATCH):
                    if _icc_ssg() or _ies_ssg() or await repo.is_cancelled(job.id):
                        await repo.cancel_job(job.id)
                        await session.commit()
                        return
                    _pb_batch = new_items[_pb_i : _pb_i + _SSG_PREFETCH_BATCH]
                    _pb_futs = [
                        _SSGQueue.add_detail_job(
                            "SSG", str(_pb_it.get("site_product_id", ""))
                        )
                        for _pb_it in _pb_batch
                    ]
                    _pb_results = await asyncio.gather(
                        *[asyncio.wait_for(f, timeout=45) for _, f in _pb_futs],
                        return_exceptions=True,
                    )
                    for _pb_it, _ext_result in zip(_pb_batch, _pb_results):
                        spid = str(_pb_it.get("site_product_id", ""))
                        det: dict = {}
                        if isinstance(_ext_result, asyncio.TimeoutError):
                            _add_job_log(
                                job.id,
                                f"[SSG] 상세 타임아웃: {spid} (확장앱 미응답)",
                                job_type="collect",
                            )
                        elif isinstance(_ext_result, Exception):
                            logger.debug(
                                f"[SSG] 확장앱 상세 실패: {spid} — {_ext_result}"
                            )
                        elif isinstance(_ext_result, dict) and _ext_result.get(
                            "success"
                        ):
                            _html = _ext_result.get("html", "")
                            _s_dom_bc = _ext_result.get("domBreadcrumb", []) or []
                            if _html:
                                _s_loop = asyncio.get_event_loop()
                                det = await _s_loop.run_in_executor(
                                    None,
                                    lambda: (
                                        client._parse_result_item_obj(
                                            _html,
                                            spid,
                                            False,
                                            dom_breadcrumb=_s_dom_bc,
                                        )
                                        or {}
                                    ),
                                )
                                if not det:
                                    _ext_obj2 = _ext_result.get("resultItemObj", {})
                                    _nm2 = _ext_obj2.get("itemNm", "")
                                    if _nm2:
                                        _opts2 = await _s_loop.run_in_executor(
                                            None,
                                            lambda: (
                                                client._parse_layered_select_options(
                                                    _html
                                                )
                                            ),
                                        )
                                        det = {
                                            "itemNm": _nm2,
                                            "name": _nm2,
                                            "options": _opts2,
                                            "soldOut": "Y"
                                            if _opts2
                                            and all(
                                                o.get("isSoldOut", False)
                                                for o in _opts2
                                            )
                                            else "N",
                                        }
                                # 확장앱 uitemOptions(AJAX 후 실제 재고)로 품절 상태 보정
                                _uitem_opts2 = _ext_result.get("uitemOptions", [])
                                if _uitem_opts2 and det.get("options"):
                                    _soldout_nm2 = {
                                        o["name"]
                                        for o in _uitem_opts2
                                        if o.get("isSoldOut")
                                    }
                                    if _soldout_nm2:
                                        for _o2 in det["options"]:
                                            if _o2.get("name") in _soldout_nm2:
                                                _o2["isSoldOut"] = True
                                                _o2["stock"] = 0
                                if det:
                                    _ssg_details[spid] = det
                        _ssg_done += 1
                    await repo.update_progress(job.id, _ssg_done, len(new_items))
                    _add_job_log(
                        job.id,
                        f"[{site}] [{sf.name}] 상세 조회 [{_ssg_done:,}/{len(new_items):,}]",
                        job_type="collect",
                    )
                    logger.info(
                        f"[잡워커] SSG 상세 선취합 [{_ssg_done}/{len(new_items)}]"
                    )

                logger.info(
                    f"[잡워커] SSG 상세 선취합 완료: {len(_ssg_details)}/{len(new_items)}건"
                )
                _add_job_log(
                    job.id,
                    f"[{site}] [{sf.name}] 상세 조회 완료: {len(_ssg_details):,}건",
                    job_type="collect",
                )
            # 상세 조회 성공한 상품만 저장 대상으로 사용 (없으면 검색 결과 그대로 사용)
            if _ssg_details:
                items_list = [
                    it
                    for it in items_list
                    if str(it.get("site_product_id", "")) in _ssg_details
                ]
            else:
                items_list = new_items

        # ABCmart/GrandStage: 저장 전 3건 병렬 선취합 (세션 배치 공유로 속도 향상)
        # LOTTEON(10건)/Nike(10건)/GSShop(20건)과 동일 패턴
        # a-rt.com 차단 방지: 3건 병렬 + 배치 간 0.5초 딜레이
        _abc_details: dict[str, dict[str, Any]] = {}
        if (
            site in ("ABCmart", "GrandStage")
            and client
            and hasattr(client, "get_detail")
        ):
            _new_items_abc = [
                it
                for it in items_list
                if str(it.get("site_product_id", "")) not in existing_ids
            ][:remaining]
            if _new_items_abc:
                _ABC_BATCH = 3
                logger.info(
                    f"[잡워커] {site} 선취합 시작: {len(_new_items_abc)}건 ({_ABC_BATCH}건 병렬)"
                )
                _add_job_log(
                    job.id,
                    f"[{site}] [{sf.name}] 상세 조회 시작: {len(_new_items_abc):,}건",
                    job_type="collect",
                )
                # 배치 단위로 세션 1개 획득 → 배치 내 모든 항목이 동일 JSESSIONID 재사용
                for _batch_start in range(0, len(_new_items_abc), _ABC_BATCH):
                    # 배치 시작 전 취소 체크 (배치당 3~5초 걸림)
                    from backend.domain.samba.emergency import (
                        clear_collect_cancel as _clear_cc,
                        is_collect_cancel_requested as _is_cc,
                        is_emergency_stopped as _is_es,
                    )

                    if _is_cc() or _is_es():
                        logger.info(f"[잡워커] {site} 선취합 취소: {job.id}")
                        try:
                            await repo.cancel_job(job.id)
                            await session.commit()
                        except Exception as _e:
                            logger.warning(
                                f"[잡워커] 취소 상태 저장 실패: {job.id} — {_e}"
                            )
                        _add_job_log(
                            job.id, f"[{site}] 수집 취소됨", job_type="collect"
                        )
                        _clear_cc()
                        return
                    _batch = _new_items_abc[_batch_start : _batch_start + _ABC_BATCH]
                    # 배치 전체가 공유할 세션 1개 획득
                    _batch_session = None
                    try:
                        _first_pid = str(_batch[0].get("site_product_id", ""))
                        _batch_session = await client._acquire_session_client(
                            _first_pid
                        )
                    except Exception as _se:
                        logger.warning(f"[잡워커] {site} 배치 세션 획득 실패: {_se!r}")
                    try:
                        _batch_details = await asyncio.gather(
                            *(
                                client.get_detail(
                                    str(it.get("site_product_id", "")),
                                    shared_client=_batch_session,
                                )
                                for it in _batch
                            ),
                            return_exceptions=True,
                        )
                    finally:
                        if _batch_session is not None:
                            try:
                                await _batch_session.aclose()
                            except Exception:
                                pass
                    for it, det in zip(_batch, _batch_details):
                        pid = str(it.get("site_product_id", ""))
                        if isinstance(det, Exception):
                            logger.warning(
                                f"[잡워커] {site} 선취합 실패 {pid}: {det!r}"
                            )
                            continue
                        if det:
                            _abc_details[pid] = det
                    _done_abc = min(_batch_start + _ABC_BATCH, len(_new_items_abc))
                    await repo.update_progress(job.id, _done_abc, len(_new_items_abc))
                    _add_job_log(
                        job.id,
                        f"[{site}] [{sf.name}] 상세 조회 [{_done_abc:,}/{len(_new_items_abc):,}]",
                        job_type="collect",
                    )
                    # 마지막 배치 제외 딜레이 (차단 방지)
                    if _batch_start + _ABC_BATCH < len(_new_items_abc):
                        await asyncio.sleep(0.5)
                logger.info(
                    f"[잡워커] {site} 선취합 완료: {len(_abc_details)}/{len(_new_items_abc)}건"
                )
                _add_job_log(
                    job.id,
                    f"[{site}] [{sf.name}] 상세 조회 완료: {len(_abc_details):,}건",
                    job_type="collect",
                )

        _collected_sold_out = 0
        _cancel_check_counter = 0
        for item in items_list:
            if total_saved >= remaining:
                break

            # 취소 확인 — 인메모리 플래그는 매 아이템, DB는 5건 단위
            from backend.domain.samba.emergency import (
                clear_collect_cancel,
                is_collect_cancel_requested,
                is_emergency_stopped,
            )

            if is_collect_cancel_requested() or is_emergency_stopped():
                logger.info(f"[잡워커] {site} 수집 취소됨: {job.id}")
                try:
                    await repo.cancel_job(job.id)
                    await session.commit()
                except Exception as _e:
                    logger.warning(f"[잡워커] 취소 상태 저장 실패: {job.id} — {_e}")
                _add_job_log(job.id, f"[{site}] 수집 취소됨", job_type="collect")
                clear_collect_cancel()
                return

            _cancel_check_counter += 1
            if _cancel_check_counter % 5 == 1:
                if await repo.is_cancelled(job.id):
                    logger.info(f"[잡워커] {site} 수집 취소됨: {job.id}")
                    try:
                        await repo.cancel_job(job.id)
                        await session.commit()
                    except Exception as _e:
                        logger.warning(f"[잡워커] 취소 상태 저장 실패: {job.id} — {_e}")
                    _add_job_log(job.id, f"[{site}] 수집 취소됨", job_type="collect")
                    return

            p_id = str(item.get("site_product_id", ""))
            # LOTTEON 서브키워드 모드: 타 필터 보유 상품도 현재 필터로 소유권 교체
            # (아래 create_collected_product의 upsert 경로가 search_filter_id 갱신)
            if p_id in existing_ids and not locals().get("_use_subkw_mode", False):
                continue

            # 품절 필터링
            _item_sold_out = item.get("is_sold_out", False) or item.get(
                "isSoldOut", False
            )
            if _item_sold_out:
                if not _include_sold_out:
                    continue
                _collected_sold_out += 1

            p_name = item.get("name", "")
            sale_price = int(item.get("sale_price", 0))
            original_price = int(item.get("original_price", 0)) or sale_price
            if not p_name and not sale_price:
                continue

            # LOTTEON: search 결과의 scat_no로 카테고리 미리 매핑
            _lotteon_cat = ""
            _lotteon_cat1 = ""
            _lotteon_cat2 = ""
            _lotteon_cat3 = ""
            _lotteon_cat4 = ""
            _lotteon_scat_no = ""
            if site == "LOTTEON":
                from backend.domain.samba.proxy.lotteon_sourcing import (
                    _LOTTEON_SCAT_NAMES,
                )

                _lotteon_scat_no = item.get("scat_no") or item.get("scatNo") or ""
                if _lotteon_scat_no:
                    _cat_name = _LOTTEON_SCAT_NAMES.get(_lotteon_scat_no, "")
                    if _cat_name:
                        _lotteon_cat = _cat_name
                        _parts = _cat_name.split(" > ")
                        _lotteon_cat1 = _parts[0] if len(_parts) > 0 else ""
                        _lotteon_cat2 = _parts[1] if len(_parts) > 1 else ""
                        _lotteon_cat3 = _parts[2] if len(_parts) > 2 else ""
                        _lotteon_cat4 = _parts[3] if len(_parts) > 3 else ""

            # 상세 페이지에서 추가 이미지/고시정보 보충
            detail = {}
            # LOTTEON: 선취합된 상세 데이터 사용
            if site == "LOTTEON" and p_id in _lotteon_details:
                detail = _lotteon_details[p_id]
            # Nike: 선취합된 상세 데이터 사용
            if site == "Nike" and p_id in _nike_details:
                detail = _nike_details[p_id]
            # GSShop: 선취합된 상세 데이터 사용
            if site == "GSShop" and p_id in _gsshop_details:
                detail = _gsshop_details[p_id]
            # SSG: 선취합된 상세 데이터 사용
            if site == "SSG" and p_id in _ssg_details:
                detail = _ssg_details[p_id]
            # ABCmart/GrandStage: 선취합된 상세 데이터 사용
            if site in ("ABCmart", "GrandStage") and p_id in _abc_details:
                detail = _abc_details[p_id]
            _skip_detail = _search_kwargs.get("_skip_detail", False)
            # ABCmart 최대혜택가: 선취합 미스 시 폴백 조회
            if (
                _use_max_discount
                and site in ("ABCmart", "GrandStage")
                and not _skip_detail
                and not detail
            ):
                if hasattr(client, "get_detail"):
                    try:
                        detail = await client.get_detail(p_id)
                    except Exception as e:
                        logger.warning(f"[잡워커] {site} 서버 상세 실패 {p_id}: {e}")
            if not _skip_detail and not detail:
                # 서버 HTTP 상세 조회 (선취합 미스 폴백)
                if hasattr(client, "get_detail"):
                    try:
                        # Nike: 검색 결과 URL 전달하여 중복 검색 방지
                        if site == "Nike":
                            detail = await client.get_detail(
                                p_id,
                                pdp_url=item.get("url") or item.get("source_url"),
                                base_info=item,
                            )
                        else:
                            detail = await client.get_detail(p_id)
                        # ABCmart/GrandStage: 선취합에서 누락된 경우이므로 sleep 불필요
                        if site not in ("ABCmart", "GrandStage"):
                            await asyncio.sleep(0.15 if site == "Nike" else 0.3)
                    except Exception as e:
                        logger.warning(f"[잡워커] {site} 서버 상세 실패 {p_id}: {e}")

            # GSShop: 검색 결과에 이름/가격 없으므로 상세에서 보충
            # (선취합·폴백 상세조회 모두 거친 뒤 실행)
            if site == "GSShop" and detail:
                if not p_name or p_name == "(GSShop)":
                    p_name = detail.get("name", "") or p_name
                if sale_price <= 1:
                    sale_price = int(
                        detail.get("salePrice", 0)
                        or detail.get("bestBenefitPrice", 0)
                        or 0
                    )
                    original_price = (
                        int(detail.get("originalPrice", 0) or 0) or sale_price
                    )

            # 이미지: 확장앱 결과와 검색 API 중 더 많은 쪽 사용
            _detail_imgs = detail.get("images") or []
            _search_imgs = item.get("images", [])
            images = (
                _detail_imgs if len(_detail_imgs) > len(_search_imgs) else _search_imgs
            )
            # 원가: 최대혜택가 옵션 시 bestBenefitPrice 우선
            if _use_max_discount:
                _bbp = int(detail.get("bestBenefitPrice", 0) or 0) or int(
                    item.get("best_benefit_price", 0) or 0
                )
                cost = _bbp if _bbp > 0 else (int(item.get("cost", 0)) or sale_price)
            else:
                cost = int(item.get("cost", 0)) or sale_price
            # 배송비 원가 가산 (무료배송 아닌 경우)
            # detail에는 파서 경로에 따라 freeShipping(camelCase) 또는 free_shipping(snake_case)이 올 수 있음
            _sourcing_ship_fee = 0
            _is_free_ship = (
                item.get("free_shipping", False)
                or detail.get("free_shipping", False)
                or detail.get("freeShipping", False)
            )
            if not _is_free_ship:
                _sourcing_ship_fee = int(detail.get("shipping_fee", 0) or 0)
                cost += _sourcing_ship_fee
            # 원가 수집 실패 시 100,000원 sentinel — 배송비만 남는 사고 방지
            if cost <= 0:
                _add_job_log(
                    job.id,
                    f"[원가수집실패] {site} → 100,000원 fallback 적용",
                    job_type="collect",
                )
                cost = 100000
            _style_code = detail.get("style_code") or item.get("style_code", "")
            # Nike: scan(item)의 parse_subtitle이 더 구체적이므로 item 우선
            # 다른 소싱처: 기존 detail 우선 로직 유지
            if site == "Nike":
                _cat = item.get("category") or detail.get("category") or _category1_name
                _cat1 = item.get("category1") or detail.get("category1") or ""
                _cat2 = item.get("category2") or detail.get("category2") or ""
                _cat3 = item.get("category3") or detail.get("category3") or ""
                _cat4 = item.get("category4") or detail.get("category4") or ""
            elif site == "SSG":
                # SSG: 개별 상품의 전시카테고리 전체 경로 우선
                # category2가 없으면 leaf 단일명만 있는 불완전 카테고리이므로 ctgPath 폴백 사용
                _det_cat = detail.get("category", "")
                if _det_cat and detail.get("category2"):
                    _cat = _det_cat
                    _cat1 = detail.get("category1", "")
                    _cat2 = detail.get("category2", "")
                    _cat3 = detail.get("category3", "")
                    _cat4 = detail.get("category4", "")
                elif _ssg_cat:
                    _cat = _ssg_cat
                    _cat1 = _ssg_cat1
                    _cat2 = _ssg_cat2
                    _cat3 = _ssg_cat3
                    _cat4 = _ssg_cat4
                else:
                    _cat = item.get("category", "")
                    _cat1 = item.get("category1", "")
                    _cat2 = item.get("category2", "")
                    _cat3 = item.get("category3", "")
                    _cat4 = item.get("category4", "")
            else:
                _cat = (
                    detail.get("category")
                    or _lotteon_cat
                    or item.get("category", "")
                    or _category1_name  # 패션플러스: URL에서 재구성된 전체 카테고리 경로
                )
                _cat1 = (
                    detail.get("category1")
                    or _lotteon_cat1
                    or item.get("category1", "")
                    or _fp_cat1  # 패션플러스 URL의 category1Name
                )
                _cat2 = (
                    detail.get("category2")
                    or _lotteon_cat2
                    or item.get("category2", "")
                    or _fp_cat2  # 패션플러스 URL의 category2Name
                )
                _cat3 = (
                    detail.get("category3")
                    or _lotteon_cat3
                    or item.get("category3", "")
                    or _fp_cat3  # 패션플러스 URL의 category3Name
                )
                _cat4 = (
                    detail.get("category4")
                    or _lotteon_cat4
                    or item.get("category4", "")
                )
            product_data = {
                "source_site": site,
                "search_filter_id": filter_id,
                "site_product_id": p_id,
                "source_url": item.get("source_url", "")
                or detail.get("source_url", ""),
                "name": p_name,
                "brand": item.get("brand", ""),
                "original_price": original_price,
                "sale_price": sale_price,
                "cost": cost,
                "images": images,
                "options": [
                    {
                        **o,
                        "stock": o.get("stock", 0)
                        if (o.get("stock") or 0) > 1
                        else (99 if (o.get("stock") or 0) > 0 else 0),
                    }
                    for o in (detail.get("options") or item.get("options", []))
                ],
                "category": _cat,
                "category1": _cat1,
                "category2": _cat2,
                "category3": _cat3,
                "category4": _cat4,
                "detail_html": detail.get("detail_html") or item.get("detail_html", ""),
                "detail_images": detail.get("detail_images")
                if len(detail.get("detail_images") or []) > len(images)
                else images,
                "material": detail.get("material", ""),
                "color": detail.get("color", ""),
                "manufacturer": detail.get("manufacturer") or item.get("brand", ""),
                "origin": detail.get("origin", ""),
                "sex": detail.get("sex", "") or "남녀공용",
                "season": detail.get("season", "") or "사계절",
                "care_instructions": detail.get("care_instructions", ""),
                "quality_guarantee": detail.get("quality_guarantee", ""),
                "sourcing_shipping_fee": _sourcing_ship_fee,
                "style_code": _style_code,
                "status": "collected",
                "group_key": generate_group_key(
                    brand=item.get("brand", ""),
                    similar_no=None,
                    style_code=_style_code,
                    name=p_name,
                )
                or f"fp_{site.lower()}_{p_id}",
                "price_history": [
                    {
                        "date": datetime.now(UTC).isoformat(),
                        "sale_price": sale_price,
                        "original_price": original_price,
                        "cost": cost,
                        "options": detail.get("options") or item.get("options", []),
                    }
                ],
            }
            try:
                saved = await svc.create_collected_product(product_data)
                # 동일 소싱처 내 동일 원 상품명 차단/블랙리스트 → None 반환 시 카운트 제외
                if not saved:
                    continue
                total_saved += 1
                _collect_last_progress[job.id] = _time.time()  # 진행 갱신
                await repo.update_progress(
                    job.id, existing_count + total_saved, requested_count
                )
                _log_b = item.get("brand", "") or ""
                _log_n = p_name or ""
                _log_s = item.get("style_code", "") or ""
                _fp_log = f"[{existing_count + total_saved:,}/{requested_count:,}] {_log_b} {_log_n}"
                if _log_s:
                    _fp_log += f" {_log_s}"
                _fp_log += f" {p_id}"
                _add_job_log(job.id, _fp_log, job_type="collect")
            except Exception as e:
                logger.warning(f"[잡워커] {site} 저장 실패 {p_id}: {e}")

        # last_collected_at 갱신 + 요청수를 실제 수집수로 보정 (카테고리 중복 제거)
        from sqlalchemy import update as sa_update

        actual_count = (
            await session.execute(
                select(_func.count()).where(CPModel.search_filter_id == filter_id)
            )
        ).scalar() or 0
        update_vals: dict = {"last_collected_at": datetime.now(UTC)}
        from backend.domain.samba.collector.model import SambaSearchFilter as _SF

        await session.execute(
            sa_update(_SF).where(_SF.id == filter_id).values(**update_vals)
        )

        # 정책 자동 적용
        policy_msg = ""
        if sf.applied_policy_id and total_saved > 0:
            try:
                from backend.domain.samba.policy.repository import SambaPolicyRepository

                policy_repo = SambaPolicyRepository(session)
                policy = await policy_repo.get_async(sf.applied_policy_id)
                policy_data = None
                if policy and policy.pricing:
                    pr = policy.pricing if isinstance(policy.pricing, dict) else {}
                    policy_data = {
                        "margin_rate": pr.get("marginRate", 15),
                        "shipping_cost": pr.get("shippingCost", 0),
                        "extra_charge": pr.get("extraCharge", 0),
                        "use_range_margin": pr.get("useRangeMargin", False),
                        "range_margins": pr.get("rangeMargins", []),
                        "source_site_margins": pr.get("sourceSiteMargins", {}),
                    }
                count = await svc.apply_policy_to_filter_products(
                    filter_id, sf.applied_policy_id, policy_data
                )
                policy_msg = f", 정책 적용: {count}개"
            except Exception as e:
                logger.error(f"[잡워커] {site} 정책 전파 실패: {e}")

        _in_stock = total_saved - _collected_sold_out
        _parts = [f"신규 {total_saved}건"]
        if _in_stock > 0 or _collected_sold_out > 0:
            _parts.append(f"재고 {_in_stock}건 | 품절 {_collected_sold_out}건")
        if policy_msg:
            _parts.append(policy_msg.lstrip(", "))
        _add_job_log(
            job.id,
            f"{_dprefix} [{sf.name}] 수집 완료: {' | '.join(_parts)}",
            job_type="collect",
        )

        await repo.complete_job(
            job.id,
            {
                "saved": total_saved,
                "in_stock_count": _in_stock,
                "sold_out_count": _collected_sold_out,
            },
        )
        logger.info(
            f"[잡워커] {site} 수집 완료: {job.id} ({total_saved}건{policy_msg})"
        )

        # LOTTEON: 수집 완료 후 상세 보강 (품번/제조국/성별/시즌/색상/재질)
        # 10건 병렬로 get_detail 호출하여 속도 개선
        # LOTTEON: 선취합 실패분만 보강 (폴백)
        _enrich_needed = total_saved - len(_lotteon_details) if site == "LOTTEON" else 0
        if site == "LOTTEON" and _enrich_needed > 0 and client:
            logger.info(f"[잡워커] LOTTEON 보강(폴백): 선취합 실패 {_enrich_needed}건")
            enrich_stmt = select(CPModel).where(
                CPModel.search_filter_id == filter_id,
                CPModel.source_site == "LOTTEON",
                CPModel.brand == None,  # noqa: E711 — 선취합 안 된 상품
            )
            products_to_enrich = (await session.execute(enrich_stmt)).scalars().all()

            BATCH_SIZE = 10
            enriched = 0
            total = len(products_to_enrich)

            for batch_start in range(0, total, BATCH_SIZE):
                batch = products_to_enrich[batch_start : batch_start + BATCH_SIZE]
                # 10건 동시 get_detail 호출
                details = await asyncio.gather(
                    *(client.get_detail(p.site_product_id) for p in batch),
                    return_exceptions=True,
                )
                for prod, detail in zip(batch, details):
                    if isinstance(detail, Exception):
                        logger.warning(
                            f"[잡워커] LOTTEON 상세 보강 실패 {prod.site_product_id}: {detail}"
                        )
                        continue
                    if not detail:
                        continue
                    changed = False
                    for field in (
                        "material",
                        "color",
                        "origin",
                        "sex",
                        "season",
                        "care_instructions",
                        "quality_guarantee",
                    ):
                        val = detail.get(field, "")
                        if val and not getattr(prod, field, ""):
                            setattr(prod, field, val)
                            changed = True
                    # 브랜드
                    brd = detail.get("brand", "")
                    if brd and not (prod.brand or ""):
                        prod.brand = brd
                        changed = True
                    # 품번 (style_code)
                    sc = detail.get("style_code") or detail.get("styleCode") or ""
                    if sc and not (prod.style_code or ""):
                        prod.style_code = sc
                        changed = True
                    # 제조사
                    mfr = detail.get("manufacturer", "")
                    if mfr and not (prod.manufacturer or ""):
                        prod.manufacturer = mfr
                        changed = True
                    # 카테고리
                    cat = detail.get("category", "")
                    if cat and not (prod.category or "" == "-"):
                        prod.category = cat
                        changed = True
                    # 이미지 보강
                    d_imgs = detail.get("images") or []
                    if len(d_imgs) > len(prod.images or []):
                        prod.images = d_imgs
                        changed = True
                    d_detail_imgs = detail.get("detail_images") or []
                    if d_detail_imgs and not (prod.detail_images or []):
                        prod.detail_images = d_detail_imgs
                        changed = True
                    # 옵션 보강
                    d_opts = detail.get("options") or []
                    if d_opts and not (prod.options or []):
                        prod.options = d_opts
                        changed = True
                    if changed:
                        session.add(prod)
                        enriched += 1
                await session.commit()
                done = min(batch_start + BATCH_SIZE, total)
                logger.info(
                    f"[잡워커] LOTTEON 상세 보강 [{done}/{total}] ({enriched}건 업데이트)"
                )
                await asyncio.sleep(0.3)

            logger.info(
                f"[잡워커] LOTTEON 상세 보강 완료: {enriched}/{total}건 업데이트"
            )

    async def _run_delete_market(self, job, repo, session):
        """마켓삭제 잡 실행 — registered_accounts에서 계정을 제거하고 마켓 API로 삭제."""
        from backend.domain.samba.shipment.service import SambaShipmentService
        from backend.domain.samba.shipment.repository import SambaShipmentRepository

        payload = job.payload or {}
        product_ids = payload.get("product_ids", [])
        target_account_ids = payload.get("target_account_ids", [])
        source_site = payload.get("source_site", "?")
        brand_name = payload.get("brand_name", "?")

        if not product_ids:
            await repo.complete_job(job.id)
            return

        total = len(product_ids)
        logger.info(
            f"[마켓삭제잡] 시작 — {source_site}/{brand_name} "
            f"← {target_account_ids} ({total}건)"
        )

        # 진행률 초기화 — UI에서 0/N 표시
        await repo.update_progress(job.id, 0, total)
        await session.commit()

        async def _on_progress(current: int, _total: int) -> None:
            from backend.db.orm import get_write_session
            from backend.domain.samba.job.repository import (
                SambaJobRepository as _JobRepo,
            )

            async with get_write_session() as prog_session:
                prog_repo = _JobRepo(prog_session)
                await prog_repo.update_progress(job.id, current, _total)
                await prog_session.commit()

        ship_svc = SambaShipmentService(SambaShipmentRepository(session), session)
        try:
            await ship_svc.delete_from_markets(
                product_ids=product_ids,
                target_account_ids=target_account_ids,
                log_to_buffer=True,
                on_progress=_on_progress,
            )
            await repo.complete_job(job.id)
            logger.info(f"[마켓삭제잡] 완료 — {source_site}/{brand_name} ({total}건)")
        except Exception as e:
            logger.error(f"[마켓삭제잡] 실패 — {job.id}: {e}")
            raise

    async def _run_stub(self, job, repo, name: str):
        """미구현 잡 타입 스텁."""
        logger.info(f"[잡워커] {name} 잡은 아직 미구현: {job.id}")
        await repo.complete_job(job.id, {"message": f"{name} 잡 미구현 — 추후 지원"})

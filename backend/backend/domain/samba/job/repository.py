"""작업 큐 리포지토리."""

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.domain.shared.base_repository import BaseRepository
from .model import JobStatus, SambaJob

UTC = timezone.utc


class SambaJobRepository(BaseRepository[SambaJob]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, SambaJob)

    @staticmethod
    def _payload_account_array_sql(alias: str) -> str:
        return (
            "ARRAY("
            "SELECT DISTINCT v FROM ("
            f"SELECT json_array_elements_text(COALESCE({alias}.payload->'target_account_ids', '[]'::json)) AS v "
            "UNION "
            f"SELECT {alias}.payload->>'account_id' AS v "
            f"WHERE COALESCE({alias}.payload->>'account_id', '') <> '' "
            "UNION "
            f"SELECT {alias}.payload->>'target_account_id' AS v "
            f"WHERE COALESCE({alias}.payload->>'target_account_id', '') <> ''"
            ") account_ids)"
        )

    # ── 원자적 잡 획득 (멀티 worker race condition 방지) ──

    async def claim_pending_job(
        self,
        exclude_types: set[str] | None = None,
        exclude_sources: set[str] | None = None,
        exclude_brand_all: bool = False,
        exclude_accounts: set[str] | None = None,
    ) -> Optional[SambaJob]:
        """Pending 잡 1개를 원자적으로 claim (FOR UPDATE SKIP LOCKED).

        다른 worker가 이미 lock 잡은 row는 건너뛰므로 중복 실행 불가.
        write session 컨텍스트 안에서 호출해야 하며, 호출부에서 commit() 필요.

        Args:
            exclude_types: 제외할 job_type 집합 (예: {"collect"})
            exclude_sources: 제외할 수집 소싱처 집합 — 현재 실행 중인 소싱처 스킵용.
                             payload->>'source_site' 값이 이 셋에 포함된 collect 잡은 건너뜀.
                             다른 소싱처 잡은 계속 pick 가능 → 소싱처별 동시 실행 허용.
            exclude_brand_all: True면 brand_all collect 잡을 skip — 1개씩 직렬 실행 보장.
                               SSG+MUSINSA 동시 실행 시 DB 커넥션 고갈/OOM 방지.
            exclude_accounts: 제외할 마켓 계정 ID 집합 — 현재 실행 중인 transmit 잡의 계정.
                              payload->'target_account_ids' 배열 중 하나라도 이 셋과 겹치는
                              transmit 잡은 건너뜀 → 같은 계정은 순차 실행 보장.
        """
        from sqlalchemy import and_, case, func, or_
        from sqlalchemy import text as _sa_text

        # transmit 잡은 product_ids 개수가 적은 것부터 우선 픽 — 짧은 잡이
        # 큰 잡 뒤에 묶여 대기하는 것을 방지. 다른 타입은 key=0으로 FIFO 유지.
        _transmit_size_key = case(
            (
                SambaJob.job_type == "transmit",
                func.coalesce(
                    func.json_array_length(SambaJob.payload.op("->")("product_ids")),
                    0,
                ),
            ),
            else_=0,
        )

        # PlayAuto 마켓 계정이 포함된 transmit 잡은 최우선 픽 — 5개 슬롯 안에 항상 진입.
        # 동일 PlayAuto 계정 동시실행 차단(_db_account_lock)은 그대로 유지되어 순차 보장.
        _job_accounts_for_priority = self._payload_account_array_sql("samba_jobs")
        _playauto_priority_key = case(
            (
                and_(
                    SambaJob.job_type == "transmit",
                    _sa_text(
                        "EXISTS (SELECT 1 FROM samba_market_account ma "
                        f"WHERE ma.id::text = ANY({_job_accounts_for_priority}) "
                        "AND ma.market_type = 'playauto')"
                    ),
                ),
                0,
            ),
            else_=1,
        )

        stmt = (
            select(SambaJob)
            .where(SambaJob.status == JobStatus.PENDING)
            .order_by(
                _playauto_priority_key.asc(),
                _transmit_size_key.asc(),
                SambaJob.created_at.asc(),
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if exclude_types:
            stmt = stmt.where(SambaJob.job_type.notin_(list(exclude_types)))
        if exclude_sources:
            # collect 잡 중 source_site 가 exclude_sources 에 포함되면 제외
            # 다른 타입(transmit 등) 이거나 다른 소싱처면 pick 허용
            _excl_list = list(exclude_sources)
            stmt = stmt.where(
                or_(
                    SambaJob.job_type != "collect",
                    and_(
                        SambaJob.job_type == "collect",
                        SambaJob.payload.op("->>")("source_site").notin_(_excl_list),
                    ),
                )
            )
        if exclude_brand_all:
            # brand_all collect 잡은 현재 실행 중인 brand_all 완료 후 픽업
            stmt = stmt.where(
                ~and_(
                    SambaJob.job_type == "collect",
                    SambaJob.payload.op("->>")("brand_all") == "true",
                )
            )
        # ── 같은 마켓 계정 순차 실행 (DB self-join, 멀티워커 안전) ──
        # transmit 잡은 항상 검사: 현재 running 상태인 다른 transmit 잡과
        # target_account_ids 배열이 하나라도 겹치면 SKIP.
        # 인메모리 exclude_accounts(같은 워커 내) + DB self-join(워커 간) 이중 방어.

        _job_accounts_sql = self._payload_account_array_sql("samba_jobs")
        _running_accounts_sql = self._payload_account_array_sql("r")

        _db_account_lock = _sa_text(
            "(samba_jobs.job_type <> 'transmit' OR NOT EXISTS ("
            "SELECT 1 FROM samba_jobs r "
            "WHERE r.status = 'running' "
            "AND r.job_type = 'transmit' "
            "AND r.id <> samba_jobs.id "
            f"AND {_job_accounts_sql} && {_running_accounts_sql}"
            "))"
        )
        stmt = stmt.where(_db_account_lock)

        if exclude_accounts:
            # 같은 워커 내 인메모리 추적 — DB 검사 전에 빠르게 거름.
            _excl_acc_list = list(exclude_accounts)
            _no_overlap = _sa_text(
                "(samba_jobs.job_type <> 'transmit' OR NOT EXISTS ("
                f"SELECT 1 FROM unnest({_job_accounts_sql}) AS v "
                "WHERE v = ANY(:excl_accs)))"
            ).bindparams(excl_accs=_excl_acc_list)
            stmt = stmt.where(_no_overlap)

        result = await self.session.execute(stmt)
        job = result.scalars().first()
        if job:
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(UTC)
            self.session.add(job)
            await self.session.flush()
        return job

    # ── 레거시 메서드 (하위 호환용, 다른 호출부가 있을 수 있음) ──

    async def pick_next_pending(self) -> Optional[SambaJob]:
        """가장 오래된 pending 잡 1개를 running으로 변경 후 반환.

        [DEPRECATED] claim_pending_job()을 사용하세요 — FOR UPDATE SKIP LOCKED 적용.
        """
        stmt = (
            select(SambaJob)
            .where(SambaJob.status == JobStatus.PENDING)
            .order_by(SambaJob.created_at.asc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        job = result.scalars().first()
        if job:
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(UTC)
            self.session.add(job)
            await self.session.flush()
        return job

    async def list_pending(self, limit: int = 5) -> list[SambaJob]:
        """pending 잡을 오래된 순으로 조회 (running 변경 포함).

        [DEPRECATED] claim_pending_job()을 사용하세요 — FOR UPDATE SKIP LOCKED 적용.
        """
        stmt = (
            select(SambaJob)
            .where(SambaJob.status == JobStatus.PENDING)
            .order_by(SambaJob.created_at.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        jobs = list(result.scalars().all())
        for job in jobs:
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(UTC)
            self.session.add(job)
        if jobs:
            await self.session.flush()
        return jobs

    async def update_progress(self, job_id: str, current: int, total: int):
        """진행률 업데이트."""
        job = await self.get_async(job_id)
        if job:
            job.current = current
            job.total = total
            job.progress = int((current / total) * 100) if total > 0 else 0
            self.session.add(job)
            await self.session.flush()
            # 최근 건당 처리속도 트래커에 샘플 기록 (인메모리)
            from backend.domain.samba.job.progress_tracker import record_progress

            record_progress(job_id, current)

    async def complete_job(self, job_id: str, result: dict | None = None):
        """잡 완료 처리 — attempt 리셋 포함."""
        job = await self.get_async(job_id)
        if job:
            job.status = JobStatus.COMPLETED
            job.progress = 100
            job.attempt = 0  # 성공 → attempt 리셋
            job.completed_at = datetime.now(UTC)
            if result:
                job.result = result
            self.session.add(job)
            await self.session.flush()
            from backend.domain.samba.job.progress_tracker import clear_progress

            clear_progress(job_id)

    async def fail_job(self, job_id: str, error: str):
        """잡 실패 처리.

        이미 CANCELLED 상태인 잡은 FAILED로 덮어쓰지 않음 —
        명시적 취소(작업취소·개별취소) 상태가 이어하기 후보로 재등장하는 것 방지.
        """
        job = await self.get_async(job_id)
        if job:
            if job.status == JobStatus.CANCELLED:
                # 사용자가 이미 취소한 잡 — 완료 시각만 보정하고 상태 유지
                if job.completed_at is None:
                    job.completed_at = datetime.now(UTC)
                    self.session.add(job)
                    await self.session.flush()
                return
            job.status = JobStatus.FAILED
            job.error = error
            job.completed_at = datetime.now(UTC)
            self.session.add(job)
            await self.session.flush()
            from backend.domain.samba.job.progress_tracker import clear_progress

            clear_progress(job_id)

    async def cancel_job(self, job_id: str) -> bool:
        """잡 취소 (pending/running 모두 가능)."""
        job = await self.get_async(job_id)
        if not job or job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
            return False
        job.status = JobStatus.CANCELLED
        job.completed_at = datetime.now(UTC)
        self.session.add(job)
        await self.session.flush()
        from backend.domain.samba.job.progress_tracker import clear_progress

        clear_progress(job_id)
        return True

    async def is_cancelled(self, job_id: str) -> bool:
        """잡이 취소 상태인지 확인 (워커에서 건별 체크용).
        완전히 새 세션으로 조회 — 워커 세션 오염 방지 + ORM 캐시 우회.
        타임아웃/DB 에러 시 False 반환 (안전 우선 — 전송/수집 계속)."""
        import asyncio
        from sqlalchemy import text
        from backend.db.orm import get_write_session

        try:
            async with get_write_session() as fresh_session:
                result = await asyncio.wait_for(
                    fresh_session.execute(
                        text("SELECT status FROM samba_jobs WHERE id = :id"),
                        {"id": job_id},
                    ),
                    timeout=5,
                )
                row = result.first()
                return row[0] == JobStatus.CANCELLED if row else False
        except (asyncio.TimeoutError, Exception):
            return False

    async def recover_stuck_running(
        self,
        exclude_ids: set[str] | None = None,
        threshold_sec: int = 0,
    ) -> int:
        """stuck된 running 잡을 pending으로 복구.

        FOR UPDATE SKIP LOCKED 적용 — 다른 worker가 처리 중인 잡은 건너뜀.

        exclude_ids: 현재 워커가 실행 중인 잡 ID 제외
        threshold_sec: >0이면 started_at 기준 N초 이상 경과한 잡만 복구
        """
        from datetime import timedelta

        from sqlalchemy import and_

        conditions = [SambaJob.status == JobStatus.RUNNING]
        if exclude_ids:
            conditions.append(SambaJob.id.notin_(list(exclude_ids)))
        if threshold_sec > 0:
            cutoff = datetime.now(UTC) - timedelta(seconds=threshold_sec)
            conditions.append(SambaJob.started_at < cutoff)

        # FOR UPDATE SKIP LOCKED — 다른 worker가 lock 잡은 running 잡은 skip
        stmt = (
            select(SambaJob).where(and_(*conditions)).with_for_update(skip_locked=True)
        )
        result = await self.session.execute(stmt)
        stuck = list(result.scalars().all())
        for job in stuck:
            job.status = JobStatus.PENDING
            job.started_at = None
            # current/progress 보존 — 전송 잡이 이어서 재개할 수 있도록
            self.session.add(job)
        if stuck:
            await self.session.flush()
        return len(stuck)

    async def list_by_status(
        self,
        status: str | None = None,
        tenant_id: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ):
        """상태별 잡 목록."""
        stmt = select(SambaJob)
        if status:
            stmt = stmt.where(SambaJob.status == status)
        if tenant_id:
            stmt = stmt.where(SambaJob.tenant_id == tenant_id)
        stmt = stmt.order_by(SambaJob.created_at.desc()).offset(skip).limit(limit)
        result = await self.session.execute(stmt)
        return result.scalars().all()

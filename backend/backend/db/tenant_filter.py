"""SQLAlchemy ORM 자동 tenant 필터.

current_tenant_id contextvar가 세팅된 상태(=HTTP 요청)에서
- SELECT: 쿼리에 등장한 tenant_id 컬럼 모델에 WHERE tenant_id = ? 자동 추가
- INSERT: tenant_id 미세팅 객체에 자동 채움

contextvar=None인 컨텍스트(워커/마이그레이션/내부 잡)는 패스.
"""

import logging

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from backend.core.tenant_context import current_tenant_id

logger = logging.getLogger(__name__)


def register_tenant_filter_events() -> None:
    """SQLAlchemy event 리스너 등록 — 앱 시작 시 한 번만 호출."""

    @event.listens_for(Session, "do_orm_execute")
    def _apply_tenant_filter(orm_execute_state):
        """SELECT 자동 WHERE tenant_id = ? 추가.

        Statement에 등장한 entity를 순회해서 tenant_id 컬럼이 있으면 적용.
        with_loader_criteria는 statement에 없는 모델은 무시하므로 안전.
        """
        if not orm_execute_state.is_select:
            return
        tenant_id = current_tenant_id.get()
        if tenant_id is None:
            return

        seen: set = set()
        for desc in orm_execute_state.statement.column_descriptions:
            entity = desc.get("entity")
            if entity is None or entity in seen:
                continue
            seen.add(entity)
            try:
                table = entity.__table__
            except AttributeError:
                continue
            if "tenant_id" not in table.columns:
                continue
            try:
                orm_execute_state.statement = orm_execute_state.statement.options(
                    with_loader_criteria(
                        entity,
                        entity.tenant_id == tenant_id,
                        include_aliases=True,
                    )
                )
            except Exception as e:
                logger.debug(f"[tenant_filter] entity {entity} 필터 적용 실패: {e}")

    @event.listens_for(Session, "before_flush")
    def _auto_set_tenant_id(session, flush_context, instances):
        """INSERT 시 tenant_id 미세팅 신규 객체에 자동 채움."""
        tenant_id = current_tenant_id.get()
        if tenant_id is None:
            return

        for obj in session.new:
            if not hasattr(obj, "tenant_id"):
                continue
            if getattr(obj, "tenant_id", None) is None:
                try:
                    obj.tenant_id = tenant_id
                except Exception:
                    pass

    logger.info("[tenant_filter] ORM 자동 tenant 필터 이벤트 등록 완료")

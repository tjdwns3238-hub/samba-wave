from typing import Any, Dict, Generic, List, Optional, Type, TypeVar, TYPE_CHECKING

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel, func, select

from backend.utils import now_kst
from backend.utils.logger import logger

if TYPE_CHECKING:
    pass

ModelType = TypeVar("ModelType", bound=SQLModel)


class BaseRepository(Generic[ModelType]):
    """
    Enhanced base repository providing common CRUD operations and utilities.

    Provides:
    - Basic CRUD operations (create, read, update, delete)
    - Batch operations
    - Transaction management
    - Error handling
    - Query building utilities
    """

    def __init__(self, session: AsyncSession, model: Type[ModelType]):
        """
        Initialize repository with database session and model class.

        Args:
            session: SQLModel database session
            model: SQLModel model class
        """
        self.session = session
        self.model = model

    async def get_async(self, id: str | int) -> Optional[ModelType]:
        """
        Get entity by ID.

        Args:
            id: Entity ID

        Returns:
            Entity if found, None otherwise
        """
        try:
            statement = select(self.model).where(self.model.id == id)  # type: ignore[attr-defined]  # SQLModel dynamic attribute
            result = await self.session.execute(statement)
            entity: Optional[ModelType] = result.scalar_one_or_none()
            return entity
        except SQLAlchemyError as e:
            logger.exception(f"Error fetching {self.model.__name__} with id {id}: {e}")
            raise

    async def list_async(
        self, skip: int = 0, limit: Optional[int] = None, order_by: Optional[str] = None
    ) -> List[ModelType]:
        """
        List entities with optional pagination and ordering.

        Args:
            skip: Number of records to skip
            limit: Maximum number of records to return
            order_by: Field name to order by (prefix with '-' for DESC)

        Returns:
            List of entities
        """
        try:
            statement = select(self.model)

            # Apply ordering
            if order_by:
                if order_by.startswith("-"):
                    field_name = order_by[1:]
                    if hasattr(self.model, field_name):
                        statement = statement.order_by(
                            getattr(self.model, field_name).desc()
                        )
                else:
                    if hasattr(self.model, order_by):
                        statement = statement.order_by(getattr(self.model, order_by))

            # Apply pagination
            if skip:
                statement = statement.offset(skip)
            if limit:
                statement = statement.limit(limit)

            results = await self.session.execute(statement)
            return list(results.scalars().all())

        except SQLAlchemyError as e:
            logger.exception(f"Error listing {self.model.__name__}: {e}")
            raise

    async def create_async(self, commit: bool = True, **kwargs: Any) -> ModelType:
        """
        Create new entity.

        Args:
            commit: True(기본)면 즉시 commit+refresh. False면 flush만 하고 refresh
                생략 — 대량 persist 루프에서 청크 commit으로 묶을 때 사용해 commit
                왕복 비용을 줄인다 (issue #401). 호출부가 일괄 commit 책임.
            **kwargs: Entity attributes

        Returns:
            Created entity

        Raises:
            IntegrityError: If unique constraint violated
            SQLAlchemyError: For other database errors
        """
        try:
            entity = self.model(**kwargs)
            self.session.add(entity)
            if commit:
                await self.session.commit()
                await self.session.refresh(entity)
            else:
                # 청크 commit 모드: INSERT만 flush해 PK 확보, commit은 호출부가 일괄 수행
                await self.session.flush()

            entity_id = getattr(entity, "id", None) or getattr(entity, "key", "?")
            logger.debug(f"Created {self.model.__name__} with id {entity_id}")
            created_entity: ModelType = entity
            return created_entity

        except IntegrityError as e:
            await self.session.rollback()
            logger.exception(f"Integrity error creating {self.model.__name__}: {e}")
            raise
        except SQLAlchemyError as e:
            await self.session.rollback()
            logger.exception(f"Error creating {self.model.__name__}: {e}")
            raise

    async def update_async(
        self, id: str | int, commit: bool = True, **kwargs: Any
    ) -> Optional[ModelType]:
        """
        Update entity by ID.

        Args:
            id: Entity ID
            commit: True(기본)면 즉시 commit+refresh. False면 flush만 하고 refresh
                생략 — 대량 persist 루프에서 청크 commit으로 묶을 때 사용 (issue #401).
            **kwargs: Fields to update

        Returns:
            Updated entity or None if not found
        """
        try:
            entity = await self.get_async(id)
            if not entity:
                return None

            for key, value in kwargs.items():
                if hasattr(entity, key):
                    setattr(entity, key, value)
                    # JSON/list/dict 필드는 SQLAlchemy가 변경 감지 못할 수 있어 명시적 표시
                    if isinstance(value, (list, dict)):
                        flag_modified(entity, key)

            # Update timestamp if model has it
            if hasattr(entity, "updated_at"):
                entity.updated_at = now_kst()

            self.session.add(entity)
            if commit:
                await self.session.commit()
                await self.session.refresh(entity)
            else:
                # 청크 commit 모드: UPDATE만 flush, commit은 호출부가 일괄 수행
                await self.session.flush()

            logger.debug(f"Updated {self.model.__name__} with id {id}")
            return entity

        except SQLAlchemyError as e:
            await self.session.rollback()
            logger.exception(f"Error updating {self.model.__name__} with id {id}: {e}")
            raise

    async def delete_async(self, id: str | int) -> bool:
        """
        Delete entity by ID.

        Args:
            id: Entity ID

        Returns:
            True if deleted, False if not found
        """
        try:
            entity = await self.get_async(id)
            if not entity:
                return False

            await self.session.delete(entity)
            await self.session.commit()

            logger.debug(f"Deleted {self.model.__name__} with id {id}")
            return True

        except SQLAlchemyError as e:
            await self.session.rollback()
            logger.exception(f"Error deleting {self.model.__name__} with id {id}: {e}")
            raise

    async def count_async(self, filters: Optional[Dict[str, Any]] = None) -> int:
        """
        Count entities with optional filters.

        Args:
            filters: Dictionary of field:value filters

        Returns:
            Count of matching entities
        """
        try:
            statement = select(func.count()).select_from(self.model)

            if filters:
                for field, value in filters.items():
                    if hasattr(self.model, field):
                        statement = statement.where(getattr(self.model, field) == value)

            result = await self.session.execute(statement)
            count: int = result.scalar_one()
            return count

        except SQLAlchemyError as e:
            logger.exception(f"Error counting {self.model.__name__}: {e}")
            raise

    async def bulk_create_async(
        self, entities: List[Dict[str, Any]]
    ) -> List[ModelType]:
        """
        Create multiple entities in a single transaction.

        Args:
            entities: List of entity dictionaries

        Returns:
            List of created entities
        """
        created = []
        try:
            for entity_data in entities:
                entity = self.model(**entity_data)
                self.session.add(entity)
                created.append(entity)

            await self.session.commit()

            # Refresh all entities
            for entity in created:
                await self.session.refresh(entity)

            logger.info(f"Bulk created {len(created)} {self.model.__name__} entities")
            return created

        except SQLAlchemyError as e:
            await self.session.rollback()
            logger.exception(f"Error in bulk create for {self.model.__name__}: {e}")
            raise

    async def find_by_async(self, **kwargs: Any) -> Optional[ModelType]:
        """
        Find single entity by field values.

        Args:
            **kwargs: Field:value pairs to search by

        Returns:
            First matching entity or None
        """
        try:
            statement = select(self.model)

            for field, value in kwargs.items():
                if hasattr(self.model, field):
                    statement = statement.where(getattr(self.model, field) == value)

            # Caller contract is "first matching entity or None". Some tables may
            # contain legacy duplicates, so avoid raising on multi-row matches.
            result = await self.session.execute(statement.limit(2))
            entities = list(result.scalars().all())
            if len(entities) > 1:
                logger.warning(
                    "Multiple %s rows matched find_by_async(%s); returning the first row",
                    self.model.__name__,
                    kwargs,
                )
            return entities[0] if entities else None

        except SQLAlchemyError as e:
            logger.exception(f"Error finding {self.model.__name__}: {e}")
            raise

    async def filter_by_async(
        self,
        skip: int = 0,
        limit: Optional[int] = None,
        order_by: Optional[str] = None,
        order_by_desc: bool = False,
        **kwargs: Any,
    ) -> List[ModelType]:
        """
        Filter entities by field values with pagination.

        Args:
            skip: Number of records to skip
            limit: Maximum number of records
            **kwargs: Field:value pairs to filter by

        Returns:
            List of matching entities
        """
        try:
            statement = select(self.model)

            for field, value in kwargs.items():
                if hasattr(self.model, field):
                    if isinstance(value, list):
                        statement = statement.where(
                            getattr(self.model, field).in_(value)
                        )
                    else:
                        statement = statement.where(getattr(self.model, field) == value)

            if order_by:
                if order_by_desc:
                    statement = statement.order_by(getattr(self.model, order_by).desc())
                else:
                    statement = statement.order_by(getattr(self.model, order_by))

            if skip:
                statement = statement.offset(skip)
            if limit:
                statement = statement.limit(limit)

            results = await self.session.execute(statement)
            return list(results.scalars().all())

        except SQLAlchemyError as e:
            logger.exception(f"Error filtering {self.model.__name__}: {e}")
            raise

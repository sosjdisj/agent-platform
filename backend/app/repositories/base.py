"""通用 Repository 基类：封装单模型基础 CRUD，供各业务 Repository 继承复用。"""
from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """基于 AsyncSession 的基础 CRUD。

    方法内部仅 flush 不 commit，事务提交由调用方（服务层 / 请求边界）统一控制。
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, entity_id: int) -> ModelT | None:
        """按主键查询单条记录。"""
        return await self.session.get(self.model, entity_id)

    async def list(self, *, offset: int = 0, limit: int = 100) -> list[ModelT]:
        """分页查询记录列表。"""
        stmt = select(self.model).offset(offset).limit(limit)
        return list(await self.session.scalars(stmt))

    async def list_by_ids(self, entity_ids: list[int]) -> list[ModelT]:
        """按主键集合批量查询；空集合直接返回空列表。"""
        if not entity_ids:
            return []
        stmt = select(self.model).where(self.model.id.in_(entity_ids))
        return list(await self.session.scalars(stmt))

    async def create(self, **fields: object) -> ModelT:
        """创建记录。"""
        entity = self.model(**fields)
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def update(self, entity: ModelT, **fields: object) -> ModelT:
        """更新记录的指定字段。"""
        for key, value in fields.items():
            setattr(entity, key, value)
        await self.session.flush()
        return entity

    async def delete(self, entity: ModelT) -> None:
        """删除记录。"""
        await self.session.delete(entity)
        await self.session.flush()

"""客户 Repository。"""
from sqlalchemy import select

from app.models.customer import Customer

from app.repositories.base import BaseRepository


class CustomerRepository(BaseRepository[Customer]):
    model = Customer

    async def search(
        self, keyword: str | None = None, *, limit: int = 20, offset: int = 0
    ) -> list[Customer]:
        """按客户名称/编码关键词模糊查询并分页；无关键词时按 id 顺序分页返回。"""
        stmt = select(self.model).order_by(self.model.id)
        if keyword:
            like = f"%{keyword}%"
            stmt = stmt.where(self.model.name.ilike(like) | self.model.code.ilike(like))
        stmt = stmt.offset(offset).limit(limit)
        return list(await self.session.scalars(stmt))

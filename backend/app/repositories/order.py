"""订单 Repository。"""
from datetime import datetime

from sqlalchemy import Row, func, select

from app.models.order import Order

from app.repositories.base import BaseRepository


class OrderRepository(BaseRepository[Order]):
    model = Order

    async def stats_by_customer(self, customer_id: int) -> Row:
        """按客户聚合订单统计，返回行字段：total_count / total_amount（无订单时金额为 None）。"""
        stmt = select(
            func.count().label("total_count"),
            func.sum(self.model.amount).label("total_amount"),
        ).where(self.model.customer_id == customer_id)
        return (await self.session.execute(stmt)).one()

    async def list_by_customer_period(
        self,
        customer_id: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Order]:
        """按客户与订单时间范围（左闭右开）分页查询，命中复合索引。

        start / end 均为可选：只传其一则只约束对应边界。
        """
        conditions = [self.model.customer_id == customer_id]
        if start is not None:
            conditions.append(self.model.order_date >= start)
        if end is not None:
            conditions.append(self.model.order_date < end)
        stmt = (
            select(self.model)
            .where(*conditions)
            .order_by(self.model.order_date, self.model.id)
            .offset(offset)
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))

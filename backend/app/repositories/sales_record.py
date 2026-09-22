"""销售记录 Repository。"""
from datetime import datetime

from sqlalchemy import Row, func, select

from app.models.sales_record import SalesRecord

from app.repositories.base import BaseRepository


class SalesRecordRepository(BaseRepository[SalesRecord]):
    model = SalesRecord

    async def list_by_customer_period(
        self,
        customer_id: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        product_id: int | None = None,
    ) -> list[SalesRecord]:
        """按客户与销售时间范围（左闭右开）查询，可选产品过滤，命中复合索引。"""
        conditions = [self.model.customer_id == customer_id]
        if start is not None:
            conditions.append(self.model.sale_date >= start)
        if end is not None:
            conditions.append(self.model.sale_date < end)
        if product_id is not None:
            conditions.append(self.model.product_id == product_id)
        stmt = (
            select(self.model)
            .where(*conditions)
            .order_by(self.model.sale_date, self.model.product_id)
        )
        return list(await self.session.scalars(stmt))

    async def sum_by_product(
        self,
        *,
        customer_id: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        product_id: int | None = None,
    ) -> list[Row]:
        """按产品聚合销售数量 / 金额（月度汇总条数），可选客户与时间范围，按金额降序。

        返回行字段：product_id / total_quantity / total_amount / month_count。
        """
        conditions = []
        if customer_id is not None:
            conditions.append(self.model.customer_id == customer_id)
        if start is not None:
            conditions.append(self.model.sale_date >= start)
        if end is not None:
            conditions.append(self.model.sale_date < end)
        if product_id is not None:
            conditions.append(self.model.product_id == product_id)
        stmt = (
            select(
                self.model.product_id,
                func.sum(self.model.quantity).label("total_quantity"),
                func.sum(self.model.amount).label("total_amount"),
                func.count().label("month_count"),
            )
            .where(*conditions)
            .group_by(self.model.product_id)
            .order_by(func.sum(self.model.amount).desc())
        )
        return list((await self.session.execute(stmt)).all())

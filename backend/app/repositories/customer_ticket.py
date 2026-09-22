"""客户工单 Repository。"""
from sqlalchemy import Row, func, select

from app.models.customer_ticket import CustomerTicket

from app.repositories.base import BaseRepository


class CustomerTicketRepository(BaseRepository[CustomerTicket]):
    model = CustomerTicket

    async def stats_by_customer(self, customer_id: int) -> Row:
        """按客户聚合工单统计，返回行字段：total_count / open_count（未解决 = resolved_at 为空）。"""
        stmt = select(
            func.count().label("total_count"),
            func.count().filter(self.model.resolved_at.is_(None)).label("open_count"),
        ).where(self.model.customer_id == customer_id)
        return (await self.session.execute(stmt)).one()

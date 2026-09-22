"""CRM 业务服务：客户与工单等写入动作（写入统一走 Service 层，Repository 仅 flush）。"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.customer_ticket import CustomerTicket
from app.repositories.customer import CustomerRepository
from app.repositories.customer_ticket import CustomerTicketRepository


class CrmService:
    """客户域业务服务：封装写入动作与派生编号，事务提交在本层完成。"""

    def __init__(self, session: AsyncSession) -> None:
        self._customers = CustomerRepository(session)
        self._tickets = CustomerTicketRepository(session)

    async def update_customer(self, customer: Customer, **fields: object) -> Customer:
        """更新客户主数据并提交事务；调用方需先加载并确保客户存在。"""
        updated = await self._customers.update(customer, **fields)
        await self._customers.session.commit()
        return updated

    async def create_ticket(
        self, *, customer_id: int, title: str, content: str, priority: str
    ) -> CustomerTicket:
        """创建客户工单并提交事务；调用方需先确保客户存在。"""
        ticket = await self._tickets.create(
            customer_id=customer_id,
            ticket_no=await self._next_ticket_no(),
            title=title,
            content=content,
            priority=priority,
        )
        await self._tickets.session.commit()
        return ticket

    async def _next_ticket_no(self) -> str:
        """生成 T-YYMM-NNN 工单号：当月前缀下取最大编号 +1（与 seed 编号规则一致）。"""
        prefix = f"T-{datetime.now(timezone.utc):%y%m}-"
        max_no = await self._tickets.session.scalar(
            select(func.max(CustomerTicket.ticket_no)).where(
                CustomerTicket.ticket_no.startswith(prefix)
            )
        )
        seq = int(max_no[len(prefix):]) + 1 if max_no else 1
        return f"{prefix}{seq:03d}"

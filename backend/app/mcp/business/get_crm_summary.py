"""get_crm_summary 工具：只读汇总客户 CRM 全景视图（主数据 / 订单 / 销售 / 工单）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.mcp.base import RiskLevel
from app.mcp.business.common import CustomerBrief
from app.mcp.database.common import DatabaseTool, get_customer_or_raise
from app.repositories.customer_ticket import CustomerTicketRepository
from app.repositories.order import OrderRepository
from app.repositories.sales_record import SalesRecordRepository


class CrmSummaryInput(BaseModel):
    customer_id: int = Field(ge=1, description="客户 ID")


class OrderStats(BaseModel):
    total_count: int = Field(description="订单总数")
    total_amount: float = Field(description="订单总金额")


class SalesStats(BaseModel):
    total_quantity: int = Field(description="销售总数量")
    total_amount: float = Field(description="销售总金额")
    month_count: int = Field(description="月度汇总条数")


class TicketStats(BaseModel):
    total_count: int = Field(description="工单总数")
    open_count: int = Field(description="未解决工单数（resolved_at 为空）")


class CrmSummaryOutput(BaseModel):
    customer: CustomerBrief
    order_stats: OrderStats
    sales_stats: SalesStats
    ticket_stats: TicketStats


class GetCrmSummaryTool(DatabaseTool[CrmSummaryInput, CrmSummaryOutput]):
    name = "get_crm_summary"
    required_permission = "customer:read"
    description = (
        "只读汇总客户 CRM 全景视图：客户主数据、订单统计（总数/总金额）、销售汇总"
        "（总数量/总金额/月度条数）与工单统计（总数/未解决数）；客户不存在返回 CUSTOMER_NOT_FOUND"
    )
    risk_level = RiskLevel.LOW
    timeout = 5.0
    InputModel = CrmSummaryInput
    OutputModel = CrmSummaryOutput

    async def run(self, params: CrmSummaryInput) -> CrmSummaryOutput:
        async with self.readonly_session() as session:
            customer = await get_customer_or_raise(session, params.customer_id)

            order_row = await OrderRepository(session).stats_by_customer(customer.id)
            sales_rows = await SalesRecordRepository(session).sum_by_product(customer_id=customer.id)
            ticket_row = await CustomerTicketRepository(session).stats_by_customer(customer.id)

            return CrmSummaryOutput(
                customer=CustomerBrief.model_validate(customer),
                order_stats=OrderStats(
                    total_count=int(order_row.total_count),
                    total_amount=float(order_row.total_amount or 0),
                ),
                sales_stats=SalesStats(
                    total_quantity=sum(int(r.total_quantity) for r in sales_rows),
                    total_amount=sum(float(r.total_amount) for r in sales_rows),
                    month_count=sum(int(r.month_count) for r in sales_rows),
                ),
                ticket_stats=TicketStats(
                    total_count=int(ticket_row.total_count),
                    open_count=int(ticket_row.open_count),
                ),
            )

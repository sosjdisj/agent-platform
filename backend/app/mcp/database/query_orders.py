"""query_orders 工具：只读查询订单明细（按客户 + 可选时间范围分页）。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.mcp.base import RiskLevel
from app.mcp.database.common import DatabaseTool, PeriodRange, get_customer_or_raise
from app.models.order import Order
from app.repositories.order import OrderRepository


class QueryOrdersInput(PeriodRange):
    customer_id: int = Field(ge=1, description="客户 ID")
    limit: int = Field(default=50, ge=1, le=200, description="分页大小")
    offset: int = Field(default=0, ge=0, description="分页偏移")


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_no: str
    customer_id: int
    product_id: int
    quantity: int
    amount: float
    order_date: datetime
    status: str


class QueryOrdersOutput(BaseModel):
    orders: list[OrderOut]
    count: int


class QueryOrdersTool(DatabaseTool[QueryOrdersInput, QueryOrdersOutput]):
    name = "query_orders"
    required_permission = "order:read"
    description = (
        "只读查询订单明细：按客户 ID（必填）与可选时间范围分页查询订单列表，"
        "客户不存在返回 CUSTOMER_NOT_FOUND"
    )
    risk_level = RiskLevel.SAFE
    timeout = 5.0
    InputModel = QueryOrdersInput
    OutputModel = QueryOrdersOutput

    async def run(self, params: QueryOrdersInput) -> QueryOrdersOutput:
        async with self.readonly_session() as session:
            await get_customer_or_raise(session, params.customer_id)
            orders = await OrderRepository(session).list_by_customer_period(
                params.customer_id,
                start=params.start_date,
                end=params.end_date,
                limit=params.limit,
                offset=params.offset,
            )
            return QueryOrdersOutput(
                orders=[OrderOut.model_validate(o) for o in orders],
                count=len(orders),
            )

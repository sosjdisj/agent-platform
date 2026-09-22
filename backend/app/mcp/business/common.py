"""business 子模块共享件：客户摘要模型与订单存在性校验。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.base import ToolError
from app.models.order import Order
from app.repositories.order import OrderRepository


class CustomerBrief(BaseModel):
    """客户主数据摘要（from_attributes 由 ORM 模型构造），供汇总 / 更新工具复用。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    industry: str | None
    region: str | None
    contact_name: str | None
    contact_phone: str | None
    status: str


async def get_order_or_raise(session: AsyncSession, order_id: int) -> Order:
    """按 ID 取订单，不存在则抛 ORDER_NOT_FOUND（错误码单一来源）。"""
    order = await OrderRepository(session).get(order_id)
    if order is None:
        raise ToolError("ORDER_NOT_FOUND", f"订单不存在: id={order_id}")
    return order

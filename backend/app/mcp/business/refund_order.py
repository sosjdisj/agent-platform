"""refund_order 工具：订单全额退款（HIGH 风险写操作，写入经 Service 层落库）。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.mcp.base import RiskLevel, RetryPolicy
from app.mcp.business.common import get_order_or_raise
from app.mcp.database.common import DatabaseTool
from app.services.order_service import OrderService


class RefundOrderInput(BaseModel):
    order_id: int = Field(ge=1, description="订单 ID")
    reason: str = Field(min_length=1, max_length=500, description="退款原因（退款记录留档）")


class OrderBrief(BaseModel):
    """订单摘要（from_attributes 由 ORM 模型构造）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_no: str
    customer_id: int
    product_id: int
    quantity: int
    amount: float
    order_date: datetime
    status: str


class RefundOrderOutput(BaseModel):
    order: OrderBrief
    already_refunded: bool = Field(description="调用前订单是否已退款（幂等重入标记）")


class RefundOrderTool(DatabaseTool[RefundOrderInput, RefundOrderOutput]):
    name = "refund_order"
    required_permission = "order:refund"
    description = (
        "订单全额退款（高风险写操作）：按订单当前金额写入退款记录并将订单标记为 refunded，"
        "需注明退款原因；重复调用幂等（不重复退款，返回 already_refunded=true）；"
        "订单不存在返回 ORDER_NOT_FOUND。成功响应携带 requires_approval=true（待人工确认）"
    )
    risk_level = RiskLevel.HIGH
    timeout = 5.0
    # 写操作不重试：瞬时错误重试可能重复执行非幂等写入，失败直接返回错误结构
    retry_policy = RetryPolicy(max_attempts=1)
    InputModel = RefundOrderInput
    OutputModel = RefundOrderOutput

    async def run(self, params: RefundOrderInput) -> RefundOrderOutput:
        async with self.session() as session:  # 读写会话（查询工具用 readonly_session）
            order = await get_order_or_raise(session, params.order_id)
            order, refund_created = await OrderService(session).refund_order(
                order, reason=params.reason
            )
            return RefundOrderOutput(
                order=OrderBrief.model_validate(order),
                already_refunded=not refund_created,
            )

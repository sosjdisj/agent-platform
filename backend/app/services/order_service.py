"""订单业务服务：退款等写入动作（写入统一走 Service 层，Repository 仅 flush）。"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.repositories.order import OrderRepository
from app.repositories.refund import RefundRepository


class OrderService:
    """订单域业务服务：封装写入动作与状态流转，事务提交在本层完成。"""

    def __init__(self, session: AsyncSession) -> None:
        self._orders = OrderRepository(session)
        self._refunds = RefundRepository(session)

    async def refund_order(self, order: Order, *, reason: str) -> tuple[Order, bool]:
        """全额退款订单（幂等）：写入退款记录并将订单标记为 refunded。

        已退款订单直接返回，不重复写入退款记录；调用方需先加载并确保订单存在。
        返回 (订单, 是否本次新写入退款)。
        """
        if order.status == "refunded":
            return order, False
        await self._refunds.create(order_id=order.id, reason=reason, amount=order.amount)
        await self._orders.update(order, status="refunded")
        await self._orders.session.commit()
        return order, True

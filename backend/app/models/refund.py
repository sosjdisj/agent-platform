"""退款记录表模型。"""
from decimal import Decimal

from sqlalchemy import BigInteger, Index, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import TimestampMixin


class Refund(Base, TimestampMixin):
    __tablename__ = "refunds"
    __table_args__ = (Index("ix_refunds_order_id", "order_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 逻辑外键 -> orders.id
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

"""客户工单表模型。"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import StatusMixin, TimestampMixin


class CustomerTicket(Base, TimestampMixin, StatusMixin):
    __tablename__ = "customer_tickets"
    __table_args__ = (Index("ix_customer_tickets_customer_id", "customer_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_no: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    customer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 逻辑外键 -> customers.id
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default="medium", server_default="medium"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

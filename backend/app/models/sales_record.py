"""销售记录表模型。"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Index, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import StatusMixin, TimestampMixin


class SalesRecord(Base, TimestampMixin, StatusMixin):
    __tablename__ = "sales_records"
    __table_args__ = (
        # 面向"按客户 + 时间范围"的核心查询场景
        Index("ix_sales_records_customer_id_sale_date", "customer_id", "sale_date"),
        Index("ix_sales_records_product_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 逻辑外键 -> customers.id
    product_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 逻辑外键 -> products.id
    quantity: Mapped[int] = mapped_column(nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    sale_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

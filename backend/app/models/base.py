"""模型公共 Mixin：时间戳与状态字段，供各业务模型复用。"""
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column


class TimestampMixin:
    """created_at / updated_at 字段，由数据库默认值与 ORM 自动维护。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class StatusMixin:
    """通用状态字段：active / disabled 等。"""

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )

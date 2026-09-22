"""database 子模块共享件：数据库工具基类、时间范围过滤参数与存在性校验。

供本子模块各查询工具复用，避免同一逻辑在多个工具中重复实现。
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Generic

from asyncpg.exceptions import PostgresConnectionError
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import SessionLocal
from app.mcp.base import BaseTool, InputT, OutputT, RetryPolicy, ToolError
from app.models.customer import Customer
from app.models.product import Product
from app.repositories.customer import CustomerRepository
from app.repositories.product import ProductRepository

# 瞬时（可重试）数据库异常：连接闪断 / 连接池获取超时；业务与数据错误不在此列
TRANSIENT_DB_ERRORS: tuple[type[Exception], ...] = (
    OperationalError,
    PoolTimeoutError,
    PostgresConnectionError,
)


class DatabaseTool(BaseTool[InputT, OutputT], Generic[InputT, OutputT]):
    """数据库工具基类：统一会话工厂注入、只读 / 读写事务会话与瞬时错误重试。

    查询工具经 readonly_session() 打开会话——事务首语句 SET TRANSACTION READ ONLY，
    写操作在数据库层被直接拒绝；写入类工具经 session() 打开读写会话，
    写入动作必须走 Service 层。
    """

    transient_errors = TRANSIENT_DB_ERRORS
    retry_policy = RetryPolicy(max_attempts=3, backoff_seconds=0.2)

    def __init__(self, session_maker: async_sessionmaker[AsyncSession] = SessionLocal) -> None:
        self._session_maker = session_maker

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """打开默认（读写）事务会话：供写入类工具使用，写入动作走 Service 层。"""
        async with self._session_maker() as session:
            yield session

    @asynccontextmanager
    async def readonly_session(self) -> AsyncGenerator[AsyncSession, None]:
        """打开只读事务会话：SET TRANSACTION READ ONLY 为事务首语句，写操作被数据库拒绝。"""
        async with self._session_maker() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            yield session


class PeriodRange(BaseModel):
    """可选时间范围（左闭右开）：start_date 含、end_date 不含。"""

    start_date: datetime | None = Field(
        default=None, description="时间下界（含），ISO 8601 格式"
    )
    end_date: datetime | None = Field(
        default=None, description="时间上界（不含），ISO 8601 格式"
    )

    @model_validator(mode="after")
    def _start_before_end(self) -> PeriodRange:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date >= self.end_date
        ):
            raise ValueError("start_date 必须早于 end_date（左闭右开区间）")
        return self


async def get_customer_or_raise(session: AsyncSession, customer_id: int) -> Customer:
    """按 ID 取客户，不存在则抛 CUSTOMER_NOT_FOUND（错误码单一来源）。"""
    customer = await CustomerRepository(session).get(customer_id)
    if customer is None:
        raise ToolError("CUSTOMER_NOT_FOUND", f"客户不存在: id={customer_id}")
    return customer


async def get_product_or_raise(session: AsyncSession, product_id: int) -> Product:
    """按 ID 取产品，不存在则抛 PRODUCT_NOT_FOUND（错误码单一来源）。"""
    product = await ProductRepository(session).get(product_id)
    if product is None:
        raise ToolError("PRODUCT_NOT_FOUND", f"产品不存在: id={product_id}")
    return product

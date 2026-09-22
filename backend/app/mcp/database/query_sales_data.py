"""query_sales_data 工具：只读查询客户月度销售时间序列（sales_records 月度汇总表）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.mcp.base import RiskLevel
from app.mcp.database.common import (
    DatabaseTool,
    PeriodRange,
    get_customer_or_raise,
    get_product_or_raise,
)
from app.repositories.product import ProductRepository
from app.repositories.sales_record import SalesRecordRepository


class QuerySalesDataInput(PeriodRange):
    customer_id: int = Field(ge=1, description="客户 ID")
    product_id: int | None = Field(default=None, ge=1, description="可选：仅统计指定产品")


class SalesMonthOut(BaseModel):
    sale_month: str = Field(description="销售月份，格式 YYYY-MM（数据粒度为月）")
    product_id: int
    sku: str
    product_name: str
    quantity: int
    amount: float


class QuerySalesDataOutput(BaseModel):
    customer_id: int
    rows: list[SalesMonthOut]
    count: int


class QuerySalesDataTool(DatabaseTool[QuerySalesDataInput, QuerySalesDataOutput]):
    name = "query_sales_data"
    required_permission = "sales:read"
    description = (
        "只读查询客户月度销售时间序列（sales_records 为月度汇总，sale_date 为该月最后一天）："
        "返回逐月 × 产品的销售数量与金额，支持可选时间范围（左闭右开）与产品过滤，"
        "客户或产品不存在返回 CUSTOMER_NOT_FOUND / PRODUCT_NOT_FOUND"
    )
    risk_level = RiskLevel.SAFE
    timeout = 5.0
    InputModel = QuerySalesDataInput
    OutputModel = QuerySalesDataOutput

    async def run(self, params: QuerySalesDataInput) -> QuerySalesDataOutput:
        async with self.readonly_session() as session:
            await get_customer_or_raise(session, params.customer_id)
            if params.product_id is not None:
                await get_product_or_raise(session, params.product_id)

            records = await SalesRecordRepository(session).list_by_customer_period(
                params.customer_id,
                start=params.start_date,
                end=params.end_date,
                product_id=params.product_id,
            )
            products = {
                p.id: p
                for p in await ProductRepository(session).list_by_ids(
                    sorted({r.product_id for r in records})
                )
            }
            rows = [
                SalesMonthOut(
                    sale_month=r.sale_date.strftime("%Y-%m"),
                    product_id=r.product_id,
                    sku=products[r.product_id].sku,
                    product_name=products[r.product_id].name,
                    quantity=r.quantity,
                    amount=float(r.amount),
                )
                for r in records
            ]
            return QuerySalesDataOutput(
                customer_id=params.customer_id, rows=rows, count=len(rows)
            )

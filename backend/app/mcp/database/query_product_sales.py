"""query_product_sales 工具：只读按产品聚合销售（总数量 / 总金额，可选客户与时间范围）。"""
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


class QueryProductSalesInput(PeriodRange):
    customer_id: int | None = Field(
        default=None, ge=1, description="可选：限定客户；不传则统计全部客户"
    )
    product_id: int | None = Field(default=None, ge=1, description="可选：仅统计指定产品")


class ProductSalesOut(BaseModel):
    product_id: int
    sku: str
    product_name: str
    total_quantity: int = Field(description="期间销售总数量")
    total_amount: float = Field(description="期间销售总金额")
    month_count: int = Field(description="命中的月度汇总条数")


class QueryProductSalesOutput(BaseModel):
    rows: list[ProductSalesOut]
    count: int


class QueryProductSalesTool(DatabaseTool[QueryProductSalesInput, QueryProductSalesOutput]):
    name = "query_product_sales"
    required_permission = "sales:read"
    description = (
        "只读按产品聚合销售数据（基于 sales_records 月度汇总）：统计各产品的销售总数量与总金额"
        "及覆盖月数，可选限定客户 / 时间范围（左闭右开）/ 单个产品，按总金额降序；"
        "客户或产品不存在返回 CUSTOMER_NOT_FOUND / PRODUCT_NOT_FOUND"
    )
    risk_level = RiskLevel.SAFE
    timeout = 5.0
    InputModel = QueryProductSalesInput
    OutputModel = QueryProductSalesOutput

    async def run(self, params: QueryProductSalesInput) -> QueryProductSalesOutput:
        async with self.readonly_session() as session:
            if params.customer_id is not None:
                await get_customer_or_raise(session, params.customer_id)
            if params.product_id is not None:
                await get_product_or_raise(session, params.product_id)

            agg_rows = await SalesRecordRepository(session).sum_by_product(
                customer_id=params.customer_id,
                start=params.start_date,
                end=params.end_date,
                product_id=params.product_id,
            )
            products = {
                p.id: p
                for p in await ProductRepository(session).list_by_ids(
                    [row.product_id for row in agg_rows]
                )
            }
            rows = [
                ProductSalesOut(
                    product_id=row.product_id,
                    sku=products[row.product_id].sku,
                    product_name=products[row.product_id].name,
                    total_quantity=int(row.total_quantity),
                    total_amount=float(row.total_amount),
                    month_count=int(row.month_count),
                )
                for row in agg_rows
            ]
            return QueryProductSalesOutput(rows=rows, count=len(rows))

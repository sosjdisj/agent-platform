"""query_customers 工具：只读查询客户主数据（按 ID 精确查 / 按关键词分页查）。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.mcp.base import RiskLevel
from app.mcp.database.common import DatabaseTool, get_customer_or_raise
from app.models.customer import Customer
from app.repositories.customer import CustomerRepository


class QueryCustomersInput(BaseModel):
    customer_id: int | None = Field(
        default=None, ge=1, description="按客户 ID 精确查询单个客户"
    )
    keyword: str | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="按客户名称/编码模糊查询（与 customer_id 互斥）",
    )
    limit: int = Field(default=20, ge=1, le=100, description="分页大小（仅列表查询生效）")
    offset: int = Field(default=0, ge=0, description="分页偏移（仅列表查询生效）")

    @model_validator(mode="after")
    def _customer_id_excludes_keyword(self) -> QueryCustomersInput:
        if self.customer_id is not None and self.keyword is not None:
            raise ValueError("customer_id 与 keyword 互斥，只能二选一")
        return self


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    industry: str | None
    region: str | None
    contact_name: str | None
    contact_phone: str | None
    status: str


class QueryCustomersOutput(BaseModel):
    customers: list[CustomerOut]
    count: int


class QueryCustomersTool(DatabaseTool[QueryCustomersInput, QueryCustomersOutput]):
    name = "query_customers"
    required_permission = "customer:read"
    description = (
        "只读查询客户主数据：customer_id 精确查单个客户"
        "（不存在返回 CUSTOMER_NOT_FOUND），或按 keyword 模糊分页查询客户列表"
    )
    risk_level = RiskLevel.SAFE
    timeout = 5.0
    InputModel = QueryCustomersInput
    OutputModel = QueryCustomersOutput

    async def run(self, params: QueryCustomersInput) -> QueryCustomersOutput:
        async with self.readonly_session() as session:
            repo = CustomerRepository(session)
            if params.customer_id is not None:
                customer = await get_customer_or_raise(session, params.customer_id)
                customers: list[Customer] = [customer]
            else:
                customers = list(
                    await repo.search(
                        params.keyword, limit=params.limit, offset=params.offset
                    )
                )
            return QueryCustomersOutput(
                customers=[CustomerOut.model_validate(c) for c in customers],
                count=len(customers),
            )

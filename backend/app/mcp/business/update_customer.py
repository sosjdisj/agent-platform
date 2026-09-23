"""update_customer 工具：更新客户主数据（HIGH 风险写操作，写入经 Service 层落库）。"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.mcp.base import RiskLevel, RetryPolicy
from app.mcp.business.common import CustomerBrief
from app.mcp.database.common import DatabaseTool, get_customer_or_raise
from app.services.crm_service import CrmService


class UpdateCustomerInput(BaseModel):
    """部分更新输入：仅提供的字段生效，至少提供一个待更新字段。"""

    customer_id: int = Field(ge=1, description="客户 ID")
    name: str | None = Field(default=None, min_length=1, max_length=100, description="客户名称")
    industry: str | None = Field(default=None, max_length=50, description="行业")
    region: str | None = Field(default=None, max_length=50, description="区域")
    contact_name: str | None = Field(default=None, max_length=50, description="联系人")
    contact_phone: str | None = Field(default=None, max_length=20, description="联系电话")

    @model_validator(mode="after")
    def _at_least_one_field(self) -> UpdateCustomerInput:
        if not self.model_dump(exclude={"customer_id"}, exclude_none=True):
            raise ValueError("至少提供一个待更新字段: name/industry/region/contact_name/contact_phone")
        return self


class UpdateCustomerOutput(BaseModel):
    customer: CustomerBrief


class UpdateCustomerTool(DatabaseTool[UpdateCustomerInput, UpdateCustomerOutput]):
    name = "update_customer"
    required_permission = "customer:write"
    description = (
        "更新客户主数据（高风险写操作，部分更新：仅提供的字段生效）——"
        "可更新 name/industry/region/contact_name/contact_phone，至少提供一个字段；"
        "客户不存在返回 CUSTOMER_NOT_FOUND。成功响应携带 requires_approval=true（待人工确认）。"
        "本环境为演示数据环境：调用即提交变更申请并进入平台人工审批，"
        "用户指令要素明确时应当直接调用，无需向用户二次确认"
    )
    risk_level = RiskLevel.HIGH
    timeout = 5.0
    # 写操作不重试：瞬时错误重试可能重复执行非幂等写入，失败直接返回错误结构
    retry_policy = RetryPolicy(max_attempts=1)
    InputModel = UpdateCustomerInput
    OutputModel = UpdateCustomerOutput

    async def run(self, params: UpdateCustomerInput) -> UpdateCustomerOutput:
        async with self.session() as session:  # 读写会话（查询工具用 readonly_session）
            customer = await get_customer_or_raise(session, params.customer_id)
            fields = params.model_dump(exclude={"customer_id"}, exclude_none=True)
            updated = await CrmService(session).update_customer(customer, **fields)
            return UpdateCustomerOutput(customer=CustomerBrief.model_validate(updated))

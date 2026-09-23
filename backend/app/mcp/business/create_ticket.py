"""create_ticket 工具：为客户创建售后工单（MEDIUM 风险写操作，写入经 Service 层落库）。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.mcp.base import RiskLevel, RetryPolicy
from app.mcp.database.common import DatabaseTool, get_customer_or_raise
from app.services.crm_service import CrmService


class CreateTicketInput(BaseModel):
    customer_id: int = Field(ge=1, description="客户 ID")
    title: str = Field(min_length=1, max_length=200, description="工单标题")
    content: str = Field(min_length=1, max_length=2000, description="工单内容（现象描述）")
    priority: Literal["low", "medium", "high", "urgent"] = Field(
        default="medium", description="优先级：low / medium / high / urgent（与客服规范分级一致）"
    )


class TicketOut(BaseModel):
    """工单结果（状态字段齐全：status / priority / resolved_at）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_no: str
    customer_id: int
    title: str
    content: str
    priority: str
    status: str
    resolved_at: datetime | None


class CreateTicketOutput(BaseModel):
    ticket: TicketOut


class CreateTicketTool(DatabaseTool[CreateTicketInput, CreateTicketOutput]):
    name = "create_ticket"
    required_permission = "ticket:create"
    description = (
        "为客户创建售后工单（写操作）：登记标题、内容与优先级（low/medium/high/urgent），"
        "系统生成 T-YYMM-NNN 工单号并落库；客户不存在返回 CUSTOMER_NOT_FOUND。"
        "这是标准客服流程操作，用户要求建单时直接调用"
    )
    risk_level = RiskLevel.MEDIUM
    timeout = 5.0
    # 写操作不重试：瞬时错误重试可能重复创建工单（非幂等），失败直接返回错误结构
    retry_policy = RetryPolicy(max_attempts=1)
    InputModel = CreateTicketInput
    OutputModel = CreateTicketOutput

    async def run(self, params: CreateTicketInput) -> CreateTicketOutput:
        async with self.session() as session:  # 读写会话（查询工具用 readonly_session）
            # 客户存在性校验与 database 子模块共用同一错误码来源
            await get_customer_or_raise(session, params.customer_id)
            ticket = await CrmService(session).create_ticket(
                customer_id=params.customer_id,
                title=params.title,
                content=params.content,
                priority=params.priority,
            )
            return CreateTicketOutput(ticket=TicketOut.model_validate(ticket))

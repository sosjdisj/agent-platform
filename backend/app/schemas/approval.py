"""审批中心接口的请求 / 响应模型（23.3）。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ApprovalResponse(BaseModel):
    """审批记录响应：工具 / 风险等级 / 参数摘要取自 payload（ApprovalService 写入约定），
    task_title 由路由层批量回查补全（审批记录本身不冗余存任务标题）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    task_title: str | None = None
    tool: str
    risk_level: str | None = None
    parameter_summary: str | None = None
    requester_id: int
    status: str
    decision: str | None
    comment: str | None
    created_at: datetime
    decided_at: datetime | None


class ApprovalDecideRequest(BaseModel):
    """审批决定入参：approve / reject 二选一，附注意见可选。"""

    decision: Literal["approved", "rejected"]
    comment: str | None = None

"""Agent 事件结构：任务频道消息的字段契约单一来源（Redis Pub/Sub 与 SSE 共用）。

同一结构在三个出口复用：
- agent_trace_events.payload（当前已写，与审批记录同事务）；
- Redis Pub/Sub 消息体与 SSE 推送（20.2 接线：TraceService / ApprovalService 发布）；
- 前端审批通知（Prompt 23.3）按本结构消费，字段约定见 README「审批事件结构」。
`event` 字段即 SSE 事件名，也是 agent_trace_events.event_type 的小写枚举值；
发布消息统一为「扁平 JSON + event 字段」：审批事件为事件 dump，其余轨迹事件为 TraceEvent。
"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.mcp.base import RiskLevel


class ApprovalRequiredEvent(BaseModel):
    """审批触发事件：HIGH 工具被审批门拦截，等待人工决定时写出。"""

    event: Literal["approval_required"] = "approval_required"
    task_id: int
    approval_id: int
    tool: str
    risk_level: RiskLevel
    requester_id: int  # 发起任务的用户（users.id）
    parameter_summary: str  # 请求参数 JSON（键排序），前端审批对话框展示用


class ApprovalResultEvent(BaseModel):
    """审批结果事件：approve / reject 决定落库时写出（与决定同事务写 Trace）。"""

    event: Literal["approval_result"] = "approval_result"
    task_id: int
    approval_id: int
    tool: str
    risk_level: RiskLevel | None = None  # 决定时从记录 payload 回读，历史记录可能缺失
    decision: Literal["approved", "rejected"]
    decided_by: int  # 审批人（users.id）
    comment: str | None = None


class TraceEvent(BaseModel):
    """通用轨迹事件：TraceService.append 落库后发布到任务频道的消息结构（20.2）。

    字段与 append 入参一一对应（task_started / agent_selected / llm_call /
    tool_called / tool_result / permission_denied / agent_finished / task_completed /
    task_failed 等），发布时排除空字段（与 payload 落库约定一致，不产生空值噪声）；
    approval_required / approval_result 不走本模型——其消息即审批事件 dump（见上）。
    """

    event: str  # agent_trace_events.event_type 小写枚举值，即 SSE 事件名
    task_id: int
    agent: str | None = None
    tool: str | None = None
    round: int | None = None
    duration_ms: int | None = None
    status: str | None = None
    parameter_summary: str | None = None
    result_summary: str | None = None
    error_code: str | None = None
    metadata: dict[str, Any] | None = None


class TraceItem(TraceEvent):
    """轨迹回放条目（23.4 时间线 REST）：TraceEvent + 落库 id（分页游标）+ 服务器时间戳。"""

    id: int
    timestamp: datetime

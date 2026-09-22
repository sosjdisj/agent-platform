"""Agent 状态与结果 Schema（Pydantic v2）：LangGraph 编排的状态与产出的单一数据源。

- AgentState：一次任务的完整执行状态；messages / tool_results / risk_assessments /
  agent_results / errors 带 operator.add reducer（LangGraph 多节点写入时追加而非覆盖），
  其余字段整体覆盖；reducer 元数据对 Pydantic 校验 / 序列化无影响
- AgentStatus / TASK_TRANSITIONS / TASK_TERMINAL_STATUSES：任务生命周期状态机
  （21.1，agent_tasks.status 的取值与合法流转的单一来源，TaskService 据此校验流转）
- AgentResult：单个 Agent 的执行结果（结论正文 + 引用来源 + 调用前风险识别）
- Source：结果引用来源（知识库分块 / 工具输出记录）
- RiskAssessment：调用前风险识别（工具 / 风险等级 / 待审批标注 / 参数摘要）
- ToolCallRecord：一次工具调用记录，result 复用 MCP ToolResult（错误码单一来源）
"""
from __future__ import annotations

import operator
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from app.mcp.base import RiskLevel, ToolResult


class Source(BaseModel):
    """AgentResult 的引用来源：知识库分块或工具输出记录。"""

    source_type: Literal["knowledge", "tool"]
    ref_id: str  # knowledge=chunk_id（对齐 DocumentChunk）；tool=工具名或工具输出的记录标识
    title: str | None = None  # knowledge=文档标题（document_title）
    score: float | None = None  # knowledge=重排相关性分
    document_id: str | None = None  # knowledge=所属文档 ID（tool 来源为空）


class RiskAssessment(BaseModel):
    """调用前风险识别：LLM 请求工具后在执行前生成的结构化标注。

    仅做识别输出（占位），不执行审批（不做 interrupt / 审批表）：
    requires_approval 由风险等级推导（规则见 app.mcp.base.approval_required，HIGH → true）。
    """

    tool: str
    risk_level: RiskLevel
    requires_approval: bool
    parameter_summary: str  # 调用参数摘要（JSON，键排序）


class AgentStatus(str, Enum):
    """任务生命周期状态（agent_tasks.status，存小写枚举值）。

    PENDING：任务已创建、尚未被执行器接单（21.1）；RUNNING：执行中（state 创建即
    运行中）；CANCELLED：人工取消的终态。PARTIAL 不进入状态机，仅作为 AgentResult
    的结果语义：流程完成但未获得有依据的完整答案（如检索不充分兜底）。
    WAITING_APPROVAL：HIGH 工具执行前触发 interrupt（审批流接线后），任务停在
    checkpoint 等待人工审批，恢复（resume）后续跑。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLED = "cancelled"
    PARTIAL = "partial"


# 任务状态机（21.1）：合法流转的单一来源，TaskService.transition 流转前据此校验。
# pending 接单后进入 running；waiting_approval 审批决定后回 running 续跑（拒绝则
# 走 fallback 报告，异常时 failed）；任一非终态可取消；三个终态不可再流转。
TASK_TRANSITIONS: dict[AgentStatus, frozenset[AgentStatus]] = {
    AgentStatus.PENDING: frozenset({AgentStatus.RUNNING, AgentStatus.CANCELLED}),
    AgentStatus.RUNNING: frozenset(
        {
            AgentStatus.WAITING_APPROVAL,
            AgentStatus.COMPLETED,
            AgentStatus.FAILED,
            AgentStatus.CANCELLED,
        }
    ),
    AgentStatus.WAITING_APPROVAL: frozenset(
        {AgentStatus.RUNNING, AgentStatus.FAILED, AgentStatus.CANCELLED}
    ),
    AgentStatus.COMPLETED: frozenset(),
    AgentStatus.FAILED: frozenset(),
    AgentStatus.CANCELLED: frozenset(),
}

# 终态集合：进入即写 finished_at，且不再接受任何流转
TASK_TERMINAL_STATUSES: frozenset[AgentStatus] = frozenset(
    {AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.CANCELLED}
)


class AgentResult(BaseModel):
    """单个 Agent 的执行结果：结论正文 + 引用来源。"""

    agent: str  # Agent 名称（与 agent_trace_events.agent 一致）
    content: str  # 结论正文（自然语言）
    sources: list[Source] = []
    status: AgentStatus = AgentStatus.COMPLETED  # 结果语义：completed=有依据的答案；partial=如实兜底
    error_code: str | None = None  # status=partial 时的机器可读原因（如 NO_RELEVANT_DOCUMENT）
    risk_assessments: list[RiskAssessment] = []  # 调用前风险识别（HIGH 工具 requires_approval=true）


class ToolCallRecord(BaseModel):
    """一次工具调用记录：调用信息 + 统一结果（result 复用 MCP ToolResult，错误码单一来源）。"""

    tool: str
    arguments: dict[str, Any] = {}
    result: ToolResult


class AgentState(BaseModel):
    """LangGraph 编排状态：一次任务的全部执行上下文与产出。"""

    task_id: int  # agent_tasks.id
    user_id: int  # 发起用户（users.id）
    messages: Annotated[list[dict[str, Any]], operator.add]  # OpenAI 消息格式，直接兼容 LLMService.chat
    current_agent: str | None = None  # 当前执行的 Agent 名称
    tool_results: Annotated[list[ToolCallRecord], operator.add] = []  # 工具调用记录（追加）
    risk_assessments: Annotated[list[RiskAssessment], operator.add] = []  # 调用前风险识别（追加）
    task_context: dict[str, Any] = {}  # 任务上下文（title / query / 共享中间结论等）
    agent_results: Annotated[list[AgentResult], operator.add] = []  # 各 Agent 产出（追加）
    status: AgentStatus = AgentStatus.RUNNING
    round: int = Field(default=1, ge=1)  # 正在执行的 LLM 决策轮次（从 1 起）
    errors: Annotated[list[str], operator.add] = []  # 累计错误信息（失败原因按顺序追加）
    final_result: AgentResult | None = None  # 最终汇总结果（任务完成时写入）

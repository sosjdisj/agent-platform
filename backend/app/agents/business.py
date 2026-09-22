"""BusinessAgent：CRM 业务操作 Agent（含高风险工具调用与风险识别输出）。

流程（复用基础 LLM↔Tool 图，不重复实现循环 / 错误处理，见 app/agents/graph.py）：
LLM 判断信息需求 → 查客户 CRM 全景（get_crm_summary，LOW 只读）/ 创建售后工单
（create_ticket，MEDIUM 写操作）→ 需要时执行高风险操作（update_customer / refund_order，
HIGH 写操作）→ 汇总作答；无需工具的场景（寒暄 / 仅咨询）直接回答。

- 工具面白名单校验：4 个业务工具且风险等级与白名单声明一致（构造时强制校验，
  非白名单工具或错误的风险标注无法混入）；
- 调用前风险识别（14.2）：每个合法工具请求在执行前由图层生成 RiskAssessment
  （risk_level / requires_approval / parameter_summary），随 AgentResult 透出；
- 审批门控（18.2–18.3，approval_session_maker 显式接线后生效）：HIGH 工具（update_customer /
  refund_order）执行前创建审批记录（agent_approvals，status=pending）并 interrupt 中断，
  任务状态置 waiting_approval，checkpoint 保留完整上下文等待人工审批；审批决定经
  ApprovalService approve / reject 落库后由 resume(task_id) 恢复续跑（批准 → 工具执行；
  拒绝 → fallback 报告标记 partial），未接线时 HIGH 工具直接执行（存量行为不变），
  ToolResult 携带 requires_approval=true；
- 写操作不重试：create_ticket / update_customer / refund_order 均非幂等（重试可能重复写入），
  graph 层不注入 tool_retry_times（默认 0），与各工具 retry_policy max_attempts=1 一致；
- 不做：Supervisor 多 Agent 路由；
- 独立调试：python -m app.agents.business "客户 1 的 CRM 概况怎么样"
  （HTTP 调试接口见 app/api/routes/debug_agents.py）。
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

from fastmcp import FastMCP
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.runner import AgentRunner
from app.agents.sources import successful_tool_sources
from app.agents.state import AgentResult, AgentState, AgentStatus
from app.core.llm import LLMService, get_llm_service
from app.core.redis import RedisService
from app.db.session import SessionLocal
from app.mcp.base import BaseTool, RiskLevel
from app.mcp.client import MCPClient
from app.mcp.business.create_ticket import CreateTicketTool
from app.mcp.business.get_crm_summary import GetCrmSummaryTool
from app.mcp.business.refund_order import RefundOrderTool
from app.mcp.business.update_customer import UpdateCustomerTool
from app.mcp.registry import ToolRegistry

BUSINESS_AGENT_NAME = "business"

# 业务白名单：工具名 → 声明风险等级（名称与注册顺序的唯一来源；校验时名称与风险需同时命中）
BUSINESS_TOOL_RISKS = {
    "get_crm_summary": RiskLevel.LOW,
    "create_ticket": RiskLevel.MEDIUM,
    "update_customer": RiskLevel.HIGH,
    "refund_order": RiskLevel.HIGH,
}

# 工具面名称清单（顺序与注册一致，供测试与调试断言）
BUSINESS_TOOLS = tuple(BUSINESS_TOOL_RISKS)

_BUSINESS_TOOL_CLASSES = (
    GetCrmSummaryTool,
    CreateTicketTool,
    UpdateCustomerTool,
    RefundOrderTool,
)

# 业务操作系统提示：按信息需求选工具、客户 ID 未知先确认、高风险操作先核对、待审批如实说明
BUSINESS_SYSTEM_PROMPT = (
    "你是企业 CRM 业务助手，可使用以下工具完成客户服务："
    "了解客户整体情况（主数据 / 订单 / 销售 / 工单统计）用 get_crm_summary（需客户 ID）；"
    "为客户创建售后工单用 create_ticket（需客户 ID、标题、内容，可选优先级 low/medium/high/urgent）；"
    "更新客户主数据用 update_customer（高风险，需客户 ID 和至少一个待更新字段）；"
    "订单全额退款用 refund_order（高风险，需订单 ID 和退款原因）。"
    "客户 ID 未知时先向用户确认，不要猜测或编造；执行更新 / 退款前需与用户核对清楚；"
    "高风险操作提交后系统会标记待人工审批，回复中如实说明已提交待审批，不要声称立即生效；"
    "创建工单成功后如实回报工单号与状态；工具报错或信息不足时如实说明。"
)


class BusinessAgentError(RuntimeError):
    """BusinessAgent 执行失败：携带状态中累计的错误信息。"""


class BusinessAgentWaiting(BusinessAgentError):
    """HIGH 工具等待人工审批：图正常结束且状态为 waiting_approval，编排层据此中断并转待审批。"""


def validate_business_tools(tools: Sequence[BaseTool]) -> None:
    """白名单校验：工具名必须命中白名单且风险等级与白名单声明一致。

    防止非白名单工具混入，或工具风险标注与白名单声明不符（如 create_ticket 标成 HIGH）。
    """
    for tool in tools:
        expected = BUSINESS_TOOL_RISKS.get(tool.name)
        if expected is None or tool.risk_level is not expected:
            raise ValueError(f"工具 {tool.name} 不在业务白名单内或风险等级与声明不符")


def assemble_business_result(state: AgentState) -> AgentResult:
    """组装业务 Agent 输出：正文与状态语义取 final_result（completed / partial，
    如审批拒绝的 fallback 报告，见 18.3）；数据来源取成功工具调用
    （规则见 app/agents/sources.py：ref_id=工具名(参数摘要)）；调用前风险识别随结果透出
    （HIGH 工具 requires_approval=true 待审批占位）；执行失败抛 BusinessAgentError。
    """
    if state.status is AgentStatus.WAITING_APPROVAL:
        raise BusinessAgentWaiting("HIGH 工具等待审批，任务暂停于 waiting_approval")
    if state.status is not AgentStatus.COMPLETED or state.final_result is None:
        raise BusinessAgentError("；".join(state.errors) or "任务未完成即结束")

    return state.final_result.model_copy(
        update={
            "agent": BUSINESS_AGENT_NAME,
            "sources": list(successful_tool_sources(state).values()),
            "risk_assessments": state.risk_assessments,
        }
    )


class BusinessAgent:
    """CRM 业务操作 Agent：run / resume 语义与状态持久化全部复用 AgentRunner。"""

    def __init__(
        self,
        llm: LLMService,
        *,
        session_maker: async_sessionmaker[AsyncSession] = SessionLocal,
        checkpointer: BaseCheckpointSaver | None = None,
        max_rounds: int | None = None,
        auth_session_maker: async_sessionmaker[AsyncSession] | None = None,
        approval_session_maker: async_sessionmaker[AsyncSession] | None = None,
        trace_session_maker: async_sessionmaker[AsyncSession] | None = None,
        trace_redis: RedisService | None = None,
    ) -> None:
        tools = [cls(session_maker=session_maker) for cls in _BUSINESS_TOOL_CLASSES]
        validate_business_tools(tools)
        registry = ToolRegistry()
        registry.register(*tools)
        server = FastMCP("business-agent")
        registry.mount_to(server)
        self._registry = registry
        self._runner = AgentRunner(
            llm,
            MCPClient(server=server, registry=registry),
            registry,
            checkpointer,
            max_rounds=max_rounds,
            auth_session_maker=auth_session_maker,
            approval_session_maker=approval_session_maker,
            trace_session_maker=trace_session_maker,
            trace_redis=trace_redis,
            trace_task_events=False,  # 任务级埋点归 TaskExecutor（21.2），本层只写节点级事件
        )

    @property
    def tool_names(self) -> tuple[str, ...]:
        """当前工具面（供测试与调试核对白名单）。"""
        return tuple(tool.name for tool in self._registry.all_tools())

    async def answer(self, task_id: int, user_id: int, query: str) -> AgentResult:
        """处理一次业务请求：全流程执行并组装带工具来源的结果（失败抛 BusinessAgentError）。"""
        state = await self._runner.run(
            task_id,
            user_id,
            [
                {"role": "system", "content": BUSINESS_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
        )
        return assemble_business_result(state)

    async def resume(self, task_id: int) -> AgentResult:
        """恢复被审批中断的任务（18.3）：决定经 ApprovalService 落库后从 checkpoint
        续跑并组装结果（批准 → 工具执行继续；拒绝 → fallback 报告标记 partial）。"""
        return assemble_business_result(await self._runner.resume(task_id))


def main() -> None:
    """CLI 独立调试入口（不经业务编排）：python -m app.agents.business "客户 1 的 CRM 概况"。

    依赖：.env 配置 LLM_* 与数据库连接。
    task_id / user_id 取 0（调试语义，不关联业务任务）。
    """
    if len(sys.argv) < 2:
        raise SystemExit("用法：python -m app.agents.business <查询文本>")

    query = " ".join(sys.argv[1:])
    agent = BusinessAgent(get_llm_service())  # 默认 SessionLocal（真实数据库）
    result = asyncio.run(agent.answer(task_id=0, user_id=0, query=query))
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()

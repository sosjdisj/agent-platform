"""DataAgent：数据查询 Agent（基础版 + 多工具组合 / 异常兜底）。

流程（复用基础 LLM↔Tool 图，不重复实现循环 / 错误处理，见 app/agents/graph.py）：
LLM 自主从 4 个只读数据工具中选择调用（tool calling 决策）→ 按需多轮组合查询
（客户资料 / 订单明细 / 月度销售趋势 / 产品聚合汇总，可对比分析）→ LLM 汇总数据作答；
无需数据的场景（寒暄等）直接回答。

- 只读约束双重保障：工具面白名单校验（仅 4 个只读查询工具且风险等级 SAFE，
  构造时强制校验，写入类工具无法混入）+ 工具自身 readonly_session
  （数据库层拒绝写操作，见 app/mcp/database/common.py）；
- 无数据兜底：全部成功查询的输出均为空（count=0）→ AgentResult status=partial
  + error_code=NO_DATA（如实说明，与知识检索 NO_RELEVANT_DOCUMENT 同模式）；
- 工具错误重试：失败原参重试 1 次（graph 层 tool_retry_times），仍失败才进入 failed；
- 不做：BusinessAgent、Supervisor 多 Agent 路由；
- 独立调试：python -m app.agents.data "对比客户 A 和 B 最近销售额"
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
from app.mcp.database.query_customers import QueryCustomersTool
from app.mcp.database.query_orders import QueryOrdersTool
from app.mcp.database.query_product_sales import QueryProductSalesTool
from app.mcp.database.query_sales_data import QuerySalesDataTool
from app.mcp.registry import ToolRegistry

DATA_AGENT_NAME = "data"

# 只读白名单：DataAgent 工具面仅允许这 4 个只读数据查询工具（名称 + 注册顺序的唯一来源）
READ_ONLY_DATA_TOOLS = (
    "query_customers",
    "query_orders",
    "query_sales_data",
    "query_product_sales",
)

_READONLY_TOOL_CLASSES = (
    QueryCustomersTool,
    QueryOrdersTool,
    QuerySalesDataTool,
    QueryProductSalesTool,
)

# 数据查询系统提示：按信息需求选工具、客户名称先解析为 ID、可多轮组合对比、只依据数据作答
DATA_SYSTEM_PROMPT = (
    "你是企业数据查询分析助手，只做只读查询，依据工具返回的数据回答："
    "查客户资料或把客户名称解析为客户 ID 用 query_customers（keyword 支持名称/编码模糊匹配）；"
    "查订单明细用 query_orders（需客户 ID，可选时间范围）；"
    "查客户月度销售趋势用 query_sales_data（需客户 ID，可选时间范围/产品）；"
    "查按产品聚合的销量与金额用 query_product_sales（可选客户/时间范围/产品）。"
    "允许组合多轮调用：对比分析（如客户之间、产品之间、不同期间）请分别查询相关数据后对比说明；"
    "查询结果为空或客户不存在时如实说明，不要编造数字。"
)

# 无数据兜底错误码：全部成功查询的输出均为空（count=0）时的结果语义
NO_DATA = "NO_DATA"


class DataAgentError(RuntimeError):
    """DataAgent 执行失败：携带状态中累计的错误信息。"""


def validate_readonly_tools(tools: Sequence[BaseTool]) -> None:
    """只读白名单校验：工具名必须命中只读清单且风险等级为 SAFE。

    防止写入类 / 非白名单工具混入 DataAgent 工具面（数据库层另有 readonly_session 兜底）。
    """
    for tool in tools:
        if tool.name not in READ_ONLY_DATA_TOOLS or tool.risk_level is not RiskLevel.SAFE:
            raise ValueError(f"工具 {tool.name} 不在只读白名单内或风险等级非 SAFE")


def assemble_data_result(state: AgentState) -> AgentResult:
    """组装数据 Agent 输出：正文取 final_result；数据来源取成功工具调用
    （规则见 app/agents/sources.py：ref_id=工具名(参数摘要)，区分对比场景的同工具不同查询）。

    结果语义：调用过工具且所有成功输出的 count 均为 0（全部查询无数据）
    → status=partial + error_code=NO_DATA（如实兜底）；任一查询命中数据或
    未调用工具（直接回答）→ completed。
    """
    if state.status is not AgentStatus.COMPLETED or state.final_result is None:
        raise DataAgentError("；".join(state.errors) or "任务未完成即结束")

    counts: list[int] = []
    for record in state.tool_results:
        data = record.result.data or {}
        if record.result.success and "count" in data:
            counts.append(data["count"])

    no_data = bool(counts) and all(count == 0 for count in counts)
    return AgentResult(
        agent=DATA_AGENT_NAME,
        content=state.final_result.content,
        sources=list(successful_tool_sources(state).values()),
        status=AgentStatus.PARTIAL if no_data else AgentStatus.COMPLETED,
        error_code=NO_DATA if no_data else None,
    )


class DataAgent:
    """数据查询 Agent：run / resume 语义与状态持久化全部复用 AgentRunner。"""

    def __init__(
        self,
        llm: LLMService,
        *,
        session_maker: async_sessionmaker[AsyncSession] = SessionLocal,
        checkpointer: BaseCheckpointSaver | None = None,
        max_rounds: int | None = None,
        tool_retry_times: int = 1,
        auth_session_maker: async_sessionmaker[AsyncSession] | None = None,
        trace_session_maker: async_sessionmaker[AsyncSession] | None = None,
        trace_redis: RedisService | None = None,
    ) -> None:
        tools = [cls(session_maker=session_maker) for cls in _READONLY_TOOL_CLASSES]
        validate_readonly_tools(tools)
        registry = ToolRegistry()
        registry.register(*tools)
        server = FastMCP("data-agent")
        registry.mount_to(server)
        self._registry = registry
        self._runner = AgentRunner(
            llm,
            MCPClient(server=server, registry=registry),
            registry,
            checkpointer,
            max_rounds=max_rounds,
            tool_retry_times=tool_retry_times,
            auth_session_maker=auth_session_maker,
            trace_session_maker=trace_session_maker,
            trace_redis=trace_redis,
            trace_task_events=False,  # 任务级埋点归 TaskExecutor（21.2），本层只写节点级事件
        )

    @property
    def tool_names(self) -> tuple[str, ...]:
        """当前工具面（供测试与调试核对白名单）。"""
        return tuple(tool.name for tool in self._registry.all_tools())

    async def answer(self, task_id: int, user_id: int, query: str) -> AgentResult:
        """回答一次数据查询：全流程执行并组装带数据来源的结果（失败抛 DataAgentError）。"""
        state = await self._runner.run(
            task_id,
            user_id,
            [{"role": "system", "content": DATA_SYSTEM_PROMPT}, {"role": "user", "content": query}],
        )
        return assemble_data_result(state)


def main() -> None:
    """CLI 独立调试入口（不经业务编排）：python -m app.agents.data "客户 A 最近销售额怎么样"。

    依赖：.env 配置 LLM_* 与数据库连接。
    task_id / user_id 取 0（调试语义，不关联业务任务）。
    """
    if len(sys.argv) < 2:
        raise SystemExit("用法：python -m app.agents.data <查询文本>")

    query = " ".join(sys.argv[1:])
    agent = DataAgent(get_llm_service())  # 默认 SessionLocal（真实数据库）
    result = asyncio.run(agent.answer(task_id=0, user_id=0, query=query))
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()

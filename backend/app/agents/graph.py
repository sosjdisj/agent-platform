"""基础 LangGraph 图：START → LLM 决策 → 条件边（需要工具 → Tool 节点 → 回 LLM；否则 → END）。

- 轮次：round 为正在执行的 LLM 决策轮次（从 1 起）；模型第 max_rounds 轮仍要求工具时
  截断为 failed（该轮工具不再执行），防止无限循环；
- 工具重试：tool_retry_times 次（默认 0 不重试）——工具调用失败时原参重调，重试耗尽仍失败
  才进入 failed 链路；重试对 LLM 与 tool_results 透明（仅保留最终结果）；
- 调用前风险识别：每个合法工具请求在执行前按注册表元数据生成 RiskAssessment
  （risk_level / requires_approval / parameter_summary），随状态累计供 AgentResult 透出；
- 工具调用经 MCPClient（失败归一化为统一错误结构）：任何工具失败记录进 tool_results 与
  errors，状态置 failed 后经条件边直接结束，全部可观测；
- 工具权限门控（RBAC）：声明 required_permission 的工具执行前校验用户权限
  （auth_session_maker 显式接线后生效，缺省不校验），无权限不执行，返回
  PERMISSION_DENIED 并写 permission_denied 轨迹事件；
- 审批门控（HITL，approval_session_maker 显式接线后生效）：requires_approval（HIGH）的
  工具在权限校验通过后、执行前先过审批门——无同操作记录则创建 agent_approvals 记录
  （status=pending）并 interrupt 中断（工具不执行），checkpoint 保留完整上下文等待人工
  审批，任务状态置 waiting_approval（见 AgentRunner）；人工决定经 ApprovalService
  approve/reject 落库后由 AgentRunner.resume 恢复：批准 → 工具执行；拒绝 → 返回
  APPROVAL_REJECTED（不计入执行错误），LLM 生成不含该操作的报告并标记 partial；
  未决定即恢复则继续等待（审批门幂等，恢复重入按 DB 最新记录决定）；
- 状态持久化可选：注入 checkpointer（app/agents/checkpoint.py）则按 thread_id = task_id
  持久化，支持中断后恢复；不传则内存执行，状态由调用方持有；
- 轨迹埋点（19.2，trace_session_maker 显式接线后生效）：LLM 调用（llm_call）、工具调用
  （tool_called / tool_result，与 state.tool_results 一一对应）、权限拒绝（permission_denied）
  统一经 TraceService 追加；任务起止与 Agent 起止在 AgentRunner 层埋点。未接线不写
  （测试 / 调试路径行为不变）；trace_redis 显式接线后追加同时发布任务事件（20.2），
  审批创建（approval_required）同样发布；
- LLM / MCP / 注册表 / checkpointer 均由构造方注入。
"""
from __future__ import annotations

import json
import time
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from openai.types.chat import ChatCompletionToolParam
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.state import (
    AgentResult,
    AgentState,
    AgentStatus,
    RiskAssessment,
    ToolCallRecord,
)
from app.core.config import get_settings
from app.core.llm import LLMService
from app.core.redis import RedisService
from app.mcp.base import ToolResult, approval_required
from app.mcp.client import MCPClient
from app.mcp.registry import ToolRegistry
from app.models.agent import AgentTraceEventType
from app.services.approval_service import PENDING, ApprovalService
from app.services.rbac_service import get_user_permissions
from app.services.trace_service import TraceService

# 基础图的 Agent 名称（写入 AgentResult.agent 与 current_agent，供 Trace 对齐）
BASE_AGENT_NAME = "assistant"

# 审批拒绝错误码：被拒工具不执行、不计入执行错误（走 fallback 报告，结果标记 partial）
APPROVAL_REJECTED = "APPROVAL_REJECTED"


def elapsed_ms(started: float) -> int:
    """perf_counter 起点至今的毫秒数（轨迹 duration_ms 用）。"""
    return round((time.perf_counter() - started) * 1000)


def _summary_text(text: str | None) -> str | None:
    """结果摘要：截断到 200 字符（轨迹 payload 保持精简）。"""
    return text[:200] if text else None


def _result_digest(value: Any, depth: int = 0) -> str | None:
    """成功结果的结构化摘要：列表字段只记条数，标量字段记 key=value（值截断）。

    轨迹只承载结构性观测信息，不携带行级数据（与 llm_call 不渲染 CoT 同一口径），
    避免原始数据库内容经轨迹写入存储并推送前端；完整结果仍经 tool_messages 交给 LLM。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            if isinstance(item, list):
                parts.append(f"{key}×{len(item)}")
            elif isinstance(item, dict):
                nested = _result_digest(item, depth + 1) if depth < 2 else None
                parts.append(f"{key}{{{nested}}}" if nested else f"{key}:{{…}}")
            else:
                parts.append(f"{key}={str(item)[:20]}")
        return "，".join(parts) or None
    if isinstance(value, list):
        return f"共 {len(value)} 条"
    return str(value)[:200]


def _openai_tools(registry: ToolRegistry) -> list[ChatCompletionToolParam]:
    """注册表工具元数据 → OpenAI function calling 定义（JSON Schema 单一数据源）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.metadata.input_schema,
            },
        }
        for tool in registry.all_tools()
    ]


def _route_after_llm(state: AgentState) -> str:
    """LLM 后路由：失败即结束；最后一条消息带 tool_calls 则去工具节点，否则结束。"""
    if state.status is AgentStatus.FAILED:
        return END
    return "tools" if state.messages[-1].get("tool_calls") else END


def _route_after_tools(state: AgentState) -> str:
    """工具后路由：工具失败（failed 可观测）即结束，否则回 LLM 继续决策。"""
    return END if state.status is AgentStatus.FAILED else "llm"


def _risk_assessment(
    registry: ToolRegistry, name: str, arguments: dict[str, Any]
) -> RiskAssessment | None:
    """调用前风险识别：按注册表元数据生成（未知工具不标注，TOOL_NOT_FOUND 由 MCPClient 归一化）。"""
    if not registry.has(name):
        return None
    risk_level = registry.get(name).metadata.risk_level
    return RiskAssessment(
        tool=name,
        risk_level=risk_level,
        requires_approval=approval_required(risk_level),
        parameter_summary=json.dumps(arguments, ensure_ascii=False, sort_keys=True),
    )


def build_agent_graph(
    llm: LLMService,
    mcp: MCPClient,
    registry: ToolRegistry,
    *,
    max_rounds: int | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    tool_retry_times: int = 0,
    auth_session_maker: async_sessionmaker[AsyncSession] | None = None,
    approval_session_maker: async_sessionmaker[AsyncSession] | None = None,
    trace_session_maker: async_sessionmaker[AsyncSession] | None = None,
    trace_redis: RedisService | None = None,
):
    """构建基础 Agent 图；依赖全部注入（测试可替换 LLM / MCP / 注册表）。

    checkpointer 注入后运行时必须携带 thread_id（agent_thread_config(task_id)）。
    tool_retry_times：工具失败后的原参重试次数（默认 0 不重试）。
    auth_session_maker：工具权限门控（RBAC）的数据源。**显式接线**：None（默认）不校验，
    供测试 / 调试路径复用；组合根（API 运行入口、Supervisor 默认构造）传入会话工厂开启。
    approval_session_maker：审批门控（HITL）的数据源。**显式接线**：None（默认）HIGH 工具
    直接执行（存量行为不变）；传入后 HIGH 工具执行前创建审批记录并中断任务。
    trace_session_maker：轨迹埋点（19.2）的数据源。**显式接线**：None（默认）不写轨迹；
    传入后节点事件（llm_call / tool_called / tool_result / permission_denied）经
    TraceService 追加落库。
    trace_redis：任务事件发布（20.2）的通道。**显式接线**：None（默认）只落库不发布；
    传入后轨迹追加与审批创建同时发布到任务频道（审批决定由 ApprovalService 发布）。
    """
    max_rounds = max_rounds if max_rounds is not None else get_settings().agent_max_rounds
    tools = _openai_tools(registry)

    async def write_trace(
        state: AgentState, event_type: AgentTraceEventType, /, **fields: Any
    ) -> None:
        """追加一条节点轨迹（未接线不写）；短会话独立提交，agent 归属当前执行 Agent，
        接线 trace_redis 时落库同时发布任务事件（20.2）。"""
        if trace_session_maker is None:
            return
        async with trace_session_maker() as session:
            await TraceService(session, redis=trace_redis).append(
                task_id=state.task_id,
                event_type=event_type,
                agent=state.current_agent or BASE_AGENT_NAME,
                **fields,
            )

    async def call_llm(state: AgentState) -> dict[str, Any]:
        """LLM 节点：模型决策一轮；直接给出答案则完成，要求工具则进入下一轮，异常/超限置失败。"""
        started = time.perf_counter()
        try:
            response = await llm.chat(state.messages, tools=tools)
        except Exception as exc:  # LLM 网络等异常：记录并终止，不向上抛
            await write_trace(
                state,
                AgentTraceEventType.LLM_CALL,
                round=state.round,
                duration_ms=elapsed_ms(started),
                status="failed",
                metadata={"error": str(exc)},
            )
            return {"status": AgentStatus.FAILED, "errors": [f"LLM 调用失败: {exc}"]}
        await write_trace(
            state,
            AgentTraceEventType.LLM_CALL,
            round=state.round,
            duration_ms=elapsed_ms(started),
            status="success",
        )

        message = response.choices[0].message
        update: dict[str, Any] = {"messages": [message.model_dump(exclude_none=True)]}

        if not message.tool_calls:  # 无工具请求 = 最终答案
            # 有被审批拒绝的操作时结果为如实兜底（partial）：报告不含该操作
            rejected = any(
                record.result.error_code == APPROVAL_REJECTED for record in state.tool_results
            )
            update["status"] = AgentStatus.COMPLETED
            update["final_result"] = AgentResult(
                agent=BASE_AGENT_NAME,
                content=message.content or "",
                sources=[],
                status=AgentStatus.PARTIAL if rejected else AgentStatus.COMPLETED,
                error_code=APPROVAL_REJECTED if rejected else None,
            )
            return update

        if state.round >= max_rounds:  # 最后一轮仍要求工具：截断，不再执行工具
            update["status"] = AgentStatus.FAILED
            update["errors"] = [f"已达最大轮次上限（{max_rounds}），任务截断"]
            return update

        update["round"] = state.round + 1  # 下一轮 LLM 决策
        return update

    async def run_tools(state: AgentState) -> dict[str, Any]:
        """Tool 节点：执行最后一条助手消息的全部工具调用；失败归一化进 errors 并置 failed。

        每个合法工具请求在调用前生成风险识别（不阻断执行，仅随状态累计供结果透出）；
        声明了 required_permission 的工具先经 RBAC 权限门控：无权限 → 不执行，
        返回 PERMISSION_DENIED 并写一条 permission_denied 轨迹事件（审计可查）；
        requires_approval（HIGH）工具在权限通过后、执行前过审批门（审批流接线后生效）：
        无决定则 interrupt 中断（工具不执行，checkpoint 保留完整状态等待人工审批），
        被拒绝则返回 APPROVAL_REJECTED（不计入执行错误，流程回 LLM 走 fallback 报告）。
        """
        records: list[ToolCallRecord] = []
        tool_messages: list[dict[str, Any]] = []
        errors: list[str] = []
        assessments: list[RiskAssessment] = []
        permissions: set[str] | None = None  # 本轮权限集合懒解析（同一用户一轮内复用）

        async def request_approval(assessment: RiskAssessment) -> AgentApproval:
            """创建待审批记录（ApprovalService 内部提交事务，接线 trace_redis 时发布事件）。"""
            async with approval_session_maker() as session:
                return await ApprovalService(session, redis=trace_redis).create(
                    task_id=state.task_id,
                    requester_id=state.user_id,
                    tool=assessment.tool,
                    parameter_summary=assessment.parameter_summary,
                    risk_level=assessment.risk_level,
                )

        async def approval_gate(assessment: RiskAssessment) -> str:
            """审批门（幂等）：返回审批记录状态——approved 放行执行，rejected 拒绝。

            首次执行无同操作记录（task + tool + parameter_summary）则创建 pending 并
            interrupt 等待人工审批；恢复重入以 DB 最新记录为准（审批表为单一事实源）：
            已有决定直接复用（不重复建记录、不重复中断），未决定即恢复则继续等待。
            """
            async with approval_session_maker() as session:
                approval = await ApprovalService(session).latest_by_operation(
                    state.task_id, assessment.tool, assessment.parameter_summary
                )
            if approval is None:
                approval = await request_approval(assessment)
            while approval.status == PENDING:
                interrupt(
                    {
                        "type": "approval_required",
                        "approval_id": approval.id,
                        "task_id": state.task_id,
                        "user_id": state.user_id,
                        "tool": assessment.tool,
                        "risk_level": assessment.risk_level.value,
                        "parameter_summary": assessment.parameter_summary,
                    }
                )
                async with approval_session_maker() as session:
                    approval = await ApprovalService(session).get(approval.id)
            return approval.status

        async def call_tool(
            name: str, arguments: dict[str, Any], assessment: RiskAssessment | None
        ) -> ToolResult:
            """单工具执行：权限门控（拒绝不执行）→ 审批门控（HIGH 中断）→ MCP 调用 → 失败原参重试。"""
            nonlocal permissions
            required = (
                registry.get(name).metadata.required_permission if registry.has(name) else None
            )
            if required is not None and auth_session_maker is not None:
                if permissions is None:  # 懒解析：本轮首个需要校验的工具触发，轮内复用
                    async with auth_session_maker() as session:
                        permissions = await get_user_permissions(session, state.user_id)
                if required not in permissions:
                    await write_trace(
                        state,
                        AgentTraceEventType.PERMISSION_DENIED,
                        tool=name,
                        metadata={"required_permission": required, "user_id": state.user_id},
                    )
                    return ToolResult.fail(
                        "PERMISSION_DENIED",
                        f"无权限执行工具 {name}（需要 {required} 权限）",
                    )
            if (
                assessment is not None
                and assessment.requires_approval
                and approval_session_maker is not None
            ):
                # 审批门控：HIGH 工具执行前过审批门——批准则执行；拒绝返回
                # APPROVAL_REJECTED（工具不执行、不计入错误，LLM 生成不含该操作的报告）
                if await approval_gate(assessment) != "approved":
                    return ToolResult.fail(APPROVAL_REJECTED, f"操作 {name} 被审批拒绝，未执行")
            await write_trace(
                state,
                AgentTraceEventType.TOOL_CALLED,
                tool=name,
                round=state.round,
                parameter_summary=assessment.parameter_summary
                if assessment is not None
                else json.dumps(arguments, ensure_ascii=False, sort_keys=True),
            )
            result = await mcp.call(name, arguments)
            # 工具失败原参重试 tool_retry_times 次（默认 0）；重试耗尽仍失败则保持失败
            for _ in range(tool_retry_times):
                if result.success:
                    break
                result = await mcp.call(name, arguments)
            return result

        for call in state.messages[-1]["tool_calls"]:
            name = call["function"]["name"]
            assessment: RiskAssessment | None = None
            duration_ms: int | None = None
            try:
                arguments = json.loads(call["function"]["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("参数必须是 JSON 对象")
            except (json.JSONDecodeError, ValueError) as exc:
                # LLM 产出的参数 JSON 不合法：不调用工具，按统一错误结构记录
                result = ToolResult.fail("TOOL_CALL_FAILED", f"工具 {name} 参数解析失败: {exc}")
                arguments = {}
            else:
                assessment = _risk_assessment(registry, name, arguments)  # 调用前风险识别
                if assessment is not None:
                    assessments.append(assessment)
                started = time.perf_counter()  # 含门控与重试的调用耗时（轨迹观测口径）
                result = await call_tool(name, arguments, assessment)
                duration_ms = elapsed_ms(started)

            records.append(ToolCallRecord(tool=name, arguments=arguments, result=result))
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result.model_dump_json(),
                }
            )
            if not result.success and result.error_code != APPROVAL_REJECTED:
                # APPROVAL_REJECTED 是业务决定（走 fallback 报告），不算执行错误
                errors.append(f"工具 {name} 调用失败 [{result.error_code}]: {result.message}")
            # 轨迹与 state.tool_results 一一对应：每次调用（含被拒 / 参数非法）各一条 tool_result
            await write_trace(
                state,
                AgentTraceEventType.TOOL_RESULT,
                tool=name,
                round=state.round,
                duration_ms=duration_ms,
                status="success" if result.success else "failed",
                error_code=result.error_code,
                parameter_summary=assessment.parameter_summary if assessment is not None else None,
                result_summary=_summary_text(
                    result.message
                    if not result.success
                    else _result_digest(result.data)
                ),
            )

        update: dict[str, Any] = {
            "messages": tool_messages,
            "tool_results": records,
            "risk_assessments": assessments,
        }
        if errors:
            update["status"] = AgentStatus.FAILED
            update["errors"] = errors
        return update

    builder = StateGraph(AgentState)
    builder.add_node("llm", call_llm)
    builder.add_node("tools", run_tools)
    builder.add_edge(START, "llm")
    builder.add_conditional_edges("llm", _route_after_llm, {"tools": "tools", END: END})
    builder.add_conditional_edges("tools", _route_after_tools, {"llm": "llm", END: END})
    return builder.compile(checkpointer=checkpointer)

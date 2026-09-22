"""Agent 图对外运行入口：run 执行新任务 / resume 恢复被中断的任务。

- run：以初始状态执行图，checkpointer 按 thread_id = task_id 逐步持久化（每步可恢复）；
  图触发 interrupt（HIGH 工具审批门控）时，返回状态置 WAITING_APPROVAL；
- resume：同 thread_id 以 Command(resume=True) 从最后 checkpoint 续跑（中断的节点重新
  执行）；审批决定先经 ApprovalService.approve / reject 落库，图内审批门按 DB 最新
  记录放行（approved → 工具执行）或拒绝（rejected → 工具不执行，走 fallback 报告）；
- state：读取 thread 当前持久化状态；存在未恢复的 interrupt 时状态显示 WAITING_APPROVAL
  （checkpoint 本身不含该状态，由 snapshot.tasks 的 interrupts 推导）；
- 任务起止埋点（19.2，trace_session_maker 显式接线后生效）：run 入口写 task_started /
  agent_selected；执行收束（终态）写 agent_finished 与 task_completed / task_failed
  （waiting_approval 属等待恢复，不写终态）；节点级事件（LLM / 工具）见 build_agent_graph；
- 薄封装：不持有业务状态，依赖（LLM / MCP / 注册表 / checkpointer）由构造方注入，
  返回值统一校验回 AgentState。
"""
from __future__ import annotations

import time
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.checkpoint import agent_thread_config
from app.agents.graph import BASE_AGENT_NAME, build_agent_graph, elapsed_ms
from app.agents.state import AgentState, AgentStatus
from app.core.llm import LLMService
from app.core.redis import RedisService
from app.mcp.client import MCPClient
from app.mcp.registry import ToolRegistry
from app.models.agent import AgentTraceEventType
from app.services.trace_service import TraceService


class AgentRunner:
    """Agent 图运行入口（run / resume / state 三个对外方法）。"""

    def __init__(
        self,
        llm: LLMService,
        mcp: MCPClient,
        registry: ToolRegistry,
        checkpointer: BaseCheckpointSaver,
        *,
        max_rounds: int | None = None,
        tool_retry_times: int = 0,
        auth_session_maker: async_sessionmaker[AsyncSession] | None = None,
        approval_session_maker: async_sessionmaker[AsyncSession] | None = None,
        trace_session_maker: async_sessionmaker[AsyncSession] | None = None,
        trace_redis: RedisService | None = None,
        trace_task_events: bool = True,
    ) -> None:
        self._trace_session_maker = trace_session_maker
        self._trace_redis = trace_redis
        # 任务级生命周期埋点开关：专业 Agent 受 Supervisor 调度时由 TaskExecutor 统一
        # 埋点（21.2），关闭以避免同任务 task_started / task_completed 事件重复发布
        self._trace_task_events = trace_task_events
        self._graph = build_agent_graph(
            llm,
            mcp,
            registry,
            max_rounds=max_rounds,
            checkpointer=checkpointer,
            tool_retry_times=tool_retry_times,
            auth_session_maker=auth_session_maker,
            approval_session_maker=approval_session_maker,
            trace_session_maker=trace_session_maker,
            trace_redis=trace_redis,
        )

    async def run(
        self,
        task_id: int,
        user_id: int,
        messages: list[dict[str, Any]],
        task_context: dict[str, Any] | None = None,
    ) -> AgentState:
        """执行新任务：初始状态 + thread_id = task_id，返回最终状态。"""
        state = AgentState(
            task_id=task_id,
            user_id=user_id,
            messages=messages,
            task_context=task_context or {},
        )
        await self._write_trace(task_id, AgentTraceEventType.TASK_STARTED)
        await self._write_trace(
            task_id, AgentTraceEventType.AGENT_SELECTED, agent=BASE_AGENT_NAME, round=1
        )
        return await self._invoke(state, task_id)

    async def resume(self, task_id: int) -> AgentState:
        """恢复被中断的任务：同 thread_id 以 Command(resume=True) 从最后 checkpoint 续跑。

        审批决定先经 ApprovalService.approve / reject 落库再调用本方法——图内审批门按
        DB 最新记录放行（approved → 工具继续执行）或拒绝（rejected → 工具不执行，走
        fallback 报告）；未决定即恢复则任务继续等待（仍返回 waiting_approval）。
        """
        return await self._invoke(Command(resume=True), task_id)

    async def state(self, task_id: int) -> AgentState:
        """读取 thread 当前持久化状态（中断后可观测进度与待审批上下文）。"""
        snapshot = await self._graph.aget_state(agent_thread_config(task_id))
        state = AgentState.model_validate(snapshot.values)
        if any(task.interrupts for task in snapshot.tasks):
            state = state.model_copy(update={"status": AgentStatus.WAITING_APPROVAL})
        return state

    async def _invoke(self, inp: AgentState | Command | None, task_id: int) -> AgentState:
        """统一执行入口：LangGraph 返回值统一为 dict，校验回 AgentState。

        图触发 interrupt（审批门控）时结果携带 __interrupt__：任务状态置 WAITING_APPROVAL
        返回，中断点已由 checkpointer 持久化，恢复经 resume。
        """
        started = time.perf_counter()
        result = await self._graph.ainvoke(inp, config=agent_thread_config(task_id))
        if result.get("__interrupt__"):
            result["status"] = AgentStatus.WAITING_APPROVAL
        state = AgentState.model_validate(result)
        await self._trace_terminal(state, elapsed_ms(started))
        return state

    async def _trace_terminal(self, state: AgentState, duration_ms: int) -> None:
        """终态埋点：completed / failed 收束 Agent 起止与任务终态；等待恢复不写终态。"""
        if state.status is AgentStatus.COMPLETED:
            await self._write_trace(
                state.task_id,
                AgentTraceEventType.AGENT_FINISHED,
                round=state.round,
                duration_ms=duration_ms,
                status="completed",
                result_summary=state.final_result.content[:200] if state.final_result else None,
            )
            await self._write_trace(state.task_id, AgentTraceEventType.TASK_COMPLETED)
        elif state.status is AgentStatus.FAILED:
            await self._write_trace(
                state.task_id,
                AgentTraceEventType.AGENT_FINISHED,
                round=state.round,
                duration_ms=duration_ms,
                status="failed",
                metadata={"errors": state.errors},
            )
            await self._write_trace(
                state.task_id, AgentTraceEventType.TASK_FAILED, metadata={"errors": state.errors}
            )

    async def _write_trace(
        self, task_id: int, event_type: AgentTraceEventType, **fields: Any
    ) -> None:
        """追加一条任务级轨迹（未接线 trace_session_maker 或关闭任务级埋点时不写）。"""
        if self._trace_session_maker is None or not self._trace_task_events:
            return
        async with self._trace_session_maker() as session:
            await TraceService(session, redis=self._trace_redis).append(
                task_id=task_id, event_type=event_type, **fields
            )

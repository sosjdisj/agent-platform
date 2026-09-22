"""Prompt 17.2：Tool 执行层权限校验——同一工具在有无权限两种用户下行为正确，拒绝事件落 Trace 表。

复用 test_agent_graph 的 LLM 桩与图构建辅助（单一来源，不重复造轮子）。
"""
import json

from pydantic import BaseModel
from sqlalchemy import func, insert, select

from app.agents.graph import build_agent_graph
from app.agents.state import AgentState, AgentStatus
from app.mcp.base import BaseTool
from app.models.agent import AgentTraceEvent, AgentTraceEventType
from app.models.rbac import Role, user_roles
from app.models.user import User
from app.services.rbac_service import seed_rbac
from tests.test_agent_graph import (
    ScriptedLLM,
    final_response,
    make_mcp,
    tool_call_response,
)


class GuardedInput(BaseModel):
    text: str


class GuardedOutput(BaseModel):
    echo: str


class GuardedTool(BaseTool[GuardedInput, GuardedOutput]):
    """声明 required_permission 的桩工具：执行即回显（是否真执行可由输出判断）。"""

    name = "guarded"
    description = "需要 customer:write 权限的回显工具"
    required_permission = "customer:write"
    InputModel = GuardedInput
    OutputModel = GuardedOutput

    async def run(self, params: GuardedInput) -> GuardedOutput:
        return GuardedOutput(echo=params.text)


async def _seed_users(db_session) -> tuple[int, int]:
    """seed RBAC 并创建 sales / employee 两个用户，返回 (sales_id, employee_id)。

    sales 拥有 customer:write；employee 没有（只有只读 + 知识检索权限）。
    """
    await seed_rbac(db_session)
    sales = User(username="u_sales", email="sales@test.com", hashed_password="x")
    employee = User(username="u_emp", email="emp@test.com", hashed_password="x")
    db_session.add_all([sales, employee])
    await db_session.flush()
    role_ids = dict((await db_session.execute(select(Role.name, Role.id))).all())
    await db_session.execute(
        insert(user_roles),
        [
            {"user_id": sales.id, "role_id": role_ids["sales"]},
            {"user_id": employee.id, "role_id": role_ids["employee"]},
        ],
    )
    await db_session.commit()
    return sales.id, employee.id


def _build_graph(db_session_maker, user_id: int, task_id: int):
    """构造带权限门控的被测图：脚本 LLM 先要求调用 guarded 工具，再给最终答案。"""
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "guarded", json.dumps({"text": "你好"})),
            final_response("汇总完毕"),
        ]
    )
    mcp, registry = make_mcp([GuardedTool()])
    graph = build_agent_graph(
        llm,
        mcp,
        registry,
        auth_session_maker=db_session_maker,
        trace_session_maker=db_session_maker,
    )
    state = AgentState(
        task_id=task_id,
        user_id=user_id,
        messages=[{"role": "user", "content": "帮我调用工具"}],
    )
    return graph, state


async def test_allowed_user_executes_tool(db_session, db_session_maker):
    """有权限用户（sales）：工具真实执行，无拒绝事件。"""
    sales_id, _ = await _seed_users(db_session)
    graph, state = _build_graph(db_session_maker, sales_id, task_id=101)

    result = AgentState.model_validate(await graph.ainvoke(state))

    assert result.status is AgentStatus.COMPLETED
    assert len(result.tool_results) == 1
    assert result.tool_results[0].result.success is True
    assert result.tool_results[0].result.data == {"echo": "你好"}
    denied = await db_session.scalar(
        select(func.count())
        .select_from(AgentTraceEvent)
        .where(AgentTraceEvent.event_type == AgentTraceEventType.PERMISSION_DENIED)
    )
    assert denied == 0


async def test_denied_user_blocked_and_trace_event_written(db_session, db_session_maker):
    """无权限用户（employee）：工具不执行，返回 PERMISSION_DENIED，拒绝事件落 agent_trace_events。"""
    _, employee_id = await _seed_users(db_session)
    graph, state = _build_graph(db_session_maker, employee_id, task_id=202)

    result = AgentState.model_validate(await graph.ainvoke(state))

    assert result.status is AgentStatus.FAILED
    record = result.tool_results[0]
    assert record.result.success is False
    assert record.result.error_code == "PERMISSION_DENIED"

    event = await db_session.scalar(
        select(AgentTraceEvent).where(
            AgentTraceEvent.task_id == 202,
            AgentTraceEvent.event_type == AgentTraceEventType.PERMISSION_DENIED,
        )
    )
    assert event is not None
    assert event.agent == "assistant"
    assert event.payload == {
        "tool": "guarded",
        "metadata": {"required_permission": "customer:write", "user_id": employee_id},
    }


async def test_no_auth_session_maker_skips_permission_check(db_session):
    """未注入 auth_session_maker（缺省）→ 不校验权限，工具直接执行（测试 / 调试路径复用）。"""
    await seed_rbac(db_session)
    llm = ScriptedLLM(
        [tool_call_response("call_1", "guarded", json.dumps({"text": "hi"})), final_response("x")]
    )
    mcp, registry = make_mcp([GuardedTool()])
    graph = build_agent_graph(llm, mcp, registry, auth_session_maker=None)
    state = AgentState(task_id=303, user_id=1, messages=[{"role": "user", "content": "q"}])

    result = AgentState.model_validate(await graph.ainvoke(state))

    assert result.tool_results[0].result.success is True
    assert result.tool_results[0].result.data == {"echo": "hi"}
    denied = await db_session.scalar(
        select(func.count())
        .select_from(AgentTraceEvent)
        .where(AgentTraceEvent.event_type == AgentTraceEventType.PERMISSION_DENIED)
    )
    assert denied == 0

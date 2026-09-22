"""Prompt 20.2 端到端验收：执行 Agent 任务，SSE 客户端按序收到全部 Trace 事件。

三个任务场景合计覆盖除 task_cancelled 外全部事件类型的发布接线（发布在
TraceService.append 与 ApprovalService 单点完成，与事件类型无关；task_cancelled
见 test_task_cancel）：
- 审批全流程（与 18.4 结构对齐）：refund_order 中断 → approve → resume，事件依次为
  task_started → agent_selected → llm_call → approval_required → approval_result
  → tool_called → tool_result → llm_call → agent_finished → task_completed，
  审批两帧的 data 即 ApprovalRequiredEvent / ApprovalResultEvent dump；
- 轮次截断失败：… → agent_finished(failed) → task_failed（TraceEvent 结构）；
- 权限拒绝：… → permission_denied → tool_result(failed) → agent_finished(failed) → task_failed。
不做：前端；SSE 管线本体（20.1）与轨迹落库（19.2）已有各自测试。
"""
import json

from app.agents.runner import AgentRunner
from app.agents.state import AgentStatus
from app.core.config import get_settings
from app.core.redis import RedisService
from app.mcp.base import RiskLevel
from app.mcp.business.refund_order import RefundOrderTool
from app.schemas.events import ApprovalRequiredEvent, ApprovalResultEvent, TraceEvent
from app.services.approval_service import ApprovalService
from tests.test_agent_graph import (
    EchoTool,
    ScriptedLLM,
    final_response,
    make_mcp,
    tool_call_response,
)
from tests.test_approval_interrupt import _approval
from tests.test_auth import login, register
from tests.test_mcp_business_tools import _first_order_id
from tests.test_task_events import (
    EVENTS_URL,
    SseStream,
    _make_task,
    _parse_event,
    _read_event,
    _wait_subscribed,
)
from tests.test_tool_permission import GuardedTool

settings = get_settings()


async def _frames(stream: SseStream, count: int) -> list[tuple[str, str]]:
    """按序读出 count 个事件帧并解析为 (event, data) 列表（顺序即发布顺序）。"""
    return [_parse_event(await _read_event(stream)) for _ in range(count)]


def _runner(
    llm: ScriptedLLM,
    tools: list,
    checkpointer,
    db_session_maker,
    fake_redis,
    *,
    approval: bool = False,
    auth: bool = False,
    max_rounds: int | None = None,
) -> AgentRunner:
    """构造接线了轨迹落库与事件发布的运行器（按需叠加审批门 / 权限门）。"""
    return AgentRunner(
        llm,
        *make_mcp(tools),
        checkpointer,
        max_rounds=max_rounds,
        auth_session_maker=db_session_maker if auth else None,
        approval_session_maker=db_session_maker if approval else None,
        trace_session_maker=db_session_maker,
        trace_redis=RedisService(fake_redis),
    )


async def test_approval_task_stream_delivers_all_events_in_order(
    api_client, db_session, db_session_maker, seeded_maker, checkpointer, case_ids, fake_redis
):
    """审批全流程任务：SSE 按序收到 10 个事件帧，审批两帧与 18.4 事件结构逐字段对齐。"""
    user_id = (await register(api_client)).json()["id"]
    await login(api_client)
    token = api_client.cookies.get(settings.sse_cookie_name)
    task_id = await _make_task(db_session_maker, user_id)

    (a_id, *_rest) = case_ids
    order_id = await _first_order_id(seeded_maker, a_id)
    llm = ScriptedLLM(
        [
            tool_call_response(
                "call_1",
                "refund_order",
                json.dumps({"order_id": order_id, "reason": "质量问题协商退款"}),
            ),
            final_response("退款已完成"),
        ]
    )
    redis = RedisService(fake_redis)
    runner = _runner(
        llm,
        [RefundOrderTool(session_maker=seeded_maker)],
        checkpointer,
        db_session_maker,
        fake_redis,
        approval=True,
    )

    stream = SseStream()
    try:
        await stream.open(EVENTS_URL.format(task_id=task_id), token)
        assert stream.status == 200
        await _wait_subscribed(stream)

        interrupted = await runner.run(task_id, user_id, [{"role": "user", "content": "退款"}])
        assert interrupted.status is AgentStatus.WAITING_APPROVAL

        approval = await _approval(db_session, task_id)
        await ApprovalService(db_session, redis=redis).approve(
            approval.id, decided_by=user_id, comment="同意退款"
        )

        state = await runner.resume(task_id)
        assert state.status is AgentStatus.COMPLETED

        frames = await _frames(stream, 10)
        assert [name for name, _ in frames] == [
            "task_started",
            "agent_selected",
            "llm_call",
            "approval_required",
            "approval_result",
            "tool_called",
            "tool_result",
            "llm_call",
            "agent_finished",
            "task_completed",
        ]

        # 审批事件与 18.4 结构逐字段对齐：data 即事件 dump（前端按此消费）
        required = ApprovalRequiredEvent.model_validate_json(frames[3][1])
        assert required.approval_id == approval.id
        assert required.tool == "refund_order"
        assert required.requester_id == user_id
        assert required.risk_level is RiskLevel.HIGH
        result = ApprovalResultEvent.model_validate_json(frames[4][1])
        assert result.decision == "approved"
        assert result.decided_by == user_id
        assert result.comment == "同意退款"
    finally:
        await stream.disconnect()


async def test_failed_task_stream_publishes_failure_events(
    api_client, db_session_maker, checkpointer, fake_redis
):
    """轮次截断失败任务：终态帧 task_failed 以 TraceEvent 结构推送（metadata 携带 errors）。"""
    user_id = (await register(api_client)).json()["id"]
    await login(api_client)
    token = api_client.cookies.get(settings.sse_cookie_name)
    task_id = await _make_task(db_session_maker, user_id)
    runner = _runner(
        ScriptedLLM([tool_call_response("call_1", "echo", json.dumps({"text": "x"}))]),
        [EchoTool()],
        checkpointer,
        db_session_maker,
        fake_redis,
        max_rounds=1,
    )

    stream = SseStream()
    try:
        await stream.open(EVENTS_URL.format(task_id=task_id), token)
        await _wait_subscribed(stream)

        state = await runner.run(task_id, user_id, [{"role": "user", "content": "分析"}])
        assert state.status is AgentStatus.FAILED

        frames = await _frames(stream, 5)
        assert [name for name, _ in frames] == [
            "task_started",
            "agent_selected",
            "llm_call",
            "agent_finished",
            "task_failed",
        ]
        failed = TraceEvent.model_validate_json(frames[4][1])
        assert failed.task_id == task_id
        assert failed.metadata == {"errors": state.errors}
    finally:
        await stream.disconnect()


async def test_permission_denied_task_stream_publishes_denial_events(
    api_client, db_session_maker, checkpointer, fake_redis
):
    """无权限用户任务：permission_denied 事件按 TraceEvent 结构推送且带门控元数据。"""
    user_id = (await register(api_client)).json()["id"]  # 新用户无角色 → 权限为空集
    await login(api_client)
    token = api_client.cookies.get(settings.sse_cookie_name)
    task_id = await _make_task(db_session_maker, user_id)
    runner = _runner(
        ScriptedLLM([tool_call_response("call_1", "guarded", json.dumps({"text": "你好"}))]),
        [GuardedTool()],
        checkpointer,
        db_session_maker,
        fake_redis,
        auth=True,
    )

    stream = SseStream()
    try:
        await stream.open(EVENTS_URL.format(task_id=task_id), token)
        await _wait_subscribed(stream)

        state = await runner.run(task_id, user_id, [{"role": "user", "content": "帮我调用工具"}])
        assert state.status is AgentStatus.FAILED

        frames = await _frames(stream, 7)
        assert [name for name, _ in frames] == [
            "task_started",
            "agent_selected",
            "llm_call",
            "permission_denied",
            "tool_result",
            "agent_finished",
            "task_failed",
        ]
        denied = TraceEvent.model_validate_json(frames[3][1])
        assert denied.tool == "guarded"
        assert denied.metadata == {"required_permission": "customer:write", "user_id": user_id}
    finally:
        await stream.disconnect()

"""Prompt 19.2：节点埋点接入——完整流程 Trace 序列完整且时间有序。

- 完整流程（工具循环 + 最终答复）：task_started → agent_selected → llm_call →
  tool_called → tool_result → llm_call → agent_finished → task_completed，时间单调有序；
- 失败链路：tool_result(failed) → agent_finished(failed) → task_failed（errors 随事件透出）；
- 审批中断：等待恢复不写终态事件，resume 后终态事件按序补齐；
- 未接线 trace_session_maker 不写轨迹（存量行为不变）。
不做：SSE 推送。
"""
import json

from app.agents.runner import AgentRunner
from app.agents.state import AgentStatus
from app.mcp.business.refund_order import RefundOrderTool
from app.models.agent import AgentTraceEventType as ET
from app.services.approval_service import ApprovalService
from app.services.trace_service import TraceService
from tests.test_agent_graph import (
    BoomTool,
    EchoTool,
    ScriptedLLM,
    final_response,
    make_mcp,
    tool_call_response,
)
from tests.test_approval_interrupt import _approval
from tests.test_mcp_business_tools import _first_order_id

USER_ID = 100


async def _events(maker, task_id: int):
    """按任务回放轨迹（短会话，timestamp 升序 + id 兜底）。"""
    async with maker() as session:
        return await TraceService(session).list_by_task(task_id)


async def test_full_flow_trace_complete_and_ordered(db_session_maker, checkpointer):
    """完整流程埋点：事件类型序列完整、关键 payload 就位、时间戳单调不减。"""
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "echo", json.dumps({"text": "你好"})),
            final_response("已完成"),
        ]
    )
    mcp, registry = make_mcp([EchoTool()])
    runner = AgentRunner(llm, mcp, registry, checkpointer, trace_session_maker=db_session_maker)

    state = await runner.run(801, USER_ID, [{"role": "user", "content": "调用工具"}])

    assert state.status is AgentStatus.COMPLETED
    events = await _events(db_session_maker, 801)
    assert [e.event_type for e in events] == [
        ET.TASK_STARTED,
        ET.AGENT_SELECTED,
        ET.LLM_CALL,
        ET.TOOL_CALLED,
        ET.TOOL_RESULT,
        ET.LLM_CALL,
        ET.AGENT_FINISHED,
        ET.TASK_COMPLETED,
    ]

    timestamps = [e.timestamp for e in events]
    assert timestamps == sorted(timestamps)  # 时间有序（同刻由 id 兜底，见 list_by_task）

    selected, llm1, called, tool_result, llm2, finished = events[1:7]
    assert selected.agent == "assistant"
    assert selected.payload["round"] == 1
    assert llm1.payload["round"] == 1
    assert llm2.payload["round"] == 2
    for llm_event in (llm1, llm2):
        assert llm_event.payload["status"] == "success"
        assert llm_event.payload["duration_ms"] >= 0
    assert called.payload["tool"] == "echo"
    assert called.payload["parameter_summary"] == json.dumps(
        {"text": "你好"}, ensure_ascii=False, sort_keys=True
    )
    assert tool_result.payload["tool"] == "echo"
    assert tool_result.payload["status"] == "success"
    assert "error_code" not in tool_result.payload  # 空字段不进 payload（19.1 契约）
    assert tool_result.payload["duration_ms"] >= 0
    assert "echo" in tool_result.payload["result_summary"]  # 结构化摘要：echo=你好
    assert "{" not in tool_result.payload["result_summary"]  # 不携带原始 JSON dump
    assert finished.payload["status"] == "completed"
    assert finished.payload["result_summary"] == "已完成"
    assert finished.payload["duration_ms"] >= 0


async def test_failed_flow_traces_task_failed(db_session_maker, checkpointer):
    """失败链路埋点：工具失败 → agent_finished(failed) → task_failed（errors 透出）。"""
    llm = ScriptedLLM([tool_call_response("call_1", "boom", "{}")])
    mcp, registry = make_mcp([BoomTool()])
    runner = AgentRunner(llm, mcp, registry, checkpointer, trace_session_maker=db_session_maker)

    state = await runner.run(802, USER_ID, [{"role": "user", "content": "引爆"}])

    assert state.status is AgentStatus.FAILED
    events = await _events(db_session_maker, 802)
    assert [e.event_type for e in events] == [
        ET.TASK_STARTED,
        ET.AGENT_SELECTED,
        ET.LLM_CALL,
        ET.TOOL_CALLED,
        ET.TOOL_RESULT,
        ET.AGENT_FINISHED,
        ET.TASK_FAILED,
    ]
    tool_result, finished, failed = events[4:7]
    assert tool_result.payload["status"] == "failed"
    assert tool_result.payload["error_code"] is not None
    assert finished.payload["status"] == "failed"
    assert finished.payload["metadata"]["errors"]
    assert failed.payload["metadata"]["errors"] == state.errors


async def test_approval_interrupt_waits_then_completes_after_resume(
    db_session, seeded_maker, checkpointer, case_ids
):
    """审批中断埋点：等待恢复不写终态；批准 → resume 后终态事件按序补齐。"""
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
    mcp, registry = make_mcp([RefundOrderTool(session_maker=seeded_maker)])
    runner = AgentRunner(
        llm,
        mcp,
        registry,
        checkpointer,
        approval_session_maker=seeded_maker,
        trace_session_maker=seeded_maker,
    )

    interrupted = await runner.run(9301, USER_ID, [{"role": "user", "content": "退款"}])
    assert interrupted.status is AgentStatus.WAITING_APPROVAL

    waiting_events = await _events(seeded_maker, 9301)
    waiting_types = [e.event_type for e in waiting_events]
    assert waiting_types[-1] is ET.APPROVAL_REQUIRED  # 停在审批等待，无终态事件
    assert ET.AGENT_FINISHED not in waiting_types
    assert ET.TASK_COMPLETED not in waiting_types

    approval = await _approval(db_session, 9301)
    await ApprovalService(db_session).approve(approval.id, decided_by=1)

    state = await runner.resume(9301)
    assert state.status is AgentStatus.COMPLETED

    resumed_types = [e.event_type for e in await _events(seeded_maker, 9301)]
    assert resumed_types[-6:] == [
        ET.APPROVAL_RESULT,
        ET.TOOL_CALLED,
        ET.TOOL_RESULT,
        ET.LLM_CALL,
        ET.AGENT_FINISHED,
        ET.TASK_COMPLETED,
    ]


async def test_no_trace_without_wiring(db_session_maker, checkpointer):
    """未接线 trace_session_maker（缺省）：不写任何轨迹（测试 / 调试路径行为不变）。"""
    llm = ScriptedLLM([final_response("直接回答")])
    mcp, registry = make_mcp([EchoTool()])
    runner = AgentRunner(llm, mcp, registry, checkpointer)

    await runner.run(803, USER_ID, [{"role": "user", "content": "在吗"}])

    assert await _events(db_session_maker, 803) == []

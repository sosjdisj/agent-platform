"""Agent Schema 单测：AgentState / AgentResult / Source / ToolCallRecord 的默认值、校验与序列化。"""

import json

import pytest
from pydantic import ValidationError

from app.agents.state import AgentResult, AgentState, AgentStatus, Source, ToolCallRecord
from app.mcp.base import ToolResult


def make_state(**overrides) -> AgentState:
    """最小必填字段构造状态，overrides 覆盖其余字段。"""
    payload: dict = {
        "task_id": 1,
        "user_id": 100,
        "messages": [{"role": "user", "content": "你好"}],
    }
    payload.update(overrides)
    return AgentState.model_validate(payload)


def test_state_defaults() -> None:
    """仅提供必填字段时，其余字段取约定默认值。"""
    state = make_state()

    assert state.current_agent is None
    assert state.tool_results == []
    assert state.task_context == {}
    assert state.agent_results == []
    assert state.status is AgentStatus.RUNNING
    assert state.round == 1
    assert state.errors == []
    assert state.final_result is None


def test_state_validation() -> None:
    """必填缺失 / 轮次越界 / 非法状态值 / 消息非 dict 均拒绝。"""
    with pytest.raises(ValidationError):
        AgentState.model_validate({"user_id": 1, "messages": []})  # 缺 task_id
    with pytest.raises(ValidationError):
        make_state(user_id=None)  # 缺 user_id
    with pytest.raises(ValidationError):
        make_state(round=0)  # 轮次从 1 起
    with pytest.raises(ValidationError):
        make_state(status="done")  # 非法状态值
    with pytest.raises(ValidationError):
        make_state(messages=["你好"])  # 消息必须为 dict（OpenAI 格式）


def test_state_serialization_roundtrip() -> None:
    """完整状态可 JSON 序列化（枚举转值、嵌套模型展开）并可无损还原。"""
    state = make_state(
        current_agent="supervisor",
        tool_results=[
            ToolCallRecord(
                tool="query_orders",
                arguments={"customer_id": 1},
                result=ToolResult.ok({"orders": []}),
            )
        ],
        task_context={"title": "查订单", "query": "1 号客户有哪些订单"},
        agent_results=[
            AgentResult(
                agent="crm",
                content="客户 1 名下有 3 笔订单",
                sources=[
                    Source(source_type="knowledge", ref_id="doc-1::c0", title="退款政策", score=0.87),
                    Source(source_type="tool", ref_id="query_orders"),
                ],
            )
        ],
        status=AgentStatus.COMPLETED,
        round=2,
        errors=["首轮知识检索未命中"],
        final_result=AgentResult(agent="supervisor", content="客户 1 名下共有 3 笔订单"),
    )

    data = state.model_dump(mode="json")
    json.dumps(data, ensure_ascii=False)  # 不抛异常：可直接写 JSONB / SSE

    assert data["status"] == "completed"  # 枚举序列化为值
    assert data["tool_results"][0]["result"]["success"] is True  # MCP ToolResult 展开
    assert data["agent_results"][0]["sources"][0]["source_type"] == "knowledge"

    assert AgentState.model_validate(json.loads(json.dumps(data, ensure_ascii=False))) == state


def test_tool_call_record_with_failed_result() -> None:
    """失败工具调用：透传 MCP 统一错误结构（error_code / message）。"""
    record = ToolCallRecord(
        tool="refund_order",
        arguments={"order_id": 9},
        result=ToolResult.fail("ORDER_NOT_FOUND", "订单不存在"),
    )

    assert record.result.success is False
    dumped = record.model_dump(mode="json")["result"]
    assert dumped["error_code"] == "ORDER_NOT_FOUND"
    assert dumped["message"] == "订单不存在"

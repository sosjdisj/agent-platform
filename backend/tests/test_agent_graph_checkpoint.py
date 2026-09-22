"""Checkpointer 集成测试（真实 Postgres 测试库）：中断后恢复状态完整 / thread_id 互不干扰。"""

import json

import pytest

from app.agents.checkpoint import agent_thread_config
from app.agents.state import AgentState, AgentStatus
from tests.test_agent_graph import (
    EchoTool,
    ScriptedLLM,
    build_graph,
    final_response,
    initial_state,
    tool_call_response,
)


class Crash(BaseException):
    """模拟进程崩溃：BaseException 不被图内 except Exception 捕获，直接终止运行。"""


class CrashOnSecondCallLLM(ScriptedLLM):
    """第 2 次 chat 调用时崩溃（此时第 1 轮 LLM 决策与工具执行已落 checkpoint）。"""

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        if len(self.calls) == 2:
            raise Crash("进程被杀")
        return self._responses.pop(0)


async def test_resume_after_crash_keeps_state_intact(checkpointer) -> None:
    """中断恢复：第 2 轮 LLM 调用时崩溃，同一 thread_id 以 None 输入恢复，
    messages / round / tool_results 等状态完整，任务继续至完成。"""
    config = agent_thread_config(1)
    graph = build_graph(
        CrashOnSecondCallLLM([tool_call_response("call_1", "echo", json.dumps({"text": "中断前"}))]),
        [EchoTool()],
        checkpointer=checkpointer,
    )

    with pytest.raises(Crash):
        await graph.ainvoke(initial_state(), config=config)

    # 崩溃点前的进度已持久化：round 推进到 2，工具消息已入链
    saved = await graph.aget_state(config)
    assert saved.values["round"] == 2
    assert [m["role"] for m in saved.values["messages"]] == ["user", "assistant", "tool"]

    # 模拟进程重启：全新图实例 + 新 LLM 脚本，同 thread_id 恢复执行
    llm = ScriptedLLM([final_response("恢复后完成")])
    resumed = build_graph(llm, [EchoTool()], checkpointer=checkpointer)

    result = AgentState.model_validate(await resumed.ainvoke(None, config=config))

    assert result.status is AgentStatus.COMPLETED
    assert result.final_result is not None
    assert result.final_result.content == "恢复后完成"
    assert result.round == 2  # 从崩溃前的轮次继续，不再回退

    # 中断前的消息链与工具结果完整保留，恢复后无缝续写
    assert [m["role"] for m in result.messages] == ["user", "assistant", "tool", "assistant"]
    assert [r.result.data["echo"] for r in result.tool_results] == ["中断前"]
    # 恢复后的 LLM 收到含 tool 消息的完整历史（而非从头开始）
    assert [m["role"] for m in llm.calls[0]["messages"]] == ["user", "assistant", "tool"]


async def test_threads_do_not_interfere(checkpointer) -> None:
    """thread_id 隔离：同一图实例上不同 task_id 各自持久化，互不读写对方状态。"""
    llm = ScriptedLLM(
        [
            tool_call_response("call_a", "echo", json.dumps({"text": "A的工具"})),
            final_response("A 完成"),
            final_response("B 直接完成"),
        ]
    )
    graph = build_graph(llm, [EchoTool()], checkpointer=checkpointer)
    config_a = agent_thread_config(101)
    config_b = agent_thread_config(202)

    result_a = AgentState.model_validate(await graph.ainvoke(initial_state(), config=config_a))
    assert result_a.status is AgentStatus.COMPLETED
    assert result_a.round == 2

    state_b = AgentState(
        task_id=202, user_id=100, messages=[{"role": "user", "content": "B 的任务"}]
    )
    result_b = AgentState.model_validate(await graph.ainvoke(state_b, config=config_b))
    assert result_b.status is AgentStatus.COMPLETED
    assert result_b.round == 1

    # 两个 thread 的持久化状态各自独立
    snap_a = await graph.aget_state(config_a)
    snap_b = await graph.aget_state(config_b)
    assert snap_a.values["final_result"].content == "A 完成"
    assert snap_b.values["final_result"].content == "B 直接完成"
    assert [m["role"] for m in snap_a.values["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert [m["role"] for m in snap_b.values["messages"]] == ["user", "assistant"]
    assert [r.result.data["echo"] for r in snap_a.values["tool_results"]] == ["A的工具"]
    assert snap_b.values["tool_results"] == []

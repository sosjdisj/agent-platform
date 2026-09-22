"""AgentRunner 入口测试（真实 Postgres 测试库）：run → 手动中断 → resume 全流程。"""

import asyncio
import json

import pytest

from app.agents.checkpoint import agent_thread_config
from app.agents.runner import AgentRunner
from app.agents.state import AgentState, AgentStatus
from tests.test_agent_graph import (
    EchoTool,
    ScriptedLLM,
    final_response,
    make_mcp,
    tool_call_response,
)


class InterruptibleLLM(ScriptedLLM):
    """第 2 次 chat 调用时挂起并发出事件，等待测试侧手动中断（模拟任务运行中被打断）。"""

    def __init__(self, responses: list) -> None:
        super().__init__(responses)
        self.at_second_call = asyncio.Event()

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        if len(self.calls) == 2:
            self.at_second_call.set()
            await asyncio.Event().wait()  # 挂起直到协程被取消
        return self._responses.pop(0)


async def test_run_interrupt_resume_full_flow(checkpointer) -> None:
    """全流程：run 执行到工具轮完成后被手动中断 → 同 task_id resume 续跑到完成。"""
    llm = InterruptibleLLM([tool_call_response("call_1", "echo", json.dumps({"text": "中断前"}))])
    runner = AgentRunner(llm, *make_mcp([EchoTool()]), checkpointer)

    task = asyncio.create_task(runner.run(1, 100, [{"role": "user", "content": "帮个忙"}]))
    await llm.at_second_call.wait()  # 到达第 2 轮 LLM 调用（工具轮已完成并落 checkpoint）
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 中断前进度已持久化：round 推进到 2，工具消息已入链，状态仍为运行中
    saved = await runner.state(1)
    assert saved.status is AgentStatus.RUNNING
    assert saved.round == 2
    assert [m["role"] for m in saved.messages] == ["user", "assistant", "tool"]

    # 模拟进程重启：全新 LLM 脚本 + 新 runner，同 task_id 恢复
    llm2 = ScriptedLLM([final_response("恢复后完成")])
    runner2 = AgentRunner(llm2, *make_mcp([EchoTool()]), checkpointer)

    result = await runner2.resume(1)

    assert result.status is AgentStatus.COMPLETED
    assert result.final_result is not None
    assert result.final_result.content == "恢复后完成"
    assert result.round == 2  # 从中断前的轮次继续

    # 中断前的消息链与工具结果完整保留，恢复后无缝续写
    assert [m["role"] for m in result.messages] == ["user", "assistant", "tool", "assistant"]
    assert [r.result.data["echo"] for r in result.tool_results] == ["中断前"]
    # 恢复后的 LLM 收到含 tool 消息的完整历史（而非从头开始）
    assert [m["role"] for m in llm2.calls[0]["messages"]] == ["user", "assistant", "tool"]


async def test_resume_after_completed_returns_state_without_new_llm_calls(checkpointer) -> None:
    """已完成任务再 resume：直接返回持久化终态，不触发新的 LLM 调用（幂等恢复）。"""
    llm = ScriptedLLM([final_response("一次完成")])
    runner = AgentRunner(llm, *make_mcp([EchoTool()]), checkpointer)

    result = await runner.run(7, 100, [{"role": "user", "content": "直接答"}])
    assert result.status is AgentStatus.COMPLETED
    assert result.final_result.content == "一次完成"

    again = await runner.resume(7)

    assert again.status is AgentStatus.COMPLETED
    assert again.final_result.content == "一次完成"
    assert len(llm.calls) == 1  # resume 未触发新的 LLM 调用


async def test_run_and_resume_use_task_id_as_thread_id(checkpointer) -> None:
    """run / resume 经 agent_thread_config 走 thread_id = task_id（集中定义，无散落拼装）。"""
    llm = ScriptedLLM([final_response("完成")])
    runner = AgentRunner(llm, *make_mcp([EchoTool()]), checkpointer)

    await runner.run(42, 100, [{"role": "user", "content": "问题"}])

    # state(42) 能读到结果 = run 确实写在该 task_id 对应的 thread 下
    assert (await runner.state(42)).final_result.content == "完成"
    assert agent_thread_config(42) == {"configurable": {"thread_id": "42"}}

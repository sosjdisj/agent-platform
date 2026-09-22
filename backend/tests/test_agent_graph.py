"""基础 LangGraph 图单测：脚本化 LLM + in-memory MCP，覆盖 工具循环 / 轮次截断 / 失败可观测。"""

import json
from typing import Any

from fastmcp import FastMCP
from openai.types.chat import ChatCompletion
from pydantic import BaseModel

from app.agents.graph import build_agent_graph
from app.agents.state import AgentState, AgentStatus
from app.mcp.base import BaseTool, ToolResult
from app.mcp.client import MCPClient
from app.mcp.registry import ToolRegistry


class EchoInput(BaseModel):
    text: str


class EchoOutput(BaseModel):
    echo: str


class EchoTool(BaseTool):
    """回声工具：原样返回输入文本。"""

    name = "echo"
    description = "原样返回输入文本"
    InputModel = EchoInput
    OutputModel = EchoOutput

    async def run(self, params: EchoInput) -> EchoOutput:
        return EchoOutput(echo=params.text)


class BoomInput(BaseModel):
    pass


class BoomOutput(BaseModel):
    done: bool


class BoomTool(BaseTool):
    """爆炸工具：执行必然抛异常（模拟工具内部错误）。"""

    name = "boom"
    description = "执行必然失败的工具"
    InputModel = BoomInput
    OutputModel = BoomOutput

    async def run(self, params: BoomInput) -> BoomOutput:
        raise RuntimeError("工具内部炸了")


class ScriptedLLM:
    """LLMService 替身：按脚本逐轮返回响应，记录每次收到的请求。"""

    def __init__(self, responses: list[ChatCompletion]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: Any, **kwargs: Any) -> ChatCompletion:
        self.calls.append({"messages": list(messages), **kwargs})
        return self._responses.pop(0)

    @property
    def remaining(self) -> int:
        return len(self._responses)


def completion(message: dict[str, Any]) -> ChatCompletion:
    """OpenAI 非流式响应 → SDK 模型实例。"""
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "test-model",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        }
    )


def tool_call_response(call_id: str, name: str, arguments: str) -> ChatCompletion:
    """要求调用单个工具的助手消息。"""
    return completion(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
            ],
        }
    )


def final_response(content: str) -> ChatCompletion:
    """直接给出最终答案的助手消息。"""
    return completion({"role": "assistant", "content": content})


def make_mcp(tools: list[BaseTool]) -> tuple[MCPClient, ToolRegistry]:
    """构造注册表 + in-memory MCP（测试共用；图编译同时需要 client 与 registry）。"""
    registry = ToolRegistry()
    registry.register(*tools)
    server = FastMCP("test-agent-graph")
    registry.mount_to(server)
    return MCPClient(server=server, registry=registry), registry


def build_graph(
    llm: ScriptedLLM,
    tools: list[BaseTool],
    *,
    max_rounds: int | None = None,
    checkpointer=None,
):
    """构造注入脚本 LLM 与 in-memory MCP 的被测图（checkpointer 可选注入）。"""
    mcp, registry = make_mcp(tools)
    return build_agent_graph(
        llm=llm,  # type: ignore[arg-type]  图仅依赖 chat 接口
        mcp=mcp,
        registry=registry,
        max_rounds=max_rounds,
        checkpointer=checkpointer,
    )


def initial_state() -> AgentState:
    return AgentState(task_id=1, user_id=100, messages=[{"role": "user", "content": "帮个忙"}])


async def invoke(graph, state: AgentState) -> AgentState:
    """执行图并把最终 channel dict 校验回 AgentState（LangGraph 返回值统一为 dict）。"""
    return AgentState.model_validate(await graph.ainvoke(state))


async def test_direct_answer_without_tools() -> None:
    """模型首轮即给出答案：直接完成，不经过工具节点。"""
    llm = ScriptedLLM([final_response("这是最终答案")])
    graph = build_graph(llm, [EchoTool()])

    result = await invoke(graph, initial_state())

    assert isinstance(result, AgentState)
    assert result.status is AgentStatus.COMPLETED
    assert result.final_result is not None
    assert result.final_result.content == "这是最终答案"
    assert result.tool_results == []
    assert result.round == 1
    assert len(llm.calls) == 1
    assert [m["role"] for m in result.messages] == ["user", "assistant"]


async def test_single_tool_call() -> None:
    """单工具调用：执行成功 → 结果回填 tool 消息 → LLM 汇总完成。"""
    llm = ScriptedLLM(
        [tool_call_response("call_1", "echo", json.dumps({"text": "你好"})), final_response("汇总完毕")]
    )
    graph = build_graph(llm, [EchoTool()])

    result = await invoke(graph, initial_state())

    assert result.status is AgentStatus.COMPLETED
    assert result.final_result.content == "汇总完毕"
    assert result.round == 2  # 第二轮 LLM 完成汇总
    assert len(llm.calls) == 2

    # 工具记录：调用参数与成功结果
    assert len(result.tool_results) == 1
    record = result.tool_results[0]
    assert (record.tool, record.arguments) == ("echo", {"text": "你好"})
    assert record.result.success is True
    assert record.result.data == {"echo": "你好"}

    # 消息链：user → assistant(tool_calls) → tool → assistant(最终)
    assert [m["role"] for m in result.messages] == ["user", "assistant", "tool", "assistant"]
    assert result.messages[-2]["tool_call_id"] == "call_1"
    assert ToolResult.model_validate_json(result.messages[-2]["content"]).data == {"echo": "你好"}


async def test_multi_round_tool_calls() -> None:
    """多轮工具调用：两轮 echo 后汇总，记录与轮次正确累加。"""
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "echo", json.dumps({"text": "第一轮"})),
            tool_call_response("call_2", "echo", json.dumps({"text": "第二轮"})),
            final_response("两轮都完成了"),
        ]
    )
    graph = build_graph(llm, [EchoTool()])

    result = await invoke(graph, initial_state())

    assert result.status is AgentStatus.COMPLETED
    assert result.final_result.content == "两轮都完成了"
    assert result.round == 3
    assert [r.tool for r in result.tool_results] == ["echo", "echo"]
    assert [r.result.data["echo"] for r in result.tool_results] == ["第一轮", "第二轮"]


async def test_round_limit_truncation() -> None:
    """轮次上限：模型每轮都要求工具，第 max_rounds 轮后截断为 failed 且不再执行工具。"""
    always_tool = [
        tool_call_response(f"call_{i}", "echo", json.dumps({"text": f"第{i}轮"})) for i in range(1, 5)
    ]
    llm = ScriptedLLM(always_tool)
    graph = build_graph(llm, [EchoTool()], max_rounds=3)

    result = await invoke(graph, initial_state())

    assert result.status is AgentStatus.FAILED
    assert result.final_result is None
    assert result.round == 3  # 停在上限轮次，不溢出
    assert len(llm.calls) == 3  # 恰好 3 轮 LLM 决策
    assert llm.remaining == 1  # 第 4 个响应未被消费
    assert len(result.tool_results) == 2  # 第 3 轮的工具请求不再执行
    assert any("最大轮次" in error for error in result.errors)


async def test_tool_error_marks_failed() -> None:
    """工具执行报错：错误归一化进 errors 与 tool_results，状态置 failed 直接结束。"""
    llm = ScriptedLLM([tool_call_response("call_1", "boom", "{}"), final_response("不应到达")])
    graph = build_graph(llm, [EchoTool(), BoomTool()])

    result = await invoke(graph, initial_state())

    assert result.status is AgentStatus.FAILED
    assert result.final_result is None
    assert len(llm.calls) == 1  # 失败后不再回到 LLM

    record = result.tool_results[0]
    assert record.tool == "boom"
    assert record.result.success is False
    assert record.result.error_code is not None
    assert "炸了" in (record.result.message or "")
    assert any("boom" in error and "失败" in error for error in result.errors)

    # 失败结果同样以 tool 消息回填，链路完整可观测
    assert result.messages[-1]["role"] == "tool"
    failed = ToolResult.model_validate_json(result.messages[-1]["content"])
    assert failed.success is False


async def test_invalid_tool_arguments_marks_failed() -> None:
    """LLM 产出非法参数 JSON：不调用工具，按统一错误结构记录并置 failed。"""
    llm = ScriptedLLM([tool_call_response("call_1", "echo", "不是JSON"), final_response("不应到达")])
    graph = build_graph(llm, [EchoTool()])

    result = await invoke(graph, initial_state())

    assert result.status is AgentStatus.FAILED
    record = result.tool_results[0]
    assert record.result.success is False
    assert record.result.error_code == "TOOL_CALL_FAILED"
    assert any("参数解析失败" in error for error in result.errors)


def test_result_digest_is_structural() -> None:
    """成功结果摘要只承载结构（列表条数 / 标量 key=value），不携带行级数据。"""
    from app.agents.graph import _result_digest

    rows = {"orders": [{"id": 1, "amount": 360000.0}, {"id": 2, "amount": 1.0}], "count": 2}
    digest = _result_digest(rows)
    assert digest == "orders×2，count=2"
    assert "amount" not in digest  # 行内字段不进摘要

    nested = {"orders": {"total_count": 12, "total_amount": 999.0}, "tickets": []}
    assert _result_digest(nested) == "orders{total_count=12，total_amount=999.0}，tickets×0"

    assert _result_digest({"echo": "你好"}) == "echo=你好"
    assert _result_digest(None) is None
    assert _result_digest(["a", "b"]) == "共 2 条"
    assert _result_digest("已完成") == "已完成"

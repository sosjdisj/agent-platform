"""统一 LLM Client 单测：httpx MockTransport 模拟 OpenAI 兼容服务端。

覆盖三种能力：chat（含 tool calling）/ chat_stream / chat_structured。
Client 一律经 create_llm_service(http_client=...) 工厂创建（与生产同一路径，仅注入 mock 传输）。
"""

import json
from typing import Any, Callable, Literal

import httpx
import pytest
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.llm import LLMService, LLMStructuredOutputError, create_llm_service


def completion_response(message: dict[str, Any]) -> dict[str, Any]:
    """构造 OpenAI 非流式响应体。"""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": get_settings().llm_model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def chunk_response(delta: dict[str, Any], finish_reason: str | None) -> dict[str, Any]:
    """构造 OpenAI 流式响应块。"""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": get_settings().llm_model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def sse_response(chunks: list[dict[str, Any]]) -> httpx.Response:
    """把响应块序列化为 SSE 响应（data 行逐块输出，[DONE] 结束标记）。"""
    events = [f"data: {json.dumps(chunk, ensure_ascii=False)}" for chunk in chunks]
    body = ("\n\n".join(events) + "\n\ndata: [DONE]\n\n").encode("utf-8")
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


@pytest.fixture
async def mock_service():
    """返回 LLMService 工厂：入参为 mock 请求处理器，产出 (service, captured)，captured 记录最近一次请求。

    测试结束统一关闭注入的 httpx 客户端。
    """
    http_clients: list[httpx.AsyncClient] = []

    def _make(
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> tuple[LLMService, dict[str, Any]]:
        captured: dict[str, Any] = {}

        def record(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return handler(request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(record))
        http_clients.append(http)
        return create_llm_service(http_client=http), captured

    yield _make

    for http in http_clients:
        await http.aclose()


async def test_chat_returns_assistant_content(mock_service) -> None:
    """chat：返回助手回复，且配置项（model）注入请求、未提供的可选参数不进入请求体。"""
    service, captured = mock_service(
        lambda request: httpx.Response(
            200, json=completion_response({"role": "assistant", "content": "您好，请问有什么可以帮您？"})
        )
    )

    result = await service.chat([{"role": "user", "content": "你好"}])

    assert result.choices[0].message.content == "您好，请问有什么可以帮您？"
    body = json.loads(captured["request"].content)
    assert body["model"] == get_settings().llm_model  # 配置项 LLM_MODEL 注入请求
    assert body["messages"] == [{"role": "user", "content": "你好"}]
    assert "tools" not in body and "temperature" not in body  # 未提供的可选参数省略


QUERY_ORDERS_TOOL = {
    "type": "function",
    "function": {
        "name": "query_orders",
        "description": "按客户查询订单明细",
        "parameters": {
            "type": "object",
            "properties": {"customer_id": {"type": "integer"}},
            "required": ["customer_id"],
        },
    },
}


async def test_chat_tool_calling_returns_tool_calls(mock_service) -> None:
    """chat + tool calling：请求携带工具定义，返回模型决策的 tool_calls（名称 + JSON 参数）。"""
    tool_call_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_test_1",
                "type": "function",
                "function": {"name": "query_orders", "arguments": json.dumps({"customer_id": 1})},
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tools"] == [QUERY_ORDERS_TOOL]
        assert body["tool_choice"] == "auto"
        return httpx.Response(200, json=completion_response(tool_call_message))

    service, _ = mock_service(handler)
    result = await service.chat(
        [{"role": "user", "content": "查一下 1 号客户的订单"}],
        tools=[QUERY_ORDERS_TOOL],
        tool_choice="auto",
    )

    message = result.choices[0].message
    assert message.tool_calls is not None
    assert message.tool_calls[0].function.name == "query_orders"
    assert json.loads(message.tool_calls[0].function.arguments) == {"customer_id": 1}


async def test_chat_stream_yields_content_deltas(mock_service) -> None:
    """chat_stream：请求 stream=true，逐块产出增量正文并可拼接为完整回复。"""
    parts = ["您好", "，", "请问有什么可以帮您？"]
    chunks = [chunk_response({"content": part}, None) for part in parts]
    chunks.append(chunk_response({}, "stop"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return sse_response(chunks)

    service, _ = mock_service(handler)
    texts = [
        chunk.choices[0].delta.content
        async for chunk in service.chat_stream([{"role": "user", "content": "你好"}])
    ]

    assert "".join(text for text in texts if text) == "您好，请问有什么可以帮您？"
    assert texts[-1] is None  # 结束块无正文（finish_reason=stop）


class TicketDraft(BaseModel):
    """结构化输出的目标模型：售后工单草稿。"""

    title: str
    priority: Literal["low", "medium", "high"]


async def test_chat_structured_returns_parsed_model(mock_service) -> None:
    """chat_structured：请求携带 json_object response_format + Schema 提示词，返回经 Pydantic 校验的模型实例。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        # Schema 注入到末尾追加的 system 消息（json_schema 类型部分兼容服务不支持）
        assert body["messages"][-1]["role"] == "system"
        assert "JSON Schema" in body["messages"][-1]["content"]
        assert "TicketDraft" in body["messages"][-1]["content"]
        content = json.dumps({"title": "无法登录系统", "priority": "high"}, ensure_ascii=False)
        return httpx.Response(200, json=completion_response({"role": "assistant", "content": content}))

    service, _ = mock_service(handler)
    result = await service.chat_structured([{"role": "user", "content": "建一个紧急工单"}], TicketDraft)

    assert result == TicketDraft(title="无法登录系统", priority="high")


async def test_chat_structured_raises_on_refusal(mock_service) -> None:
    """chat_structured：模型拒绝回答（refusal）时抛 LLMStructuredOutputError。"""
    service, _ = mock_service(
        lambda request: httpx.Response(
            200,
            json=completion_response({"role": "assistant", "content": None, "refusal": "抱歉，我无法协助该请求"}),
        )
    )

    with pytest.raises(LLMStructuredOutputError, match="无法协助"):
        await service.chat_structured([{"role": "user", "content": "干点别的"}], TicketDraft)

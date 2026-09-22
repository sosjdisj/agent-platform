"""统一 LLM 客户端：OpenAI 兼容 API 封装，全项目唯一的 LLM Client 创建点。

grep 校验约定：AsyncOpenAI 实例化仅允许出现在本模块的 create_llm_service 内。
业务代码一律经 get_llm_service() 使用单例；测试经 create_llm_service(http_client=...) 注入 MockTransport。

- 配置：LLM_BASE_URL / LLM_API_KEY / LLM_MODEL（见 app/core/config.py 与根目录 .env）
- chat：对话补全，支持 tool calling（tools / tool_choice 透传，返回 message.tool_calls）
- chat_stream：流式对话补全，逐块产出 ChatCompletionChunk
- chat_structured：结构化输出（json_object 模式 + Schema 提示词注入 + Pydantic 校验），直接返回模型实例
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, TypeVar

import httpx
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolParam,
)
from pydantic import BaseModel

from app.core.config import get_settings

T = TypeVar("T", bound=BaseModel)

# 未配置 LLM_API_KEY 时的占位密钥（vLLM / Ollama 等本地推理服务的惯例值；真实云端服务必须配置）
EMPTY_API_KEY = "EMPTY"


class LLMStructuredOutputError(Exception):
    """结构化输出失败：模型拒绝回答（refusal）或未返回可解析内容。"""


class LLMService:
    """OpenAI 兼容 LLM 服务封装：对话 / 流式 / 工具调用 / 结构化输出。

    仅做薄封装：返回 OpenAI SDK 原始类型（ChatCompletion / ChatCompletionChunk），
    不二次包装，调用方按需读取 content / tool_calls / delta。
    """

    def __init__(
        self, client: AsyncOpenAI, model: str, *, default_temperature: float | None = None
    ) -> None:
        self._client = client
        self._model = model
        self._default_temperature = default_temperature

    def _request_kwargs(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        *,
        tools: Sequence[ChatCompletionToolParam] | None,
        tool_choice: Any,
        temperature: float | None,
    ) -> dict[str, Any]:
        """公共请求参数；值为 None 的可选参数不进入请求体（部分兼容服务对 null 字段容错差）。
        temperature 未显式传入时回退实例缺省（评估配置隔离点：create_evaluation_llm 固定为评估值）。"""
        if temperature is None:
            temperature = self._default_temperature
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools is not None:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        return kwargs

    async def chat(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        *,
        tools: Sequence[ChatCompletionToolParam] | None = None,
        tool_choice: Any = None,
        temperature: float | None = None,
    ) -> ChatCompletion:
        """对话补全；提供 tools 时支持 tool calling，模型决策结果见 message.tool_calls。"""
        return await self._client.chat.completions.create(
            **self._request_kwargs(messages, tools=tools, tool_choice=tool_choice, temperature=temperature)
        )

    async def chat_stream(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        *,
        tools: Sequence[ChatCompletionToolParam] | None = None,
        tool_choice: Any = None,
        temperature: float | None = None,
    ) -> AsyncIterator[ChatCompletionChunk]:
        """流式对话补全：逐块产出 ChatCompletionChunk，正文从 delta.content 增量读取。"""
        stream = await self._client.chat.completions.create(
            stream=True,
            **self._request_kwargs(messages, tools=tools, tool_choice=tool_choice, temperature=temperature),
        )
        async for chunk in stream:
            yield chunk

    async def chat_structured(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        response_model: type[T],
        *,
        temperature: float | None = None,
    ) -> T:
        """结构化输出：json_object 模式 + Schema 提示词注入 + Pydantic 校验，直接返回模型实例。

        不使用 json_schema response_format（部分 OpenAI 兼容服务如 DeepSeek 未开放该类型，
        请求返回 400 invalid_request_error），改为 json_object 并把 Schema 写入末尾 system 消息。
        模型拒绝回答或未返回内容时抛 LLMStructuredOutputError；JSON 不合法时透传 pydantic 校验错误。
        """
        schema_instruction = (
            "只输出一个符合以下 JSON Schema 的 JSON 对象，"
            "不要输出任何解释文字或 Markdown 代码块：\n"
            f"{json.dumps(response_model.model_json_schema(), ensure_ascii=False)}"
        )
        schema_message: ChatCompletionSystemMessageParam = {
            "role": "system",
            "content": schema_instruction,
        }
        completion = await self._client.chat.completions.create(
            **self._request_kwargs(
                [*messages, schema_message],
                tools=None,
                tool_choice=None,
                temperature=temperature,
            ),
            response_format={"type": "json_object"},
        )
        message = completion.choices[0].message
        if not message.content:
            raise LLMStructuredOutputError(message.refusal or "模型未返回可解析的结构化输出")
        return response_model.model_validate_json(message.content)


def create_llm_service(
    http_client: httpx.AsyncClient | None = None, *, default_temperature: float | None = None
) -> LLMService:
    """创建 LLMService：全项目唯一创建 AsyncOpenAI 的位置（grep 校验点）。

    http_client 仅供测试注入 MockTransport（业务代码勿传）；
    default_temperature 供评估配置隔离注入（不传则不发送 temperature，生产行为不变）。
    """
    settings = get_settings()
    client = AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or EMPTY_API_KEY,
        http_client=http_client,
    )
    return LLMService(client, model=settings.llm_model, default_temperature=default_temperature)


# 全局单例：AsyncOpenAI 内部维护连接池，整个进程复用，避免每请求重建
llm_service = create_llm_service()


def get_llm_service() -> LLMService:
    """FastAPI 依赖：返回全局 LLMService 单例。"""
    return llm_service

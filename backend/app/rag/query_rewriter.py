"""查询改写：LLM 将口语化问题改写为规范化检索语句（如「退款怎么搞？」→「退款政策与流程」）。

LLM 调用以 Callable 注入（输入 prompt、返回模型文本，同步/异步均可），本模块不绑定任何 SDK，
真实客户端由上层（Prompt 12 KnowledgeAgent）接入；改写决策由检索层记录为结构化元数据。
"""
from __future__ import annotations

import inspect
from typing import Awaitable, Callable, Union

# LLM 调用可同步（检索器线程内 rewrite）或异步（事件循环内 arewrite）
LLMCallable = Union[Callable[[str], str], Callable[[str], Awaitable[str]]]

REWRITE_PROMPT = """你是检索查询改写助手。请将用户的口语化问题改写为适合向量检索的规范化查询语句：
- 保持原意，补全省略的关键信息，去除口语、语气词与寒暄；
- 只输出改写后的查询语句本身，不要解释，不要加引号；
- 若问题已足够规范或无法改写，原样输出该问题。

用户问题：{query}"""


class QueryRewriter:
    """基于 LLM 的查询改写器。"""

    def __init__(self, llm: LLMCallable) -> None:
        self.llm = llm

    def rewrite(self, query: str) -> str | None:
        """同步改写（LLM 为同步可调用）；LLM 原样返回（无需/无法改写）或输出为空时返回 None。"""
        return self._finalize(query, self.llm(REWRITE_PROMPT.format(query=query)))

    async def arewrite(self, query: str) -> str | None:
        """异步改写（事件循环内使用，兼容同步/异步 LLM 可调用）；收敛语义与 rewrite 一致。"""
        result = self.llm(REWRITE_PROMPT.format(query=query))
        if inspect.isawaitable(result):
            result = await result
        return self._finalize(query, result)

    @staticmethod
    def _finalize(query: str, rewritten: str) -> str | None:
        """收敛判定：输出为空、原样返回（无需/无法改写）时返回 None，否则返回清洗后的查询。"""
        rewritten = rewritten.strip().strip("\"“”")
        if not rewritten or rewritten == query:
            return None
        return rewritten

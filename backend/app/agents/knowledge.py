"""KnowledgeAgent：知识问答 Agent。

流程（复用基础 LLM↔Tool 图，不重复实现循环 / 错误处理，见 app/agents/graph.py）：
LLM 判断是否需要知识（tool calling 决策）→ 需要 → 调 Knowledge MCP search_knowledge
→ LLM 汇总产出回答；不需要 → 直接回答。
检索侧为充分性闭环（Prompt 12.2）：LLM 评审检索结果是否足以回答，不足则经 QueryRewriter
改写重搜（复用 Prompt 9.4，全程最多 query_rewrite_max_times 次），改写预算耗尽仍无答案
→ AgentResult status=partial + error_code=NO_RELEVANT_DOCUMENT（如实兜底）。
Agent 层职责：注入评审与改写器、从执行状态组装带知识引用来源（Source）的 AgentResult。

- 工具面仅暴露 search_knowledge（不共享全局 MCP 工具面，避免误调业务工具）；
- 不做：Supervisor 多 Agent 路由；
- 独立调试：python -m app.agents.knowledge "查询文本"（CLI 见 main），
  POST /api/debug/agents/knowledge（HTTP 调试接口，见 app/api/routes/debug_agents.py）。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Awaitable, Callable

from fastmcp import FastMCP
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.runner import AgentRunner
from app.agents.state import AgentResult, AgentState, AgentStatus, Source
from app.core.llm import LLMService, get_llm_service
from app.core.redis import RedisService
from app.mcp.client import MCPClient
from app.mcp.knowledge.search_knowledge import (
    SearchKnowledgeOutput,
    SearchKnowledgeTool,
    SufficiencyJudge,
)
from app.mcp.registry import ToolRegistry
from app.rag.query_rewriter import QueryRewriter
from app.rag.retriever import NO_RELEVANT_DOCUMENT, KnowledgeRetriever

KNOWLEDGE_AGENT_NAME = "knowledge"

# 判断是否需要知识的系统提示：事实性问题先检索、结果不足或未命中如实说明、寒暄直接回答
KNOWLEDGE_SYSTEM_PROMPT = (
    "你是知识库问答助手：回答事实性问题前，先用 search_knowledge 检索内部知识文档，"
    "依据检索结果作答；检索结果不足以回答或未命中时如实告知，不要编造；"
    "寒暄等无需知识的场景直接回答。"
)

# 充分性评审提示：只依据内容相关性判断，输出 JSON 便于机器解析
SUFFICIENCY_PROMPT = """你是检索质量评审员。判断以下检索结果是否足以回答用户问题：
- 只依据内容与问题的相关性判断：结果覆盖问题所问的关键信息即为充分，不要求措辞完整；
- 只输出 JSON：{{"sufficient": true}} 或 {{"sufficient": false}}，不要输出其他内容。

用户问题：{query}
检索结果：
{evidence}"""


def _chat_text(llm: LLMService) -> Callable[[str], Awaitable[str]]:
    """prompt → 模型文本 的异步适配（供 QueryRewriter 注入，复用统一 LLM 客户端）。"""

    async def call(prompt: str) -> str:
        completion = await llm.chat([{"role": "user", "content": prompt}])
        return completion.choices[0].message.content or ""

    return call


def _parse_sufficiency(text: str) -> bool:
    """解析评审输出中的 {"sufficient": bool}；解析失败按充分处理（评审故障不阻断作答流程）。"""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            value = json.loads(match.group()).get("sufficient")
            if isinstance(value, bool):
                return value
        except json.JSONDecodeError:
            pass
    return True


def make_sufficiency_judge(llm: LLMService) -> SufficiencyJudge:
    """充分性评审器：把当前检索结果交给 LLM 判断是否足以回答原始问题。"""

    async def judge(query: str, output: SearchKnowledgeOutput) -> bool:
        evidence = "\n".join(
            f"[{c.chunk_id}] {c.document_title}：{c.content}" for c in output.chunks
        )
        completion = await llm.chat(
            [{"role": "user", "content": SUFFICIENCY_PROMPT.format(query=query, evidence=evidence)}]
        )
        return _parse_sufficiency(completion.choices[0].message.content or "")

    return judge


class KnowledgeAgentError(RuntimeError):
    """KnowledgeAgent 执行失败：携带状态中累计的错误信息。"""


def assemble_knowledge_result(state: AgentState) -> AgentResult:
    """组装知识 Agent 输出：正文取 final_result；引用来源从 search_knowledge 工具记录
    提取（knowledge 来源：ref_id=chunk_id、document_id=文档 ID、title=文档标题、score=相关性分），
    按 chunk_id 去重。

    结果语义：最后一次 search_knowledge 的 sufficient=False（改写预算耗尽仍无答案）
    → status=partial + error_code=NO_RELEVANT_DOCUMENT；否则 completed。
    """
    if state.status is not AgentStatus.COMPLETED or state.final_result is None:
        raise KnowledgeAgentError("；".join(state.errors) or "任务未完成即结束")

    sources: dict[str, Source] = {}
    last_sufficient: bool | None = None
    for record in state.tool_results:
        if record.tool != "search_knowledge" or not record.result.success:
            continue
        data = record.result.data or {}
        last_sufficient = data.get("sufficient")
        for chunk in data.get("chunks", []):
            sources.setdefault(
                chunk["chunk_id"],
                Source(
                    source_type="knowledge",
                    ref_id=chunk["chunk_id"],
                    document_id=chunk.get("document_id"),
                    title=chunk.get("document_title"),
                    score=chunk.get("score"),
                ),
            )

    insufficient = last_sufficient is False
    return AgentResult(
        agent=KNOWLEDGE_AGENT_NAME,
        content=state.final_result.content,
        sources=list(sources.values()),
        status=AgentStatus.PARTIAL if insufficient else AgentStatus.COMPLETED,
        error_code=NO_RELEVANT_DOCUMENT if insufficient else None,
    )


class KnowledgeAgent:
    """知识问答 Agent：run / resume 语义与状态持久化全部复用 AgentRunner。"""

    def __init__(
        self,
        llm: LLMService,
        *,
        retriever: KnowledgeRetriever | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        max_rounds: int | None = None,
        auth_session_maker: async_sessionmaker[AsyncSession] | None = None,
        trace_session_maker: async_sessionmaker[AsyncSession] | None = None,
        trace_redis: RedisService | None = None,
    ) -> None:
        registry = ToolRegistry()
        registry.register(
            SearchKnowledgeTool(
                retriever,  # None → 默认真实 RAG 栈
                judge=make_sufficiency_judge(llm),
                rewriter=QueryRewriter(_chat_text(llm)),
            )
        )
        server = FastMCP("knowledge-agent")
        registry.mount_to(server)
        self._runner = AgentRunner(
            llm,
            MCPClient(server=server, registry=registry),
            registry,
            checkpointer,
            max_rounds=max_rounds,
            auth_session_maker=auth_session_maker,
            trace_session_maker=trace_session_maker,
            trace_redis=trace_redis,
            trace_task_events=False,  # 任务级埋点归 TaskExecutor（21.2），本层只写节点级事件
        )

    async def answer(self, task_id: int, user_id: int, query: str) -> AgentResult:
        """回答一次知识问答：全流程执行并组装带引用来源的结果（失败抛 KnowledgeAgentError）。"""
        state = await self._runner.run(
            task_id,
            user_id,
            [{"role": "system", "content": KNOWLEDGE_SYSTEM_PROMPT}, {"role": "user", "content": query}],
        )
        return assemble_knowledge_result(state)


def main() -> None:
    """CLI 独立调试入口（不经业务编排）：python -m app.agents.knowledge "退货流程是什么"。

    依赖：.env 配置 LLM_*，知识文档需先入库（python -m app.rag.ingest）。
    task_id / user_id 取 0（调试语义，不关联业务任务）。
    """
    if len(sys.argv) < 2:
        raise SystemExit("用法：python -m app.agents.knowledge <查询文本>")

    query = " ".join(sys.argv[1:])
    agent = KnowledgeAgent(get_llm_service())  # 默认真实 RAG 栈
    result = asyncio.run(agent.answer(task_id=0, user_id=0, query=query))
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()

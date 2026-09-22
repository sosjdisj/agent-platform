"""KnowledgeAgent 测试：脚本化 LLM + 检索器替身。

覆盖：命中（评审充分）/ 无需知识 / 改写后命中 / 改写预算耗尽（partial）/ 改写器拒绝 /
评审输出异常 / 检索异常 / 来源组装与结果语义 / 调试接口。
"""

import json

import pytest

from app.agents.knowledge import (
    KNOWLEDGE_AGENT_NAME,
    KNOWLEDGE_SYSTEM_PROMPT,
    KnowledgeAgent,
    KnowledgeAgentError,
    assemble_knowledge_result,
    make_sufficiency_judge,
)
from app.agents.state import AgentResult, AgentState, AgentStatus, ToolCallRecord
from app.mcp.base import ToolResult
from app.mcp.knowledge.search_knowledge import SearchKnowledgeTool
from app.rag.chunker import DocumentChunk
from app.rag.query_rewriter import QueryRewriter
from app.rag.retriever import NO_RELEVANT_DOCUMENT, RetrievalResult, RetrievedChunk
from tests.test_agent_graph import ScriptedLLM, final_response, tool_call_response


class StubRetriever:
    """检索器替身：返回预置 RetrievalResult，记录收到的查询。"""

    def __init__(self, result: RetrievalResult) -> None:
        self.result = result
        self.queries: list[str] = []

    def retrieve(self, query: str) -> RetrievalResult:
        self.queries.append(query)
        return self.result


class QueryMappedRetriever:
    """按查询映射结果的检索器替身：未映射的查询一律未命中（模拟改写后才命中）。"""

    def __init__(self, results: dict[str, RetrievalResult]) -> None:
        self.results = results
        self.queries: list[str] = []

    def retrieve(self, query: str) -> RetrievalResult:
        self.queries.append(query)
        if query in self.results:
            return self.results[query]
        return RetrievalResult(found=False, error_code=NO_RELEVANT_DOCUMENT)


def hit_result(*scores: float) -> RetrievalResult:
    """命中结果：每个分数对应一个知识分块（chunk_id 按序号生成）。"""
    return RetrievalResult(
        found=True,
        chunks=[
            RetrievedChunk(
                chunk=DocumentChunk(
                    chunk_id=f"kb-{i}",
                    document_id="return-policy",
                    document_title="退货政策",
                    heading_path=["售后", "退货政策"],
                    content=f"第{i}条：退货流程说明。",
                    index=i - 1,
                ),
                score=score,
            )
            for i, score in enumerate(scores, start=1)
        ],
    )


def miss_result() -> RetrievalResult:
    return RetrievalResult(found=False, error_code=NO_RELEVANT_DOCUMENT)


def agent(llm: ScriptedLLM, retriever) -> KnowledgeAgent:
    return KnowledgeAgent(llm, retriever=retriever)  # type: ignore[arg-type]  图仅依赖 chat 接口


async def test_hit_assembles_knowledge_sources() -> None:
    """评审充分：LLM 决策检索 → 工具返回分块并经评审判定充分 → 汇总作答，来源含完整信息。"""
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "search_knowledge", json.dumps({"query": "退货流程"})),
            final_response('{"sufficient": true}'),  # 评审：检索结果足以回答
            final_response("根据知识库：先提交申请，再寄回商品。"),
        ]
    )
    retriever = StubRetriever(hit_result(0.92, 0.85))

    result = await agent(llm, retriever).answer(task_id=1, user_id=100, query="怎么退货？")

    assert result.agent == KNOWLEDGE_AGENT_NAME
    assert result.status is AgentStatus.COMPLETED
    assert result.error_code is None
    assert result.content == "根据知识库：先提交申请，再寄回商品。"
    assert [(s.ref_id, s.document_id, s.title, s.score) for s in result.sources] == [
        ("kb-1", "return-policy", "退货政策", 0.92),
        ("kb-2", "return-policy", "退货政策", 0.85),
    ]
    assert all(s.source_type == "knowledge" for s in result.sources)
    assert retriever.queries == ["退货流程"]  # 首搜即充分，不触发改写重搜
    # LLM 调用序列：决策 → 评审（携带问题与检索证据）→ 汇总
    assert len(llm.calls) == 3
    judge_prompt = llm.calls[1]["messages"][0]["content"]
    assert "退货流程" in judge_prompt and "kb-1" in judge_prompt
    assert [m["role"] for m in llm.calls[2]["messages"]] == ["system", "user", "assistant", "tool"]


async def test_direct_answer_skips_search() -> None:
    """判断无需知识：直接回答，不调工具，检索器未被触发，来源为空。"""
    llm = ScriptedLLM([final_response("你好，有什么可以帮你？")])
    retriever = StubRetriever(hit_result())

    result = await agent(llm, retriever).answer(task_id=1, user_id=100, query="在吗")

    assert result.status is AgentStatus.COMPLETED
    assert result.content == "你好，有什么可以帮你？"
    assert result.sources == []
    assert retriever.queries == []
    # 系统提示指导判断：无需知识的场景不触发检索
    assert [m["role"] for m in llm.calls[0]["messages"]] == ["system", "user"]
    assert KNOWLEDGE_SYSTEM_PROMPT in llm.calls[0]["messages"][0]["content"]


async def test_insufficient_then_rewrite_hits() -> None:
    """改写后命中：首搜命中但评审不充分 → QueryRewriter 改写重搜 → 评审充分 → 正常作答。"""
    original_query, rewritten_query = "退货怎么搞", "退货政策与流程"
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "search_knowledge", json.dumps({"query": original_query})),
            final_response('{"sufficient": false}'),  # 评审：首搜结果不足以回答
            final_response(rewritten_query),  # 改写器输出
            final_response('{"sufficient": true}'),  # 评审：重搜结果充分
            final_response("根据知识库：先提交申请，再寄回商品。"),
        ]
    )
    # 首搜命中但内容不相关（低分块），改写后的查询才命中真正知识
    retriever = QueryMappedRetriever({original_query: hit_result(0.6), rewritten_query: hit_result(0.95, 0.88)})

    result = await agent(llm, retriever).answer(task_id=1, user_id=100, query=original_query)

    assert result.status is AgentStatus.COMPLETED
    assert result.error_code is None
    assert result.content == "根据知识库：先提交申请，再寄回商品。"
    assert [(s.ref_id, s.score) for s in result.sources] == [("kb-1", 0.95), ("kb-2", 0.88)]
    assert retriever.queries == [original_query, rewritten_query]  # 原查询 + 改写查询各检索一次
    # 两次评审均对照原始问题（改写只优化检索词，不改变信息需求）
    assert llm.calls[1]["messages"][0]["content"].count(original_query) == 1
    assert original_query in llm.calls[3]["messages"][0]["content"]


async def test_budget_exhausted_returns_partial() -> None:
    """2 次改写仍无答案：改写预算耗尽（默认 2 次）→ 如实兜底 → status=partial + NO_RELEVANT_DOCUMENT。"""
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "search_knowledge", json.dumps({"query": "外星文明"})),
            final_response("外星文明探索史"),  # 改写 1
            final_response("外星文明是否存在"),  # 改写 2
            final_response("知识库中暂无相关内容"),
        ]
    )
    retriever = StubRetriever(miss_result())  # 未命中是业务结果：全程不触发评审，只改写重搜

    result = await agent(llm, retriever).answer(task_id=1, user_id=100, query="外星文明")

    assert result.status is AgentStatus.PARTIAL
    assert result.error_code == NO_RELEVANT_DOCUMENT
    assert result.content == "知识库中暂无相关内容"
    assert result.sources == []
    assert retriever.queries == ["外星文明", "外星文明探索史", "外星文明是否存在"]
    # LLM 调用恰好 4 次：决策 + 2 次改写 + 兜底作答（未命中不触发评审）
    assert len(llm.calls) == 4


async def test_rewriter_decline_returns_partial() -> None:
    """改写器判定无法改写（原样返回）：立即收敛，不再重搜，结果仍为 partial。"""
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "search_knowledge", json.dumps({"query": "外星文明"})),
            final_response("外星文明"),  # 改写器原样返回 → None → 收敛
            final_response("知识库中暂无相关内容"),
        ]
    )
    retriever = StubRetriever(miss_result())

    result = await agent(llm, retriever).answer(task_id=1, user_id=100, query="外星文明")

    assert result.status is AgentStatus.PARTIAL
    assert result.error_code == NO_RELEVANT_DOCUMENT
    assert retriever.queries == ["外星文明"]  # 仅首搜一次


async def test_judge_parse_failure_treated_as_sufficient() -> None:
    """评审输出无法解析：按充分处理放行（评审故障不阻断作答流程），不触发改写。"""
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "search_knowledge", json.dumps({"query": "退货流程"})),
            final_response("我觉得还可以"),  # 非 JSON 输出
            final_response("根据知识库：先提交申请。"),
        ]
    )
    retriever = StubRetriever(hit_result(0.9))

    result = await agent(llm, retriever).answer(task_id=1, user_id=100, query="怎么退货？")

    assert result.status is AgentStatus.COMPLETED
    assert retriever.queries == ["退货流程"]


async def test_tool_failure_raises_knowledge_agent_error() -> None:
    """检索器抛异常：工具错误归一化 → 状态 failed → answer 抛 KnowledgeAgentError 携带错误链。"""

    class BoomRetriever:
        def retrieve(self, query: str) -> RetrievalResult:
            raise RuntimeError("检索炸了")

    llm = ScriptedLLM(
        [tool_call_response("call_1", "search_knowledge", json.dumps({"query": "退货"}))]
    )

    with pytest.raises(KnowledgeAgentError) as exc_info:
        await agent(llm, BoomRetriever()).answer(task_id=1, user_id=100, query="怎么退货")  # type: ignore[arg-type]

    assert "search_knowledge" in str(exc_info.value) and "TOOL_INTERNAL" in str(exc_info.value)


def _record(call_id: str, chunk_ids: list[str], *, sufficient: bool | None = None) -> ToolCallRecord:
    """一次成功的 search_knowledge 调用记录（data 为工具输出 dict）。"""
    data: dict = {
        "found": True,
        "chunks": [
            {
                "chunk_id": cid,
                "document_title": "退货政策",
                "content": f"{cid} 内容",
                "score": 0.5,
            }
            for cid in chunk_ids
        ],
    }
    if sufficient is not None:
        data["sufficient"] = sufficient
    return ToolCallRecord(
        tool="search_knowledge",
        arguments={"query": call_id},
        result=ToolResult.ok(data),
    )


def test_assemble_dedupes_repeated_chunks_across_rounds() -> None:
    """来源组装：多轮检索命中相同分块时按 chunk_id 去重，保留首次引用。"""
    state = AgentState(
        task_id=1,
        user_id=100,
        messages=[],
        status=AgentStatus.COMPLETED,
        tool_results=[_record("call_1", ["kb-1"]), _record("call_2", ["kb-1", "kb-2"])],
        final_result=AgentResult(agent="assistant", content="答案", sources=[]),
    )

    result = assemble_knowledge_result(state)

    assert [s.ref_id for s in result.sources] == ["kb-1", "kb-2"]
    assert result.agent == KNOWLEDGE_AGENT_NAME
    assert result.content == "答案"
    assert result.status is AgentStatus.COMPLETED  # 无充分性判定（纯记录）→ completed


def test_assemble_maps_final_verdict_to_result_status() -> None:
    """结果语义：最后一次检索的 sufficient=False → partial + NO_RELEVANT_DOCUMENT；True → completed。"""
    final = AgentResult(agent="assistant", content="答案", sources=[])
    partial_state = AgentState(
        task_id=1,
        user_id=100,
        messages=[],
        status=AgentStatus.COMPLETED,
        tool_results=[_record("call_1", ["kb-1"], sufficient=True), _record("call_2", [], sufficient=False)],
        final_result=final,
    )

    partial = assemble_knowledge_result(partial_state)
    assert partial.status is AgentStatus.PARTIAL
    assert partial.error_code == NO_RELEVANT_DOCUMENT

    done = assemble_knowledge_result(
        AgentState(
            task_id=1,
            user_id=100,
            messages=[],
            status=AgentStatus.COMPLETED,
            tool_results=[_record("call_1", ["kb-1"], sufficient=True)],
            final_result=final,
        )
    )
    assert done.status is AgentStatus.COMPLETED
    assert done.error_code is None


def test_assemble_raises_on_failed_state() -> None:
    """失败状态（如轮次截断）：组装入口抛 KnowledgeAgentError，不产出半成品结果。"""
    state = AgentState(
        task_id=1,
        user_id=100,
        messages=[],
        status=AgentStatus.FAILED,
        errors=["已达最大轮次上限（5），任务截断"],
    )

    with pytest.raises(KnowledgeAgentError, match="最大轮次"):
        assemble_knowledge_result(state)


async def test_search_knowledge_tool_maps_retrieval_result() -> None:
    """纯检索模式（全局工具面）：单次检索，输出不携带充分性判定与改写链路。"""
    tool = SearchKnowledgeTool(StubRetriever(hit_result(0.9)))  # type: ignore[arg-type]

    out = await tool.execute({"query": "退货"})
    assert out["success"] is True
    assert out["data"]["found"] is True
    assert out["data"]["chunks"][0]["chunk_id"] == "kb-1"
    assert out["data"]["chunks"][0]["score"] == 0.9
    assert out["data"]["sufficient"] is None
    assert out["data"]["rewrites"] == []

    miss = await SearchKnowledgeTool(StubRetriever(miss_result())).execute({"query": "未命中"})
    assert miss["success"] is True
    assert miss["data"]["found"] is False
    assert miss["data"]["error_code"] == NO_RELEVANT_DOCUMENT


def test_tool_requires_judge_and_rewriter_paired() -> None:
    """闭环组件必须成对注入：只给 judge 或只给 rewriter 均为配置错误。"""
    with pytest.raises(ValueError, match="judge 与 rewriter"):
        SearchKnowledgeTool(StubRetriever(hit_result()), judge=make_sufficiency_judge(ScriptedLLM([])))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="judge 与 rewriter"):
        SearchKnowledgeTool(StubRetriever(hit_result()), rewriter=QueryRewriter(lambda p: p))


async def test_debug_endpoint_returns_knowledge_result(client) -> None:
    """调试接口：POST /api/debug/agents/knowledge 全链路返回 AgentResult。"""
    from app.api.routes.debug_agents import get_knowledge_agent
    from app.main import app

    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "search_knowledge", json.dumps({"query": "退货流程"})),
            final_response('{"sufficient": true}'),
            final_response("根据知识库：先提交申请，再寄回商品。"),
        ]
    )
    agent = KnowledgeAgent(llm, retriever=StubRetriever(hit_result(0.92)))  # type: ignore[arg-type]
    app.dependency_overrides[get_knowledge_agent] = lambda: agent
    try:
        resp = await client.post(
            "/api/debug/agents/knowledge", json={"query": "怎么退货？", "task_id": 7, "user_id": 1}
        )
    finally:
        app.dependency_overrides.pop(get_knowledge_agent, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == KNOWLEDGE_AGENT_NAME
    assert body["status"] == "completed"
    assert body["error_code"] is None
    assert body["content"] == "根据知识库：先提交申请，再寄回商品。"
    assert body["sources"][0]["ref_id"] == "kb-1"


async def test_debug_endpoint_disabled_when_debug_off(client, monkeypatch) -> None:
    """非 debug 模式：调试接口返回 404，不暴露给生产环境。"""
    from app.api.routes.debug_agents import get_knowledge_agent
    from app.core.config import get_settings
    from app.main import app

    app.dependency_overrides[get_knowledge_agent] = lambda: None
    monkeypatch.setattr(get_settings(), "debug", False)
    try:
        resp = await client.post("/api/debug/agents/knowledge", json={"query": "在吗"})
    finally:
        app.dependency_overrides.pop(get_knowledge_agent, None)

    assert resp.status_code == 404

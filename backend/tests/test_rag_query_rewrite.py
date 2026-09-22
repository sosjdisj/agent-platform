"""查询改写链路测试：首搜未命中 → LLM 改写 → 重搜（最多 2 次）→ 结构化元数据。

Qdrant 内存模式 + FakeEmbedder（conftest）+ 可编程 FakeReranker / FakeRewriter，
不依赖外部服务与真实模型，改写序列由测试显式注入、可精确断言。
"""
import pytest
from qdrant_client import QdrantClient

from app.core.config import get_settings
from app.rag.ingest import ingest_documents
from app.rag.query_rewriter import QueryRewriter
from app.rag.retriever import NO_RELEVANT_DOCUMENT, KnowledgeRetriever
from app.rag.vector_store import KnowledgeVectorStore
from tests.conftest import FakeEmbedder


class FakeReranker:
    """按注入函数对 (query, text) 打分，并记录每次检索使用的查询。"""

    def __init__(self, score_fn) -> None:
        self.score_fn = score_fn
        self.seen_queries: list[str] = []

    def score(self, query: str, texts: list[str]) -> list[float]:
        self.seen_queries.append(query)
        return [float(self.score_fn(query, text)) for text in texts]


class FakeRewriter:
    """按预设序列依次返回改写结果，记录每次收到的查询。"""

    def __init__(self, outputs: list[str | None]) -> None:
        self.outputs = list(outputs)
        self.seen_queries: list[str] = []

    def rewrite(self, query: str) -> str | None:
        self.seen_queries.append(query)
        return self.outputs.pop(0) if self.outputs else None


COLLOQUIAL = "退款怎么搞？"
NORMALIZED = "退款政策与办理流程"


def _hit_only_for(queries: set[str]):
    """仅指定查询得高分（其余全部低于阈值），模拟「改写后才命中」。"""
    return lambda q, t: 1.0 if q in queries else -1.0


def _make_retriever(tmp_path, qdrant, score_fn, *, rewriter=None, max_rewrite_times=None):
    (tmp_path / "手册.md").write_text(
        "# 知识库手册\n\n" + "\n".join(
            f"## 第{i}节\n\n第{i}节介绍退款流程的环节{i}。\n" for i in range(1, 11)
        ),
        encoding="utf-8",
    )
    store = KnowledgeVectorStore(qdrant, collection_name="kb_rewrite", dimension=8)
    ingest_documents(tmp_path, embedder=FakeEmbedder(8), store=store, max_chars=200)
    return KnowledgeRetriever(
        embedder=FakeEmbedder(8),
        store=store,
        reranker=FakeReranker(score_fn),
        rewriter=rewriter,
        max_rewrite_times=max_rewrite_times,
    )


@pytest.fixture
def qdrant():
    return QdrantClient(":memory:")


def test_first_search_hit_skips_rewrite(tmp_path, qdrant):
    rewriter = FakeRewriter([NORMALIZED])  # 预设输出不应被消费
    retriever = _make_retriever(tmp_path, qdrant, _hit_only_for({COLLOQUIAL}), rewriter=rewriter)

    result = retriever.retrieve(COLLOQUIAL)

    assert result.found is True
    assert result.rewrites == []  # 首搜命中，不触发改写
    assert rewriter.seen_queries == []  # 改写器未被调用
    assert retriever.reranker.seen_queries == [COLLOQUIAL]  # 仅检索一次


def test_rewrite_then_hit(tmp_path, qdrant):
    rewriter = FakeRewriter([NORMALIZED])
    retriever = _make_retriever(
        tmp_path, qdrant, _hit_only_for({NORMALIZED}), rewriter=rewriter
    )

    result = retriever.retrieve(COLLOQUIAL)

    assert result.found is True
    assert result.error_code is None
    assert len(result.chunks) == 5  # 改写后重搜命中，正常返回 Top-5
    # 改写决策记录为结构化元数据
    assert len(result.rewrites) == 1
    rewrite = result.rewrites[0]
    assert rewrite.attempt == 1
    assert rewrite.from_query == COLLOQUIAL
    assert rewrite.to_query == NORMALIZED
    assert retriever.reranker.seen_queries == [COLLOQUIAL, NORMALIZED]  # 首搜 + 改写后重搜


def test_second_rewrite_hits(tmp_path, qdrant):
    first, second = "第一次改写", "第二次改写"
    rewriter = FakeRewriter([first, second])
    retriever = _make_retriever(
        tmp_path, qdrant, _hit_only_for({second}), rewriter=rewriter
    )

    result = retriever.retrieve(COLLOQUIAL)

    assert result.found is True
    assert [(r.attempt, r.from_query, r.to_query) for r in result.rewrites] == [
        (1, COLLOQUIAL, first),
        (2, first, second),
    ]
    assert retriever.reranker.seen_queries == [COLLOQUIAL, first, second]


def test_max_two_rewrites_then_no_relevant_document(tmp_path, qdrant):
    # 默认改写次数来自配置（2）：原始 + 2 次改写共 3 次检索，均未命中
    rewriter = FakeRewriter(["改写一", "改写二"])
    retriever = _make_retriever(tmp_path, qdrant, lambda q, t: -1.0, rewriter=rewriter)

    result = retriever.retrieve(COLLOQUIAL)

    assert result.found is False
    assert result.error_code == NO_RELEVANT_DOCUMENT
    assert result.chunks == []  # 不硬凑结果
    assert [(r.attempt, r.from_query, r.to_query) for r in result.rewrites] == [
        (1, COLLOQUIAL, "改写一"),
        (2, "改写一", "改写二"),
    ]
    assert rewriter.seen_queries == [COLLOQUIAL, "改写一"]  # 改写器仅被调用 2 次
    assert retriever.reranker.seen_queries == [COLLOQUIAL, "改写一", "改写二"]


def test_rewriter_decline_stops_early(tmp_path, qdrant):
    # LLM 判定无需/无法改写（原样返回）→ 提前收敛，不再重搜
    rewriter = FakeRewriter([None])
    retriever = _make_retriever(tmp_path, qdrant, lambda q, t: -1.0, rewriter=rewriter)

    result = retriever.retrieve(COLLOQUIAL)

    assert result.found is False
    assert result.error_code == NO_RELEVANT_DOCUMENT
    assert result.rewrites == []
    assert retriever.reranker.seen_queries == [COLLOQUIAL]  # 仅首搜一次


def test_rewrite_times_defaults_to_settings(tmp_path, qdrant):
    rewriter = FakeRewriter(["改写一", "改写二", "多余的输出"])
    retriever = _make_retriever(tmp_path, qdrant, lambda q, t: -1.0, rewriter=rewriter)

    assert retriever.max_rewrite_times == get_settings().query_rewrite_max_times == 2
    retriever.retrieve(COLLOQUIAL)
    assert len(rewriter.seen_queries) == 2  # 默认恰好 2 次改写调用
    assert rewriter.outputs == ["多余的输出"]  # 第 3 个输出未被消费


def test_no_rewriter_keeps_single_search(tmp_path, qdrant):
    # 未注入改写器：行为与 Prompt 9.3 一致，未命中直接返回
    retriever = _make_retriever(tmp_path, qdrant, lambda q, t: -1.0)

    result = retriever.retrieve(COLLOQUIAL)

    assert result.found is False
    assert result.error_code == NO_RELEVANT_DOCUMENT
    assert result.rewrites == []
    assert retriever.reranker.seen_queries == [COLLOQUIAL]


class FakeLLM:
    """记录收到的 prompt 并返回预设回复的可调用对象。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.seen_prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.seen_prompts.append(prompt)
        return self.reply


def test_query_rewriter_builds_prompt_and_strips_output():
    llm = FakeLLM(f'  "{NORMALIZED}"  \n')  # 模拟 LLM 带空白与引号的输出
    rewriter = QueryRewriter(llm)

    rewritten = rewriter.rewrite(COLLOQUIAL)

    assert rewritten == NORMALIZED
    assert COLLOQUIAL in llm.seen_prompts[0]  # prompt 携带用户问题


def test_query_rewriter_returns_none_when_no_rewrite():
    # 原样返回（已足够规范）或输出为空，均视为无需改写
    assert QueryRewriter(FakeLLM(COLLOQUIAL)).rewrite(COLLOQUIAL) is None
    assert QueryRewriter(FakeLLM("  \n")).rewrite(COLLOQUIAL) is None


class AsyncFakeLLM:
    """异步 LLM 可调用替身（事件循环内 arewrite 使用），记录收到的 prompt。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.seen_prompts: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.seen_prompts.append(prompt)
        return self.reply


async def test_arewrite_supports_async_llm_with_same_semantics():
    llm = AsyncFakeLLM(f'"{NORMALIZED}"')  # 模拟 LLM 带引号的异步输出
    rewriter = QueryRewriter(llm)

    assert await rewriter.arewrite(COLLOQUIAL) == NORMALIZED
    assert COLLOQUIAL in llm.seen_prompts[0]
    # 收敛语义与同步 rewrite 一致：原样返回 / 空输出 → None
    assert await QueryRewriter(AsyncFakeLLM(COLLOQUIAL)).arewrite(COLLOQUIAL) is None
    assert await QueryRewriter(AsyncFakeLLM("  \n")).arewrite(COLLOQUIAL) is None


async def test_arewrite_also_accepts_sync_llm():
    # arewrite 兼容同步可调用（事件循环内统一入口，无需区分注入形态）
    assert await QueryRewriter(FakeLLM(NORMALIZED)).arewrite(COLLOQUIAL) == NORMALIZED

"""检索链路测试：粗召回 → 重排 → 阈值过滤 → NO_RELEVANT_DOCUMENT。

Qdrant 内存模式 + FakeEmbedder（conftest）+ 可编程 FakeReranker，
不依赖外部服务与真实模型，打分逻辑由测试显式注入、可精确断言。
"""
import pytest
from qdrant_client import QdrantClient

from app.core.config import get_settings
from app.rag.ingest import ingest_documents
from app.rag.reranker import CrossEncoderReranker
from app.rag.retriever import NO_RELEVANT_DOCUMENT, KnowledgeRetriever
from app.rag.vector_store import KnowledgeVectorStore
from tests.conftest import FakeEmbedder


class FakeReranker:
    """按注入函数对 (query, text) 打分，并记录收到的候选供断言。"""

    def __init__(self, score_fn) -> None:
        self.score_fn = score_fn
        self.seen_query: str | None = None
        self.seen_texts: list[str] = []

    def score(self, query: str, texts: list[str]) -> list[float]:
        self.seen_query = query
        self.seen_texts = list(texts)
        return [float(self.score_fn(query, text)) for text in texts]


def _section_doc(sections: int) -> str:
    """生成含 n 个小节的 Markdown，每个小节恰产生一个分块。"""
    return "# 知识库手册\n\n" + "\n".join(
        f"## 第{i}节\n\n第{i}节介绍退货流程的环节{i}。\n" for i in range(1, sections + 1)
    )


def _make_retriever(tmp_path, qdrant, score_fn, *, recall=15, final=5, threshold=0.0):
    (tmp_path / "手册.md").write_text(_section_doc(10), encoding="utf-8")
    store = KnowledgeVectorStore(qdrant, collection_name="kb_rerank", dimension=8)
    report = ingest_documents(tmp_path, embedder=FakeEmbedder(8), store=store, max_chars=200)
    assert report.total_chunks == 10
    return KnowledgeRetriever(
        embedder=FakeEmbedder(8),
        store=store,
        reranker=FakeReranker(score_fn),
        recall_top_k=recall,
        final_top_k=final,
        score_threshold=threshold,
    )


@pytest.fixture
def qdrant():
    return QdrantClient(":memory:")


def test_retrieve_reranks_and_truncates_to_final_top_k(tmp_path, qdrant):
    retriever = _make_retriever(
        tmp_path, qdrant, lambda q, t: 0.9 if "第3节介绍" in t else 0.1
    )

    result = retriever.retrieve("退货流程")

    assert result.found is True
    assert result.error_code is None
    assert len(retriever.reranker.seen_texts) == 10  # 10 个分块全部进入候选池（recall=15）
    assert retriever.reranker.seen_query == "退货流程"
    assert len(result.chunks) == 5  # 重排后截取 Top-5
    scores = [item.score for item in result.chunks]
    assert scores == sorted(scores, reverse=True)  # 按相关性分降序
    assert result.chunks[0].chunk.content.startswith("## 第3节")  # 高分候选被顶到最前


def test_recall_caps_candidates_before_rerank(tmp_path, qdrant):
    retriever = _make_retriever(tmp_path, qdrant, lambda q, t: 0.5, recall=3, final=5)

    result = retriever.retrieve("退货流程")

    assert len(retriever.reranker.seen_texts) == 3  # 候选池最多 3 条
    assert len(result.chunks) == 3  # 阈值内候选不足 final_top_k 时按实际数量返回


def test_all_below_threshold_returns_no_relevant_document(tmp_path, qdrant):
    retriever = _make_retriever(tmp_path, qdrant, lambda q, t: -2.0, threshold=0.0)

    result = retriever.retrieve("今天天气怎么样")

    assert result.found is False
    assert result.error_code == NO_RELEVANT_DOCUMENT
    assert result.chunks == []  # 不硬凑结果


def test_threshold_filters_weak_candidates(tmp_path, qdrant):
    retriever = _make_retriever(
        tmp_path, qdrant, lambda q, t: 1.0 if "第1节介绍" in t else -1.0, threshold=0.0
    )

    result = retriever.retrieve("退货流程")

    assert result.found is True
    assert len(result.chunks) == 1  # 仅 1 条过阈值
    assert result.chunks[0].score == 1.0
    assert "第1节介绍" in result.chunks[0].chunk.content


def test_score_equal_to_threshold_is_kept(tmp_path, qdrant):
    retriever = _make_retriever(tmp_path, qdrant, lambda q, t: 2.0, threshold=2.0)

    result = retriever.retrieve("退货流程")

    assert result.found is True
    assert len(result.chunks) == 5  # score == threshold 视为相关（>= 判定）


def test_empty_collection_returns_no_relevant_document(qdrant):
    store = KnowledgeVectorStore(qdrant, collection_name="kb_empty", dimension=8)
    store.ensure_collection()
    retriever = KnowledgeRetriever(
        embedder=FakeEmbedder(8),
        store=store,
        reranker=FakeReranker(lambda q, t: 1.0),
        recall_top_k=15,
        final_top_k=5,
        score_threshold=0.0,
    )

    result = retriever.retrieve("任何问题")

    assert result.found is False
    assert result.error_code == NO_RELEVANT_DOCUMENT


def test_cross_encoder_reranker_delegates_to_model():
    class _StubModel:
        def predict(self, pairs, show_progress_bar=False):
            assert show_progress_bar is False
            return [len(query) + len(text) for query, text in pairs]

    reranker = CrossEncoderReranker(model_name="stub")
    reranker._model = _StubModel()

    assert reranker.score("问题", ["甲", "乙丙丁"]) == [3.0, 5.0]
    assert reranker.score("问题", []) == []  # 空候选直接短路，不触发模型


def test_reranker_model_name_defaults_to_settings():
    assert CrossEncoderReranker().model_name == get_settings().reranker_model

"""入库链路测试：幂等 / 维度变更重建 / 抽样向量维度断言。

使用 Qdrant 内存模式 + 确定性 FakeEmbedder（conftest），不依赖外部服务与真实模型；
维度对齐（Matryoshka 截断）逻辑另用纯单测覆盖。
"""
import pytest
from qdrant_client import QdrantClient

from app.rag.embedding import EmbeddingClient, EmbeddingError
from app.rag.ingest import ingest_documents
from app.rag.vector_store import KnowledgeVectorStore
from tests.conftest import FakeEmbedder

DOCS = {
    "退款政策.md": "# 退款政策\n\n> 适用范围说明。\n\n## 1. 退款条件\n\n质量问题全额退款。\n",
    "销售制度.md": (
        "# 销售制度\n\n## 1. 报价流程\n\n客户经理报价后下订单。\n\n## 2. 折扣审批\n\n5% 以上需总监审批。\n"
    ),
}


@pytest.fixture
def docs_dir(tmp_path):
    for name, content in DOCS.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    return tmp_path


@pytest.fixture
def qdrant():
    return QdrantClient(":memory:")


def _points(client: QdrantClient, collection: str):
    points, _ = client.scroll(collection, limit=100, with_vectors=True, with_payload=True)
    return points


def test_ingest_creates_collection_and_vectors_match_dimension(docs_dir, qdrant):
    store = KnowledgeVectorStore(qdrant, collection_name="kb_test", dimension=8)

    report = ingest_documents(docs_dir, embedder=FakeEmbedder(8), store=store, max_chars=200)

    assert report.collection_rebuilt is True
    assert report.total_chunks == 4  # 退款政策 2 块 + 销售制度 2 块（无正文的纯标题节跳过）
    assert qdrant.get_collection("kb_test").config.params.vectors.size == 8

    points = _points(qdrant, "kb_test")
    assert len(points) == 4
    # 抽样断言：向量维度与配置一致，payload 溯源字段齐全且自洽
    sample = points[0]
    assert len(sample.vector) == 8
    assert sample.payload["chunk_id"] == f"{sample.payload['document_id']}::c{sample.payload['index']:03d}"
    assert sample.payload["document_title"] in {"退款政策", "销售制度"}
    assert sample.payload["heading_path"] and sample.payload["content"]


def test_ingest_is_idempotent(docs_dir, qdrant):
    store = KnowledgeVectorStore(qdrant, collection_name="kb_test", dimension=8)

    first = ingest_documents(docs_dir, embedder=FakeEmbedder(8), store=store, max_chars=200)
    ids_first = {p.id for p in _points(qdrant, "kb_test")}

    second = ingest_documents(docs_dir, embedder=FakeEmbedder(8), store=store, max_chars=200)

    assert second.collection_rebuilt is False  # 维度未变 → 复用现有 Collection
    points = _points(qdrant, "kb_test")
    assert len(points) == first.total_chunks  # 不翻倍
    assert {p.id for p in points} == ids_first  # 点 ID 由 chunk_id 确定性生成


def test_dimension_change_rebuilds_collection(docs_dir, qdrant):
    old_store = KnowledgeVectorStore(qdrant, collection_name="kb_test", dimension=8)
    ingest_documents(docs_dir, embedder=FakeEmbedder(8), store=old_store, max_chars=200)

    # 配置维度 8 → 4：ensure_collection 检测不一致后删除重建
    new_store = KnowledgeVectorStore(qdrant, collection_name="kb_test", dimension=4)
    report = ingest_documents(docs_dir, embedder=FakeEmbedder(4), store=new_store, max_chars=200)

    assert report.collection_rebuilt is True
    assert qdrant.get_collection("kb_test").config.params.vectors.size == 4
    points = _points(qdrant, "kb_test")
    assert len(points) == report.total_chunks
    assert all(len(p.vector) == 4 for p in points)  # 重建后全部为新维度


def test_doc_change_removes_stale_chunks(docs_dir, qdrant):
    store = KnowledgeVectorStore(qdrant, collection_name="kb_test", dimension=8)
    ingest_documents(docs_dir, embedder=FakeEmbedder(8), store=store, max_chars=200)

    # 销售制度改为单一小节 → 旧的多余分块必须被清理
    (docs_dir / "销售制度.md").write_text(
        "# 销售制度\n\n仅保留一节。\n", encoding="utf-8"
    )
    report = ingest_documents(docs_dir, embedder=FakeEmbedder(8), store=store, max_chars=200)

    assert report.total_chunks == 3  # 退款政策 2 块 + 销售制度 1 块
    chunk_ids = {p.payload["chunk_id"] for p in _points(qdrant, "kb_test")}
    assert chunk_ids == {"退款政策::c000", "退款政策::c001", "销售制度::c000"}


def test_removed_doc_is_cleaned_up(docs_dir, qdrant):
    store = KnowledgeVectorStore(qdrant, collection_name="kb_test", dimension=8)
    ingest_documents(docs_dir, embedder=FakeEmbedder(8), store=store, max_chars=200)

    (docs_dir / "销售制度.md").unlink()
    report = ingest_documents(docs_dir, embedder=FakeEmbedder(8), store=store, max_chars=200)

    assert report.total_chunks == 2
    assert {p.payload["document_id"] for p in _points(qdrant, "kb_test")} == {"退款政策"}


def test_ingest_empty_dir_raises(tmp_path, qdrant):
    store = KnowledgeVectorStore(qdrant, collection_name="kb_test", dimension=8)

    with pytest.raises(FileNotFoundError):
        ingest_documents(tmp_path, embedder=FakeEmbedder(8), store=store, max_chars=200)


def test_fit_dimension_truncates_and_renormalizes():
    client = EmbeddingClient(dimension=4)
    fitted = client._fit_dimension([[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]])

    assert len(fitted[0]) == 4
    assert abs(sum(x * x for x in fitted[0]) - 1.0) < 1e-9  # 截断后重新归一化


def test_fit_dimension_rejects_dimension_larger_than_model():
    client = EmbeddingClient(dimension=16)

    with pytest.raises(EmbeddingError):
        client._fit_dimension([[1.0, 2.0, 3.0, 4.0]])

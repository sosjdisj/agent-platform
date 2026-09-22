"""知识文档向量化入库（幂等）：Markdown → 标题感知分块 → Embedding → Qdrant。

用法：python -m app.rag.ingest

幂等性保证：
- 点 ID 由 chunk_id 确定性生成（uuid5），重复执行 upsert 覆盖同一批点，不产生重复数据；
- 每次入库前按 document_id 删除旧向量并清理已移除文档的残留点，文档变更/删减均一致。
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from app.core.config import get_settings
from app.rag.chunker import DocumentChunk, chunk_markdown
from app.rag.embedding import EmbeddingClient
from app.rag.vector_store import KnowledgeVectorStore


class IngestReport(BaseModel):
    files: list[str]
    total_chunks: int
    dimension: int
    collection: str
    collection_rebuilt: bool


def ingest_documents(
    docs_dir: Path,
    *,
    embedder: EmbeddingClient,
    store: KnowledgeVectorStore,
    max_chars: int,
) -> IngestReport:
    """将目录下全部 Markdown 文档向量化入库（同步、可重复执行）。"""
    files = sorted(p for p in docs_dir.glob("*.md") if p.is_file())
    if not files:
        raise FileNotFoundError(f"目录下没有 Markdown 文档：{docs_dir}")

    chunks: list[DocumentChunk] = []
    for path in files:
        chunks.extend(
            chunk_markdown(path.read_text(encoding="utf-8"), document_id=path.stem, max_chars=max_chars)
        )

    vectors = embedder.embed_documents([c.content for c in chunks])
    rebuilt = store.ensure_collection()

    document_ids = sorted({c.document_id for c in chunks})
    for document_id in document_ids:
        store.delete_document(document_id)
    store.delete_stale_documents(document_ids)
    store.upsert_chunks(chunks, vectors)

    return IngestReport(
        files=[p.name for p in files],
        total_chunks=len(chunks),
        dimension=embedder.dimension,
        collection=store.collection_name,
        collection_rebuilt=rebuilt,
    )


def main() -> None:
    from qdrant_client import QdrantClient

    settings = get_settings()
    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=settings.healthcheck_timeout,
    )
    report = ingest_documents(
        settings.knowledge_docs_path,
        embedder=EmbeddingClient(),
        store=KnowledgeVectorStore(
            client,
            collection_name=settings.qdrant_collection_name,
            dimension=settings.embedding_dimension,
        ),
        max_chars=settings.chunk_max_chars,
    )
    print(f"入库完成：{report.model_dump_json(indent=2)}")


if __name__ == "__main__":
    main()

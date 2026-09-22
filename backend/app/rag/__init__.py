"""RAG 模块：Markdown 标题感知分块 / 本地 Embedding / Qdrant 向量库 / 重排检索 / 查询改写 / 入库脚本。"""
from app.rag.chunker import DocumentChunk, chunk_markdown
from app.rag.embedding import EmbeddingClient, EmbeddingError
from app.rag.query_rewriter import QueryRewriter
from app.rag.reranker import CrossEncoderReranker
from app.rag.retriever import (
    NO_RELEVANT_DOCUMENT,
    KnowledgeRetriever,
    QueryRewrite,
    RetrievedChunk,
    RetrievalResult,
)
from app.rag.vector_store import KnowledgeVectorStore, point_id

__all__ = [
    "DocumentChunk",
    "chunk_markdown",
    "EmbeddingClient",
    "EmbeddingError",
    "KnowledgeVectorStore",
    "point_id",
    "CrossEncoderReranker",
    "QueryRewriter",
    "KnowledgeRetriever",
    "RetrievedChunk",
    "RetrievalResult",
    "QueryRewrite",
    "NO_RELEVANT_DOCUMENT",
]

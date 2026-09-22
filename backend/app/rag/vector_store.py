"""知识向量库封装：按配置管理 Qdrant Collection 与分块写入（幂等）。"""
from __future__ import annotations

import uuid

from qdrant_client import QdrantClient, models

from app.rag.chunker import DocumentChunk

# 由 chunk_id 确定性生成 Qdrant 点 ID（uuid5），同一分块重复入库 ID 不变 → upsert 天然幂等
_POINT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "agent-platform/rag")


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, chunk_id))


class KnowledgeVectorStore:
    def __init__(self, client: QdrantClient, *, collection_name: str, dimension: int) -> None:
        self._client = client
        self.collection_name = collection_name
        self.dimension = dimension

    def ensure_collection(self) -> bool:
        """确保 Collection 存在且维度与配置一致；维度变更时删除重建。返回是否新建/重建。"""
        existing = {c.name for c in self._client.get_collections().collections}
        if self.collection_name in existing:
            if self._collection_dimension(self._client.get_collection(self.collection_name)) == self.dimension:
                return False
            self._client.delete_collection(self.collection_name)  # 维度变更 → 重建
        self._client.create_collection(
            self.collection_name,
            vectors_config=models.VectorParams(size=self.dimension, distance=models.Distance.COSINE),
        )
        return True

    def delete_document(self, document_id: str) -> None:
        """删除指定文档的全部向量（文档内容变化后避免新旧分块混存）。"""
        self._client.delete(
            self.collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_id", match=models.MatchValue(value=document_id)
                        )
                    ]
                )
            ),
            wait=True,
        )

    def delete_stale_documents(self, keep_document_ids: list[str]) -> None:
        """删除不属于当前文档集合的残留向量（文档被移除后同步清理）。"""
        if not keep_document_ids:
            return
        self._client.delete(
            self.collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must_not=[
                        models.FieldCondition(
                            key="document_id", match=models.MatchAny(any=keep_document_ids)
                        )
                    ]
                )
            ),
            wait=True,
        )

    def upsert_chunks(self, chunks: list[DocumentChunk], vectors: list[list[float]]) -> int:
        """按 chunk_id 确定性写入分块向量与 payload，返回写入条数。"""
        points = [
            models.PointStruct(
                id=point_id(chunk.chunk_id),
                vector=vector,
                payload=chunk.model_dump(),
            )
            for chunk, vector in zip(chunks, vectors)
        ]
        self._client.upsert(self.collection_name, points=points, wait=True)
        return len(points)

    def search(self, query_vector: list[float], *, limit: int) -> list[models.ScoredPoint]:
        """按查询向量召回最相近的 limit 条分块（含 payload 与相似度分），供重排使用。"""
        response = self._client.query_points(
            self.collection_name,
            query=query_vector,
            limit=limit,
            with_payload=True,
        )
        return list(response.points)

    def _collection_dimension(self, info) -> int:
        vectors = info.config.params.vectors
        if isinstance(vectors, models.VectorParams):
            return vectors.size
        return next(iter(vectors.values())).size  # 命名向量（当前未使用）取第一个

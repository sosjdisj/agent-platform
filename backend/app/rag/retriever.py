"""知识检索：Top-N 向量粗召回 → CrossEncoder 重排 → 阈值过滤 → Top-K。

- 粗召回条数 / 返回条数 / 相关性阈值 / 改写次数全部来自配置（RERANK_* / QUERY_REWRITE_*），禁止硬编码；
- 首搜未命中时由注入的 QueryRewriter 改写查询后重搜，全程最多 max_rewrite_times 次，
  改写决策记录为 RetrievalResult.rewrites 结构化元数据（非 CoT）；
- 重搜后若仍全部候选低于阈值（或库中无可召回内容），返回 found=False + NO_RELEVANT_DOCUMENT，
  不硬凑结果，由调用方决定兜底话术。
"""
from __future__ import annotations

from pydantic import BaseModel

from app.core.config import get_settings
from app.rag.chunker import DocumentChunk
from app.rag.reranker import CrossEncoderReranker
from app.rag.vector_store import KnowledgeVectorStore

NO_RELEVANT_DOCUMENT = "NO_RELEVANT_DOCUMENT"


class RetrievedChunk(BaseModel):
    chunk: DocumentChunk
    score: float  # CrossEncoder 相关性分（越高越相关）


class QueryRewrite(BaseModel):
    """一次查询改写决策的元数据。"""

    attempt: int  # 第几次改写（从 1 起）
    from_query: str  # 改写前查询
    to_query: str  # 改写后查询


class RetrievalResult(BaseModel):
    found: bool
    chunks: list[RetrievedChunk] = []
    error_code: str | None = None  # found=False 时为 NO_RELEVANT_DOCUMENT
    rewrites: list[QueryRewrite] = []  # 改写链路元数据，供调用方追溯


class KnowledgeRetriever:
    def __init__(
        self,
        *,
        embedder,
        store: KnowledgeVectorStore,
        reranker: CrossEncoderReranker | None = None,
        rewriter=None,  # QueryRewriter | None，未注入则不做改写重搜
        recall_top_k: int | None = None,
        final_top_k: int | None = None,
        score_threshold: float | None = None,
        max_rewrite_times: int | None = None,
    ) -> None:
        settings = get_settings()
        self.embedder = embedder
        self.store = store
        self.reranker = reranker or CrossEncoderReranker()
        self.rewriter = rewriter
        self.recall_top_k = settings.rerank_recall_top_k if recall_top_k is None else recall_top_k
        self.final_top_k = settings.rerank_final_top_k if final_top_k is None else final_top_k
        self.score_threshold = (
            settings.rerank_score_threshold if score_threshold is None else score_threshold
        )
        self.max_rewrite_times = (
            settings.query_rewrite_max_times if max_rewrite_times is None else max_rewrite_times
        )

    def retrieve(self, query: str) -> RetrievalResult:
        """检索：粗召回 → 重排 → 阈值过滤；未命中时改写查询重搜（最多 max_rewrite_times 次）。"""
        result = self._search_once(query)
        if result.found or self.rewriter is None:
            return result

        rewrites: list[QueryRewrite] = []
        current_query = query
        for attempt in range(1, self.max_rewrite_times + 1):
            rewritten = self.rewriter.rewrite(current_query)
            if rewritten is None:  # LLM 判定无需/无法改写，提前收敛
                break
            rewrites.append(
                QueryRewrite(attempt=attempt, from_query=current_query, to_query=rewritten)
            )
            current_query = rewritten
            result = self._search_once(current_query)
            if result.found:
                break

        result.rewrites = rewrites
        return result

    def _search_once(self, query: str) -> RetrievalResult:
        """单次检索：粗召回 → 重排 → 阈值过滤 → 截取 Top-K。"""
        hits = self.store.search(self.embedder.embed_query(query), limit=self.recall_top_k)
        if not hits:
            return RetrievalResult(found=False, error_code=NO_RELEVANT_DOCUMENT)

        scores = self.reranker.score(query, [hit.payload["content"] for hit in hits])
        ranked = sorted(zip(hits, scores), key=lambda pair: pair[1], reverse=True)
        kept = [
            RetrievedChunk(chunk=DocumentChunk.model_validate(hit.payload), score=float(score))
            for hit, score in ranked
            if score >= self.score_threshold
        ][: self.final_top_k]
        if not kept:
            return RetrievalResult(found=False, error_code=NO_RELEVANT_DOCUMENT)
        return RetrievalResult(found=True, chunks=kept)

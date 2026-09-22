"""search_knowledge 工具：知识库语义检索（包装 KnowledgeRetriever，见 app/rag/retriever.py）。

- 检索未命中（found=False + NO_RELEVANT_DOCUMENT）是业务结果而非工具失败：
  以成功结构返回，Agent 据此如实兜底作答，不进入错误/失败链路；
- 纯检索模式（默认，全局工具面）：单次检索，不注入 judge / rewriter；
- 充分性闭环模式（KnowledgeAgent 注入 judge + rewriter）：LLM 判断检索结果是否足以回答
  问题，不足则经 QueryRewriter 改写查询重搜，全程最多 max_rewrite_times 次（Prompt 9.4
  配置 query_rewrite_max_times）；改写链路与最终判定随结果返回（rewrites / sufficient）。
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.mcp.base import BaseTool, RiskLevel
from app.rag.query_rewriter import QueryRewriter
from app.rag.retriever import KnowledgeRetriever, QueryRewrite

# 充分性评审：输入（原始问题, 当前检索输出）→ 是否足以作答；评审自身故障不应阻断流程
SufficiencyJudge = Callable[[str, "SearchKnowledgeOutput"], Awaitable[bool]]


class SearchKnowledgeInput(BaseModel):
    query: str = Field(min_length=1, description="检索查询文本")


class KnowledgeChunkOut(BaseModel):
    chunk_id: str
    document_id: str
    document_title: str
    content: str
    score: float = Field(description="CrossEncoder 相关性分（越高越相关）")


class SearchKnowledgeOutput(BaseModel):
    found: bool
    chunks: list[KnowledgeChunkOut] = []
    error_code: str | None = None  # found=False 时为 NO_RELEVANT_DOCUMENT
    rewrites: list[QueryRewrite] = []  # 改写重搜链路元数据（纯检索模式恒为空）
    sufficient: bool | None = None  # 充分性判定：None=未启用闭环；False=判定不足以作答


def build_default_retriever() -> KnowledgeRetriever:
    """默认检索器：真实 RAG 栈（Embedding / 重排模型延迟加载，构造零成本）。

    不注入 rewriter——保持单次检索语义；改写重搜由注入本工具的闭环编排（judge + rewriter）负责。
    """
    from qdrant_client import QdrantClient

    from app.rag.embedding import EmbeddingClient
    from app.rag.vector_store import KnowledgeVectorStore

    settings = get_settings()
    client = QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout=settings.healthcheck_timeout,
    )
    store = KnowledgeVectorStore(
        client,
        collection_name=settings.qdrant_collection_name,
        dimension=settings.embedding_dimension,
    )
    return KnowledgeRetriever(embedder=EmbeddingClient(), store=store)


class SearchKnowledgeTool(BaseTool[SearchKnowledgeInput, SearchKnowledgeOutput]):
    name = "search_knowledge"
    required_permission = "knowledge:search"
    description = (
        "知识库语义检索：按查询文本检索内部知识文档（如售后 / 退货政策等），"
        "返回最相关的知识分块（chunk_id / 文档标题 / 正文 / 相关性分）；"
        "found=False 表示没有相关内容（error_code=NO_RELEVANT_DOCUMENT），"
        "此时应如实告知用户未检索到，不要编造答案"
    )
    risk_level = RiskLevel.SAFE
    timeout = 15.0  # 检索含向量召回与重排打分，宽于常规查询工具
    InputModel = SearchKnowledgeInput
    OutputModel = SearchKnowledgeOutput

    def __init__(
        self,
        retriever: KnowledgeRetriever | None = None,
        *,
        judge: SufficiencyJudge | None = None,
        rewriter: QueryRewriter | None = None,
        max_rewrite_times: int | None = None,
    ) -> None:
        # judge / rewriter 必须成对注入：评审决定是否重搜，改写器执行重搜查询
        if (judge is None) != (rewriter is None):
            raise ValueError("judge 与 rewriter 必须同时注入（充分性闭环）或同时缺省（纯检索）")
        self._retriever = retriever if retriever is not None else build_default_retriever()
        self._judge = judge
        self._rewriter = rewriter
        self._max_rewrite_times = (
            get_settings().query_rewrite_max_times if max_rewrite_times is None else max_rewrite_times
        )

    async def run(self, params: SearchKnowledgeInput) -> SearchKnowledgeOutput:
        # 检索为同步阻塞实现（Qdrant + 重排模型），放线程池避免阻塞事件循环
        output = await self._search_once(params.query)
        if self._judge is None:
            return output

        # 充分性闭环：评审 → 不足则改写重搜 → 再评审，全程最多 max_rewrite_times 次
        # 评审始终对照原始问题（改写只优化检索词，不改变信息需求）
        original_query, current_query = params.query, params.query
        rewrites: list[QueryRewrite] = []
        while True:
            sufficient = await self._judge(original_query, output) if output.found else False
            if sufficient or len(rewrites) >= self._max_rewrite_times:
                break
            rewritten = await self._rewriter.arewrite(current_query)
            if rewritten is None:  # 改写器判定无需/无法改写，提前收敛
                break
            rewrites.append(
                QueryRewrite(attempt=len(rewrites) + 1, from_query=current_query, to_query=rewritten)
            )
            current_query = rewritten
            output = await self._search_once(current_query)

        output.sufficient = sufficient
        output.rewrites = rewrites
        return output

    async def _search_once(self, query: str) -> SearchKnowledgeOutput:
        """单次检索 → 统一输出结构；未命中仍为成功（业务结果非失败）。"""
        result = await asyncio.to_thread(self._retriever.retrieve, query)
        return SearchKnowledgeOutput(
            found=result.found,
            chunks=[
                KnowledgeChunkOut(
                    chunk_id=item.chunk.chunk_id,
                    document_id=item.chunk.document_id,
                    document_title=item.chunk.document_title,
                    content=item.chunk.content,
                    score=item.score,
                )
                for item in result.chunks
            ],
            error_code=result.error_code,
        )

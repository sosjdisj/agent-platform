"""本地 Embedding 客户端：加载魔搭社区模型（默认 google/embeddinggemma-300m）。

- 模型 ID / 维度 / batch 全部来自配置（EMBEDDING_MODEL / EMBEDDING_DIMENSION 等），禁止硬编码；
- 模型与 torch 延迟加载：首次向量化时才从魔搭解析/下载模型，进程内复用；
- 配置维度小于模型输出维度时按 Matryoshka 截断（取前 N 维）并重新归一化，
  大于时直接报错（fail fast），避免静默写入错误维度的向量。
"""
from __future__ import annotations

import numpy as np

from app.core.config import get_settings


class EmbeddingError(RuntimeError):
    """Embedding 配置或执行错误。"""


class EmbeddingClient:
    def __init__(
        self,
        *,
        model_name: str | None = None,
        dimension: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        settings = get_settings()
        self.model_name = model_name or settings.embedding_model
        self.dimension = dimension or settings.embedding_dimension
        self.batch_size = batch_size or settings.embedding_batch_size
        self._model = None  # 延迟加载

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """文档分块批量向量化。"""
        if not texts:
            return []
        vectors = self._get_model().encode(
            texts, batch_size=self.batch_size, show_progress_bar=False
        )
        return self._fit_dimension(vectors)

    def embed_query(self, text: str) -> list[float]:
        """查询向量化：若模型自带 query 提示词配置则使用（如 embeddinggemma）。"""
        model = self._get_model()
        kwargs = {"prompt_name": "query"} if "query" in getattr(model, "prompts", {}) else {}
        vector = model.encode(text, show_progress_bar=False, **kwargs)
        return self._fit_dimension([vector])[0]

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # 延迟导入，避免无关场景加载 torch

            self._model = SentenceTransformer(self._resolve_model_path())
        return self._model

    def _resolve_model_path(self) -> str:
        """从魔搭社区解析模型 ID 到本地缓存路径（首次自动下载，之后命中缓存）。"""
        from modelscope import snapshot_download

        return snapshot_download(
            self.model_name,
            # 排除 onnx / openvino 等推理用不到的大文件，只取 PyTorch 权重与配置
            allow_patterns=["*.json", "*.txt", "*.md", "*.py", "*.safetensors"],
        )

    def _fit_dimension(self, vectors: np.ndarray) -> list[list[float]]:
        """对齐配置维度：截断为前 N 维（Matryoshka）并重新归一化。"""
        arr = np.asarray(vectors, dtype=float)
        if arr.ndim != 2 or arr.shape[1] < self.dimension:
            raise EmbeddingError(
                f"配置维度 EMBEDDING_DIMENSION={self.dimension} 大于模型输出维度 "
                f"{arr.shape[1] if arr.ndim == 2 else arr.ndim}，请修正配置"
            )
        if arr.shape[1] > self.dimension:
            arr = arr[:, : self.dimension]
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            arr = arr / norms
        return arr.tolist()

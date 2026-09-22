"""本地 Cross-Encoder 重排器：加载魔搭社区模型（默认 BAAI/bge-reranker-base）。

- 模型 ID 来自配置（RERANKER_MODEL），禁止硬编码；
- 模型与 torch 延迟加载：首次打分时才从魔搭解析/下载模型，进程内复用；
- 该模型 num_labels=1 且无自定义激活配置，sentence-transformers 默认应用 Sigmoid，
  输出为 [0, 1] 相关性概率；阈值语义由调用方（retriever）按配置决定。
"""
from __future__ import annotations

from app.core.config import get_settings


class CrossEncoderReranker:
    def __init__(self, *, model_name: str | None = None) -> None:
        self.model_name = model_name or get_settings().reranker_model
        self._model = None  # 延迟加载

    def score(self, query: str, texts: list[str]) -> list[float]:
        """对 (query, text) 对逐条打相关性分，返回与 texts 等长的分数列表（越高越相关）。"""
        if not texts:
            return []
        model = self._get_model()
        pairs = [(query, text) for text in texts]
        return [float(s) for s in model.predict(pairs, show_progress_bar=False)]

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder  # 延迟导入，避免无关场景加载 torch

            self._model = CrossEncoder(self._resolve_model_path())
        return self._model

    def _resolve_model_path(self) -> str:
        """从魔搭社区解析模型 ID 到本地缓存路径（首次自动下载，之后命中缓存）。"""
        from modelscope import snapshot_download

        return snapshot_download(
            self.model_name,
            # 只取 safetensors 权重与分词器/配置，跳过 pytorch_model.bin 与 onnx 重复权重；
            # *.model 为 sentencepiece 分词器文件（XLM-R 系必需）
            allow_patterns=["*.json", "*.py", "*.txt", "*.md", "*.safetensors", "*.model"],
        )

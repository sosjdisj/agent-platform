"""知识文档元数据表模型（Agentic RAG 溯源）。

仅存文档元数据与溯源信息，向量本体存于 Qdrant：
- source_path: 文档原始文件路径 / 来源地址
- document_id: 外部系统（向量库、知识库）中的文档标识，用于反查溯源
"""
from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import StatusMixin, TimestampMixin


class KnowledgeDocument(Base, TimestampMixin, StatusMixin):
    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    document_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))

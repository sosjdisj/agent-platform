"""知识文档 Repository。"""
from app.models.knowledge_document import KnowledgeDocument

from app.repositories.base import BaseRepository


class KnowledgeDocumentRepository(BaseRepository[KnowledgeDocument]):
    model = KnowledgeDocument

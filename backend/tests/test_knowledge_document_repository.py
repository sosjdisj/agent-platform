"""KnowledgeDocumentRepository 单元测试：覆盖基础 CRUD 与溯源字段。"""
from app.repositories.knowledge_document import KnowledgeDocumentRepository


async def test_knowledge_document_repository_crud(db_session):
    repo = KnowledgeDocumentRepository(db_session)

    # create（含溯源字段）
    doc = await repo.create(
        title="销售制度手册",
        source_path="docs/sales/sales-policy-2026.pdf",
        document_id="kb-doc-0001",
        file_type="pdf",
        description="2026 年度销售政策与制度",
    )
    assert doc.id is not None
    assert doc.source_path == "docs/sales/sales-policy-2026.pdf"
    assert doc.document_id == "kb-doc-0001"

    # get
    fetched = await repo.get(doc.id)
    assert fetched is not None
    assert fetched.title == "销售制度手册"
    assert fetched.file_type == "pdf"

    # update
    await repo.update(doc, status="archived")
    fetched = await repo.get(doc.id)
    assert fetched.status == "archived"

    # list
    assert len(await repo.list()) == 1

    # delete
    await repo.delete(doc)
    assert await repo.get(doc.id) is None

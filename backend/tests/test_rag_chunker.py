"""Markdown 标题感知分块器单测。"""
from app.rag.chunker import chunk_markdown

DOC = """# 退款政策

> 适用范围说明。

## 1. 退款条件

质量问题的全额退款。

## 2. 退款流程

- 提交申请
- 受理初审

### 2.1 审批

5 万元以上需会签。
"""


def test_headings_define_chunks_and_paths():
    chunks = chunk_markdown(DOC, document_id="refund-policy")

    # H1 前言 + 3 个有正文的标题节（### 2.1 审批 归入独立分块）
    assert len(chunks) == 4
    assert [c.index for c in chunks] == [0, 1, 2, 3]
    assert chunks[0].heading_path == ["退款政策"]
    assert chunks[2].heading_path == ["退款政策", "2. 退款流程"]
    assert chunks[3].heading_path == ["退款政策", "2. 退款流程", "2.1 审批"]
    # 全部携带文档主标题与 document_id
    assert all(c.document_title == "退款政策" for c in chunks)
    assert all(c.document_id == "refund-policy" for c in chunks)


def test_chunk_id_deterministic():
    first = chunk_markdown(DOC, document_id="refund-policy")
    second = chunk_markdown(DOC, document_id="refund-policy")

    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert first[0].chunk_id == "refund-policy::c000"
    assert first[3].chunk_id == "refund-policy::c003"


def test_long_section_split_repeats_heading():
    doc = "## 1. 退款条件\n\n质量问题的全额退款。\n\n交付延误的协商退款。\n\n包装完好可部分退款。\n"
    heading_pad = len("## 1. 退款条件\n\n")

    chunks = chunk_markdown(doc, document_id="d", max_chars=12)

    # 预算 12 装不下两段 → 三段各成一片，且每片重复标题行（正文预算 ≤ max_chars）
    assert len(chunks) == 3
    assert all(c.content.startswith("## 1. 退款条件") for c in chunks)
    assert all(len(c.content) <= 12 + heading_pad for c in chunks)


def test_long_section_greedy_packs_paragraphs_within_budget():
    doc = "## 1. 退款条件\n\n质量问题的全额退款。\n\n交付延误的协商退款。\n\n包装完好可部分退款。\n"
    heading_pad = len("## 1. 退款条件\n\n")

    chunks = chunk_markdown(doc, document_id="d", max_chars=30)

    # 贪心打包：能装进预算的段落合并成片
    assert len(chunks) == 2
    assert all(len(c.content) <= 30 + heading_pad for c in chunks)
    assert "质量问题的全额退款。" in chunks[0].content
    assert "包装完好可部分退款。" in chunks[1].content


def test_hash_inside_code_fence_is_not_heading():
    doc = "# 配置示例\n\n```bash\n# 这不是标题\nexport A=1\n```\n\n## 使用说明\n\n按需修改。\n"
    chunks = chunk_markdown(doc, document_id="d")

    assert len(chunks) == 2
    assert chunks[0].heading_path == ["配置示例"]
    assert "# 这不是标题" in chunks[0].content


def test_title_without_body_merges_into_next_section():
    doc = "# 主标题\n\n## 第一节\n\n正文内容。\n"
    chunks = chunk_markdown(doc, document_id="d")

    # langchain 行为：无正文的 H1 并入下一个有正文的节，标题上下文不丢失
    assert len(chunks) == 1
    assert chunks[0].heading_path == ["主标题", "第一节"]
    assert chunks[0].document_title == "主标题"
    assert "# 主标题" in chunks[0].content and "正文内容。" in chunks[0].content


def test_body_without_any_heading_uses_document_id_as_title():
    chunks = chunk_markdown("只有一段没有标题的正文。", document_id="bare")

    assert len(chunks) == 1
    assert chunks[0].heading_path == []
    assert chunks[0].document_title == "bare"
    assert chunks[0].chunk_id == "bare::c000"

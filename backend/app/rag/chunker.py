"""Markdown 标题感知分块器：基于 langchain-text-splitters。

- MarkdownHeaderTextSplitter 按标题切分，元数据携带完整标题层级链（父标题保持），
  无正文的标题节自动并入下一个有正文的节（标题上下文不丢失）；
- 正文超过 max_chars 时用 RecursiveCharacterTextSplitter 贪心二次切分，
  每片重复标题块保持语义自包含；
- chunk_id 全局确定："{document_id}::c{序号}"，同一文档重复分块结果一致（幂等入库的基础）。
"""
from __future__ import annotations

import re

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from pydantic import BaseModel, Field

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_HEADER_KEYS = ("h1", "h2", "h3", "h4", "h5", "h6")


class DocumentChunk(BaseModel):
    chunk_id: str
    document_id: str
    document_title: str
    heading_path: list[str]
    content: str
    index: int = Field(ge=0)


def chunk_markdown(
    text: str,
    *,
    document_id: str,
    max_chars: int = 500,
) -> list[DocumentChunk]:
    """将 Markdown 文本按标题层级分块。"""
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#" * level, f"h{level}") for level in range(1, 7)],
        strip_headers=False,
    )
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=max_chars, chunk_overlap=0)

    title = document_id
    title_seen = False
    chunks: list[tuple[list[str], str]] = []
    for section in header_splitter.split_text(text):
        heading_path = [section.metadata[key] for key in _HEADER_KEYS if key in section.metadata]
        if not title_seen and "h1" in section.metadata:
            title, title_seen = section.metadata["h1"], True  # 首个一级标题作为文档主标题

        lines = _normalize(section.page_content).split("\n")
        offset = 0
        while offset < len(lines) and _HEADING_RE.match(lines[offset]):
            offset += 1  # 节首连续标题行（空正文节并入时会出现多行）
        heading_block = "\n".join(lines[:offset])
        body = "\n".join(lines[offset:]).strip()
        if not body:
            continue  # 纯标题、无正文的节不产生分块
        for piece in text_splitter.split_text(body):
            content = f"{heading_block}\n\n{piece}" if heading_block else piece
            chunks.append((heading_path, content))

    return [
        DocumentChunk(
            chunk_id=f"{document_id}::c{index:03d}",
            document_id=document_id,
            document_title=title,
            heading_path=path,
            content=content,
            index=index,
        )
        for index, (path, content) in enumerate(chunks)
    ]


def _normalize(content: str) -> str:
    """清理 langchain 切分器添加的行尾换行标记（两个空格），还原紧凑文本。"""
    return "\n".join(line.rstrip() for line in content.splitlines())

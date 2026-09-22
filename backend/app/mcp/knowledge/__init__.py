"""knowledge 子模块：知识检索类工具（RAG 相关）。"""

from app.mcp.knowledge.search_knowledge import SearchKnowledgeTool
from app.mcp.registry import ToolRegistry


def register_tools(registry: ToolRegistry) -> None:
    """登记本子模块全部工具。"""
    registry.register(SearchKnowledgeTool())

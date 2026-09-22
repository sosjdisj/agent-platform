"""MCP 工具注册聚合：hello 链路验证工具 + 三个业务子模块。

各业务子模块（database / business / knowledge）各自暴露 register_tools，
新 Prompt 接入工具时只改对应子模块，无需改动此处。
"""
from app.mcp.business import register_tools as register_business_tools
from app.mcp.database import register_tools as register_database_tools
from app.mcp.knowledge import register_tools as register_knowledge_tools
from app.mcp.registry import ToolRegistry
from app.mcp.tools.hello import HelloTool


def register_default_tools(registry: ToolRegistry) -> None:
    """注册当前阶段全部工具。"""
    registry.register(HelloTool())
    register_database_tools(registry)
    register_business_tools(registry)
    register_knowledge_tools(registry)

"""FastMCP Server 构建与进程内单例（集成方式见 main.py 与 README）。"""
from __future__ import annotations

from functools import lru_cache

from fastmcp import FastMCP
from starlette.applications import Starlette

from app.mcp.registry import get_tool_registry
from app.mcp.tools import register_default_tools

# MCP 挂载点：main.py 以 app.mount(MCP_MOUNT_PATH) 挂载，最终访问 /mcp
MCP_MOUNT_PATH = "/mcp"


@lru_cache
def get_mcp_server() -> FastMCP:
    """进程内唯一 FastMCP Server：首次调用注册全部工具，之后复用。"""
    registry = get_tool_registry()
    register_default_tools(registry)
    mcp = FastMCP(
        name="agent-platform-tools",
        instructions="客户智能分析平台底层业务工具集（database / business / knowledge 按阶段接入）",
    )
    registry.mount_to(mcp)
    return mcp


def build_mcp_asgi_app() -> Starlette:
    """构建可挂载进 FastAPI 的 ASGI 应用（streamable HTTP 传输）。

    注意：FastAPI 不会自动运行挂载子应用的 lifespan，
    session manager 需由宿主在 lifespan 中启动（见 main.py）。
    """
    # 子应用内部路径为 "/"，挂载到 MCP_MOUNT_PATH 后最终端点即 /mcp
    return get_mcp_server().http_app(path="/")

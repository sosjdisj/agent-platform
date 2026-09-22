"""统一 MCP 工具调用客户端：Agent 层访问工具的唯一入口。

- in-memory 传输（与 FastMCP Server 同进程），无网络开销；
- 任何失败模式（未注册 / 参数校验失败 / 超时 / 异常）都归一化为统一错误结构，
  调用方永远拿到 ToolResult，无需捕获异常；工具自身的业务错误原样透传。
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from app.mcp.base import ToolResult
from app.mcp.registry import ToolRegistry, get_tool_registry
from app.mcp.server import get_mcp_server

# 客户端侧总预算（秒）：工具自身超时由元数据约束，此处兜底防止 Agent 无限等待
DEFAULT_CALL_TIMEOUT = 30.0


class MCPClient:
    """异步工具调用客户端：超时与错误统一归一化，不向调用方抛异常。"""

    def __init__(
        self,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
        *,
        server: FastMCP | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        """默认使用全局单例 Server 与注册表；测试可注入独立实例。"""
        self._call_timeout = call_timeout
        self._server = server if server is not None else get_mcp_server()
        self._registry = registry if registry is not None else get_tool_registry()

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """调用工具并返回统一结构；任何失败都归一化为 ToolResult。"""
        if not self._registry.has(name):
            return ToolResult.fail("TOOL_NOT_FOUND", f"工具未注册: {name}")

        # 会话在独立协程内开启：客户端超时取消时，取消沿任务组正常传播
        async def _invoke() -> Any:
            async with Client(self._server) as client:
                return await client.call_tool(name, arguments)

        try:
            result = await asyncio.wait_for(_invoke(), timeout=self._call_timeout)
        except asyncio.TimeoutError:
            return ToolResult.fail(
                "TOOL_TIMEOUT", f"工具 {name} 调用超时（>{self._call_timeout}s）"
            )
        except ToolError as exc:
            # 协议层错误：fastmcp 在进入工具前拦截（如参数不符合 schema）
            return ToolResult.fail("TOOL_CALL_FAILED", str(exc))
        except Exception as exc:  # 边界兜底：未知异常也不破坏统一结构
            return ToolResult.fail("TOOL_INTERNAL", f"工具 {name} 调用异常: {exc}")

        content = result.structured_content
        if not isinstance(content, dict) or "success" not in content:
            return ToolResult.fail("TOOL_INTERNAL", f"工具 {name} 返回不符合统一结构规范")
        return ToolResult.model_validate(content)

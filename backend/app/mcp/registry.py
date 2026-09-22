"""工具注册表：集中登记工具实例，提供查询与 FastMCP 挂载。"""
from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP

from app.mcp.base import BaseTool


def _flat_entrypoint(tool: BaseTool) -> Callable[..., Any]:
    """生成扁平签名的 MCP 入口函数，使入参 schema 与元数据 input_schema 一致。

    fastmcp 对「单个 Pydantic 模型参数」会包装成嵌套对象（{"params": {...}}），
    这里按 InputModel 字段展开为独立关键字参数，客户端即可直接传扁平字段。
    """
    parameters = [
        inspect.Parameter(
            field_name,
            kind=inspect.Parameter.KEYWORD_ONLY,
            annotation=field.annotation,
            default=(
                inspect.Parameter.empty
                if field.is_required()
                else field.get_default(call_default_factory=True)
            ),
        )
        for field_name, field in tool.InputModel.model_fields.items()
    ]

    async def entrypoint(**kwargs: Any) -> dict[str, Any]:
        return await tool.execute(kwargs)

    # fastmcp 通过 inspect.signature 读取参数 schema，显式替换为扁平签名；
    # pydantic 校验走 get_type_hints，需同步 __annotations__
    annotations: dict[str, Any] = {
        field_name: field.annotation for field_name, field in tool.InputModel.model_fields.items()
    }
    annotations["return"] = dict[str, Any]
    entrypoint.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    entrypoint.__annotations__ = annotations
    return entrypoint


class ToolRegistry:
    """工具注册表：名称唯一，供元数据查询与 FastMCP Server 构建使用。"""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, *tools: BaseTool) -> None:
        """登记工具实例；同名同类型幂等跳过（Server 可能重复构建），同名异类型为配置错误。"""
        for tool in tools:
            existing = self._tools.get(tool.name)
            if existing is not None:
                if type(existing) is type(tool):
                    continue
                raise ValueError(f"工具重名冲突: {tool.name}")
            self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"工具未注册: {name}")
        return self._tools[name]

    def has(self, name: str) -> bool:
        """判断工具是否已注册（MCPClient 归一化 TOOL_NOT_FOUND 的依据）。"""
        return name in self._tools

    def all_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def mount_to(self, mcp: FastMCP) -> None:
        """把全部工具注册到 FastMCP Server（execute 即工具入口）。"""
        for tool in self._tools.values():
            mcp.tool(
                _flat_entrypoint(tool),
                name=tool.name,
                description=tool.description,
            )


# 全局单例：注册表在进程内共享（server.py 构建时登记全部工具）
_registry = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    """返回全局工具注册表单例。"""
    return _registry

"""MCP Client 调通 hello 工具（in-memory 传输，验证 注册 → 调用 → 返回结构 全链路）。"""
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from app.mcp.server import get_mcp_server


async def test_mcp_client_calls_hello():
    async with Client(get_mcp_server()) as client:
        result = await client.call_tool("hello", {"name": "华信智造"})
        assert result.structured_content["success"] is True
        assert result.structured_content["data"] == {"greeting": "你好，华信智造！"}


async def test_mcp_client_lists_hello_with_flat_schema():
    """工具入参 schema 为扁平结构（与元数据 input_schema 一致，非嵌套 params 包裹）。"""
    async with Client(get_mcp_server()) as client:
        tools = await client.list_tools()
        hello = next(t for t in tools if t.name == "hello")
        assert hello.description == "返回一句问候语，用于验证 MCP 工具链路是否可用"
        assert set(hello.input_schema["properties"]) == {"name"}


async def test_mcp_client_unknown_tool_raises():
    async with Client(get_mcp_server()) as client:
        with pytest.raises(ToolError):
            await client.call_tool("no_such_tool", {})


async def test_mcp_client_missing_required_param_raises():
    async with Client(get_mcp_server()) as client:
        with pytest.raises(ToolError):
            await client.call_tool("hello", {})

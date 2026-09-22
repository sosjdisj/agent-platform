"""MCPClient 统一封装测试：错误归一化 + 客户端超时（Prompt 6.2）。"""
import asyncio

from fastmcp import FastMCP
from pydantic import BaseModel

from app.mcp.base import BaseTool, ToolError, ToolResult
from app.mcp.client import MCPClient
from app.mcp.registry import ToolRegistry
from app.mcp.tools.hello import HelloTool


class _EchoInput(BaseModel):
    text: str


class _EchoOutput(BaseModel):
    echo: str


class _EchoTool(BaseTool[_EchoInput, _EchoOutput]):
    """正常工具：原样返回输入。"""

    name = "echo"
    description = "原样返回输入"
    InputModel = _EchoInput
    OutputModel = _EchoOutput

    async def run(self, params: _EchoInput) -> _EchoOutput:
        return _EchoOutput(echo=params.text)


class _BoomTool(BaseTool[_EchoInput, _EchoOutput]):
    """业务错误工具：验证 MCPClient 对业务错误原样透传。"""

    name = "boom"
    description = "业务错误测试"
    InputModel = _EchoInput
    OutputModel = _EchoOutput

    async def run(self, params: _EchoInput) -> _EchoOutput:
        raise ToolError("CUSTOM_BIZ_ERROR", "业务失败")


class _SlowTool(BaseTool[_EchoInput, _EchoOutput]):
    """慢工具：自身超时 10s，客户端预算小于其执行时间，客户端超时先触发。"""

    name = "slow"
    description = "客户端超时测试"
    InputModel = _EchoInput
    OutputModel = _EchoOutput

    async def run(self, params: _EchoInput) -> _EchoOutput:
        await asyncio.sleep(0.5)
        return _EchoOutput(echo=params.text)


def _build_local_client(
    *tools: BaseTool, call_timeout: float = 30.0
) -> MCPClient:
    """用测试专用工具构建独立 registry + FastMCP Server（与生产同一注册链路）。"""
    registry = ToolRegistry()
    registry.register(*tools)
    mcp = FastMCP("test-tools")
    registry.mount_to(mcp)
    return MCPClient(call_timeout=call_timeout, server=mcp, registry=registry)


async def test_client_success_call():
    client = _build_local_client(_EchoTool())
    result = await client.call("echo", {"text": "你好"})
    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.data == {"echo": "你好"}


async def test_client_business_error_passthrough():
    """工具自身的业务错误结构原样透传，不与客户端错误码混淆。"""
    client = _build_local_client(_BoomTool())
    result = await client.call("boom", {"text": "x"})
    assert result.success is False
    assert result.error_code == "CUSTOM_BIZ_ERROR"
    assert result.message == "业务失败"


async def test_client_unknown_tool_normalized():
    client = _build_local_client(_EchoTool())
    result = await client.call("no_such_tool", {})
    assert result.success is False
    assert result.error_code == "TOOL_NOT_FOUND"
    assert "no_such_tool" in result.message


async def test_client_invalid_params_normalized():
    """参数不符合 schema：fastmcp 协议层拦截，归一化为 TOOL_CALL_FAILED。"""
    client = _build_local_client(_EchoTool())
    result = await client.call("echo", {})
    assert result.success is False
    assert result.error_code == "TOOL_CALL_FAILED"
    assert result.message


async def test_client_timeout_returns_unified_error():
    """客户端预算耗尽（工具自身超时未到）→ TOOL_TIMEOUT 统一错误结构。"""
    client = _build_local_client(_SlowTool(), call_timeout=0.05)
    result = await client.call("slow", {"text": "x"})
    assert result.success is False
    assert result.error_code == "TOOL_TIMEOUT"
    assert "0.05" in result.message


async def test_client_default_singletons_call_hello():
    """默认构造使用全局单例 Server / 注册表，可调通 hello。"""
    client = MCPClient()
    result = await client.call("hello", {"name": "华信智造"})
    assert result.success is True
    assert result.data == {"greeting": "你好，华信智造！"}

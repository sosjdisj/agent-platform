"""MCP 元数据与统一错误结构单测（Prompt 6.1）。"""
import asyncio

from pydantic import BaseModel

from app.mcp.base import BaseTool, RiskLevel, RetryPolicy, ToolError, ToolResult
from app.mcp.registry import ToolRegistry
from app.mcp.tools.hello import HelloTool


class _NumInput(BaseModel):
    x: int = 0


class _NumOutput(BaseModel):
    y: int = 0


class _BoomTool(BaseTool[_NumInput, _NumOutput]):
    """业务错误工具：run 抛 ToolError，验证统一错误结构。"""

    name = "boom"
    description = "业务错误测试工具"
    InputModel = _NumInput
    OutputModel = _NumOutput

    async def run(self, params: _NumInput) -> _NumOutput:
        raise ToolError("DATA_NOT_FOUND", "客户不存在")


class _SlowTool(BaseTool[_NumInput, _NumOutput]):
    """超时工具：run 睡眠超过 timeout。"""

    name = "slow"
    description = "超时测试工具"
    timeout = 0.01
    InputModel = _NumInput
    OutputModel = _NumOutput

    async def run(self, params: _NumInput) -> _NumOutput:
        await asyncio.sleep(0.5)
        return _NumOutput()


class _FlakyTool(BaseTool[_NumInput, _NumOutput]):
    """瞬时错误工具：前 fail_times 次 run 抛声明过的瞬时异常，之后成功。"""

    name = "flaky"
    description = "瞬时错误重试测试工具"
    transient_errors = (RuntimeError,)
    retry_policy = RetryPolicy(max_attempts=3, backoff_seconds=0)
    InputModel = _NumInput
    OutputModel = _NumOutput

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def run(self, params: _NumInput) -> _NumOutput:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("连接闪断")
        return _NumOutput(y=1)


class _HighRiskTool(BaseTool[_NumInput, _NumOutput]):
    """HIGH 风险工具：验证成功响应携带 requires_approval 风险标注。"""

    name = "high_risk"
    description = "高风险测试工具"
    risk_level = RiskLevel.HIGH
    InputModel = _NumInput
    OutputModel = _NumOutput

    async def run(self, params: _NumInput) -> _NumOutput:
        return _NumOutput(y=1)


def test_tool_metadata_completeness():
    """元数据字段齐全，input/output schema 取自 Pydantic 模型。"""
    meta = HelloTool().metadata
    assert meta.name == "hello"
    assert meta.description
    assert meta.input_schema["properties"]["name"]["type"] == "string"
    assert "greeting" in meta.output_schema["properties"]
    assert meta.risk_level is RiskLevel.SAFE
    assert meta.timeout > 0
    assert isinstance(meta.retry_policy, RetryPolicy)


def test_registry_register_and_get():
    registry = ToolRegistry()
    tool = HelloTool()
    registry.register(tool)
    assert registry.get("hello") is tool
    assert registry.all_tools() == [tool]
    try:
        registry.get("missing")
    except KeyError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("未注册工具应抛 KeyError")


def test_registry_duplicate_rejected():
    """同名同类型幂等跳过；同名异类型视为配置错误。"""
    registry = ToolRegistry()
    registry.register(HelloTool())
    registry.register(HelloTool())  # 幂等，不抛错
    assert len(registry.all_tools()) == 1

    class _ImpostorHello(HelloTool):
        pass

    try:
        registry.register(_ImpostorHello())
    except ValueError:
        pass
    else:
        raise AssertionError("同名异类型注册应抛 ValueError")


async def test_execute_success_structure():
    payload = await HelloTool().execute({"name": "华信智造"})
    assert payload == {
        "success": True,
        "data": {"greeting": "你好，华信智造！"},
        "error_code": None,
        "message": None,
        "requires_approval": None,  # SAFE 工具无审批标注
    }


async def test_execute_invalid_params_structure():
    payload = await HelloTool().execute({"name": ""})
    assert payload["success"] is False
    assert payload["error_code"] == "INVALID_PARAMS"
    assert payload["message"]


async def test_execute_high_risk_requires_approval():
    """HIGH 风险工具成功调用返回 success=true，同时携带 requires_approval=true 风险标注（HITL 占位）。"""
    payload = await _HighRiskTool().execute({})
    assert payload["success"] is True
    assert payload["requires_approval"] is True


async def test_execute_tool_error_structure():
    payload = await _BoomTool().execute({})
    assert payload["success"] is False
    assert payload["error_code"] == "DATA_NOT_FOUND"
    assert payload["message"] == "客户不存在"
    assert payload["data"] is None


async def test_execute_timeout_structure():
    payload = await _SlowTool().execute({})
    assert payload["success"] is False
    assert payload["error_code"] == "TOOL_TIMEOUT"
    assert payload["message"]


async def test_execute_transient_retry_recovers():
    """前两次瞬时失败、第三次成功：有限重试生效并返回成功结构。"""
    tool = _FlakyTool(fail_times=2)
    payload = await tool.execute({})
    assert payload["success"] is True
    assert payload["data"] == {"y": 1}
    assert tool.calls == 3  # 1 次原始 + 2 次重试


async def test_execute_transient_retry_exhausted():
    """始终瞬时失败：调用次数 == max_attempts，返回 TRANSIENT_ERROR。"""
    tool = _FlakyTool(fail_times=99)
    payload = await tool.execute({})
    assert payload["success"] is False
    assert payload["error_code"] == "TRANSIENT_ERROR"
    assert tool.calls == tool.retry_policy.max_attempts == 3
    assert "瞬时错误" in payload["message"]

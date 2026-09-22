"""hello 工具：MCP 链路验证（Prompt 6.1），无任何业务依赖。"""
from pydantic import BaseModel, Field

from app.mcp.base import BaseTool, RetryPolicy, RiskLevel


class HelloInput(BaseModel):
    """hello 入参。"""

    name: str = Field(min_length=1, description="要问候的名字")


class HelloOutput(BaseModel):
    """hello 出参。"""

    greeting: str = Field(description="问候语")


class HelloTool(BaseTool[HelloInput, HelloOutput]):
    """最简工具：验证 注册 → 调用 → 统一返回结构 全链路。"""

    name = "hello"
    description = "返回一句问候语，用于验证 MCP 工具链路是否可用"
    risk_level = RiskLevel.SAFE
    timeout = 5.0
    retry_policy = RetryPolicy(max_attempts=1)
    InputModel = HelloInput
    OutputModel = HelloOutput

    async def run(self, params: HelloInput) -> HelloOutput:
        return HelloOutput(greeting=f"你好，{params.name}！")

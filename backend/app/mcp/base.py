"""MCP 工具基类：元数据声明 + 统一错误结构 + 统一执行入口。

所有业务工具（database / business / knowledge 等）继承 BaseTool，
由 ToolRegistry 注册到 FastMCP；本文件不含任何具体业务工具。
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, Field, ValidationError

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class RiskLevel(str, Enum):
    """工具风险等级：后续评估体系与 Human-in-the-loop 审批的依据。"""

    SAFE = "safe"  # 只读查询
    LOW = "low"  # 低风险读 / 汇总类操作
    MEDIUM = "medium"  # 中风险写操作（有审计与状态要求）
    HIGH = "high"  # 高风险操作（删除 / 对外动作），需人工确认


class RetryPolicy(BaseModel):
    """重试策略（元数据）：仅瞬时错误参与重试，执行由 BaseTool.execute 实现。"""

    max_attempts: int = Field(default=1, ge=1)
    backoff_seconds: float = Field(default=0.5, ge=0)


class ToolMetadata(BaseModel):
    """工具元数据：注册时生成，供 Trace / 评估 / 审批读取。

    input_schema / output_schema 直接取自 InputModel / OutputModel 的
    JSON Schema，保证元数据与实现单一数据源。
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk_level: RiskLevel = RiskLevel.SAFE
    required_permission: str | None = None  # 调用所需权限码（None = 无需权限校验）
    timeout: float = 10.0
    retry_policy: RetryPolicy = RetryPolicy()


class ToolError(Exception):
    """工具业务错误：携带机器可读错误码，由 execute 统一转为错误结构。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def approval_required(risk_level: RiskLevel) -> bool:
    """风险审批规则（单一来源）：HIGH 风险操作需人工确认。

    BaseTool.execute 的成功响应标注（requires_approval）与 Agent 图层的调用前
    风险识别共用本规则；审批执行由后续 Human-in-the-loop 接入，当前仅占位标注。
    """
    return risk_level is RiskLevel.HIGH


class ToolResult(BaseModel):
    """统一返回结构：成功 {"success", "data"}；失败 {"success", "error_code", "message"}。

    业务失败以结构化结果返回而非抛协议层异常，Agent 可据错误码决策。
    requires_approval 为风险标注占位：HIGH 风险工具调用成功时置 true
    （审批流程由后续 Human-in-the-loop 接入），其余工具为 null。
    """

    success: bool
    data: Any | None = None
    error_code: str | None = None
    message: str | None = None
    requires_approval: bool | None = None

    @classmethod
    def ok(cls, data: Any) -> ToolResult:
        return cls(success=True, data=data)

    @classmethod
    def fail(cls, code: str, message: str) -> ToolResult:
        return cls(success=False, error_code=code, message=message)


class BaseTool(ABC, Generic[InputT, OutputT]):
    """工具基类：子类声明元数据并实现 run()，execute 提供统一执行链路。

    retry_policy.max_attempts > 1 时，仅对 transient_errors 声明的瞬时异常
    （如连接闪断）做有限重试；业务错误（ToolError）、超时与非瞬时异常不重试。
    """

    name: ClassVar[str]
    description: ClassVar[str]
    risk_level: ClassVar[RiskLevel] = RiskLevel.SAFE
    required_permission: ClassVar[str | None] = None  # 调用所需权限码（RBAC，见 rbac_service）
    timeout: ClassVar[float] = 10.0
    retry_policy: ClassVar[RetryPolicy] = RetryPolicy()
    transient_errors: ClassVar[tuple[type[Exception], ...]] = ()
    InputModel: ClassVar[type[BaseModel]]
    OutputModel: ClassVar[type[BaseModel]]

    @abstractmethod
    async def run(self, params: InputT) -> OutputT:
        """工具业务逻辑：输入输出均为强类型模型。"""

    @property
    def metadata(self) -> ToolMetadata:
        """汇总元数据；JSON Schema 直接取自 Pydantic 模型（单一数据源）。"""
        return ToolMetadata(
            name=self.name,
            description=self.description,
            input_schema=self.InputModel.model_json_schema(),
            output_schema=self.OutputModel.model_json_schema(),
            risk_level=self.risk_level,
            required_permission=self.required_permission,
            timeout=self.timeout,
            retry_policy=self.retry_policy,
        )

    async def execute(self, raw: dict[str, Any]) -> dict[str, Any]:
        """统一执行入口（MCP 调用与直接调用共用）：

        校验 → 按重试策略执行（每次尝试独立超时）→ 统一错误结构。
        仅瞬时异常参与重试；重试耗尽返回 TRANSIENT_ERROR。
        """
        try:
            params = self.InputModel.model_validate(raw)
        except ValidationError as exc:
            return ToolResult.fail("INVALID_PARAMS", str(exc)).model_dump()

        policy = self.retry_policy
        for attempt in range(1, policy.max_attempts + 1):
            try:
                output = await asyncio.wait_for(self.run(params), timeout=self.timeout)
            except asyncio.TimeoutError:
                return ToolResult.fail(
                    "TOOL_TIMEOUT", f"工具 {self.name} 执行超时（>{self.timeout}s）"
                ).model_dump()
            except ToolError as exc:
                return ToolResult.fail(exc.code, exc.message).model_dump()
            except self.transient_errors as exc:  # 空元组不匹配任何异常
                if attempt < policy.max_attempts:
                    await asyncio.sleep(policy.backoff_seconds)
                    continue
                return ToolResult.fail(
                    "TRANSIENT_ERROR",
                    f"工具 {self.name} 瞬时错误，重试 {policy.max_attempts} 次后仍失败: {exc}",
                ).model_dump()
            except Exception as exc:  # 边界兜底：未预期异常不破坏统一错误结构
                return ToolResult.fail(
                    "TOOL_INTERNAL", f"工具 {self.name} 内部错误: {exc}"
                ).model_dump()
            result = ToolResult.ok(output.model_dump())
            if approval_required(self.risk_level):  # 风险标注：HIGH 工具成功调用仍待人工确认
                result.requires_approval = True
            return result.model_dump()
        raise AssertionError("unreachable")  # 循环保证上面必然 return

"""EvaluationCase Schema：评估案例的单一数据契约（Prompt 22.1）。

字段语义（Runner 22.2 按此消费）：
- id / name：案例唯一标识与人读名；
- input：用户输入任务文本（原样作为任务 query）；
- user_role：执行角色（RBAC 角色名）；
- expected_agents：期望被调度的专业 Agent（ROUTABLE_AGENTS 子集）；
- expected_tools：期望出现调用记录的 MCP 工具名（含被权限拒绝的调用）；
- expected_outcome：期望结果标记（受控词表 OUTCOME_*，决定 Runner 断言策略）；
- expected_status：评估状态锚点——任务应到达的状态（终态，或审批暂停）；
- requires_approval：是否期望审批中断（HITL 覆盖标志）；
- expected_sources：期望出现在报告证据中的来源标识（工具名 / 知识文档 ID），
  仅正常完成的结果可声明（groundedness 断言面）。

与真实注册面的对应关系（Agent / 工具 / 角色名）由 pytest 以各单一事实源校验，
Schema 本身不反向依赖执行面，避免导入环。
"""
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

# ---------- 期望结果标记（受控词表）----------

OUTCOME_COMPLETED = "COMPLETED"  # 正常完成：报告须可溯源（groundedness）
OUTCOME_AWAITING_APPROVAL = "AWAITING_APPROVAL"  # 审批中断：任务停在等待审批
OUTCOME_RESUME_CONSISTENT = "RESUME_CONSISTENT"  # 中断恢复：审批续跑后完成且结果一致
OUTCOME_PERMISSION_DENIED = "PERMISSION_DENIED"  # 出现权限拒绝（permission_denied 轨迹）
OUTCOME_NO_RELEVANT_DOCUMENT = "NO_RELEVANT_DOCUMENT"  # 知识检索未命中（业务结果，任务仍完成）
OUTCOME_FALLBACK_DEGRADED = "FALLBACK_DEGRADED"  # 工具失败后降级完成（部分答案 + 失败说明）
OUTCOME_FAILED = "FAILED"  # 任务失败终态

ALLOWED_OUTCOMES: frozenset[str] = frozenset(
    {
        OUTCOME_COMPLETED,
        OUTCOME_AWAITING_APPROVAL,
        OUTCOME_RESUME_CONSISTENT,
        OUTCOME_PERMISSION_DENIED,
        OUTCOME_NO_RELEVANT_DOCUMENT,
        OUTCOME_FALLBACK_DEGRADED,
        OUTCOME_FAILED,
    }
)

# 评估状态锚点：三个终态 + 审批暂停（waiting_approval 仅与审批中断标记配对）
ALLOWED_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled", "waiting_approval"})

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class EvaluationCase(BaseModel):
    """单个评估案例（声明式期望；执行语义由 Runner 落地）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(description="唯一标识，kebab-case（如 e2e-sales-drop-core）")
    name: str = Field(description="案例名（人读）")
    input: str = Field(description="用户输入任务文本")
    user_role: str = Field(description="执行角色（RBAC 角色名）")
    expected_agents: list[str] = Field(default_factory=list, description="期望被调度的专业 Agent")
    expected_tools: list[str] = Field(default_factory=list, description="期望出现调用记录的工具（含被拒调用）")
    expected_outcome: str = Field(description="期望结果标记（OUTCOME_* 受控词表）")
    expected_status: str = Field(description="期望任务状态锚点（终态或 waiting_approval）")
    requires_approval: bool = Field(default=False, description="是否期望审批中断（HITL）")
    expected_sources: list[str] = Field(default_factory=list, description="期望出现在证据中的来源标识（工具名/文档 ID）")

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", value):
            raise ValueError(f"id 须为 kebab-case：{value!r}")
        return value

    @field_validator("name", "input")
    @classmethod
    def _check_non_empty(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} 不能为空白")
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> "EvaluationCase":
        if self.expected_outcome not in ALLOWED_OUTCOMES:
            raise ValueError(f"expected_outcome {self.expected_outcome!r} 不在受控词表内：{sorted(ALLOWED_OUTCOMES)}")
        if self.expected_status not in ALLOWED_STATUSES:
            raise ValueError(f"expected_status {self.expected_status!r} 不在允许集合内：{sorted(ALLOWED_STATUSES)}")

        # 审批中断 ⇔ 状态锚点为 waiting_approval；恢复续跑则锚定终态 completed
        if self.expected_status == "waiting_approval" and self.expected_outcome != OUTCOME_AWAITING_APPROVAL:
            raise ValueError(f"waiting_approval 仅可与 {OUTCOME_AWAITING_APPROVAL} 配对：{self.id}")
        if self.expected_outcome == OUTCOME_AWAITING_APPROVAL and self.expected_status != "waiting_approval":
            raise ValueError(f"{OUTCOME_AWAITING_APPROVAL} 须以 waiting_approval 为状态锚点：{self.id}")

        # HITL：中断或恢复两类结果之一，且必须锚定 waiting_approval / completed
        if self.requires_approval and self.expected_outcome not in {
            OUTCOME_AWAITING_APPROVAL,
            OUTCOME_RESUME_CONSISTENT,
        }:
            raise ValueError(f"requires_approval=True 时结果须为审批中断或恢复续跑：{self.id}")

        # 恢复续跑：审批恢复后完成，状态锚点为 completed
        if self.expected_outcome == OUTCOME_RESUME_CONSISTENT and self.expected_status != "completed":
            raise ValueError(f"{OUTCOME_RESUME_CONSISTENT} 须以 completed 为状态锚点：{self.id}")

        # 失败标记 ⇔ 失败终态
        if (self.expected_outcome == OUTCOME_FAILED) != (self.expected_status == "failed"):
            raise ValueError(f"FAILED 须与 failed 终态配对：{self.id}")

        # 权限拒绝必须指认被拒工具（含被拒调用的 expected_tools 语义）
        if self.expected_outcome == OUTCOME_PERMISSION_DENIED and not self.expected_tools:
            raise ValueError(f"权限拒绝案例须在 expected_tools 指认被拒工具：{self.id}")

        # 知识未命中只可能由知识 Agent 产生
        if self.expected_outcome == OUTCOME_NO_RELEVANT_DOCUMENT and "knowledge" not in self.expected_agents:
            raise ValueError(f"知识未命中案例须调度 knowledge Agent：{self.id}")

        # 证据只可声明在有据完成的结果上（groundedness）
        if self.expected_sources and self.expected_outcome != OUTCOME_COMPLETED:
            raise ValueError(f"expected_sources 仅支持 {OUTCOME_COMPLETED} 结果：{self.id}")

        # 列表字段内去重（保持声明简洁，重复即写错）
        for field_name in ("expected_agents", "expected_tools", "expected_sources"):
            values: list[str] = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} 存在重复项：{self.id}")

        return self

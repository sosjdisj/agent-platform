"""Prompt 22.1 验收：评估案例库数据完整、覆盖度达标。

三类校验：
1. Schema 自身约束（受控词表 / 交叉规则由模型校验器强制，负面用例证明其生效）；
2. 单一事实源对齐：expected_agents ⊆ ROUTABLE_AGENTS、expected_tools ⊆ 注册工具面、
   user_role ∈ RBAC 角色定义、requires_approval 案例必须指认高危工具；
3. 覆盖度达标：20 案例、≥2 PERMISSION_DENIED、≥2 NO_RELEVANT_DOCUMENT、
   ≥2 HITL、≥1 失败+fallback、多轮工具 / RAG / 路由零调度 / 三角色 / Groundedness。

不做：Runner（22.2）。
"""

import pytest
from pydantic import ValidationError

from app.agents.supervisor import ROUTABLE_AGENTS  # 与 Supervisor 调度面同源
from app.evaluation.cases import EVALUATION_CASES
from app.evaluation.schemas import (
    ALLOWED_OUTCOMES,
    OUTCOME_AWAITING_APPROVAL,
    OUTCOME_COMPLETED,
    OUTCOME_FALLBACK_DEGRADED,
    OUTCOME_FAILED,
    OUTCOME_NO_RELEVANT_DOCUMENT,
    OUTCOME_PERMISSION_DENIED,
    OUTCOME_RESUME_CONSISTENT,
    EvaluationCase,
)
from app.mcp.base import RiskLevel
from app.mcp.registry import ToolRegistry
from app.mcp.tools import register_default_tools
from app.services.rbac_service import ROLES

# ---------- 单一事实源对齐 ----------


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_default_tools(registry)
    return registry


def test_cases_align_with_real_registry() -> None:
    """Agent / 工具 / 角色 / 高危工具均与真实注册面对齐，防止案例引用漂移。"""
    registry = _registry()
    tool_names = {tool.name for tool in registry.all_tools()}
    high_risk_tools = {t.name for t in registry.all_tools() if t.risk_level == RiskLevel.HIGH}
    role_names = {role["name"] for role in ROLES}

    assert len(EVALUATION_CASES) == 20
    assert len({case.id for case in EVALUATION_CASES}) == 20  # id 唯一

    for case in EVALUATION_CASES:
        assert set(case.expected_agents) <= set(ROUTABLE_AGENTS), case.id
        assert set(case.expected_tools) <= tool_names, case.id
        assert case.user_role in role_names, case.id
        if case.requires_approval:
            # 审批只由高危工具触发：案例必须指认至少一个高危工具
            assert set(case.expected_tools) & high_risk_tools, case.id


# ---------- Schema 自身约束（负面用例）----------


def _base_kwargs() -> dict:
    return {
        "id": "schema-probe",
        "name": "校验探针",
        "input": "探针任务。",
        "user_role": "admin",
        "expected_agents": ["data"],
        "expected_tools": ["query_orders"],
        "expected_outcome": OUTCOME_COMPLETED,
        "expected_status": "completed",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda kw: kw.update(expected_outcome="SOMETHING_ELSE"),  # 词表外标记
        lambda kw: kw.update(expected_status="pending"),  # 非法状态锚点
        lambda kw: kw.update(expected_status="waiting_approval"),  # waiting_approval 仅配审批中断
        lambda kw: kw.update(id="Bad_Id"),  # id 非 kebab-case
        lambda kw: kw.update(input="   "),  # 空白输入
        lambda kw: kw.update(  # 证据只可声明在有据完成上
            expected_outcome=OUTCOME_PERMISSION_DENIED, expected_sources=["退款政策"]
        ),
    ],
)
def test_schema_rejects_inconsistent_cases(mutate) -> None:
    kwargs = _base_kwargs()
    mutate(kwargs)
    with pytest.raises(ValidationError):
        EvaluationCase(**kwargs)


def test_schema_rejects_semantic_mismatches() -> None:
    # 权限拒绝必须指认被拒工具
    kwargs = _base_kwargs() | {"expected_outcome": OUTCOME_PERMISSION_DENIED, "expected_tools": []}
    with pytest.raises(ValidationError):
        EvaluationCase(**kwargs)
    # 知识未命中必须调度 knowledge Agent
    kwargs = _base_kwargs() | {"expected_outcome": OUTCOME_NO_RELEVANT_DOCUMENT}
    with pytest.raises(ValidationError):
        EvaluationCase(**kwargs)
    # 重复列表项
    kwargs = _base_kwargs() | {"expected_tools": ["query_orders", "query_orders"]}
    with pytest.raises(ValidationError):
        EvaluationCase(**kwargs)


def test_schema_accepts_resume_consistent_pairing() -> None:
    """恢复续跑：审批中断后恢复并完成，锚定 completed。"""
    case = EvaluationCase(
        **_base_kwargs()
        | {
            "expected_outcome": OUTCOME_RESUME_CONSISTENT,
            "expected_status": "completed",
            "requires_approval": True,
        }
    )
    assert case.expected_status == "completed"


# ---------- 覆盖度达标 ----------


def test_coverage_checklist() -> None:
    outcomes = [case.expected_outcome for case in EVALUATION_CASES]

    # 硬性下限：≥2 权限拒绝、≥2 知识未命中、≥2 HITL、≥1 失败+fallback、≥1 恢复续跑
    assert outcomes.count(OUTCOME_PERMISSION_DENIED) >= 2
    assert outcomes.count(OUTCOME_NO_RELEVANT_DOCUMENT) >= 2
    assert sum(1 for case in EVALUATION_CASES if case.requires_approval) >= 2
    assert outcomes.count(OUTCOME_FALLBACK_DEGRADED) >= 1
    assert outcomes.count(OUTCOME_FAILED) >= 1
    assert outcomes.count(OUTCOME_RESUME_CONSISTENT) >= 1

    # Groundedness：正常完成且声明证据来源的案例充足
    grounded = [
        case
        for case in EVALUATION_CASES
        if case.expected_outcome == OUTCOME_COMPLETED and case.expected_sources
    ]
    assert len(grounded) >= 8

    # 多轮工具链（≥2 个工具）与 RAG（knowledge 调度）
    assert sum(1 for case in EVALUATION_CASES if len(case.expected_tools) >= 2) >= 3
    assert sum(1 for case in EVALUATION_CASES if "knowledge" in case.expected_agents) >= 3

    # 路由两极：零调度直答与多 Agent 编排并存；三种角色均被覆盖
    assert any(not case.expected_agents for case in EVALUATION_CASES)
    assert any(len(case.expected_agents) >= 2 for case in EVALUATION_CASES)
    assert {case.user_role for case in EVALUATION_CASES} == {role["name"] for role in ROLES}

    # 词表无冗余：所有出现的结果标记均在受控集合内（Schema 已强制，此处锚定常量面）
    assert set(outcomes) <= ALLOWED_OUTCOMES

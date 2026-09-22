"""Prompt 22.3 验收：20 案例全量跑通评估并产出报告（JSON / HTML）+ 评估配置隔离。

- 批量执行：EVALUATION_CASES 全量 × 场景脚本替身（ScriptedSupervisor 按
  DEMO_SCENARIOS 写入可观察契约：agent_selected / 工具与权限轨迹 / approval_required /
  状态流转 / 报告证据，机制单一来源见 app.evaluation.demo）；
- 偏差注入：两个案例刻意偏离期望（双 Agent 少派一个 / 报告缺证据来源），验证
  错误案例清单与指标解释力——全 1.0 的报告无法证明清单机制有效；
- 配置隔离：EvaluationAgentConfig（temperature=0 等）经 create_evaluation_llm 固定
  default_temperature（显式传参可覆盖、生产缺省行为不变）；build_evaluation_supervisor
  为生产评估组合根（与 build_task_executor 对称，真实接线但构造不触网）。
"""
import json

import pytest

from app.core.llm import LLMService, create_llm_service
from app.evaluation.cases import EVALUATION_CASES
from app.evaluation.config import (
    EvaluationAgentConfig,
    build_evaluation_supervisor,
    create_evaluation_llm,
)
from app.evaluation.demo import DEMO_SCENARIOS, ScriptedSupervisor
from app.evaluation.reporting import build_report, write_reports
from app.evaluation.runner import EvaluationRunner
from tests.test_evaluation_runner import _seed_users


async def test_full_case_run_produces_reports(seeded_maker, db_session, tmp_path) -> None:
    """验收：20 案例全量跑通，报告（JSON / HTML）含总体指标 + 错误清单 + 单案例详情。"""
    assert set(DEMO_SCENARIOS) == {case.id for case in EVALUATION_CASES}  # 场景表与案例库对齐

    users_by_role = await _seed_users(db_session)
    runner = EvaluationRunner(
        seeded_maker, lambda case: ScriptedSupervisor(seeded_maker, DEMO_SCENARIOS[case.id])
    )
    results = await runner.run_all(EVALUATION_CASES, users_by_role)
    report = build_report(EVALUATION_CASES, results)

    metrics = report.metrics
    assert metrics.total_cases == 20 and metrics.passed_cases == 18
    assert {item.case.id for item in report.failures} == {
        "routing-crm-and-policy",  # 偏差 1：路由 / 工具 / 证据三维失分
        "tool-select-product-sales",  # 偏差 2：仅 groundedness 失分
    }
    assert metrics.agent_routing_accuracy.value == pytest.approx(19 / 20)
    assert metrics.tool_selection_accuracy.value == pytest.approx(19 / 20)
    assert metrics.task_completion_rate.value == pytest.approx(17 / 20)  # 2 审批暂停 + 1 失败
    assert metrics.rag_groundedness.value == pytest.approx(7 / 9)  # 9 例声明证据，2 例失分
    assert metrics.permission_violation_rate.value == 0.0
    assert metrics.hitl_correctness.value == 1.0 and metrics.hitl_correctness.n == 3
    assert metrics.average_latency_ms.value > 0 and metrics.average_latency_ms.n == 20

    json_path, html_path = write_reports(report, tmp_path)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["metrics"]["total_cases"] == 20
    assert len(payload["cases"]) == 20
    assert {item["case"]["id"] for item in payload["failures"]} == {
        "routing-crm-and-policy",
        "tool-select-product-sales",
    }
    denied_case = next(c for c in payload["cases"] if c["case"]["id"] == "perm-sales-refund")
    assert denied_case["result"]["observation"]["permission_denied_tools"] == ["refund_order"]
    assert denied_case["result"]["mismatches"] == []

    html_text = html_path.read_text(encoding="utf-8")
    assert "总体指标" in html_text and "单案例详情" in html_text
    assert "错误案例清单（2）" in html_text
    assert "routing-crm-and-policy" in html_text and "tool-select-product-sales" in html_text


def test_evaluation_llm_temperature_isolation() -> None:
    """配置隔离：评估 LLM 固定 temperature=0（显式传参可覆盖），生产缺省不发送 temperature。"""
    eval_llm = create_evaluation_llm()
    assert isinstance(eval_llm, LLMService)
    assert eval_llm._default_temperature == 0.0
    assert create_llm_service()._default_temperature is None
    assert (
        create_evaluation_llm(EvaluationAgentConfig(temperature=0.3))._default_temperature == 0.3
    )

"""Prompt 24.2 验收：评估报告 API（Dashboard 数据源）读取 Prompt 22.3 报告数据。

- 未登录 401；报告缺失 / 损坏 → 404（EVALUATION_REPORT_NOT_FOUND，前端引导先生成）；
- 正常返回 EvaluationReport 全文（metrics / cases / failures 齐全，与落盘 JSON 一致）；
- 报告目录取 settings.evaluation_report_dir（测试 monkeypatch 指向临时目录，
  相对路径以项目根为基准的解析逻辑由 evaluation_report_dir_path 保证）。
"""
import pytest

from app.core.config import get_settings
from app.evaluation.cases import EVALUATION_CASES
from app.evaluation.reporting import build_report, write_reports
from app.evaluation.runner import CaseObservation, CaseRunResult

from tests.test_auth import login, register

REPORT_URL = "/api/evaluation/report"


@pytest.fixture
def report_dir(tmp_path, monkeypatch):
    """把评估报告目录指向临时目录（路径属性基于缓存的 Settings 实例即时计算）。"""
    monkeypatch.setattr(get_settings(), "evaluation_report_dir", str(tmp_path))
    return tmp_path


def _minimal_report():
    """最小报告：首个真实案例 + 全通过判定（Dashboard 渲染契约验证用）。"""
    case = EVALUATION_CASES[0]
    result = CaseRunResult(
        case_id=case.id,
        observation=CaseObservation(
            agents=frozenset(case.expected_agents),
            tools=frozenset(case.expected_tools),
            permission_denied_tools=frozenset(),
            approval_required=False,
            status=case.expected_status,
            sources=(),
        ),
        latency_ms=42,
        completed=case.expected_status == "completed",
        routing_ok=True,
        tools_ok=True,
        status_ok=True,
        permission_ok=True,
    )
    return build_report([case], [result])


async def _token(api_client) -> str:
    await register(api_client)
    return (await login(api_client))["access_token"]


async def test_report_requires_auth(api_client):
    """未登录读取评估报告 → 401。"""
    assert (await api_client.get(REPORT_URL)).status_code == 401


async def test_report_missing_returns_404(api_client, report_dir):
    """报告未生成 → 404 + EVALUATION_REPORT_NOT_FOUND（前端空态引导先生成）。"""
    token = await _token(api_client)
    resp = await api_client.get(
        REPORT_URL, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "EVALUATION_REPORT_NOT_FOUND"


async def test_report_serves_latest_json(api_client, report_dir):
    """已生成报告 → 返回与落盘 JSON 一致的完整报告（metrics / cases / failures）。"""
    write_reports(_minimal_report(), report_dir)
    token = await _token(api_client)

    resp = await api_client.get(REPORT_URL, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["metrics"]["total_cases"] == 1
    assert len(body["cases"]) == 1
    assert body["failures"] == []
    assert body["cases"][0]["case"]["id"] == EVALUATION_CASES[0].id
    assert body["cases"][0]["result"]["latency_ms"] == 42

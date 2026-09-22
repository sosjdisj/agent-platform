"""评估报告（Prompt 22.3）：单案例详情 + 总体指标 + 错误案例清单，JSON / HTML 双格式输出。

- EvaluationReport：聚合模型即报告契约——metrics（22.2 指标）+ cases（案例声明 ×
  判定结果，含实际观察）+ failures（computed_field：错误案例清单由 cases 单一来源
  推导，不落字段避免双写漂移，序列化时随 JSON 一并输出）；
- render_json：model_dump_json 直出（供程序消费与归档）；
- render_html：零依赖单文件 HTML（内联样式，动态内容全部 html.escape），供人工查阅；
- write_reports：落盘 JSON / HTML（固定文件名，覆盖即"最新报告"语义）。
不做：前端 Dashboard（22.3 明确排除）。
"""
import html
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, computed_field

from app.core.config import EVALUATION_REPORT_JSON
from app.evaluation.runner import CaseRunResult, MetricsReport, summarize_metrics
from app.evaluation.schemas import EvaluationCase

# 指标展示名（顺序即报告呈现顺序，与 MetricsReport 字段一一对应）
_METRIC_LABELS: tuple[tuple[str, str], ...] = (
    ("agent_routing_accuracy", "Agent 路由准确率"),
    ("tool_selection_accuracy", "工具选择准确率"),
    ("task_completion_rate", "任务完成率"),
    ("rag_groundedness", "RAG 证据可溯源率"),
    ("permission_violation_rate", "权限违规率（越低越好）"),
    ("hitl_correctness", "HITL 正确率"),
    ("average_latency_ms", "平均延迟（ms）"),
)

_STYLE = (
    "body{font-family:system-ui,-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
    "margin:24px auto;max-width:1280px;color:#1f2328;font-size:14px}"
    "h1{font-size:22px}h2{font-size:16px;margin-top:28px}"
    "table{border-collapse:collapse;width:100%;margin:10px 0}"
    "th,td{border:1px solid #d0d7de;padding:6px 10px;text-align:left;vertical-align:top}"
    "th{background:#f6f8fa;white-space:nowrap}"
    ".pass{color:#1a7f37;font-weight:600}.fail{color:#cf222e;font-weight:600}"
    ".muted{color:#57606a;font-weight:400}"
)


class CaseDetail(BaseModel):
    """单案例详情：案例声明（期望）× 判定结果（各维度对错 + 实际观察）。"""

    case: EvaluationCase
    result: CaseRunResult


class EvaluationReport(BaseModel):
    """评估报告聚合模型：model_dump 即 JSON 报告（failures 随序列化输出）。"""

    generated_at: datetime
    metrics: MetricsReport
    cases: list[CaseDetail]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failures(self) -> list[CaseDetail]:
        """错误案例清单：任一适用维度未通过的案例（由 cases 推导，单一来源）。"""
        return [item for item in self.cases if not item.result.passed]


def build_report(
    cases: Sequence[EvaluationCase], results: Sequence[CaseRunResult]
) -> EvaluationReport:
    """案例与判定结果按序配对成报告（长度不一致属调用方缺陷，直接抛错）。"""
    if len(cases) != len(results):
        raise ValueError(f"案例数 {len(cases)} 与结果数 {len(results)} 不一致")
    return EvaluationReport(
        generated_at=datetime.now(),
        metrics=summarize_metrics(results),
        cases=[CaseDetail(case=case, result=result) for case, result in zip(cases, results)],
    )


def render_json(report: EvaluationReport) -> str:
    """JSON 报告文本：总体指标 + 单案例详情 + 错误案例清单。"""
    return report.model_dump_json(indent=2)


def _format_metric(value: float | None) -> str:
    """指标值渲染：None（不适用）→ 占位符；比率三位小数；延迟取整毫秒。"""
    if value is None:
        return "—"
    return f"{value:.3f}" if value <= 1 else f"{value:.0f} ms"


def render_html(report: EvaluationReport) -> str:
    """HTML 报告：总体指标 / 错误案例清单 / 单案例详情（期望与实际对照）。"""
    e = html.escape

    def pair(expected: Sequence[str], actual: Sequence[str]) -> str:
        """期望 / 实际对照单元格：上行期望，下行实际（灰色弱化）。"""
        return (
            f"{e('、'.join(expected) or '（无）')}"
            f"<br><span class='muted'>实际：{e('、'.join(actual) or '（无）')}</span>"
        )

    def state_cell(expected: str, actual: str, ok: bool) -> str:
        cls = "pass" if ok else "fail"
        return f"{e(expected)} → <span class='{cls}'>{e(actual)}</span>"

    metric_rows = "".join(
        f"<tr><td>{e(label)}</td>"
        f"<td>{_format_metric(getattr(report.metrics, field).value)}</td>"
        f"<td>{getattr(report.metrics, field).n}</td></tr>"
        for field, label in _METRIC_LABELS
    )

    failure_rows = "".join(
        f"<tr><td>{e(item.case.id)}<br><span class='muted'>{e(item.case.name)}</span></td>"
        f"<td>{e(item.case.user_role)}</td>"
        "<td><span class='fail'>未通过</span></td>"
        f"<td>{'<br>'.join(e(m) for m in item.result.mismatches) or '—'}</td></tr>"
        for item in report.failures
    ) or "<tr><td colspan='4' class='muted'>无</td></tr>"

    case_rows = "".join(
        f"<tr><td>{e(item.case.id)}<br><span class='muted'>{e(item.case.name)}</span></td>"
        f"<td>{e(item.case.user_role)}</td>"
        f"<td>{pair(item.case.expected_agents, sorted(item.result.observation.agents))}</td>"
        f"<td>{pair(item.case.expected_tools, sorted(item.result.observation.tools))}</td>"
        f"<td>{state_cell(item.case.expected_status, item.result.observation.status, item.result.status_ok)}</td>"
        f"<td>{pair(item.case.expected_sources, list(item.result.observation.sources))}</td>"
        f"<td>{item.result.latency_ms} ms</td>"
        f"<td>{'<br>'.join(e(m) for m in item.result.mismatches) or '—'}</td>"
        f"<td><span class='{'pass' if item.result.passed else 'fail'}'>"
        f"{'通过' if item.result.passed else '未通过'}</span></td></tr>"
        for item in report.cases
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>客户智能分析 Agent 评估报告</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>客户智能分析 Agent 评估报告</h1>
<p>生成时间：{e(report.generated_at.isoformat(timespec='seconds'))}　|　案例总数：<strong>{report.metrics.total_cases}</strong>　|　通过：<strong class='pass'>{report.metrics.passed_cases}</strong>　|　未通过：<strong class='fail'>{len(report.failures)}</strong></p>
<h2>总体指标</h2>
<table><tr><th>指标</th><th>值</th><th>分母 n</th></tr>{metric_rows}</table>
<h2>错误案例清单（{len(report.failures)}）</h2>
<table><tr><th>案例</th><th>角色</th><th>结果</th><th>不匹配明细</th></tr>{failure_rows}</table>
<h2>单案例详情</h2>
<table><tr><th>案例</th><th>角色</th><th>路由（期望/实际）</th><th>工具（期望/实际）</th><th>状态（期望/实际）</th><th>证据来源（期望/实际）</th><th>延迟</th><th>不匹配明细</th><th>结果</th></tr>{case_rows}</table>
</body>
</html>
"""


def write_reports(report: EvaluationReport, out_dir: str | Path) -> tuple[Path, Path]:
    """落盘 JSON / HTML 报告（固定文件名，重复生成覆盖即"最新报告"），返回文件路径。"""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / EVALUATION_REPORT_JSON
    html_path = directory / "evaluation_report.html"
    json_path.write_text(render_json(report), encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path


def load_latest_report(report_dir: str | Path) -> EvaluationReport:
    """读取最新评估报告 JSON（Dashboard API 数据源；文件缺失 / 损坏抛 FileNotFoundError）。"""
    return EvaluationReport.model_validate_json(
        (Path(report_dir) / EVALUATION_REPORT_JSON).read_text(encoding="utf-8")
    )

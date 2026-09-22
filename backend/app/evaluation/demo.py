"""脚本化演示评估（Prompt 24.2）：无需 LLM 的确定性评估数据源，供 Dashboard 本地演示。

- ScriptedSupervisor 按 DEMO_SCENARIOS 写入可观察契约（agent_selected / 工具与权限
  轨迹 / approval_required / 状态流转 / 报告证据），驱动 Runner 的观察—判定—报告链路；
  真实 Agent 全链路保真由 21.4 E2E 与 22.2 Runner 测试覆盖，不在此重复；
- DEMO_SCENARIOS 含两处刻意偏差（双 Agent 少派一个 / 报告缺证据来源），验证错误
  案例清单与指标解释力——全 1.0 的报告无法证明清单机制有效；场景表键全集与案例库
  以"集合相等"断言锁定（见验收测试），案例库扩充时强制补充场景；
- main（python -m app.evaluation.demo）：seed RBAC 与演示用户（幂等）→ 全量跑
  EVALUATION_CASES → write_reports 落盘 settings.evaluation_report_dir（Dashboard API
  读取同一路径，--out 可覆盖）。
"""
import asyncio
from dataclasses import dataclass

from sqlalchemy import insert, select

from app.agents.report import AnalysisReport, ReportEvidence
from app.agents.state import AgentStatus, Source
from app.agents.supervisor import SupervisorError
from app.evaluation.cases import EVALUATION_CASES
from app.evaluation.reporting import EvaluationReport, build_report, write_reports
from app.evaluation.runner import EvaluationRunner
from app.models.agent import AgentTraceEventType
from app.models.user import User
from app.repositories.agent_task import AgentTaskRepository
from app.services.rbac_service import seed_rbac
from app.services.task_service import TaskService
from app.services.trace_service import TraceService

# 演示用户（角色名 → 用户名）：与 RBAC 三角色一一对应，缺失时幂等补建
_DEMO_USERS: dict[str, str] = {
    "employee": "demo_employee",
    "sales": "demo_sales",
    "admin": "demo_admin",
}


@dataclass(frozen=True)
class ScriptedScenario:
    """单案例场景脚本：声明该案例应产生的可观察行为（与 EvaluationCase 期望同构）。"""

    agents: tuple[str, ...] = ()  # 依次调度（agent_selected）
    tools: tuple[str, ...] = ()  # 正常执行的工具（tool_called / tool_result）
    denied: tuple[str, ...] = ()  # 被权限拒绝的工具（permission_denied）
    approval_tool: str | None = None  # 触发审批中断的工具（approval_required）
    resume: bool = False  # 审批恢复续跑（waiting_approval → running → completed）
    sources: tuple[str, ...] = ()  # 报告证据来源（ref_id，startswith 匹配）
    fail: bool = False  # 编排异常 → 任务 failed


# 20 案例场景表（键全集与 EVALUATION_CASES 对齐；含两处刻意偏差，见模块 docstring）
DEMO_SCENARIOS: dict[str, ScriptedScenario] = {
    "e2e-sales-drop-core": ScriptedScenario(
        agents=("data", "knowledge"),
        tools=("query_customers", "query_sales_data", "query_orders", "search_knowledge"),
        sources=("query_sales_data(客户A近6月)", "query_orders(customer_id=1)", "库存管理制度::c003"),
    ),
    "routing-chitchat": ScriptedScenario(),
    "routing-customer-profile": ScriptedScenario(
        agents=("data",),
        tools=("query_customers", "query_orders"),
        sources=("query_customers(客户B)", "query_orders(customer_id=2)"),
    ),
    # 偏差 1：应派 business+knowledge 却只派了 business（路由 / 工具 / 证据三维度失分）
    "routing-crm-and-policy": ScriptedScenario(
        agents=("business",), tools=("get_crm_summary",), sources=("get_crm_summary(客户C)",)
    ),
    "tool-select-monthly-trend": ScriptedScenario(
        agents=("data",), tools=("query_sales_data",), sources=("query_sales_data(客户A月度)",)
    ),
    # 偏差 2：路由与工具正确但报告未引用证据（仅 groundedness 失分）
    "tool-select-product-sales": ScriptedScenario(agents=("data",), tools=("query_product_sales",)),
    "multi-round-verify-and-ticket": ScriptedScenario(
        agents=("data", "business"),
        tools=("query_orders", "query_customers", "create_ticket"),
        sources=("query_orders(customer_id=2)", "create_ticket(TICKET-102)"),
    ),
    "rag-refund-policy": ScriptedScenario(
        agents=("knowledge",), tools=("search_knowledge",), sources=("退款政策::c001",)
    ),
    "rag-inventory-rule": ScriptedScenario(
        agents=("knowledge",), tools=("search_knowledge",), sources=("库存管理制度::c002",)
    ),
    "rag-no-relevant-quantum": ScriptedScenario(agents=("knowledge",), tools=("search_knowledge",)),
    "rag-no-relevant-competitor": ScriptedScenario(agents=("knowledge",), tools=("search_knowledge",)),
    "perm-employee-sales-read": ScriptedScenario(agents=("data",), denied=("query_sales_data",)),
    "perm-employee-refund": ScriptedScenario(agents=("business",), denied=("refund_order",)),
    "perm-sales-refund": ScriptedScenario(agents=("business",), denied=("refund_order",)),
    "hitl-admin-refund": ScriptedScenario(agents=("business",), approval_tool="refund_order"),
    "hitl-update-customer": ScriptedScenario(agents=("business",), approval_tool="update_customer"),
    "fallback-tool-degraded": ScriptedScenario(
        agents=("data",), tools=("query_orders", "query_customers")
    ),
    "task-failed-tool-error": ScriptedScenario(
        agents=("business",), tools=("refund_order", "update_customer"), fail=True
    ),
    "resume-after-approval": ScriptedScenario(
        agents=("data", "business"),
        tools=("query_sales_data", "update_customer"),
        approval_tool="update_customer",
        resume=True,
    ),
    "groundedness-risk-assessment": ScriptedScenario(
        agents=("knowledge", "data"),
        tools=("search_knowledge", "query_sales_data"),
        sources=("库存管理制度::c001", "query_sales_data(客户A)"),
    ),
}


class ScriptedSupervisor:
    """场景脚本替身：按 ScriptedScenario 写入可观察契约（轨迹 + 状态流转 + 报告证据），
    驱动 Runner 的观察—判定—报告链路。source_type 不进观察面，ref_id 即证据来源。"""

    def __init__(self, session_maker, scenario: ScriptedScenario) -> None:
        self._session_maker = session_maker
        self._scenario = scenario

    async def _trace(self, task_id: int, event_type: AgentTraceEventType, **fields) -> None:
        async with self._session_maker() as session:
            await TraceService(session).append(task_id=task_id, event_type=event_type, **fields)

    async def _transition(self, task_id: int, status: AgentStatus) -> None:
        async with self._session_maker() as session:
            task = await AgentTaskRepository(session).get(task_id)
            await TaskService(session).transition(task, status)

    async def run(self, task_id: int, user_id: int, query: str) -> AnalysisReport:
        scenario = self._scenario
        for round_no, agent in enumerate(scenario.agents, start=1):
            await self._trace(
                task_id, AgentTraceEventType.AGENT_SELECTED, agent=agent, round=round_no
            )
        for tool in scenario.tools:
            await self._trace(task_id, AgentTraceEventType.TOOL_CALLED, tool=tool)
            await self._trace(task_id, AgentTraceEventType.TOOL_RESULT, tool=tool, status="success")
        for tool in scenario.denied:
            await self._trace(task_id, AgentTraceEventType.PERMISSION_DENIED, tool=tool)
        if scenario.approval_tool is not None:
            await self._trace(
                task_id,
                AgentTraceEventType.APPROVAL_REQUIRED,
                tool=scenario.approval_tool,
                result_summary="高危操作待人工审批",
            )
            await self._transition(task_id, AgentStatus.WAITING_APPROVAL)
            if scenario.resume:
                await self._transition(task_id, AgentStatus.RUNNING)
        if scenario.fail:
            raise SupervisorError(f"场景脚本注入的编排失败：{task_id}")
        return AnalysisReport(
            conclusion="场景脚本汇总结论。",
            evidence=[
                ReportEvidence(
                    content=f"证据{index}",
                    sources=[Source(source_type="tool", ref_id=ref)],
                )
                for index, ref in enumerate(scenario.sources, start=1)
            ],
        )


async def ensure_demo_users(session_maker) -> dict[str, int]:
    """幂等准备演示用户：seed RBAC，缺失的角色用户补建，返回角色名 → 用户 id。"""
    from app.models.rbac import Role, user_roles

    async with session_maker() as session:
        await seed_rbac(session)
        role_ids = dict((await session.execute(select(Role.name, Role.id))).all())
        users_by_role: dict[str, int] = {}
        for role, username in _DEMO_USERS.items():
            row = (
                await session.execute(select(User).where(User.username == username))
            ).scalar_one_or_none()
            if row is None:
                row = User(
                    username=username, email=f"{username}@demo.local", hashed_password="x"
                )
                session.add(row)
                await session.flush()
                await session.execute(
                    insert(user_roles), [{"user_id": row.id, "role_id": role_ids[role]}]
                )
            users_by_role[role] = row.id
        await session.commit()
        return users_by_role


async def run_demo_evaluation(session_maker) -> EvaluationReport:
    """全量跑 EVALUATION_CASES（脚本化编排）并聚合为评估报告。"""
    users_by_role = await ensure_demo_users(session_maker)
    runner = EvaluationRunner(
        session_maker, lambda case: ScriptedSupervisor(session_maker, DEMO_SCENARIOS[case.id])
    )
    results = await runner.run_all(EVALUATION_CASES, users_by_role)
    return build_report(EVALUATION_CASES, results)


def main() -> None:
    """CLI 入口：python -m app.evaluation.demo（--out 覆盖输出目录）。"""
    import argparse
    from pathlib import Path

    from app.core.config import get_settings
    from app.db.session import SessionLocal

    parser = argparse.ArgumentParser(description="跑脚本化演示评估并落盘报告（JSON / HTML）")
    parser.add_argument("--out", default=None, help="输出目录（缺省取 settings.evaluation_report_dir）")
    args = parser.parse_args()

    directory = Path(args.out) if args.out else get_settings().evaluation_report_dir_path
    report = asyncio.run(run_demo_evaluation(SessionLocal))
    json_path, html_path = write_reports(report, directory)
    print(f"评估报告已生成：{json_path}\n{html_path}")


if __name__ == "__main__":
    main()

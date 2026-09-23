"""Prompt 22.2 验收：3 个手工案例跑通评估 Runner 并输出指标 JSON。

- 真实执行面：EvaluationRunner → TaskExecutor → Supervisor（LLM 脚本化）；
  案例 1 用真实 DataAgent（种子库真实查询 + 报告证据溯源）；案例 2 用真实
  BusinessAgent（RBAC 真实解析，refund_order 被 PERMISSION_DENIED）；案例 3 用
  审批中断替身（复刻可观察契约：agent_selected / approval_required 轨迹 +
  waiting_approval 流转）——Supervisor 多 Agent 路径的审批闭环属后续 Prompt；
- 指标 JSON 数值断言：routing / tools / groundedness / hitl = 1.0，completion = 2/3
  （HITL 案例停在 waiting_approval 不计完成），violation = 0.0，latency > 0。

不做：配置隔离（Runner 由测试直接构造）。
"""
import json
import pytest
from sqlalchemy import insert, select

from app.agents.business import BUSINESS_AGENT_NAME, BusinessAgent
from app.agents.data import DataAgent
from app.agents.report import AnalysisReport, EvidenceDraft, ReportDraft
from app.agents.state import AgentStatus
from app.agents.supervisor import RouteDecision, Supervisor
from app.evaluation.runner import EvaluationRunner, summarize_metrics
from app.evaluation.schemas import EvaluationCase
from app.models.agent import AgentTraceEvent, AgentTraceEventType
from app.models.order import Order
from app.models.rbac import Role, user_roles
from app.models.user import User
from app.services.rbac_service import seed_rbac
from app.services.trace_service import TraceService
from tests.test_agent_graph import final_response, tool_call_response
from tests.test_agent_supervisor import FakeAgent, SupervisorLLMStub


async def _seed_users(db_session) -> dict[str, int]:
    """seed RBAC 并创建 employee / sales / admin 三个用户（角色名 → 用户 id）。"""
    await seed_rbac(db_session)
    users = [
        User(username="eval_employee", email="eval_emp@test.com", hashed_password="x"),
        User(username="eval_sales", email="eval_sales@test.com", hashed_password="x"),
        User(username="eval_admin", email="eval_admin@test.com", hashed_password="x"),
    ]
    db_session.add_all(users)
    await db_session.flush()
    role_ids = dict((await db_session.execute(select(Role.name, Role.id))).all())
    await db_session.execute(
        insert(user_roles),
        [
            {"user_id": user.id, "role_id": role_ids[role]}
            for user, role in zip(users, ("employee", "sales", "admin"))
        ],
    )
    await db_session.commit()
    return dict(zip(("employee", "sales", "admin"), (user.id for user in users)))


# ---------- 三个手工案例（声明式期望，与 22.1 Schema 同构）----------

_CASE_DATA = EvaluationCase(
    id="runner-data-orders",
    name="Runner 手工案例：数据查询与证据溯源",
    input="查一下客户 A 最近有哪些订单。",
    user_role="sales",
    expected_agents=["data"],
    expected_tools=["query_orders"],
    expected_outcome="COMPLETED",
    expected_status="completed",
    expected_sources=["query_orders"],
)

_CASE_PERMISSION = EvaluationCase(
    id="runner-perm-refund",
    name="Runner 手工案例：权限拒绝",
    input="把客户 B 的一笔订单退款。",
    user_role="employee",
    expected_agents=["business"],
    expected_tools=["refund_order"],
    expected_outcome="PERMISSION_DENIED",
    expected_status="completed",
)

_CASE_HITL = EvaluationCase(
    id="runner-hitl-refund",
    name="Runner 手工案例：审批中断",
    input="客户 B 的订单需要退款，走审批流程。",
    user_role="admin",
    expected_agents=["business"],
    expected_tools=["refund_order"],
    expected_outcome="AWAITING_APPROVAL",
    expected_status="waiting_approval",
    requires_approval=True,
)


def _data_supervisor(seeded_maker, a_id: int) -> Supervisor:
    """案例 1 编排：路由 data → 真实 DataAgent（真实订单查询）→ 汇总报告（证据引用订单查询）。"""
    llm = SupervisorLLMStub(
        chats=[
            tool_call_response("d1", "query_orders", json.dumps({"customer_id": a_id})),
            final_response("客户 A 近期订单按时履约，金额与频次平稳，未见异常波动。"),
        ],
        decisions=[
            RouteDecision(next_agents=["data"], reason="订单查询需数据 Agent。"),
            RouteDecision(reason="订单结论已足以完成任务。", is_final_ready=True),
        ],
        draft=ReportDraft(
            customer_profile="客户 A 为制造业客户，持续采购传感器产品。",
            sales_trend="近月销售平稳。",
            order_changes="订单按时履约，无异常。",
            product_changes="产品结构无变化。",
            related_knowledge="",
            possible_causes="无异常信号。",
            evidence=[EvidenceDraft(content="订单明细核验：履约与金额平稳", source_ids=[1])],
            conclusion="客户 A 订单状况正常。",
            suggestions=["继续保持常规跟踪。"],
        ),
    )
    return Supervisor(
        llm,  # type: ignore[arg-type]
        data=DataAgent(llm, session_maker=seeded_maker, trace_session_maker=seeded_maker),  # type: ignore[arg-type]
        knowledge=FakeAgent("knowledge"),
        business=FakeAgent("business"),
        trace_session_maker=seeded_maker,  # 调度埋点（agent_selected）由 Supervisor 写入
    )


def _denial_supervisor(seeded_maker, order_id: int) -> Supervisor:
    """案例 2 编排：路由 business → 真实 BusinessAgent（employee 发起退款 → 权限拒绝）→ 降级汇总。"""
    llm = SupervisorLLMStub(
        chats=[
            tool_call_response(
                "b1", "refund_order", json.dumps({"order_id": order_id, "reason": "异常订单退款"})
            )
        ],
        decisions=[
            RouteDecision(next_agents=["business"], reason="退款操作需业务 Agent。"),
            RouteDecision(reason="业务操作被拒，如实降级汇总。", is_final_ready=True),
        ],
        draft=ReportDraft(
            conclusion="退款操作因权限不足被拒绝（employee 无订单退款权限），未执行。",
        ),
    )
    return Supervisor(
        llm,  # type: ignore[arg-type]
        data=FakeAgent("data"),
        knowledge=FakeAgent("knowledge"),
        business=BusinessAgent(
            llm,  # type: ignore[arg-type]
            session_maker=seeded_maker,
            auth_session_maker=seeded_maker,
            trace_session_maker=seeded_maker,
        ),
        trace_session_maker=seeded_maker,
    )


class _ApprovalStubSupervisor:
    """审批中断替身：复刻审批流的可观察契约（agent_selected / approval_required 轨迹 +
    任务流转 waiting_approval）。返回空报告——任务已离开 running，TaskExecutor 不会覆盖终态。"""

    def __init__(self, session_maker) -> None:
        self._session_maker = session_maker

    async def run(self, task_id: int, user_id: int, query: str) -> AnalysisReport:
        from app.repositories.agent_task import AgentTaskRepository
        from app.services.task_service import TaskService as _TaskService

        async with self._session_maker() as session:
            await TraceService(session).append(
                task_id=task_id,
                event_type=AgentTraceEventType.AGENT_SELECTED,
                agent=BUSINESS_AGENT_NAME,
            )
        async with self._session_maker() as session:
            await TraceService(session).append(
                task_id=task_id,
                event_type=AgentTraceEventType.APPROVAL_REQUIRED,
                tool="refund_order",
                result_summary="订单退款待人工审批",
            )
        async with self._session_maker() as session:
            task = await AgentTaskRepository(session).get(task_id)
            await _TaskService(session).transition(task, AgentStatus.WAITING_APPROVAL)
        return AnalysisReport()


async def test_runner_three_cases_produce_metrics_json(seeded_maker, case_ids, db_session) -> None:
    """验收：3 个手工案例全链路跑通，指标 JSON 数值与逐案例 passed 断言。"""
    a_id, b_id, _, _ = case_ids
    async with seeded_maker() as session:
        order_id = await session.scalar(select(Order.id).where(Order.customer_id == b_id))
    users_by_role = await _seed_users(db_session)

    runners_by_case = {
        _CASE_DATA.id: _data_supervisor(seeded_maker, a_id),
        _CASE_PERMISSION.id: _denial_supervisor(seeded_maker, order_id),
        _CASE_HITL.id: _ApprovalStubSupervisor(seeded_maker),
    }
    runner = EvaluationRunner(seeded_maker, lambda case: runners_by_case[case.id])
    results = await runner.run_all([_CASE_DATA, _CASE_PERMISSION, _CASE_HITL], users_by_role)

    # 逐案例：全维度通过且无不匹配明细
    assert [r.case_id for r in results] == [_CASE_DATA.id, _CASE_PERMISSION.id, _CASE_HITL.id]
    for result in results:
        assert result.passed, f"{result.case_id} 未通过：{result.mismatches}"
        assert result.mismatches == ()
        assert result.latency_ms > 0

    # 指标 JSON（模型即契约，dump 即交付物）
    payload = summarize_metrics(results).model_dump(mode="json")
    assert payload["total_cases"] == 3 and payload["passed_cases"] == 3
    assert payload["agent_routing_accuracy"] == {"value": 1.0, "n": 3}
    assert payload["tool_selection_accuracy"] == {"value": 1.0, "n": 3}
    assert payload["task_completion_rate"]["n"] == 3
    assert payload["task_completion_rate"]["value"] == pytest.approx(2 / 3)  # HITL 案例不计完成
    assert payload["rag_groundedness"] == {"value": 1.0, "n": 1}  # 仅案例 1 声明证据来源
    assert payload["permission_violation_rate"] == {"value": 0.0, "n": 3}
    assert payload["hitl_correctness"] == {"value": 1.0, "n": 1}
    assert payload["average_latency_ms"]["n"] == 3
    assert payload["average_latency_ms"]["value"] > 0


async def test_runner_reports_mismatch_details(seeded_maker, case_ids, db_session) -> None:
    """负面对照：期望与实际路由不符时，结果逐条给出不匹配明细（指标可解释）。"""
    a_id, b_id, _, _ = case_ids
    users_by_role = await _seed_users(db_session)

    # 用案例 1 的脚本跑一个"期望 business"的案例：实际路由 data，应判 routing/tools 不符
    wrong_case = EvaluationCase(
        id="runner-wrong-routing",
        name="Runner 负面对照：路由期望不符",
        input="查一下客户 A 最近有哪些订单。",
        user_role="sales",
        expected_agents=["business"],
        expected_tools=["refund_order"],
        expected_outcome="COMPLETED",
        expected_status="completed",
    )
    runner = EvaluationRunner(seeded_maker, lambda case: _data_supervisor(seeded_maker, a_id))
    result = await runner.run_case(wrong_case, user_id=users_by_role["sales"])

    assert not result.passed
    assert any("路由不符" in m for m in result.mismatches)
    # 新口径：期望工具未被调用 → "缺少工具"明细（额外只读调用不扣分）
    assert any("缺少工具" in m for m in result.mismatches)


def test_tools_ok_semantics_extra_readonly_ok_unsafe_extra_fails() -> None:
    """工具判定口径（纯函数单测）：期望全覆盖 + 额外只读调用不扣分；
    缺少期望工具 / 非预期的写或高风险额外调用均判错并给出明细。"""
    from app.evaluation.runner import CaseObservation, evaluate_case

    case = EvaluationCase(
        id="runner-tool-semantics",
        name="Runner 口径单测",
        input="查一下客户 A 最近有哪些订单。",
        user_role="sales",
        expected_agents=["data"],
        expected_tools=["query_orders"],
        expected_outcome="COMPLETED",
        expected_status="completed",
    )
    base = {
        "agents": frozenset({"data"}),
        "permission_denied_tools": frozenset(),
        "approval_required": False,
        "status": "completed",
        "sources": (),
    }
    # 额外只读查询（客户名 → ID 解析等预处理）不扣分
    ok = evaluate_case(
        case,
        CaseObservation(tools=frozenset({"query_orders", "query_customers", "query_sales_data"}), **base),
        latency_ms=1,
    )
    assert ok.tools_ok and not ok.mismatches

    # 非预期的高风险额外调用判错
    unsafe = evaluate_case(
        case,
        CaseObservation(tools=frozenset({"query_orders", "refund_order"}), **base),
        latency_ms=1,
    )
    assert not unsafe.tools_ok
    assert any("非预期的高风险额外调用" in m for m in unsafe.mismatches)

    # 缺少期望工具判错
    missing = evaluate_case(
        case, CaseObservation(tools=frozenset({"query_customers"}), **base), latency_ms=1
    )
    assert not missing.tools_ok
    assert any("缺少工具" in m for m in missing.mismatches)

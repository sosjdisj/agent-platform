"""Prompt 21.2 验收：后台执行器驱动 Supervisor 全流程，任务终态落库、异常 FAILED。

- 核心验收：简单数据问题（路由派 DataAgent → 报告）跑通至 COMPLETED，
  result 落库、finished_at 写入，任务级 Trace 完整（task_started → llm_call →
  agent_finished → task_completed；专业 Agent 只写节点级事件，生命周期事件
  由执行器单点写入，不重复）；
- 编排异常 → FAILED（error 落库 + agent_finished(failed) / task_failed 埋点）；
- 执行中被取消：终态写入跳过（不覆盖取消结果）；接单前已取消：跳过执行；
- submit：后台协程跑完同一状态机。
不做：SSE（20.2 已有端到端）、运行中终止协程（21.3，见 test_task_cancel）。
"""
import asyncio

from app.agents.data import DataAgent
from app.agents.report import AnalysisReport, ReportDraft
from app.agents.state import AgentStatus
from app.agents.supervisor import RouteDecision, Supervisor
from app.models.agent import AgentTask, AgentTraceEventType
from app.repositories.agent_task import AgentTaskRepository
from app.repositories.agent_trace_event import AgentTraceEventRepository
from app.services.task_executor import TaskExecutor
from app.services.task_service import TaskService

from tests.conftest import StructuredLLMStub
from tests.test_agent_graph import final_response
from tests.test_agent_supervisor import SupervisorLLMStub


class BoomReportAgent:
    """报告生成替身：generate 必抛（验证编排异常兜底为 FAILED）。"""

    async def generate(self, query, results):
        raise RuntimeError("报告生成失败")


class CancelThenReportSupervisor:
    """编排替身：执行中把任务置为 cancelled（模拟用户取消竞态），再返回报告。"""

    def __init__(self, session_maker) -> None:
        self._maker = session_maker

    async def run(self, task_id: int, user_id: int, query: str) -> AnalysisReport:
        async with self._maker() as session:
            task = await AgentTaskRepository(session).get(task_id)
            await TaskService(session).transition(task, AgentStatus.CANCELLED)
        return AnalysisReport(conclusion="迟到的报告")


async def _make_task(session: TaskService) -> AgentTask:
    return await session.create(user_id=1, title="客户A销售额", query="客户A最近销售额怎么样")


async def _row(maker, task_id: int) -> AgentTask:
    """开短会话重读任务行（执行器经独立会话落库，需绕过本侧身份映射）。"""
    async with maker() as session:
        return await AgentTaskRepository(session).get(task_id)


async def _trace(session, task_id: int):
    return await AgentTraceEventRepository(session).list_by_task(task_id)


def _data_executor(llm, maker, seeded_maker) -> TaskExecutor:
    """纯数据问题链路的执行器：真实 Supervisor + 真实 DataAgent（LLM 脚本化）。"""
    data = DataAgent(llm, session_maker=seeded_maker, trace_session_maker=maker)
    return TaskExecutor(Supervisor(llm, data=data), maker, trace_session_maker=maker)


async def test_execute_simple_data_question_completes_with_full_trace(
    db_session, db_session_maker, seeded_maker
):
    """核心验收：数据问题全流程至 COMPLETED，result 与 finished_at 落库，Trace 完整。"""
    task = await _make_task(TaskService(db_session))
    assert task.status == AgentStatus.PENDING.value

    llm = SupervisorLLMStub(
        chats=[final_response("客户A近月销售额环比下滑。")],
        decisions=[
            RouteDecision(next_agents=["data"], reason="查询客户销售额需要数据检索。"),
            RouteDecision(reason="数据结论已足以回答。", is_final_ready=True),
        ],
        draft=ReportDraft(sales_trend="客户A近月销售额环比下滑。", conclusion="客户A销售额下滑。"),
    )
    executor = _data_executor(llm, db_session_maker, seeded_maker)

    await executor.execute(task.id, 1, task.query)

    row = await _row(db_session_maker, task.id)
    assert row.status == AgentStatus.COMPLETED.value
    assert row.finished_at is not None
    assert row.error is None
    assert "【销售趋势】客户A近月销售额环比下滑。" in row.result
    assert "【结论】客户A销售额下滑。" in row.result
    # 结构化报告（24.1）与 result 文本同源落库
    assert row.report is not None
    assert row.report["sales_trend"] == "客户A近月销售额环比下滑。"
    assert row.report["conclusion"] == "客户A销售额下滑。"
    assert row.report["evidence"] == []

    async with db_session_maker() as session:
        events = await _trace(session, task.id)
    assert [e.event_type for e in events] == [
        AgentTraceEventType.TASK_STARTED,
        AgentTraceEventType.LLM_CALL,  # DataAgent 图节点级事件（生命周期事件不重复）
        AgentTraceEventType.AGENT_FINISHED,
        AgentTraceEventType.TASK_COMPLETED,
    ]
    assert events[2].payload["status"] == "completed"
    assert events[2].payload["result_summary"] == "客户A销售额下滑。"


async def test_execute_persists_failed_when_orchestration_raises(db_session, db_session_maker):
    """编排异常兜底：FAILED + error 落库，agent_finished(failed) / task_failed 埋点。"""
    task = await _make_task(TaskService(db_session))
    llm = StructuredLLMStub([RouteDecision(reason="寒暄直接汇总。", is_final_ready=True)])
    executor = TaskExecutor(
        Supervisor(llm, report_agent=BoomReportAgent()),
        db_session_maker,
        trace_session_maker=db_session_maker,
    )

    await executor.execute(task.id, 1, task.query)

    row = await _row(db_session_maker, task.id)
    assert row.status == AgentStatus.FAILED.value
    assert row.error == "报告生成失败"
    assert row.finished_at is not None
    assert row.result is None

    async with db_session_maker() as session:
        events = await _trace(session, task.id)
    assert [e.event_type for e in events] == [
        AgentTraceEventType.TASK_STARTED,
        AgentTraceEventType.AGENT_FINISHED,
        AgentTraceEventType.TASK_FAILED,
    ]
    assert events[1].payload["status"] == "failed"
    assert events[2].payload == {"metadata": {"errors": ["报告生成失败"]}}


async def test_execute_skips_terminal_write_when_cancelled_mid_run(db_session, db_session_maker):
    """执行中被取消（cancel 只改状态）：执行器不覆盖取消结果，终态埋点不写。"""
    task = await _make_task(TaskService(db_session))
    executor = TaskExecutor(
        CancelThenReportSupervisor(db_session_maker),
        db_session_maker,
        trace_session_maker=db_session_maker,
    )

    await executor.execute(task.id, 1, task.query)

    row = await _row(db_session_maker, task.id)
    assert row.status == AgentStatus.CANCELLED.value
    assert row.result is None
    async with db_session_maker() as session:
        assert [e.event_type for e in await _trace(session, task.id)] == [
            AgentTraceEventType.TASK_STARTED
        ]


async def test_execute_skips_task_cancelled_before_pickup(db_session, db_session_maker):
    """接单前已取消：不推进状态、不写轨迹。"""
    svc = TaskService(db_session)
    task = await _make_task(svc)
    await svc.transition(task, AgentStatus.CANCELLED)

    executor = TaskExecutor(
        CancelThenReportSupervisor(db_session_maker),
        db_session_maker,
        trace_session_maker=db_session_maker,
    )
    await executor.execute(task.id, 1, task.query)

    assert (await _row(db_session_maker, task.id)).status == AgentStatus.CANCELLED.value
    async with db_session_maker() as session:
        assert await _trace(session, task.id) == []


async def test_submit_runs_to_completion_in_background(db_session, db_session_maker, seeded_maker):
    """submit：后台协程跑完同一状态机（异步启动语义），完成即清理运行表。"""
    task = await _make_task(TaskService(db_session))
    llm = SupervisorLLMStub(
        chats=[final_response("客户A销售额平稳。")],
        decisions=[RouteDecision(reason="结论已足。", is_final_ready=True)],
        draft=ReportDraft(conclusion="客户A销售额平稳。"),
    )
    executor = _data_executor(llm, db_session_maker, seeded_maker)

    await asyncio.wait_for(executor.submit(task.id, 1, task.query), 5)

    assert (await _row(db_session_maker, task.id)).status == AgentStatus.COMPLETED.value
    assert executor._running == {}

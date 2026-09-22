"""Prompt 21.3 验收：运行中取消真正终止执行 + 任务事件走 20 管线 + 全接口归属校验。

- 运行中取消（API 全链路）：cancel 路由先经 TaskService.cancel 流转（归属 + 状态机），
  再由执行器终止在跑协程——阻塞中的编排被终止而非跑完（checkpoint 每步持久化，
  节点边界安全终止），后台协程清理，终态再取消 409；
- 越权访问被拒：他人取消运行中任务 403，任务不受影响继续跑至 COMPLETED；
- 事件实时可达：执行器任务级事件（含 task_cancelled）经 TraceService → Redis
  Pub/Sub → SSE 实时推送，帧序与发布顺序一致；
- 接单前取消：仅写 task_cancelled 埋点（无 task_started）。
不做：E2E 前端验收（SSE 管线本体 20.1 / 审批与失败场景 20.2 已有各自测试）。
"""
import asyncio

from app.agents.data import DataAgent
from app.agents.report import AnalysisReport, ReportDraft
from app.agents.state import AgentStatus
from app.agents.supervisor import RouteDecision, Supervisor
from app.core.config import get_settings
from app.core.redis import RedisService
from app.models.agent import AgentTraceEventType
from app.repositories.agent_trace_event import AgentTraceEventRepository
from app.schemas.events import TraceEvent
from app.services.task_executor import TaskExecutor
from app.services.task_service import TaskService

from tests.test_agent_graph import final_response
from tests.test_agent_supervisor import SupervisorLLMStub
from tests.test_auth import PASSWORD, _error_body, login, register, register_and_login
from tests.test_task_api import CANCEL_URL, CREATE_URL, TASK_BODY, TASK_URL, _auth
from tests.test_task_events import (
    EVENTS_URL,
    SseStream,
    _parse_event,
    _read_event,
    _wait_subscribed,
)

settings = get_settings()


class GateSupervisor:
    """编排替身：进入即置 started，阻塞在 gate 上——放行则跑完，被取消则停在原地。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self.finished = False

    async def run(self, task_id: int, user_id: int, query: str) -> AnalysisReport:
        self.started.set()
        await self.gate.wait()
        self.finished = True
        return AnalysisReport(conclusion="不应产出")


def _gate_executor(supervisor, maker, **kwargs) -> TaskExecutor:
    return TaskExecutor(supervisor, maker, trace_session_maker=maker, **kwargs)


def _override_executor(executor: TaskExecutor) -> None:
    """接线执行器依赖：任务接口全链路测试（创建即执行 / 取消即终止）。"""
    from app.api.routes.tasks import get_task_executor
    from app.main import app

    app.dependency_overrides[get_task_executor] = lambda: executor


# ---------- 运行中取消（API 全链路：创建 → 执行 → 取消 → 终止） ----------


async def test_cancel_running_task_terminates_execution(api_client, db_session_maker):
    """运行中取消：编排被终止而非跑完，状态 cancelled，后台协程清理，终态再取消 409。"""
    supervisor = GateSupervisor()
    executor = _gate_executor(supervisor, db_session_maker)
    _override_executor(executor)

    token = (await register_and_login(api_client))["access_token"]
    task_id = (
        await api_client.post(CREATE_URL, json=TASK_BODY, headers=_auth(token))
    ).json()["id"]

    await asyncio.wait_for(supervisor.started.wait(), 5)
    resp = await api_client.get(TASK_URL.format(task_id=task_id), headers=_auth(token))
    assert resp.json()["status"] == AgentStatus.RUNNING.value

    resp = await api_client.post(CANCEL_URL.format(task_id=task_id), headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == AgentStatus.CANCELLED.value
    assert body["finished_at"] is not None

    assert supervisor.finished is False  # 阻塞点被终止，编排未跑完
    await asyncio.sleep(0)  # 让 done callback 跑一拍
    assert executor._running == {}  # 后台协程已清理

    resp = await api_client.post(CANCEL_URL.format(task_id=task_id), headers=_auth(token))
    assert resp.status_code == 409
    assert _error_body(resp)["error"]["code"] == "TASK_INVALID_TRANSITION"


async def test_cancel_running_task_forbidden_for_other_user(api_client, db_session_maker):
    """越权取消运行中任务 403，任务不受影响继续跑至 COMPLETED。"""
    supervisor = GateSupervisor()
    executor = _gate_executor(supervisor, db_session_maker)
    _override_executor(executor)

    owner_token = (await register_and_login(api_client))["access_token"]
    task_id = (
        await api_client.post(CREATE_URL, json=TASK_BODY, headers=_auth(owner_token))
    ).json()["id"]
    await asyncio.wait_for(supervisor.started.wait(), 5)

    await register(api_client, username="bob", email="bob@example.com")
    bob_token = (await login(api_client, "bob", PASSWORD))["access_token"]
    resp = await api_client.post(CANCEL_URL.format(task_id=task_id), headers=_auth(bob_token))
    assert resp.status_code == 403
    assert _error_body(resp)["error"]["code"] == "TASK_FORBIDDEN"

    supervisor.gate.set()  # 放行 → 任务继续跑到完成，未被越权请求波及
    await asyncio.wait_for(executor._running[task_id], 5)
    resp = await api_client.get(TASK_URL.format(task_id=task_id), headers=_auth(owner_token))
    assert resp.json()["status"] == AgentStatus.COMPLETED.value
    assert supervisor.finished is True


# ---------- 事件实时可达（执行器任务级事件走 20 管线） ----------


async def test_cancel_event_reaches_sse_in_realtime(
    api_client, db_session, db_session_maker, fake_redis
):
    """运行中取消：SSE 按发布顺序收到 task_started → task_cancelled（实时、非回放）。"""
    user_id = (await register(api_client)).json()["id"]
    await login(api_client)
    token = api_client.cookies.get(settings.sse_cookie_name)
    task = await TaskService(db_session).create(
        user_id=user_id, title=TASK_BODY["title"], query=TASK_BODY["query"]
    )

    supervisor = GateSupervisor()
    executor = _gate_executor(
        supervisor, db_session_maker, trace_redis=RedisService(fake_redis)
    )

    stream = SseStream()
    try:
        await stream.open(EVENTS_URL.format(task_id=task.id), token)
        await _wait_subscribed(stream)

        executor.submit(task.id, user_id, task.query)  # 后台起跑（gate 阻塞，勿 wait_for）
        await asyncio.wait_for(supervisor.started.wait(), 5)
        assert await executor.cancel(task.id) is True
        assert supervisor.finished is False

        frames = [
            _parse_event(await _read_event(stream)) for _ in range(2)
        ]
        assert [name for name, _ in frames] == ["task_started", "task_cancelled"]
        cancelled = TraceEvent.model_validate_json(frames[1][1])
        assert cancelled.event == "task_cancelled"
        assert cancelled.task_id == task.id
    finally:
        await stream.disconnect()


async def test_task_lifecycle_events_stream_in_realtime(
    api_client, db_session, db_session_maker, seeded_maker, fake_redis
):
    """完整任务：执行器任务级事件与节点级事件经 20 管线按序实时可达（帧序 = 发布序）。"""
    user_id = (await register(api_client)).json()["id"]
    await login(api_client)
    token = api_client.cookies.get(settings.sse_cookie_name)
    task = await TaskService(db_session).create(
        user_id=user_id, title=TASK_BODY["title"], query=TASK_BODY["query"]
    )

    llm = SupervisorLLMStub(
        chats=[final_response("客户A近月销售额环比下滑。")],
        decisions=[
            RouteDecision(next_agents=["data"], reason="查询客户销售额需要数据检索。"),
            RouteDecision(reason="数据结论已足以回答。", is_final_ready=True),
        ],
        draft=ReportDraft(sales_trend="客户A近月销售额环比下滑。", conclusion="客户A销售额下滑。"),
    )
    redis = RedisService(fake_redis)
    supervisor = Supervisor(
        llm,
        data=DataAgent(
            llm,
            session_maker=seeded_maker,
            trace_session_maker=db_session_maker,
            trace_redis=redis,
        ),
    )
    executor = TaskExecutor(
        supervisor,
        db_session_maker,
        trace_session_maker=db_session_maker,
        trace_redis=redis,
    )

    stream = SseStream()
    try:
        await stream.open(EVENTS_URL.format(task_id=task.id), token)
        await _wait_subscribed(stream)

        await asyncio.wait_for(executor.submit(task.id, user_id, task.query), 5)
        frames = [
            _parse_event(await _read_event(stream)) for _ in range(4)
        ]
        assert [name for name, _ in frames] == [
            "task_started",  # 执行器任务级
            "llm_call",  # DataAgent 节点级
            "agent_finished",  # 执行器任务级
            "task_completed",
        ]
        completed = TraceEvent.model_validate_json(frames[3][1])
        assert completed.event == "task_completed"
        assert completed.task_id == task.id
    finally:
        await stream.disconnect()


async def test_cancel_before_pickup_writes_cancelled_event_only(db_session, db_session_maker):
    """接单前取消（路由语义：先流转后终止）：仅写 task_cancelled 埋点，无 task_started。"""
    svc = TaskService(db_session)
    task = await svc.create(user_id=1, title=TASK_BODY["title"], query=TASK_BODY["query"])

    executor = _gate_executor(GateSupervisor(), db_session_maker)
    await svc.cancel(task.id, user_id=1)  # 路由侧：归属校验 + 状态机流转
    assert await executor.cancel(task.id) is False  # 未在跑，仅写埋点

    async with db_session_maker() as session:
        events = await AgentTraceEventRepository(session).list_by_task(task.id)
    assert [e.event_type for e in events] == [AgentTraceEventType.TASK_CANCELLED]

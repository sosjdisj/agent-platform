"""Prompt 21.1 验收：任务状态机 + 三个任务接口（创建 / 详情 / 取消）。

- 状态机：合法流转落库（进入终态写 finished_at），非法流转 409（TASK_INVALID_TRANSITION）
  且状态不被改写，终态不可再流转；
- 接口：Bearer 认证（401）、归属校验（403 / 404）、入参校验（422）、
  创建即 pending、取消成功后二次取消 409。
不做：实际执行 LangGraph（状态推进由执行器接线，后续 Prompt 接入）。
"""
import pytest

from app.agents.report import AnalysisReport
from app.agents.state import AgentStatus
from app.core.errors import AuthError
from app.services.task_service import TaskService

from tests.test_auth import PASSWORD, _error_body, login, register, register_and_login

CREATE_URL = "/api/tasks"
TASK_URL = "/api/tasks/{task_id}"
CANCEL_URL = "/api/tasks/{task_id}/cancel"

TASK_BODY = {"title": "分析客户A", "query": "帮我分析客户A销售额下降的原因"}


class StubExecutor:
    """任务执行器替身：仅记录 submit / cancel 入参（创建启动与取消终止的接线验证）。"""

    def __init__(self) -> None:
        self.submitted: list[tuple[int, int, str]] = []
        self.cancelled: list[int] = []

    def submit(self, task_id: int, user_id: int, query: str) -> None:
        self.submitted.append((task_id, user_id, query))

    async def cancel(self, task_id: int) -> bool:
        self.cancelled.append(task_id)
        return False


@pytest.fixture
def stub_executor(api_client):
    """覆盖任务执行器依赖：凡触发 POST /api/tasks 的用例均需接线。"""
    from app.api.routes.tasks import get_task_executor
    from app.main import app

    stub = StubExecutor()
    app.dependency_overrides[get_task_executor] = lambda: stub
    return stub


def _auth(token: str) -> dict[str, str]:
    """Bearer 头：REST 任务接口沿用常规认证（区别于事件流的 SSE Cookie）。"""
    return {"Authorization": f"Bearer {token}"}


async def _task_in(session, *path: AgentStatus):
    """创建任务并经合法流转推进到 path 指定的状态，返回任务行。"""
    svc = TaskService(session)
    task = await svc.create(user_id=1, title=TASK_BODY["title"], query=TASK_BODY["query"])
    for to_status in path:
        task = await svc.transition(task, to_status)
    return task


# ---------- 状态机（TaskService） ----------


async def test_transition_legal_chain_updates_status(db_session):
    """合法链路 pending→running→waiting_approval→running→completed，仅终态写 finished_at。"""
    svc = TaskService(db_session)
    task = await svc.create(user_id=1, title=TASK_BODY["title"], query=TASK_BODY["query"])
    assert task.status == AgentStatus.PENDING.value
    assert task.finished_at is None

    for to_status in (AgentStatus.RUNNING, AgentStatus.WAITING_APPROVAL, AgentStatus.RUNNING):
        task = await svc.transition(task, to_status)
        assert task.status == to_status.value
        assert task.finished_at is None  # 等待审批 / 恢复续跑均非终态

    task = await svc.transition(task, AgentStatus.COMPLETED)
    assert task.status == AgentStatus.COMPLETED.value
    assert task.finished_at is not None


async def test_transition_running_to_failed_writes_finished_at(db_session):
    """running → failed：异常终态同样落 finished_at。"""
    task = await _task_in(db_session, AgentStatus.RUNNING)
    task = await TaskService(db_session).transition(task, AgentStatus.FAILED)
    assert task.status == AgentStatus.FAILED.value
    assert task.finished_at is not None


@pytest.mark.parametrize(
    ("path", "to_status"),
    [
        # path 为到达起始状态所经的合法流转；断言从该状态出发的非法去向被拒绝
        ((), AgentStatus.WAITING_APPROVAL),  # 未接单不得等待审批
        ((), AgentStatus.COMPLETED),  # 未执行不得完成
        ((), AgentStatus.FAILED),
        ((AgentStatus.RUNNING,), AgentStatus.PENDING),  # 状态机不回退
        ((AgentStatus.RUNNING, AgentStatus.WAITING_APPROVAL), AgentStatus.COMPLETED),
        ((AgentStatus.RUNNING, AgentStatus.COMPLETED), AgentStatus.CANCELLED),
        ((AgentStatus.RUNNING, AgentStatus.FAILED), AgentStatus.RUNNING),
        ((AgentStatus.RUNNING, AgentStatus.CANCELLED), AgentStatus.RUNNING),
    ],
)
async def test_transition_rejects_illegal_moves(db_session, path, to_status):
    """非法流转 409 + TASK_INVALID_TRANSITION，状态不被改写。"""
    task = await _task_in(db_session, *path)
    with pytest.raises(AuthError) as exc_info:
        await TaskService(db_session).transition(task, to_status)
    assert exc_info.value.status_code == 409
    assert exc_info.value.error_code == "TASK_INVALID_TRANSITION"
    assert task.status == (path[-1] if path else AgentStatus.PENDING).value


async def test_cancel_allowed_from_every_non_terminal_state(db_session):
    """pending / running / waiting_approval 均可取消，取消后写 finished_at。"""
    for path in (
        (),
        (AgentStatus.RUNNING,),
        (AgentStatus.RUNNING, AgentStatus.WAITING_APPROVAL),
    ):
        task = await _task_in(db_session, *path)
        task = await TaskService(db_session).transition(task, AgentStatus.CANCELLED)
        assert task.status == AgentStatus.CANCELLED.value
        assert task.finished_at is not None


# ---------- 接口（POST / GET / cancel） ----------


async def test_list_tasks_requires_auth(api_client):
    """未登录列表 → 401 + TOKEN_MISSING。"""
    resp = await api_client.get(CREATE_URL)
    assert resp.status_code == 401
    assert _error_body(resp)["error"]["code"] == "TOKEN_MISSING"


async def test_list_tasks_returns_owned_only_desc(api_client, stub_executor):
    """列表：只含本人任务，按创建时间倒序（最新在前），字段与详情一致。"""
    (await register(api_client))
    token = (await login(api_client))["access_token"]
    first_id = (
        await api_client.post(CREATE_URL, json=TASK_BODY, headers=_auth(token))
    ).json()["id"]
    second_id = (
        await api_client.post(CREATE_URL, json=TASK_BODY, headers=_auth(token))
    ).json()["id"]

    await register(api_client, username="bob", email="bob@example.com")
    bob_token = (await login(api_client, "bob", PASSWORD))["access_token"]
    await api_client.post(CREATE_URL, json=TASK_BODY, headers=_auth(bob_token))

    resp = await api_client.get(CREATE_URL, headers=_auth(token))
    assert resp.status_code == 200
    items = resp.json()
    assert [t["id"] for t in items] == [second_id, first_id]
    assert all(t["user_id"] == items[0]["user_id"] and t["status"] == "pending" for t in items)


async def test_list_tasks_pagination(api_client, stub_executor):
    """列表分页：limit 截断 + offset 跳过（offset=1 取到次新的一条）。"""
    (await register(api_client))
    token = (await login(api_client))["access_token"]
    ids = [
        (await api_client.post(CREATE_URL, json=TASK_BODY, headers=_auth(token))).json()["id"]
        for _ in range(3)
    ]

    resp = await api_client.get(CREATE_URL, params={"limit": 2}, headers=_auth(token))
    assert [t["id"] for t in resp.json()] == ids[::-1][:2]

    resp = await api_client.get(CREATE_URL, params={"offset": 1, "limit": 2}, headers=_auth(token))
    assert [t["id"] for t in resp.json()] == ids[::-1][1:]


async def test_create_task_requires_auth(api_client, stub_executor):
    """未登录创建任务 → 401 + TOKEN_MISSING。"""
    resp = await api_client.post(CREATE_URL, json=TASK_BODY)
    assert resp.status_code == 401
    assert _error_body(resp)["error"]["code"] == "TOKEN_MISSING"
    assert stub_executor.submitted == []  # 未创建成功不启动执行


async def test_create_task_creates_pending_row_and_submits(api_client, stub_executor):
    """创建成功 201：归属当前用户、status=pending，并异步提交执行器（task_id/user/query）。"""
    user = (await register(api_client)).json()
    token = (await login(api_client))["access_token"]

    resp = await api_client.post(CREATE_URL, json=TASK_BODY, headers=_auth(token))
    assert resp.status_code == 201
    body = resp.json()
    assert body["user_id"] == user["id"]
    assert body["title"] == TASK_BODY["title"]
    assert body["query"] == TASK_BODY["query"]
    assert body["status"] == "pending"
    assert body["result"] is None
    assert body["error"] is None
    assert body["finished_at"] is None

    assert stub_executor.submitted == [(body["id"], user["id"], TASK_BODY["query"])]


async def test_create_task_rejects_blank_payload(api_client, stub_executor):
    """入参校验：空标题 / 空查询 422。"""
    token = (await register_and_login(api_client))["access_token"]
    for body in ({"title": "", "query": "q"}, {"title": "t", "query": ""}):
        resp = await api_client.post(CREATE_URL, json=body, headers=_auth(token))
        assert resp.status_code == 422
    assert stub_executor.submitted == []


async def test_get_task_auth_and_ownership(api_client, stub_executor):
    """详情：未登录 401、任务不存在 404、他人任务 403、本人任务 200。"""
    (await register(api_client))
    token = (await login(api_client))["access_token"]
    task_id = (
        await api_client.post(CREATE_URL, json=TASK_BODY, headers=_auth(token))
    ).json()["id"]

    resp = await api_client.get(TASK_URL.format(task_id=task_id))
    assert resp.status_code == 401

    await register(api_client, username="bob", email="bob@example.com")
    bob_token = (await login(api_client, "bob", PASSWORD))["access_token"]
    resp = await api_client.get(TASK_URL.format(task_id=task_id), headers=_auth(bob_token))
    assert resp.status_code == 403
    assert _error_body(resp)["error"]["code"] == "TASK_FORBIDDEN"

    resp = await api_client.get(TASK_URL.format(task_id=424242), headers=_auth(token))
    assert resp.status_code == 404
    assert _error_body(resp)["error"]["code"] == "TASK_NOT_FOUND"

    resp = await api_client.get(TASK_URL.format(task_id=task_id), headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["id"] == task_id


async def test_get_task_returns_structured_report(api_client, db_session, stub_executor):
    """详情透出结构化报告 report（24.1 报告页数据源）：未完成为 null，完成后随详情返回。"""
    user_id = (await register(api_client)).json()["id"]
    token = (await login(api_client))["access_token"]
    task_id = (
        await api_client.post(CREATE_URL, json=TASK_BODY, headers=_auth(token))
    ).json()["id"]

    resp = await api_client.get(TASK_URL.format(task_id=task_id), headers=_auth(token))
    assert resp.json()["report"] is None  # 未完成任务无报告

    svc = TaskService(db_session)
    task = await svc.get_owned(task_id, user_id)
    await svc.transition(task, AgentStatus.RUNNING)
    report = AnalysisReport(conclusion="客户A销售额下滑。", suggestions=["回访客户"])
    await svc.transition(
        task, AgentStatus.COMPLETED, result="【结论】客户A销售额下滑。",
        report=report.model_dump(mode="json"),
    )

    body = (
        await api_client.get(TASK_URL.format(task_id=task_id), headers=_auth(token))
    ).json()
    assert body["report"]["conclusion"] == "客户A销售额下滑。"
    assert body["report"]["suggestions"] == ["回访客户"]
    assert body["report"]["evidence"] == []


async def test_cancel_success_then_terminal_conflict(api_client, stub_executor):
    """取消：pending → cancelled（写 finished_at）；终态再取消 409，状态不被改写。"""
    token = (await register_and_login(api_client))["access_token"]
    task_id = (
        await api_client.post(CREATE_URL, json=TASK_BODY, headers=_auth(token))
    ).json()["id"]

    resp = await api_client.post(CANCEL_URL.format(task_id=task_id), headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["finished_at"] is not None
    assert stub_executor.cancelled == [task_id]  # 流转成功后交执行器终止在跑协程（21.3）

    # 终态取消在状态机即被拒，不再触达执行器
    stub_executor.cancelled.clear()
    resp = await api_client.post(CANCEL_URL.format(task_id=task_id), headers=_auth(token))
    assert resp.status_code == 409
    assert _error_body(resp)["error"]["code"] == "TASK_INVALID_TRANSITION"
    assert stub_executor.cancelled == []
    resp = await api_client.get(TASK_URL.format(task_id=task_id), headers=_auth(token))
    assert resp.json()["status"] == "cancelled"


async def test_cancel_forbidden_missing_and_completed_conflict(api_client, db_session, stub_executor):
    """取消：他人任务 403、任务不存在 404、已完成任务 409（终态不可流转）。"""
    user_id = (await register(api_client)).json()["id"]
    token = (await login(api_client))["access_token"]
    task_id = (
        await api_client.post(CREATE_URL, json=TASK_BODY, headers=_auth(token))
    ).json()["id"]

    await register(api_client, username="bob", email="bob@example.com")
    bob_token = (await login(api_client, "bob", PASSWORD))["access_token"]
    resp = await api_client.post(CANCEL_URL.format(task_id=task_id), headers=_auth(bob_token))
    assert resp.status_code == 403
    assert _error_body(resp)["error"]["code"] == "TASK_FORBIDDEN"

    resp = await api_client.post(CANCEL_URL.format(task_id=424242), headers=_auth(token))
    assert resp.status_code == 404
    assert _error_body(resp)["error"]["code"] == "TASK_NOT_FOUND"

    # 经状态机把任务推进到终态 completed（执行器接线前的合法路径），再取消须 409
    svc = TaskService(db_session)
    task = await svc.get_owned(task_id, user_id)
    await svc.transition(task, AgentStatus.RUNNING)
    await svc.transition(task, AgentStatus.COMPLETED)

    resp = await api_client.post(CANCEL_URL.format(task_id=task_id), headers=_auth(token))
    assert resp.status_code == 409
    assert _error_body(resp)["error"]["code"] == "TASK_INVALID_TRANSITION"

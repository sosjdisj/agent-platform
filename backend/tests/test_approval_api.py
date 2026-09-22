"""Prompt 23.3 验收：审批中心 API（列表 / 决定）。

- 列表：仅含本人（requester）审批，最新在前，task_title 批量补全；
- 决定前置：401 / 审批不存在 404 / 他人任务 403 / 任务非 waiting_approval 409；
- approved → 审批 approved + 任务回 running + 执行器 resume；
  rejected → 审批 rejected + 任务 cancelled（写 finished_at）+ 执行器补 task_cancelled 埋点；
- 决定后任务已离开 waiting，再次决定 409。
"""
import pytest

from app.agents.state import AgentStatus
from app.core.errors import AuthError
from app.mcp.base import RiskLevel
from app.services.approval_service import ApprovalService
from app.services.task_service import TaskService

from tests.test_auth import PASSWORD, _error_body, login, register

LIST_URL = "/api/approvals"
DECIDE_URL = "/api/approvals/{approval_id}/decide"


class StubExecutor:
    """任务执行器替身：记录 resume / cancel 入参（决定后驱动任务的接线验证）。"""

    def __init__(self) -> None:
        self.resumed: list[tuple[int, int]] = []
        self.cancelled: list[int] = []

    async def resume(self, task_id: int, user_id: int):
        self.resumed.append((task_id, user_id))

    async def cancel(self, task_id: int) -> bool:
        self.cancelled.append(task_id)
        return False


@pytest.fixture
def stub_executor(api_client):
    """覆盖任务执行器依赖：决定接口触发的恢复 / 终止埋点均需接线。"""
    from app.api.routes.tasks import get_task_executor
    from app.main import app

    stub = StubExecutor()
    app.dependency_overrides[get_task_executor] = lambda: stub
    return stub


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _waiting_task_with_approval(session, user_id: int, *, tool: str = "refund_order"):
    """构造 waiting_approval 任务 + pending 审批记录（真实中断产生的最小形态）。"""
    svc = TaskService(session)
    task = await svc.create(user_id=user_id, title="退款任务", query="退款")
    task = await svc.transition(task, AgentStatus.RUNNING)
    task = await svc.transition(task, AgentStatus.WAITING_APPROVAL)
    approval = await ApprovalService(session).create(
        task_id=task.id,
        requester_id=user_id,
        tool=tool,
        parameter_summary='{"order_id": 1, "reason": "质量问题"}',
        risk_level=RiskLevel.HIGH,
    )
    return task, approval


# ---------- 列表 ----------


async def test_list_approvals_requires_auth(api_client):
    """未登录列表 → 401 + TOKEN_MISSING。"""
    resp = await api_client.get(LIST_URL)
    assert resp.status_code == 401
    assert _error_body(resp)["error"]["code"] == "TOKEN_MISSING"


async def test_list_approvals_returns_owned_only_with_title(api_client, db_session):
    """列表：仅本人审批（最新在前），task_title / risk_level / parameter_summary 透出。"""
    user = (await register(api_client)).json()
    token = (await login(api_client))["access_token"]
    first_task, first_approval = await _waiting_task_with_approval(db_session, user["id"])
    second_task, second_approval = await _waiting_task_with_approval(db_session, user["id"])

    bob_user = (await register(api_client, username="bob", email="bob@example.com")).json()
    bob_token = (await login(api_client, "bob", PASSWORD))["access_token"]
    _bob_task, bob_approval = await _waiting_task_with_approval(db_session, bob_user["id"])

    resp = await api_client.get(LIST_URL, headers=_auth(token))
    assert resp.status_code == 200
    items = resp.json()
    assert [i["id"] for i in items] == [second_approval.id, first_approval.id]
    assert items[0]["task_title"] == second_task.title
    assert items[0]["tool"] == "refund_order"
    assert items[0]["risk_level"] == "high"
    assert items[0]["parameter_summary"] == '{"order_id": 1, "reason": "质量问题"}'
    assert items[0]["status"] == "pending"
    assert items[0]["requester_id"] == user["id"]

    bob_resp = await api_client.get(LIST_URL, headers=_auth(bob_token))
    assert [i["id"] for i in bob_resp.json()] == [bob_approval.id]


# ---------- 决定 ----------


async def test_decide_auth_not_found_and_forbidden(api_client, db_session, stub_executor):
    """决定：未登录 401、审批不存在 404、他人任务 403。"""
    (await register(api_client))
    token = (await login(api_client))["access_token"]
    task, approval = await _waiting_task_with_approval(db_session, 1001)

    resp = await api_client.post(DECIDE_URL.format(approval_id=approval.id))
    assert resp.status_code == 401

    resp = await api_client.post(
        DECIDE_URL.format(approval_id=424242),
        json={"decision": "approved"},
        headers=_auth(token),
    )
    assert resp.status_code == 404
    assert _error_body(resp)["error"]["code"] == "APPROVAL_NOT_FOUND"

    await register(api_client, username="bob", email="bob@example.com")
    bob_token = (await login(api_client, "bob", PASSWORD))["access_token"]
    resp = await api_client.post(
        DECIDE_URL.format(approval_id=approval.id),
        json={"decision": "approved"},
        headers=_auth(bob_token),
    )
    assert resp.status_code == 403
    assert _error_body(resp)["error"]["code"] == "TASK_FORBIDDEN"
    assert stub_executor.resumed == [] and stub_executor.cancelled == []


async def test_decide_rejects_task_not_waiting(api_client, db_session, stub_executor):
    """任务非 waiting_approval（如编排已降级完成的存量记录）→ 409 + TASK_NOT_WAITING。"""
    (await register(api_client))
    token = (await login(api_client))["access_token"]
    svc = TaskService(db_session)
    task = await svc.create(user_id=1, title="t", query="q")
    approval = await ApprovalService(db_session).create(
        task_id=task.id,
        requester_id=1,
        tool="refund_order",
        parameter_summary="{}",
        risk_level=RiskLevel.HIGH,
    )

    resp = await api_client.post(
        DECIDE_URL.format(approval_id=approval.id),
        json={"decision": "approved"},
        headers=_auth(token),
    )
    assert resp.status_code == 409
    assert _error_body(resp)["error"]["code"] == "TASK_NOT_WAITING"


async def test_approve_resumes_task(api_client, db_session, stub_executor):
    """批准：审批 approved（含决定人与时间）、任务回 running、执行器 resume 接线。"""
    user = (await register(api_client)).json()
    token = (await login(api_client))["access_token"]
    task, approval = await _waiting_task_with_approval(db_session, user["id"])

    resp = await api_client.post(
        DECIDE_URL.format(approval_id=approval.id),
        json={"decision": "approved", "comment": "同意"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["decision"] == "approved"
    assert body["comment"] == "同意"
    assert body["decided_at"] is not None

    assert stub_executor.resumed == [(task.id, user["id"])]
    await db_session.refresh(task)
    assert task.status == AgentStatus.RUNNING.value


async def test_reject_terminates_task(api_client, db_session, stub_executor):
    """拒绝：审批 rejected、任务 cancelled（终态写 finished_at）、执行器补取消埋点。"""
    user = (await register(api_client)).json()
    token = (await login(api_client))["access_token"]
    task, approval = await _waiting_task_with_approval(db_session, user["id"])

    resp = await api_client.post(
        DECIDE_URL.format(approval_id=approval.id),
        json={"decision": "rejected"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert stub_executor.cancelled == [task.id]

    await db_session.refresh(task)
    assert task.status == AgentStatus.CANCELLED.value
    assert task.finished_at is not None


async def test_decide_twice_conflicts(api_client, db_session, stub_executor):
    """决定后任务已离开 waiting_approval，再次决定 409 + TASK_NOT_WAITING。"""
    user = (await register(api_client)).json()
    token = (await login(api_client))["access_token"]
    task, approval = await _waiting_task_with_approval(db_session, user["id"])

    first = await api_client.post(
        DECIDE_URL.format(approval_id=approval.id),
        json={"decision": "approved"},
        headers=_auth(token),
    )
    assert first.status_code == 200

    second = await api_client.post(
        DECIDE_URL.format(approval_id=approval.id),
        json={"decision": "rejected"},
        headers=_auth(token),
    )
    assert second.status_code == 409
    assert _error_body(second)["error"]["code"] == "TASK_NOT_WAITING"

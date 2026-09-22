"""Prompt 23.4 验收：任务轨迹回放 REST（keyset 分页）。

- 认证 / 归属：401 未登录、404 不存在、403 非本人；
- 返回最新在前（id 倒序），payload 扁平字段 + id + timestamp 透出；
- keyset 分页：before_id 游标翻页无重叠，拼接 == 全量倒序（慢查询页仍流畅）。
"""
import pytest

from app.models.agent import AgentTraceEventType
from app.services.trace_service import TraceService

from tests.test_auth import PASSWORD, _error_body, login, register

TRACE_URL = "/api/tasks/{task_id}/trace"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_task_with_trace(session, user_id: int, *, count: int):
    """建任务并追加 count 条轨迹事件（timestamp 递增、落库序即时间序）。"""
    from datetime import datetime, timedelta, timezone

    from app.services.task_service import TaskService

    task = await TaskService(session).create(
        user_id=user_id, title="轨迹任务", query="分析客户 A"
    )
    svc = TraceService(session)
    base = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(count):
        await svc.append(
            task_id=task.id,
            event_type=AgentTraceEventType.TOOL_CALLED if i % 2 else AgentTraceEventType.AGENT_SELECTED,
            agent="DataAgent" if i % 2 else "Supervisor",
            tool="query_orders" if i % 2 else None,
            round=1 + i // 2,
            duration_ms=100 + i,
            status="ok" if i % 2 else None,
            parameter_summary='{"customer_id": 1}' if i % 2 else None,
            result_summary=f"事件 {i}" if i % 2 else None,
            timestamp=base + timedelta(seconds=i),
        )
    return task


# ---------- 认证 / 归属 ----------


async def test_trace_requires_auth(api_client, db_session):
    """未登录回放 → 401 + TOKEN_MISSING。"""
    task = await _seed_task_with_trace(db_session, 1, count=1)
    resp = await api_client.get(TRACE_URL.format(task_id=task.id))
    assert resp.status_code == 401
    assert _error_body(resp)["error"]["code"] == "TOKEN_MISSING"


async def test_trace_not_found_and_forbidden(api_client, db_session):
    """回放：任务不存在 404、他人任务 403。"""
    (await register(api_client))
    token = (await login(api_client))["access_token"]
    task = await _seed_task_with_trace(db_session, 1, count=1)

    resp = await api_client.get(TRACE_URL.format(task_id=424242), headers=_auth(token))
    assert resp.status_code == 404
    assert _error_body(resp)["error"]["code"] == "TASK_NOT_FOUND"

    await register(api_client, username="bob", email="bob@example.com")
    bob_token = (await login(api_client, "bob", PASSWORD))["access_token"]
    resp = await api_client.get(TRACE_URL.format(task_id=task.id), headers=_auth(bob_token))
    assert resp.status_code == 403
    assert _error_body(resp)["error"]["code"] == "TASK_FORBIDDEN"


# ---------- 回放 / 分页 ----------


async def test_trace_returns_newest_first_with_fields(api_client, db_session):
    """回放：最新在前，payload 扁平字段 + id + timestamp 一致透出。"""
    user = (await register(api_client)).json()
    token = (await login(api_client))["access_token"]
    task = await _seed_task_with_trace(db_session, user["id"], count=4)

    resp = await api_client.get(TRACE_URL.format(task_id=task.id), headers=_auth(token))
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 4
    assert [i["id"] for i in items] == sorted((i["id"] for i in items), reverse=True)
    newest = items[-1]  # 落库序最早的是 agent_selected
    assert newest["event"] == "agent_selected"
    assert newest["agent"] == "Supervisor"
    assert newest["duration_ms"] == 100
    tool_call = items[0]  # 最新一条是 tool_called（i=3）
    assert tool_call["event"] == "tool_called"
    assert tool_call["tool"] == "query_orders"
    assert tool_call["result_summary"] == "事件 3"
    assert tool_call["timestamp"] > newest["timestamp"]


async def test_trace_keyset_pagination_no_overlap(api_client, db_session):
    """分页：before_id 游标翻页无重叠，三页拼接 == 全量 id 倒序。"""
    user = (await register(api_client)).json()
    token = (await login(api_client))["access_token"]
    task = await _seed_task_with_trace(db_session, user["id"], count=25)
    url = TRACE_URL.format(task_id=task.id)

    pages: list[list[dict]] = []
    before_id: int | None = None
    while True:
        params = {"limit": 10, **({"before_id": before_id} if before_id else {})}
        resp = await api_client.get(url, params=params, headers=_auth(token))
        assert resp.status_code == 200
        page = resp.json()
        if not page:
            break
        pages.append(page)
        before_id = page[-1]["id"]
        if len(page) < 10:
            break

    assert [len(p) for p in pages] == [10, 10, 5]
    all_ids = [i["id"] for p in pages for i in p]
    assert len(all_ids) == len(set(all_ids))  # 无重叠
    assert all_ids == sorted(all_ids, reverse=True)  # 拼接即全量倒序

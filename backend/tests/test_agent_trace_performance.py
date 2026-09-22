"""AgentTraceEvent 性能测试：插入 1k 行后按 task_id 查询 < 100ms。

仅用于验证 (task_id, timestamp) 联合索引的查询性能，
不做任何业务写入代码（遵循 Prompt 2.5 边界）。
"""
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select

from app.models.agent import AgentTask, AgentTraceEvent, AgentTraceEventType
from app.models.user import User

EVENT_COUNT = 1000
QUERY_BUDGET_MS = 100


async def test_trace_query_by_task_under_100ms(db_session):
    # 准备：1 个用户 + 1 个任务
    user = User(username="perf_user", email="perf@test.com", hashed_password="x")
    db_session.add(user)
    await db_session.flush()
    task = AgentTask(user_id=user.id, title="性能测试任务", query="分析客户A")
    db_session.add(task)
    await db_session.flush()

    # 批量插入 1000 条轨迹事件
    base_time = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        {
            "task_id": task.id,
            "agent": "main",
            "event_type": AgentTraceEventType.LLM_CALL,
            "payload": {"seq": i},
            "timestamp": base_time + timedelta(milliseconds=i),
        }
        for i in range(EVENT_COUNT)
    ]
    await db_session.execute(insert(AgentTraceEvent), rows)

    # 计时：按 task_id 查询（命中联合索引）
    start = time.perf_counter()
    result = await db_session.scalars(
        select(AgentTraceEvent)
        .where(AgentTraceEvent.task_id == task.id)
        .order_by(AgentTraceEvent.timestamp)
    )
    events = result.all()
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert len(events) == EVENT_COUNT
    assert events[0].timestamp < events[-1].timestamp  # 按时间升序
    assert elapsed_ms < QUERY_BUDGET_MS, f"查询耗时 {elapsed_ms:.1f}ms 超过 {QUERY_BUDGET_MS}ms"

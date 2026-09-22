"""Prompt 19.1：TraceService 纯追加写入与按任务回放排序。

- 全部事件类型均可经 append 落库，业务字段进入 payload（非空键，空字段不产生键）；
- list_by_task 按 (timestamp, id) 升序回放，命中 (task_id, timestamp) 索引；
- 追加不改写既有事件（纯追加、无 UPDATE 路径），回放按任务隔离；
- 20.2：接线 redis 时 append 落库同时发布同构事件消息到任务频道。
"""
import json
from datetime import datetime, timedelta, timezone

from app.core.redis import RedisService
from app.models.agent import AgentTraceEventType
from app.services.task_events import task_channel
from app.services.trace_service import TraceService


async def test_append_all_event_types(db_session):
    """全部事件类型写入：列与 payload 字段按约定落库，未传字段不进 payload。"""
    svc = TraceService(db_session)

    full = await svc.append(
        task_id=701,
        event_type=AgentTraceEventType.TOOL_CALLED,
        agent="crm",
        tool="query_orders",
        round=2,
        duration_ms=35,
        status="success",
        parameter_summary='{"customer_id": 1}',
        result_summary="3 orders",
        error_code=None,
        metadata={"attempt": 1},
    )
    for event_type in AgentTraceEventType:  # 其余类型各写一条最小事件
        last = await svc.append(task_id=701, event_type=event_type, agent="crm")

    events = await svc.list_by_task(701)
    assert len(events) == len(list(AgentTraceEventType)) + 1
    assert {e.event_type for e in events} == set(AgentTraceEventType)

    assert full.task_id == 701
    assert full.agent == "crm"
    assert full.timestamp is not None
    assert full.payload == {
        "tool": "query_orders",
        "round": 2,
        "duration_ms": 35,
        "status": "success",
        "parameter_summary": '{"customer_id": 1}',
        "result_summary": "3 orders",
        "metadata": {"attempt": 1},
    }
    assert last.payload is None  # 最小事件：无业务字段则 payload 为空


async def test_list_by_task_orders_by_timestamp_then_id(db_session):
    """回放排序：timestamp 升序为主，同刻事件按落库次序（id 兜底）。"""
    svc = TraceService(db_session)
    now = datetime.now(timezone.utc)

    first = await svc.append(
        task_id=702, event_type=AgentTraceEventType.TASK_STARTED, timestamp=now
    )
    earlier = await svc.append(  # id 更大但时间戳更早 → 排最前
        task_id=702,
        event_type=AgentTraceEventType.LLM_CALL,
        timestamp=now - timedelta(seconds=1),
    )
    third = await svc.append(
        task_id=702, event_type=AgentTraceEventType.AGENT_SELECTED, timestamp=now
    )
    same_ts = await svc.append(  # 与 third 同刻 → id 兜底排其后
        task_id=702, event_type=AgentTraceEventType.TASK_COMPLETED, timestamp=now
    )

    assert [e.id for e in await svc.list_by_task(702)] == [
        earlier.id,
        first.id,
        third.id,
        same_ts.id,
    ]


async def test_append_never_mutates_existing_events(db_session):
    """纯追加：后续写入不改变既有事件的内容。"""
    svc = TraceService(db_session)
    first = await svc.append(
        task_id=703,
        event_type=AgentTraceEventType.TASK_STARTED,
        agent="supervisor",
        parameter_summary="q",
    )
    snapshot = (first.id, first.payload, first.timestamp, first.event_type)

    await svc.append(
        task_id=703, event_type=AgentTraceEventType.TASK_FAILED, error_code="Boom"
    )

    reloaded = (await svc.list_by_task(703))[0]
    assert (reloaded.id, reloaded.payload, reloaded.timestamp, reloaded.event_type) == snapshot


async def test_list_by_task_scopes_to_task(db_session):
    """回放按任务隔离：只返回目标任务的轨迹。"""
    svc = TraceService(db_session)
    await svc.append(task_id=704, event_type=AgentTraceEventType.TASK_STARTED)
    await svc.append(task_id=705, event_type=AgentTraceEventType.TASK_STARTED)
    await svc.append(task_id=704, event_type=AgentTraceEventType.TASK_COMPLETED)

    events = await svc.list_by_task(704)
    assert [e.event_type for e in events] == [
        AgentTraceEventType.TASK_STARTED,
        AgentTraceEventType.TASK_COMPLETED,
    ]


async def _subscribe(fake_redis, task_id: int):
    """订阅任务频道并读出 SUBSCRIBE 确认帧：确认到达即订阅已在服务端登记（此后发布必达）。"""
    pubsub = fake_redis.pubsub()
    await pubsub.subscribe(task_channel(task_id))
    confirmation = await pubsub.get_message(timeout=2)
    assert confirmation is not None and confirmation["type"] == "subscribe"
    return pubsub


async def test_append_publishes_event_message(db_session, fake_redis):
    """20.2：接线 redis 后 append 落库同时向任务频道发布同构事件消息（空字段不进消息）。"""
    pubsub = await _subscribe(fake_redis, 710)
    svc = TraceService(db_session, redis=RedisService(fake_redis))

    await svc.append(
        task_id=710,
        event_type=AgentTraceEventType.TOOL_CALLED,
        agent="crm",
        tool="query_orders",
        round=2,
        status="success",
        metadata={"attempt": 1},
    )

    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
    assert message is not None
    assert json.loads(message["data"]) == {
        "event": "tool_called",
        "task_id": 710,
        "agent": "crm",
        "tool": "query_orders",
        "round": 2,
        "status": "success",
        "metadata": {"attempt": 1},
    }


async def test_append_without_redis_skips_publish(db_session, fake_redis):
    """未接线 redis（缺省）：只落库不发布（19.2 存量行为不变）。"""
    pubsub = await _subscribe(fake_redis, 711)
    svc = TraceService(db_session)

    await svc.append(task_id=711, event_type=AgentTraceEventType.TASK_STARTED)

    assert await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2) is None


async def test_purge_before_deletes_only_expired(db_session):
    """保留期清理（19.2）：仅删除 cutoff 之前的事件并返回删除条数，新事件不受影响。"""
    svc = TraceService(db_session)
    now = datetime.now(timezone.utc)
    await svc.append(
        task_id=901,
        event_type=AgentTraceEventType.TASK_STARTED,
        timestamp=now - timedelta(days=40),
    )
    await svc.append(
        task_id=902,
        event_type=AgentTraceEventType.LLM_CALL,
        timestamp=now - timedelta(days=31),
    )
    fresh = await svc.append(
        task_id=901,
        event_type=AgentTraceEventType.TASK_COMPLETED,
        timestamp=now - timedelta(days=1),
    )

    removed = await svc.purge_before(now - timedelta(days=30))

    assert removed == 2
    assert [e.id for e in await svc.list_by_task(901)] == [fresh.id]
    assert await svc.list_by_task(902) == []

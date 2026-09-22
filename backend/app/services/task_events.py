"""任务事件推送管线（20.1）：Redis Pub/Sub 订阅 → SSE 转发。

- 频道命名单一来源 task_channel()：订阅端（本模块 SSE 流）与发布端（20.2 接线
  Trace / 审批事件）共用，禁止各自拼字符串；
- 消息体约定：频道传递事件 JSON 文本，其 `event` 字段即 SSE 事件名
  （结构契约见 app/schemas/events.py），非 JSON 文本降级为 message 事件透传；
- SSE 流：订阅后转发事件帧，空档期发送注释行心跳保活；客户端断开时生成器在
  取消态收尾，清理须屏蔽取消（shield）完整退订，防止泄漏 Redis 订阅。
不做：历史事件回放（Last-Event-ID），真实 Trace 事件发布接线属 20.2。
"""
import contextlib
import json
from collections.abc import AsyncIterator

import anyio

from app.core.redis import RedisService

# 频道名与 auth:refresh:* / lock:* 命名风格一致，按任务隔离便于权限对齐
_TASK_CHANNEL_TEMPLATE = "agent:task:{task_id}:events"

SSE_HEARTBEAT = b": heartbeat\n\n"


def task_channel(task_id: int) -> str:
    """任务事件频道名（发布方与订阅方共用的单一来源）。"""
    return _TASK_CHANNEL_TEMPLATE.format(task_id=task_id)


def sse_frame(event: str, data: str) -> bytes:
    """单条 SSE 帧：event 字段供前端 EventSource 按类型监听。"""
    return f"event: {event}\ndata: {data}\n\n".encode()


def _event_name(data: str) -> str:
    """从事件 JSON 提取 SSE 事件名；非 JSON 或缺 event 字段降级为 message。"""
    try:
        return json.loads(data).get("event") or "message"
    except (ValueError, AttributeError):
        return "message"


async def _release(pubsub) -> None:
    """关闭订阅句柄（连接关闭即退订，幂等容忍异常）。"""
    with contextlib.suppress(Exception):
        await pubsub.aclose()


async def task_event_stream(
    service: RedisService, task_id: int, heartbeat_interval: float
) -> AsyncIterator[bytes]:
    """订阅任务频道并转发为 SSE 字节流，客户端断开（生成器被取消）时退订收尾。"""
    channel = task_channel(task_id)
    pubsub = service.pubsub()
    await pubsub.subscribe(channel)
    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=heartbeat_interval
            )
            if message is None:
                yield SSE_HEARTBEAT
            else:
                data = message["data"]
                yield sse_frame(_event_name(data), data)
    finally:
        # 客户端断开时生成器处于取消态，屏蔽取消保证退订完整执行（防泄漏订阅）
        with anyio.CancelScope(shield=True):
            await _release(pubsub)

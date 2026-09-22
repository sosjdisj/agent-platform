"""RedisService 单元测试：全部方法覆盖 + 连接异常统一 + 分布式锁行为（获取 / 重入拒绝 / 过期释放）。"""
import asyncio

import pytest
from fakeredis import FakeAsyncRedis
from redis.asyncio import Redis

from app.core.redis import RedisService, RedisUnavailableError


@pytest.fixture
async def service() -> RedisService:
    """基于 fakeredis 的 RedisService，测试结束自动清理。"""
    client = FakeAsyncRedis(decode_responses=True)
    yield RedisService(client)
    await client.aclose()


async def test_get_set_delete_expire(service: RedisService):
    """字符串读写 / TTL / 删除。"""
    # set / get / 缺失键
    await service.set("k", "v")
    assert await service.get("k") == "v"
    assert await service.get("missing") is None

    # set 带 TTL
    await service.set("k_ttl", "v", ex=60)
    assert 0 < await service.client.ttl("k_ttl") <= 60

    # expire 续期
    assert await service.expire("k", 120) is True
    assert 0 < await service.client.ttl("k") <= 120
    assert await service.expire("missing", 120) is False

    # delete：多个键统计命中数，缺失键不计入
    await service.set("a", "1")
    await service.set("b", "2")
    assert await service.delete("a", "b", "missing") == 2
    assert await service.get("a") is None
    assert await service.delete("k") == 1
    assert await service.get("k") is None


async def test_publish(service: RedisService):
    """publish 返回订阅者数量，订阅方可收到消息。"""
    # 无订阅者
    assert await service.publish("chan", "hello") == 0

    pubsub = service.client.pubsub()
    await pubsub.subscribe("chan")
    try:
        assert await service.publish("chan", "hello") == 1
        assert (await pubsub.get_message(timeout=1))["type"] == "subscribe"  # 订阅确认
        message = await pubsub.get_message(timeout=1)
        assert message["data"] == "hello"
    finally:
        await pubsub.aclose()


async def test_connection_error_raises_unified_error():
    """连接异常统一抛 RedisUnavailableError（连接不可达的真实客户端验证）。"""
    client = Redis.from_url("redis://localhost:59999/0", socket_connect_timeout=1)
    service = RedisService(client)
    with pytest.raises(RedisUnavailableError):
        await service.get("k")
    with pytest.raises(RedisUnavailableError):
        await service.acquire_lock("k", ttl=10)
    with pytest.raises(RedisUnavailableError):
        await service.publish("chan", "msg")


async def test_lock_acquire_and_reentry_rejected(service: RedisService):
    """获取成功；持有期间重入拒绝；释放后可再次获取。"""
    lock = await service.acquire_lock("agent:run", ttl=30)
    assert lock is not None
    assert await service.acquire_lock("agent:run", ttl=30) is None  # 重入拒绝
    assert await lock.release() is True
    assert await service.acquire_lock("agent:run", ttl=30) is not None  # 释放后可再获取


async def test_lock_expiry_auto_release(service: RedisService):
    """TTL 过期后锁自动释放；旧持有者的释放因 token 不匹配而失败，不误删新锁。"""
    stale = await service.acquire_lock("agent:run", ttl=1)
    assert stale is not None

    await asyncio.sleep(1.1)  # 等待 TTL 过期
    fresh = await service.acquire_lock("agent:run", ttl=30)
    assert fresh is not None

    # 旧持有者释放失败，新锁不受影响
    assert await stale.release() is False
    assert await service.get("lock:agent:run") == fresh.token
    assert await fresh.release() is True


async def test_lock_context_manager(service: RedisService):
    """async with 正常退出时自动释放锁。"""
    async with await service.acquire_lock("agent:run", ttl=30) as lock:
        assert lock is not None
        assert await service.acquire_lock("agent:run", ttl=30) is None
    assert await service.acquire_lock("agent:run", ttl=30) is not None

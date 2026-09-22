"""Redis 基础设施：统一服务封装（字符串读写 / 过期 / 分布式锁 / 发布）。

- 既有 Refresh Token 链路（3.2）继续使用裸客户端 get_redis()，本次不接入。
- 新代码一律通过 RedisService 访问 Redis，连接异常统一抛 RedisUnavailableError。
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.exceptions import WatchError

from app.core.config import get_settings

settings = get_settings()

T = TypeVar("T")

# 分布式锁 key 前缀，与普通数据 key 隔离
LOCK_KEY_PREFIX = "lock:"


class RedisUnavailableError(Exception):
    """Redis 连接异常（连接失败 / 超时），由 RedisService 统一抛出。"""


class RedisService:
    """Redis 统一封装：连接类异常统一转为 RedisUnavailableError，分布式锁带 TTL 与安全释放。"""

    def __init__(self, client: Redis) -> None:
        self.client = client

    async def _execute(self, operation: Callable[[], Awaitable[T]]) -> T:
        """执行 Redis 操作，连接失败 / 超时统一转为 RedisUnavailableError。"""
        try:
            return await operation()
        except (RedisConnectionError, RedisTimeoutError) as exc:
            raise RedisUnavailableError(f"Redis 不可用: {exc}") from exc

    async def get(self, key: str) -> str | None:
        return await self._execute(lambda: self.client.get(key))

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        await self._execute(lambda: self.client.set(key, value, ex=ex))

    async def delete(self, *keys: str) -> int:
        return await self._execute(lambda: self.client.delete(*keys))

    async def expire(self, key: str, seconds: int) -> bool:
        return await self._execute(lambda: self.client.expire(key, seconds))

    async def publish(self, channel: str, message: str) -> int:
        """发布消息，返回接收到消息的订阅者数量。"""
        return await self._execute(lambda: self.client.publish(channel, message))

    def pubsub(self) -> PubSub:
        """创建 Pub/Sub 订阅句柄（订阅 / 消费 / 关闭的生命周期由调用方管理）。"""
        return self.client.pubsub()

    async def acquire_lock(self, name: str, ttl: int) -> RedisLock | None:
        """尝试获取分布式锁（SET NX EX，TTL 防死锁）。

        成功返回锁句柄（token 标识持有者）；锁已被持有返回 None。
        """
        token = uuid.uuid4().hex
        key = f"{LOCK_KEY_PREFIX}{name}"
        acquired = await self._execute(lambda: self.client.set(key, token, nx=True, ex=ttl))
        return RedisLock(self, key, token) if acquired else None


@dataclass
class RedisLock:
    """分布式锁句柄：token 保证只能释放自己持有的锁，支持 async with 自动释放。"""

    service: RedisService
    key: str
    token: str

    async def release(self) -> bool:
        """释放锁（compare-and-delete，WATCH 事务实现，不依赖 Lua 环境）。

        锁已过期并被其他持有者获取时返回 False，不会误删他人的锁。
        """
        async with self.service.client.pipeline() as pipe:
            try:
                await pipe.watch(self.key)
                if await pipe.get(self.key) != self.token:
                    await pipe.unwatch()
                    return False
                pipe.multi()
                await pipe.delete(self.key)
                await pipe.execute()
                return True
            except WatchError:
                return False

    async def __aenter__(self) -> RedisLock:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.release()


def create_redis() -> Redis:
    """创建 Redis 客户端（decode_responses 便于直接存取字符串）。"""
    return Redis.from_url(settings.redis_url, decode_responses=True)


# 全局单例：客户端线程安全且内部维护连接池，整个进程复用，避免每请求重建
redis_client = create_redis()
_service = RedisService(redis_client)


def get_redis() -> Redis:
    """FastAPI 依赖：返回全局 Redis 客户端（既有 Refresh Token 链路使用）。"""
    return redis_client


def get_redis_service() -> RedisService:
    """FastAPI 依赖：返回统一的 RedisService 单例。"""
    return _service

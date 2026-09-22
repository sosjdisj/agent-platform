"""健康检查接口：依次检查 API 自身、PostgreSQL、Redis、Qdrant。"""
import time
from typing import Any

from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import SessionLocal
from fastapi import APIRouter

router = APIRouter()

settings = get_settings()


def _component_up(latency_ms: float) -> dict[str, Any]:
    return {"status": "up", "latency_ms": round(latency_ms, 2)}


def _component_down(error: Exception | str) -> dict[str, Any]:
    message = str(error) or error.__class__.__name__
    return {"status": "down", "error": message[:200]}


async def check_postgres() -> dict[str, Any]:
    start = time.perf_counter()
    session: AsyncSession | None = None
    try:
        session = SessionLocal()
        await session.execute(text("SELECT 1"))
        return _component_up((time.perf_counter() - start) * 1000)
    except Exception as exc:  # noqa: BLE001 - 健康检查需吞掉任何连接错误
        return _component_down(exc)
    finally:
        if session is not None:
            await session.close()


async def check_redis() -> dict[str, Any]:
    start = time.perf_counter()
    client: Redis | None = None
    try:
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=settings.healthcheck_timeout,
            socket_timeout=settings.healthcheck_timeout,
        )
        await client.ping()
        return _component_up((time.perf_counter() - start) * 1000)
    except Exception as exc:  # noqa: BLE001
        return _component_down(exc)
    finally:
        if client is not None:
            await client.aclose()


async def check_qdrant() -> dict[str, Any]:
    start = time.perf_counter()
    client: AsyncQdrantClient | None = None
    try:
        client = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
            timeout=settings.healthcheck_timeout,
            check_compatibility=False,
        )
        await client.get_collections()
        return _component_up((time.perf_counter() - start) * 1000)
    except Exception as exc:  # noqa: BLE001
        return _component_down(exc)
    finally:
        if client is not None:
            await client.close()


@router.get("/health")
async def health() -> dict[str, Any]:
    """依次检查各依赖服务的连接状态。"""
    components: dict[str, dict[str, Any]] = {
        # API 服务自身可响应即视为 up
        "api": {"status": "up"},
        "postgres": await check_postgres(),
        "redis": await check_redis(),
        "qdrant": await check_qdrant(),
    }

    all_up = all(c["status"] == "up" for c in components.values())
    return {
        "status": "ok" if all_up else "degraded",
        "app": settings.app_name,
        "version": settings.app_version,
        "components": components,
    }

"""健康检查接口测试。

注意：本地无需运行 PostgreSQL / Redis / Qdrant。
依赖服务不可用时接口应正常返回 200，且整体状态为 degraded。
"""
from httpx import AsyncClient


async def test_health_returns_200(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200

    data = resp.json()
    assert data["status"] in {"ok", "degraded"}
    assert set(data["components"].keys()) == {"api", "postgres", "redis", "qdrant"}


async def test_health_api_component_is_up(client: AsyncClient):
    resp = await client.get("/health")
    data = resp.json()
    assert data["components"]["api"]["status"] == "up"

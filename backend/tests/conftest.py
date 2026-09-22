"""pytest 公共夹具。"""
import asyncio
import hashlib
import sys
from typing import Any

if sys.platform == "win32":
    # psycopg 异步驱动（checkpointer）要求 Selector 事件循环；Windows 默认 Proactor 不支持
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

TEST_DB_NAME = "agent_platform_test"


class FakeEmbedder:
    """确定性伪随机向量（文本哈希循环填充），维度取构造参数模拟配置维度。"""

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._one(text)

    def _one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = [b / 255.0 for b in digest]
        while len(values) < self.dimension:
            values.extend(values)
        return values[: self.dimension]


class StructuredLLMStub:
    """chat_structured 替身：按序返回预置模型实例并记录请求（供只依赖结构化输出的 Agent 测试复用）。"""

    def __init__(self, outputs: list[Any]) -> None:
        self._outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def chat_structured(
        self, messages: Any, response_model: type[Any], **kwargs: Any
    ) -> Any:
        self.calls.append({"messages": list(messages), "response_model": response_model, **kwargs})
        return self._outputs.pop(0)


def _build_url(db_name: str, *, driver: str = "") -> str:
    """按指定数据库拼接连接串；driver 供 SQLAlchemy 使用（asyncpg 原生 DSN 不能带）。"""
    return (
        f"postgresql{driver}://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{db_name}"
    )


@pytest.fixture
async def test_database_url() -> str:
    """确保测试库存在，返回其异步连接串。"""
    admin = await asyncpg.connect(_build_url("postgres"))
    try:
        await admin.execute(f"CREATE DATABASE {TEST_DB_NAME}")
    except asyncpg.DuplicateDatabaseError:
        pass
    finally:
        await admin.close()
    return _build_url(TEST_DB_NAME, driver="+asyncpg")


@pytest.fixture
async def db_session_maker(test_database_url: str) -> async_sessionmaker[AsyncSession]:
    """提供指向测试库的会话工厂：工具类等自行开短会话的组件用它构造。"""
    from app.db.base import Base
    import app.models  # noqa: F401  确保模型全部注册

    engine = create_async_engine(test_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(db_session_maker: async_sessionmaker[AsyncSession]) -> AsyncSession:
    """提供已建好全部表的数据库会话，测试结束自动清理。

    通过 metadata.create_all 建表（不走迁移），保证 Repository 测试独立且快速。
    """
    async with db_session_maker() as session:
        yield session


@pytest.fixture
async def seeded_maker(db_session, db_session_maker: async_sessionmaker[AsyncSession]):
    """执行 Prompt 4 seed（内部 commit）后返回测试会话工厂，供工具开短会话读取已落库数据。"""
    from app import seed as seed_module

    await seed_module.seed(db_session)
    return db_session_maker


@pytest.fixture
async def case_ids(seeded_maker):
    """案例主数据 id：客户 A / B 与产品 P1 / P2（运行时解析，不依赖序列状态）。"""
    from sqlalchemy import select

    from app.models.customer import Customer
    from app.models.product import Product

    async with seeded_maker() as session:
        a_id = await session.scalar(select(Customer.id).where(Customer.code == "CUST-0001"))
        b_id = await session.scalar(select(Customer.id).where(Customer.code == "CUST-0002"))
        p1_id = await session.scalar(select(Product.id).where(Product.sku == "SKU-XS100"))
        p2_id = await session.scalar(select(Product.id).where(Product.sku == "SKU-DC20"))
    return a_id, b_id, p1_id, p2_id


@pytest.fixture
async def checkpointer(test_database_url: str):
    """指向测试库的 checkpointer：setup 幂等建表，并清空历史 checkpoint 隔离多次运行。"""
    from app.agents.checkpoint import open_checkpointer, psycopg_dsn

    async with open_checkpointer(psycopg_dsn(test_database_url)) as saver:
        await saver.conn.execute("TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes")
        yield saver


@pytest.fixture
async def client():
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def fake_redis():
    """独立 FakeAsyncRedis 实例：既注入应用又供测试直接 publish，保证发布/订阅同源。"""
    from fakeredis import FakeAsyncRedis

    fr = FakeAsyncRedis(decode_responses=True)
    yield fr
    await fr.aclose()


@pytest.fixture
async def api_client(test_database_url: str, fake_redis):
    """覆盖 get_session / get_redis / get_redis_service 指向测试实例，供接口全链路测试使用。"""
    from app.core.redis import RedisService, get_redis, get_redis_service
    from app.db.base import Base
    from app.db.session import get_session

    import app.models  # noqa: F401  确保模型全部注册（需在导入 app 实例前执行）
    from app.main import app  # noqa: E501  放在 import app.models 之后，避免模块名遮蔽实例

    engine = create_async_engine(test_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_session():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_redis] = lambda: fake_redis
    app.dependency_overrides[get_redis_service] = lambda: RedisService(fake_redis)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()

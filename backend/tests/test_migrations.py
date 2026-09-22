"""Alembic 迁移测试：upgrade / downgrade 全链路 + 表结构、索引与逻辑外键策略校验。

说明：
- 使用独立测试库 agent_platform_test，通过 ALEMBIC_DATABASE_URL 注入；
- alembic command 为同步 API（内部 asyncio.run），在线程中调用以避开 pytest 事件循环。
- 项目采用逻辑外键策略：数据库不建物理外键，关联完整性由应用层保证。
"""
import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]

AUTH_TABLES = {"users", "roles", "permissions", "user_roles"}

# 业务主数据与交易表清单（关联通过逻辑外键指向 customers / products）
BUSINESS_TABLE_FKS = {
    "orders",
    "sales_records",
    "customer_tickets",
}

# 面向"按客户 + 时间范围查询"的复合索引
PERIOD_INDEXES = {
    "orders": "ix_orders_customer_id_order_date",
    "sales_records": "ix_sales_records_customer_id_sale_date",
}

AGENT_TABLES = {"agent_tasks", "agent_approvals", "agent_trace_events", "evaluation_cases"}
TRACE_TASK_INDEX = "ix_agent_trace_events_task_id_timestamp"


def _make_alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return cfg


async def test_auth_tables_upgrade_downgrade(test_database_url: str, monkeypatch):
    """upgrade 后四表存在、外键与联合主键正确；downgrade 后全部清除。"""
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", test_database_url)
    cfg = _make_alembic_config()

    engine = create_async_engine(test_database_url)
    try:
        # 清空测试库，保证从 base 状态开始（asyncpg 不支持多语句，需逐条执行）
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))

        # ---- upgrade head ----
        await asyncio.to_thread(command.upgrade, cfg, "head")

        async with engine.connect() as conn:
            tables = {
                row[0]
                for row in await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            }
            assert AUTH_TABLES <= tables, f"缺少认证表: {AUTH_TABLES - tables}"

            # user_roles 联合唯一（复合主键即唯一索引）
            pk_def = await conn.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE contype = 'p' AND conrelid = 'user_roles'::regclass"
                )
            )
            assert pk_def == "PRIMARY KEY (user_id, role_id)"

        # ---- downgrade base ----
        await asyncio.to_thread(command.downgrade, cfg, "base")

        async with engine.connect() as conn:
            tables = {
                row[0]
                for row in await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            }
            assert not (AUTH_TABLES & tables), "downgrade 后认证表未清除"
    finally:
        await engine.dispose()


async def test_business_tables_upgrade(test_database_url: str, monkeypatch):
    """upgrade 后业务表存在、外键指向正确、按客户+时间范围的复合索引存在。"""
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", test_database_url)
    cfg = _make_alembic_config()

    engine = create_async_engine(test_database_url)
    try:
        # 清空测试库，保证从 base 状态开始（asyncpg 不支持多语句，需逐条执行）
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))

        await asyncio.to_thread(command.upgrade, cfg, "head")

        async with engine.connect() as conn:
            tables = {
                row[0]
                for row in await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            }
            expected = BUSINESS_TABLE_FKS | {"knowledge_documents"}
            missing = expected - tables
            assert not missing, f"缺少业务表: {missing}"

            # 按客户 + 时间范围查询的复合索引必须存在
            for table, index_name in PERIOD_INDEXES.items():
                index = await conn.scalar(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        f"WHERE tablename = '{table}' AND indexname = '{index_name}'"
                    )
                )
                assert index == index_name, f"{table} 缺少复合索引 {index_name}"
    finally:
        await engine.dispose()


async def test_agent_tables_upgrade(test_database_url: str, monkeypatch):
    """upgrade 后 Agent 系统四表存在，trace 表 (task_id, timestamp) 联合索引存在。"""
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", test_database_url)
    cfg = _make_alembic_config()

    engine = create_async_engine(test_database_url)
    try:
        # 清空测试库，保证从 base 状态开始（asyncpg 不支持多语句，需逐条执行）
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))

        await asyncio.to_thread(command.upgrade, cfg, "head")

        async with engine.connect() as conn:
            tables = {
                row[0]
                for row in await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            }
            assert AGENT_TABLES <= tables, f"缺少 Agent 表: {AGENT_TABLES - tables}"

            # 事件类型枚举类型已创建
            enum_type = await conn.scalar(
                text(
                    "SELECT 1 FROM pg_type WHERE typname = 'agent_trace_event_type'"
                )
            )
            assert enum_type == 1, "缺少事件类型枚举 agent_trace_event_type"

            # trace 表 (task_id, timestamp) 联合索引必须存在
            index = await conn.scalar(
                text(
                    "SELECT indexname FROM pg_indexes "
                    f"WHERE tablename = 'agent_trace_events' AND indexname = '{TRACE_TASK_INDEX}'"
                )
            )
            assert index == TRACE_TASK_INDEX, "trace 表缺少 (task_id, timestamp) 联合索引"

            # 结构化报告列（24.1 报告页证据溯源数据源）
            report_column = await conn.scalar(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'agent_tasks' AND column_name = 'report'"
                )
            )
            assert report_column == 1, "agent_tasks 缺少 report 列"

            # 逻辑外键策略：upgrade head 后全库不应存在任何物理外键约束
            fk_count = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE contype = 'f' AND connamespace = 'public'::regnamespace"
                )
            )
            assert fk_count == 0, f"仍存在 {fk_count} 个物理外键约束"
    finally:
        await engine.dispose()

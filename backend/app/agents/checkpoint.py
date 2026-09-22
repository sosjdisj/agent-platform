"""Postgres Checkpointer：LangGraph 状态持久化的单一入口。

- thread_id = task_id：agent_thread_config 集中定义，恢复与隔离的唯一键（全项目
  禁止散落拼装 config）；
- DSN 复用 database_url（仅去掉 asyncpg 驱动后缀，供 psycopg3 使用），不新增配置；
- open_checkpointer 打开连接并 setup 幂等建表；图编译时经
  build_agent_graph(checkpointer=...) 注入，运行时传 agent_thread_config(task_id)。
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.core.config import get_settings


def psycopg_dsn(database_url: str | None = None) -> str:
    """SQLAlchemy DSN（postgresql+asyncpg://）→ psycopg3 DSN（postgresql://）。"""
    url = database_url if database_url is not None else get_settings().database_url
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def agent_thread_config(task_id: int) -> RunnableConfig:
    """Agent 图运行 config：thread_id = task_id（checkpoint 恢复与隔离的唯一键）。"""
    return {"configurable": {"thread_id": str(task_id)}}


@asynccontextmanager
async def open_checkpointer(
    dsn: str | None = None, *, setup: bool = True
) -> AsyncGenerator[AsyncPostgresSaver, None]:
    """打开 Postgres checkpointer（连接随上下文开关）；setup 幂等建表（迁移可关）。"""
    async with AsyncPostgresSaver.from_conn_string(dsn or psycopg_dsn()) as saver:
        if setup:
            await saver.setup()
        yield saver

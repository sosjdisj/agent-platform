"""Agent 轨迹事件 Repository。"""
from datetime import datetime

from sqlalchemy import delete, select

from app.models.agent import AgentTraceEvent

from app.repositories.base import BaseRepository


class AgentTraceEventRepository(BaseRepository[AgentTraceEvent]):
    model = AgentTraceEvent

    async def list_by_task(self, task_id: int) -> list[AgentTraceEvent]:
        """按任务回放全部轨迹事件，按 (timestamp, id) 升序。

        命中 ix_agent_trace_events_task_id_timestamp 索引；id 兜底保证
        同一时刻落库的事件次序与写入顺序一致。
        """
        stmt = (
            select(self.model)
            .where(self.model.task_id == task_id)
            .order_by(self.model.timestamp, self.model.id)
        )
        return list(await self.session.scalars(stmt))

    async def list_by_task_page(
        self, task_id: int, *, before_id: int | None = None, limit: int = 50
    ) -> list[AgentTraceEvent]:
        """按任务分页回放轨迹（keyset：id 倒序 + before_id 游标，落库序即时间序）。

        首页取最新 limit 条；返回按 id 倒序（最新在前），前端升序渲染。
        """
        stmt = select(self.model).where(self.model.task_id == task_id)
        if before_id is not None:
            stmt = stmt.where(self.model.id < before_id)
        stmt = stmt.order_by(self.model.id.desc()).limit(limit)
        return list(await self.session.scalars(stmt))

    async def delete_older_than(self, cutoff: datetime) -> int:
        """批量删除 cutoff 之前的过期事件，返回删除条数（保留期清理专用）。"""
        result = await self.session.execute(delete(self.model).where(self.model.timestamp < cutoff))
        return result.rowcount or 0

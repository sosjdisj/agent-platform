"""Agent 任务 Repository。"""
from sqlalchemy import select

from app.models.agent import AgentTask

from app.repositories.base import BaseRepository


class AgentTaskRepository(BaseRepository[AgentTask]):
    model = AgentTask

    async def list_by_user(
        self, user_id: int, *, offset: int = 0, limit: int = 50
    ) -> list[AgentTask]:
        """按创建时间倒序列出本人任务（id 作次序键，避免同刻创建时排序抖动）。"""
        stmt = (
            select(self.model)
            .where(self.model.user_id == user_id)
            .order_by(self.model.created_at.desc(), self.model.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))

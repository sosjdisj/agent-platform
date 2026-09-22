"""Agent 审批记录 Repository。"""
from sqlalchemy import select

from app.models.agent import AgentApproval

from app.repositories.base import BaseRepository


class AgentApprovalRepository(BaseRepository[AgentApproval]):
    model = AgentApproval

    async def list_by_task(self, task_id: int) -> list[AgentApproval]:
        """按任务查询审批记录（命中 ix_agent_approvals_task_id 索引），按 id 升序。"""
        stmt = (
            select(self.model)
            .where(self.model.task_id == task_id)
            .order_by(self.model.id)
        )
        return list(await self.session.scalars(stmt))

    async def list_by_requester(self, requester_id: int, *, limit: int = 50) -> list[AgentApproval]:
        """按申请人查询审批记录（审批中心列表），按 id 倒序（最新在前）。"""
        stmt = (
            select(self.model)
            .where(self.model.requester_id == requester_id)
            .order_by(self.model.id.desc())
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))

    async def latest_by_operation(
        self, task_id: int, tool: str, parameter_summary: str
    ) -> AgentApproval | None:
        """同任务同操作（tool + parameter_summary）的最近一条审批记录。

        审批门恢复重入的幂等依据：中断前创建的记录按此定位，已有决定直接复用。
        """
        stmt = (
            select(self.model)
            .where(
                self.model.task_id == task_id,
                self.model.action == tool,
                self.model.payload["parameter_summary"].astext == parameter_summary,
            )
            .order_by(self.model.id.desc())
            .limit(1)
        )
        return await self.session.scalar(stmt)

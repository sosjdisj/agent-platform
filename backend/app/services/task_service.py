"""任务服务：agent_tasks 生命周期管理（创建 / 归属查询 / 状态机流转，21.1）。

状态机：合法流转单一来源为 app.agents.state.TASK_TRANSITIONS（pending → running →
waiting_approval → completed / failed / cancelled，waiting_approval 决定后回 running，
任一非终态可取消，终态不可再流转），非法流转抛 409（TASK_INVALID_TRANSITION）；
进入终态写 finished_at。归属校验（get_owned）为查询 / 取消 / 事件流共用的单一入口。
不做：实际执行 LangGraph——create 只落 pending 任务行，状态推进由后续执行器接线。
"""
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.state import TASK_TERMINAL_STATUSES, TASK_TRANSITIONS, AgentStatus
from app.core.errors import AuthError
from app.models.agent import AgentTask
from app.repositories.agent_task import AgentTaskRepository


class TaskService:
    """任务域服务：封装创建、归属查询与状态机流转，事务提交在本层完成。"""

    def __init__(self, session: AsyncSession) -> None:
        self._tasks = AgentTaskRepository(session)

    async def create(self, *, user_id: int, title: str, query: str) -> AgentTask:
        """创建任务并提交事务：status=pending（模型默认值），等待执行器接单。"""
        task = await self._tasks.create(user_id=user_id, title=title, query=query)
        await self._tasks.session.commit()
        return task

    async def get_owned(self, task_id: int, user_id: int) -> AgentTask:
        """查询本人任务：不存在 404，非本人 403（详情 / 取消 / 事件流共用同一校验）。"""
        task = await self._tasks.get(task_id)
        if task is None:
            raise AuthError("TASK_NOT_FOUND", "任务不存在", status_code=404)
        if task.user_id != user_id:
            raise AuthError("TASK_FORBIDDEN", "无权访问该任务", status_code=403)
        return task

    async def list_owned(
        self, user_id: int, *, offset: int = 0, limit: int = 50
    ) -> list[AgentTask]:
        """按创建时间倒序列出本人任务（列表页分页拉取）。"""
        return await self._tasks.list_by_user(user_id, offset=offset, limit=limit)

    async def cancel(self, task_id: int, *, user_id: int) -> AgentTask:
        """取消本人任务：先过归属校验，再按状态机流转到 cancelled（终态 409）。"""
        task = await self.get_owned(task_id, user_id)
        return await self.transition(task, AgentStatus.CANCELLED)

    async def transition(
        self,
        task: AgentTask,
        to_status: AgentStatus,
        *,
        result: str | None = None,
        error: str | None = None,
        report: dict | None = None,
    ) -> AgentTask:
        """状态机流转核心：按 TASK_TRANSITIONS 校验后落库并提交，进入终态写 finished_at。

        result / error / report 为终态附带产物（COMPLETED 写 result 文本与 report 结构化
        报告、FAILED 写 error），与状态同事务落库，保证消费方读到的终态行自洽。
        """
        current = AgentStatus(task.status)
        if to_status not in TASK_TRANSITIONS[current]:
            raise AuthError(
                "TASK_INVALID_TRANSITION",
                f"任务状态不允许从 {current.value} 流转到 {to_status.value}",
                status_code=409,
            )
        updates: dict[str, object] = {"status": to_status.value}
        if to_status in TASK_TERMINAL_STATUSES:
            updates["finished_at"] = datetime.now(timezone.utc)
        if result is not None:
            updates["result"] = result
        if error is not None:
            updates["error"] = error
        if report is not None:
            updates["report"] = report
        task = await self._tasks.update(task, **updates)
        await self._tasks.session.commit()
        return task

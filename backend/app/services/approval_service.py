"""审批服务：agent_approvals 生命周期管理（创建 / 查询 / approve-reject 流转）。

入参与现有表结构的字段映射（单一事实源为 agent_approvals 表，不冗余建列）：
- tool → action；parameter_summary / risk_level → payload（JSONB）；
- approve / reject 共用 decided_at 记录决定时间戳，方向由 status 与 decision 记录。
状态机：pending → approved / rejected（终态不可再流转）。

审批事件（18.4，结构见 app/schemas/events.py）：创建与决定各写一条 Trace 事件
（approval_required / approval_result），payload 即事件结构 dump，与审批记录同事务
提交（审计与记录原子一致）；20.2 起决定提交后把事件 dump 原样发布到任务频道
（注入 redis 时生效），SSE 管线按 event 字段转发，未注入不发布（存量行为不变）。
"""
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import RedisService
from app.mcp.base import RiskLevel
from app.models.agent import AgentApproval, AgentTraceEvent, AgentTraceEventType
from app.repositories.agent_approval import AgentApprovalRepository
from app.schemas.events import ApprovalRequiredEvent, ApprovalResultEvent
from app.services.task_events import task_channel

PENDING = "pending"


class ApprovalService:
    """审批域服务：封装创建、查询与状态流转，事务提交在本层完成。"""

    def __init__(self, session: AsyncSession, *, redis: RedisService | None = None) -> None:
        self._approvals = AgentApprovalRepository(session)
        self._redis = redis

    async def create(
        self,
        *,
        task_id: int,
        requester_id: int,
        tool: str,
        parameter_summary: str,
        risk_level: RiskLevel,
    ) -> AgentApproval:
        """创建待审批记录并提交事务（status=pending 由模型默认值保证），同时写
        approval_required 轨迹事件（与记录同事务，payload 为事件结构 dump）。"""
        approval = await self._approvals.create(
            task_id=task_id,
            requester_id=requester_id,
            action=tool,
            payload={"parameter_summary": parameter_summary, "risk_level": risk_level.value},
        )
        event = ApprovalRequiredEvent(
            task_id=task_id,
            approval_id=approval.id,
            tool=tool,
            risk_level=risk_level,
            requester_id=requester_id,
            parameter_summary=parameter_summary,
        )
        self._approvals.session.add(
            AgentTraceEvent(
                task_id=task_id,
                event_type=AgentTraceEventType.APPROVAL_REQUIRED,
                payload=event.model_dump(mode="json"),
                timestamp=datetime.now(timezone.utc),
            )
        )
        await self._approvals.session.commit()
        await self._publish(event)
        return approval

    async def get(self, approval_id: int) -> AgentApproval | None:
        """按 id 查询审批记录。"""
        return await self._approvals.get(approval_id)

    async def list_by_task(self, task_id: int) -> list[AgentApproval]:
        """按任务查询审批记录。"""
        return await self._approvals.list_by_task(task_id)

    async def latest_by_operation(
        self, task_id: int, tool: str, parameter_summary: str
    ) -> AgentApproval | None:
        """查询同任务同操作（tool + parameter_summary）的最近一条审批记录。

        图层审批门恢复重入的幂等依据：中断前创建的记录按此定位，已有决定直接复用。
        """
        return await self._approvals.latest_by_operation(task_id, tool, parameter_summary)

    async def approve(
        self, approval_id: int, *, decided_by: int, comment: str | None = None
    ) -> AgentApproval:
        """批准审批：pending → approved，记录决定人与时间戳。"""
        return await self._decide(
            approval_id, decision="approved", decided_by=decided_by, comment=comment
        )

    async def reject(
        self, approval_id: int, *, decided_by: int, comment: str | None = None
    ) -> AgentApproval:
        """驳回审批：pending → rejected，记录决定人与时间戳。"""
        return await self._decide(
            approval_id, decision="rejected", decided_by=decided_by, comment=comment
        )

    async def _decide(
        self, approval_id: int, *, decision: str, decided_by: int, comment: str | None
    ) -> AgentApproval:
        """流转核心：仅 pending 可进入终态，否则拒绝（记录不存在同理）。

        决定落库的同时写 approval_result 轨迹事件（同一事务，payload 为事件结构 dump）。
        """
        approval = await self._approvals.get(approval_id)
        if approval is None:
            raise ValueError(f"审批不存在: id={approval_id}")
        if approval.status != PENDING:
            raise ValueError(f"审批已处于终态 {approval.status}，不允许再次流转")
        await self._approvals.update(
            approval,
            status=decision,
            decision=decision,
            decided_by=decided_by,
            decided_at=datetime.now(timezone.utc),
            comment=comment,
        )
        raw_level = (approval.payload or {}).get("risk_level")
        event = ApprovalResultEvent(
            task_id=approval.task_id,
            approval_id=approval.id,
            tool=approval.action,
            risk_level=RiskLevel(raw_level) if raw_level else None,
            decision=decision,  # type: ignore[arg-type]  # 仅 approved / rejected 两种取值
            decided_by=decided_by,
            comment=comment,
        )
        self._approvals.session.add(
            AgentTraceEvent(
                task_id=approval.task_id,
                event_type=AgentTraceEventType.APPROVAL_RESULT,
                payload=event.model_dump(mode="json"),
                timestamp=datetime.now(timezone.utc),
            )
        )
        await self._approvals.session.commit()
        await self._publish(event)
        return approval

    async def _publish(self, event: ApprovalRequiredEvent | ApprovalResultEvent) -> None:
        """事件提交后发布到任务频道（注入 redis 时生效）：消息即事件 dump，与 18.4 结构对齐。"""
        if self._redis is not None:
            await self._redis.publish(task_channel(event.task_id), event.model_dump_json())

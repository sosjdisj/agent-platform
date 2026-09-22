"""轨迹写入服务：agent_trace_events 的统一追加入口（纯追加、不 UPDATE）。

字段映射（单一事实源为 agent_trace_events 表，业务维度不冗余建列）：
- task_id / agent / event_type / timestamp → 表列；
- tool / round / duration_ms / status / parameter_summary / result_summary /
  error_code / metadata → payload（JSONB，仅保留非空键，键集以 append 为准）。

纯追加语义：append 只 INSERT，事件一经落库不可变；回放按
(task_id, timestamp) 索引排序、id 兜底（见 AgentTraceEventRepository.list_by_task）。

实时发布（20.2）：注入 RedisService 后，append 落库同时把事件发布到任务频道
（task_channel，单一来源见 task_events），消息结构见 schemas.events.TraceEvent
（event 字段即 SSE 事件名，空字段不进消息）；未注入不发布（测试 / 调试路径行为不变）。
发布失败经 RedisUnavailableError 透出——与写库失败一致，接线方按需处理。
"""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import RedisService
from app.models.agent import AgentTraceEvent, AgentTraceEventType
from app.repositories.agent_trace_event import AgentTraceEventRepository
from app.schemas.events import TraceEvent
from app.services.task_events import task_channel


class TraceService:
    """轨迹域服务：统一的事件追加与按任务回放查询，追加可同时发布任务事件。"""

    def __init__(self, session: AsyncSession, *, redis: RedisService | None = None) -> None:
        self._events = AgentTraceEventRepository(session)
        self._redis = redis

    async def append(
        self,
        *,
        task_id: int,
        event_type: AgentTraceEventType,
        agent: str | None = None,
        tool: str | None = None,
        round: int | None = None,
        duration_ms: int | None = None,
        status: str | None = None,
        parameter_summary: str | None = None,
        result_summary: str | None = None,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
        commit: bool = True,
    ) -> AgentTraceEvent:
        """追加一条轨迹事件（INSERT，不触碰既有记录）并返回落库结果。

        - payload 仅收录非空业务字段，空字段不产生键（避免回放侧空值噪声）；
        - timestamp 缺省取当前 UTC 时间，供回放等场景显式指定；
        - 注入 redis 时落库后向任务频道发布同构事件消息（TraceEvent，排除空字段）；
        - commit=False 供需与业务记录同事务提交的调用方复用（发布动作不依赖提交）。
        """
        detail = {
            "tool": tool,
            "round": round,
            "duration_ms": duration_ms,
            "status": status,
            "parameter_summary": parameter_summary,
            "result_summary": result_summary,
            "error_code": error_code,
            "metadata": metadata,
        }
        event = await self._events.create(
            task_id=task_id,
            agent=agent,
            event_type=event_type,
            payload={k: v for k, v in detail.items() if v is not None} or None,
            timestamp=timestamp or datetime.now(timezone.utc),
        )
        if commit:
            await self._events.session.commit()
        if self._redis is not None:
            message = TraceEvent(event=event_type.value, task_id=task_id, agent=agent, **detail)
            await self._redis.publish(
                task_channel(task_id), message.model_dump_json(exclude_none=True)
            )
        return event

    async def list_by_task(self, task_id: int) -> list[AgentTraceEvent]:
        """按任务回放轨迹：timestamp 升序，同刻事件按落库次序（id）。"""
        return await self._events.list_by_task(task_id)

    async def list_by_task_page(
        self, task_id: int, *, before_id: int | None = None, limit: int = 50
    ) -> list[AgentTraceEvent]:
        """分页回放轨迹（23.4 时间线）：id 倒序 keyset，最新在前。"""
        return await self._events.list_by_task_page(task_id, before_id=before_id, limit=limit)

    async def purge_before(self, cutoff: datetime) -> int:
        """删除 cutoff 之前的过期事件并提交（保留期清理入口，后台任务周期调用）。"""
        removed = await self._events.delete_older_than(cutoff)
        await self._events.session.commit()
        return removed

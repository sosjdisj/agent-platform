"""任务执行器（21.2 / 21.3）：接单后推进状态机并驱动 Supervisor 全流程至终态落库。

- execute：PENDING → RUNNING（状态机流转）→ Supervisor.run（路由 → 调度专业 Agent →
  报告）→ 终态落库：COMPLETED（result = 报告文本 + report = 结构化报告）/ FAILED
  （error = 失败原因）；
  编排异常统一兜底为 FAILED 不上抛（后台协程无人接异常），已取消 / 非 pending 的任务
  跳过执行与终态写入（终态写入前重读状态，取消结果不被覆盖）；
- cancel（21.3）：路由经 TaskService.cancel 完成归属校验与状态机流转后调用——终止
  在跑协程（LangGraph 每步经 checkpointer 持久化，协程在最近的节点边界被安全终止，
  不产生半写状态），随后写 task_cancelled 埋点；未在跑（未接单 / 已收尾）时仅写埋点；
- 任务级轨迹（task_started / agent_finished / task_completed / task_failed /
  task_cancelled）由本层统一写入，trace_redis 接线后经 20.2 管线（TraceService →
  Redis Pub/Sub → SSE）实时发布——Supervisor 编排层与专业 Agent 只写节点级事件，
  生命周期事件单点写入不重复，SSE 帧契约（20.2）不变；
- submit：asyncio.create_task 异步启动（强引用表防 GC + 完成即清理 + 兜底记日志），
  返回 asyncio.Task 供测试确定性等待。
"""
import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.report import AnalysisReport
from app.agents.state import AgentStatus
from app.agents.supervisor import Supervisor, SupervisorWaiting
from app.core.errors import AuthError
from app.core.redis import RedisService
from app.models.agent import AgentTraceEventType
from app.repositories.agent_task import AgentTaskRepository
from app.services.task_service import TaskService
from app.services.trace_service import TraceService

logger = logging.getLogger(__name__)

# 报告段落渲染顺序：字段单一来源 = AnalysisReport（evidence 单列，sources 随行标注）
_REPORT_SECTIONS = (
    ("客户概况", "customer_profile"),
    ("销售趋势", "sales_trend"),
    ("订单变化", "order_changes"),
    ("产品变化", "product_changes"),
    ("相关知识", "related_knowledge"),
    ("可能原因", "possible_causes"),
)


def _report_text(report: AnalysisReport) -> str:
    """报告 → 任务结果文本：非空段落按固定顺序拼接（agent_tasks.result 落库用）。"""
    lines = [
        f"【{title}】{getattr(report, field)}"
        for title, field in _REPORT_SECTIONS
        if getattr(report, field)
    ]
    lines += [
        f"【依据】{item.content}（来源：{'；'.join(s.ref_id for s in item.sources)}）"
        for item in report.evidence
    ]
    if report.conclusion:
        lines.append(f"【结论】{report.conclusion}")
    lines += [f"- {s}" for s in report.suggestions]
    return "\n".join(lines)


class TaskExecutor:
    """任务域执行器：接单推进状态机、驱动编排、终态落库与任务级埋点。"""

    def __init__(
        self,
        supervisor: Supervisor,
        session_maker: async_sessionmaker[AsyncSession],
        *,
        trace_session_maker: async_sessionmaker[AsyncSession] | None = None,
        trace_redis: RedisService | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._session_maker = session_maker
        self._trace_session_maker = trace_session_maker
        self._trace_redis = trace_redis
        self._running: dict[int, asyncio.Task] = {}  # task_id → 后台协程（强引用防 GC）

    def submit(self, task_id: int, user_id: int, query: str) -> asyncio.Task:
        """异步启动任务执行（不阻塞创建请求）；重复 submit 同一任务交由状态机拒绝。"""
        background = asyncio.create_task(self.execute(task_id, user_id, query))
        self._running[task_id] = background
        background.add_done_callback(lambda t: self._on_done(task_id, t))
        return background

    async def cancel(self, task_id: int) -> bool:
        """终止在跑协程并写取消埋点（21.3 取消语义的执行侧，路由流转到 cancelled 后调用）。

        前置：任务状态已由 TaskService.cancel 流转为 cancelled（归属与状态机校验在路由层），
        因此被终止的协程不会覆盖取消结果（_finish 写终态前重读状态）。LangGraph 每步经
        checkpointer 持久化，协程在最近的 await 点（节点边界）被终止，不产生半写状态；
        await 直至退出，取消请求返回即已确定终止。未在跑（未接单 / 已收尾）时仅写埋点。
        返回是否终止了在跑协程。
        """
        background = self._running.pop(task_id, None)
        stopped = False
        if background is not None and not background.done():
            background.cancel()
            stopped = True
            try:
                await background
            except asyncio.CancelledError:
                pass  # 协程在节点边界被终止（预期路径）；其余异常照常上抛（_on_done 已记日志）
        await self._trace(task_id, AgentTraceEventType.TASK_CANCELLED)
        return stopped

    async def execute(self, task_id: int, user_id: int, query: str) -> None:
        """同步执行入口：接单 → 编排 → 终态落库；异常兜底为 FAILED。"""
        if not await self._claim(task_id):
            return
        await self._trace(task_id, AgentTraceEventType.TASK_STARTED)
        await self._drive(task_id, self._supervisor.run(task_id, user_id, query))

    async def resume(self, task_id: int, user_id: int) -> asyncio.Task:
        """审批决定（approved）后恢复任务（23.3）：waiting → running 已由路由流转，
        从最后 checkpoint 重执行被中断的编排节点（图内审批门按最新决定幂等放行），
        终态落库与任务级埋点复用 _finish（不重发 task_started）。"""
        background = asyncio.create_task(self._drive(task_id, self._supervisor.resume(task_id)))
        self._running[task_id] = background
        background.add_done_callback(lambda t: self._on_done(task_id, t))
        return background

    async def _drive(self, task_id: int, orchestration: Awaitable[AnalysisReport]) -> None:
        """编排驱动：等待审批（SupervisorWaiting）转 waiting_approval，其余异常兜底
        FAILED，正常产出落 COMPLETED——execute 与 resume 共用的后半段。"""
        started = time.perf_counter()
        try:
            report = await orchestration
        except SupervisorWaiting as exc:
            logger.info("任务 %s 等待审批：%s", task_id, exc)
            await self._wait_approval(task_id)
            return
        except Exception as exc:
            logger.exception("任务 %s 编排执行失败", task_id)
            message = str(exc) or type(exc).__name__
            await self._finish(
                task_id,
                AgentStatus.FAILED,
                error=message,
                duration_ms=self._elapsed_ms(started),
                errors=[message],
            )
            return
        await self._finish(
            task_id,
            AgentStatus.COMPLETED,
            result=_report_text(report),
            report=report.model_dump(mode="json"),
            duration_ms=self._elapsed_ms(started),
            result_summary=report.conclusion or "",
        )

    async def _wait_approval(self, task_id: int) -> None:
        """running → waiting_approval（HIGH 工具等待审批）；竞态取消时流转被拒即跳过。"""
        async with self._session_maker() as session:
            task = await AgentTaskRepository(session).get(task_id)
            if task is None or task.status != AgentStatus.RUNNING.value:
                logger.info("任务 %s 不处于 running（%s），等待审批流转跳过",
                            task_id, task and task.status)
                return
            try:
                await TaskService(session).transition(task, AgentStatus.WAITING_APPROVAL)
            except AuthError:
                logger.info("任务 %s 已离开 running，等待审批流转被状态机拒绝", task_id)

    async def _claim(self, task_id: int) -> bool:
        """接单：仅 pending 任务可推进到 running（已取消 / 不存在则跳过，返回 False）。"""
        async with self._session_maker() as session:
            task = await AgentTaskRepository(session).get(task_id)
            if task is None or task.status != AgentStatus.PENDING.value:
                logger.info("任务 %s 不处于 pending（%s），跳过执行", task_id, task and task.status)
                return False
            await TaskService(session).transition(task, AgentStatus.RUNNING)
            return True

    async def _finish(
        self,
        task_id: int,
        to_status: AgentStatus,
        *,
        duration_ms: int,
        result: str | None = None,
        report: dict | None = None,
        error: str | None = None,
        result_summary: str | None = None,
        errors: list[str] | None = None,
    ) -> None:
        """终态落库 + 收束埋点：仅当任务仍处 running 时写入（被取消则不覆盖）。"""
        async with self._session_maker() as session:
            task = await AgentTaskRepository(session).get(task_id)
            if task is None or task.status != AgentStatus.RUNNING.value:
                logger.info("任务 %s 已离开 running（%s），终态写入跳过", task_id, task and task.status)
                return
            await TaskService(session).transition(
                task, to_status, result=result, error=error, report=report
            )
        await self._trace(
            task_id,
            AgentTraceEventType.AGENT_FINISHED,
            duration_ms=duration_ms,
            status=to_status.value,
            result_summary=result_summary,
            metadata={"errors": errors} if errors else None,
        )
        await self._trace(
            task_id,
            AgentTraceEventType.TASK_COMPLETED if to_status is AgentStatus.COMPLETED
            else AgentTraceEventType.TASK_FAILED,
            metadata={"errors": errors} if errors else None,
        )

    async def _trace(self, task_id: int, event_type: AgentTraceEventType, /, **fields) -> None:
        """追加一条任务级轨迹并经 20.2 管线发布（未接线 trace_session_maker 时不写）。"""
        if self._trace_session_maker is None:
            return
        async with self._trace_session_maker() as session:
            await TraceService(session, redis=self._trace_redis).append(
                task_id=task_id, event_type=event_type, **fields
            )

    def _on_done(self, task_id: int, background: asyncio.Task) -> None:
        """后台协程收尾：清理强引用；execute 未兜底到的异常（如 DB 不可用）记日志。"""
        self._running.pop(task_id, None)
        if not background.cancelled() and background.exception() is not None:
            logger.error("任务 %s 后台执行异常退出", task_id, exc_info=background.exception())

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)


def build_task_executor(
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    redis: RedisService | None = None,
) -> TaskExecutor:
    """组合根：真实 LLM + Supervisor 全流程 + SessionLocal（状态机与任务级轨迹）。

    专业 Agent 缺省真实构造（RAG 栈 / 数据库连接延迟加载）；checkpointer 经 lifespan
    传入（进程级连接复用），redis 用于任务事件发布（20.2）。
    """
    from app.core.llm import get_llm_service
    from app.db.session import SessionLocal

    supervisor = Supervisor(
        get_llm_service(),
        checkpointer=checkpointer,
        auth_session_maker=SessionLocal,
        approval_session_maker=SessionLocal,
        trace_session_maker=SessionLocal,
        trace_redis=redis,
    )
    return TaskExecutor(
        supervisor, SessionLocal, trace_session_maker=SessionLocal, trace_redis=redis
    )

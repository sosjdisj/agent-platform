"""任务 API（21.1 / 21.2）：创建（异步启动执行）/ 列表（23.1）/ 详情 / 取消 + 实时事件流（SSE，20.1）。

- 认证：REST 接口沿用 Prompt 3.4 Bearer 头；事件流因 EventSource 无法携带
  Authorization 头，改用 HttpOnly SSE Cookie（禁止 URL 传 token）；
- 归属校验统一走 TaskService.get_owned：任务不存在 404，非本人任务 403；
- 创建落 pending 任务行后经 TaskExecutor.submit 异步启动 Supervisor 全流程（21.2），
  状态推进与终态落库在后台执行器内完成，创建请求即时返回；
- 取消（21.3）：归属校验 + 状态机流转（TASK_TRANSITIONS）后，经执行器终止在跑
  协程（LangGraph 每步 checkpoint 持久化，节点边界安全终止）并写 task_cancelled
  埋点（经 20.2 管线实时推送）；终态 / 非法流转 409，被终止协程不覆盖取消结果；
- 事件流经 Redis Pub/Sub 订阅任务频道转发事件帧（管线见 app/services/task_events.py）。
"""
from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_current_user_sse
from app.core.config import get_settings
from app.core.redis import RedisService, get_redis_service
from app.db.session import get_session
from app.models.user import User
from app.schemas.events import TraceItem
from app.schemas.task import TaskCreateRequest, TaskResponse
from app.services.task_events import task_event_stream
from app.services.task_executor import TaskExecutor
from app.services.task_service import TaskService
from app.services.trace_service import TraceService

settings = get_settings()

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def get_task_executor(request: Request) -> TaskExecutor:
    """取应用级任务执行器（lifespan 组装，进程内单例）；测试经 dependency_overrides 替换。"""
    return request.app.state.task_executor


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    executor: TaskExecutor = Depends(get_task_executor),
) -> TaskResponse:
    """创建任务：落 pending 任务行并异步启动执行（本请求即时返回 pending 状态）。"""
    task = await TaskService(session).create(user_id=user.id, title=body.title, query=body.query)
    executor.submit(task.id, user.id, task.query)
    return TaskResponse.model_validate(task)


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TaskResponse]:
    """列出本人任务（创建时间倒序，offset/limit 分页）。"""
    tasks = await TaskService(session).list_owned(user.id, offset=offset, limit=limit)
    return [TaskResponse.model_validate(task) for task in tasks]


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskResponse:
    """查询本人任务详情。"""
    task = await TaskService(session).get_owned(task_id, user.id)
    return TaskResponse.model_validate(task)


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    executor: TaskExecutor = Depends(get_task_executor),
) -> TaskResponse:
    """取消任务：归属校验 + 状态机流转后，终止在跑协程并写取消埋点（终态 409）。"""
    task = await TaskService(session).cancel(task_id, user_id=user.id)
    await executor.cancel(task_id)
    return TaskResponse.model_validate(task)


@router.get("/{task_id}/trace", response_model=list[TraceItem])
async def list_task_trace(
    task_id: int,
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[TraceItem]:
    """回放任务轨迹（23.4 时间线数据源）：id 倒序 keyset 分页，最新在前。

    与 SSE 实时流同源（agent_trace_events 单一事实源），前端按时间戳升序渲染。
    """
    await TaskService(session).get_owned(task_id, user.id)  # 404 / 403
    rows = await TraceService(session).list_by_task_page(
        task_id, before_id=before_id, limit=limit
    )
    return [
        TraceItem.model_validate(
            {
                "id": row.id,
                "event": row.event_type.value,
                "task_id": task_id,
                "agent": row.agent,
                **(row.payload or {}),
                "timestamp": row.timestamp,
            }
        )
        for row in rows
    ]


@router.get("/{task_id}/events")
async def stream_task_events(
    task_id: int,
    user: User = Depends(get_current_user_sse),
    session: AsyncSession = Depends(get_session),
    redis: RedisService = Depends(get_redis_service),
) -> StreamingResponse:
    """订阅指定任务的实时事件流（text/event-stream）。"""
    # 归属校验（不存在 404 / 非本人 403），返回值不参与流转发
    await TaskService(session).get_owned(task_id, user.id)
    return StreamingResponse(
        task_event_stream(redis, task_id, settings.sse_heartbeat_seconds),
        media_type="text/event-stream",
        # no-cache 禁止中间层缓存事件流；X-Accel-Buffering 关闭 nginx 响应缓冲
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

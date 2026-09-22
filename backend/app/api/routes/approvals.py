"""审批中心 API（23.3）：列表 + 决定（approve / reject）。

- 认证沿用 Prompt 3.4 Bearer 头；列表范围 = 当前用户作为申请人的审批记录
  （requester_id 过滤），task_title 由路由层批量回查补全；
- 决定前置：任务归属校验（TaskService.get_owned，不存在 404 / 非本人 403）且任务
  处于 waiting_approval（否则 409——含编排降级完成的存量 pending 记录，决定已无意义）；
- 决定落库经 ApprovalService（同事务写 approval_result 轨迹并发布到任务频道，18.4），
  随后按决定驱动任务：approved → waiting 回 running 并经执行器从 checkpoint 恢复编排
  （图内审批门按最新决定幂等放行）；rejected → waiting 转 cancelled（任务终止，
  复用执行器 cancel 写 task_cancelled 埋点）；
- 重复决定 409（审批记录 pending → 终态不可回转，18.1 状态机）。
"""
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.state import AgentStatus
from app.api.deps import get_current_user
from app.api.routes.tasks import get_task_executor
from app.core.errors import AuthError
from app.core.redis import RedisService, get_redis_service
from app.db.session import get_session
from app.models.agent import AgentApproval
from app.models.user import User
from app.repositories.agent_approval import AgentApprovalRepository
from app.repositories.agent_task import AgentTaskRepository
from app.schemas.approval import ApprovalDecideRequest, ApprovalResponse
from app.services.approval_service import ApprovalService
from app.services.task_executor import TaskExecutor
from app.services.task_service import TaskService

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


def _to_response(approval: AgentApproval, titles: dict[int, str]) -> ApprovalResponse:
    """ORM + payload → 响应模型（工具列名 action；risk_level / parameter_summary 存于 payload）。"""
    payload = approval.payload or {}
    return ApprovalResponse(
        id=approval.id,
        task_id=approval.task_id,
        task_title=titles.get(approval.task_id),
        tool=approval.action,
        risk_level=payload.get("risk_level"),
        parameter_summary=payload.get("parameter_summary"),
        requester_id=approval.requester_id,
        status=approval.status,
        decision=approval.decision,
        comment=approval.comment,
        created_at=approval.created_at,
        decided_at=approval.decided_at,
    )


@router.get("", response_model=list[ApprovalResponse])
async def list_approvals(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ApprovalResponse]:
    """列出本人任务触发的审批记录（最新在前，上限 50）。"""
    approvals = await AgentApprovalRepository(session).list_by_requester(user.id)
    tasks = await AgentTaskRepository(session).list_by_ids([a.task_id for a in approvals])
    titles = {task.id: task.title for task in tasks}
    return [_to_response(approval, titles) for approval in approvals]


@router.post("/{approval_id}/decide", response_model=ApprovalResponse)
async def decide_approval(
    approval_id: int,
    body: ApprovalDecideRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    redis: RedisService = Depends(get_redis_service),
    executor: TaskExecutor = Depends(get_task_executor),
) -> ApprovalResponse:
    """决定审批并驱动任务：approved → 恢复编排继续；rejected → 任务终止。"""
    approval = await ApprovalService(session, redis=redis).get(approval_id)
    if approval is None:
        raise AuthError("APPROVAL_NOT_FOUND", "审批不存在", status_code=404)

    task = await TaskService(session).get_owned(approval.task_id, user.id)
    if task.status != AgentStatus.WAITING_APPROVAL.value:
        raise AuthError(
            "TASK_NOT_WAITING",
            f"任务不在等待审批状态（当前 {task.status}），无法决定",
            status_code=409,
        )

    service = ApprovalService(session, redis=redis)
    decide = service.approve if body.decision == "approved" else service.reject
    try:
        decided = await decide(approval_id, decided_by=user.id, comment=body.comment)
    except ValueError as exc:
        raise AuthError("APPROVAL_ALREADY_DECIDED", str(exc), status_code=409) from exc

    if body.decision == "approved":
        await TaskService(session).transition(task, AgentStatus.RUNNING)
        await executor.resume(approval.task_id, task.user_id)
    else:
        await TaskService(session).transition(task, AgentStatus.CANCELLED)
        await executor.cancel(approval.task_id)  # 无在跑协程，仅补 task_cancelled 埋点
    return _to_response(decided, {task.id: task.title})

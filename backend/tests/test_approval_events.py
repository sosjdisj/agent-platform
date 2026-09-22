"""Prompt 18.4：approval_required / approval_result 事件——Trace 落库 + 事件结构契约。

- 创建审批 → approval_required 事件随记录同事务落库；
- approve / reject → approval_result 事件落库（决定 / 决定人 / 备注）；
- 事件结构契约：Trace payload 可无损反序列化回 schemas.events 事件模型
  （该结构即后续 Redis Pub/Sub（Prompt 20）与前端（23.3）消费的单一来源）。
不做：SSE 推送与前端审批页面。
"""
from sqlalchemy import select

from app.mcp.base import RiskLevel
from app.models.agent import AgentTraceEvent, AgentTraceEventType
from app.repositories.agent_approval import AgentApprovalRepository
from app.schemas.events import ApprovalRequiredEvent, ApprovalResultEvent
from app.services.approval_service import ApprovalService


async def _events(db_session, task_id: int) -> list[AgentTraceEvent]:
    """按任务取全部轨迹事件（按 id 升序）。"""
    return list(
        await db_session.scalars(
            select(AgentTraceEvent)
            .where(AgentTraceEvent.task_id == task_id)
            .order_by(AgentTraceEvent.id)
        )
    )


async def test_create_writes_approval_required_event(db_session):
    """创建审批即写 approval_required 事件：payload 符合事件结构，字段完整。"""
    approval = await ApprovalService(db_session).create(
        task_id=601,
        requester_id=7,
        tool="refund_order",
        parameter_summary='{"order_id": 1, "reason": "质量问题"}',
        risk_level=RiskLevel.HIGH,
    )

    events = await _events(db_session, 601)
    assert [e.event_type for e in events] == [AgentTraceEventType.APPROVAL_REQUIRED]

    # 事件结构契约：Trace payload 无损还原为事件模型（SSE / Pub/Sub / 前端共用）
    event = ApprovalRequiredEvent.model_validate(events[0].payload)
    assert event.event == "approval_required"
    assert event.task_id == 601
    assert event.approval_id == approval.id
    assert event.tool == "refund_order"
    assert event.risk_level is RiskLevel.HIGH
    assert event.requester_id == 7
    assert event.parameter_summary == '{"order_id": 1, "reason": "质量问题"}'


async def test_decide_writes_approval_result_events(db_session):
    """approve / reject 各写一条 approval_result 事件：决定、决定人、备注随 payload 透出。"""
    svc = ApprovalService(db_session)
    approved_rec = await svc.create(
        task_id=602,
        requester_id=7,
        tool="update_customer",
        parameter_summary="{}",
        risk_level=RiskLevel.HIGH,
    )
    rejected_rec = await svc.create(
        task_id=602,
        requester_id=7,
        tool="update_customer",
        parameter_summary="{}",
        risk_level=RiskLevel.HIGH,
    )

    await svc.approve(approved_rec.id, decided_by=8, comment="同意")
    await svc.reject(rejected_rec.id, decided_by=9)

    events = await _events(db_session, 602)
    assert [e.event_type for e in events] == [
        AgentTraceEventType.APPROVAL_REQUIRED,
        AgentTraceEventType.APPROVAL_REQUIRED,
        AgentTraceEventType.APPROVAL_RESULT,
        AgentTraceEventType.APPROVAL_RESULT,
    ]

    approved_event = ApprovalResultEvent.model_validate(events[2].payload)
    assert approved_event.event == "approval_result"
    assert approved_event.approval_id == approved_rec.id
    assert approved_event.risk_level is RiskLevel.HIGH
    assert approved_event.decision == "approved"
    assert approved_event.decided_by == 8
    assert approved_event.comment == "同意"

    rejected_event = ApprovalResultEvent.model_validate(events[3].payload)
    assert rejected_event.approval_id == rejected_rec.id
    assert rejected_event.decision == "rejected"
    assert rejected_event.decided_by == 9
    assert rejected_event.comment is None


async def test_result_event_tolerates_legacy_payload_without_risk_level(db_session):
    """历史记录 payload 缺 risk_level（如直建记录）时决定仍可落库，事件 risk_level=None。"""
    approval = await AgentApprovalRepository(db_session).create(
        task_id=603,
        requester_id=7,
        action="refund_order",
        payload=None,
    )
    await db_session.commit()

    await ApprovalService(db_session).approve(approval.id, decided_by=1)

    events = await _events(db_session, 603)
    assert [e.event_type for e in events] == [AgentTraceEventType.APPROVAL_RESULT]
    event = ApprovalResultEvent.model_validate(events[0].payload)
    assert event.risk_level is None
    assert event.decision == "approved"

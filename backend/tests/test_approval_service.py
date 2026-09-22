"""Prompt 18.1：ApprovalService 全生命周期——创建 / 按 id 与 task_id 查询 / approve-reject 流转 / 非法流转被拒。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.mcp.base import RiskLevel
from app.services.approval_service import ApprovalService


async def test_full_lifecycle_approve(db_session):
    """创建即 pending → 按 id / task_id 查询命中 → approve 流转落时间戳 → 终态再流转被拒。"""
    svc = ApprovalService(db_session)
    approval = await svc.create(
        task_id=501,
        requester_id=7,
        tool="refund_order",
        parameter_summary='{"order_id": 1}',
        risk_level=RiskLevel.HIGH,
    )

    # 创建态：pending、字段完整、决定字段为空
    assert approval.status == "pending"
    assert approval.task_id == 501
    assert approval.requester_id == 7
    assert approval.action == "refund_order"
    assert approval.payload == {"parameter_summary": '{"order_id": 1}', "risk_level": "high"}
    assert approval.decision is None
    assert approval.decided_at is None

    # 按 id 与 task_id 查询；无命中时为空
    fetched = await svc.get(approval.id)
    assert fetched.id == approval.id
    assert [a.id for a in await svc.list_by_task(501)] == [approval.id]
    assert await svc.list_by_task(999) == []

    # approve 流转：终态 + 决定人 + 时间戳（近 10 秒内）
    decided = await svc.approve(approval.id, decided_by=8, comment="同意退款")
    assert decided.status == "approved"
    assert decided.decision == "approved"
    assert decided.decided_by == 8
    assert decided.comment == "同意退款"
    assert decided.decided_at > datetime.now(timezone.utc) - timedelta(seconds=10)

    # 终态不可再流转（两个方向均被拒）
    with pytest.raises(ValueError):
        await svc.approve(approval.id, decided_by=8)
    with pytest.raises(ValueError):
        await svc.reject(approval.id, decided_by=8)


async def test_reject_flow_and_illegal_reapprove(db_session):
    """reject 流转同样落时间戳；rejected 后不允许再 approve。"""
    svc = ApprovalService(db_session)
    approval = await svc.create(
        task_id=502,
        requester_id=7,
        tool="create_ticket",
        parameter_summary="{}",
        risk_level=RiskLevel.MEDIUM,
    )

    rejected = await svc.reject(approval.id, decided_by=9)

    assert rejected.status == "rejected"
    assert rejected.decision == "rejected"
    assert rejected.decided_by == 9
    assert rejected.decided_at is not None
    with pytest.raises(ValueError):
        await svc.approve(rejected.id, decided_by=9)


async def test_query_and_decide_on_missing_id(db_session):
    """按 id 查不到返回 None；对不存在记录流转直接拒绝。"""
    svc = ApprovalService(db_session)
    assert await svc.get(424242) is None
    assert await svc.list_by_task(424242) == []
    with pytest.raises(ValueError):
        await svc.approve(424242, decided_by=1)

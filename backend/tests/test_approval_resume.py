"""Prompt 18.3：approve / reject resume 流程——审批决定落库后从 checkpoint 恢复。

- approve → resume：HIGH 工具真实执行（订单退款），任务 COMPLETED，审批记录 approved，
  且恢复不重复建审批记录（幂等）；
- reject → resume：工具不执行（订单状态不变），LLM 生成不含该操作的 fallback 报告，
  任务 COMPLETED 且 AgentResult 标记 partial（error_code=APPROVAL_REJECTED）；
- 未决定即 resume：任务继续等待（waiting_approval），工具不执行、不重复建记录；
- BusinessAgent 全链路：reject → resume 产出带 partial 标记的 fallback 结果。
不做：SSE 通知。
"""
import json

import pytest

from app.agents.business import BusinessAgent, BusinessAgentError
from app.agents.runner import AgentRunner
from app.agents.state import AgentStatus
from app.mcp.business.refund_order import RefundOrderTool
from app.services.approval_service import ApprovalService
from tests.test_agent_graph import ScriptedLLM, final_response, make_mcp, tool_call_response
from tests.test_approval_interrupt import _approval, _order_status
from tests.test_mcp_business_tools import _first_order_id

USER_ID = 100
APPROVER_ID = 1


def _refund_script(order_id: int, final_content: str) -> ScriptedLLM:
    """脚本：请求退款 →（中断 / 恢复）→ 最终答复（内容按批准 / 拒绝路径给定）。"""
    return ScriptedLLM(
        [
            tool_call_response(
                "call_1",
                "refund_order",
                json.dumps({"order_id": order_id, "reason": "质量问题协商退款"}),
            ),
            final_response(final_content),
        ]
    )


def _runner(llm: ScriptedLLM, seeded_maker, checkpointer) -> AgentRunner:
    """接线审批流的单工具（refund_order）运行器。"""
    mcp, registry = make_mcp([RefundOrderTool(session_maker=seeded_maker)])
    return AgentRunner(llm, mcp, registry, checkpointer, approval_session_maker=seeded_maker)


async def test_approve_resumes_and_executes_tool(
    db_session, seeded_maker, checkpointer, case_ids
):
    """批准 → 恢复：工具真实执行（订单已退款），任务 COMPLETED，审批记录 approved 且不重复。"""
    (a_id, *_rest) = case_ids
    order_id = await _first_order_id(seeded_maker, a_id)
    runner = _runner(_refund_script(order_id, "退款已完成"), seeded_maker, checkpointer)

    interrupted = await runner.run(9101, USER_ID, [{"role": "user", "content": "退款"}])
    assert interrupted.status is AgentStatus.WAITING_APPROVAL

    approval = await _approval(db_session, 9101)
    await ApprovalService(db_session).approve(
        approval.id, decided_by=APPROVER_ID, comment="同意退款"
    )

    state = await runner.resume(9101)

    assert state.status is AgentStatus.COMPLETED
    assert state.final_result.content == "退款已完成"
    assert state.final_result.status is AgentStatus.COMPLETED
    assert state.tool_results[0].result.success is True
    assert await _order_status(db_session, order_id) == "refunded"

    # 恢复重入复用同一审批记录（幂等）：仍恰好一条，状态 approved + 决定信息齐全
    decided = await _approval(db_session, 9101)
    assert decided.status == "approved"
    assert decided.decided_by == APPROVER_ID
    assert decided.decided_at is not None


async def test_reject_resumes_with_fallback_report(
    db_session, seeded_maker, checkpointer, case_ids
):
    """拒绝 → 恢复：工具不执行（订单不变），生成不含该操作的报告——COMPLETED + partial 标记。"""
    (a_id, *_rest) = case_ids
    order_id = await _first_order_id(seeded_maker, a_id)
    status_before = await _order_status(db_session, order_id)
    runner = _runner(
        _refund_script(order_id, "退款申请被拒绝，本次未执行退款"), seeded_maker, checkpointer
    )

    await runner.run(9102, USER_ID, [{"role": "user", "content": "退款"}])
    approval = await _approval(db_session, 9102)
    await ApprovalService(db_session).reject(
        approval.id, decided_by=APPROVER_ID, comment="金额异常"
    )

    state = await runner.resume(9102)

    # 终态：流程完成但该操作未执行——COMPLETED + partial 标记，不计入执行错误
    assert state.status is AgentStatus.COMPLETED
    assert state.final_result.status is AgentStatus.PARTIAL
    assert state.final_result.error_code == "APPROVAL_REJECTED"
    assert state.final_result.content == "退款申请被拒绝，本次未执行退款"
    assert state.errors == []
    # 被拒工具调用记录可观测：未执行 + APPROVAL_REJECTED
    record = state.tool_results[0]
    assert record.result.success is False
    assert record.result.error_code == "APPROVAL_REJECTED"
    # 订单未被退款；审批记录 rejected
    assert await _order_status(db_session, order_id) == status_before
    decided = await _approval(db_session, 9102)
    assert decided.status == "rejected"
    assert decided.decided_at is not None


async def test_resume_before_decision_keeps_waiting(
    db_session, seeded_maker, checkpointer, case_ids
):
    """未决定即恢复：任务继续等待（waiting_approval），工具不执行、不重复建审批记录。"""
    (a_id, *_rest) = case_ids
    order_id = await _first_order_id(seeded_maker, a_id)
    runner = _runner(_refund_script(order_id, "退款已完成"), seeded_maker, checkpointer)

    await runner.run(9103, USER_ID, [{"role": "user", "content": "退款"}])
    state = await runner.resume(9103)

    assert state.status is AgentStatus.WAITING_APPROVAL
    assert state.tool_results == []
    assert await _order_status(db_session, order_id) != "refunded"
    await _approval(db_session, 9103)  # 仍恰好一条（幂等，未重复建）


async def test_business_agent_resume_after_reject(
    db_session, seeded_maker, checkpointer, case_ids
):
    """BusinessAgent 全链路：reject → resume 产出带 partial 标记的 fallback 结果。"""
    (a_id, *_rest) = case_ids
    order_id = await _first_order_id(seeded_maker, a_id)
    agent = BusinessAgent(
        _refund_script(order_id, "退款申请被拒绝，本次未执行退款"),
        session_maker=seeded_maker,
        checkpointer=checkpointer,
        approval_session_maker=seeded_maker,
    )

    with pytest.raises(BusinessAgentError):
        await agent.answer(task_id=9104, user_id=USER_ID, query="客户 A 首笔订单退款")

    approval = await _approval(db_session, 9104)
    await ApprovalService(db_session).reject(approval.id, decided_by=APPROVER_ID)

    result = await agent.resume(9104)

    assert result.status is AgentStatus.PARTIAL
    assert result.error_code == "APPROVAL_REJECTED"
    assert result.content == "退款申请被拒绝，本次未执行退款"
    assert await _order_status(db_session, order_id) != "refunded"

"""Prompt 18.2：LangGraph interrupt 接入——HIGH 工具执行前创建 Approval 并中断任务。

- refund_order（HIGH）：审批流接线后任务停在工具执行前，状态 waiting_approval，
  checkpoint 保留完整上下文，审批记录入库，订单未被退款；
- BusinessAgent 接线后 answer() 不产出结果（任务未完成），审批记录已入库；
- LOW 工具不受审批门控影响，正常完成（接线只影响 requires_approval=true 的工具）。
不做：resume（18.3）。
"""
import json

import pytest
from sqlalchemy import select

from app.agents.business import BusinessAgent, BusinessAgentError
from app.agents.runner import AgentRunner
from app.agents.state import AgentStatus
from app.mcp.business.get_crm_summary import GetCrmSummaryTool
from app.mcp.business.refund_order import RefundOrderTool
from app.models.agent import AgentApproval
from app.models.order import Order
from tests.test_agent_graph import ScriptedLLM, final_response, make_mcp, tool_call_response
from tests.test_mcp_business_tools import _first_order_id

TASK_ID = 9001
USER_ID = 100


def _refund_llm(order_id: int) -> ScriptedLLM:
    """脚本：请求退款后给最终答复（答复轮不会到达——工具执行前即中断）。"""
    return ScriptedLLM(
        [
            tool_call_response(
                "call_1",
                "refund_order",
                json.dumps({"order_id": order_id, "reason": "质量问题协商退款"}),
            ),
            final_response("退款完成"),  # 不可达：interrupt 后任务停住
        ]
    )


async def _approval(db_session, task_id: int) -> AgentApproval:
    """取任务的审批记录（断言前置：必须恰好一条）。"""
    approvals = list(
        await db_session.scalars(select(AgentApproval).where(AgentApproval.task_id == task_id))
    )
    assert len(approvals) == 1
    return approvals[0]


async def test_refund_interrupts_before_execution(db_session, seeded_maker, checkpointer, case_ids):
    """HIGH 工具（refund_order）：先建审批记录，随后中断——任务 waiting_approval、工具未执行、
    checkpoint 状态完整（messages 含工具请求、task/user 上下文齐全）。"""
    (a_id, *_rest) = case_ids
    order_id = await _first_order_id(seeded_maker, a_id)
    registry_mcp = make_mcp([RefundOrderTool(session_maker=seeded_maker)])
    runner = AgentRunner(
        _refund_llm(order_id),
        registry_mcp[0],
        registry_mcp[1],
        checkpointer,
        approval_session_maker=seeded_maker,
    )

    state = await runner.run(TASK_ID, USER_ID, [{"role": "user", "content": "退款"}])

    # 任务停在中断点：状态 waiting_approval，工具未执行
    assert state.status is AgentStatus.WAITING_APPROVAL
    assert state.tool_results == []
    assert state.errors == []
    # checkpoint 状态完整：恢复所需上下文齐全（消息含 refund_order 工具请求）
    persisted = await runner.state(TASK_ID)
    assert persisted.status is AgentStatus.WAITING_APPROVAL  # interrupt 可观测
    assert persisted.task_id == TASK_ID
    assert persisted.user_id == USER_ID
    assert persisted.messages[-1]["tool_calls"][0]["function"]["name"] == "refund_order"

    # 审批记录入库：pending + 完整请求上下文
    approval = await _approval(db_session, TASK_ID)
    assert approval.status == "pending"
    assert approval.action == "refund_order"
    assert approval.requester_id == USER_ID
    assert approval.payload["risk_level"] == "high"
    assert json.loads(approval.payload["parameter_summary"]) == {
        "order_id": order_id,
        "reason": "质量问题协商退款",
    }

    # 工具未真实执行：订单状态未被退款
    assert await _order_status(db_session, order_id) != "refunded"


async def test_business_agent_refund_stops_for_approval(db_session, seeded_maker, checkpointer, case_ids):
    """BusinessAgent 接线审批流：refund 触发 interrupt，answer 不产出结果，审批记录已入库。"""
    (a_id, *_rest) = case_ids
    order_id = await _first_order_id(seeded_maker, a_id)
    agent = BusinessAgent(
        _refund_llm(order_id),
        session_maker=seeded_maker,
        checkpointer=checkpointer,
        approval_session_maker=seeded_maker,
    )

    with pytest.raises(BusinessAgentError):
        await agent.answer(task_id=9002, user_id=USER_ID, query="客户 A 首笔订单退款")

    approval = await _approval(db_session, 9002)
    assert approval.status == "pending"
    assert approval.action == "refund_order"
    assert await _order_status(db_session, order_id) != "refunded"


async def test_low_risk_tool_not_interrupted(db_session, seeded_maker, checkpointer, case_ids):
    """LOW 工具（get_crm_summary）不受审批门控影响：正常完成，无审批记录。"""
    (a_id, *_rest) = case_ids
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "get_crm_summary", json.dumps({"customer_id": a_id})),
            final_response("概况如下"),
        ]
    )
    mcp, registry = make_mcp([GetCrmSummaryTool(session_maker=seeded_maker)])
    runner = AgentRunner(llm, mcp, registry, checkpointer, approval_session_maker=seeded_maker)

    state = await runner.run(9003, USER_ID, [{"role": "user", "content": "概况"}])

    assert state.status is AgentStatus.COMPLETED
    assert state.tool_results[0].result.success is True
    assert await db_session.scalar(select(AgentApproval.id).limit(1)) is None


async def _order_status(db_session, order_id: int) -> str:
    return await db_session.scalar(select(Order.status).where(Order.id == order_id))

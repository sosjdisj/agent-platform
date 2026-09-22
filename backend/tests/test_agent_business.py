"""BusinessAgent 测试：脚本化 LLM + 真实业务工具（测试库 seed 数据）。

覆盖（14.1）：验收场景（get_crm_summary / create_ticket 两工具）/ 多工具组合
（查概况 → 建工单）/ 无需工具直接回答 / 白名单工具面与校验 / 来源组装 / 调试接口。
覆盖（14.2）：高风险工具（update_customer / refund_order）调用流程、调用前风险识别
（risk_level / requires_approval / parameter_summary 随 AgentResult 透出）、
工具参数校验（Pydantic 非法参数 → INVALID_PARAMS → 失败链路）。
"""

import json
import re

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.agents.business import (
    BUSINESS_AGENT_NAME,
    BUSINESS_SYSTEM_PROMPT,
    BUSINESS_TOOLS,
    BusinessAgent,
    BusinessAgentError,
    assemble_business_result,
    validate_business_tools,
)
from app.agents.state import AgentResult, AgentState, AgentStatus, ToolCallRecord
from app.mcp.base import BaseTool, RiskLevel, ToolResult
from app.mcp.business.create_ticket import CreateTicketTool
from app.models.customer import Customer
from app.models.order import Order
from app.models.customer_ticket import CustomerTicket
from tests.test_agent_graph import ScriptedLLM, final_response, tool_call_response
from tests.test_mcp_business_tools import _first_order_id


def agent(llm: ScriptedLLM, session_maker) -> BusinessAgent:
    """构造注入脚本 LLM 与测试库会话工厂的 BusinessAgent（图仅依赖 chat 接口）。"""
    return BusinessAgent(llm, session_maker=session_maker)


def llm_tool_names(llm: ScriptedLLM) -> list[str]:
    """首轮 LLM 请求携带的工具面名称（核对白名单确实暴露给模型）。"""
    return [tool["function"]["name"] for tool in llm.calls[0]["tools"]]


async def test_crm_summary_calls_needed_tool(seeded_maker, case_ids) -> None:
    """验收：客户概况问题只调用 get_crm_summary，输出带工具来源的合法 AgentResult。"""
    (a_id, _b_id, _p1_id, _p2_id) = case_ids
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "get_crm_summary", json.dumps({"customer_id": a_id})),
            final_response("客户 A（华信智造）共 5 张订单、3 个未解决工单，需重点跟进。"),
        ]
    )

    result = await agent(llm, seeded_maker).answer(
        task_id=1, user_id=100, query="客户 A 的 CRM 概况怎么样？"
    )

    assert result.agent == BUSINESS_AGENT_NAME
    assert result.status is AgentStatus.COMPLETED
    assert result.error_code is None
    assert result.content == "客户 A（华信智造）共 5 张订单、3 个未解决工单，需重点跟进。"
    assert [s.ref_id for s in result.sources] == [f"get_crm_summary(customer_id={a_id})"]
    assert all(s.source_type == "tool" for s in result.sources)
    # 调用前风险识别：LOW 只读工具不标注待审批
    assert result.risk_assessments[0].tool == "get_crm_summary"
    assert result.risk_assessments[0].risk_level is RiskLevel.LOW
    assert result.risk_assessments[0].requires_approval is False
    # 工具面 = 业务白名单工具，系统提示指导选工具
    assert llm_tool_names(llm) == list(BUSINESS_TOOLS)
    assert BUSINESS_SYSTEM_PROMPT in llm.calls[0]["messages"][0]["content"]


async def test_create_ticket_flow_writes_db(seeded_maker, case_ids) -> None:
    """验收：创建工单流程走通——create_ticket 落库生成工单号，来源含写操作调用。"""
    (a_id, _b_id, _p1_id, _p2_id) = case_ids
    llm = ScriptedLLM(
        [
            tool_call_response(
                "call_1",
                "create_ticket",
                json.dumps(
                    {
                        "customer_id": a_id,
                        "title": "发货延迟",
                        "content": "客户反馈订单迟迟未发货",
                        "priority": "high",
                    }
                ),
            ),
            final_response("已为客户 A 创建高优先级工单，稍后有专人跟进。"),
        ]
    )

    result = await agent(llm, seeded_maker).answer(
        task_id=1, user_id=100, query="给客户 A 建一个发货延迟的高优工单"
    )

    assert result.status is AgentStatus.COMPLETED
    assert result.content == "已为客户 A 创建高优先级工单，稍后有专人跟进。"
    assert result.sources[0].ref_id.startswith("create_ticket(")
    assert f"customer_id={a_id}" in result.sources[0].ref_id

    # 写操作真实落库：工单存在且字段与入参一致（工单号符合 T-YYMM-NNN 规则）
    async with seeded_maker() as session:
        ticket = await session.scalar(
            select(CustomerTicket).where(
                CustomerTicket.customer_id == a_id, CustomerTicket.title == "发货延迟"
            )
        )
    assert ticket is not None
    assert ticket.priority == "high"
    assert re.fullmatch(r"T-\d{4}-\d{3}", ticket.ticket_no)


async def test_multi_tool_flow_summary_then_ticket(seeded_maker, case_ids) -> None:
    """多工具组合：先查客户 CRM 概况，再创建工单，两个工具调用各自成源。"""
    (a_id, _b_id, _p1_id, _p2_id) = case_ids
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "get_crm_summary", json.dumps({"customer_id": a_id})),
            tool_call_response(
                "call_2",
                "create_ticket",
                json.dumps({"customer_id": a_id, "title": "发货延迟", "content": "客户投诉发货慢"}),
            ),
            final_response("客户 A 已有 3 个未解决工单，又新建 1 张中优工单。"),
        ]
    )

    result = await agent(llm, seeded_maker).answer(
        task_id=1, user_id=100, query="看下客户 A 情况，如有需要就建工单跟进发货问题"
    )

    assert result.status is AgentStatus.COMPLETED
    assert len(result.sources) == 2
    assert result.sources[0].ref_id == f"get_crm_summary(customer_id={a_id})"
    assert result.sources[1].ref_id.startswith("create_ticket(")


async def test_direct_answer_without_tools() -> None:
    """判断无需工具：直接回答，不调任何工具，来源为空（默认 SessionLocal 不触库）。"""
    llm = ScriptedLLM([final_response("请问需要我查哪位客户的情况，或创建什么工单？")])

    result = await BusinessAgent(llm).answer(task_id=1, user_id=100, query="在吗")

    assert result.status is AgentStatus.COMPLETED
    assert result.sources == []
    assert llm_tool_names(llm) == list(BUSINESS_TOOLS)  # 无需工具也不改变工具面


def test_tool_surface_is_whitelist() -> None:
    """工具面 = 业务白名单 4 工具（注册顺序与白名单一致），构造时即完成校验。"""
    assert BusinessAgent(ScriptedLLM([])).tool_names == BUSINESS_TOOLS


def test_validate_rejects_risk_mismatch() -> None:
    """白名单校验：风险等级与白名单声明不符（如 create_ticket 标成 HIGH）拒绝。"""

    class MislabeledTicket(CreateTicketTool):
        risk_level = RiskLevel.HIGH

    with pytest.raises(ValueError, match="白名单"):
        validate_business_tools([MislabeledTicket()])


def test_validate_rejects_tool_outside_whitelist() -> None:
    """白名单校验：非白名单工具（如订单更新）一律拒绝，不得混入工具面。"""

    class FakeInput(BaseModel):
        x: int = 1

    class FakeOutput(BaseModel):
        y: int = 1

    class UpdateOrdersTool(BaseTool):
        name = "update_orders"
        description = "不在白名单的写入工具"
        risk_level = RiskLevel.HIGH
        InputModel = FakeInput
        OutputModel = FakeOutput

        async def run(self, params: FakeInput) -> FakeOutput:
            return FakeOutput()

    with pytest.raises(ValueError, match="白名单"):
        validate_business_tools([UpdateOrdersTool()])


async def test_customer_not_found_raises_business_agent_error(seeded_maker) -> None:
    """客户不存在：工具业务错误（CUSTOMER_NOT_FOUND）→ 状态 failed → 抛 BusinessAgentError。"""
    llm = ScriptedLLM(
        [tool_call_response("call_1", "get_crm_summary", json.dumps({"customer_id": 999999}))]
    )

    with pytest.raises(BusinessAgentError) as exc_info:
        await agent(llm, seeded_maker).answer(task_id=1, user_id=100, query="客户 X 概况")

    assert "CUSTOMER_NOT_FOUND" in str(exc_info.value)


async def test_update_customer_flow_pending_approval(seeded_maker, case_ids) -> None:
    """高风险工具（update_customer）：调用前风险识别透出 requires_approval=true，写操作真实落库。"""
    (a_id, _b_id, _p1_id, _p2_id) = case_ids
    arguments = {"customer_id": a_id, "contact_name": "李四"}
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "update_customer", json.dumps(arguments)),
            final_response("已提交客户 A 主数据更新（联系人改为李四），待人工审批后生效。"),
        ]
    )

    result = await agent(llm, seeded_maker).answer(
        task_id=1, user_id=100, query="把客户 A 的联系人改成李四"
    )

    assert result.status is AgentStatus.COMPLETED
    assert result.content.endswith("待人工审批后生效。")
    # 调用前风险识别：HIGH → requires_approval=true，参数摘要与调用参数一致
    (assessment,) = result.risk_assessments
    assert assessment.tool == "update_customer"
    assert assessment.risk_level is RiskLevel.HIGH
    assert assessment.requires_approval is True
    assert assessment.parameter_summary == json.dumps(arguments, ensure_ascii=False, sort_keys=True)

    # 写操作真实落库（审批仅占位不阻断）：联系人已更新
    async with seeded_maker() as session:
        assert await session.scalar(
            select(Customer.contact_name).where(Customer.id == a_id)
        ) == "李四"


async def test_refund_order_flow_pending_approval(seeded_maker, case_ids) -> None:
    """高风险工具（refund_order）：风险识别透出 requires_approval=true，退款真实落库。"""
    (a_id, _b_id, _p1_id, _p2_id) = case_ids
    order_id = await _first_order_id(seeded_maker, a_id)
    llm = ScriptedLLM(
        [
            tool_call_response(
                "call_1",
                "refund_order",
                json.dumps({"order_id": order_id, "reason": "质量问题协商退款"}),
            ),
            final_response("订单退款申请已提交，待人工审批后完成退款。"),
        ]
    )

    result = await agent(llm, seeded_maker).answer(
        task_id=1, user_id=100, query="客户 A 首笔订单质量问题，办理退款"
    )

    assert result.status is AgentStatus.COMPLETED
    (assessment,) = result.risk_assessments
    assert assessment.tool == "refund_order"
    assert assessment.risk_level is RiskLevel.HIGH
    assert assessment.requires_approval is True
    assert f'"order_id": {order_id}' in assessment.parameter_summary

    # 退款真实落库：订单状态已流转（审批仅占位不阻断）
    async with seeded_maker() as session:
        assert await session.scalar(select(Order.status).where(Order.id == order_id)) == "refunded"


async def test_invalid_params_fail_agent_run(seeded_maker, case_ids) -> None:
    """参数校验：LLM 传入非法参数（update_customer 无待更新字段）→ INVALID_PARAMS → 失败链路。"""
    (a_id, _b_id, _p1_id, _p2_id) = case_ids
    llm = ScriptedLLM(
        [tool_call_response("call_1", "update_customer", json.dumps({"customer_id": a_id}))]
    )

    with pytest.raises(BusinessAgentError) as exc_info:
        await agent(llm, seeded_maker).answer(task_id=1, user_id=100, query="更新客户 A")

    assert "INVALID_PARAMS" in str(exc_info.value)
    assert "至少提供一个待更新字段" in str(exc_info.value)


def _record(tool: str, arguments: dict | None = None, *, success: bool = True) -> ToolCallRecord:
    """一次工具调用记录（success=False 模拟失败调用）。"""
    result = ToolResult.ok({"ticket_no": "T-2609-001"}) if success else ToolResult.fail("X", "失败")
    return ToolCallRecord(tool=tool, arguments=arguments or {}, result=result)


def test_assemble_dedupes_sources_and_skips_failures() -> None:
    """来源组装：按 ref_id 去重保留首次引用，失败调用不进入来源（共享规则见 sources.py）。"""
    state = AgentState(
        task_id=1,
        user_id=100,
        messages=[],
        status=AgentStatus.COMPLETED,
        tool_results=[
            _record("get_crm_summary", {"customer_id": 1}),
            _record("get_crm_summary", {"customer_id": 1}),  # 同参重复：去重
            _record("create_ticket", {"customer_id": 1, "title": "a"}),
            _record("create_ticket", success=False),
        ],
        final_result=AgentResult(agent="assistant", content="答案", sources=[]),
    )

    result = assemble_business_result(state)

    assert [s.ref_id for s in result.sources] == [
        "get_crm_summary(customer_id=1)",
        "create_ticket(customer_id=1,title=a)",
    ]
    assert result.agent == BUSINESS_AGENT_NAME
    assert result.content == "答案"


def test_assemble_raises_on_failed_state() -> None:
    """失败状态（如轮次截断）：组装入口抛 BusinessAgentError，不产出半成品结果。"""
    state = AgentState(
        task_id=1,
        user_id=100,
        messages=[],
        status=AgentStatus.FAILED,
        errors=["已达最大轮次上限（5），任务截断"],
    )

    with pytest.raises(BusinessAgentError, match="最大轮次"):
        assemble_business_result(state)


async def test_debug_endpoint_returns_business_result(client, seeded_maker, case_ids) -> None:
    """调试接口：POST /api/debug/agents/business 全链路返回 AgentResult。"""
    from app.api.routes.debug_agents import get_business_agent
    from app.main import app

    (a_id, _b_id, _p1_id, _p2_id) = case_ids
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "get_crm_summary", json.dumps({"customer_id": a_id})),
            final_response("客户 A 共 5 张订单、3 个未解决工单。"),
        ]
    )
    business_agent = BusinessAgent(llm, session_maker=seeded_maker)
    app.dependency_overrides[get_business_agent] = lambda: business_agent
    try:
        resp = await client.post(
            "/api/debug/agents/business",
            json={"query": "客户 A 的 CRM 概况", "task_id": 7, "user_id": 1},
        )
    finally:
        app.dependency_overrides.pop(get_business_agent, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == BUSINESS_AGENT_NAME
    assert body["status"] == "completed"
    assert body["error_code"] is None
    assert body["content"] == "客户 A 共 5 张订单、3 个未解决工单。"
    assert body["sources"][0]["ref_id"] == f"get_crm_summary(customer_id={a_id})"


async def test_debug_endpoint_disabled_when_debug_off(client, monkeypatch) -> None:
    """非 debug 模式：调试接口返回 404，不暴露给生产环境。"""
    from app.api.routes.debug_agents import get_business_agent
    from app.core.config import get_settings
    from app.main import app

    app.dependency_overrides[get_business_agent] = lambda: None
    monkeypatch.setattr(get_settings(), "debug", False)
    try:
        resp = await client.post("/api/debug/agents/business", json={"query": "在吗"})
    finally:
        app.dependency_overrides.pop(get_business_agent, None)

    assert resp.status_code == 404

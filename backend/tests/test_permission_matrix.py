"""Prompt 17.3：权限测试矩阵——employee / sales / admin × 查询 / 高危写操作。

用真实工具（注入测试库会话工厂）+ 种子业务数据验证 RBAC 矩阵：
- employee（customer:read / order:read / knowledge:search）：正常查询，refund 被拒；
- sales（+ customer:write / sales:read / ticket:create）：正常查询，refund 被拒；
- admin（全部权限）：查询 / 工单 / 退款全通过。
复用 test_agent_graph 的 LLM 桩与图构建辅助（单一来源，不重复造轮子）。
"""
import json

import pytest
from openai.types.chat import ChatCompletion
from sqlalchemy import func, insert, select

from app.agents.graph import build_agent_graph
from app.agents.state import AgentState, AgentStatus
from app.mcp.business.create_ticket import CreateTicketTool
from app.mcp.business.refund_order import RefundOrderTool
from app.mcp.database.query_customers import QueryCustomersTool
from app.mcp.database.query_product_sales import QueryProductSalesTool
from app.models.agent import AgentTraceEvent, AgentTraceEventType
from app.models.customer_ticket import CustomerTicket
from app.models.order import Order
from app.models.rbac import Role, user_roles
from app.models.user import User
from app.services.rbac_service import seed_rbac
from tests.test_agent_graph import (
    ScriptedLLM,
    completion,
    final_response,
    make_mcp,
    tool_call_response,
)


def multi_tool_call_response(calls: list[tuple[str, dict]]) -> ChatCompletion:
    """一条助手消息携带多个工具调用（单轮全量执行，验证 admin 多工具全通过）。"""
    return completion(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for i, (name, args) in enumerate(calls)
            ],
        }
    )


async def _seed_users(db_session) -> dict[str, int]:
    """seed RBAC 并创建 employee / sales / admin 三个用户，返回角色名 → 用户 id。"""
    await seed_rbac(db_session)
    users = [
        User(username="u_employee", email="emp@test.com", hashed_password="x"),
        User(username="u_sales", email="sales@test.com", hashed_password="x"),
        User(username="u_admin", email="admin@test.com", hashed_password="x"),
    ]
    db_session.add_all(users)
    await db_session.flush()
    role_ids = dict((await db_session.execute(select(Role.name, Role.id))).all())
    await db_session.execute(
        insert(user_roles),
        [
            {"user_id": user.id, "role_id": role_ids[role]}
            for user, role in zip(users, ("employee", "sales", "admin"))
        ],
    )
    await db_session.commit()
    return dict(zip(("employee", "sales", "admin"), (user.id for user in users)))


def _real_tools(db_session_maker):
    """矩阵涉及的真实工具（权限码：customer:read / sales:read / ticket:create / order:refund）。"""
    return [
        QueryCustomersTool(session_maker=db_session_maker),
        QueryProductSalesTool(session_maker=db_session_maker),
        CreateTicketTool(session_maker=db_session_maker),
        RefundOrderTool(session_maker=db_session_maker),
    ]


def _build_graph(db_session_maker, user_id: int, task_id: int, responses: list):
    """构造带权限门控的被测图：工具会话指向测试库，LLM 按脚本逐轮响应。"""
    mcp, registry = make_mcp(_real_tools(db_session_maker))
    graph = build_agent_graph(
        ScriptedLLM(responses),
        mcp,
        registry,
        auth_session_maker=db_session_maker,
        trace_session_maker=db_session_maker,
    )
    state = AgentState(
        task_id=task_id,
        user_id=user_id,
        messages=[{"role": "user", "content": "按脚本执行"}],
    )
    return graph, state


async def _denied_count(db_session) -> int:
    """当前库中 permission_denied 轨迹事件数（允许路径应为 0）。"""
    return await db_session.scalar(
        select(func.count())
        .select_from(AgentTraceEvent)
        .where(AgentTraceEvent.event_type == AgentTraceEventType.PERMISSION_DENIED)
    )


async def test_employee_can_query(db_session, db_session_maker, case_ids):
    """矩阵 1：employee 正常查询（customer:read）——成功返回客户数据，无拒绝事件。"""
    a_id, *_ = case_ids
    user_ids = await _seed_users(db_session)
    graph, state = _build_graph(
        db_session_maker,
        user_ids["employee"],
        task_id=1101,
        responses=[
            tool_call_response(
                "c1", "query_customers", json.dumps({"customer_id": a_id})
            ),
            final_response("查到了"),
        ],
    )

    result = AgentState.model_validate(await graph.ainvoke(state))

    assert result.status is AgentStatus.COMPLETED
    record = result.tool_results[0]
    assert record.result.success is True
    assert record.result.data["customers"][0]["code"] == "CUST-0001"
    assert await _denied_count(db_session) == 0


async def test_sales_can_query(db_session, db_session_maker, seeded_maker):
    """矩阵 2：sales 正常查询销售数据（sales:read，employee 不具备）——成功返回聚合行。"""
    user_ids = await _seed_users(db_session)
    graph, state = _build_graph(
        db_session_maker,
        user_ids["sales"],
        task_id=1102,
        responses=[
            tool_call_response("c1", "query_product_sales", json.dumps({})),
            final_response("查到了"),
        ],
    )

    result = AgentState.model_validate(await graph.ainvoke(state))

    assert result.status is AgentStatus.COMPLETED
    record = result.tool_results[0]
    assert record.result.success is True
    assert record.result.data["count"] >= 1
    assert await _denied_count(db_session) == 0


@pytest.mark.parametrize("role", ["employee", "sales"])
async def test_refund_denied_for_limited_roles(
    db_session, db_session_maker, seeded_maker, role
):
    """矩阵 3/4：employee 与 sales 均无 order:refund——refund 被拒、订单未动、拒绝事件落 Trace。"""
    user_ids = await _seed_users(db_session)
    user_id, task_id = user_ids[role], {"employee": 1201, "sales": 1202}[role]
    order_id = await db_session.scalar(select(Order.id).order_by(Order.id).limit(1))
    status_before = await db_session.scalar(
        select(Order.status).where(Order.id == order_id)
    )
    graph, state = _build_graph(
        db_session_maker,
        user_id,
        task_id=task_id,
        responses=[
            tool_call_response(
                "c1",
                "refund_order",
                json.dumps({"order_id": order_id, "reason": "矩阵测试退款"}),
            ),
            final_response("不应到达"),
        ],
    )

    result = AgentState.model_validate(await graph.ainvoke(state))

    # 权限拒绝为业务决定：不计入执行错误，LLM 以 fallback 报告正常收束（COMPLETED）
    assert result.status is AgentStatus.COMPLETED
    record = result.tool_results[0]
    assert record.result.success is False
    assert record.result.error_code == "PERMISSION_DENIED"

    event = await db_session.scalar(
        select(AgentTraceEvent).where(
            AgentTraceEvent.task_id == task_id,
            AgentTraceEvent.event_type == AgentTraceEventType.PERMISSION_DENIED,
        )
    )
    assert event is not None
    assert event.payload == {
        "tool": "refund_order",
        "metadata": {"required_permission": "order:refund", "user_id": user_id},
    }
    # 拒绝即不执行：订单状态保持原样
    status_after = await db_session.scalar(
        select(Order.status).where(Order.id == order_id)
    )
    assert status_after == status_before


async def test_admin_all_tools_pass(db_session, db_session_maker, case_ids):
    """矩阵 5：admin 全部权限——查询 / 工单 / 退款单轮全通过，写操作真实生效。"""
    a_id, *_ = case_ids
    user_ids = await _seed_users(db_session)
    order_id = await db_session.scalar(select(Order.id).order_by(Order.id).limit(1))
    tickets_before = await db_session.scalar(
        select(func.count()).select_from(CustomerTicket)
    )
    graph, state = _build_graph(
        db_session_maker,
        user_ids["admin"],
        task_id=1301,
        responses=[
            multi_tool_call_response(
                [
                    ("query_customers", {"customer_id": a_id}),
                    ("query_product_sales", {}),
                    ("create_ticket", {"customer_id": a_id, "title": "矩阵工单", "content": "测试"}),
                    ("refund_order", {"order_id": order_id, "reason": "矩阵测试退款"}),
                ]
            ),
            final_response("全部完成"),
        ],
    )

    result = AgentState.model_validate(await graph.ainvoke(state))

    assert result.status is AgentStatus.COMPLETED
    assert [r.result.success for r in result.tool_results] == [True] * 4
    assert await _denied_count(db_session) == 0
    # 写操作真实生效：订单已退款、工单已落库
    assert await db_session.scalar(select(Order.status).where(Order.id == order_id)) == "refunded"
    assert (
        await db_session.scalar(select(func.count()).select_from(CustomerTicket))
        == tickets_before + 1
    )

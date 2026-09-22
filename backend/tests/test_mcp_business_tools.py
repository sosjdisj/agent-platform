"""business 子模块工具测试（Prompt 8.1 / 8.2）。

覆盖四个工具：
- get_crm_summary（LOW）：客户 A / B 全景视图数值与 seed 数据一致；客户不存在 / 参数非法；
- create_ticket（MEDIUM，写入走 Service 层）：成功落库（DB 断言字段与计数）、工单号递增、
  客户不存在时不产生写入、参数非法（Pydantic 校验）不产生写入；
- update_customer（HIGH）：部分更新落库、重复调用幂等、不存在 / 参数非法不产生写入，
  成功响应携带 requires_approval=true 风险标注；
- refund_order（HIGH）：退款记录 + 订单状态落库、重复调用幂等（不重复退款）、
  不存在 / 参数非法不产生写入，成功响应携带 requires_approval=true 风险标注。

数据：复用 app.seed（内部 commit），工具通过测试专用 session_maker 开短会话。
"""
import re

from sqlalchemy import func, select

from app.mcp.business.create_ticket import CreateTicketTool
from app.mcp.business.get_crm_summary import GetCrmSummaryTool
from app.mcp.business.refund_order import RefundOrderTool
from app.mcp.business.update_customer import UpdateCustomerTool
from app.models.customer import Customer
from app.models.customer_ticket import CustomerTicket
from app.models.order import Order
from app.models.refund import Refund

NOT_EXIST_ID = 99999
TICKET_NO_PATTERN = re.compile(r"^T-\d{4}-\d{3}$")  # T-YYMM-NNN

# seed 案例数据期望值（与 docs 案例链一致）：
# 客户 A：12 单 = P1 2100×1200 + P2 240×150 = 2,556,000；工单 3 条（1 条未解决 T-2608-002）
# 客户 B：12 单 = P1 2970×1200 + P2 360×150 = 3,618,000；工单 2 条（全部已解决）
A_ORDER_AMOUNT = 2_556_000.0
B_ORDER_AMOUNT = 3_618_000.0


async def _ticket_count(maker, customer_id: int | None = None) -> int:
    """统计工单行数，用于写入前后对比断言。"""
    stmt = select(func.count()).select_from(CustomerTicket)
    if customer_id is not None:
        stmt = stmt.where(CustomerTicket.customer_id == customer_id)
    async with maker() as session:
        return await session.scalar(stmt)


async def _refund_count(maker, order_id: int | None = None) -> int:
    """统计退款记录行数，用于写入与幂等断言。"""
    stmt = select(func.count()).select_from(Refund)
    if order_id is not None:
        stmt = stmt.where(Refund.order_id == order_id)
    async with maker() as session:
        return await session.scalar(stmt)


async def _first_order_id(maker, customer_id: int) -> int:
    """取客户最早一单的 id，作为退款测试对象。"""
    async with maker() as session:
        return await session.scalar(
            select(Order.id).where(Order.customer_id == customer_id).order_by(Order.id).limit(1)
        )


# ---------- get_crm_summary ----------


async def test_get_crm_summary_customer_a(seeded_maker, case_ids):
    """客户 A 全景视图：主数据 + 订单 / 销售 / 工单统计与 seed 数值一致。"""
    a_id, _, _, _ = case_ids

    payload = await GetCrmSummaryTool(session_maker=seeded_maker).execute(
        {"customer_id": a_id}
    )

    assert payload["success"] is True
    data = payload["data"]
    assert data["customer"] == {
        "id": a_id,
        "code": "CUST-0001",
        "name": "华信智造",
        "industry": "制造业",
        "region": "华东",
        "contact_name": "陈敏",
        "contact_phone": None,
        "status": "active",
    }
    assert data["order_stats"] == {"total_count": 12, "total_amount": A_ORDER_AMOUNT}
    # sales_records 由订单按（客户, 产品, 月）汇总生成，总量与订单严格一致
    assert data["sales_stats"] == {
        "total_quantity": 2340,
        "total_amount": A_ORDER_AMOUNT,
        "month_count": 12,  # P1 + P2 各 6 个月
    }
    assert data["ticket_stats"] == {"total_count": 3, "open_count": 1}


async def test_get_crm_summary_customer_b(seeded_maker, case_ids):
    """对照组客户 B：订单 / 销售数值一致，工单全部已解决。"""
    _, b_id, _, _ = case_ids

    payload = await GetCrmSummaryTool(session_maker=seeded_maker).execute(
        {"customer_id": b_id}
    )

    assert payload["success"] is True
    data = payload["data"]
    assert data["customer"]["code"] == "CUST-0002"
    assert data["order_stats"] == {"total_count": 12, "total_amount": B_ORDER_AMOUNT}
    assert data["ticket_stats"] == {"total_count": 2, "open_count": 0}


async def test_get_crm_summary_not_found(seeded_maker):
    """customer_id 不存在 → 统一结构化错误 CUSTOMER_NOT_FOUND。"""
    payload = await GetCrmSummaryTool(session_maker=seeded_maker).execute(
        {"customer_id": NOT_EXIST_ID}
    )

    assert payload["success"] is False
    assert payload["error_code"] == "CUSTOMER_NOT_FOUND"
    assert str(NOT_EXIST_ID) in payload["message"]


async def test_get_crm_summary_invalid_params(seeded_maker):
    """参数非法 → Pydantic 校验拒绝（INVALID_PARAMS）。"""
    tool = GetCrmSummaryTool(session_maker=seeded_maker)
    for raw in ({"customer_id": 0}, {}):
        payload = await tool.execute(raw)
        assert payload["success"] is False, f"参数应被拒绝: {raw}"
        assert payload["error_code"] == "INVALID_PARAMS"


# ---------- create_ticket ----------


async def test_create_ticket_success_and_persisted(seeded_maker, case_ids):
    """成功创建：返回字段齐全，DB 断言行已正确写入（写入走 Service 层）。"""
    a_id, _, _, _ = case_ids
    before = await _ticket_count(seeded_maker, a_id)

    payload = await CreateTicketTool(session_maker=seeded_maker).execute(
        {
            "customer_id": a_id,
            "title": "9 月交付计划确认",
            "content": "希望确认 9 月 XS-100 的交付计划与数量。",
            "priority": "high",
        }
    )

    assert payload["success"] is True
    ticket = payload["data"]["ticket"]
    assert ticket["customer_id"] == a_id
    assert ticket["title"] == "9 月交付计划确认"
    assert ticket["priority"] == "high"
    assert ticket["status"] == "active"
    assert ticket["resolved_at"] is None
    assert TICKET_NO_PATTERN.match(ticket["ticket_no"])

    # DB 断言：行已落库且字段与返回一致；该客户工单数 +1
    async with seeded_maker() as session:
        row = await session.get(CustomerTicket, ticket["id"])
        assert row is not None
        assert row.ticket_no == ticket["ticket_no"]
        assert row.customer_id == a_id
        assert row.content == "希望确认 9 月 XS-100 的交付计划与数量。"
        assert row.status == "active"
    assert await _ticket_count(seeded_maker, a_id) == before + 1


async def test_create_ticket_sequential_no(seeded_maker, case_ids):
    """工单号递增：同月内第二次创建序号为上一次 +1。"""
    a_id, _, _, _ = case_ids
    tool = CreateTicketTool(session_maker=seeded_maker)

    first = (await tool.execute({"customer_id": a_id, "title": "t1", "content": "c1"}))
    second = (await tool.execute({"customer_id": a_id, "title": "t2", "content": "c2"}))

    no1, no2 = (
        first["data"]["ticket"]["ticket_no"],
        second["data"]["ticket"]["ticket_no"],
    )
    assert TICKET_NO_PATTERN.match(no1) and TICKET_NO_PATTERN.match(no2)
    assert no1 != no2
    assert int(no2[-3:]) == int(no1[-3:]) + 1  # 当月序号 +1


async def test_create_ticket_customer_not_found_no_write(seeded_maker):
    """customer_id 不存在 → CUSTOMER_NOT_FOUND，且不产生任何写入。"""
    before = await _ticket_count(seeded_maker)

    payload = await CreateTicketTool(session_maker=seeded_maker).execute(
        {"customer_id": NOT_EXIST_ID, "title": "t", "content": "c"}
    )

    assert payload["success"] is False
    assert payload["error_code"] == "CUSTOMER_NOT_FOUND"
    assert await _ticket_count(seeded_maker) == before  # 无写入


async def test_create_ticket_invalid_params_no_write(seeded_maker):
    """参数非法（标题超长 / 非法优先级 / 缺字段 / 非法 ID）→ INVALID_PARAMS，无写入。"""
    tool = CreateTicketTool(session_maker=seeded_maker)
    bad_inputs = (
        {"customer_id": 1, "title": "", "content": "c"},  # 空标题
        {"customer_id": 1, "title": "t" * 201, "content": "c"},  # 超长标题
        {"customer_id": 1, "title": "t", "content": "", "priority": "high"},  # 空内容
        {"customer_id": 1, "title": "t", "content": "c", "priority": "critical"},  # 非法优先级
        {"title": "t", "content": "c"},  # 缺 customer_id
        {"customer_id": 0, "title": "t", "content": "c"},  # 低于下界
    )

    before = await _ticket_count(seeded_maker)
    for raw in bad_inputs:
        payload = await tool.execute(raw)
        assert payload["success"] is False, f"参数应被拒绝: {raw}"
        assert payload["error_code"] == "INVALID_PARAMS"
    assert await _ticket_count(seeded_maker) == before  # 全部无写入


# ---------- update_customer ----------


async def test_update_customer_success_and_persisted(seeded_maker, case_ids):
    """部分更新：仅提供的字段生效，DB 断言行已正确写入，响应携带 requires_approval=true。"""
    a_id, _, _, _ = case_ids

    payload = await UpdateCustomerTool(session_maker=seeded_maker).execute(
        {"customer_id": a_id, "name": "华信智造（更名）", "contact_phone": "13800000000"}
    )

    assert payload["success"] is True
    assert payload["requires_approval"] is True  # HIGH 风险标注
    customer = payload["data"]["customer"]
    assert customer["name"] == "华信智造（更名）"
    assert customer["contact_phone"] == "13800000000"

    async with seeded_maker() as session:
        row = await session.get(Customer, a_id)
        assert row.name == "华信智造（更名）"
        assert row.contact_phone == "13800000000"
        assert row.industry == "制造业"  # 未提供字段不受影响
        assert row.code == "CUST-0001"
        assert row.status == "active"


async def test_update_customer_idempotent(seeded_maker, case_ids):
    """幂等：同一更新重复调用结果一致，不产生额外行。"""
    a_id, _, _, _ = case_ids
    tool = UpdateCustomerTool(session_maker=seeded_maker)
    raw = {"customer_id": a_id, "region": "华南"}

    first = await tool.execute(raw)
    second = await tool.execute(raw)

    assert first["success"] and second["success"]
    assert first["data"]["customer"] == second["data"]["customer"]
    async with seeded_maker() as session:
        count = await session.scalar(
            select(func.count()).select_from(Customer).where(Customer.id == a_id)
        )
    assert count == 1


async def test_update_customer_not_found_no_change(seeded_maker, case_ids):
    """customer_id 不存在 → CUSTOMER_NOT_FOUND，客户数据无任何变化。"""
    a_id, _, _, _ = case_ids

    payload = await UpdateCustomerTool(session_maker=seeded_maker).execute(
        {"customer_id": NOT_EXIST_ID, "name": "不存在客户"}
    )

    assert payload["success"] is False
    assert payload["error_code"] == "CUSTOMER_NOT_FOUND"
    async with seeded_maker() as session:
        row = await session.get(Customer, a_id)
        assert row.name == "华信智造"  # 原数据未被动到


async def test_update_customer_invalid_params(seeded_maker):
    """参数非法（零字段更新 / 越界 ID / 空名称 / 超长名称）→ INVALID_PARAMS。"""
    tool = UpdateCustomerTool(session_maker=seeded_maker)
    for raw in (
        {"customer_id": 1},  # 未提供任何待更新字段（模型级校验）
        {"customer_id": 0, "name": "x"},
        {"customer_id": 1, "name": ""},  # 空名称
        {"customer_id": 1, "name": "x" * 101},  # 超长名称
    ):
        payload = await tool.execute(raw)
        assert payload["success"] is False, f"参数应被拒绝: {raw}"
        assert payload["error_code"] == "INVALID_PARAMS"


# ---------- refund_order ----------


async def test_refund_order_success_and_persisted(seeded_maker, case_ids):
    """成功退款：写入退款记录（原因 + 金额快照）并标记订单，响应携带 requires_approval=true。"""
    a_id, _, _, _ = case_ids
    order_id = await _first_order_id(seeded_maker, a_id)

    payload = await RefundOrderTool(session_maker=seeded_maker).execute(
        {"order_id": order_id, "reason": "质量问题协商退款"}
    )

    assert payload["success"] is True
    assert payload["requires_approval"] is True  # HIGH 风险标注
    assert payload["data"]["already_refunded"] is False
    assert payload["data"]["order"]["id"] == order_id
    assert payload["data"]["order"]["status"] == "refunded"

    # DB 断言：一条退款记录（金额 = 订单金额）+ 订单状态已流转
    async with seeded_maker() as session:
        order = await session.get(Order, order_id)
        refund = await session.scalar(select(Refund).where(Refund.order_id == order_id))
        assert refund.reason == "质量问题协商退款"
        assert refund.amount == order.amount
        assert order.status == "refunded"
    assert await _refund_count(seeded_maker, order_id) == 1


async def test_refund_order_idempotent(seeded_maker, case_ids):
    """幂等：重复退款不重复写入退款记录，返回 already_refunded=true。"""
    a_id, _, _, _ = case_ids
    order_id = await _first_order_id(seeded_maker, a_id)
    tool = RefundOrderTool(session_maker=seeded_maker)

    first = await tool.execute({"order_id": order_id, "reason": "首次退款原因"})
    second = await tool.execute({"order_id": order_id, "reason": "重复调用原因"})

    assert first["success"] and second["success"]
    assert first["data"]["already_refunded"] is False
    assert second["data"]["already_refunded"] is True
    assert second["data"]["order"]["status"] == "refunded"
    # 仅一条退款记录，原因保留首次值
    async with seeded_maker() as session:
        refund = await session.scalar(select(Refund).where(Refund.order_id == order_id))
        assert refund.reason == "首次退款原因"
    assert await _refund_count(seeded_maker, order_id) == 1


async def test_refund_order_not_found_no_write(seeded_maker):
    """order_id 不存在 → ORDER_NOT_FOUND，不产生任何写入。"""
    payload = await RefundOrderTool(session_maker=seeded_maker).execute(
        {"order_id": NOT_EXIST_ID, "reason": "r"}
    )

    assert payload["success"] is False
    assert payload["error_code"] == "ORDER_NOT_FOUND"
    assert await _refund_count(seeded_maker) == 0


async def test_refund_order_invalid_params_no_write(seeded_maker):
    """参数非法（越界 ID / 缺原因 / 空原因 / 超长原因）→ INVALID_PARAMS，无写入。"""
    tool = RefundOrderTool(session_maker=seeded_maker)
    bad_inputs = (
        {"order_id": 0, "reason": "r"},
        {"order_id": 1},  # 缺 reason
        {"order_id": 1, "reason": ""},
        {"order_id": 1, "reason": "x" * 501},
    )

    for raw in bad_inputs:
        payload = await tool.execute(raw)
        assert payload["success"] is False, f"参数应被拒绝: {raw}"
        assert payload["error_code"] == "INVALID_PARAMS"
    assert await _refund_count(seeded_maker) == 0  # 全部无写入

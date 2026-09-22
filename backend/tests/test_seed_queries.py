"""Seed 数据基础查询测试：3 个基础查询断言与 docs/case-chain-sales-drop-data-design.md 一致。

1. 客户 A 月度销售：sales_records 聚合，P1 月度金额与文档 3.4 一致且近 3 个月逐月下降；
2. 客户 A 订单数：12 单（2 产品 × 6 个月），P1 各月数量与金额 = 数量 × 单价，与文档 3.3 一致；
3. 客户 A 投诉列表：3 条交付类工单与文档 3.5 一致（含 1 条未解决），对照组 B 仅 2 条日常工单。
"""
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app import seed as seed_module
from app.models.customer import Customer
from app.models.customer_ticket import CustomerTicket
from app.models.order import Order
from app.models.product import Product
from app.models.sales_record import SalesRecord


@pytest.fixture
async def seeded_session(db_session):
    """在空测试库上执行 seed（客户/产品/订单/销售/工单），供基础查询测试使用。"""
    await seed_module.seed(db_session)
    return db_session


async def _customer_id(session, code: str) -> int:
    """按 code 动态定位客户 id（不依赖自增序列状态）。"""
    return await session.scalar(select(Customer.id).where(Customer.code == code))


async def test_customer_a_monthly_sales(seeded_session):
    """基础查询 1：客户 A 月度销售。"""
    session = seeded_session
    a_id = await _customer_id(session, "CUST-0001")
    p1_id = await session.scalar(select(Product.id).where(Product.sku == "SKU-XS100"))

    stmt = (
        select(SalesRecord.sale_date, SalesRecord.amount)
        .where(SalesRecord.customer_id == a_id, SalesRecord.product_id == p1_id)
        .order_by(SalesRecord.sale_date)
    )
    rows = (await session.execute(stmt)).all()

    assert [d.strftime("%Y-%m") for d, _ in rows] == [f"2026-{m:02d}" for m in range(3, 9)]
    assert [int(amount) for _, amount in rows] == seed_module.EXPECTED_A_P1_MONTHLY
    # 近 3 个月（2026-06/07/08）逐月下降
    assert rows[3][1] > rows[4][1] > rows[5][1]


async def test_customer_a_order_count(seeded_session):
    """基础查询 2：客户 A 订单数。"""
    session = seeded_session
    a_id = await _customer_id(session, "CUST-0001")
    p1_id = await session.scalar(select(Product.id).where(Product.sku == "SKU-XS100"))

    # 文档 3.3：A 共 12 单（2 产品 × 6 个月，每客户每产品每月 1 单）
    assert await session.scalar(
        select(func.count()).select_from(Order).where(Order.customer_id == a_id)
    ) == 12

    # P1 各月数量与文档一致，金额 = 数量 × 单价 1200
    stmt = (
        select(Order.order_date, Order.quantity, Order.amount)
        .where(Order.customer_id == a_id, Order.product_id == p1_id)
        .order_by(Order.order_date)
    )
    rows = (await session.execute(stmt)).all()
    assert [qty for _, qty, _ in rows] == [500, 520, 480, 300, 180, 120]
    assert all(amount == qty * Decimal("1200.00") for _, qty, amount in rows)

    # 全库订单总量达标（其余客户随机生成）
    assert await session.scalar(select(func.count()).select_from(Order)) >= 50


async def test_customer_a_ticket_list(seeded_session):
    """基础查询 3：客户 A 投诉列表。"""
    session = seeded_session
    a_id = await _customer_id(session, "CUST-0001")
    b_id = await _customer_id(session, "CUST-0002")

    stmt = (
        select(CustomerTicket)
        .where(CustomerTicket.customer_id == a_id)
        .order_by(CustomerTicket.created_at)
    )
    a_tickets = (await session.execute(stmt)).scalars().all()

    # 文档 3.5：3 条交付类投诉，编号与优先级一致，最新 1 条（T-2608-002）未解决
    assert [t.ticket_no for t in a_tickets] == ["T-2606-001", "T-2607-003", "T-2608-002"]
    assert [t.priority for t in a_tickets] == ["high", "high", "urgent"]
    assert [t.resolved_at is None for t in a_tickets] == [False, False, True]

    # 投诉仅描述现象（缺货数量 / 延迟天数 / 业务影响），不含归因表述
    for t in a_tickets:
        text = t.title + t.content
        assert not any(kw in text for kw in ("因为", "由于", "原因", "库存管理不善"))

    # 对照组 B：仅 2 条日常工单，无交付类投诉
    b_tickets = (
        await session.execute(select(CustomerTicket).where(CustomerTicket.customer_id == b_id))
    ).scalars().all()
    assert sorted(t.ticket_no for t in b_tickets) == ["T-2604-001", "T-2607-001"]
    assert all(not any(kw in t.title + t.content for kw in ("缺货", "延迟")) for t in b_tickets)

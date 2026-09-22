"""SalesRecordRepository 查询测试：按客户 + 时间范围查询（命中复合索引）。"""
from datetime import datetime, timezone
from decimal import Decimal

from app.repositories.customer import CustomerRepository
from app.repositories.product import ProductRepository
from app.repositories.sales_record import SalesRecordRepository


async def test_list_by_customer_period(db_session):
    sales_repo = SalesRecordRepository(db_session)
    customer_repo = CustomerRepository(db_session)
    product_repo = ProductRepository(db_session)

    customer = await customer_repo.create(code="C001", name="客户A")
    other = await customer_repo.create(code="C002", name="客户B")
    product_a = await product_repo.create(sku="SKU001", name="产品A", price=Decimal("99.00"))
    product_b = await product_repo.create(sku="SKU002", name="产品B", price=Decimal("199.00"))

    # 客户A：8 月两条、7 月一条；客户B：8 月一条
    await sales_repo.create(
        customer_id=customer.id, product_id=product_a.id, quantity=10,
        amount=Decimal("1000.00"), sale_date=datetime(2026, 8, 5, tzinfo=timezone.utc),
    )
    await sales_repo.create(
        customer_id=customer.id, product_id=product_b.id, quantity=5,
        amount=Decimal("500.00"), sale_date=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    await sales_repo.create(
        customer_id=customer.id, product_id=product_a.id, quantity=8,
        amount=Decimal("800.00"), sale_date=datetime(2026, 7, 15, tzinfo=timezone.utc),
    )
    await sales_repo.create(
        customer_id=other.id, product_id=product_a.id, quantity=3,
        amount=Decimal("300.00"), sale_date=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )

    # 查询客户A 8 月（左闭右开）：只命中 2 条，按时间升序
    records = await sales_repo.list_by_customer_period(
        customer.id,
        start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        end=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    assert len(records) == 2
    assert all(r.customer_id == customer.id for r in records)
    assert records[0].sale_date < records[1].sale_date
    assert sum(r.amount for r in records) == Decimal("1500.00")

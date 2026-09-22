"""ProductRepository 单元测试：覆盖基础 CRUD。"""
from decimal import Decimal

from app.repositories.product import ProductRepository


async def test_product_repository_crud(db_session):
    repo = ProductRepository(db_session)

    # create
    product = await repo.create(sku="SKU001", name="产品A", price=Decimal("199.00"))
    assert product.id is not None
    assert product.price == Decimal("199.00")

    # get
    fetched = await repo.get(product.id)
    assert fetched is not None
    assert fetched.sku == "SKU001"

    # update
    await repo.update(product, price=Decimal("259.50"))
    fetched = await repo.get(product.id)
    assert fetched.price == Decimal("259.50")

    # list
    products = await repo.list()
    assert len(products) == 1

    # delete
    await repo.delete(product)
    assert await repo.get(product.id) is None

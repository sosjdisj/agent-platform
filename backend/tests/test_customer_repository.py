"""CustomerRepository 单元测试：覆盖基础 CRUD。"""
from app.repositories.customer import CustomerRepository


async def test_customer_repository_crud(db_session):
    repo = CustomerRepository(db_session)

    # create
    customer = await repo.create(code="C001", name="客户A", industry="制造业", region="华东")
    assert customer.id is not None
    assert customer.status == "active"

    # get
    fetched = await repo.get(customer.id)
    assert fetched is not None
    assert fetched.code == "C001"
    assert fetched.name == "客户A"

    # update
    await repo.update(customer, industry="金融业")
    fetched = await repo.get(customer.id)
    assert fetched.industry == "金融业"

    # list
    customers = await repo.list()
    assert len(customers) == 1

    # delete
    await repo.delete(customer)
    assert await repo.get(customer.id) is None

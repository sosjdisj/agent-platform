"""CustomerTicketRepository 单元测试：覆盖基础 CRUD。"""
from app.repositories.customer import CustomerRepository
from app.repositories.customer_ticket import CustomerTicketRepository


async def test_customer_ticket_repository_crud(db_session):
    customer_repo = CustomerRepository(db_session)
    ticket_repo = CustomerTicketRepository(db_session)

    customer = await customer_repo.create(code="C001", name="客户A")

    # create
    ticket = await ticket_repo.create(
        ticket_no="T001", customer_id=customer.id, title="发货延迟", content="订单超 7 天未发货"
    )
    assert ticket.id is not None
    assert ticket.priority == "medium"
    assert ticket.status == "active"

    # get
    fetched = await ticket_repo.get(ticket.id)
    assert fetched is not None
    assert fetched.ticket_no == "T001"
    assert fetched.customer_id == customer.id

    # update
    await ticket_repo.update(ticket, status="closed", priority="high")
    fetched = await ticket_repo.get(ticket.id)
    assert fetched.status == "closed"
    assert fetched.priority == "high"

    # list
    assert len(await ticket_repo.list()) == 1

    # delete
    await ticket_repo.delete(ticket)
    assert await ticket_repo.get(ticket.id) is None

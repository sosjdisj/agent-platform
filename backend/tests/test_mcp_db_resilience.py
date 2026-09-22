"""数据库工具韧性测试（Prompt 7.3）：慢查询超时 / 瞬时错误有限重试 / 只读保障。

- 超时：注入查询挂起的假会话模拟数据库慢查询，工具 timeout 先耗尽 → TOOL_TIMEOUT；
- 重试：注入抛瞬时 DB 异常的假会话，验证重试上限（TRANSIENT_ERROR）与恢复；
- 只读保障分两层：
  1. 连接层——readonly_session 事务为 READ ONLY，写操作被 PostgreSQL 拒绝；
  2. 工具侧——spy 全部 Repository 方法，断言四个工具仅调用只读白名单方法。
"""
import asyncio
import inspect
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.mcp.base import RetryPolicy, ToolError
from app.mcp.database.query_customers import QueryCustomersTool
from app.mcp.database.query_orders import QueryOrdersTool
from app.mcp.database.query_product_sales import QueryProductSalesTool
from app.mcp.database.query_sales_data import QuerySalesDataTool
from app.models.customer import Customer
from app.repositories.base import BaseRepository
from app.repositories.customer import CustomerRepository
from app.repositories.order import OrderRepository
from app.repositories.product import ProductRepository
from app.repositories.sales_record import SalesRecordRepository

NOT_EXIST_ID = 99999
READONLY_REPO_METHODS = {
    "get",
    "list",
    "list_by_ids",
    "search",
    "list_by_customer_period",
    "sum_by_product",
}
MUTATING_REPO_METHODS = {"create", "update", "delete"}


class _FakeSession:
    """脚本化假会话：execute 立即返回（SET 只读语句），get 行为由测试注入。"""

    def __init__(self, get_behavior) -> None:
        self._get_behavior = get_behavior
        self.get_calls = 0

    async def execute(self, *args, **kwargs):
        return None

    async def get(self, *args, **kwargs):
        self.get_calls += 1
        result = self._get_behavior()
        if isinstance(result, BaseException):
            raise result
        if asyncio.iscoroutine(result):
            return await result  # 供挂起场景：wait_for 超时在此取消
        return result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _fake_maker(session: _FakeSession):
    return lambda: session


def _transient() -> OperationalError:
    return OperationalError(
        "SELECT 1", {}, Exception("server closed the connection unexpectedly")
    )


# ---------- 慢查询超时 ----------


async def test_slow_query_returns_tool_timeout():
    """数据库查询挂起超过工具 timeout → 统一错误 TOOL_TIMEOUT，且超时不重试。"""
    session = _FakeSession(lambda: asyncio.sleep(0.5, result=None))
    tool = QueryCustomersTool(session_maker=_fake_maker(session))
    tool.timeout = 0.05  # 实例级收紧，避免慢测试

    payload = await tool.execute({"customer_id": 1})

    assert payload["success"] is False
    assert payload["error_code"] == "TOOL_TIMEOUT"
    assert "0.05" in payload["message"]
    assert session.get_calls == 1


# ---------- 瞬时错误有限重试 ----------


async def test_transient_error_retry_exhausted():
    """连接闪断持续发生：重试至上限（3 次），返回 TRANSIENT_ERROR 统一结构。"""
    session = _FakeSession(_transient)
    tool = QueryCustomersTool(session_maker=_fake_maker(session))
    tool.retry_policy = RetryPolicy(max_attempts=3, backoff_seconds=0)

    payload = await tool.execute({"customer_id": 1})

    assert payload["success"] is False
    assert payload["error_code"] == "TRANSIENT_ERROR"
    assert session.get_calls == 3
    assert "瞬时错误" in payload["message"]


async def test_transient_error_retry_recovers():
    """前两次闪断、第三次恢复：重试后返回成功结构。"""
    outcomes = iter([_transient(), _transient()])
    session = _FakeSession(
        lambda: next(outcomes, Customer(id=1, code="CUST-0001", name="华信智造", status="active"))
    )
    tool = QueryCustomersTool(session_maker=_fake_maker(session))
    tool.retry_policy = RetryPolicy(max_attempts=3, backoff_seconds=0)

    payload = await tool.execute({"customer_id": 1})

    assert payload["success"] is True
    assert payload["data"]["count"] == 1
    assert payload["data"]["customers"][0]["name"] == "华信智造"
    assert session.get_calls == 3


async def test_business_and_non_transient_errors_not_retried():
    """ToolError（业务）与非瞬时异常均不重试：分别透传业务码 / TOOL_INTERNAL。"""
    session = _FakeSession(lambda: ToolError("BIZ_X", "业务失败"))
    tool = QueryCustomersTool(session_maker=_fake_maker(session))
    tool.retry_policy = RetryPolicy(max_attempts=3, backoff_seconds=0)

    payload = await tool.execute({"customer_id": 1})
    assert payload["error_code"] == "BIZ_X"
    assert session.get_calls == 1

    session = _FakeSession(lambda: ValueError("参数构造错误"))
    tool = QueryCustomersTool(session_maker=_fake_maker(session))
    tool.retry_policy = RetryPolicy(max_attempts=3, backoff_seconds=0)

    payload = await tool.execute({"customer_id": 1})
    assert payload["error_code"] == "TOOL_INTERNAL"
    assert session.get_calls == 1


# ---------- 只读保障：连接层 ----------


async def test_readonly_session_blocks_writes(seeded_maker):
    """连接层保障：只读事务中读正常、写操作被 PostgreSQL 拒绝（read-only transaction）。"""
    tool = QueryCustomersTool(session_maker=seeded_maker)

    async with tool.readonly_session() as session:
        assert await session.scalar(select(func.count()).select_from(Customer)) == 22
        repo = CustomerRepository(session)
        with pytest.raises(Exception, match="read-only|只读"):
            await repo.create(code="C-RO", name="不应写入")


# ---------- 只读保障：工具侧（仅调用只读 Repository 方法） ----------


def _spy_repository_methods(monkeypatch, called: set[str]) -> None:
    """给 BaseRepository 及各子类自有公共方法包 spy，记录调用名（不改行为）。"""
    classes = [
        BaseRepository,
        CustomerRepository,
        OrderRepository,
        ProductRepository,
        SalesRecordRepository,
    ]
    for cls in classes:
        for name, attr in list(vars(cls).items()):
            # 只包裹普通（含 async）函数；类属性如 model 也是 callable 但需排除
            if name.startswith("_") or not inspect.isfunction(attr):
                continue

            def _make_wrapper(fn, method_name):
                async def wrapper(self, *args, **kwargs):
                    called.add(method_name)
                    return await fn(self, *args, **kwargs)

                return wrapper

            monkeypatch.setattr(cls, name, _make_wrapper(attr, name))


async def test_tools_only_call_readonly_repo_methods(seeded_maker, case_ids, monkeypatch):
    """工具侧断言：四个数据库工具的全部 Repository 调用都在只读白名单内。"""
    called: set[str] = set()
    _spy_repository_methods(monkeypatch, called)

    a_id, _, p1_id, _ = case_ids
    scenarios = [
        (QueryCustomersTool(session_maker=seeded_maker), {"customer_id": a_id}),
        (QueryCustomersTool(session_maker=seeded_maker), {"keyword": "华信"}),
        (QueryOrdersTool(session_maker=seeded_maker), {"customer_id": a_id}),
        (QuerySalesDataTool(session_maker=seeded_maker), {"customer_id": a_id}),
        (
            QuerySalesDataTool(session_maker=seeded_maker),
            {
                "customer_id": a_id,
                "product_id": p1_id,
                "start_date": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "end_date": datetime(2026, 7, 1, tzinfo=timezone.utc),
            },
        ),
        (QueryProductSalesTool(session_maker=seeded_maker), {"customer_id": a_id}),
        (QueryProductSalesTool(session_maker=seeded_maker), {}),
    ]
    for tool, raw in scenarios:
        payload = await tool.execute(raw)
        assert payload["success"] is True, f"{tool.name} {raw} → {payload}"

    assert called <= READONLY_REPO_METHODS, (
        f"出现非只读方法调用: {called - READONLY_REPO_METHODS}"
    )
    assert not called & MUTATING_REPO_METHODS

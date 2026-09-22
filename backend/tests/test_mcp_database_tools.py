"""database 子模块只读工具测试（Prompt 7.1）：query_customers + query_orders。

每个工具覆盖三类验收场景：
1. 正常查询（精确查 / 关键词分页 / 时间范围过滤 / 分页）；
2. customer_id 不存在 → 统一结构化错误 CUSTOMER_NOT_FOUND；
3. 参数非法 → Pydantic 校验拒绝 INVALID_PARAMS。

数据：复用 app.seed（内部 commit），工具通过测试专用 session_maker 开短会话读取。
"""
from datetime import datetime, timezone

import pytest
from fastmcp import FastMCP
from sqlalchemy import select

from app.mcp.client import MCPClient
from app.mcp.database import register_tools
from app.mcp.database.query_customers import QueryCustomersTool
from app.mcp.database.query_orders import QueryOrdersTool
from app.mcp.registry import ToolRegistry
from app.models.customer import Customer

NOT_EXIST_ID = 99999


async def _case_customer_id(session, code: str) -> int:
    return await session.scalar(select(Customer.id).where(Customer.code == code))


def _build_mcp_client(maker) -> MCPClient:
    """用测试会话工厂构建独立 registry + FastMCP Server（与生产同一注册链路）。"""
    registry = ToolRegistry()
    registry.register(
        QueryCustomersTool(session_maker=maker),
        QueryOrdersTool(session_maker=maker),
    )
    mcp = FastMCP("test-database-tools")
    registry.mount_to(mcp)
    return MCPClient(server=mcp, registry=registry)


# ---------- query_customers ----------


async def test_query_customers_by_id(seeded_maker):
    """正常查询：customer_id 精确命中案例客户 A。"""
    async with seeded_maker() as session:
        a_id = await _case_customer_id(session, "CUST-0001")

    payload = await QueryCustomersTool(session_maker=seeded_maker).execute(
        {"customer_id": a_id}
    )

    assert payload["success"] is True
    assert payload["data"]["count"] == 1
    customer = payload["data"]["customers"][0]
    assert customer["id"] == a_id
    assert customer["code"] == "CUST-0001"
    assert customer["name"] == "华信智造"
    assert customer["industry"] == "制造业"
    assert customer["status"] == "active"


async def test_query_customers_keyword_search(seeded_maker):
    """正常查询：关键词同时匹配名称与编码，分页参数生效。"""
    tool = QueryCustomersTool(session_maker=seeded_maker)

    payload = await tool.execute({"keyword": "华信"})
    assert payload["success"] is True
    assert payload["data"]["count"] == 1
    assert payload["data"]["customers"][0]["name"] == "华信智造"

    # 关键词命中编码：精确到单个案例客户
    payload = await tool.execute({"keyword": "CUST-0002"})
    assert payload["data"]["count"] == 1
    assert payload["data"]["customers"][0]["name"] == "苏南精工"

    # 分页：seed 共 22 家客户，limit=10 offset=20 只剩最后 2 条
    payload = await tool.execute({"limit": 10, "offset": 20})
    assert payload["data"]["count"] == 2


async def test_query_customers_not_found(seeded_maker):
    """customer_id 不存在 → 统一结构化错误 CUSTOMER_NOT_FOUND。"""
    payload = await QueryCustomersTool(session_maker=seeded_maker).execute(
        {"customer_id": NOT_EXIST_ID}
    )

    assert payload["success"] is False
    assert payload["error_code"] == "CUSTOMER_NOT_FOUND"
    assert str(NOT_EXIST_ID) in payload["message"]
    assert payload["data"] is None


async def test_query_customers_invalid_params(seeded_maker):
    """参数非法 → Pydantic 校验拒绝（INVALID_PARAMS）。"""
    tool = QueryCustomersTool(session_maker=seeded_maker)

    # customer_id 与 keyword 互斥（模型级校验）
    payload = await tool.execute({"customer_id": 1, "keyword": "华信"})
    assert payload["success"] is False
    assert payload["error_code"] == "INVALID_PARAMS"

    # 越界分页 / 非法 ID / 空关键词
    for raw in (
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"customer_id": 0},
        {"keyword": ""},
    ):
        payload = await tool.execute(raw)
        assert payload["success"] is False, f"参数应被拒绝: {raw}"
        assert payload["error_code"] == "INVALID_PARAMS"


# ---------- query_orders ----------


async def test_query_orders_by_customer(seeded_maker):
    """正常查询：客户 A 全量 12 单；时间范围过滤与字段数值正确。"""
    async with seeded_maker() as session:
        a_id = await _case_customer_id(session, "CUST-0001")

    tool = QueryOrdersTool(session_maker=seeded_maker)
    payload = await tool.execute({"customer_id": a_id})

    assert payload["success"] is True
    assert payload["data"]["count"] == 12
    assert all(o["customer_id"] == a_id for o in payload["data"]["orders"])

    # 2026-06 仅 2 单，按日期升序：P1（10 日，300×1200=360000）、P2（15 日，40×150=6000）
    june = await tool.execute(
        {
            "customer_id": a_id,
            "start_date": datetime(2026, 6, 1, tzinfo=timezone.utc),
            "end_date": datetime(2026, 7, 1, tzinfo=timezone.utc),
        }
    )
    rows = june["data"]["orders"]
    assert [o["quantity"] for o in rows] == [300, 40]
    assert [o["amount"] for o in rows] == [360000.0, 6000.0]


async def test_query_orders_pagination(seeded_maker):
    """分页：limit + offset 切片不重不漏。"""
    async with seeded_maker() as session:
        a_id = await _case_customer_id(session, "CUST-0001")

    tool = QueryOrdersTool(session_maker=seeded_maker)
    page1 = (await tool.execute({"customer_id": a_id, "limit": 5}))["data"]["orders"]
    page2 = (await tool.execute({"customer_id": a_id, "limit": 5, "offset": 5}))["data"]["orders"]
    rest = (await tool.execute({"customer_id": a_id, "offset": 10}))["data"]["orders"]

    ids = [o["id"] for o in page1 + page2 + rest]
    assert len(ids) == 12
    assert len(set(ids)) == 12


async def test_query_orders_customer_not_found(seeded_maker):
    """customer_id 不存在 → 统一结构化错误 CUSTOMER_NOT_FOUND（先校验客户再查单）。"""
    payload = await QueryOrdersTool(session_maker=seeded_maker).execute(
        {"customer_id": NOT_EXIST_ID}
    )

    assert payload["success"] is False
    assert payload["error_code"] == "CUSTOMER_NOT_FOUND"
    assert str(NOT_EXIST_ID) in payload["message"]


async def test_query_orders_invalid_params(seeded_maker):
    """参数非法 → Pydantic 校验拒绝（INVALID_PARAMS）。"""
    tool = QueryOrdersTool(session_maker=seeded_maker)

    for raw in (
        {},  # customer_id 必填
        {"customer_id": 0},  # 低于下界
        {  # start_date >= end_date（模型级校验）
            "customer_id": 1,
            "start_date": "2026-07-01T00:00:00Z",
            "end_date": "2026-06-01T00:00:00Z",
        },
        {"customer_id": 1, "limit": 201},  # 超上界
    ):
        payload = await tool.execute(raw)
        assert payload["success"] is False, f"参数应被拒绝: {raw}"
        assert payload["error_code"] == "INVALID_PARAMS"


# ---------- MCP 端到端（经 FastMCP 协议层） ----------


async def test_database_tools_via_mcp_client(seeded_maker):
    """端到端：MCPClient → FastMCP → 工具，验证 datetime/Decimal 经协议层序列化。"""
    async with seeded_maker() as session:
        a_id = await _case_customer_id(session, "CUST-0001")

    client = _build_mcp_client(seeded_maker)

    result = await client.call(
        "query_orders",
        {
            "customer_id": a_id,
            "start_date": "2026-06-01T00:00:00Z",
            "end_date": "2026-07-01T00:00:00Z",
        },
    )
    assert result.success is True
    orders = result.data["orders"]
    assert orders[0]["order_date"].startswith("2026-06-10")  # ISO 字符串
    assert orders[0]["amount"] == 360000.0

    result = await client.call("query_customers", {"keyword": "华信"})
    assert result.success is True
    assert result.data["count"] == 1
    assert result.data["customers"][0]["name"] == "华信智造"


async def test_register_tools_via_global_chain():
    """生产注册链中 database 子模块登记全部只读工具，且元数据 schema 齐全。"""
    registry = ToolRegistry()
    register_tools(registry)
    for name in (
        "query_customers",
        "query_orders",
        "query_sales_data",
        "query_product_sales",
    ):
        meta = registry.get(name).metadata
        assert meta.risk_level.value == "safe"
        assert "properties" in meta.input_schema
        assert "properties" in meta.output_schema

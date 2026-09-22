"""sales 查询工具测试（Prompt 7.2）：query_sales_data + query_product_sales。

验收：用 Prompt 4 Seed 数据断言客户 A 月度聚合值与数据库（文档 3.4 预期常量）一致。
- 客户 A：P1（XS-100）月度金额 [600000, 624000, 576000, 360000, 216000, 144000]（2026-03..08），
  数量 [500, 520, 480, 300, 180, 120]；P2（DC-20）每月 40×150=6000。
- sales_records 为月度汇总，sale_date 为该月最后一天。
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app import seed as seed_module
from app.mcp.database.query_product_sales import QueryProductSalesTool
from app.mcp.database.query_sales_data import QuerySalesDataTool
from app.models.product import Product
from app.models.sales_record import SalesRecord

EXPECTED_A_P1 = seed_module.EXPECTED_A_P1_MONTHLY
MONTHS = [f"2026-{m:02d}" for m in range(3, 9)]
A_P2_MONTHLY = 6000  # P2 40×150，每月 1 单
NOT_EXIST_ID = 99999


# ---------- query_sales_data ----------


async def test_query_sales_data_monthly_matches_seed(seeded_maker, case_ids):
    """验收核心：客户 A 月度聚合值与 Seed 数据（文档 3.4）一致。"""
    a_id, _, p1_id, p2_id = case_ids
    payload = await QuerySalesDataTool(session_maker=seeded_maker).execute(
        {"customer_id": a_id}
    )

    assert payload["success"] is True
    assert payload["data"]["count"] == 12  # 6 个月 × 2 产品
    rows = payload["data"]["rows"]

    # P1 月度金额 = 文档 3.4 预期，逐月下降趋势可见
    p1_rows = [r for r in rows if r["product_id"] == p1_id]
    assert [r["sale_month"] for r in p1_rows] == MONTHS
    assert [r["amount"] for r in p1_rows] == EXPECTED_A_P1
    assert [r["quantity"] for r in p1_rows] == [500, 520, 480, 300, 180, 120]
    assert p1_rows[0]["sku"] == "SKU-XS100"
    assert p1_rows[0]["product_name"] == "工业传感器 XS-100"

    # P2 每月 6000
    p2_rows = [r for r in rows if r["product_id"] == p2_id]
    assert [r["amount"] for r in p2_rows] == [A_P2_MONTHLY] * 6

    # 与数据库直接聚合一致：工具逐月合计 == 原始表逐月合计
    async with seeded_maker() as session:
        month_expr = func.to_char(SalesRecord.sale_date, "YYYY-MM")
        db_monthly = (
            await session.execute(
                select(month_expr, func.sum(SalesRecord.amount))
                .where(SalesRecord.customer_id == a_id)
                .group_by(month_expr)
                .order_by(month_expr)
            )
        ).all()
    tool_monthly = {}
    for r in rows:
        tool_monthly[r["sale_month"]] = tool_monthly.get(r["sale_month"], 0) + r["amount"]
    assert [m for m, _ in db_monthly] == MONTHS
    assert [tool_monthly[m] for m, _ in db_monthly] == [float(total) for _, total in db_monthly]


async def test_query_sales_data_period_filter(seeded_maker, case_ids):
    """时间范围过滤（左闭右开）：仅命中 6/7 两月。"""
    a_id, _, p1_id, _ = case_ids
    payload = await QuerySalesDataTool(session_maker=seeded_maker).execute(
        {
            "customer_id": a_id,
            "start_date": datetime(2026, 6, 1, tzinfo=timezone.utc),
            "end_date": datetime(2026, 8, 1, tzinfo=timezone.utc),
        }
    )

    rows = payload["data"]["rows"]
    assert payload["data"]["count"] == 4  # 2 月 × 2 产品
    p1 = [r for r in rows if r["product_id"] == p1_id]
    assert [r["sale_month"] for r in p1] == ["2026-06", "2026-07"]
    assert [r["amount"] for r in p1] == [360000.0, 216000.0]


async def test_query_sales_data_product_filter_and_empty(seeded_maker, case_ids):
    """产品过滤：P1-only 返回 6 行；A 未采购的产品返回空结果。"""
    a_id, _, p1_id, p2_id = case_ids
    tool = QuerySalesDataTool(session_maker=seeded_maker)

    payload = await tool.execute({"customer_id": a_id, "product_id": p1_id})
    assert payload["data"]["count"] == 6
    assert all(r["product_id"] == p1_id for r in payload["data"]["rows"])

    # A 只采购案例产品 P1/P2：其余产品应返回空（success 且 0 行）
    async with seeded_maker() as session:
        other_id = await session.scalar(
            select(Product.id).where(Product.id.notin_([p1_id, p2_id])).limit(1)
        )
    payload = await tool.execute({"customer_id": a_id, "product_id": other_id})
    assert payload["success"] is True
    assert payload["data"]["count"] == 0
    assert payload["data"]["rows"] == []


async def test_query_sales_data_not_found_and_invalid(seeded_maker, case_ids):
    """customer_id / product_id 不存在返回结构化错误；参数非法被校验拒绝。"""
    a_id, _, _, _ = case_ids
    tool = QuerySalesDataTool(session_maker=seeded_maker)

    payload = await tool.execute({"customer_id": NOT_EXIST_ID})
    assert payload["success"] is False
    assert payload["error_code"] == "CUSTOMER_NOT_FOUND"

    payload = await tool.execute({"customer_id": a_id, "product_id": NOT_EXIST_ID})
    assert payload["error_code"] == "PRODUCT_NOT_FOUND"

    for raw in (
        {"customer_id": 0},
        {"customer_id": a_id, "start_date": "2026-07-01T00:00:00Z",
         "end_date": "2026-06-01T00:00:00Z"},
    ):
        payload = await tool.execute(raw)
        assert payload["success"] is False, f"参数应被拒绝: {raw}"
        assert payload["error_code"] == "INVALID_PARAMS"


# ---------- query_product_sales ----------


async def test_query_product_sales_customer_totals(seeded_maker, case_ids):
    """客户 A 各产品期间总量：P1 在前（按总金额降序），数值与 Seed 一致。"""
    a_id, _, p1_id, p2_id = case_ids
    payload = await QueryProductSalesTool(session_maker=seeded_maker).execute(
        {"customer_id": a_id}
    )

    assert payload["success"] is True
    rows = payload["data"]["rows"]
    assert payload["data"]["count"] == 2
    assert [r["product_id"] for r in rows] == [p1_id, p2_id]  # P1 总额远高于 P2

    p1, p2 = rows
    assert p1["sku"] == "SKU-XS100"
    assert p1["total_amount"] == float(sum(EXPECTED_A_P1))  # 2,520,000
    assert p1["total_quantity"] == 2100
    assert p1["month_count"] == 6
    assert p2["total_amount"] == float(A_P2_MONTHLY * 6)  # 36,000
    assert p2["total_quantity"] == 240


async def test_query_product_sales_period_and_product_filter(seeded_maker, case_ids):
    """时间范围 + 产品过滤：A 的 P1 在 6-8 月合计 720000（数量 600，3 个月）。"""
    a_id, _, p1_id, _ = case_ids
    payload = await QueryProductSalesTool(session_maker=seeded_maker).execute(
        {
            "customer_id": a_id,
            "product_id": p1_id,
            "start_date": "2026-06-01T00:00:00Z",
            "end_date": "2026-09-01T00:00:00Z",
        }
    )

    rows = payload["data"]["rows"]
    assert payload["data"]["count"] == 1
    assert rows[0]["total_amount"] == 720000.0  # 360000 + 216000 + 144000
    assert rows[0]["total_quantity"] == 600  # 300 + 180 + 120
    assert rows[0]["month_count"] == 3


async def test_query_product_sales_global_matches_db(seeded_maker, case_ids):
    """全客户聚合：P1 仅案例客户 A / B 采购，工具聚合值与数据库直查一致。"""
    _, _, p1_id, _ = case_ids
    payload = await QueryProductSalesTool(session_maker=seeded_maker).execute(
        {"product_id": p1_id}
    )

    rows = payload["data"]["rows"]
    assert payload["data"]["count"] == 1

    async with seeded_maker() as session:
        db_total = await session.scalar(
            select(func.sum(SalesRecord.amount)).where(SalesRecord.product_id == p1_id)
        )
    assert rows[0]["total_amount"] == float(db_total)
    # A 的文档值只是 P1 全局总额的一部分（B 为对照组）
    assert rows[0]["total_amount"] > float(sum(EXPECTED_A_P1))


async def test_query_product_sales_not_found_and_invalid(seeded_maker, case_ids):
    """customer_id / product_id 不存在返回结构化错误；start >= end 被校验拒绝。"""
    a_id, _, _, _ = case_ids
    tool = QueryProductSalesTool(session_maker=seeded_maker)

    payload = await tool.execute({"customer_id": NOT_EXIST_ID})
    assert payload["error_code"] == "CUSTOMER_NOT_FOUND"

    payload = await tool.execute({"customer_id": a_id, "product_id": NOT_EXIST_ID})
    assert payload["error_code"] == "PRODUCT_NOT_FOUND"

    payload = await tool.execute(
        {
            "customer_id": a_id,
            "start_date": "2026-07-01T00:00:00Z",
            "end_date": "2026-07-01T00:00:00Z",
        }
    )
    assert payload["success"] is False
    assert payload["error_code"] == "INVALID_PARAMS"

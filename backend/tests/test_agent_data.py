"""DataAgent 测试：脚本化 LLM + 真实只读数据工具（测试库 seed 数据）。

覆盖（13.1）：验收场景（『客户 A 最近销售额』只调所需工具）/ 多工具接力（客户名称 → ID）/
无需数据直接回答 / 白名单工具面与校验 / 来源组装 / 调试接口。
覆盖（13.2）：多轮组合对比 / 无数据 partial / 工具失败重试一次（成功与耗尽）。
"""

import json
from typing import ClassVar

import pytest
from pydantic import BaseModel

from app.agents import data as data_module
from app.agents.data import (
    DATA_AGENT_NAME,
    DATA_SYSTEM_PROMPT,
    NO_DATA,
    READ_ONLY_DATA_TOOLS,
    DataAgent,
    DataAgentError,
    assemble_data_result,
    validate_readonly_tools,
)
from app.agents.state import AgentResult, AgentState, AgentStatus, ToolCallRecord
from app.mcp.base import BaseTool, RiskLevel, ToolError, ToolResult
from app.mcp.database.query_customers import QueryCustomersTool
from app.mcp.database.query_orders import QueryOrdersTool
from app.mcp.database.query_product_sales import QueryProductSalesTool
from tests.test_agent_graph import ScriptedLLM, final_response, tool_call_response


def agent(llm: ScriptedLLM, session_maker) -> DataAgent:
    """构造注入脚本 LLM 与测试库会话工厂的 DataAgent（图仅依赖 chat 接口）。"""
    return DataAgent(llm, session_maker=session_maker)


def llm_tool_names(llm: ScriptedLLM) -> list[str]:
    """首轮 LLM 请求携带的工具面名称（核对白名单确实暴露给模型）。"""
    return [tool["function"]["name"] for tool in llm.calls[0]["tools"]]


async def test_sales_question_calls_only_needed_tools(seeded_maker, case_ids) -> None:
    """验收：『客户 A 最近销售额』只调用所需工具（query_sales_data），输出合法 AgentResult。"""
    (a_id, _b_id, _p1_id, _p2_id) = case_ids
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "query_sales_data", json.dumps({"customer_id": a_id})),
            final_response("客户 A 最近 3 个月销售额逐月下降，合计 ¥1,296,000，6 月最高。"),
        ]
    )

    result = await agent(llm, seeded_maker).answer(
        task_id=1, user_id=100, query="客户 A 最近销售额怎么样？"
    )

    assert result.agent == DATA_AGENT_NAME
    assert result.status is AgentStatus.COMPLETED
    assert result.error_code is None
    assert result.content == "客户 A 最近 3 个月销售额逐月下降，合计 ¥1,296,000，6 月最高。"
    # 成功工具调用足迹 = 来源：只调用了所需工具，未触发其余工具
    assert [s.ref_id for s in result.sources] == [f"query_sales_data(customer_id={a_id})"]
    assert all(s.source_type == "tool" for s in result.sources)
    # 工具面 = 4 个只读白名单工具，系统提示指导选工具
    assert llm_tool_names(llm) == list(READ_ONLY_DATA_TOOLS)
    assert DATA_SYSTEM_PROMPT in llm.calls[0]["messages"][0]["content"]


async def test_multi_tool_flow_via_customer_lookup(seeded_maker, case_ids) -> None:
    """多工具接力：客户名称 → query_customers（keyword 按编码）解析 ID → query_sales_data 查销售。"""
    (a_id, _b_id, _p1_id, _p2_id) = case_ids
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "query_customers", json.dumps({"keyword": "CUST-0001"})),
            tool_call_response("call_2", "query_sales_data", json.dumps({"customer_id": a_id})),
            final_response("客户 A（华信智造）最近 3 个月销售额逐月下降。"),
        ]
    )

    result = await agent(llm, seeded_maker).answer(
        task_id=1, user_id=100, query="客户 A 最近销售额怎么样？"
    )

    assert result.status is AgentStatus.COMPLETED
    assert result.content == "客户 A（华信智造）最近 3 个月销售额逐月下降。"
    assert [s.ref_id for s in result.sources] == [
        "query_customers(keyword=CUST-0001)",
        f"query_sales_data(customer_id={a_id})",
    ]


async def test_direct_answer_without_tools() -> None:
    """判断无需数据：直接回答，不调任何工具，来源为空（DataAgent 默认 SessionLocal 不触库）。"""
    llm = ScriptedLLM([final_response("你好，请告诉我想查询哪位客户的数据。")])

    result = await DataAgent(llm).answer(task_id=1, user_id=100, query="在吗")

    assert result.status is AgentStatus.COMPLETED
    assert result.content == "你好，请告诉我想查询哪位客户的数据。"
    assert result.sources == []
    assert llm.calls[0]["tools"]  # 无需数据也不改变只读工具面
    assert llm_tool_names(llm) == list(READ_ONLY_DATA_TOOLS)


def test_tool_surface_is_whitelist() -> None:
    """工具面 = 4 个只读白名单工具（注册顺序与白名单一致），构造时即完成白名单校验。"""
    assert DataAgent(ScriptedLLM([])).tool_names == READ_ONLY_DATA_TOOLS


def test_validate_rejects_tool_outside_whitelist() -> None:
    """白名单校验：非白名单工具（写入类）一律拒绝，不得混入 DataAgent 工具面。"""

    class FakeInput(BaseModel):
        x: int = 1

    class FakeOutput(BaseModel):
        y: int = 1

    class WriteOrdersTool(BaseTool):
        name = "update_orders"
        description = "不在白名单的写入工具"
        risk_level = RiskLevel.HIGH
        InputModel = FakeInput
        OutputModel = FakeOutput

        async def run(self, params: FakeInput) -> FakeOutput:
            return FakeOutput()

    with pytest.raises(ValueError, match="白名单"):
        validate_readonly_tools([WriteOrdersTool()])


def test_validate_rejects_unsafe_risk_level() -> None:
    """白名单校验：名称命中但风险等级非 SAFE 的工具同样拒绝（双条件缺一不可）。"""

    class MediumRiskOrders(QueryOrdersTool):
        risk_level = RiskLevel.MEDIUM

    with pytest.raises(ValueError, match="白名单"):
        validate_readonly_tools([MediumRiskOrders()])


async def test_customer_not_found_raises_data_agent_error(seeded_maker) -> None:
    """客户不存在：工具业务错误（CUSTOMER_NOT_FOUND）→ 状态 failed → answer 抛 DataAgentError。"""
    llm = ScriptedLLM(
        [tool_call_response("call_1", "query_sales_data", json.dumps({"customer_id": 999999}))]
    )

    with pytest.raises(DataAgentError) as exc_info:
        await agent(llm, seeded_maker).answer(task_id=1, user_id=100, query="客户 X 销售额")

    assert "CUSTOMER_NOT_FOUND" in str(exc_info.value)


async def test_comparison_combines_multiple_rounds(seeded_maker, case_ids) -> None:
    """多轮组合对比：分别查询客户 A 与 B 的销售后对比，同工具不同参数各自成源。"""
    (a_id, b_id, _p1_id, _p2_id) = case_ids
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "query_sales_data", json.dumps({"customer_id": a_id})),
            tool_call_response("call_2", "query_sales_data", json.dumps({"customer_id": b_id})),
            final_response("客户 A 最近销售额逐月下降，客户 B 平稳；A 已低于 B。"),
        ]
    )

    result = await agent(llm, seeded_maker).answer(
        task_id=1, user_id=100, query="对比客户 A 和 B 最近销售额"
    )

    assert result.status is AgentStatus.COMPLETED
    assert result.error_code is None
    assert result.content == "客户 A 最近销售额逐月下降，客户 B 平稳；A 已低于 B。"
    assert [s.ref_id for s in result.sources] == [
        f"query_sales_data(customer_id={a_id})",
        f"query_sales_data(customer_id={b_id})",
    ]


async def test_empty_data_returns_partial_with_no_data(seeded_maker, case_ids) -> None:
    """无数据兜底：查询区间无任何记录（count=0）→ status=partial + NO_DATA，正文如实说明。"""
    (a_id, _b_id, _p1_id, _p2_id) = case_ids
    llm = ScriptedLLM(
        [
            tool_call_response(
                "call_1",
                "query_sales_data",
                json.dumps(
                    {
                        "customer_id": a_id,
                        "start_date": "2030-01-01T00:00:00",
                        "end_date": "2030-02-01T00:00:00",
                    }
                ),
            ),
            final_response("客户 A 在 2030 年 1 月没有任何销售记录。"),
        ]
    )

    result = await agent(llm, seeded_maker).answer(
        task_id=1, user_id=100, query="客户 A 2030 年 1 月销售额"
    )

    assert result.status is AgentStatus.PARTIAL
    assert result.error_code == NO_DATA
    assert result.content == "客户 A 在 2030 年 1 月没有任何销售记录。"
    assert result.sources[0].ref_id.startswith(f"query_sales_data(customer_id={a_id},")


async def test_customer_found_but_sales_empty_still_completed(seeded_maker, case_ids) -> None:
    """部分命中不算无数据：客户查到（count=1）+ 期间无销售（count=0）→ completed。"""
    (a_id, _b_id, _p1_id, _p2_id) = case_ids
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "query_customers", json.dumps({"keyword": "CUST-0001"})),
            tool_call_response(
                "call_2",
                "query_sales_data",
                json.dumps(
                    {
                        "customer_id": a_id,
                        "start_date": "2030-01-01T00:00:00",
                        "end_date": "2030-02-01T00:00:00",
                    }
                ),
            ),
            final_response("客户 A 存在，但 2030 年 1 月无销售记录。"),
        ]
    )

    result = await agent(llm, seeded_maker).answer(
        task_id=1, user_id=100, query="客户 A 2030 年 1 月销售额"
    )

    assert result.status is AgentStatus.COMPLETED
    assert result.error_code is None
    assert len(result.sources) == 2


def _patch_flaky_sales(monkeypatch, tool_cls) -> None:
    """把 DataAgent 工具面中的 query_sales_data 替换为故障替身（白名单校验不受影响）。"""
    monkeypatch.setattr(
        data_module,
        "_READONLY_TOOL_CLASSES",
        (QueryCustomersTool, QueryOrdersTool, tool_cls, QueryProductSalesTool),
    )


async def test_tool_failure_retried_once_then_succeeds(
    seeded_maker, case_ids, monkeypatch
) -> None:
    """工具错误重试一次：首查失败 → 原参重查成功 → 任务正常完成（恰好调用 2 次）。"""
    (a_id, _b_id, _p1_id, _p2_id) = case_ids

    class FlakySalesTool(data_module.QuerySalesDataTool):
        """首查抛错、重查放行到真实查询的替身（模拟瞬时故障后恢复）。"""

        calls: ClassVar[list] = []

        async def run(self, params):
            type(self).calls.append(params.model_dump())
            if len(type(self).calls) == 1:
                raise ToolError("TRANSIENT_GLITCH", "模拟瞬时故障")
            return await super().run(params)

    _patch_flaky_sales(monkeypatch, FlakySalesTool)
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "query_sales_data", json.dumps({"customer_id": a_id})),
            final_response("客户 A 最近销售额查询成功。"),
        ]
    )

    result = await agent(llm, seeded_maker).answer(
        task_id=1, user_id=100, query="客户 A 最近销售额"
    )

    assert result.status is AgentStatus.COMPLETED
    assert result.error_code is None
    assert len(FlakySalesTool.calls) == 2  # 首查 + 重试，恰好一次
    assert FlakySalesTool.calls[0] == FlakySalesTool.calls[1]  # 原参重试


async def test_tool_failure_after_retry_raises_data_agent_error(
    seeded_maker, monkeypatch
) -> None:
    """重试耗尽仍失败：恰好重试 1 次（共 2 次调用）→ 状态 failed → 抛 DataAgentError。"""

    class AlwaysFailSalesTool(data_module.QuerySalesDataTool):
        """恒定失败的替身：模拟重试也无法恢复的工具错误。"""

        calls: ClassVar[list] = []

        async def run(self, params):
            type(self).calls.append(params.model_dump())
            raise ToolError("CUSTOMER_NOT_FOUND", f"客户不存在: id={params.customer_id}")

    _patch_flaky_sales(monkeypatch, AlwaysFailSalesTool)
    llm = ScriptedLLM(
        [tool_call_response("call_1", "query_sales_data", json.dumps({"customer_id": 999999}))]
    )

    with pytest.raises(DataAgentError) as exc_info:
        await agent(llm, seeded_maker).answer(task_id=1, user_id=100, query="客户 X 销售额")

    assert "CUSTOMER_NOT_FOUND" in str(exc_info.value)
    assert len(AlwaysFailSalesTool.calls) == 2  # 只重试一次，不无限重试


def _record(
    tool: str, arguments: dict | None = None, *, success: bool = True, count: int = 1
) -> ToolCallRecord:
    """一次工具调用记录（success=False 模拟失败调用；count 模拟输出数据量）。"""
    result = ToolResult.ok({"count": count}) if success else ToolResult.fail("X", "失败")
    return ToolCallRecord(tool=tool, arguments=arguments or {}, result=result)


def test_assemble_dedupes_sources_and_skips_failures() -> None:
    """来源组装：按 ref_id 去重保留首次引用，失败调用不进入来源。"""
    state = AgentState(
        task_id=1,
        user_id=100,
        messages=[],
        status=AgentStatus.COMPLETED,
        tool_results=[
            _record("query_orders"),
            _record("query_customers", success=False),
            _record("query_orders"),
            _record("query_sales_data"),
        ],
        final_result=AgentResult(agent="assistant", content="答案", sources=[]),
    )

    result = assemble_data_result(state)

    assert [s.ref_id for s in result.sources] == ["query_orders", "query_sales_data"]
    assert result.agent == DATA_AGENT_NAME
    assert result.content == "答案"


def test_assemble_keeps_same_tool_with_different_args() -> None:
    """来源组装：同工具不同参数的调用（对比场景）各自成源，不互相去重。"""
    state = AgentState(
        task_id=1,
        user_id=100,
        messages=[],
        status=AgentStatus.COMPLETED,
        tool_results=[
            _record("query_sales_data", {"customer_id": 1}),
            _record("query_sales_data", {"customer_id": 2}),
            _record("query_sales_data", {"customer_id": 1}),  # 同参重复：去重
        ],
        final_result=AgentResult(agent="assistant", content="答案", sources=[]),
    )

    result = assemble_data_result(state)

    assert [s.ref_id for s in result.sources] == [
        "query_sales_data(customer_id=1)",
        "query_sales_data(customer_id=2)",
    ]


def test_assemble_maps_all_empty_counts_to_partial() -> None:
    """结果语义：全部成功查询 count=0 → partial + NO_DATA；任一命中或未调工具 → completed。"""
    final = AgentResult(agent="assistant", content="答案", sources=[])

    empty = assemble_data_result(
        AgentState(
            task_id=1,
            user_id=100,
            messages=[],
            status=AgentStatus.COMPLETED,
            tool_results=[_record("query_sales_data", count=0)],
            final_result=final,
        )
    )
    assert empty.status is AgentStatus.PARTIAL
    assert empty.error_code == NO_DATA

    partial_hit = assemble_data_result(
        AgentState(
            task_id=1,
            user_id=100,
            messages=[],
            status=AgentStatus.COMPLETED,
            tool_results=[_record("query_customers", count=1), _record("query_sales_data", count=0)],
            final_result=final,
        )
    )
    assert partial_hit.status is AgentStatus.COMPLETED
    assert partial_hit.error_code is None

    no_tools = assemble_data_result(
        AgentState(
            task_id=1,
            user_id=100,
            messages=[],
            status=AgentStatus.COMPLETED,
            tool_results=[],
            final_result=final,
        )
    )
    assert no_tools.status is AgentStatus.COMPLETED
    assert no_tools.error_code is None


def test_assemble_raises_on_failed_state() -> None:
    """失败状态（如轮次截断）：组装入口抛 DataAgentError，不产出半成品结果。"""
    state = AgentState(
        task_id=1,
        user_id=100,
        messages=[],
        status=AgentStatus.FAILED,
        errors=["已达最大轮次上限（5），任务截断"],
    )

    with pytest.raises(DataAgentError, match="最大轮次"):
        assemble_data_result(state)


async def test_debug_endpoint_returns_data_result(client, seeded_maker, case_ids) -> None:
    """调试接口：POST /api/debug/agents/data 全链路返回 AgentResult。"""
    from app.api.routes.debug_agents import get_data_agent
    from app.main import app

    (a_id, _b_id, _p1_id, _p2_id) = case_ids
    llm = ScriptedLLM(
        [
            tool_call_response("call_1", "query_sales_data", json.dumps({"customer_id": a_id})),
            final_response("客户 A 最近 3 个月销售额逐月下降。"),
        ]
    )
    data_agent = DataAgent(llm, session_maker=seeded_maker)
    app.dependency_overrides[get_data_agent] = lambda: data_agent
    try:
        resp = await client.post(
            "/api/debug/agents/data", json={"query": "客户 A 最近销售额", "task_id": 7, "user_id": 1}
        )
    finally:
        app.dependency_overrides.pop(get_data_agent, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == DATA_AGENT_NAME
    assert body["status"] == "completed"
    assert body["error_code"] is None
    assert body["content"] == "客户 A 最近 3 个月销售额逐月下降。"
    assert body["sources"][0]["ref_id"] == f"query_sales_data(customer_id={a_id})"


async def test_debug_endpoint_disabled_when_debug_off(client, monkeypatch) -> None:
    """非 debug 模式：调试接口返回 404，不暴露给生产环境。"""
    from app.api.routes.debug_agents import get_data_agent
    from app.core.config import get_settings
    from app.main import app

    app.dependency_overrides[get_data_agent] = lambda: None
    monkeypatch.setattr(get_settings(), "debug", False)
    try:
        resp = await client.post("/api/debug/agents/data", json={"query": "在吗"})
    finally:
        app.dependency_overrides.pop(get_data_agent, None)

    assert resp.status_code == 404

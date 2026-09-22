"""三个业务子模块（database / business / knowledge）可注册测试（Prompt 6.2）。"""
from app.mcp.business import register_tools as register_business_tools
from app.mcp.database import register_tools as register_database_tools
from app.mcp.knowledge import register_tools as register_knowledge_tools
from app.mcp.registry import ToolRegistry
from app.mcp.tools.hello import HelloTool


def test_three_submodules_registerable():
    """注册链路全通过：hello + database 只读工具 + business 读写工具 + knowledge 检索工具正常登记。"""
    registry = ToolRegistry()
    registry.register(HelloTool())
    register_database_tools(registry)
    register_business_tools(registry)
    register_knowledge_tools(registry)
    tools = {t.name: t for t in registry.all_tools()}
    assert list(tools) == [
        "hello",
        "query_customers",
        "query_orders",
        "query_sales_data",
        "query_product_sales",
        "get_crm_summary",
        "create_ticket",
        "update_customer",
        "refund_order",
        "search_knowledge",
    ]
    # business 子模块风险等级齐全：读视图 LOW / 写操作 MEDIUM / 高风险写 HIGH
    assert tools["get_crm_summary"].risk_level.value == "low"
    assert tools["create_ticket"].risk_level.value == "medium"
    assert tools["update_customer"].risk_level.value == "high"
    assert tools["refund_order"].risk_level.value == "high"
    # knowledge 检索为只读安全工具
    assert tools["search_knowledge"].risk_level.value == "safe"


def test_register_default_tools_via_global_chain():
    """生产注册链 register_default_tools 可完整执行且不抛异常。"""
    from app.mcp.tools import register_default_tools

    registry = ToolRegistry()
    register_default_tools(registry)
    assert registry.has("hello")

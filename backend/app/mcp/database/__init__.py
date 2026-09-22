"""database 子模块：数据查询类工具（订单 / 销售 / 客户 / 工单等）。"""
from app.mcp.database.query_customers import QueryCustomersTool
from app.mcp.database.query_orders import QueryOrdersTool
from app.mcp.database.query_product_sales import QueryProductSalesTool
from app.mcp.database.query_sales_data import QuerySalesDataTool
from app.mcp.registry import ToolRegistry


def register_tools(registry: ToolRegistry) -> None:
    """登记本子模块全部工具。"""
    registry.register(
        QueryCustomersTool(),
        QueryOrdersTool(),
        QuerySalesDataTool(),
        QueryProductSalesTool(),
    )

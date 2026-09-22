"""business 子模块：业务操作类工具（读视图汇总与按制度执行的写动作）。"""
from app.mcp.business.create_ticket import CreateTicketTool
from app.mcp.business.get_crm_summary import GetCrmSummaryTool
from app.mcp.business.refund_order import RefundOrderTool
from app.mcp.business.update_customer import UpdateCustomerTool
from app.mcp.registry import ToolRegistry


def register_tools(registry: ToolRegistry) -> None:
    """登记本子模块全部工具。"""
    registry.register(GetCrmSummaryTool(), CreateTicketTool(), UpdateCustomerTool(), RefundOrderTool())

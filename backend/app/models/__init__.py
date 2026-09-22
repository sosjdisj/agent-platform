"""数据模型层（ORM models）。

此处统一导入全部模型，确保其注册到 Base.metadata，
Alembic autogenerate 与 metadata.create_all 均依赖该注册。
"""
from app.models.agent import AgentApproval, AgentTask, AgentTraceEvent, AgentTraceEventType
from app.models.base import StatusMixin, TimestampMixin
from app.models.customer import Customer
from app.models.customer_ticket import CustomerTicket
from app.models.evaluation_case import EvaluationCase
from app.models.knowledge_document import KnowledgeDocument
from app.models.order import Order
from app.models.product import Product
from app.models.rbac import Permission, Role, user_roles
from app.models.refund import Refund
from app.models.sales_record import SalesRecord
from app.models.user import User

__all__ = [
    "AgentApproval",
    "AgentTask",
    "AgentTraceEvent",
    "AgentTraceEventType",
    "Customer",
    "CustomerTicket",
    "EvaluationCase",
    "KnowledgeDocument",
    "Order",
    "Permission",
    "Product",
    "Refund",
    "Role",
    "SalesRecord",
    "StatusMixin",
    "TimestampMixin",
    "User",
    "user_roles",
]

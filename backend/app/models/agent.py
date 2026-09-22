"""Agent 系统表模型：任务、人工审批、执行轨迹事件。

- agent_tasks: Supervisor 接收的任务及最终结果
- agent_approvals: Human-in-the-loop 审批记录
- agent_trace_events: LangGraph 编排过程的执行轨迹，供 Trace 可视化与回放

项目采用逻辑外键策略：表间关联由应用层保证，数据库不建物理外键约束。
"""
import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import StatusMixin, TimestampMixin


class AgentTraceEventType(str, enum.Enum):
    """轨迹事件类型（存枚举值，全小写）。"""

    TASK_STARTED = "task_started"
    AGENT_SELECTED = "agent_selected"
    AGENT_FINISHED = "agent_finished"
    LLM_CALL = "llm_call"
    TOOL_CALLED = "tool_called"
    TOOL_RESULT = "tool_result"
    PERMISSION_DENIED = "permission_denied"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESULT = "approval_result"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"


class AgentTask(Base, TimestampMixin, StatusMixin):
    __tablename__ = "agent_tasks"
    __table_args__ = (Index("ix_agent_tasks_user_id", "user_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 逻辑外键 -> users.id
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[str | None] = mapped_column(Text)
    # 结构化九部分报告（AnalysisReport dump，24.1 报告页证据溯源用；
    # 与 result 文本同源于完成时的同一报告对象，历史行 / 未完成任务为 NULL）
    report: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 覆盖 StatusMixin 默认值：任务生命周期为 pending / running / waiting_approval /
    # completed / failed / cancelled（合法流转单一来源见 app.agents.state.TASK_TRANSITIONS）
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )


class AgentApproval(Base, TimestampMixin, StatusMixin):
    __tablename__ = "agent_approvals"
    __table_args__ = (Index("ix_agent_approvals_task_id", "task_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 逻辑外键 -> agent_tasks.id
    requester_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 逻辑外键 -> users.id
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    decision: Mapped[str | None] = mapped_column(String(20))
    decided_by: Mapped[int | None] = mapped_column(BigInteger)  # 逻辑外键 -> users.id
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)

    # 覆盖 StatusMixin 默认值：审批记录生命周期为 pending / approved / rejected
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )


class AgentTraceEvent(Base, TimestampMixin, StatusMixin):
    __tablename__ = "agent_trace_events"
    __table_args__ = (
        # 按 task_id 拉取完整轨迹并按时间排序的核心查询路径
        Index("ix_agent_trace_events_task_id_timestamp", "task_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 逻辑外键 -> agent_tasks.id
    agent: Mapped[str | None] = mapped_column(String(50))
    event_type: Mapped[AgentTraceEventType] = mapped_column(
        Enum(
            AgentTraceEventType,
            name="agent_trace_event_type",
            values_callable=lambda e: [i.value for i in e],
        ),
        nullable=False,
    )
    payload: Mapped[dict | None] = mapped_column(JSONB)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

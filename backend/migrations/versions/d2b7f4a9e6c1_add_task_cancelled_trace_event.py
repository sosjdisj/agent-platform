"""add task_cancelled trace event type

Revision ID: d2b7f4a9e6c1
Revises: c5d9e7f21a48
Create Date: 2026-09-21 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2b7f4a9e6c1'
down_revision: Union[str, None] = 'c5d9e7f21a48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """为 agent_trace_event_type 枚举追加 task_cancelled 值（21.3 取消埋点）。"""
    op.execute(
        "ALTER TYPE agent_trace_event_type ADD VALUE IF NOT EXISTS 'task_cancelled'"
    )


def downgrade() -> None:
    """PostgreSQL 枚举值不可直接删除：不重建类型，由应用层保证不再写入（语义上等同回退）。"""
    # PostgreSQL 不支持 DROP VALUE；保持空函数，由应用层保证不再写入
    pass

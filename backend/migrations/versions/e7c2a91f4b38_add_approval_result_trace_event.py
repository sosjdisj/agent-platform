"""add approval_result trace event type

Revision ID: e7c2a91f4b38
Revises: b6e1a93c4d27
Create Date: 2026-09-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7c2a91f4b38'
down_revision: Union[str, None] = 'b6e1a93c4d27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """为 agent_trace_event_type 枚举追加 approval_result 值（approval_required 已存在）。"""
    op.execute(
        "ALTER TYPE agent_trace_event_type ADD VALUE IF NOT EXISTS 'approval_result'"
    )


def downgrade() -> None:
    """PostgreSQL 枚举值不可直接删除：不重建类型，由应用层保证不再写入（语义上等同回退）。"""
    # PostgreSQL 不支持 DROP VALUE；保持空函数，由应用层保证不再写入
    pass

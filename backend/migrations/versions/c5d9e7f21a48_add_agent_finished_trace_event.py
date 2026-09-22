"""add agent_finished trace event type

Revision ID: c5d9e7f21a48
Revises: e7c2a91f4b38
Create Date: 2026-09-20 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5d9e7f21a48'
down_revision: Union[str, None] = 'e7c2a91f4b38'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """为 agent_trace_event_type 枚举追加 agent_finished 值（19.2 Agent 起止埋点）。"""
    op.execute(
        "ALTER TYPE agent_trace_event_type ADD VALUE IF NOT EXISTS 'agent_finished'"
    )


def downgrade() -> None:
    """PostgreSQL 枚举值不可直接删除：不重建类型，由应用层保证不再写入（语义上等同回退）。"""
    # PostgreSQL 不支持 DROP VALUE；保持空函数，由应用层保证不再写入
    pass

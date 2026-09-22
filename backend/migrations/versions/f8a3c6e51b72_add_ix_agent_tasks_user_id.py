"""add ix_agent_tasks_user_id

Revision ID: f8a3c6e51b72
Revises: d2b7f4a9e6c1
Create Date: 2026-09-21 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f8a3c6e51b72'
down_revision: Union[str, None] = 'd2b7f4a9e6c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """任务列表按 user_id 过滤（逻辑外键列补索引，避免全表扫描）。"""
    op.create_index('ix_agent_tasks_user_id', 'agent_tasks', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_agent_tasks_user_id', table_name='agent_tasks')

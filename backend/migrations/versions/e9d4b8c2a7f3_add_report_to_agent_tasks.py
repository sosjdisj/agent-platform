"""add report to agent_tasks

Revision ID: e9d4b8c2a7f3
Revises: f8a3c6e51b72
Create Date: 2026-09-21 23:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'e9d4b8c2a7f3'
down_revision: Union[str, None] = 'f8a3c6e51b72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """结构化九部分报告落库（24.1 报告页证据溯源）：JSONB 可空，历史行保持 NULL。"""
    op.add_column('agent_tasks', sa.Column('report', JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('agent_tasks', 'report')

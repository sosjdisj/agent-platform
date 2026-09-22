"""add requester_id to agent_approvals

Revision ID: b6e1a93c4d27
Revises: 8f4a7c2b1d90
Create Date: 2026-09-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6e1a93c4d27'
down_revision: Union[str, None] = '8f4a7c2b1d90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """审批记录补充发起人（ApprovalService.create 必填，历史行为空表不受影响）。"""
    op.add_column(
        'agent_approvals',
        sa.Column('requester_id', sa.BigInteger(), nullable=False, comment='逻辑外键 -> users.id'),
    )


def downgrade() -> None:
    op.drop_column('agent_approvals', 'requester_id')

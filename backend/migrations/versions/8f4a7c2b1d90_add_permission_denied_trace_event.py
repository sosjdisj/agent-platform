"""add permission_denied trace event type

Revision ID: 8f4a7c2b1d90
Revises: 3d59e58bb439
Create Date: 2026-09-20 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8f4a7c2b1d90'
down_revision: Union[str, None] = '3d59e58bb439'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """为 agent_trace_event_type 枚举追加 permission_denied 值。

    PostgreSQL 枚举追加值是可逆的（新值不影响既有行），直接用 ALTER TYPE ... ADD VALUE。
    """
    op.execute(
        "ALTER TYPE agent_trace_event_type ADD VALUE IF NOT EXISTS 'permission_denied'"
    )


def downgrade() -> None:
    """PostgreSQL 枚举值不可直接删除：删除该值需重建类型（会重写全部 trace 事件行）。

    此处仅删除未来新建该值的能力（IF NOT EXISTS 保证幂等），不重建类型，避免数据丢失；
    应用层亦不再写入 permission_denied 事件，语义上等同回退。
    """
    # PostgreSQL 不支持 DROP VALUE；保持空函数，由应用层保证不再写入
    pass

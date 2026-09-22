"""add refunds table

Revision ID: f1a2b3c4d5e6
Revises: e9d4b8c2a7f3
Create Date: 2026-09-21 12:00:00.000000

补漏：refund_order 业务工具落库依赖 refunds 表，历史迁移链从未创建
（测试环境经 create_all 建表，未暴露该缺口）。按 Hard Constraints 使用
逻辑外键（不建物理 FK），保留 order_id 列索引防止关联查询全表扫描。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e9d4b8c2a7f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('refunds',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('order_id', sa.BigInteger(), nullable=False),  # 逻辑外键 -> orders.id
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_refunds_order_id', 'refunds', ['order_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_refunds_order_id', table_name='refunds')
    op.drop_table('refunds')

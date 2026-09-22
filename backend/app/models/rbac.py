"""角色、权限与用户-角色关联表。

项目采用逻辑外键策略：表间关联由应用层保证，数据库不建物理外键约束。
"""
from sqlalchemy import BigInteger, Column, String, Table
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import StatusMixin, TimestampMixin

# 用户-角色多对多关联：复合主键 (user_id, role_id) 即联合唯一索引
user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", BigInteger, primary_key=True),  # 逻辑外键 -> users.id
    Column("role_id", BigInteger, primary_key=True),  # 逻辑外键 -> roles.id
)

# 角色-权限多对多关联：复合主键 (role_id, permission_id) 即联合唯一索引
role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", BigInteger, primary_key=True),  # 逻辑外键 -> roles.id
    Column("permission_id", BigInteger, primary_key=True),  # 逻辑外键 -> permissions.id
)


class Role(Base, TimestampMixin, StatusMixin):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))


class Permission(Base, TimestampMixin, StatusMixin):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))

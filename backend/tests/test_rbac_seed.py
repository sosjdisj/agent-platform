"""Prompt 17.1：RBAC 种子数据测试——3 角色 × 7 权限映射入库，get_user_permissions 解析正确。

期望映射在测试内独立硬编码（不引用 rbac_service 常量），避免与被测数据构成同义反复。
"""
from sqlalchemy import func, insert, select

from app.models.rbac import Permission, Role, role_permissions, user_roles
from app.models.user import User
from app.services.rbac_service import get_user_permissions, seed_rbac

# 各角色的期望权限集合（与 README「RBAC 角色与权限清单」一致）
EXPECTED_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "employee": {"customer:read", "order:read", "knowledge:search"},
    "sales": {
        "customer:read",
        "customer:write",
        "order:read",
        "sales:read",
        "ticket:create",
        "knowledge:search",
    },
    "admin": {
        "customer:read",
        "customer:write",
        "order:read",
        "order:refund",
        "sales:read",
        "ticket:create",
        "knowledge:search",
    },
}


async def _create_user_with_role(session, role_name: str) -> int:
    """创建一个用户并分配指定角色，返回 user_id。"""
    user = User(username=f"u_{role_name}", email=f"u_{role_name}@test.com", hashed_password="x")
    session.add(user)
    await session.flush()
    role_id = await session.scalar(select(Role.id).where(Role.name == role_name))
    await session.execute(insert(user_roles).values(user_id=user.id, role_id=role_id))
    return user.id


async def test_seed_creates_roles_permissions_and_mappings(db_session):
    """seed 后：3 个 active 角色、7 个 active 权限、映射数 = 各角色权限数之和。"""
    counts = await seed_rbac(db_session)

    assert counts == {"roles": 3, "permissions": 7, "mappings": 16}
    role_names = set(await db_session.scalars(select(Role.name).where(Role.status == "active")))
    assert role_names == set(EXPECTED_ROLE_PERMISSIONS)
    perm_codes = set(
        await db_session.scalars(select(Permission.code).where(Permission.status == "active"))
    )
    assert perm_codes == {"customer:read", "customer:write", "order:read", "order:refund",
                          "sales:read", "ticket:create", "knowledge:search"}


async def test_get_user_permissions_per_role(db_session):
    """验收核心：三种角色的用户经四表联查解析出各自正确的权限集合。"""
    await seed_rbac(db_session)

    for role_name, expected in EXPECTED_ROLE_PERMISSIONS.items():
        user_id = await _create_user_with_role(db_session, role_name)
        assert await get_user_permissions(db_session, user_id) == expected, role_name


async def test_seed_rbac_idempotent(db_session):
    """重复执行不产生重复数据，映射与角色/权限 id 保持稳定。"""
    await seed_rbac(db_session)
    ids_before = set(await db_session.scalars(select(Role.id)))
    perm_ids_before = set(await db_session.scalars(select(Permission.id)))
    mapping_before = set((await db_session.execute(select(role_permissions))).all())

    await seed_rbac(db_session)

    assert set(await db_session.scalars(select(Role.id))) == ids_before
    assert set(await db_session.scalars(select(Permission.id))) == perm_ids_before
    assert set((await db_session.execute(select(role_permissions))).all()) == mapping_before
    assert await db_session.scalar(select(func.count()).select_from(Role)) == 3
    assert await db_session.scalar(select(func.count()).select_from(Permission)) == 7


async def test_seed_preserves_user_role_assignments(db_session):
    """seed 幂等重跑不破坏既有用户的角色分配（user_roles 不被触碰）。"""
    await seed_rbac(db_session)
    user_id = await _create_user_with_role(db_session, "employee")

    await seed_rbac(db_session)

    assert await get_user_permissions(db_session, user_id) == EXPECTED_ROLE_PERMISSIONS["employee"]


async def test_unknown_user_has_no_permissions(db_session):
    """未分配角色的用户解析结果为空集合。"""
    await seed_rbac(db_session)

    assert await get_user_permissions(db_session, 999999) == set()

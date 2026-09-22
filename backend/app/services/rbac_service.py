"""RBAC 权限解析与种子数据：user_roles → roles → role_permissions → permissions 四表联查。"""
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rbac import Permission, Role, role_permissions, user_roles

# ---------- 权限与角色定义（单一事实源，seed、测试与文档均以此为准）----------

# 七个权限：覆盖现有 MCP 工具面（查询类 customer:read / order:read / sales:read / knowledge:search，
# 写入类 customer:write / ticket:create，高危类 order:refund）
PERMISSIONS: list[dict[str, str]] = [
    {"code": "customer:read", "name": "客户查询", "description": "查询客户信息与 CRM 概览"},
    {"code": "customer:write", "name": "客户更新", "description": "修改客户资料"},
    {"code": "order:read", "name": "订单查询", "description": "查询订单明细"},
    {"code": "order:refund", "name": "订单退款", "description": "发起订单退款（高危）"},
    {"code": "sales:read", "name": "销售查询", "description": "查询销售数据与聚合分析"},
    {"code": "ticket:create", "name": "工单创建", "description": "创建客户工单"},
    {"code": "knowledge:search", "name": "知识检索", "description": "检索知识库文档"},
]

ROLES: list[dict[str, str]] = [
    {"name": "employee", "description": "普通员工：业务数据只读 + 知识库检索"},
    {"name": "sales", "description": "销售人员：客户/订单/销售数据查询与写入、工单创建"},
    {"name": "admin", "description": "管理员：全部权限（含订单退款）"},
]

# 角色 → 权限 code 集合：employee ⊂ sales ⊂ admin 逐级递增
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "employee": {"customer:read", "order:read", "knowledge:search"},
    "sales": {"customer:read", "customer:write", "order:read", "sales:read", "ticket:create", "knowledge:search"},
    "admin": {p["code"] for p in PERMISSIONS},
}


async def get_user_permissions(session: AsyncSession, user_id: int) -> set[str]:
    """解析用户的最终权限 code 集合，仅统计 active 状态的角色与权限。"""
    stmt = (
        select(Permission.code)
        .join(role_permissions, role_permissions.c.permission_id == Permission.id)
        .join(Role, Role.id == role_permissions.c.role_id)
        .join(user_roles, user_roles.c.role_id == Role.id)
        .where(
            user_roles.c.user_id == user_id,
            Role.status == "active",
            Permission.status == "active",
        )
        .distinct()
    )
    return set(await session.scalars(stmt))


async def seed_rbac(session: AsyncSession) -> dict[str, int]:
    """幂等对齐 RBAC 字典数据（角色 / 权限 / 角色-权限映射），不触碰用户-角色分配。

    角色 / 权限按唯一键（name / code）存在则更新（含强制 active）、缺失则插入；
    映射关系仅对这三个角色全量重建，保证与 ROLE_PERMISSIONS 定义严格一致。
    返回各类数据行数，供调用方打印或断言。
    """
    for spec in PERMISSIONS:
        perm = await session.scalar(select(Permission).where(Permission.code == spec["code"]))
        if perm is None:
            session.add(Permission(**spec, status="active"))
        else:
            perm.name, perm.description, perm.status = spec["name"], spec["description"], "active"

    for spec in ROLES:
        role = await session.scalar(select(Role).where(Role.name == spec["name"]))
        if role is None:
            session.add(Role(**spec, status="active"))
        else:
            role.description, role.status = spec["description"], "active"
    await session.flush()  # 取得 id，供映射引用

    perm_codes: dict[str, int] = dict(
        (await session.execute(select(Permission.code, Permission.id))).all()
    )
    role_names: dict[str, int] = dict((await session.execute(select(Role.name, Role.id))).all())

    await session.execute(
        delete(role_permissions).where(
            role_permissions.c.role_id.in_([role_names[r["name"]] for r in ROLES])
        )
    )
    mapping_rows = [
        {"role_id": role_names[name], "permission_id": perm_codes[code]}
        for name, codes in ROLE_PERMISSIONS.items()
        for code in codes
    ]
    await session.execute(insert(role_permissions), mapping_rows)
    await session.commit()

    return {"roles": len(ROLES), "permissions": len(PERMISSIONS), "mappings": len(mapping_rows)}


async def main() -> None:
    """CLI 入口：python -m app.services.rbac_service。"""
    from app.db.session import SessionLocal, engine

    async with SessionLocal() as session:
        counts = await seed_rbac(session)
        print(f"RBAC Seed 完成: {counts}")
    await engine.dispose()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())

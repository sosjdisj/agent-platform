"""认证全链路测试：注册 → 登录 → /me，双令牌刷新/撤销，RBAC 权限解析，密码无明文落库校验。"""
import json
from datetime import datetime, timedelta, timezone

import asyncpg
import jwt
import pytest
from fastapi import Depends
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.security import ACCESS_TOKEN_TYPE, decode_token
from app.models.user import User

settings = get_settings()

USERNAME = "alice"
EMAIL = "alice@example.com"
PASSWORD = "super-secret-123"


async def register(client: AsyncClient, **overrides):
    body = {"username": USERNAME, "email": EMAIL, "password": PASSWORD} | overrides
    return await client.post("/api/auth/register", json=body)


async def login(client: AsyncClient, username: str = USERNAME, password: str = PASSWORD) -> dict:
    """用给定用户名密码登录，返回响应体（仅 access_token，refresh 在 Cookie 中）。"""
    resp = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200
    return resp.json()


async def register_and_login(client: AsyncClient) -> dict:
    """注册并登录，返回令牌对。"""
    assert (await register(client)).status_code == 201
    return await login(client)


def make_expired_token(token_type: str, user_id: int = 1) -> str:
    """构造一个已过期但签名合法的 JWT。"""
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        "type": token_type,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


async def test_register_success(api_client: AsyncClient):
    resp = await register(api_client)
    assert resp.status_code == 201
    data = resp.json()
    assert data["username"] == USERNAME
    assert data["email"] == EMAIL
    assert "id" in data
    # 响应不应泄漏任何密码字段
    assert not any("password" in key for key in data)


async def test_register_duplicate_username(api_client: AsyncClient):
    assert (await register(api_client)).status_code == 201
    resp = await register(api_client, email="other@example.com")
    assert resp.status_code == 409


async def test_register_duplicate_email(api_client: AsyncClient):
    assert (await register(api_client)).status_code == 201
    resp = await register(api_client, username="bob")
    assert resp.status_code == 409


async def test_register_invalid_payload(api_client: AsyncClient):
    # 用户名过短 / 邮箱格式错误 / 密码过短
    for body in (
        {"username": "a", "email": EMAIL, "password": PASSWORD},
        {"username": USERNAME, "email": "not-an-email", "password": PASSWORD},
        {"username": USERNAME, "email": EMAIL, "password": "short"},
    ):
        resp = await api_client.post("/api/auth/register", json=body)
        assert resp.status_code == 422


async def test_login_success(api_client: AsyncClient):
    assert (await register(api_client)).status_code == 201
    resp = await api_client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["token_type"] == "bearer"
    assert data["access_token"]
    # 长效 refresh token 不进响应体，仅通过 HttpOnly Cookie 下发
    assert "refresh_token" not in data
    assert "HttpOnly" in _cookie(resp, settings.refresh_cookie_name)


async def test_login_wrong_password(api_client: AsyncClient):
    assert (await register(api_client)).status_code == 201
    resp = await api_client.post(
        "/api/auth/login", json={"username": USERNAME, "password": "wrong-password"}
    )
    assert resp.status_code == 401


async def test_login_unknown_user(api_client: AsyncClient):
    resp = await api_client.post(
        "/api/auth/login", json={"username": "nobody", "password": PASSWORD}
    )
    assert resp.status_code == 401


async def test_me_full_chain(api_client: AsyncClient):
    """注册 → 登录 → 携带 Token 访问 /me，返回当前用户信息。"""
    assert (await register(api_client)).status_code == 201

    login_resp = await api_client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await api_client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == USERNAME
    assert data["email"] == EMAIL

    # 未携带 Token / Token 无效 → 401
    assert (await api_client.get("/api/auth/me")).status_code == 401
    bad = {"Authorization": "Bearer not-a-valid-token"}
    assert (await api_client.get("/api/auth/me", headers=bad)).status_code == 401


async def test_password_not_stored_in_plaintext(api_client: AsyncClient, test_database_url: str):
    assert (await register(api_client)).status_code == 201

    conn = await asyncpg.connect(test_database_url.replace("+asyncpg", ""))
    try:
        row = await conn.fetchrow("SELECT hashed_password FROM users WHERE username = $1", USERNAME)
    finally:
        await conn.close()

    stored = row["hashed_password"]
    assert stored != PASSWORD
    # Argon2 哈希以 $argon2 开头
    assert stored.startswith("$argon2")


async def test_refresh_rotation(api_client: AsyncClient):
    """refresh 成功换新令牌；旧 refresh 立即失效；新 refresh 可继续使用。"""
    await register_and_login(api_client)
    old_refresh = api_client.cookies.get(settings.refresh_cookie_name)
    assert old_refresh

    # httpx 客户端自动携带登录响应保存的 refresh Cookie
    resp = await api_client.post("/api/auth/refresh")
    assert resp.status_code == 200
    assert resp.json()["access_token"]
    new_refresh = api_client.cookies.get(settings.refresh_cookie_name)
    assert new_refresh != old_refresh  # rotation：jti 不同

    # 旧 refresh 已被撤销 → 401
    api_client.cookies.set(settings.refresh_cookie_name, old_refresh)
    assert (await api_client.post("/api/auth/refresh")).status_code == 401

    # 新 refresh 仍可继续刷新
    api_client.cookies.set(settings.refresh_cookie_name, new_refresh)
    assert (await api_client.post("/api/auth/refresh")).status_code == 200


async def test_refresh_without_cookie_401(api_client: AsyncClient):
    """无 refresh Cookie → 401 + 结构化错误体 TOKEN_MISSING。"""
    await register(api_client)
    resp = await api_client.post("/api/auth/refresh")
    assert resp.status_code == 401
    assert _error_body(resp)["error"]["code"] == "TOKEN_MISSING"


async def test_logout_revokes_refresh(api_client: AsyncClient):
    """logout 后该 refresh token 被拒绝刷新。"""
    await register_and_login(api_client)
    old_refresh = api_client.cookies.get(settings.refresh_cookie_name)

    assert (await api_client.post("/api/auth/logout")).status_code == 204

    # 用旧 refresh Cookie 再刷新 → 401（Redis 状态已撤销）
    api_client.cookies.set(settings.refresh_cookie_name, old_refresh)
    denied = await api_client.post("/api/auth/refresh")
    assert denied.status_code == 401


async def test_logout_idempotent(api_client: AsyncClient):
    """重复 logout / 无 Cookie / 无效 token logout 均幂等成功。"""
    await register_and_login(api_client)
    assert (await api_client.post("/api/auth/logout")).status_code == 204
    # Cookie 已被清除，无 Cookie 登出仍 204
    assert (await api_client.post("/api/auth/logout")).status_code == 204
    api_client.cookies.set(settings.refresh_cookie_name, "garbage")
    assert (await api_client.post("/api/auth/logout")).status_code == 204


async def test_expired_access_token_rejected(api_client: AsyncClient):
    """过期的 access token 访问 /me 返回 401。"""
    await register_and_login(api_client)
    expired = make_expired_token(ACCESS_TOKEN_TYPE)
    headers = {"Authorization": f"Bearer {expired}"}
    resp = await api_client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 401


async def test_token_type_mixing_rejected(api_client: AsyncClient):
    """access 与 refresh token 不能混用：refresh 当 access 用 → 401，反之亦然。"""
    tokens = await register_and_login(api_client)
    refresh = api_client.cookies.get(settings.refresh_cookie_name)
    assert refresh

    # refresh token 当 access token 访问 /me → 401
    headers = {"Authorization": f"Bearer {refresh}"}
    assert (await api_client.get("/api/auth/me", headers=headers)).status_code == 401

    # access token 当 refresh cookie 刷新 → 401
    api_client.cookies.set(settings.refresh_cookie_name, tokens["access_token"])
    assert (await api_client.post("/api/auth/refresh")).status_code == 401


# ---------- Prompt 3.3：统一认证异常 + 权限解析 ----------


def _error_body(resp) -> dict:
    """断言结构化错误格式 {"error": {"code", "message"}} 并返回内容。"""
    body = json.loads(resp.content)
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    return body


async def test_me_without_token_401_structure(api_client: AsyncClient):
    """无 token → 401 + 结构化错误体。"""
    resp = await api_client.get("/api/auth/me")
    assert resp.status_code == 401
    assert _error_body(resp)["error"]["code"] == "TOKEN_MISSING"


async def test_me_invalid_token_401_structure(api_client: AsyncClient):
    """无效 token → 401 + 结构化错误体。"""
    for token in ("not-a-valid-token", make_expired_token(ACCESS_TOKEN_TYPE)):
        headers = {"Authorization": f"Bearer {token}"}
        resp = await api_client.get("/api/auth/me", headers=headers)
        assert resp.status_code == 401
        assert _error_body(resp)["error"]["code"] == "TOKEN_INVALID"


async def test_auth_error_403_structure():
    """AuthError 支持 403 授权拒绝，返回统一结构。"""
    from types import SimpleNamespace

    from app.core.errors import AuthError, auth_error_handler

    resp = await auth_error_handler(
        SimpleNamespace(), AuthError("PERMISSION_DENIED", "无权限执行该操作", 403)
    )
    assert resp.status_code == 403
    assert json.loads(resp.body) == {
        "error": {"code": "PERMISSION_DENIED", "message": "无权限执行该操作"}
    }


async def test_me_permissions_empty_for_new_user(api_client: AsyncClient):
    """新注册用户未分配任何角色 → /me 返回空权限集合。"""
    reg = await register(api_client)
    tokens = await login(api_client)  # 复用同一用户登录

    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    resp = await api_client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == reg.json()["id"]
    assert data["username"] == USERNAME
    assert data["permissions"] == []


async def test_me_permissions_resolved_from_rbac(api_client: AsyncClient, test_database_url: str):
    """通过 user_roles → roles → role_permissions → permissions 解析权限；
    disabled 角色与 disabled 权限不参与解析。"""
    reg = await register(api_client)
    user_id = reg.json()["id"]

    # 造 RBAC 数据：analyst（active）带 2 active + 1 disabled 权限；retired（disabled）角色
    engine = create_async_engine(test_database_url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO roles (id, name, status) VALUES "
                "(1, 'analyst', 'active'), (2, 'retired', 'disabled')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO permissions (id, code, name, status) VALUES "
                "(1, 'customer:read', '查看客户', 'active'), "
                "(2, 'order:read', '查看订单', 'active'), "
                "(3, 'order:write', '写入订单', 'disabled')"
            )
        )
        await conn.execute(
            text("INSERT INTO user_roles (user_id, role_id) VALUES (:uid, 1), (:uid, 2)"),
            {"uid": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO role_permissions (role_id, permission_id) VALUES "
                "(1, 1), (1, 2), (1, 3), (2, 1)"
            )
        )
    await engine.dispose()

    tokens = await login(api_client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    resp = await api_client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200
    # disabled 角色（retired）与 disabled 权限（order:write）均被过滤
    assert set(resp.json()["permissions"]) == {"customer:read", "order:read"}


# ---------- Prompt 3.4：认证 Cookie（SSE access token + 长效 refresh token） ----------


def _cookie(resp, name: str) -> str:
    """从响应 set-cookie 列表中取指定 Cookie，不存在则断言失败。"""
    headers = resp.headers.get_list("set-cookie")
    matched = [c for c in headers if c.startswith(f"{name}=")]
    assert matched, f"缺少 Cookie {name}，set-cookie: {headers}"
    return matched[0]


@pytest.fixture
def sse_probe_route():
    """注册一个受 get_current_user_sse 保护的探针路由，用完即移除。"""
    from app.api.deps import get_current_user_sse
    from app.main import app

    @app.get("/api/auth/sse-probe", include_in_schema=False)
    async def probe(user: User = Depends(get_current_user_sse)) -> dict:
        return {"id": user.id}

    # 动态注册默认追加到路由表末尾，会被 SPA 兜底路由（/​{full_path:path}）拦截，需移到最前
    probe_route = app.router.routes.pop()
    app.router.routes.insert(0, probe_route)
    yield
    app.router.routes.remove(probe_route)


async def test_login_sets_httponly_sse_cookie(api_client: AsyncClient):
    """登录响应写入 HttpOnly SSE Cookie，属性与约定一致。"""
    await register(api_client)
    resp = await api_client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    cookie = _cookie(resp, settings.sse_cookie_name)
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    # 生命周期与 access token 一致（15 分钟）
    assert f"Max-Age={settings.access_token_expire_minutes * 60}" in cookie


async def test_login_sets_httponly_refresh_cookie(api_client: AsyncClient):
    """登录响应写入 HttpOnly refresh Cookie，生命周期与 refresh token 一致。"""
    await register(api_client)
    resp = await api_client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}
    )
    cookie = _cookie(resp, settings.refresh_cookie_name)
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert f"Max-Age={settings.refresh_token_expire_days * 86400}" in cookie


async def test_refresh_rotates_sse_cookie(api_client: AsyncClient):
    """refresh 成功后 SSE Cookie 轮转为新的有效 access token。"""
    user_id = (await register(api_client)).json()["id"]
    await login(api_client)
    resp = await api_client.post("/api/auth/refresh")
    assert resp.status_code == 200
    cookie = _cookie(resp, settings.sse_cookie_name)
    new_value = cookie.split("=", 1)[1].split(";", 1)[0]
    # JWT 时间戳为秒级，同一秒内重签的 token 逐字节相同，故校验有效性而非字节差异
    payload = decode_token(new_value, expected_type=ACCESS_TOKEN_TYPE)
    assert int(payload["sub"]) == user_id


async def test_logout_clears_sse_cookie(api_client: AsyncClient):
    """logout 响应删除 SSE 与 refresh 两个 Cookie（Max-Age=0）。"""
    await register_and_login(api_client)
    resp = await api_client.post("/api/auth/logout")
    assert resp.status_code == 204
    assert "max-age=0" in _cookie(resp, settings.sse_cookie_name).lower()
    assert "max-age=0" in _cookie(resp, settings.refresh_cookie_name).lower()


async def test_sse_cookie_authenticates_user(api_client: AsyncClient, sse_probe_route):
    """携带登录后自动保存的 SSE Cookie 可通过 Cookie 认证（EventSource 场景）。"""
    user_id = (await register(api_client)).json()["id"]
    await login(api_client)  # 登录响应的 Set-Cookie 由 httpx 客户端自动保存

    resp = await api_client.get("/api/auth/sse-probe")
    assert resp.status_code == 200
    assert resp.json() == {"id": user_id}


async def test_sse_cookie_missing_rejected(api_client: AsyncClient, sse_probe_route):
    """无 SSE Cookie → 401 + 结构化错误体。"""
    resp = await api_client.get("/api/auth/sse-probe")
    assert resp.status_code == 401
    assert _error_body(resp)["error"]["code"] == "TOKEN_MISSING"

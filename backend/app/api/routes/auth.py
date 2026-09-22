"""认证接口：注册、登录、刷新、登出、获取当前用户。

refresh token 全程经 HttpOnly Cookie 传递（下发 / 刷新 / 登出），不进响应体；
SSE access token Cookie 供 EventSource 使用。禁止通过 URL 传递 token。
"""
from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.cookies import (
    delete_refresh_cookie,
    delete_sse_cookie,
    set_refresh_cookie,
    set_sse_cookie,
)
from app.core.errors import AuthError
from app.core.redis import get_redis
from app.db.session import get_session
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.services.auth_service import AuthService
from app.services.rbac_service import get_user_permissions
from redis.asyncio import Redis

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> UserResponse:
    """注册新用户，密码以 Argon2 哈希入库。"""
    user = await AuthService(session, redis).register(body.username, body.email, body.password)
    return UserResponse.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> TokenResponse:
    """登录并签发双令牌；refresh token 写入 HttpOnly Cookie，SSE Cookie 承载当前 access token。"""
    access, refresh = await AuthService(session, redis).login(body.username, body.password)
    set_sse_cookie(response, access)
    set_refresh_cookie(response, refresh)
    return TokenResponse(access_token=access)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> TokenResponse:
    """从 Cookie 读取 refresh token 完成轮转（旧值立即失效），并轮转两个 Cookie。"""
    token = request.cookies.get(settings.refresh_cookie_name)
    if not token:
        raise AuthError("TOKEN_MISSING", "登录已失效，请重新登录")
    access, new_refresh = await AuthService(session, redis).refresh(token)
    set_sse_cookie(response, access)
    set_refresh_cookie(response, new_refresh)
    return TokenResponse(access_token=access)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> None:
    """从 Cookie 读取 refresh token 并撤销 Redis 状态，同时清除两个 Cookie（幂等）。"""
    token = request.cookies.get(settings.refresh_cookie_name)
    if token:
        await AuthService(session, redis).logout(token)
    delete_sse_cookie(response)
    delete_refresh_cookie(response)


@router.get("/me", response_model=UserResponse)
async def me(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserResponse:
    """返回当前登录用户信息及其权限集合（临时实现：解码 JWT + RBAC 四表解析）。"""
    resp = UserResponse.model_validate(user)
    resp.permissions = await get_user_permissions(session, user.id)
    return resp

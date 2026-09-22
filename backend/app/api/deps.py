"""API 公共依赖：Bearer 头（常规请求）或 SSE Cookie（EventSource）解析当前登录用户。"""
import jwt
from fastapi import Cookie, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AuthError
from app.core.security import ACCESS_TOKEN_TYPE, decode_token
from app.db.session import get_session
from app.models.user import User
from app.repositories.user import UserRepository

settings = get_settings()
_bearer = HTTPBearer(auto_error=False)


async def _resolve_user(token: str, session: AsyncSession) -> User:
    """解码 access token 并加载用户，无效 / 过期 / 用户不存在统一 401。"""
    try:
        payload = decode_token(token, expected_type=ACCESS_TOKEN_TYPE)
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise AuthError("TOKEN_INVALID", "登录已失效，请重新登录")
    user = await UserRepository(session).get(user_id)
    if user is None:
        raise AuthError("USER_NOT_FOUND", "用户不存在")
    return user


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    """从 Authorization: Bearer <token> 解析当前用户。"""
    if credentials is None:
        raise AuthError("TOKEN_MISSING", "请先登录后再进行操作")
    return await _resolve_user(credentials.credentials, session)


async def get_current_user_sse(
    sse_token: str | None = Cookie(default=None, alias=settings.sse_cookie_name),
    session: AsyncSession = Depends(get_session),
) -> User:
    """从 SSE 认证 Cookie（HttpOnly，登录/刷新时写入）解析当前用户。

    供 EventSource 等无法携带 Authorization 头的接口使用，禁止 URL 传 token。
    """
    if sse_token is None:
        raise AuthError("TOKEN_MISSING", "请先登录后再进行操作")
    return await _resolve_user(sse_token, session)

"""认证 Cookie：HttpOnly 承载 SSE access token 与长效 refresh token。

- sse_token：当前 access token，供 EventSource 等无法携带 Authorization 头的接口使用；
- refresh_token：长效刷新令牌，浏览器 JS 不可读，/refresh 与 /logout 从 Cookie 读取。
禁止通过 URL 传递 token，约定详见 README「认证与 SSE Cookie 约定」。
"""
from fastapi import Response

from app.core.config import get_settings

settings = get_settings()

_COOKIE_PATH = "/"


def set_sse_cookie(response: Response, access_token: str) -> None:
    """写入 SSE 认证 Cookie，生命周期与 access token 一致（过期前由 /refresh 轮转续期）。"""
    _set_cookie(
        response, settings.sse_cookie_name, access_token,
        settings.access_token_expire_minutes * 60,
    )


def set_refresh_cookie(response: Response, refresh_token: str) -> None:
    """写入 refresh token Cookie，生命周期与 refresh token 一致（rotation 时自动覆盖旧值）。"""
    _set_cookie(
        response, settings.refresh_cookie_name, refresh_token,
        settings.refresh_token_expire_days * 86400,
    )


def delete_sse_cookie(response: Response) -> None:
    """登出时删除 SSE 认证 Cookie。"""
    _delete_cookie(response, settings.sse_cookie_name)


def delete_refresh_cookie(response: Response) -> None:
    """登出时删除 refresh token Cookie。"""
    _delete_cookie(response, settings.refresh_cookie_name)


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        path=_COOKIE_PATH,
        domain=settings.cookie_domain or None,
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )


def _delete_cookie(response: Response, name: str) -> None:
    response.delete_cookie(
        key=name,
        path=_COOKIE_PATH,
        domain=settings.cookie_domain or None,
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )

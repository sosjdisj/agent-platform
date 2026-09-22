"""认证服务：注册、登录、双令牌签发、Refresh Rotation 与撤销。"""
import jwt
from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AuthError
from app.core.security import (
    REFRESH_TOKEN_TYPE,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import User
from app.repositories.user import UserRepository

settings = get_settings()

# refresh token 的 jti 在 Redis 中的 key 前缀，value 为 user_id
REFRESH_KEY_PREFIX = "auth:refresh:"


class AuthService:
    def __init__(self, session: AsyncSession, redis: Redis) -> None:
        self.repo = UserRepository(session)
        self.redis = redis

    async def register(self, username: str, email: str, password: str) -> User:
        """注册新用户，用户名或邮箱已存在时返回 409。"""
        if await self.repo.get_by_username(username):
            raise HTTPException(status.HTTP_409_CONFLICT, "用户名已被占用")
        if await self.repo.get_by_email(email):
            raise HTTPException(status.HTTP_409_CONFLICT, "邮箱已经注册过了")
        user = await self.repo.create(
            username=username, email=email, hashed_password=hash_password(password)
        )
        await self.repo.session.commit()
        return user

    async def login(self, username: str, password: str) -> tuple[str, str]:
        """校验用户名密码，成功则签发双令牌（access, refresh）。"""
        user = await self.repo.get_by_username(username)
        if user is None or not verify_password(password, user.hashed_password):
            raise AuthError("BAD_CREDENTIALS", "用户名或密码错误")
        return await self._issue_tokens(user.id)

    async def refresh(self, refresh_token: str) -> tuple[str, str]:
        """用 refresh token 换取新双令牌（rotation：旧 refresh 立即失效）。

        通过 Redis GETDEL 原子取出并删除旧 jti，天然防止并发重放。
        """
        payload = self._decode_refresh(refresh_token)
        jti: str = payload["jti"]
        user_id = int(payload["sub"])

        stored = await self.redis.getdel(f"{REFRESH_KEY_PREFIX}{jti}")
        if stored is None or int(stored) != user_id:
            raise AuthError("REFRESH_REVOKED", "登录已失效，请重新登录")
        return await self._issue_tokens(user_id)

    async def logout(self, refresh_token: str) -> None:
        """幂等撤销：尽力解析 jti 并删除 Redis 状态，无有效状态也返回成功。"""
        try:
            payload = self._decode_refresh(refresh_token)
        except AuthError:
            return
        await self.redis.delete(f"{REFRESH_KEY_PREFIX}{payload['jti']}")

    async def _issue_tokens(self, user_id: int) -> tuple[str, str]:
        """签发双令牌 (access, refresh)，并将 refresh 的 jti 状态写入 Redis。

        refresh token 由路由层写入 HttpOnly Cookie，不进响应体。
        """
        access = create_access_token(user_id)
        refresh_token, jti = create_refresh_token(user_id)
        ttl_seconds = settings.refresh_token_expire_days * 86400
        await self.redis.set(f"{REFRESH_KEY_PREFIX}{jti}", user_id, ex=ttl_seconds)
        return access, refresh_token

    @staticmethod
    def _decode_refresh(refresh_token: str) -> dict:
        """解码 refresh token，无效 / 过期 / 类型不符统一 401。"""
        try:
            payload = decode_token(refresh_token, expected_type=REFRESH_TOKEN_TYPE)
        except jwt.PyJWTError:
            raise AuthError("REFRESH_INVALID", "登录已失效，请重新登录")
        if "jti" not in payload:
            raise AuthError("REFRESH_INVALID", "登录已失效，请重新登录")
        return payload

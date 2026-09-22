"""密码哈希（Argon2）与 JWT 签发/解码工具。"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from pwdlib import PasswordHash

from app.core.config import get_settings

settings = get_settings()

# recommended() 即 Argon2（需安装 pwdlib[argon2]）
_password_hash = PasswordHash.recommended()

# JWT type claim，用于区分 access / refresh，防止混用
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


def hash_password(plain: str) -> str:
    """生成 Argon2 哈希。"""
    return _password_hash.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码与哈希是否匹配。"""
    return _password_hash.verify(plain, hashed)


def create_access_token(user_id: int) -> str:
    """签发短命 Access Token（默认 15 分钟）。"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": str(user_id), "exp": expire, "type": ACCESS_TOKEN_TYPE}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: int) -> tuple[str, str]:
    """签发长命 Refresh Token（默认 7 天），返回 (token, jti)。"""
    jti = uuid4().hex
    expire = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    payload = {
        "sub": str(user_id),
        "exp": expire,
        "type": REFRESH_TOKEN_TYPE,
        "jti": jti,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm), jti


def decode_token(token: str, expected_type: str) -> dict:
    """解码并校验签名、有效期与令牌类型，返回 payload。

    无效 / 过期 / 类型不匹配均抛 jwt.PyJWTError。
    """
    payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"expected {expected_type} token")
    return payload

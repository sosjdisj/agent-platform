"""认证接口的请求/响应模型。"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_]+$")
    email: str = Field(max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    """登录/刷新响应：refresh token 走 HttpOnly Cookie，不进响应体。"""

    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # 支持从 ORM 对象直接校验

    id: int
    username: str
    email: str
    created_at: datetime
    permissions: set[str] = set()  # 由 RBAC 四表解析，/me 时填充

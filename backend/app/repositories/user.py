"""用户 Repository。"""
from sqlalchemy import select

from app.models.user import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_username(self, username: str) -> User | None:
        """按用户名查询。"""
        stmt = select(self.model).where(self.model.username == username)
        return await self.session.scalar(stmt)

    async def get_by_email(self, email: str) -> User | None:
        """按邮箱查询。"""
        stmt = select(self.model).where(self.model.email == email)
        return await self.session.scalar(stmt)

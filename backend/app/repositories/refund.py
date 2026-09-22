"""退款记录 Repository。"""
from app.models.refund import Refund

from app.repositories.base import BaseRepository


class RefundRepository(BaseRepository[Refund]):
    model = Refund

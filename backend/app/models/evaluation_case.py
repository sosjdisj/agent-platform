"""评估用例表模型（Evaluation 体系）。

存储评估的输入与期望输出，用于对 Agent 回答质量做回归评估。
"""
from sqlalchemy import BigInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import StatusMixin, TimestampMixin


class EvaluationCase(Base, TimestampMixin, StatusMixin):
    __tablename__ = "evaluation_cases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    expected_answer: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict | None] = mapped_column(JSONB)

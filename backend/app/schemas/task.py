"""任务接口的请求/响应模型（21.1 / 24.1）。"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.agents.report import AnalysisReport


class TaskCreateRequest(BaseModel):
    """创建任务入参：title 上限与 agent_tasks.title 列宽（200）一致。"""

    title: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1)


class TaskResponse(BaseModel):
    """任务详情响应：字段与 agent_tasks 表一一对应（status 存小写枚举值）。

    report 为结构化九部分报告（24.1 报告页渲染与证据溯源的数据源）；result 为
    人类可读文本（列表预览 / 评估解析）；两者同源于任务完成时的同一 AnalysisReport，
    未完成任务 / 历史行 report 为 NULL。
    """

    model_config = ConfigDict(from_attributes=True)  # 支持从 ORM 对象直接校验

    id: int
    user_id: int
    title: str
    query: str
    result: str | None
    report: AnalysisReport | None = None
    error: str | None
    status: str
    finished_at: datetime | None
    created_at: datetime

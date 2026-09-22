"""评估专用 Agent 配置（Prompt 22.3）：评估运行与生产运行的行为参数隔离。

评估复现性优先：temperature 固定 0（贪心解码，同输入多次运行行为一致），
编排轮次上限独立取值；LLM 接入信息（BASE_URL / API_KEY / 模型名）仍来自全局
settings——隔离的是"行为参数"，不是"接入配置"。
生产评估组合根经 build_evaluation_supervisor 接线（与 build_task_executor 对称）。
"""
from pydantic import BaseModel, ConfigDict

from app.agents.supervisor import Supervisor
from app.core.llm import LLMService, create_llm_service
from app.core.redis import RedisService


class EvaluationAgentConfig(BaseModel):
    """评估运行的 Agent 行为参数（代码内固定缺省，独立于生产 .env 配置漂移）。"""

    model_config = ConfigDict(frozen=True)

    temperature: float = 0.0  # 贪心解码：评估结果可复现
    supervisor_max_rounds: int = 4  # 编排轮次上限（评估独立取值，不随生产配置漂移）


def create_evaluation_llm(config: EvaluationAgentConfig | None = None) -> LLMService:
    """评估专用 LLMService：复用 create_llm_service 的接入配置，注入评估行为参数
    （default_temperature 使所有未显式传 temperature 的 LLM 调用固定为评估值）。"""
    evaluation_config = config or EvaluationAgentConfig()
    return create_llm_service(default_temperature=evaluation_config.temperature)


def build_evaluation_supervisor(
    *, redis: RedisService | None = None, config: EvaluationAgentConfig | None = None
) -> Supervisor:
    """生产评估组合根：评估 LLM + 真实专业 Agent 栈 + RBAC / 轨迹接线
    （与 build_task_executor 对称；Agent 的 RAG 栈 / 数据库连接延迟加载，构造不触网）。"""
    from app.db.session import SessionLocal

    evaluation_config = config or EvaluationAgentConfig()
    return Supervisor(
        create_evaluation_llm(evaluation_config),
        max_rounds=evaluation_config.supervisor_max_rounds,
        auth_session_maker=SessionLocal,
        trace_session_maker=SessionLocal,
        trace_redis=redis,
    )

"""Agent 调试接口：不经业务编排直接运行单个 Agent，供开发联调（仅 debug 模式开放）。

- 知识问答：POST /api/debug/agents/knowledge → AgentResult（含 status / error_code / sources）；
- 数据查询：POST /api/debug/agents/data → AgentResult；
- 业务操作：POST /api/debug/agents/business → AgentResult；
- 依赖经 get_knowledge_agent / get_data_agent / get_business_agent 注入，
  测试用 dependency_overrides 替换为替身 Agent。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agents.business import BusinessAgent
from app.agents.data import DataAgent
from app.agents.knowledge import KnowledgeAgent
from app.agents.state import AgentResult
from app.core.config import get_settings
from app.core.llm import get_llm_service
from app.core.redis import RedisService, get_redis_service
from app.db.session import SessionLocal

router = APIRouter(prefix="/api/debug", tags=["debug"])


class AgentDebugRequest(BaseModel):
    """单 Agent 调试请求：query 必填；task_id / user_id 为调试语义（不关联业务任务）。"""

    query: str = Field(min_length=1, description="用户问题")
    task_id: int = Field(default=0, ge=0, description="调试用任务 ID（checkpoint thread_id）")
    user_id: int = Field(default=0, ge=0, description="调试用用户 ID")


def get_knowledge_agent(redis: RedisService = Depends(get_redis_service)) -> KnowledgeAgent:
    """构造 KnowledgeAgent（真实 LLM + RAG 栈 + 任务事件发布）；测试经 dependency_overrides 替换。"""
    return KnowledgeAgent(get_llm_service(), trace_session_maker=SessionLocal, trace_redis=redis)


@router.post("/agents/knowledge")
async def debug_knowledge_agent(
    body: AgentDebugRequest,
    agent: KnowledgeAgent = Depends(get_knowledge_agent),
) -> AgentResult:
    """运行 KnowledgeAgent 回答一次知识问答，返回带结果语义的 AgentResult。"""
    if not get_settings().debug:
        raise HTTPException(status_code=404, detail="调试接口仅在 debug 模式开放")
    return await agent.answer(task_id=body.task_id, user_id=body.user_id, query=body.query)


def get_data_agent(redis: RedisService = Depends(get_redis_service)) -> DataAgent:
    """构造 DataAgent（真实 LLM + 数据库 + 任务事件发布）；测试经 dependency_overrides 替换。"""
    return DataAgent(get_llm_service(), trace_session_maker=SessionLocal, trace_redis=redis)


@router.post("/agents/data")
async def debug_data_agent(
    body: AgentDebugRequest,
    agent: DataAgent = Depends(get_data_agent),
) -> AgentResult:
    """运行 DataAgent 回答一次数据查询，返回带数据来源的 AgentResult。"""
    if not get_settings().debug:
        raise HTTPException(status_code=404, detail="调试接口仅在 debug 模式开放")
    return await agent.answer(task_id=body.task_id, user_id=body.user_id, query=body.query)


def get_business_agent(redis: RedisService = Depends(get_redis_service)) -> BusinessAgent:
    """构造 BusinessAgent（真实 LLM + 数据库 + 任务事件发布）；测试经 dependency_overrides 替换。"""
    return BusinessAgent(get_llm_service(), trace_session_maker=SessionLocal, trace_redis=redis)


@router.post("/agents/business")
async def debug_business_agent(
    body: AgentDebugRequest,
    agent: BusinessAgent = Depends(get_business_agent),
) -> AgentResult:
    """运行 BusinessAgent 处理一次业务请求，返回带工具来源的 AgentResult。"""
    if not get_settings().debug:
        raise HTTPException(status_code=404, detail="调试接口仅在 debug 模式开放")
    return await agent.answer(task_id=body.task_id, user_id=body.user_id, query=body.query)

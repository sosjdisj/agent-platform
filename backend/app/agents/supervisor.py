"""Supervisor：Multi-Agent 编排（15.1 路由决策 + 15.2 单 Agent 串行调度 + 15.3 失败重试 / 降级）。

Main Agent 编排图：START → route（路由决策节点）→ 条件边 → dispatch（调度节点）→ 回 route；
is_final_ready=true 或无可派 Agent 时 → report（ReportAgent，唯一出口）→ END。

- route：单次 LLM 结构化输出（chat_structured 强制 JSON Schema）产出 RouteDecision
  （next_agents / reason / task_context / is_final_ready / need_knowledge / query）；决策时可见
  已执行 Agent 结论与失败原因（回环依据），轮次守卫（supervisor_max_rounds）内直接截断，
  不再询问 LLM；知识需求显式判断（need_knowledge + 检索查询 query）随决策结构化记入
  state.decisions，不记思维链（Agentic RAG 16.1）；
- dispatch：仅执行本轮决策的第一个 Agent（next_agents[0]，单 Agent 串行调度），
  复用各专业 Agent 的 answer()（内部自带 LLM↔Tool 图与结果组装），产出写回
  state.agent_results；Agent 异常转为 failed 结果写回（不上抛），重试 / 降级由下一轮路由决策；
  knowledge 上次结果 NO_RELEVANT_DOCUMENT 时重派视为重搜：用决策改写的 query 执行，
  次数记入 state.retries 并经路由提示可见（上限 MAX_RESEARCH_TIMES=2，Agentic RAG 16.2）；
- report：ReportAgent 汇总 agent_results 生成最终报告（降级时仅基于已有结果，failed 结果如实呈现）；
- checkpointer 可选注入（编译期传入）：thread_id = task_id（agent_thread_config 单一来源），
  run 逐步持久化、resume 以 None 输入续跑——被中断的节点整体重执行；专业 Agent 内部图
  不注入 checkpointer（同 task 多 Agent 复用 thread_id 会串线），故恢复粒度为 Supervisor 节点级；
- 专业 Agent 实例可注入（测试替身），缺省构造真实 Agent（RAG 栈 / 数据库连接延迟加载）。

不做：并行 fan-out（BusinessAgent 并行调度，留待 Evaluation 稳定后）。
"""
from __future__ import annotations

import operator
from collections.abc import Sequence
from typing import Annotated, Any, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.business import BUSINESS_AGENT_NAME, BusinessAgent, BusinessAgentWaiting
from app.agents.checkpoint import agent_thread_config
from app.agents.data import DATA_AGENT_NAME, DataAgent
from app.agents.knowledge import KNOWLEDGE_AGENT_NAME, KnowledgeAgent
from app.agents.report import AnalysisReport, ReportAgent
from app.agents.sources import result_blocks
from app.agents.state import AgentResult, AgentStatus
from app.core.config import get_settings
from app.core.llm import LLMService
from app.core.redis import RedisService
from app.models.agent import AgentTraceEventType
from app.rag.retriever import NO_RELEVANT_DOCUMENT
from app.services.trace_service import TraceService

# 可被路由调度的专业 Agent（单一来源 = 各 Agent 模块的名称常量）；
# report 为汇总节点，作为编排唯一出口，不参与路由
ROUTABLE_AGENTS = (KNOWLEDGE_AGENT_NAME, DATA_AGENT_NAME, BUSINESS_AGENT_NAME)

# knowledge 因 NO_RELEVANT_DOCUMENT 的重搜上限（Agentic RAG 16.2）：决策改写 query 后重搜
MAX_RESEARCH_TIMES = 2


class RouteDecision(BaseModel):
    """路由决策：Supervisor 对任务的结构化裁决（LLM 输出经 Pydantic 校验）。"""

    next_agents: list[str] = []  # 待调度专业 Agent（ROUTABLE_AGENTS 子集，空 = 本轮不调度）
    reason: str  # 一句话任务级说明（为什么派这些 Agent），禁止长篇推理
    task_context: dict[str, str] = {}  # 提炼的关键实体（客户名称 / 时间范围等，字符串键值）
    is_final_ready: bool = False  # 无需再调度专业 Agent 即可产出最终结果（如寒暄、信息已足）
    need_knowledge: bool = False  # 任务是否需要知识检索（Agentic RAG：显式判断 + 结构化记录）
    query: str = ""  # need_knowledge=true 时提炼的知识检索查询（一句话检索词，非任务原文复述）

    @field_validator("next_agents")
    @classmethod
    def _check_known_agents(cls, value: list[str]) -> list[str]:
        """路由目标必须是已知专业 Agent：LLM 幻觉出的名字在校验层即被拒绝。"""
        unknown = [name for name in value if name not in ROUTABLE_AGENTS]
        if unknown:
            raise ValueError(f"未知 Agent：{unknown}，可选：{list(ROUTABLE_AGENTS)}")
        return value

    @model_validator(mode="after")
    def _check_query_present(self) -> "RouteDecision":
        """need_knowledge=true 必须给出检索查询：没有查询的知识需求无法执行。"""
        if self.need_knowledge and not self.query.strip():
            raise ValueError("need_knowledge=true 时必须给出知识检索查询 query")
        return self


# 路由决策系统提示：Agent 分工 + 选用规则 + 输出约束（Agent 名称取自名称常量，单一来源）
SUPERVISOR_SYSTEM_PROMPT = (
    "你是 Multi-Agent 任务调度主管：判断完成用户任务需要哪些专业 Agent，"
    "并提炼调度所需的关键实体。可选 Agent 及分工：\n"
    f"- {KNOWLEDGE_AGENT_NAME}：企业知识、制度、政策类问题（退款政策、报销流程等）；\n"
    f"- {DATA_AGENT_NAME}：客户销售、订单、产品等数据查询与统计对比；\n"
    f"- {BUSINESS_AGENT_NAME}：实际执行业务操作——创建工单、更新客户资料、订单退款等"
    "写操作（退款等高风险操作执行前会进入人工审批）；\n"
    "规则：\n"
    "- 任务要求实际执行操作（退款、创建工单、更新客户资料）时必须派 business："
    "knowledge 与 data 只读不写，不能替代业务执行；\n"
    "- 客户全景 / CRM 概览（主数据 + 订单 + 工单综合视图）类查询派 business；"
    "仅要数据明细或统计对比时才派 data；\n"
    "- 归因分析类任务（销售下滑、客诉原因、交付异常等）默认需要制度规范佐证，"
    "need_knowledge=true；无法用业务数据回答的通用问题（政策、行业、竞品咨询）"
    "也派 knowledge 检索，未命中时如实汇总；\n"
    "- 每轮只执行 next_agents 的第一个 Agent（按优先级排序），其余需求留待下一轮决策；\n"
    "- 寒暄等无需专业 Agent 的任务，或已执行 Agent 的结论已足以完成任务时："
    "next_agents 留空，is_final_ready=true；\n"
    "- 已执行结论状态为 failed 的 Agent 可重试派一次；重试仍失败或判断无法恢复时："
    "next_agents 留空，is_final_ready=true，降级为仅基于已有结果汇总；\n"
    f"- knowledge 结论标记 NO_RELEVANT_DOCUMENT（未检索到相关知识）时：可改写检索查询"
    f"（query 字段）重派 knowledge 重搜，全程最多 {MAX_RESEARCH_TIMES} 次"
    "（已用次数见任务信息）；重搜仍无答案或达上限时放弃重搜，is_final_ready=true 如实汇总；\n"
    "- task_context 只提炼任务级关键实体（如客户名称、时间范围、产品）；\n"
    "- 先判断任务是否需要知识检索：need_knowledge=任务是否涉及企业知识、制度、政策"
    "（含混合任务的数据+知识复合需求）；\n"
    "- need_knowledge=true 时必须给出 query：提炼的一句话知识检索查询（非任务原文复述）；\n"
    "- reason 只用一句话说明任务级理由，禁止长篇推理。"
)


class SupervisorState(BaseModel):
    """Supervisor 编排状态：路由决策与专业 Agent 产出按轮追加，报告整体覆盖。"""

    task_id: int  # agent_tasks.id
    user_id: int  # 发起用户（users.id）
    query: str  # 用户任务（原样传给各专业 Agent 与报告）
    decisions: Annotated[list[RouteDecision], operator.add] = []  # 历次路由决策（追加）
    agent_results: Annotated[list[AgentResult], operator.add] = []  # 已执行 Agent 产出（追加）
    retries: dict[str, int] = {}  # knowledge 等因 NO_RELEVANT_DOCUMENT 已重搜次数（agent 名 → 次数）
    report: AnalysisReport | None = None  # 最终报告（唯一出口产出）


class RoutableAgent(Protocol):
    """可被 Supervisor 调度的专业 Agent 协议（与各专业 Agent 的 answer 签名对齐）。"""

    async def answer(self, task_id: int, user_id: int, query: str) -> AgentResult: ...


class SupervisorError(RuntimeError):
    """Supervisor 编排失败：编排结束未产出报告等异常终止。"""


class SupervisorWaiting(RuntimeError):
    """编排因 HIGH 工具等待审批而中断（23.3）：checkpoint 已持久化，决定后经 resume 续跑。"""


def _route_user_prompt(query: str, results: Sequence[AgentResult], retries: dict[str, int]) -> str:
    """路由输入：任务问题 + 已执行 Agent 结论（回环决策依据，格式与报告共用 result_blocks）
    + 重搜用量（有重搜时追加，供 LLM 判断是否仍可重搜）。"""
    note = ""
    if retries:
        used = "；".join(f"{name} 已重搜 {n}/{MAX_RESEARCH_TIMES} 次" for name, n in retries.items())
        note = f"\n\n重搜用量：{used}"
    return f"任务：{query}\n\n已执行 Agent 结论：\n{result_blocks(results) or '（无）'}{note}"


class Supervisor:
    """Multi-Agent 编排器：路由 → 单 Agent 串行调度 → 循环 → 报告（唯一出口）。"""

    def __init__(
        self,
        llm: LLMService,
        *,
        knowledge: RoutableAgent | None = None,
        data: RoutableAgent | None = None,
        business: RoutableAgent | None = None,
        report_agent: ReportAgent | None = None,
        max_rounds: int | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        auth_session_maker: async_sessionmaker[AsyncSession] | None = None,
        approval_session_maker: async_sessionmaker[AsyncSession] | None = None,
        trace_session_maker: async_sessionmaker[AsyncSession] | None = None,
        trace_redis: RedisService | None = None,
    ) -> None:
        self._llm = llm
        self._trace_session_maker = trace_session_maker
        self._trace_redis = trace_redis
        self._agents: dict[str, RoutableAgent] = {
            KNOWLEDGE_AGENT_NAME: knowledge
            if knowledge is not None
            else KnowledgeAgent(
                llm,
                auth_session_maker=auth_session_maker,
                trace_session_maker=trace_session_maker,
                trace_redis=trace_redis,
            ),
            DATA_AGENT_NAME: data
            if data is not None
            else DataAgent(
                llm,
                auth_session_maker=auth_session_maker,
                trace_session_maker=trace_session_maker,
                trace_redis=trace_redis,
            ),
            BUSINESS_AGENT_NAME: business
            if business is not None
            else BusinessAgent(
                llm,
                auth_session_maker=auth_session_maker,
                approval_session_maker=approval_session_maker,
                trace_session_maker=trace_session_maker,
                trace_redis=trace_redis,
            ),
        }
        self._report = report_agent if report_agent is not None else ReportAgent(llm)
        self._max_rounds = (
            max_rounds if max_rounds is not None else get_settings().supervisor_max_rounds
        )
        self._graph = self._build_graph(checkpointer)

    async def route(
        self,
        query: str,
        results: Sequence[AgentResult] = (),
        retries: dict[str, int] | None = None,
    ) -> RouteDecision:
        """路由一次任务：structured output 强制产出 RouteDecision（校验失败透传 pydantic 错误）。"""
        return await self._llm.chat_structured(
            [
                {"role": "system", "content": SUPERVISOR_SYSTEM_PROMPT},
                {"role": "user", "content": _route_user_prompt(query, results, retries or {})},
            ],
            RouteDecision,
        )

    async def run(self, task_id: int, user_id: int, query: str) -> AnalysisReport:
        """执行完整编排：路由 → 调度 → 循环 → 报告（Agent / LLM 异常按节点语义处理）。"""
        return await self._invoke(
            SupervisorState(task_id=task_id, user_id=user_id, query=query), task_id
        )

    async def _trace_dispatch(self, task_id: int, agent_name: str, *, round: int) -> None:
        """调度埋点：agent_selected 携带被调度的真实 Agent 名（路由可观测性单点：
        编排路径的调度者是自己；单 Agent 调试路径由 AgentRunner 以基名写入）。
        未接线轨迹时静默，评估 Runner 与 SSE 均以此核验实际路由。"""
        if self._trace_session_maker is None:
            return
        async with self._trace_session_maker() as session:
            await TraceService(session, redis=self._trace_redis).append(
                task_id=task_id,
                event_type=AgentTraceEventType.AGENT_SELECTED,
                agent=agent_name,
                round=round,
            )

    async def resume(self, task_id: int) -> AnalysisReport:
        """恢复被中断的编排：同 thread_id（= task_id）以 None 输入从最后 checkpoint 续跑
        （被中断的节点整体重执行）。"""
        return await self._invoke(None, task_id)

    async def _invoke(self, inp: SupervisorState | None, task_id: int) -> AnalysisReport:
        """统一执行入口：LangGraph 返回 dict，校验回状态并断言报告已产出。"""
        final = await self._graph.ainvoke(inp, config=agent_thread_config(task_id))
        state = SupervisorState.model_validate(final)
        if state.report is None:
            raise SupervisorError("编排结束但未产出报告")
        return state.report

    def _build_graph(self, checkpointer: BaseCheckpointSaver | None):
        """构建编排图：route ⇄ dispatch 循环，report 为唯一出口；checkpointer 可选注入。"""
        max_rounds = self._max_rounds

        async def route_node(state: SupervisorState) -> dict[str, Any]:
            """路由节点：轮次守卫内询问 LLM；已达调度上限则强制终态（不再消耗 LLM 调用）。"""
            if len(state.agent_results) >= max_rounds:
                return {
                    "decisions": [
                        RouteDecision(
                            reason=f"已调度 {max_rounds} 个专业 Agent，达上限，直接汇总。",
                            is_final_ready=True,
                        )
                    ]
                }
            return {"decisions": [await self.route(state.query, state.agent_results, state.retries)]}

        async def dispatch_node(state: SupervisorState) -> dict[str, Any]:
            """调度节点：仅执行本轮决策的第一个 Agent；异常转为 failed 结果写回
            （不上抛，路由可见失败原因后决策重试或降级），产出写回 agent_results 后回路由。

            knowledge 上次结果为 NO_RELEVANT_DOCUMENT 时本次派发视为一次重搜（Agentic RAG 16.2）：
            以决策改写的检索查询执行并计入 retries（次数经路由提示可见，上限由系统提示与
            max_rounds 共同约束，与 15.3 失败重试的提示规则同构）；其余调度用任务原文。"""
            decision = state.decisions[-1]
            name = decision.next_agents[0]
            await self._trace_dispatch(state.task_id, name, round=len(state.agent_results) + 1)
            last = next((r for r in reversed(state.agent_results) if r.agent == name), None)
            re_search = (
                name == KNOWLEDGE_AGENT_NAME
                and last is not None
                and last.error_code == NO_RELEVANT_DOCUMENT
            )
            query = decision.query if re_search and decision.query else state.query
            update: dict[str, Any] = {}
            if re_search:
                update["retries"] = {**state.retries, name: state.retries.get(name, 0) + 1}
            try:
                result = await self._agents[name].answer(state.task_id, state.user_id, query)
            except BusinessAgentWaiting as exc:
                # HIGH 工具等待审批：穿透编排上抛（TaskExecutor 据此转 waiting_approval），
                # 不落为 failed 结果——决定后 supervisor.resume 从 checkpoint 重执行本节点，
                # Agent 图内审批门按最新决定幂等放行（approved → 执行 / rejected → fallback）。
                raise SupervisorWaiting(str(exc)) from exc
            except Exception as exc:
                result = AgentResult(
                    agent=name,
                    content=f"执行失败：{exc}",
                    status=AgentStatus.FAILED,
                )
            return {"agent_results": [result], **update}

        async def report_node(state: SupervisorState) -> dict[str, Any]:
            """汇总节点：ReportAgent 生成最终报告（唯一出口）。"""
            return {"report": await self._report.generate(state.query, state.agent_results)}

        def after_route(state: SupervisorState) -> str:
            """路由后分流：有 Agent 可派且未终态 → 调度；否则（终态 / 空清单）→ 报告。"""
            decision = state.decisions[-1]
            return "dispatch" if decision.next_agents and not decision.is_final_ready else "report"

        builder = StateGraph(SupervisorState)
        builder.add_node("route", route_node)
        builder.add_node("dispatch", dispatch_node)
        builder.add_node("report", report_node)
        builder.add_edge(START, "route")
        builder.add_conditional_edges("route", after_route, {"dispatch": "dispatch", "report": "report"})
        builder.add_edge("dispatch", "route")
        builder.add_edge("report", END)
        return builder.compile(checkpointer=checkpointer)

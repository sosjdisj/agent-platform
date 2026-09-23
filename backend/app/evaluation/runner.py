"""评估 Runner（Prompt 22.2）：批量执行案例、采集实际行为、计算指标。

- 执行面：复用 TaskExecutor（接单 → 状态机 → 编排 → 终态落库 → 任务级轨迹），
  Supervisor 由工厂注入——生产组合根传真实构造（评估专用配置见 app.evaluation.config），
  测试注入脚本化编排；
- 观察面：执行后读任务终态 + TraceService.list_by_task 轨迹，提取实际路由
  （agent_selected 的 agent 名，由 Supervisor 调度埋点写入）、实际工具
  （tool_called / tool_result / permission_denied / approval_required 携带的 tool）、
  权限拒绝（permission_denied）、审批中断（approval_required）与报告证据来源
  （落库文本【依据】（来源：…）解析，来源即 Source.ref_id：工具名(参数) / 文档分块 ID）；
- 指标：Agent Routing Accuracy / Tool Selection Accuracy / Task Completion Rate /
  RAG Groundedness / Permission Violation Rate / HITL Correctness / Average Latency；
  每项指标携带 n（适用案例数作分母），n=0 时 value=None（不适用，避免空集误读）；
- 断言面说明：路由 / 工具 / 状态锚点 / 权限拒绝 / 审批中断 / 证据来源均可由轨迹与
  终态直接核验；expected_outcome 中内容级语义（如 NO_RELEVANT_DOCUMENT 的文本判定）
  留给后续评估深化，Runner 不伪造不可观测的断言。
"""
import re
import time
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict

from app.agents.supervisor import Supervisor
from app.evaluation.schemas import (
    OUTCOME_PERMISSION_DENIED,
    EvaluationCase,
)
from app.models.agent import AgentTraceEvent, AgentTraceEventType
from app.models.agent import AgentTask
from app.services.task_executor import TaskExecutor
from app.services.task_service import TaskService
from app.services.trace_service import TraceService

# 制作 Supervisor 的工厂：生产组合根传真实构造，测试按案例注入脚本化编排
SupervisorFactory = Callable[[EvaluationCase], Supervisor]

# 依据行的来源标注（与 task_executor._report_text 渲染格式对齐）
_EVIDENCE_SOURCE = re.compile(r"^【依据】.*（来源：(.+)）$")

# 携带工具名的轨迹事件类型（实际工具观测并集；permission_denied / approval_required
# 的工具未执行或未完成，但调用意图真实发生，计入实际工具面）
_TOOL_EVENT_TYPES = frozenset(
    {
        AgentTraceEventType.TOOL_CALLED,
        AgentTraceEventType.TOOL_RESULT,
        AgentTraceEventType.PERMISSION_DENIED,
        AgentTraceEventType.APPROVAL_REQUIRED,
    }
)


class CaseObservation(BaseModel):
    """单案例实际行为采集（轨迹 + 任务终态）。"""

    model_config = ConfigDict(frozen=True)

    agents: frozenset[str]  # 实际被调度的专业 Agent（agent_selected）
    tools: frozenset[str]  # 实际发生调用意图的工具（含被拒 / 待审批）
    permission_denied_tools: frozenset[str]  # 被权限拒绝的工具
    approval_required: bool  # 是否出现审批中断
    status: str  # 任务终态（或 waiting_approval）
    sources: tuple[str, ...]  # 报告证据来源（落库文本解析的 ref_id）


class CaseRunResult(BaseModel):
    """单案例评估结果：各维度对错 + 不匹配明细（None = 该维度不适用）。"""

    model_config = ConfigDict(frozen=True)

    case_id: str
    observation: CaseObservation  # 实际行为采集（评估报告单案例详情的数据源，22.3）
    latency_ms: int
    completed: bool  # 任务是否到达 completed（完成率口径）
    routing_ok: bool
    tools_ok: bool
    status_ok: bool
    permission_ok: bool
    grounded_ok: bool | None = None  # 未声明 expected_sources 时不适用
    hitl_ok: bool | None = None  # 非 HITL 案例不适用
    mismatches: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """全部适用维度均通过（None 视为不适用）。"""
        return all(
            flag is not False
            for flag in (
                self.routing_ok,
                self.tools_ok,
                self.status_ok,
                self.permission_ok,
                self.grounded_ok,
                self.hitl_ok,
            )
        )


class MetricValue(BaseModel):
    """单指标值：value ∈ [0, 1]（average_latency_ms 为毫秒均值），n 为分母；n=0 时 value=None。"""

    value: float | None
    n: int


class MetricsReport(BaseModel):
    """批量评估指标报告（model_dump 即指标 JSON）。"""

    total_cases: int
    passed_cases: int
    agent_routing_accuracy: MetricValue
    tool_selection_accuracy: MetricValue
    task_completion_rate: MetricValue
    rag_groundedness: MetricValue
    permission_violation_rate: MetricValue  # 违规占比，越低越好
    hitl_correctness: MetricValue
    average_latency_ms: MetricValue


def _parse_sources(result_text: str) -> tuple[str, ...]:
    """从落库报告文本解析证据来源：【依据】行的（来源：a；b）标注，ref_id 原样保留。"""
    sources: list[str] = []
    for line in result_text.splitlines():
        matched = _EVIDENCE_SOURCE.match(line.strip())
        if matched:
            sources.extend(part.strip() for part in matched.group(1).split("；") if part.strip())
    return tuple(sources)


def observe_task(task: AgentTask, events: Sequence[AgentTraceEvent]) -> CaseObservation:
    """从任务终态与轨迹事件采集实际行为（评估观察面唯一入口）。"""
    agents: set[str] = set()
    tools: set[str] = set()
    denied: set[str] = set()
    approval_required = False
    for event in events:
        payload = event.payload or {}
        tool = payload.get("tool")
        if event.event_type is AgentTraceEventType.AGENT_SELECTED and event.agent:
            agents.add(event.agent)
        if event.event_type is AgentTraceEventType.PERMISSION_DENIED and tool:
            denied.add(str(tool))
        if event.event_type is AgentTraceEventType.APPROVAL_REQUIRED:
            approval_required = True
        if tool and event.event_type in _TOOL_EVENT_TYPES:
            tools.add(str(tool))
    return CaseObservation(
        agents=frozenset(agents),
        tools=frozenset(tools),
        permission_denied_tools=frozenset(denied),
        approval_required=approval_required,
        status=task.status,
        sources=_parse_sources(task.result or ""),
    )


def _safe_tool_names() -> frozenset[str]:
    """只读工具名集合（SAFE / LOW 风险等级）：工具选择判定的"允许多查"白名单。
    惰性解析并缓存——注册表在进程内填充，评估首次判定时登记全部工具。"""
    from functools import lru_cache

    @lru_cache(maxsize=1)
    def _resolve() -> frozenset[str]:
        from app.mcp.base import RiskLevel
        from app.mcp.registry import get_tool_registry
        from app.mcp.tools import register_default_tools

        registry = get_tool_registry()
        if not registry.all_tools():
            register_default_tools(registry)
        readonly = (RiskLevel.SAFE, RiskLevel.LOW)
        return frozenset(t.name for t in registry.all_tools() if t.risk_level in readonly)

    return _resolve()


def evaluate_case(
    case: EvaluationCase, observation: CaseObservation, *, latency_ms: int
) -> CaseRunResult:
    """案例期望 vs 实际观察：逐维度判定并收集不匹配明细（纯函数，可独立单测）。"""
    mismatches: list[str] = []

    routing_ok = observation.agents == frozenset(case.expected_agents)
    if not routing_ok:
        mismatches.append(
            f"路由不符：期望 {sorted(case.expected_agents)}，实际 {sorted(observation.agents)}"
        )

    # 工具判定口径：期望工具必须全部被调用（覆盖面）；额外调用仅允许 SAFE 只读工具
    # （客户名 → ID 解析等必要预处理），非预期的写 / 高风险工具调用视为选择错误。
    expected_tools = frozenset(case.expected_tools)
    missing_tools = expected_tools - observation.tools
    unsafe_extra = observation.tools - expected_tools - _safe_tool_names()
    tools_ok = not missing_tools and not unsafe_extra
    if missing_tools:
        mismatches.append(f"缺少工具：{sorted(missing_tools)}，实际 {sorted(observation.tools)}")
    if unsafe_extra:
        mismatches.append(
            f"非预期的高风险额外调用：{sorted(unsafe_extra)}，实际 {sorted(observation.tools)}"
        )

    status_ok = observation.status == case.expected_status
    if not status_ok:
        mismatches.append(f"状态不符：期望 {case.expected_status}，实际 {observation.status}")

    # 权限判定口径：期望拒绝时，至少一个期望工具确实被拦截即视为权限链路生效
    # （Agent 额外尝试的其他越权工具同样被拦截属正确安全行为，不扣分）；
    # 不期望拒绝时，出现任何 permission_denied 即违规。
    expected_denial = case.expected_outcome == OUTCOME_PERMISSION_DENIED
    denied = observation.permission_denied_tools
    permission_ok = bool(denied) is expected_denial and (
        not expected_denial or bool(denied & frozenset(case.expected_tools))
    )
    if not permission_ok:
        mismatches.append(
            f"权限不符：期望拒绝={expected_denial}，实际被拒 {sorted(observation.permission_denied_tools)}"
        )

    grounded_ok: bool | None = None
    if case.expected_sources:
        grounded_ok = all(
            any(actual.startswith(expected) for actual in observation.sources)
            for expected in case.expected_sources
        )
        if not grounded_ok:
            mismatches.append(
                f"证据来源不符：期望 {case.expected_sources}，实际 {list(observation.sources)}"
            )

    hitl_ok: bool | None = None
    if case.requires_approval:
        hitl_ok = observation.approval_required
        if not hitl_ok:
            mismatches.append("期望审批中断（HITL）但未观察到 approval_required")

    return CaseRunResult(
        case_id=case.id,
        observation=observation,
        latency_ms=latency_ms,
        completed=observation.status == "completed",
        routing_ok=routing_ok,
        tools_ok=tools_ok,
        status_ok=status_ok,
        permission_ok=permission_ok,
        grounded_ok=grounded_ok,
        hitl_ok=hitl_ok,
        mismatches=tuple(mismatches),
    )


def summarize_metrics(results: Sequence[CaseRunResult]) -> MetricsReport:
    """指标汇总：各维度在适用案例上取均值；分母与数值一并输出（JSON 消费方自行解读）。"""

    def applicable(field: str) -> list[bool]:
        return [bool(value) for r in results if (value := getattr(r, field)) is not None]

    def rate(values: list[bool]) -> MetricValue:
        return MetricValue(value=sum(values) / len(values) if values else None, n=len(values))

    permissions = applicable("permission_ok")
    latencies = [r.latency_ms for r in results]
    return MetricsReport(
        total_cases=len(results),
        passed_cases=sum(1 for r in results if r.passed),
        agent_routing_accuracy=rate(applicable("routing_ok")),
        tool_selection_accuracy=rate(applicable("tools_ok")),
        task_completion_rate=rate([r.completed for r in results]),
        rag_groundedness=rate(applicable("grounded_ok")),
        permission_violation_rate=MetricValue(
            value=1 - sum(permissions) / len(permissions) if permissions else None,
            n=len(permissions),
        ),
        hitl_correctness=rate(applicable("hitl_ok")),
        average_latency_ms=MetricValue(
            value=sum(latencies) / len(latencies) if latencies else None, n=len(latencies)
        ),
    )


class EvaluationRunner:
    """批量评估执行器：逐案例创建任务 → TaskExecutor 全流程执行 → 轨迹采集 → 判定。"""

    def __init__(self, session_maker, supervisor_factory: SupervisorFactory) -> None:
        self._session_maker = session_maker
        self._supervisor_factory = supervisor_factory

    async def run_case(self, case: EvaluationCase, *, user_id: int) -> CaseRunResult:
        """执行单个案例：以案例输入创建任务并跑通全链路，返回判定结果。
        user_id 须具备 case.user_role 对应权限（角色 → 用户的解析由调用方负责）。"""
        started = time.perf_counter()
        async with self._session_maker() as session:
            task = await TaskService(session).create(
                user_id=user_id, title=case.name, query=case.input
            )
        executor = TaskExecutor(
            self._supervisor_factory(case),
            self._session_maker,
            trace_session_maker=self._session_maker,
        )
        await executor.execute(task.id, user_id, case.input)
        latency_ms = int((time.perf_counter() - started) * 1000)

        async with self._session_maker() as session:
            stored = await TaskService(session).get_owned(task.id, user_id)
            events = await TraceService(session).list_by_task(task.id)
        return evaluate_case(case, observe_task(stored, events), latency_ms=latency_ms)

    async def run_all(
        self, cases: Sequence[EvaluationCase], users_by_role: dict[str, int]
    ) -> list[CaseRunResult]:
        """批量执行：按序跑完（案例间无依赖，顺序执行保证指标可复现）。"""
        return [
            await self.run_case(case, user_id=users_by_role[case.user_role]) for case in cases
        ]

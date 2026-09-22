"""Supervisor 路由与编排测试（15.1 路由决策 + 15.2 单 Agent 串行调度 + 15.3 失败重试 / 降级
+ 15.4 checkpoint / resume + 16.1 知识需求判断 + 16.2 NO_RELEVANT_DOCUMENT 重搜）。

15.1 验收：退款政策 → KnowledgeAgent / 客户A销售额 → DataAgent / reason 一句话任务级。
15.2 验收：纯知识问题（Knowledge→Report）、纯数据问题（Data→Report）两条链路真实跑通
（真实专业 Agent 图 + 真实 ReportAgent + 真实 DB，LLM 脚本化）。
15.3 验收：Agent 失败一次后重试成功、连续失败触发 fallback（仅基于已有结果汇总）、默认 5 轮截断。
15.4 验收：多轮组合（Data→Knowledge→Data→Report）派发序列符合预期、中断后 resume 结果一致
（真实 Postgres checkpointer，Supervisor 节点级恢复）。
16.1 验收：三类问题（纯数据 / 纯知识 / 混合）的 need_knowledge 判断正确，
判断与检索查询以结构化 JSON 记入决策（不记思维链）。
16.2 验收：首次无答案改写 query 重搜后命中、2 次重搜仍无答案放弃并如实降级汇总
（重搜次数记入 state.retries 并经路由提示可见）。
另覆盖：仅调度 next_agents[0]、寒暄直达报告。
"""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.data import DataAgent
from app.agents.knowledge import KnowledgeAgent, KnowledgeAgentError
from app.agents.report import EvidenceDraft, ReportDraft
from app.agents.supervisor import (
    MAX_RESEARCH_TIMES,
    ROUTABLE_AGENTS,
    SUPERVISOR_SYSTEM_PROMPT,
    RouteDecision,
    Supervisor,
)
from app.agents.state import AgentResult, AgentStatus
from app.rag.retriever import NO_RELEVANT_DOCUMENT
from tests.conftest import StructuredLLMStub
from tests.test_agent_graph import final_response, tool_call_response
from tests.test_agent_knowledge import QueryMappedRetriever, StubRetriever, hit_result


class SupervisorLLMStub:
    """Supervisor 级 LLM 替身：chat 走脚本队列（专业 Agent 图用），
    chat_structured 按 response_model 分队列返回（route → RouteDecision，report → ReportDraft）。"""

    def __init__(self, chats: list[Any], decisions: list[RouteDecision], draft: ReportDraft) -> None:
        self._chats = list(chats)
        self._decisions = list(decisions)
        self._drafts = [draft]
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append({"kind": "chat", "messages": list(messages), **kwargs})
        return self._chats.pop(0)

    async def chat_structured(
        self, messages: Any, response_model: type[Any], **kwargs: Any
    ) -> Any:
        self.calls.append(
            {"kind": "structured", "messages": list(messages), "response_model": response_model, **kwargs}
        )
        if response_model is RouteDecision:
            return self._decisions.pop(0)
        if response_model is ReportDraft:
            return self._drafts.pop(0)
        raise AssertionError(f"意外的 response_model：{response_model}")


class FakeAgent:
    """专业 Agent 替身：返回预置 AgentResult 并记录 answer 调用。"""

    def __init__(self, name: str, content: str = "结论。") -> None:
        self.name = name
        self.calls: list[tuple[int, int, str]] = []
        self._content = content

    async def answer(self, task_id: int, user_id: int, query: str) -> AgentResult:
        self.calls.append((task_id, user_id, query))
        return AgentResult(agent=self.name, content=self._content)


class BoomAgent:
    """总是抛错的专业 Agent 替身（验证连续失败触发 fallback）。"""

    def __init__(self, name: str, exc: Exception) -> None:
        self.name = name
        self._exc = exc
        self.calls = 0

    async def answer(self, task_id: int, user_id: int, query: str) -> AgentResult:
        self.calls += 1
        raise self._exc


class FlakyAgent:
    """首次抛错、之后成功的专业 Agent 替身（验证失败重试）。"""

    def __init__(self, name: str, exc: Exception, content: str = "结论。") -> None:
        self.name = name
        self._exc = exc
        self._content = content
        self.calls = 0

    async def answer(self, task_id: int, user_id: int, query: str) -> AgentResult:
        self.calls += 1
        if self.calls == 1:
            raise self._exc
        return AgentResult(agent=self.name, content=self._content)


class Crash(BaseException):
    """模拟进程崩溃：BaseException 不被 dispatch 的 except Exception 捕获，直接终止运行。"""


class CrashAgent:
    """每次 answer 都抛 Crash 的专业 Agent 替身（验证 checkpoint 中断与恢复）。"""

    async def answer(self, task_id: int, user_id: int, query: str) -> AgentResult:
        raise Crash("进程被杀")


class ScriptedAgent:
    """按调用次序返回预置结果的专业 Agent 替身（结果项为 Exception 时抛出，模拟执行失败）。"""

    def __init__(self, name: str, results: list[Any]) -> None:
        self._results = list(results)
        self.calls: list[tuple[int, int, str]] = []

    async def answer(self, task_id: int, user_id: int, query: str) -> AgentResult:
        self.calls.append((task_id, user_id, query))
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def no_answer(agent_name: str = "knowledge") -> AgentResult:
    """无答案结果替身：KnowledgeAgent 检索无相关文档时的如实兜底（partial + 错误码）。"""
    return AgentResult(
        agent=agent_name,
        content="未在知识库中找到相关内容。",
        status=AgentStatus.PARTIAL,
        error_code=NO_RELEVANT_DOCUMENT,
    )


def make_supervisor(llm: Any, **overrides: Any) -> Supervisor:
    """构造 Supervisor：未显式注入的专业 Agent 用替身补齐（避免路由单测构造真实 RAG / DB 组件）。"""
    fakes: dict[str, Any] = {
        "knowledge": FakeAgent("knowledge"),
        "data": FakeAgent("data"),
        "business": FakeAgent("business"),
    }
    return Supervisor(llm, **{**fakes, **overrides})


def route_prompts(llm: Any) -> list[str]:
    """历次路由决策的用户消息（按序），断言回环依据（已执行结论 / 失败原因是否可见）。"""
    return [
        c["messages"][1]["content"]
        for c in llm.calls
        if c["kind"] == "structured" and c["response_model"] is RouteDecision
    ]


async def test_policy_question_routes_to_knowledge() -> None:
    """验收：输入"公司退款政策是什么？"路由到 KnowledgeAgent。"""
    llm = StructuredLLMStub(
        [RouteDecision(next_agents=["knowledge"], reason="退款政策属于企业制度，需检索知识库。")]
    )

    decision = await make_supervisor(llm).route("公司退款政策是什么？")

    assert decision.next_agents == ["knowledge"]
    assert decision.is_final_ready is False  # 默认需继续调度
    # 结构化输出强制：response_model 即 RouteDecision；用户消息 = 任务 + 已执行结论（首轮为空占位）
    (call,) = llm.calls
    assert call["response_model"] is RouteDecision
    system, user = call["messages"]
    assert system["role"] == "system" and SUPERVISOR_SYSTEM_PROMPT in system["content"]
    assert user == {"role": "user", "content": "任务：公司退款政策是什么？\n\n已执行 Agent 结论：\n（无）"}


async def test_sales_question_routes_to_data() -> None:
    """验收：输入"客户A最近销售额"路由到 DataAgent。"""
    llm = StructuredLLMStub(
        [
            RouteDecision(
                next_agents=["data"],
                reason="查询客户销售额需要数据检索。",
                task_context={"customer_name": "客户A", "metric": "销售额"},
            )
        ]
    )

    decision = await make_supervisor(llm).route("客户A最近销售额")

    assert decision.next_agents == ["data"]
    assert decision.task_context == {"customer_name": "客户A", "metric": "销售额"}


async def test_reason_is_one_sentence_task_level() -> None:
    """验收：reason 为一句话任务级说明，不含长篇推理——结构化输出仅含决策字段
    （无思维链字段），代码原样透传不追加内容，一句话约束由系统提示给出。"""
    reason = "查询客户销售额需要数据检索。"
    llm = StructuredLLMStub([RouteDecision(next_agents=["data"], reason=reason)])

    decision = await make_supervisor(llm).route("客户A最近销售额")

    assert decision.reason == reason
    assert "一句话" in SUPERVISOR_SYSTEM_PROMPT and "禁止长篇推理" in SUPERVISOR_SYSTEM_PROMPT
    # 决策 Schema 不含承载思维链的字段
    assert set(RouteDecision.model_fields) == {
        "next_agents",
        "reason",
        "task_context",
        "is_final_ready",
        "need_knowledge",
        "query",
    }


@pytest.mark.parametrize(
    ("question", "decision", "expected_need", "expected_query"),
    [
        pytest.param(
            "客户A最近销售额",
            RouteDecision(next_agents=["data"], reason="销售额查询属于数据需求。"),
            False,
            "",
            id="pure-data",
        ),
        pytest.param(
            "公司退款政策是什么？",
            RouteDecision(
                next_agents=["knowledge"],
                reason="退款政策属于企业制度，需知识检索。",
                need_knowledge=True,
                query="退款政策 退款条件",
            ),
            True,
            "退款政策 退款条件",
            id="pure-knowledge",
        ),
        pytest.param(
            "分析客户A销售额下降原因，并对照退款政策给出建议",
            RouteDecision(
                next_agents=["data", "knowledge"],
                reason="销售数据与退款制度分属两类信息需求。",
                task_context={"customer_name": "客户A"},
                need_knowledge=True,
                query="退款政策 退款条件",
            ),
            True,
            "退款政策 退款条件",
            id="mixed",
        ),
    ],
)
async def test_need_knowledge_judgment_by_question_type(
    question: str, decision: RouteDecision, expected_need: bool, expected_query: str
) -> None:
    """验收：三类问题（纯数据 / 纯知识 / 混合）的 need_knowledge 判断正确——
    判断结果与检索查询是决策的固定结构化字段（JSON 序列化可读），非自由文本思维链。"""
    llm = StructuredLLMStub([decision])

    result = await make_supervisor(llm).route(question)

    assert result.need_knowledge is expected_need
    assert result.query == expected_query
    # 结构化决策记录：决策 JSON 含 need_knowledge / reason / query 字段（记入 state.decisions 的形态）
    recorded = json.loads(result.model_dump_json())
    assert recorded["need_knowledge"] is expected_need and recorded["query"] == expected_query
    assert "reason" in recorded
    # 判断规则由系统提示给出
    assert "need_knowledge" in SUPERVISOR_SYSTEM_PROMPT


async def test_need_knowledge_requires_query() -> None:
    """need_knowledge=true 必须给出检索查询：无查询的知识需求在校验层即被拒绝。"""
    with pytest.raises(ValidationError, match="必须给出知识检索查询"):
        RouteDecision(next_agents=["knowledge"], reason="需知识检索。", need_knowledge=True, query="   ")


async def test_combined_task_routes_multiple_agents() -> None:
    """多 Agent 组合：数据与知识可同时派出（顺序保留，串行只执行第一个）。"""
    llm = StructuredLLMStub(
        [RouteDecision(next_agents=["data", "knowledge"], reason="销售额对比与退款制度分属两类信息需求。")]
    )

    decision = await make_supervisor(llm).route("分析客户A销售额下降原因，并对照退款政策")

    assert decision.next_agents == ["data", "knowledge"]


async def test_chitchat_needs_no_agents() -> None:
    """寒暄：不派任何 Agent，is_final_ready=true（可直接产出最终结果）。"""
    llm = StructuredLLMStub([RouteDecision(reason="寒暄无需专业 Agent。", is_final_ready=True)])

    decision = await make_supervisor(llm).route("你好呀")

    assert decision.next_agents == []
    assert decision.is_final_ready is True
    assert decision.task_context == {}  # 缺省空上下文


def test_unknown_agent_rejected() -> None:
    """路由到未知 Agent（含未开放路由的汇总节点 report）：模型校验层即拒绝。"""
    with pytest.raises(ValidationError, match="未知 Agent"):
        RouteDecision(next_agents=["report"], reason="越权路由汇总节点。")


def test_routable_agents_single_source() -> None:
    """可路由集合来自各专业 Agent 名称常量，不含汇总节点 report。"""
    assert ROUTABLE_AGENTS == ("knowledge", "data", "business")


async def test_knowledge_chain_routes_then_reports() -> None:
    """验收：纯知识问题 Knowledge→Report 链路真实跑通——路由派 KnowledgeAgent
    （真实 LLM↔Tool 图 + 检索替身），回环后终态，ReportAgent 产出报告且证据可溯源。"""
    llm = SupervisorLLMStub(
        chats=[
            tool_call_response("call_1", "search_knowledge", json.dumps({"query": "退款政策"})),
            final_response('{"sufficient": true}'),  # 充分性评审：检索结果足以回答
            final_response("根据知识库：7 天内无理由退款。"),
        ],
        decisions=[
            RouteDecision(next_agents=["knowledge"], reason="退款政策属于企业制度，需知识检索。"),
            RouteDecision(reason="知识结论已足以回答。", is_final_ready=True),
        ],
        draft=ReportDraft(
            related_knowledge="7 天内无理由退款。",
            conclusion="公司退款政策：7 天内无理由退款。",
            evidence=[EvidenceDraft(content="退款政策依据知识库", source_ids=[1])],
        ),
    )
    supervisor = make_supervisor(
        llm,
        knowledge=KnowledgeAgent(llm, retriever=StubRetriever(hit_result(0.92))),  # type: ignore[arg-type]
    )

    report = await supervisor.run(task_id=11, user_id=1, query="公司退款政策是什么？")

    # 链路：route → KnowledgeAgent 真实图（决策 + 评审 + 汇总 3 次 chat）→ route 终态 → report
    assert [c["kind"] for c in llm.calls] == ["structured", "chat", "chat", "chat", "structured", "structured"]
    assert llm.calls[0]["response_model"] is RouteDecision
    assert llm.calls[5]["response_model"] is ReportDraft
    # 回环依据：第二次路由的用户消息携带 knowledge 结论（决策可见已执行结果）
    assert "根据知识库：7 天内无理由退款。" in llm.calls[4]["messages"][1]["content"]
    # 报告证据可溯源：source_ids=[1] 解析回真实链路产出的知识来源 kb-1
    assert report.conclusion == "公司退款政策：7 天内无理由退款。"
    source = report.evidence[0].sources[0]
    assert source.source_type == "knowledge" and source.ref_id == "kb-1"


async def test_data_chain_routes_then_reports(seeded_maker, case_ids) -> None:
    """验收：纯数据问题 Data→Report 链路真实跑通——路由派 DataAgent（真实图 + 真实 DB 查询），
    回环后终态，报告证据可溯源到真实工具来源。"""
    a_id, _, _, _ = case_ids
    llm = SupervisorLLMStub(
        chats=[
            tool_call_response("call_1", "query_sales_data", json.dumps({"customer_id": a_id})),
            final_response("客户A近月销售额环比下滑。"),
        ],
        decisions=[
            RouteDecision(next_agents=["data"], reason="查询客户销售额需要数据检索。"),
            RouteDecision(reason="数据结论已足以回答。", is_final_ready=True),
        ],
        draft=ReportDraft(
            sales_trend="客户A近月销售额环比下滑。",
            conclusion="客户A销售额下滑。",
            evidence=[EvidenceDraft(content="销售额下滑依据销售数据", source_ids=[1])],
        ),
    )
    supervisor = make_supervisor(
        llm, data=DataAgent(llm, session_maker=seeded_maker)  # type: ignore[arg-type]
    )

    report = await supervisor.run(task_id=12, user_id=1, query="客户A最近销售额")

    # 链路：route → DataAgent 真实图（工具决策 + 汇总 2 次 chat，工具真查 DB）→ route 终态 → report
    assert [c["kind"] for c in llm.calls] == ["structured", "chat", "chat", "structured", "structured"]
    assert "客户A近月销售额环比下滑。" in llm.calls[3]["messages"][1]["content"]  # 回环依据
    assert report.sales_trend == "客户A近月销售额环比下滑。"
    # 证据来源 = 真实 DB 查询产出的工具来源（ref_id 规则单一来源见 app/agents/sources.py）
    source = report.evidence[0].sources[0]
    assert source.source_type == "tool"
    assert source.ref_id == f"query_sales_data(customer_id={a_id})"


async def test_dispatch_executes_only_first_agent() -> None:
    """单 Agent 串行调度：仅执行 next_agents[0]，其余留待下一轮决策（不并行调度）。"""
    llm = SupervisorLLMStub(
        chats=[],
        decisions=[
            RouteDecision(next_agents=["knowledge", "data"], reason="两类信息需求按优先级派出。"),
            RouteDecision(reason="结论已足以回答。", is_final_ready=True),
        ],
        draft=ReportDraft(conclusion="汇总完成。"),
    )
    knowledge, data, business = FakeAgent("knowledge"), FakeAgent("data"), FakeAgent("business")
    supervisor = make_supervisor(llm, knowledge=knowledge, data=data, business=business)

    await supervisor.run(task_id=1, user_id=1, query="任务")

    assert knowledge.calls == [(1, 1, "任务")]  # 仅第一个 Agent 被调度，且收到原始任务
    assert data.calls == [] and business.calls == []


async def test_max_rounds_truncates_at_five() -> None:
    """验收：5 轮截断——调度数（含失败重试）达默认上限 5 后强制终态汇总，
    第 6 次决策由守卫生成，不再消耗路由 LLM 调用（防无限循环）。"""
    llm = SupervisorLLMStub(
        chats=[],
        decisions=[RouteDecision(next_agents=["knowledge"], reason="继续派知识检索。")] * 5,
        draft=ReportDraft(conclusion="达到上限汇总。"),
    )
    knowledge = FakeAgent("knowledge")
    supervisor = make_supervisor(llm, knowledge=knowledge, max_rounds=5)  # 显式 5，不受 .env 覆盖

    report = await supervisor.run(task_id=1, user_id=1, query="任务")

    assert knowledge.calls == [(1, 1, "任务")] * 5  # 恰好调度 5 次
    assert len(route_prompts(llm)) == 5  # 第 6 次决策由守卫生成，不再询问 LLM
    assert report.conclusion == "达到上限汇总。"


async def test_agent_retry_after_failure_succeeds() -> None:
    """验收：Agent 失败一次后重试成功——异常转 failed 结果写回（不上抛），
    路由可见失败原因后决策重试，第二次成功回环终态汇总。"""
    llm = SupervisorLLMStub(
        chats=[],
        decisions=[
            RouteDecision(next_agents=["knowledge"], reason="需要知识检索。"),
            RouteDecision(next_agents=["knowledge"], reason="知识检索失败，重试一次。"),
            RouteDecision(reason="重试成功，结论已足以回答。", is_final_ready=True),
        ],
        draft=ReportDraft(conclusion="重试成功后的汇总。"),
    )
    flaky = FlakyAgent("knowledge", KnowledgeAgentError("检索炸了"), content="知识结论。")
    supervisor = make_supervisor(llm, knowledge=flaky)

    report = await supervisor.run(task_id=1, user_id=1, query="任务")

    assert flaky.calls == 2  # 失败一次后恰好重试一次
    prompts = route_prompts(llm)
    # 回环依据：重试决策可见 failed 结果与失败原因；终态决策可见重试成功的结论
    assert "【knowledge】（failed）" in prompts[1] and "检索炸了" in prompts[1]
    assert "【knowledge】（completed）" in prompts[2] and "知识结论。" in prompts[2]
    assert report.conclusion == "重试成功后的汇总。"


async def test_consecutive_failures_fallback_to_report() -> None:
    """验收：连续失败触发 fallback——重试仍失败后路由降级（is_final_ready=true），
    ReportAgent 仅基于已有结果（含 failed 结果）生成报告，编排不因 Agent 异常终止。"""
    llm = SupervisorLLMStub(
        chats=[],
        decisions=[
            RouteDecision(next_agents=["knowledge"], reason="需要知识检索。"),
            RouteDecision(next_agents=["knowledge"], reason="失败重试一次。"),
            RouteDecision(reason="重试仍失败，降级为仅基于已有结果汇总。", is_final_ready=True),
        ],
        draft=ReportDraft(conclusion="基于已有结果的降级汇总。"),
    )
    boom = BoomAgent("knowledge", KnowledgeAgentError("检索炸了"))
    supervisor = make_supervisor(llm, knowledge=boom)

    report = await supervisor.run(task_id=1, user_id=1, query="任务")

    assert boom.calls == 2  # 首次 + 重试各一次，之后降级不再派发
    prompts = route_prompts(llm)
    assert "【knowledge】（failed）" in prompts[1] and "检索炸了" in prompts[1]
    assert "【knowledge】（failed）" in prompts[2]  # 降级决策同样可见失败原因
    assert report.conclusion == "基于已有结果的降级汇总。"


async def test_chitchat_goes_straight_to_report() -> None:
    """寒暄：首轮即终态，不调度任何专业 Agent，报告为唯一出口。"""
    llm = SupervisorLLMStub(
        chats=[],
        decisions=[RouteDecision(reason="寒暄无需专业 Agent。", is_final_ready=True)],
        draft=ReportDraft(conclusion="您好，请问有什么可以帮您？"),
    )
    knowledge, data, business = FakeAgent("knowledge"), FakeAgent("data"), FakeAgent("business")
    supervisor = make_supervisor(llm, knowledge=knowledge, data=data, business=business)

    report = await supervisor.run(task_id=1, user_id=1, query="你好呀")

    assert report.conclusion == "您好，请问有什么可以帮您？"
    assert knowledge.calls == [] and data.calls == [] and business.calls == []
    assert [c["kind"] for c in llm.calls] == ["structured", "structured"]  # 仅路由 + 报告


async def test_research_after_no_answer_hits() -> None:
    """验收：首次无答案（NO_RELEVANT_DOCUMENT）→ 改写 query 重搜 → 命中后终态汇总。
    重搜以决策改写的检索查询执行，次数记入 state 并经路由提示可见。"""
    llm = SupervisorLLMStub(
        chats=[],
        decisions=[
            RouteDecision(next_agents=["knowledge"], reason="需要知识检索。"),
            RouteDecision(
                next_agents=["knowledge"],
                reason="未检索到相关知识，改写查询重搜。",
                query="退货政策 退款流程",
            ),
            RouteDecision(reason="重搜已命中，结论已足以回答。", is_final_ready=True),
        ],
        draft=ReportDraft(conclusion="重搜命中后的汇总。"),
    )
    knowledge = ScriptedAgent(
        "knowledge",
        [no_answer(), AgentResult(agent="knowledge", content="根据知识库：7 天内无理由退款。")],
    )
    supervisor = make_supervisor(llm, knowledge=knowledge)

    report = await supervisor.run(task_id=1, user_id=1, query="公司退款政策是什么？")

    # 首搜用任务原文，重搜用决策改写的检索查询
    assert knowledge.calls == [(1, 1, "公司退款政策是什么？"), (1, 1, "退货政策 退款流程")]
    prompts = route_prompts(llm)
    # 无答案结果以 error_code 渲染（路由可判断重搜）；重搜用量对后续路由可见
    assert "【knowledge】（partial·NO_RELEVANT_DOCUMENT）" in prompts[1]
    assert f"重搜用量：knowledge 已重搜 1/{MAX_RESEARCH_TIMES} 次" in prompts[2]
    assert report.conclusion == "重搜命中后的汇总。"


async def test_research_exhausted_two_times_gives_up() -> None:
    """验收：2 次改写重搜仍无答案 → 放弃重搜（is_final_ready）如实降级汇总；
    恰好 3 次调度（首搜 + 2 重搜），重搜达上限对放弃决策可见。"""
    llm = SupervisorLLMStub(
        chats=[],
        decisions=[
            RouteDecision(next_agents=["knowledge"], reason="需要知识检索。"),
            RouteDecision(next_agents=["knowledge"], reason="未命中，改写重搜一次。", query="退货流程"),
            RouteDecision(
                next_agents=["knowledge"], reason="仍未命中，换角度改写再搜一次。", query="退款 审批 时限"
            ),
            RouteDecision(reason="重搜已达上限仍无答案，放弃并如实汇总。", is_final_ready=True),
        ],
        draft=ReportDraft(conclusion="未找到相关知识，基于已有信息汇总。"),
    )
    knowledge = ScriptedAgent("knowledge", [no_answer(), no_answer(), no_answer()])
    supervisor = make_supervisor(llm, knowledge=knowledge)

    report = await supervisor.run(task_id=1, user_id=1, query="公司售后审批时限的规定是什么")

    assert knowledge.calls == [
        (1, 1, "公司售后审批时限的规定是什么"),  # 首搜：任务原文
        (1, 1, "退货流程"),  # 重搜 1：改写查询
        (1, 1, "退款 审批 时限"),  # 重搜 2：再次改写
    ]
    prompts = route_prompts(llm)
    assert f"knowledge 已重搜 1/{MAX_RESEARCH_TIMES} 次" in prompts[2]
    assert f"knowledge 已重搜 2/{MAX_RESEARCH_TIMES} 次" in prompts[3]  # 放弃决策可见已达上限
    assert report.conclusion == "未找到相关知识，基于已有信息汇总。"


async def test_query_not_used_outside_research() -> None:
    """决策 query 仅在 NO_RELEVANT_DOCUMENT 重搜时生效：failed 重试复述任务原文且不计重搜次数。"""
    llm = SupervisorLLMStub(
        chats=[],
        decisions=[
            RouteDecision(next_agents=["knowledge"], reason="需要知识检索。"),
            RouteDecision(next_agents=["knowledge"], reason="执行失败，重试一次。", query="改写词"),
            RouteDecision(reason="重试成功，结论已足以回答。", is_final_ready=True),
        ],
        draft=ReportDraft(conclusion="汇总。"),
    )
    flaky = ScriptedAgent(
        "knowledge", [KnowledgeAgentError("检索炸了"), AgentResult(agent="knowledge", content="知识结论。")]
    )
    supervisor = make_supervisor(llm, knowledge=flaky)

    await supervisor.run(task_id=1, user_id=1, query="任务")

    assert flaky.calls == [(1, 1, "任务"), (1, 1, "任务")]  # 两次都是任务原文（重试 ≠ 重搜）
    assert "重搜用量" not in route_prompts(llm)[2]  # failed 重试不计入重搜


async def test_research_chain_with_real_knowledge_agent() -> None:
    """端到端：真实 KnowledgeAgent 首搜（任务原文）未命中（内部改写器拒绝改写快速收敛）→
    Supervisor 决策改写 query 重搜 → 真实检索命中 → 终态汇总且证据可溯源。"""
    llm = SupervisorLLMStub(
        chats=[
            tool_call_response("call_1", "search_knowledge", json.dumps({"query": "公司退款政策是什么？"})),
            final_response("公司退款政策是什么？"),  # 内部改写器原样输出 = 拒绝改写，闭环快速收敛
            final_response("未在知识库中找到相关内容。"),  # 首搜汇总兜底 → partial + NO_RELEVANT_DOCUMENT
            tool_call_response("call_2", "search_knowledge", json.dumps({"query": "退款政策 退款条件"})),
            final_response('{"sufficient": true}'),  # 重搜命中的充分性评审
            final_response("根据知识库：7 天内无理由退款。"),
        ],
        decisions=[
            RouteDecision(next_agents=["knowledge"], reason="退款政策属于企业制度，需知识检索。"),
            RouteDecision(
                next_agents=["knowledge"],
                reason="未检索到相关政策，改写检索查询重搜。",
                query="退款政策 退款条件",
            ),
            RouteDecision(reason="重搜已命中，结论已足以回答。", is_final_ready=True),
        ],
        draft=ReportDraft(
            related_knowledge="7 天内无理由退款。",
            conclusion="公司退款政策：7 天内无理由退款。",
            evidence=[EvidenceDraft(content="退款政策依据知识库", source_ids=[1])],
        ),
    )
    retriever = QueryMappedRetriever({"退款政策 退款条件": hit_result(0.92)})  # 仅改写词命中
    supervisor = make_supervisor(
        llm,
        knowledge=KnowledgeAgent(llm, retriever=retriever),  # type: ignore[arg-type]
    )

    report = await supervisor.run(task_id=13, user_id=1, query="公司退款政策是什么？")

    # 检索查询序列即重搜语义的端到端证据：首搜原文未命中 → 重搜改写词命中
    assert retriever.queries == ["公司退款政策是什么？", "退款政策 退款条件"]
    assert [c["kind"] for c in llm.calls] == [
        "structured", "chat", "chat", "chat", "structured", "chat", "chat", "chat", "structured", "structured",
    ]
    assert report.conclusion == "公司退款政策：7 天内无理由退款。"
    assert report.evidence[0].sources[0].ref_id == "kb-1"


async def test_multi_round_chain_dispatch_sequence() -> None:
    """验收：多轮组合 Data→Knowledge→Data→Report——每轮调度结果逐步累积进路由输入，
    派发序列由结果累积形态唯一确定，报告为唯一出口。"""
    llm = SupervisorLLMStub(
        chats=[],
        decisions=[
            RouteDecision(next_agents=["data"], reason="先查销售数据。"),
            RouteDecision(next_agents=["knowledge"], reason="再查制度依据。"),
            RouteDecision(next_agents=["data"], reason="最后查订单明细。"),
            RouteDecision(reason="信息已足，生成报告。", is_final_ready=True),
        ],
        draft=ReportDraft(conclusion="多轮组合报告。"),
    )
    data = FakeAgent("data", content="数据结论。")
    knowledge = FakeAgent("knowledge", content="知识结论。")
    supervisor = make_supervisor(llm, data=data, knowledge=knowledge)

    report = await supervisor.run(task_id=71, user_id=1, query="复杂问题")

    assert data.calls == [(71, 1, "复杂问题")] * 2  # data 被派发两次
    assert knowledge.calls == [(71, 1, "复杂问题")]  # knowledge 恰好一次
    # 派发序列 data→knowledge→data 由回环结果累积唯一确定
    prompts = route_prompts(llm)
    assert prompts[1].count("【data】") == 1 and "【knowledge】" not in prompts[1]
    assert prompts[2].count("【data】") == 1 and "【knowledge】" in prompts[2]
    assert prompts[3].count("【data】") == 2 and "【knowledge】" in prompts[3]
    assert report.conclusion == "多轮组合报告。"


async def test_multi_round_chain_resume_consistent(checkpointer) -> None:
    """验收：多轮组合全程可 resume——第 2 次调度（knowledge）时进程崩溃
    （BaseException 不被 dispatch 吞掉），同 thread_id 以 None 输入恢复后从中断的
    调度节点续跑（已完成的首次 data 不重放），最终报告与不中断基线完全一致。"""
    decisions = [
        RouteDecision(next_agents=["data"], reason="先查销售数据。"),
        RouteDecision(next_agents=["knowledge"], reason="再查制度依据。"),
        RouteDecision(next_agents=["data"], reason="最后查订单明细。"),
        RouteDecision(reason="信息已足，生成报告。", is_final_ready=True),
    ]
    draft = ReportDraft(conclusion="多轮组合报告。")

    # 基线：不中断完整跑通（thread_id=91）
    baseline_llm = SupervisorLLMStub(chats=[], decisions=decisions, draft=draft)
    baseline = make_supervisor(
        baseline_llm,
        data=FakeAgent("data", content="数据结论。"),
        knowledge=FakeAgent("knowledge", content="知识结论。"),
        checkpointer=checkpointer,
    )
    expected = await baseline.run(task_id=91, user_id=1, query="复杂问题")

    # 中断：首次 data 完成后，knowledge 调度时崩溃（thread_id=92，与基线互不干扰）
    crash_llm = SupervisorLLMStub(chats=[], decisions=decisions, draft=draft)
    crash_data = FakeAgent("data", content="数据结论。")
    crashed = make_supervisor(
        crash_llm,
        data=crash_data,
        knowledge=CrashAgent(),
        checkpointer=checkpointer,
    )
    with pytest.raises(Crash):
        await crashed.run(task_id=92, user_id=1, query="复杂问题")

    assert crash_data.calls == [(92, 1, "复杂问题")]  # 崩溃前首次 data 已完成
    assert [c["kind"] for c in crash_llm.calls] == ["structured", "structured"]  # d1/d2 路由已落盘

    # 模拟进程重启：全新 Supervisor 实例 + 同决策脚本（前两条已在恢复态中），同 thread_id 续跑
    resumed_llm = SupervisorLLMStub(chats=[], decisions=decisions[2:], draft=draft)
    resumed_data = FakeAgent("data", content="数据结论。")
    resumed_knowledge = FakeAgent("knowledge", content="知识结论。")
    resumed = make_supervisor(
        resumed_llm,
        data=resumed_data,
        knowledge=resumed_knowledge,
        checkpointer=checkpointer,
    )
    report = await resumed.resume(task_id=92)

    assert report == expected  # 结果一致：同草稿同证据解析
    # 恢复后恰好续跑 3 步：knowledge 调度重执行 + 第 3/4 次路由（不重放首次 data 调度）
    assert [c["kind"] for c in resumed_llm.calls] == ["structured", "structured", "structured"]
    assert resumed_data.calls == [(92, 1, "复杂问题")]  # 仅第 2 次 data
    assert resumed_knowledge.calls == [(92, 1, "复杂问题")]

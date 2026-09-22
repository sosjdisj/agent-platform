"""ReportAgent：汇总各 Agent 结论，生成固定结构分析报告。

消费 AgentResult[]（DataAgent / KnowledgeAgent / BusinessAgent 的产出），
不重新查数据：无工具面、无状态持久化，单次 LLM 结构化输出（chat_structured）产出九段固定报告
（客户概况 / 销售趋势 / 订单变化 / 产品变化 / 相关知识 / 可能原因 / 证据 / 结论 / 建议）。

证据可溯源（Prompt 14.3 验收核心）：LLM 只输出来源引用编号（source_ids，对应输入来源目录序号），
ReportAgent 按编号解析为实际 Source——knowledge 来源含 document_id / chunk_id（ref_id），
tool 来源含工具名与参数摘要（ref_id）；证据来源永远来自输入 AgentResult，模型无法编造。
引用编号越界或证据无来源 → ReportAgentError（证据完整性不妥协，不产出半成品报告）。

不做：前端报告页、Supervisor 多 Agent 路由。
"""
from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from app.agents.state import AgentResult, Source
from app.agents.sources import result_blocks
from app.core.llm import LLMService

REPORT_AGENT_NAME = "report"

# 报告撰写系统提示：只依据输入作答、缺信息如实留空、证据必须标注来源编号
REPORT_SYSTEM_PROMPT = (
    "你是分析报告撰写员：只依据给定的各 Agent 结论与来源目录撰写报告，"
    "不重新查数据、不编造信息。固定结构：客户概况 / 销售趋势 / 订单变化 / 产品变化 / "
    "相关知识 / 可能原因 / 证据 / 结论 / 建议。输入未覆盖的段落如实写“暂无相关信息”；"
    "每条证据必须标注支撑它的来源编号（source_ids，对应来源目录中的序号）；"
    "结论与建议必须有证据支撑，语言简洁、面向业务读者。"
)


class ReportAgentError(RuntimeError):
    """ReportAgent 执行失败：报告生成或证据溯源校验未通过。"""


class ReportEvidence(BaseModel):
    """报告证据：一条结论依据 + 来源引用（自输入 AgentResult.sources 解析，保证可溯源）。"""

    content: str
    sources: list[Source] = []


class AnalysisReport(BaseModel):
    """固定结构分析报告：九段字段即报告结构的单一来源。"""

    customer_profile: str = ""  # 客户概况
    sales_trend: str = ""  # 销售趋势
    order_changes: str = ""  # 订单变化
    product_changes: str = ""  # 产品变化
    related_knowledge: str = ""  # 相关知识
    possible_causes: str = ""  # 可能原因
    evidence: list[ReportEvidence] = []  # 证据（每条必须带来源）
    conclusion: str = ""  # 结论
    suggestions: list[str] = []  # 建议


class EvidenceDraft(BaseModel):
    """LLM 输出的证据条目：来源以引用编号表达（解析为实际 Source 前的中间形态）。"""

    content: str
    source_ids: list[int] = []  # 1 起的来源目录序号


class ReportDraft(BaseModel):
    """LLM 结构化输出的报告草稿（chat_structured response_model）：段落文本 + 证据引用编号。"""

    customer_profile: str = ""
    sales_trend: str = ""
    order_changes: str = ""
    product_changes: str = ""
    related_knowledge: str = ""
    possible_causes: str = ""
    evidence: list[EvidenceDraft] = []
    conclusion: str = ""
    suggestions: list[str] = []


def build_source_catalog(results: Sequence[AgentResult]) -> list[Source]:
    """输入来源目录：展平各 AgentResult.sources 并按（类型, ref_id）去重，序号即 LLM 引用号（1 起）。"""
    catalog: list[Source] = []
    seen: set[tuple[str, str]] = set()
    for result in results:
        for source in result.sources:
            key = (source.source_type, source.ref_id)
            if key not in seen:
                seen.add(key)
                catalog.append(source)
    return catalog


def assemble_report(draft: ReportDraft, catalog: Sequence[Source]) -> AnalysisReport:
    """草稿 → 报告：按引用编号解析证据来源。

    证据完整性校验：无来源引用或编号越界均抛 ReportAgentError（不产出无法溯源的半成品报告）。
    """
    evidence: list[ReportEvidence] = []
    for item in draft.evidence:
        if not item.source_ids:
            raise ReportAgentError(f"证据缺少来源引用：{item.content}")
        try:
            sources = [catalog[i - 1] for i in item.source_ids]
        except IndexError:
            raise ReportAgentError(
                f"证据来源编号越界：{item.source_ids}（来源目录共 {len(catalog)} 条）"
            ) from None
        evidence.append(ReportEvidence(content=item.content, sources=sources))

    return AnalysisReport(
        customer_profile=draft.customer_profile,
        sales_trend=draft.sales_trend,
        order_changes=draft.order_changes,
        product_changes=draft.product_changes,
        related_knowledge=draft.related_knowledge,
        possible_causes=draft.possible_causes,
        evidence=evidence,
        conclusion=draft.conclusion,
        suggestions=draft.suggestions,
    )


def _catalog_lines(catalog: Sequence[Source]) -> str:
    """来源目录文本（供 LLM 引用）：序号. [类型] 标识（文档标题）。"""
    lines = []
    for index, source in enumerate(catalog, start=1):
        line = f"{index}. [{source.source_type}] {source.ref_id}"
        if source.title:
            line += f"（{source.title}）"
        lines.append(line)
    return "\n".join(lines)


def _build_user_prompt(query: str, results: Sequence[AgentResult], catalog: Sequence[Source]) -> str:
    """撰写输入：任务问题 + 各 Agent 结论（带状态）+ 来源目录。"""
    return (
        f"任务：{query}\n\n各 Agent 结论：\n{result_blocks(results) or '（无）'}\n\n"
        f"来源目录（引用编号. [类型] 标识）：\n{_catalog_lines(catalog) or '（无）'}"
    )


class ReportAgent:
    """报告生成 Agent：无工具面、无状态持久化，单次结构化输出。"""

    def __init__(self, llm: LLMService) -> None:
        self._llm = llm

    async def generate(self, query: str, results: Sequence[AgentResult]) -> AnalysisReport:
        """汇总各 Agent 结论生成报告：证据来源自输入解析（失败抛 ReportAgentError）。"""
        catalog = build_source_catalog(results)
        draft = await self._llm.chat_structured(
            [
                {"role": "system", "content": REPORT_SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(query, results, catalog)},
            ],
            ReportDraft,
        )
        return assemble_report(draft, catalog)

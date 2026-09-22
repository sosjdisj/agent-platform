"""ReportAgent 测试：假 AgentResult 输入 + chat_structured 替身。

覆盖：固定九段结构与证据溯源完整性（验收核心）/ 来源目录去重编号 /
引用越界与无来源拒绝 / 空输入与缺省段落 / 提示词装配。
"""

from typing import Any

import pytest

from app.agents.report import (
    REPORT_SYSTEM_PROMPT,
    AnalysisReport,
    EvidenceDraft,
    ReportAgent,
    ReportAgentError,
    ReportDraft,
    build_source_catalog,
)
from app.agents.state import AgentResult, Source
from tests.conftest import StructuredLLMStub


def make_results() -> list[AgentResult]:
    """三个假 AgentResult：数据（工具来源）+ 知识（知识来源）+ 业务（工具来源）。"""
    return [
        AgentResult(
            agent="data",
            content="客户 1 销售额近三月连续下滑，6 月订单金额环比下降 12%。",
            sources=[
                Source(source_type="tool", ref_id="query_sales_data(customer_id=1)"),
                Source(source_type="tool", ref_id="query_orders(customer_id=1)"),
            ],
        ),
        AgentResult(
            agent="knowledge",
            content="退货政策支持 7 天内无理由退货，需保留原始包装。",
            sources=[
                Source(
                    source_type="knowledge",
                    ref_id="return-policy::c000",
                    document_id="return-policy",
                    title="退货政策",
                    score=0.92,
                )
            ],
        ),
        AgentResult(
            agent="business",
            content="客户 1 共 12 笔订单，累计消费 ¥2,556,000，有 1 个未解决工单。",
            sources=[Source(source_type="tool", ref_id="get_crm_summary(customer_id=1)")],
        ),
    ]


def draft(**overrides: Any) -> ReportDraft:
    """预置报告草稿：两段叙事 + 两条证据（分别引用工具与知识来源），overrides 覆盖。"""
    payload: dict[str, Any] = {
        "customer_profile": "客户 1 共 12 笔订单，累计消费 ¥2,556,000，有 1 个未解决工单。",
        "sales_trend": "近三月销售额连续下滑。",
        "order_changes": "6 月订单金额环比下降 12%。",
        "product_changes": "P1 销量占比下降。",
        "related_knowledge": "退货政策支持 7 天内无理由退货。",
        "possible_causes": "可能存在售后体验问题。",
        "evidence": [
            EvidenceDraft(content="销售额连续三月环比下降", source_ids=[1, 2]),
            EvidenceDraft(content="退货流程依据退货政策文档", source_ids=[3]),
        ],
        "conclusion": "客户 1 销售额下滑，疑似与未解决工单相关。",
        "suggestions": ["优先跟进未解决工单", "复核 P1 供货情况"],
    }
    payload.update(overrides)
    return ReportDraft.model_validate(payload)


async def test_report_structure_and_evidence_traceable() -> None:
    """验收核心：九段结构完整，证据来源按引用编号解析回输入 Source（knowledge 含 document_id/chunk_id）。"""
    results = make_results()
    llm = StructuredLLMStub([draft()])

    report = await ReportAgent(llm).generate("分析客户 1 销售额下降的原因", results)  # type: ignore[arg-type]

    assert isinstance(report, AnalysisReport)
    # 九段固定结构：字段齐备且取草稿文本
    assert report.customer_profile.startswith("客户 1 共 12 笔订单")
    assert report.sales_trend == "近三月销售额连续下滑。"
    assert report.order_changes == "6 月订单金额环比下降 12%。"
    assert report.product_changes == "P1 销量占比下降。"
    assert report.related_knowledge == "退货政策支持 7 天内无理由退货。"
    assert report.possible_causes == "可能存在售后体验问题。"
    assert report.conclusion.startswith("客户 1 销售额下滑")
    assert report.suggestions == ["优先跟进未解决工单", "复核 P1 供货情况"]

    # 证据完整性：来源目录 1=销售数据 2=订单 3=知识分块 4=CRM 概况，编号解析为实际 Source
    catalog = build_source_catalog(results)
    assert len(catalog) == 4
    assert [(e.content, e.sources) for e in report.evidence] == [
        ("销售额连续三月环比下降", [catalog[0], catalog[1]]),
        ("退货流程依据退货政策文档", [catalog[2]]),
    ]
    knowledge_source = report.evidence[1].sources[0]
    assert knowledge_source.source_type == "knowledge"
    assert knowledge_source.ref_id == "return-policy::c000"  # chunk_id
    assert knowledge_source.document_id == "return-policy"
    assert knowledge_source.title == "退货政策"
    assert knowledge_source.score == 0.92
    assert all(s.source_type == "tool" for s in report.evidence[0].sources)

    # 单次结构化调用；提示词装配：系统提示 + 任务 + 各 Agent 结论 + 来源目录
    (call,) = llm.calls
    assert call["response_model"] is ReportDraft
    system, user = call["messages"]
    assert system["role"] == "system" and REPORT_SYSTEM_PROMPT in system["content"]
    assert user["role"] == "user"
    assert "分析客户 1 销售额下降的原因" in user["content"]
    assert "客户 1 销售额近三月连续下滑" in user["content"]  # data 结论原文
    assert "【knowledge】" in user["content"] and "【business】" in user["content"]
    for ref_id in ("query_sales_data(customer_id=1)", "return-policy::c000", "get_crm_summary(customer_id=1)"):
        assert ref_id in user["content"]


def test_catalog_dedupes_across_results() -> None:
    """来源目录：跨 AgentResult 的相同来源按（类型, ref_id）去重，序号连续（即 LLM 引用号）。"""
    results = [
        AgentResult(
            agent="data",
            content="a",
            sources=[Source(source_type="tool", ref_id="query_sales_data(customer_id=1)")],
        ),
        AgentResult(
            agent="business",
            content="b",
            sources=[
                Source(source_type="tool", ref_id="query_sales_data(customer_id=1)"),  # 与 data 重复
                Source(source_type="tool", ref_id="get_crm_summary(customer_id=1)"),
            ],
        ),
    ]

    catalog = build_source_catalog(results)

    assert [s.ref_id for s in catalog] == ["query_sales_data(customer_id=1)", "get_crm_summary(customer_id=1)"]


async def test_out_of_range_citation_raises() -> None:
    """引用编号越界：拒绝产出报告（证据完整性不妥协）。"""
    llm = StructuredLLMStub([draft(evidence=[EvidenceDraft(content="无中生有", source_ids=[99])])])

    with pytest.raises(ReportAgentError, match="越界"):
        await ReportAgent(llm).generate("分析", make_results())  # type: ignore[arg-type]


async def test_evidence_without_source_raises() -> None:
    """证据无来源引用：拒绝产出报告（每条证据必须带来源）。"""
    llm = StructuredLLMStub([draft(evidence=[EvidenceDraft(content="凭空推断", source_ids=[])])])

    with pytest.raises(ReportAgentError, match="缺少来源"):
        await ReportAgent(llm).generate("分析", make_results())  # type: ignore[arg-type]


async def test_empty_results_generate_report_with_defaults() -> None:
    """空输入：无来源目录与证据，未覆盖段落取缺省值（结构固定、如实留空）。"""
    llm = StructuredLLMStub([ReportDraft(conclusion="输入不足以生成分析。")])

    report = await ReportAgent(llm).generate("分析客户 1", [])  # type: ignore[arg-type]

    assert report.conclusion == "输入不足以生成分析。"
    assert report.evidence == []
    assert report.customer_profile == "" and report.sales_trend == "" and report.suggestions == []
    (call,) = llm.calls
    assert "（无）" in call["messages"][1]["content"]  # 结论块与来源目录均为空占位

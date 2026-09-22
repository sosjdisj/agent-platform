"""Prompt 21.4 E2E 核心场景验收：帮我分析客户 A 最近销售额下降的原因。

全链路真实组件编排（仅 LLM 脚本化，其余皆真）：
- Supervisor 路由 → DataAgent（真实 4 工具面 + 真实 PostgreSQL 案例数据）→
  KnowledgeAgent（真实 ingest 分块入库 + 内存 Qdrant 检索，Agentic RAG）→
  ReportAgent（真实结构化汇总 + 证据溯源解析）；
- 验收断言：报告九部分齐全（客户概况 / 销售趋势 / 订单变化 / 产品变化 / 相关知识 /
  可能原因 / 证据 / 结论 / 建议）；证据可溯源——knowledge 证据带 document_id /
  chunk_id（ref_id）与相关性分，tool 证据 ref_id 回溯到真实 DB 查询（customer_id
  为运行时真实主键）；agent_tasks.result 落库文本九段齐全；
- 任务级（21.2 接线）：TaskExecutor 全流程跑通，pending 接单 → completed + 报告落库。

不做：评估体系（Prompt 22）。
"""

import json

import pytest
from qdrant_client import QdrantClient

from app.agents.data import DataAgent
from app.agents.knowledge import KnowledgeAgent
from app.agents.report import EvidenceDraft, ReportDraft
from app.agents.supervisor import RouteDecision, Supervisor
from app.rag.ingest import ingest_documents
from app.rag.retriever import KnowledgeRetriever
from app.rag.vector_store import KnowledgeVectorStore
from app.seed import KNOWLEDGE_DOCS
from app.services.task_executor import TaskExecutor, _report_text
from app.services.task_service import TaskService
from tests.conftest import FakeEmbedder
from tests.test_agent_graph import final_response, tool_call_response
from tests.test_agent_supervisor import FakeAgent, SupervisorLLMStub
from tests.test_rag_retriever import FakeReranker

QUERY = "帮我分析客户 A 最近销售额下降的原因"

# 落库报告文本的段落标记（渲染顺序单一来源见 task_executor._report_text）
_REPORT_MARKERS = (
    "【客户概况】",
    "【销售趋势】",
    "【订单变化】",
    "【产品变化】",
    "【相关知识】",
    "【可能原因】",
    "【依据】",
    "【结论】",
)


def _knowledge_retriever(tmp_path) -> KnowledgeRetriever:
    """真实 RAG 链路：seed 知识文档落盘 → 真实分块入库（内存 Qdrant + 伪嵌入）→
    检索（全库进重排池，相关性由内容重排函数决定：缺货/补货/供应商相关高分命中）。"""
    for filename, content in KNOWLEDGE_DOCS:
        (tmp_path / filename).write_text(content, encoding="utf-8")
    store = KnowledgeVectorStore(QdrantClient(":memory:"), collection_name="kb_e2e", dimension=8)
    ingest_documents(tmp_path, embedder=FakeEmbedder(8), store=store, max_chars=200)
    return KnowledgeRetriever(
        embedder=FakeEmbedder(8),
        store=store,
        reranker=FakeReranker(
            lambda query, text: 0.9 if any(k in text for k in ("缺货", "补货", "供应商")) else 0.05
        ),
        recall_top_k=100,  # 全库候选进入重排：命中与否只由重排打分决定，确定性不受粗召回序影响
        final_top_k=5,
        score_threshold=0.5,
    )


def _scenario_llm(a_id: int) -> SupervisorLLMStub:
    """核心场景 LLM 脚本：路由决策（data → knowledge → 终态）+ 专业 Agent 图对话 +
    报告草稿（九段 + 证据引用编号，编号对应真实来源目录：1-3 工具源，4 起知识源）。"""
    return SupervisorLLMStub(
        chats=[
            # DataAgent：解析客户 → 月度销售趋势 → 订单明细 → 汇总结论（工具真查 DB）
            tool_call_response("d1", "query_customers", json.dumps({"keyword": "CUST-0001"})),
            tool_call_response("d2", "query_sales_data", json.dumps({"customer_id": a_id})),
            tool_call_response("d3", "query_orders", json.dumps({"customer_id": a_id})),
            final_response(
                "客户A（华信智造，CUST-0001）2026年3-8月月度销售额：60.0/62.4/57.6/36.0/21.6/14.4 万元，"
                "核心产品 XS-100 自 6 月起连续三个月下滑（500→480→300→180→120 台），DC-20 每月 40 台持平。"
            ),
            # KnowledgeAgent：检索缺货交付制度 → 评审判定充分 → 汇总结论（真实向量检索）
            tool_call_response("k1", "search_knowledge", json.dumps({"query": "缺货 交付延迟 供应商 补货"})),
            final_response('{"sufficient": true}'),
            final_response(
                "《库存管理制度》规定：连续两个月同一产品缺货须上报运营总监并启动供应商评审；"
                "部分到货须当日同步客户经理并告知预计补齐时间。"
            ),
        ],
        decisions=[
            RouteDecision(next_agents=["data"], reason="销售额下降分析需先查询客户与订单销售数据。"),
            RouteDecision(
                next_agents=["knowledge"],
                reason="缺货与交付问题需对照企业制度判断原因与流失风险。",
                need_knowledge=True,
                query="缺货 交付延迟 供应商 补货",
            ),
            RouteDecision(reason="数据与知识结论已足以生成分析报告。", is_final_ready=True),
        ],
        draft=ReportDraft(
            customer_profile=(
                "客户A（华信智造，CUST-0001）为华东制造业客户，联系人陈敏，"
                "近 6 个月持续采购核心产品 XS-100 与配套线缆 DC-20。"
            ),
            sales_trend="2026年3-8月月度销售额 60.0→62.4→57.6→36.0→21.6→14.4 万元，6 月起环比大幅下滑，8 月较峰值下降约 77%。",
            order_changes="XS-100 订单量由 3 月 500 台逐月降至 8 月 120 台；DC-20 保持每月 40 台，无订单波动。",
            product_changes="下滑集中于核心产品 XS-100（约 -77%）；DC-20 无变化，排除产品结构迁移因素。",
            related_knowledge=(
                "《库存管理制度》要求连续两个月同一产品缺货须上报运营总监并启动供应商评审，"
                "部分到货须当日同步客户并告知补齐时间。"
            ),
            possible_causes="XS-100 连续缺货与交付延迟（6 月部分到货、7 月缺货 60 台）压制客户下单意愿，为销售额下滑的最可能原因。",
            evidence=[
                EvidenceDraft(content="月度销售额 6 月起逐月大幅下滑，XS-100 订单量同步锐减", source_ids=[2, 3]),
                EvidenceDraft(content="制度要求连续缺货升级供应商评审；客户 A 工单显示连续两月缺货并出现转向其他供应商的表态", source_ids=[4]),
            ],
            conclusion="客户 A 销售额下降主要由 XS-100 连续缺货与交付延迟引发，客户已出现流失风险信号，需立即干预。",
            suggestions=[
                "按制度启动供应商评审并上报运营总监",
                "客户经理 48 小时内回访客户 A 并给出明确补货承诺",
                "为 XS-100 设置安全库存预警，避免再次缺货",
            ],
        ),
    )


def _scenario_supervisor(llm: SupervisorLLMStub, seeded_maker, tmp_path) -> Supervisor:
    """核心场景编排：真实 DataAgent（真实 DB）+ 真实 KnowledgeAgent（真实 RAG 链路）+
    真实 ReportAgent；business 不在本场景路由清单内，用替身补齐避免构造真实组件。"""
    return Supervisor(
        llm,  # type: ignore[arg-type]
        data=DataAgent(llm, session_maker=seeded_maker),  # type: ignore[arg-type]
        knowledge=KnowledgeAgent(llm, retriever=_knowledge_retriever(tmp_path)),  # type: ignore[arg-type]
        business=FakeAgent("business"),
    )


async def test_sales_drop_nine_section_report(seeded_maker, case_ids, tmp_path) -> None:
    """验收：全链路产出九部分报告；证据带 source/document_id/chunk_id 且可回溯真实数据。"""
    a_id, _, _, _ = case_ids
    supervisor = _scenario_supervisor(_scenario_llm(a_id), seeded_maker, tmp_path)

    report = await supervisor.run(task_id=801, user_id=1, query=QUERY)

    # 九部分齐全：七个文本段非空，证据非空且每条带来源，建议非空
    assert report.customer_profile and report.sales_trend and report.order_changes
    assert report.product_changes and report.related_knowledge and report.possible_causes
    assert report.conclusion and report.suggestions
    assert report.evidence and all(item.sources for item in report.evidence)

    # 工具证据：ref_id 回溯到真实 DB 查询（customer_id 为运行时真实主键，非脚本常量）
    tool_refs = [s.ref_id for item in report.evidence for s in item.sources if s.source_type == "tool"]
    assert f"query_sales_data(customer_id={a_id})" in tool_refs
    assert f"query_orders(customer_id={a_id})" in tool_refs

    # 知识证据：source / document_id / chunk_id 三元组齐备（真实 ingest 产出的分块）
    knowledge = [s for item in report.evidence for s in item.sources if s.source_type == "knowledge"]
    assert knowledge
    source = knowledge[0]
    assert source.document_id == "库存管理制度"  # 文档 ID = seed 知识文档名（stem）
    assert source.ref_id.startswith("库存管理制度::c")  # chunk_id 规则：{document_id}::c{序号}
    assert source.title == "库存管理制度" and source.score is not None

    # 落库形态（agent_tasks.result）：九段文本齐全
    text = _report_text(report)
    for marker in _REPORT_MARKERS:
        assert marker in text
    assert any(line.startswith("- ") for line in text.splitlines())  # 建议段渲染为条目


async def test_sales_drop_task_lifecycle_completes(seeded_maker, case_ids, tmp_path) -> None:
    """验收：21.2 任务链路全流程跑通——pending 接单 → 编排 → completed，九段报告落库。"""
    a_id, _, _, _ = case_ids
    async with seeded_maker() as session:
        task = await TaskService(session).create(user_id=1, title="客户A销售额下降分析", query=QUERY)
    assert task.status == "pending"

    supervisor = _scenario_supervisor(_scenario_llm(a_id), seeded_maker, tmp_path)
    await TaskExecutor(supervisor, seeded_maker).execute(task.id, 1, QUERY)

    async with seeded_maker() as session:
        stored = await TaskService(session).get_owned(task.id, 1)
        assert stored.status == "completed"
        assert stored.finished_at is not None and stored.error is None
        assert stored.result is not None
        for marker in _REPORT_MARKERS:
            assert marker in stored.result

        # 结构化报告落库（24.1 报告页数据源）：九部分齐全，知识证据带
        # source / document_id / chunk_id / 文件名（title），与 result 文本同源
        report = stored.report
        assert report is not None
        assert all(
            report[field]
            for field in (
                "customer_profile", "sales_trend", "order_changes", "product_changes",
                "related_knowledge", "possible_causes", "conclusion",
            )
        )
        assert report["suggestions"] and report["evidence"]
        knowledge = [
            s for item in report["evidence"] for s in item["sources"]
            if s["source_type"] == "knowledge"
        ]
        assert knowledge
        assert knowledge[0]["title"] == "库存管理制度"  # 来源文件名
        assert knowledge[0]["document_id"] == "库存管理制度"
        assert knowledge[0]["ref_id"].startswith("库存管理制度::c")  # chunk_id


def test_report_markers_match_renderer() -> None:
    """测试常量与 _report_text 渲染单一来源一致（防漂移）。"""
    from app.services.task_executor import _REPORT_SECTIONS

    assert _REPORT_MARKERS == tuple(f"【{title}】" for title, _ in _REPORT_SECTIONS) + ("【依据】", "【结论】")

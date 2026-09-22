"""Agent 结果组装共享件：来源提取（tool 来源标识与去重）+ 结论文本块渲染（路由 / 报告提示词）。

工具来源（source_type="tool"）的标识与去重规则在此统一（单一数据源）：
ref_id=工具名(排序参数摘要)——同工具不同参数的调用（对比 / 不同客户等场景）各自成源，
同参重复调用按 ref_id 去重，失败调用不产生来源。
"""
from __future__ import annotations

from collections.abc import Sequence

from app.agents.state import AgentResult, AgentState, Source, ToolCallRecord


def result_blocks(results: Sequence[AgentResult]) -> str:
    """各 Agent 结论文本块：【agent】（status[·error_code]）正文，供路由与报告提示词复用
    （格式单一来源）；error_code 仅在有值时渲染（partial 兜底原因对路由 / 报告可见）。"""
    return "\n\n".join(_result_block(result) for result in results)


def _result_block(result: AgentResult) -> str:
    head = f"【{result.agent}】（{result.status.value}"
    if result.error_code:
        head += f"·{result.error_code}"
    return f"{head}）\n{result.content}"


def tool_ref_id(record: ToolCallRecord) -> str:
    """工具来源标识：工具名(排序参数摘要)；参数为空则仅工具名。"""
    if not record.arguments:
        return record.tool
    summary = ",".join(f"{key}={record.arguments[key]}" for key in sorted(record.arguments))
    return f"{record.tool}({summary})"


def successful_tool_sources(state: AgentState) -> dict[str, Source]:
    """成功工具调用 → 来源映射（按 ref_id 去重保留首次引用，失败调用跳过）。"""
    sources: dict[str, Source] = {}
    for record in state.tool_results:
        if not record.result.success:
            continue
        ref_id = tool_ref_id(record)
        sources.setdefault(ref_id, Source(source_type="tool", ref_id=ref_id))
    return sources

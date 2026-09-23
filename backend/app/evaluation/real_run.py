"""真实评估运行（python -m app.evaluation.real_run）：真实 LLM + Supervisor 全链路。

与 demo.py 的区别：demo 为脚本化编排（无 LLM、结果预置，供 Dashboard 本地演示），
本模块走生产同款 TaskExecutor + Supervisor（评估行为参数：temperature=0、
supervisor_max_rounds=4），requires_approval 且期望 completed 的案例
（resume-after-approval）自动批准并从 checkpoint 续跑。
结果落盘 settings.evaluation_report_dir（Dashboard API 读取同一路径，--out 可覆盖）。
"""
import asyncio
import logging
import time
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from app.agents.state import AgentStatus
from app.agents.supervisor import Supervisor
from app.core.llm import create_llm_service
from app.db.session import SessionLocal
from app.evaluation.cases import EVALUATION_CASES
from app.evaluation.config import EvaluationAgentConfig
from app.evaluation.demo import ensure_demo_users
from app.evaluation.reporting import EvaluationReport, build_report, write_reports
from app.evaluation.runner import EvaluationRunner, evaluate_case, observe_task
from app.services.approval_service import ApprovalService
from app.services.task_executor import TaskExecutor
from app.services.task_service import TaskService
from app.services.trace_service import TraceService

logger = logging.getLogger(__name__)


def _build_real_supervisor(config: EvaluationAgentConfig) -> Supervisor:
    """真实评估 Supervisor：评估 LLM 行为参数 + 审批记录接线 + 进程内 checkpoint
    （resume-after-approval 需要中断点续跑；评估单进程串行执行，InMemory 即可）。"""
    return Supervisor(
        create_llm_service(default_temperature=config.temperature),
        max_rounds=config.supervisor_max_rounds,
        checkpointer=InMemorySaver(),
        auth_session_maker=SessionLocal,
        approval_session_maker=SessionLocal,
        trace_session_maker=SessionLocal,
    )


async def _approve_and_resume(session_maker, executor: TaskExecutor, task_id: int, user_id: int) -> bool:
    """复刻 decide_approval 的 approved 分支：批准 → waiting 回 running → checkpoint 续跑。
    无审批记录（中断未发生）时返回 False，由调用方如实记录评估结果。"""
    async with session_maker() as session:
        approvals = await ApprovalService(session).list_by_task(task_id)
        if not approvals:
            logger.warning("任务 %s 无审批记录，跳过批准续跑", task_id)
            return False
        approval = approvals[-1]
        await ApprovalService(session).approve(
            approval.id, decided_by=user_id, comment="评估自动批准"
        )
        task = await TaskService(session).get_owned(task_id, user_id)
        await TaskService(session).transition(task, AgentStatus.RUNNING)
    # TaskExecutor.resume 返回后台协程句柄（生产语义），评估需等待续跑到达终态/待审批
    background = await executor.resume(task_id, user_id)
    await background
    return True


class RealEvaluationRunner(EvaluationRunner):
    """真实链路评估：Runner 标准流程 + requires_approval 案例的自动批准续跑。

    全部案例共享同一个 Supervisor 实例（RAG 栈仅加载一次，避免逐案例重复加载
    Embedding / Reranker 模型触发 Windows 内存耗尽；graph 状态按 thread_id=task_id
    隔离，共享安全）。
    """

    def __init__(self, session_maker, config: EvaluationAgentConfig) -> None:
        supervisor = _build_real_supervisor(config)
        super().__init__(session_maker, lambda case: supervisor)

    async def run_case(self, case, *, user_id: int):
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
        async with self._session_maker() as session:
            stored = await TaskService(session).get_owned(task.id, user_id)

        if (
            case.requires_approval
            and case.expected_outcome == "RESUME_CONSISTENT"
            and stored.status == AgentStatus.WAITING_APPROVAL.value
        ):
            # 仅续跑型案例（RESUME_CONSISTENT）自动批准续跑；AWAITING_APPROVAL 案例
            # 的审批中断本身即观察目标，续跑会推进到终态覆盖观察
            if await _approve_and_resume(self._session_maker, executor, task.id, user_id):
                async with self._session_maker() as session:
                    stored = await TaskService(session).get_owned(task.id, user_id)
        latency_ms = int((time.perf_counter() - started) * 1000)

        async with self._session_maker() as session:
            events = await TraceService(session).list_by_task(task.id)
        return evaluate_case(case, observe_task(stored, events), latency_ms=latency_ms)


async def run_real_evaluation(session_maker, case_filter: str | None = None) -> list:
    """跑 EVALUATION_CASES（真实 LLM 编排）。

    case_filter：案例 id 子串过滤（冒烟测试用）；过滤模式下只打印逐例结果，
    不聚合全量报告（指标分母依赖完整用例集）。"""
    users_by_role = await ensure_demo_users(session_maker)
    runner = RealEvaluationRunner(session_maker, EvaluationAgentConfig())
    cases = [c for c in EVALUATION_CASES if case_filter is None or case_filter in c.id]
    results = []
    for case in cases:
        print(f"运行案例 {case.id} ...", flush=True)
        result = await runner.run_case(case, user_id=users_by_role[case.user_role])
        verdict = "PASS" if result.passed else "FAIL: " + "; ".join(result.mismatches)
        print(f"  -> {verdict}", flush=True)
        results.append(result)
    return results


def main() -> None:
    """CLI 入口：python -m app.evaluation.real_run（--out 覆盖输出目录，--filter 冒烟单跑）。"""
    import argparse

    from app.core.config import get_settings

    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="真实 LLM 全链路评估并落盘报告（JSON / HTML）")
    parser.add_argument("--out", default=None, help="输出目录（缺省取 settings.evaluation_report_dir）")
    parser.add_argument(
        "--filter", default=None, help="案例 id 子串过滤：只跑匹配用例并打印结果，不写报告（冒烟测试）"
    )
    args = parser.parse_args()

    results = asyncio.run(run_real_evaluation(SessionLocal, case_filter=args.filter))
    if args.filter:
        return
    directory = Path(args.out) if args.out else get_settings().evaluation_report_dir_path
    report = build_report(EVALUATION_CASES, results)
    json_path, html_path = write_reports(report, directory)
    print(f"评估报告已生成：{json_path}\n{html_path}")


if __name__ == "__main__":
    main()

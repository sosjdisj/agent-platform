"""评估报告 API（24.2 Dashboard 数据源）：读取最新评估报告 JSON（Prompt 22.3 产出）。

- 数据源：settings.evaluation_report_dir 下的 evaluation_report.json（write_reports
  覆盖写，"最新报告"语义），结构契约 = EvaluationReport（metrics / cases / failures）；
- 认证：沿用 Bearer 头（get_current_user），报告为平台级数据不做按用户隔离；
- 报告缺失 / 损坏 → 404（EVALUATION_REPORT_NOT_FOUND），前端引导先生成报告。
"""
from pathlib import Path

from fastapi import APIRouter, Depends

from app.core.config import get_settings
from app.core.errors import AuthError
from app.evaluation.reporting import EvaluationReport, load_latest_report
from app.models.user import User
from app.api.deps import get_current_user

router = APIRouter(prefix="/api/evaluation", tags=["evaluation"])


@router.get("/report", response_model=EvaluationReport)
async def get_evaluation_report(user: User = Depends(get_current_user)) -> EvaluationReport:
    """返回最新评估报告：总体指标 + 单案例详情 + 错误案例清单。"""
    report_dir: Path = get_settings().evaluation_report_dir_path
    try:
        return load_latest_report(report_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise AuthError(
            "EVALUATION_REPORT_NOT_FOUND",
            f"评估报告不存在或格式无效（{report_dir}），请先生成报告：{exc}",
            status_code=404,
        ) from None

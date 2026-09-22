"""后台定时任务：轨迹事件保留期清理（19.2）。

应用 lifespan 启动 trace_cleanup_loop 常驻协程，按配置周期删除超过保留期的
agent_trace_events 事件；清理逻辑本身在 TraceService.purge_before（可单测），
此处仅负责周期调度与失败兜底（单轮失败记日志，等待下一轮，不中断循环）。
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.trace_service import TraceService

logger = logging.getLogger(__name__)


async def trace_cleanup_loop() -> None:
    """周期清理过期轨迹事件：启动即清理一轮，之后按 trace_cleanup_interval_seconds 轮询。"""
    settings = get_settings()
    interval = settings.trace_cleanup_interval_seconds
    while True:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=settings.trace_retention_days)
            async with SessionLocal() as session:
                removed = await TraceService(session).purge_before(cutoff)
            if removed:
                logger.info("轨迹清理：删除 %s 条超过 %s 天的事件", removed, settings.trace_retention_days)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("轨迹清理失败，等待下一轮")
        await asyncio.sleep(interval)

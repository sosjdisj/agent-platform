"""应用入口：uvicorn app.main:app --reload"""
import asyncio
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response

from app.api import api_router
from app.agents.checkpoint import open_checkpointer
from app.core.config import get_settings
from app.core.errors import AuthError, auth_error_handler
from app.core.logging import setup_logging
from app.core.middleware import SecurityHeadersMiddleware
from app.core.redis import RedisService, redis_client
from app.core.tasks import trace_cleanup_loop
from app.mcp.server import MCP_MOUNT_PATH, build_mcp_asgi_app
from app.services.task_executor import build_task_executor

settings = get_settings()

# FastMCP ASGI 应用：进程内集成（独立进程方案见 README「MCP Server 集成方式」）
mcp_asgi_app = build_mcp_asgi_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    cleanup_task = asyncio.create_task(trace_cleanup_loop())
    # FastAPI 不运行挂载子应用的 lifespan，需手动启动 MCP session manager
    try:
        # 任务执行器（21.2）：checkpointer 连接随进程常驻，组合根见 build_task_executor
        async with open_checkpointer() as checkpointer:
            app.state.task_executor = build_task_executor(
                checkpointer=checkpointer, redis=RedisService(redis_client)
            )
            async with mcp_asgi_app.lifespan(mcp_asgi_app):
                yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        # 释放数据库连接池与 Redis 连接池
        from app.db.session import engine

        await engine.dispose()
        await redis_client.aclose()


# 交互式 API 文档仅在 debug 模式开放（生产环境减少攻击面，且其静态资源依赖 CDN）
debug_kwargs = (
    {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}
    if settings.debug
    else {"docs_url": None, "redoc_url": None, "openapi_url": None}
)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    **debug_kwargs,
)

app.include_router(api_router)
app.add_exception_handler(AuthError, auth_error_handler)

# 前端 axios 开启 withCredentials，CORS 必须为显式来源（不可用 "*"）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 响应体压缩（API JSON 与前端静态资源；≥1KB 才压缩）
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
# 安全响应头（CSP / nosniff / 反点击劫持等，见 core/middleware.py）
app.add_middleware(SecurityHeadersMiddleware, enable_hsts=settings.enable_hsts)


# MCP 端点（/mcp）：挂载为独立前缀，不影响其他路由（须先于 SPA 兜底路由注册）
app.mount(MCP_MOUNT_PATH, mcp_asgi_app)


def _mount_frontend(dist: Path) -> None:
    """托管前端 SPA 构建产物：带哈希的 assets 长缓存，index.html 不缓存，未知路径回退 SPA。

    必须在所有路由与挂载之后注册，作为最低优先级的兜底路由。
    注意：兜底路由需接受全部方法，否则会以 PARTIAL 匹配（405）抢占
    redirect_slashes 对精确路径（如 POST /mcp → 307 → /mcp/）的重定向。
    """
    # Windows 下 mimetypes 可能按注册表把 .js 判为 text/plain，导致模块脚本被浏览器拒绝
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/javascript", ".mjs")
    resolved_dist = dist.resolve()

    @app.api_route(
        "/{full_path:path}",
        include_in_schema=False,
        methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    )
    async def serve_spa(request: Request, full_path: str) -> Response:
        # /mcp 精确路径沿用原 redirect_slashes 语义：307 重定向到挂载的 MCP 子应用
        if full_path == "mcp":
            return RedirectResponse("/mcp/", status_code=307)
        # 未知 API / 健康检查路径不回退 SPA，仍返回 404 以暴露接口错误
        if full_path.startswith(("api/", "mcp/", "health")) or full_path in ("api", "health"):
            raise HTTPException(status_code=404)
        if request.method not in ("GET", "HEAD"):
            raise HTTPException(status_code=405)
        candidate = (resolved_dist / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(resolved_dist):
            # 带内容哈希的静态资源可永久缓存；根下未哈希文件（若有）按需协商缓存
            cache = "public, max-age=31536000, immutable" if full_path.startswith("assets/") else "no-cache"
            return FileResponse(candidate, headers={"Cache-Control": cache})
        return FileResponse(resolved_dist / "index.html", headers={"Cache-Control": "no-cache"})


frontend_dist = settings.frontend_dist_path
if frontend_dist.is_dir():
    _mount_frontend(frontend_dist)

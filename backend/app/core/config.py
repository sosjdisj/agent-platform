"""应用配置：基于 pydantic-settings，自动读取项目根目录下的 .env 文件。"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py 位于 backend/app/core/，向上三级即项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# 评估报告 JSON 文件名（write_reports 落盘与 Dashboard API 读取共用，防止漂移）
EVALUATION_REPORT_JSON = "evaluation_report.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 应用
    app_name: str = "Agent Platform"
    app_version: str = "0.1.0"
    debug: bool = True
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    postgres_db: str = "agent_platform"
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_platform"
    )

    # JWT（双令牌：短命 Access + 长命 Refresh，Refresh 状态存 Redis）
    jwt_secret_key: str = "dev-secret-change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Qdrant
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None

    # 知识文档目录（相对项目根）：seed 落盘知识文档，后续 RAG / 向量化由此读取
    knowledge_docs_dir: str = "data/knowledge_docs"

    # 评估报告目录（相对项目根）：write_reports 落盘与 Dashboard API 读取的单一位置
    evaluation_report_dir: str = "data/evaluation"

    # RAG 向量化（参数全部来自配置，代码中禁止硬编码模型名 / 维度）
    # embedding_model：魔搭社区模型 ID，首次运行自动下载到本地缓存
    # embedding_dimension：向量维度，须等于模型输出维度或其 Matryoshka 截断值
    #   （embeddinggemma-300m 原生 768，可截断 512 / 256 / 128）；修改后重新入库自动重建 Collection
    embedding_model: str = "google/embeddinggemma-300m"
    embedding_dimension: int = 768
    embedding_batch_size: int = 16
    qdrant_collection_name: str = "knowledge_chunks"
    chunk_max_chars: int = 500  # 单个分块正文最大字符数

    # RAG 重排（CrossEncoder 粗召回后精排）
    # reranker_model：魔搭社区模型 ID，首次运行自动下载到本地缓存
    # rerank_score_threshold：相关性阈值（Sigmoid 概率分 [0,1]，≥ 阈值视为相关）。
    #   按本库语料实测校准：无关问题 top1 ≤ 0.0031，弱相关 top1 ≈ 0.0455，
    #   强相关 top1 ≥ 0.7，取 0.01（语料变更后可按需调整）
    reranker_model: str = "BAAI/bge-reranker-base"
    rerank_recall_top_k: int = 15  # 向量粗召回条数（重排候选池）
    rerank_final_top_k: int = 5  # 重排后返回条数
    rerank_score_threshold: float = 0.01  # 低于该分视为不相关，全部低于时返回 NO_RELEVANT_DOCUMENT

    # RAG 查询改写（首搜未命中时 LLM 将口语化查询改写为规范化检索语句后重搜）
    query_rewrite_max_times: int = 2  # 全程最多改写重搜次数

    # LLM（OpenAI 兼容 API；统一 Client 唯一创建点见 app/core/llm.py，业务代码经 get_llm_service 获取）
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""  # 生产环境必须配置
    llm_model: str = "gpt-4o-mini"

    # Agent 编排（LangGraph 图见 app/agents/graph.py）
    agent_max_rounds: int = 5  # 单次任务最多 LLM 决策轮次，超限截断为 failed
    supervisor_max_rounds: int = 5  # Supervisor 单任务最多调度 5 次（含失败重试），超限强制汇总降级

    # Trace 可观测（19.2）：轨迹事件保留期与后台清理周期
    trace_retention_days: int = 30  # 轨迹事件保留天数，过期事件由后台任务删除
    trace_cleanup_interval_seconds: int = 3600  # 清理任务轮询间隔（秒）

    # SSE 推送（20.1）：无事件空档期的心跳间隔（秒），须低于常见代理的空闲连接超时
    sse_heartbeat_seconds: float = 15.0

    # 连接超时（秒），用于健康检查等短连接场景
    healthcheck_timeout: float = 3.0

    # CORS（逗号分隔的允许来源；前端 axios 开启 withCredentials 时必须为显式来源）
    cors_origins: str = "http://localhost:5173"

    # 认证 Cookie（HttpOnly，禁止通过 URL 传递 token，详见 README「认证与 SSE Cookie 约定」）
    # sse_cookie_name：SSE 认证 Cookie，承载当前 access token，供 EventSource 等无法携带
    # Authorization 头的场景使用；refresh_cookie_name：承载长效 refresh token，
    # 浏览器 JS 不可读，/refresh 与 /logout 从 Cookie 读取
    sse_cookie_name: str = "sse_token"
    refresh_cookie_name: str = "refresh_token"
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_domain: str = ""  # 留空 = 仅当前 host；跨子域部署时填 ".example.com"
    cookie_secure: bool = False  # 生产环境（HTTPS）必须开启

    # 安全与前端托管
    enable_hsts: bool = False  # HTTPS 部署时开启，附加 Strict-Transport-Security 响应头
    frontend_dist_dir: str = "frontend/dist"  # 前端构建产物目录（相对项目根），存在时由后端托管 SPA

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def knowledge_docs_path(self) -> Path:
        return PROJECT_ROOT / self.knowledge_docs_dir

    @property
    def evaluation_report_dir_path(self) -> Path:
        """评估报告目录绝对路径（相对路径以项目根为基准，与知识文档目录一致）。"""
        return PROJECT_ROOT / self.evaluation_report_dir

    @property
    def evaluation_report_path(self) -> Path:
        """最新评估报告 JSON 路径（Dashboard API 数据源；文件名单一来源见下方常量）。"""
        return self.evaluation_report_dir_path / EVALUATION_REPORT_JSON

    @property
    def frontend_dist_path(self) -> Path:
        """前端构建产物绝对路径（相对路径以项目根为基准）。"""
        path = Path(self.frontend_dist_dir)
        return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache
def get_settings() -> Settings:
    return Settings()

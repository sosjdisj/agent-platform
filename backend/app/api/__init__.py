"""API 路由聚合。"""
from fastapi import APIRouter

from app.api.routes import approvals, auth, debug_agents, evaluation, health, tasks

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(health.router, tags=["health"])
api_router.include_router(debug_agents.router)
api_router.include_router(tasks.router)
api_router.include_router(approvals.router)
api_router.include_router(evaluation.router)

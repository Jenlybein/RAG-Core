"""API 路由汇总：第 1 步的 4 个接口统一挂到 /api/v1 前缀下。

main.py（步骤 8）只需 include_router(api_router) 一次。
"""

from fastapi import APIRouter

from app.api.routes import documents, knowledge_bases, rag

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(knowledge_bases.router)
api_router.include_router(documents.router)
api_router.include_router(rag.router)
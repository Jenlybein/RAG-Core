"""API 路由汇总：全部业务接口统一挂到 /api/v1 前缀下。

main.py 只需 include_router(api_router) 一次；具体端点清单以 main.py 的
docstring 为准，避免两处数量再次漂移。
"""

from fastapi import APIRouter

from app.api.routes import documents, knowledge_bases, rag

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(knowledge_bases.router)
api_router.include_router(documents.router)
api_router.include_router(rag.router)
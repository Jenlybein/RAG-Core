"""FastAPI 应用入口。

骨架期只挂 3 个接口，多一个都不挂：
  POST /api/v1/knowledge-bases/create
  POST /api/v1/documents/upload
  POST /api/v1/rag/retrieve

/docs 与 /openapi.json 由 FastAPI 自动提供。
"""

from fastapi import FastAPI

from app.api.router import api_router

# ── T2: 结构化日志（模块级幂等初始化，不进 lifespan）──
from app.core.logging import setup_logging

setup_logging()
# ── T2 结束 ──

app = FastAPI(title="RAG-Core", version="0.1.0")

# ── T3: 请求关联标识（纯 ASGI，横跨挂载子应用）──
from app.core.request_context import RequestContextMiddleware  # noqa: E402

app.add_middleware(RequestContextMiddleware)
# ── T3 结束 ──

app.include_router(api_router)


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    """仅用于探活，不算业务接口，不出现在 /docs 里。"""

    return {"status": "ok"}

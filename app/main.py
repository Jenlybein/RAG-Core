"""FastAPI 应用入口（第 1 步：两个入口都接通）。

HTTP 数据面（7 个业务接口，/api/v1/*）：
  POST   /api/v1/knowledge-bases/create
  GET    /api/v1/knowledge-bases
  POST   /api/v1/documents/upload
  GET    /api/v1/documents
  POST   /api/v1/documents/{document_id}/reindex
  DELETE /api/v1/documents/{document_id}
  POST   /api/v1/rag/retrieve

另有一个不进业务契约的探活口 ``GET /health``（不出现在 OpenAPI 文档里）。

MCP 数据面（/mcp/，JSON-RPC）：rag_retrieve / rag_list_knowledge_bases。
两个入口共用同一把占位 API Key 与同一批 Service。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastmcp.utilities.lifespan import combine_lifespans

from app.api.error_handlers import install_handlers
from app.api.router import api_router
from app.core.config import settings
from app.db.session import SessionLocal
from app.repositories import knowledge_base_repository
from app.services import rag_service

# ── T2: 结构化日志（模块级幂等初始化，不进 lifespan）──
from app.core.logging import setup_logging

setup_logging()
# ── T2 结束 ──


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """主应用自己的启动逻辑；挂子应用时不能被顶掉。"""

    yield


if settings.ENABLE_MCP:
    from app.mcp_server import create_mcp_http_app  # noqa: E402

    mcp_app = create_mcp_http_app(
        get_db=SessionLocal,
        retrieve=rag_service.retrieve,
        list_kbs=knowledge_base_repository.list_by_tenant,
    )
    combined_lifespan = combine_lifespans(app_lifespan, mcp_app.lifespan)
else:
    mcp_app = None
    combined_lifespan = app_lifespan

app = FastAPI(title="RAG-Core", version="0.1.0", lifespan=combined_lifespan)

# ── T3: 请求关联标识（BaseHTTPMiddleware，本项目无流式响应）──
from app.core.request_context import RequestContextMiddleware  # noqa: E402

app.add_middleware(RequestContextMiddleware)
# ── T3 结束 ──

app.include_router(api_router)

# ── T4: HTTP 入口适配器（异常 → 统一响应，仅覆盖 /api/v1/*）──
install_handlers(app)
# ── T4 结束 ──

# /mcp/ 是唯一前缀来源：子应用内部 path="/"，客户端 URL 必须带尾斜杠。
# 离线测试用 ENABLE_MCP=false 关掉，避免每个 TestClient 都启停会话管理器。
if mcp_app is not None:
    app.mount("/mcp", mcp_app)


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    """仅用于探活，不算业务接口，不出现在 /docs 里。"""

    return {"status": "ok"}

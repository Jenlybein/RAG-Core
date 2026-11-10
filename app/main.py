"""FastAPI 应用入口（第 1 步：两个入口都接通）。

控制面（第 7 步，/console/*，会话 cookie 鉴权）：
  POST   /console/login
  POST   /console/logout
  GET    /console/me
  GET    /console/plans
  PATCH  /console/plans/{plan_id}?dry_run=
  GET    /console/accounts
  PATCH  /console/accounts/{account_id}
  GET    /console/audit-logs
  GET    /console/keys                 （票 #91：明文 + masked + 配置，独立路由模块）
  POST   /console/keys                 （签发：连带建密钥配置）
  DELETE /console/keys/{key_id}        （删 Key，不删配置）
  PATCH  /console/key-policies/{policy_id}

控制面「管库管文档」（第 8 步票 #90，另起一个路由模块）：
  GET    /console/knowledge-bases/tree
  POST   /console/knowledge-bases
  DELETE /console/knowledge-bases/{kb_id}
  POST   /console/knowledge-bases/{kb_id}/documents
  POST   /console/documents/{document_id}/reindex
  DELETE /console/documents/{document_id}

HTTP 数据面（8 个业务接口，/api/v1/*）：
  GET    /api/v1/auth/me
  POST   /api/v1/knowledge-bases/create
  GET    /api/v1/knowledge-bases
  POST   /api/v1/documents/upload
  GET    /api/v1/documents
  POST   /api/v1/documents/{document_id}/reindex
  DELETE /api/v1/documents/{document_id}
  POST   /api/v1/rag/retrieve

另有一个不进业务契约的探活口 ``GET /health``（不出现在 OpenAPI 文档里）。

MCP 数据面（/mcp/，JSON-RPC）：rag_retrieve / rag_list_knowledge_bases。
两条入口只共用**同一份**判定内核与**同一套**关卡（身份 → 数据范围白名单 → 业务）：
HTTP 由 ``app/api/deps.py`` 取请求，MCP 由 ``app/mcp_server.py`` 取请求，其余全在
``app.core.auth`` 与 ``app.services.data_scope`` 里，各只有一份。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastmcp.utilities.lifespan import combine_lifespans

from app.api.error_handlers import install_handlers
from app.api.router import api_router
from app.api.routes.console import router as console_router
from app.api.routes.console_kbs import router as console_kbs_router
from app.api.routes.console_keys import router as console_keys_router
from app.core.config import settings
from app.utils import tokenizer
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

    # 切块的窗口口径依赖 Qwen 词表：缺它就只能换近似词表，而近似会让同一个库在
    # 不同部署上切出不同的片。所以启动时就要验，带病启动比启动失败更难查。
    tokenizer.verify_ready()
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

# 控制面不进 /api/v1：它是「人」的门，与数据面共用响应壳但不共用鉴权依赖。
# 票 #91 的 Key / 密钥配置接口放在独立模块里，与共用文件平级挂载（同一前缀）。
app.include_router(console_router)
# 管库管文档的六条单独成模块（并行票各自 include，不把别人的路由挤进 console.py）。
app.include_router(console_kbs_router)
app.include_router(console_keys_router)

# ── T4: HTTP 入口适配器（异常 → 统一响应；数据面与控制面共用同一套壳）──
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

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
  GET    /console/settings            （第 8 步 #93：配置中台）
  PATCH  /console/settings?dry_run=
  GET    /console/model-configs        （第 8 步 #94：多模型配置 + 角色指向）
  POST   /console/model-configs
  PATCH  /console/model-configs/{id}
  DELETE /console/model-configs/{id}
  GET    /console/model-configs/{id}/secret
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

import structlog
from fastapi import FastAPI
from fastmcp.utilities.lifespan import combine_lifespans

from app.api.error_handlers import install_handlers
from app.api.router import api_router
from app.api.routes.console import router as console_router
from app.api.routes.console_kbs import router as console_kbs_router
from app.api.routes.console_keys import router as console_keys_router
from app.api.routes.console_models import router as console_models_router
from app.api.routes.console_profiles import router as console_profiles_router
from app.api.routes.console_settings import router as console_settings_router
from app.api.routes.console_traces import router as console_traces_router
from app.api.routes.console_eval import router as console_eval_router
from app.api.routes.console_observability import (
    router as console_observability_router,
)
from app.core.config import settings
from app.core.http_clients import aclose_current_loop_clients
from app.core.profile_presets import load_profile_presets, set_profile_preset_loader
from app.utils import tokenizer
from app.db.session import SessionLocal
from app.providers.llm import set_role_resolver
from app.repositories import knowledge_base_repository
from app.services import model_config_service, profile_preset_service, rag_service

# ── T2: 结构化日志（模块级幂等初始化，不进 lifespan）──
from app.core.logging import setup_logging

setup_logging()
logger = structlog.get_logger(__name__)
# ── T2 结束 ──

# ── #94/#82: 模型角色解析器在**组合根**注入一次 ──
# Provider 层不许 import services（层向），而「角色指针 → 模型配置行」本来就只在服务层
# 有一份实现。这里把那份实现（及其 Provider 形状适配器）接到 Provider 的可注入解析器上：
# 不注入 = 没有角色指针 = 只用 .env，于是「谁先 import 谁」不再影响装配结果。
# Worker 是另一个进程、不走 app.main，在 ``app/tasks/indexing.py`` 里再注入一次。
set_role_resolver(model_config_service.role_endpoint)
# ── #94/#82 结束 ──


def _load_profile_presets_from_db() -> None:
    """档位参数装载器。组合根是唯一知道 ``SessionLocal`` 的地方，所以连接在这里给，
    注册表那边只留一个可注入的钩子（见 ``app.core.profile_presets``）。"""

    with SessionLocal() as db:
        profile_preset_service.load_into_registry(db)


set_profile_preset_loader(_load_profile_presets_from_db)


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """主应用自己的启动逻辑；挂子应用时不能被顶掉。

    **关闭动作是确定性的**（第 10 步票 4）：先停接收（``yield`` 返回就意味着 uvicorn 已经
    不再把新请求交给这个应用），再关这一轮攒下的 HTTP 客户端。顺序固定、且必须走
    ``try/finally``——启动期的校验抛错时也要把已经建起来的连接收掉；反过来（先关客户端
    再停接收）会让正在飞的请求拿到一个已关闭的池子。
    """

    # 切块的窗口口径依赖 Qwen 词表：缺它就只能换近似词表，而近似会让同一个库在
    # 不同部署上切出不同的片。所以启动时就要验，带病启动比启动失败更难查。
    tokenizer.verify_ready()

    # 档位参数装载一次。失败由 `load_profile_presets` 自己吞掉并记 warning（迁移没跑的
    # 库要能启动），这里不做二次兜底——两处兜底会让人分不清是哪一处救回的。
    load_profile_presets()
    try:
        yield
    finally:
        # 只关**当前 loop** 名下的客户端：API 进程的主循环是这一个，别的进程（Worker）
        # 有自己的一套，绝不能把这里的客户端带过去。
        await aclose_current_loop_clients()


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

# ── #93: 运行时配置热更新（每请求一次 TTL 比较，不读库）──
# 注册顺序决定内外层（后注册的在最外层）：先注册本中间件、再注册请求标识中间件，
# 于是刷新发生在请求标识绑定之后——刷新告警能带上 request_id。
from app.api.runtime_settings_middleware import RuntimeSettingsRefreshMiddleware  # noqa: E402

app.add_middleware(RuntimeSettingsRefreshMiddleware)
# ── #93 结束 ──

# ── T3: 请求关联标识（BaseHTTPMiddleware，本项目无流式响应）──
from app.core.request_context import RequestContextMiddleware  # noqa: E402

app.add_middleware(RequestContextMiddleware)
# ── T3 结束 ──

app.include_router(api_router)

# 控制面不进 /api/v1：它是「人」的门，与数据面共用响应壳但不共用鉴权依赖。
# 票 #91 的 Key / 密钥配置接口与 #93 的配置中台各走自己的路由模块，与共用文件平级挂载。
app.include_router(console_router)
# 管库管文档的六条单独成模块（并行票各自 include，不把别人的路由挤进 console.py）。
app.include_router(console_kbs_router)
app.include_router(console_keys_router)
# 配置中台走自己的路由模块（并行票的生命线：不往 console.py 里追加路由）。
app.include_router(console_settings_router)
# 多模型配置（#94）同样自带前缀：六条接口与配置中台平级挂载。
app.include_router(console_models_router)
# 档位预设（平台级配置，只有管理员读写）：两条接口。
app.include_router(console_profiles_router)
# 瀑布图（#103）：按 log_id 读回六段耗时，先本地记录、后观测服务。
app.include_router(console_traces_router)
# 语义旁证（#109）：触发一次旁证跑批并读它的状态。
app.include_router(console_eval_router)
# 观测总览（管理端，跨租户只读）：本地事实 + 观测栈聚合。
app.include_router(console_observability_router)

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

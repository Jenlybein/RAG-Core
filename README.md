# RAG-Core

面向业务系统的 **RAG 能力中台**。

核心边界一句话：**文档进来，切片出去**。它不生成最终答案，只返回「最相关的切片 + 相似度分数 + 出处」——怎么拼 Prompt、调大模型、组织成自然语言答复，是租户自己的事。

## 三种接入面

| 接入面 | 入口 | 面向 |
| --- | --- | --- |
| HTTP API | `POST /api/v1/rag/retrieve` | 业务系统后端 |
| MCP 入口 | `POST /mcp/`（JSON-RPC，三个工具，入参仅 `query`；第 11 步挂载，本步只留接缝） | AI Agent（Claude Desktop 等） |
| 控制台 | Vue 3 调试台：用户面板 / 管理员面板（需 `admin` 角色） | 人 |

HTTP 入口与 MCP 入口共用 `ErrorCode` / `AppError`，但响应契约不同：HTTP 入口返回 `{code, msg, data}` 加 HTTP 状态码，MCP 入口走 JSON-RPC `isError: true` + 文本带 code。见 [ADR-0008](docs/adr/0008-dual-entry-error-contract.md)。

## 本地环境（阶段 0 已就绪）

按教程第 0 步装好。下表是**实际落地时偏离教程早期版本**的七处，均已反向写回教程；原因写在 `pyproject.toml` 注释与 `deploy/README.md` 里：

| 项 | 教程早期值 | 本地实际 | 原因 |
| --- | --- | --- | --- |
| Python | 3.12.8 | 3.12.14 | 本机已有 3.12.14，同为 cp312，所有依赖 wheel 等价 |
| httpx | 0.27.2 | 0.28.1 | fastmcp 要求 `httpx>=0.28.1`（0.28 起 `proxies=` 改名 `proxy=`） |
| pydantic | 2.9.2 | 2.11.7 | fastmcp 3.0 要求 `pydantic>=2.11.7` |
| uvicorn | 0.32.0 | 0.35.0 | fastmcp 3.0 要求 `uvicorn>=0.35` |
| fastmcp | `>=2.9` | `==3.0.0` | 2.9～2.13 都没有 `fastmcp.dependencies` 与 `fastmcp.utilities.lifespan`，阶段 10 会 import 失败 |
| Embedding | `qwen3.7-text-embedding-flash` | `qwen3.7-text-embedding` | flash 版封顶 1024 维（传 1536 静默降级），撑不起 `vector(1536)` |
| ES 镜像 | ACR 个人命名空间预装 IK 镜像 | 官方 8.17.10 + `deploy/elasticsearch/Dockerfile` 自建 IK 层 | 原镜像已无法匿名拉取 |

```bash
uv sync                                                # 装全量依赖（一次装完，含阶段 10 的 fastmcp）
uv sync --all-groups                                   # 连开发组（ruff / mypy / pre-commit）一起装
docker compose -f deploy/docker-compose.yml up -d      # 四个容器：PG / Redis / ES(+IK) / Langfuse
docker exec -it pg16-pgvector psql -U postgres -d rag_db \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"          # 数据卷已存在时手动补一次
```

`.env` 从 `.env.example` 复制后只需改 3 行：`MODEL_BASE_URL`（百炼控制台给的、含业务空间 ID）、`MODEL_API_KEY`、`LLM_API_KEY`（DeepSeek，阶段 4 才用）。库密码默认与容器一致，不用改。`.env` 已在 `.gitignore` 里。第 2 步新增的 `LOG_LEVEL` / `LOG_DIR` 保持默认即可（`INFO` / `logs`），不必改。

## 第 2 步：工程可维护性（已落地）

- 与协议无关的 `ErrorCode(IntEnum)` 全表与 `AppError` 放在 `app/core/exceptions.py`；全表一次性落地，`3xxxx` 仅预留。权威表见 [错误码全表](docs/设计决策.md#错误码全表)。
- HTTP 入口适配器 `app/api/error_handlers.py` 的三个处理器：业务失败 HTTP 200 + 非零 `code`；参数校验失败 200 + `20002`（只含 `loc/msg/type`）；未捕获异常 500 + `50000`（堆栈只进日志）。成功 `msg="success"`。
- 包壳只覆盖 `/api/v1/*`；`/health`、`/docs`、`/openapi.json`、写错路径 404 保持 FastAPI 原生。
- `structlog 26.1.0` + 标准库 `RotatingFileHandler`：控制台彩色文本、`logs/app.log` 全量 JSON Lines、`logs/error.log` 仅 ERROR；只新增 `LOG_LEVEL`、`LOG_DIR` 两个配置项。
- 纯 ASGI 的 `request_id` 中间件：合法 `X-Request-ID` 原样回写，只进日志字段与响应头，不进响应体；500 的响应头由 `Exception` 处理器显式补写。业务失败不写 ERROR 日志。

## 运行与验证（离线）

```bash
uv run ruff check .                                    # 静态检查
uv run ruff format --check tests                      # 格式检查（历史文件暂不纳入）
uv run mypy app                                       # 宽松类型检查
uv run pytest -q                                      # 离线测试：不连 PG、不发真实模型请求
uv run uvicorn app.main:app --reload --port 8000      # 本地起服务，再按教程跑三条 curl
```

CI 在 push / PR 触发，依次跑 `uv sync --all-groups --frozen` → ruff 检查 → 格式检查 → mypy → pytest；不起容器、不发真实模型请求。本机网络受限时可用主 checkout 的解释器跑 `python -m pytest -q` 并设 `PYTHONPATH` 为当前 worktree 绝对路径；`mypy` / `pre-commit` 交给 CI 云端执行。

## 文档

- [项目综述](docs/项目综述.md) —— 定位与边界、要解决的四个实际问题、五个竞争力亮点
- [架构](docs/架构.md) —— 五层全景、九个模块、两条主链路、降级约定、本地起步
- [技术栈](docs/技术栈.md) —— 八个分层、完整版本矩阵、四个版本陷阱、替代方案代价、环境变量
- [设计决策](docs/设计决策.md) —— 五个「一改就全盘皆变」的决策与各自代价，附错误码全表（第 2 步一次性落地，`3xxxx` 预留）
- [ADR-0008 双入口错误契约](docs/adr/0008-dual-entry-error-contract.md) —— 共享错误词汇、各自翻译、协议级不对称及其代价
- [GLOSSARY](GLOSSARY.md) —— 领域术语：正名与禁用词

## Agent 协作约定

- **Issue tracker**：GitHub Issues（`Jenlybein/RAG-Core`，`gh` CLI）—— [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md)
- **Triage labels**：`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix` —— [docs/agents/triage-labels.md](docs/agents/triage-labels.md)
- **Domain docs**：single-context，根目录 `GLOSSARY.md` + `docs/adr/` —— [docs/agents/domain.md](docs/agents/domain.md)

约定汇总见 [AGENTS.md](AGENTS.md)。

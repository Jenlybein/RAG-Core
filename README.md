# RAG-Core

面向业务系统的 **RAG 能力中台**。

核心边界一句话：**文档进来，切片出去**。它不生成最终答案，只返回「最相关的切片 + 相似度分数 + 出处」——怎么拼 Prompt、调大模型、组织成自然语言答复，是租户自己的事。

## 三种接入面

| 接入面 | 入口 | 面向 |
| --- | --- | --- |
| HTTP API | `POST /api/v1/rag/retrieve` | 业务系统后端 |
| MCP | `POST /mcp`（JSON-RPC，三个工具，入参仅 `query`） | AI Agent（Claude Desktop 等） |
| 控制台 | Vue 3 调试台：用户面板 / 管理员面板（需 `admin` 角色） | 人 |

## 本地环境（阶段 0 已就绪）

按教程第 0 步装好。与教程版本矩阵有六处差异，原因写在 `pyproject.toml` 注释与 `deploy/README.md` 里：

| 项 | 教程原值 | 本地实际 | 原因 |
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
docker compose -f deploy/docker-compose.yml up -d      # 四个容器：PG / Redis / ES(+IK) / Langfuse
docker exec -it pg16-pgvector psql -U postgres -d rag_db \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"          # 数据卷已存在时手动补一次
```

`.env` 从 `.env.example` 复制，改 4 行（两个 Base URL/Key + DeepSeek Key + 库密码）。

## 文档

- [项目综述](docs/项目综述.md) —— 定位与边界、要解决的四个实际问题、五个竞争力亮点
- [架构](docs/架构.md) —— 五层全景、九个模块、两条主链路、降级约定、本地起步
- [技术栈](docs/技术栈.md) —— 八个分层、完整版本矩阵、四个版本陷阱、替代方案代价、环境变量
- [设计决策](docs/设计决策.md) —— 五个「一改就全盘皆变」的决策与各自代价，附错误码全表
- [GLOSSARY](GLOSSARY.md) —— 领域术语：正名与禁用词

## Agent 协作约定

- **Issue tracker**：GitHub Issues（`Jenlybein/RAG-Core`，`gh` CLI）—— [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md)
- **Triage labels**：`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix` —— [docs/agents/triage-labels.md](docs/agents/triage-labels.md)
- **Domain docs**：single-context，根目录 `GLOSSARY.md` + `docs/adr/` —— [docs/agents/domain.md](docs/agents/domain.md)

约定汇总见 [AGENTS.md](AGENTS.md)。

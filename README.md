# RAG-Core

面向业务系统的 **RAG 能力中台**。

核心边界一句话：**文档进来，切片出去**。它不生成最终答案，只返回「最相关的切片 + 排序分与分列分数 + 出处」——怎么拼 Prompt、调大模型、组织成自然语言答复，是租户自己的事。

## 三种接入面

| 接入面 | 入口 | 面向 |
| --- | --- | --- |
| HTTP API | `POST /api/v1/rag/retrieve` | 业务系统后端 |
| MCP 入口 | `POST /mcp/`（JSON-RPC，两个工具：`rag_retrieve` / `rag_list_knowledge_bases`；检索入参 `kb_ids` + `query`，`rag_research` 留给第 9 步） | AI Agent（Claude Desktop 等） |
| 控制台 | Vue 3 调试台：用户面板 / 管理员面板（需 `admin` 角色） | 人 |

HTTP 入口与 MCP 入口共用 `ErrorCode` / `AppError`，但响应契约不同：HTTP 入口返回 `{code, msg, data}` 加 HTTP 状态码，MCP 入口走 JSON-RPC `isError: true` + 文本带 code。见 [ADR-0008](docs/adr/0008-dual-entry-error-contract.md)。

## 本地环境（阶段 0 已就绪）

按教程第 0 步装好。下表是**实际落地时偏离教程早期版本**的地方：阶段 0 七处、第 4 步五处。阶段 0 的七处已反向写回教程；第 4 步的五处以本表与 [技术栈](docs/技术栈.md) 的版本矩阵为准。依赖版本的原因写在 `pyproject.toml` 注释里，部署相关的原因写在 `deploy/README.md` 里：

| 项 | 教程早期值 | 本地实际 | 原因 |
| --- | --- | --- | --- |
| Python | 3.12.8 | 3.12.14 | 本机已有 3.12.14，同为 cp312，所有依赖 wheel 等价 |
| httpx | 0.27.2 | 0.28.1 | fastmcp 要求 `httpx>=0.28.1`（0.28 起 `proxies=` 改名 `proxy=`） |
| pydantic | 2.9.2 | 2.11.7 | fastmcp 3.0 要求 `pydantic>=2.11.7` |
| uvicorn | 0.32.0 | 0.35.0 | fastmcp 3.0 要求 `uvicorn>=0.35` |
| fastmcp | `>=2.9` | `==3.0.0` | 2.9～2.13 都没有 `fastmcp.dependencies` 与 `fastmcp.utilities.lifespan`，阶段 10 会 import 失败 |
| Embedding | `qwen3.7-text-embedding-flash` | `qwen3.7-text-embedding` | flash 版封顶 1024 维（传 1536 静默降级），撑不起 `vector(1536)` |
| ES 镜像 | ACR 个人命名空间预装 IK 镜像 | 官方 8.17.10 + `deploy/elasticsearch/Dockerfile` 自建 IK 层 | 原镜像已无法匿名拉取 |
| LangChain 全家 | 0.2 / 0.3 世代（教程未钉版本） | `langchain==1.4.9`，连带 `langchain-core==1.7.6`、`langchain-openai==1.7.4`、`langchain-anthropic==1.7.10`、`langchain-google-genai==3.2.0`、`langgraph==1.2.20`、`langsmith==0.14.17` | 第 4 步统一模型接入层；LangChain 1.x 与 0.2/0.3 世代不共存 |
| ragas / datasets | `ragas>=0.2.0`、`datasets>=2.0.0` | `ragas==0.4.3`、`datasets==4.8.5`，并连带拉起 `openai==2.54.0` | 0.2.x 拖着 LangChain 0.2/0.3 时代的依赖，与 1.x 不可能共存 |
| langchain-community | 未声明（随 ragas 传递） | **必须钉 `0.4.1`** | `ragas==0.4.3` 在模块顶层 `from langchain_community.chat_models.vertexai import ChatVertexAI`；该模块在 `0.4.2` 被删除，不钉就 `ImportError` |
| pydantic-settings | 2.6.0 | 2.16.2 | `langchain-community==0.4.1` 要求 `pydantic-settings>=2.10.1`，2.6.0 卡在下面 |
| 模型名 | `deepseek-chat` / `deepseek-reasoner` | `deepseek-flash`（fast）/ `deepseek-v4-pro`（strong） | 旧模型名已下线 |

```bash
uv sync                                                # 装全量依赖（一次装完，含阶段 10 的 fastmcp）
uv sync --all-groups                                   # 连开发组（ruff / mypy / pre-commit）一起装
uv run python scripts/fetch_qwen_vocab.py              # 取 Qwen 分词词表（2.56 MB，带 SHA256 校验）
docker compose -f deploy/docker-compose.yml up -d      # 四个容器：PG / Redis / ES(+IK) / Langfuse
docker exec -it pg16-pgvector psql -U postgres -d rag_db \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"          # 数据卷已存在时手动补一次
```

第三条**不能省**：切块窗口按 Qwen token 计长，词表是必需资产。缺它时应用会拒绝启动
（报 `TokenizerUnavailable` 并提示执行该脚本），而不是悄悄换成近似词表——近似会让
同一个库在不同部署上切出不同的片，且那种差异完全静默。产物落 `.tokenizer/`，不入库。

`.env` 从 `.env.example` 复制后只需改 3 行：`MODEL_BASE_URL`（百炼控制台给的、含业务空间 ID）、`MODEL_API_KEY`、`LLM_API_KEY`（DeepSeek，第 4 步才用）。库密码默认与容器一致，不用改。`.env` 已在 `.gitignore` 里。第 1 步的 `DEV_API_KEY` / `DEV_TENANT_ID` 与第 2 步的 `LOG_LEVEL` / `LOG_DIR` 都保持默认即可（`dev-key` / `tenant_demo` / `INFO` / `logs`），不必改。

## 第 1 步：双入口骨架（已落地）

- 4 个 HTTP 接口：`POST /api/v1/knowledge-bases/create`、`POST /api/v1/documents/upload`、`POST /api/v1/rag/retrieve`、`GET /api/v1/knowledge-bases`（列这把 Key 可见的库）。
- MCP 入口挂在 `/mcp/`，两个工具 `rag_retrieve` / `rag_list_knowledge_bases`；`json_response=True`，工具调用回普通 JSON，不走流式。
- 两个入口头部都带 `Authorization: Bearer <Key>`；`tenant_id` 由服务端从这把 Key 解出，不进请求体。检索传 `kb_ids`（列表）+ `query`。**第 7 步已把占位 Key 换成 `api_keys` 表真校验与密钥配置白名单**：`AUTH_ENABLED=true`（默认）时只认真 Key；改成 `false` 才回落到内置开发身份（`dev-key` / `tenant_demo`）。
- `ENABLE_MCP=true` 时挂子应用（生产默认）；离线测试用 `ENABLE_MCP=false` 关掉会话管理器。

## 第 2 步：工程可维护性（已落地）

- 与协议无关的 `ErrorCode(IntEnum)` 全表与 `AppError` 放在 `app/core/exceptions.py`；全表一次性落地，`3xxxx` 仅预留。权威表见 [错误码全表](docs/设计决策.md#错误码全表)。
- HTTP 入口适配器 `app/api/error_handlers.py` 的三个处理器：业务失败 HTTP 200 + 非零 `code`；参数校验失败 200 + `20002`（只含 `loc/msg/type`）；未捕获异常 500 + `50000`（堆栈只进日志）。成功 `msg="success"`。
- 包壳只覆盖 `/api/v1/*`；`/health`、`/docs`、`/openapi.json`、写错路径 404 保持 FastAPI 原生。
- `structlog 26.1.0` + 标准库 `RotatingFileHandler`：控制台彩色文本、`logs/app.log` 全量 JSON Lines、`logs/error.log` 仅 ERROR；只新增 `LOG_LEVEL`、`LOG_DIR` 两个配置项。
- 基于 `BaseHTTPMiddleware` 的 `request_id` 中间件：合法 `X-Request-ID` 原样回写，只进日志字段与响应头，不进响应体；500 的响应头由 `Exception` 处理器显式补写。业务失败不写 ERROR 日志。

## 第 3 步：文档处理链路（已落地）

- 解析层：`.txt` / `.md` / `.docx` / `.pdf` / `.html` 五个解析器与注册表；`.docx` 走 `mammoth → markdownify(heading_style="ATX")`，`.html` 走 `bs4 + markdownify(ATX)`，`.pdf` 走 `pdf-inspector==1.25.2` 的 `process_pdf_with_ocr_bytes`（扫描件自动路由 OCR）。全项目唯一的「文档 → 文本」入口是 `app/services/document_ingestion.py::prepare_document_content`，下游只看到一条 markdown 化文本。
- 切块层：`MarkdownStructuredSplitter` —— 标题栈维护层级、h1/h2/h3 新起切片、`####` 及以下只加深路径、表格整块或按行分组且每组重复表头；**片内窗口**交给 `RecursiveCharacterTextSplitter`（正文按段落→中文句末标点，代码按围栏语言走 `from_language`），所以片不会从句子中间断开
- **切块窗口按 Qwen token 计长**（ADR-0015）：`chunk_size` 的单位是 token 而不是字符，计数与 DashScope 官方分词器逐 token 一致；词表由构建期脚本下载并校验，**缺失即拒绝启动**，不降级成近似词表（那会让同一个库在不同部署上切出不同的片）。
- **切块策略即语料准入**（ADR-0014）：建库时必须选 `structure` 或 `size`，它同时决定该库收哪些格式。`structure` 只收 markdown（它的能力全建立在标题语法上，收 pdf 只会退化成窗口硬切还不自知），`size` 默认五种全开、一律固定窗口切。上传格式不在白名单当场 `422` + `UNSUPPORTED_DOCUMENT_FORMAT`（ADR-0007「业务失败一律 200」的唯一例外）。
- 异步索引：`POST /api/v1/documents/upload`（JSON 与 multipart 双模式）只做「登记 + 投递」，立即返回 `status=3`；Celery Worker 拉取后解析 / 切块 / 向量化，写库是「先删后写 + 状态回写」的单事务。终态靠 `GET /api/v1/documents?kb_id=` 轮询。
- 幂等与租约：数据库 CAS 抢占 + 600 秒租约（`documents.index_claimed_at` 区分「已入队未开工」与「正在跑」）；`acks_late=True` 的重投由后到者 `rowcount=0` 直接跳过。入队侧只置队、**只有 Worker 抢占**，重试前归还租约。
- 运维能力：`POST /documents/{id}/reindex?reparse=&force=`（有效处理中拒绝 `20025`，成功态需 `force` 否则 `20027`）、`DELETE /documents/{id}`、`scripts/reindex_knowledge_base.py`、`scripts/requeue_stuck_documents.py`。
- OCR 运行时：Linux 容器内预装 PDFium + ONNX Runtime + PP-OCRv6 Small 模型，运行期不联网；构建与启动见 [deploy/README.md](deploy/README.md)，测试口径见 [tests/README.md](tests/README.md)。
- 切块策略自评：scripts/eval_chunking.py 复用第 4 步的 eval/corpus 与 eval/questions.json，对比**朴素基线**（评测侧自带实现，只存在于评测）与生产的结构化切块，并跑四组交叉（算法 × 单位）的检索指标（实测 R@1 0.688 → 0.761、切片均长 744 → 148 字）。

## 第 4 步：混合检索（已落地）

**这一步解决什么问题**：只靠向量检索，会漏掉「字面几乎一样、只差一个编号」的东西——同一个模板下的十几段内容，向量分不出谁是谁。做法是让两条通道各查一次，再把结果合成一份。

合成算法叫 **RRF（倒数排名融合）**：它不看原始分数（两路量纲不同，没法直接比），只看各自排第几，把名次换成 `1/(60 + 名次)` 再相加。于是「两路都出现」比「某一路拿第一」更值钱。

一条请求走固定路径：**查询优化 → 两条通道各一次全局召回 → 一次 RRF 融合 → 可选重排**。

### 做了什么

- **两条通道**：向量（pgvector，`1 - 余弦距离`）与关键词（Elasticsearch 8.17.10 + IK 分词，BM25）并行召回。默认（改写关闭）每条通道只查**一次**、嵌入也只算一次；改写开启时向量一路按原句 / 改写句各查一次（#31，见下一条），但每次仍一次覆盖全部「知识库 + 索引版本」配对，**不按库循环**。
- **融合契约**：`k=60` 服务端写死、不进请求体；唯一键 `(kb_id, chunk_id)`；同分顺序固定为 融合分降序 → 双路命中优先 → 最佳名次 → `kb_id` → `chunk_id`。完整契约见 [ADR-0010](docs/adr/0010-retrieval-contract.md)。
- **查询优化与重排**：改写、同义词扩展、重排**默认关闭**，失败一律降级、不把整单打挂。改写开启时向量一路**原句 + 改写句都搜**（两句各嵌一次、各查一次，合成一个池再进 RRF；不再是「改写句替换原句」），关闭 / 改写失败时只搜原句；同义词只影响关键词那一路，重排仍看改写句——**两路拿不同的查询是设计，不是 bug**。
- **模型接入**：大模型与嵌入走 LangChain（`fast` / `strong` 两个**模型角色**）；**检索层不引入框架**——召回、融合、重排编排、降级与响应契约全部自写，理由与可执行判据见 [ADR-0011](docs/adr/0011-langchain-boundary.md)。

### 三种结局，只有这三种

| 结局 | 什么时候 | 长什么样 |
| --- | --- | --- |
| **拒绝** | 两条通道都挂；或知识库不可见 | HTTP 200 + 非零 `code`（`50002` / `20008`） |
| **降级** | 只挂了一条 | `code: 0` + `metadata.retrieval.degraded: true` + `degraded_reason`，**仍有结果** |
| **诚实空** | 还没索引 / 一路没命中 | HTTP 200 + `empty_reason`（`no_indexed_chunks` / `no_chunks_matched`） |

降级与诚实空**可以同时出现**：一路失败、另一路也没命中时，会同时看到 `degraded: true` 和 `empty_reason`。

### ⚠️ 破坏性变更：`score` 换意思了

```text
score = 本次最终排序分（见下表）
```

| 情况 | `score` 是什么 |
| --- | --- |
| 单模式（`vector` / `bm25`） | 该路的原始分 |
| 混合 + 折叠开启（默认） | **单元内命中片的最高分** |
| 混合 + 折叠关闭 / 重排发生 | 融合分 / 重排分 |

它**不再恒等于 `rrf_score`**——后者始终是代表片那条结果的融合分。**不再是余弦相似度**，别拿它做 `score >= 0.8` 这类阈值判断。

要判断某一路是否命中、或拿具体分数，读 `vector_score` / `bm25_score` / `vector_rank` / `bm25_rank` / `rrf_score` / `rerank_score`；**未命中是 `null`，不是 0**。迁移方式见 [ADR-0010「后续修订」](docs/adr/0010-retrieval-contract.md)。

### ⚠️ 运维注意：既有 ES 索引必须重建

查询侧分析器改成了 `ik_smart_query`（`ik_smart` + 75 条中文停用词）并加了 `minimum_should_match="50%"`，用来止住「问句里的疑问词被当成罕见特征」的 BM25 噪声。

代价是 **mapping 变了，既有索引不能原地升级**：必须删掉 `rag_chunks` 重新入库。`scripts/reindex_knowledge_base.py` 只重投索引任务，**不重建 mapping**。修复前后数字见 [eval/ACCEPTANCE.md](eval/ACCEPTANCE.md)。

### 验收

```bash
uv run pytest -q
# 期望：1722 passed, 17 skipped, 2 warnings
# 17 skipped = 3 条 OCR 资产门禁 + 10 条真 PostgreSQL（CAS 6 + Worker 常驻循环 4）
#             + 3 条真 Elasticsearch（其中 1 条是查询分析器守卫）+ 1 条嵌入等价性
```

真实链路自测（先按上面的 `docker compose` 起 PG / ES，并至少入库一篇文档；把 `kb_ids` 换成自己的库 ID）：

```bash
uv run uvicorn app.main:app --port 8000

curl --noproxy '*' -sS -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer rc_live_<你在 Key 管理页签发的>" \
  -H "Content-Type: application/json" \
  -d '{"kb_ids":["<你的 kb_id>"],"query":"设备证书过期怎么处理"}'
```

`.env` 默认 `AUTH_ENABLED=true`，所以数据面认的是 `api_keys` 表里的**真实 Key**
（`rc_live_…`）：`tenant_id`、密钥配置白名单、档位、配额都由那把 Key 决定。

**内置开发身份**（`Authorization: Bearer dev-key`，固定租户 `tenant_demo`、配置全开）只在
`AUTH_ENABLED=false` 时可达。这个开关一旦漏配会**静默降级**——数据面只认占位 Key，调试台
里选中的每一把真实 Key 都被判 `20010`「无效或已停用」，看起来像 Key 坏了。所以两条路都
固化成脚本，别靠命令行前缀：

```bash
./scripts/dev-api.sh            # Linux / macOS，等价于 AUTH_ENABLED=true RATE_LIMIT_ENABLED=true uvicorn --reload
powershell -File scripts/dev-api.ps1   # Windows
```

> Docker 那条路同理：`deploy/docker-compose.yml` 的 `api` 服务把 `AUTH_ENABLED: "true"`
> 钉死，不受 `.env` 改来改去影响。

期望 `code: 0`，**默认一条结果代表一节**（`section_id` 是身份，`chunk_id` 是让它浮上来的代表片），每条都带 `score` 与分列字段。

检索方式、返回条数、是否重排、是否改写现在都由这把 Key 的密钥配置决定（请求里没有 `mode` / `top_k` / `include_parent` / `expand_table_rows`，传了会被 `20002` 拒绝）。想拿回第 4 步「一条结果 = 一个切片」的形状，把密钥配置的 `include_parent` 改成 `false`——本步还没有改配置的界面（面板入口在第 8 步），用 `uv run python scripts/update_key_policy.py --policy-id <policy_id> --no-include-parent` 改：它在写前跑与开租户同一个自洽校验，改完下一次调用即生效。把 ES 停掉再发一次，应看到 `degraded: true` / `degraded_reason: "keyword search failed"` 和基于向量的结果，而**不是** `50002`。

评测与验收矩阵留痕在 [eval/ACCEPTANCE.md](eval/ACCEPTANCE.md)（**第 4 步的 8 篇 / 245 片 / 26 题是历史快照**；当前语料已扩容，以该文件为准）。

**第 7 步已完成**：占位 Key 换成 `api_keys` 表真校验，「质量档位」锁在密钥配置上；`mode` / `top_k` 已经不在请求体里。做法与可复现步骤见下节。

## 第 7 步：多租户（已落地）

这一步把「一把假钥匙」换成三层授权：**请求只说问什么、查哪些库**（`kb_ids` + `query`），**密钥配置**（`key_policies`，由 Key 通过 `policy_id` 引用）决定这套集成能用多好、能用多少，**套餐**（`plans`）决定这个租户买得起多少。三层互不重叠，套餐与密钥配置都在库里。

### 1. 播种与开租户

```bash
uv run python scripts/seed_plans.py                    # 首次启动：写 free/standard/pro，并给旧租户建档
uv run python scripts/create_tenant.py --id tenant_acme --name "Acme 公司" --plan standard
uv run python scripts/issue_api_key.py --tenant-id tenant_acme --policy-id <上一步打印的 policy id>
uv run python scripts/create_admin.py --username admin   # 控制台第一个管理员（口令只存 scrypt 哈希）
```

`create_tenant.py` 会顺手建一套**最小权限**的密钥配置：能力全关、上限照抄套餐、档位取套餐白名单的第一档；**空白名单 = 一个库都不许查**（不是「全部」）。所以新租户的第一个库要走「建库 → 把 `kb_id` 圈进白名单」两步。

### 2. 打开真实鉴权（`.env`，生产必须）

```dotenv
AUTH_ENABLED=true          # 关着时仍落进 tenant_demo 的内置开发身份
RATE_LIMIT_ENABLED=true    # 两级限流与日配额；需要 Redis
```

### 3. 建第一个库并圈定白名单

```bash
uv run uvicorn app.main:app --port 8000          # 改了 .env 之后要重启服务

# 建库本身不判白名单——白名单管的是「查哪些库」
curl --noproxy '*' -sS -X POST http://127.0.0.1:8000/api/v1/knowledge-bases/create \
  -H "Authorization: Bearer rc_live_<32位hex>" \
  -H "Content-Type: application/json" \
  -d '{"name":"Acme 手册","split_strategy":"size"}'

# 把响应里的 kb_id 圈进这套配置。本步没有改配置的界面（面板入口在第 8 步），用运维脚本：
# 白名单是**替换**语义，多个库就重复 --kb-id；写前会跑与开租户同一个自洽校验。
uv run python scripts/update_key_policy.py --policy-id <policy_id> --kb-id <kb_id>
```

### 4. 用真 Key 打通两扇门

```bash
curl --noproxy '*' -sS -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer rc_live_<32位hex>" \
  -H "Content-Type: application/json" \
  -d '{"kb_ids":["<kb_id>"],"query":"设备证书过期怎么处理"}'
```

成功时 `code: 0`，`metadata.tenant_policy` 回显本次实际生效的 `plan` / `requested_profile` / `mode` / `top_k` / `rerank` / `rewrite` / `kb_count` / `clipped`。MCP 侧用同一把 Key：先 `initialize`（响应头里的 `mcp-session-id` 要带回），再 `notifications/initialized`，然后 `tools/call` 调 `rag_retrieve`，参数仍是 `{"kb_ids":[...],"query":"..."}`；两条入口的鉴权、白名单与档位门禁是同一份实现，拿到的 data 载荷逐字段相同（见 `tests/test_tenant_wiring.py`）。

### 5. 越权回「不存在」

把 `kb_ids` 换成白名单外的库（或别的租户的库）：`code: 20008`、`msg: knowledge base not found`。**不是「无权限」**——可区分的拒绝等于告诉攻击者「这个库存在」。

### 6. 查自己的余量（数据面，给外部程序）

外部程序拿同一把 Key 调 `GET /api/v1/auth/me`，一次拿回自己的租户、套餐快照与今日用量——集成方不必靠「再试一次看会不会超额」来探余量：

```bash
curl --noproxy '*' -sS http://127.0.0.1:8000/api/v1/auth/me \
  -H "Authorization: Bearer rc_live_<32位hex>"
```

返回 `{code, msg, data}`，`data` 三栏：`tenant`（`tenant_id` / `name`）、`plan`（`code` / `name` / 四个能力开关 / 六个数值上限 / `allowed_profiles`）、`usage`（`used` / `limit` / `source`）。用量读的是与控制台 `/console/me` **同一个**租户级日桶，只读不扣：`RATE_LIMIT_ENABLED=true` 时 `source: "daily_counter"`；关着时 `used: null` + `source: "not_tracked"`——平台没在统计，报 0 会骗人。两扇 `/me` 的门**不合并**：这一扇只认 API Key，`/console/me` 只认会话 cookie。

### 7. 套餐改完立刻生效

控制台登录拿 cookie（写操作还要带防跨站的 `x-requested-with: XMLHttpRequest`）：

```bash
curl --noproxy '*' -sS -c cookie.txt -X POST http://127.0.0.1:8000/console/login \
  -H "Content-Type: application/json" -d '{"username":"admin","password":"<口令>"}'

# 先试算（dry_run 默认 true：只算不写，返回会超库数的租户、会失去档位的租户与受影响配置条数）
curl --noproxy '*' -sS -b cookie.txt -X PATCH "http://127.0.0.1:8000/console/plans/<plan_id>" \
  -H "x-requested-with: XMLHttpRequest" -H "Content-Type: application/json" \
  -d '{"operation":"update","allowed_profiles":["speed","balanced"]}'

# 再落库：dry_run=false。落库与审计同一事务，提交后删套餐缓存。
curl --noproxy '*' -sS -b cookie.txt -X PATCH "http://127.0.0.1:8000/console/plans/<plan_id>?dry_run=false" \
  -H "x-requested-with: XMLHttpRequest" -H "Content-Type: application/json" \
  -d '{"operation":"update","allowed_profiles":["speed","balanced"]}'
```

把某个正在被用的档位从白名单里去掉之后，挂着那个档位的 Key **下一次调用**就是 `20013`；把日配额改小，**下一次超限就是 `20014`**——不需要重启，也不需要等缓存过期。运维侧也可以 `uv run python scripts/change_tenant_plan.py --tenant-id tenant_acme --plan free`（它改的是租户的 `plan_id` 指针）。

## 运行与验证（离线）

```bash
uv run ruff check .                                    # 静态检查
uv run ruff format --check tests                      # 格式检查（历史文件暂不纳入）
uv run mypy app                                       # 宽松类型检查
uv run pytest -q                                      # 离线测试：1722 passed, 17 skipped；不连 PG、不发真实模型请求
uv run uvicorn app.main:app --reload --port 8000      # 本地起服务，再按教程跑四条 curl（含列库）
```

CI 在 push / PR 触发，依次跑 `uv sync --all-groups --frozen` → ruff 检查 → 格式检查 → mypy → pytest；不起容器、不发真实模型请求。本机网络受限时可用主 checkout 的解释器跑 `python -m pytest -q` 并设 `PYTHONPATH` 为当前 worktree 绝对路径；`mypy` / `pre-commit` 交给 CI 云端执行。

## 文档

- [项目综述](docs/项目综述.md) —— 定位与边界、要解决的四个实际问题、五个竞争力亮点
- [架构](docs/架构.md) —— 五层全景、九个模块、两条主链路、降级约定、本地起步
- [技术栈](docs/技术栈.md) —— 八个分层、完整版本矩阵、六个版本陷阱、替代方案代价、环境变量
- [设计决策](docs/设计决策.md) —— 五个「一改就全盘皆变」的决策与各自代价，附错误码全表（第 2 步一次性落地，`3xxxx` 预留）
- [ADR-0008 双入口错误契约](docs/adr/0008-dual-entry-error-contract.md) —— 共享错误词汇、各自翻译、协议级不对称及其代价
- [ADR-0010 检索契约](docs/adr/0010-retrieval-contract.md) —— RRF `k=60`、tie-breaker、三种结局、`score` 语义变更与索引配对范围
- [ADR-0011 LangChain 边界](docs/adr/0011-langchain-boundary.md) —— 框架只进 LLM / Embedding，检索链路零框架代码
- [ADR-0017 套餐定义进库](docs/adr/0017-plans-in-database.md) / [ADR-0018 三层授权](docs/adr/0018-three-layer-authority.md) —— 第 7 步的两条核心决策
- [评测验收记录](eval/ACCEPTANCE.md) —— 切块改造的双口径对照、四组交叉与验收留痕
- [GLOSSARY](GLOSSARY.md) —— 领域术语：正名与禁用词

## Agent 协作约定

- **Issue tracker**：GitHub Issues（`Jenlybein/RAG-Core`，`gh` CLI）—— [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md)
- **Triage labels**：`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix` —— [docs/agents/triage-labels.md](docs/agents/triage-labels.md)
- **Domain docs**：single-context，根目录 `GLOSSARY.md` + `docs/adr/` —— [docs/agents/domain.md](docs/agents/domain.md)

约定汇总见 [AGENTS.md](AGENTS.md)。

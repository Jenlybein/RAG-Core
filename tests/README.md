# 测试分层与离线验证矩阵（T7）

本文件是验证矩阵落档：说明每一层测试守什么、当前基线计数、离线 / OCR / 集成三套
运行命令，以及只能由人执行的容器检查与评测复跑方式。早先按 integration tip `30af3cb`
统计的「48 条测试」与旧版「不引入项目级 conftest.py」的说法都已作废；下面的计数来自
当前 tip（第 7 步收尾，2026-11-10 实测：945 条用例，**934 passed / 11 skipped**）。

## 分层

| 层 | 接缝 / 被测对象 | 代表文件（不按票重复列举） |
| --- | --- | --- |
| ① 纯函数单测 | 切块器、4 个 Parser、状态守卫、错误码词汇、CAS 的 `UPDATE ... WHERE` 分支（内存 SQLite 只建 `documents` 一张表）、**RRF 融合真值表与 tie-breaker、检索文本拼装、BM25 查询体、ES mapping、同义词扩展、评测算分与语料约束、鉴权内核、数据范围白名单、档位展开与五道门禁、配置自洽校验、配额计数与静态配额边界、套餐三档对账、口令哈希** | `test_contracts.py`、`test_parsers.py`、`test_markdown_splitter.py`、`test_document_state_machine.py`、`test_indexing_task.py`（`_is_retryable` 真值表）、`test_business_errors.py` 的数据契约、`test_upload_async.py` 的守卫矩阵与 CAS、`test_rrf_fusion.py`、`test_retrieval_text.py`、`test_keyword_query_builder.py`、`test_query_synonyms.py`、`test_eval_metrics.py`、`test_eval_corpus.py`、`test_auth_kernel.py`、`test_data_scope.py`、`test_tier_gating.py`、`test_quota_gate.py`、`test_static_quota.py`、`test_plan_presets.py`、`test_console_password.py` |
| ② ASGI 接缝 | `TestClient` 发真实请求，只看响应体 / 响应头 / cookie 行为 / 日志文件副作用；数据库用 `dependency_overrides` + `tests/fake_db.RecordingSession`、外部能力用 monkeypatch 换掉 | `test_business_errors.py`、`test_exception_boundary.py`、`test_request_id.py`、`test_logging.py`、`test_retrieval_log_hygiene.py`、`test_upload_async.py` 的上传与列表、`test_indexing_failure.py`、`test_dual_entry_skeleton.py`、`test_toolchain.py`、`test_retrieval_mode.py`、`test_hybrid_retrieval.py`、`test_query_optimization_retrieval.py`、`test_tenant_wiring.py`、`test_quota_wiring.py`、`test_console_api.py` |
| ③ Worker 任务 | 任务体：CAS 抢占用两段事务、purge → 写入 → SUCCESS、重试白名单、失败留痕与脱敏文案、租约语义、**双写与索引阶段不降级** | `test_indexing_task.py`（`IndexingService` 流水线 + `_run_indexing`）、`test_indexing_dual_write.py`、`test_index_version.py`、`test_keyword_bulk_failure.py` |
| ④ OCR（asset-gated） | 零路由页不碰原生运行时、缺运行时抛 `ValueError`、扫描件路由到 OCR、整篇全空是质量错误 | `test_ocr_contract.py` + `tests/fixtures/ocr/*.pdf` |
| ⑤ 跨层 e2e | 一条链：上传 → 投递 → 解析 → 切块 → 向量化 → 先删后写 → SUCCESS，切块 provenance 与表格完整性问题只有这一层能发现 | `test_document_pipeline_e2e.py` + `tests/fixtures/markdown/structured_notes.md` |
| ⑥ 真实引擎集成（env-gated） | 真 PostgreSQL 上的 CAS/租约与范围召回、真 Elasticsearch 上的建索引 → 写入 → 中文命中 → 按文档删除 → 计数归零、两套嵌入实现的等价性。**不注入传输层 mock**：那测的是别人库的实现细节，升级就碎 | `test_integration_cas.py`、`test_keyword_search_es.py`、`test_embedding_equivalence.py` |
| ⑦ 依赖元数据守卫 | 硬钉版本组合可解析、`langchain` / `ragas` / `langchain-community` 这类「一升就碎」的链条没被悄悄抬高 | `test_dependency_compat.py` |
| ⑧ 表形状 / 迁移 / 运维脚本（离线） | 直接对 `Base.metadata` 断言七张身份与控制面表的列、类型与「无外键、裸字符串 id」；迁移文件一表一迁移、可升可降对称、不碰既有四张业务表；`seed_plans` / `create_tenant` / `issue_api_key` / `create_admin` 的入参与退出码 | `test_identity_tables.py`、`test_identity_migrations.py`、`test_identity_seeding.py`、`test_identity_ops_scripts.py`、`test_console_tables.py`、`test_console_migrations.py`、`test_console_bootstrap.py` |

第 4 步（双路召回）的测试重心在 ① 与 ②：融合与查询构造是纯函数，走真值表；三种
`mode`、降级矩阵、空结果两种原因、多库配对全部在 ASGI 接缝上按响应体断言。**没有
新增生产接缝**——替身仍然只换工厂函数（大模型 / 嵌入 / 向量存储 / 关键词检索 / 重排）。

第 7 步（多租户）新增的测试落在三处，仍然守着同一条「只看外部可观察行为」的口径：

- **纯函数（①）**：鉴权内核的四种未授权、白名单三种语义（越界 = 不存在、空白 = 关闭、只有内置开发身份显式放开）、五道门禁与两种超限反应（命名档位拒绝 / `custom` 裁剪回显）、两级配额的边界与「成功才记账」、静态配额的三条写路径、配置自洽校验、三档种子逐列对账。
- **ASGI（②）**：同一把真实 Key 打 HTTP 与 MCP，整份 data 载荷逐字段相等；白名单越权、档位越权、QPS / 日配额超限在真实路由上的错误码；控制面的 cookie / 防跨站头 / 试算与落库 / 删缓存 / 审计。
- **表与运维脚本（⑧）**：七张表的形状（裸字符串 id、无外键、无 ORM 关系）、一表一迁移、`seed_plans` 的三条分档口径与四条运维命令的退出码。

**第 7 步唯一新增的接缝是「注入式假 Redis」**（`app.core.redis_client.set_redis_client`），不是 monkeypatch 全局：限流与配额服务通过构造参数收客户端，测试注入内存假实现；默认套件因此不碰 Redis。鉴权那条缝则完全没有新增——它本来就是纯函数，测试直接把假行交给 `resolve_identity`。

## 当前基线

| 场景 | 命令 | 期望 |
| --- | --- | --- |
| 默认离线（**有没有 `.env` 都一样**） | `uv run pytest -q` | `934 passed, 11 skipped`（共收集 945 条） |
| 只跑 OCR（无资产机器） | `uv run pytest -m ocr -q` | `3 skipped, 942 deselected` |
| 装了 OCR 资产的机器跑全量 | `uv run pytest -q` | `937 passed, 8 skipped`（3 条由 skip 变 pass） |
| 只跑 OCR（有资产机器） | `uv run pytest -m ocr -q` | `3 passed, 942 deselected` |
| 只跑集成用例（一个环境变量都没设） | `uv run pytest -m integration -q` | `8 skipped, 937 deselected` |
| 只跑真 PostgreSQL 集成用例 | `RAG_INTEGRATION_DATABASE_URL=... uv run pytest -m integration -q` | `4 passed, 4 skipped, 937 deselected` |

11 条 skip 分**四道门禁**，都不是失败：

- 3 条 OCR 资产门禁（`skipif(not os.environ.get("PDF_INSPECTOR_MODEL_CACHE"))`）；
- 4 条真 PostgreSQL 用例（`RAG_INTEGRATION_DATABASE_URL` 未设时整体跳过）；
- 3 条真 Elasticsearch 用例（`RAG_INTEGRATION_ELASTICSEARCH_URL` 未设时整体跳过；
  其中 1 条守「查询侧分析器在真实 ES 上真的生效」，见第 4 步票 8 的 `_chinese_` 陷阱）；
- 1 条嵌入等价性用例（`RAG_INTEGRATION_EMBEDDING=1` 才跑，它会真的调模型）。

CI 的 `quality` job 不装资产、不起容器，所以那里恒为 `934 passed, 11 skipped`；`ocr`
与 `integration` 是独立 job，由 `workflow_dispatch` 或 `run-ocr` 标签手动触发。

> 数字怎么来的：`uv run pytest -q --collect-only` 收集 945 条，`-m ocr` 收集 3 条、
> `-m integration` 收集 8 条。任何一条测试增减后按这三行重算，不要照抄上一版。

**CI 对齐检查**：把 `.env` 挪走后 `uv run pytest -q` 必须与带 `.env` 的结果完全一致。
`tests/test_business_errors.py` 与 `tests/test_exception_boundary.py` 的 `client` fixture
都把 `app.services.rag_service` 里的三个工厂（嵌入 / 向量存储 / 关键词检索）换成离线
替身——检索链路先向量化再校验知识库可见性，不换掉这些工厂，无 `.env`
（`MODEL_BASE_URL=""`）时会先在 httpx 层抛 `UnsupportedProtocol`，业务失败分支根本走不到
20008。`test_business_errors.py::test_unknown_kb_id_returns_20008_even_when_model_base_url_is_empty`
是这条契约的回归守卫。

## 评测脚本不进默认套件

`scripts/eval_retrieval.py`（票 8 的固定语料 + 24 道题，v2 重新校准）**不是 pytest 用例**，也**不在
默认 `pytest -q` 里**：它必须连真 PostgreSQL、真 Elasticsearch、真嵌入，跑一次要发几十次
网络请求。它靠「不在 `tests/` 下、不被 `testpaths` 收集」这一条天然隔离，而不是靠 marker
——把它做成 pytest 用例只会让「默认离线」这条红线多一个可能被误开的开关。

容易被误解的一点：`tests/test_eval_corpus.py` 与 `tests/test_eval_metrics.py` **在**默认套件里，
但它们**不跑检索**——前者只用真切块器数切片、查标题里有没有编号，后者只做算分真值表。
「语料结构」与「怎么算分」是纯函数，可以在离线同步守住；「真链路上谁赢」只能由脚本测。

复跑方式与清理边界写在脚本文件头：`uv run python scripts/eval_retrieval.py --env-file
<主 checkout>/.env`，产物落 `eval/runs/<时间戳>/`（已 gitignore）。

## 在装了资产的机器上跑 OCR 套件

上游 `pdf-inspector` 的 OCR 路径只在 Linux x64 端到端验证过（macOS / Windows 属
preview）。资产由 `deploy/ocr/fetch_runtime.sh` 下载并校验（任一文件字节数或 SHA-256
对不上就直接失败）：

```bash
bash deploy/ocr/fetch_runtime.sh
export PDFIUM_LIB_PATH="$PWD/.ocr/lib/libpdfium.so"
export ORT_DYLIB_PATH="$PWD/.ocr/lib/libonnxruntime.so"
export PDF_INSPECTOR_MODEL_CACHE="$PWD/.ocr/models"
uv run pytest -m ocr -q
```

三个环境变量就是唯一门禁：`PDFIUM_LIB_PATH` / `ORT_DYLIB_PATH` 指原生库，
`PDF_INSPECTOR_MODEL_CACHE` 指 PP-OCRv6 模型缓存目录（布局必须是
`<cache>/pp-ocrv6-small/oar-ocr-v0.7.0/`）。CI 的 `ocr` job 用同样三个变量，但只在
`workflow_dispatch` 或 PR 带 `run-ocr` 标签时触发。资产目录已被 gitignore / `.dockerignore`
排除，不进提交、不进镜像。

## 只能由人执行的容器检查

本仓库的自动化沙箱**跑不了**这些命令（没有 Docker daemon，镜像下载与构建也不该在
测试里发生），所以它们属于人工验收：

```bash
# 构建预灌 OCR 运行时的 Linux Worker 镜像（见 deploy/Dockerfile）
docker compose -f deploy/docker-compose.yml build worker

# 起基础设施 + Worker（见 deploy/docker-compose.yml 的 worker 服务）
docker compose -f deploy/docker-compose.yml up -d
```

`deploy/Dockerfile` 构建期下载并校验 PDFium / ONNX Runtime / PP-OCRv6，运行期
`OCR_OFFLINE=true` 也能命中缓存；`worker` 服务必须与 API 共用同一个
`DOCUMENT_STORAGE_PATH`，否则 `reparse=true` 找不到原文件。

## 离线约定（默认 `pytest -q` 的硬约束）

- **默认 `pytest -q` 必须保持离线**：不连 PostgreSQL、不连 Elasticsearch、不连 Redis、不发真实
  模型 / embedding 请求、不下载任何原生资产。③⑤ 的数据库依赖都换成了内存替身或只建
  一张表的内存 SQLite；**ES 在默认套件里永远是 `NoopKeywordSearchProvider`**
  （`KEYWORD_SEARCH_PROVIDER` 缺省 `none`），混合模式因此只会走「关键词一路不可用」
  这条降级分支，不会偷偷发网络请求。
- **第 7 步的两个开关默认都关**：`AUTH_ENABLED=false` 走内置开发身份、`RATE_LIMIT_ENABLED=false`
  让限流与配额整段 no-op。默认套件因此不需要真 Key、也不需要 Redis；要测开 / 关两种行为，
  把参数显式传进纯函数或注入假客户端，不改全局配置。
- 主要接缝仍是 ASGI 应用边界（`TestClient` 发真实请求，看响应 + 日志文件副作用）；
  外部能力只换**工厂函数**。第 7 步的例外是**注入式假 Redis**（`set_redis_client`）与
  假数据库行（`RecordingSession` 传给纯函数 / 依赖覆盖）：两者都是显式构造参数或
  `dependency_overrides`，不是 monkeypatch 私有全局，也不会在用例之间留下污染。
- 夹具保持**模块内**定义，不在 `tests/conftest.py` 里养夹具动物园。项目级 `conftest.py`
  只做一件事：在任何 `app.main` 导入前设置 `ENABLE_MCP=false`，避免每个 `TestClient`
  起停 MCP 会话管理器。
- 日志文件断言一律显式 monkeypatch `settings.LOG_DIR` 到 `tmp_path`、重置
  `app.core.logging._configured = False`、再跑 `setup_logging()`——绝不依赖全局默认目录。
- 新增跨层测试（⑤）把 Worker 放在独立线程里消费任务：真实投递由另一个进程的消息循环
  消费，在请求协程里直接 `asyncio.run` 会撞上「event loop already running」。
- 真实引擎用例一律「**没设环境变量就整条 skip**」，不用 `xfail`、不在缺环境时退化成替身：
  替身会把「客户端版本对齐 + 查询体真能执行」这件事测成假绿。

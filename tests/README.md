# 测试分层与离线验证矩阵（T7）

本文件是验证矩阵落档：说明每一层测试守什么、当前基线计数、离线 / OCR / 集成三套
运行命令，以及只能由人执行的容器检查与评测复跑方式。早先按 integration tip `30af3cb`
统计的「48 条测试」与旧版「不引入项目级 conftest.py」的说法都已作废；下面的计数来自
当前 tip（第 11 步收尾，2026-11-13 实测：**1736 条用例，1719 passed / 17 skipped**）。
其中「1736 条用例」是 `uv run pytest --collect-only` 的收集数，「1719 passed / 17 skipped」
是 `uv run pytest -q` 的实测输出，两者相加正好是收集数。

## 分层

| 层 | 接缝 / 被测对象 | 代表文件（不按票重复列举） |
| --- | --- | --- |
| ① 纯函数单测 | 切块器、4 个 Parser、状态守卫、错误码词汇、CAS 的 `UPDATE ... WHERE` 分支（内存 SQLite 只建 `documents` 一张表）、**RRF 融合真值表与 tie-breaker、检索文本拼装、BM25 查询体、ES mapping、同义词扩展、评测算分与语料约束、门禁判据与报告渲染、鉴权内核、数据范围白名单、档位展开与五道门禁、配置自洽校验、配额计数与静态配额边界、套餐三档对账、口令哈希** | `test_contracts.py`、`test_parsers.py`、`test_markdown_splitter.py`、`test_document_state_machine.py`、`test_indexing_task.py`（`_is_retryable` 真值表）、`test_business_errors.py` 的数据契约、`test_upload_async.py` 的守卫矩阵与 CAS、`test_rrf_fusion.py`、`test_retrieval_text.py`、`test_keyword_query_builder.py`、`test_query_synonyms.py`、`test_eval_metrics.py`、`test_eval_corpus.py`、`test_eval_gate.py`、`test_eval_runner.py`、`test_auth_kernel.py`、`test_data_scope.py`、`test_profile_gating.py`、`test_quota_gate.py`、`test_static_quota.py`、`test_plan_presets.py`、`test_console_password.py` |
| ② ASGI 接缝 | `TestClient` 发真实请求，只看响应体 / 响应头 / cookie 行为 / 日志文件副作用；数据库用 `dependency_overrides` + `tests/fake_db.RecordingSession`、外部能力用 monkeypatch 换掉 | `test_business_errors.py`、`test_exception_boundary.py`、`test_request_id.py`、`test_logging.py`、`test_retrieval_log_hygiene.py`、`test_upload_async.py` 的上传与列表、`test_indexing_failure.py`、`test_dual_entry_skeleton.py`、`test_toolchain.py`、`test_retrieval_mode.py`、`test_hybrid_retrieval.py`、`test_query_optimization_retrieval.py`、`test_tenant_wiring.py`、`test_quota_wiring.py`、`test_console_api.py`、`test_trace_identity.py` |
| ③ Worker 任务 | 任务体：CAS 抢占用两段事务、purge → 写入 → SUCCESS、重试白名单、失败留痕与脱敏文案、租约语义、**双写与索引阶段不降级** | `test_indexing_task.py`（`IndexingService` 流水线 + `_run_indexing`）、`test_indexing_dual_write.py`、`test_index_version.py`、`test_keyword_bulk_failure.py` |
| ④ OCR（asset-gated） | 零路由页不碰原生运行时、缺运行时抛 `ValueError`、扫描件路由到 OCR、整篇全空是质量错误 | `test_ocr_contract.py` + `tests/fixtures/ocr/*.pdf` |
| ⑤ 跨层 e2e | 一条链：上传 → 投递 → 解析 → 切块 → 向量化 → 先删后写 → SUCCESS，切块 provenance 与表格完整性问题只有这一层能发现 | `test_document_pipeline_e2e.py` + `tests/fixtures/markdown/structured_notes.md` |
| ⑥ 真实引擎集成（env-gated） | 真 PostgreSQL 上的 CAS/租约与范围召回、真 Elasticsearch 上的建索引 → 写入 → 中文命中 → 按文档删除 → 计数归零、两套嵌入实现的等价性。**不注入传输层 mock**：那测的是别人库的实现细节，升级就碎 | `test_integration_cas.py`、`test_keyword_search_es.py`、`test_embedding_equivalence.py` |
| ⑦ 依赖元数据守卫 | 硬钉版本组合可解析、`langchain` / `ragas` / `langchain-community` 这类「一升就碎」的链条没被悄悄抬高 | `test_dependency_compat.py` |
| ⑧ 表形状 / 迁移 / 运维脚本（离线） | 直接对 `Base.metadata` 断言七张身份与控制面表的列、类型与「无外键、裸字符串 id」；迁移文件一表一迁移、可升可降对称、不碰既有四张业务表；`seed_plans` / `create_tenant` / `issue_api_key` / `create_admin` 的入参与退出码 | `test_identity_tables.py`、`test_identity_migrations.py`、`test_identity_seeding.py`、`test_identity_ops_scripts.py`、`test_console_tables.py`、`test_console_migrations.py`、`test_console_bootstrap.py`、`test_retrieval_trace_migration.py` |

第 4 步（双路召回）的测试重心在 ① 与 ②：融合与查询构造是纯函数，走真值表；三种
`mode`、降级矩阵、空结果两种原因、多库配对全部在 ASGI 接缝上按响应体断言。**没有
新增生产接缝**——替身仍然只换工厂函数（大模型 / 嵌入 / 向量存储 / 关键词检索 / 重排）。

第 7 步（多租户）新增的测试落在三处，仍然守着同一条「只看外部可观察行为」的口径：

- **纯函数（①）**：鉴权内核的四种未授权、白名单三种语义（越界 = 不存在、空白 = 关闭、只有内置开发身份显式放开）、五道门禁与两种超限反应（命名档位拒绝 / `custom` 裁剪回显）、两级配额的边界与「成功才记账」、静态配额的三条写路径、配置自洽校验、三档种子逐列对账。
- **ASGI（②）**：同一把真实 Key 打 HTTP 与 MCP，整份 data 载荷逐字段相等；白名单越权、档位越权、QPS / 日配额超限在真实路由上的错误码；控制面的 cookie / 防跨站头 / 试算与落库 / 删缓存 / 审计。
- **表与运维脚本（⑧）**：七张表的形状（裸字符串 id、无外键、无 ORM 关系）、一表一迁移、`seed_plans` 的三条分档口径与四条运维命令的退出码。

**第 7 步唯一新增的接缝是「注入式假 Redis」**（`app.core.redis_client.set_redis_client`），不是 monkeypatch 全局：限流与配额服务通过构造参数收客户端，测试注入内存假实现；默认套件因此不碰 Redis。鉴权那条缝则完全没有新增——它本来就是纯函数，测试直接把假行交给 `resolve_identity`。

第 9 步（观测与评测）新增的测试仍落在既有三层。**新增的生产接缝逐条列在这里**——说「没有新增」是错的，说「只有一处」也是错的，所以干脆把清单摆出来，下一个人加缝时照着更新：

- `app.observability.set_trace_sink` / `reset_trace_sink`：**进程级**注入点，与第 7 步的注入式假 Redis 同一套做法（把「去向」做成可注入，而不是 monkeypatch 全局）。`tests/conftest.py` 的 autouse 夹具靠它把每个用例的观测去向固定成 `none`、并在结束时清场；没有这条缝，「这台机器上恰好起着观测栈」就会渗进套件的结论。
- `rag_service.retrieve(..., stage_timer=...)`：计时器可注入（默认真计时器），用来断言「六段都开过」而不必真的等。
- `retrieval_waterfall.waterfall_of(..., remote_reader=...)`：读观测服务那一步由组合根显式交进来，测试才能换掉「去不去问栈」。
- `console_deps.get_langfuse_reader`：路由的 `Depends`，测试用 `dependency_overrides` 换掉它。
- `LangfuseTraceSink(client=...)` / `read_stage_spans(client=...)`：SDK 客户端可注入，观测路径因此能在不发网络、不起栈的情况下被逐项断言。
- `JsonlTraceSink(clock=...)`：时钟可注入，轮转与保留期不必等真实时间。

前六条里，**只有第一条是进程级的**（会影响同一进程里后续所有用例，所以必须由 autouse 夹具兜底清场）；其余都是构造参数或 `Depends`，作用域限于单个调用——这也是本仓一贯的偏好：优先把依赖做成参数，而不是改全局状态。

**第 10 步（性能优化）新增的接缝**，同样是「能做成参数就不改全局」，但这次有三条**必须**由 autouse 夹具清场的进程级状态：

- `rag_service.retrieve(..., rerank_factory=...)` → `hybrid_retrieve` → `rerank_stage(provider_factory=...)`：重排实现由组合根交进来，评测才能在不碰生产工厂的前提下装旧的大模型重排做对照。
- `Qwen3RerankProvider(client=...)`：HTTP 客户端可注入（测试用 `httpx.MockTransport`，不发网络）。
- `LangChainEmbedding(embeddings_factory=...)` / `LangChainLLMProvider(chat_model_factory=...)`：模型每次现装，工厂可换。
- `app.core.http_clients.reset_http_clients()` 与 `app.db.worker_loop.reset_worker_loop()`：**进程级**，前后各清一次——那一槽里挂着一个属于已关闭循环的客户端（`reset` 只丢引用，不尝试关闭）、或一个属于上一个用例的常驻循环，都会让下一个用例莫名其妙地红。（`app.core.loop_pool.reset_retrieval_pool()` 在 #127 随执行池一起删除；按 loop 的注册表在 #134 收敛成一个槽，见 ADR-0024。）
- `reset_llm_provider_cache` / `reset_rerank_provider_cache` / `reset_embedding_provider_cache`：三个 Provider 工厂按配置指纹缓存，同样每例清场。
- `tests/conftest.py` 另外**强制** `RERANK_BASE_URL=""`：开发机 `.env` 里写着真实端点与真 key，不钉死的话任何「打开重排」的用例都会真发一次**计费**请求。

- **纯函数（①）**：`trace_id` 派生恒等、六段计时与排序、门禁判据（相对退化 / 未评分排除 / 分题型）、旁证聚合与部分失败、导出筛选 / 合并 / 产物形状、反馈错误码文案 → `test_trace_identity.py`、`test_observability_sink.py`、`test_eval_gate.py`、`test_eval_runner.py`、`test_side_evidence.py`、`test_build_answer_set.py`、`test_export_eval_cases.py`、`frontend/src/utils/__tests__/feedback.spec.ts`。
- **ASGI（②）**：写库失败仍返回 `trace_id`、观测旁路失败只 warning、瀑布图与反馈的归属校验、评分反查、控制台旁证触发 → `test_retrieval_observability.py`、`test_feedback.py`、`test_console_waterfall.py`、`test_console_trace_lookup.py`。
- **表形状 / 迁移（⑧）**：`retrieval_traces` 十列与逐列原文哨兵、`retrieval_feedback` 一表一迁移 → `test_retrieval_trace_migration.py`、`test_console_migrations.py`、`test_identity_migrations.py`。

四条回归红线（响应逐字节不变、`trace_id` 恒非空、归属索引无原文、观测挂掉反馈仍成功）分别钉在上述文件里，汇总见 `docs/step9-acceptance.md`。

**第 11 步（端到端异步）新增的接缝**共三条，其中两条是**进程级**状态（conftest 每例前后清场）；票 #132 把**写路径**也搬进同一栈之后，请求路径上只剩 `get_async_db` / `get_async_transaction` 两个异步入口，同步的 `get_db` / `get_transaction` 已从代码里删除：

- `app.db.session_factory.set_async_session_factory`（**进程级**）：异步会话工厂的注入点，与同步那条 `set_session_factory` 是孪生（刻意分开，见该模块文档）。`tests/conftest.py` 的 autouse 夹具把它固定成 `AsyncRecordingSession` 并在结束时复位——不注入的话，反馈那条**旁路短事务**会真的去连 PostgreSQL，套件就会取决于「这台机器今天开没开库」。
- `Depends(get_async_db)` + `Depends(get_async_transaction)`（`app/db/async_session.py`）：读 / 写两条请求级入口，控制面读、写、登录与数据面全走它们。用例用 `tests/fake_db.override_all_db(app, session)` 一次把两个异步入口收口：覆写 `get_async_db`，并清掉 `get_async_transaction` 上可能残留的 no-op 覆写、让它走**生产实现**（它靠 FastAPI 的依赖缓存取同一个会话）。于是只读、写入与登录看到的是同一个假库，没有第二套同步入口要挡。
- `app.db.worker_loop.get_worker_loop()`（**进程级**，conftest 每例前后 `reset_worker_loop()`）：任务体把工厂投进常驻循环。`test_indexing_task.py` / `test_document_pipeline_e2e.py` / `test_http_clients.py` 用的是**真的** `WorkerLoop`，只换会话工厂与仓储——于是「投工厂、在常驻循环里跑、客户端跨任务复用、关停才关」这条接线本身也在被断言。

对应的替身与契约：`tests/fake_db.AsyncRecordingSession`（同一份语义的异步面；`add` 是**同步**方法，因为真 `AsyncSession` 上只有它没变成 `await`）与 `tests/test_session_double_contract.py`。请求路径（含 `/console/retrieval-traces*` 与 `/api/v1/rag/feedback`）已全部走异步会话；`test_console_waterfall.py` / `test_console_trace_lookup.py` 的夹具把行放进**同一个假库**，登录写下的会话行在只读请求里必须读得到——「这条读退回同步会话」会让用例当场红，而不是悄悄照样绿。

收口时删掉的过渡脚手架（`app/db/dual_session.py`、`app/core/sync_bridge.py`、`tests/test_dual_session.py`）与替身函数（`fake_db.async_mirror` / `share_rows_with` / `no_transaction`）都已不在代码里，`app/core/loop_pool.py` 也随 #127 的执行池一起删除；上面只列**现役**接缝。

写路径的 SQL 级证据不在离线套件里（内存 SQLite 只覆盖同步孪生）：`tests/test_integration_cas.py` 有两条 `AsyncSession` 上的 CAS / 重试生命周期用例，`tests/test_integration_worker_loop.py` 有一条并发 24 任务、池上限 15 的连接数用例，都走 `RAG_INTEGRATION_DATABASE_URL` 门禁。

## 当前基线

| 场景 | 命令 | 期望 |
| --- | --- | --- |
| 默认离线（**有没有 `.env` 都一样**） | `uv run pytest -q` | `1719 passed, 17 skipped`（共收集 1736 条） |
| 只跑 OCR（无资产机器） | `uv run pytest -m ocr -q` | `3 skipped, 1733 deselected` |
| 装了 OCR 资产的机器跑全量 | `uv run pytest -q` | `1722 passed, 14 skipped`（3 条由 skip 变 pass） |
| 只跑 OCR（有资产机器） | `uv run pytest -m ocr -q` | `3 passed, 1733 deselected` |
| 只跑集成用例（一个环境变量都没设） | `uv run pytest -m integration -q` | `14 skipped, 1722 deselected` |
| 只跑真 PostgreSQL 集成用例 | `RAG_INTEGRATION_DATABASE_URL=... uv run pytest -m integration -q` | `10 passed, 4 skipped, 1722 deselected` |

17 条 skip 分**四道门禁**，都不是失败：

- 3 条 OCR 资产门禁（`skipif(not os.environ.get("PDF_INSPECTOR_MODEL_CACHE"))`）；
- 10 条真 PostgreSQL 用例（`RAG_INTEGRATION_DATABASE_URL` 未设时整体跳过：索引权 CAS 6 条 + Worker 常驻循环探针 4 条）；
- 3 条真 Elasticsearch 用例（`RAG_INTEGRATION_ELASTICSEARCH_URL` 未设时整体跳过；
  其中 1 条守「查询侧分析器在真实 ES 上真的生效」，见第 4 步票 8 的 `_chinese_` 陷阱）；
- 1 条嵌入等价性用例（`RAG_INTEGRATION_EMBEDDING=1` 才跑，它会真的调模型）。

CI 的 `quality` job 不装资产、不起容器，所以那里恒为 `1719 passed, 17 skipped`；`ocr`
与 `integration` 是独立 job，由 `workflow_dispatch` 或 `run-ocr` 标签手动触发。

> 数字怎么来的：`uv run pytest -q --collect-only` 收集 1736 条，`-m ocr` 选中 3 条、
> `-m integration` 选中 14 条。收集 = passed + skipped（1719 + 17 = 1736；装了 OCR 资产时
> 1722 + 14 = 1736），deselect = 收集 − 选中（`-m ocr`：1736 − 3 = 1733；
> `-m integration`：1736 − 14 = 1722）。任何一条测试增减后按这三行重算，不要照抄上一版。

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

第 9 步的门禁（`#107`）把这件事收成一条命令，仍然是**脚本而不是 pytest 用例**：

```powershell
uv run python scripts/eval_runner.py validate-dataset        # 离线：标注能不能定位
uv run python scripts/eval_runner.py validate-config         # 离线：实验配置与基线可读性
uv run python scripts/eval_runner.py run --env-file <主 checkout>\.env
uv run python scripts/eval_runner.py report --run eval/runs/<UTC 时间戳>
uv run python scripts/eval_runner.py report --run eval/baseline --select metrics.overall.context_precision
```

- **纯函数部分**（`scripts/eval_gate.py`：算分口径、未评分排除、相对判据、报告渲染、
  数据集与配置校验）**在**默认套件里，见 `tests/test_eval_gate.py`。它 import 时不碰
  数据库与网络；ragas 的 ID 版双指标也只吃 chunk id，所以「门禁怎么算」可以离线钉死。
- **运行器本身**（`scripts/eval_runner.py`）的四个子命令里，`validate-*` 与 `report`
  离线可跑，由 `tests/test_eval_runner.py` 用临时目录 + 假数据覆盖；`run` 必须连真
  PostgreSQL / Elasticsearch / 嵌入，只能真跑（跑不起来时它会留下一份「未跑出数字」的
  失败档案，绝不编数字）。
- 归档：`eval/runs/<UTC 时间戳>/`（人读 `report.md` + 机读 `results.json` + 环境快照
  `environment.json`，已 gitignore）；随仓库走的固定基线在 `eval/baseline/`（同一套三份
  文件，`report.md` 里每个数字都附复现命令），由 `run --update-baseline` 或
  `report --run <归档> --update-baseline` 重钉。

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
  只做**四件事**：在任何 `app.main` 导入前设置 `ENABLE_MCP=false` 与 `KEYWORD_SEARCH_PROVIDER=none`；
  把**进程级注入点**换成离线替身——`app.db.session_factory`（拿不到 `Depends` 的路径用，
  含 #101 的归属索引旁路写入）、运行时配置缓存（每 30 秒一次的覆盖层刷新会先读缓存，
  本机 Redis 里留着旧覆盖时会让跑得够久的套件随机红几条）与套餐缓存的默认实现
  （`plan_service.set_default_plan_cache`：登录与「我的视图」在缓存缺省时会走它，
  不换就会去连真 Redis）；以及**底层任务发布器**（`index_dispatch.set_task_publisher` /
  `side_evidence_dispatch.set_side_evidence_publisher`，票 #137 评审后加的）。
  这些都是既有的注入点，不是 monkeypatch 私有全局。

  **发布器这一条为什么要 autouse**：漏桩的用例在开发机上会静默连上本机 Redis、一路绿，
  到了没有 Redis 的 CI 才变成 500 + `Connection refused`。桩打在**最底层** `apply_async`
  处，于是 `enqueue_document` 里 kwargs 组装与 `task_id` 形状仍由真实代码执行——
  「投递接线」不会被离线夹具盖掉（守卫见 `tests/test_write_atomicity.py` 的两条用例）。
- **读写登录只用同一个 `AsyncSession`**（第 11 步收口之后）：控制面的读、写、登录全在异步栈
  上，`tests/fake_db.override_all_db(app, session)` 一次把 `get_async_db` /
  `get_async_transaction` 两个异步入口收口到同一个假库——登录刚写下的会话行，下一个只读
  请求自然读得到。同步栈与「让两套替身共享行」的镜像函数都已删除，不再有第二套写法要补。
- 日志文件断言一律显式 monkeypatch `settings.LOG_DIR` 到 `tmp_path`、重置
  `app.core.logging._configured = False`、再跑 `setup_logging()`——绝不依赖全局默认目录。
- 新增跨层测试（⑤）把 Worker 放在独立线程里消费任务：真实投递由另一个进程的消息循环
  消费，在请求协程里直接 `asyncio.run` 会撞上「event loop already running」。
- 真实引擎用例一律「**没设环境变量就整条 skip**」，不用 `xfail`、不在缺环境时退化成替身：
  替身会把「客户端版本对齐 + 查询体真能执行」这件事测成假绿。

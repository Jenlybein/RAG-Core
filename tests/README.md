# 测试分层与离线验证矩阵（T7）

本文件是 T7 的验证矩阵落档：说明每一层测试守什么、当前基线计数、离线 / OCR 两套
运行命令，以及只能由人执行的容器检查。早先按 integration tip `30af3cb` 统计的
「48 条测试」与旧版「不引入项目级 conftest.py」的说法都已作废——T0–T6 的测试已在
集成分支合并，下面的计数来自当前 tip。

## 五层测试

| 层 | 接缝 / 被测对象 | 代表文件（不按 T 票重复列举） |
| --- | --- | --- |
| ① 纯函数单测 | 切块器、4 个 Parser、状态守卫、错误码词汇、CAS 的 `UPDATE ... WHERE` 分支（内存 SQLite 只建 `documents` 一张表） | `test_contracts.py`、`test_parsers.py`、`test_markdown_splitter.py`、`test_document_state_machine.py`、`test_indexing_task.py`（`_is_retryable` 真值表）、`test_business_errors.py` 的数据契约、`test_upload_async.py` 的守卫矩阵与 CAS |
| ② ASGI 接缝 | `TestClient` 发真实请求，只看响应体 / 响应头 / 日志文件副作用；数据库用 `dependency_overrides`、外部能力用 monkeypatch 换掉 | `test_business_errors.py`、`test_exception_boundary.py`、`test_request_id.py`、`test_logging.py`、`test_retrieval_log_hygiene.py`、`test_upload_async.py` 的上传与列表、`test_indexing_failure.py`、`test_dual_entry_skeleton.py`、`test_toolchain.py` |
| ③ Worker 任务 | 任务体：CAS 抢占用两段事务、purge → 写入 → SUCCESS、重试白名单、失败留痕与脱敏文案、租约语义 | `test_indexing_task.py`（`IndexingService` 流水线 + `_run_indexing`） |
| ④ OCR（asset-gated） | 零路由页不碰原生运行时、缺运行时抛 `ValueError`、扫描件路由到 OCR、整篇全空是质量错误 | `test_ocr_contract.py` + `tests/fixtures/ocr/*.pdf` |
| ⑤ 跨层 e2e | 一条链：上传 → 投递 → 解析 → 切块 → 向量化 → 先删后写 → SUCCESS，切块 provenance 与表格完整性问题只有这一层能发现 | `test_document_pipeline_e2e.py` + `tests/fixtures/markdown/structured_notes.md` |

## 当前基线计数

| 场景 | 命令 | 期望 |
| --- | --- | --- |
| 默认离线（**有没有 `.env` 都一样**） | `uv run pytest -q` | `165 passed, 3 skipped` |
| 只跑 OCR（无资产机器） | `uv run pytest -m ocr -q` | `3 skipped, 165 deselected` |
| 装了 OCR 资产的机器跑全量 | `uv run pytest -q` | `168 passed`（④ 的 3 条由 skip 变 pass） |
| 只跑 OCR（有资产机器） | `uv run pytest -m ocr -q` | `3 passed, 165 deselected` |

3 条 skip 全部来自 ④ 的资产门禁（`skipif(not os.environ.get("PDF_INSPECTOR_MODEL_CACHE"))`），
不是失败。CI 的 `quality` job 不装资产，所以那里恒为 `165 passed, 3 skipped`。

**CI 对齐检查**：把 `.env` 挪走后 `uv run pytest -q` 必须与带 `.env` 的结果完全一致。
`tests/test_business_errors.py` 与 `tests/test_exception_boundary.py` 的 `client` fixture
都把 `app.services.rag_service.get_embedding_provider` 换成离线替身——`retrieve` 先
`embed_query` 再校验知识库可见性，不换掉这个工厂，无 `.env`（`MODEL_BASE_URL=""`）时
会先在 httpx 层抛 `UnsupportedProtocol`，业务失败分支根本走不到 20008。
`test_business_errors.py::test_unknown_kb_id_returns_20008_even_when_model_base_url_is_empty`
是这条契约的回归守卫。

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

- **默认 `pytest -q` 必须保持离线**：不连 PostgreSQL、不发真实模型 / embedding 请求、
  不下载任何原生资产。③⑤ 的数据库依赖都换成了内存替身或只建一张表的内存 SQLite。
- 唯一接缝仍是 ASGI 应用边界（`TestClient` 发真实请求，看响应 + 日志文件副作用）；
  外部能力只换**工厂函数**，不新增生产接缝。
- 夹具保持**模块内**定义，不在 `tests/conftest.py` 里养夹具动物园。项目级 `conftest.py`
  只做一件事：在任何 `app.main` 导入前设置 `ENABLE_MCP=false`，避免每个 `TestClient`
  起停 MCP 会话管理器。
- 日志文件断言一律显式 monkeypatch `settings.LOG_DIR` 到 `tmp_path`、重置
  `app.core.logging._configured = False`、再跑 `setup_logging()`——绝不依赖全局默认目录。
- 新增跨层测试（⑤）把 Worker 放在独立线程里消费任务：真实投递由另一个进程的消息循环
  消费，在请求协程里直接 `asyncio.run` 会撞上「event loop already running」。

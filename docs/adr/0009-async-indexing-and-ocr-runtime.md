# 0009. 异步索引用 Celery + `asyncio.run`，OCR 用 pdf-inspector 1.25.2 只跑 Linux 容器

- 状态：已采纳
- 日期：2026-11-06
- 相关：`GLOSSARY.md`「索引链路」「重建」「清除」、`docs/架构.md` ⑥异步任务、`docs/技术栈.md` 文档解析、`app/celery_app.py`、`app/tasks/indexing.py`、`app/providers/parsers/pdf.py`、`deploy/Dockerfile`

## 一句话决定

文档索引继续用 **Celery + Redis** 做异步任务，在同步 Worker 里用 `asyncio.run` 包 async Provider；PDF/扫描件解析升级到 **`pdf-inspector==1.25.2`**，Worker 只跑 **Linux x64 容器**，OCR 运行时与 PP-OCRv6 模型在构建期预灌进镜像，运行期不联网。

## 为什么

两件事都卡在同一个前提上：索引任务必须能把「async 的 embedding 调用」放进「同步的 worker 进程」里跑，而 PDF 解析必须能在生产环境里处理扫描件。

- `EmbeddingProvider.embed_texts` 是 `async def`，Celery 是同步进程模型，所以任务体里必须有 `asyncio.run(...)` 这一层壳。
- 原钉的 `pdf-inspector==0.2.6` **完全没有 OCR**：它导出的只有 `process_pdf` / `process_pdf_bytes` / `detect_pdf*` / `classify_pdf*` / `extract_*`，docstring 自称 "all without OCR / no ML models"。于是「教程里讲 OCR、依赖里却没有 OCR」成了文档与代码同时漂移的一处。
- 1.25.2 才把 OCR 做进同一条管线，字节入口是 `process_pdf_with_ocr_bytes`；`process_pdf_with_ocr` 的第一个位置参数是**文件路径**，传 bytes 会失败。这是升级时最容易踩的一脚。

## 决定一：Celery + `asyncio.run`，不换 arq，也不做 PostgreSQL 任务表

**考虑过的替代方案**：

| 方案 | 为什么不选 |
| --- | --- |
| `arq` | async 原生，还白送任务去重键（`_job_id`）与「悲观执行」，技术契合度更高；但作者在 2025-10 宣布 **maintenance-only**（只修安全漏洞、不再演进）。把一个已冻结的框架接进教学主线，后续每一步都要自己维护它。 |
| PostgreSQL 任务表（`SELECT … FOR UPDATE SKIP LOCKED`） | async 原生、零新中间件；但要推翻技术栈 / 架构 / 教程三处既有决定，而且后续的配额与限流仍然要 Redis，省不掉中间件。 |

**选择的理由**：项目处在「照着既有教程复现 + 学习」的阶段，Celery 是教程主线，换框架等于推倒这一章的核心叙事。更关键的是，`asyncio.run` 的经典坑在本项目**不会发生**：

- Provider 每次 `_post` 都新建 `httpx.AsyncClient`（`async with`），没有跨事件循环复用的异步连接池；
- 数据库是同步 `Session`，任务里用独立 `SessionLocal()`。

所以「每任务新建 / 销毁事件循环」在这里只是丑，不是不安全。

**实现形状**（`app/celery_app.py` / `app/tasks/indexing.py`）：

- `Celery("rag_center", broker=settings.CELERY_BROKER_URL, backend=settings.CELERY_RESULT_BACKEND, include=["app.tasks.indexing"])`
- `task_acks_late=True`（至少一次投递）、`worker_prefetch_multiplier=1`、`task_track_started=True`、`broker_connection_retry_on_startup=True`
- 任务 `bind=True, max_retries=2, default_retry_delay=10`，任务体 `asyncio.run(IndexingService().index_existing_document(...))`
- 并发去重靠数据库 CAS（`try_claim_for_indexing`），**不靠 Celery 的 `task_id`**；`acks_late` 与 CAS 是配套的一对

**代价**：

- 每个任务新建 / 销毁一个事件循环，有固定开销；对分钟级的 embedding 任务可以忽略，但不要拿它做高频短任务。
- **Windows 上 worker 不可用**（Celery 官方 FAQ 明确不支持），因此 Worker 一律跑 Linux 容器，Windows 本机只跑 API / 纯文字 PDF。
- `include` 漏写时，任务会被当作 `Received unregistered task` 静默消费——这是运维排障里最容易误判成「Redis 挂了」的一类故障。

**翻案条件**：脱离教程包袱，或 `asyncio.run` 这一层成为真实瓶颈时，`arq` 是第一顺位重评对象；如果那时 arq 仍处于 maintenance-only，就改评估「PostgreSQL 任务表 + 单 Redis 用于限流」。

## 决定二：OCR 用 `pdf-inspector==1.25.2` + Linux 容器，不自研 OCR

**考虑过的替代方案**：

| 方案 | 为什么不选 |
| --- | --- |
| 继续用 `pymupdf`（原技术栈口径） | PDF 抽文字可以，**扫描件不支持**；要 OCR 就得自己接一套识别 + 版面分析，那是另一个项目。 |
| 自研 OCR | 不现实：模型、预处理、版面分析、置信度策略全要自己做，且没有评测基线。 |
| 托管 OCR 服务 | 会引入新的外部供应商与网络依赖，且本步的目标是「扫描件也能进库」，不是「再签一个供应商」。 |

**选择的理由**：

- 1.25.2 是当前稳定版，并修了影响 OCR 质量的缺陷（Form-XObject `/BBox` 空白渲染、ToUnicode CMap、上下标）；
- `requires_dist` 为空，升级**零新增 Python 传递依赖**；
- PDFium / ONNX Runtime / PP-OCRv6 三个运行时资产在 Linux x64 上有完整发布，镜像构建期可以按 SHA-256 校验后预灌；
- Linux x64 是上游**唯一端到端 CI 覆盖**的路径；Windows x64 资产齐全但上游标 **preview**。

**关于 Docker 要说清楚**：上游文档**从未提到 Docker**。「Worker 只跑 Linux 容器」是我们基于「唯一被端到端验证的 OS」做出的工程选择，不是上游承诺。写进运维手册时别把它说成「官方要求」。

**失败二分**（代码里必须按这个口径）：Python 绑定把所有 Rust 错误都拍平成同一个 `ValueError`，没有专用异常类。所以：

- **拿到 `OcrPdfResult`** = 成功。低置信 / 不完整是**质量信息**，在 `pages_recommending_hosted` 与每页 `provenance.warnings` 里，写进 metadata 继续处理；
- **没拿到（抛 `ValueError`）** = 部署 / 执行问题（缺 PDFium / ONNX Runtime、模型下载或校验失败、OCR 执行错误），冒泡到任务层记 ERROR、告警、**不重试**；
- 整篇页面都空，才抛 `DOCUMENT_CONTENT_EMPTY`。

不要解析 `ValueError` 的文案来分类——上游文案一升级就碎。

**代价**：

- 镜像体积与构建时间增加（两个原生库 + 约 31 MB 模型）；
- Windows 本机不能跑 Worker，OCR 验证必须进容器或 Linux CI；
- OCR 用例需要三个运行时资产，所以默认 `pytest -q` 里是 **skip**（`@pytest.mark.ocr` + 资产存在才跑），独立 CI job 才真跑；
- 非 Linux 路径（尤其 Intel macOS，ORT 1.27.0 不发 Intel mac 包）不在支持范围。

**翻案条件**：上游把 Windows 路径转正（preview 摘掉、并有等价 smoke job），或改用托管 OCR 服务时。

## 接着验证

```bash
# 决策一：Worker 入口与任务注册
docker compose -f deploy/docker-compose.yml logs -f worker   # 期望 celery@... ready.
# 决策二：OCR 字节入口与运行时
uv run python -c "import pdf_inspector; print(pdf_inspector.process_pdf_with_ocr_bytes)"
bash deploy/ocr/fetch_runtime.sh                              # 校验尺寸与 SHA-256
```

对照阅读：

- `app/celery_app.py`：应能看到 `include=["app.tasks.indexing"]` 与 `task_acks_late=True`；
- `app/providers/parsers/pdf.py`：应能确认调用的是 `process_pdf_with_ocr_bytes`，且**没有** `try/except ValueError`；
- `deploy/Dockerfile`：应能看到构建期下载 + SHA-256 校验，以及 `OCR_OFFLINE=true` 仍能命中模型缓存。

**自检**：一个没读过本项目的人，能否只看这份 ADR 说清「为什么 Worker 不能跑在 Windows 上」「为什么 OCR 失败不能靠异常类型分类」？说不清就补大白话，不要补术语。

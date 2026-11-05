# T7 · 验收缺口分析（测试分层说明）

本票是整体验收闸门，**不重写、不复制 T1..T6 已落地的测试**。以 ticket #8 的
8 条验收项 + spec #1 的用户故事 32（日志不落检索原文 / 文档内容 / API Key）为
清单，逐条核对 integration tip `30af3cb` 上已有的 48 条测试：

| # | 验收项 | 现状 | 证据（已有测试，均不重复新增） |
| --- | --- | --- | --- |
| 1 | 本机真实链路三条调用（创建知识库 / 传文档 / 检索）全部成功 | **不进离线测试**：需本地 PostgreSQL + embedding key + 真实模型额度，属人工验收 | 本地人工验证清单（项目维护记录，仓库外）收三条 `curl --noproxy '*'` 与逐项打勾表；`tests/test_toolchain.py::test_app_imports_without_database_connection` 只守「导入不连库」，不是真实链路 |
| 2 | 业务失败 / 参数校验失败 / 未捕获异常三类分支 | **已完全覆盖** | `test_business_errors.py`（`20008` / 跨租户同码 / `20002` / bytes 输入不崩）、`test_exception_boundary.py`（`500` + `50000`、堆栈只进日志、内部细节不回显）、两份文件里的「业务/校验仍是 HTTP 200」无回归断言 |
| 3 | 包壳边界（探活 / 接口文档 / 404 保持 FastAPI 原生） | **已完全覆盖** | `test_exception_boundary.py::test_health_docs_openapi_and_wrong_path_stay_native`（`/health`、`/docs`、`/openapi.json`、`/nope`、`/api/v1/does-not-exist`）、同文件 `test_500_envelope_does_not_leak_into_docs_or_health`、`test_business_errors.py::test_health_and_unknown_path_are_not_wrapped` |
| 4 | 三路日志的格式 / 分级 / 滚动 | **已完全覆盖** | `test_logging.py`：JSONL 可解析且含 `time/level/logger/msg` + 业务 ID、`error.log` 只收 ERROR、uvicorn 三 logger 落盘、小阈值滚动不丢内容、幂等不重复、级别与目录可配、import 顺序无关；控制台彩色输出由 `ConsoleRenderer` 配置保证 |
| 5 | 错误码契约守卫（唯一值 / 分段 / 权威全表一致） | **已完全覆盖** | `test_business_errors.py::test_error_codes_match_authoritative_table`（独立来源 `docs/设计决策.md` 全表）、`test_error_code_values_are_unique`、`test_error_codes_keep_responsibility_segments`（含 `3xxxx` 无成员） |
| 6 | 请求关联标识的生成 / 净化 / 回传 | **已完全覆盖** | `test_request_id.py`：每条响应带头、合法原样回传、7 种非法/缺失值被替换且非法值不进日志、同一请求全部日志共享 id、挂载子应用覆盖、流不被缓冲、不进 body；500 分支由 `test_exception_boundary.py::test_500_header_and_error_log_share_one_id_without_incoming_id` 与 `test_500_echoes_legal_incoming_request_id` 补齐 |
| 7 | 索引链路失败留痕 + 脱敏 | **已完全覆盖**（T6 显式交付，非 #8 验收项但属 spec） | `test_indexing_failure.py`：`code=0 + status=2` 契约不变、ERROR 日志带 `document_id/tenant_id/kb_id` 与堆栈、`error_message` 固定脱敏文案、失败行回写可重试、成功路径对照 |
| 8 | 验证矩阵逐条打勾并留档 / CI 不起容器不发真实请求全绿 | **矩阵在仓库外落档**；CI 离线由 `pytest -q` 全绿 + `.github/workflows/ci.yml` 无容器步骤保证 | 本文件 + 仓库外矩阵文件；CI 实际链路为 `uv sync --all-groups --frozen` → `ruff check .` → `ruff format --check tests` → `mypy app` → `pytest -q` |

## 唯一真正的缺口（T6 明确标记，本票补上）

**真实检索请求的日志卫生**：spec #1 用户故事 32 / CONTEXT.md §4 要求应用日志里
不出现**检索文本原文、文档内容、API Key**。T6 的
`test_application_logs_never_carry_content_query_or_api_key` 只在**上传链路**上、
且只对**自己产生的那两条记录**（`document indexed` / `document indexing failed`）
做了结构化断言，并明确记录「端到端检索日志卫生未覆盖」。

本票因此只新增 `tests/test_retrieval_log_hygiene.py`：在 ASGI 边界发一次**真实
检索请求**（`POST /api/v1/rag/retrieve`），用 `dependency_overrides` 换掉数据库、
monkeypatch 换掉知识库仓储 / embedding / 向量库工厂（不发真实模型请求、不连 PG），
然后断言**整份 `app.log` 与 `error.log` 的原始字节**里都不出现检索原文、召回切片
内容、API Key；同时正向断言响应体确实带回了这些内容（证明哨兵真的走过了整条
链路，避免「因为没数据所以断言恒真」的假绿）。

## 离线约定

- 唯一接缝仍是 ASGI 应用边界（`TestClient` 发真实请求，看响应 + 日志文件副作用）。
- 日志文件断言一律显式 monkeypatch `settings.LOG_DIR` 到 `tmp_path`、重置
  `app.core.logging._configured = False`、再跑 `setup_logging()`——T2 的 teardown
  会把全局 LOG_DIR 留在 pytest 临时目录，绝不依赖全局默认目录。
- 不引入项目级 `tests/conftest.py`（与 T2 模块级 autouse teardown 顺序难控）。
- CI 不起容器、不发真实模型/embedding 请求；`mypy` / `pre-commit` 因本机网络
  未实跑，配置已就位，由 CI 在云端执行。

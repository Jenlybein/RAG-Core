# 第 9 步 · 观测与评测验收矩阵

> 日期：2026-11-11 ｜ 集成分支：`step9/observability-eval`
> 本文只记**已经跑过的**数字与命令。每一个「通过」后面都站着一条可复现的命令，
> 不写「应该可以」「理论上是」。教程里的验收清单见 `03-实现开始/00-复现总指引/01-路线图.md`，
> 由 #112 同步到与本文件一致。

## 一、全量基线（合并态，一次性跑完）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端离线套件 | `uv run --frozen pytest -q` | **1352 passed / 0 failed / 11 skipped** |
| 前端类型 | `cd frontend && vue-tsc --noEmit` | **0 errors** |
| 前端单测 | `cd frontend && vitest run` | **178 passed（26 个文件）** |
| 依赖守卫 | `uv run --frozen pytest -q tests/test_dependency_compat.py` | **9 passed** |

> 环境：无 `.env`、无 ES、无网络（`tests/conftest.py` 固定 `KEYWORD_SEARCH_PROVIDER=none` 与
> `TRACE_SINK=none`，并把会话工厂 / 运行时配置缓存换成内存替身，套件的事实不取决于本机开着什么）。

## 二、四条回归红线

| # | 红线 | 钉在哪 | 结果 |
| --- | --- | --- | --- |
| 1 | 关掉观测时，响应除 `metadata.trace_id` 外**逐字节不变** | `tests/test_multi_query_union.py`（#78 版黄金哈希，基线**未重采**，比对前只摘掉新字段） | ✅ |
| 2 | `trace_id` 任何配置下都非空、同 `log_id` 派生一致 | `tests/test_trace_identity.py`（32 位 hex、恒等、写库失败仍有值） | ✅ |
| 3 | 归属索引任何一行都不含问句原文 / 改写句 / 切片正文 | `tests/test_trace_identity.py`（**逐列**哨兵扫描真实写入行）、`test_retrieval_log_hygiene.py` | ✅ |
| 4 | 观测不可用时反馈仍成功、瀑布图仍可读或明确「未启用」 | `tests/test_feedback.py`、`tests/test_console_waterfall.py`、`tests/test_retrieval_observability.py` | ✅ |

## 三、按票验收

| 票 | 关键行为 | 验收证据 |
| --- | --- | --- |
| #99 | ADR-0010 第五次修订、ADR-0021、术语表六词 | `docs/adr/0010` 末节、`docs/adr/0021`、`GLOSSARY.md`「观测与评测」节 |
| #100 | 观测 SDK 5.2.0；ragas 私有导入与大版本有守卫 | `test_dependency_compat.py` 两条守卫（实测能红）；`uv.lock` 只多 7 个 `opentelemetry-*` |
| #101 | `retrieval_traces` 十列、`trace_id` NOT NULL、独立短事务 | `test_trace_identity.py` 12 条 + `test_retrieval_trace_migration.py` 12 条；`alembic upgrade/downgrade --sql` 双向渲染 |
| #102 | 六段度量常开；`none`/`jsonl`/`langfuse` 三去向一个配置切 | `test_observability_sink.py` 15 条 + `test_retrieval_observability.py` 6 条 |
| #103 | 控制台按 `log_id` 读回瀑布、先本地后观测、跨租户同「不存在」 | `test_console_waterfall.py` |
| #104 | 观测栈独立 profile（默认不背 6 个容器） | `docker compose -f deploy/docker-compose.yml config --services` → 默认 4 个；`--profile obs` → 9 个 |
| #105 | 反馈本地权威、三种失败同一码、观测挂掉仍成功 | `test_feedback.py`；迁移 `a91f3c7e2058` 已应用到共享开发库 |
| #106 | 评分控件按 `trace_id` 反查 `log_id`，三种失败各一句人话 | `test_console_trace_lookup.py` 7 条 + `frontend/.../feedback.spec.ts` 5 条；vue-tsc 0 errors |
| #107 | 确定性门禁（零 LLM）、相对判据、可复现报告 | **首次基线**：`CP 0.2220 / CR 0.8717`（127 题全部评分）；合并前后两次全量跑逐项 **+0.00pp** |
| #108 | 50 题旁证答案集、机械校验、抽检留档 | `eval/answers_v1.json`：50 题、**47/50 校验通过**，3 条硬失败（`q55`/`q100`/`q80`）原样留档不凑数 |
| #109 | 旁证跑批 + 控制台触发；未评分不按 0 分算 | `test_side_evidence.py` 12 条；实跑见下节 |
| #110 | 低分导出；观测不可用即明确失败、不落空文件 | `test_export_eval_cases.py` 9 条 |

## 四、真实跑（带出处，能复现）

### 门禁基线（#107）

```powershell
uv run python scripts/eval_runner.py run
uv run python scripts/eval_runner.py report --run eval/baseline --select metrics
```

| 指标 | 值 | 已评分 |
| --- | --- | --- |
| `context_precision` | **0.2220** | 127/127 |
| `context_recall` | **0.8717** | 127/127 |

- 条件：进程内 `retrieve()`，`mode=hybrid`、`top_k=5`、重排/改写/同义词全关，
  嵌入 `qwen3.7-text-embedding`，语料 `1946e4e5954f`（21 篇 / 469 片），检索索引 `rag_chunks_eval`。
- 切块口径取自 `.env` 的 `800/100`（与文档 `512/50` 在这批语料上切出同样的 469 片，
  `eval/ACCEPTANCE.md` 有网格证据），已单列并纳入「不可比判定」。
- `absent`(6) 与 `false_premise`(4) 不进主平均；`unscored` 单列、不按 0 分算。
- 这里的 CP 是 ragas **ID 版集合口径**（分母 = 本次返回的不同切片数），与 `ACCEPTANCE.md`
  里按行算的 `P@5`（0.2252 那一类）同量级但**不是同一把尺子**，不混着讲。

### 语义旁证（#109）

```powershell
uv run python scripts/eval_side_evidence.py --env-file ../RAG-Core/.env --limit 3   # 通路
uv run python scripts/eval_side_evidence.py --env-file ../RAG-Core/.env            # 全量
```

| 跑次 | 题数 | CP | CR | 状态 |
| --- | --- | --- | --- | --- |
| `--limit 3` 通路 | 3 | 0.6833 | 1.0000 | ok（2 题评分、1 题未评） |
| 全量（2026-11-11 07:5x） | 50 | 0.8000（5 题） | 1.0000（5 题） | **partial** |

- 裁判：`deepseek-v4-1-flash-economy-261017`，取自数据库 `model_configs`（与运行时同一套）。
- 全量跑到第 5 题后撞 **5 小时配额** `429 AccountQuotaExceeded`（10:39 重置），
  其余 45 题全部记为 `error` 并**从平均里排除**，报告单列清单。
- **这不是一次「全量绿」**。代码对这种情况给 `partial` 状态与非零退出码；
  配额重置后重跑即可得完整数。旁证只提示、不阻断，门禁不受影响。

## 五、仍未实机验证的两条（如实留档）

| 项 | 状态 | 翻案条件 |
| --- | --- | --- |
| 观测栈**经 HTTP 检索端点**跑通完整链路 | 栈**已起过并实测**：5 个容器（web / worker / clickhouse / minio / redis）在跑，`docker stats` 读到合计 ≈1.8 GB（见 `deploy/README.md`「资源占用」）；trace 注入与读回也验过——`emit_retrieval_trace` + 真 `LangfuseTraceSink` + 真栈，六段耗时齐全、`traceId` 与 `trace_id_for(log_id)` 逐字符相同。**未做的那一步**：本机 worktree 没有 `.env`，嵌入与 LLM 的 key 都不在，`POST /api/v1/rag/retrieve` 这条真实检索链路起不来，所以「响应里的 `trace_id` → 栈里那条 trace」这条端到端链路没跑过 | 有 `.env` 的机器上发一次真实检索，核对 UI/lifecycle API 里的 trace id 与响应 `metadata.trace_id` 一致、且含六段 |
| `--via-http` 评测冒烟 | 已实现（3 道题、断言字段清单），离线测试覆盖**挑题顺序与缺字段**，但未起 API 服务真跑 | 起服务 + 签一把 Key 后跑一次 |

## 六、口径提醒（读这份表前先知道）

- **门禁指标**（零外部依赖、可复现、会阻断）与**旁证指标**（LLM 裁判、有随机性、只提示）
  是两类东西，见 `GLOSSARY.md`。本文件里所有 CP/CR 都标了是哪一类、用哪把尺子。
- 任何数字换过语料版本 / 裁判模型 / 切块窗口，就是**另一把尺子**，不与旧数字并排比。
- 报告产物落在 `eval/runs/`（gitignore），固定基线在 `eval/baseline/`（进仓库）。

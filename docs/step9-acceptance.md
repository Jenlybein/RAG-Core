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
| #106 | 评分控件按 `trace_id` 反查 `log_id`，三种失败各一句人话 | `test_console_trace_lookup.py` 7 条 + `frontend/.../feedback.spec.ts` 5 条；vue-tsc 0 errors。**票外必要补充**：`GET /console/retrieval-traces?trace_id=` 这个反查端点不在 #105/#106 的 AC 里——检索响应只有 `trace_id`（ADR-0010 红线），而 `trace_id` 是 `log_id` 的单向派生，推不回去；不加这条端点就没有「点一下就能打分」的合法路径 |
| #107 | 确定性门禁（零 LLM）、相对判据、可复现报告 | **首次基线**：`CP 0.2220 / CR 0.8717`（127 题全部评分）；合并前后两次全量跑逐项 **+0.00pp** |
| #108 | 50 题旁证答案集、机械校验、抽检留档 | `eval/answers_v1.json`：50 题、**47/50 校验通过**，3 条硬失败（`q55`/`q100`/`q80`）原样留档不凑数。**偏离**：票面写「12 个题型各 2~3 题」，实际是 11 个题型（`absent` 不进旁证）、每型 2~6 题——50 题 ÷ 12 型 × 2~3 题在算术上不可同时成立，`eval/answers_v1.report.md` 已自陈并取「下限口径」。AC 未随之修订，属**未闭环**，留给下一轮改票 |
| #109 | 旁证跑批 + 控制台触发；未评分不按 0 分算 | `test_side_evidence.py` 12 条；实跑见下节 |
| #110 | 低分导出；观测不可用即明确失败、不落空文件 | `test_export_eval_cases.py` 10 条；**实机跑通整条闭环**（见第六节）：真实反馈 → 反查 → 从栈取回问句原文 → 落候选集 |

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
| `--limit 3` 通路 | 3 | 0.6833 | 1.0000 | `ok`（2 题评分、1 题未评）※ |

※ 这一跑发生在 `partial` 档加上**之前**，所以当时写的是 `ok`。按现在的代码，
`unscored > 0` 一律判 `partial` 并非零退出——同一批结果今天重跑会显示 `partial`。
两处记录都留着，因为改档位这件事本身要看得出痕迹。
| 全量（2026-11-11 07:5x） | 50 | 0.8000（**n=5**） | 1.0000（**n=5**） | **partial** |

> **n=5，不是 50。** 这两列的分母是「真正评上的题数」，不是「跑了几道题」——引用这两个
> 数字时必须连 n 一起说，否则会被读成「50 题的平均」。控制台面板也同时显示
> `已评分 / 未评分`，就是为此。

- 裁判：`deepseek-v4-1-flash-economy-261017`，取自数据库 `model_configs`（与运行时同一套）。
- 全量跑到第 5 题后撞 **5 小时配额** `429 AccountQuotaExceeded`（10:39 重置），
  其余 45 题全部记为 `error` 并**从平均里排除**，报告单列清单。
- **这不是一次「全量绿」**。代码对这种情况给 `partial` 状态与非零退出码；
  配额重置后重跑即可得完整数。旁证只提示、不阻断，门禁不受影响。

## 五、端到端实机验证（2026-11-11，已补齐）

原先留档为「未验证」的那条**已经做掉了**：观测栈起来、真实 HTTP 检索端点发请求、同一个
`trace_id` 在栈里读回六段真值。

```powershell
# 观测栈（v4 全家桶）
docker compose -f deploy/docker-compose.yml --profile obs up -d
# API（TRACE_SINK 是启动时读的，所以要带环境变量重启）
$env:TRACE_SINK='langfuse'; $env:LANGFUSE_HOST='http://localhost:3000'
$env:LANGFUSE_PUBLIC_KEY='pk-lf-...'; $env:LANGFUSE_SECRET_KEY='sk-lf-...'
uv run uvicorn app.main:app --port 8899
# 真实检索（走数据面端点，Bearer 身份）
curl -s -X POST http://127.0.0.1:8899/api/v1/rag/retrieve \
  -H 'Authorization: Bearer dev-key' -H 'Content-Type: application/json' \
  -d '{"query":"退款多久到账","kb_ids":["kb-eval"]}'
```

结果：

| 项 | 值 |
| --- | --- |
| 响应 `code` | `0` |
| 响应 `metadata.trace_id` | `d6f2346098fdf5f60079b7f83f6c5747` |
| 栈里同 `trace_id` 的 observation 数 | **6** |
| 六段耗时（ms） | `query_optimization 0.09` / `vector_recall 474.568` / `keyword_recall 9372.298` / `fusion 0.154` / `rerank 8483.89` / `collapse 12.246` |

这一跑还顺手抓出一个**真 bug**：`langfuse_reader` 读栈时没传 `fields`，而 v2 的
observations 接口默认不返回 `metadata`——于是每段都退回 `end - start`（事后补记里几乎
相同），瀑布图从栈里读时**全是 0.0 ms**。修复是 `fields=["basic", "metadata"]`（两个都要：
只写 `metadata` 会把 `name` 一起白名单滤掉，六段全被丢弃），并由
`test_reader_asks_the_api_for_name_and_metadata_explicitly` 钉住——把 `fields` 删掉那条
测试立刻变红（已做变异验证）。

> 本次环境里 ES 没起，所以 `degraded_reason=keyword search failed`、`keyword_recall`
> 花了 9372 ms 才失败——这正好验证了「失败的那一路仍然记录耗时」（#102 的设计决定）。

### 顺带跑通 #110 的整条闭环

同一次环境里，从「用户打差评」一路跑到「候选评测题落盘」：

```powershell
# 1) 给那次检索打 2 分（真实数据面端点）
curl -s -X POST http://127.0.0.1:8899/api/v1/rag/feedback \
  -H 'Authorization: Bearer dev-key' -H 'Content-Type: application/json' \
  -d '{"log_id":"44db034a-…","trace_id":"d6f23460…","score":2,"comment":"关键词那路挂了，结果不准"}'
# 2) 导出候选评测题
uv run python scripts/export_eval_cases.py --env-file .env --max-score 3 --days 30 \
  --out "$env:TEMP\candidates.json"
```

结果：`新增 1 条`，产出的候选题里 `query` 正确取回了 `"退款多久到账"`，
`source` 带 `trace_id` / `log_id` / `kb_ids` / `score`，`relevant: []` 待人工标注。

这一跑又抓出**两个**会让 #110 永远失败（或静默产出残缺数据）的坑，都在读端：

| 坑 | 表现 | 修法 |
| --- | --- | --- |
| 导出脚本自己写了一份 `get_many`，**漏 `fields`** | 每次报「观测服务里没有问句原文」，导出中止 | 取数收进 `langfuse_reader.fetch_observations`，脚本不再自己拼 |
| metadata **默认按 200 字符截断** | 482 字的问句只回 200 字——不报错，静默产出一道**残缺的题** | `expand_metadata=True`（实机量过：加它之后 482 字完整回来） |

两个坑都是「不报错的坏」，所以都进了守卫测试：
`test_query_source_goes_through_the_shared_reader_and_can_actually_read`（钉住必须走共用取数）、
`test_reader_asks_the_api_for_name_and_metadata_explicitly`（钉住 `fields` 与 `expand_metadata`）。

## 六、控制台入口补全（本轮新增）

此前这套能力只在**接口层**完整：瀑布、评分、旁证都要先知道一个 `log_id` / `trace_id`，
而那个 id 只能回调用方系统里翻。这一轮把三个入口补到了界面上：

| 入口 | 位置 | 补的是什么 |
| --- | --- | --- |
| **检索记录**页 | `/traces`（顶栏第四条） | 本租户最近 N 次检索的列表；点一行看六段瀑布；每行一个「在观测栈里打开」外链 |
| 旁证**历史** | `/retrieve` 页面「语义旁证」卡片下半部 | 刷新页面后仍能看到上一轮；每轮带「看报告」 |
| 旁证**报告** | 同卡片 / 历史列表的「看报告」 | 弹窗显示 `report.md` 原文（后端按 `run_id` 读，前端不拼路径） |
| **没有 Worker** 的提示 | 同上，`queued` 超过 45 秒 | 说清「多半是队列里没人消费」——此前 `queued` 与「没人跑」长得一模一样 |

配套后端（都在控制面、都带租户过滤）：

- `GET /console/retrieval-traces/recent?limit=`：列表，`limit` 上限 100；`score` 未评分时是
  `null`（不是 0）。
- `GET /console/observability`：只下发 `trace_sink` 与 `langfuse_host`，**密钥一个都不出进程**。
- `GET /console/eval/side-evidence?limit=`：历史列表，只列本租户。
- `GET /console/eval/side-evidence/{run_id}/report`：报告原文，只认 `run_id` 形状、
  只读那一个固定文件名。

`LANGFUSE_PUBLIC_URL` 是这轮新加的配置项：容器里 `LANGFUSE_HOST` 是服务名
（`http://langfuse-web:3000`），浏览器解析不了；「在观测栈里打开」必须用对外地址。

### Docker 直接访问

`api` 与 `frontend` 两个服务已进 compose，**打开 `http://localhost:8080` 就是控制台**：

```bash
docker compose -f deploy/docker-compose.yml up -d --build api frontend
```

三条与这一轮强相关的取舍：

1. `api` 与 `worker` **共用镜像标签** `rag-core-app:local`：只构建一次，两边跑同一份代码。
2. 两个容器**共享命名卷 `eval_runs`**（挂在 `/app/eval/runs`）：旁证的 `status.json` 由 worker
   写、api 读；不共享时前端永远看到 `queued`。
3. 前端镜像的 npm 源默认走 npmmirror（`--build-arg NPM_REGISTRY=` 可覆盖）：官方源在部分
   网络下只有几十 KiB/s，构建期不该赌网络。

## 七、仍未实机验证的一条（如实留档）

| 项 | 状态 | 翻案条件 |
| --- | --- | --- |
| `--via-http` 评测冒烟 | 已实现（3 道题、断言字段清单），离线测试覆盖**挑题顺序与缺字段**，但未起 API 服务真跑 | 起服务 + 签一把 Key 后跑一次 |

## 八、口径提醒（读这份表前先知道）
- **门禁指标**（零外部依赖、可复现、会阻断）与**旁证指标**（LLM 裁判、有随机性、只提示）
  是两类东西，见 `GLOSSARY.md`。本文件里所有 CP/CR 都标了是哪一类、用哪把尺子。
- 任何数字换过语料版本 / 裁判模型 / 切块窗口，就是**另一把尺子**，不与旧数字并排比。
- 报告产物落在 `eval/runs/`（gitignore），固定基线在 `eval/baseline/`（进仓库）。

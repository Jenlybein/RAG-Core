# C6 · 端到端验收（#153）：v1/v2 对照 + 8 题开/关

本轮验收在**本机真实环境**上跑完：`pg16-pgvector`（`localhost:5432/rag_db`）+ `rag-center-redis`
+ `rag-es`（`localhost:9200`，IK 已烘进镜像），真实嵌入 / 重排 / LLM 端点。没有 mock，
没有改参数去凑数字；不好看的数字原样写在下面，原因一起写。

- 环境快照（不含任何密钥）：[`environment.json`](environment.json)
- 三份对照报告：**[① v1 vs v2](c6-v1-vs-v2.md)**、**[② 图谱开/关](c6-graph-on-off.md)**、
  **[③ Research 开/关](c6-research-on-off.md)**
- 守分抽样（20 题）：[`c6-guard-20261114T113656Z.md`](c6-guard-20261114T113656Z.md)
- 每次运行的原始产物（`results.json` + `report.md`）在 [`runs/`](runs/) 下，按 `01..04` 编号。

---

## 一、先读这一节：退化项与「本轮什么没验证到」

| # | 事项 | 结论 | 位置 |
| --- | --- | --- | --- |
| 1 | **守分抽样 1/20 退化为 0 分**（`q05`，exact_code） | 归因是**关键词那一路超时降级**（该题的精确错误码只能靠关键词命中），不是检索链路退化；其余 19 题逐题与基线完全相同。 | [守分抽样](c6-guard-20261114T113656Z.md) |
| 2 | **图谱开/关在本语料上「饱和」**：注入 0 条 | 语料只有 3 篇 / 15 个节，混合召回深度 20 已把整个库都放进候选池（`candidates=15`），种子 10 个、邻居全都已在池里 → `injected=0`。**这组对照没有测到图谱的价值**，只测到「开着它不坏事」。 | [图谱报告](c6-graph-on-off.md) |
| 3 | **v1 的参考边只有 5 条，v2 有 9 条**（`manifest.json` 期望 9） | 逐文档入库时，文档**只能引用「此刻已入库」的文档**：先入库的 `refund_policy` 指向 `promo_rules` / `points_policy` 的 4 条边当场丢弃；整版重建看得见整库 → 9/9。 | [v1/v2 报告](c6-v1-vs-v2.md) |
| 4 | **v1/v2 的分句没换**（c6 用「原样重建」这一档） | 节身份（`section_id`）逐节相同，所以「切分点变化」这一维**本轮没跑到**；重跑命令写在 v1/v2 报告里。 | [v1/v2 报告](c6-v1-vs-v2.md) |
| 5 | **`eval_runner` 的 live 路径当前跑不通** | 同步 Session 撞异步检索链路：`TypeError: object ChunkedIteratorResult can't be used in 'await' expression`。137 题整体门禁因此**无法重跑**，守分只能抽 20 题（见下）。仓储层迁异步（#130/#132）时评测运行器没跟着迁。 | 缺陷 #1 |
| 6 | **Research 首跑整组 `degraded`** | 评测进程没接生产模型链（回落 `.env` 的单供应商 key，账号 403）。接上 `model_configs` 角色指针后重跑正常——这不是 Research 的问题，是评测的接线问题。 | [Research 报告](c6-research-on-off.md) |

## 二、验收逐条对账

| 验收（issue #153） | 结果 | 证据 |
| --- | --- | --- |
| 环境可重建、可重复 | ✅ `scripts/prep_eval_c6.py` 幂等（重跑 `plan changes=（已达标）`） | 脚本 + `environment.json` |
| 实跑 8 题 × 两组模型 / 断构图 / 研究模式 | ✅ 4 组共 32 次「题 × 模式」运行全部有读数 | `runs/01..04` |
| 检索链路图谱开 / 关各一组（各组 8 题） | ✅ 两组都跑完（`graph_factory` 真的进 / 真的不进） | [②](c6-graph-on-off.md) |
| 命中 / 引用回溯 / 注入条数 / 延迟齐全 | ✅ 逐题 `hits` / `citation_traceability` / `metadata.retrieval.graph` / `latency_ms` | 各报告 |
| Research 开 / 关各一组 | ✅ 关=检索（16 次调用、p50 384ms）；开=状态机（90 次、p50 29.2s） | [③](c6-research-on-off.md) |
| 停止原因分布 / 引用块完整度 | ✅ `evidence_complete 2 / max_rounds 3 / no_new_evidence 3`；引用块 26、带证据 26、悬空 0 | [③](c6-research-on-off.md) |
| 逐部分 v1/v2 对照（命中集合、节身份、新增/丢失） | ✅ 命中集合 8/8 完全相同、节身份 15/15 相同；**差异在边：5 → 9** | [①](c6-v1-vs-v2.md) |
| 守分：基线题集抽样 20 题「不变差」 | ⚠️ **抽样**：19/20 逐题不变，1 题（`q05`）归零、归因关键词路由 | [守分抽样](c6-guard-20261114T113656Z.md) |
| 模型调用预算 ≤ 400 | ✅ 实测 **≈ 302 次**（明细见第四节） | 本节表 |
| 报告写清「与原项目数字不可比」 | ✅ 见第三节 | 本节 |

## 三、这些数字**不可**与原项目的数字横比

- **语料不同**：本票用 `eval/corpus-refs/` 的 3 篇参考文档（`manifest.json` 声明 9 条跨文档引用），
  原项目的 137 题跑的是 `eval/corpus/`（5 个库、469 片）。参考语料是**故意做小**的，只为看清
  「引用边有没有被建出来 / 注入有没有生效」。
- **题集不同**：`eval/refs-questions.json` 的 8 道参考题是四类跨篇机制（`cross_section` /
  `cross_document` / `parallel_points` / `dependent_hop`），不进 137 题门禁。
- **口径不同**：本报告只统计「命中集合 + 回溯率 + 注入条数 + 延迟 + 停止原因」，不算 CP/CR
  （那要人工标注，参考语料没有标注）。
- 唯一**同尺子**的一栏是守分抽样：ragas ID 版 CP/CR，与 `eval/baseline` 同一个函数。

## 四、模型调用预算（上限 400，实测 ≈ 302）

| 项目 | 调用 | 说明 |
| --- | --- | --- |
| 环境准备 · 文档入库（v1） | 15 次嵌入 | 3 篇 → 15 片，生产入库路径 |
| 环境准备 · 整版重建（v2） | 15 次嵌入 | #142 流水线，同分句重建 |
| 环境准备 · 首轮误用别名 `rag_chunks` 后清理重来 | 30 次嵌入 | 真实花掉了：那一轮的库与 ES 索引已删除，重跑后才落到 `rag_chunks_c6` |
| ① v1 · 图谱关 | 16 | 8 × (嵌入 1 + 重排 1) |
| ① v2 · 图谱关 | 16 | 同上 |
| ② v2 · 图谱开 | 16 | 同上 |
| ③ Research 开 | 90 | 27 次状态机 LLM + 33 次子检索 × 2（嵌入 + 重排） |
| ③ Research 首跑（接线错误，作废） | ≈ 40 | 8 × (3 次 LLM 尝试 + 1 次子检索 × 2)；读数全 `degraded`，已重跑 |
| 守分抽样 3 次（2 次作废） | 60 | 每次 20 次查询嵌入；作废的两次一次是计数口径 bug、一次是脚本崩在汇总 |
| 探针 / 冒烟 | 6 | 单题冒烟 2、图谱元数据探针 2、v1 冒烟 2 |
| **合计** | **≈ 302** | 每组开跑前都先印了预计值，没有超上限 |

## 五、本轮修掉的缺陷（都带用例）

| 缺陷 | 症状 | 修法 | 用例 |
| --- | --- | --- | --- |
| **版本重建会删掉别人的入边** | 逐文档重建边时，把「以该文档为目标」的入边一起删了；9 条参考边只剩 5，而构建照样报 ready、计数自洽 | 改成「整版一次删 → 一次插 → 清悬空」 | `tests/test_section_edge_service.py::test_the_version_pass_keeps_the_inbound_edges_of_the_document_it_touches_last` |
| **回滚在真库上直接 500（部分唯一索引）** | `rollback` 的两条状态 UPDATE 由 ORM 决定顺序，激活排在归档前面 → `duplicate key value violates uq_index_build_versions_active` | 归档后显式 `flush()` 再激活 | `tests/test_index_build_rollback.py::test_the_switch_flushes_the_archive_before_it_activates_the_new_version` |
| **切换失败后的收尾会炸在过期行上** | 事务一滚 ORM 行过期，收尾再读 `row.kb_id` → `MissingGreenlet`，把「提交为什么失败」顶掉，且别名留在错的版本上 | 收尾只读普通值（`build_id`/`kb_id`/`tenant_id`/`version` 先抄出来） | `…::test_the_failed_commit_cleanup_never_touches_an_expired_row` |
| **`eval_refs.py` live 路径自 #146 起是死的** | `RetrievalOptions` 少了 `orchestration` → 每题 `error:TypeError` | 显式传 `orchestration=False`（评测不要编排） | `tests/test_eval_refs.py` 接线用例 |
| **live 路径在 Windows 上第一条查询就崩** | psycopg 异步连接不能跑在 Proactor 循环上 | `main()` 里设 `WindowsSelectorEventLoopPolicy` | 同上 |
| **`--rerank on` 不生效** | 「生效值」只在跑批时切，预计调用那一栏按 `.env` 猜 | `_effective_rerank` 以显式开关为准 | `tests/test_eval_refs.py` |
| **Research 响应是 pydantic 模型，读数按 dict 读** | 整组落 `error:AttributeError`，报告只剩「0 引用块」 | `_as_mapping`：`model_dump(mode="json")` | `…::test_a_real_research_data_response_is_understood` |
| **模型调用计数把「重排这一栏存在」当成「重排放了」** | 关着重排也每题多算一次（20 题报 40） | 判据换成 `metadata.retrieval.rerank.applied` | `…::test_the_retrieval_side_counts_the_model_calls_it_actually_made` |

**没修的（够大，按纪律停下报告）**：

1. `scripts/eval_runner.py` / `scripts/eval_retrieval.py` 的 live 路径要整体迁到异步会话
   （清库、建库、建文档、索引、标签解析、打分六处）。这是另一张票的规模；本轮只补了一条
   窄路（`scripts/eval_c6_guard.py`）拿到守分抽样，尺子与标签解析**复用同一份实现**。
2. 评测关键词索引没有版本化（`rag_chunks_eval` 的文档没有 `index_version` 字段），
   版本化查询读不到它 → 关键词一路在评测里等于「空跑」，偶尔超时降级。补齐要重建
   469 片的索引（≈ 469 次嵌入），**超出本轮 400 次预算**，故只记录不执行。
3. 逐文档入库时「引用边只能指向已入库文档」是**当前语义**（不是崩溃），改它涉及入库与构建的
   先后契约；本轮把它写进 v1/v2 报告当作发现，不动实现。

## 六、怎么重跑

```bash
# 0. 环境准备（幂等；会打印「计划改什么」，重复跑应当是「（已达标）」）
uv run python scripts/prep_eval_c6.py

# 1. 本机该库的关键词物理索引别名（迁移脚本会删旧索引，属维护窗口动作，本机没跑）
export ELASTICSEARCH_INDEX=rag_chunks_c6

# 2. 三组对照（每组开跑前会印预计模型调用）
uv run python scripts/eval_refs.py --source live --allow-model-calls \
  --kb-ids "$C6_KB_ID" --tenant-id tenant_c6 --graph off --rerank on     # ① v2 侧
uv run python scripts/rebuild_kb_version.py --kb-id "$C6_KB_ID" --rollback v1  # 切 v1
uv run python scripts/eval_refs.py ... --graph off --rerank on           # ① v1 侧
uv run python scripts/rebuild_kb_version.py --kb-id "$C6_KB_ID" --rollback v2  # 切回 v2
uv run python scripts/eval_refs.py ... --graph on --rerank on            # ② 图谱开
EVAL_API_KEY=<c6-state.json 里的 key> uv run python scripts/eval_refs.py ... \
  --research on --rerank on                                              # ③ Research 开

# 3. 守分抽样（20 题，不连关键词也需要库；重排按基线口径关着）
uv run python scripts/eval_c6_guard.py
```

`$C6_KB_ID` 与 API key 只出现在 `eval/refs-runs/c6-state.json`（gitignored），**不进仓库**。

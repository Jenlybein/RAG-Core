# 验收矩阵执行记录（第 4 步 · 票 8）

九条逐条执行、逐条留痕。**执行时间**：2026-11-08（Asia/Shanghai）。
**原始产物**：`eval/runs/20261108T014656/`（`results.json` 机器可读、`report.md` 人读）。
**复跑入口**：`uv run python scripts/eval_retrieval.py --env-file <主 checkout>/.env`。

本文件只记录「怎么验、验出什么」。语料与题目本身见 `eval/corpus/`、`eval/questions.json`，
两组对比的解读见上面那个 `report.md`。

## 环境与前置

| 项 | 实际值 |
| --- | --- |
| PostgreSQL | 容器 `pg16-pgvector`（5432，库 `rag_db`）；`uv run alembic upgrade head` 到 `6dfebeded96c` |
| Elasticsearch | 容器 `rag-es`（9200，镜像已烘入 IK） |
| 评测专用索引 | `rag_chunks_eval`（**不是** `rag_chunks`：脚本强制加 `_eval` 后缀） |
| 嵌入 | 百炼 `qwen3.7-text-embedding`，1536 维，真实调用 |
| 语料 | `eval/corpus/support_kb.md`（33 片）+ `ops_handbook.md`（6 片）= 39 片，落在 `kb-eval` / `kb-eval-ops` |
| 题目 | 12 道（3 精确编号 / 2 型号 / 2 口语 / 2 同义词 / 2 跨章节 / 1 库里没有） |
| 旋钮 | `rrf_k=60`、`top_k=5`、每路召回深度 20、重排关、同义词扩展关 |

> 空库 `kb-eval-empty` 只为一件事存在：触发 `no_indexed_chunks`。

## 九条

### 1. ES 有数据（`_count > 0`）

```powershell
docker exec rag-es curl -s "http://localhost:9200/rag_chunks_eval/_count"
```

- **期望**：`count > 0`
- **实际**：`{"count":39,"_shards":{"total":1,"successful":1,"skipped":0,"failed":0}}`
- **结论**：**通过**。39 = 两份语料的全部切片，与 PG 里的 `chunks` 行数一致。

### 2. IK 生效（`_analyze` 切出中文词而非整句）

```powershell
curl.exe -s -X POST "http://localhost:9200/rag_chunks_eval/_analyze" `
  -H "Content-Type: application/json" `
  -d '{\"analyzer\":\"ik_max_word\",\"text\":\"设备证书链已过期\"}'
```

- **期望**：tokens 多于一个，且不是整句
- **实际**：
  - `ik_max_word` → `['设备', '证书', '链', '已过期', '已过', '过期']`
  - `ik_smart`  → `['设备', '证书', '链', '已过期']`
- **结论**：**通过**。两套分词器都真的在切中文；`ik_max_word` 更细（`已过` / `过期`
  这类子词也建），`ik_smart` 更粗——正是 mapping 里「写入细粒度、查询智能」的预期行为。
  注意 `链` 被单独切出来，说明 IK 的主词典没有把「证书链」收进去，但这不影响命中：
  短查询照样能与文档共享 `设备`/`证书` 这些词。

### 3. hybrid 生效（响应里两路都有命中数）

```powershell
uv run python scripts/eval_retrieval.py --env-file <主 checkout>/.env
# 看 groups.hybrid.per_question[*].returned 里的 vector_rank / bm25_rank
```

- **期望**：混合返回里既有 `vector_rank` 非空的切片，也有 `bm25_rank` 非空的切片
- **实际**：12 道题的 60 条返回中，`vector_rank` 非空 **59** 条、`bm25_rank` 非空 **46** 条
- **结论**：**通过**。两路都真的在贡献结果；`vector_rank` 比 `bm25_rank` 多，是因为关键词
  一路对短问句更容易「一个词都没对上」，而向量一路永远返回 20 条候选。

### 4. RRF 数值正确（用已知名次手算，与 `rrf_score` 一致）

```powershell
uv run python scripts/eval_retrieval.py --env-file <主 checkout>/.env
# 见 results.json 的 rrf_check
```

- **期望**：每条 `rrf_score == 1/(60+vector_rank) + 1/(60+bm25_rank)`（未命中的一路不累加），误差 < 1e-9
- **实际**：核对 **60 条**，最大绝对误差 **0.0**
  - 样例（q01 第 1 条，双路都排第 1）：手算 `1/(60+1) + 1/(60+1) = 2/61 = 0.0327868852`，
    返回 `0.032786885`
  - 样例（q01 第 2 条，向量第 2、关键词第 3）：手算 `1/62 + 1/63 = 0.0161290323 +
    0.0158730159 = 0.0320020481`，返回 `0.03200205`
- **结论**：**通过**。对账用的是返回里的名次，手算公式不从融合模块 import——否则是
  「用实现验证实现」。

### 5. 编号查询（`E1024` 能命中；纯向量是否漏）

```powershell
uv run python scripts/eval_retrieval.py --env-file <主 checkout>/.env
# 看 groups.vector / groups.hybrid 的 q01
```

- **期望**：hybrid 命中 E1024 那一节，且名次不差于纯向量
- **实际（q01「E1024 是什么错误，要怎么处理」，`top_k=5`）**：

  | 名次 | mode=vector | mode=hybrid |
  | --- | --- | --- |
  | 1 | **错误码 E1024：证书链已过期**（`vector_rank=1`） | **错误码 E1024：证书链已过期**（`vector_rank=1`、`bm25_rank=1`、`rrf=0.03278689`） |
  | 2 | 错误码 E1025：证书已被吊销 | 错误码 E1025：证书已被吊销（2 / 3） |
  | 3 | 错误码 E2048：请求频率超配额 | 错误码 W0001：存储余量告警（4 / 2） |
  | 4 | 错误码 W0001：存储余量告警 | 错误码 E2048：请求频率超配额（3 / 8） |
  | 5 | 星河客户支持知识库（文档导语） | 证书轮换流程（7 / 4） |

- **结论**：**通过（带保留）**。E1024 确实命中，hybrid 名次不差于纯向量。
  **但父 spec 预期的「纯向量模式下命中不了」没有复现**：纯向量把 E1024 排在第 1 名。
  原因见 `report.md` 的「结论与解读」第 3 条：票 2 的检索文本把编号写进了切片正文，
  这套嵌入对规整标识符并不盲。要复现「向量漏」需要更大的语料或更不规整的标识符
  （随机串 / 哈希 / 内部拼写），本票不做，已记给上游决定是否补测。

### 6. ES 降级（停 ES 后 `code:0` + `degraded:true` + 有结果）

```powershell
docker stop rag-es
uv run python scripts/eval_retrieval.py --env-file <主 checkout>/.env --degraded-probe
docker start rag-es
```

- **期望**：`code` 仍为成功、`degraded=true`、`degraded_reason=keyword search failed`、仍有向量结果
- **实际**：`error=None degraded=True degraded_reason=keyword search failed 返回 5 条`
  （日志同时出现 `hybrid retrieval degraded degraded_reason='keyword search failed'`）
- **结论**：**通过**。ES 真停着（`docker ps` 里 `rag-es` 已消失），混合模式没有报错，
  只是把结果降级成「仅向量」并如实标注原因。
- 备注：`docker start rag-es` 之后 10 秒恢复 `healthy`，验收结束时容器是**运行状态**。

### 7. 单模式不降级（`mode=bm25` 且 ES 停着 → 明确失败）

```powershell
docker stop rag-es
uv run python scripts/eval_retrieval.py --env-file <主 checkout>/.env --degraded-probe
docker start rag-es
```

- **期望**：`mode=bm25` 明确失败（`RETRIEVAL_FAILED` / 50002），绝不偷偷退回向量
- **实际**：`error=RETRIEVAL_FAILED: keyword search failed`（日志另有
  `keyword search failed error=ConnectionError`）
- **结论**：**通过**。调用方选 bm25 往往就是在评测单路效果，静默退回向量会把评测变成
  假数据，所以这里必须明确失败。

### 8. 空结果有原因（两种 `empty_reason` 各触发一次）

```powershell
uv run python scripts/eval_retrieval.py --env-file <主 checkout>/.env
# 看 acceptance 里的 A8 探针
```

- **期望**：`no_indexed_chunks` 与 `no_chunks_matched` 各出现一次，且都是成功（非报错）
- **实际**：
  - 空库 `kb-eval-empty` + `mode=vector` + query「证书过期怎么处理」→
    `empty_reason=no_indexed_chunks`，`retrieved_chunks=[]`，业务成功
  - 有内容的 `kb-eval` + `mode=bm25` + query「zzzqqqxxx」→
    `empty_reason=no_chunks_matched`，`retrieved_chunks=[]`，业务成功
- **结论**：**通过**。两种「空」被区分开：前者该引导用户去传文档，后者该引导用户换个
  问法。第二种之所以要指定 `mode=bm25`，是因为**向量一路永远返回最近的 k 条，不可能
  出现「有内容但没命中」**——这本身就是一条值得记住的事实（见下面「需要上游决定」）。

### 9. 多库检索（传 2 个 `kb_ids`，结果带不同 `kb_id`）

```powershell
uv run python scripts/eval_retrieval.py --env-file <主 checkout>/.env
# 看 acceptance 里的 A9 探针：kb_ids=[kb-eval, kb-eval-ops]
```

- **期望**：一次调用返回的结果里带不同的 `kb_id`（两个库都真的被召回）
- **实际**：`top_k=10`，「证书过期怎么处理」→ 返回 10 条，
  `kb_id=['kb-eval', 'kb-eval-ops']`
- **结论**：**通过**。两条通道各查一次、覆盖全部「库 + 索引版本」配对，不是分库召回再
  合并；结果自带 `kb_id` 能归属到库。

## 本地残留与清理

| 残留 | 位置 | 怎么清 |
| --- | --- | --- |
| 39 个切片 + 2 份文档 + 3 个知识库（`kb-eval` / `kb-eval-ops` / `kb-eval-empty`，租户 `tenant_demo`） | PostgreSQL `rag_db` | `uv run python scripts/eval_retrieval.py --env-file <主 checkout>/.env --cleanup` |
| 评测专用索引 `rag_chunks_eval`（39 个文档） | Elasticsearch | 同上（脚本先删索引再删 PG 行） |
| 运行产物 | `eval/runs/<时间戳>/` | 已在 `.gitignore` 里，不会入库 |

清理命令只碰上面这些 id：**其他知识库、其他租户、`rag_chunks` 索引一行都不动**。脚本
还会拒绝对名字里不含 `eval` 的索引执行删除。

## 与既有测试的关系

本票**没有**把评测脚本塞进 `pytest`：它必须连真 PostgreSQL + 真 ES + 真嵌入，跑一次
要发几十次网络请求。默认 `uv run pytest -q` 仍是纯离线（`369 passed, 10 skipped`），
守卫方式与集成用例一致——不设环境变量就整条跳过 / 不进收集范围，详见 `tests/README.md`。

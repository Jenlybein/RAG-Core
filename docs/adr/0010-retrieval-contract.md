# 0010. 检索契约：RRF 融合、三种结局与索引范围

- 状态：已采纳
- 日期：2026-11-08
- 相关：父 spec #20「第 4 步」、ADR-0007「业务失败 HTTP 200」、ADR-0011「LangChain 只进 LLM 与 Embedding」、`GLOSSARY.md`「检索链路」「混合检索」「召回深度」「融合池」「降级原因」「诚实空」「索引版本」、`app/services/retrieval/fusion.py`、`app/services/retrieval/pipeline.py`、`app/services/retrieval/rerank.py`、`app/services/rag_service.py`、`app/models/chunk.py`、`app/providers/vectorstores/pgvector.py`、`app/providers/keyword_search/query_builder.py`、`app/schemas/rag.py`

## 一句话决定

一次检索请求只有一条路径：**查询优化 → 两条通道各一次全局召回 → 一次 RRF 融合（`k=60` 写死）→ 可选重排**；结果落入**拒绝 / 降级 / 诚实空**三种结局之一。每条结果同时带五个通道分列字段（重排发生时再加 `rerank_score`），而 `score` 表示**本次最终排序分**。

这条契约由 `app/services/retrieval/` 与 `app/services/rag_service.py` 实现，`app/schemas/rag.py` 负责把它固定成响应形状；三者任何一处改动都要回来看本 ADR。

## 1. 融合

### 公式

```text
score(d) = 1 / (k + rank_vector(d)) + 1 / (k + rank_keyword(d))
```

- `rank_*` 是**该路召回列表里的位置**（从 1 开始），不是按分数重新排序后的位置。
- 某一路没有召回该文档时，该项**不累加**（不是补 0 分再参与平均）。
- 两路分数量纲不同（向量分 0~1、BM25 分无上界），所以**不做加权平均，也不做带权重的 RRF**。权重一开，「同一份输入两次返回同一顺序」就不再成立，评测也无法复现。

### `k=60` 写死，不进请求体

`k` 固定为 `60`：

- 没有对应的环境变量，也没有请求字段；`fuse_by_rrf(..., rrf_k=...)` 的参数只给单测用，生产路径永远不传。
- 请求体只接受 `kb_ids` / `query` / `mode` / `top_k`（`RetrieveRequest`，`extra="forbid"`）。任何 `hybrid_fusion` / `rrf_k` 一类的字段都会被参数校验拒绝。
- 理由：`k` 一旦可调，同一个「质量档位」在不同调用方那里就是不同的检索链路，历史评测与线上结果再也对不上。

### 唯一键是 `(kb_id, chunk_id)`

融合时按这个二元组去重，**不是只按 `chunk_id`**：

- 跨库结果必须能各自成为一条结果；
- 将来若出现同名切片 ID，也不会被误合并；
- 每一条结果的 `kb_id` 因此必须非空（`similarity_search_scope` 每行自带 `kb_id`）。

### 同分顺序（tie-breaker）

排序键按顺序比较，**顺序即契约**：

1. 融合分降序；
2. 两路都命中的优先（`len(ranks)` 大者在前）；
3. 两路中的最佳名次升序；
4. `kb_id` 升序；
5. `chunk_id` 升序。

没有它，同分顺序就取决于哪一路先返回——那取决于网络抖动，评测不可复现。由于固定 `k` 下 `1/(k+rank)` 是严格凸的，真正同分的两条必然名次多重集相同，第 2、3 条只在浮点碰撞时发力；即便如此，顺序也必须写死。

## 2. 三种结局

| 结局 | 触发条件 | HTTP / 业务码 | 关键字段 | 调用方应做什么 |
| --- | --- | --- | --- | --- |
| **拒绝** | 混合模式两路都失败；`mode=bm25` 时关键词引擎不可用或失败；任一知识库不可见 | HTTP 200 + 非零 `code`（`RETRIEVAL_FAILED=50002`；不可见为 `20008`）；`mode=vector` 的嵌入异常例外，为 HTTP 500 / `50000` | `code` / `msg` | 按业务码处理（重试、修配置或放弃），**不要**从空结果里猜 |
| **降级** | 混合模式仅一路失败；查询改写失败；重排失败 | HTTP 200 + `code: 0` | `metadata.retrieval.degraded: true`、`degraded_reason` | 结果可用但打了折；按原因决定是否提示用户 |
| **诚实空** | 检索成功但确实没有结果 | HTTP 200 + `code: 0` | `metadata.retrieval.empty_reason` | 按原因引导：去传文档，或换个问法；**不是故障，不要整单重试** |

几点容易读错的边界：

- **降级与诚实空可以同时出现。** 一路失败、另一路也没命中时，响应会同时是 `degraded: true` 和 `empty_reason: no_chunks_matched`。三种结局是三种「判断」，不是三个互斥的枚举值。
- **拒绝先于召回。** 知识库可见性在进入任何分支之前逐个校验；被拒的请求连嵌入都不会算（`offline_embedding.queries == []`）。
- **单模式不降级。** `mode=bm25` 时引擎不可用就是 `50002`，绝不偷偷退回向量——调用方选 `bm25` 往往就是在评测单路效果。
- **`mode=vector` 的嵌入失败不在降级矩阵里。** 它没有另一路可兜，也不该谎报空结果，按未捕获异常走 `50000`（HTTP 500）；`test_single_vector_mode_still_fails_loudly_when_embedding_breaks` 钉住这条。`50002` 用于通道层面的失败（关键词引擎不可用/失败、混合模式两路都失败）。
- **为什么嵌入失败不是 `50002`（这不是遗漏，是刻意区分）：** `50002` 是**业务拒绝**——请求合法、库里有东西、但这一路检不出来，按 ADR-0007 走 HTTP 200 + 业务码；而嵌入服务不可用是**基础设施故障**，必须让网关、重试与告警看得见。两者合并的后果很具体：嵌入服务整体宕机时，监控面板上会显示「检索命中率下降」，而不是「依赖故障」，排查方向直接跑偏。
- **空结果不是错误。** 把它当故障会让监控满地假警报，也让调用方误以为该重试。

## 3. 降级原因是一组固定英文串

`degraded_reason` 只允许四个值，**不拼异常文本**：

| 原因 | 产生位置 | 含义 |
| --- | --- | --- |
| `query rewrite failed` | `app/services/query/rewrite.py` | 改写失败（含超时、空返回、结构不合法），已回退原句 |
| `vector search failed` | `app/services/retrieval/pipeline.py` | 向量通道失败（含嵌入失败），已用关键词结果继续 |
| `keyword search failed` | `app/services/retrieval/pipeline.py` | 关键词通道失败，已用向量结果继续 |
| `rerank failed` | `app/services/retrieval/rerank.py` | 重排失败（含超时、空返回），已保留融合顺序 |

规则：

- **一因一码。** 调用方按码做判断；异常文案变了不该影响判断。日志里也只记异常**类型**，不记原文（原文可能夹带提示词或切片内容）。
- **先到的原因是根因，后到的不覆盖。** 实际顺序是：改写（召回之前）→ 通道（召回时）→ 重排（融合之后）。所以改写在先失败时，通道故障与重排失败都不覆盖它；通道已降级时，重排失败也不覆盖通道原因。
- **拒绝不是降级。** `20008`（不可见）/ `20002`（参数）这类拒绝不参与降级判定，也不会被降级吞掉。
- **「没配引擎」是能力缺失，不是「查过了没命中」。** 空实现 `available=False` 在混合模式记为该路失败（降级），在 `mode=bm25` 记 `50002`。

## 4. `score` 语义变更（破坏性）

`score` 从「余弦相似度」变成「**本次最终排序分**」：

| 场景 | `score` 现在是 | 具体分数该读 |
| --- | --- | --- |
| `mode=vector` | 向量分（`1 - 余弦距离`） | `vector_score` |
| `mode=bm25` | BM25 分（ES `_score`，无上界） | `bm25_score` |
| `mode=hybrid`，未开重排 | 融合分（约 `0.016`~`0.033` 量级） | `rrf_score` |
| `mode=hybrid`，重排发生 | 重排分（0~1） | `rerank_score` |

**这是破坏性变更。** 旧调用方如果把 `score` 当余弦相似度做阈值判断（例如 `score >= 0.8` 才采用），升级后会得到完全不同的行为：混合模式下 `score` 只有 `0.03` 左右。请求体本身没变，变的是响应里这个字段的含义。

### 迁移方式

1. **路由 / 下一跳判定**改读分列字段，不要读 `score`：
   - 这一条是不是向量命中的：`vector_rank is not None`；
   - 这一条是不是关键词命中的：`bm25_rank is not None`；
   - 语义相似度阈值：`vector_score`；
   - 融合排序的依据：`rrf_score`；
   - 重排置信度：`rerank_score`。
2. **未命中的那一路是 `null`，不是 `0`。** 「没命中」与「命中但分数为 0」必须可区分；把 `null` 读成 `0` 会制造假的「命中但极不相关」。
3. 不要跨模式比较 `score`（向量分、BM25 分、融合分、重排分四种量纲），也不要把不同量纲的分数字段相加。
4. `score` 仍可用于「按返回顺序展示」和兼容旧的展示代码；它不再是一个可解释的相似度。

## 5. 索引范围：`index_versions` 是配对映射

检索范围由 `kb_id → index_version` 的**映射**表示，唯一构造点是 `app/models/chunk.py::default_index_versions`。本步只写默认版本 `v1`，请求体里没有 `index_version` 字段；将来接上版本生命周期时，改的是这个构造点。

### SQL / 查询条件必须是配对

向量侧：

```python
or_(*[
    and_(Chunk.kb_id == kb_id, Chunk.index_version == version)
    for kb_id, version in index_versions.items()
])
```

关键词侧：每一对是一个 `bool.filter`（`term kb_id` + `term index_version`），对与对之间是 `should` + `minimum_should_match=1`。

**绝不能写成两个独立的 `IN`：**

```text
kb_id IN ('kb-a', 'kb-b') AND index_version IN ('v1', 'v2')
```

单版本时两种写法等价；多版本并存时会匹配出「`kb-a` 配 `v2`」这种根本不存在的组合——那是越权或脏数据，而且不会报错。

### 空映射编译成恒假

- 向量侧：`build_scope_filter({})` 返回 `false()`，SQL 渲染成恒假条件，**不是**「不过滤」。
- 关键词侧：`should: []` + `minimum_should_match: 1`，按构造匹配不到任何文档。
- 原因：拿不到范围却不过滤，等于把整个租户的库都召回——比空结果严重得多。

## 6. 多库：一次全局召回 + 一次融合

- 两条通道各查**一次**，各自的查询覆盖全部「库 + 索引版本」配对；嵌入只算一次，不随库数量增加。
- `top_k` 是**全局**返回条数；`HYBRID_RECALL_TOP_K` 是每路召回深度。**没有「每库各占名额」**——分库各召回一批再合并是假均衡，不相关的库也会占名额，把真正该进来的切片挤掉。
- **「某库没有切片进入结果」不是失败。** 不记 `failed_kb_ids`，也不按结果条数推断某个库出了事；结果只按实际命中呈现，每条结果自带 `kb_id`。
- 可见性校验在进入任何分支之前统一做一遍（N 个库 = N 次查询），拒绝（`20008`）先于召回，且不参与降级。

## 7. 豁免记录：回归红线上的 5 个测试文件

父 spec 的回归红线是「**现有 176 条离线测试一条都不许改**」（数字出自 issue #20，对应第 3 步收尾时的记录；到本步开工时测试总数已因前序票增加，所以这里按**文件**判红线：第 4 步之前就存在的测试文件不许改）。本步实现「一次全局召回 + 一次融合」时，实际改动了 5 个测试文件。逐条记录如下；**没有任何一条是删除断言或放宽期望**。

| 文件 | 改了什么 | 为什么必须改 | 改后强度 |
| --- | --- | --- | --- |
| `tests/test_hybrid_retrieval.py` | 替身的 `similarity_search` 换成 `similarity_search_scope`；断言从「按库各调一次」改成「一次范围调用 + 配对映射」；新增「某库没结果不降级」「不记 `failed_kb_ids`」「每条结果可归属到库」等断言 | 契约翻转：分库召回再合并不再是契约，旧断言守的是被推翻的行为 | **收紧**：向量调用 1 次、关键词调用 1 次、嵌入 1 次，且旧的按库方法改为直接 `AssertionError` |
| `tests/test_dual_entry_skeleton.py` | 测试更名 `test_retrieve_traverses_every_kb_id` → `test_retrieve_covers_every_kb_in_one_scope_call`；`fake_store.seen == ["kb-b", "kb-a"]` → `scopes == [{"kb-b": "v1", "kb-a": "v1"}]`；`similarity_search` 改为直接 `AssertionError`；新增 `kb_id` 归属断言 | 契约翻转：旧断言只能证明「每个库都被调用过」，证明不了「一次调用覆盖全部配对」 | **收紧**：同时钉住调用次数、配对映射与结果归属 |
| `tests/test_retrieval_mode.py` | 只给替身补 `similarity_search_scope`；**原有断言零改动**；另新增一条「多库一次范围调用」用例 | 替身缺方法，旧断言本身仍然成立（单库时范围版与逐库版语义等价） | **等价 + 新增**：原回归红线不变，新增多库语义守卫 |
| `tests/test_retrieval_log_hygiene.py` | 只给替身补 `similarity_search_scope` 并抽出 `_row()`；**日志断言零改动** | 同上；该文件守的是「日志里不许出现检索原文」，与召回形状无关 | **等价** |
| `tests/test_business_errors.py` | `offline_embedding.queries == ["hello"]` → `== []` | 可见性预校验前移：被拒请求不再先嵌入。旧断言钉的是「先嵌入、后校验」的实现顺序，不是业务意图 | **收紧**：从「嵌入被调用过」变成「拒绝先于召回、不白烧嵌入」 |

补充说明：

- `test_dual_entry_skeleton.py`、`test_retrieval_log_hygiene.py`、`test_business_errors.py` 在第 4 步之前就存在，属于 176 条红线的覆盖范围。
- `test_hybrid_retrieval.py` 与 `test_retrieval_mode.py` 是第 4 步早前票新增的，不在 176 条之内；这里按同一口径一并记录，避免「哪些测试动过」只存在于提交信息里。
- 改后整套离线测试只增不减：`369 passed, 10 skipped` → `418 passed, 11 skipped`（新增用例与四道门禁的说明见 `eval/ACCEPTANCE.md`）。

## 8. 代价

- **`score` 是破坏性变更。** 旧调用方的阈值逻辑必须迁移，且没有「两个语义并存」的过渡期字段。
- **`k=60` 写死**意味着不能按业务调融合的平滑度；要改就得改代码并重跑评测。
- **降级与诚实空可同时出现**，调用方必须同时读 `degraded` 和 `empty_reason`，不能只读一个。
- **不按库记失败**让「某个库是不是坏了」不能只从响应判断，需要看日志或监控；这是用「不误报」换来的。

## 9. 翻案条件

- **要调 `k` 或引入加权融合**：先出新 ADR，再用同一份语料/题目重跑 `eval/ACCEPTANCE.md` 的矩阵；没有新数字不改。
- **产品需要「每库保底名额」**：那会推翻「一次全局召回」，等于换一种检索语义；应作为独立需求评估，不要在现有 RRF 上加隐式配额。
- **调用方普遍需要「相似度阈值」**：优先引导读 `vector_score` / `rerank_score`；只有在两种分列字段都无法满足时，才考虑新增专门的阈值字段。

## 10. 验证

离线守卫（默认 `pytest -q`，不连 PG / 不发网络）：

- `tests/test_rrf_fusion.py`：融合真值表、唯一键、tie-breaker、`k` 的单调性。
- `tests/test_hybrid_retrieval.py`：一次范围召回、对称降级、两路都挂才拒绝、降级原因先到优先。
- `tests/test_retrieval_mode.py`：三种模式、`score` 与分列字段、单模式不降级、多库一次范围调用。
- `tests/test_business_errors.py`：拒绝先于召回、业务码与 HTTP 200。
- `tests/test_vector_scope_search.py` / `tests/test_keyword_query_builder.py`：配对条件、空映射恒假、查询分析器。
- `tests/test_dependency_compat.py`：LangChain 1.x / ragas 0.4.3 的依赖窗口自洽，且 `langchain-community` 仍带 ragas 需要的 `vertexai` 模块（0.4.1 钉死的守卫）。

真实链路与评测数字：`eval/ACCEPTANCE.md`（语料 245 片 / 26 题，基线 `418 passed, 11 skipped`）。

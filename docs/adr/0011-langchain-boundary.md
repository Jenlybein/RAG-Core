# 0011. LangChain 只进 LLM 与 Embedding，不进检索链路

- 状态：已采纳（2026-11-08 第 4 步收口）
- 日期：2026-11-07
- 相关：ADR-0002「六类 Provider」、ADR-0010「检索契约」、`GLOSSARY.md`「检索管道」「召回」「融合」「重排」「模型角色」「available」、`app/providers/llm/`、`app/providers/embedding/`、`app/providers/langchain_runtime.py`、`app/services/retrieval/`、教程 `03-实现开始/04-混合检索/03-具体实现.md`

## 一句话决定

**LLM 接入与 Embedding 接入交给 LangChain**（`init_chat_model` / `Embeddings`）；
**召回、融合、重排编排、降级、响应契约全部自己写**。
`EnsembleRetriever` 这类现成混合检索一律不用。

## 为什么

### 一句话

LangChain 的 `Retriever` 把「检索」压成了三个字 —— **`query → list[Document]`**；
而第 4 步要交付的东西，**恰好就是被这三个字压掉的部分**。

### 先说它当初是为了解决什么

2023 年做 RAG demo 的痛点是「把 5 个部件接起来，别写 500 行」：换向量库、换检索器、换模型，业务代码不改。
所以 `Retriever` 的 KPI 是**接口足够小** —— 小到 Milvus、Qdrant、pgvector、BM25、Web 搜索都能套进去。

它极其成功，因为它够小；它也极其丢信息，**同样**因为够小。

而第 4 步的 KPI 不是「换库不改代码」，是**「出问题时调用方能知道为什么，并能据此做决策」**。
这是产品契约，不是库替换问题 —— 两者要的方向相反：一个要接口更小，一个要信息更多。

> 打个比方：`Retriever` 像 JDBC 的 `ResultSet`，它保证你换数据库不用改代码，
> 但它**不告诉你**这条 SQL 为什么慢、哪个索引没用上、是不是降级成了全表扫描。
> 第 4 步的全部价值，就在这些问题上。

### 它压掉了什么（三条）

**1）排名与分数不可解释。**
`Document` 只有 `page_content` 与 `metadata`。我们要对外的
`vector_score` / `bm25_score` / `vector_rank` / `bm25_rank` / `rrf_score`
五个字段只能塞进 `metadata` 这个无类型字典，序列化出去是一坨嵌套 dict ——
「可解释性」这个验收点当场作废。

**2）「失败」与「没结果」分不开。**
`with_fallbacks()` 的语义是「这个 runnable 失败了，换一个」，由**异常**驱动。
可我们要的语义是「这一路只出了一半结果，我带着这个事实继续，并把事实告诉调用方」。
更要命的是：ES 返回 HTTP 200 但 0 命中（索引压根没建）**不会触发 fallback**，
而那恰恰是最该报警的状态。本章有一行硬约定：

> 某库没结果 ≠ 某库失败。

`with_fallbacks` 里只有「失败」一种状态，分辨不出「没结果」。

**3）同分顺序不确定，评测直接不可复现。**
它的实现是「累加分数 → `sorted()`」，同分时顺序取决于插入顺序，
而插入顺序取决于哪一路先返回 —— 那只取决于网络抖动。
我们的融合函数里有三条**不属于算法、属于可复现性**的东西：

- `k=60` 不允许改（一可调，同一档位在不同调用方就是不同链路）；
- 唯一键是 `(kb_id, chunk_id)` 而不是 `chunk_id`；
- 同分必须有确定性 tie-breaker：双路命中优先 → 最佳名次 → `kb_id` / `chunk_id` 兜底。

### 它管不到的三件事

| 事项 | 为什么必须在框架外 |
| --- | --- |
| 权限校验 | `ensure_visible` 必须在召回**之前**、按 kb 逐个校验、越界整体 `20008` 拒绝。塞进 retriever 的 filter 里 = 把权限逻辑藏进查询字符串，错一次就是越权。 |
| 事务不变量 | 「先删后写 + 由 service 统一提交」是第 3 步花大代价保住的不变量。框架的 `VectorStore.add_texts` 自己管会话，与我们的 `Session` 不共享事务。 |
| 真并行 | 我们两路召回里向量分支是同步 psycopg，必须 `asyncio.to_thread` + 独立 `SessionLocal` 才有真并行。框架的 async 接口不保证底层并行。 |

### 一个查询的对照

框架版：

```python
retriever = EnsembleRetriever(retrievers=[bm25, pg], weights=[0.5, 0.5])
docs = await retriever.ainvoke("错误码 E1024")
# docs[0].metadata == {"source": "...", "relevance_score": 0.032}
```

拿到 3 条，但你**不知道**：第 2 条是向量第 3 名还是 BM25 第 2 名；第 1 条是不是两路都命中；
ES 是不是刚挂掉、这是不是降级凑出的一半结果。

本项目版：

```json
{"chunk_id": "c_9f2", "vector_rank": null, "bm25_rank": 1,
 "vector_score": null, "bm25_score": 12.4, "rrf_score": 0.0164,
 "degraded": true, "degraded_reason": "vector search failed"}
```

一眼读出三件事：这条只有关键词命中（`vector_rank: null`，正是「BM25 补了向量补不了的洞」的现场证据）；
这次是降级结果；降级原因。**这三件事是消融实验的原料。**

## 边界

```text
路由 / MCP 入口
  └─ rag_service.retrieve              ← 契约 / degraded / empty_reason   （自写）
      ├─ services/query/  改写·同义词   ← 自写（内部调 LLMProvider）
      ├─ services/retrieval/ 召回·RRF·重排 ← 自写（fusion 是纯函数，可手算验证）
      │    ├─ VectorStore (pgvector)    ← 自写：租户 / 版本配对 / 距离换算 / 事务
      │    └─ KeywordSearch (ES)        ← 自写：IK / filter 配对 / _score 映射
      ├─ LLMProvider                    ← ★ LangChain
      ├─ Embedding                      ← ★ LangChain
      └─ RerankProvider                 ← 自写（包着 LLMProvider，不自己发 HTTP）
```

一句话：**LangChain 负责「和模型说话」，我们负责「和业务说话」。**

## 本步落点（实现证据，2026-11-08）

### LangChain 只出现在三处

| 位置 | 用途 | 关键约束 |
| --- | --- | --- |
| `app/providers/llm/` | `init_chat_model` 统一入口、结构化输出、`fast` / `strong` 两个模型角色 | 业务层只依赖 `LLMProvider`，不认识框架类型 |
| `app/providers/embedding/` | `Embeddings` 适配（默认 `EMBEDDING_PROVIDER=langchain`） | `embed_query` / `embed_texts` 的签名由我方 ABC 决定 |
| `app/providers/langchain_runtime.py` | 把追踪开关同步到框架真正读的环境变量 | 两个构造点都调用，包括「没配 key、走空实现」的路径 |

### 检索链路零框架代码

- `app/services/query/`：改写只调 `LLMProvider`，同义词扩展是纯函数；没有框架 import。
- `app/services/retrieval/`：`fusion.py` 是纯函数，`pipeline.py` 与 `rerank.py` 只调 Provider 工厂；没有框架 import。
- `app/services/rag_service.py`：组合根，只 import 各 Provider 的工厂函数；响应契约、降级与空结果原因全部在这里与 `pipeline` 里判定。
- 因此「召回 / 融合 / 重排编排 / 降级」四件事都能离线单测，不需要模型 key，也不需要网络。

### 可验证判据

判据盯的是 **import**，不是普通子串：

```bash
grep -rnE '^\s*(from|import)\s+langchain' app/ --include=*.py | grep -v 'app/providers/'
# 必须为空（grep 退出码 1）
```

PowerShell 等价写法：

```powershell
Get-ChildItem app -Recurse -Filter *.py |
  Select-String -Pattern '^\s*(from|import)\s+langchain' |
  Where-Object { $_.Path -notmatch '\\providers\\' }
# 必须无输出
```

**为什么不能写成 `grep -rn "langchain" app/`**：仓库里另有 7 处普通子串命中，全部不是框架调用——`EMBEDDING_PROVIDER="langchain"` 的默认值、`LANGCHAIN_TRACING_V2` 配置名，以及五处解释性注释。裸子串会把「配置名/注释里出现 langchain」误判成「业务层 import 了框架」；判据必须落在 import 上才可执行。

## 考虑过的替代方案

| 方案 | 为什么不选 |
| --- | --- |
| 检索层也用 `EnsembleRetriever` | 它的 RRF 是**带权重的**（`weight_i/(c+rank_i)`，权重可调），与本项目「k=60 写死、纯名次、不许权重」的契约直接对冲；且 `Document` 装不下五个分列字段。省 90 行，换来契约、降级、可解释性全丢。 |
| 只用 `Document` 作为内部传输对象 | 我们已有 dict + Pydantic schema，两套模型互转纯粹是成本。 |
| 用 LangChain 的 `PGVector` 替掉自研 SQL | 会丢掉租户过滤、`(kb_id, index_version)` 配对、`<=>` 距离换算、以及「先删后写」的事务不变量。 |
| 用 `BM25Retriever`（纯 Python）替掉 Elasticsearch | 第 4 步的核心就是 ES + IK 中文分词；纯 Python BM25 给不了中文分词与倒排索引。 |
| 用 `RecursiveCharacterTextSplitter` 替掉自研切块器 | 第 3 步的标题栈、`section_id` 内容哈希、表格整组保留，它一样都给不了。 |

## 代价

- 依赖面变大：`langchain` 1.x 连带 `langgraph` 全家、`langsmith`、`langchain-classic`。
- **必须显式关闭 LangSmith 遥测**（`LANGCHAIN_TRACING_V2=false`）并确认不外发；企业环境要能审计这一点。
- LLM 层的 bug 归因要多隔一层（框架内部重试/解析）。

## 翻案条件

- **第 8 步换专业重排模型时**：LangChain 的 `DocumentCompressor` 抽象在「接 Cohere / BGE / Jina 都是一行」这件事上**确有价值**，届时单独评估、单独接，不影响本步的召回与融合契约。
- 若将来产品需求从「可解释的检索结果」退化成「一个够用的排序列表」，`EnsembleRetriever` 的性价比会反转 —— 那意味着本项目的定位变了。

## 教程落点（供教程侧吸收）

### 落点一：`03-实现开始/04-混合检索/03-具体实现.md` —— 步骤 5 之前插入

标题建议：`### 步骤 5 之前的说明：为什么不直接用现成的混合检索`

正文照抄上面「为什么」一节的「一句话」「先说它当初是为了解决什么」「它压掉了什么（三条）」「它管不到的三件事」「一个查询的对照」，外加 `### 边界` 的图。

### 落点二：`03-具体实现.md` 的「④ 七个误区」新增一条 → 改为「八个误区」

> **误区八：以为 `EnsembleRetriever` 就是 RRF**
> 它确实是 RRF，但它是**带权重的** RRF，而且同分顺序取决于哪一路先返回。
> 用它你就同时失去了两样东西：`k=60` 写死带来的可复现性，以及五个分列字段带来的可解释性。
> 本章手写的那 90 行，写的不是算法，是**评测可复现**的保证。

### 落点三：`02-功能与架构.md` 决策一末尾加脚注

> 注：这一段之所以值得手写，是因为现成实现的 KPI 是「接口足够小」，而这里的 KPI 是「出问题时能说清为什么」。
> 取舍过程见 ADR-0011。

### 落点四：`01-亮点难点.md` 亮点一后补一句

> 补充一个反直觉的经验：**用框架反而不如手写**，当你要交付的是「可解释 + 可降级」而不是「一个排序列表」时。
> 框架优化的是接口的通用性，你优化的是信息的完整性 —— 这两件事会打架。

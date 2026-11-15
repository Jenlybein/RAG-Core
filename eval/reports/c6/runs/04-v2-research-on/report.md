# 引用语料跑批报告 · 20261114T113051Z

- 生成时间：2026-11-14T11:30:51+00:00
- 数据来源：真实检索链路（进程内 retrieve，真实 PostgreSQL + 检索 Provider）
- 开关：graph=off / research=on / rerank=on（实际 开）
- 索引版本（这一次查的是哪一次构建）：`ddae7e35-1858-4861-b9eb-631cfc778feb` → `v2`
- 语料：`eval/corpus-refs/`（3 篇，9 条引用边，指纹 `2dd07e55d50b`）
- 题库：`eval/refs-questions.json`（8 道，四类机制各 2 道；k=5）
- 模型调用：本次实际 **90 次**（开跑前预计 128 次：嵌入 8 + 重排 8 + Research 32 次 LLM 与 ≤40 条子检索）

> **与原项目的数字不可比**：语料与题集都是本仓库自己的。原项目那组效果数字（+4.9% / −82.5% / +3.4pp）在 #138 里已明确不复现，也不作为承诺。

## 这次跑的是什么

| 项 | 值 | 复现命令 |
| --- | --- | --- |
| 数据来源 | live | `uv run python scripts/eval_refs.py --source live` |
| 知识库 | ddae7e35-1858-4861-b9eb-631cfc778feb | `uv run python scripts/eval_refs.py --source live --allow-model-calls --kb-ids ddae7e35-1858-4861-b9eb-631cfc778feb` |
| 返回条数 | top_k=5 | `uv run python scripts/eval_refs.py --top-k 5` |
| 预计模型调用 | 嵌入 8 + Research 32 = 128 | 见 `results.json` 的 `predicted_calls` |

## 一、停止原因分布

Research 的七种固定停止原因（#138 §C）：`evidence_complete`、`no_new_evidence`、`max_rounds`、`duplicate_query`、`timeout`、`budget_exhausted`、`degraded`

| 停止原因 | 次数 | 占比 |
| --- | --- | --- |
| `evidence_complete` | 2 | 25.00% |
| `max_rounds` | 3 | 37.50% |
| `no_new_evidence` | 3 | 37.50% |

### 引用块完整度（`evidence_state`）

完整度由**核心片数量**算出来、不由模型自评（#138 §B）：`complete` = 每个要点都有核心片、`partial` = 部分有、`missing` = 都没有。引用块自检只回答「这一块点的证据下标在不在材料里」，**不做答案质量**（#138 Out of Scope）。

| 完整度 | 题数 |
| --- | --- |
| `complete` | 7 |
| `missing` | 1 |

| 引用块自检 | 值 |
| --- | --- |
| 引用块数 | 26 |
| 带证据下标的块 | 26 |
| 指向材料之外的下标（题数） | 0 |

### 今天链路的终止形态（等价物，**不是**停止原因）

上一节是 Research 状态机的出口；本脚本跑的是检索内核（一次返回），它的终止形态只有这几种。两者并列展示，谁都不冒充谁。

| 终止形态 | 题数 |
| --- | --- |
| `degraded:strong model failed` | 2 |
| `returned` | 6 |

## 二、引用回溯率

判定：每一条命中都拿「文档 + 节路径 + 原文片段」回语料原文里找一遍，找得到才算可回溯。同一段逻辑既服务这里，也服务 `verify-citation`——两处口径不可能分叉。

| 指标 | 值 |
| --- | --- |
| 命中的交付单元数 | 68 |
| 可回溯 | 68 |
| 回溯率 | 1.0000（n/a = 没有样本，不是「回溯率 0」） |

**手工复算一条引用**（把 `<文档> <节号> <片段>` 换成报告里那一条）：

```bash
uv run python scripts/eval_refs.py verify-citation --document refund_policy --section 3 --snippet "银行卡一般 3 个工作日"
```

## 三、延迟

只统计有真实耗时的题（`0.0` 一律算「未测量」，不进百分位）。

| 指标 | 值 |
| --- | --- |
| p50 | 29180.7 ms |
| p95 | 115066.9 ms |
| max | 115066.9 ms |
| 合计 | 365710.2 ms |

## 四、模型调用次数

| 项 | 值 |
| --- | --- |
| 实际调用 | 90 |
| 有调用的题数 | 8 |
| 开跑前预计 | 128（嵌入 8、重排 8、Research 32 次 LLM + ≤40 条子检索） |

- 嵌入：8 题 × 1 次查询向量（--source live，改写关）
- 重排：8 题 × 1 次重排（重排开着）
- Research：会真的走 /rag/research：每题 ≤4 次模型调用 + ≤5 条子检索（每条 1 次嵌入 + 1 次重排），按上限估
- 口径：每次检索 = 1 次查询嵌入 + （响应里有重排事实时）1 次重排；一次 Research = `metadata.model_calls` 次 LLM + `rounds.searches` 条子检索 × （嵌入 [+ 重排]）。**按响应里的事实数，不按配置估**。

## 能力开关状态

两栏分开读：**状态**是探测代码得出的（这台机器上有没有这一步），**这次跑**是开关与脚本接线共同决定的（这次有没有真的走那条路）。两者不是同一件事（#156）。

| 开关 | 本次取值 | 状态（探测） | 这次跑 | 说明 |
| --- | --- | --- | --- | --- |
| graph | off | `available` | **未采集** | 注入服务已装配（`app/services/graph_injection.py` + 组合根的 `graph_factory`）；注入项 score=0、按本次请求的库与版本过滤，名次交给重排 |
| research | on | `available` | 已采集 | `/rag/research` 与 MCP `rag_research` 已就位（同一份 `gated_research`）；一次调用硬顶 3 次模型调用（含生成兜底 4 次） |

## 逐题明细

逐题明细见 `results.json` 的 `per_question`；下表是摘要。覆盖 = 期望命中的节取回了几节（它不进四类指标：覆盖率是题集的事，四类指标是链路的事）。

| 题号 | 机制 | 命中 | 覆盖 | 终止形态 | 延迟 | 模型调用 |
| --- | --- | --- | --- | --- | --- | --- |
| r01 | cross_section | 10 | 2/2 | `degraded:strong model failed` | 115066.9 ms | 14 |
| r02 | cross_section | 7 | 2/2 | `returned` | 15170.3 ms | 11 |
| r03 | cross_document | 8 | 2/2 | `returned` | 41477.3 ms | 9 |
| r04 | cross_document | 8 | 2/2 | `returned` | 27027.8 ms | 11 |
| r05 | parallel_points | 9 | 3/3 | `returned` | 29180.7 ms | 9 |
| r06 | parallel_points | 7 | 3/3 | `degraded:strong model failed` | 64681.7 ms | 14 |
| r07 | dependent_hop | 8 | 3/3 | `returned` | 28781.3 ms | 11 |
| r08 | dependent_hop | 11 | 3/3 | `returned` | 44324.2 ms | 11 |

## 复算与自检

```bash
# 默认（空跑自检）：不连库、不发网络、零模型调用，产出结构正确的报告
uv run python scripts/eval_refs.py

# 引用回溯率机械复算：给一条引用，还原到语料原文（退出码 0 = 能还原）
uv run python scripts/eval_refs.py verify-citation --document points_policy --section 2.2 --snippet "银卡及以上时，200 元的固定上限提高到 500 元"

# 冻结资产自检：eval/corpus 与 eval/baseline 是否被改过（多出文件也算失败）
uv run python scripts/eval_refs.py check-frozen
```

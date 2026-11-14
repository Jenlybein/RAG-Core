# ② 图谱开 / 关（v2 索引，同 8 题）

**结论先说**：这组对照**没有测到图谱的价值**，只测到「开着它不坏事」。

| | 图谱关 | 图谱开 |
| --- | --- | --- |
| 命中集合 | 5 条/题，8 题 | **完全相同** |
| 注入条数（`metadata.retrieval.graph.injected`） | 0（未装图谱） | **0** |
| 种子数（`seeds`） | — | 10 条/题，合计 80 |
| 期望节覆盖 | 17/20 | 17/20 |
| 引用回溯 | 40/40 = 1.0000 | 40/40 = 1.0000 |
| 模型调用 | 16 | 16 |
| 延迟 p50 / p95 / 合计 | 383.55 / 3033.37 / 5800.20 ms | 406.05 / 3033.83 / 5923.15 ms |

**注入为什么是 0（已定位，不是故障）**：候选池已经装下了整个库。
在同一个库上单跑一次检索，`metadata.retrieval` 是这样：

```json
{"mode": "hybrid", "rerank": {"applied": true, "candidates": 15, "returned": 15},
 "graph": {"injected": 0, "seeds": 10, "by_relation": {}}}
```

`candidates = 15`，而本语料一共就 **15 个节**（3 篇 × 5 节）——混合召回两路各取 20 条，
融合之后**每一节都在池子里**。注入的作用是「把种子的邻居补进候选」，而邻居早就在池里；
更关键的是注入只在「目标节不在池中」时才有意义，于是 `injected=0`、`by_relation={}`。
种子 10 条说明图谱**真的被调用、真的查到了边**（v2 有 9 条参考边 + 22 条兄弟边 + 2 条父边），
只是没有可补的。

**这一组能下的结论**：在 v2 索引上，打开图谱不会改变这 8 题的命中、不会拉长到影响使用的延迟
（p50 +22.5 ms，样本内差异）、不会引入悬空引用。**不能**下的结论：图谱注入有用或没用——
本语料的结构决定了这组对照是「饱和」的。

## 一、逐题

| 题号 | 机制 | 关：命中 | 开：命中 | 关：注入 | 开：注入 | 命中差 |
| --- | --- | --- | --- | --- | --- | --- |
| r01 | cross_section | refund_policy:3 / refund_policy:2 / points_policy:1 / promo_rules:4 / promo_rules:3 | 同左 | — | 0（种子 10） | — |
| r02 | cross_section | points_policy:3 / refund_policy:5 / promo_rules:3 / points_policy:1 / refund_policy:3 | 同左 | — | 0（种子 10） | — |
| r03 | cross_document | promo_rules:3 / promo_rules:4 / refund_policy:5 / points_policy:3 / refund_policy:4 | 同左 | — | 0（种子 10） | — |
| r04 | cross_document | points_policy:2.2 / promo_rules:1 / promo_rules:3 / points_policy:4 / points_policy:1 | 同左 | — | 0（种子 10） | — |
| r05 | parallel_points | promo_rules:3 / points_policy:3 / refund_policy:5 / promo_rules:4 / refund_policy:1 | 同左 | — | 0（种子 10） | — |
| r06 | parallel_points | promo_rules:1 / promo_rules:2 / promo_rules:3 / points_policy:2.2 / refund_policy:4 | 同左 | — | 0（种子 10） | — |
| r07 | dependent_hop | promo_rules:3 / refund_policy:5 / points_policy:3 / refund_policy:4 / refund_policy:3 | 同左 | — | 0（种子 10） | — |
| r08 | dependent_hop | points_policy:4 / refund_policy:5 / refund_policy:4 / promo_rules:3 / points_policy:3 | 同左 | — | 0（种子 10） | — |

## 二、开关真的切了吗（「接线为真」的证据）

不是看命令行说了什么，而是看**响应里有没有图谱那一栏**：

- 图谱关：`exercised.graph = false`，响应的 `metadata.retrieval` 里**没有** `graph` 键
  （`graph_factory=None`，装配层根本不装），`metrics.graph_injection.graphs_reported = 0`；
- 图谱开：`exercised.graph = true`，`graphs_reported = 8`（8 题都有），`seeds = 80`，
  `injected = 0`。

`graph_factory` 在关的时候传 `None`、开的时候传 `DbSectionGraph`，这是**组合根上的真实接缝**，
不是报告里按配置推断的标签（`scripts/eval_refs.py::graph_factory_for`）。此前那版脚本把
`exercised` 硬编码成两侧都为真，本票已改成从响应回读。

## 三、悬空引用与回溯

| | 关 | 开 |
| --- | --- | --- |
| 引用块 / 带证据 | 40 / 40 | 40 / 40 |
| 悬空证据下标（`dangling_evidence_ids`） | 0 | 0 |
| 各题命中内容能否在库中定位 | 全部可定位 | 全部可定位 |

图谱注入的候选 `score = 0`（ADR-0027），排序上永远排在真实命中之后；本轮注入为 0，所以这一条
没有被真正检验——语料放大之后再复跑这一组才有意义。

## 四、覆盖：两版都缺同样的 3 个期望节

`r03` 缺 `refund_policy:2`、`r07` 缺 `refund_policy:2`、`r08` 缺 `points_policy:2.2`——
开关图谱**没有**补上它们（原因同上：这三个节本来就在候选池里，只是没进 top_k，而注入不做
「重排后补位」）。这三题正是最该由「引用边」帮忙的场景（跨篇依赖跳一步），所以下一轮放大
语料时，优先看这三题。

## 五、复跑

```bash
export ELASTICSEARCH_INDEX=rag_chunks_c6
uv run python scripts/eval_refs.py --source live --allow-model-calls \
  --kb-ids "$C6_KB_ID" --tenant-id tenant_c6 --graph off --rerank on   # 关
uv run python scripts/eval_refs.py --source live --allow-model-calls \
  --kb-ids "$C6_KB_ID" --tenant-id tenant_c6 --graph on --rerank on    # 开
```

> 图谱注入的开关归属是**重排**（ADR-0027：`options.rerank and graph_factory` 才装配），
> 所以两组都必须 `--rerank on`，否则「图谱开」这一侧根本不会装注入，测出来的是两件事。

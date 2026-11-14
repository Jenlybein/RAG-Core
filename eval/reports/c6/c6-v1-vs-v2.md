# ① v1 vs v2：同一批 8 题、同一份分句，两版索引的逐部分对照

**结论先说**：命中集合 **8/8 题完全相同**、节身份 **15/15 逐节相同**、回溯率两边都是 **40/40**；
真正的差别在**引用边**——v1 只有 **5 条**参考边，v2 有 **9 条**（`manifest.json` 的
`reference_inventory` 期望 9）。

这**不是**「v1 也算对了」：本语料只有 3 篇 / 15 个节，混合召回深度 20 已把整个库放进候选池，
图谱注入在两边都**无一条可注入**（详见 [② 图谱报告](c6-graph-on-off.md)）——所以「命中相同」
只能说明**这个语料太小、边没机会影响结果**，不能说明边丢了无所谓。

- 运行版本：`v1` = 生产入库路径逐篇入库（`scripts/prep_eval_c6.py`），`v2` = #142 整版重建
- 两版**用的是同一份分句**（c6 取的是「原样重建」这一档），节身份逐节相同
- 原始产物：[`runs/01-v1-graph-off`](runs/01-v1-graph-off/results.json)、
  [`runs/02-v2-graph-off`](runs/02-v2-graph-off/results.json)
- 两组都：`--graph off --rerank on`，8 题，`top_k=5`，`fold_to_sections=true`

## 一、逐题命中（`文档:节`，按返回顺序）

| 题号 | 机制 | v1 命中 | v2 命中 | 新增 | 丢失 |
| --- | --- | --- | --- | --- | --- |
| r01 | cross_section | refund_policy:3 / refund_policy:2 / points_policy:1 / promo_rules:4 / promo_rules:3 | 同左 | — | — |
| r02 | cross_section | points_policy:3 / refund_policy:5 / promo_rules:3 / points_policy:1 / refund_policy:3 | 同左 | — | — |
| r03 | cross_document | promo_rules:3 / promo_rules:4 / refund_policy:5 / points_policy:3 / refund_policy:4 | 同左 | — | — |
| r04 | cross_document | points_policy:2.2 / promo_rules:1 / promo_rules:3 / points_policy:4 / points_policy:1 | 同左 | — | — |
| r05 | parallel_points | promo_rules:3 / points_policy:3 / refund_policy:5 / promo_rules:4 / refund_policy:1 | 同左 | — | — |
| r06 | parallel_points | promo_rules:1 / promo_rules:2 / promo_rules:3 / points_policy:2.2 / refund_policy:4 | 同左 | — | — |
| r07 | dependent_hop | promo_rules:3 / refund_policy:5 / points_policy:3 / refund_policy:4 / refund_policy:3 | 同左 | — | — |
| r08 | dependent_hop | points_policy:4 / refund_policy:5 / refund_policy:4 / promo_rules:3 / points_policy:3 | 同左 | — | — |

**新增 0 条、丢失 0 条**——逐题逐位置都相同，连返回顺序都没变。

## 二、节身份（每节一个稳定 id，跨版本必须一致）

| 版本 | 篇数 | 节身份数 | 与另一版逐节相同 |
| --- | --- | --- | --- |
| v1 | 3 | 15 | ✅ 是 |
| v2 | 3 | 15 | ✅ 是 |

按 `(文档, meta.section_id)` 拆开比：3 篇文档、每篇 5 节，v1 与 v2 的集合**逐节完全相等**。
这是「切分点没变」的直接证据——也说明本轮的 v1/v2 **没有覆盖「切分点变化」这一维**
（见第五节的重跑方法）。

## 三、引用边：v1 5 条 / v2 9 条（本轮唯一的真实差异）

| 版本 | parent | sibling | reference | 期望（manifest） |
| --- | --- | --- | --- | --- |
| v1 | 2 | 22 | **5** | 9 |
| v2 | 2 | 22 | **9** | 9 |

v2 多出来的 4 条，全是「**先入库的文档指向后入库的文档**」的跨篇引用：

| 源篇 / 节 | → 目标篇 / 节 |
| --- | --- |
| 会员订单退款政策 `6d3041af…` | 促销活动与优惠券使用规则 `8154d9b1…` |
| 会员订单退款政策 `c75a9365…` | 积分规则 `10f983ae…` |
| 促销活动与优惠券使用规则 `8154d9b1…` | 积分规则 `62d4920e…`（`46fdbc13…` 侧同理） |
| 促销活动与优惠券使用规则 `bbfed911…` | 积分规则 `46fdbc13…` |

**原因**：逐篇入库时，一篇文档的引用只能在「此刻已经入库的文档」里解析。入库顺序是
`refund_policy → promo_rules → points_policy`，所以 `refund_policy` 指向后两篇的 4 条边在它
自己那一轮就找不到目标、当场丢弃；反过来，后两篇指向 `refund_policy` 的边能建（那时它已存在）。
整版重建看得见整库，9 条一条不少。

**顺带修掉的那个缺陷**（本轮在真库上复现并修复）：整版重建时逐篇重跑「删边 + 建边」，
会把「以该文档为目标」的入边一起删掉——9 条参考边只剩 5 条，而构建照样报 `ready`、计数自洽。
修法是「整版一次删 → 一次插 → 清悬空」，用例见
`tests/test_section_edge_service.py::test_the_version_pass_keeps_the_inbound_edges_of_the_document_it_touches_last`。

> 两边都是 5 → 不是巧合：v1 的 5 条正是「入库顺序决定的子集」，v2 修复后才是 9 条。

## 四、读数汇总

| 指标 | v1 | v2 |
| --- | --- | --- |
| 检索返回题数 | 8/8 | 8/8 |
| 期望节覆盖（`coverage`） | 17/20 | 17/20 |
| 引用回溯（`citation_traceability`） | 40/40 = 1.0000 | 40/40 = 1.0000 |
| 图谱注入（`metadata.retrieval.graph`） | 0（未开） | 0（未开） |
| 模型调用 | 16（8 嵌入 + 8 重排） | 16 |
| 延迟 p50 / p95 / 合计 | 384.14 / 2887.19 / 5642.83 ms | 383.55 / 3033.37 / 5800.20 ms |
| 生效索引版本（响应回读，不是命令行说的） | `ddae7e35…: v1` | `ddae7e35…: v2` |

两版都缺同样的 3 个期望节：`r03` 缺 `refund_policy:2`、`r07` 缺 `refund_policy:2`、
`r08` 缺 `points_policy:2.2`——**这是 8 题本身的设计**（跨篇题故意把答案放在必须靠
第二跳/注入才够得着的节上），不是版本差异。

## 五、这一轮**没有**覆盖到的

- **切分点变化**：两版用的是同一份分句（节身份逐节相同）。要覆盖这一维，重跑时把
  `scripts/prep_eval_c6.py` 的 `BUILD_VERSION` 换一个分句参数（或直接对 v2 跑一次改过
  分句器的整版重建），节身份会出现「新增 / 消失」，那时本报告的第二节才有对照意义：

  ```bash
  # 换分句后重建 → 第三版，再与本报告的两列并排
  export ELASTICSEARCH_INDEX=rag_chunks_c6
  uv run python scripts/rebuild_kb_version.py --kb-id "$C6_KB_ID" --version v3 --activate
  uv run python scripts/eval_refs.py --source live --allow-model-calls \
    --kb-ids "$C6_KB_ID" --tenant-id tenant_c6 --graph off --rerank on
  ```

- **边的价值**：本语料注入为 0（下一份报告给出证据），所以「v1 少 4 条边」在命中上无可观测
  后果。要测「边有没有用」，语料必须大到候选池装不下整库（≥ 数百个节），否则召回自己就把
  邻居带进来了。

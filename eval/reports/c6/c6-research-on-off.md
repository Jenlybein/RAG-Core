# ③ Research 开 / 关（v2 索引，同 8 题）

**结论先说**：Research 侧**跑通了、能给答案、引用块 100% 可回溯**，代价是延迟两个数量级
（p50 0.38 s → 29.2 s）和模型调用 5.6 倍（16 → 90）。8 题里 **7 题 `evidence_state=complete`**，
1 题 `missing`；**悬空证据下标 0**。

- Research 关 = 一次检索内核（同 ② 的「图谱关」那一组，直接复用它的读数）
- Research 开 = `gated_research`（`/rag/research` 的门禁函数），固定 5 步状态机
- 两侧都是 v2 索引、同样 8 题、`--rerank on`
- 原始产物：[`runs/02-v2-graph-off`](runs/02-v2-graph-off/results.json)、
  [`runs/04-v2-research-on`](runs/04-v2-research-on/results.json)

## 一、总账

| 指标 | Research 关（检索） | Research 开 |
| --- | --- | --- |
| 有答案的题 | 8（返回材料） | **8/8 `answered=true`** |
| 模型调用 | 16（8 嵌入 + 8 重排） | **90**（27 次状态机 LLM + 33 次子检索 × 2） |
| 延迟 p50 | 383.55 ms | **29 180.72 ms** |
| 延迟 p95 | 3 033.37 ms | **115 066.87 ms** |
| 延迟合计（8 题串行） | 5 800.20 ms | **365 710.17 ms**（≈ 6 分 6 秒） |
| 引用回溯 | 40/40 = 1.0000 | **68/68 = 1.0000** |
| 引用块 / 带证据 | —（检索不给引用块） | **26 / 26** |
| 悬空证据下标 | 0 | **0** |
| 期望节覆盖 | 17/20 | **20/20** |
| 检索终止原因 | `returned ×8` | `returned ×6`、`degraded:strong model failed ×2` |

**覆盖从 17/20 涨到 20/20**：检索侧缺的那 3 个期望节（`r03`/`r07`/`r08` 的跨篇节），Research
靠「二次检索 / 多轮」拿到了。这是本轮唯一一处「开比关强」的硬读数。

## 二、逐题

| 题号 | 停止原因 | 完整度 | 引用块 | 带证据 | 材料 | 模型调用 | 延迟(ms) | 悬空下标 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r01 | `max_rounds` | **missing** | 0 | 0 | 10 | 14 | 115 066.87 | — |
| r02 | `no_new_evidence` | complete | 1 | 1 | 7 | 11 | 15 170.34 | — |
| r03 | `evidence_complete` | complete | 6 | 6 | 8 | 9 | 41 477.26 | — |
| r04 | `no_new_evidence` | complete | 2 | 2 | 8 | 11 | 27 027.80 | — |
| r05 | `evidence_complete` | complete | 5 | 5 | 9 | 9 | 29 180.72 | — |
| r06 | `max_rounds` | complete | 3 | 3 | 7 | 14 | 64 681.66 | — |
| r07 | `no_new_evidence` | complete | 6 | 6 | 8 | 11 | 28 781.32 | — |
| r08 | `max_rounds` | complete | 3 | 3 | 11 | 11 | 44 324.20 | — |

### 停止原因分布（7 种原因里出现了 3 种）

| 原因 | 题数 |
| --- | --- |
| `no_new_evidence` | 3 |
| `max_rounds` | 3 |
| `evidence_complete` | 2 |

### 完整度分布（`evidence_state`）

| 完整度 | 题数 |
| --- | --- |
| `complete` | 7 |
| `missing` | 1 |

**唯一的 `missing` 是 r01**，它的形状值得记下来：跑到 `max_rounds`（1 轮追询用完）、
115 秒、14 次模型调用、10 份材料，却**一块引用都没产出**（`citation_blocks=0`）。看服务端日志，
这一题在中途两次撞上 `strong_model_fallback='strong model failed'`——**强模型那一步失败**，
状态机按上限兜底收尾，答案被判 `malformed answer output` 而拒收。这不是「检索没找到材料」
（材料 10 份），而是「生成那一步没跑完」。

## 三、上一轮为什么整组 `degraded`（已修，记录原因）

第一次跑 Research 时 8 题全是 `stop_reason=degraded`、`answered=false`、`evidence_state=missing`，
日志是 `research planning failed / stage=plan` + `strong model failed`。原因是**评测进程没接生产
模型链**：`eval_refs.py` 直接读 `.env`（单供应商 key，那个账号 403），而 `eval_runner` 早就通过
`bind_llm_role_resolver()` 接到 `model_configs` 的角色指针上。本票给 `eval_refs.py` 也接上了同一条链，
并把「用的是哪一条」写进产物：

```json
"llm_chain": {"llm_endpoint_source": "model_configs",
              "llm_model": "deepseek-v4-1-flash-economy-261017",
              "llm_config_id": "78b47ae9-7691-4566-8d2c-d95adbfd3a5e"}
```

**这条要写进报告首页的理由**：`degraded` 的读数看起来像「Research 不行」，实际是「评测没接对
链路」。不把 `llm_chain` 记进产物，事后无法区分这两种情况。

## 四、延迟怎么读

- Research 的 29 s p50 里，**大头是 LLM 的串行步数**（规划 → 3 条子检索 → 判断 → 追询 →
  判断 → 生成），不是检索本身：子检索 33 次、每次 1 嵌入 + 1 重排，按检索侧 p50 0.38 s 估
  约 12 s，其余是模型时延。
- 状态机有硬上限（`STAGE_BUDGET_SECONDS=90`、`MAX_MODEL_CALLS=3` / 带兜底 4、
  `MAX_FOLLOWUP_ROUNDS=1`、`FINALIZER_TIMEOUT_SECONDS=30`）。r01 的 115 s 说明**单题的
  真实耗时可以越过 90 s 的阶段预算**——预算是「阶段」的，不是「整题」的；整题上限由
  「步数 × 阶段预算」决定。这一点与本轮 8 题的观测一致（最长 115 s，最短 15 s）。
- 本轮 `research_qps=1` / `research_daily=100` 已按票面写好，但**本机没开限流**
  （`RATE_LIMIT_ENABLED=false`），所以这 8 题没有撞配额；配额那一层的行为由专门的配额用例覆盖，
  不在本报告范围内。

## 五、引用块完整度

| 项 | 值 |
| --- | --- |
| 引用块总数 | 26 |
| 带证据（能指回材料下标） | **26（100%）** |
| 悬空证据下标 | **0 题** |
| 引用回溯（块内引用的材料 id 能否在命中里找到） | **68/68 = 1.0000** |

`citation_blocks` 与 `citations_with_evidence` 逐个相等，说明交付的每个引用块都能落到具体的
材料上——没有「写了引用但没有出处」的块，也没有指向不存在下标的块。

## 六、复跑

```bash
export ELASTICSEARCH_INDEX=rag_chunks_c6
# key 只在 gitignored 的 eval/refs-runs/c6-state.json 里，不要贴进命令行历史/报告
EVAL_API_KEY=$(python -c "import json;print(json.load(open('eval/refs-runs/c6-state.json'))['key_value'])") \
uv run python scripts/eval_refs.py --source live --allow-model-calls \
  --kb-ids "$C6_KB_ID" --tenant-id tenant_c6 --graph off --research on --rerank on
```

开跑前脚本会先印预计调用（本轮印的是上限 128，实测 90）：Research 一路的预测是
「8 题 × ≤4 次 LLM + 8 题 × ≤5 条子检索 ×（1 嵌入 + 1 重排）」，**是上限不是期望值**。

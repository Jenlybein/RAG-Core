# 评测门禁报告 · 20261112T125728Z

- 生成时间：2026-11-12T12:57:29+00:00
- 归档目录：`eval/baseline-rerank`（`report.md` + `results.json` + `environment.json`）
- 来源 run：`eval/runs/20261112T125728Z`（本目录是它的基线副本）
- 状态：**不可比（本次不参与门禁）**

> 门禁指标零 LLM 依赖：两个数只用 chunk id 与标注算，不需要裁判模型。

## 这次跑的是什么

| 项 | 值 | 复现命令 |
| --- | --- | --- |
| 检索方式 | 进程内 `retrieve()`（真实 PostgreSQL + Elasticsearch + 嵌入；不经 HTTP） | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select environment.retrieval.mode` |
| 检索模式 / 返回条数 | hybrid / top_k=5 | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select environment.retrieval` |
| 嵌入模型 | qwen3.7-text-embedding（1536 维，langchain） | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select environment.embedding.model` |
| 关键词一路 | elasticsearch / 索引 rag_chunks_eval | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select environment.keyword.index` |
| 语料版本 | `1946e4e5954f`：21 篇 / 469 片 | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select corpus.version` |
| 切块口径 | structure / 800（overlap 100，表格行上限 10）（这一行与语料版本一起决定「两次跑是不是同一把尺子」） | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select environment.chunking` |
| 题库 | 137 道（进主平均 127 道；排除 absent 6、false_premise 4） | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.excluded` |
| 重排预算 | 超时 60s / 候选封顶 20 片 / 单条截断 1024 字符（**评测抬高过上限**；生产预算 15s） | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select environment.retrieval.rerank_timeout_seconds` |
| 开关状态 | 重排 开、改写 关、同义词扩展 关 | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select environment.retrieval` |
| 门禁指标 | ragas ID 版 `context_precision` / `context_recall`（只吃 chunk id，零 LLM、零外部服务） | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select environment.gate_metrics` |

完整环境快照（运行时 / git 提交 / 全部开关）见同目录的 `environment.json`。

## 门禁判定

**不可比（本次不参与门禁）**

- 判据：整体不低于基线 −1.00pp；单题型退化不超过 3.00pp。
- 结论：本次与基线不可比：判定口径不同（重排开关：本次 开、基线 关）；换了尺子就不能谈「退化了多少」。因此这里不给出任何「变好 / 变坏」的结论，也不参与门禁。
- 基线：`20261110T224302Z`（来源 `eval/baseline/results.json`，生成于 2026-11-10T22:43:04+00:00）
- 参与平均：127 道题；已评分 CP 127/127、CR 127/127。
- 口径：CP = 本次返回的**不同**切片里有多少在标注内（分母是返回条数），CR = 标注的切片被找回了几成（分母是标注条数）。两者都是**集合**口径，与 `eval/ACCEPTANCE.md` 里按行算的 P@5 / R@5 同量级、但不是同一把尺子——跨文档比较前先看分母。

| 指标 | 本次 | 基线 | Δ(pp) | 判据 | 结论 | 复现命令 |
| --- | --- | --- | --- | --- | --- | --- |
| context_precision | 0.2205 | n/a | — | ≥ -1.00pp | — | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.overall.context_precision` |
| context_recall | 0.8776 | n/a | — | ≥ -1.00pp | — | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.overall.context_recall` |

（本次还没有可比基线，所以「Δ」与「结论」是空的；本次的 CP / CR 仍然照实列出——报告不因为还没基线就少一列数字。）

### 分题型（单类退化 > 3pp 判红；题数为本次进平均的题数）

| 题型 | 题数 | CP | ΔCP(pp) | CR | ΔCR(pp) | 结论 | 复现命令 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| colloquial | 11 | 0.1818 | — | 0.9091 | — | — | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.by_type.colloquial` |
| counterfactual | 13 | 0.1846 | — | 0.9231 | — | — | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.by_type.counterfactual` |
| cross_section | 17 | 0.2824 | — | 0.6252 | — | — | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.by_type.cross_section` |
| exact_code | 5 | 0.2000 | — | 1.0000 | — | — | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.by_type.exact_code` |
| model_number | 15 | 0.2267 | — | 0.9048 | — | — | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.by_type.model_number` |
| negative_scope | 14 | 0.2143 | — | 0.9524 | — | — | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.by_type.negative_scope` |
| noise_robust | 17 | 0.2000 | — | 0.9020 | — | — | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.by_type.noise_robust` |
| rare_token | 6 | 0.2000 | — | 1.0000 | — | — | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.by_type.rare_token` |
| synonym | 12 | 0.2000 | — | 1.0000 | — | — | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.by_type.synonym` |
| table_lookup | 17 | 0.2588 | — | 0.7996 | — | — | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.by_type.table_lookup` |

## 延迟（逐题耗时，P50 / P95）

来源：`uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.latency`，即逐题明细里每一道的 `duration_ms`（与 CP/CR 同一份归档，没有另采一次）。它**不参与门禁**：判据只有 CP / CR 两条。

- 题数：127 道（没被计时的题不进分母）；整轮总时长 1472.50s。

| 指标 | 毫秒 | 复现命令 |
| --- | --- | --- |
| 均值 | 11341.7 | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.latency` |
| P50 | 10064.0 | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.latency` |
| P95 | 20647.1 | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.latency` |
| 最慢 | 33924.4 | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.latency` |

口径：P50 / P95 取升序第 ⌈p/100 × N⌉ 个（N=127，不插值）——P50 = 第 64 个、P95 = 第 121 个。插值会造出一个从来没发生过的耗时。

## 重排（应用率与它自己的耗时）

- 重排开关：开；逐题明细里留下重排事实的题数 127/127 道。
- **应用率 100.0%**（127/127 道真的排过）。它不是修辞：只要有一道题降级成「按融合顺序返回」，这份 CP / CR 就混着两种状态，质量差异会归因到「谁没超时」而不是「重排模型好不好」。

| 指标 | 值 | 复现命令 |
| --- | --- | --- |
| 应用率（%） | 100.0 | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.rerank` |
| 重排耗时均值（ms） | 10866.3 | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.rerank` |
| 重排耗时 P50（ms） | 9593.3 | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.rerank` |
| 重排耗时 P95（ms） | 20263.7 | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.rerank` |
| 重排耗时最慢（ms） | 33451.6 | `uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.rerank` |

- **按生产预算 15 秒会有多少道降级**：至少 16/127 道（q26、q31、q35、q36、q41、q48、q52、q58、q61、q69、q93、q97、q106、q114、q133、q136）——这些题的重排耗时已超过 15 秒，在生产预算下一定超时、降级成「按融合顺序返回」。**这是下限不是定值**：掐断后不再排队等，那一轮的耗时分布会变。
- 本轮用的是 60 秒上限（只在这一次评测进程里抬高，`.env` 与生产预算都没动）：所以这份基线是「每题都真的排过」的读数，不能与 15 秒下的读数直接相减。

口径：耗时只统计 `applied=true` 那些题（降级路径里的时长是「失败那次尝试」，混进来就不是「重排要多久」）；分母是**留下了重排事实的题**，检索本身失败的题没有重排可言，不计入。逐题的 `candidates` / `returned` 见 `per_question[*].rerank`——`returned` 是重排吐出的条数（折叠前，开着折叠时是 `top_k × 3`），不是最终返回条数。

## 未评分记录（不计入平均）

没有未评分记录：本次每一道进平均的题都算出了两个指标。

## 逐题明细

来源：`uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select per_question`；每一行是它的第 N 项，`--select per_question.<N>`（0 起）可单独打印。

| # | 题号 | 题型 | 返回 | 标注 | CP | CR | 未评分原因 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | q01 | exact_code | 5 | 1 | 0.2000 | 1.0000 | — |
| 1 | q02 | exact_code | 5 | 1 | 0.2000 | 1.0000 | — |
| 2 | q03 | exact_code | 5 | 1 | 0.2000 | 1.0000 | — |
| 3 | q04 | exact_code | 5 | 1 | 0.2000 | 1.0000 | — |
| 4 | q05 | exact_code | 5 | 1 | 0.2000 | 1.0000 | — |
| 5 | q06 | model_number | 5 | 1 | 0.2000 | 1.0000 | — |
| 6 | q07 | model_number | 5 | 1 | 0.2000 | 1.0000 | — |
| 7 | q08 | model_number | 5 | 1 | 0.2000 | 1.0000 | — |
| 8 | q09 | colloquial | 5 | 1 | 0.2000 | 1.0000 | — |
| 9 | q10 | colloquial | 5 | 1 | 0.2000 | 1.0000 | — |
| 10 | q11 | colloquial | 5 | 1 | 0.2000 | 1.0000 | — |
| 11 | q12 | synonym | 5 | 1 | 0.2000 | 1.0000 | — |
| 12 | q13 | synonym | 5 | 1 | 0.2000 | 1.0000 | — |
| 13 | q14 | synonym | 5 | 1 | 0.2000 | 1.0000 | — |
| 14 | q15 | synonym | 5 | 1 | 0.2000 | 1.0000 | — |
| 15 | q16 | cross_section | 5 | 2 | 0.4000 | 1.0000 | — |
| 16 | q17 | cross_section | 5 | 2 | 0.4000 | 1.0000 | — |
| 17 | q18 | cross_section | 5 | 2 | 0.4000 | 1.0000 | — |
| 18 | q19 | cross_section | 5 | 2 | 0.4000 | 1.0000 | — |
| 19 | q23 | rare_token | 5 | 1 | 0.2000 | 1.0000 | — |
| 20 | q24 | rare_token | 5 | 1 | 0.2000 | 1.0000 | — |
| 21 | q25 | rare_token | 5 | 1 | 0.2000 | 1.0000 | — |
| 22 | q26 | rare_token | 5 | 1 | 0.2000 | 1.0000 | — |
| 23 | q27 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 24 | q28 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 25 | q29 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 26 | q30 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 27 | q31 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 28 | q32 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 29 | q33 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 30 | q34 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 31 | q35 | counterfactual | 5 | 1 | 0.2000 | 1.0000 | — |
| 32 | q36 | counterfactual | 5 | 1 | 0.2000 | 1.0000 | — |
| 33 | q37 | counterfactual | 5 | 1 | 0.2000 | 1.0000 | — |
| 34 | q38 | counterfactual | 5 | 1 | 0.2000 | 1.0000 | — |
| 35 | q39 | table_lookup | 5 | 1 | 0.2000 | 1.0000 | — |
| 36 | q40 | table_lookup | 5 | 1 | 0.2000 | 1.0000 | — |
| 37 | q41 | table_lookup | 5 | 1 | 0.2000 | 1.0000 | — |
| 38 | q42 | table_lookup | 5 | 1 | 0.2000 | 1.0000 | — |
| 39 | q43 | table_lookup | 5 | 1 | 0.2000 | 1.0000 | — |
| 40 | q44 | table_lookup | 5 | 1 | 0.2000 | 1.0000 | — |
| 41 | q45 | table_lookup | 5 | 1 | 0.2000 | 1.0000 | — |
| 42 | q46 | negative_scope | 5 | 1 | 0.2000 | 1.0000 | — |
| 43 | q47 | negative_scope | 5 | 1 | 0.2000 | 1.0000 | — |
| 44 | q48 | negative_scope | 5 | 1 | 0.2000 | 1.0000 | — |
| 45 | q49 | negative_scope | 5 | 1 | 0.2000 | 1.0000 | — |
| 46 | q50 | synonym | 5 | 1 | 0.2000 | 1.0000 | — |
| 47 | q51 | synonym | 5 | 1 | 0.2000 | 1.0000 | — |
| 48 | q52 | synonym | 5 | 1 | 0.2000 | 1.0000 | — |
| 49 | q53 | synonym | 5 | 1 | 0.2000 | 1.0000 | — |
| 50 | q54 | colloquial | 5 | 1 | 0.2000 | 1.0000 | — |
| 51 | q55 | colloquial | 5 | 1 | 0.0000 | 0.0000 | — |
| 52 | q56 | colloquial | 5 | 1 | 0.2000 | 1.0000 | — |
| 53 | q57 | colloquial | 5 | 1 | 0.2000 | 1.0000 | — |
| 54 | q58 | cross_section | 5 | 2 | 0.2000 | 0.5000 | — |
| 55 | q59 | cross_section | 5 | 2 | 0.4000 | 1.0000 | — |
| 56 | q60 | cross_section | 5 | 2 | 0.2000 | 0.5000 | — |
| 57 | q61 | cross_section | 5 | 2 | 0.2000 | 0.5000 | — |
| 58 | q65 | rare_token | 5 | 1 | 0.2000 | 1.0000 | — |
| 59 | q66 | rare_token | 5 | 1 | 0.2000 | 1.0000 | — |
| 60 | q67 | model_number | 5 | 1 | 0.2000 | 1.0000 | — |
| 61 | q68 | model_number | 5 | 1 | 0.2000 | 1.0000 | — |
| 62 | q69 | model_number | 5 | 1 | 0.2000 | 1.0000 | — |
| 63 | q70 | table_lookup | 5 | 1 | 0.2000 | 1.0000 | — |
| 64 | q71 | table_lookup | 5 | 1 | 0.2000 | 1.0000 | — |
| 65 | q72 | negative_scope | 5 | 1 | 0.2000 | 1.0000 | — |
| 66 | q73 | synonym | 5 | 1 | 0.2000 | 1.0000 | — |
| 67 | q74 | colloquial | 5 | 1 | 0.2000 | 1.0000 | — |
| 68 | q75 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 69 | q76 | counterfactual | 5 | 1 | 0.2000 | 1.0000 | — |
| 70 | q77 | cross_section | 5 | 2 | 0.4000 | 1.0000 | — |
| 71 | q78 | table_lookup | 5 | 7 | 0.4000 | 0.2857 | — |
| 72 | q79 | table_lookup | 5 | 9 | 0.4000 | 0.2222 | — |
| 73 | q80 | noise_robust | 5 | 6 | 0.4000 | 0.3333 | — |
| 74 | q81 | negative_scope | 5 | 6 | 0.4000 | 0.3333 | — |
| 75 | q82 | table_lookup | 5 | 5 | 0.4000 | 0.4000 | — |
| 76 | q83 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 77 | q84 | negative_scope | 5 | 1 | 0.2000 | 1.0000 | — |
| 78 | q85 | colloquial | 5 | 1 | 0.2000 | 1.0000 | — |
| 79 | q86 | synonym | 5 | 1 | 0.2000 | 1.0000 | — |
| 80 | q87 | table_lookup | 5 | 1 | 0.2000 | 1.0000 | — |
| 81 | q88 | cross_section | 5 | 7 | 0.4000 | 0.2857 | — |
| 82 | q89 | counterfactual | 5 | 1 | 0.2000 | 1.0000 | — |
| 83 | q90 | counterfactual | 5 | 1 | 0.2000 | 1.0000 | — |
| 84 | q91 | negative_scope | 5 | 1 | 0.2000 | 1.0000 | — |
| 85 | q92 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 86 | q93 | counterfactual | 5 | 1 | 0.2000 | 1.0000 | — |
| 87 | q94 | table_lookup | 5 | 7 | 0.4000 | 0.2857 | — |
| 88 | q95 | negative_scope | 5 | 1 | 0.2000 | 1.0000 | — |
| 89 | q96 | cross_section | 5 | 2 | 0.2000 | 0.5000 | — |
| 90 | q97 | counterfactual | 5 | 1 | 0.2000 | 1.0000 | — |
| 91 | q98 | negative_scope | 5 | 1 | 0.2000 | 1.0000 | — |
| 92 | q99 | table_lookup | 5 | 5 | 0.4000 | 0.4000 | — |
| 93 | q104 | cross_section | 5 | 2 | 0.2000 | 0.5000 | — |
| 94 | q105 | cross_section | 5 | 2 | 0.2000 | 0.5000 | — |
| 95 | q106 | cross_section | 5 | 10 | 0.4000 | 0.2000 | — |
| 96 | q107 | cross_section | 5 | 7 | 0.2000 | 0.1429 | — |
| 97 | q108 | colloquial | 5 | 1 | 0.2000 | 1.0000 | — |
| 98 | q109 | noise_robust | 5 | 1 | 0.0000 | 0.0000 | — |
| 99 | q110 | negative_scope | 5 | 1 | 0.2000 | 1.0000 | — |
| 100 | q111 | cross_section | 5 | 1 | 0.0000 | 0.0000 | — |
| 101 | q112 | counterfactual | 5 | 1 | 0.0000 | 0.0000 | — |
| 102 | q113 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 103 | q114 | synonym | 5 | 1 | 0.2000 | 1.0000 | — |
| 104 | q115 | counterfactual | 5 | 1 | 0.2000 | 1.0000 | — |
| 105 | q116 | table_lookup | 5 | 1 | 0.2000 | 1.0000 | — |
| 106 | q117 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 107 | q118 | negative_scope | 5 | 1 | 0.2000 | 1.0000 | — |
| 108 | q119 | cross_section | 5 | 1 | 0.2000 | 1.0000 | — |
| 109 | q120 | model_number | 5 | 1 | 0.2000 | 1.0000 | — |
| 110 | q121 | model_number | 5 | 1 | 0.2000 | 1.0000 | — |
| 111 | q122 | model_number | 5 | 1 | 0.2000 | 1.0000 | — |
| 112 | q123 | model_number | 5 | 1 | 0.2000 | 1.0000 | — |
| 113 | q124 | model_number | 5 | 1 | 0.2000 | 1.0000 | — |
| 114 | q125 | model_number | 5 | 1 | 0.2000 | 1.0000 | — |
| 115 | q126 | model_number | 5 | 1 | 0.2000 | 1.0000 | — |
| 116 | q127 | model_number | 5 | 7 | 0.4000 | 0.2857 | — |
| 117 | q128 | model_number | 5 | 7 | 0.4000 | 0.2857 | — |
| 118 | q129 | synonym | 5 | 1 | 0.2000 | 1.0000 | — |
| 119 | q130 | negative_scope | 5 | 1 | 0.2000 | 1.0000 | — |
| 120 | q131 | colloquial | 5 | 1 | 0.2000 | 1.0000 | — |
| 121 | q132 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 122 | q133 | counterfactual | 5 | 1 | 0.2000 | 1.0000 | — |
| 123 | q134 | negative_scope | 5 | 1 | 0.2000 | 1.0000 | — |
| 124 | q135 | table_lookup | 5 | 1 | 0.2000 | 1.0000 | — |
| 125 | q136 | noise_robust | 5 | 1 | 0.2000 | 1.0000 | — |
| 126 | q137 | counterfactual | 5 | 1 | 0.2000 | 1.0000 | — |

## 复现命令（照抄即可）

```powershell
$env:UV_DEFAULT_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
uv sync --frozen
# 1. 校验题库与实验配置（离线，不连库、不发网络）
uv run python scripts/eval_runner.py validate-dataset
uv run python scripts/eval_runner.py validate-config
# 2. 真实跑（进程内检索；需要 PG / ES / 嵌入凭据）
$env:RERANK_TIMEOUT_SECONDS = "60"  # 本轮评测的预算；生产是 15 秒
uv run python scripts/eval_runner.py run --env-file <主 checkout>\.env --rerank
# 3. 重出这份报告（只读归档，不再跑检索）
uv run python scripts/eval_runner.py report --run eval/baseline-rerank
# 4. 单独打印某一个数字（报告里每处数字旁边都写了这条命令）
uv run python scripts/eval_runner.py report --run eval/baseline-rerank --select metrics.overall.context_precision
```

上面每个数字都来自同一份 `results.json`：报告是它的渲染，不是另算一遍。

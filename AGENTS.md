## Agent skills

### Issue tracker

Issue 与 spec 以 GitHub Issue 形式存放于 `Jenlybein/RAG-Core`，一律通过 `gh` CLI 操作。详见 `docs/agents/issue-tracker.md`。

### Triage labels

五个标准 triage 角色直接使用同名标签（`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`）。详见 `docs/agents/triage-labels.md`。

### Domain docs

单上下文（single-context）：仓库根目录一个 `GLOSSARY.md`，加 `docs/adr/`。详见 `docs/agents/domain.md`。

### 新工作树的准备（否则首次跑测试必炸）

`git worktree add` **不会**带上两个被 gitignore 的东西，而它们都是测试的前提：

- `.tokenizer/`（Qwen 词表）——缺了会一批用例直接 error（ADR-0015：词表缺失即拒绝启动）；
- `.env`——缺了配置走默认值，与本地开发口径不一致，排障时先怀疑代码，其实是没有环境。

从主 checkout 复制这两个到新工作树，然后 `uv run --frozen pytest -q`（`uv run` 会在工作树里
建自己的 `.venv`；**不要**借用主 checkout 的 `.venv`：editable 安装会让 `import app` 解析回主
checkout，测试全绿但测的是旧代码）。


# 领域文档

工程类 skill 在探索代码库时，应如何消费本仓库的领域文档。

## 探索之前，先读这些

- 仓库根目录的 **`GLOSSARY.md`**；或
- 若根目录存在 **`GLOSSARY-MAP.md`**：它指向每个上下文各自的 `GLOSSARY.md`，读取与当前话题相关的那些。
- **`docs/adr/`**：读取与即将涉足领域相关的 ADR。多上下文仓库中，还要检查 `src/<context>/docs/adr/` 下的上下文级决策。

若上述文件不存在，**静默跳过**。不要提示缺失，也不要主动建议先创建它们。`/domain-modeling` skill（经由 `/grill-with-docs` 与 `/improve-codebase-architecture` 到达）会在术语或决策真正定案时按需创建。

## 文件结构

单上下文仓库（绝大多数仓库）：

```
/
├── GLOSSARY.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── src/
```

多上下文仓库（根目录存在 `GLOSSARY-MAP.md`）：

```
/
├── GLOSSARY-MAP.md
├── docs/adr/                          ← 系统级决策
└── src/
    ├── ordering/
    │   ├── GLOSSARY.md
    │   └── docs/adr/                  ← 上下文级决策
    └── billing/
        ├── GLOSSARY.md
        └── docs/adr/
```

## 使用术语表里的词汇

当你的产出要提到某个领域概念（issue 标题、重构建议、假设、测试名）时，使用 `GLOSSARY.md` 中定义的术语，不要漂移到术语表明确回避的同义词。

如果需要的概念还不在术语表中，这是一个信号：要么你在发明项目不用的语言（重新考虑），要么确实存在空缺（记下来交给 `/domain-modeling`）。

## 标出 ADR 冲突

若你的产出与既有 ADR 相矛盾，明确提出来，而不是悄悄覆盖：

> _与 ADR-0007（事件溯源订单）相冲突，但因为…… 值得重新讨论。_

"""#220 · 单库 token 容量：``plans.max_tokens_per_kb`` + ``documents.token_count``

## 为什么容量要按 token 而不是按篇

改造前的「单库装多少」数是**行数**（``max_documents_per_kb``）。那个口径有个明显的洞：
**一篇 5 万 token 的文档与一篇 200 字的文档都算 1 篇**。用户把「200 篇」用满时，库里可能是
4 万 token，也可能是 4000 万——而后者才是真正压垮检索与成本的那一端。

改成 token 之后，容量回答的是「这个库装得下多少内容」，行数上限继续留着当廉价的兜底
（防「十万篇空文档」那种 token 很省、但列表与计数会崩的玩法）。

## 两种 token 口径，别混

| 列 | 口径 | 用在哪 |
| --- | --- | --- |
| ``documents.token_count`` | **正文** token（解析后的全文，与版本行正文同源） | 单库容量判定 |
| 日额度那边记的 | **检索文本** token（文档名 + 标题路径 + 正文 + 节说明前缀） | 每日切块额度 |

两者差 10–20%，而账单认的是后者。ADR-0035 决策一把这条写死了：容量用正文（本地确定、
与切块器同一把尺子）、额度用检索文本（真正发给嵌入模型的那一份）。混用会让「容量没满
但额度超了」这种解释不清的场面出现。

## 为什么回填存量：不回填 = 存量库看起来全是 0 token

``token_count`` 的默认值是 0（新列必须可枚举），而 0 在容量判据里是「这一篇不占空间」——
于是上线那一刻所有存量库都显示「已用 0 / 上限 3 万」，用户第一次上传就可能撞上一个
**假的**超限（其实是存量内容没算进去）。所以升级路径要回填，且：

* 回填按**正文长度现算**是不行的（迁移不能 import 应用代码，也不该在 SQL 里跑 tiktoken）。
  这里只做一件确定的事：**按字符数给一个下界**（``length(content)``），并在文档里写明
  它是下界而不是真值——真正的数字由运维脚本 ``scripts/backfill_document_tokens.py``
  用真词表算一遍覆盖掉。
* 为什么还要那个下界：完全不回填（0）会把存量库的容量显示成「空库」，而下界至少让
  「这个库明显装了很多」这件事在界面上可见。**它是过渡值，不是最终值。**

## downgrade 是完整的

两列都是本次新增：``max_tokens_per_kb`` 有种子可重建，``token_count`` 是派生量（可重算），
删掉即回到上一版。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f9c2a7b3e8"
down_revision: str | None = "c4e8f1b6a2d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 四档种子值（ADR-0035 决策六）。迁移里写第二份拷贝是刻意的：历史脚本不许 import
#: 应用代码，否则「当时那份代码」会被今天的改动悄悄改写。护栏用例逐列对账这两份。
#: 每个值两格：``max_tokens_per_kb`` 是**新列**（本次才加的，不存在「运营改过」的值）；
#: ``max_kb`` 是**已有列**，所以它多一格 ``was`` —— 只有在库里那个值**仍然是旧种子值**时
#: 才动它。
#:
#: 为什么要 ``was``：spec 明写「只在空值上写，**不覆盖运营改过的值**」。一个租户运营把 free
#: 档的库数从 1 调到 5，这条迁移若无条件写成 3，等于**把运营的决定改回去**，而且没有任何
#: 提示——运维会在下一次有人报「怎么只能建 3 个库了」时才发现。带上 ``was`` 之后：
#:
#: * 全新库：种子已经是新值（3），``max_kb = 1`` 不成立 → 不动，正确；
#: * 刚升级的老库：``max_kb`` 还是 1 → 改成 3，正确；
#: * 运营改过的库：``max_kb`` 是 5 → 不匹配 → 保持 5，正确。
_TIER_VALUES: dict[str, dict[str, int]] = {
    "free": {"max_tokens_per_kb": 30_000, "max_kb": 3, "was": 1},
    "plus": {"max_tokens_per_kb": 300_000, "max_kb": 10, "was": 5},
    "pro": {"max_tokens_per_kb": 2_000_000, "max_kb": 50, "was": 50},
    "max": {"max_tokens_per_kb": 5_000_000, "max_kb": 100, "was": 50},
}

#: 回填**下界**：按正文的字符数记（中文一字约 1 token 量级，英文一字不到 1）。它是过渡值，
#: 真值由 ``scripts/backfill_document_tokens.py`` 用词表算一遍覆盖。
_BACKFILL_LOWER_BOUND = sa.text(
    "UPDATE documents SET token_count = length(content) WHERE token_count = 0 AND content <> ''"
)


def upgrade() -> None:
    # 1) 套餐列：先给一个能过 NOT NULL 的默认值，再按四档播种。
    op.add_column(
        "plans",
        sa.Column("max_tokens_per_kb", sa.Integer(), nullable=False, server_default="30000"),
    )
    for code, values in _TIER_VALUES.items():
        # 新列直接写（本次才加，不存在运营改过的值）。
        op.execute(
            sa.text(
                "UPDATE plans SET max_tokens_per_kb = :limit WHERE code = :code"
            ).bindparams(limit=values["max_tokens_per_kb"], code=code)
        )
        # 已有列只在**还是旧种子值**时才动（理由见 _TIER_VALUES 的注释）。
        op.execute(
            sa.text(
                "UPDATE plans SET max_kb = :kbs WHERE code = :code AND max_kb = :was"
            ).bindparams(kbs=values["max_kb"], was=values["was"], code=code)
        )

    # 2) 文档列：正文 token 数（0 = 还没算过）。
    op.add_column(
        "documents",
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(_BACKFILL_LOWER_BOUND)


def downgrade() -> None:
    op.drop_column("documents", "token_count")
    op.drop_column("plans", "max_tokens_per_kb")

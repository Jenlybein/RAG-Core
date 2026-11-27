"""#227 · ``api_keys`` 的过渡密钥：重新生成后的 24 小时窗口

## 为什么需要它

「Key 丢了/泄了，我要换一把」在本仓只有一条路：**重新生成**（库里只有 `scrypt` 哈希，
原文取不回来）。但重新生成之后，正在跑的业务还拿着旧 Key——立刻让它失效等于当场把所有
集成打断。反过来，如果旧密钥可以长期继续用，那「怀疑泄露」这个场景就永远收不了口。

折中只有一种形状：**旧哈希留一个 24 小时的窗口**，窗口内新旧都能过校验，窗口外只认新的。

## 为什么是 4 列而不是一张历史表

* `prev_key_hash` / `prev_key_tail`：旧密钥本体（哈希 + 末 5 位，后者只给面板打码）；
* `prev_key_expires_at`：窗口的截止时刻，**唯一判据**（不看 `rotated_at`，那是给人看的）；
* `rotated_at`：最近一次重新生成的时刻。

历史表能留一串旧址，但那等于同时允许一串凭证有效——与「一把 Key 只有一份有效密钥」正好
相反。**只保留最近一把**：24 小时内再轮换一次，上一把旧址直接被覆盖（同时作废）。

## 为什么窗口长度的常量在服务层、不在这张表

24 小时是**策略**，不是数据：表里存的是那一把 Key 自己的截止时刻（`prev_key_expires_at`），
于是策略以后改成 1 小时/72 小时时，已经在窗口里的这些行不会被追溯修改——它们的截止时刻
是**轮换那一刻**按当时的策略算出来的。这也是审计能讲清楚「这把 Key 为什么在某个时刻还能用」
的原因。

## downgrade 是完整的

四列都是派生量（都能从「下一次重新生成」重建），删掉即回到上一版：过渡窗口消失，旧密钥
立刻失效——那是改造前的行为，不是新引入的损坏。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4b1d7a9c2f5"
down_revision: str | None = "c3d6e9f2b5a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("prev_key_hash", sa.String(255), nullable=True))
    op.add_column("api_keys", sa.Column("prev_key_tail", sa.String(16), nullable=True))
    op.add_column("api_keys", sa.Column("prev_key_expires_at", sa.DateTime(), nullable=True))
    op.add_column("api_keys", sa.Column("rotated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "rotated_at")
    op.drop_column("api_keys", "prev_key_expires_at")
    op.drop_column("api_keys", "prev_key_tail")
    op.drop_column("api_keys", "prev_key_hash")

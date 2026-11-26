"""#225 · ``documents.redispatch_count``：清清翻录器「救不回来」的次数上限

## 为什么需要它

spec 的自愈一节要求「卡住的文档能在十分钟内被自动救回」。清扫器能做的只有一件事：
**把租约清掉、重新投一次**。但我必须回答一个问题——**这次投出去，它又没声了怎么办**？

没有次数上限时是这样的：清扫器每轮都看见同一篇的租约过期，于是每轮都投一次；队列里因此
常年躺着一批必然失败的消息，而用户看到的是**永远转圈**。那比「失败」更糟：失败至少是个
结论，转圈是一个没有结论的状态。

有上限之后，到顶那一次就把它标成 ``2 失败``（用既有的失败文案），用户得到一个明确的
「这篇不行了，重建它」。**「救不回来」也是一个必须兑现的结论**——这句话是本步反复出现的
那条原则，在这里的第 4 次出现。

## 为什么是文档行而不是 Redis

Redis 里也可以数（`INCR` + 过期），但那会让「这篇重投过几次」变成一份**会消失**的记忆：
Redis 重启、键过期、库号换了，计数就归零，于是那篇文档又开始无限重投。而这个计数要表达的
是「这篇文档**已经试过 N 次**」——它是文档的事实，就该跟着文档行一起活着、一起被删。
代价是一次迁移与一次 UPDATE，都很便宜。

## 什么时候清零

**成功索引时清零**（``IndexingService`` 的 Tx2 里，与 ``token_count``、状态同一笔事务）。
不清零的后果：一篇曾经被救回 3 次的文档，第 4 次万一再卡住就直接被判失败——而它明明是
好的。清零的语义是「上一次的事已经结束了」。

## downgrade 是完整的

一列纯计数（派生量、可重建），删掉即回到上一版：文档行回到「没有重投记忆」的状态，
自愈退化成「无限重投」——那是改造前的行为，不是新引入的损坏。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a2b5c8d1e4f9"
down_revision: str | None = "f1a4b8d6c392"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("redispatch_count", sa.Integer(), nullable=False, server_default="0"),
    )
    # 部分索引：清扫器每轮都要问「哪些 processing 行的租约过期了」，而这是**跨租户**的扫描。
    # 没有索引时它是一张全表扫（几万篇文档每分钟一次），代价不该被忽略。
    op.create_index(
        "ix_documents_stale_claim",
        "documents",
        ["index_claimed_at"],
        postgresql_where=sa.text("status = 3 AND index_claimed_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_documents_stale_claim", table_name="documents")
    op.drop_column("documents", "redispatch_count")

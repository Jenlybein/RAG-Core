"""#225 · ``documents.index_heartbeat_at``：长文档的心跳（不被误判为死）

## 为什么需要它

租约（``index_claimed_at``）有两件事要做，而它们对「时间」的要求正好相反：

| 用途 | 需要什么 |
| --- | --- |
| **fencing 令牌**（#213）：Tx2 开头核对「这个租约还是不是我的」 | 必须**永不变**——变了就等于换了一把钥匙，旧任务自己就把自己锁在门外 |
| **「这个 Worker 还活着吗」**（#225 清扫器判「接了之后没声了」） | 必须**会变**——不变就分不出「跑了两分钟」与「Worker 死了两小时」 |

一件东西不能同时满足两者。硬塞进一列只有两个结果：要么清扫器把**正在跑**的长文档抢走
（真的把同一篇索引两遍，浪费一轮 embedding），要么长文档永远不被救（Worker 挂掉之后它停在
「索引中」直到有人手工处理）。

所以分成两列：``index_claimed_at`` 是**令牌**（抢占时写的那个值，谁持有谁就是主人），
``index_heartbeat_at`` 是**心跳**（任务跑着的时候周期性刷新）。清扫器只看心跳。

## 心跳由谁写、多久写一次

``IndexingService`` 在流水线的**耗时段之后**写（解析完、每批嵌入完）：那些段动辄几十秒，
正是「看起来像死了」的地方。心跳用**独立短事务**写（不是 Tx2），因为 Tx2 要等到最后才提交，
而心跳的意义恰恰是「还没到最后也要证明我活着」。

写心跳**不改** ``index_claimed_at``：改了令牌，Tx2 的 fencing 就会把自己拒掉——这是这一列
与租约分家的全部理由，也是这次最容易写错的一行。

## 为什么清扫器的判据用 ``COALESCE``

改造前抢过但没写过心跳的行（上线那一刻还在跑）心跳为空。判据取
``COALESCE(index_heartbeat_at, index_claimed_at)``：没有心跳就按抢占时间算，于是老行不会
因为「心跳为空」被当成「从来没活过」。这条回落在清扫器里，不在列上——列的缺省值是 NULL
（NULL = 「没写过心跳」，不是「很久没心跳」）。

## 索引与回滚

``ix_documents_stale_claim`` 那条部分索引（迁移 ``a2b5c8d1e4f9``）按 ``index_claimed_at``
建；本迁移再建一条按心跳的：清扫器每轮扫的是心跳，让它走索引而不是全表扫。两条都留着——
前者的读者是「按令牌找行」（fencing 相关排障），后者是「按心跳找行」（清扫器）。

## downgrade 是完整的

一列纯时间戳（派生量、可重写），删掉即回到上一版：清扫器退回按 ``index_claimed_at`` 判死，
长文档重新有了被误判的风险——那是改造前的行为，不是新引入的损坏。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d6e9f2b5a8"
down_revision: str | None = "a2b5c8d1e4f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("index_heartbeat_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_documents_heartbeat",
        "documents",
        ["index_heartbeat_at"],
        postgresql_where=sa.text("status = 3 AND index_heartbeat_at IS NOT NULL"),
    )
    # 存量「正在跑」的行补一次心跳：否则它们要等到下一次流水线写心跳才「有活的证据」，而在
    # 那之前清扫器会把它们当成老行——用 ``COALESCE`` 回落本来也能兜住，但补一次更直接：
    # 升级那一刻正在跑的文档，本来就刚刚被抢占过。
    op.execute(
        sa.text(
            "UPDATE documents SET index_heartbeat_at = index_claimed_at "
            "WHERE status = 3 AND index_claimed_at IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_documents_heartbeat", table_name="documents")
    op.drop_column("documents", "index_heartbeat_at")

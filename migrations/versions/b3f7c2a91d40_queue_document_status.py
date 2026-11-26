"""#217：文档状态拆出「排队中」——存量「索引中但没有租约」的行改成 ``4``

## 这条迁移改了什么

**一条数据迁移，不动任何 DDL。** 三态时代那个「处理中（3）」同时装着两种事实：排了队
还没开工、以及真的有 Worker 在跑；区分它们的唯一线索是 ``index_claimed_at``（有租约 =
在跑）。四态之后（ADR-0034 决策一）这不是线索而是**定义**：

| 值 | 名字 | 唯一权威证据 |
| --- | --- | --- |
| ``3`` | 索引中 | 租约时间戳落在租约窗口内 |
| ``4`` | 排队中 | **没有租约**（也不该有） |

于是存量里 ``status = 3 AND index_claimed_at IS NULL`` 的行，语义上就是排队中。它们要么是
「入队了、Worker 还没来」，要么是「Worker 抢到之后崩在写租约之前」——两条都该显示成
排队中，也都该允许被删除 / 重新提交（撤回一条还没被执行的意图）。

## 为什么不一并改「租约已过期」的行

``status=3`` 且租约**过期**的行同样没人在跑，但它们不是「排队中」的形状：它们曾经被接过，
只是没跑完。清扫器（ADR-0034 决策七）按「租约过期」重投它们，Worker 下一次抢占会把行写成
``3 + 新租约``——语义与显示都自动正确。把它们也改成 4 等于抹掉「这一篇被谁接过」这条线索，
而且让可抢占条件里「租约过期」那一支失去可验证的形状。

## 读侧在迁移前后都必须成立

可抢占条件里**保留**「``3`` 且租约为空」这一支（``document_repository._claimable_conditions``）：
迁移之前、迁移过程中、以及执行了 ``downgrade`` 之后，读侧都不能把那种行当成「在跑」。

## downgrade 是完整的

反向就是把 ``4`` 写回 ``3``（租约仍为空，正是改造前的形状）。回滚会丢掉「排队中」这个区分，
但三态读侧照样把这些行当「处理中」，而「``3`` 且无租约」本来就在可抢占 / 可删除的那一档上，
所以回滚之后行为与改造前一致——不是「回滚了一半」。

## 不要对正在跑服务的库直接执行

本迁移只改数据不改结构，但会让旧代码看到一堆它不认识的 ``4``（旧前端徽章会显示「未知」）。
上线顺序：先发新代码（它认识两种形状），再跑这条迁移。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b3f7c2a91d40"
down_revision = "f8b3c2d91a70"
branch_labels = None
depends_on = None

#: 「索引中但没有租约」→ 排队中。只认这两个条件的交集：租约有效的 3 是真的在跑。
MARK_QUEUED = sa.text(
    "UPDATE documents SET status = 4 WHERE status = 3 AND index_claimed_at IS NULL"
)

#: 反向：排队中 → 索引中（租约仍为空，与改造前的形状逐字一致）。
RESTORE_PROCESSING = sa.text("UPDATE documents SET status = 3 WHERE status = 4")


def upgrade() -> None:
    op.execute(MARK_QUEUED)


def downgrade() -> None:
    op.execute(RESTORE_PROCESSING)

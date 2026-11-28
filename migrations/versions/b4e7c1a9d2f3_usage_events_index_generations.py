"""第 17 步现场九：归属索引从「库级版本」改记「篇级代」，并补缺代计数列。

## 这条迁移改了什么

1. **加** ``usage_events.index_generations``（``JSONB``，``{document_id: 代数}``，可空，
   ``server_default '{}'``）；
2. **加** ``usage_events.missing_generation_documents``（``Integer``，``NOT NULL``，
   ``server_default '0'``）：这次调用范围内有多少篇还没有任何产物；
3. **删** ``usage_events.index_versions``（``{kb_id: version}``）。

## 为什么删旧列，以及这个代价

旧列回答「这次查的是**哪个库的哪一版构建**」（ADR-0025 决策六）。新模型里没有对应物：
可见性判据已经从库级索引版本下放到「篇级当前代 × 当前稿」（``chunks.generation =
documents.current_index_generation`` + 稿号对齐），库级版本轴退居构建真值、正在被下一支
迁移删掉。留着它只会让审计账本**自相矛盾**——账上写着某一版，检索却按每篇各自的当前代
取数，事后按它归因必然归错。

**代价必须写明白**：历史行「哪一次构建」的归因能力随这次迁移**消失**。旧值是库级版本名，
与新列的「篇级代数」不是同一种坐标系里的东西，**没有任何正确映射**；所以这条迁移
**只有 DDL，不做数据搬运**。升级后老行拿到 ``server_default`` 的空对象——「这一行没有篇级
代可记」是它当时的真实处境，比编一个「换算」出来的假代数诚实。换来的是新模型里唯一能回答
问题的那个形状：「每篇在第几代交付」。

## 为什么 ``missing_generation_documents`` 跟这条迁移一起上

它回答「结果为什么凑不满」：范围内有多少篇 ``current_index_generation IS NULL``（= 还没有
任何产物，检索时被跳过）。这一列的取值时机在应用侧（只在结果不足时才查、满额时恒 0 且不发
查询，取舍见 ``app/services/retrieval_trace.py``），迁移只需保证它有值可写：``NOT NULL`` +
``server_default '0'`` 让存量行在升级那一刻落成 0（= 那个老行为没有数过这件事）。

## downgrade 对称

降级放弃「记得每篇在第几代」这个能力：加回 ``index_versions``（形状与原来的迁移
``e41b7c9d2a63`` 一字不差：JSONB、可空、无 server_default），删掉两列新列。降到这一步之后，
库里是「旧列还在、但没有数据」的状态——旧值本来就在 upgrade 里删掉了，回滚不会把数据带
回来。这是回滚的应有之义，**不要**在生产上随手跑。

Revision ID: b4e7c1a9d2f3
Revises: d91f4b7c3e28（现场九第六步的旧代回收索引）
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b4e7c1a9d2f3"
down_revision: Union[str, None] = "d91f4b7c3e28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 新列先加、旧列后删：中间不存在「两列都没有」的窗口（在单事务迁移里其实也无所谓，
    # 但顺序写成「先立后破」读起来与意图一致）。
    #
    # ``server_default`` 是空对象：老行的旧值无法映射成篇级代数（见模块文档），落成 ``{}``
    # 就是「这一行没有篇级代可记」。可空是给迁移之前的行留的（它们连库级版本都没记）。
    op.add_column(
        "usage_events",
        sa.Column(
            "index_generations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "usage_events",
        sa.Column("missing_generation_documents", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_column("usage_events", "index_versions")


def downgrade() -> None:
    # 与 ``e41b7c9d2a63`` 给这一列的定义对齐：JSONB、可空、无 server_default。
    op.add_column(
        "usage_events",
        sa.Column("index_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.drop_column("usage_events", "missing_generation_documents")
    op.drop_column("usage_events", "index_generations")

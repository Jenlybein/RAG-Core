"""add retrieval trace doc versions

#179。归属索引多一列 ``doc_versions``：``{document_id: doc_version}`` 的**映射**，形状与
第 11 步加的 ``index_versions`` 同形。要它回答的问题是「那次查的是第几稿」——用户故事
第 3 条，**即使那一稿的切片后来被保留策略清掉**（#178 会真的删掉旧稿的切片与版本行）。
``chunk_ids`` 答不了这个：片没了，那串 id 就只是一串没有意义的字符串。

三条性质与 ``index_versions`` 一致，所以这里不需要任何新判断：

* **可空、不回填**。迁移之前写下的行没有稿号可记（那时稿号恒为 1，没人需要在日志里
  记它），而「老行缺这一列」是合法状态而不是待修数据——补一个假想值反而会让它在事后
  读起来像「当时确实是第 1 稿」。
* **只加一列，不动别的**。没有新表、没有新索引、不改已有列：这一列按文档 id 查不起来，
  也没有任何查询按它过滤（它是归因材料，不是检索条件）。
* **升降对称**。降级就是放弃「记得哪几稿」这个能力：把这一列删掉，其余十二列里剩下的
  十一列原样留着。

Revision ID: 3f7b2c9d5e14
Revises: 7c2e5a9b4d31（#172 的版本表，本票接在它后面）
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "3f7b2c9d5e14"
down_revision: Union[str, None] = "7c2e5a9b4d31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 与 ``index_versions`` 同一个写法：JSONB、可空、无 server_default。
    # 给默认值 ``{}`` 会让「这一行是老的」与「这一行确实没命中任何文档」变成同一个读数
    # ——前者是历史，后者是事实，两者在事后复盘时的含义完全不同。
    op.add_column(
        "retrieval_traces",
        sa.Column("doc_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("retrieval_traces", "doc_versions")

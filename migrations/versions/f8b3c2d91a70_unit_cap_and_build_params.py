"""第 16 步（#207·B）：``key_policies.max_unit_chars`` + ``index_build_versions.params``

## 这条迁移改了什么

1. **``key_policies.max_unit_chars``**：一条检索结果 ``content`` 的硬上限（字符）。它是
   检索期那颗旋钮——同一个库，页面问答要少、批量归档要多，所以它跟着 Key 走，和
   ``retrieval_top_k`` 同类。``server_default`` 取 ``2000``：与库的节段阈值
   （``knowledge_bases.settings["section_part_max_chars"]`` 的默认值）**同值**，
   于是正常情况下「节段 ≤ 阈值 ≤ 上限」，上限根本不触发；它只在历史索引、表格单元、
   以及「片边界使节段超阈值」这三种情形下起作用。
2. **``index_build_versions.params``**（JSONB，可空）：这一版索引是**用哪些切块参数**构出来的。
   切块参数是索引期旋钮，改了必须重建；不记下来的话，「我明明改成 1600 了怎么还是整节」
   只能靠人记忆。库当前设置 ≠ 生效版本的 ``params`` 时，控制台据此标「需重建」。

## 为什么 ``params`` 可空

老版本行**补不出来**：那时压根没记过用的是哪个阈值，``chunk_size`` 也只存在库的设置里、
随时可改。猜一个「大概就是当时的默认值」写进去，会让一个该显示「需重建」的库显示成已就绪
——那正是这一列要防的那件事。``NULL`` 的语义是「这一版没留参数」，读侧按「与当前设置不一致」
处理（宁可多提示一次重建，也不要少提示）。

## ``max_unit_chars`` 为什么不是 0 或 NULL

0 不是一个上限，是「什么都别给」；而这条列在新写入的行上必须有值（``KeyPolicySnapshot``
不做兜底，缺值就是 ``TypeError``）。``server_default='2000'`` 让老行落成一个**可用**的值，
不需要回填：它本来就是「与默认阈值同值」的那颗旋钮。

## downgrade 是完整的

两列都可逆，删掉即回到上一版。回滚会丢掉「这一版用了哪些切块参数」与「各 Key 的上限」——
但两者都能从设置与默认值重新得到，不是不可再生的数据。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f8b3c2d91a70"
down_revision: str | None = "e4f7b2c9a613"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_UNIT_CHARS_DEFAULT = 2000


def upgrade() -> None:
    op.add_column(
        "key_policies",
        sa.Column(
            "max_unit_chars",
            sa.Integer(),
            nullable=False,
            server_default=str(MAX_UNIT_CHARS_DEFAULT),
        ),
    )
    op.add_column(
        "index_build_versions",
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("index_build_versions", "params")
    op.drop_column("key_policies", "max_unit_chars")

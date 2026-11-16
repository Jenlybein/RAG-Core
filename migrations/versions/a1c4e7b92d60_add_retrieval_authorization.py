"""add retrieval authorization（第 13 步 · T1，expand 段）

新形式的列**加在旁边**，旧列一列不删、一列不改。这是 expand–contract 的第一步：
旧形式仍服务现有判定，本票的新列此刻**不参与任何运行时判定**，于是升级完的库与
改造前的库跑出同一个结果。T2 起把门禁接到这些列上，T8 contract 才删旧形式。

| 表 | 新增列 | 回答什么 |
| --- | --- | --- |
| ``plans`` / ``key_policies`` | 五个授权位 ``*_allowed`` | 这把 Key 能不能用这条端点（能不能，不是用了多少） |
| 同上 | ``rerank_qps`` / ``rerank_daily`` / ``llm_qps`` / ``llm_daily`` | 三本账里另外两本的每秒与每日额度 |
| ``plans`` | ``max_top_k`` | 档位白名单退役后唯一的质量封顶 |

``retrieve_qps`` / ``retrieve_daily`` **不新增**：它们本来就是「retrieve 那本账」，
三本账共用同一组列名形状（账本_窗口），加一份带后缀的重复列只会让两级计数多一个
谁也说不清的真相源。

所有新列 ``NOT NULL`` + ``server_default``：既有行在升级那一刻被回填成
「false / 0」——新能力默认不开放，要开就由控制面板显式分配。这与 #146 加能力开关、
#150 加 Research 配额两列是同一条精神：**加能力不许悄悄改变现有行为**。

``downgrade`` 干净删掉本票加的这些列，别的旧列一列不碰。

Revision ID: a1c4e7b92d60
Revises: 3f7b2c9d5e14（#179 归属索引文档稿号映射列，本步此前的唯一 head）
Create Date: 2026-11-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1c4e7b92d60"
down_revision: Union[str, None] = "3f7b2c9d5e14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 真正**新增**的端点级授权位。布尔，缺省 false。
#: ``research_allowed`` / ``rerank_allowed`` 在两张表里早已有列（旧能力开关）：
#: 第 13 步是**复用并升级语义**，不是再加一列——重复 add_column 会让真库升级直接失败。
CAPABILITY_COLUMNS: tuple[str, ...] = (
    "retrieve_allowed",
    "rewrite_allowed",
    "decompose_allowed",
)

#: 另外两本账的四列。整数，缺省 0。
NEW_METER_COLUMNS: tuple[str, ...] = (
    "rerank_qps",
    "rerank_daily",
    "llm_qps",
    "llm_daily",
)

#: 两张表共有的新列；max_top_k 只在 plans 上（套餐级上限，不在 Key 上单独配）。
SHARED_COLUMNS: tuple[str, ...] = (*CAPABILITY_COLUMNS, *NEW_METER_COLUMNS)
PLAN_ONLY_COLUMNS: tuple[str, ...] = ("max_top_k",)


def _add(table: str, name: str) -> None:
    """按列的类型加一列；布尔用 false、整数用 0，都非空且回填存量行。"""

    if name in CAPABILITY_COLUMNS:
        column = sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false())
    else:
        column = sa.Column(name, sa.Integer(), nullable=False, server_default="0")
    op.add_column(table, column)


def upgrade() -> None:
    for name in SHARED_COLUMNS:
        _add("plans", name)
        _add("key_policies", name)
    for name in PLAN_ONLY_COLUMNS:
        _add("plans", name)


def downgrade() -> None:
    for name in PLAN_ONLY_COLUMNS:
        op.drop_column("plans", name)
    for name in SHARED_COLUMNS:
        op.drop_column("key_policies", name)
        op.drop_column("plans", name)

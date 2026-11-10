"""create profile_presets

四个质量档位的参数第一次落库（四行）。**这次要播种**，且与 ``model_configs`` 那次
「一行都不灌」正好相反——理由是对称的：

* ``model_configs`` 空表 = 回落到 ``.env``，那是刻意的缺省行为，灌了反而把「启动读环境
  变量」变成「读库里的指向」；
* 本表空表却没有可回落的东西：档位是产品契约，代码里那份 ``PROFILE_CHAINS`` 只是**播种
  用的种子**，不是运行时缺省。不灌行，检索时每一档都会回落到「部署缺省」，于是
  ``quality`` 变成跟 ``speed`` 一样——这是静默的功能退化，看不出来也报不出来。

播种值就是改造前硬编码的那三行（``speed`` 向量 5 条、``balanced`` 混合 5 条、
``quality`` 混合 5 条 + 重排 + 改写），外加 ``custom`` 一行给编辑器当初值。
**语义差异**记在 ``app/models/profile_preset.py`` 的模块文档里：``custom`` 行只被
编辑器读，检索时 custom 仍然逐键回落部署配置。

Revision ID: a7d2e8f4c615
Revises: f5a2c9e17b38
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d2e8f4c615"
down_revision: Union[str, None] = "f5a2c9e17b38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 播种的四行。与 ``app/services/retrieval_options.py`` 里那份种子常量逐字一致——
#: 迁移是历史，不能回头去 import 应用代码（将来那份常量改了，这段历史就跟着变）。
_SEED_ROWS: tuple[tuple[str, str, int, bool, bool], ...] = (
    ("speed", "vector", 5, False, False),
    ("balanced", "hybrid", 5, False, False),
    ("quality", "hybrid", 5, True, True),
    ("custom", "vector", 5, False, False),
)


def upgrade() -> None:
    op.create_table(
        "profile_presets",
        # 主键就是档位名：四行固定，「加一档」不是插一行能了的事（要动套餐白名单、
        # 前端卡片与门禁校验）。
        sa.Column("profile", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=False),
        # 意图值，不是生效值：生效 = 意图 ∧ 部署开关，合取在 profile_policy 里做。
        sa.Column("rerank", sa.Boolean(), nullable=False),
        sa.Column("rewrite", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("profile"),
    )

    table = sa.table(
        "profile_presets",
        sa.column("profile", sa.String),
        sa.column("mode", sa.String),
        sa.column("top_k", sa.Integer),
        sa.column("rerank", sa.Boolean),
        sa.column("rewrite", sa.Boolean),
    )
    op.bulk_insert(
        table,
        [
            {
                "profile": profile,
                "mode": mode,
                "top_k": top_k,
                "rerank": rerank,
                "rewrite": rewrite,
            }
            for profile, mode, top_k, rerank, rewrite in _SEED_ROWS
        ],
    )


def downgrade() -> None:
    op.drop_table("profile_presets")

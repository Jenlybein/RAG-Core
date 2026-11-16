"""materialize retrieval parameters（第 13 步 · I 阶段：物理删档位存储）

把等价载体 ``key_policies.custom_options`` 里的三项参数搬成真列，然后删除档位存储形态：

* 加真列 ``key_policies.retrieval_mode`` / ``retrieval_top_k`` / ``retrieval_rerank``；
* 从 ``custom_options`` 的 ``mode`` / ``top_k`` / ``rerank`` 三个键搬迁（缺键按旧部署缺省）；
* 删列：``plans.allowed_profiles``、``key_policies.profile`` / ``custom_options``、
  两表的旧能力列 ``hybrid_allowed`` / ``query_rewrite_allowed`` 与旧
  ``research_qps`` / ``research_daily``；
* 删表 ``profile_presets``。

**这是纯存储层等价变换**：行为语义已由 ADR-0032 交付，三项参数此前从 custom_options
直读；本迁移只是把「直存」从 JSONB 的一个键变成一列。对外契约逐字节不变。

历史迁移一律不改（已落地环境的回放一致性），所以这里用一条新迁移接在当前 head 之后。
``downgrade`` 反向把真列写回 ``custom_options``、重建表与列，保证可回滚。

Revision ID: b4f6c1d90e27
Revises: a1c4e7b92d60（add_retrieval_authorization）
Create Date: 2026-11-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4f6c1d90e27"
down_revision: Union[str, None] = "a1c4e7b92d60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 三项真列：与 key_policies 上已有的 rerank_* / llm_* 计量列同名前缀不冲突。
PARAM_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, object], ...] = (
    ("retrieval_mode", sa.String(length=16), "vector"),
    ("retrieval_top_k", sa.Integer(), 5),
    ("retrieval_rerank", sa.Boolean(), False),
)

#: 要从两表删除的旧列。custom_options 是本次搬迁的源，搬完才删。
DROPPED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("plans", "allowed_profiles"),
    ("plans", "hybrid_allowed"),
    ("plans", "query_rewrite_allowed"),
    ("plans", "research_qps"),
    ("plans", "research_daily"),
    ("key_policies", "profile"),
    ("key_policies", "custom_options"),
    ("key_policies", "hybrid_allowed"),
    ("key_policies", "query_rewrite_allowed"),
    ("key_policies", "research_qps"),
    ("key_policies", "research_daily"),
)


def upgrade() -> None:
    # 1. 加三列，存量行先填旧部署缺省（随后被 custom_options 里的真值覆盖）。
    for name, type_, default in PARAM_COLUMNS:
        op.add_column(
            "key_policies",
            sa.Column(name, type_, nullable=False, server_default=sa.literal(default)),
        )

    # 2. 数据搬迁：custom_options 是租户可写的 JSONB，缺键/错型都按旧缺省处理。
    op.execute(
        """
        UPDATE key_policies
        SET retrieval_mode = COALESCE(NULLIF(custom_options->>'mode', ''), 'vector'),
            retrieval_top_k = COALESCE(
                CASE WHEN jsonb_typeof(custom_options->'top_k') = 'number'
                     THEN (custom_options->>'top_k')::int END,
                5
            ),
            retrieval_rerank = COALESCE(
                CASE WHEN jsonb_typeof(custom_options->'rerank') = 'boolean'
                     THEN (custom_options->>'rerank')::boolean END,
                false
            )
        """
    )

    # 3. 删旧列与档位表。顺序：先搬完再删源。
    for table, column in DROPPED_COLUMNS:
        op.drop_column(table, column)
    op.drop_table("profile_presets")


def downgrade() -> None:
    # 1. 重建被删的表（形状与 a7d2e8f4c615 一致，不重播历史种子）。
    op.create_table(
        "profile_presets",
        sa.Column("profile", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("rerank", sa.Boolean(), nullable=False),
        sa.Column(
            "rewrite",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("profile"),
    )

    # 2. 重建被删的列（形状与建表迁移一致）。
    op.add_column(
        "plans",
        sa.Column(
            "allowed_profiles",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )
    for table in ("plans", "key_policies"):
        for column in ("hybrid_allowed", "query_rewrite_allowed"):
            op.add_column(
                table,
                sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false()),
            )
        for column in ("research_qps", "research_daily"):
            op.add_column(
                table,
                sa.Column(column, sa.Integer(), nullable=False, server_default="0"),
            )
    op.add_column(
        "key_policies",
        sa.Column("profile", sa.String(length=32), nullable=False, server_default="custom"),
    )
    op.add_column(
        "key_policies",
        sa.Column(
            "custom_options",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
    )

    # 3. 反向搬迁：真列写回 custom_options，再删真列。
    op.execute(
        """
        UPDATE key_policies
        SET custom_options = jsonb_build_object(
                'mode', retrieval_mode,
                'top_k', retrieval_top_k,
                'rerank', retrieval_rerank
            ),
            profile = 'custom'
        """
    )
    for name, _type, _default in PARAM_COLUMNS:
        op.drop_column("key_policies", name)

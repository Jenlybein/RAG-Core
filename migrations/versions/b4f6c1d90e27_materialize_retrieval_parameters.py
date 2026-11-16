"""materialize retrieval parameters（第 13 步 · I 阶段：物理删档位存储）

把「档位 + 一组手工参数」搬成三项真列，然后删除档位存储形态：

* 加真列 ``key_policies.retrieval_mode`` / ``retrieval_top_k`` / ``retrieval_rerank``；
* **数据搬迁**：旧 ``profile`` 按预设**展开**（speed→vector/5/false、balanced→hybrid/5/false、
  quality→hybrid/5/true），``custom`` 再逐键覆盖，未知档位回落部署缺省；
  ``custom_options.rewrite`` 键丢弃（改写已是端点）；
* 删列：``plans.allowed_profiles``、``key_policies.profile`` / ``custom_options``、
  两表的旧能力列 ``hybrid_allowed`` / ``query_rewrite_allowed`` 与旧
  ``research_qps`` / ``research_daily``；
* 删表 ``profile_presets``。

**搬迁必须读 ``profile``**：只读 ``custom_options`` 会让所有 ``quality`` / ``balanced``
的存量配置静默退化成 ``vector/5/false``——重排与混合检索没了，日志里一条异常都没有。
展开用的预设值就是被删的 ``profile_presets`` 表种下的那三行。

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
    # 1. 加三列，存量行先填旧部署缺省（随后被第 2 步的真值覆盖）。
    for name, type_, default in PARAM_COLUMNS:
        op.add_column(
            "key_policies",
            sa.Column(name, type_, nullable=False, server_default=sa.literal(default)),
        )

    # 2. 数据搬迁：**先按旧 ``profile`` 展开，再让 ``custom`` 逐键覆盖**。
    #
    # 这一段是本迁移唯一有业务语义的地方，也是最容易做错的地方：只读
    # ``custom_options``、把 ``profile`` 直接丢掉，会让所有 ``quality`` / ``balanced``
    # 的存量配置**静默退化成 vector/5/false**——客户升级后重排没了、混合没了，而日志里
    # 一条异常都没有。展开规则与旧 ``profile_presets`` 表的种子一致：
    #
    # * ``speed``    → vector / 5  / false
    # * ``balanced`` → hybrid / 5  / false
    # * ``quality``  → hybrid / 5  / true
    # * ``custom``   → 逐键覆盖；缺键 / 错型回落部署缺省（vector / 5 / false）
    # * 其它（未知档位、空值）→ 部署缺省
    #
    # ``custom_options.rewrite`` 键**丢弃**：改写已经是 ``/rag/rewrite`` 端点，不再是参数。
    op.execute(
        """
        UPDATE key_policies
        SET retrieval_mode = CASE profile
                WHEN 'speed' THEN 'vector'
                WHEN 'balanced' THEN 'hybrid'
                WHEN 'quality' THEN 'hybrid'
                WHEN 'custom' THEN CASE
                    WHEN custom_options->>'mode' IN ('vector', 'bm25', 'hybrid')
                    THEN custom_options->>'mode'
                    ELSE 'vector'
                END
                ELSE 'vector'
            END,
            retrieval_top_k = CASE profile
                WHEN 'custom' THEN CASE
                    WHEN jsonb_typeof(custom_options->'top_k') = 'number'
                         AND (custom_options->>'top_k')::int >= 1
                    THEN (custom_options->>'top_k')::int
                    ELSE 5
                END
                ELSE 5
            END,
            retrieval_rerank = CASE profile
                WHEN 'quality' THEN true
                WHEN 'custom' THEN CASE
                    WHEN jsonb_typeof(custom_options->'rerank') = 'boolean'
                    THEN (custom_options->>'rerank')::boolean
                    ELSE false
                END
                ELSE false
            END
        """
    )

    # 3. 删旧列与档位表。顺序：先搬完再删源。
    for table, column in DROPPED_COLUMNS:
        op.drop_column(table, column)
    op.drop_table("profile_presets")

    # 4. 收掉 server_default：搬迁完成后，新行必须由写入方显式给值，
    #    否则「漏填三项参数」会被一个看起来合理的 ``vector/5/false`` 悄悄盖住。
    for name, _type, _default in PARAM_COLUMNS:
        op.alter_column("key_policies", name, server_default=None)


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

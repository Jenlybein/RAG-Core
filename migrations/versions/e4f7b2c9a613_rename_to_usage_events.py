"""第 14 步（#199）· 归属索引升级成「一行一次的账」：``retrieval_traces`` → ``usage_events`` + ``usage_daily``

## 这条迁移改了什么

1. **改名**：``retrieval_traces`` → ``usage_events``。它装的已经不只是「检索的轨迹」——从这一版起，
   进了门的请求（含被挡下的 ``20010`` / ``20013`` / ``20014``）都落一行，所以它是**账**，
   同时仍然是按 ``trace_id`` 回查的**归属索引**（ADR-0021 的修订写清了两个身份怎么共存）。
2. **补六列**：``key_id`` / ``endpoint`` / ``code`` / ``meter_r`` / ``meter_rr`` / ``meter_llm``。
   没有这三样就没有 per-Key 视图、没有成功率的分母、也没有三本账。
3. **换索引**：旧的单列 ``tenant_id`` 索引换成 ``(tenant_id, created_at)``，另加
   ``(key_id, created_at)``。原来 ``created_at`` 上一条索引都没有，按时间窗聚合只能靠单列索引硬扛。
4. **建 ``usage_daily``**：按天汇总 + 7 档延迟直方图（分位数不能相加，必须存桶）。

## 为什么六列全部可空

老行**补不出来**：那时没有 ``key_id`` 这一列，也没有 ``endpoint`` 语义，更没记过业务码。
用 ``trace_id`` 反查、按时间猜归属、或者把老行一律算成 ``retrieve`` + ``code = 0``——
三种都是「编一个看起来合理的值」，错归因比没有数据更糟。所以留 ``NULL``，读侧把它归进
「历史汇总」那一格，页面上明写「Key 级曲线从本次上线起才有数」。

``meter_*`` 与 ``usage_daily`` 的计数列不同：它们是**新写入的行必填**，所以带
``server_default='0'``（老行落成 0，语义是「这本账没记过」，与「记了 0 次」在实践中等价，
因为这三本账在写入侧是从 0 开始加的）。

## 为什么 ``usage_daily`` 的维度用空串而不是 NULL

``(day, tenant_id, key_id, endpoint, code)`` 是主键，而 Postgres 的主键列不允许 NULL；
更关键的是 ``NULL != NULL`` 会让唯一约束形同虚设，同一格被重复插入，rollup 就不再幂等。
约定 **``""`` = 未知 / 历史汇总**。

## 为什么不在这条迁移里回填

回填需要「知道那一次是哪把 Key、哪个端点、什么码」——这些信息当时根本没记。任何回填都是猜。
真正需要历史的人是少数，而猜错一次就会让「这个 Key 那天出过事」变成假结论。

## downgrade 是完整的

改名、加列、换索引、建表四步都可逆，所以 ``downgrade()`` 老老实实写回去（不像 #200 那条删列的迁移
只能抛错）。回滚会丢掉六列与新表里的数据——这是回滚的应有之义，但**不要**在生产上随手跑。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4f7b2c9a613"
down_revision: Union[str, None] = "c7a1e9d3b5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 直方图 7 档：<200 / 200-400 / 400-600 / 600-800 / 800-1000 / 1000-1500 / >1500
BUCKET_COLUMNS = ("lat_b0", "lat_b1", "lat_b2", "lat_b3", "lat_b4", "lat_b5", "lat_b6")


def upgrade() -> None:
    # ── 1. 改名 ──────────────────────────────────────────────────────────────
    op.rename_table("retrieval_traces", "usage_events")

    # ── 2. 六列（先加列，后面才有键可建索引）───────────────────────────────
    op.add_column("usage_events", sa.Column("key_id", sa.String(length=36), nullable=True))
    op.add_column("usage_events", sa.Column("endpoint", sa.String(length=24), nullable=True))
    op.add_column("usage_events", sa.Column("code", sa.Integer(), nullable=True))
    for column in ("meter_r", "meter_rr", "meter_llm"):
        op.add_column(
            "usage_events",
            sa.Column(column, sa.Integer(), nullable=False, server_default="0"),
        )

    # ── 3. 换索引 ────────────────────────────────────────────────────────────
    # 旧名字里带旧表名；改名不会顺带改索引名，所以按原名删、按新名建。
    op.drop_index("ix_retrieval_traces_tenant_id", table_name="usage_events")
    op.drop_index("ix_retrieval_traces_trace_id", table_name="usage_events")
    op.create_index("ix_usage_events_trace_id", "usage_events", ["trace_id"], unique=True)
    op.create_index("ix_usage_events_tenant_created", "usage_events", ["tenant_id", "created_at"])
    op.create_index("ix_usage_events_key_created", "usage_events", ["key_id", "created_at"])

    # 主键约束名还停在里面那个名字上（rename_table 不会改约束名）：改掉它，
    # 否则 ``\d usage_events`` 里会一直挂着一个已经不存在的表名。
    # 用 DO 块是有意的：先判存在再改，换一个约束名不同的库也不会把迁移卡死。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'retrieval_logs_pkey') THEN
                ALTER TABLE usage_events RENAME CONSTRAINT retrieval_logs_pkey TO usage_events_pkey;
            END IF;
        END $$;
        """
    )

    # ── 4. 按天汇总 ──────────────────────────────────────────────────────────
    op.create_table(
        "usage_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("endpoint", sa.String(length=24), nullable=False, server_default=""),
        sa.Column("code", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ok_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("meter_r", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("meter_rr", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("meter_llm", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lat_sum_ms", sa.Integer(), nullable=False, server_default="0"),
        *[sa.Column(name, sa.Integer(), nullable=False, server_default="0") for name in BUCKET_COLUMNS],
        sa.PrimaryKeyConstraint("day", "tenant_id", "key_id", "endpoint", "code", name="pk_usage_daily"),
    )


def downgrade() -> None:
    op.drop_table("usage_daily")

    op.drop_index("ix_usage_events_key_created", table_name="usage_events")
    op.drop_index("ix_usage_events_tenant_created", table_name="usage_events")
    op.drop_index("ix_usage_events_trace_id", table_name="usage_events")

    for column in ("meter_llm", "meter_rr", "meter_r", "code", "endpoint", "key_id"):
        op.drop_column("usage_events", column)

    op.rename_table("usage_events", "retrieval_traces")
    op.create_index("ix_retrieval_traces_tenant_id", "retrieval_traces", ["tenant_id"], unique=False)
    op.create_index("ix_retrieval_traces_trace_id", "retrieval_traces", ["trace_id"], unique=True)
    # 主键名跟着退回去，这样「升→降」之后与升级前逐名一致（改名迁移应当完全可逆）。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'usage_events_pkey') THEN
                ALTER TABLE retrieval_traces RENAME CONSTRAINT usage_events_pkey TO retrieval_logs_pkey;
            END IF;
        END $$;
        """
    )

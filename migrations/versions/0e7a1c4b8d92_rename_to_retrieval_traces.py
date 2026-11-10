"""rename retrieval_logs → retrieval_traces, keep structure only

#101。一张从未进过响应、也从未被检索链路回读的表，这次同时做了三件事：

1. **改名**：``retrieval_logs`` → ``retrieval_traces``。名字与术语表的「归属索引」对齐，
   且不再暗示「日志」——日志是应用日志，这张表是结构记录。
2. **只留结构**：删掉 ``query``（问句原文）与 ``kb_id``（单库），补上 ``kb_ids`` /
   ``trace_id`` / ``top_k`` / ``latency_ms`` / ``degraded_reason`` / ``empty_reason``。
   原文一旦删掉就**永久没有**（观测栈保留期一到也没有，见 ADR-0021「代价」），所以
   ``downgrade()`` 只能把 ``query`` 补成空串——这不是偷懒，是「业务库不再承担原文」的
   直接后果。
3. **约束**：``trace_id`` NOT NULL 且唯一。一次检索一行，与响应 ``metadata.trace_id``
   一一对应；有了唯一索引，「按 trace_id 回查」就不需要第二张对照表。

存量行的回填只碰**结构列**，并且与 Python 侧的派生逐字一致
（``sha256(log_id)`` 的前 32 位十六进制，见 ``app/services/trace_identity.py``）——于是
「同一个 log_id 恒等派生」在迁移之后对老行也成立，不需要区分「新行 / 老行」两套口径。
``top_k`` / ``latency_ms`` 对老行回填 0：这张表在本票之前**没有任何写入方**（骨架期只建表），
0 是「契约之前的历史行」的标记，不是编出来的度量。

``kb_ids`` 的回填必须在删 ``kb_id`` 之前完成，``trace_id`` 的 NOT NULL 必须在回填之后加
——顺序即是本迁移的全部正确性。

Revision ID: 0e7a1c4b8d92
Revises: a7d2e8f4c615
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0e7a1c4b8d92"
down_revision: Union[str, None] = "a7d2e8f4c615"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 改名不会顺手重命名索引：``ix_retrieval_logs_*`` 会原样跟着表走，所以先显式删掉，
    # 最后按新名字重建。留着旧名字虽然能跑，但「旧表名不再出现」就只是一句空话。
    op.rename_table("retrieval_logs", "retrieval_traces")
    op.drop_index("ix_retrieval_logs_kb_id", table_name="retrieval_traces")
    op.drop_index("ix_retrieval_logs_tenant_id", table_name="retrieval_traces")

    # ``trace_id`` 先按可空加：存量行还没有值，NOT NULL 要等回填之后再上。
    op.add_column("retrieval_traces", sa.Column("trace_id", sa.String(length=32), nullable=True))
    # 这三个用**临时** server_default 让存量行直接成立，回填完立刻收回（见文件末尾）：
    # 永久默认值会让「新行忘了带这个值」变成静默的空数组 / 0，而那是要当场报错的 bug。
    op.add_column(
        "retrieval_traces",
        sa.Column(
            "kb_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "retrieval_traces",
        sa.Column("top_k", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "retrieval_traces",
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    # 两个原因都可空：「没有降级」「不是空结果」时它们就该是 NULL，不是空串。
    op.add_column(
        "retrieval_traces", sa.Column("degraded_reason", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "retrieval_traces", sa.Column("empty_reason", sa.String(length=32), nullable=True)
    )

    # 回填 1：单库 → 列表。必须在 drop ``kb_id`` 之前。
    op.execute(
        "UPDATE retrieval_traces SET kb_ids = jsonb_build_array(kb_id) WHERE kb_id IS NOT NULL"
    )
    # 回填 2：``trace_id`` = sha256(log_id) 的前 32 位十六进制（PG 11+ 自带 sha256）。
    # 与 Python 侧同一份派生，于是老的与新的行走同一条契约。
    op.execute(
        "UPDATE retrieval_traces "
        "SET trace_id = substring(encode(sha256(convert_to(id, 'UTF8')), 'hex') from 1 for 32) "
        "WHERE trace_id IS NULL"
    )

    # 原文离开业务库。这一步是不可逆的：``downgrade`` 补回来的只会是空串。
    op.drop_column("retrieval_traces", "kb_id")
    op.drop_column("retrieval_traces", "query")

    op.alter_column(
        "retrieval_traces", "trace_id", existing_type=sa.String(length=32), nullable=False
    )
    op.alter_column(
        "retrieval_traces",
        "kb_ids",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        server_default=None,
    )
    op.alter_column("retrieval_traces", "top_k", existing_type=sa.Integer(), server_default=None)
    op.alter_column(
        "retrieval_traces", "latency_ms", existing_type=sa.Integer(), server_default=None
    )

    op.create_index(
        "ix_retrieval_traces_tenant_id", "retrieval_traces", ["tenant_id"], unique=False
    )
    op.create_index("ix_retrieval_traces_trace_id", "retrieval_traces", ["trace_id"], unique=True)


def downgrade() -> None:
    """退回「一张只存 id 与想法的日志表」。

    **原文回不来**：``query`` 这一列在 upgrade 里已经被删掉，业务库（以及它的备份、
    只读副本）里再没有任何一份副本。这里补的是空串占位——它是「这一行来自哪次升级之前」
    的标记，不是一次数据恢复。真要那份问句，只能去观测栈按 ``trace_id`` 查，而观测栈的
    保留期由它自己管。
    """

    op.drop_index("ix_retrieval_traces_trace_id", table_name="retrieval_traces")
    op.drop_index("ix_retrieval_traces_tenant_id", table_name="retrieval_traces")

    # 两个 NOT NULL 列分两步加：先可空、回填、再收紧（旧库里已经有行了）。
    op.add_column("retrieval_traces", sa.Column("kb_id", sa.String(length=36), nullable=True))
    op.add_column("retrieval_traces", sa.Column("query", sa.Text(), nullable=True))
    op.execute("UPDATE retrieval_traces SET kb_id = kb_ids->>0 WHERE kb_id IS NULL")
    op.execute("UPDATE retrieval_traces SET query = '' WHERE query IS NULL")
    op.alter_column("retrieval_traces", "kb_id", existing_type=sa.String(length=36), nullable=False)
    op.alter_column("retrieval_traces", "query", existing_type=sa.Text(), nullable=False)

    for column in ("trace_id", "kb_ids", "top_k", "latency_ms", "degraded_reason", "empty_reason"):
        op.drop_column("retrieval_traces", column)

    op.rename_table("retrieval_traces", "retrieval_logs")
    op.create_index("ix_retrieval_logs_tenant_id", "retrieval_logs", ["tenant_id"], unique=False)
    op.create_index("ix_retrieval_logs_kb_id", "retrieval_logs", ["kb_id"], unique=False)

"""add index build versions

索引即构建产物（ADR-0025）：新增版本表、库上的「生效索引版本」列、归属索引上的
``index_versions`` 映射列。

**本迁移的顺序是它的全部要点**（ADR-0025 决策七）：

```text
① 加列（库的生效版本、归属索引的映射）+ 建版本表
② 回填：每个已有库一行 v1（状态 active，计数快照取自本次能读到的两张表），
        并把库的生效版本写成 v1
③ 核对：还有库没有生效版本、或生效版本在版本表里找不到 → **抛错中止**
④ 才允许启用版本过滤（检索侧的守卫在 ``app/models/index_version.py``）
```

顺序颠倒等于历史数据静默变空：先启用过滤、后回填的话，历史切片的版本列是空值，
``(kb AND version)`` 一条都匹配不上——不报错，只是搜不到，而且看起来像「这个库没内容」。
所以核对不是一句注释，而是一条**会真的把迁移中止**的语句。

**回填写在迁移里，不放在脚本里**：脚本可以被忘记跑、可以在某台机器上跑漏；迁移只有
「跑过 / 没跑过」两种状态，而且 DDL 与 DML 在同一个事务里——第 ③ 步抛错时，第 ① 步加的
列与第 ② 步写的行一起回滚，不会留下「列已加、过滤已启用、数据没回填」的半成品。

「四项计数相等」里的**向量数**与**关键词文档数**在本库里读不到（那是 pgvector 之外与
Elasticsearch 里的事），它们属于 ``ready`` 的前置校验，由 #142 的激活流程比对；本迁移
能落的只有 ``document_count`` / ``chunk_count`` 两项快照。

Revision ID: e41b7c9d2a63
Revises: c4e9b7d1a2f8（#146 的编排开关；本票与其并行，合并时接在它后面）
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e41b7c9d2a63"
down_revision: Union[str, None] = "c4e9b7d1a2f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 回填出来的版本名：与「索引流水线一直写的那个常量」同值（``chunks.index_version``）。
_BACKFILL_VERSION = "v1"

#: 回填行的主键：由 (租户, 库, 版本) 确定性派生，重复执行不会造出第二个 v1 行
#: （32 位十六进制，落在 ``String(36)`` 里）。用确定性 id 而不是 uuid 生成器，
#: 是因为「这一行就是那个库的 v1」本来就该是同一个身份。
#:
#: **版本名必须单独成一段字面量**（``|| ':' || 'v1'``，而不是 ``|| ':v1'``）：
#: SQLAlchemy 的 ``text()`` 把 ``:v1`` 解析成绑定参数，连引号里也不放过——于是这条
#: 迁移在真 PostgreSQL 上才报 ``A value is required for bind parameter 'v1'``，
#: 而 SQLite 替身与 ``--sql`` 离线渲染都看不出来（#153 真库首跑时抓到）。
_BACKFILL_ID = (
    "md5(kb.tenant_id || ':' || kb.id || ':' || '" + _BACKFILL_VERSION + "')"
)

_BACKFILL_INSERT = f"""
INSERT INTO index_build_versions
    (id, tenant_id, kb_id, version, status, document_count, chunk_count,
     created_at, activated_at)
SELECT
    {_BACKFILL_ID},
    kb.tenant_id,
    kb.id,
    '{_BACKFILL_VERSION}',
    'active',
    (SELECT count(*) FROM documents d WHERE d.kb_id = kb.id AND d.tenant_id = kb.tenant_id),
    (SELECT count(*) FROM chunks c WHERE c.kb_id = kb.id AND c.tenant_id = kb.tenant_id),
    now(),
    now()
FROM knowledge_bases kb
ON CONFLICT (tenant_id, kb_id, version) DO NOTHING
"""

_BACKFILL_KB_COLUMN = (
    "UPDATE knowledge_bases SET active_index_version = '"
    + _BACKFILL_VERSION
    + "' WHERE active_index_version IS NULL"
)

#: 核对的条件：既查「还没回填的库」，也查「回填了一个版本表里不存在的版本名」。
#: **两份核对语句共用这一份条件**——条件写两遍就会在某一次改动里只改一处。
_UNRESOLVED_CONDITION = """
    kb.active_index_version IS NULL
    OR NOT EXISTS (
        SELECT 1 FROM index_build_versions v
        WHERE v.tenant_id = kb.tenant_id
          AND v.kb_id = kb.id
          AND v.version = kb.active_index_version
    )
"""

#: 在线（连库）执行时的核对：读到数字，由下面那句 Python 判断中止迁移。
_VERIFY = f"SELECT count(*) FROM knowledge_bases kb WHERE {_UNRESOLVED_CONDITION}"

#: 离线渲染（``alembic upgrade --sql``）时的核对：没有库可查，于是把同一段判断
#: 写成 DO 块——那份 SQL 被真正执行时**自己会中止**，而不是渲染出一个人畜无害的脚本。
#: 只保留在线那条 Python 判断的话，``--sql`` 会直接崩在 ``op.get_bind()`` 上；
#: 只保留 DO 块的话，在线执行要绕一圈 psycopg 的异常。两种模式各用对的那一种。
_VERIFY_RAISING = f"""
DO $$
DECLARE remaining bigint;
BEGIN
    SELECT count(*) INTO remaining FROM knowledge_bases kb WHERE {_UNRESOLVED_CONDITION};
    IF remaining > 0 THEN
        RAISE EXCEPTION 'index build version backfill incomplete: % knowledge_bases row(s) '
            'have no effective index version', remaining;
    END IF;
END $$;
"""


def upgrade() -> None:
    # ① 加列 + 建表。
    #
    # 库上的生效版本**保持可空**：新的空库还没有生效构建，那是合法状态；而回填漏做时
    # 它必须是 NULL，否则「未回填」这个状态无从被发现（守卫就失去了可看的东西）。
    op.add_column(
        "knowledge_bases",
        sa.Column("active_index_version", sa.String(length=32), nullable=True),
    )
    # 归属索引的映射列：迁移之前写下的行没有版本可记，所以可空、不回填。
    op.add_column(
        "retrieval_traces",
        sa.Column("index_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "index_build_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("kb_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "kb_id", "version", name="uq_index_build_versions_tenant_kb_version"
        ),
        sa.CheckConstraint(
            "status IN ('building', 'ready', 'active', 'archived', 'failed')",
            name="ck_index_build_versions_status",
        ),
    )
    op.create_index(
        op.f("ix_index_build_versions_tenant_id"),
        "index_build_versions",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_index_build_versions_kb_id"), "index_build_versions", ["kb_id"], unique=False
    )
    # 同一库同一时刻最多一个 active / 一个 building（部分唯一索引）。
    op.create_index(
        "uq_index_build_versions_active",
        "index_build_versions",
        ["tenant_id", "kb_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_index_build_versions_building",
        "index_build_versions",
        ["tenant_id", "kb_id"],
        unique=True,
        postgresql_where=sa.text("status = 'building'"),
    )

    # ② 回填（版本行 → 库列，顺序不能反：核对着的是「列指向的行存在」）。
    op.execute(_BACKFILL_INSERT)
    op.execute(_BACKFILL_KB_COLUMN)

    # ③ 核对：不通过就中止整条迁移（同事务，于是 ① ② 一起回滚）。
    #
    # 两种执行模式各用对的那一种（理由见上面两个常量的注释）：连库执行时读到数字，
    # 由这里中止；离线渲染时把同一段判断写成 DO 块，交给那份 SQL 自己中止。
    if op.get_context().as_sql:
        op.execute(_VERIFY_RAISING)
        return

    remaining = op.get_bind().execute(sa.text(_VERIFY)).scalar_one()
    if remaining:
        raise RuntimeError(
            "index build version backfill incomplete: "
            f"{remaining} knowledge_bases row(s) have no effective index version. "
            "Refusing to enable version filtering on top of an un-backfilled table."
        )


def downgrade() -> None:
    # 降级就是放弃「哪次构建」这个能力：映射列、生效版本列与版本表一起走。
    op.drop_index("uq_index_build_versions_building", table_name="index_build_versions")
    op.drop_index("uq_index_build_versions_active", table_name="index_build_versions")
    op.drop_index(op.f("ix_index_build_versions_kb_id"), table_name="index_build_versions")
    op.drop_index(op.f("ix_index_build_versions_tenant_id"), table_name="index_build_versions")
    op.drop_table("index_build_versions")
    op.drop_column("retrieval_traces", "index_versions")
    op.drop_column("knowledge_bases", "active_index_version")

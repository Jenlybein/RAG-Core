"""add document versions（一稿一行）+ chunks.doc_version + 清掉 JSONB 那份副本

让「哪一稿」在库里真实存在（#172 / ADR-0005 D2 D3 D8 D13），同时**行为零变化**：这一步
用户看不出区别，但审计与复盘所需的数据从此有了。三件事：

```text
① 建表 document_versions（一稿一行）+ 按文档的索引
② chunks 加真实稿号列 doc_version（非空、默认 1）+ (document_id, doc_version) 复合索引
③ 断言：有切片的文档正文为空 → 硬停（提示先跑正文补齐脚本）
④ 回填：每篇文档一行当前稿（正文 / 校验和 / 源文件 / 创建时间），存量切片按所属文档的
   当前稿指针回填稿号
⑤ 清 JSONB：``chunks.metadata`` 里那份 ``doc_version`` 副本删掉，稿号只有一个来源
```

**回填写在迁移里，不放在脚本里**（与 ``e41b7c9d2a63`` 同一条理由）：脚本可以被忘记跑、
可以在某台机器上跑漏；迁移只有「跑过 / 没跑过」两种状态，DDL 与 DML 在同一笔事务里，
断言不过时 ①② 一起回滚，不会留下「列已加、版本行没回填」的半成品。

**校验和走 Python 的唯一入口**（``#172`` 的决定）：回填逐行调
:func:`app.utils.checksum.checksum_of`，**不用**数据库的 ``digest()`` / ``pgcrypto``。
理由是判重口径全局只能有一份——库里算一套、Python 算一套的话，两份「碰巧一致」只是
今天的事。代价写在这里：这条迁移不能 ``alembic upgrade --sql`` 离线渲染（渲染时没有库
可读，逐行算不出校验和），碰到 ``as_sql`` 直接拒绝并说明原因，而不是渲染出一份
「只建表不回填」的脚本。

**断言只对「有切片的文档」硬停**：正文为空且有切片，说明正文回写（#170）还没补齐就上线
了——此时给每篇文档建一条空正文版本行，等于把洞写进历史：重建会从版本表读到空正文，
检索静默变空。没有切片的失败文档允许空正文（它本来就没有可检索的内容），由对账脚本
列为「待补」。提示里点名是哪几篇，并给出补齐脚本与 ``--apply``。

**为什么正文为空的判定在 Python 里做，而不是一条 SQL**：判「空」的口径必须与
:func:`app.utils.checksum.normalize_text` 同一份（``\\r\\n``、NFC、首尾空白都磨掉）。
写一条 SQL 近似条件就成了第二份口径，而两份口径会在某天分叉——SQL 放过的「只有换行」
的正文会被写成一条空正文版本行。于是：条件只在 Python 里判一次，PostgreSQL 上把结论
用 ``RAISE EXCEPTION`` 交给库（库级硬停，运维看到的是数据库报错），其余方言（离线套件
里的 SQLite 形状测试）用同一个异常文本中止迁移。

**JSONB 那一步按方言分支**：``metadata - 'doc_version'`` 是 PostgreSQL 的 jsonb 运算，
SQLite 没有它（``tests/test_document_versions_migration.py`` 在 SQLite 上只断言结构部分，
两个方言分支各自有专门的用例）。``downgrade`` 对称地把那份副本**从列**（唯一来源）重新
生出来，而不是凭空消失。

**零外键**：仓库里一张表都没有 ``ForeignKey``（``git grep ForeignKey -- app migrations``
为空），版本表的 ``document_id`` 因此是裸 ``String(36)`` + 索引。「文档删掉时版本行跟着走」
落在删文档那条路径里（:func:`app.repositories.document_version_repository.delete_for_document`），
与清除切片同一笔事务。

Revision ID: 7c2e5a9b4d31
Revises: 5d8a2f1c7b34（#171 的稿次列改名）
Create Date: 2026-11-15 21:10:00.000000

"""

from __future__ import annotations

import hashlib
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.utils.checksum import CHECKSUM_HEX_LENGTH, checksum_of, normalize_text

# revision identifiers, used by Alembic.
revision: str = "7c2e5a9b4d31"
down_revision: Union[str, None] = "5d8a2f1c7b34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 版本表的表名（与 ``app/models/document_version.py`` 同一份）。
_VERSION_TABLE = "document_versions"
#: 切片稿号列的列名（对外响应字段名也叫这个，契约不变）。
_CHUNK_VERSION_COLUMN = "doc_version"
#: 回填出来的那一稿是当前稿（GLOSSARY「文档版本」的两个状态之一）。
_CURRENT = "current"

#: 正文补齐脚本（#170）：迁移中止时提示运维先跑它。默认 dry-run，``--apply`` 才写。
_BACKFILL_SCRIPT = "scripts/backfill_document_content.py"

#: 断言不通过时的**唯一**一句提示（Python 分支与 PostgreSQL 的 RAISE EXCEPTION 共用）。
_EMPTY_CONTENT_MESSAGE = (
    "document versions backfill refused: {count} document(s) have chunks but an empty body "
    "({sample}). The bodies were never written back (#170), so every version row would be "
    "empty and retrieval would silently go blank. Backfill them first: `uv run python "
    + _BACKFILL_SCRIPT
    + "` (dry-run, lists what it would do) and then `uv run python "
    + _BACKFILL_SCRIPT
    + " --apply`. Re-run this migration afterwards. Documents without chunks are allowed to "
    "have an empty body."
)

#: 离线渲染（``alembic upgrade --sql``）被拒绝的原因，原样给运维看。
_OFFLINE_REFUSAL = (
    "add_document_versions must run online: it computes each historical checksum in Python "
    "through app.utils.checksum (the single normalization entry point, #172), so it needs to "
    "read every document body. `alembic upgrade --sql` has no database to read and would "
    "render a migration that builds the table without backfilling it. Run "
    "`uv run alembic upgrade head` against the real database instead."
)

#: 每篇文档的正文、指针与源文件（回填与断言共用这一次读，不读两遍）。
_SELECT_DOCUMENTS = sa.text(
    "SELECT id AS document_id, content AS content, current_version AS current_version, "
    "source_file_path AS source_file_path, created_at AS created_at "
    "FROM documents ORDER BY id"
)

#: 有切片的文档 id（断言只对它们硬停）。
_SELECT_CHUNKED_DOCUMENT_IDS = sa.text(
    "SELECT DISTINCT document_id AS document_id FROM chunks"
)

_INSERT_VERSION = sa.text(
    "INSERT INTO document_versions "
    "(id, document_id, version, content, source_file_path, checksum, status, created_at) "
    "VALUES (:id, :document_id, :version, :content, :source_file_path, :checksum, :status, "
    ":created_at)"
)

#: 存量切片按所属文档的当前稿指针回填；查不到文档的孤儿切片按第 1 稿解释（对账脚本
#: #180 会把它们列成分歧，不由这条迁移裁决）。
_BACKFILL_CHUNK_VERSIONS = sa.text(
    "UPDATE chunks SET doc_version = coalesce("
    "(SELECT d.current_version FROM documents d WHERE d.id = chunks.document_id), 1)"
)

#: 稿号在 JSONB 里那份副本：删掉之后稿号只有一个来源（切片的真实列）。
_DROP_META_DOC_VERSION = (
    "UPDATE chunks SET metadata = metadata - 'doc_version' WHERE metadata IS NOT NULL"
)

#: 回滚时把那份副本**从列**重新生出来（对称：数据不凭空消失，也不凭空出现）。
_RESTORE_META_DOC_VERSION = (
    "UPDATE chunks SET metadata = coalesce(metadata, '{}'::jsonb) "
    "|| jsonb_build_object('doc_version', doc_version)"
)


# ─────────────────────────────────────────────────────────────── 方言与中止


def _dialect_name(bind: sa.engine.Connection) -> str:
    """当前连接的方言名；认不出来时回空串（于是所有方言特化的步骤都跳过）。"""

    return getattr(getattr(bind, "dialect", None), "name", "")


def _is_postgresql(bind: sa.engine.Connection) -> bool:
    return _dialect_name(bind) == "postgresql"


def _raise_exception_sql(message: str) -> str:
    """把一句提示包成 ``DO $$ ... RAISE EXCEPTION ... $$``（单引号转义成两个）。"""

    escaped = message.replace("'", "''")
    return f"DO $$ BEGIN RAISE EXCEPTION '{escaped}'; END $$;"


def _stop_on_empty_content(
    bind: sa.engine.Connection, offending_document_ids: Sequence[str]
) -> None:
    """有切片的文档没有正文：中止整条迁移，并把「先补正文」说清楚。

    PostgreSQL 上先发一句 ``RAISE EXCEPTION``——真库上中止迁移的是数据库自己，运维看到
    的是数据库报错（而不是一段 Python 栈）。随后那句 ``raise`` 是兜底：万一 ``DO`` 块没
    把它拦住（例如语句被跳过），迁移也不许继续往下走。
    """

    message = _EMPTY_CONTENT_MESSAGE.format(
        count=len(offending_document_ids),
        sample=", ".join(list(offending_document_ids)[:3]),
    )
    if _is_postgresql(bind):
        op.execute(_raise_exception_sql(message))
    raise RuntimeError(message)


# ─────────────────────────────────────────────────────────────── 回填与清理


def _version_row_id(document_id: str, version: int) -> str:
    """版本行的确定性 id：``sha256(文档 id : 稿号)`` 的前 32 位。

    与 ``e41b7c9d2a63`` 的回填同一个理由——「这一行就是那篇文档的第 N 稿」本来就该是
    同一个身份，重跑不会造出第二行。用 Python 算而不是数据库函数：这条迁移已经在读正文了，
    再引入一个方言相关的 ``md5()`` 只是多一处要解释的东西。
    """

    return hashlib.sha256(f"{document_id}:{version}".encode("utf-8")).hexdigest()[:32]


def _version_row(document: sa.engine.Row) -> dict[str, object]:
    """一篇文档 → 它那一行的参数（正文 / 校验和 / 源文件 / 创建时间都从文档行来）。"""

    content = document.content or ""
    return {
        "id": _version_row_id(document.document_id, document.current_version),
        "document_id": document.document_id,
        "version": document.current_version,
        "content": content,
        # 校验和走唯一入口（磨换行 / NFC / 首尾空白），与将来写入侧算出来的可比。
        "checksum": checksum_of(content),
        "source_file_path": document.source_file_path,
        "status": _CURRENT,
        # 存量那一稿建于**文档行被创建的时刻**，不是迁移这一刻：审计要回答的是「那次答复
        # 引用的是当时哪一版」，把所有历史都记成今天没有意义。
        "created_at": document.created_at,
    }


def _drop_meta_doc_version(bind: sa.engine.Connection) -> None:
    """删掉 JSONB 里那份稿号副本（只对 PostgreSQL；理由见模块文档）。"""

    if not _is_postgresql(bind):
        return
    op.execute(_DROP_META_DOC_VERSION)


def _restore_meta_doc_version(bind: sa.engine.Connection) -> None:
    """回滚时把稿号副本从列重新生出来（只对 PostgreSQL）。"""

    if not _is_postgresql(bind):
        return
    op.execute(_RESTORE_META_DOC_VERSION)


# ────────────────────────────────────────────────────────────────── upgrade


def upgrade() -> None:
    # ⓪ 离线渲染没有库可读，算不出校验和：拒绝，而不是渲染一份不回填的脚本。
    if op.get_context().as_sql:
        raise RuntimeError(_OFFLINE_REFUSAL)

    bind = op.get_bind()

    # ① 版本表：一稿一行。(document_id, version) 唯一；document_id 是裸 id + 索引（零外键）。
    op.create_table(
        _VERSION_TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_file_path", sa.String(length=1024), nullable=True),
        sa.Column("checksum", sa.String(length=CHECKSUM_HEX_LENGTH), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=_CURRENT),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "version", name="uq_document_versions_document_id_version"
        ),
        sa.CheckConstraint(
            "status IN ('current', 'archived')", name="ck_document_versions_status"
        ),
    )
    op.create_index(
        op.f("ix_document_versions_document_id"),
        _VERSION_TABLE,
        ["document_id"],
        unique=False,
    )

    # ② 切片的稿号变成真实列：非空 + 默认 1 让存量行在加列时就有值（不写 NULL），
    #    随后按所属文档的指针回填（真实数据里指针恒为 1，这里不假设它）。
    op.add_column(
        "chunks",
        sa.Column(
            _CHUNK_VERSION_COLUMN,
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_index(
        "ix_chunks_document_id_doc_version",
        "chunks",
        ["document_id", _CHUNK_VERSION_COLUMN],
        unique=False,
    )

    # ③ 断言：有切片却没有正文的文档 → 中止（提示见 _EMPTY_CONTENT_MESSAGE）。
    #
    #    只在写任何一行**之前**判：断言不过时这一笔只回滚 DDL，库里不留半成品版本行。
    documents = list(bind.execute(_SELECT_DOCUMENTS))
    chunked_ids = {
        row.document_id for row in bind.execute(_SELECT_CHUNKED_DOCUMENT_IDS)
    }
    offending = [
        document.document_id
        for document in documents
        if document.document_id in chunked_ids
        and not normalize_text(document.content or "")
    ]
    if offending:
        _stop_on_empty_content(bind, offending)

    # ④ 回填：每篇文档一行当前稿（一条 executemany，不逐行往返）。
    bind.execute(_INSERT_VERSION, [_version_row(document) for document in documents])

    # ⑤ 存量切片按文档指针回填稿号，于是「切片稿号 = 文档指针」当场成立。
    bind.execute(_BACKFILL_CHUNK_VERSIONS)

    # ⑥ 清 JSONB 里那份副本：稿号从此只有一个来源。
    _drop_meta_doc_version(bind)


# ──────────────────────────────────────────────────────────────── downgrade


def downgrade() -> None:
    bind = op.get_bind()

    # ⑤ 先按列把 JSONB 副本补回去（必须在删列之前：它是唯一来源）。
    _restore_meta_doc_version(bind)

    # ② 撤切片稿号：索引 → 列。
    op.drop_index("ix_chunks_document_id_doc_version", table_name="chunks")
    op.drop_column("chunks", _CHUNK_VERSION_COLUMN)

    # ① 撤版本表。
    op.drop_index(op.f("ix_document_versions_document_id"), table_name=_VERSION_TABLE)
    op.drop_table(_VERSION_TABLE)

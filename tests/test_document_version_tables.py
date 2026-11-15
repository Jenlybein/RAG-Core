"""#172 · 版本表与切片稿号列的形状。

票面要的三件事在这里是可执行的形状，而不是迁移文件里抄一遍的文本：

1. **一稿一行**：``document_versions`` 的主键是行 id，``(document_id, version)`` 唯一；
2. **零外键**：仓库里一张表都没有 ``ForeignKey``（``git grep ForeignKey -- app migrations``
   为空），版本表照这条走——``document_id`` 是裸字符串 id + 索引，「文档删掉时版本行跟着
   走」由删文档那条路径显式删除（见 ``app/repositories/document_version_repository.py``），
   不靠库级级联；
3. **稿号只有一个来源**：``chunks`` 上有真实的 ``doc_version`` 列（非空、默认 1）与
   ``(document_id, doc_version)`` 复合索引；JSONB 里那份副本由迁移一次性删掉（见
   ``tests/test_document_versions_migration.py``），这里守着列真的在。

迁移 ``add_document_versions`` 与模型拿的是同一份形状，所以对 ``Base.metadata`` 断言
就等于对库断言——两边不一致时，``alembic check`` 之外的这条路也会响。
"""

from __future__ import annotations

import pytest
from sqlalchemy import ForeignKeyConstraint, Integer, String, Text, inspect

import app.models as models_package
from app.db.base import Base
from app.models.chunk import Chunk
from app.models.document_version import (
    DOCUMENT_VERSION_ARCHIVED,
    DOCUMENT_VERSION_CURRENT,
    DocumentVersion,
)

#: 版本行按 D3 定下的列，一个不多一个不少（``document_id`` 是裸 id，没有外键）。
EXPECTED_COLUMNS = {
    "id",
    "document_id",
    "version",
    "content",
    "source_file_path",
    "checksum",
    "status",
    "created_at",
}


def _column(model: type[Base], name: str):  # noqa: ANN202 - 返回的是 SQLAlchemy 列
    return model.__table__.c[name]


def _index_signature(index: object) -> tuple[str, tuple[str, ...], bool]:
    return (
        index.name,
        tuple(column.name for column in index.columns),
        index.unique,
    )


# ────────────────────────────────────────────────────────────── 注册与列集合


def test_the_versions_table_is_registered_in_metadata() -> None:
    """``app.models`` 是 alembic 的入口：没导出的模型等于不存在于 metadata。"""

    assert DocumentVersion.__tablename__ == "document_versions"
    assert "document_versions" in Base.metadata.tables
    assert "DocumentVersion" in models_package.__all__
    assert models_package.DocumentVersion is DocumentVersion


def test_the_versions_table_has_exactly_the_enumerated_columns() -> None:
    assert set(DocumentVersion.__table__.c.keys()) == EXPECTED_COLUMNS


def test_the_primary_key_is_the_row_id_not_the_pair() -> None:
    """``(document_id, version)`` 是唯一约束，不是主键——版本行要能被单独引用。"""

    table = DocumentVersion.__table__
    assert [column.name for column in table.primary_key.columns] == ["id"]
    assert isinstance(_column(DocumentVersion, "id").type, String)
    assert _column(DocumentVersion, "id").type.length == 36


def test_every_column_is_non_nullable_except_the_source_path() -> None:
    for name in EXPECTED_COLUMNS - {"source_file_path"}:
        assert _column(DocumentVersion, name).nullable is False, f"{name} 必须非空"
    assert _column(DocumentVersion, "source_file_path").nullable is True, (
        "JSON 上传的文档没有源文件；重解析不了的版本行仍然是一稿"
    )


def test_content_and_checksum_are_typed_for_what_they_store() -> None:
    assert isinstance(_column(DocumentVersion, "content").type, Text)
    checksum = _column(DocumentVersion, "checksum")
    assert isinstance(checksum.type, String)
    assert checksum.type.length == 64, "sha256 的十六进制摘要是定长 64"
    assert isinstance(_column(DocumentVersion, "version").type, Integer)
    assert isinstance(_column(DocumentVersion, "status").type, String)


def test_the_version_number_has_no_default() -> None:
    """稿号由写入侧算（max+1），不给默认值：默认 1 会让第二次写入静默撞唯一约束。"""

    assert _column(DocumentVersion, "version").server_default is None
    assert _column(DocumentVersion, "version").default is None


def test_a_new_row_is_current_by_default() -> None:
    status = _column(DocumentVersion, "status")
    assert status.server_default.arg == DOCUMENT_VERSION_CURRENT == "current"
    assert DOCUMENT_VERSION_ARCHIVED == "archived"


def test_created_at_gets_a_python_side_default() -> None:
    assert _column(DocumentVersion, "created_at").default is not None


# ────────────────────────────────────────────────────────────── 约束与索引


def test_one_document_never_has_two_rows_of_the_same_version() -> None:
    unique = {
        tuple(column.name for column in constraint.columns): constraint.name
        for constraint in DocumentVersion.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert tuple(sorted(unique)) == (("document_id", "version"),), (
        f"唯一约束必须恰好是 (document_id, version)，实际 {sorted(unique)}"
    )
    assert unique[("document_id", "version")] == ("uq_document_versions_document_id_version"), (
        "约束要取名：迁移与回滚都按名字引用它"
    )


def test_the_document_id_column_is_indexed() -> None:
    """按文档列版本是唯一的热路径（列表 / 回滚 / 保留策略都按它取行）。"""

    indexes = {_index_signature(index) for index in DocumentVersion.__table__.indexes}
    assert ("ix_document_versions_document_id", ("document_id",), False) in indexes


# ──────────────────────────────────────────────── 零外键 / 零关系（仓库铁律）


@pytest.mark.parametrize("model", [DocumentVersion, Chunk], ids=["document_versions", "chunks"])
def test_no_foreign_keys_anywhere(model: type[Base]) -> None:
    """引用关系靠应用层校验：加了外键就有两套引用风格，而既有业务表一张都没有。"""

    table = model.__table__
    assert not table.foreign_keys
    assert not [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]


def test_the_versions_table_has_no_orm_relationship() -> None:
    assert len(inspect(DocumentVersion).relationships) == 0


# ────────────────────────────────────────────────────── 切片上的稿号真实列


def test_chunks_carry_a_real_doc_version_column() -> None:
    column = _column(Chunk, "doc_version")
    assert isinstance(column.type, Integer)
    assert column.nullable is False
    assert column.server_default.arg == "1", "存量切片按第 1 稿解释"
    assert column.default is not None and column.default.arg == 1


def test_chunks_index_document_and_version_together() -> None:
    """``(document_id, doc_version)`` 复合索引：按文档 + 稿取片是最常用的两种形状。"""

    indexes = {_index_signature(index) for index in Chunk.__table__.indexes}
    assert ("ix_chunks_document_id_doc_version", ("document_id", "doc_version"), False) in (indexes)


def test_the_pointer_and_the_chunk_column_are_two_different_columns() -> None:
    """文档行上的「当前稿指针」与切片上的「稿号」被刻意分开（#171 / D2）。"""

    from app.models.document import Document

    assert "current_version" in Document.__table__.c
    assert "current_version" not in Chunk.__table__.c
    assert "doc_version" in Chunk.__table__.c
    assert "doc_version" not in Document.__table__.c

"""#179 · 归属索引记下「这次命中的是哪几稿」。

用户故事第 3 条要的是一句事后能回答的话：「那次查的是第几稿」——**即使那一稿的切片后来
被保留策略清掉**（#178 会真的删掉旧稿的切片与版本行）。`chunk_ids` 只能回答「命中过哪些
片」，片被清掉之后就只剩一串没有意义的 id；所以这一列存的是**文档 → 稿号**：

```text
doc_versions = {"doc-1": 2, "doc-2": 1}
```

形状与既有的 `index_versions`（`{kb_id: 版本}`）**完全同形**，理由也同一条（ADR-0010 §5）：
两个平行数组（`document_ids` 一个、`versions` 一个）拼得出「A 文档配 B 文档稿号」这种不存在
的组合，而且不报错。稿号是整数，映射的键是文档 id——都是结构，不是原文，ADR-0021「只存
结构」的红线不受影响。

三条要守的东西：

1. **列的形状**：JSONB 映射、可空（迁移之前的老行没有稿号可记）、不与平行数组并存；
2. **写入**：每次检索都写，且写的就是**交付单元上那个真实列读出来的稿号**（#174）；
3. **历史行为不变**：老行是 NULL，读取方不必为「这一列有没有值」写分支。

数据库一律替身、Provider 一律离线：整套不连 PostgreSQL、不连 ES。
"""

from __future__ import annotations

from app.models.retrieval_trace import RetrievalTrace

#: 一次检索里交付的两个单元（折叠之后每单元代表一节，形状与 `collapse_results` 一致：
#: `document_id` 与 `doc_version` 都是交付单元上的真实字段，#174 起稿号来自 `chunks.doc_version`）。
DELIVERED_UNITS = [
    {"chunk_id": "chunk-1", "document_id": "doc-1", "doc_version": 2, "source_chunk_ids": []},
    {"chunk_id": "chunk-2", "document_id": "doc-1", "doc_version": 2, "source_chunk_ids": []},
    {"chunk_id": "chunk-3", "document_id": "doc-2", "doc_version": 1, "source_chunk_ids": []},
]


# ─────────────────────────────────────────────────────────────────── 列的形状


def test_the_trace_table_adds_a_doc_versions_mapping_column() -> None:
    """第十二列 `doc_versions`：**JSONB 映射**，可空（老行没有稿号可记）。"""

    column = RetrievalTrace.__table__.columns["doc_versions"]

    assert str(column.type) == "JSONB", "映射必须是一个 JSON 对象，不是两个数组"
    assert column.nullable is True, "迁移之前写下的行没有稿号可记（那时稿号恒为 1）"


def test_the_column_is_named_for_the_draft_number_inside_the_mapping() -> None:
    """列名是 `doc_versions`（映射），与「当前稿指针」`current_version`、切片的
    `chunks.doc_version` 都不是同一件事——三个名字各说各的事（术语表「文档版本」）。"""

    columns = {column.key for column in RetrievalTrace.__table__.columns}

    assert "doc_versions" in columns
    assert "document_versions" not in columns, "别与版本表 `document_versions` 撞名"
    assert "current_version" not in columns, "指针是文档行上的东西，不是这一行的"

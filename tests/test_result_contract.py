"""响应契约：一条结果代表一节（ADR-0013）。

只钉对外字段，不碰折叠算法（那在 test_result_collapse.py）。
"""

from __future__ import annotations

from app.schemas.rag import RetrievedChunk


def _payload(**overrides) -> dict:
    base = {
        "document_id": "doc-1",
        "chunk_id": "c-77",
        "title": "部署实施指南",
        "content": "整节正文。",
        "score": 0.91,
        "kb_id": "kb-1",
        "section_id": "a3f9",
        "source_chunk_ids": ["c-77", "c-81"],
        "expansion": "parent",
    }
    base.update(overrides)
    return base


def test_new_identity_fields_are_declared() -> None:
    """身份是节；代表片与溯源片各就各位。"""

    chunk = RetrievedChunk.model_validate(_payload())

    assert chunk.section_id == "a3f9"
    assert chunk.chunk_id == "c-77"
    assert chunk.source_chunk_ids == ["c-77", "c-81"]
    assert chunk.expansion == "parent"


def test_collapsed_result_needs_no_parent_field() -> None:
    """折叠之后 content 就是大块，父节不该再套一层。"""

    chunk = RetrievedChunk.model_validate(_payload())

    assert chunk.parent is None
    assert chunk.table_context is None


def test_deprecated_fields_still_accept_old_payloads() -> None:
    """过渡期：旧调用方送来的 parent 字段仍能解析，不炸。"""

    chunk = RetrievedChunk.model_validate(
        _payload(
            parent={
                "section_id": "a3f9",
                "heading_path": "指南 > 上架",
                "content": "整节正文。",
            }
        )
    )

    assert chunk.parent is not None
    assert chunk.parent.section_id == "a3f9"


def test_locating_fields_default_to_null_for_markdown() -> None:
    """Markdown 没有页码，也不能凭空造一个。"""

    chunk = RetrievedChunk.model_validate(_payload())

    assert chunk.page is None
    assert chunk.doc_version is None
    assert chunk.split_strategy is None


def test_source_chunk_ids_defaults_to_empty_list() -> None:
    """老响应没有这个字段时给空列表，不给 None——调用方少一处判空。"""

    payload = _payload()
    payload.pop("source_chunk_ids")
    payload.pop("section_id")
    payload.pop("expansion")

    chunk = RetrievedChunk.model_validate(payload)

    assert chunk.source_chunk_ids == []
    assert chunk.section_id is None
    assert chunk.expansion is None

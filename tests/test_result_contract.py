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


def test_collapsed_result_carries_the_heading_path() -> None:
    """折叠后 parent 那层没了，章节路径直接平铺在这条结果上。"""

    chunk = RetrievedChunk.model_validate(_payload(heading_path="指南 > 上架"))

    assert chunk.heading_path == "指南 > 上架"


def test_removed_fields_are_gone_from_the_contract() -> None:
    """ADR-0013 删掉 parent 与 table_context：折叠后 content 就是大块，它们是重复数据。

    旧调用方若还在读这两个字段，会拿到 KeyError——这是破坏性变更，ADR 里写明了。
    """

    dumped = RetrievedChunk.model_validate(_payload()).model_dump()

    assert "parent" not in dumped
    assert "table_context" not in dumped


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


def test_the_result_says_which_part_and_whether_it_was_cut() -> None:
    """一条结果要能说出「这是同一节的第几段」与「正文被裁过没有」（第 16 步 #207）。

    这两项都是**防误读**的字段：没有 ``section_part``，同一节的第 2 段看起来像另一节；
    没有 ``truncated``，一个片段会被读成全文——而正文确实是有限的，只是调用方不知道。
    """

    chunk = RetrievedChunk.model_validate(_payload(section_part=2, truncated=True))

    assert chunk.section_part == 2
    assert chunk.truncated is True


def test_an_old_index_result_defaults_to_part_zero_and_not_truncated() -> None:
    """老索引没有段号、也没有裁过：缺省值必须是「第 0 段、没裁」。

    缺省填反了（比如 ``truncated=True``）会让每一条老结果都挂着「被裁过」的标记，
    那个提示很快就没人看了。
    """

    chunk = RetrievedChunk.model_validate(_payload())

    assert chunk.section_part == 0
    assert chunk.truncated is False

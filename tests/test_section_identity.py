"""#143 · 节的身份证改成「文档 id + 标题路径」的稳定哈希（不含版本）。

ADR-0025 决策五给了三条硬约束，本文件逐条钉住：

1. **必须带文档 id**：两份文档都叫「退款 > 流程」时，两条节必须是**两个** id。
   不带文档 id 的旧算法（``sha256(heading_path)``）在这里会给同一个 id，跨文档
   一对就串——而且不报错。
2. **必须不含版本**：同一份文档重建两次，每一节都得到同一个 id。这是「新旧构建
   逐节对比」的前提；id 里混进版本之后，两次构建的 id 全不同，对比无从谈起。
3. **版本由引用方的数据范围表达**：所以 ``section_id_of`` 的入参里根本没有版本这
   个位置——不是「传了个空版本」，是**没有那个参数**。

接缝是切块器的公开面（``split`` 产出的 ``metadata``）与 :mod:`app.utils.section_identity`
的两个纯函数：用例不看内部怎么拼字符串，只看「两条节的 id 相不相等」。

顺带钉住一条**不该变**的东西：折叠的归组键是 ``unit_path_of(heading_path)``（章节路径
前两段），与 ``section_id`` 无关。节身份改造不许顺手把折叠改成用新 id——本文件最后两条
用例就是这条边界的哨兵。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from app.models.document import Document
from app.models.knowledge_base import SPLIT_STRUCTURE
from app.services.result_collapse import unit_id_of, unit_path_of
from app.utils.markdown_splitter import MarkdownStructuredSplitter
from app.utils.section_identity import (
    parent_heading_path,
    parent_section_id_of,
    section_id_of,
)
from app.utils.status import DocumentStatus
from tests.fake_db import ZERO_COUNT

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

#: 两份文档共用的一小篇 Markdown：h1 只有标题（无正文），正文落在 h2 / h3 上。
MARKDOWN = """# 售后手册

## 退款

退款政策见下。

### 流程

1. 提交申请

2. 客服审核

## 发票

发票由财务开具。
"""

DOC_A = "doc-aaaaaaaa-0000-4000-8000-000000000001"
DOC_B = "doc-bbbbbbbb-0000-4000-8000-000000000002"


def _split(document_id: str, text: str = MARKDOWN) -> list[dict[str, object]]:
    """切一遍，返回 ``[metadata]``（顺序即切片顺序）。"""

    splitter = MarkdownStructuredSplitter(
        chunk_size=800, chunk_overlap=100, document_id=document_id
    )
    return [piece.metadata for piece in splitter.split(text)]


# ─────────────────────────────────────── 验收 ①：两份文档同名章节得不同 id


def test_two_documents_with_the_same_heading_path_get_different_section_ids() -> None:
    """「退款 > 流程」在两份文档里是两条不同的节。"""

    first = {str(meta["heading_path"]): meta for meta in _split(DOC_A)}
    second = {str(meta["heading_path"]): meta for meta in _split(DOC_B)}

    shared = set(first) & set(second)
    assert "售后手册 > 退款 > 流程" in shared, "两份文档必须真的有同名章节，否则这条用例什么都没验"

    for path in sorted(shared):
        assert first[path]["section_id"] != second[path]["section_id"], (
            f"同名章节「{path}」在两份文档里撞了同一个 section_id"
        )
        assert first[path]["parent_section_id"] != second[path]["parent_section_id"], (
            f"同名章节「{path}」的父 id 也撞了"
        )


# ─────────────────────────────────────── 验收 ②：同一文档重建两次得同一 id


def test_the_same_document_split_twice_gets_identical_section_ids() -> None:
    """重建两次（两次构造切块器、两次切同一篇正文）→ 逐节 id 相同。"""

    first = _split(DOC_A)
    second = _split(DOC_A)

    assert [meta["heading_path"] for meta in first] == [meta["heading_path"] for meta in second]
    assert [meta["section_id"] for meta in first] == [meta["section_id"] for meta in second]
    assert [meta["parent_section_id"] for meta in first] == [
        meta["parent_section_id"] for meta in second
    ]


def test_section_id_is_a_stable_hex_digest_of_document_and_path() -> None:
    """id 是 64 位十六进制，且只由 (文档 id, 标题路径) 两件事决定。"""

    section_id = section_id_of(document_id=DOC_A, heading_path="售后手册 > 退款")

    assert _HEX64.match(section_id), section_id
    assert section_id == section_id_of(document_id=DOC_A, heading_path="售后手册 > 退款")
    assert section_id != section_id_of(document_id=DOC_B, heading_path="售后手册 > 退款")
    assert section_id != section_id_of(document_id=DOC_A, heading_path="售后手册 > 发票")


# ─────────────────────────────────────── 父子推导：仍是「上一级路径算出来的那个 id」


def test_child_parent_id_equals_the_parent_sections_own_id() -> None:
    """子节的 ``parent_section_id`` 等于父节自己的 ``section_id``（同一份文档内）。"""

    pieces = {str(meta["heading_path"]): meta for meta in _split(DOC_A)}

    parent = pieces["售后手册 > 退款"]
    child = pieces["售后手册 > 退款 > 流程"]

    assert parent["parent_section_id"] == section_id_of(document_id=DOC_A, heading_path="售后手册")
    assert child["parent_section_id"] == parent["section_id"]


def test_parent_heading_path_is_the_previous_level_only() -> None:
    """父路径 = 标题路径去掉最后一段；只有一段或空路径时没有父。"""

    assert parent_heading_path("售后手册 > 退款 > 流程") == "售后手册 > 退款"
    assert parent_heading_path("售后手册 > 退款") == "售后手册"
    assert parent_heading_path("售后手册") is None
    assert parent_heading_path("") is None

    assert parent_section_id_of(document_id=DOC_A, heading_path="售后手册") is None
    assert parent_section_id_of(
        document_id=DOC_A, heading_path="售后手册 > 退款 > 流程"
    ) == section_id_of(document_id=DOC_A, heading_path="售后手册 > 退款")


# ─────────────────────────────────────── 边界：折叠与单元归组不参与这次改造


def test_collapse_unit_identity_is_independent_of_section_id() -> None:
    """折叠的归组键仍是标题路径前两段，与节的身份证是两套东西。

    两份文档的同名单元会得到**同一个** ``unit_id_of``——折叠本来就是这样：区分它们的是
    ``UnitKey`` 里的 ``document_id``，不是单元 id。这条用例存在的意义是拦住「顺手把折叠
    改成用新 ``section_id``」：那样两份文档的同名单元会被拆成两条，交付口径当场变化。
    """

    path = "售后手册 > 退款 > 流程"

    assert unit_path_of(path) == "售后手册 > 退款"
    assert unit_id_of(path) == unit_id_of(path)
    # 同一段路径在两个文档里算出的单元 id 相同：单元 id 与文档无关，节 id 有关。
    assert section_id_of(document_id=DOC_A, heading_path=path) != section_id_of(
        document_id=DOC_B, heading_path=path
    )


def test_every_piece_carries_the_same_section_id_inside_one_section() -> None:
    """同一节的多片共享一个 ``section_id``：身份属于节，不属于片。"""

    pieces = _split(DOC_A)

    assert len({meta["section_id"] for meta in pieces}) == len(
        {meta["heading_path"] for meta in pieces}
    )
    assert all(meta["section_id"] for meta in pieces)


# ─────────────────────────────────────── 接线：生产路径必须把文档 id 交下去


class _PipelineSession:
    """索引流水线用的最小异步会话：只回答「按主键取文档」与收尾三件套。

    这条用例要验的是**接线**（``IndexingService`` 有没有把文档 id 交给切块器），
    不是数据库行为：抢占与库配置都被换成替身，会话因此只需要能把文档行交出去。
    """

    def __init__(self, document: Any) -> None:
        self.info: dict[str, Any] = {}
        self._document = document
        self.commits = 0

    async def get(self, _model: Any, ident: Any) -> Any | None:
        return self._document if ident == self._document.id else None

    async def execute(self, _statement: Any) -> Any:
        """抢占与库配置都已换成替身；这里只剩建边读节目录（#152），替身表示空版本。"""

        return ZERO_COUNT

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _StubEmbeddings:
    """固定 4 维向量（向量存储是替身，维度不重要），绝不发网络。"""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]

    async def embed_query(self, _query: str) -> list[float]:
        raise AssertionError("索引链路不应调用单条查询向量化")


class _RecordingStore:
    """记下写进存储的切片字典（可观测结果就是「落库的 meta」）。"""

    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []

    async def delete_by_document_id(
        self, _db: Any, _document_id: str, *, tenant_id: str, index_version: str | None = None
    ) -> None:
        return None

    async def add_chunks(self, _db: Any, chunks: list[dict[str, Any]]) -> None:
        self.written.extend(chunks)


def _document(document_id: str) -> Document:
    return Document(
        id=document_id,
        tenant_id="tenant-identity",
        kb_id="kb-identity",
        title="售后手册",
        content=MARKDOWN,
        source_type="text",
        status=DocumentStatus.SUCCESS,
    )


def _index_one_document(
    document: Document, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    """跑一遍**真实**的 ``IndexingService``（只换 embedding / 存储 / 库配置）。"""

    from app.core.config import settings
    from app.repositories import document_repository, knowledge_base_repository
    from app.services import indexing_service

    real_build_splitter = indexing_service.build_splitter
    document_ids: list[str] = []

    def _recording_build_splitter(strategy: str, **kwargs: Any) -> Any:
        # 记下生产路径传了什么，再交给真实现——否则这条用例就只是在测替身。
        document_ids.append(str(kwargs.get("document_id", "")))
        return real_build_splitter(strategy, **kwargs)

    async def _claim(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _split_config(*_args: Any, **_kwargs: Any) -> tuple[str, int]:
        return SPLIT_STRUCTURE, 800

    async def _effective_version(*_args: Any, **_kwargs: Any) -> str:
        return "v1"

    monkeypatch.setattr(indexing_service, "build_splitter", _recording_build_splitter)
    monkeypatch.setattr(document_repository, "try_claim_for_indexing_async", _claim)
    monkeypatch.setattr(knowledge_base_repository, "get_split_config", _split_config)
    monkeypatch.setattr(
        knowledge_base_repository, "get_effective_index_version", _effective_version
    )
    monkeypatch.setattr(settings, "SECTION_CONTEXT_ENABLED", False)

    store = _RecordingStore()
    service = indexing_service.IndexingService(embeddings=_StubEmbeddings(), stores=[store])
    asyncio.run(service.index_existing_document(_PipelineSession(document), document.id))

    assert document_ids == [document.id], "生产路径必须把文档 id 交给切块器"
    return store.written


def test_the_indexing_pipeline_stamps_section_ids_with_the_document_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产线上的 ``section_id`` = ``stable_hash(文档 id : 标题路径)``（不是只哈希路径）。

    这是「跨文档不撞」真正生效的地方：切块器算得再对，只要流水线没把文档 id 传下去，
    落库的 id 仍然是路径的自哈希。
    """

    written = _index_one_document(_document(DOC_A), monkeypatch)

    by_path = {chunk["meta"]["heading_path"]: chunk["meta"] for chunk in written}
    assert set(by_path) == {"售后手册 > 退款", "售后手册 > 退款 > 流程", "售后手册 > 发票"}

    for path, meta in by_path.items():
        assert meta["section_id"] == section_id_of(document_id=DOC_A, heading_path=path)
        assert meta["parent_section_id"] == parent_section_id_of(
            document_id=DOC_A, heading_path=path
        )



"""按篇重建后 v1 与 v2 **逐节同一身份**（#143 的身份判据）。

这张票要求的两件事在这里落地：

1. **重建不许另写一套**——两次都走真实的 ``IndexingService`` 索引流水线（与上传 / 单篇重建
   同一条），本文件的第二份产物就是这么来的；
2. **改造前后语义一致**：重建后同一节的 ``section_id`` 与改造前**语义**一致——父子关系
   与折叠分组都不变，只有 id 的算法换了（带上文档 id）。这正是「可逐节对比新旧产物」的前提
   （ADR-0025 决策五）。

接缝是索引流水线本身 + 真实的 ``IndexingService`` / 真实切块器：只把 embedding、向量
存储、关键词 Provider 与「库配置 / 文档行 / 租约」的读取换成替身，不连 PostgreSQL、不连 ES。

**为什么父子关系用「路径对」来断言**：父子关系由``标题路径``决定（上一级路径），与 id
用哪种算法无关。所以「两版之间 (子路径, 父路径) 的集合相同」才是「父子关系不变」的准确
说法；直接比 id 值只会在两种算法下都成立（因为两边各自自洽），验不出东西。

**库级构建那一层已经删除**：本文件原先用的是 ``IndexBuildService`` 的
``start_build → build → activate``，那条状态机（版本行、四计数、激活、别名切换）随第 17 步
整层退场，全库重建现在就是「按篇投递单篇重建任务」。于是这里改成直接跑两次索引流水线：
第二份产物落在**下一代**（``chunks.generation``），判据换成按代收集切片——节身份与折叠
口径一个字都没变，变的只是产物归属的表达。

原先还有两节验「发起入口 / 回滚入口」（靠 ``scripts/rebuild_kb_version.py``）与一节验
``app.services.index_verification``；前者随库级版本能力删除、后者随核对模块整层删除，
一并删掉。这里只留索引流水线本身的语义。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.config import settings
from app.db.base import now
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.knowledge_base import SPLIT_STRUCTURE, KnowledgeBase
from app.repositories import document_repository, knowledge_base_repository
from app.services.indexing_service import IndexingService
from app.services.result_collapse import Hit, unit_key
from app.utils.section_identity import parent_heading_path, section_id_of
from app.utils.status import DocumentStatus
from tests.fake_db import AsyncRecordingSession

TENANT_ID = "tenant_rebuild_identity"
KB_ID = "kb-rebuild"
DOC_A = "doc-rebuild-aaaaaaaa-0000-4000-8000-000000000001"
DOC_B = "doc-rebuild-bbbbbbbb-0000-4000-8000-000000000002"

#: 带 h3 与表格的一小篇：正文片、整表片、行级表片都会出现。
MARKDOWN = """# 售后手册

## 退款

退款 3 个工作日内到账。

### 流程

1. 提交申请

2. 客服审核

| 环节 | 时限 |
| --- | --- |
| 审核 | 1 天 |
| 到账 | 3 天 |

## 发票

发票由财务开具。
"""

EMBEDDING_DIM = 4


# ──────────────────────────────────────────────────────────────── 替身


class _StubEmbeddings:
    """固定 4 维向量（向量存储是替身，维度不重要），绝不发网络。"""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * EMBEDDING_DIM for _ in texts]

    async def embed_query(self, _query: str) -> list[float]:
        raise AssertionError("索引链路不应调用单条查询向量化")


class _GenerationalStore:
    """向量存储替身 + 观测点：按**代**收住写入的切片。

    判据从「库级版本」换成「篇级代」（现场九）：每次索引写下一代，上一代留在库里给回滚。
    所以这里按 ``chunk["generation"]`` 分组——索引流的两次在不同代上，节身份因此可逐代对比。
    """

    def __init__(self, session: AsyncRecordingSession) -> None:
        self._session = session
        self.by_generation: dict[int, list[dict[str, Any]]] = {}
        self.purged: list[tuple[str, int | None]] = []

    async def delete_by_document_id(
        self,
        _db: Any,
        document_id: str,
        *,
        tenant_id: str,
        doc_version: int | None = None,
        generation: int | None = None,
    ) -> None:
        self.purged.append((document_id, generation))

    async def add_chunks(self, _db: Any, chunks: list[dict[str, Any]]) -> None:
        for chunk in chunks:
            generation = int(chunk["generation"])
            self.by_generation.setdefault(generation, []).append(chunk)
            self._session.add(
                Chunk(
                    id=chunk["id"],
                    tenant_id=chunk["tenant_id"],
                    kb_id=chunk["kb_id"],
                    document_id=chunk["document_id"],
                    content=chunk["content"],
                    embedding=[0.0] * 1536,
                    meta=chunk["meta"],
                    # 稿号与代都照抄：真库上这两列非空，替身缺它们就不再等价于真行。
                    doc_version=chunk["doc_version"],
                    generation=generation,
                )
            )


# ──────────────────────────────────────────────────────────────── 夹具


def _kb() -> KnowledgeBase:
    """还没算过任何产物的库（每篇的 ``current_index_generation`` 从 NULL 起算）。"""

    return KnowledgeBase(
        id=KB_ID,
        tenant_id=TENANT_ID,
        name="售后",
        description="",
        split_strategy=SPLIT_STRUCTURE,
        allowed_formats=["markdown"],
        settings={},
    )


def _document(document_id: str) -> Document:
    return Document(
        id=document_id,
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        title="售后手册",
        content=MARKDOWN,
        source_type="text",
        status=DocumentStatus.SUCCESS,
        current_version=1,
    )


def _session(*, kb: KnowledgeBase, documents: list[Document]) -> AsyncRecordingSession:
    return AsyncRecordingSession(rows={KnowledgeBase: [kb], Document: list(documents), Chunk: []})


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch, *, documents: dict[str, Document]) -> None:
    """把「读文档行 / 抢租约 / 读库配置 / 推代指针」换成替身，其余全部走真实现。

    ``advance_index_generation`` 的替身除了放行，还要**在文档对象上推指针**：
    生产里它是一条 UPDATE，而本文件用的是内存替身（写语句不改行的值），不推指针的话
    第二次索引会看到同一个当前代、又写一遍第 1 代——两次产物落在同一代上就比不了节身份。
    """

    async def _get_by_id(_db: Any, document_id: str) -> Any:
        return documents.get(document_id)

    async def _claim(*_args: Any, **_kwargs: Any) -> Any:
        # 抢到回租约令牌（#213）。令牌核对的判定只认真库（行锁 + 库里那个时间戳），
        # 本文件验的是节目录 / 稿号这类身份，与租约归属无关，因此固定放行。
        return now()

    async def _holds_index_claim(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _split_config(*_args: Any, **_kwargs: Any) -> tuple[str, int, int]:
        return SPLIT_STRUCTURE, 800, 2000

    async def _advance_generation(
        _db: Any, document_id: str, *, tenant_id: str, generation: int, settings_hash: str | None
    ) -> None:
        document = documents.get(document_id)
        if document is not None:
            document.current_index_generation = generation
            document.settings_hash = settings_hash

    monkeypatch.setattr(document_repository, "get_by_id", _get_by_id)
    monkeypatch.setattr(document_repository, "try_claim_for_indexing_async", _claim)
    monkeypatch.setattr(document_repository, "holds_index_claim_async", _holds_index_claim)
    monkeypatch.setattr(document_repository, "advance_index_generation", _advance_generation)
    monkeypatch.setattr(knowledge_base_repository, "get_split_config", _split_config)
    # 节说明会调模型：本票与它无关，钉死成关闭（检索文本因此逐字可比）。
    monkeypatch.setattr(settings, "SECTION_CONTEXT_ENABLED", False)


def _service(store: _GenerationalStore) -> IndexingService:
    return IndexingService(
        embeddings=_StubEmbeddings(),
        stores=[store],
    )


def _rebuild(service: IndexingService, session: AsyncRecordingSession, document: Document) -> None:
    """跑一次真实的单篇索引（按篇重建走的就是这一段）。"""

    asyncio.run(service.index_existing_document(session, document.id, allow_success=True))


def _by_section(chunks: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """``(文档 id, 标题路径) → 切片字典``（同一节的片取第一片即可：身份是节级的）。"""

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for chunk in chunks:
        grouped.setdefault((str(chunk["document_id"]), str(chunk["meta"]["heading_path"])), chunk)
    return grouped


# ─────────────────────────────────────────── 按篇重建：两代产物逐节同一身份


def test_a_second_indexing_pass_keeps_every_section_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同库同文档重建一次（第 1 代 → 第 2 代），每一节的 id 与父 id 都不变。"""

    document = _document(DOC_A)
    session = _session(kb=_kb(), documents=[document])
    store = _GenerationalStore(session)
    service = _service(store)
    _patch_pipeline(monkeypatch, documents={DOC_A: document})

    _rebuild(service, session, document)
    _rebuild(service, session, document)

    assert sorted(store.by_generation) == [1, 2], "两次索引必须落在两代上（上一代留着给回滚）"

    first = _by_section(store.by_generation[1])
    second = _by_section(store.by_generation[2])

    assert set(first) == set(second), "两次索引的节集合必须一样，否则「逐节对比」无从谈起"
    for key in first:
        assert second[key]["meta"]["section_id"] == first[key]["meta"]["section_id"], (
            f"{key} 的 section_id 在重建后变了：{first[key]['meta']['section_id']} → "
            f"{second[key]['meta']['section_id']}"
        )
        assert second[key]["meta"]["parent_section_id"] == first[key]["meta"]["parent_section_id"]
        assert second[key]["content"] == first[key]["content"], "正文必须逐字相同（切法没变）"
        assert second[key]["retrieval_text"] == first[key]["retrieval_text"]


def test_parent_child_relation_is_the_same_graph_in_both_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """父子关系由标题路径决定，两代产物是同一张图（只换了 id 的算法）。

    改造前的算法是 ``sha256(标题路径)``，改造后是 ``sha256(文档 id : 标题路径)``。两种
    算法下「子节父 id == 父节自己的 id」都成立——这就是「语义一致」：换的是编码，不是关系。
    """

    document = _document(DOC_A)
    session = _session(kb=_kb(), documents=[document])
    store = _GenerationalStore(session)
    service = _service(store)
    _patch_pipeline(monkeypatch, documents={DOC_A: document})

    _rebuild(service, session, document)
    first = _by_section(store.by_generation[1])
    _rebuild(service, session, document)
    second = _by_section(store.by_generation[2])

    def _pairs(sections: dict[tuple[str, str], dict[str, Any]]) -> set[tuple[str, str | None]]:
        return {(path, parent_heading_path(path)) for (_doc, path) in sections}

    assert _pairs(first) == _pairs(second)
    assert ("售后手册 > 退款", "售后手册") in _pairs(second)
    assert ("售后手册 > 退款 > 流程", "售后手册 > 退款") in _pairs(second)

    for (_doc, path), chunk in second.items():
        parent_path = parent_heading_path(path)
        if parent_path is None:
            assert chunk["meta"]["parent_section_id"] is None
            continue
        assert chunk["meta"]["parent_section_id"] == section_id_of(
            document_id=DOC_A, heading_path=parent_path
        )


def test_collapse_grouping_is_unchanged_between_two_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """折叠的归组键（单元）在两代之间完全相同——重建不碰折叠口径。"""

    document = _document(DOC_A)
    session = _session(kb=_kb(), documents=[document])
    store = _GenerationalStore(session)
    service = _service(store)
    _patch_pipeline(monkeypatch, documents={DOC_A: document})

    _rebuild(service, session, document)
    first = list(store.by_generation[1])
    _rebuild(service, session, document)
    second = list(store.by_generation[2])

    def _groups(chunks: list[dict[str, Any]]) -> list[tuple[str, ...]]:
        hits = [
            Hit(
                chunk_id=str(chunk["id"]),
                document_id=str(chunk["document_id"]),
                score=1.0,
                content=str(chunk["content"]),
                chunk_type=str(chunk["meta"]["chunk_type"]),
                section_id=str(chunk["meta"]["section_id"]),
                parent_section_id=str(chunk["meta"]["parent_section_id"] or ""),
                table_part=int(chunk["meta"]["table_part"] or 0),
                heading_path=str(chunk["meta"]["heading_path"]),
            )
            for chunk in chunks
        ]
        return sorted(unit_key(hit) for hit in hits)

    assert _groups(first) == _groups(second)


# ─────────────────────────────────────────── 跨文档不撞：同一批重建


def test_two_documents_indexed_together_do_not_share_section_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两份文档都有「售后手册 > 退款 > 流程」，索引后是两节，不是一个。"""

    first_document = _document(DOC_A)
    second_document = _document(DOC_B)
    session = _session(kb=_kb(), documents=[first_document, second_document])
    store = _GenerationalStore(session)
    service = _service(store)
    _patch_pipeline(monkeypatch, documents={DOC_A: first_document, DOC_B: second_document})

    _rebuild(service, session, first_document)
    _rebuild(service, session, second_document)

    sections = _by_section(store.by_generation[1])
    assert {(DOC_A, "售后手册 > 退款 > 流程"), (DOC_B, "售后手册 > 退款 > 流程")} <= set(sections)

    a_id = sections[(DOC_A, "售后手册 > 退款 > 流程")]["meta"]["section_id"]
    b_id = sections[(DOC_B, "售后手册 > 退款 > 流程")]["meta"]["section_id"]
    assert a_id != b_id, "两份文档的同名章节撞了同一个 section_id"
    assert a_id == section_id_of(document_id=DOC_A, heading_path="售后手册 > 退款 > 流程")
    assert b_id == section_id_of(document_id=DOC_B, heading_path="售后手册 > 退款 > 流程")


def test_written_section_ids_are_not_the_old_path_only_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改造真的生效了：落库的 id 不再是「只哈希标题路径」的那个值。"""

    import hashlib

    document = _document(DOC_A)
    session = _session(kb=_kb(), documents=[document])
    store = _GenerationalStore(session)
    service = _service(store)
    _patch_pipeline(monkeypatch, documents={DOC_A: document})

    _rebuild(service, session, document)

    for chunk in store.by_generation[1]:
        path = str(chunk["meta"]["heading_path"])
        old_style = hashlib.sha256(path.encode("utf-8")).hexdigest()
        assert chunk["meta"]["section_id"] != old_style
        assert chunk["meta"]["section_id"] == section_id_of(document_id=DOC_A, heading_path=path)

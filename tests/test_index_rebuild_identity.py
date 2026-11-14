"""#143 · 全量重建走 #142 的构建流水线，v1 与 v2 **逐节同一身份**。

这张票要求的两件事在这里落地：

1. **全量重建不许另写一套**——用 ``IndexBuildService`` 的 ``start_build → build →
   activate``，也就是线上那条流水线；本文件的 v2 就是这么来的。
2. **改造前后语义一致**：重建后同一节的 ``section_id`` 与改造前**语义**一致——父子关系
   与折叠分组都不变，只有 id 的算法换了（带上文档 id）。这正是「可逐节对比新旧构建」
   的前提（ADR-0025 决策五）。

接缝是构建流水线本身 + 真实的 ``IndexingService`` / 真实切块器：只把 embedding、向量
存储、关键词 Provider 与「库配置 / 文档行」的读取换成替身，不连 PostgreSQL、不连 ES。

**为什么父子关系用「路径对」来断言**：父子关系由``标题路径``决定（上一级路径），与 id
用哪种算法无关。所以「两版之间 (子路径, 父路径) 的集合相同」才是「父子关系不变」的准确
说法；直接比 id 值只会在两种算法下都成立（因为两边各自自洽），验不出东西。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.index_version import (
    INDEX_BUILD_ACTIVE,
    INDEX_BUILD_ARCHIVED,
    IndexBuildVersion,
)
from app.models.knowledge_base import SPLIT_STRUCTURE, KnowledgeBase
from app.providers.keyword_search.noop import NoopKeywordSearchProvider
from app.repositories import document_repository, knowledge_base_repository
from app.services.index_build_service import IndexBuildService
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


class _VersionedStore:
    """向量存储替身 + 观测点：按版本收住写入的切片，并按版本回答条数。

    它同时冒充「切片计数」的来源：每次写入都把对应行 ``add`` 进会话，于是
    ``chunk_repository.count_by_index_version``（真 SQL 形状）与自己的向量数一致，
    构建才能走到 ``ready``——否则四项计数不等，构建会被拒（那是 #142 的闸门，不是本
    票要验的东西）。
    """

    def __init__(self, session: AsyncRecordingSession) -> None:
        self._session = session
        self.by_version: dict[str, list[dict[str, Any]]] = {}
        self.purged: list[tuple[str, str | None]] = []

    async def delete_by_document_id(
        self,
        _db: Any,
        document_id: str,
        *,
        tenant_id: str,
        index_version: str | None = None,
    ) -> None:
        self.purged.append((document_id, index_version))

    async def add_chunks(self, _db: Any, chunks: list[dict[str, Any]]) -> None:
        for chunk in chunks:
            self.by_version.setdefault(str(chunk["index_version"]), []).append(chunk)
            self._session.add(
                Chunk(
                    id=chunk["id"],
                    tenant_id=chunk["tenant_id"],
                    kb_id=chunk["kb_id"],
                    document_id=chunk["document_id"],
                    content=chunk["content"],
                    embedding=[0.0] * 1536,
                    meta=chunk["meta"],
                    index_version=chunk["index_version"],
                )
            )

    async def count_by_scope(
        self, _db: Any, *, tenant_id: str, kb_id: str, index_version: str
    ) -> int:
        return sum(
            1
            for chunk in self.by_version.get(index_version, [])
            if chunk["tenant_id"] == tenant_id and chunk["kb_id"] == kb_id
        )


# ──────────────────────────────────────────────────────────────── 夹具


def _kb() -> KnowledgeBase:
    """还没激活过任何版本的库（第一次构建时库上那一列是 NULL）。"""

    return KnowledgeBase(
        id=KB_ID,
        tenant_id=TENANT_ID,
        name="售后",
        description="",
        settings={},
        active_index_version=None,
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
    )


def _session(*, kb: KnowledgeBase, documents: list[Document]) -> AsyncRecordingSession:
    return AsyncRecordingSession(
        rows={KnowledgeBase: [kb], Document: list(documents), IndexBuildVersion: [], Chunk: []}
    )


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch, *, documents: dict[str, Document]) -> None:
    """把「读文档行 / 抢租约 / 读库配置」换成替身，其余全部走真实现。"""

    async def _get_by_id(_db: Any, document_id: str) -> Any:
        return documents.get(document_id)

    async def _claim(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _split_config(*_args: Any, **_kwargs: Any) -> tuple[str, int]:
        return SPLIT_STRUCTURE, 800

    async def _effective_version(*_args: Any, **_kwargs: Any) -> str:
        return "v1"

    monkeypatch.setattr(document_repository, "get_by_id", _get_by_id)
    monkeypatch.setattr(document_repository, "try_claim_for_indexing_async", _claim)
    monkeypatch.setattr(knowledge_base_repository, "get_split_config", _split_config)
    monkeypatch.setattr(
        knowledge_base_repository, "get_effective_index_version", _effective_version
    )
    # 节说明会调模型：本票与它无关，钉死成关闭（检索文本因此逐字可比）。
    monkeypatch.setattr(settings, "SECTION_CONTEXT_ENABLED", False)


def _service(store: _VersionedStore) -> IndexBuildService:
    return IndexBuildService(
        indexing=IndexingService(embeddings=_StubEmbeddings(), stores=[store]),
        vector_store=store,
        keyword_search=NoopKeywordSearchProvider(),
    )


def _build_and_activate(
    service: IndexBuildService,
    session: AsyncRecordingSession,
    *,
    version: str | None = None,
) -> IndexBuildVersion:
    """跑一次完整构建：起版本行 → 逐文档重建 → 四项计数 → 单事务激活。"""

    row = asyncio.run(
        service.start_build(session, kb_id=KB_ID, tenant_id=TENANT_ID, version=version)
    )
    asyncio.run(service.build(session, row.id, tenant_id=TENANT_ID))
    asyncio.run(service.activate(session, row.id, tenant_id=TENANT_ID))
    return row


def _by_section(chunks: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """``(文档 id, 标题路径) → 切片字典``（同一节的片取第一片即可：身份是节级的）。"""

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for chunk in chunks:
        grouped.setdefault((str(chunk["document_id"]), str(chunk["meta"]["heading_path"])), chunk)
    return grouped


# ─────────────────────────────────────────── 全量重建：v2 来自构建流水线本身


def test_a_full_rebuild_through_the_build_pipeline_keeps_every_section_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同库同文档重建一次（v1 → v2），每一节的 id 与父 id 都不变。"""

    document = _document(DOC_A)
    session = _session(kb=_kb(), documents=[document])
    store = _VersionedStore(session)
    service = _service(store)
    _patch_pipeline(monkeypatch, documents={DOC_A: document})

    first = _build_and_activate(service, session)
    second = _build_and_activate(service, session, version="v2")

    assert (first.version, second.version) == ("v1", "v2")
    assert first.status == INDEX_BUILD_ARCHIVED
    assert second.status == INDEX_BUILD_ACTIVE

    v1 = _by_section(store.by_version["v1"])
    v2 = _by_section(store.by_version["v2"])

    assert set(v1) == set(v2), "两次构建的节集合必须一样，否则「逐节对比」无从谈起"
    for key in v1:
        assert v2[key]["meta"]["section_id"] == v1[key]["meta"]["section_id"], (
            f"{key} 的 section_id 在重建后变了：{v1[key]['meta']['section_id']} → "
            f"{v2[key]['meta']['section_id']}"
        )
        assert v2[key]["meta"]["parent_section_id"] == v1[key]["meta"]["parent_section_id"]
        assert v2[key]["content"] == v1[key]["content"], "正文必须逐字相同（切法没变）"
        assert v2[key]["retrieval_text"] == v1[key]["retrieval_text"]


def test_parent_child_relation_is_the_same_graph_in_both_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """父子关系由标题路径决定，改造前后是同一张图（只换了 id 的算法）。

    改造前的算法是 ``sha256(标题路径)``，改造后是 ``sha256(文档 id : 标题路径)``。两种
    算法下「子节父 id == 父节自己的 id」都成立——这就是「语义一致」：换的是编码，不是关系。
    """

    document = _document(DOC_A)
    session = _session(kb=_kb(), documents=[document])
    store = _VersionedStore(session)
    service = _service(store)
    _patch_pipeline(monkeypatch, documents={DOC_A: document})

    _build_and_activate(service, session)
    v1 = _by_section(store.by_version["v1"])
    _build_and_activate(service, session, version="v2")
    v2 = _by_section(store.by_version["v2"])

    def _pairs(sections: dict[tuple[str, str], dict[str, Any]]) -> set[tuple[str, str | None]]:
        return {(path, parent_heading_path(path)) for (_doc, path) in sections}

    assert _pairs(v1) == _pairs(v2)
    assert ("售后手册 > 退款", "售后手册") in _pairs(v2)
    assert ("售后手册 > 退款 > 流程", "售后手册 > 退款") in _pairs(v2)

    for (_doc, path), chunk in v2.items():
        parent_path = parent_heading_path(path)
        if parent_path is None:
            assert chunk["meta"]["parent_section_id"] is None
            continue
        assert chunk["meta"]["parent_section_id"] == section_id_of(
            document_id=DOC_A, heading_path=parent_path
        )


def test_collapse_grouping_is_unchanged_between_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """折叠的归组键（单元）在两版之间完全相同——本次改造不碰折叠口径。"""

    document = _document(DOC_A)
    session = _session(kb=_kb(), documents=[document])
    store = _VersionedStore(session)
    service = _service(store)
    _patch_pipeline(monkeypatch, documents={DOC_A: document})

    _build_and_activate(service, session)
    v1 = list(store.by_version["v1"])
    _build_and_activate(service, session, version="v2")
    v2 = list(store.by_version["v2"])

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

    assert _groups(v1) == _groups(v2)


# ─────────────────────────────────────────── 跨文档不撞：同一库、同一批构建


def test_two_documents_rebuilt_in_the_same_build_do_not_share_section_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两份文档都有「售后手册 > 退款 > 流程」，构建后是两节，不是一个。"""

    first_document = _document(DOC_A)
    second_document = _document(DOC_B)
    session = _session(kb=_kb(), documents=[first_document, second_document])
    store = _VersionedStore(session)
    service = _service(store)
    _patch_pipeline(monkeypatch, documents={DOC_A: first_document, DOC_B: second_document})

    _build_and_activate(service, session)

    sections = _by_section(store.by_version["v1"])
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
    store = _VersionedStore(session)
    service = _service(store)
    _patch_pipeline(monkeypatch, documents={DOC_A: document})

    _build_and_activate(service, session)

    for chunk in store.by_version["v1"]:
        path = str(chunk["meta"]["heading_path"])
        old_style = hashlib.sha256(path.encode("utf-8")).hexdigest()
        assert chunk["meta"]["section_id"] != old_style
        assert chunk["meta"]["section_id"] == section_id_of(document_id=DOC_A, heading_path=path)


def test_the_verification_check_agrees_with_what_the_build_actually_wrote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """核对脚本的两端对得上：真实构建产物**通过**核对，掺一条旧 id 就**不通过**。

    这条用例把两个模块钉在一起：切块器写 ``section_id``（M1）、构建流水线把它落库（M2）、
    核对逻辑按同一份算法验它（M3）。三处任何一处漂移，这里都会红——而单看任何一处都绿。
    """

    import hashlib

    from app.services.index_verification import IdentityRow, verify_identity

    document = _document(DOC_A)
    session = _session(kb=_kb(), documents=[document])
    store = _VersionedStore(session)
    service = _service(store)
    _patch_pipeline(monkeypatch, documents={DOC_A: document})

    _build_and_activate(service, session)

    rows: dict[tuple[str, str], IdentityRow] = {}
    for chunk in store.by_version["v1"]:
        meta = chunk["meta"]
        key = (str(chunk["document_id"]), str(meta["heading_path"]))
        rows[key] = IdentityRow(
            document_id=key[0],
            heading_path=key[1],
            section_id=str(meta["section_id"]),
            parent_section_id=(
                str(meta["parent_section_id"]) if meta["parent_section_id"] else None
            ),
        )

    report = verify_identity(rows.values())
    assert report.ok, report.render_problems()
    assert report.sections == len(rows)
    assert report.parent_without_body >= 1, "「售后手册」只有标题，父节无正文这一档必须出现"

    poisoned = dict(rows)
    first_key = next(iter(poisoned))
    poisoned[first_key] = IdentityRow(
        document_id=first_key[0],
        heading_path=first_key[1],
        section_id=hashlib.sha256(first_key[1].encode("utf-8")).hexdigest(),
        parent_section_id=poisoned[first_key].parent_section_id,
    )

    assert not verify_identity(poisoned.values()).ok


# ─────────────────────────────── 发起入口：全量重建必须能被执行，且走的是同一条流水线

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_script(module_name: str) -> ModuleType:
    """从文件加载运维脚本：``scripts*`` 不在安装包里，显式路径最可靠。

    加载本身也要成功——脚本顶层不得连数据库、不得解析 argv（导入即安全）。
    """

    path = SCRIPTS_DIR / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"t143_script_{module_name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


class _RecordingPublisher:
    """记下 ``apply_async`` 的实参；不起 broker、不连 Redis。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def apply_async(self, *, kwargs: dict[str, Any], task_id: str) -> None:
        self.calls.append({"kwargs": dict(kwargs), "task_id": task_id})


def test_the_rebuild_script_starts_a_building_row_and_dispatches_the_build_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认跑法：起一行 ``building`` + 投递构建任务，**不动线上**的生效版本。"""

    from app.services import index_dispatch

    module = _load_script("rebuild_kb_version")
    recorder = _RecordingPublisher()
    index_dispatch.set_build_task_publisher(recorder)

    kb = _kb()
    session = AsyncRecordingSession(rows={KnowledgeBase: [kb], IndexBuildVersion: []})

    row = asyncio.run(module.start_rebuild(session, kb_id=KB_ID, tenant_id=TENANT_ID))

    assert row.status == "building"
    assert row.version == "v1", "没有既有版本时就是 v1（版本名由版本行算出）"
    assert kb.active_index_version is None, "构建期间库上的生效版本一个字节都不改"
    assert recorder.calls == [
        {
            "kwargs": {
                "build_id": row.id,
                "tenant_id": TENANT_ID,
                "request_id": f"ops-rebuild:{row.id}",
            },
            "task_id": f"build:{row.id}",
        }
    ]


def test_the_rebuild_script_refuses_to_activate_a_dispatched_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--activate`` 必须与 ``--in-process`` 同用：投递出去就不知道何时跑完。"""

    module = _load_script("rebuild_kb_version")

    with pytest.raises(SystemExit) as excinfo:
        module.main(["--kb-id", KB_ID, "--activate"])

    assert excinfo.value.code == 2


def test_the_in_process_rebuild_builds_and_optionally_activates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """就地跑法：``build`` 一定被调；``activate`` 只在明确要求时被调。"""

    module = _load_script("rebuild_kb_version")
    calls: list[tuple[str, str]] = []

    class _RecordingService:
        async def start_build(
            self, _db: Any, *, kb_id: str, tenant_id: str, version: str | None = None
        ) -> Any:
            calls.append(("start_build", version or "v1"))
            return SimpleNamespace(
                id="build-1", version=version or "v1", document_count=None, chunk_count=None
            )

        async def build(self, _db: Any, build_id: str, *, tenant_id: str) -> None:
            calls.append(("build", build_id))

        async def activate(self, _db: Any, build_id: str, *, tenant_id: str) -> None:
            calls.append(("activate", build_id))

    monkeypatch.setattr(module, "IndexBuildService", _RecordingService)
    session = AsyncRecordingSession(rows={KnowledgeBase: [_kb()], IndexBuildVersion: []})

    asyncio.run(
        module.run_rebuild(session, kb_id=KB_ID, tenant_id=TENANT_ID, version="v7", activate=False)
    )
    assert calls == [("start_build", "v7"), ("build", "build-1")]

    calls.clear()
    asyncio.run(
        module.run_rebuild(session, kb_id=KB_ID, tenant_id=TENANT_ID, version="v7", activate=True)
    )
    assert calls == [
        ("start_build", "v7"),
        ("build", "build-1"),
        ("activate", "build-1"),
    ]


def test_the_rebuild_script_reports_a_missing_knowledge_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库不存在 → 退出码 1（而不是 IndexError 或一条假成功）。"""

    module = _load_script("rebuild_kb_version")
    session = AsyncRecordingSession(rows={KnowledgeBase: [], IndexBuildVersion: []})

    with pytest.raises(LookupError):
        asyncio.run(module.resolve_tenant_id(session, kb_id="kb-missing", tenant_id=None))


# ────────────────────────────────── 回滚入口：运维不必再写一段 Python（#156）

#: 回滚目标版本名（坑里那一版）。
ROLLBACK_TARGET = "v1"
#: 此刻线上生效的那一版。
LIVE = "v2"


class _RecordingKeywordProvider(NoopKeywordSearchProvider):
    """回滚入口用：回答「这一版的物理索引还在不在」，并记录别名切到了哪一版。"""

    def __init__(self, *, indexes: set[str] | None = None, available: bool = True) -> None:
        self.indexes = set(indexes or ())
        self._available = available
        self.activate_calls: list[str] = []

    @property
    def available(self) -> bool:
        return self._available

    async def physical_index_exists(self, version: str) -> bool:
        return version in self.indexes

    async def activate_version(self, version: str) -> None:
        self.activate_calls.append(version)


def _live_kb(*, version: str = LIVE) -> KnowledgeBase:
    kb = _kb()
    kb.active_index_version = version
    return kb


def _version_row(version: str, status: str) -> IndexBuildVersion:
    return IndexBuildVersion(
        id=f"build-{version}",
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        version=version,
        status=status,
        created_at=datetime(2026, 11, 1, 9, 0, 0),
    )


def _version_chunks(version: str, count: int) -> list[Chunk]:
    """该版本的切片行：回滚守卫要数它们（0 片 = 数据已经没了）。"""

    return [
        Chunk(
            id=f"{version}-c{index}",
            tenant_id=TENANT_ID,
            kb_id=KB_ID,
            document_id=DOC_A,
            content="正文",
            embedding=[0.0] * 4,
            meta={},
            index_version=version,
        )
        for index in range(count)
    ]


def _patch_session(monkeypatch: pytest.MonkeyPatch, module: ModuleType, session: Any) -> None:
    """把脚本自己的会话入口换成替身：``--rollback`` 的整条路径因此不连库。"""

    async def _open() -> Any:
        return session

    monkeypatch.setattr(module, "_open_session", _open)


def _rollback_session(*, kb: KnowledgeBase, chunks: list[Chunk]) -> AsyncRecordingSession:
    return AsyncRecordingSession(
        rows={
            KnowledgeBase: [kb],
            IndexBuildVersion: [
                _version_row(LIVE, INDEX_BUILD_ACTIVE),
                _version_row(ROLLBACK_TARGET, INDEX_BUILD_ARCHIVED),
            ],
            Chunk: list(chunks),
        }
    )


def test_the_rollback_flag_takes_a_version_name_and_switches_the_kb_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--rollback v1``：按**版本名**（不是 build_id）切回，退出码 0。

    运维手里有的是「上一次构建叫 v1」，不是那一行 UUID；要求他先查 build_id 再回切，
    等于把一次应急动作变成两段手工 SQL。
    """

    module = _load_script("rebuild_kb_version")
    keyword = _RecordingKeywordProvider(indexes={ROLLBACK_TARGET})
    service = IndexBuildService(
        indexing=SimpleNamespace(),
        vector_store=SimpleNamespace(),
        keyword_search=keyword,
    )
    monkeypatch.setattr(module, "IndexBuildService", lambda: service)
    kb = _live_kb()
    _patch_session(
        monkeypatch,
        module,
        _rollback_session(kb=kb, chunks=_version_chunks(ROLLBACK_TARGET, 2)),
    )

    assert module.main(["--kb-id", KB_ID, "--rollback", ROLLBACK_TARGET]) == 0

    assert kb.active_index_version == ROLLBACK_TARGET, "库列切回 v1（检索 pin 跟着走）"
    assert keyword.activate_calls == [ROLLBACK_TARGET], "关键词别名切到 v1 的物理索引"


def test_a_refused_rollback_exits_non_zero_and_moves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目标版本的切片一片都没有 → 退出码 1，库列与别名都不动。

    「失败了但退 0」比失败本身更糟：运维会以为已经切回去了，而线上查的还是那一版。
    """

    module = _load_script("rebuild_kb_version")
    keyword = _RecordingKeywordProvider(indexes={ROLLBACK_TARGET, LIVE})
    service = IndexBuildService(
        indexing=SimpleNamespace(),
        vector_store=SimpleNamespace(),
        keyword_search=keyword,
    )
    monkeypatch.setattr(module, "IndexBuildService", lambda: service)
    kb = _live_kb()
    _patch_session(monkeypatch, module, _rollback_session(kb=kb, chunks=[]))

    assert module.main(["--kb-id", KB_ID, "--rollback", ROLLBACK_TARGET]) == 1

    assert kb.active_index_version == LIVE, "被拒的回滚不动库列"
    assert keyword.activate_calls == [], "被拒的回滚不动别名"


def test_rolling_back_to_an_unknown_version_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """版本名对不上任何一行 → 退出码 1（而不是静默什么都没做）。"""

    module = _load_script("rebuild_kb_version")
    keyword = _RecordingKeywordProvider(indexes={"v9"})
    service = IndexBuildService(
        indexing=SimpleNamespace(),
        vector_store=SimpleNamespace(),
        keyword_search=keyword,
    )
    monkeypatch.setattr(module, "IndexBuildService", lambda: service)
    kb = _live_kb()
    _patch_session(
        monkeypatch,
        module,
        _rollback_session(kb=kb, chunks=_version_chunks(ROLLBACK_TARGET, 1)),
    )

    assert module.main(["--kb-id", KB_ID, "--rollback", "v9"]) == 1

    assert kb.active_index_version == LIVE
    assert keyword.activate_calls == []


@pytest.mark.parametrize("extra", [["--in-process"], ["--activate"], ["--version", "v3"]])
def test_the_rollback_flag_cannot_be_combined_with_a_build(
    monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    """回滚与构建是两个动作：混在一起时**拒绝解析**（退出码 2），不猜用户想干什么。"""

    module = _load_script("rebuild_kb_version")

    with pytest.raises(SystemExit) as excinfo:
        module.main(["--kb-id", KB_ID, "--rollback", ROLLBACK_TARGET, *extra])

    assert excinfo.value.code == 2

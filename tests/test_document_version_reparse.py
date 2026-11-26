"""#182 · 重解析之后，「文档正文 == 当前稿正文」这条对账口径必须成立。

## 为什么这一条要单独一个文件、还要连真库

重解析此前只改写**文档行**，那一稿的版本行留着上一份解析产物——正文真值在两张表上
分叉。离线替身会话「写完就在那儿」，两边一起看见，看不出分叉；而 T10 的对账脚本读的
是**提交之后**的两行。所以这里用 ``sqlite+aiosqlite`` 跑真实的
``IndexingService.index_existing_document(reparse=True)``，再从**另一条连接**把两行读回来
比对（先例：``tests/test_reindex_e2e.py`` 的「另开连接读 Worker 提交过的状态」）。

解析走的是真的解析注册表、真的源文件（Markdown 解析器原样回传字节）：重解析要证明的
正是「读的是这一稿自己的源文件、写回的是这一稿的版本行」，把解析换成替身就等于把问题
假设掉了。切块器 / embedding / 存储仍是替身——它们与这条口径无关，且离线套件不碰网络。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.document import Document
from app.models.document_version import DocumentVersion
from app.models.plan import Plan
from app.models.tenant import Tenant
from app.repositories import knowledge_base_repository
from app.services.indexing_service import IndexingService
from app.utils.checksum import checksum_of
from app.utils.status import DocumentStatus
from app.utils.text_splitter import SplitPiece

TENANT_ID = "tenant_demo"
KB_ID = "kb-1"
DOC_ID = "doc-reparse-alignment"

#: 这一稿的源文件（重解析读的就是它）。
REPARSED_TEXT = "# 退款政策\n\n签收后 15 天内可申请无理由退款。"
#: 上一份解析产物：版本行里现在存着的正文，重解析之后必须被换掉。
STALE_TEXT = "# 退款政策\n\n签收后 7 天内可申请无理由退款。"
#: 第 1 稿（归档稿）的正文：重解析第 2 稿一个字节都不许碰它。
FIRST_DRAFT_TEXT = "# 退款政策（初稿）\n\n签收后 3 天内。"


class _StubSplitter:
    """切块结果固定：这条用例只看正文去了哪儿，不看切成几片。"""

    def split(self, _text: str) -> list[SplitPiece]:
        return [SplitPiece(text="piece", metadata={"heading_path": "退款政策"})]


class _StubEmbeddings:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * 3 for _ in texts]

    async def embed_query(self, _query: str) -> list[float]:
        raise AssertionError("索引链路不应调用单条查询向量化")


class _RecordingStore:
    """空存储：只让流水线有地方写切片，不模拟任何检索行为。"""

    async def delete_by_document_id(
        self,
        _db: Any,
        _document_id: str,
        *,
        tenant_id: str,
        index_version: str | None = None,
        doc_version: int | None = None,
    ) -> None:
        return None

    async def add_chunks(self, _db: Any, _chunks: list[dict[str, Any]]) -> None:
        return None

    async def similarity_search(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("索引链路不应调用检索")


def _version(
    *, version_id: str, version: int, content: str, status: str, source_file_path: str | None
) -> DocumentVersion:
    return DocumentVersion(
        id=version_id,
        document_id=DOC_ID,
        version=version,
        content=content,
        checksum=checksum_of(content),
        status=status,
        source_file_path=source_file_path,
    )


@pytest.mark.asyncio
async def test_reconciliation_holds_after_reparse_document_content_equals_current_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重解析当前稿之后：``documents.content`` 与「当前稿版本行正文」逐字相同。

    这是 T10 对账脚本的第一条硬检查。它在这里成立，靠的是三件事同时为真：
    重解析的产物**写回同一稿的版本行**（正文 + 校验和一起换）、文档行跟着当前稿同步、
    以及**不新增版本行**——少任何一条，两行就会在提交之后各说各话。
    """

    source_file = tmp_path / "doc.v2.md"
    source_file.write_text(REPARSED_TEXT, encoding="utf-8", newline="\n")

    database_path = tmp_path / "docver-reparse.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Document.__table__.create)
        await connection.run_sync(DocumentVersion.__table__.create)
        # 租户与套餐（#220）：索引流水线要读租户套餐来判单库 token 容量。
        # 少了这两张，这条用例会在「no such table: tenants」上炸。
        await connection.run_sync(Tenant.__table__.create)
        await connection.run_sync(Plan.__table__.create)

    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with sessions() as seed:
        seed.add(
            Document(
                id=DOC_ID,
                tenant_id=TENANT_ID,
                kb_id=KB_ID,
                title="退款政策",
                # 文档行里躺的是**上一份解析产物**（旧解析器写的），重解析必须无视它。
                content=STALE_TEXT,
                source_type="markdown",
                source_filename="policy.md",
                source_file_path=str(source_file),
                status=DocumentStatus.PROCESSING,
                current_version=2,
            )
        )
        seed.add(
            _version(
                version_id="v1",
                version=1,
                content=FIRST_DRAFT_TEXT,
                status="archived",
                source_file_path=None,
            )
        )
        seed.add(
            _version(
                version_id="v2",
                version=2,
                content=STALE_TEXT,
                status="current",
                source_file_path=str(source_file),
            )
        )
        await seed.commit()

    # 写入目标版本取自库上的生效版本（#142）：这条临时库里没有 knowledge_bases 表，
    # 于是把「问库要版本」换成固定的 v1——与其余索引用例同一做法。
    async def _effective_version(_db: Any, _kb_id: str, *, tenant_id: str) -> str:
        return "v1"

    monkeypatch.setattr(
        knowledge_base_repository, "get_effective_index_version", _effective_version
    )

    service = IndexingService(
        splitter=_StubSplitter(),
        embeddings=_StubEmbeddings(),
        stores=[_RecordingStore()],
    )
    async with sessions() as worker:
        await service.index_existing_document(worker, DOC_ID, reparse=True)
    await engine.dispose()

    # 另一条连接读回**已提交**的库：对账脚本看到的就是这两行。
    verify_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    try:
        async with AsyncSession(verify_engine) as verify:
            doc = await verify.get(Document, DOC_ID)
            assert doc is not None
            versions = list(
                (
                    await verify.execute(
                        select(DocumentVersion)
                        .where(DocumentVersion.document_id == DOC_ID)
                        .order_by(DocumentVersion.version)
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await verify_engine.dispose()

    assert [(row.version, row.status) for row in versions] == [
        (1, "archived"),
        (2, "current"),
    ], "重解析不新增版本行，也不改归档状态"

    current = versions[1]
    assert current.content == REPARSED_TEXT, "这一稿的版本行正文就是本次解析结果"
    assert current.checksum == checksum_of(REPARSED_TEXT), "校验和跟着正文一起换"
    assert doc.current_version == 2, "稿号与指针不变"
    assert doc.content == current.content, "对账口径：文档正文 == 当前稿正文（逐字）"
    assert doc.content == REPARSED_TEXT
    assert versions[0].content == FIRST_DRAFT_TEXT, "别的稿一个字节都不动"
    assert doc.status == DocumentStatus.SUCCESS

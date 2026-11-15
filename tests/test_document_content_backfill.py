"""#170 · 正文补齐脚本：把「正文从来没落过库」的历史文档补上。

两个接缝，都是既有的：

* **仓储层**（内存 SQLite，只建 ``documents`` 表）——候选查询只挑「正文为空」的行，
  可按库收窄、可按条数收敛。补齐脚本的输入就是它，所以「挑谁」这件事必须钉在这里，
  而不是靠脚本里的 if。
* **运维脚本的公开函数与 CLI 入口**（假会话 + monkeypatched 仓储）——默认 dry-run
  不写库、``--apply`` 才写、读不到源文件的跳过并列出来、退出码规则。

脚本通过文件路径加载：``scripts*`` 不在安装包里（见 pyproject 的 ``packages.find``），
测试显式加载文件，顺带验证脚本「导入即安全」（模块顶层不连库、不解析 argv）。

**源文件用真文件**（``tmp_path`` + 绝对路径）：解析那一段不打桩——补的就是「读源文件
解析出正文」这件事，把解析器换成替身等于把被测行为换掉了。
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.base import now
from app.models.document import Document
from app.repositories import document_repository
from tests.fake_db import RecordingSession

TENANT_ID = settings.DEV_TENANT_ID
KB_ID = "kb-1"
OTHER_KB_ID = "kb-2"

_SOURCE_TEXT = "落盘文件里的正文：签收后 7 天内可申请无理由退款。\n"

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


# ─────────────────────────────────────────────────────── 仓储层（内存 SQLite）


@pytest.fixture
def document_db() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Document.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(
    db: Session,
    document_id: str,
    *,
    content: str = "",
    kb_id: str = KB_ID,
    tenant_id: str = TENANT_ID,
    source_file_path: str | None = None,
) -> None:
    db.add(
        Document(
            id=document_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            title=document_id,
            content=content,
            source_type="text",
            source_file_path=source_file_path,
            source_filename="notes.txt",
            created_at=now(),
        )
    )
    db.commit()


def test_list_missing_content_returns_only_blank_documents(document_db: Session) -> None:
    """候选就是「正文为空」的那些行：已经有正文的一个都不该被扫出来。"""

    _seed(document_db, "doc-blank", source_file_path="tenant_demo/doc-blank.txt")
    _seed(document_db, "doc-filled", content=_SOURCE_TEXT)
    _seed(document_db, "doc-blank-other-kb", kb_id=OTHER_KB_ID)

    found = {doc.id for doc in document_repository.list_missing_content(document_db)}

    assert found == {"doc-blank", "doc-blank-other-kb"}


def test_list_missing_content_can_be_narrowed_by_kb_and_capped(document_db: Session) -> None:
    """``--kb-id`` 收窄范围、``--limit`` 收敛条数：运维先看一小批再决定。"""

    _seed(document_db, "doc-a", kb_id=KB_ID)
    _seed(document_db, "doc-b", kb_id=KB_ID)
    _seed(document_db, "doc-c", kb_id=OTHER_KB_ID)

    only_kb = document_repository.list_missing_content(document_db, kb_id=KB_ID)
    capped = document_repository.list_missing_content(document_db, kb_id=KB_ID, limit=1)

    assert {doc.id for doc in only_kb} == {"doc-a", "doc-b"}
    assert len(capped) == 1


# ──────────────────────────────────────────────────────────────── 脚本加载


def _load_script(module_name: str) -> ModuleType:
    """从文件加载脚本模块（先登记进 ``sys.modules``：dataclass 要按模块名反查）。"""

    path = _SCRIPTS_DIR / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"t12_170_{module_name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


@pytest.fixture
def backfill_script() -> ModuleType:
    return _load_script("backfill_document_content")


class _FakeSession(RecordingSession):
    """共享会话替身加一条「提交顺序」，用来断言每篇文档各自提交。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def commit(self) -> None:
        self.calls.append("commit")
        super().commit()


def _document(
    document_id: str,
    *,
    source_file_path: str | None,
    content: str = "",
) -> SimpleNamespace:
    """历史行替身：正文为空、带（或不带）落盘路径。"""

    return SimpleNamespace(
        id=document_id,
        tenant_id=TENANT_ID,
        kb_id=KB_ID,
        content=content,
        source_file_path=source_file_path,
        source_filename="notes.txt",
    )


def _source_file(tmp_path: Path, name: str, text: str = _SOURCE_TEXT) -> Path:
    # newline="\n"：文件里的换行必须与期望值一致，别让 Windows 把 \n 翻成 \r\n，
    # 否则断言的是「解析器有没有做换行转换」，而不是「正文有没有写回」。
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _patch_listing(
    monkeypatch: pytest.MonkeyPatch,
    script: ModuleType,
    documents: list[SimpleNamespace],
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _list(_db: Any, *, kb_id: str | None = None, limit: int | None = None) -> list[Any]:
        calls.append({"kb_id": kb_id, "limit": limit})
        return documents

    monkeypatch.setattr(script.document_repository, "list_missing_content", _list)
    return calls


# ────────────────────────────────────────────────────────────── 默认 dry-run


def test_backfill_script_defaults_to_dry_run_and_writes_nothing(
    backfill_script: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """默认只报告：能补的算出条数、不写行、不提交。"""

    source = _source_file(tmp_path, "a.txt")
    documents = [_document("doc-a", source_file_path=str(source))]
    listed = _patch_listing(monkeypatch, backfill_script, documents)

    session = _FakeSession()
    stats = backfill_script.backfill_documents(session)

    assert stats == backfill_script.BackfillStats(
        considered=1, ready=1, backfilled=0, skipped=0, failed=0
    )
    assert session.commits == 0
    assert documents[0].content == "", "dry-run 一个字都不写"
    assert listed == [{"kb_id": None, "limit": None}]


# ──────────────────────────────────────────────────── --apply 才真的写回


def test_backfill_script_apply_writes_the_parsed_text_and_commits_per_document(
    backfill_script: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--apply``：逐篇解析源文件、写回正文、各自提交（一篇失败不影响其余）。"""

    first = _source_file(tmp_path, "a.txt", "第一篇正文。\n")
    second = _source_file(tmp_path, "b.txt", "第二篇正文。\n")
    documents = [
        _document("doc-a", source_file_path=str(first)),
        _document("doc-b", source_file_path=str(second)),
    ]
    _patch_listing(monkeypatch, backfill_script, documents)

    session = _FakeSession()
    stats = backfill_script.backfill_documents(session, apply=True)

    assert stats == backfill_script.BackfillStats(
        considered=2, ready=2, backfilled=2, skipped=0, failed=0
    )
    assert [doc.content for doc in documents] == ["第一篇正文。\n", "第二篇正文。\n"]
    assert session.calls == ["commit", "commit"], "每篇一笔事务，与两个既有脚本同形"


def test_backfill_script_skips_documents_without_a_readable_source_file(
    backfill_script: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """读不到源文件的跳过，并**按名字列出来**（运维要知道是哪几篇补不了）。"""

    readable = _source_file(tmp_path, "readable.txt")
    documents = [
        _document("doc-no-path", source_file_path=None),
        _document("doc-missing-file", source_file_path=str(tmp_path / "gone.txt")),
        _document("doc-ok", source_file_path=str(readable)),
    ]
    _patch_listing(monkeypatch, backfill_script, documents)

    session = _FakeSession()
    stats = backfill_script.backfill_documents(session, apply=True)

    assert stats == backfill_script.BackfillStats(
        considered=3,
        ready=1,
        backfilled=1,
        skipped=2,
        failed=0,
        skipped_documents=(
            ("doc-no-path", "no source file"),
            ("doc-missing-file", "source file is missing"),
        ),
    )
    assert documents[2].content == _SOURCE_TEXT
    assert session.calls == ["commit"], "跳过的那两篇不该有任何写动作"


def test_backfill_script_counts_a_write_failure_as_failed_and_keeps_going(
    backfill_script: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """写库失败记 failed 并回滚；后面那篇仍然照补。"""

    first = _source_file(tmp_path, "a.txt", "第一篇正文。\n")
    second = _source_file(tmp_path, "b.txt", "第二篇正文。\n")
    documents = [
        _document("doc-boom", source_file_path=str(first)),
        _document("doc-ok", source_file_path=str(second)),
    ]
    _patch_listing(monkeypatch, backfill_script, documents)

    session = _FakeSession()
    original_commit = session.commit
    commits: list[int] = []

    def _commit() -> None:
        commits.append(len(commits) + 1)
        if len(commits) == 1:
            raise RuntimeError("deadlock detected")
        original_commit()

    session.commit = _commit  # type: ignore[method-assign]

    stats = backfill_script.backfill_documents(session, apply=True)

    assert stats == backfill_script.BackfillStats(
        considered=2, ready=2, backfilled=1, skipped=0, failed=1
    )
    assert session.rollbacks == 1
    assert documents[1].content == "第二篇正文。\n"


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        (dict(considered=2, ready=2, backfilled=1, skipped=1, failed=1), 0),
        (dict(considered=1, ready=1, backfilled=0, skipped=0, failed=1), 1),
        (dict(considered=2, ready=0, backfilled=0, skipped=2, failed=0), 0),
        (dict(considered=0), 0),
    ],
)
def test_backfill_script_exit_code_rule(
    backfill_script: ModuleType, stats: dict[str, Any], expected: int
) -> None:
    """只有「一篇都没补成且有失败」才退出 1；读不到源文件属跳过，不算失败。"""

    assert backfill_script.exit_code_for(backfill_script.BackfillStats(**stats)) == expected


# ──────────────────────────────────────────────────────────── CLI 入口


def test_backfill_script_main_defaults_to_dry_run_and_lists_the_skipped_documents(
    backfill_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 默认 dry-run：标题写明模式，统计逐行打印，跳过的那几篇连原因一起列出来。"""

    session = _FakeSession()
    monkeypatch.setattr(backfill_script, "SessionLocal", lambda: session)
    monkeypatch.setattr(backfill_script, "setup_logging", lambda: None)
    monkeypatch.setattr(
        backfill_script,
        "backfill_documents",
        lambda _db, **_kwargs: backfill_script.BackfillStats(
            considered=3,
            ready=1,
            backfilled=0,
            skipped=2,
            failed=0,
            skipped_documents=(
                ("doc-no-path", "no source file"),
                ("doc-missing-file", "source file is missing"),
            ),
        ),
    )

    exit_code = backfill_script.main([])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "dry-run" in out
    assert "considered: 3" in out
    assert "ready: 1" in out
    assert "backfilled: 0" in out
    assert "skipped: 2" in out
    assert "doc-no-path" in out and "no source file" in out, "跳过的那几篇必须列出来"
    assert "doc-missing-file" in out and "source file is missing" in out
    assert session.closed


def test_backfill_script_main_passes_apply_and_scope_to_the_function(
    backfill_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--apply`` 才把 apply=True 传下去；``--kb-id`` / ``--limit`` 原样透传。"""

    session = _FakeSession()
    monkeypatch.setattr(backfill_script, "SessionLocal", lambda: session)
    monkeypatch.setattr(backfill_script, "setup_logging", lambda: None)
    captured: dict[str, Any] = {}

    def _run(_db: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return backfill_script.BackfillStats(considered=1, ready=1, backfilled=1)

    monkeypatch.setattr(backfill_script, "backfill_documents", _run)

    exit_code = backfill_script.main(["--apply", "--kb-id", KB_ID, "--limit", "5"])

    out = capsys.readouterr().out
    assert captured == {"kb_id": KB_ID, "limit": 5, "apply": True}
    assert exit_code == 0
    assert "live" in out
    assert "backfilled: 1" in out
    assert session.closed

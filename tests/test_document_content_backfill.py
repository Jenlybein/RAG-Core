"""#170 · 正文补齐脚本：把「正文从来没落过库」的历史文档补上。

三个接缝，都是既有的：

* **仓储层**（内存 SQLite，但**只建升级前的老列表结构**）——候选查询只挑「正文为空」的行，
  可按库收窄、可按条数收敛。夹具刻意不建 ``current_version``（升级前的稿次列还叫
  ``doc_version``）、不建 ``document_versions``：这条脚本的**使命就是升级前先补正文**，
  所以它的查询与写回一旦引用迁移之后才存在的列，在真库上就是 `UndefinedColumn` 崩在
  升级之前（这条 bug 的回归钉见 :func:`test_backfill_runs_before_the_migration`）。
* **运维脚本的公开函数与 CLI 入口**——默认 dry-run 不写库、``--apply`` 才写、读不到源文件
  的跳过并列出来、退出码规则；以及**跑错时机**的守卫：库里已经有 ``document_versions``
  表时拒跑（:func:`test_backfill_script_refuses_to_run_after_the_migration`）。
* **写回是 SQL 语句**，不是改 ORM 实体：用例从库里**真读**回来（:func:`_content_of`），
  而不是读回对象上的属性——后者在「以为写了、其实没发语句」时仍然是绿的。

脚本通过文件路径加载：``scripts*`` 不在安装包里（见 pyproject 的 ``packages.find``），
测试显式加载文件，顺带验证脚本「导入即安全」（模块顶层不连库、不解析 argv）。

**源文件用真文件**（``tmp_path`` + 绝对路径）：解析那一段不打桩——补的就是「读源文件
解析出正文」这件事，把解析器换成替身等于把被测行为换掉了。
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.base import now
from app.models.document_version import DocumentVersion
from app.repositories import document_repository
from app.utils.status import DocumentStatus
from tests.fake_db import RecordingSession

TENANT_ID = settings.DEV_TENANT_ID
KB_ID = "kb-1"
OTHER_KB_ID = "kb-2"

_SOURCE_TEXT = "落盘文件里的正文：签收后 7 天内可申请无理由退款。\n"

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"

#: **升级前**（``f2b8d4a6c913``）的 ``documents`` 形状，逐列照抄当时的迁移：
#: 初始建表（``d657b09304ca``）+ ``index_claimed_at``（``b71a4c5e2f90``）+
#: 还叫 ``doc_version`` 的稿次列（``4c7e8f9a0b1c``，改名在 ``5d8a2f1c7b34``，
#: 建 ``document_versions`` 在 ``7c2e5a9b4d31``）。
#:
#: 用一张**手写的 Core 表**而不是 ``Document.__table__``：后者映射了 #171 改名后的
#: ``current_version``，拿它建表等于把「已经升级过」的库当成前提，这条 bug 就再也复现
#: 不出来。老列表结构在这里是**被测前提**，不是省事。
_OLD_DOCUMENTS = sa.Table(
    "documents",
    sa.MetaData(),
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("kb_id", sa.String(36), nullable=False),
    sa.Column("title", sa.String(512), nullable=False),
    sa.Column("content", sa.Text(), nullable=False),
    sa.Column("source_type", sa.String(32), nullable=False),
    sa.Column("source_file_path", sa.String(1024), nullable=True),
    sa.Column("source_filename", sa.String(512), nullable=True),
    sa.Column("status", sa.Integer(), nullable=False),
    sa.Column("error_message", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(), nullable=False),
    sa.Column("updated_at", sa.DateTime(), nullable=False),
    sa.Column("index_claimed_at", sa.DateTime(), nullable=True),
    sa.Column("doc_version", sa.Integer(), nullable=False, server_default="1"),
)


# ─────────────────────────────────────── 仓储层（升级前形状的内存 SQLite）


@pytest.fixture
def pre_migration_engine(tmp_path: Path) -> Iterator[Engine]:
    """只建**老列** ``documents`` 的库；没有 ``current_version``、没有版本表。

    用文件库而不是 ``sqlite://``：诊断与拒跑用例各自要开独立会话，共享一条内存连接
    会让「谁的提交算数」变成一件与用例无关的事。
    """

    engine = create_engine(f"sqlite:///{(tmp_path / 'pre-migration.sqlite3').as_posix()}")
    _OLD_DOCUMENTS.create(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def document_db(pre_migration_engine: Engine) -> Iterator[Session]:
    session = sessionmaker(bind=pre_migration_engine)()
    try:
        yield session
    finally:
        session.close()


def _seed(
    db: Session,
    document_id: str,
    *,
    content: str = "",
    kb_id: str = KB_ID,
    tenant_id: str = TENANT_ID,
    source_file_path: str | None = None,
    created_at: datetime | None = None,
) -> None:
    """往老列表里插一行（Core INSERT：ORM 实体插不进去，它要写 ``current_version``）。"""

    moment = created_at or now()
    db.execute(
        _OLD_DOCUMENTS.insert().values(
            id=document_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            title=document_id,
            content=content,
            source_type="text",
            source_file_path=source_file_path,
            source_filename="notes.txt",
            status=int(DocumentStatus.SUCCESS),
            created_at=moment,
            updated_at=moment,
            doc_version=1,
        )
    )
    db.commit()


def _content_of(db: Session, document_id: str) -> str:
    """从库里**真读**回正文（不是读回对象上的属性）。"""

    stmt = sa.select(_OLD_DOCUMENTS.c.content).where(_OLD_DOCUMENTS.c.id == document_id)
    return db.execute(stmt).scalar_one()


def _count_commits(session: Session) -> list[str]:
    """记下这个会话提交了几次（``after_commit`` 事件：真提交才算）。"""

    calls: list[str] = []
    event.listen(session, "after_commit", lambda _session: calls.append("commit"))
    return calls


def _ordered(base: datetime, seconds: int) -> datetime:
    """把第 n 行的 ``created_at`` 拉开：扫描顺序就是建行顺序，不靠「同一微秒」碰运气。"""

    return base + timedelta(seconds=seconds)


def test_list_missing_content_returns_only_blank_documents(document_db: Session) -> None:
    """候选就是「正文为空」的那些行：已经有正文的一个都不该被扫出来。"""

    _seed(document_db, "doc-blank", source_file_path="tenant_demo/doc-blank.txt")
    _seed(document_db, "doc-filled", content=_SOURCE_TEXT)
    _seed(document_db, "doc-blank-other-kb", kb_id=OTHER_KB_ID)

    found = {doc.id for doc in document_repository.list_missing_content(document_db)}

    assert found == {"doc-blank", "doc-blank-other-kb"}


def test_list_missing_content_can_be_narrowed_by_kb_and_capped(document_db: Session) -> None:
    """``--kb-id`` 收窄范围、``--limit`` 收敛条数：运维先看一小批再决定。"""

    base = now()
    _seed(document_db, "doc-a", kb_id=KB_ID, created_at=base)
    _seed(document_db, "doc-b", kb_id=KB_ID, created_at=_ordered(base, 1))
    _seed(document_db, "doc-c", kb_id=OTHER_KB_ID, created_at=_ordered(base, 2))

    only_kb = document_repository.list_missing_content(document_db, kb_id=KB_ID)
    capped = document_repository.list_missing_content(document_db, kb_id=KB_ID, limit=1)

    assert {doc.id for doc in only_kb} == {"doc-a", "doc-b"}
    assert len(capped) == 1


def test_list_missing_content_returns_a_named_row_of_the_columns_the_script_needs(
    document_db: Session,
) -> None:
    """候选是**具名结构**、不是 ORM 实体：脚本要用的列都在，实体映射的列不在前提里。

    这条守的是「别再把 ``select(Document)`` 写回来」：真库上那等于要求候选查询读到
    ``documents`` 的全部映射列，于是一条运维查询把「升级到什么程度」变成了它的隐含前提。
    """

    _seed(document_db, "doc-blank", source_file_path="tenant_demo/doc-blank.txt")

    (candidate,) = document_repository.list_missing_content(document_db)

    assert isinstance(candidate, document_repository.MissingContentDocument)
    assert (candidate.id, candidate.tenant_id, candidate.kb_id) == (
        "doc-blank",
        TENANT_ID,
        KB_ID,
    )
    assert candidate.content == ""
    assert candidate.source_type == "text"
    assert candidate.source_file_path == "tenant_demo/doc-blank.txt"
    assert candidate.source_filename == "notes.txt"


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
    """共享会话替身加一条「提交顺序」，给 CLI 用例断言「没在守卫生效时提交过」。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def commit(self) -> None:
        self.calls.append("commit")
        super().commit()


def _source_file(tmp_path: Path, name: str, text: str = _SOURCE_TEXT) -> Path:
    # newline="\n"：文件里的换行必须与期望值一致，别让 Windows 把 \n 翻成 \r\n，
    # 否则断言的是「解析器有没有做换行转换」，而不是「正文有没有写回」。
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# ─────────────────────────────────────────── 升级前可跑（本次 bug 的回归钉）


def test_backfill_runs_before_the_migration(
    backfill_script: ModuleType, document_db: Session, tmp_path: Path
) -> None:
    """**升级前可跑**：老列表结构上候选查得出来、正文写得回去（回归钉）。

    真库上这条的失败形态是 :class:`sqlalchemy.exc.ProgrammingError`
    （``column documents.current_version does not exist``）：候选查询取了整个 ORM 实体，
    而这条脚本的使命恰恰是「升级前先补正文」。夹具里没有 ``current_version``，所以
    任何人把迁移之后才存在的列加回候选查询 / 写回语句，这条用例当场变红。
    """

    source = _source_file(tmp_path, "a.txt")
    _seed(document_db, "doc-blank", source_file_path=str(source))
    _seed(document_db, "doc-filled", content="已有正文。\n")
    commits = _count_commits(document_db)

    stats = backfill_script.backfill_documents(document_db, apply=True)

    assert stats == backfill_script.BackfillStats(
        considered=1, ready=1, backfilled=1, skipped=0, failed=0
    )
    assert _content_of(document_db, "doc-blank") == _SOURCE_TEXT
    assert _content_of(document_db, "doc-filled") == "已有正文。\n", "已有正文不该被覆盖"
    assert commits == ["commit"]


# ────────────────────────────────────────────────────────────── 默认 dry-run


def test_backfill_script_defaults_to_dry_run_and_writes_nothing(
    backfill_script: ModuleType, document_db: Session, tmp_path: Path
) -> None:
    """默认只报告：能补的算出条数、不写行、不提交。"""

    source = _source_file(tmp_path, "a.txt")
    _seed(document_db, "doc-a", source_file_path=str(source))
    commits = _count_commits(document_db)

    stats = backfill_script.backfill_documents(document_db)

    assert stats == backfill_script.BackfillStats(
        considered=1, ready=1, backfilled=0, skipped=0, failed=0
    )
    assert commits == [], "dry-run 一次都不提交"
    assert _content_of(document_db, "doc-a") == "", "dry-run 一个字都不写"


# ──────────────────────────────────────────────────── --apply 才真的写回


def test_backfill_script_apply_writes_the_parsed_text_and_commits_per_document(
    backfill_script: ModuleType, document_db: Session, tmp_path: Path
) -> None:
    """``--apply``：逐篇解析源文件、写回正文、各自提交（一篇失败不影响其余）。"""

    first = _source_file(tmp_path, "a.txt", "第一篇正文。\n")
    second = _source_file(tmp_path, "b.txt", "第二篇正文。\n")
    base = now()
    _seed(document_db, "doc-a", source_file_path=str(first), created_at=base)
    _seed(
        document_db,
        "doc-b",
        source_file_path=str(second),
        created_at=_ordered(base, 1),
    )
    commits = _count_commits(document_db)

    stats = backfill_script.backfill_documents(document_db, apply=True)

    assert stats == backfill_script.BackfillStats(
        considered=2, ready=2, backfilled=2, skipped=0, failed=0
    )
    assert _content_of(document_db, "doc-a") == "第一篇正文。\n"
    assert _content_of(document_db, "doc-b") == "第二篇正文。\n"
    assert commits == ["commit", "commit"], "每篇一笔事务，与两个既有脚本同形"


def test_backfill_script_skips_documents_without_a_readable_source_file(
    backfill_script: ModuleType, document_db: Session, tmp_path: Path
) -> None:
    """读不到源文件的跳过，并**按名字列出来**（运维要知道是哪几篇补不了）。"""

    readable = _source_file(tmp_path, "readable.txt")
    base = now()
    _seed(document_db, "doc-no-path", created_at=base)
    _seed(
        document_db,
        "doc-missing-file",
        source_file_path=str(tmp_path / "gone.txt"),
        created_at=_ordered(base, 1),
    )
    _seed(
        document_db,
        "doc-ok",
        source_file_path=str(readable),
        created_at=_ordered(base, 2),
    )
    commits = _count_commits(document_db)

    stats = backfill_script.backfill_documents(document_db, apply=True)

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
    assert _content_of(document_db, "doc-ok") == _SOURCE_TEXT
    assert commits == ["commit"], "跳过的那两篇不该有任何写动作"


def test_backfill_script_counts_a_write_failure_as_failed_and_keeps_going(
    backfill_script: ModuleType, document_db: Session, tmp_path: Path
) -> None:
    """写库失败记 failed 并回滚；后面那篇仍然照补。"""

    first = _source_file(tmp_path, "a.txt", "第一篇正文。\n")
    second = _source_file(tmp_path, "b.txt", "第二篇正文。\n")
    base = now()
    _seed(document_db, "doc-boom", source_file_path=str(first), created_at=base)
    _seed(
        document_db,
        "doc-ok",
        source_file_path=str(second),
        created_at=_ordered(base, 1),
    )

    original_commit = document_db.commit
    attempts: list[int] = []

    def _commit() -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("deadlock detected")
        original_commit()

    document_db.commit = _commit  # type: ignore[method-assign]

    stats = backfill_script.backfill_documents(document_db, apply=True)

    assert stats == backfill_script.BackfillStats(
        considered=2, ready=2, backfilled=1, skipped=0, failed=1
    )
    assert _content_of(document_db, "doc-boom") == "", "那一笔回滚了，正文不该留下"
    assert _content_of(document_db, "doc-ok") == "第二篇正文。\n"


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
    monkeypatch.setattr(backfill_script, "database_is_migrated", lambda _db: False)
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
    monkeypatch.setattr(backfill_script, "database_is_migrated", lambda _db: False)
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


# ────────────────────────────────────────────────────── 升级后拒跑（守卫）


def test_backfill_script_refuses_to_run_after_the_migration(
    backfill_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    pre_migration_engine: Engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**升级后拒跑**：``document_versions`` 已存在 = 迁移跑过，此时补正文会让不变量分叉。

    迁移会把 ``documents.content`` 抄进版本行；之后单独改文档正文，就出现「文档正文 ≠
    当前稿正文」。升级后要补正文，正确姿势是重建 / 重解析（那条链路同时回写两者）。
    所以这里断言的是三件事：非 0 退出、说明可操作（点名版本表与重解析入口）、**一行都没写**。
    """

    # 迁移跑过的痕迹：版本表在。
    DocumentVersion.__table__.create(pre_migration_engine)
    source = _source_file(tmp_path, "a.txt")

    seeding = sessionmaker(bind=pre_migration_engine)()
    try:
        _seed(seeding, "doc-blank", source_file_path=str(source))
    finally:
        seeding.close()

    monkeypatch.setattr(backfill_script, "SessionLocal", sessionmaker(bind=pre_migration_engine))
    monkeypatch.setattr(backfill_script, "setup_logging", lambda: None)

    exit_code = backfill_script.main(["--apply"])

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert exit_code != 0, "升级之后不许再补正文"
    assert "document_versions" in combined, "说明里要点名判据（这张表在 = 已升级）"
    assert "reindex" in combined, "说明里要给出升级后的正确姿势（重建 / 重解析）"

    verifying = sessionmaker(bind=pre_migration_engine)()
    try:
        assert _content_of(verifying, "doc-blank") == "", "拒跑必须一行都没写"
        assert (
            verifying.execute(
                sa.select(sa.func.count()).select_from(DocumentVersion.__table__)
            ).scalar_one()
            == 0
        )
    finally:
        verifying.close()

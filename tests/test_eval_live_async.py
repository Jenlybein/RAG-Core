"""评测 live 路径的异步改造（#157 §①）：六处会话与两个命令行入口。

## 这张票在修什么

``scripts/eval_runner.py`` / ``scripts/eval_retrieval.py`` 的 live 路径仍拿**同步** ``Session``
去喂异步检索链路（#130 / #132 之后 ``rag_service.retrieve`` 是协程），第一条查询上就是
``TypeError: object ChunkedIteratorResult can't be used in 'await' expression``。要动的六处是
清库、建库、建文档、索引、标签解析、打分——它们都是「评测脚本与数据库之间的那一层」，
所以**接缝就是这六个函数本身**：调用方（``_run``）只认它们是协程。

第二件同等重要、但更容易漏的事是**事件循环策略**。psycopg 的异步连接在 Windows 的缺省
``ProactorEventLoop`` 上直接拒绝工作（``Psycopg cannot use the 'ProactorEventLoop'``），
所以两个入口都必须先钉 ``WindowsSelectorEventLoopPolicy``——其余运维脚本（``eval_c6_guard``
/ ``migrate_keyword_alias``）早就是这么做的，评测脚本是漏掉的那两个。

## 为什么还要一条「别再写回同步」的结构守卫

这六处搬家之后，最可能的回归不是写错 await，而是**下一个人顺手再开一个同步会话**：那不会
报错，只会在真库上炸成上面那个 ``TypeError``，而离线套件一直是绿的。所以用例直接扫源码：
评测脚本里不许出现 ``SessionLocal``、两个入口必须 ``asyncio.run`` + 选择器策略。
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from scripts import eval_retrieval, eval_runner
from tests.fake_db import AsyncRecordingSession

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EVAL_SCRIPTS = (
    REPO_ROOT / "scripts" / "eval_retrieval.py",
    REPO_ROOT / "scripts" / "eval_runner.py",
)


def _source(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


# ────────────────────────────────────────────────── 六处：形状（必须是协程）

#: 清库 / 建库 / 建文档 / 索引 / 标签解析 / 打分 —— 六处逐一对上一个可调用对象。
#: 「打分」对应 ``eval_runner._score_questions``（它把 ``db`` 传给 ``_run_question``，
#: 同一条会话一路走到 ``retrieve``）；``_cleanup`` 是清库的入口形态，一起钉住。
LIVE_STEPS = (
    ("清库", eval_retrieval, "_reset_postgres"),
    ("建库", eval_retrieval, "_ensure_knowledge_base"),
    ("建文档", eval_retrieval, "_ensure_document"),
    ("索引", eval_retrieval, "_index_documents"),
    ("标签解析", eval_retrieval, "_load_indexed_chunks"),
    ("清库入口", eval_retrieval, "_cleanup"),
    ("打分", eval_runner, "_score_questions"),
)


@pytest.mark.parametrize(("label", "module", "name"), LIVE_STEPS)
def test_every_live_step_is_a_coroutine(label: str, module: object, name: str) -> None:
    """六处都必须是 ``async def``：同步函数接不住 ``await retrieve(...)``。

    断言的是**函数形状**而不是某一行实现——搬家可以改名字以外的任何东西，但
    「这一层是协程」是调用方 ``_run`` 与 ``asyncio.run`` 之间的契约。
    """

    function = getattr(module, name, None)
    assert function is not None, f"{label}：{module.__name__}.{name} 不存在（被改名了？）"
    assert inspect.iscoroutinefunction(function), (
        f"{label}：{module.__name__}.{name} 还是同步函数，接异步检索链路会炸 ChunkedIteratorResult"
    )


# ────────────────────────────────────────────────── 六处：行为（经异步会话替身）


@pytest.mark.asyncio
async def test_reset_postgres_clears_the_three_tables_in_one_transaction() -> None:
    """清库：三条 DELETE（切片 → 文档 → 库，外键顺序）+ 恰好一次提交。

    断言 SQL 的**顺序**：先删切片再删文档最后删库。反过来会撞外键（真库上直接失败），
    而替身不会替我们拦住它，所以顺序在用例里钉死。
    """

    db = AsyncRecordingSession()

    counts = await eval_retrieval._reset_postgres(db)

    assert set(counts) == {"chunks", "documents", "knowledge_bases"}
    verbs = [statement.lstrip().split(maxsplit=1)[0].upper() for statement in db.statements]
    assert verbs == ["DELETE", "DELETE", "DELETE"]
    tables = [statement.split()[2] for statement in db.statements]
    assert tables == ["chunks", "documents", "knowledge_bases"]
    assert db.commits == 1, "清库必须在一个事务里收口（三条 DELETE 各提交一次会留下半清的库）"


@pytest.mark.asyncio
async def test_ensure_knowledge_base_inserts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """建库：库里没有这个 kb 时插入一行，并且**只**插入一次。"""

    db = AsyncRecordingSession()

    await eval_retrieval._ensure_knowledge_base(
        db,
        kb_id="kb-eval",
        name="评测语料库·客户支持",
        description="",
        split_strategy="structure",
    )

    assert len(db.added) == 1
    created = db.added[0]
    assert created.id == "kb-eval"
    assert created.split_strategy == "structure"
    # 白名单按策略推导（不在脚本里另写一份）：structure → 只收 markdown。
    assert created.allowed_formats == ["markdown"]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_ensure_knowledge_base_refuses_a_strategy_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已存在但策略不同 → 当场拒绝，不就地改（改配置补不回旧的切法）。"""

    from app.models.knowledge_base import KnowledgeBase

    existing = KnowledgeBase(
        id="kb-eval",
        tenant_id="tenant_demo",
        name="评测语料库·客户支持",
        description="",
        split_strategy="size",
        allowed_formats=[],
        settings={},
    )
    db = AsyncRecordingSession(rows={KnowledgeBase: [existing]})

    with pytest.raises(SystemExit, match="策略"):
        await eval_retrieval._ensure_knowledge_base(
            db,
            kb_id="kb-eval",
            name="评测语料库·客户支持",
            description="",
            split_strategy="structure",
        )

    assert db.added == []
    assert db.commits == 0, "拒绝时一个字节都不该写"


@pytest.mark.asyncio
async def test_ensure_document_requeues_instead_of_duplicating() -> None:
    """建文档：已有行就原地重置成「已排队、未开工」，不再 add 一条（主键会撞）。"""

    from app.models.document import Document

    existing = Document(
        id="eval-doc-support_errors",
        tenant_id="tenant_demo",
        kb_id="kb-eval",
        title="旧标题",
        content="旧正文",
        source_type="markdown",
    )
    db = AsyncRecordingSession(rows={Document: [existing]})
    doc = eval_retrieval.CorpusDoc(
        stem="support_errors",
        path=REPO_ROOT / "eval" / "corpus" / "support_errors.md",
        kb_id="kb-eval",
        title="售后故障与错误码速查",
        text="正文",
    )

    await eval_retrieval._ensure_document(db, doc)

    assert db.added == []
    assert existing.title == "售后故障与错误码速查"
    assert existing.content == "正文"
    assert existing.index_claimed_at is None
    assert db.commits == 1


@pytest.mark.asyncio
async def test_ensure_document_inserts_a_claimable_row() -> None:
    """建文档：没有行时插入一行，且状态是 Worker 可抢占的那一个。"""

    db = AsyncRecordingSession()
    doc = eval_retrieval.CorpusDoc(
        stem="support_errors",
        path=REPO_ROOT / "eval" / "corpus" / "support_errors.md",
        kb_id="kb-eval",
        title="售后故障与错误码速查",
        text="正文",
    )

    await eval_retrieval._ensure_document(db, doc)

    assert len(db.added) == 1
    row = db.added[0]
    assert row.index_claimed_at is None, "占着租约的行会让 Worker 静默跳过，重跑就不再索引"
    from app.utils.status import DocumentStatus

    assert row.status == DocumentStatus.PROCESSING


# ────────────────────────────────────────────────── 结构守卫：别写回同步


@pytest.mark.parametrize("path", EVAL_SCRIPTS, ids=lambda path: path.name)
def test_eval_scripts_never_open_a_synchronous_session(path: pathlib.Path) -> None:
    """评测脚本里不许再出现同步会话：那正是本票要根治的那条路。

    不查「有没有 await」（写法太多），只查「有没有那个会炸的对象」——``SessionLocal``
    与 ``app.db.session`` 的一次出现就足够把整条 live 路径打回同步栈。
    """

    source = _source(path)
    assert not re.search(r"(?<![A-Za-z_])SessionLocal\s*\(", source), f"{path.name} 又开了同步会话"
    assert "from app.db.session import" not in source, f"{path.name} 又把同步会话引进来了"


@pytest.mark.parametrize("path", EVAL_SCRIPTS, ids=lambda path: path.name)
def test_eval_scripts_pin_the_selector_event_loop(path: pathlib.Path) -> None:
    """入口必须钉 ``WindowsSelectorEventLoopPolicy``。

    psycopg 的异步连接在 Windows 缺省的 Proactor 循环上直接拒绝工作，而这只在**真库**
    上暴露：离线套件全绿，第一次连库就炸。其余运维脚本早就是这套，评测脚本不能例外。
    """

    source = _source(path)
    assert "asyncio.run(" in source, f"{path.name} 的入口没有走 asyncio.run"
    assert "WindowsSelectorEventLoopPolicy" in source, (
        f"{path.name} 的入口没钉选择器事件循环（psycopg 异步在 Proactor 上直接拒绝）"
    )

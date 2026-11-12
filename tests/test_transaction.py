"""票 #124：写路径的**唯一**提交点（``app.db.transaction``）。

这里断言的是事务入口的四条外部契约，不碰任何内部结构：

1. 正常出块 → 提交一次；块内抛错 → 回滚一次并把异常原样抛回去（错误码因此不变）；
2. 嵌套入口并入外层：只有最外层提交，内层提前「成功」不会把半成品落库；
3. ``after_commit`` 登记的动作**只在提交之后**执行，回滚时整批丢弃（投递任务、删缓存
   这类「必须等库先落地」的副作用靠它，而不是靠「先 commit 再做」的写法）；
4. 会话上挂着的这一笔可以被取到（``current``），没在入口里时是 ``None``。

第 4 条是服务层能拿到它的唯一途径：请求边界由路由的 ``Depends(get_transaction)`` 开，
服务层只问「这一笔在不在」并往上登记，自己不提交。

**异步孪生**（第 11 步票 #128）守着**同一组**契约：``async with async_transaction(session):``。
两者必须逐条一致——否则「换了栈之后事务语义变了」这件事只会在生产上以半提交的形态出现。
所以最后一节把同一组操作在同步与异步两笔事务上各跑一遍，断言**事件序列逐字相同**；
期望值写成字面量，而不是「用另一套算一遍」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db import transaction as tx


class _Session:
    """最小会话替身：只记事件顺序，并带一个真实的 ``info`` 字典。

    事件列表而不是计数器：``after_commit`` 的契约是**顺序**（提交在前、动作在后），
    只数次数会把「先做动作后提交」这种错当成通过。
    """

    def __init__(self) -> None:
        self.info: dict[str, object] = {}
        self.events: list[str] = []

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")


def test_commits_once_when_the_block_succeeds() -> None:
    db = _Session()

    with tx.transaction(db):
        db.events.append("work")

    assert db.events == ["work", "commit"]


def test_rolls_back_and_reraises_when_the_block_fails() -> None:
    db = _Session()

    with pytest.raises(RuntimeError, match="boom"):
        with tx.transaction(db):
            db.events.append("half-done")
            raise RuntimeError("boom")

    assert db.events == ["half-done", "rollback"]


def test_nested_entries_join_the_outer_transaction() -> None:
    db = _Session()

    with tx.transaction(db):
        with tx.transaction(db):
            db.events.append("inner")
        db.events.append("outer")

    assert db.events == ["inner", "outer", "commit"]


def test_inner_failure_rolls_back_the_whole_outer_transaction() -> None:
    db = _Session()

    with pytest.raises(RuntimeError):
        with tx.transaction(db):
            db.events.append("first")
            with tx.transaction(db):
                raise RuntimeError("inner failed")

    assert db.events == ["first", "rollback"]


def test_after_commit_runs_only_after_the_commit() -> None:
    db = _Session()

    with tx.transaction(db):
        tx.after_commit(db, lambda: db.events.append("dispatch"))

    assert db.events == ["commit", "dispatch"]


def test_after_commit_actions_are_dropped_when_the_block_fails() -> None:
    db = _Session()

    with pytest.raises(RuntimeError):
        with tx.transaction(db):
            tx.after_commit(db, lambda: db.events.append("dispatch"))
            raise RuntimeError("write failed")

    assert db.events == ["rollback"]


def test_after_commit_actions_run_in_registration_order() -> None:
    db = _Session()

    with tx.transaction(db):
        tx.after_commit(db, lambda: db.events.append("first"))
        tx.after_commit(db, lambda: db.events.append("second"))

    assert db.events == ["commit", "first", "second"]


def test_after_commit_from_a_nested_block_waits_for_the_outer_commit() -> None:
    db = _Session()

    with tx.transaction(db):
        with tx.transaction(db):
            tx.after_commit(db, lambda: db.events.append("dispatch"))
        db.events.append("still inside")

    assert db.events == ["still inside", "commit", "dispatch"]


def test_after_commit_outside_any_entry_is_a_programming_error() -> None:
    """写路径忘了声明事务边界时必须立刻炸：静默「立即执行」等于回到散落提交的老路。"""

    db = _Session()

    with pytest.raises(tx.NoTransactionError):
        tx.after_commit(db, lambda: db.events.append("dispatch"))


def test_current_reports_the_open_transaction_and_clears_on_exit() -> None:
    db = _Session()
    assert tx.current(db) is None

    with tx.transaction(db) as opened:
        assert tx.current(db) is opened

    assert tx.current(db) is None


def test_repeated_entry_on_the_same_session_does_not_leave_state_behind() -> None:
    """请求之间复用同一个会话（脚本、Worker）时，第二次入口必须是一笔**新**事务。"""

    db = _Session()

    with tx.transaction(db):
        pass
    with tx.transaction(db):
        pass

    assert db.events == ["commit", "commit"]


# ──────────────────────────────────────────── 异步孪生：同一组契约，另一套语法


class _AsyncSession:
    """``_Session`` 的异步面：只把 ``commit`` / ``rollback`` 变成可 ``await`` 的。

    事件列表而不是计数器，理由与同步那份一字不差：契约是**顺序**。
    """

    def __init__(self) -> None:
        self.info: dict[str, object] = {}
        self.events: list[str] = []

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")


@pytest.mark.asyncio
async def test_the_async_twin_commits_once_when_the_block_succeeds() -> None:
    db = _AsyncSession()

    async with tx.async_transaction(db):
        db.events.append("work")

    assert db.events == ["work", "commit"]


@pytest.mark.asyncio
async def test_the_async_twin_rolls_back_and_reraises_when_the_block_fails() -> None:
    db = _AsyncSession()

    with pytest.raises(RuntimeError, match="boom"):
        async with tx.async_transaction(db):
            db.events.append("half-done")
            raise RuntimeError("boom")

    assert db.events == ["half-done", "rollback"]


@pytest.mark.asyncio
async def test_async_nested_entries_join_the_outer_transaction() -> None:
    db = _AsyncSession()

    async with tx.async_transaction(db):
        async with tx.async_transaction(db):
            db.events.append("inner")
        db.events.append("outer")

    assert db.events == ["inner", "outer", "commit"]


@pytest.mark.asyncio
async def test_async_after_commit_runs_only_after_the_commit() -> None:
    db = _AsyncSession()

    async with tx.async_transaction(db):
        tx.after_commit(db, lambda: db.events.append("dispatch"))

    assert db.events == ["commit", "dispatch"]


@pytest.mark.asyncio
async def test_async_after_commit_actions_are_dropped_when_the_block_fails() -> None:
    db = _AsyncSession()

    with pytest.raises(RuntimeError):
        async with tx.async_transaction(db):
            tx.after_commit(db, lambda: db.events.append("dispatch"))
            raise RuntimeError("write failed")

    assert db.events == ["rollback"]


@pytest.mark.asyncio
async def test_async_after_commit_outside_any_entry_is_a_programming_error() -> None:
    db = _AsyncSession()

    with pytest.raises(tx.NoTransactionError):
        tx.after_commit(db, lambda: db.events.append("dispatch"))


@pytest.mark.asyncio
async def test_the_two_twins_produce_the_same_event_sequence() -> None:
    """共享契约本身：同一组操作在同步与异步两笔事务上产生**同一串**事件。

    两笔事务各写一份实现（一份 ``with``、一份 ``async with``），漂移的症状是「异步那笔
    少了提交后动作」或「嵌套时提前提交」——两者都只在生产上以半提交的形态出现。期望值
    写成字面量：用另一套算出来的期望，在漂移时仍然是绿的。
    """

    sync_db = _Session()
    async_db = _AsyncSession()

    with tx.transaction(sync_db):
        sync_db.events.append("work")
        tx.after_commit(sync_db, lambda: sync_db.events.append("dispatch"))
    async with tx.async_transaction(async_db):
        async_db.events.append("work")
        tx.after_commit(async_db, lambda: async_db.events.append("dispatch"))

    assert sync_db.events == ["work", "commit", "dispatch"]
    assert async_db.events == sync_db.events


def test_only_the_transaction_module_commits() -> None:
    """结构守卫：全仓只有 ``app/db/transaction.py`` 允许调 ``session.commit()`` / ``rollback()``。

    这条守卫守的不是洁癖，是**换栈的成本边界**：下一票把同步会话换成异步会话时，
    ``session.commit()`` 会变成 ``await session.commit()``。散在几十处的提交点会变成
    几十处 async 传染；集中在一个模块时，改的是这一个文件。

    第 11 步票 #127 起检索链路已经是异步的，守卫**一个字都没改**仍然成立——异步提交
    （``acommit_now`` 里的 ``await session.commit()``）也在这个文件里，所以「提交散出去」
    在代码层依然走不通。
    """

    allowed = Path("app/db/transaction.py")
    offenders: list[str] = []
    for path in Path("app").rglob("*.py"):
        if path == allowed:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if ".commit()" in line or ".rollback()" in line:
                offenders.append(f"{path}:{number}: {line.strip()}")

    assert offenders == [], "提交点必须集中在 app/db/transaction.py：\n" + "\n".join(offenders)



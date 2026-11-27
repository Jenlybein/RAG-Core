"""写路径的「索引代」（现场九·第二步）：每次索引新开一代，并把文档指针推到那一代。

## 这一步修的是什么

第一步只加了列并回填。写路径没跟上的后果，在真栈上量到过：重建一个库之后，`v2` 与 `v3`
两批切片**都标着第 1 代**（新切片拿的是列缺省值），于是既没法做「保留最近 2 代」的回收，
也没法做「单篇切回上一代」——两代的东西长得一模一样。

所以这一步只做一件事：**每跑一次索引就新开一代**，并在**同一笔事务**里把文档的当前代推上去。

## 为什么「新开一代」而不是「覆盖当前代」

覆盖会让上一份产物消失：既没有回滚的落点，也没法回答「换成新切法之后那几条答案变差，是因为
切法变了还是内容变了」。两份产物必须同时在库里才比得了。代价是存储随重建次数增长，由保留
策略（保留最近 N 代）收口。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from app.models.chunk import DEFAULT_GENERATION, Chunk
from app.models.document import Document
from app.repositories import document_repository
from app.services.index_generation import FIRST_GENERATION, next_generation


class _RecordingSession:
    """只记下发出去的语句：这一层要断言的是「发了什么 UPDATE」，不是库里的效果。"""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> Any:
        self.statements.append(statement)
        return None


def test_the_first_product_is_the_first_generation() -> None:
    """「还没有任何一代」从第一代起算——与迁移回填的口径同一个号。

    两边不同源的话，回填过的文档第一次重建就跳号，而跳号不报错，只是让「保留最近 N 代」
    的窗口算错一格（少留或多留一份产物）。
    """

    assert next_generation(None) == FIRST_GENERATION == DEFAULT_GENERATION


def test_every_run_advances_one_generation() -> None:
    assert next_generation(1) == 2
    assert next_generation(7) == 8


@pytest.mark.parametrize("current", [None, 1, 9])
def test_the_next_generation_is_always_strictly_greater(current: int | None) -> None:
    """新的一代必须严格大于当前代：等于就会让「覆盖」伪装成「新开一代」。"""

    assert next_generation(current) > (current or 0)


def test_advancing_the_pointer_writes_both_columns_in_one_statement() -> None:
    """一代指针与设置指纹写在**同一条 UPDATE** 里。

    分开写（两条语句）就多了一个「指针已动、指纹还是旧的」的窗口，而那个窗口里界面显示的
    「这一篇是最新的」是假的——它恰恰会让人停止更新，于是错的那一版永远留在线上。
    """

    session = _RecordingSession()

    asyncio.run(
        document_repository.advance_index_generation(
            session,  # type: ignore[arg-type]
            "doc-1",
            tenant_id="tenant-a",
            generation=3,
            settings_hash="fingerprint-abc",
        )
    )

    assert len(session.statements) == 1, "指针与指纹必须同一条语句"
    statement = session.statements[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert statement.is_update
    assert statement.table.name == Document.__table__.name
    assert "current_index_generation=3" in compiled
    assert "settings_hash='fingerprint-abc'" in compiled
    # 租户条件不是装饰：少了它，一次 id 猜测就能改别人的指针。
    assert "tenant_id = 'tenant-a'" in compiled


def test_the_chunk_carries_the_generation_it_was_built_as() -> None:
    """切片上那一列是**真实列**（不是 JSONB 里的键），写入侧显式给值。

    让它走列缺省值是第一步留下的坑：所有产物都变成「第 1 代」，两代分不开。
    """

    assert Chunk.__table__.c.generation.nullable is False
    assert Chunk.__table__.c.generation.default.arg == DEFAULT_GENERATION


def test_the_statement_targets_only_the_generation_columns() -> None:
    """这条 UPDATE 只碰两列：它跑在索引的热路径上，多写一列就多一份与别处打架的机会。"""

    session = _RecordingSession()

    asyncio.run(
        document_repository.advance_index_generation(
            session,  # type: ignore[arg-type]
            "doc-1",
            tenant_id="tenant-a",
            generation=2,
            settings_hash=None,
        )
    )

    statement = session.statements[0]
    assert statement.is_update
    assert {column.name for column in statement._values} == {
        "current_index_generation",
        "settings_hash",
    }

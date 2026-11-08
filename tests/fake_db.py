"""离线测试用的最小假会话。

检索层在拼「父块」时会 `execute` 一次查询；以前 `get_db` 的替身直接返回
`object()`，什么都不实现，所以一旦有真实查询走进去就会 `AttributeError`。

这里的假会话刻意**只实现被用到的那几个方法**，并且一律返回空结果：离线测试关心
的是「查询有没有按正确参数发出去」和「响应契约是否稳定」，不关心数据本身。
要断言父块内容，用专门的用例注入真实返回值（见 ``test_parent_section.py``）。
"""

from __future__ import annotations

from typing import Any


class _EmptyResult:
    """SQLAlchemy Result 的最小替身：空行、0 计数。"""

    def all(self) -> list[Any]:
        return []

    def first(self) -> None:
        return None

    def scalar_one(self) -> int:
        return 0

    def scalar_one_or_none(self) -> None:
        return None

    def scalars(self) -> _EmptyResult:
        return self

    def __iter__(self) -> Any:
        return iter(())


class EmptySession:
    """不做任何事的会话：写入被忽略，查询返回空。"""

    def execute(self, *_args: Any, **_kwargs: Any) -> _EmptyResult:
        return _EmptyResult()

    def add(self, _obj: Any) -> None:
        return None

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

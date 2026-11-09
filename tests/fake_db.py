"""离线测试用的最小假会话。

检索层在拼「父块」时会 `execute` 一次查询；以前 `get_db` 的替身直接返回
`object()`，什么都不实现，所以一旦有真实查询走进去就会 `AttributeError`。

这里的假会话刻意**只实现被用到的那几个方法**：离线测试关心的是「查询有没有按正确参数
发出去」和「响应契约是否稳定」，不关心数据本身。两个变体：

- ``EmptySession``：一律返回空结果（读路径的默认替身）；
- ``RecordingSession``：记下 ``add`` 进来的行，并能按模型返回预置行——运维脚本与播种
  这类「可观测结果就是落了哪些行」的场景用它（见 ``test_identity_seeding.py``）。
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


class _Rows:
    """``execute(...).scalars()`` 的最小替身：包住一组预置值。"""

    def __init__(self, values: list[Any]) -> None:
        self._values = list(values)

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[Any]:
        return list(self._values)

    def __iter__(self) -> Any:
        return iter(self._values)


class CountResult:
    """``db.execute(SELECT count(*))`` 的最小替身：一个数字就是全部。

    静态资源配额（票 #79）在写路径上数行，用的是 ``scalar_one()``；替身会话因此需要
    能回答「这个租户 / 这个库现在有多少行」。缺省 0 行，需要别的数字时由测试自己注入。
    """

    def scalar_one(self) -> int:
        return 0


ZERO_COUNT = CountResult()


class _FakeQuery:
    """``db.query(Model)`` 的最小替身。

    只认仓储层真正用到的那几种写法：``.all()``、``.order_by(...).all()``、
    ``.filter(列 == 字面量).first()``、``.count()``。别的写法一律落到
    ``AttributeError``——假会话越像真会话，越容易让人误以为测过的是数据库。
    """

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self._criterion: Any = None

    def filter(self, criterion: Any) -> _FakeQuery:
        self._criterion = criterion
        return self

    def order_by(self, *_args: Any) -> _FakeQuery:
        return self

    def all(self) -> list[Any]:
        return [row for row in self._rows if self._matches(row)]

    def first(self) -> Any:
        rows = self.all()
        return rows[0] if rows else None

    def count(self) -> int:
        return len(self.all())

    def _matches(self, row: Any) -> bool:
        if self._criterion is None:
            return True
        # 只支持 `Model.列 == 字面量`：左边是属性（有 .key），右边是绑定参数（有 .value）
        return bool(getattr(row, self._criterion.left.key) == self._criterion.right.value)


class RecordingSession(EmptySession):
    """记下写入了什么、并能按模型返回预置行的假会话。

    播种、回填、开租户这类运维动作的可观测结果就是「加进去哪些行」，所以这里记
    ``added``；查询侧沿用 ``_FakeQuery``。``tenant_ids`` 是给「从既有四张业务表里
    读出已存在租户」那条读路径用的预置结果。
    """

    def __init__(
        self,
        *,
        rows: dict[type, list[Any]] | None = None,
        tenant_ids: list[str] | None = None,
    ) -> None:
        self._rows = {model: list(items) for model, items in (rows or {}).items()}
        self._tenant_ids = list(tenant_ids or [])
        self.added: list[Any] = []
        self.commits = 0
        self.closed = False
        self.rollbacks = 0

    def query(self, model: type) -> _FakeQuery:
        return _FakeQuery(self._rows.get(model, []))

    def get(self, model: type, primary_key: Any) -> Any:
        for row in self._rows.get(model, []):
            if getattr(row, "id", None) == primary_key:
                return row
        return None

    def execute(self, _statement: Any, *_args: Any, **_kwargs: Any) -> _Rows:
        return _Rows(self._tenant_ids)

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        # 让刚 add 的行之后还能被查到：真实会话里 flush 之后就是这个效果。
        self._rows.setdefault(type(obj), []).append(obj)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True

    def added_of(self, model: type) -> list[Any]:
        """本次真正写入的某类行；断言「只写了该写的表」比数总数更准。"""

        return [obj for obj in self.added if isinstance(obj, model)]

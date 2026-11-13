"""离线测试用的最小假会话。

检索层在拼「父块」时会 `execute` 一次查询；以前 `get_db` 的替身直接返回
`object()`，什么都不实现，所以一旦有真实查询走进去就会 `AttributeError`。

这里的假会话刻意**只实现被用到的那几个方法**：离线测试关心的是「查询有没有按正确参数
发出去」和「响应契约是否稳定」，不关心数据本身。两个变体：

- ``EmptySession``：一律返回空结果（读路径的默认替身）；
- ``RecordingSession``：记下 ``add`` 进来的行，并能按模型返回预置行——运维脚本与播种
  这类「可观测结果就是落了哪些行」的场景用它（见 ``test_identity_seeding.py``）；
- ``AsyncRecordingSession``：同一个替身的**异步面**（票 #126）。语义只写一遍，见下。

**第 11 步票 #134 之后，这一份替身只剩三个角色**（ADR-0024）：异步面（
``AsyncRecordingSession``，请求路径与旁路短事务的默认替身）、被 ``AsyncSessionAdapter``
包住的逐用例预置答案（``EmptySession`` / ``TransactionalSession`` 与各文件自己造的小类）、
以及**运维脚本**用的同步面。``get_db`` / ``get_transaction`` 两个 FastAPI 依赖已经删除，
所以这里不再有「同步请求替身」这一角色——``override_all_db`` 只换异步那两个入口。

**为什么 ``statements`` 记的是 SQL 文本**：同一个操作在同步与异步替身上必须产生同一组
SQL（``tests/test_session_double_contract.py``）。只记「execute 被调了几次」看不出
「同一次调用发的是不是同一条语句」。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy.sql import operators

from app.models.chunk import DEFAULT_INDEX_VERSION


async def async_visible_kb(_db: Any, _kb_id: str, *, tenant_id: str) -> Any:
    """「库可见」的**异步**替身：租户原样回传，``settings`` 为空、名称与描述为空。

    检索链路的可见性校验走 ``knowledge_base_repository.get_by_id_for_tenant``
    （第 11 步票 #127），于是各用例里那句
    ``lambda db, kb_id, *, tenant_id: SimpleNamespace(tenant_id=tenant_id)``
    换成它即可——返回的东西与原来那份等价，只是可 ``await``。

    ``id`` 与 ``active_index_version`` 是**真行上也有的两列**（票 #141）：检索范围从
    「所有库配 v1」改成「读各库的生效版本」之后，替身缺这两列就不再等价于真行——
    它会变成「一个还没回填的库」，而那正是检索守卫要拦下的状态。默认给 ``v1``：
    与迁移的回填值、与索引流水线写下的版本同值。
    """

    return SimpleNamespace(
        id=_kb_id,
        tenant_id=tenant_id,
        settings={},
        name="",
        description="",
        active_index_version=DEFAULT_INDEX_VERSION,
    )


async def async_invisible_kb(_db: Any, _kb_id: str, *, tenant_id: str) -> Any:
    """「库不可见」的**异步**替身：仓储按口径回 ``None``，守卫据此抛 20008。"""

    return None


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
    """不做任何事的会话：写入被忽略，查询返回空。

    ``info`` 是 SQLAlchemy 会话上给调用方留的那个字典：事务入口把「当前这一笔」挂在
    这里（见 ``app.db.transaction``），所以替身必须有它，否则「写路径在事务里」这件事
    在离线套件里根本跑不起来。
    """

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}

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

    def first(self) -> Any:
        """按主键 / 唯一条件取一行（``select(...).scalars().first()`` 的形状）。

        异步仓储只能走 ``execute(select(...))``（``AsyncSession`` 没有 ``db.query``），
        而「这一行在不在」这类判断最自然的收尾就是 ``.first()``。少了它，那些地方要么
        被写成 ``.all()[0]``，要么只能绕开替身——两者都会让用例离真实调用形状更远。

        **空行回 ``None``、否则回第一行**——真 ``Result.first()`` 就是这个口径。票 #130
        之前这个方法是空的（只有文档），于是「按用户名找账号」在替身上永远查不到人，
        症状是登录一律 ``20010``，看着像口令错。按用户名读的仓储正是走
        ``execute(select(...)).scalars().first()`` 的那条路。
        """

        return self._values[0] if self._values else None

    def scalar_one(self) -> Any:
        """``select(...)`` 只该命中一行时用（``SELECT count(*)`` 那条走 :class:`CountResult`）。"""

        if len(self._values) != 1:
            raise AssertionError(f"scalar_one 期望恰好一行，实际 {len(self._values)} 行")
        return self._values[0]

    def scalar_one_or_none(self) -> Any:
        """按 id + 租户点查一行的收尾（``execute(select(...)).scalar_one_or_none()``）。

        第 11 步票 #127 起请求会话（异步替身）要回答「这个库对这个租户可不可见」，票 #132
        起上传登记也走同一条收尾——真 ``Result`` 本来就有这个方法。空行回 ``None``、
        恰好一行回那一行，多于一行按 SQLAlchemy 的口径**报错**：多行说明查询条件写错了，
        静默取第一条正是最难查的那类假绿。

        这类查询的 ``id`` 是主键，真库上不可能多行，所以那条纪律在这里**从不会误伤**
        正常数据——它只在假数据构造错了的时候响，而那正是应该响的时候。
        """

        if not self._values:
            return None
        if len(self._values) > 1:
            raise AssertionError(f"期望 0 或 1 行，实际 {len(self._values)} 行")
        return self._values[0]

    def __iter__(self) -> Any:
        return iter(self._values)


class CountResult:
    """``db.execute(SELECT count(*))`` 的最小替身：一个数字就是全部。

    静态资源配额（票 #79）在写路径上数行，用的是 ``scalar_one()``；替身会话因此需要
    能回答「这个租户 / 这个库现在有多少行」。缺省 0 行，需要别的数字时由测试自己注入。
    """

    def __init__(self, value: int = 0) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value

    def scalar_one_or_none(self) -> int:
        """「按 id 查一行再看有没有」的同一批调用也会落到替身上；计数场景恒有一个值。"""

        return self._value

    def scalars(self) -> "CountResult":
        """有些查询走 ``.scalars().all()``（列表页）；计数替身没有行，回空。"""

        return self

    def all(self) -> list[Any]:
        return []

    def first(self) -> None:
        return None


ZERO_COUNT = CountResult()


def _orm_model_of(statement: Any) -> type | None:
    """``select(...)`` 语句读的是哪张 ORM 映射（认不出来就返回 ``None``）。

    认法是 SQLAlchemy 自己的列描述：``column_descriptions[i]["entity"]`` 给出被选的映射类，
    对 ``select(Model.col1, Model.col2)`` 同样成立。``select(func.count())`` 那一类没有
    映射类，于是落回 ``None``（那条形状由 ``SELECT COUNT(`` 分支先接住）。
    """

    for description in getattr(statement, "column_descriptions", None) or ():
        entity = description.get("entity")
        if isinstance(entity, type) and hasattr(entity, "__tablename__"):
            return entity
    return None


def _matches_where(row: Any, criterion: Any) -> bool:
    """``WHERE`` 只认「列 = 字面量」「列 IN (...)」「列 < / > 字面量」（可 ``AND`` 组合）。

    与 ``_FakeQuery._matches`` 同一条口径，只是补上了仓储层真的用到的几种写法：
    ``audit_logs.action = ?``、``retrieval_traces.trace_id = ? AND tenant_id = ?``、
    ``retrieval_feedback.log_id IN (...)``、以及「比某个时刻更早」这类**时间比较**
    （``index_build_versions.created_at < cutoff``，见 #142 的「有行、没任务」检测）。
    """

    clauses = getattr(criterion, "clauses", None) or (criterion,)
    for node in clauses:
        left = getattr(node, "left", None)
        right = getattr(node, "right", None)
        key = getattr(left, "key", None)
        expected = getattr(right, "value", _MISSING)
        if key is None or expected is _MISSING:
            return False
        actual = getattr(row, key, None)
        operator = getattr(node, "operator", None)
        if operator is operators.in_op:
            if actual not in expected:
                return False
        elif operator in (operators.lt, operators.gt, operators.le, operators.ge):
            # 时间比较：``None`` 一律不匹配（真实 SQL 里 NULL 参与比较也不成立）。
            if actual is None:
                return False
            matched = {
                operators.lt: actual < expected,
                operators.gt: actual > expected,
                operators.le: actual <= expected,
                operators.ge: actual >= expected,
            }[operator]
            if not matched:
                return False
        elif actual != expected:
            return False
    return True


_MISSING = object()


class MemoryPlanCache:
    """套餐缓存的内存假实现（契约见 ``app.core.plan_cache``）。

    三个方法都是 ``async``：生产实现自票 #129 起握的是 ``redis.asyncio`` 客户端，替身必须
    同形，否则「忘了 ``await``」在套件里表现为「缓存永远不命中」而不是一条明确的失败。
    ``set_calls`` / ``deleted`` 让「未命中时回填一次」「改完是删不是写」这两条行为可以被
    直接断言，``items`` 则是用例自己预置缓存内容的入口。

    九份逐字相同的本地实现（票 #129 之前每个控制面测试各写一份）收在这里：接口一改就要
    改九处，而漏掉一处只会让那个文件的用例悄悄连真 Redis。
    """

    def __init__(self) -> None:
        self.items: dict[int, Any] = {}
        self.set_calls: list[int] = []
        self.deleted: list[int] = []

    async def get(self, plan_id: int) -> Any:
        return self.items.get(plan_id)

    async def set(self, plan_id: int, plan: Any) -> None:
        self.set_calls.append(plan_id)
        self.items[plan_id] = plan

    async def delete(self, plan_id: int) -> None:
        self.deleted.append(plan_id)
        self.items.pop(plan_id, None)


class _FakeQuery:
    """``db.query(Model)`` 的最小替身。

    只认仓储层真正用到的那几种写法：``.all()``、``.order_by(...).all()``、
    ``.filter(列 == 字面量).first()``、``.count()``。别的写法一律落到
    ``AttributeError``——假会话越像真会话，越容易让人误以为测过的是数据库。
    """

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self._criterion: Any = None
        self._limit: int | None = None
        self._offset: int = 0

    def filter(self, criterion: Any) -> _FakeQuery:
        self._criterion = criterion
        return self

    def order_by(self, *_args: Any) -> _FakeQuery:
        return self

    def limit(self, count: int) -> _FakeQuery:
        """只记上限，取行时才截断（顺序由 ``order_by`` 的语义决定，这里不模拟排序）。"""

        self._limit = count
        return self

    def offset(self, count: int) -> _FakeQuery:
        """分页起点：与 ``limit`` 一样只记参数，``all()`` 时才切片。

        审计列表（#82）在 SQL 里做 ``LIMIT`` / ``OFFSET``，所以假会话必须认识后者；
        只在取行时「先跳过 offset 再截 limit」，与真实 SQL 的语义一致。
        """

        self._offset = count
        return self

    def all(self) -> list[Any]:
        rows = [row for row in self._rows if self._matches(row)][self._offset :]
        return rows if self._limit is None else rows[: self._limit]

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

    ``deleted`` 是「这一行真的不在了」的记录（登出、删套餐）。假会话越像真会话，越容易
    让人误以为测过的是数据库，所以这里只实现仓储层真正用到的那几个方法。

    ``statements`` 按调用顺序记下 SQL 文本（见模块文档最后一段）。``EmptySession`` 不记：
    它本来就什么都不做。
    """

    def __init__(
        self,
        *,
        rows: dict[type, list[Any]] | None = None,
        tenant_ids: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._rows = {model: list(items) for model, items in (rows or {}).items()}
        self._tenant_ids = list(tenant_ids or [])
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.commits = 0
        self.closed = False
        self.rollbacks = 0
        self.statements: list[str] = []

    def query(self, model: type) -> _FakeQuery:
        return _FakeQuery(self._rows.get(model, []))

    def get(self, model: type, primary_key: Any) -> Any:
        for row in self._rows.get(model, []):
            if getattr(row, "id", None) == primary_key:
                return row
        return None

    def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _Rows | CountResult:
        """``existing_tenant_ids`` 的 union 查询返回预置值；``SELECT count(*)`` 真实数行。

        控制面「改套餐影响面」用 ``knowledge_base_repository.count_by_tenant`` 数库数，
        它走的是 ``execute(...).scalar_one()``。假会话如果只回空结果，「哪几个账号会超限」
        这一半就永远测不到。这里只模拟 ``SELECT count(*) ... WHERE 列 = 字面量`` 这一种
        形状，别的语句仍退回 ``_tenant_ids``（沿用本模块的「只实现被用到的那几种写法」）。

        **ORM 的 ``select(Model)`` 也认**（票 #129 起控制面的读走异步会话，异步会话只有
        ``execute(select(...))`` 这条路）：从预置行里按 ``WHERE`` 的等值条件过滤，再按
        ``OFFSET`` / ``LIMIT`` 切片。**不模拟排序**——与 ``_FakeQuery`` 同一条口径，用例
        自己按「查询会返回的顺序」摆预置行。
        """

        self._record(statement)
        if str(statement).lstrip().upper().startswith("SELECT COUNT("):
            return CountResult(self._count_rows(statement))
        model = _orm_model_of(statement)
        if model is not None:
            return _Rows(self._select_rows(statement, model))
        # 写语句（INSERT / UPDATE / DELETE）：替身不模拟行的变化（行走 ``add`` / ``delete``），
        # 只保证「这条语句确实发出去了」（``_record`` 已经记上）。
        sql = str(statement)
        verb = sql.lstrip().split(maxsplit=1)[0].upper() if sql.strip() else ""
        if verb in {"INSERT", "UPDATE", "DELETE"}:
            return _Rows([])
        # 原始读**只认得一种**：既有的 ``tenant_ids`` 视图（``SELECT id FROM knowledge_base``）。
        if verb == "SELECT" and "knowledge_base" in sql.lower():
            return _Rows(self._tenant_ids)
        # 认不出的**读**语句不猜：静默回一批行会让「这条查询写错了」变成一条绿着的用例。
        # 真会话对一条它解析不了的语句只会炸，替身照做。
        raise TypeError(
            f"假会话认不出这条读语句：{type(statement).__name__}。"
            "要么补一条等价的 ORM select，要么这个用例该用真库"
            "（见 tests/test_index_version.py 的 aiosqlite 写法）。"
        )

    def _select_rows(self, statement: Any, model: type) -> list[Any]:
        """``select(Model)`` → 预置行里匹配的那几条（过滤 + 切片，不排序）。"""

        rows = list(self._rows.get(model, []))
        criterion = getattr(statement, "whereclause", None)
        if criterion is not None:
            rows = [row for row in rows if _matches_where(row, criterion)]
        offset = getattr(statement, "_offset", None) or 0
        limit = getattr(statement, "_limit", None)
        rows = rows[offset:]
        return rows if limit is None else rows[:limit]

    def _record(self, statement: Any) -> None:
        """记下一条 SQL 文本（按调用顺序）。"""

        self.statements.append(str(statement))

    def _count_rows(self, statement: Any) -> int:
        try:
            table = statement.get_final_froms()[0]
        except (AttributeError, IndexError):
            return 0
        model = next(
            (candidate for candidate in self._rows if candidate.__tablename__ == table.name),
            None,
        )
        if model is None:
            return 0
        rows = self._rows.get(model, [])
        criterion = getattr(statement, "whereclause", None)
        if criterion is None:
            return len(rows)
        # 计数语句的 ``WHERE`` 与 ``select(Model)`` 同形状（可 ``AND`` 组合的等值 / 范围
        # 条件），于是复用同一份匹配逻辑：写两份的下场是计数与读行对同一条件给出不同答案。
        return sum(1 for row in rows if _matches_where(row, criterion))

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        # 让刚 add 的行之后还能被查到：真实会话里 flush 之后就是这个效果。
        self._rows.setdefault(type(obj), []).append(obj)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def delete(self, obj: Any) -> None:
        """真删行：登出与删套餐的可观测结果就是「这一行不在了」。

        同时记进 ``deleted``——断言「删掉的正是那一行」比只断言「提交了一次」准。
        """

        self.deleted.append(obj)
        rows = self._rows.get(type(obj), [])
        if obj in rows:
            rows.remove(obj)

    def close(self) -> None:
        self.closed = True

    def added_of(self, model: type) -> list[Any]:
        """本次真正写入的某类行；断言「只写了该写的表」比数总数更准。"""

        return [obj for obj in self.added if isinstance(obj, model)]


class TransactionalSession(RecordingSession):
    """有**事务语义**的假会话：提交才落库，回滚撤销这一笔里的全部写入。

    ``RecordingSession`` 是「写完就在那儿」的：它适合断言「该写哪些行」，但答不了
    「这一笔提交了吗」——而票 #124 要验的正是后者（审计与业务要么都有、要么都没有）。

    这里在第一次写入时给三份可观测状态拍一张快照（``_rows`` / ``added`` / ``deleted``），
    ``commit`` 丢掉快照（改动留下），``rollback`` 用快照复原（改动消失）。于是
    「服务中途抛错 → 整笔回滚」在离线套件里可以被直接看见，而不必连真库。

    快照在**第一次写入**时拍，不是构造时：事务入口开在请求里，而请求开始前
    ``_rows`` 里的预置行是「上一笔已经提交过」的数据，不该被这一笔的回滚带走。
    """

    def __init__(
        self,
        *,
        rows: dict[type, list[Any]] | None = None,
        tenant_ids: list[str] | None = None,
    ) -> None:
        super().__init__(rows=rows, tenant_ids=tenant_ids)
        self._snapshot: tuple[dict[type, list[Any]], list[Any], list[Any]] | None = None

    def _begin(self) -> None:
        if self._snapshot is None:
            self._snapshot = (
                {model: list(items) for model, items in self._rows.items()},
                list(self.added),
                list(self.deleted),
            )

    def add(self, obj: Any) -> None:
        self._begin()
        super().add(obj)

    def delete(self, obj: Any) -> None:
        self._begin()
        super().delete(obj)

    def commit(self) -> None:
        self._snapshot = None
        super().commit()

    def rollback(self) -> None:
        if self._snapshot is not None:
            rows, added, deleted = self._snapshot
            self._rows = {model: list(items) for model, items in rows.items()}
            self.added = list(added)
            self.deleted = list(deleted)
            self._snapshot = None
        super().rollback()


class AsyncSessionAdapter:
    """把一个**同步**替身包成异步面：请求会话用（第 11 步票 #127）。

    迁移期每个用例模块都有一份「只回答检索路径那几种语句」的假会话，而且各写各的。
    把它们逐个改写成异步类，等于让同一份语义存在两处；这里只加一层薄适配：
    ``await session.execute(...)`` 落到被包的那个替身上，状态（记了几条 SQL、关没关、
    加了几行）仍记在**同一个对象**里——于是用例里的断言一行都不用改。

    ``execute`` 必须是 ``async def``：忘了 ``await`` 的调用点会拿到一个协程对象，在套件里
    表现为「那段副作用没发生」，而不是静默通过。

    这不是给「异步会话」造第二个实现：真正的语义替身是 :class:`AsyncRecordingSession`
    （它与同步版共享同一份契约，见 ``tests/test_session_double_contract.py``）。这里包的
    是**每个用例自己的预置答案**，包一层只是为了让它们能被 ``await``。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        # 其余属性（``info`` / ``closed`` / ``executed`` / 用例自己的计数器）一律透传，
        # 断言因此仍然打在**同一个**对象上。
        return getattr(self._inner, name)

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.execute(*args, **kwargs)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.get(*args, **kwargs)

    async def delete(self, *args: Any, **kwargs: Any) -> Any:
        """``AsyncSession.delete`` 是要 ``await`` 的（登出、删套餐都走它）。

        不显式写成 ``async def`` 的话，``__getattr__`` 会把被包对象的**同步** ``delete``
        透传出去，调用点 ``await db.delete(row)`` 于是拿到 ``None`` 并炸成
        ``TypeError: object NoneType can't be used in 'await' expression``——而那条路径
        想断言的只是「这一行不在了」。
        """

        return self._inner.delete(*args, **kwargs)

    # 收尾三件套也必须是 ``async def``：票 #132 起数据面上传声明了请求级的
    # ``Depends(get_async_transaction)``，那条依赖在出块时 ``await session.commit()`` /
    # ``rollback()``。只透传同步方法的话，``await None`` 会在**被拒的请求**上炸成
    # ``TypeError``——而那条用例想断言的只是「回哪个业务码」。
    async def flush(self) -> Any:
        return self._inner.flush()

    async def commit(self) -> Any:
        return self._inner.commit()

    async def rollback(self) -> Any:
        return self._inner.rollback()

    async def close(self) -> Any:
        return self._inner.close()


class AsyncRecordingSession(RecordingSession):
    """``RecordingSession`` 的**异步面**：语义只写一遍，这里只把同一组方法变成可 ``await`` 的。

    **为什么继承而不是另写一份**：两套替身各写一遍语义，漂移是迟早的事（异步那份少记一条
    SQL、``rollback`` 只数了个寂寞，而用例全绿）。这里让状态与记录逻辑只有一份，异步替身
    只负责「这些方法必须被 ``await``」——于是「忘了 await」在套件里表现为
    ``RuntimeWarning: coroutine was never awaited`` 加上「那段副作用没发生」，而不是静默通过。

    共享契约测试见 ``tests/test_session_double_contract.py``：同一组操作在同步与异步替身上
    必须产生同一组 SQL 与同一组结果。

    ``query`` **故意留在同步面**：真 ``AsyncSession`` 没有 ``db.query(...)``，异步仓储只能走
    ``execute(select(...))``。替身如果替它兜底，迁移时那些「照抄同步写法」的地方就永远暴露
    不出来——那是 #127 起要显式改的代码。

    ``add`` **也留在同步面，而且必须留在同步面**：真 ``AsyncSession`` 上 ``add`` 是**唯一**
    一个仍是同步的写方法（``delete`` / ``flush`` / ``commit`` / ``rollback`` / ``close`` 全都
    要 ``await``）。替身照着把 ``add`` 写成 ``async def``，症状是「生产代码写了 ``db.add(row)``
    而用例全绿」——行其实没进去，因为那个协程根本没人 await。票 #128 迁移反馈写入时踩到了
    这一条，所以这里按真实 API 的形状钉住。

    **还没有 ``AsyncTransactionalSession``**：票 #124 的事务语义（提交才落库、回滚撤销写入）
    目前只有同步版 ``TransactionalSession``。异步写路径开始被断言的那一票按同样的形状加一个
    即可；先把「同一组方法可 await」这条地基铺好，不预先造第二个变体。
    """

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _Rows | CountResult:
        return super().execute(statement, *_args, **_kwargs)

    async def get(self, model: type, primary_key: Any) -> Any:
        return super().get(model, primary_key)

    async def delete(self, obj: Any) -> None:
        super().delete(obj)

    async def flush(self) -> None:
        super().flush()

    async def commit(self) -> None:
        super().commit()

    async def rollback(self) -> None:
        super().rollback()

    async def close(self) -> None:
        super().close()


def override_all_db(app: Any, session: Any) -> AsyncSessionAdapter:
    """把请求里的**全部**会话入口指向同一个假库（票 #130 之后控制面夹具的统一写法）。

    **为什么必须一次换全**：``Depends`` 与 ``dependency_overrides[...]`` 认的是可调用对象
    本身。控制面的身份判定（``get_console_account``）与写入的事务边界
    （``get_async_transaction``）都从 ``get_async_db`` 取会话，只覆写其中一条的夹具因此会
    **真的去连 PostgreSQL**：默认套件离线，症状是登录一律 ``20010``（读不到账号）或
    ``InterfaceError``（没有库可连），而不是「夹具少了一行」。

    **票 #134 起请求路径上只剩异步那两个入口**：``get_db`` / ``get_transaction`` 两个
    FastAPI 依赖已删除（同步引擎与 ``SessionLocal`` 仍在，只服务三处不在事件循环上的读者
    与 Alembic，见 ADR-0024 决策四），请求路径上没有第二种写法可换。``get_async_db`` 给读、
    ``get_async_transaction`` 给写，后一条**不另外造替身**——生产的事务入口只要会话上有
    ``info`` / ``commit`` / ``rollback`` 就能跑，于是「一笔请求 = 一笔事务、异常整笔回滚」
    这些纪律在离线套件里照旧被真实代码执行一遍，而不是被一个假的 no-op 事务糊过去。

    **异步那一面是同一个对象的薄适配**（:class:`AsyncSessionAdapter`），不是第二个替身：
    行、``added``、``statements``、``commits`` 全都记在**同一个**对象上，于是用例既有的
    断言（「只写了一张表」「提交了一次」）一行都不用改。

    返回那个异步视图，调用方一般不关心（夹具里保留它只是为了让「同一个库」这件事可读）。
    """

    from app.db.async_session import get_async_db, get_async_transaction

    view = AsyncSessionAdapter(session)
    app.dependency_overrides[get_async_db] = lambda: view
    # 事务入口用**生产实现**（它只依赖上面那个会话入口），这里显式置回默认值：
    # 上一个用例若把它换成了 no-op，``clear()`` 之外的残留会让这条写路径悄悄「没有事务边界」。
    app.dependency_overrides.pop(get_async_transaction, None)
    return view

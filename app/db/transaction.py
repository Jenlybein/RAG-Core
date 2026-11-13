"""写路径的**唯一**提交点：一笔业务写入 = 一笔事务。

全仓只有这一个模块允许调 ``session.commit()`` / ``session.rollback()``
（见 ``tests/test_transaction.py`` 的结构守卫）。别处的写法只有两种：

* **请求边界**：写接口在路由签名上声明 ``Depends(get_async_transaction)``
  （``app.db.async_session.get_async_transaction``）。一个请求恰好开一笔，成功出块提交、异常回滚，
  审计行与业务写入因此**要么都有、要么都没有**——不需要谁记得「先写业务再写审计」。
* **非请求边界**（Celery 任务、运维脚本、旁路短事务）：自己 ``with transaction(session):``
  圈住那一段。一个动作一笔事务，同样不许裸 ``commit``。

**为什么提交点必须集中**：第 11 步把请求路径从同步会话换成了异步会话——``session.commit()``
在这条路径上变成了 ``await session.commit()``。如果提交点散在三十处，换栈就是三十处
``async`` 传染；集中在这一个模块时，改的只是这个文件的孪生类（:class:`AsyncTransaction`）。
这不是洁癖，是「换栈」这件事的成本边界。同步的 :func:`transaction` 保留下来服务**不在请求
路径上**的两类调用者：运维脚本与旁路短事务（ADR-0024 决策四）。

**``after_commit`` 的动作现在分两种**：协程动作由 :class:`AsyncTransaction` ``await``
（删缓存、投递任务）；同步动作直接调用——但**不许在事件循环上做阻塞 I/O**，所以三处
Celery 投递统一走 ``index_dispatch.enqueue_document_after_commit``（``to_thread``）。
反过来，同步 :func:`Transaction` 收到协程动作会当场抛 ``TypeError``：它没法 ``await``，
静默丢掉那个协程的后果是「请求成功、副作用没发生」，宁可炸。

**为什么服务层不再各自提交**：一次请求里改了三项，第三项失败时，前两项必须跟着消失。
提交散在服务层时，每个服务只能保证「自己那一小段」，跨服务的原子性没人负责；收口到
请求边界之后，整笔请求自然是一笔事务。

**为什么副作用登记在 ``after_commit`` 上**：投递索引任务、删套餐缓存、重载档位注册表
这些动作**必须等库先落地**（任务跑得比提交快时，Worker 读不到那一行；缓存删得比提交早时，
另一个请求会把旧行读回来再写进缓存）。``after_commit`` 把它们挂在提交之后，而不是让每个
服务自己写「先 commit 再……」。

**为什么在会话上而不是全局变量里**：一笔事务属于**一个会话**。旁路短事务（归属索引、
反馈）在一个请求里另开会话，它的事务不能并进请求那一笔；挂在 ``session.info`` 上，
两笔各归各的，嵌套的入口自然并入外层。

**异步孪生（第 11 步票 #128）**：:class:`AsyncTransaction` 与 :class:`Transaction` 逐条
同构——同一份嵌套规则、同一份 ``after_commit`` 登记、同一个 ``session.info`` 键。差别只在
``commit`` / ``rollback`` 要 ``await``。**不并成一个类**：一个类里靠 ``isinstance`` 分叉
会让「这笔到底是哪一套」在运行期才可知，而两套的收尾动作（一个立刻返回、一个要 await）
恰恰是最不该猜的那一步。两份实现的风险是漂移，由 ``tests/test_transaction.py`` 的共享契约
用例守着（同一组操作 → 同一串事件）。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from types import TracebackType
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

#: ``session.info`` 里的键。``info`` 是 SQLAlchemy 给调用方留的字典，值随会话走。
_SESSION_KEY: Final[str] = "transaction"

#: 提交之后才允许执行的动作。返回 ``None`` 或一个协程：**同步与异步动作都收**——同步那
#: 一侧（Celery 投递、进程内注册表重载）原样调用；异步那一侧（删套餐缓存、重读档位表）
#: 交回一个协程，由 :meth:`AsyncTransaction.commit` ``await`` 掉。写成联合类型而不是两份
#: 登记表，是因为「提交之后做什么」这件事本身与它会阻塞多久无关。
AfterCommit = Callable[[], Any]


class NoTransactionError(RuntimeError):
    """在没有任何事务入口的会话上登记「提交后动作」。

    这是**编程错误**，不是运行期故障：写路径忘了在路由上声明 ``Depends(get_async_transaction)``，
    或者服务被一个不认识的调用方直接调用了。静默降级成「立即执行」会让「先提交再投递」
    这条纪律悄悄失效，而症状要等到某次 Worker 读不到行才出现。
    """


class Transaction:
    """一笔事务：正常出块提交，异常回滚，副作用登记在提交之后。

    可嵌套：内层 ``with`` 并入外层（``_depth`` 计数），只有最外层提交或回滚。嵌套存在的
    理由不是「允许到处开事务」，而是**运维脚本与任务可能已经在外层圈过一段**，内层再圈
    一次不该变成第二笔——否则「一个动作一笔事务」在调用链上就守不住了。
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._depth = 0
        self._after: list[AfterCommit] = []

    # ── 上下文管理

    def __enter__(self) -> Transaction:
        if self._depth == 0:
            # 挂到会话上：服务层靠 ``current(session)`` 找到这一笔。
            self._session.info[_SESSION_KEY] = self
        self._depth += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        self._depth -= 1
        if self._depth:
            # 内层：把异常交给外层决定提交还是回滚，自己不做任何收尾。
            return False
        self._session.info.pop(_SESSION_KEY, None)
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        # 异常照旧往上抛：错误码与响应形状由异常处理器决定，事务不吞任何东西。
        return False

    # ── 提交点

    def after_commit(self, action: AfterCommit) -> None:
        """登记一个「提交成功之后」才执行的动作；回滚时整批丢弃。"""

        self._after.append(action)

    def commit(self) -> None:
        """落库，然后按登记顺序执行提交后动作。

        提交失败时动作**不会**执行（异常从 ``session.commit()`` 抛出，列表原样留在
        这一笔上——它随后随会话关闭一起消失，不会有「提交失败却投递了任务」的窗口）。
        """

        self._session.commit()
        actions, self._after = self._after, []
        for action in actions:
            result = action()
            # 同步事务里**不可能** await：登记了一个协程动作（例如把异步投递写进了
            # ``with transaction(...)`` 的脚本里）时，那个协程会被直接丢掉——副作用静默
            # 不发生，而且不报错。宁可当场炸，也别让它悄悄消失。
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError(
                    "同步事务里登记了异步的 after_commit 动作：协程在这里无法 await，副作用会静默丢失。"
                    "请在异步事务入口（async_transaction）里登记，或把动作改成同步版本。"
                )

    def rollback(self) -> None:
        """丢弃这一笔的全部改动与全部提交后动作。"""

        self._after.clear()
        self._session.rollback()


def transaction(session: Session) -> Transaction:
    """事务入口：``with transaction(db):``。

    已经有一笔在跑时返回**同一笔**（并入外层），于是「请求边界 + 内层再圈一次」仍然只有
    一次提交。
    """

    opened = current(session)
    return opened if isinstance(opened, Transaction) else Transaction(session)


def commit_now(session: Session) -> None:
    """非请求边界的**显式**提交：``Tx1`` 这种「抢占后立刻落库、只持锁一瞬间」的场合用它。

    为什么还要这个函数，而不是让调用方直接 ``session.commit()``：提交点必须能一眼数清。
    全仓 ``session.commit()`` 只出现在本模块，别处要么走 ``with transaction(...)``，
    要么走这里——换异步栈时，要改的就是这两个函数。
    """

    session.commit()


def rollback_now(session: Session) -> None:
    """非请求边界的显式回滚（与 :func:`commit_now` 同一理由）。"""

    session.rollback()


class AsyncTransaction:
    """:class:`Transaction` 的**异步孪生**：``async with async_transaction(session):``。

    契约与同步那份逐条相同（正常出块提交一次、异常回滚并原样抛出、嵌套并入外层、
    ``after_commit`` 只在提交之后执行、登记在 ``session.info`` 上），只有 ``commit`` /
    ``rollback`` 需要 ``await``。它服务的正是**旁路短事务**（反馈、归属索引）：那些路径
    自己开会话、自己收尾，而它们的会话是 ``AsyncSession``——所以提交点必须也是异步的，
    否则那两条路径只能裸 ``await session.commit()``，把「提交点只有一个」这条纪律破掉。

    与同步版的**唯一**差别（票 #130）：提交后动作可以是协程。异步 Redis 的删除、异步
    会话上的重读都只能 ``await``，而它们又必须排在提交之后；在这里统一 await 掉，比让
    每个服务各自记着「先提交再 await」要可靠得多。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._depth = 0
        self._after: list[AfterCommit] = []

    # ── 上下文管理

    async def __aenter__(self) -> AsyncTransaction:
        if self._depth == 0:
            self._session.info[_SESSION_KEY] = self
        self._depth += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        self._depth -= 1
        if self._depth:
            return False
        self._session.info.pop(_SESSION_KEY, None)
        if exc_type is None:
            await self.commit()
        else:
            await self.rollback()
        # 与同步版同一口径：事务不吞异常，错误码与响应形状由异常处理器决定。
        return False

    # ── 提交点

    def after_commit(self, action: AfterCommit) -> None:
        """登记一个「提交成功之后」才执行的动作；回滚时整批丢弃。"""

        self._after.append(action)

    async def commit(self) -> None:
        """落库，然后按登记顺序执行提交后动作（提交失败时动作一个都不执行）。

        动作**可以返回协程**（票 #130）：删套餐缓存、重读档位表这些副作用背后是异步
        Redis / 异步会话，它们必须在同一个事件循环上 ``await``。同步动作（Celery 投递、
        进程内注册表写入）照旧直接调用——这里按返回值判断，不按动作的来源判断，于是
        「这个动作是同步还是异步」不需要登记方多写一行。
        """

        await self._session.commit()
        actions, self._after = self._after, []
        for action in actions:
            result = action()
            if inspect.isawaitable(result):
                await result

    async def rollback(self) -> None:
        """丢弃这一笔的全部改动与全部提交后动作。"""

        self._after.clear()
        await self._session.rollback()


def async_transaction(session: AsyncSession) -> AsyncTransaction:
    """异步事务入口：``async with async_transaction(db):``；已有一笔时并入外层。"""

    opened = current(session)
    return opened if isinstance(opened, AsyncTransaction) else AsyncTransaction(session)


async def async_commit_now(session: AsyncSession) -> None:
    """非请求边界的**显式**异步提交（:func:`commit_now` 的孪生，同一理由）。"""

    await session.commit()


async def async_rollback_now(session: AsyncSession) -> None:
    """非请求边界的显式异步回滚（与 :func:`async_commit_now` 同一理由）。"""

    await session.rollback()


def current(session: Session | AsyncSession) -> Transaction | AsyncTransaction | None:
    """这个会话上正在跑的那一笔；不在任何入口里时是 ``None``。"""

    found = session.info.get(_SESSION_KEY)
    return found if isinstance(found, (Transaction, AsyncTransaction)) else None


def after_commit(session: Session | AsyncSession, action: AfterCommit) -> None:
    """把 ``action`` 登记到**当前这一笔**的提交之后。

    没有当前事务时抛 :class:`NoTransactionError`：那条写路径缺了事务边界，先补边界
    （路由加 ``Depends(get_async_transaction)``，或脚本里 ``with transaction(db):``）。
    """

    opened = current(session)
    if opened is None:
        raise NoTransactionError(
            "after_commit 只能在事务入口里登记：路由声明 Depends(get_async_transaction)，"
            "或脚本/任务用 with transaction(db) 圈住这段写路径"
        )
    opened.after_commit(action)


__all__ = [
    "AfterCommit",
    "AsyncTransaction",
    "NoTransactionError",
    "Transaction",
    "after_commit",
    "async_commit_now",
    "async_rollback_now",
    "async_transaction",
    "current",
    "transaction",
]

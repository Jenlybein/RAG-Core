"""双栈期的会话桥：一段异步仓储代码，同步会话与异步会话都能喂（**临时接缝**）。

## 为什么需要它

第 11 步按**垂直切片**换栈，但有几处「同一条写路径被两个入口共用」，两个入口不在同一
张票里：

| 共用的函数 | 已经换栈的入口（本票 #132） | 还没换栈的入口（#130 控制面写） |
| --- | --- | --- |
| ``document_service.upload`` | ``POST /documents/upload``（``AsyncSession``） | ``POST /console/kbs/{id}/documents``（同步 ``Session``） |
| ``purge_document_chunks`` | ``DELETE /documents/{id}``（本票一并换） | ``console_kb_service.delete_document``（同步 ``Session``） |

两条路都必须走**同一份实现**——控制面另写一套是这条链路历史上最贵的错（见
``app/services/console_kb_service.py`` 模块文档）。于是这一层只做一件事：把
``execute`` / ``flush`` / ``get`` 三个原语对齐成「可 await 的同一句话」，会话是同步还是
异步由它判断。仓储层因此只有**一份**代码，而不是同步、异步各一份靠自觉保持同步。

## 这不是「两条读写路径」

两条路径读写的仍是同一张表、同一段业务代码、同一笔事务边界；变的只有会话对象。这一点
与「同一张表同时存在同步与异步两套仓储实现」不同：那种形态迟早漂移成两个真相，这一层
恰恰是为了避免它。

## 删除条件（写在前面，别让它活过双栈期）

票 #130（控制面写异步）落地后：控制面拿到的也是 ``AsyncSession``，本模块的三个函数退化成
「直接 ``await``」。届时**删掉本模块**，让 ``mypy`` 把每一个 ``Session | AsyncSession``
签名指出来，逐个收窄成 ``AsyncSession``——这正是「先集中、后收窄」的顺序。

## 为什么按「结果可不可 await」判断，而不是 ``isinstance(db, AsyncSession)``

按类型分叉会让**异步替身**走错分支：套件里的异步假会话不是 ``AsyncSession`` 的实例，
``isinstance`` 于是判成同步、把 ``flush`` 的协程丢掉——症状只有一条
``RuntimeWarning: coroutine ... was never awaited``，而「那段副作用没发生」会被断言漏过去
（``tests/fake_db.py`` 开头讲的就是这类假绿）。按结果是否可等待判断，真会话与替身走同一条
分支；同步会话的 ``execute`` / ``flush`` / ``get`` 本来就返回非 awaitable，判定不会误伤。

## 为什么不写成「看类型再 await」散落在各处

分叉只出现在这一个文件里：业务代码读起来与真异步一致，而「这里在兼容同步会话」这件事
只有一个地方需要记住、一个地方需要删除。
"""

from __future__ import annotations

import inspect
from typing import Any, cast

from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql import Executable

#: 迁移期仓储/服务可以接受的会话类型（收窄成 ``AsyncSession`` 是票 #130 之后的收尾）。
DBSession = Session | AsyncSession


async def execute(db: DBSession, statement: Executable) -> CursorResult[Any]:
    """执行一条语句并等结果：异步会话 ``await``，同步会话直接返回。

    返回类型写成 ``CursorResult`` 而不是 ``Result``：CAS 那几条 ``UPDATE`` 要读
    ``rowcount``（``Result`` 协议上没有它）。两种会话在这条路径上返回的都是它。
    """

    result: Any = db.execute(statement)
    if inspect.isawaitable(result):
        result = await result
    return cast("CursorResult[Any]", result)


async def flush(db: DBSession) -> None:
    """把挂起的写入推给数据库（**不提交**：提交点仍只在 ``app.db.transaction``）。"""

    result: Any = db.flush()
    if inspect.isawaitable(result):
        await result


async def get(db: DBSession, model: type[Any], ident: Any) -> Any:
    """按主键取一行；``None`` 表示不存在。"""

    result: Any = db.get(model, ident)
    if inspect.isawaitable(result):
        return await result
    return result


__all__ = ["DBSession", "execute", "flush", "get"]

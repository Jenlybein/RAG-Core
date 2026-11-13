"""索引任务投递缝：T3 定形状，T4 提供被投递的任务。

任务模块在函数内**惰性 import**，所以路由层与上传业务在 T3 阶段不依赖 Celery 是否
已经就位；测试一律 monkeypatch 本函数，不需要 Broker。

并发去重**不是**靠 Celery 的 ``task_id``：真正的去重由数据库 CAS
（``document_repository.try_claim_for_indexing``）保证；``task_id`` 只为日志 / 追踪
可读，别让后人误以为 Celery 会按它去重。

## 为什么还有一个 ``enqueue_document_after_commit``

``apply_async`` 是 Celery 的**同步** API（内部是 broker 的阻塞写），而 ``after_commit``
的回调在**事件循环上**执行——``AsyncTransaction.commit`` 会 ``await`` 协程动作，却会
直接调用同步动作。于是「上传成功」这条最热的写路径会在循环上做一次 Redis 写，正是
端到端异步要消灭的形状（票 #137 的评审抓到的）。

这个包装把阻塞的 publish 交给 ``asyncio.to_thread``：循环继续转，语义（参数、task_id、
投递顺序）一字不变。同步的 :func:`enqueue_document` 保留给**运维脚本**（独立进程、同步
到底）与测试注入缝；两者参数形状必须完全一致，差异只在「等不等」。

**翻案条件**：一旦 Celery 提供可 await 的发布通道，这一层就该消失。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol


class TaskPublisher(Protocol):
    """Celery 任务对象上我们只用这一个方法；测试注入一个记账实现即可。"""

    def apply_async(self, *, kwargs: dict[str, Any], task_id: str) -> Any: ...


#: 可注入的**底层**发布器（不引 Celery 依赖链）。``None`` = 生产实现，第一次用时构造。
#: 这里而不是在 ``enqueue_document`` 上打桩，是为了让 ``kwargs`` 组装与 ``task_id`` 形状
#: **仍然被真实代码执行**——否则「投递接线」从来没被测过，只在 CI 上以 Connection refused
#: 的形式暴露（票 #137 的评审发现的第二种掩盖）。
_publisher: TaskPublisher | None = None


def set_task_publisher(publisher: TaskPublisher | None) -> None:
    """注入发布器（离线套件设为记账版；``None`` 复原生产实现）。"""

    global _publisher
    _publisher = publisher


def get_task_publisher() -> TaskPublisher:
    """取当前发布器；未注入时构造生产实现（惰性，不连 Broker 直到 ``apply_async``）。"""

    return _publisher if _publisher is not None else _default_publisher()


def _default_publisher() -> TaskPublisher:
    from app.tasks.indexing import index_document_task

    return index_document_task


def enqueue_document(
    *,
    document_id: str,
    tenant_id: str,
    kb_id: str,
    request_id: str | None,
    reparse: bool = False,
) -> None:
    """把索引任务投递到 Broker，带上请求关联标识以便 Worker 续用同一个号牌。"""

    publisher = _publisher if _publisher is not None else _default_publisher()
    publisher.apply_async(
        kwargs={
            "document_id": document_id,
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "request_id": request_id,
            "reparse": reparse,
        },
        task_id=f"doc:{document_id}",
    )


async def run_publish_off_loop(publish: Callable[..., Any], **kwargs: Any) -> None:
    """把一次同步 publish（Celery ``apply_async``）挪出事件循环。

    这是**唯一**一处 ``to_thread``：所有「提交之后投递」的入口共用它，避免每个模块各写
    一遍、其中一处忘了就又在循环上做 broker I/O。``publish`` 由调用方传进来（而不是在
    这里 import），于是各模块对 ``enqueue_document`` 的 ``monkeypatch`` 照旧有效——
    **离线替身不需要连 Broker 这件事，不能被这层包装破坏**。
    """

    await asyncio.to_thread(publish, **kwargs)


async def enqueue_document_after_commit(
    *,
    publish: Callable[..., Any],
    document_id: str,
    tenant_id: str,
    kb_id: str,
    request_id: str | None,
    reparse: bool = False,
) -> None:
    """``after_commit`` 用的异步版本：参数与 :func:`enqueue_document` 逐字一致。

    ``publish`` **必填**且必须由调用方传自己模块里的 ``enqueue_document`` 绑定：各模块
    （``document_service`` / ``console_kb_service`` / ``routes.documents``）都是
    ``from ... import enqueue_document``，测试 monkeypatch 的是**那些模块的属性**。若这里
    缺省用本模块的绑定，替身就绕不过去，用例会在 ``to_thread`` 里真连 Broker——那正是
    这层包装最不该制造的假绿。
    """

    await run_publish_off_loop(
        publish,
        document_id=document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        request_id=request_id,
        reparse=reparse,
    )

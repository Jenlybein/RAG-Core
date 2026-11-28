"""任务投递：形状、离线注入点，以及「提交之后投递别占事件循环」的包装。

任务模块在函数内**惰性 import**，所以路由层与上传业务不依赖 Celery 是否已经就位。

并发去重**不是**靠 Celery 的 ``task_id``：真正的去重由数据库 CAS
（``document_repository.try_claim_for_indexing``）保证；``task_id`` 只为日志 / 追踪
可读，别让后人误以为 Celery 会按它去重。

## 两条投递缝

- :func:`enqueue_document` —— 索引任务（``index_document_task``）；
- :func:`app.services.side_evidence_dispatch.enqueue_side_evidence` —— 旁证跑批任务。

整库构建的「派活 + 构建子任务」两条缝已随库级版本层删除：全库重建改成**逐篇投递**
单篇重建任务（复用本模块这一条缝），于是只剩一条索引缝。

两条缝各持一个 :class:`PublisherSlot`。**槽是注入点**：离线套件把生产任务对象换成记账
版，于是「``kwargs`` 组装 + ``task_id`` 形状」仍由真实代码执行，而 broker 一次都不碰。
桩打在 ``apply_async`` 那一层、不是打在 ``enqueue_document`` 上：后者会把接线本身也盖掉，
症状是默认套件里「投递」从来没被断言过，只在没有 Redis 的 CI 上以 ``Connection refused``
暴露（票 #137 的评审发现的第二种掩盖）。

## 为什么还有一个 ``enqueue_document_after_commit``

``apply_async`` 是 Celery 的**同步** API（内部是 broker 的阻塞写），而 ``after_commit``
的回调在**事件循环上**执行——``AsyncTransaction.commit`` 会 ``await`` 协程动作，却会
直接调用同步动作。于是「上传成功」这条最热的写路径会在循环上做一次 Redis 写，正是
端到端异步要消灭的形状（票 #137 的评审抓到的）。

这个包装把阻塞的 publish 交给 ``asyncio.to_thread``：循环继续转，语义（参数、task_id、
投递顺序）一字不变。同步的 :func:`enqueue_document` 保留给**运维脚本**（独立进程、同步
到底）。**翻案条件**：一旦 Celery 提供可 await 的发布通道，这一层就该消失。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol


class TaskPublisher(Protocol):
    """Celery 任务对象上我们只用这一个方法；测试注入一个记账实现即可。"""

    def apply_async(self, *, kwargs: dict[str, Any], task_id: str) -> Any: ...


class PublisherSlot:
    """一个可注入的发布器槽：未注入时按需构造生产实现（惰性，不连 Broker）。

    两条缝各持一个槽（索引 / 旁证），而不是共用一个可变全局：共用一个时「这次投递到底
    该唤醒哪个任务」就失去断言力，两者的任务号牌形状也不一样。
    """

    def __init__(self, factory: Callable[[], TaskPublisher], *, name: str) -> None:
        self._factory = factory
        self._name = name
        self._injected: TaskPublisher | None = None

    @property
    def name(self) -> str:
        return self._name

    def set(self, publisher: TaskPublisher | None) -> None:
        """注入发布器（离线套件设为记账版；``None`` 复原生产实现）。"""

        self._injected = publisher

    def current(self) -> TaskPublisher:
        """当前生效的发布器：注入的优先，否则构造生产实现。"""

        if self._injected is not None:
            return self._injected
        return self._factory()

    def is_injected(self) -> bool:
        """是否处于「注入替身」状态——离线套件用它自检，不去摸私有字段。"""

        return self._injected is not None


def _index_task() -> TaskPublisher:
    from app.tasks.indexing import index_document_task

    return index_document_task


#: 索引任务的发布器槽。离线套件通过 :func:`set_task_publisher` 注入记账版。
_index_slot = PublisherSlot(_index_task, name="index")


def set_task_publisher(publisher: TaskPublisher | None) -> None:
    """注入索引任务的发布器（离线套件设为记账版；``None`` 复原生产实现）。"""

    _index_slot.set(publisher)


def get_task_publisher() -> TaskPublisher:
    """取索引任务当前生效的发布器（注入的优先，否则生产实现）。"""

    return _index_slot.current()


def index_publisher_is_injected() -> bool:
    """索引投递缝是否处于注入状态（给离线自检用）。"""

    return _index_slot.is_injected()


def enqueue_document(
    *,
    document_id: str,
    tenant_id: str,
    kb_id: str,
    request_id: str | None,
    reparse: bool = False,
    doc_version: int | None = None,
) -> None:
    """把索引任务投递到 Broker，带上请求关联标识以便 Worker 续用同一个号牌。

    ``doc_version`` 是**这次索引算的是哪一稿**（#173），跟着载荷一起走：投递方知道
    就用它（路由 / 脚本在投递那一刻读指针），不知道才留 ``None``——那时 Worker 回落
    文档行的当前稿指针。**绝不能反过来「让 Worker 自己读」**：指针是活数据，回滚刚
    切完它时，一个迟到的任务会把别的稿写进来。

    键始终出现在载荷里（哪怕值是 ``None``）：任务记录里因此一眼看得出「这次投递有没有
    指定稿号」，而不是靠键的缺席去表达。

    全库重建不再有另一种任务形状：它就是把这一条按篇投 N 次（见
    ``console_kb_service.rebuild_kb``）。
    """

    get_task_publisher().apply_async(
        kwargs={
            "document_id": document_id,
            "tenant_id": tenant_id,
            "kb_id": kb_id,
            "request_id": request_id,
            "reparse": reparse,
            "doc_version": doc_version,
        },
        task_id=f"doc:{document_id}",
    )


async def enqueue_document_after_commit(
    *,
    publish: Callable[..., Any],
    document_id: str,
    tenant_id: str,
    kb_id: str,
    request_id: str | None,
    reparse: bool = False,
    doc_version: int | None = None,
) -> None:
    """``after_commit`` 用的异步版本：参数与 :func:`enqueue_document` 逐字一致。

    ``publish`` **必填**且必须由调用方传自己模块里的 ``enqueue_document`` 绑定：各模块
    （``document_service`` / ``console_kb_service`` / ``routes.documents``）都是
    ``from ... import enqueue_document``，测试 monkeypatch 的是**那些模块的属性**。若这里
    用本模块的绑定，替身就绕不过去，用例会在 ``to_thread`` 里真连 Broker——那正是这层
    包装最不该制造的假绿。
    """

    await asyncio.to_thread(
        publish,
        document_id=document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        request_id=request_id,
        reparse=reparse,
        doc_version=doc_version,
    )

"""去向的**最小协议**：记录 trace → 记录 span(name, duration_ms) → flush（#102）。

三个实现都满足它，而且都不需要主链路知道自己是谁：

* :class:`NullTraceSink`：什么都不做（测试与 CI 的缺省）；
* ``app.observability.jsonl_sink.JsonlTraceSink``：一次检索一行，落本地文件；
* ``app.observability.langfuse_sink.LangfuseTraceSink``：走 SDK v5，注入我们自己派生的
  ``trace_id``。

**协议刻意小**：三个方法、四个参数，没有「打开 / 关闭 / 采样 / 重试」。多一个方法，
主链路就多一处可能被某个实现忘掉的分支；而观测层的能力差异应该体现在实现里，不该
体现在调用点的 ``if`` 上。

``trace_id`` 每个方法都要带：去向是共享的（一个进程一个实例），而一次请求一条 trace。
把「当前是哪条 trace」存在实现里，等于让并发请求互相覆盖——那是最难复现的一类错。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class TraceSink(Protocol):
    """一个去向：收下这次检索的事实，按自己的方式落地。

    实现**可以**抛异常（连不上、目录不可写、序列化失败）：门面
    ``app.observability.emit_retrieval_trace`` 会把它们吞成一条 ``warning``。实现里再
    兜一层会让「这个去向真的坏了」变成静默，那条 warning 正是运维唯一能看到的信号。
    """

    def record_trace(self, trace_id: str, attributes: Mapping[str, object]) -> None:
        """记下这条 trace 的结构化事实（模式、条数、降级 / 空结果原因）。

        **不含原文**，也不含切片内容：这里是旁路，不是第二份业务数据。
        """
        ...

    def record_span(self, trace_id: str, name: str, duration_ms: float) -> None:
        """记一段耗时。``name`` 取自 ``stage_timing`` 的六个常量。"""
        ...

    def flush(self, trace_id: str) -> None:
        """把这条 trace 交出去（写盘 / 推给观测服务 / 什么都不做）。"""
        ...


class NoneTraceSink:
    """空操作：不写文件、不建连接、不发网络。

    它是「未接入观测」这个词的实现：选它时链路代码一行不改，六段**照样被计时**
    （收集器与去向是两件事），只是这份计量没人收下。测试与 CI 靠它把「这台机器上恰好
    起着什么」从结果里摘出去。
    """

    def record_trace(self, trace_id: str, attributes: Mapping[str, object]) -> None:
        return None

    def record_span(self, trace_id: str, name: str, duration_ms: float) -> None:
        return None

    def flush(self, trace_id: str) -> None:
        return None

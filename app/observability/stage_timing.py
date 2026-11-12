"""六段耗时的**收集器**：显式传进链路，不是一个全局开关（#102）。

为什么是一个传进去的对象，而不是「观测开着就顺手记到某个模块级列表里」：

* **度量常开**。任何配置下六段都会计时（父 spec「度量常开」）——开关只决定这份计量
  **送到哪**。如果计时依赖「观测是否开启」，那么切到 ``none`` 时耗时就不再被算出，
  「关掉观测等于关掉计时」这条正是本票要断掉的东西。
* **没有全局可变状态**。一次检索一份收集器，跟着参数传到需要计时的那几层；两次并发
  请求的 span 因此不会串到一个列表里，测试也不必在用例之间清场。
* **不认识的段名不抛**。收集器在旁路上：一个拼错的段名最多让这一行少一段可读的名字，
  绝不能因此让检索失败（ADR-0021 决策 1 的边界）。

段名与顺序是**契约**（``GLOSSARY.md``「span 阶段」）：观测栈按名字聚合，改名字等于改
契约，所以它们在这里只写一遍，别的模块引用常量，不复制字符串。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

#: 查询优化：召回之前把用户那句话加工成「向量逐句 + 关键词一句 + 重排一句」。
#: 关闭改写时这一步是恒等变换，但它**仍然发生**（做了一次判断），所以仍然计时。
STAGE_QUERY_OPTIMIZATION: Final[str] = "query_optimization"
#: 向量召回：逐句嵌入 + 一次范围召回（混合模式里在独立线程里跑）。
STAGE_VECTOR_RECALL: Final[str] = "vector_recall"
#: 关键词召回：一次全局关键词查询。
STAGE_KEYWORD_RECALL: Final[str] = "keyword_recall"
#: 融合：一次 RRF。只有混合模式有这一步。
STAGE_FUSION: Final[str] = "fusion"
#: 重排：只有「这次要重排」且真调了 Provider 才有这一步；关闭时不记录。
STAGE_RERANK: Final[str] = "rerank"
#: 折叠：把命中的小片折成交付单元（ADR-0013）。
STAGE_COLLAPSE: Final[str] = "collapse"

#: 报告顺序 = 链路顺序（GLOSSARY「检索链路」）。它**不是**完成先后：两路召回并行，
#: 谁先返回取决于网络抖动，而同一份输入的观测行不该因此变样。
STAGE_ORDER: Final[tuple[str, ...]] = (
    STAGE_QUERY_OPTIMIZATION,
    STAGE_VECTOR_RECALL,
    STAGE_KEYWORD_RECALL,
    STAGE_FUSION,
    STAGE_RERANK,
    STAGE_COLLAPSE,
)

#: 段名 → 报告位置。不认识的段名排在最后（稳定排序，不抛）：拼错一个段名只该让这一行
#: 难读，不该让这次检索失败。
_ORDER_INDEX: Final[dict[str, int]] = {name: index for index, name in enumerate(STAGE_ORDER)}


@dataclass(frozen=True)
class StageSpan:
    """一段的耗时：只有名字与毫秒数，**不含任何正文**（ADR-0021 决策 1）。"""

    name: str
    duration_ms: float


class StageTimer:
    """一次检索的六段计时；由组合根创建、显式传给每一层。

    **第 11 步票 #127 起六段都记在同一个事件循环上**：向量分支不再进检索执行池
    （``app.core/loop_pool.py`` 已删除），所以「两路并行」不再意味着「两个线程写同一个
    收集器」。那把锁留着只是防御——``record`` 也可能被评测脚本或未来的旁路从别的执行
    上下文调到，而多一把锁的代价是零。**不要**据此以为这里还允许跨线程写。
    """

    def __init__(self) -> None:
        self._spans: list[StageSpan] = []
        self._lock = threading.Lock()

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        """给一段计时；这一段抛异常时也记下已花的时间，然后把异常原样放走。

        ``finally`` 里记录而不是 ``except`` 里记录：失败的那一段同样花了时间，而观测层
        对「成功与否」不持意见（那是响应与日志的事）。
        """

        started = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, (time.perf_counter() - started) * 1000)

    def record(self, name: str, duration_ms: float) -> None:
        """直接记一段耗时（已经量好的那些段用它，例如重排的自带 ``duration_ms``）。"""

        with self._lock:
            self._spans.append(StageSpan(name=name, duration_ms=round(duration_ms, 3)))

    def spans(self) -> tuple[StageSpan, ...]:
        """按链路顺序交出已记录的段：**没发生的段不会出现在这里**，也不补 0。"""

        with self._lock:
            collected = list(self._spans)
        return order_spans(collected)


def order_spans(spans: Iterable[StageSpan]) -> tuple[StageSpan, ...]:
    """按链路顺序排一组段；不认识的段名稳定地排在最后（不抛）。

    读回通道（控制台瀑布，#103）也要这一份顺序：观测栈按 ``start_time`` 回给我们的顺序，
    与文件里写下的顺序，都只是「什么时候记的」；而要看的是**链路顺序**——两路召回并行，
    谁先返回取决于网络抖动，同一份输入的瀑布不该因此变样。顺序只有这一份实现，
    调用点不各自 ``sorted``。
    """

    return tuple(sorted(spans, key=lambda span: _ORDER_INDEX.get(span.name, len(STAGE_ORDER))))

"""``langfuse`` 去向：把六段耗时送进自托管观测栈（SDK v5，#102）。

两条硬约束来自 ADR-0021 与 ADR-0010「第五次修订」：

1. **``trace_id`` 我们给，SDK 不生成**。它是 ``app.services.trace_identity.trace_id_for``
   由 ``log_id`` 派生的那根线；这一层通过 ``trace_context={"trace_id": ...}`` 把它注入。
   让 SDK 另生成一个，就等于响应里那根线与栈里那条 trace 之间要再维护一张对照表——
   而「同一根线」正是这一步要证明的东西。
2. **连不上不算失败**。这一层的任何异常都由门面吞成 ``warning``；检索照常返回，
   ``trace_id`` 照样有值。

**这里是「事后补记」，不是「包住真实调用」**：六段耗时在链路里已经量完了
（``StageTimer``），观测层拿到的是名字与毫秒数。所以每段 span 的**真实耗时写在
``metadata.duration_ms`` 里**，SDK 自己量到的那段接近 0。为什么不用 ``start_observation``
先开一个再 ``end()`` 补时间：那要求一个具体的时钟单位（毫秒 / 纳秒 / FILETIME）猜测，
猜错就静默偏移；而 ``duration_ms`` 是我们自己算出来的数，读的人不会读错。

SDK 只在**第一次真正要用**时构造与 import：``none`` / ``jsonl`` 去向与离线套件因此
既不加载 langfuse，也不读它那几个配置项。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.config import settings


class LangfuseTraceSink:
    """SDK v5 去向：每段一个 span，全部挂在注入的 ``trace_id`` 上。

    ``client`` 是测试接缝：给定一个记录用的假客户端，这条路径就能在不发网络、不起栈的
    情况下被逐项断言（``as_type`` / ``name`` / ``trace_context`` / ``metadata``）。生产
    路径传 ``None``，第一次 ``record_span`` 时才构造真客户端。
    """

    def __init__(self, *, client: Any | None = None) -> None:
        self._client = client
        # trace 级事实（模式、条数、结局）。它们在栈里没有单独的落点，所以随每一段一起
        # 带过去：按 trace_id 聚合时，读的人不必先找到某一根「主 span」才能知道这次是什么。
        self._attributes: dict[str, dict[str, object]] = {}
        # 原文单独一份（#110 复盘后补）。它**只进观测栈**：低分样本导出要靠它把一次差评
        # 变回一道评测题。不混进 ``_attributes``，是因为后者的语义是「这次检索的结构化
        # 事实」；原文塞进去，读的人就分不清哪些字段是结论、哪些是输入了。
        self._queries: dict[str, str] = {}

    # ── 协议 ──
    def record_trace(
        self,
        trace_id: str,
        attributes: Mapping[str, object],
        *,
        query: str | None = None,
    ) -> None:
        self._attributes[trace_id] = {
            key: value for key, value in attributes.items() if value is not None
        }
        if query:
            self._queries[trace_id] = query

    def record_span(self, trace_id: str, name: str, duration_ms: float) -> None:
        client = self._client_or_create()
        metadata: dict[str, object] = {"duration_ms": duration_ms}
        metadata.update(self._attributes.get(trace_id) or {})
        # 原文随**每一段**带过去，而不是只挂第一段：v5 移除了 trace 级 IO setter
        # （``set_current_trace_io`` 已删），trace 的 input 由根 observation 推出来，
        # 而六段是并列的兄弟 span、没有根。重复六份的代价是几十字节，换来的是
        # 「导出器读任意一段都拿得到」。
        question = self._queries.get(trace_id)
        if question:
            metadata["query"] = question
        # ``trace_context`` 是我们注入的那根线；``as_type="span"`` 让它在栈里是一段而不是
        # 一次模型调用（六段里没有一段是 generation）。
        with client.start_as_current_observation(
            as_type="span",
            name=name,
            trace_context={"trace_id": trace_id},
            metadata=metadata,
        ):
            pass

    def record_score(self, trace_id: str, score: int, *, comment: str | None = None) -> None:
        """把反馈分推成一条 trace score（#105 决策 2 的旁路）。

        SDK v5 里 ``create_score`` 仍在（v5 只改了 ``api.scores.create`` 的签名，这个便捷
        方法是 ``span.score()`` 同一族）；``name="user_feedback"`` 与 ADR 里写的名字一致，
        栈里按它筛「≤3 分的 trace」就是低分样本导出的上游。
        """

        client = self._client_or_create()
        client.create_score(
            trace_id=trace_id,
            name="user_feedback",
            value=score,
            comment=comment,
        )

    def flush(self, trace_id: str) -> None:
        self._attributes.pop(trace_id, None)
        self._queries.pop(trace_id, None)
        # 没建过客户端就不必 flush：那说明这条 trace 一段都没记上（或去向是空实现）。
        if self._client is not None:
            self._client.flush()

    # ── 客户端 ──
    def _client_or_create(self) -> Any:
        if self._client is None:
            self._client = _new_client()
        return self._client


def _new_client() -> Any:
    """按配置构造 SDK 客户端；import 也延迟到这里（见模块文档最后一段）。"""

    from langfuse import Langfuse

    return Langfuse(
        public_key=settings.LANGFUSE_PUBLIC_KEY,
        secret_key=settings.LANGFUSE_SECRET_KEY,
        host=settings.LANGFUSE_HOST,
    )

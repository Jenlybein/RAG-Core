"""用量记账：**被拒也要落一行**（第 14 步票 #200）。

## 它解决什么

``usage_events`` 原来只在**成功路径的末尾**写一行，于是「成功率」没有分母：
被挡下的请求（``20013`` 越权 / ``20014`` 额度用尽 / ``20033`` 问题无内容）在门禁里就抛掉了，
库里一个字都没有。这一层只负责**拒绝侧**那一行；成功侧的行仍然写在原来的地方
（``rag_service`` / ``decompose_gate`` / ``research_gate``），因为那些函数手里才有
「这次交付了哪些片、哪几稿」——把成功侧也搬过来，就得把交付物再解析一遍，等于制造第二份真相。

## 为什么包在门禁函数里，而不是包在路由上

两个入口（HTTP 与 MCP）调的是同一批 ``gated_*`` 函数。包在路由上，MCP 的用量就永远不进账；
包在门禁里，两个入口自动都算。

## 三条不能破的纪律（沿用 ``record_retrieval_trace``）

1. **独立短事务**：不能借用请求会话（它从不 commit）；
2. **永不抛**：写不进去只留 warning，不许把一次「本该 20013 的响应」变成 500；
3. **不写原文**：只有 id、端点、业务码、耗时与三本账计数。

## 落不下的两类请求（有意为之）

- **``20010`` 鉴权失败**：没有身份就没有租户与 Key，一行账无从归属；而且它**不需要通过鉴权**，
  落行等于给未认证流量开一条写库通道。这类请求属于「门口之外」，要看就看反代日志。
- **非业务异常**（真 500）：没有业务码可记，记 ``0`` 是撒谎。它们由 ``error.log`` 与告警负责。

这两条写进了 ADR-0021 的修订与术语表。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import structlog

from app.core.auth import KeyContext
from app.services.retrieval_trace import record_retrieval_trace
from app.services.trace_identity import trace_id_for
from app.utils.id_generator import new_retrieval_log_id

logger = structlog.get_logger(__name__)

#: 「这一次请求是哪把 Key」。用 ``contextvar`` 而不是函数参数：组合根
#: ``rag_service.retrieve`` 的调用方有三类（门禁、MCP、内部评测），往它的签名里加一个参数
#: 会让每个替身与包装都跟着改；而这条信息只对「写账」有用，不该出现在检索的接口上。
#: 异步上下文天然隔离，并发请求不会串。
_current_key_id: ContextVar[str | None] = ContextVar("usage_key_id", default=None)


@contextmanager
def current_key(key_id: str | None) -> Iterator[None]:
    """把「这次是哪把 Key」放进上下文：作用域内写下的那一行账都会带上它。"""

    token = _current_key_id.set(key_id)
    try:
        yield
    finally:
        _current_key_id.reset(token)


def current_key_id() -> str | None:
    """当前上下文里那把 Key；不在门禁里时（内部评测的召回）为 ``None``。"""

    return _current_key_id.get()


#: 端点名。写进 ``usage_events.endpoint``，看板按它拆「按接口」。
ENDPOINT_RETRIEVE = "retrieve"
ENDPOINT_REWRITE = "rewrite"
ENDPOINT_DECOMPOSE = "decompose"
ENDPOINT_RESEARCH = "research"
#: 按稿直查（``/documents/{id}/versions/{v}/search``，#208）。它不进配额门禁、三本账
#: 全 0，但**要留一行**：它真的调一次 ``embed_query``，账上得能回答「谁在白用」。
ENDPOINT_VERSION_SEARCH = "version_search"


#: 三本账**这次实际扣了多少**。由门禁在 ``record_n`` 那一刻记下，写账时原样取用。
#:
#: 为什么不是「落行时再算一遍」：账本要回答的是「额度被扣了几次」，不是「按这次用了哪些
#: 能力应该扣几次」。两者平时相等，岔开的时候（分解检索的 kernel 次数、重排实际跑没跑）
#: 正是要查账的时候——那时两份数不一样，看板上的占比就永远对不上。
#:
#: 键是 ``"r"`` / ``"rr"`` / ``"llm"``；``None`` 表示「这次没人记账」（例如内部评测的
#: 召回根本没过门禁），调用方据此回落到自己那份推断。
_meter_tally: ContextVar[dict[str, int] | None] = ContextVar("usage_meter_tally", default=None)

BOOK_R = "r"
BOOK_RR = "rr"
BOOK_LLM = "llm"


@contextmanager
def meter_tally() -> Iterator[None]:
    """开一份「这次请求的三本账」计数；作用域里 ``count_meter`` 累加的就是它。"""

    token = _meter_tally.set({BOOK_R: 0, BOOK_RR: 0, BOOK_LLM: 0})
    try:
        yield
    finally:
        _meter_tally.reset(token)


def count_meter(book: str, n: int) -> None:
    """把门禁**刚刚扣掉的那 N 次**记进上下文（与 ``record_n`` 紧挨着调用）。

    没有开 ``meter_tally`` 时什么也不做：不在门禁里的调用（内部评测、脚本）不该伪造账。
    """

    tally = _meter_tally.get()
    if tally is None or n <= 0:
        return
    tally[book] = tally.get(book, 0) + n


def current_meter_counts() -> tuple[int, int, int] | None:
    """这次请求三本账各扣了几次；不在门禁作用域里时返回 ``None``。"""

    tally = _meter_tally.get()
    if tally is None:
        return None
    return tally[BOOK_R], tally[BOOK_RR], tally[BOOK_LLM]


async def record_refusal(
    *,
    endpoint: str,
    key: KeyContext,
    kb_ids: list[str],
    code: int,
    started_at: float,
) -> None:
    """记一行「被拒」：业务码 + 已经烧掉的耗时，其余字段留空。

    为什么交付类字段（``chunk_ids`` / 稿号 / 版本）在这里是空的：被拒的请求**没有交付**
    任何东西，它不是「空结果」，而是「没走到召回」。用空值表达这件事，比让它们看起来
    像一次 0 命中的检索要诚实。

    为什么三本账都是 0：六道关卡里「先判、后记账」是硬约束（ADR-0018），被拒的请求
    一次都不扣额度——账上没扣，这里就不能记。
    """

    log_id = new_retrieval_log_id()
    await record_retrieval_trace(
        log_id=log_id,
        # 被拒的请求没有人看到 trace，但 trace_id 仍必须是 **log_id 的确定性派生**（ADR-0021）：
        # 它和成功路径同一口径，才使得「按 trace_id 反查这一行」在两处都成立。
        trace_id=trace_id_for(log_id),
        tenant_id=key.tenant_id,
        key_id=key.key_id,
        endpoint=endpoint,
        code=code,
        kb_ids=list(kb_ids),
        index_versions={},
        doc_versions={},
        chunk_ids=[],
        top_k=0,
        latency_ms=int((time.perf_counter() - started_at) * 1000),
        degraded_reason=None,
        empty_reason=None,
        meter_r=0,
        meter_rr=0,
        meter_llm=0,
    )

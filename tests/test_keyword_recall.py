"""关键词一路的「多查询并集」（第 15 步）：与向量路同形——各查一次、再合并。

这一组用例钉的是**形状与失败语义**，不是检索质量：

* 两句 → 两次 ``keyword_search``（并发），结果并成一个池；
* 同一 ``(kb_id, chunk_id)`` 只留一条：分数取更高、整行跟着名次更好的那份；
* 只有一句（改写关闭 / 模型原句返回）时**只查一次**，且结果原样返回、不重排；
* 任一句失败 = 这一路失败（``50002``），绝不拿另一句的半份结果冒充完整池；
* 引擎没配（空实现）与「查过了没命中」分得开。

为什么值得单独一组：这两件事（不合并 / 合并成两个通道票）都会让「关键词这一路到底
查了几次」变得说不清，而它正是第 15 步要改的那一件事。
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.services.retrieval.keyword_recall import keyword_recall

TENANT = "tenant_demo"
KB = "kb_a"
VERSIONS = {KB: "v1"}


class _StubProvider:
    """按查询串返回预先摆好的行；记录调用顺序与并发度。"""

    def __init__(
        self,
        *,
        rows_by_query: dict[str, list[dict]] | None = None,
        fail_on: str | None = None,
        available: bool = True,
    ) -> None:
        self._rows = rows_by_query or {}
        self._fail_on = fail_on
        self._available = available
        self.queries: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    @property
    def available(self) -> bool:
        return self._available

    async def keyword_search(
        self, query: str, *, tenant_id: str, index_versions: dict[str, str], top_k: int
    ) -> list[dict]:
        self.queries.append(query)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            if self._fail_on == query:
                raise RuntimeError("connection reset by peer")
            return list(self._rows.get(query, []))[:top_k]
        finally:
            self.in_flight -= 1


def _row(chunk_id: str, *, score: float, content: str = "") -> dict:
    return {
        "chunk_id": chunk_id,
        "document_id": f"doc_{chunk_id}",
        "kb_id": KB,
        "title": f"t-{chunk_id}",
        "content": content or f"c-{chunk_id}",
        "score": score,
    }


def _recall(provider: _StubProvider, queries: list[str], *, top_k: int = 5) -> list[dict]:
    return asyncio.run(
        keyword_recall(
            provider=provider,
            tenant_id=TENANT,
            index_versions=VERSIONS,
            queries=queries,
            top_k=top_k,
        )
    )


# ───────────────────────────────────────────────────── 两句 = 两次请求


def test_two_sentences_are_two_searches() -> None:
    provider = _StubProvider(
        rows_by_query={"退钱怎么弄": [_row("A", score=9.0)], "退款怎么走": [_row("B", score=8.0)]}
    )

    rows = _recall(provider, ["退钱怎么弄", "退款怎么走"])

    assert provider.queries == ["退钱怎么弄", "退款怎么走"]
    assert [row["chunk_id"] for row in rows] == ["A", "B"], "并集按分数降序"


def test_the_two_searches_are_issued_concurrently() -> None:
    """两次 ES 请求并发发出：约 1 次 RTT，而不是串行等两次。"""

    provider = _StubProvider(rows_by_query={"a": [], "b": []})

    _recall(provider, ["a", "b"])

    assert provider.max_in_flight == 2


def test_a_hit_found_by_both_sentences_appears_once() -> None:
    """同一 chunk 两句都命中：只留一条，分数取更高者、内容跟着名次更好的那份。"""

    provider = _StubProvider(
        rows_by_query={
            "原句": [_row("X", score=3.0, content="from-raw")],
            "改写句": [_row("X", score=7.5, content="from-rewrite")],
        }
    )

    rows = _recall(provider, ["原句", "改写句"])

    assert len(rows) == 1
    assert rows[0]["chunk_id"] == "X"
    assert rows[0]["score"] == 7.5, "分数取更高者，不是相加"
    assert rows[0]["content"] == "from-raw", "整行跟着名次更小的那份（两句都是第 1 名时取先到的）"


# ───────────────────────────────────────────── 一句 = 一次请求（快路径）


def test_one_sentence_is_one_search_and_keeps_es_order() -> None:
    """改写关闭 / 模型原句返回时只查一次，且**不重排**（ES 已按分数给顺序）。"""

    provider = _StubProvider(
        rows_by_query={"退钱怎么弄": [_row("A", score=1.0), _row("B", score=9.0)]}
    )

    rows = _recall(provider, ["退钱怎么弄"])

    assert provider.queries == ["退钱怎么弄"]
    assert [row["chunk_id"] for row in rows] == ["A", "B"], "逐字节保留替身给的原顺序"


def test_blank_sentences_are_dropped_before_querying() -> None:
    """空串不发给 ES：空查询会被 ES 变成 match_all，等于把整个库捞回来。"""

    provider = _StubProvider(rows_by_query={"退钱怎么弄": [_row("A", score=1.0)]})

    rows = _recall(provider, ["", "   ", "退钱怎么弄"])

    assert provider.queries == ["退钱怎么弄"]
    assert [row["chunk_id"] for row in rows] == ["A"]


def test_no_sentence_at_all_returns_empty_without_a_request() -> None:
    provider = _StubProvider()

    assert _recall(provider, ["", "  "]) == []
    assert provider.queries == []


# ─────────────────────────────────────────────────── 失败语义


def test_any_failing_sentence_fails_the_whole_channel() -> None:
    """任一句失败 = 这一路失败（``50002``）：不拿另一句的半份结果冒充完整池。"""

    provider = _StubProvider(rows_by_query={"原句": [_row("A", score=9.0)]}, fail_on="改写句")

    with pytest.raises(AppError) as caught:
        _recall(provider, ["原句", "改写句"])

    assert caught.value.code is ErrorCode.RETRIEVAL_FAILED


def test_a_missing_engine_is_not_an_empty_result() -> None:
    """空实现是「能力不存在」，不是「查过了没命中」——两者在页面上是两件事。"""

    provider = _StubProvider(available=False)

    with pytest.raises(AppError) as caught:
        _recall(provider, ["退钱怎么弄"])

    assert caught.value.code is ErrorCode.RETRIEVAL_FAILED
    assert provider.queries == []


def test_top_k_truncates_the_pool() -> None:
    provider = _StubProvider(
        rows_by_query={
            "a": [_row("A1", score=9.0), _row("A2", score=8.0)],
            "b": [_row("B1", score=7.0)],
        }
    )

    rows = _recall(provider, ["a", "b"], top_k=2)

    assert [row["chunk_id"] for row in rows] == ["A1", "A2"]


def test_the_provider_contract_is_unchanged() -> None:
    """Provider 仍吃**一句**：编排在这一层，不在 Provider 里。

    这不是洁癖：ES 那侧一个请求只有一个 ``multi_match``，同义词表 / 分词器都没变；
    多句是应用层的事，换 Provider（比如以后换别的引擎）不需要跟着改接口。
    """

    import inspect

    from app.providers.keyword_search.base import KeywordSearchProvider

    signature = inspect.signature(KeywordSearchProvider.keyword_search)
    assert list(signature.parameters) == ["self", "query", "tenant_id", "index_versions", "top_k"]
    assert signature.parameters["query"].annotation is str

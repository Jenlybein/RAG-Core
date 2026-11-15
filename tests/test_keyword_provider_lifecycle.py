"""关键词 Provider 的关闭路径（#184）：不关就会在进程收尾刷 `Unclosed client session`。

这一组用例守三件事：
1. `aclose_keyword_search_provider()` 只关**已经构造过**的实例，并清空缓存；
2. ES 实现的 `aclose()` 关掉每个事件循环名下的客户端，且**不关**调用方注入的替身；
3. 两个运维脚本即使中途抛错，也一定走到关闭那一步。
"""

import asyncio
from typing import Any

import pytest

from app.providers.keyword_search import (
    aclose_keyword_search_provider,
    reset_keyword_search_provider_cache,
)
from app.providers.keyword_search.base import KeywordSearchProvider
from app.providers.keyword_search.noop import NoopKeywordSearchProvider


class _RecordingProvider(KeywordSearchProvider):
    """只记「被关过没有」的假 Provider。"""

    def __init__(self) -> None:
        self.closed = 0

    @property
    def available(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed += 1

    # 其余抽象方法在这一组用例里用不到，但 ABC 要求实现
    async def add_chunks(self, chunks: list[dict]) -> None:  # pragma: no cover
        raise AssertionError("not used")

    async def keyword_search(self, *args: Any, **kwargs: Any) -> list[dict]:  # pragma: no cover
        raise AssertionError("not used")

    async def count_documents(self, *args: Any, **kwargs: Any) -> int:  # pragma: no cover
        raise AssertionError("not used")

    async def physical_index_exists(self, version: str) -> bool:  # pragma: no cover
        raise AssertionError("not used")

    async def activate_version(self, version: str) -> None:  # pragma: no cover
        raise AssertionError("not used")

    async def delete_by_document_id(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("not used")

    async def mark_current_draft(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("not used")

    async def current_draft_versions(self, *args: Any, **kwargs: Any) -> dict:  # pragma: no cover
        raise AssertionError("not used")


class _FakeClient:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


def test_noop_provider_aclose_is_a_no_op() -> None:
    """空实现没有外部资源，`aclose()` 必须能安全调用（否则脚本的 finally 会炸）。"""

    async def scenario() -> None:
        await NoopKeywordSearchProvider().aclose()

    asyncio.run(scenario())


def test_closing_clears_the_cache_and_closes_each_provider() -> None:
    """进程级缓存里的每个实例都被关一次，缓存随后清空。"""

    reset_keyword_search_provider_cache()
    from app.providers.keyword_search import _PROVIDER_CACHE

    first, second = _RecordingProvider(), _RecordingProvider()
    _PROVIDER_CACHE[("a", "u", "i", 1.0)] = first
    _PROVIDER_CACHE[("b", "u", "i", 1.0)] = second

    async def scenario() -> None:
        await aclose_keyword_search_provider()

    asyncio.run(scenario())
    assert (first.closed, second.closed) == (1, 1)
    assert _PROVIDER_CACHE == {}


def test_closing_an_empty_cache_does_not_construct_a_provider() -> None:
    """没有构造过实例就什么都不做——绝不「为了关闭而现造一个」。"""

    reset_keyword_search_provider_cache()
    from app.providers.keyword_search import _PROVIDER_CACHE

    async def scenario() -> None:
        await aclose_keyword_search_provider()

    asyncio.run(scenario())
    assert _PROVIDER_CACHE == {}


def test_elasticsearch_aclose_closes_every_loop_client_and_keeps_the_injected_one() -> None:
    """按循环缓存的客户端全部关掉并清空；注入的替身归调用方，不动它。"""

    from app.providers.keyword_search.elasticsearch import (
        ElasticsearchKeywordSearchProvider,
    )

    async def scenario() -> None:
        injected = _FakeClient()
        provider = ElasticsearchKeywordSearchProvider(
            url="http://localhost:9200",
            index_name="rag_chunks",
            client=injected,
            timeout_s=1.0,
        )
        first, second = _FakeClient(), _FakeClient()
        loop = asyncio.get_running_loop()
        provider._clients[loop] = first
        provider._clients[asyncio.new_event_loop()] = second

        await provider.aclose()

        assert (first.closed, second.closed) == (1, 1)
        assert len(provider._clients) == 0
        assert injected.closed == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("script_name", ["verify_document_versions", "purge_document_versions"])
def test_ops_scripts_close_the_provider_even_when_the_run_fails(
    script_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """脚本中途抛错也必须走到关闭那一步（`finally` 不是装饰）。"""

    module = __import__(f"scripts.{script_name}", fromlist=["_run"])
    closed: list[int] = []

    async def fake_close() -> None:
        closed.append(1)

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    async def fake_session() -> Any:
        class _Session:
            async def close(self) -> None:
                return None

        return _Session()

    monkeypatch.setattr(module, "_close_keyword_provider", fake_close)
    monkeypatch.setattr(module, "_open_session", fake_session)
    target = (
        "reconcile_document_versions"
        if script_name == "verify_document_versions"
        else "_scan_and_purge"
    )
    if hasattr(module, target):
        monkeypatch.setattr(module, target, boom)

    args = type("Args", (), {"kb_id": None, "limit": None, "apply": False, "retain": 3})()

    async def scenario() -> None:
        try:
            await module._run(args)
        except RuntimeError:
            pass

    if hasattr(module, target):
        asyncio.run(scenario())
        assert closed == [1]

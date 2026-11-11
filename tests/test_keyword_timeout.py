"""关键词渠道的**时间预算**：引擎挂掉时不许把整条检索拖住。

真实事故（2026-11-11）：`rag-es` 容器被杀之后，每次检索都在等一条永远不会通的渠道。
六段耗时里 `keyword_recall` 独占 **19445 ms**，其它五段加起来不到 600 ms。界面上表现为
「检索突然极慢」，而真相是关键词渠道已经不在了。

ES 客户端自己的重试把一次 DNS 失败累计成 ~19 秒（日志里能数出 4 次 attempt）。
关键词是**可降级**的一路（ADR-0021 同一口径），所以预算必须有界。

**预算落在 Provider 里，不落在调用方。** 关键词通道有两份实现——单通道走
`rag_service._retrieve_by_keyword`，混合模式走 `pipeline._keyword_branch`（控制台看到的
就是后者）。写在调用方就得写两遍，而写两遍的下场是漂移：第一版只改了前者，实测**依然
19 秒**，因为控制台走的是后者。这个教训写在测试里，免得下一次又只改一半。

接缝：
- `ElasticsearchKeywordSearchProvider` + 假客户端（连得上但永远不返回）——验预算本身；
- `pipeline._keyword_branch` + 同一个 Provider——验「混合模式那条路真的被兜住且照常降级」。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.observability.stage_timing import StageTimer
from app.providers.keyword_search.elasticsearch import ElasticsearchKeywordSearchProvider
from app.services.retrieval import pipeline


class _HangingClient:
    """永远不返回的 ES 客户端替身：模拟「连得上但永远不通」或客户端内部反复重试。"""

    def __init__(self, *, seconds: float = 10.0) -> None:
        self.seconds = seconds
        self.calls = 0

    async def search(self, **_: Any) -> dict:
        self.calls += 1
        await asyncio.sleep(self.seconds)
        return {"hits": {"hits": []}}


class _FastClient:
    """正常返回一条命中的客户端替身。"""

    async def search(self, **_: Any) -> dict:
        return {
            "hits": {
                "hits": [
                    {
                        "_score": 1.5,
                        "_source": {
                            "chunk_id": "chunk-a",
                            "document_id": "doc-a",
                            "kb_id": "kb-a",
                            "title": "Refund Policy",
                            "content": "within 7 days",
                        },
                    }
                ]
            }
        }


def _provider(client: Any, *, timeout_s: float) -> ElasticsearchKeywordSearchProvider:
    return ElasticsearchKeywordSearchProvider(
        url="http://elasticsearch:9200",
        index_name="rag_chunks",
        client=client,
        timeout_s=timeout_s,
    )


def test_the_provider_abandons_a_hanging_query_within_its_budget() -> None:
    """预算 0.1 秒、查询挂 10 秒 → Provider 必须在 1 秒内抛 TimeoutError。"""

    client = _HangingClient(seconds=10.0)
    provider = _provider(client, timeout_s=0.1)

    started = time.perf_counter()
    with pytest.raises(TimeoutError):
        asyncio.run(
            provider.keyword_search(
                "退款多久到账", tenant_id="tenant_demo", index_versions={}, top_k=5
            )
        )
    elapsed = time.perf_counter() - started

    assert client.calls == 1, "查询应当被真的发出去过一次（不是没发就放弃）"
    assert elapsed < 1.0, f"预算没生效，等了 {elapsed:.1f}s"


def test_the_provider_still_returns_hits_when_the_budget_is_ample() -> None:
    """预算只切「不返回」的情形，正常查询照常拿到结果。"""

    provider = _provider(_FastClient(), timeout_s=2.0)

    rows = asyncio.run(
        provider.keyword_search("退款", tenant_id="tenant_demo", index_versions={}, top_k=5)
    )

    assert [row["chunk_id"] for row in rows] == ["chunk-a"]


def test_the_hybrid_keyword_channel_degrades_fast_instead_of_waiting() -> None:
    """控制台走的这条路：引擎卡住时，通道要在预算内判成「这一路挂了」。

    这条是**回归的那一条**：预算只加在 `rag_service` 上时，这个用例会等满 10 秒。
    """

    client = _HangingClient(seconds=10.0)
    provider = _provider(client, timeout_s=0.1)

    started = time.perf_counter()
    with pytest.raises(AppError) as exc:
        asyncio.run(
            pipeline._keyword_branch(
                tenant_id="tenant_demo",
                kb_ids=["kb-a"],
                query="退款多久到账",
                top_k=5,
                keyword_factory=lambda: provider,
                stage_timer=StageTimer(),
            )
        )
    elapsed = time.perf_counter() - started

    # 失败仍走既有口径：pipeline 认这个码来判降级，结果照常用向量那一路返回。
    assert exc.value.code is ErrorCode.RETRIEVAL_FAILED
    assert elapsed < 1.0, f"混合模式那条路没被兜住，等了 {elapsed:.1f}s"


def test_the_hybrid_keyword_channel_records_the_failed_stage() -> None:
    """失败的那一段**也**留下耗时：它确实花了时间，抹掉会让「这次为什么慢」少一行。"""

    timer = StageTimer()
    provider = _provider(_HangingClient(seconds=10.0), timeout_s=0.1)

    with pytest.raises(AppError):
        asyncio.run(
            pipeline._keyword_branch(
                tenant_id="tenant_demo",
                kb_ids=["kb-a"],
                query="退款",
                top_k=5,
                keyword_factory=lambda: provider,
                stage_timer=timer,
            )
        )

    assert any(span.name == "keyword_recall" for span in timer.spans())


def test_the_provider_factory_reuses_one_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一个配置反复取 Provider，必须拿到**同一个实例**。

    每次新建一个 ``AsyncElasticsearch`` 等于每次丢掉连接池：每题重做 TLS 握手，进程里攒下
    一堆从不关闭的客户端（跑批结束刷出一串 `Unclosed client session`，把真日志淹掉）。
    """

    from app.core.config import settings as live_settings
    from app.providers.keyword_search import (
        get_keyword_search_provider,
        reset_keyword_search_provider_cache,
    )

    monkeypatch.setattr(live_settings, "KEYWORD_SEARCH_PROVIDER", "elasticsearch")
    reset_keyword_search_provider_cache()

    first = get_keyword_search_provider()
    second = get_keyword_search_provider()

    assert first is second


def test_changing_the_address_yields_a_fresh_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """改了地址就必须换实例：否则「配置改了但连接还指着旧地址」。"""

    from app.core.config import settings as live_settings
    from app.providers.keyword_search import (
        get_keyword_search_provider,
        reset_keyword_search_provider_cache,
    )

    monkeypatch.setattr(live_settings, "KEYWORD_SEARCH_PROVIDER", "elasticsearch")
    monkeypatch.setattr(live_settings, "ELASTICSEARCH_URL", "http://es-a:9200")
    reset_keyword_search_provider_cache()
    before = get_keyword_search_provider()

    monkeypatch.setattr(live_settings, "ELASTICSEARCH_URL", "http://es-b:9200")
    after = get_keyword_search_provider()

    assert before is not after

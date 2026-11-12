"""专用 Cross-Encoder 重排 Provider 的契约（全部离线，用 ``httpx.MockTransport``）。

接缝只有一个：``RerankProvider.rerank(query=..., chunks=..., top_n=...)``——既有抽象，
调用方看不出实现换了。这里断言的是**发出去的报文**与**回来的顺序**，不看内部实现。

本文件守的契约（票 #116）：

* 报文扁平：``{model, query, documents, top_n, instruct}``；``instruct`` 用**英文**常量
  （官方要求英文，中文 instruct 不是「更好的提示」，而是没被接受过的输入）；
* 文档串只搬运 ``标题`` + ``正文``（正文截到上限），**不**拼分数、租户、内部 id——
  那些东西对 Cross-Encoder 只是噪音，也会把一份凭证无关的事实写进外部请求；
* 候选上限是**硬上限 50**（官方单请求 500 文档，但本仓库的重排池按 20 起步；50 是
  这一层自己的护栏，配置写多大都越不过去）；
* 按接口给的 ``index`` 回填原切片，**不做文本匹配**；同分保持原候选顺序；
* 空候选**不调远程**；
* 超时 / 非 2xx / 结构不合法 / 空返回一律抛出——降级是编排层的职责，不是 Provider 的；
* 身份属性（``provider_name`` / ``model_name``）如实回显，供响应元数据与评测快照用。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.core import http_clients
from app.providers.rerank.qwen import (
    INSTRUCT,
    MAX_DOCUMENTS,
    Qwen3RerankProvider,
)

BASE_URL = "https://rerank.example.test/compatible-api/v1"
RERANK_URL = f"{BASE_URL}/reranks"
MODEL = "qwen3.7-rerank-test"


def _chunk(
    chunk_id: str,
    *,
    content: str | None = None,
    title: str | None = None,
    rrf: float = 0.03,
) -> dict[str, Any]:
    """一条融合后的候选：带着五列字段与融合分，重排前后都该在。"""

    return {
        "document_id": f"doc-{chunk_id}",
        "chunk_id": chunk_id,
        "kb_id": "kb-a",
        "title": title if title is not None else f"title-{chunk_id}",
        "content": content if content is not None else f"content-{chunk_id}",
        "vector_score": 0.9,
        "bm25_score": None,
        "vector_rank": 1,
        "bm25_rank": None,
        "rrf_score": rrf,
        "score": rrf,
    }


def _ranking_response(*pairs: tuple[int, float], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={"results": [{"index": index, "relevance_score": score} for index, score in pairs]},
    )


class _Recorder:
    """替身端点：记下每一次请求（URL / 头 / 报文），按预置的函数作答。"""

    def __init__(self, responder: Any) -> None:
        self._responder = responder
        self.requests: list[httpx.Request] = []
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.payloads.append(json.loads(request.content))
        return self._responder(request)


def _provider(recorder: Any, **overrides: Any) -> Qwen3RerankProvider:
    options: dict[str, Any] = {
        "base_url": BASE_URL,
        "api_key": "rerank-key",
        "model": MODEL,
        "max_candidates": 20,
        "chunk_max_chars": 1024,
        "timeout_seconds": 5.0,
    }
    options.update(overrides)
    client = options.pop("client", None)
    if client is None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return Qwen3RerankProvider(client=client, **options)


# ───────────────────────────────────────────────────────────────── 报文


def test_the_request_is_flat_and_carries_an_english_instruct() -> None:
    """报文键集合是契约：多一个键就是给外部端点发它没要的东西。"""

    recorder = _Recorder(lambda _request: _ranking_response((0, 0.9)))
    chunks = [_chunk("c1"), _chunk("c2")]

    asyncio.run(_provider(recorder).rerank(query="退款怎么走", chunks=chunks, top_n=2))

    assert len(recorder.payloads) == 1
    payload = recorder.payloads[0]
    assert set(payload) == {"model", "query", "documents", "top_n", "instruct"}
    assert payload["model"] == MODEL
    assert payload["query"] == "退款怎么走"
    assert payload["instruct"] == INSTRUCT
    assert payload["instruct"] == (
        "Given a question, retrieve passages that contain evidence needed to answer the question."
    )
    # 官方要求英文：中文 instruct 是「教程里的说法」，不是被接受过的输入。
    assert payload["instruct"].isascii()


def test_documents_are_title_and_body_only() -> None:
    """文档串只有标题与正文：不带分数、租户、内部 id。"""

    recorder = _Recorder(lambda _request: _ranking_response((0, 0.9)))
    chunks = [_chunk("c1", title="退款政策", content="七天无理由退货")]

    asyncio.run(_provider(recorder).rerank(query="q", chunks=chunks, top_n=1))

    documents = recorder.payloads[0]["documents"]
    assert documents == ["标题：退款政策\n正文：七天无理由退货"]
    # 融合分 / 租户 / chunk_id / document_id 一个都不许出现在发出去的文档里。
    for leaked in ("0.03", "kb-a", "c1", "doc-c1", "vector_score", "rrf_score"):
        assert leaked not in documents[0], f"{leaked} 不该进文档串"


def test_the_candidate_cap_is_fifty_even_when_configured_higher() -> None:
    """候选 ≤ 50 是这一层的硬上限，配置写多大都越不过去。"""

    recorder = _Recorder(lambda _request: _ranking_response((0, 0.9)))
    chunks = [_chunk(f"c{i}") for i in range(MAX_DOCUMENTS + 20)]

    asyncio.run(
        _provider(recorder, max_candidates=MAX_DOCUMENTS + 40).rerank(
            query="q", chunks=chunks, top_n=5
        )
    )

    documents = recorder.payloads[0]["documents"]
    assert MAX_DOCUMENTS == 50
    assert len(documents) == MAX_DOCUMENTS
    assert "content-c49" in documents[-1]
    assert "content-c50" not in "".join(documents)


def test_each_document_body_is_truncated_to_the_configured_limit() -> None:
    """单条正文截到上限——官方对超限单条直接 400 且不截断，所以客户端截断是必要条件。"""

    recorder = _Recorder(lambda _request: _ranking_response((0, 0.9)))
    chunks = [_chunk("c1", content="X" * 3000), _chunk("c2", content="短正文")]

    asyncio.run(_provider(recorder, chunk_max_chars=1024).rerank(query="q", chunks=chunks, top_n=2))

    documents = recorder.payloads[0]["documents"]
    body = documents[0].split("正文：", 1)[1]
    assert len(body) == 1024
    assert documents[1] == "标题：title-c2\n正文：短正文"


def test_top_n_never_exceeds_the_documents_sent() -> None:
    """``top_n`` 是「要端点回几条」，不可能多于送去的文档条数。"""

    recorder = _Recorder(lambda _request: _ranking_response((0, 0.9), (1, 0.8)))
    chunks = [_chunk("c1"), _chunk("c2")]

    result = asyncio.run(_provider(recorder).rerank(query="q", chunks=chunks, top_n=10))

    assert recorder.payloads[0]["top_n"] == 2
    assert len(result) == 2


def test_the_caller_top_n_still_caps_the_returned_rows() -> None:
    """回调条数由调用方的 ``top_n`` 决定：端点多给了也不能多返回。"""

    recorder = _Recorder(lambda _request: _ranking_response((0, 0.9), (1, 0.8), (2, 0.7), (3, 0.6)))
    chunks = [_chunk(f"c{i}") for i in range(4)]

    result = asyncio.run(_provider(recorder).rerank(query="q", chunks=chunks, top_n=2))

    assert [row["chunk_id"] for row in result] == ["c0", "c1"]


# ─────────────────────────────────────────────── 下标回填与顺序


def test_results_are_backfilled_by_index_not_by_text_matching() -> None:
    """按接口给的 ``index`` 回填原切片；回给调用方的仍是切片原文与五列字段。"""

    recorder = _Recorder(lambda _request: _ranking_response((2, 0.95), (0, 0.61)))
    chunks = [_chunk("c1", rrf=0.03), _chunk("c2", rrf=0.02), _chunk("c3", rrf=0.01)]

    result = asyncio.run(_provider(recorder).rerank(query="q", chunks=chunks, top_n=3))

    assert [row["chunk_id"] for row in result] == ["c3", "c1"]
    assert [row["score"] for row in result] == [0.95, 0.61]
    assert [row["rerank_score"] for row in result] == [0.95, 0.61]
    # 只改「最终排序分」：融合分、分列字段、正文都原样搬运。
    assert [row["rrf_score"] for row in result] == [0.01, 0.03]
    assert result[0]["vector_score"] == 0.9
    assert result[0]["content"] == "content-c3"
    assert result[0]["kb_id"] == "kb-a"


def test_equal_scores_keep_the_original_candidate_order() -> None:
    """同分不能取决于端点回来的行序：按原候选顺序兜底，结果可复现。"""

    recorder = _Recorder(lambda _request: _ranking_response((2, 0.5), (0, 0.5), (1, 0.5)))
    chunks = [_chunk("c1"), _chunk("c2"), _chunk("c3")]

    result = asyncio.run(_provider(recorder).rerank(query="q", chunks=chunks, top_n=3))

    assert [row["chunk_id"] for row in result] == ["c1", "c2", "c3"]


def test_a_duplicate_index_keeps_the_highest_score() -> None:
    """端点把同一条说了两遍时取最高分：顺序可复现，不取决于行序。"""

    recorder = _Recorder(lambda _request: _ranking_response((1, 0.2), (1, 0.8)))
    chunks = [_chunk("c1"), _chunk("c2")]

    result = asyncio.run(_provider(recorder).rerank(query="q", chunks=chunks, top_n=2))

    assert [row["chunk_id"] for row in result] == ["c2"]
    assert result[0]["score"] == 0.8


# ─────────────────────────────────────────────── 空候选与失败语义


def test_no_candidates_means_no_remote_call() -> None:
    """空候选不浪费一次远程调用；返回空列表、不报错（空结果不是重排失败）。"""

    def _forbidden(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("空候选不该发起远程调用")

    result = asyncio.run(_provider(_forbidden).rerank(query="q", chunks=[], top_n=5))

    assert result == []


def test_a_non_2xx_response_is_an_error() -> None:
    """非 2xx 一律抛出：降级由编排层决定，Provider 不假装自己排过序。"""

    recorder = _Recorder(lambda _request: httpx.Response(429, json={"message": "busy"}))

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_provider(recorder).rerank(query="q", chunks=[_chunk("c1")], top_n=1))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"results": []}),
        httpx.Response(200, json={"results": "不是列表"}),
        httpx.Response(200, json={"oops": 1}),
        httpx.Response(200, json={"results": [{"index": 0}]}),
        httpx.Response(200, json={"results": [{"index": 9, "relevance_score": 0.5}]}),
        httpx.Response(200, json={"results": [{"index": 0, "relevance_score": "nan"}]}),
        # nan / inf 不是合法 JSON，用原文送进来（``json.loads`` 默认接受这两个字面量）。
        httpx.Response(200, text='{"results": [{"index": 0, "relevance_score": NaN}]}'),
        httpx.Response(200, text='{"results": [{"index": 0, "relevance_score": Infinity}]}'),
        httpx.Response(200, json={"results": [{"index": True, "relevance_score": 0.5}]}),
        httpx.Response(200, json={"results": ["c1"]}),
        httpx.Response(200, text="not json"),
    ],
)
def test_a_malformed_response_is_an_error(response: httpx.Response) -> None:
    """空返回、结构不合法、下标越界、分数不是有限浮点：全部抛出。"""

    recorder = _Recorder(lambda _request: response)

    with pytest.raises(ValueError):
        asyncio.run(_provider(recorder).rerank(query="q", chunks=[_chunk("c1")], top_n=1))


def test_a_slow_endpoint_times_out() -> None:
    """超时是失败的一种：``RERANK_TIMEOUT_SECONDS`` 真的兜住了慢调用。"""

    async def _slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return _ranking_response((0, 0.9))

    recorder = _Recorder(_slow)

    with pytest.raises(TimeoutError):
        asyncio.run(
            _provider(recorder, timeout_seconds=0.01).rerank(
                query="q", chunks=[_chunk("c1")], top_n=1
            )
        )


# ─────────────────────────────────────────────── 客户端注入与身份


def test_an_injected_client_is_reused_and_never_closed() -> None:
    """注入的客户端归调用方管：本类不替它决定生命周期（连接复用是 #117）。"""

    recorder = _Recorder(lambda _request: _ranking_response((0, 0.9)))
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))

    async def _run() -> Any:
        provider = _provider(recorder, client=client)
        try:
            return await provider.rerank(query="q", chunks=[_chunk("c1")], top_n=1)
        finally:
            assert client.is_closed is False, "注入的客户端不许被 Provider 关掉"
            await client.aclose()

    assert asyncio.run(_run())


def test_without_an_injected_client_the_registry_client_is_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没注入时走连接注册表：一个 loop 只造一个池子，连续调用复用它，Provider 不关它。

    这是第 10 步票 4 的行为（改造前是「每次调用各建一个」）：重排是延迟敏感的一步，
    每题重做一次 TLS 握手等于把「换专用端点」省下来的时间又还回去。
    """

    recorder = _Recorder(lambda _request: _ranking_response((0, 0.9)))
    built: list[httpx.AsyncClient] = []
    # 先抓住真类：下面的 monkeypatch 换的是 ``httpx.AsyncClient`` 这个**模块属性**，
    # 它同时也是本文件里的 ``httpx.AsyncClient``——不先存一份就会自己调自己。
    real_client = httpx.AsyncClient

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        client = real_client(transport=httpx.MockTransport(recorder), **kwargs)
        built.append(client)
        return client

    # 注册表用的是同一个 ``httpx`` 模块对象，所以这一行同时接住了它的构造点。
    monkeypatch.setattr("app.providers.rerank.qwen.httpx.AsyncClient", _factory)

    async def _run() -> None:
        provider = Qwen3RerankProvider(
            base_url=BASE_URL,
            api_key="rerank-key",
            model=MODEL,
            timeout_seconds=5.0,
        )
        for _ in range(2):
            await provider.rerank(query="q", chunks=[_chunk("c1")], top_n=1)

        client = http_clients.get_http_client()
        assert client.is_closed is False, "注册表的客户端归 loop 收尾，Provider 不许关它"
        await http_clients.aclose_current_loop_clients()
        assert client.is_closed is True, "收尾由注册表那侧做"

    asyncio.run(_run())

    assert len(built) == 1, "同一 loop 内只该造一个客户端（复用）"
    assert len(recorder.requests) == 2, "两次调用都发在同一个客户端上"


def test_the_endpoint_url_is_joined_from_the_configured_base() -> None:
    """不许在这里把 embedding 的 ``compatible-mode`` 路径段写死：重排是另一段。"""

    recorder = _Recorder(lambda _request: _ranking_response((0, 0.9)))

    asyncio.run(
        _provider(recorder, base_url=f"{BASE_URL}/").rerank(
            query="q", chunks=[_chunk("c1")], top_n=1
        )
    )

    assert str(recorder.requests[0].url) == RERANK_URL
    assert recorder.requests[0].headers["authorization"] == "Bearer rerank-key"


def test_the_provider_identifies_itself_for_observability() -> None:
    """身份属性是响应的 ``rerank.provider`` / ``rerank.model`` 与评测快照的来源。"""

    provider = _provider(lambda _request: _ranking_response((0, 0.9)))

    assert provider.available is True
    assert provider.provider_name == "qwen3"
    assert provider.model_name == MODEL


def test_the_document_limit_is_clamped_inside_the_provider() -> None:
    """配置把单条上限写大了也越不过 1024：官方超限直接 400，护栏不能交给调用方。"""

    from app.providers.rerank.qwen import MAX_CHARS_PER_DOC, Qwen3RerankProvider

    provider = Qwen3RerankProvider(
        base_url=RERANK_URL,
        api_key="rerank-key",
        chunk_max_chars=99_999,
        client=None,
    )

    assert MAX_CHARS_PER_DOC == 1024
    assert provider._chunk_max_chars == MAX_CHARS_PER_DOC  # noqa: SLF001 - 护栏就是这一条
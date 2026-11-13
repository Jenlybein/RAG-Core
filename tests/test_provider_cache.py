"""Provider 实例缓存与「客户端从注册表来」：同一份配置只装配一次 Provider。

**为什么必须缓存 Provider**：改造前每次检索都现装两套模型（改写、重排各一次），每一套
连带一个新建的连接池；而 Provider 本身只是「几个配置字段 + 一个装配动作」，没有任何
请求级状态。缓存键因此是「真正决定它身份的那几个配置」——配置一改，拿到的就是另一个
实例，不会出现「改了端点、连接还指着旧地址」。

**为什么缓存里只放 Provider、不放模型**：模型对象（``ChatOpenAI`` / ``OpenAIEmbeddings``）
在构造时就把连接池焊在一个事件循环上。Provider 被进程级缓存，模型若跟着被缓存，第二个
loop（Celery 每个任务一个、混合检索的向量分支一个）拿到的就是别人的连接池——那正是
``RuntimeError: Event loop is closed`` 的来源。所以 Provider 每次向注册表要「当前 loop
的客户端」，模型在这个客户端上现装。

本文件守的契约：

* 同配置两次 → 同一个 Provider 对象；改任何一个决定身份的字段 → 新对象；
* ``reset_*`` 之后重新装配（测试接缝，与 ``reset_keyword_search_provider_cache`` 同形）；
* 三种 Provider 都向注册表要客户端，而不是自己新建；
* Provider 不关注册表的客户端（谁的 loop 谁负责关）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from app.core import http_clients
from app.core.config import settings
from app.providers.embedding import get_embedding_provider, reset_embedding_provider_cache
from app.providers.llm import (
    RoleEndpoint,
    get_llm_provider,
    reset_llm_provider_cache,
    set_role_resolver,
)
from app.providers.rerank import get_rerank_provider, reset_rerank_provider_cache
from app.providers.rerank.noop import NoopRerankProvider

_RERANK_BASE_URL = "https://rerank.example.test/compatible-api/v1"


@pytest.fixture(autouse=True)
def _clean_caches() -> Iterator[None]:
    """每个用例从「什么都没缓存」出发：进程级缓存不许在用例之间串味。"""

    set_role_resolver(None)
    reset_llm_provider_cache()
    reset_rerank_provider_cache()
    reset_embedding_provider_cache()
    http_clients.reset_http_clients()
    yield
    set_role_resolver(None)
    reset_llm_provider_cache()
    reset_rerank_provider_cache()
    reset_embedding_provider_cache()
    http_clients.reset_http_clients()


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """一条确定的 ``.env`` 单供应商配置（改哪个字段由用例自己再说）。"""

    monkeypatch.setattr(settings, "LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "LLM_MODEL", "deepseek-flash")
    monkeypatch.setattr(settings, "LLM_STRONG_MODEL", "deepseek-v4-pro")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "LLM_TIMEOUT_SECONDS", 30.0)
    return monkeypatch


@pytest.fixture
def rerank_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """一条确定的专用重排端点配置。"""

    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", _RERANK_BASE_URL)
    monkeypatch.setattr(settings, "RERANK_API_KEY", "rerank-key")
    monkeypatch.setattr(settings, "RERANK_MODEL", "qwen3.7-text-rerank")
    monkeypatch.setattr(settings, "RERANK_MAX_CANDIDATES", 20)
    monkeypatch.setattr(settings, "RERANK_CHUNK_MAX_CHARS", 1024)
    monkeypatch.setattr(settings, "RERANK_TIMEOUT_SECONDS", 15.0)
    return monkeypatch


# ───────────────────────────────────────────────────── LLM Provider 缓存


@pytest.mark.asyncio
async def test_the_same_llm_config_is_assembled_once(llm_env: pytest.MonkeyPatch) -> None:
    """同配置两次必须是同一个对象：这就是「连续两次检索只装配一次」的实现。"""

    assert await get_llm_provider("fast") is await get_llm_provider("fast")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("DEEPSEEK_API_KEY", "sk-another"),
        ("LLM_MODEL", "deepseek-flash-2"),
        ("LLM_BASE_URL", "https://proxy.example.test/v1"),
        ("LLM_TIMEOUT_SECONDS", 5.0),
    ],
)
@pytest.mark.asyncio
async def test_changing_any_endpoint_field_yields_a_new_llm_provider(
    llm_env: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    """配置一改就得到新实例：缓存键必须覆盖 provider / base_url / model / key / 超时。"""

    before = await get_llm_provider("fast")
    llm_env.setattr(settings, field, value)

    assert await get_llm_provider("fast") is not before


@pytest.mark.asyncio
async def test_changing_the_role_endpoint_yields_a_new_llm_provider(llm_env: pytest.MonkeyPatch) -> None:
    """角色解析器指向的那一条变了（面板上改了指向），下一次装配就用新的那条。"""

    async def _none(_role: str) -> RoleEndpoint | None:
        return None

    set_role_resolver(_none)
    before = await get_llm_provider("fast")

    async def _ark(_role: str) -> RoleEndpoint | None:
        return RoleEndpoint(
            provider="ark",
            base_url="https://ark.example.test/v3",
            model="doubao-pro",
            api_key="sk-ark",
            config_id="config-ark",
        )

    set_role_resolver(_ark)

    after = await get_llm_provider("fast")
    assert after is not before
    assert after.model_name == "doubao-pro"


@pytest.mark.asyncio
async def test_the_strong_reasoning_effort_is_part_of_the_cache_key(llm_env: pytest.MonkeyPatch) -> None:
    """思考强度变了，强档装配出来的就是另一个模型：旧实例不许被复用。"""

    llm_env.setattr(settings, "LLM_REASONING_EFFORT", "off")
    before = await get_llm_provider("strong")

    llm_env.setattr(settings, "LLM_REASONING_EFFORT", "high")

    assert await get_llm_provider("strong") is not before


@pytest.mark.asyncio
async def test_roles_are_cached_separately(llm_env: pytest.MonkeyPatch) -> None:
    """fast 与 strong 是两档模型：共用一条缓存会串味。"""

    assert await get_llm_provider("fast") is not await get_llm_provider("strong")


@pytest.mark.asyncio
async def test_reset_clears_the_llm_provider_cache(llm_env: pytest.MonkeyPatch) -> None:
    """复位入口是测试接缝（与 ``reset_keyword_search_provider_cache`` 同一条纪律）。"""

    before = await get_llm_provider("fast")
    reset_llm_provider_cache()

    assert await get_llm_provider("fast") is not before


@pytest.mark.asyncio
async def test_the_cache_holds_the_provider_but_never_the_model(
    llm_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缓存里只有 Provider：模型是**每次调用**现装的（它带着一个 loop 的连接池）。

    这条是「缓存 Provider 但不缓存模型」的牙齿：工厂命中缓存时不该再装配任何模型，
    而取模型这件事每次都要真的发生一次。反过来写（缓存模型）在全量套件里是绿的，
    只会在「第二个事件循环」的部署里炸——那正是这张票要防的。
    """

    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    built: list[str] = []

    def _fake_init(model_name: str, **_kwargs: Any) -> FakeListChatModel:
        built.append(model_name)
        return FakeListChatModel(responses=["unused"])

    monkeypatch.setattr("app.providers.llm.init_chat_model", _fake_init)

    provider = await get_llm_provider("fast")
    assert built == [], "装配 Provider 时不该构造模型"

    assert provider.chat_model is not None
    assert provider.chat_model is not None
    assert built == ["deepseek-flash", "deepseek-flash"], "每次要用模型时现装一个"


# ───────────────────────────────────────────────────── 重排 Provider 缓存


def test_the_same_rerank_config_is_assembled_once(rerank_env: pytest.MonkeyPatch) -> None:
    assert get_rerank_provider() is get_rerank_provider()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("RERANK_BASE_URL", "https://rerank2.example.test/compatible-api/v1"),
        ("RERANK_API_KEY", "rerank-key-2"),
        ("RERANK_MODEL", "qwen3-rerank"),
        ("RERANK_MAX_CANDIDATES", 30),
        ("RERANK_CHUNK_MAX_CHARS", 512),
        ("RERANK_TIMEOUT_SECONDS", 5.0),
    ],
)
def test_changing_any_rerank_field_yields_a_new_provider(
    rerank_env: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    """实现 / 端点指纹 / 超时 / 候选上限 / 截断长度全部进键：改哪个都换一个实例。"""

    before = get_rerank_provider()
    rerank_env.setattr(settings, field, value)

    assert get_rerank_provider() is not before


def test_an_empty_rerank_provider_is_cached_too(rerank_env: pytest.MonkeyPatch) -> None:
    """关掉重排时也不许每次现造：空实现是同一个对象，路径与开启时一致。"""

    rerank_env.setattr(settings, "RERANK_ENABLED", False)

    assert isinstance(get_rerank_provider(), NoopRerankProvider)
    assert get_rerank_provider() is get_rerank_provider()


def test_reset_clears_the_rerank_provider_cache(rerank_env: pytest.MonkeyPatch) -> None:
    before = get_rerank_provider()
    reset_rerank_provider_cache()

    assert get_rerank_provider() is not before


# ───────────────────────────────────────────────────── 嵌入 Provider 缓存


def test_the_same_embedding_config_is_assembled_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """嵌入是每条检索必经的一步：每次现装一套客户端就是每请求一次 TLS 握手。"""

    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "langchain")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "text-embedding-test")

    assert get_embedding_provider() is get_embedding_provider()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("EMBEDDING_MODEL", "text-embedding-test-2"),
        ("EMBEDDING_DIMENSIONS", 1024),
        ("EMBEDDING_BATCH_SIZE", 7),
        ("MODEL_BASE_URL", "https://embed.example.test/v1"),
        ("MODEL_API_KEY", "sk-embed-2"),
    ],
)
def test_changing_any_embedding_field_yields_a_new_provider(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "langchain")
    before = get_embedding_provider()

    monkeypatch.setattr(settings, field, value)

    assert get_embedding_provider() is not before


def test_the_fallback_embedding_implementation_is_cached_by_its_own_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两套实现各自成键：换实现就是换一个对象，不会延续旧实现的状态。"""

    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "langchain")
    langchain_provider = get_embedding_provider()

    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "openai_compatible")
    fallback = get_embedding_provider()

    assert fallback is not langchain_provider
    assert fallback is get_embedding_provider()


def test_reset_clears_the_embedding_provider_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "langchain")
    before = get_embedding_provider()
    reset_embedding_provider_cache()

    assert get_embedding_provider() is not before


# ──────────────────────────────────── 客户端从注册表来（而不是每次新建）


def test_the_llm_provider_uses_the_current_loops_clients(llm_env: pytest.MonkeyPatch) -> None:
    """装配出来的模型必须挂在注册表给的客户端上：异步按 loop、同步进程级。"""

    async def _run() -> None:
        provider = await get_llm_provider("fast")
        model = provider.chat_model  # type: ignore[attr-defined]
        try:
            assert model.http_async_client is http_clients.get_http_client()
            assert model.http_client is http_clients.get_sync_http_client()
        finally:
            await http_clients.aclose_http_client()

    asyncio.run(_run())


def test_the_embedding_provider_uses_the_current_loops_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """嵌入同理：连接池是注册表的那一个，而不是每次现建的。"""

    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "langchain")

    async def _run() -> None:
        provider = get_embedding_provider()
        embeddings = provider.embeddings  # type: ignore[attr-defined]
        try:
            assert embeddings.http_async_client is http_clients.get_http_client()
            assert embeddings.http_client is http_clients.get_sync_http_client()
        finally:
            await http_clients.aclose_http_client()

    asyncio.run(_run())


def test_the_rerank_provider_posts_through_the_registry_client(
    rerank_env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重排没有注入客户端时走注册表：请求发在注册表的客户端上，且 Provider 不关它。"""

    seen: list[httpx.Request] = []

    def _responder(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]})

    def _builder() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_responder))

    monkeypatch.setattr(http_clients, "_build_async_client", _builder)
    chunk = {
        "chunk_id": "c1",
        "document_id": "d1",
        "kb_id": "kb-1",
        "title": "t",
        "content": "c",
        "score": 0.5,
        "rrf_score": 0.03,
    }

    async def _run() -> None:
        provider = get_rerank_provider()
        client = http_clients.get_http_client()
        try:
            rows = await provider.rerank(query="q", chunks=[chunk], top_n=1)
            assert [row["chunk_id"] for row in rows] == ["c1"]
            assert client.is_closed is False, "注册表的客户端归 loop 收尾，Provider 不许关它"
        finally:
            await http_clients.aclose_http_client()

    asyncio.run(_run())

    assert len(seen) == 1
    assert str(seen[0].url) == f"{_RERANK_BASE_URL}/reranks"

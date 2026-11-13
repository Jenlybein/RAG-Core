"""票 #77：请求只剩 ``kb_ids`` + ``query``，质量参数改由密钥配置提供。

票 #78 在本文件里换掉了两处**故意的临时断言**：命名档位曾经等于「服务端缺省」
（那时它们还没有预设），现在每一档都跑自己那条链路。两条接缝不变：

1. **纯函数直测**（本文件上半）：``profile_chain_of`` 把一份密钥配置快照展开成「这次调用
   用哪条链路」——模式、条数、重排、改写。它不碰数据库、不碰请求、不碰套餐，所以边界
   （缺键、值不认识、命名档位忽略手动参数）都在这里逐条钉住。
2. **ASGI 边界**（本文件下半）：同一件事在真实应用上的样子——旧字段进请求体拿
   20002、``custom`` 档的手动参数真的生效、命名档位跑的是它自己的链路。

门禁、裁剪与回显（套餐那一层）在 ``tests/test_profile_gating.py``：这里的用例一律挂最高
套餐，只验证「展开」。
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.auth import DEV_KEY_POLICY
from app.core.config import settings
from app.core.exceptions import ErrorCode
from app.models.key_policy import (
    PROFILE_BALANCED,
    PROFILE_CUSTOM,
    PROFILE_QUALITY,
    PROFILE_SPEED,
    KeyPolicySnapshot,
)
from app.models.plan import PLAN_PRESETS, PLAN_PRO
from app.services.result_collapse import OVERFETCH_FACTOR
from app.services.retrieval_options import (
    MAX_TOP_K,
    RetrievalOptions,
    ProfileChain,
    profile_chain_of,
)
from app.services.profile_policy import resolve_retrieval_options
from tests.fake_db import AsyncSessionAdapter, EmptySession, async_visible_kb
from tests.retrieval_identity import RetrievalIdentity

RETRIEVE_PATH = "/api/v1/rag/retrieve"
TENANT_ID = "tenant_demo"
KB_A = "kb-a"


# ───────────────────────────────────────────── 密钥配置 → 本次调用的参数（纯函数）


def _policy(
    *,
    profile: str = PROFILE_CUSTOM,
    include_parent: bool = True,
    expand_table_rows: bool = True,
    custom_options: tuple[tuple[str, object], ...] = (),
) -> KeyPolicySnapshot:
    """一份只关心「配置 → 参数」的密钥配置快照；其余字段取内置开发身份那份。"""

    return dataclasses.replace(
        DEV_KEY_POLICY,
        profile=profile,
        include_parent=include_parent,
        expand_table_rows=expand_table_rows,
        custom_options=custom_options,
    )


def _resolve(policy: KeyPolicySnapshot) -> RetrievalOptions:
    """最高套餐 + 一个库：本文件的纯函数用例只想看「展开」，不想撞门禁。"""

    return resolve_retrieval_options(policy=policy, plan=PLAN_PRESETS[PLAN_PRO], kb_count=1)


def test_custom_policy_manual_parameters_are_taken_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``custom`` 档：手动参数原样生效，两个形状开关读配置自己的那两列。

    重排 / 改写的**意图**写的是 true，所以这台机器必须装着这两项能力（否则生效值会是
    合取之后的 false——那是下一节 ``test_profile_gating`` 的事，不是「手动参数没读到」）。
    """

    # #116 起能力 = 开关 ∧ 端点配齐：只拨开关等于「说要重排但没接上端点」。
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "test-rerank-key")
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", True)

    options = _resolve(
        _policy(
            custom_options=(
                ("mode", "bm25"),
                ("top_k", 3),
                ("rerank", True),
                ("rewrite", True),
            ),
            include_parent=False,
            expand_table_rows=False,
        )
    )

    assert (
        options.mode,
        options.top_k,
        options.rerank,
        options.rewrite,
        options.fold_to_sections,
        options.expand_table_rows,
    ) == ("bm25", 3, True, True, False, False)


def test_custom_policy_missing_keys_fall_back_to_server_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺键按「这个键没给」处理，逐键回落到服务端缺省（不是整份作废）。"""

    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "vector")
    monkeypatch.setattr(settings, "TOP_K", 7)
    # #116 起能力 = 开关 ∧ 端点配齐：只拨开关等于「说要重排但没接上端点」。
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "test-rerank-key")
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", False)

    # 只给了 mode：top_k / 重排 / 改写都走部署缺省，形状开关仍读配置自己的列。
    options = _resolve(_policy(custom_options=(("mode", "bm25"),), expand_table_rows=False))

    assert options.mode == "bm25", "给了的键必须生效"
    assert options.top_k == 7, "没给的键回落到服务端缺省"
    assert (options.rerank, options.rewrite) == (True, False)
    assert options.fold_to_sections is True
    assert options.expand_table_rows is False


def test_custom_policy_unusable_values_fall_back_per_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """值不认识 / 越界按缺键处理。

    理由：``20002`` 是**请求参数**校验失败的意思，不能拿来报配置问题；而一条脏配置
    不该把调用当场打死（配置写入时已有自洽校验，运行时只做兜底）。``True`` 是
    ``int`` 的子类，所以 ``top_k=True`` 也必须被当成不可用值而不是 ``1``。
    """

    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "hybrid")
    monkeypatch.setattr(settings, "TOP_K", 5)

    for bad_mode, bad_top_k in (
        ("bing", 0),
        (None, True),
        (None, "3"),
        (None, None),
    ):
        custom_options: tuple[tuple[str, object], ...] = ()
        if bad_mode is not None:
            custom_options += (("mode", bad_mode),)
        if bad_top_k is not None:
            custom_options += (("top_k", bad_top_k),)

        options = _resolve(_policy(custom_options=custom_options))

        assert options.mode == "hybrid", f"mode={bad_mode!r} 应回落到服务端缺省"
        assert options.top_k == 5, f"top_k={bad_top_k!r} 应回落到服务端缺省"


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        # 三档的全部内容写在票面（#78）上，这里逐字抄一遍。
        (PROFILE_SPEED, ProfileChain(mode="vector", top_k=5, rerank=False, rewrite=False)),
        (PROFILE_BALANCED, ProfileChain(mode="hybrid", top_k=5, rerank=False, rewrite=False)),
        (PROFILE_QUALITY, ProfileChain(mode="hybrid", top_k=5, rerank=True, rewrite=True)),
    ],
)
def test_a_named_profile_is_its_chain_not_the_server_defaults(
    monkeypatch: pytest.MonkeyPatch, profile: str, expected: ProfileChain
) -> None:
    """命名档位 = 它自己那条链路（票 #78 换掉了「暂时等于服务端缺省」）。

    把部署缺省设成**与每一档都不同**的值，再把配置里塞满手动参数：结果仍然是预设那四项
    ——命名档位是完整替换，配置里的 ``custom_options`` 一个都不生效。
    """

    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "bm25")
    monkeypatch.setattr(settings, "TOP_K", 9)
    # #116 起能力 = 开关 ∧ 端点配齐：只拨开关等于「说要重排但没接上端点」。
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "test-rerank-key")
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", False)

    chain = profile_chain_of(
        _policy(
            profile=profile,
            custom_options=(("mode", "bm25"), ("top_k", 1)),
            include_parent=False,
            expand_table_rows=False,
        )
    )

    assert chain == expected


def test_the_shape_group_is_not_custom_only() -> None:
    """两个形状列是「结果形状组」：命名档位同样读它们（不是 ``custom`` 专属）。"""

    options = _resolve(_policy(profile=PROFILE_QUALITY, include_parent=False))

    assert options.fold_to_sections is False, "命名档位也读配置的形状列"
    assert options.expand_table_rows is True


def test_custom_top_k_above_the_hard_cap_is_clamped_not_silently_dropped() -> None:
    """条数超过平台硬上限：**夹到上限并回显**，不是悄悄回落到缺省。

    #77 时代这里是「越界按缺键处理」——那对一个只关心「有没有值」的解析器是合理的，
    但 #78 之后它变成了一条**看不见的降级**：调用方要 70 条、拿到 5 条，响应里没有
    一处说明。裁剪 + 回显是同一件事的可解释版本（``clipped`` 里会有 ``top_k``）。
    """

    options = _resolve(_policy(custom_options=(("top_k", MAX_TOP_K + 7),)))

    assert options.top_k == MAX_TOP_K
    assert options.tenant_policy is not None
    assert options.tenant_policy.clipped == ("top_k",)


def test_options_are_immutable() -> None:
    """值对象不可变：拿到它的人（组合根、日志、回显）改不动它。"""

    options = _resolve(_policy())

    with pytest.raises(dataclasses.FrozenInstanceError):
        options.top_k = 1  # type: ignore[misc]


# ───────────────────────────────────────────── ASGI 边界：请求只有两个字段


IDENTITY = RetrievalIdentity(TENANT_ID)


class _FakeEmbedding:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    def __init__(self) -> None:
        self.scope_calls: list[dict[str, Any]] = []

    async def similarity_search_scope(self, _db, _vector, *, tenant_id, index_versions, top_k):
        self.scope_calls.append(
            {"tenant_id": tenant_id, "index_versions": dict(index_versions), "top_k": top_k}
        )
        return [
            {
                "document_id": f"doc-{kb_id}",
                "chunk_id": f"chunk-{kb_id}",
                "kb_id": kb_id,
                "title": "错误码手册",
                "content": "错误码 E1024 表示证书过期。",
                "score": 0.9,
            }
            for kb_id in index_versions
        ]


class _FakeKeyword:
    available = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def keyword_search(self, query, *, tenant_id, index_versions, top_k):
        self.calls.append(
            {
                "query": query,
                "tenant_id": tenant_id,
                "index_versions": index_versions,
                "top_k": top_k,
            }
        )
        return [
            {
                "chunk_id": "chunk-hit",
                "document_id": "doc-hit",
                "kb_id": KB_A,
                "title": "错误码手册",
                "content": "错误码 E1024 表示证书过期。",
                "score": 12.5,
            }
        ]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """离线 ASGI 客户端 + 三个工厂替身；身份由 :data:`IDENTITY` 提供。"""

    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    embedding = _FakeEmbedding()
    vector = _FakeVectorStore()
    keyword = _FakeKeyword()

    monkeypatch.setattr(service, "get_embedding_provider", lambda: embedding)
    monkeypatch.setattr(service, "get_vector_store", lambda: vector)
    monkeypatch.setattr(service, "get_keyword_search_provider", lambda: keyword)
    # 可见性走异步仓储（第 11 步票 #127）：替身因此也得是 ``async def``。
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", async_visible_kb)

    from app.api.deps import get_current_key

    # 检索链路走异步依赖（第 11 步票 #127）：同步那条留给身份判定与其他路由。
    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: (
        AsyncSessionAdapter(EmptySession())
    )

    main.app.dependency_overrides[get_current_key] = IDENTITY.context
    IDENTITY.use()  # 每个用例从「服务端缺省」开始，不继承上一个用例的配置
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield {"client": client, "embedding": embedding, "vector": vector, "keyword": keyword}
    finally:
        main.app.dependency_overrides.clear()


def _post(client: TestClient, **body: Any) -> dict:
    payload: dict[str, Any] = {"kb_ids": [KB_A], "query": "E1024"}
    payload.update(body)
    response = client.post(RETRIEVE_PATH, json=payload)
    assert response.status_code == 200
    return response.json()


def test_the_request_model_carries_exactly_two_fields() -> None:
    """请求模型只有 ``kb_ids`` + ``query``：质量参数不再是「有没有默认值」的问题。"""

    from app.schemas.rag import RetrieveRequest

    assert set(RetrieveRequest.model_json_schema()["properties"]) == {"kb_ids", "query"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "bm25"),
        ("mode", "hybrid"),
        ("top_k", 5),
        ("top_k", 1),
        ("include_parent", False),
        ("include_parent", True),
        ("expand_table_rows", False),
        ("expand_table_rows", True),
    ],
)
def test_a_removed_field_in_the_body_is_a_validation_failure(
    fake: dict[str, Any], field: str, value: Any
) -> None:
    """旧调用方继续传被删的字段 → ``20002``，**不是悄悄忽略**。

    静默忽略比报错更糟：调用方会以为 ``mode=bm25`` 生效了，直到某天发现结果一直
    是混合链路。被拒的请求也不该走到召回（``vector.scope_calls == []``）。
    """

    body = _post(fake["client"], **{field: value})

    assert body["code"] == ErrorCode.REQUEST_VALIDATION_ERROR
    assert fake["vector"].scope_calls == []
    assert fake["embedding"].queries == []


def test_a_custom_policy_manual_parameters_take_effect_end_to_end(
    fake: dict[str, Any],
) -> None:
    """``custom`` 档的手动参数真的接管了这次调用：模式与条数都按配置走。"""

    IDENTITY.use(mode="bm25", top_k=2)

    body = _post(fake["client"])

    assert body["code"] == ErrorCode.SUCCESS
    assert body["data"]["metadata"]["mode"] == "bm25"
    assert body["data"]["metadata"]["top_k"] == 2
    assert fake["embedding"].queries == [], "bm25 不做向量化"
    assert fake["vector"].scope_calls == []
    # 折叠会把同一节的命中并成一条，所以召回要多取几倍；对外承诺的条数仍是 2。
    assert fake["keyword"].calls[0]["top_k"] == 2 * OVERFETCH_FACTOR


def test_a_custom_policy_shape_switch_reaches_the_recall_depth(
    fake: dict[str, Any],
) -> None:
    """不折叠时（``fold_to_sections=False``）连多取也关掉：召回深度回到 ``top_k``。"""

    IDENTITY.use(mode="vector", top_k=2, include_parent=False)

    _post(fake["client"])

    assert fake["vector"].scope_calls[0]["top_k"] == 2, "不折叠就不该多取"


def test_a_named_profile_policy_runs_its_own_chain(
    fake: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """命名档位在边界处就是它自己那条链路（票 #78 换掉了「暂时等于服务端缺省」）。

    部署缺省设成 ``bm25`` + 9 条，配置里还塞了 ``mode=bm25, top_k=1``：``balanced`` 档
    仍然走混合检索、返回 5 条——预设之外的东西一个都不生效。
    """

    monkeypatch.setattr(settings, "RETRIEVAL_MODE", "bm25")
    monkeypatch.setattr(settings, "TOP_K", 9)
    IDENTITY.use_policy(
        _policy(
            profile=PROFILE_BALANCED,
            custom_options=(("mode", "bm25"), ("top_k", 1)),
        )
    )

    body = _post(fake["client"])

    assert body["data"]["metadata"]["mode"] == "hybrid", "balanced 的链路是混合检索"
    assert body["data"]["metadata"]["top_k"] == 5, "预设的条数是 5，不跟 TOP_K 漂"
    assert fake["keyword"].calls, "混合链路里关键词一路真的被调了"
    assert fake["embedding"].queries, "混合链路里向量一路真的被调了"
    assert fake["keyword"].calls[0]["top_k"] != 1, "配置里的 top_k 不该漏进召回"


def test_the_composition_root_receives_the_resolved_options(
    fake: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """组合根收到的是**解析后的值对象**，不是请求里的四个字段。

    请求里已经没有这四个字段可读，这条用例把这件事钉在接缝上：入口交给
    ``rag_service.retrieve`` 的 ``options`` 必须已经由密钥配置解析完毕（并且带着回显要
    用的那份事实）。
    """

    service = importlib.import_module("app.services.rag_service")
    seen: dict[str, Any] = {}

    async def spy(
        _db: Any, *, tenant_id: str, kb_ids: list[str], query: str, options: Any
    ) -> dict:
        seen["options"] = options
        return {
            "query": query,
            "kb_ids": list(kb_ids),
            "retrieved_chunks": [],
            "metadata": {
                "mode": options.mode,
                "top_k": options.top_k,
                "vector_store": settings.VECTOR_STORE,
                "retrieval": {
                    "mode": options.mode,
                    "degraded": False,
                    "degraded_reason": None,
                    "empty_reason": "no_chunks_matched",
                    "rerank": None,
                },
            },
        }

    monkeypatch.setattr(service, "retrieve", spy)
    IDENTITY.use(mode="vector", top_k=4, include_parent=False, expand_table_rows=True)

    body = _post(fake["client"])

    options = seen["options"]
    assert body["code"] == ErrorCode.SUCCESS
    assert (options.mode, options.top_k) == ("vector", 4)
    assert (options.fold_to_sections, options.expand_table_rows) == (False, True)
    assert options.tenant_policy is not None
    assert options.tenant_policy.requested_profile == PROFILE_CUSTOM


def test_http_and_mcp_accept_literally_the_same_arguments() -> None:
    """两条入口字面一致：签名里有什么，模型就能填什么（ADR-0018）。"""

    from app.mcp_server import create_mcp_server
    from app.schemas.rag import RetrieveRequest

    class _FakeSession:
        def close(self) -> None:
            pass

    async def _retrieve(*_args: Any, **_kwargs: Any) -> dict:
        return {}

    mcp = create_mcp_server(
        # 票 #130 起只有一个会话工厂（``get_db`` 已删）：判定内核与检索链路共用它。
        get_async_db=lambda: AsyncSessionAdapter(_FakeSession()),
        retrieve=_retrieve,
        list_kbs=lambda *_a, **_k: [],
    )
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    mcp_properties = set(getattr(tools["rag_retrieve"], "parameters", {})["properties"])

    assert mcp_properties == set(RetrieveRequest.model_json_schema()["properties"]) == {
        "kb_ids",
        "query",
    }

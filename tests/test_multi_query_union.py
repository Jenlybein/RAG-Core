"""#31：改写开启时向量一路「原句 + 改写句」都搜，合成一个池再进既有 RRF。

两个接缝，都是仓库里既有的：

* 模块接缝 ``optimize_query``：断言 ``QueryProcessResult.recall_queries`` 的形状（顺序 /
  去重 / 失败语义），以及 ``effective_query`` 只供归因、不再有第三句；
* ASGI 接缝 ``POST /api/v1/rag/retrieve``：数据库依赖用 ``dependency_overrides`` 换掉，
  三个 Provider 工厂用离线替身换掉（与 ``tests/test_hybrid_retrieval.py`` 同一套做法）。
  全部替身不发网络、不连 PG / ES。

守的契约：

* 改写成功：嵌入收到**两句**（查询侧没有批量接口，故两次 ``embed_query`` 并发发出），
  向量库与关键词引擎**各查两次**、拿同一组句子（第 15 步起两路对称）；
* 并集收益：原句命中 A、改写句命中 B，A 与 B 都进向量池；
* 去重取优：同一 ``(kb_id, chunk_id)`` 只出现一次，保留名次更好的那份内容、
  分数取更高者，池按分数降序 + 确定性 tie-breaker 排好再截断；
* 改写失败：只剩原句一路，``degraded`` / ``degraded_reason`` 语义不变；
* ``mode=vector`` 单模式同样搜两句；``mode=bm25`` 不受影响；
* **回归红线**：``QUERY_REWRITE_ENABLED=false`` 时 hybrid / vector / bm25 三种 mode
  的响应全文 SHA-256 与 ``f1ddb46`` 上实跑取到的基线逐一相等。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.providers.llm.base import LLMProvider
from app.services.query.optimizer import (
    QueryProcessResult,
    direct_query,
    optimize_for_rewrite,
)
from app.services.query.rewrite import QueryRewrite
from tests.fake_db import AsyncSessionAdapter
from tests.retrieval_identity import RetrievalIdentity

RETRIEVE_PATH = "/api/v1/rag/retrieve"
REWRITE_PATH = "/api/v1/rag/rewrite"
TENANT_ID = "tenant_demo"
KB_A = "kb-a"

# 票 #77 起质量参数由密钥配置提供：请求助手把旧写法翻译成「造一个 custom 档的假身份」。
IDENTITY = RetrievalIdentity(TENANT_ID)

RAW_QUERY = "退钱怎么弄"
REWRITTEN = "退款的处理流程"

# 关闭改写时三种 mode 的响应全文 SHA-256。取值方式见 ``_BASELINE_SOURCE``：用本文件
# 同一个 ``_response_hashes`` 实跑采集，不是手写的期望字符串。
#
# **#78 重采过一次**：响应契约新增了 ``metadata.tenant_policy``（票 #78 的交付之一），
# 全文哈希必然改变——那是有意的契约变更，不是检索行为变化。检索内容那一半仍由本文件
# 其余用例守着（关闭改写时只搜一句、同一句、同一顺序），重采没有放宽任何一条。
#
# **第 7 步收尾又重采一次**：回显字段 ``requested_profile`` 的旧名已改成现在这个
# （ADR-0010「第四次修订」，旧名违反术语表）。同样只动名字、不动检索内容：新基线
# 仍然只比 #78 那版多这一个字段名的差别，其余字节未变。
#
# **第 13 步重采一次**（有意的契约变更）：``metadata.tenant_policy`` 改报参数本身，
# 删掉 ``requested_profile`` 与 ``rewrite`` 两个键（后端不再有档位、改写由端点决定）。
# 检索内容那一半仍由本文件其余用例守着（恒等原句、同一句、同一顺序）；重采只反映
# 回显形状的变化，没有放宽任何一条内容断言。
#
# **#101 没有重采**，而且刻意不重采：响应多出的 ``metadata.trace_id`` 每次调用都不同，
# 没法进黄金哈希，所以 ``_response_hashes`` 在比对前把它归一化成一个占位符。
_BASELINE_SOURCE = (
    "第 13 步重采（相对第 7 步：tenant_policy 删 requested_profile / rewrite 两键，改报参数本身）"
)
_BASELINE_SHA256: dict[str, str] = {
    "hybrid": "29beef4d43f0339d79228946ea9bd5e1b1511bf71f877cd64c113238686f3e80",
    "vector": "de7a20872bf43616ffae0e8bc95165e20517b8747f60ac02b31490312fd6e408",
    "bm25": "61274ae810f2725b596946aefd480f891f70615ada81d39ca245cd287875218c",
}

#: #101 起响应多出的那个字段（每次调用都不同）。比对前先把它**整段摘掉**，见 ``_without_trace_id``。
_TRACE_ID_RE = re.compile(r',"trace_id":"[0-9a-f]{32}"')

#: #116 起 ``metadata.retrieval.rerank`` 多出的两个**实现身份**键（``provider`` / ``model``）。
#: 它们与 ``trace_id`` 是同一类东西：有意的契约新增，不是检索行为变化。比对前整段摘掉，
#: 基线常量因此一个都不用重采——「除这两个新键外逐字节不变」由同一组数字继续证明。
_RERANK_IDENTITY_RE = re.compile(r',"provider":(?:null|"[^"]*"),"model":(?:null|"[^"]*")')

#: #147 起 ``metadata.tenant_policy`` 曾多出 ``orchestration``，比对时摘过一段（第七次修订）；
#: ADR-0029 收窄（#138）把它撤掉之后，**这一段摘除不需要了**——键不存在，响应文本直接
#: 回到基线，哈希常量仍然一个都没重采。也就是说这个键从来只是「被摘掉才对齐」，撤掉之后
#: 连摘都不用摘：这正是「它没碰过别的字节」的最终形态。

#: #207 起每条结果多出两个**交付形状**字段（``section_part`` / ``truncated``）：前者是
#: 「这是同一节的第几段」，后者是「正文被裁过没有」。它们与 ``trace_id`` 同一类东西——
#: 有意的契约新增，不是检索行为变化；比对前整段摘掉，基线常量因此一个都不用重采。
#: 顺序也是契约的一部分（两个键固定相邻出现），正则按位置钉住，改顺序就会红。
_UNIT_BOUNDARY_RE = re.compile(r',"section_part":\d+,"truncated":(?:true|false)')


# ─────────────────────────────────────────────────────────────── 替身


class _FakeResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value

    def all(self) -> list[Any]:
        return []

    def __iter__(self) -> Any:
        return iter(())


class _FakeDb:
    """只回答「库里有多少条可检索切片」；检索两侧都是替身，不真连 PostgreSQL。

    第 11 步票 #127 起检索跑在异步会话上：这个替身由 ``AsyncSessionAdapter``
    包成异步面（见 ``tests/fake_db.py``），语义与预置答案一行没改。
    """

    def execute(self, _statement: Any) -> _FakeResult:
        return _FakeResult(1)

    def close(self) -> None:
        pass


class _RecordingEmbedding:
    """查询侧替身：每次 ``embed_query`` 记下句子，并给该次调用一个可辨认的向量标记。

    ``concurrent_probe=True`` 时插入一次 ``await``，让 ``max_in_flight`` 能证明
    两次嵌入是并发发出而不是一前一后串行（查询侧没有批量接口，并发是为了不付两次 RTT）。
    """

    def __init__(self, *, concurrent_probe: bool = False) -> None:
        self.queries: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._concurrent_probe = concurrent_probe

    async def embed_query(self, query: str) -> list[float]:
        marker = float(len(self.queries) + 1)
        self.queries.append(query)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self._concurrent_probe:
            await asyncio.sleep(0.01)
        self.in_flight -= 1
        return [marker, 0.2, 0.3]


class _FakeVectorStore:
    """按向量标记返回不同候选池，用来证明两句各自都真的查了向量库。

    标记 ``1.0`` = 第一句（原句），``2.0`` = 第二句（改写句）。没配 ``pools`` 时
    两次都返回同一份 ``rows``，供不关心候选来源的用例使用。
    """

    def __init__(
        self,
        pools: dict[float, list[dict]] | None = None,
        *,
        rows: list[dict] | None = None,
    ) -> None:
        self.pools = pools or {}
        self.rows = rows or []
        self.seen: list[float] = []
        self.calls: list[dict[str, Any]] = []

    async def similarity_search_scope(
        self, db, query_vector, *, tenant_id, kb_ids, top_k
    ) -> list[dict]:
        marker = float(query_vector[0])
        self.seen.append(marker)
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "kb_ids": list(kb_ids),
                "top_k": top_k,
            }
        )
        kb_id = next(iter(kb_ids), None)
        rows = self.pools.get(marker, self.rows)
        return [{**row, "kb_id": row.get("kb_id", kb_id)} for row in rows]

    async def similarity_search(self, *args: Any, **kwargs: Any) -> list[dict]:
        raise AssertionError("必须走一次范围召回，不许按库循环")


class _FakeKeyword:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.available = True
        self.queries: list[str] = []

    async def keyword_search(self, query, *, tenant_id, kb_ids, top_k) -> list[dict]:
        self.queries.append(query)
        return [dict(row) for row in self.rows]


class _RewriteLLM(LLMProvider):
    """改写阶段的模型替身；重排默认关闭，本文件不会走到别的 schema。"""

    def __init__(self, *, rewritten: str | None = None, error: Exception | None = None) -> None:
        self._rewritten = rewritten
        self._error = error

    async def chat_structured(self, schema, *, system: str, user: str) -> Any:
        if self._error is not None:
            raise self._error
        return QueryRewrite(query=self._rewritten or "")

    async def chat_json(self, *, system: str, user: str) -> dict:  # pragma: no cover
        raise AssertionError("改写只走结构化输出")


def _vector_row(
    chunk_id: str, *, score: float, content: str | None = None, kb_id: str | None = None
) -> dict:
    row: dict[str, Any] = {
        "document_id": f"doc-{chunk_id}",
        "chunk_id": chunk_id,
        "title": f"title-{chunk_id}",
        "content": content if content is not None else f"content-{chunk_id}",
        "score": score,
    }
    if kb_id is not None:
        row["kb_id"] = kb_id
    return row


def _keyword_row(chunk_id: str, *, score: float = 9.0) -> dict:
    return {**_vector_row(chunk_id, score=score), "kb_id": KB_A}


@contextmanager
def _offline_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding: Any = None,
    vector: Any = None,
    keyword: Any = None,
    llm: LLMProvider | None = None,
    kb_settings: dict[str, dict] | None = None,
) -> Iterator[dict[str, Any]]:
    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    embedding = embedding or _RecordingEmbedding()
    vector = vector or _FakeVectorStore(rows=[_vector_row("c1", score=0.9)])
    keyword = keyword if keyword is not None else _FakeKeyword(rows=[_keyword_row("c1")])

    monkeypatch.setattr(service, "get_embedding_provider", lambda: embedding)
    monkeypatch.setattr(service, "get_vector_store", lambda: vector)
    monkeypatch.setattr(service, "get_keyword_search_provider", lambda: keyword)

    tables = kb_settings or {}

    async def _kb_for_tenant(_db: Any, kb_id: str, *, tenant_id: str) -> Any:
        return SimpleNamespace(
            id=kb_id,
            tenant_id=tenant_id,
            settings=tables.get(kb_id) or {},
            name="",
            description="",
            # 真行上有的两列（票 #141）：检索范围从库行里读生效版本。
        )

    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", _kb_for_tenant)
    if llm is not None:
        _install_llm(monkeypatch, llm)

    from app.api.deps import get_current_key

    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: AsyncSessionAdapter(
        _FakeDb()
    )
    main.app.dependency_overrides[get_current_key] = IDENTITY.context
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield {
                "client": client,
                "embedding": embedding,
                "vector": vector,
                "keyword": keyword,
            }
    finally:
        main.app.dependency_overrides.clear()


def _response(client: TestClient, *, path: str = RETRIEVE_PATH, **body: Any) -> Any:
    payload: dict[str, Any] = {
        "kb_ids": [KB_A],
        "query": RAW_QUERY,
    }
    # 票 #77：``mode`` / ``top_k`` / ``include_parent`` 这批旧请求字段改由密钥配置表达，
    # 助手把它们翻译成「造一个 custom 档的假身份」，调用点保持原样。
    payload.update(IDENTITY.apply({"mode": "hybrid", "top_k": 5, **body}))
    return client.post(path, json=payload)


def _chunk_ids(body: dict) -> list[str]:
    return [chunk["chunk_id"] for chunk in body["data"]["retrieved_chunks"]]


def _metadata(body: dict) -> dict:
    return body["data"]["metadata"]["retrieval"]


def _without_trace_id(text: str) -> str:
    """把新字段 ``metadata.trace_id`` **整段摘掉**，其余字节原样保留。

    #101 起响应多一个 ``trace_id``（ADR-0010 第五次修订），它的值是本次检索的派生 id，
    天然每次都不同。黄金哈希要守的是「**除该字段外**逐字节不变」，所以：

    * 摘掉的是 ``,"trace_id":"…"`` 这一整段（该字段在 ``metadata`` 里是**最后**一个键，
      摘掉不吃掉任何相邻的逗号或括号），而不是把它替换成一个占位值——替换之后响应仍然
      多一个键，哈希就不可能等于旧基线；
    * 用**字符串替换**而不是重新序列化 JSON：重新序列化会顺手动到空白与转义，那就不是
      「逐字节」了；
    * 摘掉之后哈希**仍等于 #78 那版基线**：这正是本次改动没有碰到别的字节的证据，
      因此基线常量一个都不用重采（重采会把「只多了这一个字段」这件事从证据里抹掉）。

    #116 起 ``metadata.retrieval.rerank`` 又多出两个身份键（``provider`` / ``model``，
    见 :data:`_RERANK_IDENTITY_RE`），处理方式与 ``trace_id`` **完全相同**：它们是有意的
    契约新增，摘掉之后仍与旧基线逐字节相同。这两段摘除各自都要求「至少命中一次」——
    否则这条比对会退化成「什么也没摘、什么也没比」。

    #147 起 ``metadata.tenant_policy`` 曾多出 ``orchestration``，当时按同一手法摘过一段；
    #138（ADR-0029）把它撤掉之后那一段摘除**不再需要**：键不存在，响应文本直接与 #78 那版
    基线逐字节相同，三个哈希常量一次都没重采。这条哈希因此同时证明了「撤销只删了那个键、
    没有碰别的字节」。
    """

    without_trace = _TRACE_ID_RE.sub("", text)
    without_rerank = _RERANK_IDENTITY_RE.sub("", without_trace)
    return _UNIT_BOUNDARY_RE.sub("", without_rerank)


def _response_hashes(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """同一份 ASGI 请求，三种 mode 各自的响应全文 SHA-256（``trace_id`` 已摘掉）。

    这个函数同时是基线采集脚本的入口：在 ``f1ddb46`` 上直接调它取到的字典，
    就是上面 ``_BASELINE_SHA256`` 的来源，因此测试与基线共用完全相同的替身与请求。
    """

    # 显式三个开关全关：黄金哈希守的是「默认路径」，不能被别的用例改过的 settings 影响。
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", False)
    monkeypatch.setattr(settings, "RERANK_ENABLED", False)

    hashes: dict[str, str] = {}
    for mode in ("hybrid", "vector", "bm25"):
        with _offline_client(monkeypatch) as fake:
            response = _response(fake["client"], mode=mode)
        assert response.status_code == 200
        assert _TRACE_ID_RE.search(response.text), "摘除必须真的命中一次，否则这条比对是空比对"
        # 回显里那个键（#147 加的、#138 撤掉的）**不许再出现**：它一旦回来，下面三个
        # 哈希就会与基线不等，而这条断言把失败原因直接指到「谁把性质上不属于默认路径的
        # 东西塞回了 tenant_policy」。
        assert '"orchestration"' not in response.text, "tenant_policy 回显不许再出现这个键"
        if mode == "hybrid":
            # 重排关着时 ``rerank`` 仍是字典（applied=false …），身份键是 null；它们必须
            # 真的出现过一次，否则「摘掉之后仍等于旧基线」证明不了「只多了这两个键」。
            assert _RERANK_IDENTITY_RE.search(response.text), "身份键摘除也必须真的命中"
        # 交付形状的两个新字段（#207）也必须真的出现过一次，否则「摘掉之后仍等于旧基线」
        # 证明不了「只多了这两个键」——只是演示了一次干净的比对。
        assert _UNIT_BOUNDARY_RE.search(response.text), "交付形状键的摘除也必须真的命中"
        hashes[mode] = hashlib.sha256(_without_trace_id(response.text).encode("utf-8")).hexdigest()
    return hashes


def _switches(monkeypatch: pytest.MonkeyPatch, *, rewrite: bool) -> None:
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", rewrite)
    monkeypatch.setattr(settings, "RERANK_ENABLED", False)


def _install_llm(monkeypatch: pytest.MonkeyPatch, llm: LLMProvider) -> None:
    """把大模型出口换成**协程**替身（票 #137：真货要 ``await``，替身不许比它宽容）。"""

    async def _fake_llm(role: str = "fast") -> LLMProvider:
        return llm

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _fake_llm)


# ───────────────────────────────── 接缝一：两条加工的产出形状


def test_direct_retrieve_is_an_identity_transform() -> None:
    """`/rag/retrieve`：向量一路只有原句，三个查询字段恒等变换、零模型调用。"""

    result = direct_query(RAW_QUERY)

    assert isinstance(result, QueryProcessResult)
    assert result.recall_queries == (RAW_QUERY,)
    assert result.raw_query == result.effective_query == RAW_QUERY
    assert result.rewrite_applied is False


def test_rewrite_yields_raw_then_effective_in_a_fixed_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/rag/rewrite` 成功：``(原句, 改写句)`` 顺序固定；effective 仍给重排。"""

    _switches(monkeypatch, rewrite=True)
    _install_llm(monkeypatch, _RewriteLLM(rewritten=REWRITTEN))

    result = asyncio.run(optimize_for_rewrite(query=RAW_QUERY))

    assert result.recall_queries == (RAW_QUERY, REWRITTEN)
    assert all(query.strip() for query in result.recall_queries)
    assert result.effective_query == REWRITTEN
    assert result.rewrite_applied is True


def test_rewrite_same_as_raw_is_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    """两句相同时只留一句：不白付第二次嵌入，也不让同一句占两个名次。"""

    _switches(monkeypatch, rewrite=True)
    _install_llm(monkeypatch, _RewriteLLM(rewritten=RAW_QUERY))

    result = asyncio.run(optimize_for_rewrite(query=RAW_QUERY))

    assert result.recall_queries == (RAW_QUERY,)
    assert result.effective_query == RAW_QUERY


def test_the_same_sentences_feed_both_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """**第 15 步的核心断言**：召回用句只有一组，两路都用它。

    第 15 步之前这里是「向量拿两句、关键词拿第三句（同义词扩展后的改写句）」。现在
    同义词下线，两条路对「这次到底问的是什么」必须看法一致——这正是不对称最难查的那类
    偏差：它不报错，只是字面那一路悄悄少了一半词面。
    """

    _switches(monkeypatch, rewrite=True)
    _install_llm(monkeypatch, _RewriteLLM(rewritten=REWRITTEN))

    result = asyncio.run(optimize_for_rewrite(query=RAW_QUERY))

    assert result.recall_queries == (RAW_QUERY, REWRITTEN)
    # 只有一组：没有任何「只喂字面那一路」的第三句。
    assert [name for name in vars(result) if "quer" in name] == [
        "raw_query",
        "effective_query",
        "recall_queries",
    ]


def test_a_failed_rewrite_is_a_50003(monkeypatch: pytest.MonkeyPatch) -> None:
    """改写模型失败 → 整单 50003，不再「只剩原句一路 + 降级原因」。"""

    from app.core.exceptions import AppError, ErrorCode
    import pytest

    _switches(monkeypatch, rewrite=True)
    _install_llm(monkeypatch, _RewriteLLM(error=TimeoutError("模型超时")))

    with pytest.raises(AppError) as caught:
        asyncio.run(optimize_for_rewrite(query=RAW_QUERY))

    assert caught.value.code is ErrorCode.MODEL_CALL_FAILED


# ───────────────────────────────── 接缝二：ASGI 上的并集检索


def test_rewrite_on_embeds_and_searches_both_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开启成功：**两路各两次**——嵌入收到两句、向量库查两次、关键词也查两次。"""

    _switches(monkeypatch, rewrite=True)
    llm = _RewriteLLM(rewritten=REWRITTEN)
    vector = _FakeVectorStore(
        pools={
            1.0: [_vector_row("A", score=0.91)],
            2.0: [_vector_row("B", score=0.82)],
        }
    )
    keyword = _FakeKeyword(rows=[])

    with _offline_client(
        monkeypatch,
        vector=vector,
        keyword=keyword,
        llm=llm,
    ) as fake:
        body = _response(fake["client"], path=REWRITE_PATH, include_parent=False).json()

    assert body["code"] == 0
    # 查询侧 EmbeddingProvider 没有批量接口：两句 = 两次 embed_query（并发发出）。
    assert fake["embedding"].queries == [RAW_QUERY, REWRITTEN]
    assert fake["vector"].seen == [1.0, 2.0], "两句各做一次范围召回"
    assert len(fake["vector"].calls) == 2
    assert fake["keyword"].queries == [RAW_QUERY, REWRITTEN], "关键词一路也逐句搜这两句"
    assert _metadata(body)["degraded"] is False


def test_union_keeps_hits_that_only_one_of_the_two_queries_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并集收益的替身证据：原句只命中 A、改写句只命中 B，A 与 B 都进向量池。

    这是 q13（改写赢）与 q16（原句赢）能同时吃到而不必二选一的最小模型，
    不需要真模型、真库，也不需要跑评测数字。
    """

    _switches(monkeypatch, rewrite=True)
    llm = _RewriteLLM(rewritten=REWRITTEN)
    vector = _FakeVectorStore(
        pools={
            1.0: [_vector_row("A", score=0.91)],
            2.0: [_vector_row("B", score=0.82)],
        }
    )

    with _offline_client(
        monkeypatch, vector=vector, keyword=_FakeKeyword(rows=[]), llm=llm
    ) as fake:
        body = _response(fake["client"], path=REWRITE_PATH, include_parent=False).json()

    chunks = body["data"]["retrieved_chunks"]
    assert set(_chunk_ids(body)) == {"A", "B"}, "两边的命中都进了向量池"
    assert all(chunk["vector_rank"] is not None for chunk in chunks)
    assert all(chunk["bm25_rank"] is None for chunk in chunks), "关键词一路没喂结果"


def test_duplicate_hit_is_deduplicated_taking_better_rank_and_higher_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 chunk 两句都命中：只出现一次，保留名次更好的那份内容、分数取更高者。

    X 在原句里是第 1 名（0.40），在改写句里是第 2 名（0.90）。合并后应当是
    「第 1 名那份的 content」+「0.90 这个更高分」，而不是二选一整条覆盖。
    """

    _switches(monkeypatch, rewrite=True)
    llm = _RewriteLLM(rewritten=REWRITTEN)
    vector = _FakeVectorStore(
        pools={
            1.0: [_vector_row("X", score=0.40, content="from-raw")],
            2.0: [
                _vector_row("Z", score=0.80, content="from-rewrite-z"),
                _vector_row("X", score=0.90, content="from-rewrite-x"),
            ],
        }
    )

    with _offline_client(
        monkeypatch, vector=vector, keyword=_FakeKeyword(rows=[]), llm=llm
    ) as fake:
        body = _response(fake["client"], path=REWRITE_PATH, include_parent=False).json()

    chunks = body["data"]["retrieved_chunks"]
    assert len(chunks) == 2, "同一 (kb_id, chunk_id) 只留一条"
    matches = [chunk for chunk in chunks if chunk["chunk_id"] == "X"]
    assert len(matches) == 1
    assert matches[0]["content"] == "from-raw", "同名取 rank 更小的那一次"
    assert matches[0]["vector_score"] == 0.90, "分数取更高者"
    assert _chunk_ids(body) == ["X", "Z"], "池按分数降序，名次可复现"


def test_multi_query_embeddings_are_issued_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """查询侧没有批量接口，两次 ``embed_query`` 必须并发发出（约 1 次 RTT）。"""

    _switches(monkeypatch, rewrite=True)
    embedding = _RecordingEmbedding(concurrent_probe=True)
    vector = _FakeVectorStore(
        pools={
            1.0: [_vector_row("A", score=0.91)],
            2.0: [_vector_row("B", score=0.82)],
        }
    )

    with _offline_client(
        monkeypatch,
        embedding=embedding,
        vector=vector,
        keyword=_FakeKeyword(rows=[]),
        llm=_RewriteLLM(rewritten=REWRITTEN),
    ) as fake:
        _response(fake["client"], path=REWRITE_PATH, include_parent=False)

    assert embedding.queries == [RAW_QUERY, REWRITTEN]
    assert embedding.max_in_flight == 2, "两次嵌入同时在飞，而不是串行"


def test_a_failed_rewrite_is_a_50003_and_touches_no_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第 13 步：改写失败整单 50003，一次嵌入 / 向量检索都不发，不再静默降级。"""

    _switches(monkeypatch, rewrite=True)
    vector = _FakeVectorStore(rows=[_vector_row("c1", score=0.9)])

    with _offline_client(
        monkeypatch,
        vector=vector,
        keyword=_FakeKeyword(rows=[]),
        llm=_RewriteLLM(error=RuntimeError("模型服务 502")),
    ) as fake:
        body = _response(fake["client"], path=REWRITE_PATH, include_parent=False).json()

    assert body["code"] == 50003, "模型失败不再包成 code 0 的降级结果"
    assert body["data"] is None, "失败响应没有 data，也就没有降级原因字段"
    assert fake["embedding"].queries == [], "被拒的请求不该白烧一次嵌入"
    assert fake["vector"].calls == []


def test_vector_single_mode_also_searches_both_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mode=vector`` 单模式同样吃并集；``bm25`` 完全不碰嵌入。"""

    _switches(monkeypatch, rewrite=True)
    vector = _FakeVectorStore(
        pools={
            1.0: [_vector_row("A", score=0.91)],
            2.0: [_vector_row("B", score=0.82)],
        }
    )

    with _offline_client(
        monkeypatch,
        vector=vector,
        keyword=_FakeKeyword(rows=[_keyword_row("k1")]),
        llm=_RewriteLLM(rewritten=REWRITTEN),
    ) as fake:
        vector_body = _response(
            fake["client"], path=REWRITE_PATH, mode="vector", include_parent=False
        ).json()

    assert fake["embedding"].queries == [RAW_QUERY, REWRITTEN]
    assert fake["vector"].seen == [1.0, 2.0]
    assert set(_chunk_ids(vector_body)) == {"A", "B"}
    assert fake["keyword"].queries == [], "单模式不碰另一路"


def test_one_failing_embedding_degrades_the_whole_vector_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两句里有一句嵌入失败：整路降级，绝不拿另一句的半份结果冒充完整池。"""

    _switches(monkeypatch, rewrite=True)

    class _OneFails(_RecordingEmbedding):
        async def embed_query(self, query: str) -> list[float]:
            if query == REWRITTEN:
                raise RuntimeError("embedding endpoint refused the connection")
            return await super().embed_query(query)

    vector = _FakeVectorStore(
        pools={
            1.0: [_vector_row("A", score=0.91)],
            2.0: [_vector_row("B", score=0.82)],
        }
    )

    with _offline_client(
        monkeypatch,
        embedding=_OneFails(),
        vector=vector,
        keyword=_FakeKeyword(rows=[_keyword_row("k1")]),
        llm=_RewriteLLM(rewritten=REWRITTEN),
    ) as fake:
        body = _response(fake["client"], path=REWRITE_PATH, include_parent=False).json()

    assert body["code"] == 0
    assert _metadata(body)["degraded"] is True
    assert _metadata(body)["degraded_reason"] == "vector search failed"
    assert _chunk_ids(body) == ["k1"], "只剩关键词一路，向量半份结果不参与融合"


def test_single_query_keeps_each_caller_historical_tie_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭 / 失败时同分多行：单模式稳定排序、混合分支按 kb_id / chunk_id 兜底。

    两条调用方历史上排序口径不同，黄金哈希的替身没有同分行，覆盖不到这一维；
    把它们钉住，才敢说「关闭时行为不变」不只是「关闭时那份特定响应不变」。
    """

    _switches(monkeypatch, rewrite=False)
    # 两条同分行：库返回顺序是 Z → A；稳定排序应保留它，tiebreak 应换成 A → Z。
    same_score_rows = [
        _vector_row("Z", score=0.5, kb_id=KB_A),
        _vector_row("A", score=0.5, kb_id=KB_A),
    ]

    with _offline_client(
        monkeypatch,
        vector=_FakeVectorStore(rows=same_score_rows),
        keyword=_FakeKeyword(rows=[]),
    ) as fake:
        single = _response(fake["client"], mode="vector", include_parent=False).json()
        hybrid = _response(fake["client"], mode="hybrid", include_parent=False).json()

    assert _chunk_ids(single) == ["Z", "A"], "单模式：稳定排序，同分保留库返回顺序"
    assert _chunk_ids(hybrid) == ["A", "Z"], "混合向量分支：同分按 kb_id / chunk_id 兜底"


# ───────────────────────────────── 回归红线：关闭时逐字节一致


def test_rewrite_off_response_is_byte_identical_to_pre_change_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``QUERY_REWRITE_ENABLED=false``：三种 mode 的响应全文与采集基线逐字节相同。

    基线常量取于 ``_BASELINE_SOURCE``，用本文件同一个 ``_response_hashes`` 实跑采集
    （#78 因响应新增 ``tenant_policy``、第 7 步收尾因回显字段改名各重采一次，见那段注释；
    #101 的 ``trace_id`` 靠归一化吸收，不重采——见 ``_normalized_text``）。
    这条守的是本票最容易出的那类 bug：并集分支顺手让**关闭时**也多搜一句，让所有默认
    用户白付一次嵌入费用。

    它与票 #78 的「部署开关关着时行为逐字节不变」是同一条红线的两个方向：那边守
    「档位不会凭空变出部署没配的 Provider」，这边守「没有一键顺手改掉默认路径」。
    """

    assert "PENDING" not in _BASELINE_SHA256.values(), "基线必须来自实跑，不能留占位"
    assert _response_hashes(monkeypatch) == _BASELINE_SHA256

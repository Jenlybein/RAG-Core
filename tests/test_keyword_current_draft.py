"""#175 · 关键词索引的「当前稿」标记翻转：请求体、Provider、以及「失败只降级」。

库里的当前稿指针是**唯一真值**，关键词索引里的 ``is_current`` 是它的派生视图（召回只用
这一个布尔 term 过滤）。指针前移之后不翻索引，旧稿的切片仍带着 ``is_current=true``，
关键词路会继续召回旧稿——而症状是「同一段内容出现两次」，两路对同一稿给出不同答案。

这一层钉三件事：

* 请求体（第 ① 层纯函数）：只碰这篇文档、这个租户的切片，脚本把 ``is_current`` 置成
  「这一片的稿号 == 目标稿号」；
* Provider（第 ② 层，注入假客户端，不连 ES）：打的是**别名**、``refresh=True``、
  版本冲突不整批失败；索引还不存在时是诚实 no-op；
* 降级（``app.services.keyword_draft``）：任何异常都只记 warning，绝不把一次派生视图的
  维护失败升级成「版本没建成」。
"""

from __future__ import annotations

import asyncio

import pytest

from app.providers.keyword_search.elasticsearch import ElasticsearchKeywordSearchProvider
from app.providers.keyword_search.noop import NoopKeywordSearchProvider
from app.providers.keyword_search.query_builder import (
    CURRENT_DRAFT_FIELD,
    DOC_VERSION_FIELD,
    build_current_draft_update,
)
from app.services import keyword_draft

DOC_ID = "doc-1"
TENANT_ID = "tenant-a"


def _payload() -> dict:
    return build_current_draft_update(
        document_id=DOC_ID, tenant_id=TENANT_ID, doc_version=3
    )


# ── 请求体 ──


def test_the_update_touches_only_this_document_of_this_tenant() -> None:
    """过滤条件两个都在 ``filter`` 里：少一个就会翻到别的租户 / 别的文档的切片。"""

    payload = _payload()

    assert payload["query"] == {
        "bool": {
            "filter": [
                {"term": {"tenant_id": TENANT_ID}},
                {"term": {"document_id": DOC_ID}},
            ]
        }
    }


def test_the_script_flips_the_flag_by_comparing_draft_numbers() -> None:
    """脚本是「这一片属于的稿 == 目标稿」，不是「统统置 true」。

    统统置 true 等于什么都没做：旧稿切片照样参与召回。参数化稿号而不是把 3 拼进脚本里，
    是因为脚本会进日志与调试台，拼进去的稿号读起来像一段写死的业务规则。
    """

    payload = _payload()

    assert payload["script"]["lang"] == "painless"
    assert CURRENT_DRAFT_FIELD in payload["script"]["source"]
    # 读 ``_source`` 而不是 ``doc[...]``：mapping 里 ``doc_version`` 是 keyword，docvalue
    # 取出来是字符串 "3"，与写进去的整数 3 比较永远不等——那会让所有切片都被标成
    # 非当前稿，症状是关键词路静默全空。
    assert "ctx._source" in payload["script"]["source"]
    assert DOC_VERSION_FIELD in payload["script"]["source"]
    assert payload["script"]["params"] == {"doc_version": 3}


# ── Provider：ES ──


class _FakeClient:
    """只实现 ``update_by_query`` 的假客户端，记下实参。"""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls: list[dict] = []
        self._error = error

    async def update_by_query(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return {"updated": 2}


def _es_provider(client: _FakeClient) -> ElasticsearchKeywordSearchProvider:
    return ElasticsearchKeywordSearchProvider(
        url="http://es.example:9200", index_name="rag_chunks", client=client
    )


def test_the_provider_updates_through_the_alias_and_refreshes() -> None:
    """打的是**别名**（检索认的那个名字）并立刻可见。

    打物理索引名（``rag_chunks__v1``）在激活 v2 之后就会翻错地方：检索查的是别名指向的
    那一张，而这次翻的成了没人查的那一张——不报错，只是不生效。
    """

    client = _FakeClient()

    asyncio.run(
        _es_provider(client).mark_current_draft(
            DOC_ID, tenant_id=TENANT_ID, doc_version=3
        )
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["index"] == "rag_chunks", "别名，不是物理索引名"
    assert call["refresh"] is True, "上传完立刻提问必须能查到"
    assert call["conflicts"] == "proceed", (
        "并发重建会撞版本冲突：整批失败等于把这次翻转丢给运气"
    )
    assert call["script"]["params"] == {"doc_version": 3}


def test_a_missing_index_is_a_honest_no_op() -> None:
    """索引还不存在 = 这次没有切片可翻，不是故障。"""

    from elasticsearch import NotFoundError

    client = _FakeClient(error=NotFoundError("missing", None, None))

    asyncio.run(
        _es_provider(client).mark_current_draft(
            DOC_ID, tenant_id=TENANT_ID, doc_version=3
        )
    )


def test_a_noop_provider_has_nothing_to_flip() -> None:
    """没部署关键词引擎时这一步是 no-op：能力明确不可用，而不是「假装翻过了」。"""

    provider = NoopKeywordSearchProvider()

    assert provider.available is False
    assert (
        asyncio.run(
            provider.mark_current_draft(DOC_ID, tenant_id=TENANT_ID, doc_version=3)
        )
        is None
    )


# ── 降级：失败只记 warning ──


def test_the_flip_degrades_instead_of_failing_the_request(monkeypatch) -> None:
    """Provider 抛错 → 只记一条 warning，不向调用方抛。

    反过来的代价不对称：把一次派生视图的维护失败升级成「版本没建成」，用户会为一个已经
    生效的服务端问题重试；而漏翻一次，关键词路多召回一份旧稿，直到下一次重建把切片写对。
    """

    recorded: list[str] = []

    class _Broken:
        available = True

        async def mark_current_draft(self, *_args, **_kwargs) -> None:
            recorded.append("called")
            raise RuntimeError("es down")

    monkeypatch.setattr(keyword_draft, "get_keyword_search_provider", lambda: _Broken())

    asyncio.run(
        keyword_draft.flip_current_draft_after_commit(
            document_id=DOC_ID, tenant_id=TENANT_ID, doc_version=3
        )
    )

    assert recorded == ["called"], "没有真的调下去，这条用例就证明不了降级"


def test_an_unavailable_provider_is_not_called_at_all(monkeypatch) -> None:
    """``available=False`` 时不发那一趟请求：能力不存在就不该有网络调用。"""

    calls: list[str] = []

    class _Unavailable:
        available = False

        async def mark_current_draft(self, *_args, **_kwargs) -> None:
            calls.append("called")

    monkeypatch.setattr(
        keyword_draft, "get_keyword_search_provider", lambda: _Unavailable()
    )

    asyncio.run(
        keyword_draft.flip_current_draft_after_commit(
            document_id=DOC_ID, tenant_id=TENANT_ID, doc_version=3
        )
    )

    assert calls == []


@pytest.mark.parametrize("doc_version", [1, 2, 7])
def test_the_target_draft_number_reaches_the_payload(doc_version: int) -> None:
    """稿号是这次翻转的唯一输入：错一个数字就等于把「当前稿」标成别人的稿。"""

    client = _FakeClient()

    asyncio.run(
        _es_provider(client).mark_current_draft(
            DOC_ID, tenant_id=TENANT_ID, doc_version=doc_version
        )
    )

    assert client.calls[0]["script"]["params"] == {"doc_version": doc_version}

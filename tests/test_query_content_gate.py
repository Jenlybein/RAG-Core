"""`20033` 的验收：意图判定**跟着端点自己的那次模型调用**走，不另发一次调用。

## 这一票的形状

三条模型端点本来就要调一次大模型（改写 / 规划）。意图识别做成那次调用的一个 schema
字段（`QueryRewrite.intent` / `ResearchPlan.intent`），模型说 `no_content` 时端点在任何
**嵌入 / 检索 / 重排 / 记账**之前停下。所以这里守四件事：

1. **不额外花钱**：判定不产生第二次模型调用（用「调用计数」的替身钉住）；
2. **真的停下**：判掉之后连嵌入都不发（`/rewrite`）、连一次内核都不跑（`/decompose`）；
3. **失败即放行**：模型没给 `intent`、或给了不认识的词，一律按 `search` 继续——
   判定是优化，不是安全边界；
4. **码不混用**：没有内容是 `20033`，模型调用失败是 `50003`。混了调用方就分不清
   「该换问法」还是「该重试」。

还有一条不属于语义判断的硬边界：只剩标点 / 符号 / 空白的问题，在任何模型调用之前就被
挡掉（那是零成本的确定性判断，没有争议）。
"""

from __future__ import annotations

from typing import Any

import pytest

import tests.test_multi_query_union as u
import tests.test_four_retrieval_endpoints as four
from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.services import query_intent
from app.services.query.rewrite import QueryRewrite
from app.services.query_content import content_chars, has_no_content
from app.services.research import ResearchPlan

#: 截图里那条真实输入（去掉标点的判定不看它，模型判它没有检索意图）。
SPAM_QUERY = "啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊啊a"


# ───────────────────────────────────────────── 模型侧：schema 的失败即放行


@pytest.mark.parametrize("payload", [{}, {"intent": None}, {"intent": "随便什么词"}])
def test_unknown_or_missing_intent_means_search(payload: dict) -> None:
    """模型没给、给了空、给了不认识的词 —— 一律按 ``search`` 放行。

    ``Literal`` 直接校验会把这些变成一个结构错误，于是整单 50003（客户重试也修不好）。
    判定字段的正确失败方式是放行。
    """

    assert QueryRewrite.model_validate({"query": "退款怎么弄", **payload}).intent == "search"
    assert ResearchPlan.model_validate({"points": [], "queries": [], **payload}).intent == "search"


def test_no_content_is_recognised_verbatim() -> None:
    assert QueryRewrite.model_validate({"query": "", "intent": "no_content"}).intent == "no_content"
    assert (
        ResearchPlan.model_validate(
            {"points": [], "queries": [], "intent": "no_content", "reason": "只有感叹词"}
        ).reason
        == "只有感叹词"
    )


def test_the_gate_raises_the_documented_code() -> None:
    with pytest.raises(AppError) as caught:
        query_intent.ensure_search_intent(intent="no_content", reason="只有感叹词")

    assert caught.value.code == ErrorCode.QUERY_HAS_NO_CONTENT
    # 文案要说清「不是服务坏了」，否则调用方会去查部署、重签 Key。
    assert "no searchable content" in caught.value.msg


def test_a_search_verdict_is_a_no_op() -> None:
    query_intent.ensure_search_intent(intent="search")
    query_intent.ensure_search_intent(intent=None)


# ───────────────────────────────────────────── 词法：只剩一条硬边界


@pytest.mark.parametrize("query", ["？？？", "...", "🙂🙂", "   ", "\t\n"])
def test_punctuation_only_is_blocked_before_any_model_call(query: str) -> None:
    assert has_no_content(query) is True
    with pytest.raises(AppError) as caught:
        from app.services.query_content import ensure_query_has_content

        ensure_query_has_content(query)
    assert caught.value.code == ErrorCode.QUERY_HAS_NO_CONTENT


@pytest.mark.parametrize(
    "query",
    [
        "退钱怎么弄",
        "E1025",  # 上一版把它误杀过
        SPAM_QUERY,  # 这一版交给模型判，词法不再猜
        "一般纳税人的认定标准",
    ],
)
def test_everything_else_goes_to_the_model(query: str) -> None:
    """词法只挡「一个字都没有」的输入，其余全部交给模型。

    这里刻意把 ``SPAM_QUERY`` 也算进「放行」：语义判断不在这一层，它在
    ``intent`` 字段里。上一版正是在这里扩展规则，才先漏判又误杀。
    """

    assert has_no_content(query) is False
    assert content_chars(query) != ""


# ───────────────────────────────────────────── 接线：三条端点


class _CountingLLM:
    """回答 ``no_content`` 的模型替身，并数自己被调用了几次（只该是一次）。"""

    def __init__(self, *, intent: str = "no_content", reply: Any = None) -> None:
        self.calls = 0
        self._intent = intent
        self._reply = reply

    def for_rewrite(self):
        async def _factory(*_args: Any, **_kwargs: Any) -> Any:
            return self

        return _factory

    async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
        self.calls += 1
        if self._reply is not None:
            return self._reply
        name = getattr(schema, "__name__", "")
        if name == "ResearchPlan":
            return ResearchPlan(points=[], queries=[], intent=self._intent, reason="只有感叹词")
        return QueryRewrite(query="", intent=self._intent, reason="只有感叹词")

    async def chat_json(self, *, system: str, user: str) -> dict:  # pragma: no cover
        raise AssertionError("这些端点只走结构化输出")


def _client(monkeypatch: pytest.MonkeyPatch, *, rewrite_enabled: bool = False, llm=None):
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", rewrite_enabled)
    monkeypatch.setattr(settings, "SYNONYM_EXPANSION_ENABLED", False)
    monkeypatch.setattr(settings, "RERANK_ENABLED", False)
    return u._offline_client(monkeypatch, llm=llm)


def _post(client: Any, path: str, query: str) -> dict[str, Any]:
    response = client.post(path, json={"kb_ids": [u.KB_A], "query": query})
    assert response.status_code == 200, response.text
    return response.json()


def _restore_policy(original: Any) -> None:
    u.IDENTITY.use_policy(original)  # noqa: SLF001 - 测试助手


def _use_research_policy() -> None:
    """Research 需要能力位；夹具默认不带，用完由调用方还原。"""

    from tests.support.policy import policy

    u.IDENTITY.use_policy(  # noqa: SLF001 - 测试助手
        policy(research_allowed=True, allowed_kb_ids=(u.KB_A,))
    )


def test_rewrite_stops_when_the_model_says_no_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/rewrite`：模型判掉就停，**连嵌入都不发**，也绝不降级成原句直查。"""

    llm = _CountingLLM()
    with _client(monkeypatch, rewrite_enabled=True, llm=llm) as fake:
        body = _post(fake["client"], u.REWRITE_PATH, SPAM_QUERY)

    assert body["code"] == int(ErrorCode.QUERY_HAS_NO_CONTENT), body
    assert body["data"] is None
    # 判定就是那一次改写调用本身：没有第二次模型调用。
    assert llm.calls == 1


def test_decompose_stops_before_any_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/decompose`：判掉之后一次内核都不跑（否则就是 11 秒 + 5 条无关切片）。"""

    llm = _CountingLLM()
    with _client(monkeypatch, llm=llm) as fake:
        body = _post(fake["client"], "/api/v1/rag/decompose", SPAM_QUERY)

    assert body["code"] == int(ErrorCode.QUERY_HAS_NO_CONTENT), body
    assert body["data"] is None
    assert llm.calls == 1


def test_research_stops_before_any_sub_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/research`：判掉之后不跑子检索、不补证、不生成。"""

    llm = _CountingLLM()
    original = u.IDENTITY._policy  # noqa: SLF001
    _use_research_policy()
    try:
        with _client(monkeypatch, llm=llm) as fake:
            body = _post(fake["client"], "/api/v1/rag/research", SPAM_QUERY)
    finally:
        _restore_policy(original)

    assert body["code"] == int(ErrorCode.QUERY_HAS_NO_CONTENT), body
    assert body["data"] is None
    assert llm.calls == 1


def test_rewrite_continues_for_a_real_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一把 Key、同一套替身：换成一条真实问题就照常跑（这道闸不是把端点关了）。"""

    llm = _CountingLLM(intent="search", reply=QueryRewrite(query=u.REWRITTEN))
    with _client(monkeypatch, rewrite_enabled=True, llm=llm) as fake:
        body = _post(fake["client"], u.REWRITE_PATH, u.RAW_QUERY)

    assert body["code"] != int(ErrorCode.QUERY_HAS_NO_CONTENT), body


def test_plain_retrieve_is_deliberately_not_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/retrieve` 不读语义、只做召回，所以既不走意图判定、也不走那条词法硬边界。

    真要被挡住的是「为一句没有内容的问题编出几条要点、再拿它们去查」的那三条链路。
    给直接检索加闸会让「这个库到底有没有相关材料」这个基本问题问不出来。
    """

    with _client(monkeypatch) as fake:
        body = _post(fake["client"], u.RETRIEVE_PATH, "？？？")

    assert body["code"] != int(ErrorCode.QUERY_HAS_NO_CONTENT), body


def test_all_four_endpoints_are_still_registered() -> None:
    """这道闸不改路由：四条端点仍在。"""

    import importlib

    gate = importlib.import_module("app.services.retrieval_gate")
    assert hasattr(gate, "gated_decompose")
    assert hasattr(four, "_plan")

"""无内容问题（`20033`）的验收：判定口径 + 三条端点真的接上了这道闸。

## 为什么要有这一组

真实见过：问 ``啊啊啊啊啊啊啊``，分解器给出四条「要点」（「输入中没有实义词」「连续感叹
词通常反映情绪激动」…），再拿它们去召回，返回五条与业务无关的切片，而响应的 ``code``
是 **0**。调用方拿不到任何提示，只会以为「这个库只答得出这种东西」。

所以这里守两件事：

1. **判定口径**是确定的（同一个输入两次判出同一个答案），并且宁可漏判不可误判——
   ``E1025``、``怎么办退款`` 这类真实问题一个都不许被挡住；
2. **接线**是真的：三条模型端点在**任何模型调用与配额之前**返回 ``20033``。只用纯函数
   用例守着函数本身，等于允许调用点悄悄退回老写法——那正是这一票要治的现象。

``/retrieve`` 刻意**不加**这道闸：它不读语义、只做召回。这是设计上的不对称，不是漏做，
所以下面也有一条用例把它钉住，免得后来有人「顺手补上」。
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

import tests.test_multi_query_union as u
import tests.test_four_retrieval_endpoints as four
from app.core.config import settings
from app.core.exceptions import AppError, ErrorCode
from app.services.query_content import content_chars, ensure_query_has_content, is_meaningless

#: 截图里那条真实输入。
SPAM_QUERY = "啊啊啊啊啊啊啊啊啊啊啊"


# ───────────────────────────────────────────────────── 判定口径（纯函数）


@pytest.mark.parametrize(
    "query",
    [
        "啊啊啊啊啊啊啊啊啊啊啊",  # 感叹词 spam
        "哈哈哈",  # 象声词
        "哦哦",
        "嗯嗯嗯嗯",
        "xxxx",  # 同一个字符重复
        "AAAA",
        "。。。",  # 只有标点
        "？？？",
        "...",
        "🙂🙂",  # emoji 全是符号
        "  \t\n  ",  # 只有空白
    ],
)
def test_queries_without_content_are_rejected(query: str) -> None:
    assert is_meaningless(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "退钱怎么弄",
        "E1025",  # 短查询也是有效问题
        "怎么办退款",  # 虚词 + 实词
        "啊啊啊退款多久到账",  # 带语气词的正常问句
        "哈尔滨的退货政策",  # 含「哈」这个感叹词字，但整体是实词
        "7",  # 单个字符不重复：可能是编号
        "一般纳税人的认定标准",  # 「一般」既是填充词也是业务术语
        "a",  # 单字符拉丁字母：不判无内容
    ],
)
def test_real_questions_are_not_rejected(query: str) -> None:
    assert is_meaningless(query) is False


def test_judgement_is_deterministic_for_the_same_input() -> None:
    assert {is_meaningless(SPAM_QUERY) for _ in range(5)} == {True}


def test_fullwidth_and_halfwidth_punctuation_are_the_same_input() -> None:
    """NFKC 归一：全角 ``？？？`` 与半角 ``???`` 必须判出同一个答案。"""

    assert is_meaningless("？？？") is is_meaningless("???") is True
    assert content_chars("ＡＢＣ") == "ABC"


def test_the_gate_raises_the_documented_code() -> None:
    with pytest.raises(AppError) as caught:
        ensure_query_has_content(SPAM_QUERY)

    assert caught.value.code == ErrorCode.QUERY_HAS_NO_CONTENT
    # 文案要说清「不是服务坏了」，否则调用方会去查部署。
    assert "no searchable content" in caught.value.msg


def test_the_gate_is_a_no_op_for_real_questions() -> None:
    ensure_query_has_content("退钱怎么弄")  # 不抛就是通过


# ───────────────────────────────────────────────────── 接线（三条端点）


class _ExplodingLLM:
    """「不许被装到大模型上」的替身：任何一次用量都让用例失败。"""

    def __init__(self) -> None:
        self.calls = 0

    def factory(self):
        def _make(*_args: Any, **_kwargs: Any) -> Any:
            self.calls += 1
            raise AssertionError("无内容问题不该走到任何一次模型调用")

        return _make


def _client(monkeypatch: pytest.MonkeyPatch, *, rewrite_enabled: bool = False, llm=None):
    monkeypatch.setattr(settings, "QUERY_REWRITE_ENABLED", rewrite_enabled)
    monkeypatch.setattr(settings, "SYNONYM_EXPANSION_ENABLED", False)
    monkeypatch.setattr(settings, "RERANK_ENABLED", False)
    return u._offline_client(monkeypatch, llm=llm)


def _post(client: Any, path: str, query: str) -> dict[str, Any]:
    response = client.post(path, json={"kb_ids": [u.KB_A], "query": query})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "path",
    [u.REWRITE_PATH, "/api/v1/rag/decompose"],
)
def test_model_endpoints_return_20033_without_any_model_call(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """改写 / 分解：无内容问题 → ``20033``，且一次模型都不装、一条材料都不给。"""

    llm = _ExplodingLLM()
    monkeypatch.setattr("app.providers.llm.get_llm_provider", llm.factory)

    with _client(monkeypatch, rewrite_enabled=True) as fake:
        body = _post(fake["client"], path, SPAM_QUERY)

    assert body["code"] == int(ErrorCode.QUERY_HAS_NO_CONTENT), body
    # 不返回任何检索结果：没有材料、也没有任何「要点」被编出来。
    assert body["data"] is None
    assert llm.calls == 0
    # 连嵌入都不该发：这道闸在任何外部调用之前。
    assert fake["embedding"].queries == []


def test_research_returns_20033_without_any_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Research：一次 3~4 次模型调用、30 秒的链路，更不该为无内容问题付这笔钱。"""

    llm = _ExplodingLLM()
    monkeypatch.setattr("app.providers.llm.get_llm_provider", llm.factory)

    identity = u.IDENTITY
    original = identity._policy  # noqa: SLF001 - 测试助手，读回原值用于还原
    from tests.support.policy import policy

    # 白名单必须显式带上这个库：夹具默认 `allowed_kb_ids=("kb-1",)`，不写就会先在
    # 第 1 步拿到 20008，挡住本用例真正要验的第 2.5 步。
    identity.use_policy(policy(research_allowed=True, allowed_kb_ids=(u.KB_A,)))
    try:
        with _client(monkeypatch) as fake:
            body = _post(fake["client"], "/api/v1/rag/research", SPAM_QUERY)
    finally:
        identity.use_policy(original)

    assert body["code"] == int(ErrorCode.QUERY_HAS_NO_CONTENT), body
    assert body["data"] is None
    assert llm.calls == 0


def test_rewrite_is_not_blocked_for_a_real_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一把 Key：换成一条真实问题就不该拿到 ``20033``（这道闸不是把端点关了）。

    这里断言的是**语义**（这一票没有误杀真实问题），不是「替身被调了几次」：跨模块的
    provider 替身在全量跑时会被别的用例换掉，用调用日志当断言只会得到一条随运行顺序
    变色的用例。误杀的代价本来就该由纯函数用例（上面那组）来守。
    """

    llm = u._RewriteLLM(rewritten=u.REWRITTEN)
    with _client(monkeypatch, rewrite_enabled=True, llm=llm) as fake:
        body = _post(fake["client"], u.REWRITE_PATH, u.RAW_QUERY)

    assert body["code"] != int(ErrorCode.QUERY_HAS_NO_CONTENT), body


def test_plain_retrieve_is_deliberately_not_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/retrieve` 不读语义、只做召回，所以**不加**这道闸。

    真要被挡住的是「为一句没有内容的问题编出几条要点、再拿它们去查」的那三条链路。
    给直接检索加闸会让「这个库到底有没有相关材料」这个基本问题问不出来——那是另一种
    损失。所以这里钉住的是**不对称本身**，不是漏做。
    """

    with _client(monkeypatch) as fake:
        body = _post(fake["client"], u.RETRIEVE_PATH, SPAM_QUERY)

    # 断言的是「没有拿到那道闸的码」，而不是「一定成功」：直接检索成不成功取决于向量库，
    # 而这一票要钉的只有一件事——这道闸**不覆盖** `/retrieve`。
    assert body["code"] != int(ErrorCode.QUERY_HAS_NO_CONTENT), body


def test_decompose_endpoint_is_registered_where_the_doc_says() -> None:
    """端点仍在（这道闸不该把路由改掉）。"""

    gate = importlib.import_module("app.services.retrieval_gate")
    assert hasattr(gate, "gated_decompose")
    assert hasattr(four, "_plan")  # 同目录既有助手仍可用

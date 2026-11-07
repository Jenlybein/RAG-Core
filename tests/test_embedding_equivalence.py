"""嵌入等价性：同一批 10 条文本，「LangChain 分批」与「原有直连逐条」得到同一组向量。

票 21 的验收项「同一批 10 条文本，分批调用与逐条调用相比余弦相似度 ≥ 0.9999」以前
只有一份人工报告，没有可重跑的测试——本文件把它钉住。

比较的两侧与生产代码同源：

* **分批侧**：``LangChainEmbedding``（``EMBEDDING_PROVIDER=langchain``，默认实现），
  批次大小故意小于 10（4 + 4 + 2），否则「分批」这件事根本没发生；
* **逐条侧**：``OpenAICompatibleEmbedding``（第 1 步的直连实现，``batch_size=1``）。
  它是回退分支，不是历史遗留：两套实现必须给出同一组向量，回退才有意义。

默认套件**必须 skip**：这个文件会真的打 embedding 端点（要 key、要网络、要花钱）。
守卫是显式开关 ``RAG_INTEGRATION_EMBEDDING=1`` **加上**凭据齐备——一份默认的
``pytest`` 不该因为「这台机器恰好有 key」就发起真实网络调用。跑法::

    $env:RAG_INTEGRATION_EMBEDDING = "1"
    # 并在 .env 或环境变量里给好 MODEL_BASE_URL / MODEL_API_KEY / EMBEDDING_MODEL
    uv run pytest -m integration tests/test_embedding_equivalence.py -q
"""

from __future__ import annotations

import asyncio
import math
import os

import pytest

from app.core.config import settings
from app.providers.embedding import _build_openai_embeddings
from app.providers.embedding.langchain_embeddings import LangChainEmbedding
from app.providers.embedding.openai_compatible import OpenAICompatibleEmbedding

# 等价性的门槛（票 21 验收项）：向量会逐位相同，这里留一点浮点余量。
MIN_COSINE = 0.9999

# 批次大小必须**小于**文本条数，否则「分批调用」退化成一次调用，比对就没有意义了。
BATCH_SIZE = 4

# 10 条互不相同的短文本：内容差异要够大，顺序错了才会被下面的交叉比对抓住。
TEXTS = [
    "退款申请提交后三个工作日内到账。",
    "错误码 E1024 表示证书已经过期。",
    "型号 XR-2000 的整机保修期为两年。",
    "发票抬头只能开具公司名称，不能开个人。",
    "换货需要保留原包装与全部配件。",
    "订单超过 30 天不支持无理由退货。",
    "运费险只覆盖签收后 7 天内的退货运费。",
    "售后网点周末营业时间为上午十点到下午四点。",
    "电池属于易耗件，保修期为六个月。",
    "开具增值税专用发票需要提供一般纳税人证明。",
]


def _guard_reason() -> str | None:
    """默认套件里 skip 的理由；返回 ``None`` 才允许真的发请求。"""

    if os.environ.get("RAG_INTEGRATION_EMBEDDING") != "1":
        return "RAG_INTEGRATION_EMBEDDING is not 1; embedding equivalence test skipped"

    missing = [
        name
        for name in ("MODEL_BASE_URL", "MODEL_API_KEY")
        if not str(getattr(settings, name, "") or "").strip()
    ]
    if missing:
        return f"{', '.join(missing)} not configured; embedding equivalence test skipped"
    return None


_GUARD_REASON = _guard_reason()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_GUARD_REASON is not None, reason=_GUARD_REASON or ""),
]


def _cosine(left: list[float], right: list[float]) -> float:
    """余弦相似度。两侧都是 embedding 向量，范数不为 0。"""

    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm


def _batched() -> list[list[float]]:
    """生产默认实现：LangChain 抽象 + 我们自己分批。"""

    provider = LangChainEmbedding(
        embeddings=_build_openai_embeddings(),
        batch_size=BATCH_SIZE,
        dimensions=settings.EMBEDDING_DIMENSIONS,
    )
    return asyncio.run(provider.embed_texts(TEXTS))


def _one_by_one() -> list[list[float]]:
    """原有直连实现逐条调用：``batch_size=1`` 才是「逐条」。"""

    provider = OpenAICompatibleEmbedding(
        base_url=settings.MODEL_BASE_URL,
        api_key=settings.MODEL_API_KEY,
        model=settings.EMBEDDING_MODEL,
        dimensions=settings.EMBEDDING_DIMENSIONS,
        batch_size=1,
    )
    return asyncio.run(provider.embed_texts(TEXTS))


def test_batched_langchain_matches_one_by_one_direct_calls() -> None:
    """维度一致、顺序一致、逐条余弦 ≥ 0.9999。"""

    batched = _batched()
    single = _one_by_one()

    # 1) 条数与维度：少一条是错位，维度变了是换了模型或漏传 dimensions。
    assert len(batched) == len(TEXTS) == len(single)
    assert {len(vector) for vector in batched} == {settings.EMBEDDING_DIMENSIONS}
    assert {len(vector) for vector in single} == {settings.EMBEDDING_DIMENSIONS}

    # 2) 逐条比对：同一位置的向量必须几乎相同。
    cosines = [_cosine(batched[index], single[index]) for index in range(len(TEXTS))]
    assert min(cosines) >= MIN_COSINE, f"最小余弦 {min(cosines)} 低于 {MIN_COSINE}"

    # 3) 顺序一致：把两条实现错开一位再比，相似度必须明显掉下来。
    #    没有这一步，「都返回了 10 条、逐条都很像」可能只是向量彼此高度相似，
    #    上面的断言就成了空的。
    crossed = [
        _cosine(batched[index], single[(index + 1) % len(TEXTS)]) for index in range(len(TEXTS))
    ]
    assert max(crossed) < MIN_COSINE, "错位比对仍然 ≥ 0.9999：这些向量彼此太像，顺序无法验证"

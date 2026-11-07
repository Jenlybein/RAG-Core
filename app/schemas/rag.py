"""检索接口的请求 / 响应模型。

响应里有 retrieved_chunks 与 metadata，没有 context_text，也没有答案。
第 1 步起：头部带占位 API Key，``tenant_id`` 由服务端从 Key 解出、
不进请求体；``kb_ids`` 是列表，单库也写 ``["kb_a"]``。

第 4 步票 3 起请求体多了两个字段，其余质量参数（各路召回条数、融合算法、
改写 / 同义词 / 重排开关）仍全部是服务端配置——它们一旦可调，同一档位在不同
调用方那里就是不同的检索链路，评测与复现都做不了。

``mode`` 本票只允许 ``vector`` / ``bm25``。``hybrid`` 留到票 4：先把允许值
放进来、行为却没做好，比不支持更糟——调用方会以为自己在测混合检索。
"""

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings

# ``top_k`` 的硬上下界：下界保证「至少召回一条」，上界防止一个请求把整个库拖回来。
# 它是接口契约里的常量，不进配置：改了它，调用方拿到的结果条数上限就变了。
MIN_TOP_K = 1
MAX_TOP_K = 50

RetrievalMode = Literal["vector", "bm25"]


def _default_mode() -> RetrievalMode:
    # 「缺省值来自服务端配置」这句话的实现。配置里的值由 Settings 的校验器在
    # **启动时**验过一遍（配错就起不来），所以这里不必再抛一次——这里的 cast 只对
    # 类型检查器负责，不是运行时守卫。
    return cast(RetrievalMode, settings.RETRIEVAL_MODE)


def _default_top_k() -> int:
    return settings.TOP_K


class RetrieveRequest(BaseModel):
    """调用参数；tenant_id 从 Key 解，硬传会被拒（extra=forbid）。"""

    model_config = ConfigDict(extra="forbid")

    kb_ids: list[str] = Field(min_length=1)
    query: str = Field(min_length=1)
    # 缺省取服务端配置（本票默认 vector，票 4 才翻回 hybrid）。
    mode: RetrievalMode = Field(default_factory=_default_mode)
    top_k: int = Field(default_factory=_default_top_k, ge=MIN_TOP_K, le=MAX_TOP_K)


class RetrievedChunk(BaseModel):
    """一条检索结果。

    ``score`` 是「本次最终排序分」：本票单模式下就是该路的原始分，票 4 起混合
    模式会是融合分、重排后是重排分——调用方要读具体分数请读 ``vector_score`` /
    ``bm25_score``。没走的那一路，分数与名次是 ``null`` 而不是 0：
    「没命中」与「命中但分数为 0」必须可区分。
    """

    document_id: str
    chunk_id: str
    title: str
    content: str
    score: float
    kb_id: str | None = None
    vector_score: float | None = None
    bm25_score: float | None = None
    vector_rank: int | None = None
    bm25_rank: int | None = None
    rrf_score: float | None = None


class RetrieveData(BaseModel):
    query: str
    kb_ids: list[str]
    retrieved_chunks: list[RetrievedChunk]
    metadata: dict

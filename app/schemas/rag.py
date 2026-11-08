"""检索接口的请求 / 响应模型。

响应里有 retrieved_chunks 与 metadata，没有 context_text，也没有答案。
第 1 步起：头部带占位 API Key，``tenant_id`` 由服务端从 Key 解出、
不进请求体；``kb_ids`` 是列表，单库也写 ``["kb_a"]``。

第 4 步票 3 起请求体多了两个字段，其余质量参数（各路召回条数、融合算法、
``rrf_k``、改写 / 同义词 / 重排开关）仍全部是服务端配置——它们一旦可调，同一档位
在不同调用方那里就是不同的检索链路，评测与复现都做不了。

``mode`` 自票 4 起三种取值都真可用：``vector`` / ``bm25`` / ``hybrid``。
``hybrid`` 的融合常数 ``k`` 写死在服务端（``app/services/retrieval/fusion.py``），
这里刻意**没有**对应的请求字段：传了会被 ``extra="forbid"`` 直接拒掉。
"""

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings

# ``top_k`` 的硬上下界：下界保证「至少召回一条」，上界防止一个请求把整个库拖回来。
# 它是接口契约里的常量，不进配置：改了它，调用方拿到的结果条数上限就变了。
MIN_TOP_K = 1
MAX_TOP_K = 50

# 这份 ``Literal`` 必须与 ``app.core.config.SUPPORTED_RETRIEVAL_MODES`` 保持一致；
# ``Literal`` 无法从元组展开，两处写两遍是语言限制，由一致性测试钉住。
RetrievalMode = Literal["vector", "bm25", "hybrid"]


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
    # 缺省取服务端配置（票 4 起默认 hybrid）。
    mode: RetrievalMode = Field(default_factory=_default_mode)
    top_k: int = Field(default_factory=_default_top_k, ge=MIN_TOP_K, le=MAX_TOP_K)
    # 折叠开关（ADR-0013）。默认开：默认关着就没人会开，small-to-big 等于没做。
    #
    # 关掉 = **不折叠**，返回小片，响应与折叠之前逐字一致——留给「只要精确到
    # 那一行的引用」的调用方。注意关掉的不只是「不挂字段」：它连按节归组也不做。
    include_parent: bool = True
    # 行级表片是否折回它所属的整表。关掉时每个行级片各自成一条结果。
    expand_table_rows: bool = True


class RetrievedChunk(BaseModel):
    """一条检索结果。

    ``score`` 是「本次最终排序分」：单模式下是该路的原始分，混合模式下是融合分
    （重排接上后是重排分）——调用方要读具体分数请读 ``vector_score`` /
    ``bm25_score``。没命中的那一路，分数与名次是 ``null`` 而不是 0：
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
    # 重排分（0~1）。只在重排真的发生时出现；与 ``score`` 同值，但留成独立字段
    # 是为了让「这一路的分数」与「最终排序分」在契约上分开——前三路分数都这么给。
    rerank_score: float | None = None
    # 这条结果代表的**节**（ADR-0013）。折叠之后一条结果 = 一节，不是一片，
    # 所以身份是 section_id；`chunk_id` 降级为「代表片」——让它浮上来的那一片。
    # 没有章节路径的切片（无标题文档）为 null，此时它自己就是一个单元。
    section_id: str | None = None
    # 这一节是被哪几片顶上来的：溯源用，不是主键。折叠会丢掉其余片的内容，
    # 但要能回答「为什么是这一节」。
    source_chunk_ids: list[str] = Field(default_factory=list)
    # 这次上下文给了多大范围：neighbors / parent / table / chunk（兜底，见折叠函数）。
    expansion: str | None = None
    # 这一节的章节路径：引用要能显示「出自哪一节」。折叠后 parent 那层没了，
    # 所以路径直接平铺在这条结果上。
    heading_path: str | None = None
    # 引用定位：PDF 的物理页码；Markdown 无页码时为 null。
    page: int | None = None
    # 稿号（ADR-0005）：检索结果每条都带，审计时能回答「当时引用的是哪一稿」。
    doc_version: int | None = None
    # 这一片是用哪种策略切的：跨库检索时用来解释片长差异。
    split_strategy: str | None = None


class RetrieveData(BaseModel):
    query: str
    kb_ids: list[str]
    retrieved_chunks: list[RetrievedChunk]
    metadata: dict

"""检索接口的请求 / 响应模型。

响应里有 retrieved_chunks 与 metadata，没有 context_text，也没有答案。
第 1 步起：头部带 API Key，``tenant_id`` 由服务端从 Key 解出、
不进请求体；``kb_ids`` 是列表，单库也写 ``["kb_a"]``。

**第 7 步起请求体只有两个字段**（ADR-0010「第三次修订」、ADR-0018）：``kb_ids``
（查哪些库）+ ``query``（问什么）。检索方式、返回条数、是否重排、是否改写、是否折叠
成整节、表格行是否折回整表——全部来自**这把 Key 的密钥配置**，请求里一个都没有。

旧调用方继续传其中任何一个（四个质量字段的名字见 ADR-0010）会拿到参数校验失败
（``20002``），**不是被静默忽略**：静默忽略会让调用方以为自己的参数生效了，直到某天
发现结果跟想的不一样。迁移方式是「把这次调用想要的行为写进这把 Key 的密钥配置」，
不是把字段加回来。这次调用的已解析参数由 ``app/services/retrieval_options.py`` 提供。

融合常数 ``k``、各路召回深度、融合池宽度仍是**链路常数**，由服务端配置决定
（``app/services/retrieval/fusion.py``）：它们决定「池子里有多少料」，不是「这次用
多好」，做成可调项会让同一个档位名在不同租户那里变成不同链路。
"""

from pydantic import BaseModel, ConfigDict, Field


class RetrieveRequest(BaseModel):
    """调用参数：只表达「问什么、查哪些库」；tenant_id 从 Key 解，硬传会被拒。"""

    model_config = ConfigDict(extra="forbid")

    kb_ids: list[str] = Field(min_length=1)
    query: str = Field(min_length=1)


class RetrievedChunk(BaseModel):
    """一条检索结果。

    ``score`` 是「本次最终排序分」：单模式下是该路的原始分；混合模式下，
    **折叠开启时（默认）是单元内命中片的最高分**，不折叠 / 重排发生时分别是
    融合分或重排分。它**不再恒等于** ``rrf_score``——后者始终是代表片那条结果的
    融合分。调用方要读具体分数请读 ``vector_score`` / ``bm25_score`` /
    ``rrf_score`` / ``rerank_score``：没命中的那一路是 ``null`` 而不是 0，
    「没命中」与「命中但分数为 0」必须可区分。语义变更见 ADR-0010「后续修订」。
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

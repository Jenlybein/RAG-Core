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

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_serializer

from app.core.exceptions import AppError, ErrorCode


class RetrieveRequest(BaseModel):
    """调用参数：只表达「问什么、查哪些库」；tenant_id 从 Key 解，硬传会被拒。"""

    model_config = ConfigDict(extra="forbid")

    kb_ids: list[str] = Field(min_length=1)
    query: str = Field(min_length=1)

    @field_validator("query")
    @classmethod
    def _reject_blank_query(cls, value: str) -> str:
        """纯空白不是「一个问题」。

        ``min_length=1`` 只挡住空串：``"   "`` 长度是 3，会一路走到召回，然后被当成一个
        没有词的查询静默产出空结果。两条入口共用一个值对象，这条规则才只写一遍。
        """

        if not value.strip():
            raise ValueError("query must not be blank or whitespace-only")
        return value


def parse_retrieve_request(*, kb_ids: Any, query: Any) -> RetrieveRequest:
    """两条入口共用的解析入口：校验规则只来自 :class:`RetrieveRequest`。

    HTTP 侧由 FastAPI 把同一个模型接在请求体上（失败由异常处理器翻成 ``20002``）；
    MCP 的工具签名只有类型、没有 ``Field`` 约束，因此必须显式走一遍同一个值对象，再把
    ``ValidationError`` 翻成同一个业务码。**不在这里复制一份字段规则**——复制出去的那
    一份迟早和 HTTP 侧漂移，而漂移的形态正是「同一份请求，两扇门两种答案」。
    """

    try:
        return RetrieveRequest(kb_ids=kb_ids, query=query)
    except ValidationError as exc:
        # 与 HTTP 的 20002 同一个码、同一个缺省消息（``request validation error``）。
        raise AppError(ErrorCode.REQUEST_VALIDATION_ERROR) from exc


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
    # 图谱注入（#144 / ADR-0027 决策 4）：**只有**「按关系补进来」的候选带这个标记
    # （``graph_injection``）；直接命中项连这个键都没有。序列化时摘掉 null，所以关掉
    # 图谱的响应逐字节不变。
    retrieval_source: str | None = None
    # 折叠关闭时的父节正文（#144 / ADR-0027 决策 7）：交付单元小于「节」时补上，
    # **不改分数、不改排名，只附加内容**。``content`` 仍是切片原文（ADR-0012 决策 3），
    # 父节正文单独一栏是为了不破坏「引用能指回那一片」。
    parent_content: str | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_graph_fields(self, handler: Any) -> dict[str, Any]:
        """没开图谱 / 没补父节时，这两个键**连存在都不存在**。

        ``null`` 也是一个字节：「关闭时响应逐字节不变」是红线上的一句话，所以这里显式
        摘掉——与 ``RetrieveData`` 摘 ``orchestration``、``metadata`` 摘 ``trace_id``
        同一条纪律。
        """

        dumped = handler(self)
        for name in ("retrieval_source", "parent_content"):
            if dumped.get(name) is None:
                dumped.pop(name, None)
        return dumped


class OrchestrationPoint(BaseModel):
    """一个要点：编号（A1…An）+ 文本（模型给的要点，不含正文）+ 它有没有核心片。

    ``has_primary`` 由材料数出来，不是模型自评：完整度这条判断的分子分母都在后端。
    """

    point_id: str
    text: str
    has_primary: bool


class OrchestrationEvidence(BaseModel):
    """一条材料（E1…En）：正文逐字来自库里的切片，身份字段只用于回溯。

    模型只能点序号，所以这里每个字段都是后端按序号回查重建的——``chunk_id`` /
    ``kb_id`` / ``document_id`` 只是让调用方能对回 ``retrieved_chunks``。
    """

    evidence_id: str
    point_id: str
    role: Literal["primary", "supporting"]
    chunk_id: str
    kb_id: str
    document_id: str
    title: str
    heading_path: str | None = None
    content: str


class OrchestrationBlock(BaseModel):
    """切片编排的结果：三态完整度 + 要点清单 + 材料清单。

    **没有 ``answer``**：ADR-0001 说 ``/rag/retrieve`` 永不生成答案，这里一个字的模型文本
    都不留（要点是清单项，不是答案）。
    """

    completeness: Literal["complete", "partial", "missing"]
    points: list[OrchestrationPoint]
    evidence: list[OrchestrationEvidence]


class RetrieveData(BaseModel):
    query: str
    kb_ids: list[str]
    retrieved_chunks: list[RetrievedChunk]
    metadata: dict
    # 切片编排（第 11 步票 #147）：**只在开启时出现**，关闭时这个键一个字节都不多。
    # 位置在 ``data`` 顶层而不是 ``metadata.retrieval`` 里：它是「这一次编排出了什么」
    # （要点、材料、完整度），不是「这一次检索遇到了什么」。语义见 ADR-0010「第七次修订」。
    orchestration: OrchestrationBlock | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_orchestration(self, handler: Any) -> dict[str, Any]:
        """关闭时把这个键**整段去掉**，而不是留一个 ``null``。

        「关闭时响应逐字节不变」是红线上的一句话，而 ``null`` 也是一个字节。Pydantic 的
        默认行为是「可选字段照样序列化成 null」，所以这里显式摘掉——与 ``trace_id`` /
        重排身份键那两次修订同一条纪律：新增字段不许改变没开它的那些调用的响应。
        """

        dumped = handler(self)
        if self.orchestration is None:
            dumped.pop("orchestration", None)
        return dumped


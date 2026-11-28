"""控制面「知识库与文档」接口的请求 / 响应模型。

树接口的形状由前端 ``KnowledgeBaseNode`` 决定：**一次拿全**，空库的 ``documents`` 是
``[]`` 而不是 ``null``（列表页用它区分父子行）。文档项直接复用数据面的
:class:`app.schemas.document.DocumentListItem`：两边的字段集本来就是同一个
「进度摘要」，各写一份迟早会漂移。

建库与上传/重建/删除的回执也复用数据面的 ``CreateKBData`` / ``UploadDocumentData`` /
``ReindexDocumentData`` / ``DeleteDocumentData``：控制面换的是门（会话 cookie 而不是
API Key），**不是契约**；前端从阶段 1 切到阶段 2 时不该看到响应形状变化。

``extra="forbid"`` 照仓库习惯：``tenant_id``、``kb_id`` 这类由路径或会话决定的字段
不许出现在请求体里，多发一个键就是参数错误（20002）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.models.knowledge_base import (
    MAX_CHUNK_SIZE,
    MAX_SECTION_PART_MAX_CHARS,
    MIN_CHUNK_SIZE,
    MIN_SECTION_PART_MAX_CHARS,
    SplitStrategy,
)
from app.schemas.document import DocumentListItem


class ConsoleUploadDocumentRequest(BaseModel):
    """控制面 JSON 上传：``kb_id`` 在路径里，正文只收 ``title`` 与 ``content``。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1)


class BuildFailureData(BaseModel):
    """「未入新版本」的一条明细：哪一篇、叫什么、为什么。"""

    document_id: str
    title: str
    reason: str


class BuildProgressData(BaseModel):
    """一次进行中的整库构建的进度与失败明细（#216）。

    ## 为什么「未入新版本」跟着进度一起回

    用户看到的两个问题其实是同一个：「还要等多久」与「为什么有些篇没进来」。分两次请求
    去问，就会在两次之间看见不一致的中间态（进度 500/500，明细还是上一轮的）。这里一次
    给全，前端一次渲染。

    ``done`` 是**进度条**（抢不到的不计、续建跳过的不计），``planned`` 是起构建那刻的快照，
    所以 ``done < planned`` 里既有「还在跑」也有「失败了」。判断「还剩多少要跑」看
    ``pending``，判断「有没有出问题」看 ``failures``——两个字段语义不同，别互相替代。
    """

    build_id: str
    version: str
    #: 参与篇数快照（分母）。
    planned: int
    #: 已有结果的篇数（进度条；失败也算「有结果」）。
    done: int
    #: 还没有结果的篇数 = ``max(planned - done, 0)``。
    pending: int
    #: 未入新版本的明细（最多回这么多条：界面只给前几条，其余折叠成「还有 N 篇」）。
    failures: list[BuildFailureData] = []
    #: 失败总数（``failures`` 可能被截断，这个数不截断）。
    failure_count: int = 0

    #: 与其它控制面 schema 同一条纪律：多余字段不许悄悄进来。
    model_config = ConfigDict(extra="forbid")


class BuildSummaryData(BaseModel):
    """**一次构建的摘要**：不只是「在跑的那次」，也包括最近一次**已有结论**的。

    与 :class:`BuildProgressData` 的分工：

    * :class:`BuildProgressData` 只服务「还在跑」——没有 ``building`` 行就是 ``None``。
      它回答的是「这一批还剩多少」，所以没有 ``status``；
    * 这一个带着 ``status``（``building`` / ``ready`` / ``active`` / ``failed`` /
      ``archived``），因为详情页要回答另一个问题：「这一版建成了吗？我这一篇进去了吗？」

    为什么详情页需要后者：构建收尾是一瞬间的事（实测 10 篇的库 9.5 秒建完），如果只回
    「在跑的那次」，用户一进去就只剩一张没有上下文的文档表——「刚才那次成没成」无从回答。
    """

    build_id: str
    version: str
    #: 构建状态之一（见 `app.models.index_version` 的常量）。
    status: str
    #: 参与篇数快照（分母）。
    planned: int
    #: 已有结果的篇数（进度条；失败也算「有结果」）。
    done: int
    #: 还没有结果的篇数 = ``max(planned - done, 0)``。
    #:
    #: 叫 ``remaining`` 而不是 ``pending``：行级的 ``build_state`` 里也有一个 ``pending``
    #: （「这一版还没收它」），两个 pending 一个数是、一个是枚举值，读代码的人会串。
    remaining: int
    #: 这一版对应的统一稿号（缺省 ``None`` = 老行没留快照）。
    doc_version: int | None = None
    failures: list[BuildFailureData] = []
    failure_count: int = 0
    #: 起建时刻（带时区的 ISO 串）：界面用它说「建于 14:03」。
    created_at: str | None = None
    #: 生效时刻（``active`` 的行才有值）：界面用它说「v5 已生效 · 14:03:12」。
    #: 没有这个时刻，「已生效」只是一句话——用户没有任何办法核对它是刚刚还是十分钟前。
    activated_at: str | None = None

    model_config = ConfigDict(extra="forbid")


class DocumentStatusCountsData(BaseModel):
    """四态各有多少篇。字段名写死而不是 ``dict[int, int]``：JSON 的键只能是字符串，
    一个 ``{"1": 10}`` 会在前端变成「看不见的字符串键」，而这里每个字段名都是一句人话。
    """

    success: int = 0
    failed: int = 0
    processing: int = 0
    queued: int = 0

    model_config = ConfigDict(extra="forbid")


class KnowledgeBaseSummaryData(BaseModel):
    """**库级摘要**：卡片网格与详情页 header 用的那一份，**不含文档数组**。

    为什么把它从 :class:`KnowledgeBaseNode` 里拆出来：树接口一次把整个租户的所有文档
    咽下去（一个 5000 篇的库就是 5000 行 × 每个库），而卡片上只显示四个数字。拆开之后
    「看库」与「看库里的文档」是两个请求、两种刷新节奏——这正是「进详情总要刷新一次」
    的根：详情以前没有自己的请求，只能读列表那一份。
    """

    kb_id: str
    name: str
    split_strategy: str
    allowed_formats: list[str]
    chunk_size: int
    #: 节段阈值。**必填**：给默认 2000 会让「漏传」与「真的是 2000」长得一样，
    #: 而界面把它当事实显示（「节段上限 2000」）——那是在替库编一个设置。
    section_part_max_chars: int
    #: 文档总数（**不随分页变**）。
    document_count: int
    #: 四态各有多少篇：卡片上「3 篇失败」与顶部「有文档在跑」都读它。
    status_counts: DocumentStatusCountsData = DocumentStatusCountsData()
    #: 这个库在**生效版本 × 当前稿**下的切片总数（与检索口径一致）。
    chunk_count: int
    #: 这个库有生效版本吗。``False`` 时界面必须说「未建索引」而不是「0 个分块」
    #: ——「还没有索引」与「索引里有 0 片」是两句话，处置也不同（前者要建、后者要查）。
    has_active_version: bool
    needs_rebuild: bool = False
    #: 「N 篇待更新」（现场九·第三步）：按篇比设置指纹得出的篇数。0 = 全部是最新的。
    #: 与 ``needs_rebuild`` 并存而不是替代：后者回答「库这一级要不要重建」，前者回答
    #: 「按篇看差几篇」——两句话的处置不同（一个点按钮，一个看列表）。
    stale_documents: int = 0
    token_usage: TokenUsageData | None = None
    #: 进行中**或最近一次**的构建摘要（没有构建过就是 ``None``）。
    build: BuildSummaryData | None = None

    model_config = ConfigDict(extra="forbid")


class KbDocumentRowData(BaseModel):
    """详情表的一行：文档自身状态 + 它在**当前这一版构建**里的位置。

    两根轴刻意分开（ADR-0034 决策九）：``status`` 是文档自己的生命周期（构建失败**不改**
    它）；``build_state`` 是「它与这一版的关系」，由构建的记账算出来。合并成一个状态会让
    「构建失败」污染文档状态，而那正是这条 ADR 拦住的事。
    """

    document_id: str
    title: str
    source_type: str
    #: 四态：1 成功 / 2 失败 / 3 索引中 / 4 排队中。
    status: int
    #: 当前稿的切片数（**生效版本 × 当前稿**）。
    chunk_count: int = 0
    token_count: int = 0
    error_message: str | None = None
    #: 当前稿号（回滚过的那篇会往回走）。
    current_version: int
    created_at: str | None = None
    #: 它与**当前这一版构建**的关系：``done`` 已入这一版 / ``pending`` 还没进去 /
    #: ``failed`` 这一版明确没收它 / ``None`` 没有构建可参照（从没建过，或与本次无关）。
    build_state: str | None = None

    model_config = ConfigDict(extra="forbid")


class KnowledgeBaseListData(BaseModel):
    """库列表（卡片网格）：只有摘要，没有文档数组。"""

    items: list[KnowledgeBaseSummaryData]
    #: 服务端给这份数据打的时间戳（ISO 串）。前端拿它显示「数据更新于 14:03:12」——
    #: 没有这一格，「自动刷新停了」与「数据卡住了」在界面上长得一模一样。
    generated_at: str


class KbDocumentsData(BaseModel):
    """详情页一次拿全：库摘要 + 构建 + 一页文档。"""

    kb: KnowledgeBaseSummaryData
    build: BuildSummaryData | None = None
    documents: list[KbDocumentRowData]
    total: int
    page: int
    page_size: int
    generated_at: str


class TokenUsageData(BaseModel):
    """一个库的 token 容量占用（#221 / 用户故事 #13）。

    ``limit`` 来自套餐（``max_tokens_per_kb``），``used`` 是库里所有文档 ``token_count``
    的合计。**空库也报**（used=0）而不是 ``None``：界面上「0 / 3 万」是一句有用的话，
    「未统计」在这里没有意义——这个数是我们自己算的。
    """

    used: int
    limit: int


class KnowledgeBaseNode(BaseModel):
    """一条库 + 它名下的全部文档（控制面按会话租户取全量，不做白名单收窄）。"""

    kb_id: str
    name: str
    split_strategy: str
    allowed_formats: list[str]
    chunk_size: int
    #: 节段阈值（第 16 步 #207）：库的切块设置之一，读侧要回显才能「改了再改」。
    section_part_max_chars: int = 2000
    #: 库当前设置与**生效那一版**的构建参数已不一致 → 这个库需要重建才生效。
    #: 老版本行没留参数（``params`` 为 NULL）时也判 ``True``：宁可多提示一次。
    needs_rebuild: bool = False
    #: 进行中的构建（没有就是 ``None``）。列表与详情都靠它显示「重建中 37 / 500」，
    #: 前端在它非空时轮询（#216）。
    build_progress: BuildProgressData | None = None
    #: 这个库的 token 容量占用；``None`` 只出现在「拿不到套餐」的降级路径上。
    token_usage: TokenUsageData | None = None
    documents: list[DocumentListItem]


class UpdateKBSettingsRequest(BaseModel):
    """改库的切块设置（第 16 步 #207）。

    只收「改了就必须重建」的那几项 + 名称/描述。**不传 = 不改**（``None`` 语义），
    否则一次只想改阈值的请求会把策略也重置掉。

    ``confirm`` 是显式的二次确认：这次改动会触发整库重建（可能几十分钟的 Worker），
    前端在确认弹窗里点过之后才带 ``true``。少了它，一次误点就是一次全库重嵌入。
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    split_strategy: SplitStrategy | None = None
    chunk_size: int | None = Field(default=None, ge=MIN_CHUNK_SIZE, le=MAX_CHUNK_SIZE)
    section_part_max_chars: int | None = Field(
        default=None, ge=MIN_SECTION_PART_MAX_CHARS, le=MAX_SECTION_PART_MAX_CHARS
    )
    confirm: bool = False


class UpdateKBSettingsData(BaseModel):
    """改设置的回执：新设置 + 这次重建的版本。"""

    kb_id: str
    name: str
    split_strategy: str
    allowed_formats: list[str]
    chunk_size: int
    section_part_max_chars: int
    build_id: str
    build_version: str


class RebuildKBData(BaseModel):
    """重建一个库的回执：这次构建的定位信息（设置没变，所以不回吐设置）。"""

    kb_id: str
    build_id: str
    build_version: str
    #: 起跑那一刻快照的可参与文档数（= 这次要重切多少篇）。
    documents_planned: int


class KBSettingsPreviewData(BaseModel):
    """改设置前的确认弹窗：当前设置、生效版本参数、以及这次重建的量。"""

    kb_id: str
    name: str
    split_strategy: str
    allowed_formats: list[str]
    chunk_size: int
    section_part_max_chars: int
    #: 生效那一版的构建参数；``None`` = 老版本没留参数（不是「没有参数」）。
    live_params: dict[str, object] | None = None
    live_version: str | None = None
    needs_rebuild: bool
    build_running: bool
    documents_to_rebuild: int
    chunks_to_rebuild: int


class KnowledgeBaseTreeData(BaseModel):
    knowledge_bases: list[KnowledgeBaseNode]


class DeleteKnowledgeBaseData(BaseModel):
    """删库回执：本票只删空库，删成功时库行与它（必然为空）的文档都不在了。"""

    kb_id: str
    deleted: bool


class DocumentContentData(BaseModel):
    """一份文档的原文（控制面只读）。

    只读接口，所以形状照着「看清这份文档本身」来给，而不是照某个写回执：

    - ``content`` 是**被切块的那份文本**（JSON 模式读库里的 content，文件模式现场解析
      落盘文件），与 Worker 用同一个函数产出，因此和检索命中的切片同源。
    - ``truncated`` / ``length`` 是一对：前者只说「给全了没有」，后者是**原文真实长度**。
      只给截断后的内容而不说长度，会让用户以为文档只有这么长。
    - ``status`` 带上是因为「原文能看」与「索引成功」是两件事：失败文档照样有原文，
      而用户正是要靠这份原文判断解析有没有出错。
    """

    document_id: str
    title: str
    source_type: str
    source_filename: str | None
    status: int
    content: str
    truncated: bool
    length: int

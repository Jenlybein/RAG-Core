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
    #: 这个库在**当前代 × 当前稿**下的切片总数（与检索口径一致）。
    chunk_count: int
    #: 「N 篇待更新」（现场九·第三步）：按篇比设置指纹得出的篇数。0 = 全部是最新的。
    stale_documents: int = 0
    token_usage: TokenUsageData | None = None

    model_config = ConfigDict(extra="forbid")


class KbDocumentRowData(BaseModel):
    """详情表的一行：文档自身状态 + 当前稿 / 切片数。

    文档状态是**文档自己的生命周期**（四态），不掺任何「它与某次整库构建的关系」——那个
    第二根轴随库级版本层删除，一篇文档现在只有「我算到第几代、正文是第几稿」这两个事实，
    都表达在文档行自己身上。
    """

    document_id: str
    title: str
    source_type: str
    #: 四态：1 成功 / 2 失败 / 3 索引中 / 4 排队中。
    status: int
    #: 当前代 × 当前稿的切片数。
    chunk_count: int = 0
    token_count: int = 0
    error_message: str | None = None
    #: 当前稿号（回滚过的那篇会往回走）。
    current_version: int
    created_at: str | None = None

    model_config = ConfigDict(extra="forbid")


class KnowledgeBaseListData(BaseModel):
    """库列表（卡片网格）：只有摘要，没有文档数组。"""

    items: list[KnowledgeBaseSummaryData]
    #: 服务端给这份数据打的时间戳（ISO 串）。前端拿它显示「数据更新于 14:03:12」——
    #: 没有这一格，「自动刷新停了」与「数据卡住了」在界面上长得一模一样。
    generated_at: str


class KbDocumentsData(BaseModel):
    """详情页一次拿全：库摘要 + 一页文档。"""

    kb: KnowledgeBaseSummaryData
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
    #: 「N 篇待更新」：按篇比设置指纹得出的篇数。0 = 全部最新。
    stale_documents: int = 0
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
    """改设置的回执：新设置 + 这次投递了多少篇重建。"""

    kb_id: str
    name: str
    split_strategy: str
    allowed_formats: list[str]
    chunk_size: int
    section_part_max_chars: int
    #: 本次真的投进队列的篇数（``prepare_for_indexing`` 抢不到的篇不计）。
    documents_dispatched: int
    #: 库里可重建的总篇数；大于 ``documents_dispatched`` 说明超过单次上限，再点一次继续。
    documents_total: int


class RebuildKBData(BaseModel):
    """重建一个库的回执：**按篇投递**的结果（设置没变，所以不回吐设置）。

    进度不再由服务端记账：前端看每篇自己的 ``status``。回执里两个数回答「这一批投了多少、
    库里一共有多少」——**两个数都不对**：`documents_total` 是「产物落后的篇数」，
    两者不等只表示这次没投完（撞上限或抢不到），投完就投完了。
    """

    kb_id: str
    documents_dispatched: int
    documents_total: int


class KBSettingsPreviewData(BaseModel):
    """改设置前的确认弹窗：当前设置 + 这次重建的量。"""

    kb_id: str
    name: str
    split_strategy: str
    allowed_formats: list[str]
    chunk_size: int
    section_part_max_chars: int
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

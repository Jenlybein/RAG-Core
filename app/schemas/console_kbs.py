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
    chunk_size: int | None = Field(
        default=None, ge=MIN_CHUNK_SIZE, le=MAX_CHUNK_SIZE
    )
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

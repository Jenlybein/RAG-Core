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
    documents: list[DocumentListItem]


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

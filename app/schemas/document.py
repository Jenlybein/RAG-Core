"""上传与状态查询接口的请求 / 响应模型。

上传是单端点按 content-type 分流：JSON 模式用 ``UploadDocumentRequest``（title、
content 必填）；文件模式的 kb_id / title / file 走 Form，由路由手工补齐 20002。
``tenant_id`` 由服务端从占位 Key 解出，不进请求体。

``DocumentListData`` 是异步化后调用方唯一能观测进度的形状：异步上传后前端 / 调用方
轮询 ``GET /documents?kb_id=``，看 ``status`` 与 ``chunk_count``。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class UploadDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1)


class UploadDocumentData(BaseModel):
    """登记回执。``source_type`` 是这份文档最终被归成的来源类型（JSON 入口为
    ``markdown``，文件入口按扩展名），调用方靠它确认准入判定落在了哪一类上。"""

    document_id: str
    kb_id: str
    status: int
    chunk_count: int
    error_message: str | None = None
    source_type: str


class DocumentListItem(BaseModel):
    """一份文档的进度摘要；``chunk_count`` 来自切片表的分组计数。"""

    document_id: str
    title: str
    status: int
    chunk_count: int
    error_message: str | None = None
    source_type: str
    created_at: datetime


class DocumentListData(BaseModel):
    items: list[DocumentListItem]


class ReindexDocumentData(BaseModel):
    """重建索引的受理回执：文档已被重新置为处理中并投递。

    ``status`` 恒为 ``PROCESSING``、``chunk_count`` 恒为 0：重建端点只受理，
    purge + 写入 + 状态回写全部发生在 Worker 的同一个事务里，调用方要确认终态
    仍需轮询 ``GET /documents?kb_id=``。``reparse`` 回显是否要求重解析原始文件。
    """

    document_id: str
    status: int
    chunk_count: int
    reparse: bool


class DeleteDocumentData(BaseModel):
    """删除文档的受理回执；切片、文件与记录都已清掉。"""

    document_id: str
    deleted: bool


class CreateDocumentVersionRequest(BaseModel):
    """提交新版本的 JSON 入口：只要正文（标题、知识库都属于文档本身，不在这一层改）。

    ``extra="forbid"``：多传一个 ``document_id`` / ``version`` 之类的字段要被 20002 拒，
    而不是被静默忽略——「我传了版本号，服务端却自己算了一个」是最难查的那类分歧。
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)


class CreateDocumentVersionData(BaseModel):
    """建版本的受理回执：新稿次已落库、文档已置处理中并投递了重建。

    ``version`` 是刚建好的稿号，``previous_version`` 是提交生效前的那一稿——两者一起给，
    调用方不必再列一次版本就知道「这次改的是第几稿」。``checksum`` 是**规范化后**正文的
    ``sha256``：之后按稿直查 / 对账都用同一个口径。
    """

    document_id: str
    version: int
    previous_version: int
    checksum: str
    status: int


class DocumentVersionItem(BaseModel):
    """版本列表里的一行。

    ``content`` **默认不给**（``None``）：正文可能是整篇文档，列表接口的调用方通常只关心
    「改过几稿、哪一稿是哪天」。要看正文得显式要求（``?include_content=true``）。

    **不给 ``source_file_path``**：那是服务端存储根下的相对路径（``tenant/<id>.v2.md``），
    调用方拿它做不了任何事，而把内部路径暴露出去只会扩大泄露面。重解析要读它，但那是
    Worker 的事（版本行自己留着）。
    """

    version: int
    status: str
    checksum: str
    created_at: datetime
    content: str | None = None


class DocumentVersionListData(BaseModel):
    """列版本的出参：按稿号**倒序**（现在这一稿在最前），加上当前稿指针。"""

    document_id: str
    current_version: int
    items: list[DocumentVersionItem]


class RollbackDocumentVersionData(BaseModel):
    """回滚受理的回执：指针已拨回 ``version``，文档已置处理中并投递了重建。

    ``previous_version`` 是回滚生效前的那一稿——调用方不必再列一次版本就知道「从哪一稿
    退回哪一稿」。与建版本的回执不同，这里**没有 ``checksum``**：回滚不产生新正文，
    目标稿的校验和早就在版本行上了（列版本能拿到）。

    ``status`` 恒为 ``PROCESSING``：回滚之后切片要重建，终态仍需轮询
    ``GET /documents?kb_id=``——「回滚受理」不等于「已经能检索到新内容」。
    """

    document_id: str
    version: int
    previous_version: int
    status: int

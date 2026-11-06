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
    document_id: str
    kb_id: str
    status: int
    chunk_count: int
    error_message: str | None = None


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

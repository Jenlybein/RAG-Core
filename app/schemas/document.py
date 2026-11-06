"""上传接口的请求 / 响应模型。

骨架期只接受 JSON（title + content），文件上传留给第 3 步。
``tenant_id`` 由服务端从占位 Key 解出，不进请求体。
"""

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

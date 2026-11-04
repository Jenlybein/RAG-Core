"""上传接口的请求 / 响应模型。

骨架期只接受 JSON（title + content），文件上传留给阶段 3。
"""

from pydantic import BaseModel, Field


class UploadDocumentRequest(BaseModel):
    tenant_id: str = Field(min_length=1)
    kb_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1)


class UploadDocumentData(BaseModel):
    document_id: str
    kb_id: str
    status: int
    chunk_count: int
    error_message: str | None = None
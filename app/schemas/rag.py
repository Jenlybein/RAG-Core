"""检索接口的请求 / 响应模型。

响应里有 retrieved_chunks 与 metadata，没有 context_text，也没有答案。
第 1 步起：头部带占位 API Key，``tenant_id`` 由服务端从 Key 解出、
不进请求体；``kb_ids`` 是列表，单库也写 ``["kb_a"]``。
"""

from pydantic import BaseModel, ConfigDict, Field


class RetrieveRequest(BaseModel):
    """调用参数；tenant_id 从 Key 解，硬传会被拒（extra=forbid）。"""

    model_config = ConfigDict(extra="forbid")

    kb_ids: list[str] = Field(min_length=1)
    query: str = Field(min_length=1)


class RetrievedChunk(BaseModel):
    document_id: str
    chunk_id: str
    title: str
    content: str
    score: float


class RetrieveData(BaseModel):
    query: str
    kb_ids: list[str]
    retrieved_chunks: list[RetrievedChunk]
    metadata: dict

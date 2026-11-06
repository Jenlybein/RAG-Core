"""建库 / 列库接口的请求 / 响应模型。

``tenant_id`` 由服务端从占位 Key 解出，不进请求体。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CreateKBRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class CreateKBData(BaseModel):
    kb_id: str
    name: str
    tenant_id: str
    created_at: datetime


class KnowledgeBaseItem(BaseModel):
    kb_id: str
    name: str
    tenant_id: str


class KnowledgeBaseListData(BaseModel):
    """这把 Key 可见的库；第 1 步占位期返回默认租户下的全部库。"""

    knowledge_bases: list[KnowledgeBaseItem]

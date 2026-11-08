"""建库 / 列库接口的请求 / 响应模型。

``tenant_id`` 由服务端从占位 Key 解出，不进请求体。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.knowledge_base import (
    DEFAULT_CHUNK_SIZE,
    MAX_CHUNK_SIZE,
    MIN_CHUNK_SIZE,
    FormatKind,
)


class CreateKBRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    # **必填、无默认**：策略同时决定这个库收哪些格式，给默认值等于让调用方在
    # 不知情的情况下被套上一个语料类型。
    split_strategy: Literal["structure", "size"]
    # 不传就按策略补全：structure → [markdown]，size → 五种全开。
    allowed_formats: list[FormatKind] | None = None
    chunk_size: int = Field(
        default=DEFAULT_CHUNK_SIZE, ge=MIN_CHUNK_SIZE, le=MAX_CHUNK_SIZE
    )


class CreateKBData(BaseModel):
    kb_id: str
    name: str
    tenant_id: str
    split_strategy: str
    allowed_formats: list[str]
    chunk_size: int
    created_at: datetime


class KnowledgeBaseItem(BaseModel):
    kb_id: str
    name: str
    tenant_id: str
    split_strategy: str
    allowed_formats: list[str]
    chunk_size: int


class KnowledgeBaseListData(BaseModel):
    """这把 Key 可见的库；第 1 步占位期返回默认租户下的全部库。"""

    knowledge_bases: list[KnowledgeBaseItem]

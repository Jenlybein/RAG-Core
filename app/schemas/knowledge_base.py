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
    SPLIT_STRUCTURE,
)


class CreateKBRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    # 切块策略在**建库时**定一次，之后该库所有文档都走同一种切法。不放到上传
    # 接口上：同库混用两种切法会让片长分布不一致，检索时「片长」会变成与内容
    # 无关的排序因素。
    split_strategy: Literal["structure", "size"] = SPLIT_STRUCTURE
    chunk_size: int = Field(
        default=DEFAULT_CHUNK_SIZE, ge=MIN_CHUNK_SIZE, le=MAX_CHUNK_SIZE
    )


class CreateKBData(BaseModel):
    kb_id: str
    name: str
    tenant_id: str
    split_strategy: str
    chunk_size: int
    created_at: datetime


class KnowledgeBaseItem(BaseModel):
    kb_id: str
    name: str
    tenant_id: str
    split_strategy: str
    chunk_size: int


class KnowledgeBaseListData(BaseModel):
    """这把 Key 可见的库；第 1 步占位期返回默认租户下的全部库。"""

    knowledge_bases: list[KnowledgeBaseItem]

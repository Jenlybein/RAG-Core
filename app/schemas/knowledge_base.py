"""建库接口的请求 / 响应模型。"""

from datetime import datetime

from pydantic import BaseModel, Field


class CreateKBRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    tenant_id: str = Field(min_length=1, max_length=64)


class CreateKBData(BaseModel):
    kb_id: str
    name: str
    tenant_id: str
    created_at: datetime
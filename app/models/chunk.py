"""表 3：chunks —— 检索的最小返回单元。

embedding 是硬约束 Vector(1536)：维度改了就要改列 + 迁移 + 全量重建索引。
字段名 meta 映射到数据库列 metadata（metadata 是 SQLAlchemy 的保留名）。

content 是**切片原文**（原样返回给调用方）；送进 embedding 的检索文本不落库，
只在索引期由 ``app.utils.retrieval_text`` 算出来。

index_version 是数据范围的一部分：本步只写默认值 ``v1``，不引入版本生命周期
（重建仍是先删后写）。复合索引 ``(tenant_id, kb_id, index_version)`` 供后续的
范围检索使用。
"""

from pgvector.sqlalchemy import Vector
from sqlalchemy import Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# 切片默认索引版本；列默认值与索引期写入共用这一个常量。
DEFAULT_INDEX_VERSION = "v1"


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_tenant_id_kb_id_index_version", "tenant_id", "kb_id", "index_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    document_id: Mapped[str] = mapped_column(String(36), index=True)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536))
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    index_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DEFAULT_INDEX_VERSION,
        server_default=DEFAULT_INDEX_VERSION,
    )

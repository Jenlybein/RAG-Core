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


def default_index_versions(kb_ids: list[str]) -> dict[str, str]:
    """把一批知识库映射成「本次检索的范围」：``kb_id → 默认索引版本``。

    这是检索两路（向量 / 关键词）唯一的范围构造点。它必须只有一份：

    * 配对关系只能从**映射**里读出来，两个平行数组错位就会静默越权；
    * 本步只写默认版本 ``v1``，将来接上版本生命周期时，改的是这里而不是四个调用点。

    放在模型层（与 ``DEFAULT_INDEX_VERSION`` 同源）而不是服务层：任何有知识库 ID
    的层都能 import 它，且不引入任何循环依赖（本模块只依赖 ``app.db.base``）。
    """

    return {kb_id: DEFAULT_INDEX_VERSION for kb_id in kb_ids}


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

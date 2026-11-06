"""VectorStore 抽象。

判据：向量写入与相似度检索归它，普通 CRUD 归 ChunkRepository。
业务层只依赖这个 ABC，换 Milvus / Qdrant 只改实现与工厂函数。
"""

from abc import ABC, abstractmethod

from sqlalchemy.orm import Session


class VectorStore(ABC):
    """向量存储能力。"""

    @abstractmethod
    async def add_chunks(self, db: Session, chunks: list[dict]) -> None:
        """批量写入切片向量。"""

    @abstractmethod
    async def similarity_search(
        self, db: Session, query_vector: list[float], *, tenant_id: str, kb_id: str, top_k: int
    ) -> list[dict]:
        """返回相似度结果，score = 1 - 余弦距离（越大越相关）；
        必须带 tenant_id 与 kb_id 过滤，否则会跨库召回。"""

    @abstractmethod
    async def delete_by_document_id(self, db: Session, document_id: str, *, tenant_id: str) -> None:
        """删某文档的全部切片向量；必须带 tenant_id 过滤，否则会删到其他租户的行。"""
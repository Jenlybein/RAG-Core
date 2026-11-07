"""KeywordSearchProvider 抽象（范围版，与向量存储同形状）。

判据与向量侧一致：关键词索引的写入与检索归它，普通 CRUD 归 Repository。
业务层只依赖这个 ABC，换引擎（ES / OpenSearch / 别的 BM25 实现）只改实现与工厂。

**签名为什么是范围版**：一次检索可以覆盖多个知识库，每个库有自己的索引版本。
``index_versions`` 是「kb_id → 索引版本」的映射，不是两个平行数组——配对关系
只能从映射里读出来，数组错位会静默越权。返回值与 ``VectorStore`` 一样是
``list[dict]``（``chunk_id`` / ``document_id`` / ``title`` / ``content`` / ``score``），
融合层不需要区分两路的数据形状。

``available`` 是**能力存在性**，不是「这次查到了没有」：空实现返回 ``False``，
单模式检索据此硬失败（``RETRIEVAL_FAILED``），绝不偷偷退回向量。
"""

from abc import ABC, abstractmethod


class KeywordSearchProvider(ABC):
    """关键词检索能力。"""

    @property
    @abstractmethod
    def available(self) -> bool:
        """这项能力是否真的可用（配置了引擎 = True，空实现 = False）。"""

    @abstractmethod
    async def add_chunks(self, chunks: list[dict]) -> None:
        """批量写入切片的关键词文档（``_id`` 取 ``chunk_id``，重复写入幂等）。"""

    @abstractmethod
    async def keyword_search(
        self, query: str, *, tenant_id: str, index_versions: dict[str, str], top_k: int
    ) -> list[dict]:
        """按关键词召回切片，score 越大越相关；必须带租户过滤，否则会跨租户召回。

        ``index_versions`` 是「kb_id → 索引版本」；租户 + 库/版本配对是硬过滤条件，
        打分只来自关键词命中。
        """

    @abstractmethod
    async def delete_by_document_id(self, document_id: str, *, tenant_id: str) -> None:
        """删某文档的全部关键词文档；必须带 tenant_id 过滤，否则会删到别的租户。"""

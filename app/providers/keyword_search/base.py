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
    async def count_documents(
        self, *, tenant_id: str, kb_id: str, index_version: str
    ) -> int:
        """该版本在这个库里的关键词文档数（``ready`` 前的四项计数之一，ADR-0025）。

        只数**目标版本那一个物理索引**里这个租户 / 库 / 版本的文档：数错索引等于让
        「四项计数相等」这道闸门形同虚设。索引不存在就是 0（那是诚实数字，不是错误）。
        """

    @abstractmethod
    async def activate_version(self, version: str) -> None:
        """把别名切到该版本的物理索引（**激活的一部分**，ADR-0025 决策二 / 三）。

        切换前检索查的是旧物理索引——不需要任何开关，别名指哪儿就查哪儿。实现必须在
        **一次**别名更新里完成「摘旧 + 挂新」，否则中间会出现别名指向零个索引的窗口
        （那一刻的关键词召回是空的，而向量那一路照旧返回结果，症状是「偶发少了关键词
        命中」）。
        """

    @abstractmethod
    async def delete_by_document_id(
        self, document_id: str, *, tenant_id: str, index_version: str | None = None
    ) -> None:
        """删某文档的关键词文档；必须带 tenant_id 过滤，否则会删到别的租户。

        ``index_version`` 是**删除范围**（#142）：给了版本只删那一版（重建新版本时绝不
        碰线上那一版），``None`` 删全部版本（删文档）。物理索引按版本隔离（见
        ``app/providers/keyword_search/versioned_index.py``），所以「全部版本」意味着
        遍历该库的全部版本物理索引——只删生效那一个会留下幽灵文档：某个归档版本被重新
        启用时，那些已删文档会原样回来。
        """

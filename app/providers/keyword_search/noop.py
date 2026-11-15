"""空实现：显式表达「关键词检索能力不存在」。

它照常实现写入与删除（安静地什么都不做），所以索引编排的存储列表里可以永远
摆着它；但 ``available`` 为 ``False``，``mode=bm25`` 会因此硬失败，而不是拿一个
空列表冒充「查过了、没命中」。
"""

from collections.abc import Sequence

from app.providers.keyword_search.base import KeywordSearchProvider


class NoopKeywordSearchProvider(KeywordSearchProvider):
    @property
    def available(self) -> bool:
        return False

    async def add_chunks(self, chunks: list[dict]) -> None:
        return None

    async def keyword_search(
        self, query: str, *, tenant_id: str, index_versions: dict[str, str], top_k: int
    ) -> list[dict]:
        return []

    async def count_documents(
        self, *, tenant_id: str, kb_id: str, index_version: str
    ) -> int:
        """能力不存在 → 0。构建编排据此**不把这一维**算进四项计数（见构建服务）。"""

        return 0

    async def physical_index_exists(self, version: str) -> bool:
        """没有物理索引这回事 → ``False``（调用方按 ``available`` 跳过这一维）。"""

        return False

    async def activate_version(self, version: str) -> None:
        """没有别名可切：空实现不持有任何索引。"""

        return None

    async def delete_by_document_id(
        self,
        document_id: str,
        *,
        tenant_id: str,
        index_version: str | None = None,
        doc_version: int | None = None,
    ) -> None:
        return None

    async def mark_current_draft(
        self, document_id: str, *, tenant_id: str, doc_version: int
    ) -> None:
        """没有关键词索引这回事 → 没有标记可翻（#175）。"""

        return None

    async def current_draft_versions(
        self, *, tenant_id: str, document_ids: Sequence[str]
    ) -> dict[str, list[int]]:
        """能力不存在 → 空映射（#179）。**调用方必须先看 ``available``**，否则会把
        「这台机器没部署引擎」读成「关键词索引里一篇都没标当前稿」──那正是对账要分开的
        两种状态。这里不抛异常：形状与 ES 实现一致，脚本据此走同一条代码路径。"""

        return {}

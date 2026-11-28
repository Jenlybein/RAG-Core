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
        self, query: str, *, tenant_id: str, kb_ids: Sequence[str], top_k: int
    ) -> list[dict]:
        return []

    async def delete_by_document_id(
        self,
        document_id: str,
        *,
        tenant_id: str,
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

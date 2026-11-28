"""KeywordSearchProvider 抽象（范围版，与向量存储同形状）。

判据与向量侧一致：关键词索引的写入与检索归它，普通 CRUD 归 Repository。
业务层只依赖这个 ABC，换引擎（ES / OpenSearch / 别的 BM25 实现）只改实现与工厂。

**签名为什么是范围版**：一次检索可以覆盖多个知识库，所以范围是一个**库集合**
（``kb_ids``）。库级索引版本那一层（别名指向生效版本、物理索引按版本隔离）已经整层删除：
关键词侧是「每篇一份的镜像」，可见性靠「写入先按文档全清、再写新的一份」表达，版本不再
参与任何判据。返回值与 ``VectorStore`` 一样是 ``list[dict]``（``chunk_id`` /
``document_id`` / ``title`` / ``content`` / ``score``），融合层不需要区分两路的数据形状。

``available`` 是**能力存在性**，不是「这次查到了没有」：空实现返回 ``False``，
单模式检索据此硬失败（``RETRIEVAL_FAILED``），绝不偷偷退回向量。
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence


class KeywordSearchProvider(ABC):
    """关键词检索能力。"""

    async def aclose(self) -> None:
        """释放 Provider 持有的外部资源。**默认什么都不做**，引擎实现按需覆盖。

        调用时机只有两个：**应用关停**（lifespan 的退出段）与**脚本退出前**。
        ``AsyncElasticsearch`` 的连接池挂在 aiohttp 会话上，不显式关就只能在解释器
        退出时由 GC 兜底，日志里留下一串 ``Unclosed client session`` / ``Unclosed
        connector``——噪声会盖住真正要看的日志（这是实测踩过的）。

        **不许**在每次检索后调用：ES 客户端按事件循环缓存，一次检索关一次等于把连接池
        拆了重搭，TLS 握手开销会回到「每查一次重建一个客户端」的水平。
        """

        return None

    @property
    @abstractmethod
    def available(self) -> bool:
        """这项能力是否真的可用（配置了引擎 = True，空实现 = False）。"""

    @abstractmethod
    async def add_chunks(self, chunks: list[dict]) -> None:
        """批量写入切片的关键词文档（``_id`` 取 ``chunk_id``，重复写入幂等）。"""

    @abstractmethod
    async def keyword_search(
        self, query: str, *, tenant_id: str, kb_ids: Sequence[str], top_k: int
    ) -> list[dict]:
        """按关键词召回切片，score 越大越相关；必须带租户过滤，否则会跨租户召回。

        ``kb_ids`` 是这次要查的库；租户 + 库范围是硬过滤条件，打分只来自关键词命中。
        范围只剩库：库级索引版本那一层删除之后，可见性由「每篇一份镜像」表达。
        """

    @abstractmethod
    async def delete_by_document_id(
        self,
        document_id: str,
        *,
        tenant_id: str,
        doc_version: int | None = None,
    ) -> None:
        """删某文档的关键词文档；必须带 tenant_id 过滤，否则会删到别的租户。

        ``doc_version`` 是**第二个删除范围**（#173，与稿号正交）：给了稿号只删那一稿，
        ``None`` 删这篇文档的所有稿（删文档那条老路径）。同一张物理索引里装着这篇文档的
        每一稿，所以稿号在这里是**唯一**的那道范围：少了它，重建一次就把旧稿从关键词那
        一路抹掉，而向量那一路还留着。

        没有库级版本参数：物理索引只有一个（配置里的那个名字），不存在「只删某一版」这
        件事——那正是版本化物理索引删除后省下的那一维。
        """

    @abstractmethod
    async def mark_current_draft(
        self, document_id: str, *, tenant_id: str, doc_version: int
    ) -> None:
        """把这篇文档的关键词切片里「当前稿」标记翻到 ``doc_version``（#175）。

        指针前移（``documents.current_version``）之后必须调它：不翻的话，旧稿的切片仍带着
        ``is_current=true``，关键词路会继续召回旧稿，而**症状是同一段内容出现两次**——
        向量那一路已经只看新稿了。

        范围是「这一篇文档、这一个租户」：少任何一个，翻的就是别人的切片。

        **实现方的失败语义**：抛异常即可，调用方（``app.services.keyword_draft``）负责
        降级成 warning。Provider 自己不要在内部吞掉错误——那样「没翻成」就没有任何痕迹。
        """

    @abstractmethod
    async def current_draft_versions(
        self, *, tenant_id: str, document_ids: Sequence[str]
    ) -> dict[str, list[int]]:
        """这批文档在关键词索引里**被标成「当前稿」**的稿号（对账用，#179）。

        返回 ``{document_id: [doc_version…]}``。一篇正常只有一个稿号（当前稿的每一片都带
        同一个）；**多于一个就是分歧的现场**，由调用方（``app.services.document_version_reconcile``）
        判，Provider 只如实回答。

        为什么这条读能力必须挂在 Provider 上，而不是让脚本自己去连 ES：查询体、别名、
        版本命名都是这一层的事（``build_current_draft_versions_query`` 就在旁边）；脚本
        自己拼一遍，就是第二套「关键词索引长什么样」的知识，而它只会在 ES 升级时过期。

        **查的是那一个物理索引**（``self._index_name``）：线上那一份才是「关键词索引认为的
        当前稿」，别的索引已经不存在（版本化物理索引随库级版本层一并删除）。

        **空批次不发语句**（回 ``{}``）：一次空扫描不该换来一条 ``terms: []`` 的查询。
        **索引还不存在**（``NotFoundError``）回 ``{}``：那是「关键词库里一个字都没有」，
        不是故障。**其他失败一律抛**：调用方要把「能力不存在」与「能力在、这一次读不到」
        分成两种不可判定（前者不用排障，后者要）。
        """

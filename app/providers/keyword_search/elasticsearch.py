"""Elasticsearch 关键词检索实现：只做「组装与调用」。

请求体、mapping、切片文档的形状全部由 ``query_builder`` 的三个纯函数产出，
这一层不拼过滤条件、不做分词选择，也不在这里降级：索引阶段任一次写入失败
就是整单失败（调用方回滚事务并把文档置为失败），检索阶段能不能退回向量由
业务层决定，不是 Provider 的权限。

``refresh=True`` 是刻意的：文档索引完必须立刻能查到，否则「上传完马上提问」
会稳定地查不到内容。批量导入的吞吐优化留给后续步骤。

**只有一个索引**（库级索引版本层删除后的形状）：读写都用配置里的
``ELASTICSEARCH_INDEX``。历史上它是别名，指向 ``<别名>__vN`` 的某一版物理索引，
激活时把别名挪来挪去；那套版本化物理命名 / 别名切换已经整层删除，这个名字现在
就是**唯一的物理索引**。代价写在明处：既有部署里那些 ``<别名>__vN`` 物理索引不再
被读，关键词索引必须**清空重建一次**（否则旧数据留在别的版本索引里、新数据写进
这一个，两半各查一半）。
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Sequence
from typing import Any

import structlog
from elasticsearch import AsyncElasticsearch, BadRequestError, NotFoundError

from app.core.exceptions import AppError, ErrorCode
from app.providers.keyword_search.base import KeywordSearchProvider
from app.providers.keyword_search.query_builder import (
    CURRENT_DRAFT_VERSIONS_AGG,
    build_bm25_query,
    build_current_draft_update,
    build_current_draft_versions_query,
    build_es_mapping,
    build_keyword_documents,
    build_mapping_update,
    build_purge_query,
)

logger = structlog.get_logger(__name__)


class ElasticsearchKeywordSearchProvider(KeywordSearchProvider):
    """ES 8.x 实现。客户端惰性构造：装配阶段绝不发网络请求。

    ``timeout_s`` 是**查询的时间预算**，由组合根从配置注入（不在这里读全局配置：这个类
    因此能在测试里被直接构造，也就不用 monkeypatch 环境）。

    为什么预算必须落在这里而不是调用方：关键词通道有**两份实现**——单通道走
    ``rag_service._retrieve_by_keyword``，混合模式走 ``pipeline._keyword_channel``
    （控制台看到的就是后者）。预算写在调用方，就得写两遍，而写两遍的下场是漂移。
    真正知道「我在跟 ES 说话」的是这个类，所以由它兜住。

    实测事故（2026-11-11）：ES 容器被杀后，客户端自己重试 4 次、每次 DNS 失败约 3.8 秒，
    合计让一次检索等了 **19.4 秒**，而其它五段加起来不到 600 ms。预算就是防这个的。
    """

    def __init__(
        self,
        *,
        url: str,
        index_name: str,
        client: Any | None = None,
        timeout_s: float = 2.0,
    ) -> None:
        self._url = url
        #: 唯一的物理索引名（配置里的 ``ELASTICSEARCH_INDEX``）：读、写、删都认它。
        self._index_name = index_name
        self._client = client
        #: 本进程是否已经确认过这个索引存在。只有一个索引，所以只记一个布尔。
        self._index_ready = False
        #: 是否已经补过「当前稿」mapping（#174）。补丁幂等，但没必要每次写入都发一趟；
        #: 失败的不记，于是下一次写入会重试。
        self._mapping_patched = False
        self._timeout_s = timeout_s
        # **一个事件循环一个客户端**。见 `_get_client` 的注释：Celery 每个任务各
        # `asyncio.run` 一次，跨 loop 复用同一个 `AsyncElasticsearch` 会让连接池绑在
        # 一个已经关闭的 loop 上，任务直接 `RuntimeError: Event loop is closed`。
        # 用弱引用键：loop 被回收后条目自动消失，不会把死循环留在字典里。
        self._clients: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()

    @property
    def available(self) -> bool:
        return True

    @property
    def index_name(self) -> str:
        """唯一的物理索引名（读 / 写 / 删除都认的那个名字）。"""

        return self._index_name

    def _get_client(self) -> AsyncElasticsearch:
        """取当前事件循环该用的客户端。

        **必须按 loop 隔离**（一次真实事故换来的）：Provider 现在是进程级缓存，
        而 Celery 每个任务都 `asyncio.run(...)` 一次——也就是每个任务一个新事件循环，
        循环结束即关闭。如果整个进程只留一个 `AsyncElasticsearch`，它的连接池就绑在
        **第一个**循环上；第二个任务拿它发请求会立刻炸：

            RuntimeError: Event loop is closed

        表现是「同一批上传，第一篇成功、其余全失败」（每篇一个任务），而错误在界面上
        只是一句通用的 `document indexing failed`。

        注入的 `_client`（测试用假客户端）优先，且不参与按 loop 的缓存：那是调用方
        自己造的对象，生命周期由调用方负责。
        """

        if self._client is not None:
            return self._client

        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = AsyncElasticsearch(self._url)
            self._clients[loop] = client
        return client

    async def aclose(self) -> None:
        """关掉本 Provider 攒下的所有客户端（每个事件循环一个）。

        只在**进程要退出**时调用（应用 lifespan 的退出段 / 脚本 `finally`）。不关的话
        ``AsyncElasticsearch`` 的连接池会留到解释器收尾时由 GC 兜底，日志里就是那串
        ``Unclosed client session`` / ``Unclosed connector``。

        注入的 ``_client``（测试替身）**不在这里关**：它的生命周期归调用方，见
        :meth:`_get_client`。关单个客户端失败只记一条 warning——退出路径上抛异常会把
        真正的退出原因盖掉，而连接泄露最坏是多活几毫秒。
        """

        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            try:
                await client.close()
            except Exception:  # pragma: no cover - 退出路径不因关闭失败而中断
                logger.warning("keyword client close failed")

    async def ensure_index(self) -> None:
        """幂等建**那个物理索引**：已存在就跳过；并发建索引时把「已存在」当作成功。

        **已存在的索引要补一次 mapping**（#174）：第 11 步建好的物理索引里没有
        ``doc_version`` / ``is_current`` 两个字段，而 ES 对未映射字段的 ``term``
        查询不报错、只命中 0 条——关键词路会静默全空。补 mapping 是 additive 的，
        不重建索引也不动已有文档，见 :func:`build_mapping_update`。
        """

        if self._index_ready:
            return
        client = self._get_client()
        created = False
        if not await client.indices.exists(index=self._index_name):
            try:
                await client.indices.create(index=self._index_name, **build_es_mapping())
                created = True
            except BadRequestError as exc:
                # 两个 Worker 同时第一次索引时，后手会拿到 400
                # 「resource_already_exists_exception」。那是幂等，不是失败；
                # 其他 400（mapping 写错等）必须照常抛出去。
                if exc.error != "resource_already_exists_exception":
                    raise
        if not created:
            await self._patch_mapping()
        self._index_ready = True

    async def _patch_mapping(self) -> None:
        """给存量索引补上「当前稿」两个字段（每进程只试一次）。

        **失败不升级**：这是给老索引打的补丁，不是写入本身。集群只读、字段类型冲突这类
        拒绝确实会让关键词路继续少一路结果，但让整篇文档因为一次元数据补丁而索引失败，
        代价比它治的问题大——所以降级成一条 warning（带索引名，便于运维发现），
        并且**不记进已补标记**：下次再遇到这张索引会重试。
        """

        if self._mapping_patched:
            return
        try:
            await self._get_client().indices.put_mapping(
                index=self._index_name, **build_mapping_update()
            )
        except Exception as exc:  # noqa: BLE001 - 见上：补丁失败不拖垮写入
            logger.warning(
                "keyword index mapping patch skipped",
                index=self._index_name,
                error=type(exc).__name__,
            )
            return
        self._mapping_patched = True

    async def add_chunks(self, chunks: list[dict]) -> None:
        """把切片文档写进**那一个物理索引**（``_id`` 取 chunk_id，重复写入幂等）。"""

        documents = build_keyword_documents(chunks)
        if not documents:
            return

        await self.ensure_index()
        operations: list[dict] = []
        for document in documents:
            operations.append(
                {"index": {"_index": self._index_name, "_id": document["chunk_id"]}}
            )
            operations.append(document)

        response = await self._get_client().bulk(operations=operations, refresh=True)
        if response.get("errors"):
            # bulk 是「部分成功」的接口：不主动抛就会留下半边数据，索引阶段必须失败。
            #
            # 用 ``AppError(API_REQUEST_ERROR)`` 而不是裸 ``RuntimeError``：这个码在
            # ``app/tasks/indexing.py`` 的 ``RETRYABLE_ERROR_CODES`` 里，Worker 会把
            # 「ES 抖了一下」当成可重试的瞬时故障；抛 RuntimeError 则被判定为不可重试，
            # 一次 bulk 分片失败就把文档钉成失败态。文案固定、不带响应体——失败留痕
            # 只存固定文案，绝不把 ES 的内部报错写进库里或回给调用方。
            raise AppError(
                ErrorCode.API_REQUEST_ERROR, "elasticsearch bulk indexing reported errors"
            )

    async def keyword_search(
        self, query: str, *, tenant_id: str, kb_ids: Sequence[str], top_k: int
    ) -> list[dict]:
        # 查的是**那一个物理索引**（库级版本层删除之后没有第二张可选）：每篇在索引里
        # **只有一份**（写入总是先按文档全清、再写新的一份），所以不需要版本 / 代过滤。
        payload = build_bm25_query(
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            search_query=query,
            top_k=top_k,
        )
        try:
            response = await asyncio.wait_for(
                self._get_client().search(index=self._index_name, **payload),
                # 有界预算见类文档。超时抛 ``TimeoutError``，由调用方按「这一路挂了」处理
                # ——不在这里翻译成业务码：Provider 不认识业务码。
                timeout=self._timeout_s,
            )
        except NotFoundError:
            # 索引还没建 = 关键词库里一个字都没有。这不是故障，是诚实的空结果。
            return []
        except TimeoutError:
            logger.warning(
                "elasticsearch keyword search exceeded its budget",
                timeout_s=self._timeout_s,
                index=self._index_name,
            )
            raise

        return [
            {
                "chunk_id": hit["_source"]["chunk_id"],
                "document_id": hit["_source"]["document_id"],
                "kb_id": hit["_source"]["kb_id"],
                "title": hit["_source"].get("title", ""),
                "content": hit["_source"].get("content", ""),
                "score": float(hit.get("_score") or 0.0),
            }
            for hit in response["hits"]["hits"]
        ]

    async def delete_by_document_id(
        self,
        document_id: str,
        *,
        tenant_id: str,
        doc_version: int | None = None,
    ) -> None:
        # 关键词引擎没有「代」这一维（现场九·第五步）：它永远是**当前代的镜像**——重建时
        # 先按文档清空、再写新的一份。也只有一个物理索引可删，不再需要版本范围。
        try:
            await self._get_client().delete_by_query(
                index=self._index_name,
                **build_purge_query(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    # 稿号是唯一的删除范围（#173）：重建第 2 稿只清第 2 稿，旧稿原样留着。
                    doc_version=doc_version,
                ),
                refresh=True,
            )
        except NotFoundError:
            # 索引不存在 = 本来就没有可删的文档。
            return None

    async def mark_current_draft(
        self, document_id: str, *, tenant_id: str, doc_version: int
    ) -> None:
        """把这篇文档的关键词切片里「当前稿」标记翻到 ``doc_version``（#175）。

        **打的是那一个物理索引**，与检索同范围：只有一份镜像，所以翻转的范围就是检索的
        范围，不存在「别的物理索引里 ``is_current`` 仍为真的旧稿切片」这种漏网。

        ``refresh=True``：这次翻转之后马上就会有人提问，索引刷新不及时的表现是「关键词路
        偶发少了新稿的命中」。

        ``conflicts="proceed"``：并发重建正在改同一批文档时会撞版本冲突，``proceed`` 让其余
        文档照常更新；整批 abort 等于把这次翻转交给运气。冲突那几条会在下一次重建写切片时
        按文档指针重新算出来（索引期写 ``is_current``）。

        索引还不存在（``NotFoundError``）= 这次没有切片可翻，是诚实的 no-op；其余异常照常
        抛给调用方，由 ``app.services.keyword_draft`` 降级成 warning——Provider 不替调用方
        决定「这次失败能不能忍」。
        """

        try:
            await self._get_client().update_by_query(
                index=self._index_name,
                **build_current_draft_update(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    doc_version=doc_version,
                ),
                refresh=True,
                conflicts="proceed",
            )
        except NotFoundError:
            return None

    async def current_draft_versions(
        self, *, tenant_id: str, document_ids: Sequence[str]
    ) -> dict[str, list[int]]:
        """读回这批文档里**标成「当前稿」**的稿号（对账用，#179）。

        一条两层聚合打完整批（先按文档分组、组内数稿号），因此**不取命中行**：一篇文档
        的当前稿切片可能有几百片，把正文捞回来只为读两个字段是白花带宽。

        桶的 key 是**字符串**（mapping 里 ``doc_version`` 是 keyword），所以这里显式
        ``int()``——把 ``"3"`` 直接交给判据去和整数指针比，那一维会永远显示「相等」。

        失败语义：空批次与「索引还不存在」都回 ``{}``（没有可答的东西）；其余异常照常抛，
        由对账脚本分成「能力不存在」与「这一次读不到」两种不可判定——Provider 不替调用方
        决定这两件事要不要区分。
        """

        ids = [str(document_id) for document_id in document_ids if document_id]
        if not ids:
            return {}
        try:
            response = await self._get_client().search(
                index=self._index_name,
                **build_current_draft_versions_query(
                    tenant_id=tenant_id, document_ids=ids
                ),
            )
        except NotFoundError:
            return {}

        buckets = ((response.get("aggregations") or {}).get(CURRENT_DRAFT_VERSIONS_AGG) or {}).get(
            "buckets"
        ) or []
        return {
            str(bucket.get("key")): [
                int(item.get("key")) for item in (bucket.get("drafts") or {}).get("buckets") or []
            ]
            for bucket in buckets
        }

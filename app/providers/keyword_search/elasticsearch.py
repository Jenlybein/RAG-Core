"""Elasticsearch 关键词检索实现：只做「组装与调用」。

请求体、mapping、切片文档的形状全部由 ``query_builder`` 的三个纯函数产出，
这一层不拼过滤条件、不做分词选择，也不在这里降级：索引阶段任一次写入失败
就是整单失败（调用方回滚事务并把文档置为失败），检索阶段能不能退回向量由
业务层决定，不是 Provider 的权限。

``refresh=True`` 是刻意的：文档索引完必须立刻能查到，否则「上传完马上提问」
会稳定地查不到内容。批量导入的吞吐优化留给后续步骤。
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

import structlog
from elasticsearch import AsyncElasticsearch, BadRequestError, NotFoundError

from app.core.exceptions import AppError, ErrorCode
from app.providers.keyword_search.base import KeywordSearchProvider
from app.providers.keyword_search.query_builder import (
    build_bm25_query,
    build_es_mapping,
    build_keyword_documents,
    build_purge_query,
)
from app.providers.keyword_search.versioned_index import (
    build_alias_actions,
    physical_index_name,
    version_index_pattern,
)

logger = structlog.get_logger(__name__)


class ElasticsearchKeywordSearchProvider(KeywordSearchProvider):
    """ES 8.x 实现。客户端惰性构造：装配阶段绝不发网络请求。

    **``index_name`` 是别名，不是物理索引**（#142，ADR-0025 决策二）：检索与删除都认
    这个名字，而写入落到 ``<别名>__<版本>``。激活时把别名从旧物理索引挪到新物理索引，
    于是「构建期间线上照旧查旧版本」由名字解析本身保证，不需要任何开关或降级标记。

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
        #: 别名（配置里的 ``ELASTICSEARCH_INDEX``）。写入落在它派生出的物理索引上。
        self._index_name = index_name
        self._client = client
        #: 已经确认存在的**物理索引名**集合。按版本分，因为每个版本是一张新索引；
        #: 只记一个布尔会让「v2 的索引还没建」在第一次写 v2 时被跳过。
        self._index_ready: set[str] = set()
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
        """别名（检索 / 删除认的那个名字）。"""

        return self._index_name

    def physical_index_name(self, version: str) -> str:
        """某个版本对应的物理索引名（命名规则只在 ``versioned_index`` 里写一次）。"""

        return physical_index_name(self._index_name, version)

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

    async def ensure_index(self, index_name: str) -> None:
        """幂等建**物理索引**：已存在就跳过；并发建索引时把「已存在」当作成功。

        参数是物理索引名而不是别名：建完之后**不**自动挂别名——挂别名是激活的事
        （:meth:`activate_version`）。构建期间新建的 v2 索引没人指向它，正是想要的。
        """

        if index_name in self._index_ready:
            return
        client = self._get_client()
        if not await client.indices.exists(index=index_name):
            try:
                await client.indices.create(index=index_name, **build_es_mapping())
            except BadRequestError as exc:
                # 两个 Worker 同时第一次索引时，后手会拿到 400
                # 「resource_already_exists_exception」。那是幂等，不是失败；
                # 其他 400（mapping 写错等）必须照常抛出去。
                if exc.error != "resource_already_exists_exception":
                    raise
        self._index_ready.add(index_name)

    async def ensure_version_index(self, version: str) -> str:
        """确保该版本的物理索引存在，返回它的名字（给激活用）。"""

        index_name = self.physical_index_name(version)
        await self.ensure_index(index_name)
        return index_name

    async def physical_index_exists(self, version: str) -> bool:
        """该版本的物理索引还在不在（回滚守卫，#155）。

        **不查 ``_index_ready`` 缓存**：那份缓存记的是「本进程建过它」，而回滚要问的是
        「它此刻还在不在」。缓存说在、实际被清掉了（运维清理磁盘、集群重建过），别名切
        过去就是一张空索引——检索不报错，只是永远搜不到。
        """

        exists = await self._get_client().indices.exists(index=self.physical_index_name(version))
        return bool(exists)

    async def activate_version(self, version: str) -> None:
        """把别名切到该版本（一次 ``update_aliases``：摘旧 + 挂新）。

        目标物理索引不存在时**先建**再切：一个指向不存在索引的别名会让关键词召回
        一直 ``NotFoundError``（被当成「诚实空结果」），那比一次显式失败难查得多。
        """

        client = self._get_client()
        target = await self.ensure_version_index(version)
        current = await self._alias_targets()
        actions = build_alias_actions(
            alias=self._index_name, target=target, current=current
        )
        if not actions:
            # 已经指向目标：重复激活是幂等的 no-op，不多发一次可能失败的写。
            return
        await client.indices.update_aliases(actions=actions)
        logger.info(
            "keyword index alias switched",
            alias=self._index_name,
            target=target,
            previous=current,
        )

    async def _alias_targets(self) -> list[str]:
        """别名当前指向哪些物理索引（还没建过别名时为空）。"""

        try:
            response = await self._get_client().indices.get_alias(name=self._index_name)
        except NotFoundError:
            return []
        return [str(name) for name in response.keys()]

    async def add_chunks(self, chunks: list[dict]) -> None:
        """按版本分组写入**各自的物理索引**（不在同一张索引里双写两个版本）。"""

        documents = build_keyword_documents(chunks)
        if not documents:
            return

        by_index: dict[str, list[dict]] = {}
        for document in documents:
            index_name = self.physical_index_name(document["index_version"])
            by_index.setdefault(index_name, []).append(document)

        for index_name, group in by_index.items():
            await self.ensure_index(index_name)
            operations: list[dict] = []
            for document in group:
                operations.append(
                    {"index": {"_index": index_name, "_id": document["chunk_id"]}}
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

    async def count_documents(
        self, *, tenant_id: str, kb_id: str, index_version: str
    ) -> int:
        """数目标版本物理索引里这个租户 / 库 / 版本的文档数（``ready`` 前的校验）。

        版本条件在这层**看着冗余**（物理索引本来就只装一个版本），但它挡住的是最贵的一
        种错：写进了错误的物理索引。少了它，那种错会让四项计数「全都相等却都是错的」。
        """

        try:
            response = await self._get_client().count(
                index=self.physical_index_name(index_version),
                query={
                    "bool": {
                        "filter": [
                            {"term": {"tenant_id": tenant_id}},
                            {"term": {"kb_id": kb_id}},
                            {"term": {"index_version": index_version}},
                        ]
                    }
                },
            )
        except NotFoundError:
            # 物理索引还不存在 = 这个版本一个文档都没有。0 是诚实数字。
            return 0
        return int(response.get("count") or 0)

    async def keyword_search(
        self, query: str, *, tenant_id: str, index_versions: dict[str, str], top_k: int
    ) -> list[dict]:
        # 查的是**别名**：它此刻指向哪个物理索引就是哪一版。构建期间别名仍指着旧的，
        # 于是「构建中照旧查旧版本」不需要任何额外判断（ADR-0025 决策二）。
        payload = build_bm25_query(
            tenant_id=tenant_id,
            index_versions=index_versions,
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
        self, document_id: str, *, tenant_id: str, index_version: str | None = None
    ) -> None:
        # 给了版本只清那一张物理索引（重建新版本时绝不碰线上那一版）；没给版本时用
        # ``<别名>__*`` 覆盖**全部**版本——删文档要在每个版本里都清掉，否则某个归档版本
        # 被重新启用时，那些已删文档会原样回来。
        scoped = index_version is not None
        index = (
            self.physical_index_name(str(index_version))
            if index_version is not None
            else version_index_pattern(self._index_name)
        )
        try:
            await self._get_client().delete_by_query(
                index=index,
                **build_purge_query(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    index_version=index_version,
                ),
                refresh=True,
                # 通配符匹配到零张索引时 ES 会报「no such index」；那是「本来就没有可删的」。
                ignore_unavailable=not scoped,
            )
        except NotFoundError:
            # 索引不存在 = 本来就没有可删的文档。
            return None

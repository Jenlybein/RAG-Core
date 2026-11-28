"""把关键词索引「清空并重建」的一次性运维脚本（第 17 步现场九·上线动作）。

## 为什么需要它

关键词索引在最后一批里**去掉了版本化**：`ELASTICSEARCH_INDEX` 不再是一个指向 `<名字>__vN`
的别名，而是**那一个物理索引**——读、写、删都打它。这让「哪一份在服务」不再需要一份副本，
代价是**旧部署的数据不能自动搬过来**：

* 老部署的 `<名字>__vN` 物理索引不再被任何代码读到（它们变成孤儿索引，纯占盘）；
* 如果老部署的 `<名字>` 本身是**别名**，新代码的 `indices.exists()` 会认为「索引已存在」，
  于是继续往别名指向的那张旧物理索引里写——新数据进去了，旧数据也在里面，读到的是一半一半。

所以升级后**必须**对全部文档跑一次关键词重建。那条路只能靠本脚本：控制面的「全库重建」
是按库点的、一次最多 300 篇，一个几十库的部署要人点几十次；而且它顺带会做向量重建
（那我们不需要，向量侧本来就没有这个迁移问题）。

## 为什么默认 dry-run

它会删索引。删之前先把「要删什么、要重投几篇、哪些库」打出来，人看一眼再 `--apply`——
与 `scripts/purge_document_versions.py` 同一套纪律：**破坏性动作默认只读**。

## 它**不做**什么

* 不重建向量：pgvector 那侧的可见性判据是按代比列（没有第二份索引），不需要清空。
* 不自己重算切块：它只把「按文档删关键词切片 + 重新投递单篇索引任务」这两步做掉，切片
  与向量由既有的索引任务照常写。所以它**不会**让向量侧多算一次（`add` 是幂等的 upsert）。

## 用之前要知道的一件事

`--apply` 之后到索引任务跑完之前，关键词路会**暂时查不到**（索引删了、新数据还没写进去）。
检索是混合的，向量路照旧，所以是「召回变少」而不是「检索报错」。这也是为什么它不挂在任何
自动流程里。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

import structlog

from sqlalchemy import select

from app.core.config import settings
from app.db.session_factory import async_session_factory
from app.db.transaction import async_transaction
from app.models.knowledge_base import KnowledgeBase
from app.repositories import document_repository
from app.services.index_dispatch import enqueue_document
from app.utils.status import DocumentStatus

logger = structlog.get_logger(__name__)


async def _plan() -> tuple[list[tuple[str, str, str]], int]:
    """返回 ``(待重建的篇, 库里篇数合计)``：每项是 ``(document_id, tenant_id, kb_id)``。

    只收 ``SUCCESS`` 的篇：其余四态本来就有别的机制在推（排队中 / 索引中 / 失败要走单篇
    重建），这里再投一次只会撞状态 CAS。判据与 `generation_sweep` 不同——**这一批要的是
    「全部文档在关键词索引里各有一份」**，不是「产物换代」，所以不按指纹筛。
    """

    rows: list[tuple[str, str, str]] = []
    async with async_session_factory()() as db:
        # 脚本要覆盖**整个部署**（不是某个租户的一次请求），所以这里按库全扫一遍：
        # 控制面的分页入口一次只给一页，用它凑全量会把「哪些篇还没投」变成调用方的心算。
        kbs = (await db.execute(select(KnowledgeBase))).scalars().all()
        for kb in kbs:
            documents = await document_repository.list_by_kb_async(
                db, str(kb.id), tenant_id=str(kb.tenant_id)
            )
            for document in documents:
                if int(document.status) != int(DocumentStatus.SUCCESS):
                    # 只投成功态：其余四态另有机制在推（排队中 / 索引中 / 失败走单篇重建）。
                    continue
                rows.append((str(document.id), str(kb.tenant_id), str(kb.id)))
    return rows, len(rows)


def _clear_index() -> None:
    """删掉 `ELASTICSEARCH_INDEX` 指向的东西，并说明它原来是索引还是别名。

    这一步刻意区分两种情况：老部署里它是**别名**（指向 `<名字>__vN`），删别名只会让那个
    名字消失、物理索引还在、新代码又不会重建它——于是关键词路**永久空**。所以：
    先看它是不是别名，是别名就连它指向的物理索引一起删掉。
    """

    from elasticsearch import Elasticsearch

    client = Elasticsearch([settings.ELASTICSEARCH_URL])
    name = settings.ELASTICSEARCH_INDEX
    aliases = client.indices.get_alias(name=name, ignore_unavailable=True)
    if aliases:
        # 别名：它下面可能挂着好几张物理索引（历史版本），全删。
        for physical in aliases:
            print(f"  [别名] {name} → 物理索引 {physical}")
            client.indices.delete(index=physical, ignore_unavailable=True)
        print(f"  [别名] {name} 保留为空别名（下次写入会自动建索引）")
    elif client.indices.exists(index=name):
        print(f"  [索引] {name}（物理索引，直接删）")
        client.indices.delete(index=name, ignore_unavailable=True)
    else:
        print(f"  [缺失] {name} 不存在，无需删除")
    client.close()


async def _apply(rows: Sequence[tuple[str, str, str]]) -> int:
    """删索引 → 逐篇置排队中并投递（与单篇重建走同一条链路）。"""

    _clear_index()

    dispatched = 0
    async with async_session_factory()() as db:
        async with async_transaction(db):
            for document_id, tenant_id, kb_id in rows:
                prepared = await document_repository.prepare_for_indexing_async(
                    db, document_id, tenant_id=tenant_id, allow_success=True
                )
                if not prepared:
                    # 刚被别处投过（或正在跑）：跳过，不计入——「重建篇数必须是真数字」。
                    continue
                enqueue_document(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    request_id=None,
                )
                dispatched += 1
    return dispatched


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清空并重建关键词索引（索引代改造的上线动作）")
    parser.add_argument("--apply", action="store_true", help="真的删索引并投递（默认只打印计划）")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="最多投多少篇（分批推进用；不填=全部）",
    )
    args = parser.parse_args(argv)

    rows, total = asyncio.run(_plan())
    print(f"计划：清空并重建 {settings.ELASTICSEARCH_INDEX}；待重建 {total} 篇（SUCCESS 篇）")
    if args.limit is not None:
        rows = rows[: args.limit]
        print(f"  --limit {args.limit}：这一轮只投前 {len(rows)} 篇")

    if not args.apply:
        print("dry-run：没有删任何索引、没有投任何任务（加 --apply 才执行）")
        return 0

    dispatched = asyncio.run(_apply(rows))
    print(f"已投递 {dispatched} / {len(rows)} 篇（抢不到的篇不计入，可再跑一次）")
    logger.info(
        "keyword index rebuilt by script",
        dispatched=dispatched,
        planned=len(rows),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    # Windows 的默认事件循环（Proactor）跑不了 psycopg 的异步连接：脚本在开发机上会
    # 直接 `InterfaceError: cannot use ProactorEventLoop`。与其它运维脚本同一条修法。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(main())

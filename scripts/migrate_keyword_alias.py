"""把既有部署的关键词物理索引改名成 ``<别名>__v1`` 并建别名（#143；**不自动跑**）。

## 为什么需要这一步

``ELASTICSEARCH_INDEX`` 在这一轮从「物理索引名」变成了「**别名**」（#142，ADR-0025
决策二）：写入落到 ``<别名>__<版本>``，检索永远只认那个别名，激活时把别名挪到新物理索引。
于是**既有部署里那个叫 ``rag_chunks`` 的物理索引必须先改名再挂别名**，否则：

* 检索按 ``rag_chunks`` 查——它现在是个物理索引也能查到，但激活新版本时别名会「另外」
  建出来，两份数据从此各查一半（不报错）；
* 旧索引里的文档**没有 ``index_version`` 字段**（#142 才加），而查询带着
  ``index_version`` 过滤 → 别名一旦指向它，这一路会**静默返回空**：不报错、只是搜不到。
  所以迁移必须**回填**这一列（脚本用 ``_reindex`` 的 script 做）。

## 一条命令

```text
# 先看它准备做什么（默认就是这一步，一个写都不发）
uv run python scripts/migrate_keyword_alias.py

# 确认之后再加 --execute 真跑
uv run python scripts/migrate_keyword_alias.py --execute
```

参数都可以显式给：``--alias rag_chunks``、``--version v1``、``--url http://localhost:9200``；
缺省取配置（``ELASTICSEARCH_INDEX`` / ``ELASTICSEARCH_URL``）。

## 它做的事（顺序不能换）

```text
① 判形态：名字已经是别名 → 什么都不做（幂等）
          名字是物理索引 → 迁移
          名字不存在     → 全新部署，什么都不做（第一次构建会自己建 __v1）
② 建目标物理索引 <别名>__v1（mapping 用 app 里那一份，含中文分词器）
③ _reindex：老索引 → __v1，并回填 index_version=v1
④ 核对条数：不一致就**停在这里**（绝不删旧的）
⑤ 删掉老的物理索引 → 建别名 <别名> → <别名>__v1
```

⑤ 里「删索引」与「建别名」不能同时完成（ES 不允许一个名字既是索引又是别名），中间有
**毫秒级**的空窗：那段时间关键词这一路会拿到「索引不存在」→ 按既有口径退化成空结果。
所以这一步要在**停写**的维护窗口里跑（``reindex`` 期间也不要写入老索引，否则拷贝之后
新增的文档会丢）。

## 为什么默认不自动跑

它**删索引**。把它挂在启动、迁移或任何「跟着代码跑一遍」的位置，就意味着一次部署可能
删掉线上关键词索引——而删掉之后关键词这一路只是「变空」，不会报错（ADR-0025 决策四的
那种静默故障）。所以：两个开关都要人给（脚本本身要人敲，且要 ``--execute``），并且
②~⑤ 每一步都打印出来。

## 能不能不用这个脚本

能，而且**更干净**：拿 ``scripts/rebuild_kb_version.py`` 全量重建一次再激活。区别是
代价——重建要把全部文档重新解析 + embedding（贵、慢），迁移只搬关键词那一份数据。语料
不大的部署优先选重建：它连「历史遗留的脏 mapping」一起解决掉了。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import structlog

from app.providers.keyword_search.query_builder import build_es_mapping
from app.providers.keyword_search.versioned_index import (
    physical_index_name,
    version_index_pattern,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = structlog.get_logger(__name__)


class IndexState(str, Enum):
    """``ELASTICSEARCH_INDEX`` 在当前集群里的三种形态。"""

    #: 已经是别名（迁移过了，或本来就是新部署跑过一次激活）。
    ALIAS = "alias"
    #: 还是个物理索引（既有部署的原样，需要迁移）。
    PHYSICAL = "physical"
    #: 这个名字什么都没有（全新部署：第一次构建会自己建 ``__v1``）。
    ABSENT = "absent"


class MigrationRefused(RuntimeError):
    """不该继续（条数对不上、reindex 报错）：**调用方一行都别动**。"""


@dataclass(frozen=True)
class MigrationOutcome:
    """一次迁移的结论。``applied=False`` 表示「什么都没改」（dry-run / 无需迁移）。"""

    state: IndexState
    alias: str
    version: str
    source: str
    dest: str
    source_documents: int | None = None
    dest_documents: int | None = None
    applied: bool = False
    messages: tuple[str, ...] = field(default_factory=tuple)

    @property
    def needed(self) -> bool:
        return self.state is IndexState.PHYSICAL

    def render(self) -> list[str]:
        return [
            f"state={self.state.value} alias={self.alias} version={self.version}",
            f"source={self.source} dest={self.dest}",
            f"source_documents={self.source_documents} "
            f"dest_documents={self.dest_documents} applied={self.applied}",
            *self.messages,
        ]


def reindex_body(*, source: str, dest: str, version: str) -> dict:
    """``_reindex`` 的请求体：搬数据 + **回填 ``index_version``**。

    回填是这次迁移最容易漏、也最致命的一步：老文档没有这一列，而关键词查询按
    ``(库, 版本)`` 过滤。不填的话，别名切过去之后每一次查询都「诚实返回空」——线上表现
    是「关键词检索突然什么都搜不到」，而日志里一条错误都没有。

    只在字段**缺失**时写（``== null``）：已经是别的版本的文档不动它，那是别的构建的数据。
    """

    return {
        "source": {"index": source},
        "dest": {"index": dest},
        "script": {
            "source": (
                "if (ctx._source.index_version == null) {"
                " ctx._source.index_version = params.version }"
            ),
            "params": {"version": version},
        },
    }


def add_alias_actions(*, alias: str, index: str) -> list[dict[str, dict[str, str]]]:
    """把别名挂到目标索引上（``update_aliases`` 的 actions）。"""

    return [{"add": {"index": index, "alias": alias}}]


async def detect_state(client, *, alias: str) -> IndexState:
    """判断这个名字现在是别名 / 物理索引 / 不存在。

    顺序要紧：**先看别名**。一个名字不可能同时是索引与别名，但 `exists(index=...)` 在
    某些通配形参下会把别名也算进来；先问别名就不会误判成「还要迁一次」。
    """

    if await client.indices.exists_alias(name=alias):
        return IndexState.ALIAS
    if await client.indices.exists(index=alias):
        return IndexState.PHYSICAL
    return IndexState.ABSENT


async def _count(client, index: str) -> int:
    """索引里的文档数。**不吞异常**：这里读错就别往下走（下一步是删索引）。"""

    response = await client.count(index=index)
    return int(response.get("count") or 0)


async def migrate_keyword_alias(
    client, *, alias: str, version: str, execute: bool = False
) -> MigrationOutcome:
    """把物理索引 ``alias`` 改名成 ``alias__version`` 并建别名（``execute=False`` 只看）。

    返回结论对象；所有「改集群」的动作都排在 ``execute`` 之后，且**条数不一致就不往下
    走**——那一步之后是删除旧索引，走错了没有回头的路。
    """

    dest = physical_index_name(alias, version)
    state = await detect_state(client, alias=alias)
    messages: list[str] = []

    if state is IndexState.ALIAS:
        messages.append("名字已经是别名：迁移过了（本脚本是幂等的 no-op）")
        return MigrationOutcome(
            state=state, alias=alias, version=version, source=alias, dest=dest, messages=tuple(messages)
        )
    if state is IndexState.ABSENT:
        messages.append(
            f"名字不存在：全新部署无需迁移（第一次构建会建 {dest}，激活时挂别名；"
            f"同类物理索引：{version_index_pattern(alias)}）"
        )
        return MigrationOutcome(
            state=state, alias=alias, version=version, source=alias, dest=dest, messages=tuple(messages)
        )

    source_documents = await _count(client, alias)
    messages.append(f"① 老物理索引 {alias} 有 {source_documents} 个文档")

    if not execute:
        messages.extend(
            (
                f"② 建目标索引 {dest}（mapping 用 app 里那一份，含中文分词器）",
                f"③ _reindex：{alias} → {dest}，缺失的 index_version 回填为 {version}",
                "④ 核对条数：不一致就停（不删旧的）",
                f"⑤ 删掉 {alias} → 建别名 {alias} → {dest}",
                "（dry-run：以上一个写都没发；确认后加 --execute）",
            )
        )
        return MigrationOutcome(
            state=state,
            alias=alias,
            version=version,
            source=alias,
            dest=dest,
            source_documents=source_documents,
            messages=tuple(messages),
        )

    if not await client.indices.exists(index=dest):
        await client.indices.create(index=dest, **build_es_mapping())
        messages.append(f"② 已建 {dest}")
    else:
        messages.append(f"② {dest} 已存在，复用")

    response = await client.reindex(reindex_body(source=alias, dest=dest, version=version))
    failures = response.get("failures") or []
    if failures:
        raise MigrationRefused(
            f"reindex 报错 {len(failures)} 条，已停：{failures[0]!r}"
        )
    messages.append(f"③ reindex 完成（{response.get('total', source_documents)} 条）")

    dest_documents = await _count(client, dest)
    if dest_documents != source_documents:
        raise MigrationRefused(
            f"条数对不上：源 {source_documents} / 目标 {dest_documents}；"
            f"旧索引 {alias} 未删除，先查清再重跑"
        )
    messages.append(f"④ 条数一致（{dest_documents}）")

    await client.indices.delete(index=alias)
    await client.indices.update_aliases(actions=add_alias_actions(alias=alias, index=dest))
    messages.append(f"⑤ 已删 {alias} 并把别名指向 {dest}")

    logger.info(
        "keyword alias migrated", alias=alias, version=version, documents=dest_documents
    )
    return MigrationOutcome(
        state=state,
        alias=alias,
        version=version,
        source=alias,
        dest=dest,
        source_documents=source_documents,
        dest_documents=dest_documents,
        applied=True,
        messages=tuple(messages),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rename the legacy physical keyword index to <alias>__<version> and point the "
            "alias at it. Dry-run by default; pass --execute to change the cluster."
        )
    )
    parser.add_argument(
        "--url", default=None, help="Elasticsearch URL; defaults to ELASTICSEARCH_URL"
    )
    parser.add_argument(
        "--alias",
        default=None,
        help="the configured index name, now an alias; defaults to ELASTICSEARCH_INDEX",
    )
    parser.add_argument(
        "--version",
        default="v1",
        help="version name of the legacy data (default v1: it is what it has been serving)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually change the cluster (creates, reindexes, deletes the old index)",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    from elasticsearch import AsyncElasticsearch

    from app.core.config import settings

    alias = args.alias or settings.ELASTICSEARCH_INDEX
    url = args.url or settings.ELASTICSEARCH_URL
    client = AsyncElasticsearch(url)
    try:
        try:
            outcome = await migrate_keyword_alias(
                client, alias=alias, version=args.version, execute=args.execute
            )
        except MigrationRefused as exc:
            print(f"migration refused: {exc}", file=sys.stderr)
            return 1
        for line in outcome.render():
            print(line)
        if outcome.needed and not outcome.applied:
            print("RESULT: DRY-RUN（未改动集群；确认后加 --execute）")
            return 0
        if not outcome.needed:
            print("RESULT: NOTHING TO DO")
            return 0
        print("RESULT: MIGRATED")
        return 0
    finally:
        await client.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # psycopg 无关，但 ES 的异步客户端同样要求一个正常的循环；Windows 的缺省 Proactor
    # 循环对 aiohttp 有已知限制，这里与其它运维脚本保持同一套（见 _ops_common 的用法）。
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

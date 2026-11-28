"""关键词一路的「多查询并集」（第 15 步）：逐句召回 → 并成一个池 → 交给既有 RRF。

与 :mod:`app.services.retrieval.vector_recall` **一一对称**，这不是巧合，是这一层现在
唯一值钱的约定：**两条路对「这次到底问的是什么」必须看法一致**。第 15 步之前不是这样——
向量逐句都搜（原句 + 改写句），关键词只拿一句（改写句经同义词扩展），于是改写把口语换成
文档说法之后，字面路再也搜不到只含原句词面的切片，而那类切片恰恰是 BM25 的强项。

**为什么单独一个模块**：``pipeline._keyword_branch``（混合模式）与
``kernel._retrieve_by_keyword``（``mode=bm25`` 单模式）都要做同一件事，而它们对该有的
行为约定必须完全一致——尤其「只有一句时逐字节不变」。放在同一处，才不会出现两条调用点
一套去重规则。

**为什么不在一 个 ES 请求里 `should` 两句**：那会让分数变成两句相加（与单句时的分数不可
比，而响应里 ``score`` 是要给人看的），且召回池只有 ``top_k`` 条，不是各 ``top_k`` 条并起来
——一句话独有的命中会被挤掉。两次请求并发的成本约等于 1 次 RTT。

**任一句失败就是这一路失败**：``gather`` 不吞异常，绝不拿「另一句的半份结果」冒充完整
召回池。空实现（引擎没配）同样是「能力不存在」而不是「查过了没命中」。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import structlog

from app.core.exceptions import AppError, ErrorCode
from app.services.retrieval.merge import merge_rows

logger = structlog.get_logger(__name__)


async def keyword_recall(
    *,
    provider: Any,
    tenant_id: str,
    kb_ids: Sequence[str],
    queries: Sequence[str],
    top_k: int,
) -> list[dict]:
    """每句各查一次，并成一个池后截到 ``top_k``。

    ``kb_ids`` 是**调用方解析好的范围**（``kb_id → 索引版本``的映射）：这里不再
    自己造一份——范围是「一次调用」的属性，而这个函数会被多句各调一次。

    单句（改写关闭 / 失败 / 两句相同）时走快路径：一次 ``keyword_search``，结果原样返回、
    **不重排**（ES 已按分数给顺序），调用次数与逐字节响应都不变。

    错误一律翻译成 ``50002``：引擎故障属于「这一路不可用」，不是整单 500。日志只记类型
    与范围，不记检索原文（ADR-0021）。
    """

    if not provider.available:
        raise AppError(ErrorCode.RETRIEVAL_FAILED, "keyword search engine is not configured")

    wanted = [query for query in queries if query.strip()]
    if not wanted:
        # 空查询在上游被 20033 挡掉了；真到了这里也当「没有结果」，
        # 而不是拿空串去查——ES 会把空查询变成 match_all，那等于把整个库捞回来。
        return []

    try:
        batches = await asyncio.gather(
            *(
                provider.keyword_search(
                    query,
                    tenant_id=tenant_id,
                    # 范围只剩「哪几个库」（现场九·第四步）：代下放到篇级之后，库级版本
                    # 不再是可见性判据；索引里每篇只有一份（写入先按文档全清再写）。
                    kb_ids=list(kb_ids),
                    top_k=top_k,
                )
                for query in wanted
            )
        )
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001 - 引擎故障一律归为「这一路不可用」
        logger.warning(
            "keyword search failed",
            tenant_id=tenant_id,
            kb_ids=list(kb_ids),
            error=type(exc).__name__,
        )
        raise AppError(ErrorCode.RETRIEVAL_FAILED, "keyword search failed") from exc

    return merge_rows([list(batch) for batch in batches])[:top_k]

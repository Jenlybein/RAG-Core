"""把关键词索引里的「当前稿」标记翻到某一稿（#175）。

## 为什么要有这一层（而不是在服务里直接调 Provider）

判据只有一句：**库里的当前稿指针是唯一真值，关键词索引里的 ``is_current`` 是派生视图**。
派生视图的维护失败不该升级成业务失败——但在服务里直接调 Provider 时，一次 ES 抖动就会
让「版本没建成」返回给用户，而那时新稿其实已经落库了（提交在先、副作用在后）。

所以失败语义收在这一层：任何异常都降级成一条 warning。反方向的代价更小：漏翻一次，
关键词路会继续召回旧稿，直到下一次重建写切片时按文档指针重新算出 ``is_current``；
而两路的差异在下一次任何勾选都会消失。

## 为什么必须在 ``after_commit``

库先落地、索引后翻。反过来的话，索引里的「当前稿」会指向一个还不存在的稿次——那一刻
检索按 ``is_current`` 过滤，命中的切片属于一份数据库里查不到的稿。
"""

from __future__ import annotations

import structlog

from app.providers.keyword_search import get_keyword_search_provider

logger = structlog.get_logger(__name__)


async def flip_current_draft(*, document_id: str, tenant_id: str, doc_version: int) -> None:
    """把这篇文档的关键词切片里「当前稿」标记翻到 ``doc_version``。

    ``available`` 为假时不发那一趟请求：没部署关键词引擎就没有标记可翻，而空实现也只是
    「安静地什么都不做」——少一次调用、少一条日志噪音。
    """

    provider = get_keyword_search_provider()
    if not provider.available:
        return
    await provider.mark_current_draft(document_id, tenant_id=tenant_id, doc_version=doc_version)


async def flip_current_draft_after_commit(
    *, document_id: str, tenant_id: str, doc_version: int
) -> None:
    """``after_commit`` 用的那一版：**失败只降级，不阻断**。

    日志里只放 ID 与派生量（稿号），不放检索文本、正文或异常原文——异常原文可能带着
    ES 的响应体（mapping 细节、内部字段名），那些不该经应用日志扩散。
    """

    try:
        await flip_current_draft(
            document_id=document_id, tenant_id=tenant_id, doc_version=doc_version
        )
    except Exception as exc:  # noqa: BLE001 - 派生视图维护失败不阻断请求，见模块文档
        logger.warning(
            "keyword current draft flip skipped",
            document_id=document_id,
            tenant_id=tenant_id,
            doc_version=doc_version,
            error=type(exc).__name__,
        )


__all__ = ["flip_current_draft", "flip_current_draft_after_commit"]

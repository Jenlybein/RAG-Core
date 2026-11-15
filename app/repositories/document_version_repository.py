"""版本行的数据访问：唯一能碰 ``document_versions`` 的一层（#172 / #173）。

本文件只放仓储该管的读与删：

* :func:`get_by_version` —— 取某一稿的版本行（重建的正文真值从这里读，#173 / D6）；
* :func:`delete_for_document` —— 删文档时把这一篇的版本行带走（#172）。

写入侧（建版本、归档旧稿、切指针）属于后续的票（#175 / #176），这里不提前长出来：
没有调用点的函数没人验证，而「先建好再说」的那一半最容易与真实写入路径分叉。

只 flush / execute，不 commit——事务由请求边界收口（与其余仓储同一口径）。

## 为什么这里没有租户参数

``document_versions`` 没有 ``tenant_id`` 列（ADR-0005 D3 的列集），租户隔离由**文档 id**
兜底：入口都是先按 ``(id, tenant_id)`` 取到文档行、守卫通过，再调本层；
``document_id`` 是服务端生成的 UUID，跨租户撞不上。给这里加一个没有列可比的租户参数
只会造出「看起来在过滤、其实没有」的假守卫。
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document_version import DocumentVersion


async def get_by_version(
    db: AsyncSession, document_id: str, version: int
) -> DocumentVersion | None:
    """取这篇文档第 ``version`` 稿的版本行；没有这一稿就回 ``None``。

    **``scalars().first()`` 而不是 ``scalar_one_or_none()``**：``(document_id, version)``
    有唯一约束，真库上至多一行，两者等价；但 ``first()`` 是离线替身（``tests/fake_db``）
    也实现的那一种收尾，于是「没有版本行」在套件里表现为干净的 ``None``，而不是一条
    只有真库才炸的 ``AttributeError``。

    读不到就是 ``None``：调用方决定是回落还是拒绝（见
    ``app.services.indexing_service.IndexingService`` 的正文来源分支）。仓储在这里
    替调用方猜「那就当第 1 稿吧」是最坏的选择——它会把一次指针错位变成一份错的切片。
    """

    statement = select(DocumentVersion).where(
        DocumentVersion.document_id == document_id,
        DocumentVersion.version == version,
    )
    return (await db.execute(statement)).scalars().first()


async def delete_for_document(db: AsyncSession, document_id: str) -> None:
    """删掉这篇文档的**全部**版本行（当前稿与归档稿一起走）。

    零外键的代价（见 ``app/models/document_version.py`` 的模块文档）：库不会替我们级联，
    所以删文档那条路径必须显式调它，并且与清除切片、删文档行落在**同一笔事务**里——
    分开提交就会留下「文档还在、版本没了」或「版本还在、文档没了」的半截状态。

    一条 DELETE 打掉整篇的所有稿次（不是只删当前稿）：文档都不在了，审计所需的「这篇文档
    曾经有哪些稿」应当随文档一起消失，否则版本行会变成永远无人认领的孤儿。
    """

    await db.execute(delete(DocumentVersion).where(DocumentVersion.document_id == document_id))


__all__ = ["delete_for_document", "get_by_version"]

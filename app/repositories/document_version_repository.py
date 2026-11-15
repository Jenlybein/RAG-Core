"""版本行的数据访问：唯一能碰 ``document_versions`` 的一层（#172 / #173 / #175）。

本文件放仓储该管的读、写与删：

* :func:`get_by_version` —— 取某一稿的版本行（重建的正文真值从这里读，#173 / D6）；
* :func:`max_version` —— 这篇文档现在最高是第几稿（建版本的 ``+1`` 从它起算，#175）；
* :func:`create` —— 落一条新版本行（只 flush，让 ``(document_id, version)`` 唯一约束
  在这里撞出来）；
* :func:`rewrite_content` —— 把**同一稿**的正文与校验和换掉（重解析的写回，#182）；
* :func:`archive_current_except` —— 把别的当前稿行转归档（切指针的另一半，#175）；
* :func:`list_by_document` —— 列版本（按稿号倒序，#175 的读接口）；
* :func:`list_archived_at_or_below` —— 保留窗口**要清掉的那批**归档稿（#178）；
* :func:`delete_versions` —— 按稿号删行（保留策略的另一半，#178）；
* :func:`source_file_paths` —— 这篇文档各稿的源文件路径（删文档时一起清掉）；
* :func:`delete_for_document` —— 删文档时把这一篇的版本行带走（#172）。

只 flush / execute，不 commit——事务由请求边界收口（与其余仓储同一口径）。

## 为什么这里没有租户参数

``document_versions`` 没有 ``tenant_id`` 列（ADR-0005 D3 的列集），租户隔离由**文档 id**
兜底：入口都是先按 ``(id, tenant_id)`` 取到文档行、守卫通过，再调本层；
``document_id`` 是服务端生成的 UUID，跨租户撞不上。给这里加一个没有列可比的租户参数
只会造出「看起来在过滤、其实没有」的假守卫。

## 稿号为什么由写侧算（``max + 1``）

列上没有默认值（见 ``app/models/document_version.py``），于是「忘了传稿号」当场是
``IntegrityError`` 而不是一次静默的 ``1``。串行化由**文档行的 ``FOR UPDATE`` 锁**给
（``app.repositories.document_repository.lock_for_version``）：两个提交排队算同一个
``max``，后一个看到的一定是前一个写下的行。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document_version import (
    DOCUMENT_VERSION_ARCHIVED,
    DOCUMENT_VERSION_CURRENT,
    DocumentVersion,
)
from app.utils.checksum import checksum_of


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


async def max_version(db: AsyncSession, document_id: str) -> int:
    """这篇文档现在的最高稿号；一条都没有时是 **0**（于是下一个稿号是 1）。

    ``coalesce`` 写在 SQL 里而不是在 Python 里 ``or 0``：``max()`` 对空集返回 NULL，
    而 ``None + 1`` 会炸在一个和稿号无关的行上。返回 0 让「还没建过版本」成为一条
    正常输入（#172 之前上传的文档全是这样）。
    """

    statement = select(func.coalesce(func.max(DocumentVersion.version), 0)).where(
        DocumentVersion.document_id == document_id
    )
    return int((await db.execute(statement)).scalar_one())


async def create(db: AsyncSession, version: DocumentVersion) -> DocumentVersion:
    """落一条新版本行并 ``flush``（不提交）。

    ``flush`` 是刻意的：``(document_id, version)`` 的唯一约束必须在**写路径里**撞出来，
    调用方才能把它映射成「冲突，请重试」（20029）。若拖到请求边界的提交才炸，那时已经
    没法把错误归因到「稿号撞了」，只能整笔 500。
    """

    db.add(version)
    await db.flush()
    return version


async def rewrite_content(
    db: AsyncSession, version: DocumentVersion, *, content: str
) -> DocumentVersion:
    """把**这一稿**的正文与校验和一起换掉（重解析的写回，#182）。

    ## 为什么是改写同一行，而不是新加一稿

    重解析不是「改稿」：同一份原始文件，换一个解析器再读一遍，稿次没变。新加一行等于
    在时间轴上凭空长出一稿，而 ``max(version) + 1`` 的下一次提交会跟着错位；更直接的
    症状是「文档正文 == 当前稿正文」这条对账口径永久不成立。

    稿号、``status``（当前 / 归档）、``created_at``、``id`` 一律不动：重解析不是切稿，
    归档稿重新解析完仍是归档稿。

    ## 校验和为什么由本层算，而不是调用方传进来

    传进来的话，「正文」与「校验和」就是两个独立输入，调用方某天传了一份规范化过的正文
    配一份没规范化的校验和，库里的两列从此互相矛盾——而对账读的正是校验和。由这里用
    唯一入口 :func:`app.utils.checksum.checksum_of` 从刚写进去的那份正文现算，
    「两列说的是同一份文本」就是**构造上**成立的，不靠调用方守纪律。

    ## 只 flush

    与 :func:`create` 同一口径：这一笔写与切片、文档行正文、状态回写同属 Tx2，提交点
    在 Worker 那一层（``app.db.transaction.async_commit_now``）。这里 flush 一下，让
    「重解析之后同一段代码里再读这行」拿到的是新值。
    """

    version.content = content
    version.checksum = checksum_of(content)
    await db.flush()
    return version


async def archive_current_except(db: AsyncSession, document_id: str, *, keep: int) -> None:
    """把这篇文档里**除第 ``keep`` 稿之外**的当前稿行转归档。

    「除 keep 之外」而不是「比 keep 小的」：库里万一残留一条更高稿号的当前行（历史事故、
    人工修数），「每篇文档恰好一条当前稿版本行」也不能被它破掉。

    一条 ``UPDATE ... WHERE`` 打掉整批，不在 Python 里逐行改：行数是「这篇文档有几条
    当前稿」的函数，逐行改会把一次切稿变成 N 次往返，而切稿在写请求里。
    """

    await db.execute(
        update(DocumentVersion)
        .where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version != keep,
            DocumentVersion.status == DOCUMENT_VERSION_CURRENT,
        )
        .values(status=DOCUMENT_VERSION_ARCHIVED)
        .execution_options(synchronize_session=False)
    )


async def list_by_document(db: AsyncSession, document_id: str) -> list[DocumentVersion]:
    """这篇文档的全部版本行，**按稿号倒序**（现在这一稿在最前）。

    排序放在 SQL 里：调用方（列表接口）按同一个顺序渲染，而应用层排序会在
    「有 200 稿的文档」上把整表读进内存。带正文与否由调用方决定，这里一律读全行——
    正文列是 ``Text``，多读一次比多一个「只读元数据」的孪生查询便宜，也少一处会漂移的
    列白名单。
    """

    statement = (
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version.desc())
    )
    return list((await db.execute(statement)).scalars().all())


async def source_file_paths(db: AsyncSession, document_id: str) -> list[str]:
    """这篇文档各稿的源文件路径（去掉 NULL）。

    删文档时用：版本行的源文件是本票新增的落盘产物（``<doc_id>.v<N><ext>``），不跟着
    删就是一批永远无人认领的文件。

    读的是**整行**而不是只选 ``source_file_path`` 一列：``select(Model.列)`` 在离线替身
    （``tests/fake_db.RecordingSession``）上认不出是哪张表，删文档那条既有路径会在套件里
    炸掉——而这条查询的唯一目的就是删文件，不值得为它换一套替身。
    """

    rows = await list_by_document(db, document_id)
    return [row.source_file_path for row in rows if row.source_file_path]


async def list_archived_at_or_below(
    db: AsyncSession, document_id: str, *, version: int
) -> list[DocumentVersion]:
    """这篇文档里**稿号 ≤ ``version`` 的归档稿**，按稿号倒序。

    这是保留窗口的输入（#178）：窗口下界由调用方算（``当前稿号 - 保留稿数``），仓储只
    回答「这个界以下有哪些归档稿」。``status == archived`` 是「当前稿永不删」的落点——
    写成查询条件而不是调用方的一句 if，库里残留的低稿号当前行（历史事故、人工修数）就
    不会被交出去，也就不会被删掉。

    排序仍放 SQL 里（与 :func:`list_by_document` 同一理由）：调用方按「最老的先走」的顺序
    打印 / 逐稿清切片，应用层再排一次只会多一次整表读。
    """

    statement = (
        select(DocumentVersion)
        .where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.status == DOCUMENT_VERSION_ARCHIVED,
            DocumentVersion.version <= version,
        )
        .order_by(DocumentVersion.version.desc())
    )
    return list((await db.execute(statement)).scalars().all())


async def delete_versions(
    db: AsyncSession, document_id: str, versions: Sequence[int]
) -> None:
    """按稿号删这篇文档的版本行（保留策略的另一半，#178）；空列表是 no-op。

    一条 ``DELETE ... IN (...)`` 打掉整批，不逐行 ``db.delete``：删的是同一篇文档的同一段
    窗口，逐行删会在网络往返上放大成「有几稿就几次」，而这批行来自一次整体判断。

    **调用方必须先把这些稿的切片清掉**（先删切片、再删版本行）：反过来的顺序在事务回滚时
    留下的是「切片还在、版本行没了」——那批切片永远不会被任何查询命中，也无法再被定位到
    是哪一稿的，属于比半份更坏的一种孤儿。仓储只负责删行，顺序由服务层守
    （``app.services.document_version_service.purge_expired_versions``）。
    """

    if not versions:
        return
    await db.execute(
        delete(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version.in_(tuple(versions)),
        )
    )


async def delete_for_document(db: AsyncSession, document_id: str) -> None:
    """删掉这篇文档的**全部**版本行（当前稿与归档稿一起走）。

    零外键的代价（见 ``app/models/document_version.py`` 的模块文档）：库不会替我们级联，
    所以删文档那条路径必须显式调它，并且与清除切片、删文档行落在**同一笔事务**里——
    分开提交就会留下「文档还在、版本没了」或「版本还在、文档没了」的半截状态。

    一条 DELETE 打掉整篇的所有稿次（不是只删当前稿）：文档都不在了，审计所需的「这篇文档
    曾经有哪些稿」应当随文档一起消失，否则版本行会变成永远无人认领的孤儿。
    """

    await db.execute(delete(DocumentVersion).where(DocumentVersion.document_id == document_id))


__all__ = [
    "archive_current_except",
    "create",
    "delete_for_document",
    "delete_versions",
    "get_by_version",
    "list_archived_at_or_below",
    "list_by_document",
    "max_version",
    "rewrite_content",
    "source_file_paths",
]


"""切片的普通 CRUD（向量写入与相似度检索归 PgVectorStore）。

``count_by_document_ids`` 供文档列表接口一次拿到全部文档**当前稿**的切片数，不做 N+1；
``count_by_kb_ids`` 供检索层判定「空结果到底是哪种空」（票 4）；
``delete_by_document_id`` 由索引重建 / 删除文档复用。

**两个面，一条语句**（第 11 步票 #127）：检索链路上那三条读（折叠回查 meta、折叠取整节、
空结果计数）各自有一个 ``*_async`` 孪生，与同步版**共用同一个语句构造器**。语句写两遍是
迁移期最容易漂移的东西——两边的租户过滤、版本过滤、排序一旦分叉，症状是「异步路径偶尔
多出几条别人的切片」这种事后极难查的故障。

**四条读都带「只看当前稿」**（#174，以及文档列表那一处计数）：``chunks.doc_version =
documents.current_version`` 由 :func:`app.models.chunk.current_draft_filter` 一处编译，
四条语句各 JOIN 一次文档表。数字上多了一次 JOIN，换来的是「两稿切片并存的窗口里，旧稿
永远不会被折进整节、不会被算进『库里有内容』，也不会被数进文档列表的 ``chunk_count``」
——这正是父 spec 点名的五个口径中的三个（另外两个在 Provider 里），外加一个对外可见的
数字。回查那条还是唯一把稿号带出来的地方：``doc_version`` 来自真实列。

**文档列表的分组计数已经在异步面上**（第 11 步票 #130）：列文档（数据面）与控制面的树
都换了 ``AsyncSession``，所以 ``count_by_document_ids`` 只剩这一份实现。写侧的
``delete_by_document_id`` 仍只有同步面——purge 走的是存储抽象
（``app/services/chunk_store.py``），不经过它。
"""

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import Integer, Select, and_, delete, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.chunk import (
    Chunk,
    current_draft_filter,
    current_generation_filter,
)
from app.models.document import Document


def _join_documents(statement: Select[Any]) -> Select[Any]:
    """把文档表 JOIN 进来：**当前稿指针在文档行上**，不 JOIN 就没有「谁是当前稿」。

    一并写成一个函数是为了让三条语句长得一样——各写一遍 ``.join(Document, ...)`` 时，
    漏掉一处的那条查询不会报错，只会静默少一个条件（旧稿切片照旧被折进整节）。
    """

    return statement.join(Document, Document.id == Chunk.document_id)


def _meta_statement(
    *, tenant_id: str, kb_ids: list[str], chunk_ids: list[str]
) -> Select[tuple[str, str, dict, int]]:
    """折叠回查 / 溯源回查：按 ``(kb_id, chunk_id)`` 取回 ``meta`` 与稿号。

    **稿号也在这里取**（``chunks.doc_version`` 列）：它是引用三元组（文档 + 稿次 + 切片）
    的第三段，而 JSONB 里那份副本在 #172 的迁移里已经删掉。

    **带当前稿条件**（#174）：切片被重建替换之后，旧 id 在这一条查询里查不到——调用方
    据此把 ``doc_version`` 落成 ``null``（明确的「已过期」），而不是拿到当前稿那一行的
    元数据、把当前稿的页码与稿号当成旧切片的。
    """

    # 唯一键是 ``(kb_id, chunk_id)``（ADR-0010），不是单独的 chunk_id：跨库同 ID
    # 的两条结果必须能各自取到自己的 meta，不能被后到的那条覆盖。
    return _join_documents(select(Chunk.kb_id, Chunk.id, Chunk.meta, Chunk.doc_version)).where(
        Chunk.tenant_id == tenant_id,
        Chunk.kb_id.in_(kb_ids),
        Chunk.id.in_(chunk_ids),
        current_draft_filter(),
    )


async def fetch_meta_by_chunk_ids(
    db: AsyncSession, *, tenant_id: str, kb_ids: list[str], chunk_ids: list[str]
) -> dict[tuple[str, str], dict]:
    """:func:`fetch_meta_by_chunk_ids` 的异步面（检索链路，第 11 步票 #127）。

    返回的每份 meta 里都带 ``doc_version``，**值来自真实列**（同名字段对外）。放在同一份
    字典里而不是多返回一个结构：调用方（折叠补定位字段）只认一个键名，不关心它出身是列
    还是 JSONB——#172 之后 JSONB 那份副本不再存在，两处读法迟早分叉。
    """

    if not chunk_ids or not kb_ids:
        return {}
    rows = (
        await db.execute(_meta_statement(tenant_id=tenant_id, kb_ids=kb_ids, chunk_ids=chunk_ids))
    ).all()
    return {
        (str(row.kb_id), str(row.id)): {
            **(row.meta or {}),
            # 列覆盖 JSONB：老行里或许还留着 ``doc_version`` 这个键，它已经不可信。
            "doc_version": int(row.doc_version),
        }
        for row in rows
    }


def _unit_rows_statement(
    *,
    tenant_id: str,
    document_ids: list[str],
    units: Sequence[tuple[str, int]],
) -> Select[tuple[str, str, int, int, str, str, str, int]]:
    """折叠取一节段：按 h2 级章节路径**加节段号**取该单元下的全部切片。

    **只取当前稿的行**（#174）：漏掉这条条件的症状最隐蔽——命中片是当前稿的，折出来的
    整节却拼上了旧稿的句子，「一半旧稿一半新稿」，而结果看起来完全正常。

    节段号取自 ``metadata``（JSONB），缺省按 0 处理：老索引里没有这个键，那里的一整节
    就是第 0 段——与切段之前的取数范围逐行相同。
    """

    heading = Chunk.meta["heading_path"].astext
    # ``coalesce(section_part, 0)``：老行没有这个键，它们属于第 0 段。
    section_part = func.coalesce(Chunk.meta["section_part"].astext.cast(Integer), 0)
    clauses = []
    for path, part in units:
        path_clause = or_(heading == path, heading.like(f"{path} > %"))
        # 每个单元是「路径 AND 节段号」，单元之间 OR：否则 A 的第 1 段会把 B 的第 0 段
        # 也一并取回来，折出来的正文就是两节的混合体。
        clauses.append(and_(path_clause, section_part == part))
    stmt = _join_documents(
        select(
            Chunk.document_id,
            heading,
            section_part.label("section_part"),
            Chunk.meta["chunk_index"].astext.cast(Integer),
            Chunk.content,
            Chunk.id,
            Chunk.meta["chunk_type"].astext,
            Chunk.meta["table_part"].astext.cast(Integer),
        )
    ).where(
        Chunk.tenant_id == tenant_id,
        Chunk.document_id.in_(document_ids),
        or_(*clauses),
        current_draft_filter(),
    )
    stmt = stmt.order_by(
        Chunk.document_id, Chunk.meta["chunk_index"].astext.cast(Integer), Chunk.id
    )
    # 「哪一代」由库按文档行求值（现场九·第四步）：不再按库级生效版本收窄——构建期间新代
    # 已在库里、生效版本还是旧的，两个条件相乘会把刚建好的那篇整个抹掉。
    stmt = stmt.where(current_generation_filter())
    return stmt


def _group_unit_rows(
    rows: Iterable[Sequence[Any]],
) -> dict[tuple[str, str, int], list[tuple[int, str, str, str, int]]]:
    """按 ``(文档, 单元路径, 节段号)`` 归组。

    节段号进键（第 16 步 #207）：一个超长 h2 在索引期被切成若干段，段与段是不同的节；
    只按路径归组会把整节的片又拼回来，正是这次要消灭的「上万字」。
    """

    grouped: dict[tuple[str, str, int], list[tuple[int, str, str, str, int]]] = {}
    for document_id, path, section_part, index, content, chunk_id, chunk_type, table_part in rows:
        grouped.setdefault((str(document_id), str(path), int(section_part or 0)), []).append(
            (
                int(index or 0),
                str(content),
                str(chunk_id),
                str(chunk_type or ""),
                int(table_part or 0),
            )
        )
    return grouped


async def fetch_unit_rows(
    db: AsyncSession,
    *,
    tenant_id: str,
    document_ids: list[str],
    units: Sequence[tuple[str, int]],
) -> dict[tuple[str, str, int], list[tuple[int, str, str, str, int]]]:
    """:func:`fetch_unit_rows` 的异步面（检索链路，第 11 步票 #127）。

    ``units`` 是 ``(单元路径, 节段号)`` 的列表——不是裸路径，理由见 :func:`_group_unit_rows`。
    """

    if not document_ids or not units:
        return {}
    stmt = _unit_rows_statement(
        tenant_id=tenant_id,
        document_ids=document_ids,
        units=units,
    )
    return _group_unit_rows(list((await db.execute(stmt)).all()))


def build_parent_rows_statement(
    *,
    tenant_id: str,
    document_ids: Sequence[str],
    heading_paths: Sequence[str],
) -> Select[tuple[str, str, int, str]]:
    """**父节自己的**正文：按 ``(文档, 精确标题路径)`` 取，供「折叠关闭时补父节」用（#144）。

    为什么要一条新查询、而不是复用 :func:`_unit_rows_statement`：那条是**前缀**语义
    （``path = p OR path LIKE 'p > %'``），拿它取父节会把命中片自己与它的兄弟节一起
    拼回来——材料量翻倍，而 ADR-0027 决策 7 说的正是「不要再翻一倍」。所以这里只认
    **等值**：父节自己那几片，不多不少。

    排序按 ``chunk_index``：父节正文拼回去必须与文档里的顺序一致（ADR-0010 的确定性）。

    同样**只取当前稿**（#174）：它是 collapsed 关闭分支的第 6 个调用点，交付口径与
    折叠取整节相同——补的父节正文若来自旧稿，读者拿到的是「这一段当初不是这样写的」。
    """

    heading = Chunk.meta["heading_path"].astext
    chunk_index = Chunk.meta["chunk_index"].astext.cast(Integer)
    stmt = _join_documents(select(Chunk.document_id, heading, chunk_index, Chunk.content)).where(
        Chunk.tenant_id == tenant_id,
        Chunk.document_id.in_(sorted({str(item) for item in document_ids})),
        heading.in_(sorted({str(item) for item in heading_paths})),
        current_draft_filter(),
    )
    stmt = stmt.order_by(Chunk.document_id, chunk_index, Chunk.id)
    # 「哪一代」由库按文档行求值（现场九·第四步）：不再按库级生效版本收窄——构建期间新代
    # 已在库里、生效版本还是旧的，两个条件相乘会把刚建好的那篇整个抹掉。
    stmt = stmt.where(current_generation_filter())
    return stmt


async def fetch_parent_rows(
    db: AsyncSession,
    *,
    tenant_id: str,
    document_ids: Sequence[str],
    heading_paths: Sequence[str],
) -> dict[tuple[str, str], list[str]]:
    """``{(文档 id, 父节路径): [父节自己那几片的正文…]}``，按文档顺序。

    只在**当前代 × 当前稿**里取（判据见 :func:`serving_chunk_filters`）：跨代拼父节会让
    重建中途的检索拿到两套切法拼起来的上下文——同一个小节一半是旧切法的片、一半是新的。
    """

    if not document_ids or not heading_paths:
        return {}
    rows = (
        await db.execute(
            build_parent_rows_statement(
                tenant_id=tenant_id,
                document_ids=list(document_ids),
                heading_paths=list(heading_paths),
            )
        )
    ).all()
    parents: dict[tuple[str, str], list[str]] = {}
    for document_id, path, _index, content in rows:
        parents.setdefault((str(document_id), str(path)), []).append(str(content or ""))
    return parents


def _count_by_kb_ids_statement(
    *, tenant_id: str, kb_ids: Sequence[str]
) -> Select[tuple[int]]:
    """空结果判定的计数语句：租户 + 范围 + 当前稿 + **当前代**。

    范围就是召回时那几个库（``kb_id IN (...)``）：口径一旦与召回不同，「库里有内容、本次
    没命中」就会被误判成「空库」，而这正是 ``empty_reason`` 唯一要说清的事。

    「当前稿」同理（#174）：数的是**召回真能看见的行**。少了这一条，两稿并存时计数会
    说「库里有内容」（2 片）而召回一条都没有（当前稿那 0 片），于是
    ``empty_reason`` 把「这一稿没内容」误报成「检索没命中」——两个原因的处置完全不同。
    """

    return _join_documents(select(func.count()).select_from(Chunk)).where(
        Chunk.tenant_id == tenant_id,
        Chunk.kb_id.in_(list(kb_ids)),
        current_draft_filter(),
        # 旧代切片按保留策略留在库里，它们不是「现在能召回的内容」——不算进来的话，
        # 「库里有内容、这次没命中」会被一条早就淘汰的产物撑成假象。
        current_generation_filter(),
    )


async def count_by_kb_ids(
    db: AsyncSession, *, tenant_id: str, kb_ids: Sequence[str]
) -> int:
    """:func:`count_by_kb_ids` 的异步面（检索链路，第 11 步票 #127）。

    口径与召回逐字相同：同样那几个库、同一个强制租户参数、同样只数当前代 × 当前稿。
    """

    if not kb_ids:
        return 0
    stmt = _count_by_kb_ids_statement(tenant_id=tenant_id, kb_ids=kb_ids)
    return int((await db.execute(stmt)).scalar_one())


async def count_by_index_version(
    db: AsyncSession, kb_id: str, *, tenant_id: str, index_version: str
) -> int:
    """该库在**某一个版本**里有多少切片（``ready`` 前的四项计数之一，ADR-0025）。

    与 ``count_by_kb_ids`` 的区别只有一个：这里问的是「某一次构建产出了多少片」，而那
    一条问的是「按生效版本能不能召回」。两者都带租户，也都不许省。
    """

    stmt = (
        select(func.count())
        .select_from(Chunk)
        .where(
            Chunk.tenant_id == tenant_id,
            Chunk.kb_id == kb_id,
            Chunk.index_version == index_version,
        )
    )
    return int((await db.execute(stmt)).scalar_one())


async def list_indexed_document_ids(
    db: AsyncSession,
    kb_id: str,
    *,
    tenant_id: str,
    index_version: str,
    doc_version: int | None = None,
) -> set[str]:
    """**续建的判据**：这一版（可选：这一稿）下已经有切片的文档 id 集合。

    一次读回整批，而不是逐篇问 ``count``：续建要判几百上千篇，逐篇问会让「派活」这一步
    自己变成一次 N+1 扫描（而它本来就只是投递前的一步准备）。

    ``COUNT(DISTINCT ...)`` 那条（:func:`count_documents_in_version`）回答「够不够」，
    这一条回答「哪几篇够了」——两者共用同一份过滤条件，不许各自漂移。
    """

    stmt = (
        select(Chunk.document_id)
        .distinct()
        .where(
            Chunk.kb_id == kb_id,
            Chunk.tenant_id == tenant_id,
            Chunk.index_version == index_version,
        )
    )
    if doc_version is not None:
        stmt = stmt.where(Chunk.doc_version == doc_version)
    rows = (await db.execute(stmt)).scalars().all()
    return {str(row) for row in rows}


async def count_documents_in_version(
    db: AsyncSession,
    kb_id: str,
    *,
    tenant_id: str,
    index_version: str,
    doc_version: int | None = None,
) -> int:
    """**构建收敛的真值**：该库在这一版（可选：这一稿）下已经有切片的**文档数**。

    为什么数文档而不是数切片：「这次构建把该建的篇都建了吗」问的是篇。数切片会把「一篇
    切了很多片」读成「建了很多篇」，一篇成功、其余全丢的构建照样显得够——那正是
    [ADR-0034](../../docs/adr/0034-queue-state-and-build-convergence.md) 决策四要堵的
    判据漂移。所以是 ``COUNT(DISTINCT document_id)``，不是 ``COUNT(*)``。

    ``doc_version`` 为 ``None`` 时**不按稿过滤**（把这一版里出现的全部文档数进来）。这是给
    「没有稿号快照的老行」留的一支：没有快照就没有立场替它挑一稿，而这一版里的切片本来就
    只能是这一轮构建产出的（新版本号的第一次写入）。正常构建都会带上快照。

    与 :func:`count_by_index_version` 的分工：那一条数**片**（四项计数对账要的是片的数量
    与向量 / 关键词一致），这一条数**篇**（收敛判据）。两个口径各说各的事，不许互相冒名。
    """

    stmt = select(func.count(distinct(Chunk.document_id))).where(
        Chunk.tenant_id == tenant_id,
        Chunk.kb_id == kb_id,
        Chunk.index_version == index_version,
    )
    if doc_version is not None:
        stmt = stmt.where(Chunk.doc_version == doc_version)
    return int((await db.execute(stmt)).scalar_one())


async def list_document_ids_in_version(
    db: AsyncSession,
    kb_id: str,
    *,
    tenant_id: str,
    index_version: str,
    doc_version: int | None = None,
) -> set[str]:
    """:func:`count_documents_in_version` 的**名单**版本：这一版里已经有切片的文档 id。

    三个调用方，问的都是「还差哪几篇」：

    * 清扫器的**缺篇重投**（数够了就够了，但它要知道重投哪几篇）；
    * 运维脚本的排查（「这几篇为什么没进来」）；
    * 控制面详情页的**第二根轴**（``console_kb_service._build_state_of``：逐篇判
      「它进没进这一版」）。查询接口 #227 之后这一处也成了生产调用方，
      所以这个函数不再是「只服务运维脚本」。

    取集合而不是列表：调用方要做的是差集。
    """

    stmt = select(Chunk.document_id).where(
        Chunk.tenant_id == tenant_id,
        Chunk.kb_id == kb_id,
        Chunk.index_version == index_version,
    )
    if doc_version is not None:
        stmt = stmt.where(Chunk.doc_version == doc_version)
    stmt = stmt.distinct()
    return {str(value) for value in (await db.execute(stmt)).scalars().all()}


def _draft_counts_statement(document_ids: Sequence[str]) -> Select[Any]:
    """对账用的分组语句：``(文档, 稿号) → 几片``（#179）。

    一条语句同时回答两件事，所以不必扫两遍表：

    * **出现过哪些稿号**（检查 1：它们每一个都得有版本行）；
    * **某一稿有几片**（信息项：指针那一稿若一片都没有，要么重建还没跑完，要么这篇本来
      就没有内容）。

    读的是 ``chunks.doc_version`` **列**（#172 起稿号只有一个来源），不是 JSONB 里那份
    副本——那份已经不可信，而且老行里可能还残留着。
    """

    return (
        select(Chunk.document_id, Chunk.doc_version, func.count())
        .where(Chunk.document_id.in_(tuple(document_ids)))
        .group_by(Chunk.document_id, Chunk.doc_version)
        .order_by(Chunk.document_id, Chunk.doc_version)
    )


async def draft_counts(
    db: AsyncSession, *, document_ids: Sequence[str]
) -> list[tuple[str, int, int]]:
    """这批文档的 ``(文档 id, 稿号, 切片数)``，按 (文档, 稿号) 升序（#179）。

    只有 ``scripts/verify_document_versions.py`` 调用。**空列表不发语句**：一次空扫描
    （``--limit 0`` 或空库）不该产生一条 ``IN ()`` 的查询。

    没有刻意带租户：入参是调用方从 ``documents`` 表读出来的文档 id（UUID，跨租户撞不上），
    而对账要看的正是「整库」的分布；租户过滤在检索路径上不能省，在**对账**这条只读扫描上
    会让另一个租户里的分歧永远看不见。
    """

    if not document_ids:
        return []
    rows = (await db.execute(_draft_counts_statement(document_ids))).all()
    return [
        (str(document_id), int(doc_version), int(count)) for document_id, doc_version, count in rows
    ]


async def list_section_identity_rows(
    db: AsyncSession, *, tenant_id: str, kb_id: str, index_version: str
) -> list[tuple[str, str, str, str | None]]:
    """这个版本里**每个节**的 ``(文档 id, 标题路径, section_id, parent_section_id)``。

    核对脚本用它验「节的身份证」自洽（#143）：同一节的多片共享同一份 meta，所以先
    ``distinct`` 再判——一片一行会让「这条节被判了 20 次」变成报错条数虚高。

    读的是 ``metadata``（JSONB）里的三个键，不是新增的列：节的身份从第 3 步起就随切片
    落在那份 meta 里，核对不该为了它改表（改表要连带改迁移与回填，而这里只需要「读得到」）。
    """

    stmt = (
        select(
            Chunk.document_id,
            Chunk.meta["heading_path"].astext,
            Chunk.meta["section_id"].astext,
            Chunk.meta["parent_section_id"].astext,
        )
        .where(
            Chunk.tenant_id == tenant_id,
            Chunk.kb_id == kb_id,
            Chunk.index_version == index_version,
        )
        .distinct()
    )
    rows = (await db.execute(stmt)).all()
    return [
        (str(document_id), str(path or ""), str(section_id or ""), parent or None)
        for document_id, path, section_id, parent in rows
    ]


async def count_in_kb(db: AsyncSession, kb_id: str, *, tenant_id: str) -> int:
    """一个库在「**当前代 × 当前稿**」下有多少片（卡片上那个合计）。

    为什么不是「先列全部文档 id，再调 :func:`count_by_document_ids` 求和」：那要先把
    5000 个 id 传进 ``IN (...)``，再用应用层求和。这里一条聚合就够，而且与逐文档计数
    **共用同一个谓词**（:func:`serving_chunk_filters`），不会出现「合计」与「每行之和」
    两个数不一致这种最容易被当成巧合的情况。

    还没有任何产物的库自然回 0（每篇的 ``current_index_generation`` 都是 NULL，一条也对不上）。
    """

    statement = (
        _join_documents(select(func.count()).select_from(Chunk))
        .where(
            Chunk.kb_id == kb_id,
            Chunk.tenant_id == tenant_id,
            *serving_chunk_filters(),
        )
    )
    return int((await db.execute(statement)).scalar_one() or 0)


async def count_by_document_ids(
    db: AsyncSession,
    document_ids: list[str],
    *,
    tenant_id: str,
) -> dict[str, int]:
    """一次分组查询拿回 ``{document_id: 当前稿的切片数}``；当前稿没有切片的文档不在字典里。

    文档列表接口（数据面与控制面树）必须用它，不能对每个文档各查一次（N+1）。
    ``tenant_id`` 是强制过滤参数（ADR-0006）：即使文档 id 已被上游按租户筛过，计数也不跨租户。

    **数的是「当前代 × 当前稿」这一格**（现场九·第四步）：

    * ``generation``：由 :func:`current_generation_filter` 交 JOIN 的文档行算
      （``chunks.generation = documents.current_index_generation``）；
    * ``doc_version``：由 :func:`current_draft_filter` 交同一行算。

    两根轴都由数据库求值，调用方**不用传任何范围参数**——这也是改造的一个结果：以前要传
    「该库当前生效版本」，现在「哪一代生效」是每篇文档自己的事，没有库级的答案可传。

    只锁后者（改造前那种写法）会把每篇留存的旧代切片都数一遍——实测一个建过 4 版的库显示
    152 片，而当前代只有 38 片，**每次重建都涨一份且永不回落**。旧代按保留策略留在库里
    （给单篇回滚用），它们不该出现在这个数里。

    **数的是当前稿**（:func:`app.models.chunk.current_draft_filter`），不是各留存稿之和：
    每稿各留一套切片（ADR-0031 决策一）之后，同一篇文档库里可以有两三稿的切片同时存在，
    数全部的读数会随改稿次数一起涨（留 3 稿就约 3 倍）。而这个数字是**对外可见**的——前端
    拿它判断「这份文档现在有没有内容」——所以口径错的那一侧不会报错，只会被当成真的。
    指针那一稿还没切片时（D5 的在途窗口）这份文档不出现，调用方回落 0。
    """

    if not document_ids:
        return {}
    stmt = _count_by_document_ids_statement(document_ids, tenant_id=tenant_id)
    return {document_id: int(count) for document_id, count in (await db.execute(stmt)).all()}


def serving_chunk_filters() -> tuple[Any, ...]:
    """「**当前代 × 当前稿**」那两轴——检索、界面计数、构建真值共用的口径（现场九·第四步）。

    ## 为什么这里不再有 ``index_version``

    改造前「哪一份产物生效」是**库级**的（``index_version = 生效版本``），一条相等条件就够。
    下放到篇级之后，那一条件不再等价于「这篇现在用哪一代」：构建往新版本写时，切片在新版本里、
    而库的生效版本还是旧的——两个条件相乘会把刚建好的那篇从检索里**整个抹掉**（真库实测
    ``count_in_kb("v4") = 0``）。所以这里只看代，**不再看库级版本**。

    代价写在明处：一篇重建完就立刻按新一代服务，不再等整个库切过去。这正是本步的取舍
    ——放弃「全库原子切换」，接受「重建进行到一半」的中间态（ADR-0025 第 2 次修订）。

    前置条件与 :func:`app.models.chunk.current_draft_filter` 相同：语句必须 JOIN 了
    ``documents``（两条都由库求值：``chunks.doc_version = documents.current_version`` 与
    ``chunks.generation = documents.current_index_generation``）。
    """

    return (current_generation_filter(), current_draft_filter())


def _count_by_document_ids_statement(document_ids: list[str], *, tenant_id: str):
    """「每份文档**当前稿**各有几片」的那一条分组计数。

    JOIN 文档表是为了让当前稿指针由库来算（与检索链路上那五处同一个谓词，见
    ``tests/test_document_chunk_count.py``）：应用层先读指针再传进来会把「读指针」与
    「按指针过滤」拆成两个时刻，中间正好是别人翻指针的窗口。
    """

    return (
        _join_documents(select(Chunk.document_id, func.count()))
        .where(
            Chunk.document_id.in_(document_ids),
            Chunk.tenant_id == tenant_id,
            *serving_chunk_filters(),
        )
        .group_by(Chunk.document_id)
    )


def delete_by_document_id(db: Session, document_id: str) -> None:
    db.execute(delete(Chunk).where(Chunk.document_id == document_id))
    db.flush()

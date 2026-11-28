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

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import Integer, Select, and_, delete, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.chunk import Chunk, current_draft_filter, scope_filter
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
    index_version: str | None,
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
    if index_version is not None:
        stmt = stmt.where(Chunk.index_version == index_version)
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
    index_version: str | None = None,
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
        index_version=index_version,
    )
    return _group_unit_rows(list((await db.execute(stmt)).all()))


def build_parent_rows_statement(
    *,
    tenant_id: str,
    document_ids: Sequence[str],
    heading_paths: Sequence[str],
    index_version: str | None = None,
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
    if index_version is not None:
        stmt = stmt.where(Chunk.index_version == index_version)
    return stmt


async def fetch_parent_rows(
    db: AsyncSession,
    *,
    tenant_id: str,
    document_ids: Sequence[str],
    heading_paths: Sequence[str],
    index_version: str | None = None,
) -> dict[tuple[str, str], list[str]]:
    """``{(文档 id, 父节路径): [父节自己那几片的正文…]}``，按文档顺序。

    只在这一版本身份里取（``index_version`` 由调用方按「这份文档所属库」给出）：
    跨版本拼父节会让重建中途的检索拿到两套切法拼起来的上下文。
    """

    if not document_ids or not heading_paths:
        return {}
    rows = (
        await db.execute(
            build_parent_rows_statement(
                tenant_id=tenant_id,
                document_ids=list(document_ids),
                heading_paths=list(heading_paths),
                index_version=index_version,
            )
        )
    ).all()
    parents: dict[tuple[str, str], list[str]] = {}
    for document_id, path, _index, content in rows:
        parents.setdefault((str(document_id), str(path)), []).append(str(content or ""))
    return parents


def _count_by_kb_ids_statement(
    *, tenant_id: str, index_versions: Mapping[str, str]
) -> Select[tuple[int]]:
    """空结果判定的计数语句：租户 + 范围 + 当前稿。

    范围用的是**召回时那一条配对条件**（``app.models.chunk.scope_filter``），不是
    「所有库 × 所有版本」：口径一旦与召回不同，「库里有内容、本次没命中」就会被
    误判成「空库」，而这正是 ``empty_reason`` 唯一要说清的事。

    「当前稿」同理（#174）：数的是**召回真能看见的行**。少了这一条，两稿并存时计数会
    说「库里有内容」（2 片）而召回一条都没有（当前稿那 0 片），于是
    ``empty_reason`` 把「这一稿没内容」误报成「检索没命中」——两个原因的处置完全不同。
    """

    return _join_documents(select(func.count()).select_from(Chunk)).where(
        Chunk.tenant_id == tenant_id,
        scope_filter(index_versions),
        current_draft_filter(),
    )


async def count_by_kb_ids(
    db: AsyncSession, *, tenant_id: str, index_versions: Mapping[str, str]
) -> int:
    """:func:`count_by_kb_ids` 的异步面（检索链路，第 11 步票 #127）。

    口径与召回逐字相同：同一条配对条件、同一个强制租户参数。

    ``index_versions`` 是 ``kb_id → 生效版本`` 的映射（ADR-0025），**不是**一个版本
    常量：多库各自 pin 在不同构建上时，用一个常量的计数会把「别的版本有内容」算成
    「这个库没有内容」。
    """

    if not index_versions:
        return 0
    stmt = _count_by_kb_ids_statement(tenant_id=tenant_id, index_versions=index_versions)
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


async def count_in_kb(
    db: AsyncSession, kb_id: str, *, tenant_id: str, index_version: str | None
) -> int:
    """一个库在「生效版本 × 当前稿」下有多少片（卡片上那个合计）。

    为什么不是「先列全部文档 id，再调 :func:`count_by_document_ids` 求和」：那要先把
    5000 个 id 传进 ``IN (...)``，再用应用层求和。这里一条聚合就够，而且与逐文档计数
    **共用同一个谓词**（:func:`serving_chunk_filters`），不会出现「合计」与「每行之和」
    两个数不一致这种最容易被当成巧合的情况。

    ``index_version`` 为 ``None``（还没有生效版本）时回 0，与逐文档计数同一条约定。
    """

    if index_version is None:
        return 0
    statement = (
        _join_documents(select(func.count()).select_from(Chunk))
        .where(
            Chunk.kb_id == kb_id,
            Chunk.tenant_id == tenant_id,
            *serving_chunk_filters(index_version=index_version),
        )
    )
    return int((await db.execute(statement)).scalar_one() or 0)


async def count_by_document_ids(
    db: AsyncSession,
    document_ids: list[str],
    *,
    tenant_id: str,
    index_version: str | None,
) -> dict[str, int]:
    """一次分组查询拿回 ``{document_id: 当前稿的切片数}``；当前稿没有切片的文档不在字典里。

    文档列表接口（数据面与控制面树）必须用它，不能对每个文档各查一次（N+1）。
    ``tenant_id`` 是强制过滤参数（ADR-0006）：即使文档 id 已被上游按租户筛过，计数也不跨租户。

    **数的是「生效版本 × 当前稿」这一格**，不是留存稿之和、也不是留存版本之和：

    多稿并存（ADR-0031）之后又多了多版本并存（构建出 v5 时 v4 仍留着给回滚），于是
    「这份文档现在有多少片」有**两个**必须一起锁的维度：

    * ``doc_version``：由 :func:`current_draft_filter` 交 JOIN 的文档行算；
    * ``index_version``：由调用方传**该库当前生效版本**（``knowledge_bases.active_index_version``）。

    只锁前者（本函数在 #17x 那版的写法）会让界面把每个留存版本的切片都数一遍——实测一个
    建过 4 版的库显示 152 片，而生效版本只有 38 片，**每次重建都涨一份且永不回落**。而检索
    链路上每一条语句都带 ``Chunk.index_version == <生效版本>``：界面与检索因此说着两个数。

    ``index_version`` 是**必填关键字**（没有默认值）：这个数是对外可见的，让调用方必须
    当场回答「哪一版」比给一个会静默算错的默认值强。没有生效版本（新建库、只写了文件还没
    建索引）时传 ``None``，本函数直接回零——那种库里没有任何可服务的切片。

    **数的是当前稿**（:func:`app.models.chunk.current_draft_filter`），不是各留存稿之和：
    每稿各留一套切片（ADR-0031 决策一）之后，同一篇文档库里可以有两三稿的切片同时存在，
    数全部的读数会随改稿次数一起涨（留 3 稿就约 3 倍）。而这个数字是**对外可见**的——前端
    拿它判断「这份文档现在有没有内容」——所以口径错的那一侧不会报错，只会被当成真的。
    指针那一稿还没切片时（D5 的在途窗口）这份文档不出现，调用方回落 0。
    """

    if not document_ids or index_version is None:
        return {}
    stmt = _count_by_document_ids_statement(
        document_ids, tenant_id=tenant_id, index_version=index_version
    )
    return {document_id: int(count) for document_id, count in (await db.execute(stmt)).all()}


def serving_chunk_filters(*, index_version: str) -> tuple[Any, ...]:
    """「**生效版本 × 当前稿**」那两轴——检索、界面计数、构建真值共用的口径。

    ## 本仓里「现在有多少内容」的两种拼写

    单库一侧用本函数（``index_version == 生效版本`` + 当前稿）；**多库召回**那一侧用
    ``build_scope_filter(kb → 生效版本)`` + 当前稿（:meth:`PgVectorStore.similarity_search_scope`
    一次跨库取数，没法写成单个相等条件）。两者是**同一个谓词**的两种拼写，语义必须逐字一致
    ——`tests/test_console_kb_read_model_sql.py` 里有一条用例把两种拼写在同一批数据上跑一遍、
    要求切出来的行集合相同。

    第三种拼写是「只有稿号」（本函数在 #17x 那版的写法）：界面因此显示 152 片而生效版本只有
    38 片，且每次重建涨一份。还有第四种「只有版本」——那是**构建真值**
    （:func:`count_documents_in_version`），它问的是「这一版那一稿下有没有片」，与「现在能召回
    什么」不是同一个问题，所以它刻意不带当前稿指针（快照稿号由调用方传）。

    所以口径只有这一处：**要么两条都有，要么调用方明确说明它问的不是「现在有多少内容」**。
    前置条件与 :func:`app.models.chunk.current_draft_filter` 相同：语句必须 JOIN 了
    ``documents``。
    """

    return (Chunk.index_version == index_version, current_draft_filter())


def _count_by_document_ids_statement(
    document_ids: list[str], *, tenant_id: str, index_version: str
):
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
            *serving_chunk_filters(index_version=index_version),
        )
        .group_by(Chunk.document_id)
    )


def delete_by_document_id(db: Session, document_id: str) -> None:
    db.execute(delete(Chunk).where(Chunk.document_id == document_id))
    db.flush()

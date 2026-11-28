"""折叠：把命中的小片折成「交付单元」——一条结果代表一节（ADR-0013）。

它是内核的最后一步（``召回 → 融合 → 重排 → TopK → 折叠``），也是**交付口径**本身：
检索仍按小片算分（保住「找得准」），返回前把同一父节的命中折成一条，所以 ``top_k``
的名次不会被同一节的碎片占满。

三件事在这里一次做完，顺序不能换：

1. **回查**：先按 ``(kb_id, chunk_id)`` 取这批命中的 ``meta``（章节路径、类型、表格行号），
   再按章节路径取回整个单元的全部切片。单元 id 由路径算出来（``sha256(前两段)``），
   所以不需要新增列，重建索引前后都稳定。
2. **折叠算法**本身在 ``result_collapse`` 里——**评测调同一个函数**，口径不会再和生产分叉。
3. **补定位字段**（页码 / 稿号 / 切块策略）：引用要能说清「哪一页、哪一稿、怎么切的」。

**折叠被关掉时还有第四件**（#144 / ADR-0027 决策 7）：交付单元此时小于「节」，于是补一次
**父节自己的**正文（``parent_content``）。它只附加内容——不改分数、不改排名、不替换
``content``：小片读不懂的原因是缺上下文，不是缺一条新名次。折叠开着时不补：折叠已经
把整节给了，再补一份父节就是材料量翻倍。

**哪一代的正文由 SQL 按文档行求值，不是调用方传进来的常量**（现场九·第四步）：折叠取
整节、补父节这两处都走 :func:`app.models.chunk.current_generation_filter`。改造前按
「这份文档所属库的生效版本」各查一次——代下放到篇级之后没有库级答案可传，而用错一代
（拿上一版）会把旧切法的正文拼进这一条结果，症状是同一节一半旧一半新。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import chunk_repository
from app.services import result_collapse
from app.utils.section_identity import parent_heading_path


def _meta_of(chunk: dict, metas: dict[tuple[str, str], dict]) -> dict:
    """取这条结果的 meta；唯一键是 ``(kb_id, chunk_id)``（ADR-0010）。

    单独按 chunk_id 取会让跨库同 ID 的两条结果互相覆盖。
    """

    chunk_id = chunk.get("chunk_id")
    kb_id = chunk.get("kb_id")
    if not isinstance(chunk_id, str) or not isinstance(kb_id, str):
        return {}
    return metas.get((kb_id, chunk_id)) or {}


async def collapse_results(
    db: AsyncSession,
    *,
    tenant_id: str,
    chunks: list[dict],
    kb_ids: Sequence[str],
    top_k: int,
    fold_to_sections: bool,
    expand_table_rows: bool,
    max_unit_chars: int = result_collapse.MAX_UNIT_CHARS_DEFAULT,
) -> list[dict]:
    """把命中的小片折成交付单元；关掉折叠时返回小片本身（与折叠之前逐字一致）。"""

    if not chunks:
        return []

    if not fold_to_sections:
        # 不折叠：返回小片，响应与折叠之前逐字一致。留给「只要精确到那一行」的调用方。
        # 交付单元此时**小于「节」**，所以补一次父节内容（ADR-0027 决策 7）：不改分数、
        # 不改排名，只附加内容——小片读不懂的正是「它在上下文的哪一段里」。
        metas = await _fetch_metas(db, tenant_id=tenant_id, chunks=chunks)
        with_locating = await _with_locating_fields(
            db, tenant_id=tenant_id, chunks=chunks, metas=metas
        )
        if metas is None:
            return with_locating
        return await _with_parent_content(
            db,
            tenant_id=tenant_id,
            chunks=with_locating,
            metas=metas,
            max_unit_chars=max_unit_chars,
        )

    kb_ids = sorted({str(chunk["kb_id"]) for chunk in chunks if chunk.get("kb_id")})
    document_ids = sorted(
        {str(chunk["document_id"]) for chunk in chunks if chunk.get("document_id")}
    )
    if not kb_ids or not document_ids:
        return chunks

    metas = await chunk_repository.fetch_meta_by_chunk_ids(
        db,
        tenant_id=tenant_id,
        kb_ids=kb_ids,
        chunk_ids=[str(chunk["chunk_id"]) for chunk in chunks if chunk.get("chunk_id")],
    )

    hits: list[result_collapse.Hit] = []
    for chunk in chunks:
        meta = _meta_of(chunk, metas)
        hits.append(
            result_collapse.Hit(
                chunk_id=str(chunk.get("chunk_id") or ""),
                document_id=str(chunk.get("document_id") or ""),
                score=float(chunk.get("score") or 0.0),
                content=str(chunk.get("content") or ""),
                chunk_type=str(meta.get("chunk_type") or ""),
                section_id=str(meta.get("section_id") or ""),
                parent_section_id=str(meta.get("parent_section_id") or ""),
                table_part=int(meta.get("table_part") or 0),
                # 老索引没有这个键：缺省 0 = 整节，与切段之前完全一致。
                section_part=int(meta.get("section_part") or 0),
                heading_path=str(meta.get("heading_path") or ""),
                kb_id=chunk.get("kb_id"),
            )
        )

    # 一次查出这些单元（h2 级章节**的某一节段**）下的全部切片，含挂在该节自己身上的片。
    # 用路径前缀而不是 parent_section_id：后者是上一级，对 h2 级的切片会指到 h1，
    # 按它归组会把整篇文档并成一个单元（这个坑真踩过）。
    #
    # 键是 (路径, 节段号)：一个超长的 h2 在索引期被切成若干段（第 16 步 #207），
    # 段与段是**不同的节**——只按路径取会把整节的片又拼回来，等于没切。
    unit_keys = sorted(
        {
            (result_collapse.unit_path_of(hit.heading_path), hit.section_part)
            for hit in hits
            if hit.heading_path
        }
    )
    raw_rows: dict[tuple[str, str, int], list] = {}
    if unit_keys:
        # 不再按「库 → 版本」分组（现场九·第四步）：「哪一代生效」由文档行决定，取整节用的
        # 那条 SQL 会 JOIN 文档按代过滤——分组反而会把一篇的旧代也拉进来拼进交付单元。
        raw_rows.update(
            await chunk_repository.fetch_unit_rows(
                db,
                tenant_id=tenant_id,
                document_ids=[str(c["document_id"]) for c in chunks if c.get("document_id")],
                units=unit_keys,
            )
        )

    # 一个单元键 = (种类, 文档, 单元 id[, table_part][, 节段号])。单元 id 由路径算出来
    # （sha256(前两段)），所以不需要新增列，重建索引前后都稳定。
    sections: dict[result_collapse.UnitKey, result_collapse.SectionRows] = {}
    tables: dict[result_collapse.UnitKey, str] = {}
    for (document_id, path, section_part), rows in raw_rows.items():
        unit_id = result_collapse.unit_id_of(path)
        if not unit_id:
            continue
        sections[
            result_collapse.UnitKey(
                result_collapse.UNIT_SECTION, document_id, unit_id, "", section_part
            )
        ] = tuple((index, content, chunk_id) for index, content, chunk_id, _type, _part in rows)
        for _index, content, _chunk_id, chunk_type, table_part in rows:
            if chunk_type == "table":
                tables[
                    result_collapse.UnitKey(
                        result_collapse.UNIT_TABLE, document_id, unit_id, str(table_part)
                    )
                ] = content

    units = result_collapse.collapse(
        hits,
        sections=sections,
        tables=tables if expand_table_rows else None,
        top_k=top_k,
        merge_table_rows=expand_table_rows,
        max_unit_chars=max_unit_chars,
    )

    # 唯一键是 (kb_id, chunk_id)（ADR-0010）：跨库同 ID 的两条结果必须能各自取到
    # 自己那条，否则代表片会拿错库的分数字段，甚至拿到空 dict。
    by_result = {(str(chunk.get("kb_id")), str(chunk.get("chunk_id"))): chunk for chunk in chunks}
    results: list[dict] = []
    for unit in units:
        # 以代表片那条结果为底：向量分 / 融合分 / 名次这些对外字段要原样保留，
        # 只把「身份、正文、分数、溯源」换成单元的。
        row = dict(by_result.get((str(unit.kb_id), unit.chunk_id)) or {})
        row["chunk_id"] = unit.chunk_id
        row["content"] = unit.content
        row["score"] = unit.score
        row["section_id"] = unit.section_id or None
        row["heading_path"] = unit.heading_path or None
        row["source_chunk_ids"] = list(unit.source_chunk_ids)
        row["expansion"] = unit.expansion
        # 第 16 步 #207：这条是同一节里的第几段，以及它被裁过没有。调用方要能分清
        # 「同一节的第 2 段」不是另一节，也要知道片段后面还有内容。
        row["section_part"] = unit.section_part
        row["truncated"] = unit.truncated
        results.append(row)

    # meta 已经在上面查过一次，直接复用：同一次检索不为同一批 ID 查两遍库。
    return await _with_locating_fields(db, tenant_id=tenant_id, chunks=results, metas=metas)


async def _with_locating_fields(
    db: AsyncSession,
    *,
    tenant_id: str,
    chunks: list[dict],
    metas: dict[tuple[str, str], dict] | None = None,
) -> list[dict]:
    """补页码 / 稿号 / 切块策略：引用要能说清「哪一页、哪一稿、怎么切的」。

    ``metas`` 可以由调用方传进来复用——折叠路径上已经查过一次，不该为同一批 ID
    再查一遍库。

    **稿号来自真实列**（#174）：``fetch_meta_by_chunk_ids`` 把 ``chunks.doc_version``
    带进这份 meta（#172 的迁移已删掉 JSONB 里那份副本），所以对外字段名不变、值只有一个
    来源。回查不到这一片时 meta 是空字典，``doc_version`` 落成 ``null``——那是**如实**的
    「这一片已经不在当前稿里了」，而不是静默沿用当前稿的稿号。
    """

    if metas is None:
        fetched = await _fetch_metas(db, tenant_id=tenant_id, chunks=chunks)
        if fetched is None:
            # 没有可用 id 时保持原样：不补三个定位字段（与折叠之前的响应逐字一致）。
            return chunks
        metas = fetched
    for chunk in chunks:
        meta = _meta_of(chunk, metas)
        page = meta.get("page")
        doc_version = meta.get("doc_version")
        strategy = meta.get("split_strategy")
        chunk["page"] = page if isinstance(page, int) else None
        chunk["doc_version"] = doc_version if isinstance(doc_version, int) else None
        chunk["split_strategy"] = strategy if isinstance(strategy, str) else None
    return chunks


async def _fetch_metas(
    db: AsyncSession, *, tenant_id: str, chunks: list[dict]
) -> dict[tuple[str, str], dict] | None:
    """按 ``(kb_id, chunk_id)`` 一次取回 meta；拿不到可用 id 时回 ``None``（不是空字典）。

    回 ``None`` 与回 ``{}`` 是两件事：前者是「这批结果没有可查的身份」，调用方据此
    **不加**任何定位字段（响应与折叠之前逐字一致）；后者是「查了，库里没有这些行」，
    那时定位字段落成 ``null`` 才是如实的。
    """

    ids = [str(chunk["chunk_id"]) for chunk in chunks if chunk.get("chunk_id")]
    kb_ids = sorted({str(chunk["kb_id"]) for chunk in chunks if chunk.get("kb_id")})
    if not ids or not kb_ids:
        return None
    return await chunk_repository.fetch_meta_by_chunk_ids(
        db, tenant_id=tenant_id, kb_ids=kb_ids, chunk_ids=ids
    )


async def _with_parent_content(
    db: AsyncSession,
    *,
    tenant_id: str,
    chunks: list[dict],
    metas: dict[tuple[str, str], dict],
    max_unit_chars: int = result_collapse.MAX_UNIT_CHARS_DEFAULT,
) -> list[dict]:
    """折叠关闭时补**父节自己的**正文（ADR-0027 决策 7）：只附加内容，不改分数与排名。

    启用条件只有一条：交付单元小于「节」——也就是折叠被关掉（本函数只在那个分支被调
    用）。折叠开着时不补：折叠已经把整节给了，再补一份父节就是材料量翻倍。

    三件不许做的事，都在这里守：

    * **不改 ``score``**：父节内容不参与打分，它没有分数可言；
    * **不改顺序**：函数只在结果字典上加一个键，不重排、不进出列表；
    * **不替换 ``content``**：``content`` 永远是切片原文（ADR-0012 决策 3），引用要能
      指回那一片；父节正文单独放 ``parent_content``。

    哪一代的父节由 SQL 按文档行求值（现场九·第四步）：同一篇的旧代切片留在库里，用常量
    去查就会把上一版的父节正文拼进这一条结果。
    """

    wanted_documents: set[str] = set()
    wanted_paths: set[str] = set()
    targets: dict[int, tuple[str, str]] = {}
    for index, chunk in enumerate(chunks):
        meta = _meta_of(chunk, metas) or {}
        path = str(meta.get("heading_path") or "")
        parent_path = parent_heading_path(path)
        document_id = str(chunk.get("document_id") or "")
        if parent_path is None or not document_id:
            continue
        wanted_documents.add(document_id)
        wanted_paths.add(parent_path)
        targets[index] = (document_id, parent_path)
    if not targets:
        return chunks

    parents: dict[tuple[str, str], list[str]] = (
        await chunk_repository.fetch_parent_rows(
            db,
            tenant_id=tenant_id,
            document_ids=sorted(wanted_documents),
            heading_paths=sorted(wanted_paths),
        )
    )

    for index, (document_id, parent_path) in targets.items():
        rows = parents.get((document_id, parent_path))
        if rows:
            text = "\n\n".join(rows)
            # 父节正文同样有上限（第 16 步 #207）：这条分支是「调用方自己关掉了折叠」，
            # 但关掉折叠不等于允许把上万字塞回响应。
            if len(text) > max_unit_chars:
                chunks[index]["parent_content"] = text[:max_unit_chars]
                chunks[index]["parent_content_truncated"] = True
            else:
                chunks[index]["parent_content"] = text
    return chunks

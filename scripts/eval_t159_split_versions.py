"""#159 第二组：**切分点变化**的 v1 / v2——同一批文档、同一批问题，只换一处切分点。

```text
语料   eval/corpus-refs 的 3 篇（引用语料，15 节 / 3 篇）
题集   eval/refs-questions.json 的 8 道（每道都写了 answer_points 与 expected）
v1     当前切法（生产切块器 SECTION_LEVEL=3：h1 / h2 / h3 都成节）
v2     只改一处：h3 不再是节边界（SECTION_LEVEL=2），h3 的正文并入父级 h2
```

## 为什么选「h3 是否成边界」这一处

它同时满足三个条件，而别的候选都不满足：

1. **语料里有这一处**：``points_policy`` 的 2.1 / 2.2 是 h3；这份语料没有表格、没有代码块，
   所以「表格行级片」「small-to-big 粒度」在这批文档上换不出任何交付单元差异——那不是
   一处切分点，那是换个说法。
2. **题集直接考它**：r04 / r08 明确指向「第 2.2 节」（r04 的 ``expected`` 里就是
   ``points_policy:2.2``），C6 的覆盖读数里 r08 也正是缺 ``points_policy:2.2``。
3. **它改的是「什么算一个交付单元」**，那正是本组要看的量：v1 里 2.1 与 2.2 各自一片，
   v2 里它们并成一片大的。

## 版本怎么来（走 #142 的构建流水线，不直接改 chunks 表）

| 版本 | 怎么来 | 切法 |
| --- | --- | --- |
| ``v1`` | 生产入库路径（上传登记 → Worker 那段索引体） | 当前切法 |
| ``v2`` | ``IndexBuildService``（#142 的 start_build → build → activate） | h3 并进父级 |

切块器的注入点：``IndexingService`` 的 ``_resolve_splitter``（生产不注入、按库配置现建；
本脚本注入一个**只改 SECTION_LEVEL 的切块器子类**，并按文档带 ``document_id``——节的
身份证是 ``stable_hash(文档 id : 标题路径)``，少了它两份文档的同名节会撞成一个 id）。
**没有改 app/ 任何一行，也没有直接写 chunks 表**：v2 的切片由同一条构建流水线产出。

## 比什么

* 命中集合（``文档:节``）逐题差异、交付单元差异（v1 消失的单元是「被父节吞了」还是
  「真的没取回」，按正文包含关系判定）；
* ``section_id`` 稳定性（**这次应当有差异**：2.1 / 2.2 的 id 在 v2 里不存在；同一个节
  的 id 跨版本必须相等）；
* CP / CR（ragas ID 版、零 LLM 打分，与 137 题门禁同一把尺子）：**两套标签口径**——
  ``strict`` 与 ``parent_fallback``。2.2 并进 2 之后，strict 标签在 v2 里解析不到，
  这不是「退步」而是「标签粒度随切法变了」；报告里两个口径分开列，混着读会把切法变化
  误读成检索退化。

## 预算

建 v1 / v2 的文档嵌入 + 两版各 8 次查询嵌入，合计 ≈ 50 次模型调用（无 LLM 调用）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # 以 ``python scripts/...`` 方式运行时补上仓库根
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

REPORT_DIR = REPO_ROOT / "eval" / "reports" / "t159"
REFS_CORPUS_DIR = REPO_ROOT / "eval" / "corpus-refs"
QUESTIONS_PATH = REPO_ROOT / "eval" / "refs-questions.json"

#: 本组专用的关键词别名（本机 ``rag_chunks`` 还是物理索引，别名与同名物理索引不能并存；
#: 评测语料本来就该与线上索引隔离——与 C6 / 137 题那两套同一个做法）。
KEYWORD_ALIAS = "rag_chunks_t159"
TENANT = "tenant_c6"
KB_NAME = "切分点对照·引用语料"
KB_DESCRIPTION = "v1/v2 切分点对照：同一批引用语料，v2 把 h3 并入父级 h2"

#: 版本名（与 #142 的语义一致：v1 = 建库声明的生效版本，v2 = 第一次整库构建）。
V1 = "v1"
V2 = "v2"

#: 生产切法 = h1/h2/h3 都成节（``MarkdownStructuredSplitter.SECTION_LEVEL``）。
SECTION_LEVEL_V1 = 3
#: v2：h3 不再是节边界——**唯一**被改动的切分点。
SECTION_LEVEL_V2 = 2

_WS_RE = re.compile(r"\s+")


# ─────────────────────────────────────────────────────────── 切块器（一处差异）


def build_splitter_for_version(
    strategy: str,
    *,
    chunk_size: int,
    chunk_overlap: int | None = None,
    table_max_rows: int | None = None,
    document_id: str = "",
    section_level: int = SECTION_LEVEL_V1,
) -> Any:
    """按版本构造切块器：``3`` 走生产实现（逐字不变），``2`` 只把 h3 降级成深层标题。

    除 ``SECTION_LEVEL`` 外，两种构造的窗口参数取自同一份配置（重叠加 / 表格行上限都
    跟着 ``settings``，不另写一套）——「只改一处」这句话因此是可核对的。
    """

    from app.core.config import settings
    from app.utils.markdown_splitter import MarkdownStructuredSplitter
    from app.utils.splitter_factory import build_splitter

    overlap = settings.CHUNK_OVERLAP if chunk_overlap is None else chunk_overlap
    max_rows = settings.TABLE_MAX_ROWS_PER_CHUNK if table_max_rows is None else table_max_rows
    if section_level == SECTION_LEVEL_V1:
        # 生产实现原样调用：v1 这一列必须与生产路径逐字一致，否则对照的前提就没了。
        return build_splitter(
            strategy,
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            table_max_rows=max_rows,
            document_id=document_id,
        )
    if section_level != SECTION_LEVEL_V2:
        raise ValueError(f"unsupported section level: {section_level}")
    if strategy != "structure":
        # 只改 h3 边界这件事对固定窗口切法不成立（它根本不认标题）。
        raise ValueError("section level 只对 structure 切法有意义")

    class _H3MergedSplitter(MarkdownStructuredSplitter):
        """生产切块器的子类，**只**把 SECTION_LEVEL 改成 2（h3 不再 flush）。"""

        SECTION_LEVEL = SECTION_LEVEL_V2

    return _H3MergedSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        table_max_rows=max_rows,
        document_id=document_id,
    )


# ─────────────────────────────────────────────────────────── 清单与差异（纯函数）


@dataclass(frozen=True)
class ChunkRow:
    """一个版本的切片清单里的一行（只装比较要用的字段）。"""

    chunk_id: str
    document: str
    heading_path: str
    section: str | None
    section_id: str
    chunk_type: str
    content: str
    index_version: str


def inventory(rows: Sequence[ChunkRow]) -> dict[str, Any]:
    """一个版本的清单：切片数、节数、每个 (文档, 节) 的 ``section_id``。"""

    by_document: dict[str, dict[str, Any]] = {}
    section_ids: dict[str, str] = {}
    unlabeled = 0
    for row in rows:
        docs = by_document.setdefault(row.document, {"chunks": 0, "sections": []})
        docs["chunks"] += 1
        if row.section is None:
            unlabeled += 1
            continue
        key = f"{row.document}:{row.section}"
        section_ids[key] = row.section_id
        if row.section not in docs["sections"]:
            docs["sections"].append(row.section)
    for docs in by_document.values():
        docs["sections"] = sorted(docs["sections"])
    return {
        "chunks": len(rows),
        "documents": len(by_document),
        "sections": len(section_ids),
        "unlabeled_chunks": unlabeled,
        "chars": sum(len(row.content) for row in rows),
        "by_document": by_document,
        "section_ids": dict(sorted(section_ids.items())),
    }


def section_identity_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """节身份差异：v2 应当出现「消失的节」，而同一个节的 id 不许变。

    ``changed`` 非空意味着 ``stable_hash(文档 id : 标题路径)`` 不再稳定——那**不是**切法
    变化的结果，是身份算法坏了，必须在报告里单独点名。
    """

    ids_before = dict(before["section_ids"])
    ids_after = dict(after["section_ids"])
    before_keys = set(ids_before)
    after_keys = set(ids_after)
    common = before_keys & after_keys
    return {
        "only_v1": sorted(before_keys - after_keys),
        "only_v2": sorted(after_keys - before_keys),
        "same": len([key for key in common if ids_before[key] == ids_after[key]]),
        "changed": sorted(key for key in common if ids_before[key] != ids_after[key]),
    }


def hit_keys(outcome: Any) -> list[str]:
    """一次检索的命中集合，按返回顺序写成 ``文档:节``（节号取 heading_path 尾段）。"""

    keys: list[str] = []
    for hit in outcome.hits:
        if hit.section is None:
            keys.append(f"{hit.document}:?")
        else:
            keys.append(f"{hit.document}:{hit.section}")
    return keys


def hit_diff(before: Any, after: Any) -> dict[str, Any]:
    """两版命中集合的差异（按 ``文档:节`` 比，不按 ``chunk_id``——重建后 id 会换代）。"""

    before_keys = set(hit_keys(before))
    after_keys = set(hit_keys(after))
    return {
        "removed": sorted(before_keys - after_keys),
        "added": sorted(after_keys - before_keys),
        "same": sorted(before_keys & after_keys),
    }


def coverage_of(hits: Sequence[str], expected: Sequence[str]) -> dict[str, Any]:
    """期望节覆盖（按 ``文档:节`` 对账）：命中里有没有那些节。

    节号是**这一版**的节号：合并之后期望的 2.2 在 v2 里不再是一个节，命中里只有 2 ——
    strict 覆盖因此把它判成「没取回」。这不是检索退化，是标签粒度跟着切法变了；
    材料还在不在要另用 ``absorbed_units``（按正文包含）判。
    """

    hit_set = set(hits)
    expected_set = set(expected)
    return {
        "found": sorted(hit_set & expected_set),
        "found_count": len(hit_set & expected_set),
        "expected_count": len(expected_set),
        "missing": sorted(expected_set - hit_set),
    }


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def absorbed_units(before: Any, after: Any) -> dict[str, Any]:
    """v1 有、v2 没有的那些单元，按**正文包含关系**分成两档：

    * ``absorbed``：v1 那一片的正文能在 v2 的某一片里逐字找到（被父节吞了，材料还在）；
    * ``lost``：找不到——这才是「真的少了一条材料」。

    为什么按正文判、不按节号推断：节号消失只能说明「不再是独立单元」，不能说明
    「内容还在不在」。把这两件事合成一件，就会把一次合并读成一次退化。
    """

    lost_keys = [key for key in hit_keys(before) if key not in set(hit_keys(after))]
    after_contents = [_normalize(hit.content) for hit in after.hits]

    absorbed: list[str] = []
    lost: list[str] = []
    for key in lost_keys:
        unit = next(
            (
                hit
                for hit in before.hits
                if (f"{hit.document}:{hit.section}" if hit.section else f"{hit.document}:?") == key
            ),
            None,
        )
        body = _normalize(unit.content) if unit is not None else ""
        if body and any(body in content for content in after_contents):
            absorbed.append(key)
        else:
            lost.append(key)
    return {"absorbed": sorted(absorbed), "lost": sorted(lost)}


def reference_labels(
    question: Mapping[str, Any],
    rows: Sequence[ChunkRow],
    *,
    allow_parent_fallback: bool = False,
) -> dict[str, Any]:
    """把题目的 ``expected``（文档 + 节号）解析成这一版的切片 id。

    **两套口径**，分开输出、绝不合并：

    * 严格（缺省）：只认节号相同的切片。切法一变，被并入父级的节号就解析不到——那正是
      本组要看的差异（不是检索退步，是标签粒度跟着切法变了）；
    * 父级回退（``allow_parent_fallback=True``）：解析不到时退到「节号是它前缀」的父级切片，
      用来回答另一个问题——「那段材料还在不在库里」。回退过的条目单独列在 ``fallback``。
    """

    by_key: dict[tuple[str, str], list[str]] = {}
    parents: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        if row.section is None:
            continue
        by_key.setdefault((row.document, row.section), []).append(row.chunk_id)
        parents.setdefault((row.document, row.section), []).append(row.chunk_id)

    reference: list[str] = []
    unresolved: list[str] = []
    fallback: list[str] = []
    for item in question.get("expected", []):
        document = str(item["document"])
        section = str(item["section"])
        matched = by_key.get((document, section), [])
        if matched:
            reference.extend(matched)
            continue
        if allow_parent_fallback:
            # 父级：节号是期望节号的前缀（``2`` 之于 ``2.2``）。只向上退一层**已编号**的父。
            parent_hits: list[str] = []
            for (doc, candidate_section), chunk_ids in parents.items():
                if doc != document:
                    continue
                if section.startswith(f"{candidate_section}."):
                    parent_hits.extend(chunk_ids)
            if parent_hits:
                reference.extend(parent_hits)
                fallback.append(f"{document}:{section}")
                continue
        unresolved.append(f"{document}:{section}")
    # 去重但保持行序（同一片被两个期望节同时命中时只算一次）。
    unique: list[str] = []
    for chunk_id in reference:
        if chunk_id not in unique:
            unique.append(chunk_id)
    return {"reference": unique, "unresolved": unresolved, "fallback": fallback}


def predicted_calls(*, chunks_v1: int, chunks_v2: int, questions: int) -> dict[str, Any]:
    """开跑前的预估：两次建的文档嵌入 + 两版各一遍查询嵌入（零 LLM）。"""

    return {
        "document_embeddings": chunks_v1 + chunks_v2,
        "query_embeddings": questions * 2,
        "total": chunks_v1 + chunks_v2 + questions * 2,
        "note": "构建时按**切片条数**计文档嵌入；查询侧每题一次（重排关着）。",
    }


# ─────────────────────────────────────────────────────────── 真实跑批


async def _plan_spec(db: Any) -> Any:
    """租户套餐（建库要它做配额校验）：读库里那一行，不在用例里编一个。"""

    from sqlalchemy import select

    from app.models.plan import Plan, plan_spec_of

    row = (await db.execute(select(Plan).where(Plan.code == "pro"))).scalar_one_or_none()
    if row is None:
        raise SystemExit("plans 表里没有 pro 档：先跑 `uv run python scripts/seed_plans.py`")
    return plan_spec_of(row)


async def _ensure_kb(db: Any) -> dict[str, Any]:
    """按 (租户, 库名) 建库或复用；返回 id / 切法 / 当前生效版本。"""

    from sqlalchemy import select

    from app.models.knowledge_base import KnowledgeBase
    from app.services.knowledge_base_service import create_kb

    existing = (
        await db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == TENANT, KnowledgeBase.name == KB_NAME
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {
            "kb_id": existing.id,
            "created": False,
            "split_strategy": existing.split_strategy,
            "active_index_version": existing.active_index_version,
            "chunk_size": (existing.settings or {}).get("chunk_size"),
        }
    plan = await _plan_spec(db)
    created = await create_kb(
        db,
        name=KB_NAME,
        description=KB_DESCRIPTION,
        tenant_id=TENANT,
        split_strategy="structure",
        plan=plan,
    )
    await db.commit()
    return {"kb_id": created["kb_id"], "created": True, "chunk_size": created["chunk_size"]}


async def _ingest(db: Any, *, kb_id: str) -> dict[str, Any]:
    """走生产入库路径把 3 篇语料写成 ``v1``（上传登记 → 索引体 → 切片 + 边）。"""

    from scripts import prep_eval_c6

    plan = await _plan_spec(db)
    readings = await prep_eval_c6.ensure_documents(db, tenant_id=TENANT, kb_id=kb_id, plan=plan)
    return {"readings": readings}


async def _activate_keyword_version(version: str) -> dict[str, Any]:
    """让关键词别名指向这一版的物理索引（v1 是「入库写出来的那一版」，没人替它激活）。"""

    from app.providers.keyword_search import get_keyword_search_provider

    provider = get_keyword_search_provider()
    if not provider.available:
        raise SystemExit("关键词能力不可用（ES 没起？）：没有它混合检索只剩一条腿")
    await provider.activate_version(version)
    return {"alias": KEYWORD_ALIAS, "version": version}


async def _build_v2(db: Any, *, kb_id: str) -> dict[str, Any]:
    """用 #142 的构建流水线把 v2 建出来（切块器注入 ``SECTION_LEVEL=2`` 的那一版）。"""

    from app.services.index_build_service import IndexBuildService
    from app.services.indexing_service import IndexingService

    class _H3MergedIndexing(IndexingService):
        """生产索引服务 + **只改一处**切块器：按文档现建，带 ``document_id``。"""

        async def _resolve_splitter(self, db: Any, doc: Any) -> Any:
            from app.repositories import knowledge_base_repository

            strategy, chunk_size = await knowledge_base_repository.get_split_config(
                db, doc.kb_id, tenant_id=doc.tenant_id
            )
            splitter = build_splitter_for_version(
                strategy,
                chunk_size=chunk_size,
                document_id=str(doc.id),
                section_level=SECTION_LEVEL_V2,
            )
            return splitter, strategy

    service = IndexBuildService(indexing=_H3MergedIndexing())
    row = await service.start_build(db, kb_id=kb_id, tenant_id=TENANT, version=V2)
    await service.build(db, row.id, tenant_id=TENANT)
    await service.activate(db, row.id, tenant_id=TENANT)
    return {
        "build_id": row.id,
        "version": row.version,
        "document_count": row.document_count,
        "chunk_count": row.chunk_count,
    }


async def _switch_to(db: Any, *, kb_id: str, version: str) -> dict[str, Any]:
    """把库切到 ``version``（重跑用）：active 不动，archived 走回滚，ready 走激活。"""

    from app.repositories import index_version_repository
    from app.services.index_build_service import IndexBuildService

    from app.models.knowledge_base import KnowledgeBase

    rows = await index_version_repository.list_for_kb(db, kb_id, tenant_id=TENANT)
    active = next((row for row in rows if row.status == "active"), None)
    if active is not None and active.version == version:
        return {"switched": False, "version": version}
    target = next((row for row in rows if row.version == version), None)
    if target is None:
        # 新库只写了「生效版本」那一列、还没有版本行（#141 的落法）：此时生效版本就是它，
        # 不必切，也没有行可切（start_build 会把它收养成一行 active）。
        kb_row = await db.get(KnowledgeBase, kb_id)
        if kb_row is not None and str(kb_row.active_index_version or "") == version:
            return {"switched": False, "version": version, "note": "no version row yet"}
        raise SystemExit(f"版本不存在：{version}")
    service = IndexBuildService()
    if target.status == "ready":
        await service.activate(db, target.id, tenant_id=TENANT)
    else:
        await service.rollback(db, target.id, tenant_id=TENANT)
    return {"switched": True, "version": version, "from": active.version if active else None}


async def _collect_chunks(db: Any, *, kb_id: str) -> dict[str, list[ChunkRow]]:
    """把两个版本的切片清单读回来（``section_id`` / ``heading_path`` / 正文都在 meta 里）。"""

    from sqlalchemy import select

    from app.models.chunk import Chunk
    from scripts import eval_refs

    document_names = await eval_refs._document_names(db, [kb_id])
    rows = (
        await db.execute(
            select(Chunk).where(
                Chunk.kb_id == kb_id,
                Chunk.index_version.in_([V1, V2]),
            )
        )
    ).scalars()
    by_version: dict[str, list[ChunkRow]] = {V1: [], V2: []}
    for row in rows.all():
        meta = row.meta or {}
        version = str(row.index_version)
        by_version.setdefault(version, []).append(
            ChunkRow(
                chunk_id=row.id,
                document=document_names.get(str(row.document_id), str(row.document_id)),
                heading_path=str(meta.get("heading_path") or ""),
                section=eval_refs.section_of_heading_path(meta.get("heading_path")),
                section_id=str(meta.get("section_id") or ""),
                chunk_type=str(meta.get("chunk_type") or ""),
                content=str(row.content or ""),
                index_version=version,
            )
        )
    return by_version


async def _run_questions(*, kb_id: str, questions: Sequence[Mapping[str, Any]]) -> list[Any]:
    """跑一批题（同一份题集、同一组参数；重排关、编排关——本组只动切法）。"""

    from scripts import eval_refs

    return await eval_refs.run_live(
        questions=questions,
        kb_ids=[kb_id],
        top_k=5,
        tenant_id=TENANT,
        graph="off",
        research="off",
        rerank=eval_refs.RERANK_OFF,
    )


async def _run(*, out_dir: Path | None) -> int:
    """整套流程：建库 → v1 入库 → 跑 v1 → 建 v2 → 跑 v2 → 出对照产物。"""

    from app.core.config import settings
    from app.db.async_session import AsyncSessionLocal
    from scripts import eval_refs, eval_gate

    settings.ELASTICSEARCH_INDEX = KEYWORD_ALIAS
    settings.RERANK_ENABLED = False
    settings.QUERY_REWRITE_ENABLED = False
    settings.SYNONYM_EXPANSION_ENABLED = False

    payload_questions = eval_refs.load_questions()
    questions = [
        {"id": item["id"], "query": item["query"], "mechanism": item["mechanism"]}
        for item in payload_questions["questions"]
    ]

    async with AsyncSessionLocal() as db:
        kb = await _ensure_kb(db)
        kb_id = str(kb["kb_id"])
        ingest = await _ingest(db, kb_id=kb_id)
        keyword_v1 = await _activate_keyword_version(V1)
        await _switch_to(db, kb_id=kb_id, version=V1)
        v1_outcomes = await _run_questions(kb_id=kb_id, questions=questions)

        build = await _build_v2(db, kb_id=kb_id)
        keyword_v2 = {"alias": KEYWORD_ALIAS, "version": V2}
        await _switch_to(db, kb_id=kb_id, version=V2)
        v2_outcomes = await _run_questions(kb_id=kb_id, questions=questions)

        chunks = await _collect_chunks(db, kb_id=kb_id)

    inv_v1 = inventory(chunks.get(V1, []))
    inv_v2 = inventory(chunks.get(V2, []))
    identity = section_identity_diff(inv_v1, inv_v2)

    rows: list[dict[str, Any]] = []
    for source, outcome_v1 in zip(payload_questions["questions"], v1_outcomes):
        outcome_v2 = next((item for item in v2_outcomes if item.id == outcome_v1.id), None)
        if outcome_v2 is None:
            continue
        labels_v1 = reference_labels(source, chunks.get(V1, []))
        labels_v2 = reference_labels(source, chunks.get(V2, []))
        fallback_v1 = reference_labels(source, chunks.get(V1, []), allow_parent_fallback=True)
        fallback_v2 = reference_labels(source, chunks.get(V2, []), allow_parent_fallback=True)

        async def _score(outcome: Any, labels: Mapping[str, Any]) -> tuple[Any, Any]:
            returned = [hit.chunk_id for hit in outcome.hits if hit.chunk_id]
            return await eval_gate.context_scores(returned, labels["reference"])

        strict_v1 = await _score(outcome_v1, labels_v1)
        strict_v2 = await _score(outcome_v2, labels_v2)
        relax_v1 = await _score(outcome_v1, fallback_v1)
        relax_v2 = await _score(outcome_v2, fallback_v2)

        expected_keys = [
            f"{item['document']}:{item['section']}" for item in source.get("expected", [])
        ]
        # 覆盖**逐版**算：把两个 outcome 一起喂给 ``coverage_by_question`` 时，同一个题号
        # 只留最后一条——v1 的覆盖会被 v2 静默顶掉（真机上第一次就是这么错的）。
        rows.append(
            {
                "id": source["id"],
                "query": source["query"],
                "mechanism": source.get("mechanism"),
                "answer_points": source.get("answer_points", []),
                "v1_hits": hit_keys(outcome_v1),
                "v2_hits": hit_keys(outcome_v2),
                "hit_diff": hit_diff(outcome_v1, outcome_v2),
                "absorbed": absorbed_units(outcome_v1, outcome_v2),
                "coverage": {
                    "v1": coverage_of(hit_keys(outcome_v1), expected_keys),
                    "v2": coverage_of(hit_keys(outcome_v2), expected_keys),
                },
                "expected": expected_keys,
                "labels": {
                    "v1": {**labels_v1, "reference_count": len(labels_v1["reference"])},
                    "v2": {**labels_v2, "reference_count": len(labels_v2["reference"])},
                    "v1_parent_fallback": {
                        **fallback_v1,
                        "reference_count": len(fallback_v1["reference"]),
                    },
                    "v2_parent_fallback": {
                        **fallback_v2,
                        "reference_count": len(fallback_v2["reference"]),
                    },
                    "strict_comparable": not labels_v1["unresolved"]
                    and not labels_v2["unresolved"],
                },
                "cp_cr": {
                    "strict_v1": {
                        "context_precision": strict_v1[0],
                        "context_recall": strict_v1[1],
                    },
                    "strict_v2": {
                        "context_precision": strict_v2[0],
                        "context_recall": strict_v2[1],
                    },
                    "parent_fallback_v1": {
                        "context_precision": relax_v1[0],
                        "context_recall": relax_v1[1],
                    },
                    "parent_fallback_v2": {
                        "context_precision": relax_v2[0],
                        "context_recall": relax_v2[1],
                    },
                },
                "v1_heading_paths": [hit.heading_path for hit in outcome_v1.hits],
                "v2_heading_paths": [hit.heading_path for hit in outcome_v2.hits],
            }
        )

    account = {
        "document_embeddings_v1": inv_v1["chunks"],
        "document_embeddings_v2": inv_v2["chunks"],
        "query_embeddings": len(questions) * 2,
        "total": inv_v1["chunks"] + inv_v2["chunks"] + len(questions) * 2,
    }
    payload = {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version_construction": {
            "v1": {
                "path": "生产入库（upload → IndexingService）",
                "section_level": SECTION_LEVEL_V1,
            },
            "v2": {"path": "IndexBuildService（#142）", "section_level": SECTION_LEVEL_V2},
            "build": build,
        },
        "corpus": {
            "dir": "eval/corpus-refs",
            "documents": 3,
            "questions": len(questions),
        },
        "switches": {
            "mode": "hybrid",
            "top_k": 5,
            "fold_to_sections": True,
            "expand_table_rows": True,
            "rewrite": False,
            "rerank": "off",
            "orchestration": "off",
        },
        "keyword_alias": [keyword_v1, keyword_v2],
        "kb": {**kb, "ingest": ingest},
        "inventory": {"v1": inv_v1, "v2": inv_v2},
        "section_identity": identity,
        "rows": rows,
        "aggregate": _aggregate(rows, identity=identity),
        "accounted_calls": account,
        "premise": {
            "comparable_with_original_project": False,
            "why": (
                "本仓库的语料与题集都是自己的，原项目那组效果数字（+4.9% / +7.5% / −82.5% / "
                "+3.4pp）既不成立也不可比（#138 Out of Scope）。本轮数字只与本轮产物可比。"
            ),
        },
    }
    root = out_dir or (REPORT_DIR / f"run-split-{payload['run_id']}")
    root.mkdir(parents=True, exist_ok=True)
    target = root / "results.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"产物：{target}")
    print(
        f"  section_id：同 {identity['same']} / 消失 {len(identity['only_v1'])} / "
        f"新增 {len(identity['only_v2'])} / 变了 {len(identity['changed'])}"
    )
    print(
        f"  切片：v1 {inv_v1['chunks']} / v2 {inv_v2['chunks']}；节：v1 {inv_v1['sections']} / v2 {inv_v2['sections']}"
    )
    print(f"  记账调用数：{account['total']}")
    return 0


def _aggregate(rows: Sequence[Mapping[str, Any]], *, identity: Mapping[str, Any]) -> dict[str, Any]:
    """总账：命中变化题数、吸收 / 丢失单元、CP/CR 两种口径的均值、标签可解析性。"""

    def _mean(values: Sequence[float | None]) -> float | None:
        present = [value for value in values if value is not None]
        return (sum(present) / len(present)) if present else None

    changed = [row for row in rows if row["hit_diff"]["removed"] or row["hit_diff"]["added"]]
    strict_comparable = [row for row in rows if row["labels"]["strict_comparable"]]
    return {
        "questions": len(rows),
        "questions_with_hit_change": len(changed),
        "hit_change_ids": [row["id"] for row in changed],
        "absorbed_units": sum(len(row["absorbed"]["absorbed"]) for row in rows),
        "lost_units": sum(len(row["absorbed"]["lost"]) for row in rows),
        "coverage": {
            "v1_found": sum(row["coverage"]["v1"]["found_count"] for row in rows),
            "v2_found": sum(row["coverage"]["v2"]["found_count"] for row in rows),
            "expected_total": sum(row["coverage"]["v1"]["expected_count"] for row in rows),
            "v1_missing": sorted(key for row in rows for key in row["coverage"]["v1"]["missing"]),
            "v2_missing": sorted(key for row in rows for key in row["coverage"]["v2"]["missing"]),
        },
        "strict_label_unresolved": {
            "v1": sum(len(row["labels"]["v1"]["unresolved"]) for row in rows),
            "v2": sum(len(row["labels"]["v2"]["unresolved"]) for row in rows),
        },
        "strict_comparable_questions": len(strict_comparable),
        "cp_cr": {
            "strict_v1": {
                "context_precision": _mean(
                    [row["cp_cr"]["strict_v1"]["context_precision"] for row in strict_comparable]
                ),
                "context_recall": _mean(
                    [row["cp_cr"]["strict_v1"]["context_recall"] for row in strict_comparable]
                ),
            },
            "strict_v2": {
                "context_precision": _mean(
                    [row["cp_cr"]["strict_v2"]["context_precision"] for row in strict_comparable]
                ),
                "context_recall": _mean(
                    [row["cp_cr"]["strict_v2"]["context_recall"] for row in strict_comparable]
                ),
            },
            "parent_fallback_v1": {
                "context_precision": _mean(
                    [row["cp_cr"]["parent_fallback_v1"]["context_precision"] for row in rows]
                ),
                "context_recall": _mean(
                    [row["cp_cr"]["parent_fallback_v1"]["context_recall"] for row in rows]
                ),
            },
            "parent_fallback_v2": {
                "context_precision": _mean(
                    [row["cp_cr"]["parent_fallback_v2"]["context_precision"] for row in rows]
                ),
                "context_recall": _mean(
                    [row["cp_cr"]["parent_fallback_v2"]["context_recall"] for row in rows]
                ),
            },
        },
        "section_identity": dict(identity),
    }


def _planned_counts() -> tuple[int, int]:
    """离线扫描语料，预估两版的切片数（用于开跑前的预算打印）。"""

    from scripts import eval_refs

    layout = eval_refs.load_manifest()
    v1_pieces = 0
    v2_pieces = 0
    for meta in layout["documents"].values():
        text = (REFS_CORPUS_DIR / meta["file"]).read_text(encoding="utf-8")
        for version, section_level in ((V1, SECTION_LEVEL_V1), (V2, SECTION_LEVEL_V2)):
            pieces = build_splitter_for_version(
                "structure", chunk_size=800, document_id="probe", section_level=section_level
            ).split(text)
            if version == V1:
                v1_pieces += len(pieces)
            else:
                v2_pieces += len(pieces)
    return v1_pieces, v2_pieces


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "T159 group 2: build v1 (current chunking) and v2 (h3 merged into its parent) "
            "from the same documents through the #142 pipeline, then compare hits, "
            "delivery units, section_id stability and CP/CR."
        )
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划与预估，不连库")
    parser.add_argument("--out-dir", default=None, help="产物目录（缺省 eval/reports/t159/run-*）")
    args = parser.parse_args(argv)

    questions = len(json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))["questions"])
    v1_chunks, v2_chunks = _planned_counts()
    predicted = predicted_calls(chunks_v1=v1_chunks, chunks_v2=v2_chunks, questions=questions)
    print(
        f"预计模型调用 {predicted['total']} 次（文档嵌入 v1 {v1_chunks} + v2 {v2_chunks}；"
        f"查询嵌入 {questions}×2；零 LLM）"
    )
    print(
        f"计划：建库 {KB_NAME}（{TENANT}）→ v1 生产入库 → 跑 v1 → "
        f"#142 构建 v2（h3 并入父级）→ 跑 v2"
    )
    if args.dry_run:
        return 0

    from app.core.logging import setup_logging

    setup_logging()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(_run(out_dir=Path(args.out_dir) if args.out_dir else None))


if __name__ == "__main__":
    raise SystemExit(main())

"""#159 第二组：**切分点变化**的 v1 / v2——同一批文档、同一批问题，只换一处切分点。

```text
语料   eval/corpus-refs 的 3 篇（引用语料，15 节 / 3 篇）
题集   eval/refs-questions.json 的 8 道（每道都写了 answer_points 与 expected）
v1     当前切法（生产切块器 SECTION_LEVEL=3：h1 / h2 / h3 都成节）
v2     只改一处：h3 不再是节边界（SECTION_LEVEL=2），h3 的正文并入父级 h2
```

## 这一组现在是什么

它是**两组切法的离线对照**：一份清单（切片 / 节 / 节身份证）对另一份，算出命中差异、
交付单元「被父节吞了」还是「真的丢了」、``section_id`` 是否稳定。所有比较都是纯函数，
不连库、不烧模型。

## 为什么原来那条真实跑批路径删了

原实现造 v1 / v2 的方式是让两版**同时活在两个物理索引**里（``IndexBuildService`` 的
start_build → build → activate + 按 ``chunks.index_version`` 分组读回）。库级索引版本层
在第 17 步整层删除：现在一个库只有一个物理索引、每篇只有一份产物（按篇级代对齐），
「两版并存、切别名后跑两趟」这件事在结构上不存在了。留着它只会让人照着写一段跑不起来的
脚本，所以真实跑批路径（建库 / 入库 / 建 v2 / 切版本 / 读两版切片）整段删除。

要重新做这组对照，正确的做法是**两轮独立入库**（各用一个库、各记一份清单）再把清单交给
本文件的纯函数——那不需要库级版本，也不需要在这里写死。

## 比什么

* 命中集合（``文档:节``）逐题差异、交付单元差异（v1 消失的单元是「被父节吞了」还是
  「真的没取回」，按正文包含关系判定）；
* ``section_id`` 稳定性（**这次应当有差异**：2.1 / 2.2 的 id 在 v2 里不存在；同一个节
  的 id 跨版本必须相等）；
* CP / CR（ragas ID 版、零 LLM 打分）：**两套标签口径**——``strict`` 与
  ``parent_fallback``。2.2 并进 2 之后，strict 标签在 v2 里解析不到，这不是「退步」而是
  「标签粒度随切法变了」；报告里两个口径分开列，混着读会把切法变化误读成检索退化。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # 以 ``python scripts/...`` 方式运行时补上仓库根
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

REFS_CORPUS_DIR = REPO_ROOT / "eval" / "corpus-refs"
QUESTIONS_PATH = REPO_ROOT / "eval" / "refs-questions.json"

#: 对照标签（只是「第几组切法」的编号，与库级索引版本无关）。
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
    """一份清单里的一行（只装比较要用的字段）。"""

    chunk_id: str
    document: str
    heading_path: str
    section: str | None
    section_id: str
    chunk_type: str
    content: str


def inventory(rows: Sequence[ChunkRow]) -> dict[str, Any]:
    """一份清单：切片数、节数、每个 (文档, 节) 的 ``section_id``。"""

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
    """做这组对照要花的调用：两次建的文档嵌入 + 两版各一遍查询嵌入（零 LLM）。

    仍然保留这个纯函数，是因为「做一次这个对照要花多少」本身是要回答的问题——只是现在
    两次建分在两轮独立入库里做。
    """

    return {
        "document_embeddings": chunks_v1 + chunks_v2,
        "query_embeddings": questions * 2,
        "total": chunks_v1 + chunks_v2 + questions * 2,
        "note": "入库时按**切片条数**计文档嵌入；查询侧每题一次（重排关着）。",
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
            "T159 group 2: compare the two chunking variants (h3 as a section vs h3 merged "
            "into its parent) offline — splitter check, inventory, section identity and hit "
            "diff helpers. The live two-version build path was removed with the KB-level "
            "index version layer."
        )
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划与预估，不连库")
    args = parser.parse_args(argv)

    questions = len(json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))["questions"])
    v1_chunks, v2_chunks = _planned_counts()
    predicted = predicted_calls(chunks_v1=v1_chunks, chunks_v2=v2_chunks, questions=questions)
    print(
        f"预计模型调用 {predicted['total']} 次（文档嵌入 v1 {v1_chunks} + v2 {v2_chunks}；"
        f"查询嵌入 {questions}×2；零 LLM）"
    )
    print(
        "计划：两轮独立入库（各一个库）各记一份清单 → 用本文件的纯函数做命中 / 单元 / "
        "节身份 / CP-CR 对照（库级索引版本已删除，不再有两版并存）"
    )
    if args.dry_run:
        return 0
    raise SystemExit(
        "本组只保留离线对照：真实跑批要两轮独立入库，请按报告里的做法重做——"
        "这个脚本不再内置那条路径。"
    )


if __name__ == "__main__":
    raise SystemExit(main())

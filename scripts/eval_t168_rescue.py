"""⑤ 的「救回」这一面：压小检索深度强制造漏召回，测真救回 / 假救回 / 未救回（票 #168）。

**它是测量，不是功能。** #167 把 ⑤ 的另一面测出来了：`unknown → none` 13 条、`unknown →
supported` **0 条**。那个 0 被题集**结构性压低**——#167 每题的缺口方面在语料里根本不存在，
补证捞回的材料不可能撑住它。于是 ADR-0029 §2.2 里那句「`救回 = 0` 被结构性压低、那一种本轮
没测」至今只是一条**免责声明**，不是实测结论。本票把它测出来，并回答 ⑤ 到底是**双向**
（暴露 + 挽回）还是**纯收紧器**。

## 「救回」的严格定义（不许放宽）

```text
某要点：第一轮 none → 补证捞到材料 → unknown
        → ⑤ 复查后 supported
        → 且 evidence_ids 里至少一条材料的**章节**在该题的 expected 标注里
```

**为什么要带这道标准答案**：不带它，「救回率」会虚高——复查判成 `supported` 也可能是模型
看错了材料（把「相关」当「支撑」）。所以三分类必须分开报：

===============================  ==========================================================
**真救回**                         `supported` 且证据章节 ∈ `expected`（本题的正读数）
**假救回**                         `supported` 但证据章节 ∉ `expected`（模型变乐观了，**负向**）
**未救回**                         `unknown → none`（补证带了材料回来，复查仍判没有）
===============================  ==========================================================

第四类单独列、**不进分母**：

* **无机会**：补证一条新材料都没捞到（`no_new_evidence`）或首轮就没有缺口——那批要点是
  `none`（真缺口），**没有可复查的对象**，⑤ 也就不可能救回它。把它算进分母等于用「造不出
  机会」去解释「没救回」。

## 两组一起跑

**A 组（3 题，归档那 3 道停在 `max_rounds` 的题）**：`eval/reports/c6/runs/04-v2-research-on/
results.json` 里 `stop_reason == max_rounds` 的 r01 / r06 / r08。这个停止原因的定义就是
「补证捞到了新材料」，所以归档那几次**结构上必然**带至少一个 `unknown`。

> ⚠️ **票面说「复用归档」就地重判，实测做不到**：归档的 `per_question` 里**没有**要点清单、
> **没有**逐要点 `coverage`、**没有**「首轮材料 / 补证材料」的切分（`:func:`archive_gaps` 把
> 这件事机械核实并入库）。缺口闸/复查要判的对象是**要点**，而归档只有材料与停止原因，于是
> 「那批 `unknown`」在归档里**不存在**。A 组因此改成**用归档那 3 道题的题面重跑生产链路**
> （`top_k=5`、⑤ 开、跑到复查为止），并把归档的 `stop_reason` 与材料清单作为旁证入库。

**B 组（10 题，本票主体）**：压 `top_k=2` 强制造出「答案在库里、首轮没召回」。这不是造假
数据——**压 `top_k` 是检索侧参数，不是诱导模型犯错**，与「编题骗模型」是两回事。选题条件
**机械可查**（见 :func:`select_b2_cases`）：以 `top_k=2` 跑一次首轮检索，该题的 `expected`
章节**不在** top-2 里。每题跑**两个条件**：⑤ 开 / ⑤ 关。

## ⑤ 关掉的手法与 #167 逐字一致

评测进程内 `setattr(research._Run, "_rejudge", <async no-op>)`，`finally` 里换回来。
**`app/` 一个字节不改，不加生产开关**——那会在生产代码里留一条「默认关、生产永不发生」的
分支，正是 ADR-0029 点名的死代码。

⑤ 开着的那一趟也要知道「复查之前那批 `unknown` 是什么」，所以同一个接缝上挂一个**直通探针**：
先记下 `self.coverage` 与材料条数，再原样调用原来的 `_rejudge`。探针只读、不改状态，`finally`
里一律还原（一份被污染的方法会让后面每一题都静默跑错条件，而读数看起来完全正常）。

## 指标（分母写清楚）

========================  ==================================================================
**机会数（要点）**         有要点落 `unknown` 且补证真的带回材料的**要点**数（题数另报）
真救回率 / 假救回率 /      各自要点数 / 机会数（分母是「机会数」，不是「题数」，也不是
未救回率                   「全部要点数」）
A 组同指标                 3 题，单列
========================  ==================================================================

## 预算

**≤120 次模型调用**。A 组 3 题 × 最坏 5 = 15，B 组 10 题 × 2 条件 × 最坏 5 = 100，合计最坏
115。开跑前打印名义与最坏两个预估（:func:`predict_model_calls`），跑完把实际次数与分账写进
报告。超了就减 B 的题数，并在报告写明减了哪几道、为什么。

用法::

    # 只打印预估、选题与机械证据（0 次模型调用；**要连库与检索**——选题本身靠一次检索）
    uv run python scripts/eval_t168_rescue.py plan

    # 跑 A 组与 B 组（约 92 次模型调用）
    uv run python scripts/eval_t168_rescue.py run

    # 只跑其中一组（产物可增量补：先跑 A，再跑 B，前面的记录原样保留）
    uv run python scripts/eval_t168_rescue.py run --groups A

    # 按当前口径重算指标并重出报告（0 次模型调用，只读落盘记录）
    uv run python scripts/eval_t168_rescue.py recompute

产物：``eval/reports/t168/results.json``（机器可读）与 ``report.md``（人读）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    # 以 ``python scripts/eval_t168_rescue.py`` 运行时 ``sys.path[0]`` 是 ``scripts/``；
    # ``scripts`` 是没有 ``__init__.py`` 的命名空间包，靠这行才能 ``from scripts...``。
    sys.path.insert(0, str(REPO_ROOT))

EVAL_DIR = REPO_ROOT / "eval"
REFS_QUESTIONS = EVAL_DIR / "refs-questions.json"
BASE_QUESTIONS = EVAL_DIR / "questions.json"
REPORT_DIR = EVAL_DIR / "reports" / "t168"

#: 模型调用的硬顶（票面 ≤120）。
MODEL_CALL_CAP: Final[int] = 120

#: 两组：A = 归档那 3 题重跑；B = 压 top_k 的 10 题。
GROUP_A: Final[str] = "A"
GROUP_B: Final[str] = "B"
RUN_LABELS: Final[tuple[str, ...]] = (GROUP_A, GROUP_B)

#: B 组的题数（票面 10）。
B_CASE_COUNT: Final[int] = 10

#: 「⑤ 开着」与「⑤ 关掉」两个条件在逐题记录里的标记。
CONDITION_FIVEFOLD: Final[str] = "fivefold"
CONDITION_CONTROL: Final[str] = "control"

#: 一次 Research 的**名义**与**最坏**模型调用次数，从生产常量抄（不手编）：
#: 规划 + 判定 + 复查 + 生成 = ``MAX_MODEL_CALLS``（4）；含生成阶段的强模型兜底位 =
#: ``MAX_MODEL_CALLS_WITH_FALLBACK``（5）。``tests/test_eval_t168_rescue.py`` 钉住两者相等。
NOMINAL_CALLS_PER_CASE: Final[int] = 4
WORST_CALLS_PER_CASE: Final[int] = 5

#: A 组与 B 组各自的检索深度。**B 组压到 2 是本票唯一的人造条件**（见模块文档）。
A_TOP_K: Final[int] = 5
B_TOP_K: Final[int] = 2

#: A 组：归档里 `stop_reason == max_rounds` 的那 3 题（题号从归档机械筛出，见
#: :func:`load_archived_max_rounds`）。写死题号是为了「跑的一定是被判过的那 3 题」——
#: 归档变了就让 :func:`check_archived_premise` 红。
ARCHIVED_RUN: Final[Path] = (
    EVAL_DIR / "reports" / "c6" / "runs" / "04-v2-research-on" / "results.json"
)
ARCHIVED_MAX_ROUNDS_IDS: Final[tuple[str, ...]] = ("r01", "r06", "r08")

#: 下面几个词/码与生产**同一个字面量**，但抄一份：``plan`` / ``recompute`` 要在没有配置、
#: 没有库、没有模型的机器上也能跑（``import app`` 会读 settings）。
#: ``tests/test_eval_t168_rescue.py`` 把「抄漏了」变成一条会红的断言。
SUPPORT_SUPPORTED: Final[str] = "supported"
SUPPORT_NONE: Final[str] = "none"
SUPPORT_UNKNOWN: Final[str] = "unknown"
GAP_DECLARATION: Final[str] = "材料不足"
STATUS_INSUFFICIENT: Final[str] = "insufficient"

#: 三分类（+「仍未知」）的机器名。报告与用例读它们，不写字面量。
RESCUE_TRUE: Final[str] = "true_rescue"
RESCUE_FALSE: Final[str] = "false_rescue"
RESCUE_NOT: Final[str] = "not_rescued"
RESCUE_UNRESOLVED: Final[str] = "still_unknown"
NO_CHANCE: Final[str] = "no_chance"

#: 逐要点的三分类（进分母）与「不进分母」的那一类。
RESCUE_OUTCOMES: Final[tuple[str, ...]] = (RESCUE_TRUE, RESCUE_FALSE, RESCUE_NOT)

#: 检索侧：评测租户 / 库 / 关键词别名，按**语料**分组（两个语料各自的别名不同）。
#: 别名必须与建索引时用的一致：物理索引是 ``<别名>__<版本>``，写错别名 = 关键词一路查不到。
REFS_SOURCE: Final[str] = "refs"
EVAL_SOURCE: Final[str] = "eval"

#: refs 语料（3 篇 15 节，`eval/corpus-refs/`）：租户 ``tenant_c6``、别名 ``rag_chunks_c6``。
REFS_TENANT: Final[str] = "tenant_c6"
REFS_KB_ID: Final[str] = "ddae7e35-1858-4861-b9eb-631cfc778feb"
REFS_KEYWORD_ALIAS: Final[str] = "rag_chunks_c6"

#: eval 语料（5 个库 21 篇）：租户 ``tenant_demo``、别名 ``rag_chunks_eval``。
EVAL_TENANT: Final[str] = "tenant_demo"
EVAL_KB_IDS: Final[tuple[str, ...]] = (
    "kb-eval",
    "kb-eval-biz",
    "kb-eval-long",
    "kb-eval-ops",
    "kb-eval-ref",
)
EVAL_KEYWORD_ALIAS: Final[str] = "rag_chunks_eval"

#: 关键词通道的冷启动会超预算（实测首查 3.4 秒 > Provider 的 2 秒预算，第二查 0.16 秒），
#: 于是每题都会记一条 `sub_search_degraded`，读数悄悄变成「单路检索」。选题与跑批之前各打
#: 一次预热查询（0 次模型调用），把这一格排除掉。
WARMUP_QUERY: Final[str] = "预热"

#: 章节号：``2. 退款时限与审核`` → ``2``；``2.2 抵扣上限`` → ``2.2``。
_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+(?:\.\d+)*)")


# ─────────────────────────────────────────────────────────── 题集（两组的候选池）


@dataclass(frozen=True)
class Expected:
    """一条标准答案标注：某一**节**（或某个 heading 分支）里有回答该要点的事实。

    两种语料的标注口径不同，而两边都是**人工写在检索之前**的：

    * refs 语料（`eval/refs-questions.json`）：``document`` + ``section``（节号），另有 ``role``
      区分「答案本体 / 支撑 / 指路」；
    * eval 语料（`eval/questions.json` 的 ``relevant``）：``document`` + ``heading_path``。

    本票把两者都收成同一形状，**:attr:`kind` 决定比对口径**（见 :func:`material_matches`）：
    节号按「文档 + 节号」比，heading 按「文档 + 同一 heading 分支」比。
    """

    document: str
    section: str | None = None
    heading: str | None = None
    role: str = ""
    kind: str = "section"

    def as_json(self) -> dict[str, Any]:
        return {
            "document": self.document,
            "section": self.section,
            "heading": self.heading,
            "role": self.role,
            "kind": self.kind,
        }

    @property
    def label(self) -> str:
        """一行可读的标注（报告与台账共用这一份）。"""

        return f"{self.document}:{self.section if self.kind == 'section' else self.heading}"


@dataclass(frozen=True)
class Candidate:
    """一道候选题：题面 + 它的 standard answer 标注 + 这一题该用哪套检索参数。"""

    source: str
    origin_id: str
    query: str
    expected: tuple[Expected, ...]
    note: str = ""
    mechanism: str = ""


@dataclass(frozen=True)
class EvalCase:
    """被选中的一道题（**选题结果**，不是候选池）：组 + 来源 + 标注 + 检索深度。"""

    case_id: str
    group: str
    source: str
    origin_id: str
    query: str
    expected: tuple[Expected, ...]
    top_k: int
    note: str = ""
    mechanism: str = ""


def _refs_expected(item: Mapping[str, Any]) -> tuple[Expected, ...]:
    return tuple(
        Expected(
            document=str(entry["document"]),
            section=str(entry["section"]),
            role=str(entry.get("role") or ""),
            kind="section",
        )
        for entry in item["expected"]
    )


def _eval_expected(item: Mapping[str, Any]) -> tuple[Expected, ...]:
    return tuple(
        Expected(
            document=str(entry["document"]),
            heading=str(entry["heading_path"]),
            kind="heading",
        )
        for entry in item.get("relevant") or []
    )


def load_refs_candidates(
    path: Path | None = None, *, ids: Sequence[str] | None = None
) -> list[Candidate]:
    """refs 语料的候选题（8 题，自带 ``expected`` 节号标注）。``ids`` 只取这几题。"""

    payload = json.loads((path or REFS_QUESTIONS).read_text(encoding="utf-8"))
    wanted = set(ids) if ids is not None else None
    candidates: list[Candidate] = []
    for item in payload["questions"]:
        if wanted is not None and item["id"] not in wanted:
            continue
        candidates.append(
            Candidate(
                source=REFS_SOURCE,
                origin_id=str(item["id"]),
                query=str(item["query"]),
                expected=_refs_expected(item),
                note=str(item.get("mechanism_note") or ""),
                mechanism=str(item.get("mechanism") or ""),
            )
        )
    return candidates


def load_eval_candidates(path: Path | None = None) -> list[Candidate]:
    """eval 语料的候选题（137 题，其中带 ``relevant`` 标注的才是候选）。

    **没有 ``relevant`` 的题不进来**：本票的「救回」必须有标准答案可查，没有标注就无从判断
    「证据章节 ∈ expected」——那正是本票不肯放宽的那道闸。
    """

    payload = json.loads((path or BASE_QUESTIONS).read_text(encoding="utf-8"))
    candidates: list[Candidate] = []
    for item in payload["questions"]:
        expected = _eval_expected(item)
        if not expected:
            continue
        candidates.append(
            Candidate(
                source=EVAL_SOURCE,
                origin_id=str(item["id"]),
                query=str(item["query"]),
                expected=expected,
                note=str(item.get("note") or ""),
                mechanism=str(item.get("type") or ""),
            )
        )
    return candidates


# ─────────────────────────────────────────────────────────── 机械比对（标准答案）


def normalize_heading(text: Any) -> str:
    """heading 路径归一化：只在 ``>`` 两侧留单空格，其余原样。

    比对的合法性靠这一条：两条 heading 只有在**去掉排版差异后**才可能相等，于是「同一天然
    标题」不会被空白差一个字判成不同节。
    """

    parts = [part.strip() for part in str(text or "").split(">")]
    return " > ".join(part for part in parts if part)


def section_of_heading(heading_path: Any) -> str:
    """从 heading 路径的最后一段取节号：``… > 2.2 抵扣上限`` → ``2.2``；没有节号 → ``""``。"""

    parts = [part.strip() for part in str(heading_path or "").split(">") if part.strip()]
    if not parts:
        return ""
    match = _SECTION_RE.match(parts[-1])
    return match.group(1) if match else ""


def heading_in_branch(expected: Any, actual: Any) -> bool:
    """两条 heading 是否在**同一条分支**上（相等，或一条是另一条的前缀路径）。

    为什么不用「完全相等」：切块器的交付单元可以是 h2，也可以是 h2 下面某个 h3（「一节大于
    窗口」时尤其如此）。于是「标准答案标在 h2、材料是它下面的 h3」与「标准答案标在 h3、材料
    是它的父 h2」都会出现——两者都指**同一节内容**，判成不相交会让「证据章节 ∈ expected」
    这条机械规则把真事判错。反向的代价是宽松，但**宽松有边界**：不同分支的 heading 永远
    不可能一条是另一条的前缀，所以它不会把「别的节」算进来。
    """

    left = normalize_heading(expected)
    right = normalize_heading(actual)
    if not left or not right:
        return False
    if left == right:
        return True
    return right.startswith(left + " > ") or left.startswith(right + " > ")


def document_stem_of(document_id: Any, source_filename: Any = None) -> str:
    """材料属于哪篇文档：优先 ``source_filename`` 的词干，退化到 ``eval-doc-`` 前缀剥离。

    两种语料的文档 id 口径不同（eval 语料是 ``eval-doc-<stem>``，refs 语料是真实 uuid），
    所以这里用**两个都成立的**规则，而不是给两套语料各写一份。
    """

    name = str(source_filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if name:
        return name.rsplit(".", 1)[0]
    raw = str(document_id or "")
    return raw.removeprefix("eval-doc-")


@dataclass(frozen=True)
class MaterialRef:
    """一条材料的身份（复审用）：文档 + 节号 + heading 路径 + 两个 id。"""

    document: str
    section: str
    heading: str
    section_id: str
    chunk_id: str

    def as_json(self) -> dict[str, Any]:
        return {
            "document": self.document,
            "section": self.section,
            "heading": self.heading,
            "section_id": self.section_id,
            "chunk_id": self.chunk_id,
        }

    @property
    def label(self) -> str:
        return f"{self.document}:{self.section or self.heading}"


def material_ref(row: Mapping[str, Any], *, stems: Mapping[str, str]) -> MaterialRef:
    """检索/归档的一行 → :class:`MaterialRef`。"""

    document_id = str(row.get("document_id") or "")
    return MaterialRef(
        document=stems.get(document_id)
        or document_stem_of(document_id, row.get("source_filename")),
        section=section_of_heading(row.get("heading_path")),
        heading=normalize_heading(row.get("heading_path")),
        section_id=str(row.get("section_id") or ""),
        chunk_id=str(row.get("chunk_id") or ""),
    )


def material_matches(expected: Expected, material: MaterialRef) -> bool:
    """**标准答案机械可查的唯一一处**：这条材料算不算落在该标注里。

    两种口径分别对应两种标注（不许混用）：

    * ``kind == "section"``（refs 语料）：文档相同 **且** 节号相同；
    * ``kind == "heading"``（eval 语料）：文档相同 **且** heading 在同一分支（见
      :func:`heading_in_branch`）。

    **没有人工判断**：两边的取值都是标注文件与索引元数据里的字符串。
    """

    if material.document != expected.document:
        return False
    if expected.kind == "section":
        return bool(expected.section) and material.section == expected.section
    return heading_in_branch(expected.heading, material.heading)


def missing_expected(
    expected: Sequence[Expected], materials: Sequence[MaterialRef]
) -> list[Expected]:
    """这批材料里**没有**落到的那些标注（按入参顺序，便于逐题比对）。"""

    return [
        item
        for item in expected
        if not any(material_matches(item, material) for material in materials)
    ]


# ─────────────────────────────────────────────────────────── 选题（机械条件）


@dataclass
class Probe:
    """一次选题探针：这道题在 ``top_k`` 下**首轮**检出了什么、缺了哪些标注。"""

    source: str
    origin_id: str
    query: str
    top_k: int
    expected: tuple[Expected, ...]
    hits: tuple[MaterialRef, ...]
    missing: tuple[Expected, ...]
    degraded: tuple[str, ...] = ()
    note: str = ""
    mechanism: str = ""

    @property
    def qualified(self) -> bool:
        """选题条件：**至少一条** ``expected`` 标注在 top-k 里缺席。"""

        return bool(self.missing)

    @property
    def all_missing(self) -> bool:
        """更强的条件：所有标注都缺席（首轮一条相关内容都没进来）。"""

        return bool(self.expected) and len(self.missing) == len(self.expected)

    def as_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "origin_id": self.origin_id,
            "query": self.query,
            "top_k": self.top_k,
            "expected": [item.as_json() for item in self.expected],
            "expected_labels": [item.label for item in self.expected],
            "hits": [item.as_json() for item in self.hits],
            "hit_labels": [item.label for item in self.hits],
            "missing_labels": [item.label for item in self.missing],
            "qualified": self.qualified,
            "all_missing": self.all_missing,
            "degraded": list(self.degraded),
            "note": self.note,
            "mechanism": self.mechanism,
        }


def case_order_key(origin_id: str) -> tuple[str, tuple[int, ...], str]:
    """题号的自然序：``q9 < q10 < q100``（纯字符串序会把 ``q100`` 排到 ``q13`` 前面）。

    排序规则必须是**机械的**，否则「选了哪 10 题」就变成一次人工挑选；而自然序是最不需要
    解释的那一种。
    """

    match = re.match(r"^([A-Za-z]*)(\d+(?:\.\d+)*)(.*)$", str(origin_id))
    if match is None:
        return ("", (), str(origin_id))
    prefix, numbers, tail = match.groups()
    return (prefix, tuple(int(part) for part in numbers.split(".")), tail)


def select_b2_cases(probes: Sequence[Probe], *, limit: int = B_CASE_COUNT) -> list[Probe]:
    """从候选池里按**机械规则**挑 B 组 10 题（不许人工挑）。

    三层排序，每层都是可复核的事实：

    1. **refs 语料优先**（它自带 ``answer_points`` 与节号标注，是本题面点名的题集），按题号；
    2. refs 不够时补 **eval 语料里「全部标注都缺席」的题**（首轮一条相关内容都没进来，漏得最
       干净），按题号；
    3. 仍不够时补 **eval 语料里「部分缺席」的题**，按题号。

    落选的题（``qualified == False``）**不参与排序**：它们在 top-2 下已经命中了全部标注，
    首轮不可能漏，也就造不出本票要测的那种机会。
    """

    qualified = [probe for probe in probes if probe.qualified]
    refs = sorted(
        (probe for probe in qualified if probe.source == REFS_SOURCE),
        key=lambda probe: case_order_key(probe.origin_id),
    )
    eval_all = sorted(
        (probe for probe in qualified if probe.source == EVAL_SOURCE and probe.all_missing),
        key=lambda probe: case_order_key(probe.origin_id),
    )
    eval_part = sorted(
        (probe for probe in qualified if probe.source == EVAL_SOURCE and not probe.all_missing),
        key=lambda probe: case_order_key(probe.origin_id),
    )
    return (refs + eval_all + eval_part)[:limit]


def build_cases(probes: Sequence[Probe], *, limit: int = B_CASE_COUNT) -> list[EvalCase]:
    """选题结果 → 可跑的题（``case_id`` 就是它在 B 组的序号）。"""

    chosen = select_b2_cases(probes, limit=limit)
    return [
        EvalCase(
            case_id=f"B{index:02d}",
            group=GROUP_B,
            source=probe.source,
            origin_id=probe.origin_id,
            query=probe.query,
            expected=probe.expected,
            top_k=probe.top_k,
            note=probe.note,
            mechanism=probe.mechanism,
        )
        for index, probe in enumerate(chosen, start=1)
    ]


# ─────────────────────────────────────────────────────────── 归档（A 组的素材）


def load_archived_max_rounds(path: Path | None = None) -> dict[str, Any]:
    """从归档里机械筛出 ``stop_reason == max_rounds`` 的题并取出它们的材料清单。

    **不手挑**：停止原因的定义就是「补证捞到了新材料」（`research._stages` 里
    `return MAX_ROUNDS if sum(added)`），所以这批题**结构上必然**带至少一个 `unknown`。
    """

    payload = json.loads((path or ARCHIVED_RUN).read_text(encoding="utf-8"))
    per_question = payload.get("per_question") or []
    chosen = [item for item in per_question if item.get("stop_reason") == "max_rounds"]
    questions = {str(item["id"]): item for item in chosen}
    return {
        "run": str((path or ARCHIVED_RUN).relative_to(REPO_ROOT)),
        "generated_at": payload.get("generated_at"),
        "index_versions": payload.get("index_versions"),
        "llm_chain": payload.get("llm_chain"),
        "max_rounds_ids": [str(item["id"]) for item in chosen],
        "questions": questions,
    }


def archive_gaps(
    archive: Mapping[str, Any], *, refs_questions: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """归档**缺什么**——把「票面说的复用做不到」变成一条可复核的机械结论。

    逐题目看三样东西在不在：要点清单（``points``）、逐要点判定（``coverage`` / ``coverage.points``）、
    「首轮材料 / 补证材料」的切分（补证新增材料的标记）。三样都缺时，「那批 `unknown`」在归档里
    **不存在**，就地重判没有对象。
    """

    questions = archive.get("questions") or {}
    report: dict[str, Any] = {"missing": {}, "present": {}}
    for field_name, keys in (
        ("points", ("points", "plan", "plan.points")),
        ("coverage", ("coverage", "coverage.points", "judged")),
        ("followup_split", ("followup", "round", "added")),
    ):
        found = [qid for qid, item in questions.items() if any(key in item for key in keys)]
        report["present"][field_name] = found
        report["missing"][field_name] = sorted(set(questions) - set(found))
    report["questions"] = sorted(questions)
    report["reusable_as_is"] = all(
        not report["missing"][name] for name in ("points", "coverage", "followup_split")
    )
    if refs_questions is not None:
        payload = refs_questions
        report["refs_question_ids"] = [str(item["id"]) for item in payload.get("questions") or []]
    return report


def check_archived_premise(archive: Mapping[str, Any]) -> None:
    """A 组的题必须真的来自归档里那 3 道 ``max_rounds`` 题；归档变了就当场失败。"""

    ids = tuple(str(item) for item in archive.get("max_rounds_ids") or ())
    if ids != ARCHIVED_MAX_ROUNDS_IDS:
        raise RuntimeError(
            f"归档里停在 max_rounds 的题变了：{ids!r} != {ARCHIVED_MAX_ROUNDS_IDS!r}"
        )
    questions = archive.get("questions") or {}
    for question_id in ARCHIVED_MAX_ROUNDS_IDS:
        item = questions.get(question_id)
        if item is None:
            raise RuntimeError(f"归档里没有 {question_id}")
        if not item.get("hits"):
            raise RuntimeError(f"归档里 {question_id} 没有材料清单")


# ─────────────────────────────────────────────────────────── 预算与预估


def predict_model_calls(*, b_cases: int = B_CASE_COUNT, a_cases: int = 3) -> dict[str, Any]:
    """开跑前的两个预估（名义 / 最坏）与分账。超硬顶就当场报出来。"""

    a_nominal = a_cases * NOMINAL_CALLS_PER_CASE
    a_worst = a_cases * WORST_CALLS_PER_CASE
    b_nominal = b_cases * 2 * NOMINAL_CALLS_PER_CASE
    b_worst = b_cases * 2 * WORST_CALLS_PER_CASE
    nominal = a_nominal + b_nominal
    worst = a_worst + b_worst
    return {
        "nominal": nominal,
        "worst": worst,
        "budget": MODEL_CALL_CAP,
        "nominal_fits": nominal <= MODEL_CALL_CAP,
        "worst_fits": worst <= MODEL_CALL_CAP,
        "breakdown": {
            "A": {"cases": a_cases, "conditions": 1, "nominal": a_nominal, "worst": a_worst},
            "B": {"cases": b_cases, "conditions": 2, "nominal": b_nominal, "worst": b_worst},
        },
    }


@dataclass
class Budget:
    """模型调用的账：**先预留再发**，预留不到就不跑下一题（上限不是一句愿望）。"""

    cap: int = MODEL_CALL_CAP
    spent: int = 0
    skipped: list[str] = field(default_factory=list)

    def can(self, reserve: int) -> bool:
        return self.spent + reserve <= self.cap

    def charge(self, calls: int) -> None:
        self.spent += int(calls)

    def skip(self, case_id: str, reserve: int) -> None:
        self.skipped.append(f"{case_id}（还需 {reserve} 次，已用 {self.spent}/{self.cap}）")


# ─────────────────────────────────────────────────────────── 题集自检


def check_case_set(
    cases: Sequence[EvalCase], *, probes: Sequence[Probe] | None = None
) -> dict[str, Any]:
    """题集不变量：题数、题号唯一、每题都带标准答案、**B 组的选题条件真的成立**。

    最后一条是本票的命根子——「`expected` 在 top-2 下缺席」如果没有机械证据，所谓「强制造出
    漏召回」就只是一句自述。所以这里不采信 `cases` 自己的说法，而是回到探针结果（``probes``）
    逐题复核。
    """

    problems: list[str] = []
    b_cases = [case for case in cases if case.group == GROUP_B]
    if len(b_cases) != B_CASE_COUNT:
        problems.append(f"B 组题数 {len(b_cases)} != {B_CASE_COUNT}")
    if len({case.case_id for case in cases}) != len(cases):
        problems.append("题号重复")
    for case in cases:
        if not case.expected:
            problems.append(f"{case.case_id} 没有标准答案标注")
        if case.top_k <= 0:
            problems.append(f"{case.case_id} 的 top_k 不合法")
    if probes is not None:
        evidence = {(probe.source, probe.origin_id): probe for probe in probes}
        for case in b_cases:
            probe = evidence.get((case.source, case.origin_id))
            if probe is None:
                problems.append(f"{case.case_id} 没有选题证据")
                continue
            if probe.top_k != B_TOP_K:
                problems.append(f"{case.case_id} 的选题探针不是 top_k={B_TOP_K}")
            if not probe.missing:
                problems.append(
                    f"{case.case_id} 的 expected 在 top-{B_TOP_K} 下没有缺席（选题条件不成立）"
                )
    return {
        "ok": not problems,
        "problems": problems,
        "cases": len(cases),
        "b_cases": len(b_cases),
        "sources": sorted({case.source for case in cases}),
        "top_k": sorted({case.top_k for case in cases}),
    }


# ─────────────────────────────────────────────────────────── 检索与探针（真实内核）


def _retrieval_options(top_k: int) -> Any:
    """这次评测用的检索参数：与 ``balanced`` 档同形，只改 ``top_k``。

    为什么由脚本构造而不是走 ``resolve_retrieval_options``：那条路要一份 ``KeyPolicySnapshot``
    + 套餐，而评测没有「这套集成买的是哪一档」。编一个套餐名比承认「这是评测自己的参数」更糟
    ——``RetrievalOptions`` 的 ``tenant_policy=None`` 正是为这种情形留的。
    """

    from app.services.retrieval_options import RetrievalOptions

    return RetrievalOptions(
        mode="hybrid",
        top_k=top_k,
        fold_to_sections=True,
        expand_table_rows=False,
        rerank=False,
        rewrite=False,
    )


def _scope_of(source: str) -> dict[str, Any]:
    """语料 → (租户 / 库 / 关键词别名)。两组用两套，因为它们是两个独立的库。"""

    if source == REFS_SOURCE:
        return {
            "tenant_id": REFS_TENANT,
            "kb_ids": [REFS_KB_ID],
            "keyword_alias": REFS_KEYWORD_ALIAS,
        }
    return {
        "tenant_id": EVAL_TENANT,
        "kb_ids": list(EVAL_KB_IDS),
        "keyword_alias": EVAL_KEYWORD_ALIAS,
    }


async def _resolve_scope(db: Any, *, tenant_id: str, kb_ids: Sequence[str]) -> dict[str, str]:
    """``kb_id → 生效索引版本``：走生产那一个构造点（``resolve_index_versions``）。

    不手写 ``"v2"``：版本是服务端的决定，评测抄一个常量就等于把「这次查的是哪一版」写成一个
    随时会过期的假设。
    """

    from sqlalchemy import select

    from app.models.index_version import resolve_index_versions
    from app.models.knowledge_base import KnowledgeBase

    rows = (
        (
            await db.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.id.in_(list(kb_ids))
                )
            )
        )
        .scalars()
        .all()
    )
    return dict(resolve_index_versions(list(rows)))


async def _document_stems(db: Any, *, tenant_id: str, kb_ids: Sequence[str]) -> dict[str, str]:
    """``document_id → 文档词干``（材料身份的一半，另一半是节号/heading）。

    refs 语料的文档有 ``source_filename``；eval 语料的文档 id 本身就是 ``eval-doc-<stem>``
    （``source_filename`` 为空）。两种都走 :func:`document_stem_of`，不各写一份。
    """

    from sqlalchemy import select

    from app.models.document import Document

    rows = (
        await db.execute(
            select(Document.id, Document.source_filename).where(
                Document.tenant_id == tenant_id, Document.kb_id.in_(list(kb_ids))
            )
        )
    ).all()
    return {str(row[0]): document_stem_of(row[0], row[1]) for row in rows}


def _apply_eval_settings(*, keyword_alias: str) -> dict[str, Any]:
    """钉死评测要用的环境：**只读评测索引**、不重排、不改写。

    与 ``scripts/eval_retrieval.py`` 同一条纪律（``settings.ELASTICSEARCH_INDEX`` → 评测别名）：
    评测绝不读生产别名，也绝不写任何索引。三个开关在这里显式写下而不是吃 ``.env`` 的偶然值
    ——它们会改变「检索引擎捞到什么」，而这个评测要量的正是捞之后的判定。
    """

    from app.core.config import settings

    settings.ELASTICSEARCH_INDEX = keyword_alias
    settings.RERANK_ENABLED = False
    settings.QUERY_REWRITE_ENABLED = False
    return {
        "elasticsearch_index": settings.ELASTICSEARCH_INDEX,
        "rerank": settings.RERANK_ENABLED,
        "query_rewrite": settings.QUERY_REWRITE_ENABLED,
        "retrieval_mode": settings.RETRIEVAL_MODE,
        "top_k": settings.TOP_K,
    }


async def probe_candidates(
    candidates: Sequence[Candidate],
    *,
    top_k: int,
    search: Callable[[str], Awaitable[Sequence[Mapping[str, Any]]]],
    stems: Mapping[str, str],
    on_degraded: list[str],
) -> list[Probe]:
    """逐题跑一次 ``top_k`` 首轮检索（**0 次模型调用**），算出哪些标注缺席。

    为什么「首轮」用**原问题**跑一次，而不是先调一次规划器：选题发生在任何模型调用之前
    （预算得先算得出来），而规划器是那笔预算的第一个消费者。原问题是最保守的探针——它问的
    是「这个问题本身在 top-k 里够不够」，漏了就是漏了。真实首轮（按要点的多条查询）检出的
    只会**更多**，所以这里的缺席条件是**充分**的：真跑时至少这条标注仍然可能漏。
    """

    probes: list[Probe] = []
    for candidate in candidates:
        on_degraded.clear()
        rows = await search(candidate.query)
        materials: list[MaterialRef] = []
        seen: set[str] = set()
        for row in rows:
            ref = material_ref(row, stems=stems)
            # 按节去重：`_Run._merge` 就是这么并池的，探针跟着它去重才对得上「真跑时首轮
            # 到底看得见哪几节」。否则同一条材料会因为两次子查询各命中一次而在证据里出现两遍。
            key = ref.section_id or ref.chunk_id or ref.heading
            if key in seen:
                continue
            seen.add(key)
            materials.append(ref)
        probes.append(
            Probe(
                source=candidate.source,
                origin_id=candidate.origin_id,
                query=candidate.query,
                top_k=top_k,
                expected=candidate.expected,
                hits=tuple(materials),
                missing=tuple(missing_expected(candidate.expected, materials)),
                degraded=tuple(on_degraded),
                note=candidate.note,
                mechanism=candidate.mechanism,
            )
        )
    return probes


async def collect_probes(*, top_k: int = B_TOP_K) -> tuple[list[Probe], dict[str, Any]]:
    """两组候选各跑一趟探针（0 次模型调用），返回探针结果与本轮环境。

    两套语料的关键词别名不同（物理索引是 ``<别名>__<版本>``），所以**按语料分两趟**：换别名
    必须换 ``settings``，而一次进程只有一个 ``settings``——这与「一次只查一套语料」的生产形态
    一致，不是评测的取巧。
    """

    from app.db.async_session import AsyncSessionLocal
    from app.services.research_gate import build_kernel_search

    probes: list[Probe] = []
    environment: dict[str, Any] = {"sources": {}}
    async with AsyncSessionLocal() as db:
        for source, candidates in (
            (REFS_SOURCE, load_refs_candidates()),
            (EVAL_SOURCE, load_eval_candidates()),
        ):
            scope = _scope_of(source)
            _apply_eval_settings(keyword_alias=scope["keyword_alias"])
            versions = await _resolve_scope(
                db, tenant_id=scope["tenant_id"], kb_ids=scope["kb_ids"]
            )
            stems = await _document_stems(db, tenant_id=scope["tenant_id"], kb_ids=scope["kb_ids"])
            degraded: list[str] = []
            search = build_kernel_search(
                db,
                tenant_id=scope["tenant_id"],
                index_versions=versions,
                options=_retrieval_options(top_k),
                on_degraded=degraded.append,
            )
            # 冷启动那一次的关键词通道会超预算，先打一发再开始记（见 WARMUP_QUERY）。
            await search(WARMUP_QUERY)
            degraded.clear()
            probes.extend(
                await probe_candidates(
                    candidates,
                    top_k=top_k,
                    search=search,
                    stems=stems,
                    on_degraded=degraded,
                )
            )
            environment["sources"][source] = {
                "tenant_id": scope["tenant_id"],
                "kb_ids": scope["kb_ids"],
                "keyword_alias": scope["keyword_alias"],
                "index_versions": versions,
                "documents": len(stems),
                "candidates": len(candidates),
            }
    environment["probe_top_k"] = top_k
    return probes, environment


def build_plan(
    *,
    probes: Sequence[Probe],
    archive: Mapping[str, Any],
    environment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把探针结果整理成「这一趟打算跑什么」：预估值、选题、机械证据、自检结论。"""

    cases = build_cases(probes)
    # A 组那 3 题同时也在 refs 题集里（带 `expected` 节号标注），所以标准答案与 B 组同源：
    # 不另抄一份，而是按题号取同一份标注——两边判「证据章节 ∈ expected」用的是同一把尺子。
    refs_by_id = {candidate.origin_id: candidate for candidate in load_refs_candidates()}
    a_cases = [
        EvalCase(
            case_id=f"A{index:02d}",
            group=GROUP_A,
            source=REFS_SOURCE,
            origin_id=question_id,
            query=str(archive["questions"][question_id]["query"]),
            expected=refs_by_id[question_id].expected,
            top_k=A_TOP_K,
        )
        for index, question_id in enumerate(ARCHIVED_MAX_ROUNDS_IDS, start=1)
    ]
    predicted = predict_model_calls(b_cases=len(cases), a_cases=len(a_cases))
    return {
        "predicted": predicted,
        "cases": cases,
        "a_cases": a_cases,
        "probes": list(probes),
        "selected": [(probe.source, probe.origin_id) for probe in select_b2_cases(probes)],
        "checks": check_case_set(list(cases) + list(a_cases), probes=probes),
        "archive": dict(archive),
        "gaps": archive_gaps(archive),
        "environment": dict(environment or {}),
    }


def render_plan(plan: Mapping[str, Any]) -> str:
    """``plan`` 模式的人读输出：预估、选题与机械证据（一屏能看完）。"""

    predicted = plan["predicted"]
    lines = [
        f"预计模型调用：名义 {predicted['nominal']} 次 / 最坏 {predicted['worst']} 次"
        f"（硬顶 {predicted['budget']}）",
        "分账："
        + "；".join(
            f"{name} {item['cases']} 题 × {item['conditions']} 条件 × "
            f"{NOMINAL_CALLS_PER_CASE}~{WORST_CALLS_PER_CASE} 次"
            for name, item in predicted["breakdown"].items()
        ),
    ]
    if not predicted["worst_fits"]:
        lines.append(
            f"⚠️ 最坏预估 {predicted['worst']} 超过硬顶 {predicted['budget']}：需要减题（见报告）"
        )
    gaps = plan["gaps"]
    lines.append(
        "归档可复用性："
        + (
            "可以就地重判"
            if gaps["reusable_as_is"]
            else "**不能就地重判**（缺 "
            + "、".join(name for name, ids in gaps["missing"].items() if ids)
            + "）"
        )
    )
    probes = plan["probes"]
    qualified = [probe for probe in probes if probe.qualified]
    lines.append(
        f"候选池：{len(probes)} 题，其中 top_k={B_TOP_K} 下 expected 缺席的 **{len(qualified)}** 题"
    )
    lines.append("")
    lines.append(f"B 组选题（{len(plan['cases'])} 题，机械条件：expected 不在 top-{B_TOP_K}）：")
    evidence = {(probe.source, probe.origin_id): probe for probe in probes}
    for case in plan["cases"]:
        probe = evidence[(case.source, case.origin_id)]
        lines.append(
            f"  {case.case_id} ← {case.origin_id}（{case.source}）"
            f" 缺 {len(probe.missing)}/{len(probe.expected)}"
            f" ⏐ 期望 {[item.label for item in probe.expected]}"
            f" ⏐ top-{B_TOP_K} 实得 {[item.label for item in probe.hits]}"
        )
    checks = plan["checks"]
    lines.append("")
    lines.append(
        "题集自检：" + ("通过" if checks["ok"] else "**失败** " + "；".join(checks["problems"]))
    )
    lines.append(f"A 组（{len(plan['a_cases'])} 题，归档 max_rounds）：")
    for case in plan["a_cases"]:
        lines.append(f"  {case.case_id} ← {case.origin_id}：{case.query}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────── 命令行


async def run_plan(*, out_dir: Path) -> int:
    """``plan``：只打印预估、选题与机械证据（**0 次模型调用**，但要连库与检索）。"""

    archive = load_archived_max_rounds()
    check_archived_premise(archive)
    probes, environment = await collect_probes()
    plan = build_plan(probes=probes, archive=archive, environment=environment)
    print(render_plan(plan))
    if not plan["checks"]["ok"]:
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="⑤ 的「救回」这一面（票 #168）：真救回 / 假救回 / 未救回"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="只打印预估、选题与机械证据（0 次模型调用）")
    plan.add_argument("--out-dir", default=str(REPORT_DIR), help="产物目录（plan 不写）")

    run = subparsers.add_parser("run", help="跑 A / B 两组并写 eval/reports/t168/")
    run.add_argument("--groups", nargs="+", default=list(RUN_LABELS), choices=list(RUN_LABELS))
    run.add_argument("--out-dir", default=str(REPORT_DIR), help="产物目录")

    recompute = subparsers.add_parser(
        "recompute", help="按当前口径重算已有产物的指标并重出报告（0 次模型调用）"
    )
    recompute.add_argument("--out-dir", default=str(REPORT_DIR), help="产物目录")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    if sys.platform == "win32" and args.command in ("run", "plan"):
        # psycopg 的异步连接在 Windows 的缺省 Proactor 循环上跑不起来（第一条查询就
        # InterfaceError）。plan 也要连库选题，所以两个命令都设——与
        # scripts/eval_t167_gap_gate.py / eval_retrieval.py 同一行理由。
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if args.command == "plan":
        return asyncio.run(run_plan(out_dir=out_dir))
    print(f"{args.command} 尚未实现（下一里程碑）")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

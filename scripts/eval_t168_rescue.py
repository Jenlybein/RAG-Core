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

## 追加条件（实施中加的，票面没有）：`B-tight` —— 同一批题，`top_k=1`

**为什么加**：B 组（`top_k=2`）跑出来机会数是 **0**——十个题里没有一个要点落 `unknown`。
成因在第四章的「无机会」表里逐题摊开了：**「标注节缺席」不等于「答案缺席」**，判定在首轮就用
**别的节**把要点判成了 `supported`，于是 `_stages` 直接 `evidence_complete` 收尾，补证与复查
都没被叫到。这样一来本票就成了「⑤ 没被测到」，而不是「⑤ 没救回」。

于是追加一档 `top_k=1`（同一批题、同一套流程、只改深度），回答一个具体问题：**「机会数=0」
是因为深度还不够小，还是因为卡点在上游？** 这一档的读数**同样按严格定义三分类**，假救回也
照样计入——加它是为了让「机会」真的出现，不是为了把某个率做漂亮。

它**不是**票面要求的设计，报告里单列，并且不参与主读数。

**预算口径要说清**：追加这一档之后，纸面的名义/最坏预估变成 132 / 165，**超过 ≤120 的硬顶**；
但预算闸是「按最坏逐题预留、按**实际**调用扣账」，整票实际花掉 **108** 次，仍在硬顶之内。
两个数都写进报告（第九节），因为「纸面超界」与「实际超跑」不是一回事。追加条件是在 A、B 两组
跑完之后才决定的，所以这里的名义数是事后补算的。

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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
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

#: 两组：A = 归档那 3 题重跑；B = 压 top_k 的 10 题。第三个是**追加条件**（见模块文档末尾）。
GROUP_A: Final[str] = "A"
GROUP_B: Final[str] = "B"
GROUP_B_TIGHT: Final[str] = "B-tight"
RUN_LABELS: Final[tuple[str, ...]] = (GROUP_A, GROUP_B, GROUP_B_TIGHT)

#: B 组的题数（票面 10）。
B_CASE_COUNT: Final[int] = 10

#: 「⑤ 开着」与「⑤ 关掉」两个条件在逐题记录里的标记。
CONDITION_FIVEFOLD: Final[str] = "fivefold"
CONDITION_CONTROL: Final[str] = "control"

#: **追加条件**：同一批 B 组题、同一套流程，只把 `top_k` 压到 1（见模块文档末尾）。
CONDITION_TIGHT: Final[str] = "tight"
TIGHT_TOP_K: Final[int] = 1

#: 一次 Research 的**名义**与**最坏**模型调用次数，从生产常量抄（不手编）：
#: 规划 + 判定 + 复查 + 生成 = ``MAX_MODEL_CALLS``（4）；含生成阶段的强模型兜底位 =
#: ``MAX_MODEL_CALLS``（4）。``tests/test_eval_t168_rescue.py`` 钉住两者相等。
NOMINAL_CALLS_PER_CASE: Final[int] = 3
WORST_CALLS_PER_CASE: Final[int] = 4

#: A 组与 B 组各自的检索深度。**B 组压到 2 是本票唯一的人造条件**（见模块文档）。
A_TOP_K: Final[int] = 5
B_TOP_K: Final[int] = 2

#: 补证轮的子检索条数上限（生产常量 `research_limits.MAX_FOLLOWUP_QUERIES`）。**抄一份而不是
#: import**：`plan` / `recompute` 要在没有配置、没有库的机器上也能跑。这一格是本票**机会数的
#: 结构性上界**——每题最多 2 条补证查询，于是最多 2 个要点能从 `none` 变成 `unknown`。
#: `tests/test_eval_t168_rescue.py` 把「抄漏了」变成一条会红的断言。
FOLLOWUP_QUERY_CAP: Final[int] = 2

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

#: 三分类的中文名（只给报告用；机器名进产物与用例）。
RESCUE_LABELS: Final[dict[str, str]] = {
    RESCUE_TRUE: "真救回（supported ∧ 证据章节 ∈ expected）",
    RESCUE_FALSE: "假救回（supported 但证据章节 ∉ expected，**负向**）",
    RESCUE_NOT: "未救回（unknown → none）",
    RESCUE_UNRESOLVED: "仍 unknown（复查失败，不进分母）",
}

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
        "run": (path or ARCHIVED_RUN).relative_to(REPO_ROOT).as_posix(),
        "generated_at": payload.get("generated_at"),
        "kb_scope": payload.get("kb_scope"),
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


def predict_model_calls(
    *, b_cases: int = B_CASE_COUNT, a_cases: int = 3, tight_cases: int = B_CASE_COUNT
) -> dict[str, Any]:
    """开跑前的预估与分账：**票面设计**（A + B 两条件）与**追加条件**分开算。

    为什么要分开：追加的 `B-tight` 是实施中途加的，如果把它混进「名义/最坏」，票面那句
    「≤120」就会在纸面上被突破，而实际花掉的钱并没有突破。两个数分开写，读者才看得出
    「预算够不够」与「我多花在哪」是两件事。
    """

    a_nominal = a_cases * NOMINAL_CALLS_PER_CASE
    a_worst = a_cases * WORST_CALLS_PER_CASE
    b_nominal = b_cases * 2 * NOMINAL_CALLS_PER_CASE
    b_worst = b_cases * 2 * WORST_CALLS_PER_CASE
    tight_nominal = tight_cases * NOMINAL_CALLS_PER_CASE
    tight_worst = tight_cases * WORST_CALLS_PER_CASE
    spec_nominal = a_nominal + b_nominal
    spec_worst = a_worst + b_worst
    nominal = spec_nominal + tight_nominal
    worst = spec_worst + tight_worst
    return {
        "nominal": nominal,
        "worst": worst,
        "spec_nominal": spec_nominal,
        "spec_worst": spec_worst,
        "budget": MODEL_CALL_CAP,
        "nominal_fits": nominal <= MODEL_CALL_CAP,
        "worst_fits": worst <= MODEL_CALL_CAP,
        "spec_fits": spec_worst <= MODEL_CALL_CAP,
        "breakdown": {
            "A": {"cases": a_cases, "conditions": 1, "nominal": a_nominal, "worst": a_worst},
            "B": {"cases": b_cases, "conditions": 2, "nominal": b_nominal, "worst": b_worst},
            "B-tight": {
                "cases": tight_cases,
                "conditions": 1,
                "nominal": tight_nominal,
                "worst": tight_worst,
            },
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


async def _resolve_scope(db: Any, *, tenant_id: str, kb_ids: Sequence[str]) -> list[str]:
    """这次评测覆盖哪些库（读库行确认它们确实在）：范围只剩「库」这一维。

    库级索引版本删除之后，检索范围不再要求「每库一个生效版本」——按篇级代对齐就够了，
    所以这里返回的是**库 id 列表**，不是 ``kb_id → 版本`` 的映射。
    """

    from sqlalchemy import select

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
    return [str(kb.id) for kb in rows]




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

    两套语料的关键词索引不同（各有一个独立的索引名），所以**按语料分两趟**：换索引名
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
            kb_scope = await _resolve_scope(
                db, tenant_id=scope["tenant_id"], kb_ids=scope["kb_ids"]
            )
            stems = await _document_stems(db, tenant_id=scope["tenant_id"], kb_ids=scope["kb_ids"])
            degraded: list[str] = []
            search = build_kernel_search(
                db,
                tenant_id=scope["tenant_id"],
                kb_ids=list(kb_scope),
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
                "kb_scope": kb_scope,
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
    a_case_set = a_cases(archive)
    predicted = predict_model_calls(b_cases=len(cases), a_cases=len(a_case_set))
    return {
        "predicted": predicted,
        "cases": cases,
        "a_cases": a_case_set,
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
    if not predicted["spec_fits"]:
        lines.append(
            f"⚠️ 票面设计的最坏预估 {predicted['spec_worst']} 超过硬顶 {predicted['budget']}："
            "需要减题（见报告）"
        )
    elif not predicted["worst_fits"]:
        lines.append(
            f"（追加条件 B-tight 让名义/最坏纸面到 {predicted['nominal']}/"
            f"{predicted['worst']}，超过硬顶 {predicted['budget']}——硬顶按**实际**调用扣账，"
            "预算闸按最坏预留，实际用量见报告）"
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


# ─────────────────────────────────────────────────────────── 救回三分类


@dataclass(frozen=True)
class RescueClaim:
    """一个**机会要点**的判决：复查前是 ``unknown``，复查之后落成了什么、凭什么落的。

    ``matched`` 是本票那道「标准答案」闸的输出：证据里**落在 expected 里**的材料标签。
    它有内容 ⇒ 真救回；`supported` 但它是空的 ⇒ 假救回（模型变乐观了，负向读数）。

    ``evidence_rounds`` 记每条证据是**第几轮**检索带回来的（1 = 首轮，2 = 补证）：报告要能
    说清「这个救回是靠补证捞到的材料，还是靠首轮就在池子里的材料」——后者其实是「复查读到了
    首轮漏判的材料」，与「补证把材料捞回来」不是同一件事，读的时候要能分开。
    """

    case: str = ""
    point_id: str = ""
    point_text: str = ""
    pre_status: str = SUPPORT_UNKNOWN
    final_status: str = SUPPORT_UNKNOWN
    evidence_ids: tuple[str, ...] = ()
    evidence: tuple[MaterialRef, ...] = ()
    matched: tuple[str, ...] = ()
    evidence_rounds: tuple[int, ...] = ()
    outcome: str = RESCUE_UNRESOLVED

    def as_json(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "point_id": self.point_id,
            "point": self.point_text,
            "pre_status": self.pre_status,
            "final_status": self.final_status,
            "evidence_ids": list(self.evidence_ids),
            "evidence": [item.as_json() for item in self.evidence],
            "evidence_labels": [item.label for item in self.evidence],
            "evidence_rounds": list(self.evidence_rounds),
            "matched_expected": list(self.matched),
            "outcome": self.outcome,
        }


CoverageRow = tuple[str, str, tuple[str, ...]]


def coverage_rows(coverage: Sequence[Any]) -> list[CoverageRow]:
    """把「逐要点判定」收成三元组——接受 :class:`PointVerdict`、字典或三元组本身。

    三个来源（生产对象、落盘记录、用例构造）都要能喂进同一个分类函数，否则「口径只有一处」
    就会退化成「每个调用点各写一份」。
    """

    rows: list[CoverageRow] = []
    for item in coverage:
        if isinstance(item, tuple) and len(item) == 3:
            rows.append((str(item[0]), str(item[1]), tuple(str(e) for e in item[2])))
            continue
        if isinstance(item, Mapping):
            point_id = str(item.get("point_id") or "")
            status = str(item.get("status") or "")
            evidence = tuple(str(e) for e in item.get("evidence_ids") or ())
            rows.append((point_id, status, evidence))
            continue
        rows.append(
            (
                str(getattr(item, "point_id", "")),
                str(getattr(item, "status", "")),
                tuple(str(e) for e in getattr(item, "evidence_ids", ()) or ()),
            )
        )
    return rows


def evidence_materials(
    evidence_ids: Sequence[str],
    materials: Sequence[MaterialRef],
    *,
    rounds: Sequence[int] | None = None,
) -> tuple[tuple[MaterialRef, ...], tuple[int, ...]]:
    """``E1`` / ``E3`` → 材料池里那两条材料（1-based，与引用块同一套编号）。

    越界的编号丢掉而不是猜一个：编号制度是「同一份材料池内不复用、不重排」，越界只可能是
    记录坏了，猜一个等于替它编一条证据。
    """

    picked: list[MaterialRef] = []
    picked_rounds: list[int] = []
    for evidence_id in evidence_ids:
        text = str(evidence_id)
        if not text.startswith("E") or not text[1:].isdigit():
            continue
        index = int(text[1:]) - 1
        if not 0 <= index < len(materials):
            continue
        picked.append(materials[index])
        if rounds is not None and index < len(rounds):
            picked_rounds.append(int(rounds[index]))
    return tuple(picked), tuple(picked_rounds)


def classify_unknown_points(
    *,
    unknown: Sequence[str],
    coverage: Sequence[Any],
    materials: Sequence[MaterialRef],
    expected: Sequence[Expected],
    texts: Mapping[str, str] | None = None,
    rounds: Sequence[int] | None = None,
    case: str = "",
) -> list[RescueClaim]:
    """**本票唯一的分类处**：把「复查前落 unknown 的要点」逐条判成三类（+ 仍未知）。

    判据只有两条，都机械可查：

    1. 复查后的状态（``supported`` / ``none`` / 仍是 ``unknown``）；
    2. 若是 ``supported``，它的 ``evidence_ids`` 里**有没有**一条材料的章节落在 ``expected``
       里（:func:`material_matches`）。

    不做人工复核，也不「看一眼觉得像」——那正是本票要防的那件事。
    """

    final = {point_id: (status, evidence) for point_id, status, evidence in coverage_rows(coverage)}
    claims: list[RescueClaim] = []
    for point_id in unknown:
        status, evidence_ids = final.get(point_id, (SUPPORT_UNKNOWN, ()))
        picked, picked_rounds = evidence_materials(evidence_ids, materials, rounds=rounds)
        matched: list[str] = []
        if status == SUPPORT_SUPPORTED:
            for material in picked:
                if any(material_matches(item, material) for item in expected):
                    matched.append(material.label)
        if status == SUPPORT_SUPPORTED:
            outcome = RESCUE_TRUE if matched else RESCUE_FALSE
        elif status == SUPPORT_NONE:
            outcome = RESCUE_NOT
        else:
            outcome = RESCUE_UNRESOLVED
        claims.append(
            RescueClaim(
                case=case,
                point_id=point_id,
                point_text=(texts or {}).get(point_id, ""),
                final_status=status,
                evidence_ids=tuple(evidence_ids),
                evidence=picked,
                matched=tuple(dict.fromkeys(matched)),
                evidence_rounds=picked_rounds,
                outcome=outcome,
            )
        )
    return claims


def _rate(numerator: int, denominator: int) -> float | None:
    """``None`` 而不是 0.0：分母为 0 时「没有读数」，不是「读数等于零」。"""

    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def rescue_summary(claims: Sequence[RescueClaim], *, cases: int) -> dict[str, Any]:
    """三分类的计数与比率。**分母只有一个**：机会数（要点级）。

    「无机会」按**题**数：一题里没有任何要点落 ``unknown``（补证白跑一趟、或首轮就没有缺口），
    这题就没有可复查的对象——它解释的是「机会造得少不少」，不是「救回率高低」。
    """

    counts = {name: 0 for name in RESCUE_OUTCOMES}
    counts[RESCUE_UNRESOLVED] = 0
    for claim in claims:
        counts[claim.outcome] = counts.get(claim.outcome, 0) + 1
    opportunity_points = sum(counts[name] for name in RESCUE_OUTCOMES)
    cases_with_claim = {claim.case for claim in claims if claim.outcome in RESCUE_OUTCOMES}
    rates = {name: _rate(counts[name], opportunity_points) for name in RESCUE_OUTCOMES}
    return {
        "opportunity_points": opportunity_points,
        "opportunity_cases": len(cases_with_claim),
        "cases": cases,
        "no_chance_cases": max(0, cases - len(cases_with_claim)),
        "counts": counts,
        "rates": rates,
        "denominator": "opportunity_points",
    }


# ─────────────────────────────────────────────────────────── ⑤ 的接缝（只在评测进程内）


def _snapshot_of(run: Any) -> dict[str, Any]:
    """复查前的那一格：``coverage`` 与材料条数（**只读**）。"""

    return {
        "coverage": [
            (item.point_id, item.status, tuple(item.evidence_ids)) for item in run.coverage
        ],
        "materials": len(run.materials),
        "points": [(point.point_id, point.text) for point in run.points],
        "answered_queries": len(run.queries),
    }


class _RejudgeControl:
    """把 ``research._Run._rejudge`` 换成「关掉」或「直通观察」，退出时**一律还原**。

    为什么动类属性而不是给 `app/` 加一个开关：那会在生产代码里留一条「默认关、生产永不发生」
    的分支，正是 ADR-0029 点名的死代码。为什么不是实例属性：本票走的是
    :func:`app.services.research.run_research`（与生产同一条入口），实例由它自己造。

    两种模式都必须**先拍照再动手**：``off`` 之后 `unknown` 就没人改了，快照是这一趟唯一的
    「复查前」记录；``spy`` 则把快照与原始调用串起来（只读，不改状态）。
    """

    def __init__(self, mode: str) -> None:
        if mode not in ("off", "spy"):
            raise ValueError(f"未知的模式：{mode!r}")
        self.mode = mode
        self.sink: list[dict[str, Any]] = []
        self._original: Any = None

    def __enter__(self) -> list[dict[str, Any]]:
        from app.services import research as research_service

        self._original = research_service._Run._rejudge
        original = self._original
        mode = self.mode
        sink = self.sink

        async def _hook(run: Any) -> None:
            sink.append(_snapshot_of(run))
            if mode == "spy":
                await original(run)
            return None

        research_service._Run._rejudge = _hook  # type: ignore[method-assign]
        return self.sink

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        from app.services import research as research_service

        research_service._Run._rejudge = self._original  # type: ignore[method-assign]


def rejudge_control(*, mode: str) -> _RejudgeControl:
    """``mode="spy"``：⑤ 开着，顺便留下复查前的快照；``mode="off"``：⑤ 关掉（对照条件）。"""

    return _RejudgeControl(mode)


# ─────────────────────────────────────────────────────────── 跑批（生产完整链路）


async def _bind_model_chain() -> dict[str, Any]:
    """把模型接入接到**生产那条链**（``model_configs`` 的角色指针），返回用的是哪一条。

    不接会怎样：走 ``.env`` 的单供应商 key，与库里配的那条不是同一个账号，每道题都会在
    生成阶段失败，读数看起来像「救回不行」——#153 / #165 / #167 都在评测侧踩过同一脚。
    """

    from app.core.config import settings
    from app.providers.llm import set_role_resolver
    from app.services import model_config_service

    set_role_resolver(model_config_service.role_endpoint)
    endpoint = await model_config_service.role_endpoint("fast")
    if endpoint is None:
        return {"llm_endpoint_source": "env", "llm_model": str(settings.LLM_MODEL)}
    return {
        "llm_endpoint_source": "model_configs",
        "llm_model": str(endpoint.model),
        "llm_config_id": str(endpoint.config_id),
    }


async def _fast_provider() -> Any:
    from app.providers.llm import get_llm_provider

    return await get_llm_provider("fast")


async def _strong_provider() -> Any:
    from app.providers.llm import get_llm_provider

    return await get_llm_provider("strong")


def infer_material_rounds(
    calls: Sequence[Mapping[str, Any]],
    *,
    round_of_query: Mapping[str, int],
) -> tuple[list[int], list[dict[str, Any]]]:
    """按**调用顺序**重放检索，给每条材料标上「第几轮进来的」，并算出每条查询新增了几条。

    为什么能这样重放：``_Run._parallel`` 用 ``asyncio.gather`` 跑一批查询，合并顺序与入参
    顺序一致；``_merge`` 又是「按节去重、先到先得」。于是**调用顺序 + 返回顺序**完全决定了
    材料池的顺序——与生产里那张表逐格同序，不需要往 `app/` 里加任何记账。
    """

    seen: set[str] = set()
    rounds: list[int] = []
    per_call: list[dict[str, Any]] = []
    for call in calls:
        query = str(call.get("query") or "")
        round_no = int(round_of_query.get(normalize_heading(query), 1))
        added = 0
        returned: list[str] = []
        for row in call.get("rows") or ():
            key = str(row.get("section_id") or row.get("chunk_id") or "")
            if not key:
                continue
            returned.append(key)
            if key in seen:
                continue
            seen.add(key)
            rounds.append(round_no)
            added += 1
        per_call.append({"query": query, "round": round_no, "added": added, "returned": returned})
    return rounds, per_call


def _insufficient_blocks(citations: Sequence[Mapping[str, Any]]) -> int:
    """答案里「材料不足」块的数量（与引用块同一套词：``status == insufficient``）。"""

    return sum(1 for block in citations if str(block.get("status")) == STATUS_INSUFFICIENT)


async def _chunk_ids_present(db: Any, chunk_ids: Sequence[str]) -> dict[str, int]:
    """归档里的 ``chunk_id`` 现在还查得到几条——归档是不是还指着同一批材料。"""

    from sqlalchemy import bindparam, text

    if not chunk_ids:
        return {"archived": 0, "present": 0}
    stmt = text("select count(*) from chunks where id in :ids").bindparams(
        bindparam("ids", expanding=True)
    )
    present = (await db.execute(stmt, {"ids": list(chunk_ids)})).scalar()
    return {"archived": len(chunk_ids), "present": int(present or 0)}


async def run_case(
    case: EvalCase,
    *,
    fivefold: bool,
    kb_scope: Sequence[str],
    stems: Mapping[str, str],
    archive: Mapping[str, Any] | None = None,
    condition: str | None = None,
) -> dict[str, Any]:
    """跑**生产完整链路**一次（真实检索内核 + 真实模型 + 完整状态机），返回一行台账。

    每题一个会话：一次 Research 会跑 1~5 次子检索，长跑批共用一个会话会让连接在「既不在事务
    里也没人用」的状态上挂十几分钟——那是评测机的运气，不是被测对象的行为。
    """

    from app.db.async_session import AsyncSessionLocal
    from app.services.research import run_research
    from app.services.research_gate import build_kernel_search

    scope = _scope_of(case.source)
    _apply_eval_settings(keyword_alias=scope["keyword_alias"])
    degraded: list[str] = []
    calls: list[dict[str, Any]] = []
    archive_evidence: dict[str, Any] = {}

    with rejudge_control(mode="spy" if fivefold else "off") as sink:
        async with AsyncSessionLocal() as db:
            inner = build_kernel_search(
                db,
                tenant_id=scope["tenant_id"],
                kb_ids=list(kb_scope),
                options=_retrieval_options(case.top_k),
                on_degraded=degraded.append,
            )

            async def search(sub_query: str) -> list[dict[str, Any]]:
                index = len(calls)
                calls.append({"query": sub_query, "rows": []})
                rows = [dict(row) for row in await inner(sub_query)]
                calls[index]["rows"] = rows
                return rows

            outcome = await run_research(
                query=case.query,
                tenant_id=scope["tenant_id"],
                search=search,
                llm_factory=_fast_provider,
                strong_llm_factory=_strong_provider,
                sub_search_degraded=degraded,
            )
            if archive is not None:
                item = (archive.get("questions") or {}).get(case.origin_id) or {}
                hits = [str(row.get("chunk_id") or "") for row in item.get("hits") or ()]
                archive_evidence = {
                    "run": archive.get("run"),
                    "stop_reason": item.get("stop_reason"),
                    "hits": len(hits),
                    **await _chunk_ids_present(db, [item for item in hits if item]),
                }

    return case_record(
        case,
        outcome,
        fivefold=fivefold,
        calls=calls,
        sink=sink,
        stems=stems,
        archive_evidence=archive_evidence,
        condition=condition,
    )


RECORD_KEYS: Final[tuple[str, ...]] = (
    "case",
    "group",
    "source",
    "origin",
    "condition",
    "fivefold",
    "query",
    "top_k",
    "expected",
    "points",
    "materials",
    "materials_in_round1",
    "round1_expected_found",
    "round1_expected_missing",
    "followup_calls",
    "rounds_inferred",
    "coverage_pre_rejudge",
    "unknown_ids",
    "coverage",
    "gaps",
    "claims",
    "eventual_outcomes",
    "searches",
    "followup_rounds",
    "model_calls",
    "stop_reason",
    "answered",
    "answer_rejected_reason",
    "insufficient",
    "evidence_state",
    "sub_search_degraded",
    "duration_ms",
    "archive",
)


def case_record(
    case: EvalCase,
    outcome: Any,
    *,
    fivefold: bool,
    calls: Sequence[Mapping[str, Any]],
    sink: Sequence[Mapping[str, Any]],
    stems: Mapping[str, str],
    archive_evidence: Mapping[str, Any] | None = None,
    condition: str | None = None,
) -> dict[str, Any]:
    """一次 Research 的读数 → 一行台账（报告与 results.json 共用这一份）。"""

    materials = [material_ref(row, stems=stems) for row in outcome.materials]
    round_of_query = {normalize_heading(item.query): int(item.round) for item in outcome.queries}
    rounds, per_call = infer_material_rounds(calls, round_of_query=round_of_query)
    rounds_inferred = len(rounds) == len(materials)
    if not rounds_inferred:
        rounds = [1] * len(materials)

    pre = dict(sink[0]) if sink else {}
    unknown_ids = [
        str(point_id)
        for point_id, status, _ in pre.get("coverage") or ()
        if status == SUPPORT_UNKNOWN
    ]
    texts = {str(point_id): str(text) for point_id, text in pre.get("points") or ()}
    if not texts:
        texts = {point.point_id: point.text for point in outcome.points}

    claims = classify_unknown_points(
        unknown=unknown_ids,
        coverage=outcome.coverage,
        materials=materials,
        expected=case.expected,
        texts=texts,
        rounds=rounds,
        case=case.case_id,
    )
    round1_materials = [item for item, round_no in zip(materials, rounds) if round_no == 1]
    missing_round1 = missing_expected(case.expected, round1_materials)
    found_round1 = [item for item in case.expected if item not in missing_round1]
    citations = [dict(citation) for citation in outcome.citations]
    record = {
        "case": case.case_id,
        "group": case.group,
        "source": case.source,
        "origin": case.origin_id,
        "condition": condition or (CONDITION_FIVEFOLD if fivefold else CONDITION_CONTROL),
        "fivefold": fivefold,
        "query": case.query,
        "top_k": case.top_k,
        "expected": [item.as_json() for item in case.expected],
        "points": [{"point_id": point.point_id, "text": point.text} for point in outcome.points],
        "materials": len(materials),
        "materials_in_round1": len(round1_materials),
        "round1_expected_found": [item.label for item in found_round1],
        "round1_expected_missing": [item.label for item in missing_round1],
        "followup_calls": [call for call in per_call if call["round"] == 2],
        "rounds_inferred": rounds_inferred,
        "coverage_pre_rejudge": [
            {"point_id": point_id, "status": status, "evidence_ids": list(evidence)}
            for point_id, status, evidence in pre.get("coverage") or ()
        ],
        "unknown_ids": unknown_ids,
        "coverage": [
            {
                "point_id": item.point_id,
                "status": item.status,
                "evidence_ids": list(item.evidence_ids),
            }
            for item in outcome.coverage
        ],
        "gaps": list(outcome.gaps),
        "claims": [claim.as_json() for claim in claims],
        "eventual_outcomes": rescue_summary(claims, cases=1),
        "searches": int(outcome.searches),
        "followup_rounds": int(outcome.followup_rounds),
        "model_calls": int(outcome.model_calls),
        "stop_reason": str(outcome.stop_reason.value),
        "answered": outcome.answer is not None,
        "answer_rejected_reason": outcome.answer_rejected_reason,
        "insufficient": _insufficient_blocks(citations),
        "evidence_state": outcome.evidence_state,
        "sub_search_degraded": list(outcome.sub_search_degraded),
        "duration_ms": int(outcome.duration_ms),
        "archive": dict(archive_evidence or {}),
    }
    assert set(record) == set(RECORD_KEYS), set(record) ^ set(RECORD_KEYS)
    return record


async def run_cases(
    cases: Sequence[EvalCase],
    *,
    budget: Budget,
    fivefold: bool,
    kb_scope_by_source: Mapping[str, Mapping[str, str]],
    stems_by_source: Mapping[str, Mapping[str, str]],
    archive: Mapping[str, Any] | None = None,
    condition: str | None = None,
) -> list[dict[str, Any]]:
    """逐题跑，**先预留再发**：预留不到就不跑下一题（预算是一条上限，不是一句愿望）。"""

    records: list[dict[str, Any]] = []
    for case in cases:
        if not budget.can(WORST_CALLS_PER_CASE):
            condition = CONDITION_FIVEFOLD if fivefold else CONDITION_CONTROL
            budget.skip(f"{case.case_id}/{condition}", WORST_CALLS_PER_CASE)
            continue
        record = await run_case(
            case,
            fivefold=fivefold,
            kb_ids=list(kb_scope_by_source[case.source]),
            stems=stems_by_source[case.source],
            archive=archive,
            condition=condition,
        )
        budget.charge(int(record["model_calls"]))
        records.append(record)
        summary = record["eventual_outcomes"]
        print(
            f"  {record['case']}（{record['condition']}）: stop={record['stop_reason']} "
            f"unknown={len(record['unknown_ids'])} 机会={summary['opportunity_points']} "
            f"真/假/未={summary['counts'][RESCUE_TRUE]}/{summary['counts'][RESCUE_FALSE]}"
            f"/{summary['counts'][RESCUE_NOT]} calls={record['model_calls']}"
            f" rejected={record['answer_rejected_reason']}",
            flush=True,
        )
    return records


def recompute_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    """按当前口径重算所有指标（**0 次模型调用**）：产物与人读报告的唯一来源。"""

    records = list(payload.get("records") or [])

    def claims_of(items: Sequence[Mapping[str, Any]]) -> list[RescueClaim]:
        out: list[RescueClaim] = []
        for record in items:
            for claim in record.get("claims") or ():
                out.append(
                    RescueClaim(
                        case=str(claim.get("case") or ""),
                        point_id=str(claim.get("point_id") or ""),
                        point_text=str(claim.get("point") or ""),
                        pre_status=str(claim.get("pre_status") or SUPPORT_UNKNOWN),
                        final_status=str(claim.get("final_status") or SUPPORT_UNKNOWN),
                        evidence_ids=tuple(str(e) for e in claim.get("evidence_ids") or ()),
                        matched=tuple(str(e) for e in claim.get("matched_expected") or ()),
                        evidence_rounds=tuple(int(r) for r in claim.get("evidence_rounds") or ()),
                        outcome=str(claim.get("outcome") or RESCUE_UNRESOLVED),
                    )
                )
        return out

    def in_condition(record: Mapping[str, Any]) -> str:
        return str(record.get("condition") or "")

    # 三档按 `condition` 分，不按 `group`：B-tight 与 B 组**是同一批题**（问的问题、标准答案
    # 都一样），差别只有 `top_k`。按 group 分会把追加条件混进主读数——那正是「口径漂移」。
    a_on = [
        r for r in records if r.get("group") == GROUP_A and in_condition(r) == CONDITION_FIVEFOLD
    ]
    b_on = [
        r for r in records if r.get("group") == GROUP_B and in_condition(r) == CONDITION_FIVEFOLD
    ]
    b_off = [r for r in records if in_condition(r) == CONDITION_CONTROL]
    b_tight = [r for r in records if in_condition(r) == CONDITION_TIGHT]

    calls_by_condition: dict[str, int] = {}
    for record in records:
        key = in_condition(record) or "unknown"
        calls_by_condition[key] = calls_by_condition.get(key, 0) + int(
            record.get("model_calls") or 0
        )

    def group_block(
        items: Sequence[Mapping[str, Any]], *, rescue_applicable: bool = True
    ) -> dict[str, Any]:
        claims = claims_of(items)
        summary = rescue_summary(claims, cases=len(items))
        # ⑤ 关掉时**不存在「复查之后的判定」**，所以三分类不适用：那 N 个机会要点全部停在
        # `unknown`（不进 gaps、也没人判）。这一格不报「救回率」，只报「机会数」与它停在哪儿。
        summary["rescue_applicable"] = rescue_applicable
        if not rescue_applicable:
            summary["rates"] = {name: None for name in RESCUE_OUTCOMES}
        summary["unknown_points"] = sum(len(item.get("unknown_ids") or ()) for item in items)
        summary["points_total"] = sum(len(item.get("points") or ()) for item in items)
        summary["cases_with_followup"] = sum(
            1 for item in items if int(item.get("followup_rounds") or 0) > 0
        )
        summary["gaps_total"] = sum(len(item.get("gaps") or ()) for item in items)
        summary["answered"] = sum(1 for item in items if item.get("answered"))
        summary["insufficient_total"] = sum(int(item.get("insufficient") or 0) for item in items)
        summary["model_calls"] = sum(int(item.get("model_calls") or 0) for item in items)
        reasons: dict[str, int] = {}
        for item in items:
            reason = no_chance_reason(item)
            if reason == "有机会":
                continue
            reasons[reason] = reasons.get(reason, 0) + 1
        summary["no_chance_reasons"] = reasons
        return summary

    metrics: dict[str, Any] = {
        "A": group_block(a_on),
        "B": group_block(b_on),
        "B_control": group_block(b_off, rescue_applicable=False),
        "B_tight": group_block(b_tight),
        "model_calls": {
            "actual": sum(int(item.get("model_calls") or 0) for item in records),
            "by_condition": calls_by_condition,
        },
    }
    control_unknown = sum(len(item.get("unknown_ids") or ()) for item in b_off)
    metrics["B_control"]["unknown_points"] = control_unknown
    metrics["B_control"]["unknown_cases"] = sum(1 for item in b_off if item.get("unknown_ids"))
    payload["metrics"] = metrics
    # 预估也按**记录里实际跑了几题**重算：`recompute` 不重跑，但「票面设计 / 追加条件」两栏
    # 的名字必须跟着产物走，否则回填之后报告头会印出上一趟留下的旧数（本票踩过）。
    payload["predicted_calls"] = predict_model_calls(
        a_cases=len({str(r.get("case")) for r in a_on}),
        b_cases=len({str(r.get("case")) for r in b_on}),
        tight_cases=len({str(r.get("case")) for r in b_tight}),
    )
    # 顶层这一格也一起刷新：`recompute` 不重跑，但「实际调用次数」必须跟着记录走，
    # 否则报告里的记账会停在上一趟的数上（本票踩过：回填之后仍显示旧的条件分账）。
    previous = dict(payload.get("model_calls") or {})
    previous.update(metrics["model_calls"])
    previous["budget"] = MODEL_CALL_CAP
    previous["predicted"] = payload["predicted_calls"]
    payload["model_calls"] = previous
    return payload


# ─────────────────────────────────────────────────────────── 落盘


def a_cases(archive: Mapping[str, Any]) -> list[EvalCase]:
    """A 组那 3 题：题面来自归档，标准答案来自 refs 题集（同一把尺子）。"""

    refs_by_id = {candidate.origin_id: candidate for candidate in load_refs_candidates()}
    return [
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


async def _scopes_by_source() -> tuple[
    dict[str, dict[str, str]], dict[str, dict[str, str]], dict[str, Any]
]:
    """两套语料各自的检索库与文档词干（一次进程只查一遍）。"""

    from app.db.async_session import AsyncSessionLocal

    kb_scope_by_source: dict[str, dict[str, str]] = {}
    stems_by_source: dict[str, dict[str, str]] = {}
    environment: dict[str, Any] = {"sources": {}}
    async with AsyncSessionLocal() as db:
        for source in (REFS_SOURCE, EVAL_SOURCE):
            scope = _scope_of(source)
            _apply_eval_settings(keyword_alias=scope["keyword_alias"])
            kb_scope = await _resolve_scope(
                db, tenant_id=scope["tenant_id"], kb_ids=scope["kb_ids"]
            )
            kb_scope_by_source[source] = kb_scope
            stems_by_source[source] = await _document_stems(
                db, tenant_id=scope["tenant_id"], kb_ids=scope["kb_ids"]
            )
            environment["sources"][source] = {
                "tenant_id": scope["tenant_id"],
                "kb_ids": scope["kb_ids"],
                "keyword_alias": scope["keyword_alias"],
                "kb_scope": kb_scope,
                "documents": len(stems_by_source[source]),
            }
    return kb_scope_by_source, stems_by_source, environment


async def run_all(*, groups: Sequence[str], out_dir: Path) -> dict[str, Any]:
    """跑指定的组并落盘（产物可增量补：先跑 A，再跑 B，前面的记录原样保留）。"""

    archive = load_archived_max_rounds()
    check_archived_premise(archive)
    predicted = predict_model_calls()
    print(
        f"预计模型调用：名义 {predicted['nominal']} 次 / 最坏 {predicted['worst']} 次"
        f"（硬顶 {predicted['budget']}）"
    )
    chain = await _bind_model_chain()
    kb_scope_by_source, stems_by_source, environment = await _scopes_by_source()

    probes: list[Probe] = []
    cases_b: list[EvalCase] = []
    if GROUP_B in groups or GROUP_B_TIGHT in groups:
        probes, probe_environment = await collect_probes()
        environment["probe"] = probe_environment
        cases_b = build_cases(probes)
        print(
            f"B 组选题：{[(case.case_id, case.origin_id) for case in cases_b]}"
            f"（候选 {len(probes)} 题，expected 在 top-{B_TOP_K} 下缺席的 "
            f"{sum(1 for probe in probes if probe.qualified)} 题）"
        )
    cases_a = a_cases(archive)
    checks = check_case_set(list(cases_a) + list(cases_b), probes=probes or None)

    budget = Budget()
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.json"
    previous: dict[str, Any] = {}
    if results_path.exists():
        previous = json.loads(results_path.read_text(encoding="utf-8"))

    payload: dict[str, Any] = dict(previous)
    payload.update(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "issue": "#168 ⑤ 的「救回」这一面",
            "llm_chain": chain,
            "environment": environment,
            "predicted_calls": predicted,
            "checks": checks,
            "archive": {
                "run": archive.get("run"),
                "generated_at": archive.get("generated_at"),
                "kb_scope": archive.get("kb_scope"),
                "llm_chain": archive.get("llm_chain"),
                "max_rounds_ids": archive.get("max_rounds_ids"),
            },
            "archive_gaps": archive_gaps(archive),
            "cases": [
                {
                    "case_id": case.case_id,
                    "group": case.group,
                    "source": case.source,
                    "origin": case.origin_id,
                    "query": case.query,
                    "top_k": case.top_k,
                    "expected": [item.as_json() for item in case.expected],
                    "note": case.note,
                    "mechanism": case.mechanism,
                }
                for case in list(cases_a) + list(cases_b)
            ],
            "probes": [probe.as_json() for probe in probes],
        }
    )

    records = list(previous.get("records") or [])
    if GROUP_A in groups:
        records = [record for record in records if record.get("group") != GROUP_A]
        print(f"跑 A 组（{CONDITION_FIVEFOLD}）：{[case.case_id for case in cases_a]}", flush=True)
        records.extend(
            await run_cases(
                cases_a,
                budget=budget,
                fivefold=True,
                kb_scope_by_source=kb_scope_by_source,
                stems_by_source=stems_by_source,
                archive=archive,
            )
        )
    if GROUP_B in groups:
        records = [record for record in records if record.get("group") != GROUP_B]
        for fivefold in (True, False):
            condition = CONDITION_FIVEFOLD if fivefold else CONDITION_CONTROL
            print(f"跑 B 组（{condition}）：{[case.case_id for case in cases_b]}", flush=True)
            records.extend(
                await run_cases(
                    cases_b,
                    budget=budget,
                    fivefold=fivefold,
                    kb_scope_by_source=kb_scope_by_source,
                    stems_by_source=stems_by_source,
                    archive=archive,
                )
            )
    if GROUP_B_TIGHT in groups:
        # **追加条件**（lead 未要求，实施中追加，理由见模块文档末尾）：同一批 B 组题、同一套
        # 流程，只把 top_k 压到 1。它证明的是「机会数=0 不是深度还不够小」这件事，
        # 而不是为了让某个率好看——它的假救回同样计入。
        tight_cases = [replace(case, top_k=TIGHT_TOP_K) for case in cases_b]
        records = [record for record in records if record.get("condition") != CONDITION_TIGHT]
        print(
            f"跑 B-tight（{CONDITION_TIGHT}，top_k={TIGHT_TOP_K}）："
            f"{[case.case_id for case in tight_cases]}",
            flush=True,
        )
        records.extend(
            await run_cases(
                tight_cases,
                budget=budget,
                fivefold=True,
                kb_scope_by_source=kb_scope_by_source,
                stems_by_source=stems_by_source,
                archive=archive,
                condition=CONDITION_TIGHT,
            )
        )
    order = {
        (case.case_id, condition): index
        for index, (case, condition) in enumerate(
            (case, condition)
            for case in list(cases_a) + list(cases_b)
            for condition in (CONDITION_FIVEFOLD, CONDITION_CONTROL, CONDITION_TIGHT)
        )
    }
    records.sort(
        key=lambda record: order.get((str(record.get("case")), str(record.get("condition"))), 999)
    )
    payload["records"] = records
    payload["groups_run"] = sorted({str(record.get("condition")) for record in records})
    payload = recompute_metrics(payload)
    model_calls = dict(payload["metrics"]["model_calls"])
    model_calls["skipped"] = list(budget.skipped)
    model_calls["predicted"] = predicted
    model_calls["budget"] = MODEL_CALL_CAP
    payload["model_calls"] = model_calls

    results_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "report.md").write_text(render_report(payload), encoding="utf-8")
    print(f"实际模型调用：{model_calls['actual']} 次（硬顶 {MODEL_CALL_CAP}）")
    if budget.skipped:
        print("因预算跳过：" + "；".join(budget.skipped))
    print(f"产物：{results_path} / {out_dir / 'report.md'}")
    return payload


def _recompute(out_dir: Path) -> int:
    """不重跑、只重算：改统计口径不该重花那几十次调用。"""

    results_path = out_dir / "results.json"
    if not results_path.exists():
        print(f"没有产物可重算：{results_path}")
        return 1
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    payload = recompute_metrics(payload)
    results_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "report.md").write_text(render_report(payload), encoding="utf-8")
    print(f"已重算：实际模型调用 {payload['metrics']['model_calls']['actual']} 次（未新增调用）")
    return 0


# ─────────────────────────────────────────────────────────── 报告


def _posix(path: Any) -> str:
    """路径在报告里一律用 ``/``：Windows 的反斜杠在 markdown 里会被吃掉一个，读者复制不走。"""

    return str(path or "").replace("\\", "/")


def _fmt_rate(value: Any) -> str:
    """``None`` 印成 ``—``：「没有读数」不许被印成 0%。"""

    if value is None:
        return "—"
    return f"{float(value) * 100:.1f}%"


def _fmt_counts(block: Mapping[str, Any]) -> str:
    counts = block["counts"]
    return (
        f"真 {counts[RESCUE_TRUE]} / 假 {counts[RESCUE_FALSE]} / 未 {counts[RESCUE_NOT]}"
        f" / 仍未知 {counts[RESCUE_UNRESOLVED]}"
    )


def _rate_row(block: Mapping[str, Any], outcome: str) -> str:
    counts = block["counts"]
    return (
        f"| {RESCUE_LABELS[outcome]} | {counts[outcome]} | {block['opportunity_points']} | "
        f"{_fmt_rate(block['rates'][outcome])} |"
    )


def _rescue_table(block: Mapping[str, Any], *, title: str) -> list[str]:
    lines = [
        f"**{title}**",
        "",
        "| 分类 | 要点数 | 分母（机会数） | 占机会数 |",
        "| --- | --- | --- | --- |",
        _rate_row(block, RESCUE_TRUE),
        _rate_row(block, RESCUE_FALSE),
        _rate_row(block, RESCUE_NOT),
        f"| （仍 unknown，不进分母） | {block['counts'][RESCUE_UNRESOLVED]} | — | — |",
        "",
        f"机会数（要点）**{block['opportunity_points']}** ⏐ 有机会的题 **{block['opportunity_cases']}"
        f"/{block['cases']}** ⏐ 无机会的题 **{block['no_chance_cases']}** ⏐ "
        f"落 unknown 的要点 **{block['unknown_points']}/{block['points_total']}**",
        "",
    ]
    return lines


def _in_condition(record: Mapping[str, Any], condition: str) -> bool:
    """这一行记录属不属于某个条件（`fivefold` / `control` / `tight`）。

    **只按 `condition` 判，不按 `fivefold` 判**：B-tight 也是 ⑤ 开着的跑，如果按 `fivefold`
    过滤，追加条件会混进主读数、甚至把同一题的行覆盖掉（配对表里「⑤ 开」那一列就会变成
    tight 的数）。这个坑本票踩过一次，用法是用例钉住的。
    """

    return str(record.get("condition") or "") == condition


def _case_detail_rows(records: Sequence[Mapping[str, Any]], *, condition: str) -> list[str]:
    lines = [
        "| 题 | 来源 | 停止原因 | 要点 | 首轮漏的标注 | unknown | 真/假/未 | 调用 | 答案 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        if not _in_condition(record, condition):
            continue
        counts = record["eventual_outcomes"]["counts"]
        answer = "非空" if record.get("answered") else "null"
        lines.append(
            f"| {record['case']} | {record['origin']} | `{record['stop_reason']}` "
            f"| {len(record['points'])} | {len(record['round1_expected_missing'])} "
            f"| {len(record['unknown_ids'])} "
            f"| {counts[RESCUE_TRUE]}/{counts[RESCUE_FALSE]}/{counts[RESCUE_NOT]} "
            f"| {record['model_calls']} | {answer} |"
        )
    return lines


def no_chance_reason(record: Mapping[str, Any]) -> str:
    """「无机会」的**成因**：每一种都不是「⑤ 没救回」，而是「没有可复查的对象」。

    这个拆分是第三问的关键，四种成因对「机会数为什么小」的解释完全不同：

    1. **首轮就命中全部 expected**：探针（原问题、top-2）比真跑（按要点拆开的多条查询）保守，
       漏召回没落在标注节上；
    2. **首轮漏了标注节，但判定仍判全部要点 `supported`**：判定用**别的节**撑住了它，于是
       补证根本没发生——这一种最值得注意，它说明「压 `top_k`」被**判定的乐观**抵消了；
    3. **补证空手**：补证跑了，但一条新材料都没捞到（捞回来的都是池子里已有的节）；
    4. **补证捞到材料但没绑到缺口要点**：`_settle_after_followup` 只给「自己的补证查询带回
       材料」的要点记 `unknown`，别的要点的 `none` 保持不变。
    """

    if record.get("unknown_ids"):
        return "有机会"
    missing = record.get("round1_expected_missing") or ()
    if not missing:
        return "首轮就命中全部 expected"
    coverage = record.get("coverage") or ()
    if record.get("stop_reason") == "evidence_complete" or (
        coverage and all(str(item.get("status")) == SUPPORT_SUPPORTED for item in coverage)
    ):
        return "首轮漏了标注节但判定仍判全部 supported"
    if not record.get("followup_calls"):
        return "判定报缺口但没有补证查询"
    if not sum(int(call.get("added") or 0) for call in record.get("followup_calls") or ()):
        return "补证空手（一条新材料都没捞到）"
    return "补证捞到材料但没绑到缺口要点"


def _runtime_miss_table(
    records: Sequence[Mapping[str, Any]], payload: Mapping[str, Any]
) -> list[str]:
    """**复核**：选题时的「expected 不在 top-2」在**真跑的首轮**里还成立吗？

    这一格必须报，因为两个「首轮」不是一回事：选题探针拿**原问题**问一次（1 条查询），真跑的
    首轮是规划器拆出来的**按要点多条**查询（≤3 条）。后者更容易把标注节捞回来，于是「压
    `top_k=2` 强制造漏召回」这个人造条件，在运行时**可能已经被规划器抵消掉了**。不报这一格，
    读者会以为 10 题都真的漏了。
    """

    probes = {
        (probe.get("source"), probe.get("origin_id")): probe
        for probe in payload.get("probes") or ()
    }
    lines = [
        "| 题 | 选题探针漏（原问题 top-2） | 真跑首轮漏（按要点 top-2） | 真跑首轮材料数 | 首轮材料里有没有别的节 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for record in records:
        if not _in_condition(record, CONDITION_FIVEFOLD) or record.get("group") != GROUP_B:
            continue
        probe = probes.get((record.get("source"), record.get("origin")), {})
        probe_missing = len(probe.get("missing_labels") or ())
        runtime_missing = len(record.get("round1_expected_missing") or ())
        others = max(
            0,
            int(record.get("materials_in_round1") or 0)
            - max(0, len(record.get("expected") or ()) - runtime_missing),
        )
        lines.append(
            f"| {record['case']}（{record['origin']}） | {probe_missing}/{len(probe.get('expected_labels') or ())} "
            f"| {runtime_missing}/{len(record.get('expected') or ())} "
            f"| {record.get('materials_in_round1')} | {others} |"
        )
    return lines


def _opportunity_breakdown(
    records: Sequence[Mapping[str, Any]], *, condition: str = CONDITION_FIVEFOLD
) -> list[str]:
    """机会数的**构成**：每一题里哪些要点落了 unknown、补证捞回来几条、复查后落在哪。

    第三问（「⑤ 确实很少救回」还是「我们把机会造得太少」）只能靠这张表回答——机会数是
    分子分母之外唯一能解释「为什么小」的东西。
    """

    lines: list[str] = []
    for record in records:
        if not _in_condition(record, condition) or record.get("group") != GROUP_B:
            continue
        unknown = set(record.get("unknown_ids") or ())
        if not unknown:
            calls = "；".join(
                f"{call['query'][:24]}…(+{call['added']})"
                for call in record.get("followup_calls") or ()
            )
            lines.append(
                f"- **{record['case']}**（{record['origin']}）：**无机会**"
                f"（{no_chance_reason(record)}；`{record['stop_reason']}`，"
                f"首轮缺 {len(record.get('round1_expected_missing') or ())} 个标注，"
                f"补证查询 {len(record.get('followup_calls') or ())} 条"
                f"{'：' + calls if calls else ''}）"
            )
            continue
        texts = {point["point_id"]: point["text"] for point in record["points"]}
        parts: list[str] = []
        for claim in record["claims"]:
            rounds = set(claim.get("evidence_rounds") or ())
            where = "补证材料" if 2 in rounds else ("首轮材料" if 1 in rounds else "无材料")
            parts.append(
                f"{claim['point_id']}「{(texts.get(claim['point_id']) or '')[:18]}」"
                f"→ `{claim['final_status']}`（{claim['outcome']}，证据来自{where}）"
            )
        lines.append(
            f"- **{record['case']}**（{record['origin']}）：unknown {len(unknown)} 个 —— "
            + "；".join(parts)
            + f" ⏐ 补证新增 {sum(call['added'] for call in record.get('followup_calls') or ())} 条材料"
        )
    return lines


def _paired_rows(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """⑤ 开 / 关 的配对读数（同一题两次独立跑，规划与判定都是新样本）。"""

    on = {record["case"]: record for record in records if _in_condition(record, CONDITION_FIVEFOLD)}
    off = {record["case"]: record for record in records if _in_condition(record, CONDITION_CONTROL)}
    lines = [
        "| 题 | gaps（开/关） | unknown（开/关） | 答案（开/关） | 声明块（开/关） | 调用（开/关） |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case_id in sorted(set(on) & set(off), key=case_order_key):
        left, right = on[case_id], off[case_id]
        lines.append(
            f"| {case_id} | {len(left['gaps'])} / {len(right['gaps'])} "
            f"| {len(left['unknown_ids'])} / {len(right['unknown_ids'])} "
            f"| {'非空' if left['answered'] else 'null'} / "
            f"{'非空' if right['answered'] else 'null'} "
            f"| {left['insufficient']} / {right['insufficient']} "
            f"| {left['model_calls']} / {right['model_calls']} |"
        )
    return lines


def _selection_table(payload: Mapping[str, Any]) -> list[str]:
    """B 组 10 题清单：题号 + expected + top-2 实际命中（**机械证据**）。"""

    probes = {(probe["source"], probe["origin_id"]): probe for probe in payload.get("probes") or ()}
    lines = [
        "| 题 | 来源 | 题面 | expected 标注 | top-2 实际命中 | 首轮缺 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in payload.get("cases") or ():
        if case.get("group") != GROUP_B:
            continue
        probe = probes.get((case["source"], case["origin"]), {})
        expected = "、".join(probe.get("expected_labels") or ())
        hits = "、".join(probe.get("hit_labels") or ()) or "（空）"
        missing = "、".join(probe.get("missing_labels") or ())
        lines.append(
            f"| {case['case_id']} | {case['origin']}（{case['source']}） | {case['query']} "
            f"| {expected} | {hits} | {missing} |"
        )
    return lines


def render_report(payload: Mapping[str, Any]) -> str:
    """人读报告：三分类与分母、两条件对照、三个问题的答案、机械证据、不能回答什么。"""

    metrics = payload["metrics"]
    a_block = metrics["A"]
    b_block = metrics["B"]
    control = metrics["B_control"]
    records = payload.get("records") or []
    environment = payload.get("environment") or {}
    calls = payload.get("model_calls") or {}
    predicted = payload.get("predicted_calls") or {}
    archive = payload.get("archive") or {}
    gaps = payload.get("archive_gaps") or {}
    sources = environment.get("sources") or {}
    refs_env = sources.get(REFS_SOURCE) or {}
    eval_env = sources.get(EVAL_SOURCE) or {}

    lines: list[str] = []
    lines.append("# #168 ⑤ 的「救回」这一面")
    lines.append("")
    lines.append(f"- 生成时间：`{payload.get('generated_at')}`")
    lines.append(
        f"- 模型链路：`{(payload.get('llm_chain') or {}).get('llm_model')}`"
        f"（来源 `{(payload.get('llm_chain') or {}).get('llm_endpoint_source')}`）"
    )
    lines.append(
        f"- 模型调用：票面设计（A+B）名义 **{predicted.get('spec_nominal')}** / 最坏 "
        f"**{predicted.get('spec_worst')}**；含追加条件 B-tight 的名义 **{predicted.get('nominal')}**"
        f" / 最坏 **{predicted.get('worst')}**；**实际 {calls.get('actual')} 次**"
        f"（硬顶 {calls.get('budget')}）"
    )
    lines.append(
        f"- 检索：`hybrid`（不重排、不改写）⏐ B 组 `top_k={B_TOP_K}`、A 组 `top_k={A_TOP_K}`"
    )
    lines.append(
        f"- 语料：refs（租户 `{refs_env.get('tenant_id')}`、索引 `{refs_env.get('keyword_alias')}`、"
        f"库 `{refs_env.get('kb_scope')}`）⏐ eval（租户 `{eval_env.get('tenant_id')}`、"
        f"索引 `{eval_env.get('keyword_alias')}`、库 `{eval_env.get('kb_scope')}`）"
    )
    lines.append("")

    # ── 一、三个数 ──
    lines.append("## 一、救回到底有没有发生")
    lines.append("")
    lines.append(
        "**「救回」的严格定义**：某要点 第一轮 `none` → 补证捞到材料 → 记 `unknown` → ⑤ 复查后 "
        "`supported` **且** `evidence_ids` 里至少一条材料的章节在该题的 `expected` 标注里。"
        "少了最后这道标准答案，「救回率」会虚高（模型看错材料也会判 supported），所以三分类"
        "分开报，**分母只有一个：机会数（要点级）**。"
    )
    lines.append("")
    lines.extend(_rescue_table(b_block, title="B 组（主体：压 top_k=2 强制造漏召回）"))
    tight = metrics.get("B_tight") or {}
    if tight.get("cases"):
        lines.extend(
            _rescue_table(
                tight,
                title=f"B-tight（追加条件：同一批题，`top_k={TIGHT_TOP_K}`，⑤ 开）",
            )
        )
    lines.extend(_rescue_table(a_block, title="A 组（归档那 3 题，top_k=5 重跑，单列）"))
    lines.append(
        f"> **「仍 unknown」不进分母**，单列：B 组 {b_block['counts'][RESCUE_UNRESOLVED]} 条、"
        f"A 组 {a_block['counts'][RESCUE_UNRESOLVED]} 条。"
        "「无机会」按**题**数：一题里没有任何要点落 `unknown`（首轮就够、或补证一条新材料都没"
        "捞到），就没有可复查的对象——它解释的是「机会造得少不少」，不是「救回率高低」。"
    )
    lines.append("")

    # ── 二、两条件对照 ──
    lines.append("## 二、⑤ 开 / 关 的配对对照（B 组每题跑两次）")
    lines.append("")
    lines.append(
        '关法与 #167 逐字一致：评测进程内 `setattr(research._Run, "_rejudge", <async no-op>)`，'
        "`finally` 还原；**`app/` 一个字节不改**。⑤ 开着的那一趟在同一个接缝上挂一个**直通探针**"
        "（先只读快照，再原样调用），用来取「复查之前那批 `unknown`」——否则只能靠猜。"
    )
    lines.append("")
    lines.append(
        f"- ⑤ **关掉**时的 `unknown`（直接测量，不是反推）：**{control['unknown_points']}** 条要点、"
        f"**{control['unknown_cases']}/{control['cases']}** 题"
    )
    lines.append(
        f"- ⑤ 关掉时三分类**不适用**：没有复查，那 {control['unknown_points']} 个机会要点全部停在 "
        "`unknown`——它们既不进 `gaps`（缺口闸看不见），也不代表有支撑"
    )
    lines.append(
        f"- 缺口总数：⑤ 开 **{b_block['gaps_total']}** / 关 **{control['gaps_total']}**；"
        f"答案非空：开 **{b_block['answered']}** / 关 **{control['answered']}** / "
        f"{b_block['cases']} 题；声明块：开 **{b_block['insufficient_total']}** / "
        f"关 **{control['insufficient_total']}**"
    )
    lines.append("")
    lines.extend(_paired_rows(records))
    lines.append("")

    # ── 三、三个问题 ──
    lines.append("## 三、必须回答的三个问题")
    lines.append("")
    lines.append("### 3.1 救回到底有没有发生？")
    lines.append("")
    lines.append(
        f"- **B 组**：机会数 **{b_block['opportunity_points']}** 条要点（分布在 "
        f"{b_block['opportunity_cases']} 题上，另 {b_block['no_chance_cases']} 题无机会）。"
        f"真救回 **{b_block['counts'][RESCUE_TRUE]}**（{_fmt_rate(b_block['rates'][RESCUE_TRUE])}）、"
        f"假救回 **{b_block['counts'][RESCUE_FALSE]}**（{_fmt_rate(b_block['rates'][RESCUE_FALSE])}）、"
        f"未救回 **{b_block['counts'][RESCUE_NOT]}**（{_fmt_rate(b_block['rates'][RESCUE_NOT])}）。"
        f"分母是 {b_block['opportunity_points']}。"
    )
    lines.append(
        f"- **A 组**：机会数 **{a_block['opportunity_points']}**（{a_block['cases']} 题里 "
        f"{a_block['no_chance_cases']} 题无机会）。真 "
        f"{a_block['counts'][RESCUE_TRUE]} / 假 {a_block['counts'][RESCUE_FALSE]} / 未 "
        f"{a_block['counts'][RESCUE_NOT]}。**A 组的 0 不能读成「⑤ 不救回」**：分母只有 "
        f"{a_block['opportunity_points']}，一次翻转就是 100 个百分点。"
    )
    if tight.get("cases"):
        lines.append(
            f"- **B-tight（追加条件，`top_k={TIGHT_TOP_K}`）**：机会数 "
            f"**{tight['opportunity_points']}**（{tight['cases']} 题里 "
            f"{tight['opportunity_cases']} 题有机会、{tight['no_chance_cases']} 题无机会）。真 "
            f"{tight['counts'][RESCUE_TRUE]} / 假 {tight['counts'][RESCUE_FALSE]} / 未 "
            f"{tight['counts'][RESCUE_NOT]}。这一档**不是票面要求的**，加它只为回答一个问题："
            "「机会数这么少，是不是因为 `top_k=2` 压得还不够小」。"
        )
    lines.append("")
    lines.append("### 3.2 ⑤ 的定位该怎么写（这句会进 ADR-0029 §2.2）")
    lines.append("")
    verdict = _positioning_verdict(b_block, a_block, control, tight)
    lines.extend(verdict)
    lines.append("")
    lines.append("### 3.3 真救回率为什么是这个数：⑤ 很少救回，还是机会造得太少？")
    lines.append("")
    lines.extend(_opportunity_verdict(b_block, control, tight, records))
    lines.append("")

    # ── 四、机械证据 ──
    lines.append("## 四、B 组 10 题与机械证据（expected 不在 top-2）")
    lines.append("")
    lines.append(
        "选题**不是我挑的**：以 `top_k=2` 跑一次首轮检索（原问题、0 次模型调用），"
        "该题 `expected` 标注**不在** top-2 才入选。规则在 :func:`select_b2_cases` 里，"
        "排序是三层机械规则（refs 优先 → eval 全缺 → eval 部分缺，各自按题号自然序）。"
    )
    lines.append("")
    lines.extend(_selection_table(payload))
    lines.append("")
    lines.append(
        f"候选池 {len(payload.get('probes') or ())} 题，其中 expected 在 top-2 下缺席的 "
        f"{sum(1 for probe in payload.get('probes') or () if probe.get('qualified'))} 题。"
    )
    lines.append("")
    lines.append("### 复核：选题条件在**真跑的首轮**里还成立吗")
    lines.append("")
    lines.append(
        "选题探针用**原问题**问一次；真跑首轮是规划器拆出来的**按要点多条**查询。两者不是"
        "一回事，后者更容易把标注节捞回来——所以「压 `top_k=2`」这个人造条件在运行时可能已经"
        "被规划器抵消了。这张表是机械复核（两列都来自落盘记录）："
    )
    lines.append("")
    lines.extend(_runtime_miss_table(records, payload))
    lines.append("")
    lines.append("逐题读数（⑤ 开着那一趟）：")
    lines.append("")
    lines.extend(_case_detail_rows(records, condition=CONDITION_FIVEFOLD))
    lines.append("")
    lines.append("### 机会数的构成（第三问的原始材料）")
    lines.append("")
    lines.extend(_opportunity_breakdown(records))
    lines.append("")
    reasons = b_block.get("no_chance_reasons") or {}
    if reasons:
        lines.append("无机会的题**为什么无机会**（这一格决定第三问怎么答）：")
        lines.append("")
        lines.append("| 成因 | 题数 |")
        lines.append("| --- | --- |")
        for reason, count in sorted(reasons.items(), key=lambda item: -item[1]):
            lines.append(f"| {reason} | {count} |")
        lines.append("")
        lines.append(
            "> 第 1 种（首轮就命中全部 expected）说明**选题探针比真跑保守**：探针拿原问题问一次，"
            "真跑首轮是按要点拆开的多条查询，后者更容易把标注节捞回来。"
            "第 2 种（首轮漏了标注节、判定仍判全部 supported）更值得注意：补证**根本没有发生**，"
            "因为定论说「都有支撑」——支撑它的是**别的节**。这两条合起来说明「压死 `top_k=2`」"
            "在运行时被两件事抵消了：规划器的按要点查询、以及判定的乐观。"
        )
        lines.append("")
    if tight.get("cases"):
        lines.append(f"### 追加条件 B-tight（`top_k={TIGHT_TOP_K}`）的逐题读数与机会构成")
        lines.append("")
        lines.extend(_case_detail_rows(records, condition=CONDITION_TIGHT))
        lines.append("")
        lines.extend(_opportunity_breakdown(records, condition=CONDITION_TIGHT))
        lines.append("")
        true_claims = [
            claim
            for record in records
            if _in_condition(record, CONDITION_TIGHT)
            for claim in record.get("claims") or ()
            if claim.get("outcome") == RESCUE_TRUE
        ]
        if true_claims:
            lines.append("**真救回逐条（可复核）**：")
            lines.append("")
            for claim in true_claims:
                lines.append(
                    f"- {claim['case']} · {claim['point_id']}「{str(claim.get('point'))[:30]}」："
                    f"复查判 `supported`，证据 {claim['evidence_ids']} → "
                    f"{'、'.join(claim.get('evidence_labels') or ())}（第 "
                    f"{'/'.join(str(round_no) for round_no in claim.get('evidence_rounds') or ())} 轮"
                    f"检索回来），落在 expected 里的标注："
                    f"{'、'.join(claim.get('matched_expected') or ())}"
                )
            lines.append("")

    # ── 五、A 组：归档复用做不到什么 ──
    lines.append("## 五、A 组：票面说「复用归档」，实测做不到")
    lines.append("")
    lines.append(
        f"归档：`{_posix(archive.get('run'))}`"
        f"（`generated_at` `{archive.get('generated_at')}`、"
        f"`stop_reason == max_rounds` 的题 {archive.get('max_rounds_ids')}）。"
    )
    lines.append("")
    lines.append(
        "**做不到就地重判，原因是机械的**（:func:`archive_gaps` 逐题查过）：那 3 题的 `per_question` "
        "里只有「停止原因 + 最终材料清单（`chunk_id`/`heading_path`/正文）」，"
        f"**没有要点清单**（缺 {gaps.get('missing', {}).get('points')}）、"
        f"**没有逐要点 `coverage`**（缺 {gaps.get('missing', {}).get('coverage')}）、"
        f"**没有「首轮材料 / 补证材料」的切分**（缺 {gaps.get('missing', {}).get('followup_split')}）。"
        "而复查/救回判定的对象是**要点**——归档里没有要点，也就没有「那批 `unknown`」可以重判。"
    )
    lines.append("")
    lines.append(
        "所以 A 组改成**用归档那 3 道题的题面重跑生产链路**（`top_k=5`、⑤ 开）：题面与停止原因"
        "来自归档，材料与判定是重跑的。顺带记一条同向的证据：归档那次 `rerank` 是**开的**，"
        "本票与 #167 一样关掉它，于是**连停止原因都不可复现**——"
        + "；".join(
            f"{record['case']}（{record['origin']}）归档 `{record['archive'].get('stop_reason')}` "
            f"→ 这次 `{record['stop_reason']}`"
            for record in records
            if record.get("group") == GROUP_A
        )
        + "。归档材料本身还查得到："
        + "、".join(
            f"{record['case']} {record['archive'].get('present')}/{record['archive'].get('archived')}"
            for record in records
            if record.get("group") == GROUP_A
        )
        + " 条 `chunk_id` 仍在 `chunks` 表里。"
    )
    lines.append("")

    # ── 六、能证明什么 ──
    lines.append("## 六、这些数能证明什么")
    lines.append("")
    lines.append("| 数 | 值 | 能证明 | 不能证明 |")
    lines.append("| --- | --- | --- | --- |")
    lines.append(
        f"| 真救回率（B 组） | {_fmt_rate(b_block['rates'][RESCUE_TRUE])}"
        f"（{b_block['counts'][RESCUE_TRUE]}/{b_block['opportunity_points']}） "
        "| 压缩召回深度、答案确实在库里的条件下，复查能不能把材料读回成支撑 "
        "| 自然检索（top_k=5）下的救回率 |"
    )
    lines.append(
        f"| 假救回率（B 组） | {_fmt_rate(b_block['rates'][RESCUE_FALSE])}"
        f"（{b_block['counts'][RESCUE_FALSE]}/{b_block['opportunity_points']}） "
        "| 复查会不会「太乐观」（用别的节的材料撑住一个要点） "
        "| 模型换一个之后的同一读数 |"
    )
    lines.append(
        f"| 未救回率（B 组） | {_fmt_rate(b_block['rates'][RESCUE_NOT])}"
        f"（{b_block['counts'][RESCUE_NOT]}/{b_block['opportunity_points']}） "
        "| 补证带了材料回来、复查仍判「没有」的比例 "
        "| 「材料真的不够」还是「模型读不出来」 |"
    )
    lines.append(
        f"| 无机会的题（B 组） | {b_block['no_chance_cases']}/{b_block['cases']} "
        "| 压到 top_k=2 也不是每题都能造出「材料到了但没人判」 "
        "| 自然问题上这个比例 |"
    )
    lines.append(
        f"| ⑤ 关掉时的 unknown | {control['unknown_points']} 条 / "
        f"{control['unknown_cases']} 题 "
        "| 「材料到了但没判」在压 top_k 后有多常见（直接测量） "
        "| 自然检索下的频率 |"
    )
    lines.append(
        f"| A 组机会数 | {a_block['opportunity_points']} | 15 节的小语料上 `top_k=5` 已覆盖全库，"
        "造不出「补证才捞到」的机会 | 大语料上的机会数 |"
    )
    if tight.get("cases"):
        lines.append(
            f"| 真救回率（B-tight，`top_k={TIGHT_TOP_K}`） | "
            f"{_fmt_rate(tight['rates'][RESCUE_TRUE])}"
            f"（{tight['counts'][RESCUE_TRUE]}/{tight['opportunity_points']}） "
            "| **救回通路真的会走通**：把首轮压到每查询 1 节之后，确实出现过 "
            "`none → unknown → supported` 且证据落在 expected 上的要点 "
            "| 它在自然检索下有多常见（这一档是我方造的极端条件） |"
        )
        lines.append(
            f"| 机会数随深度变化 | `top_k=2` → {b_block['opportunity_points']}、"
            f"`top_k={TIGHT_TOP_K}` → {tight['opportunity_points']} "
            "| 机会数对检索深度敏感，所以「真救回率低」先要问「机会造够了没有」 "
            "| 这条曲线只有两个点 |"
        )
    lines.append("")

    # ── 七、不能回答什么 ──
    lines.append("## 七、这份对照不能回答什么")
    lines.append("")
    lines.append(
        "- **样本小**：B 组 10 题两条件、A 组 3 题"
        + ("、追加条件 B-tight 10 题" if tight.get("cases") else "")
        + "；机会数分母是 "
        f"**{b_block['opportunity_points']}**（B）+ **{a_block['opportunity_points']}**（A）"
        + (f"、**{tight['opportunity_points']}**（B-tight）" if tight.get("cases") else "")
        + "。一次翻转就是几十个百分点，这里够判断「方向」，不够给发生率。"
    )
    lines.append(
        "- **压 `top_k=2` 是人工造的漏召回**：真实检索用的是这套集成的档位参数（本票与 #167 一样"
        "取 `top_k=5` 那一档），救回的真实频率取决于「首轮漏的一节能不能被定向补证捞回来」，"
        "而这个概率由检索深度、语料规模、查询改写共同决定。本条只测了「一压到 2」这一个点。"
    )
    lines.append(
        "- **只测一个模型链**：规划 / 判定 / 复查 / 生成走同一条供应商的同一条模型（见报告头）。"
        "换模型、换思考强度，救回与假救回都会变；这里没有做模型间对照。"
    )
    lines.append(
        "- **`expected` 只是「够不够」的近似标准答案**：它标的是「哪一节里有回答该要点的事实」，"
        "而**一节可能覆盖多个方面**——所以「证据章节 ∈ expected」是**必要但不充分**的："
        "它挡住了「用别的节硬撑」（假救回），但挡不住「同一节里读到的是另一段话」。"
        "反过来，一条真材料如果落在标注漏掉的节上，会被记成**假救回**（这是本口径唯一的系统性"
        "偏严之处）。"
    )
    lines.append(
        "- **`unknown → supported` 与 `unknown → none` 是同一个模型的两次判定**：两次判定的差别里"
        "既有「材料真的够了」，也有抽样噪声。#167 已经说明两次跑连要点清单都会变，本票的"
        "『复查改判』因此也不能读成『模型严格变准了』。"
    )
    lines.append(
        "- **两条件是两次独立跑**（与 #167 同一条限制）：规划、判定都是新样本，于是「⑤ 开/关」的"
        "gaps 差里既有 ⑤ 的效果，也有抽样噪声。"
    )
    lines.append("- **没有跑间方差**：一次跑批，没有重复跑。")
    lines.append(
        "- **⑤ 关掉是一个不存在的生产配置**：真实部署里 ⑤ 总是开着；对照条件只回答"
        "「若没有它会怎样」。"
    )
    lines.append(
        "- **A 组不是「复用归档」**：归档缺要点与逐要点 `coverage`（已机械核实），A 组是重跑。"
        "它与 B 组的差别只有 `top_k`（5 vs 2）与题源（refs 3 题），**不要把 A 当成「自然场景的"
        "对照组」**——3 题的噪声比两组差别还大。"
    )
    if tight.get("cases"):
        lines.append(
            f"- **B-tight（`top_k={TIGHT_TOP_K}`）是我方追加的条件，票面没有**：它存在的理由是"
            "主条件（`top_k=2`）造出的机会太少（只有 "
            f"{b_block['opportunity_points']} 个），不加一档更狠的深度，本票就成了「⑤ 没被测到」。"
            "但它是**极端人造条件**：每查询只取 1 节，真实检索里不存在这一档，所以它的三分类"
            "**只能读成「机制走得通」**，不能读成发生率。"
        )
        lines.append(
            "- **`top_k` 只测了两个点（2 与 1）**：看不出「机会数随深度怎么变」的形状，"
            "也说不清在真实档位（本票 A 组用的 5）下机会数会不会是 0。"
        )
    lines.append("")

    # ── 八、复现 ──
    lines.append("## 八、怎么复现")
    lines.append("")
    lines.append("```powershell")
    lines.append("# 只打印预估、选题与机械证据（0 次模型调用；要连库与检索）")
    lines.append("uv run python scripts/eval_t168_rescue.py plan")
    lines.append("")
    lines.append(
        f"# 全量（票面 A+B：名义 {predicted.get('spec_nominal')} 次、最坏 "
        f"{predicted.get('spec_worst')} 次模型调用；追加的 B-tight 另计）"
    )
    lines.append("uv run python scripts/eval_t168_rescue.py run")
    lines.append("")
    lines.append("# 按当前口径重算指标并重出报告（0 次模型调用）")
    lines.append("uv run python scripts/eval_t168_rescue.py recompute")
    lines.append("```")
    lines.append("")
    lines.append(
        "跑的是**生产完整链路**：`run_research`（规划 → 并行检索 → 覆盖判定 → 定向补证 → 复查 → "
        "生成）+ 生产检索内核 `build_kernel_search` + 两个真实模型角色。没有构造材料池，也没有"
        "另写一套提示词。"
    )
    lines.append("")

    # ── 九、记账 ──
    lines.append("## 九、记账口径")
    lines.append("")
    lines.append("- 实际调用次数从**逐题记录**加总（`recompute` 会重算这一格），不另记一本账。")
    lines.append(
        f"- 纸面预估：票面设计（A+B）名义 **{predicted.get('spec_nominal')}** / 最坏 "
        f"**{predicted.get('spec_worst')}**；加上追加条件 B-tight 之后名义 "
        f"**{predicted.get('nominal')}** / 最坏 **{predicted.get('worst')}**。"
        "**纸面超界 ≠ 实际超跑**：预算闸按最坏逐题预留、按**实际**调用扣账，"
        f"实际 {calls.get('actual')} 次仍在硬顶 {calls.get('budget')} 之内。追加条件是在 A、B "
        "两组跑完之后才决定加的（理由见脚本模块文档），所以这里的名义数是事后补算的。"
    )
    lines.append(
        f"- 分账：⑤ 开 **{calls.get('by_condition', {}).get(CONDITION_FIVEFOLD)}** 次"
        f"（A 组 {a_block['model_calls']} + B 组 {b_block['model_calls']}）、"
        f"⑤ 关 **{calls.get('by_condition', {}).get(CONDITION_CONTROL)}** 次（B 组对照）"
        + (
            f"、追加条件 B-tight **{calls.get('by_condition', {}).get(CONDITION_TIGHT)}** 次"
            if tight.get("cases")
            else ""
        )
        + "。"
    )
    lines.append("- 选题探针与预热查询**不计模型调用**：它们只用检索与嵌入，不碰 LLM。")
    if calls.get("skipped"):
        lines.append(f"- 因预算跳过：{calls['skipped']}")
    lines.append("")
    return "\n".join(lines)


def _positioning_verdict(
    b_block: Mapping[str, Any],
    a_block: Mapping[str, Any],
    control: Mapping[str, Any],
    tight: Mapping[str, Any],
) -> list[str]:
    """第二问：⑤ 是「双向」还是「纯收紧器」。判断与建议措辞都在这里。"""

    def count(block: Mapping[str, Any], outcome: str) -> int:
        return int(block["counts"][outcome]) if block.get("cases") else 0

    true_rescues = (
        count(b_block, RESCUE_TRUE) + count(a_block, RESCUE_TRUE) + count(tight, RESCUE_TRUE)
    )
    false_rescues = (
        count(b_block, RESCUE_FALSE) + count(a_block, RESCUE_FALSE) + count(tight, RESCUE_FALSE)
    )
    exposed = count(b_block, RESCUE_NOT) + count(a_block, RESCUE_NOT) + count(tight, RESCUE_NOT)
    total = true_rescues + false_rescues + exposed
    lines: list[str] = []
    if total == 0:
        lines.append(
            "- **本轮没有机会要点落进三分类**（两组机会数都是 0）：⑤ 的定位问题这一轮**答不了**。"
            "能说的只有：在 `top_k=2` 与 `top_k=5` 这两种深度下，本票的题集没有造出「补证捞到"
            "材料」的机会。"
        )
        return lines
    share_true = true_rescues / total
    lines.append(
        f"- 三个数合起来看：真救回 **{true_rescues}**、假救回 **{false_rescues}**、未救回 "
        f"**{exposed}**（合计 {total} 个机会要点；B 组 + A 组"
        + (" + B-tight" if tight.get("cases") else "")
        + f" 相加）。真救回占 **{share_true * 100:.1f}%**。"
    )
    if true_rescues == 0:
        tiny = total < 10
        lines.append(
            "- **判断：本轮**一次真救回也没观测到——⑤ 在这几个机会上唯一动过的方向是「更严格」"
            f"（未救回 {exposed}、假救回 {false_rescues}、真救回 0）。"
            + (
                f"但分母只有 **{total}**，一次翻转就是 {100 / total:.0f} 个百分点：这是"
                "**方向性读数，不是发生率**，**不足以把 ⑤ 定性成「纯收紧器」**。"
                if tiny
                else ""
            )
        )
        lines.append(
            "- **建议措辞（ADR-0029 §2.2）**："
            "\n  > ⑤ 的方向是**更严格**，不是更好：它把 `unknown` 落成 `none`（隐藏缺口现形、"
            "答案更保守），这一面 #167 与 #168 都测到了。**「救回」这一面 #168 已经测过**"
            "（压 `top_k` 造漏召回），"
            f"在造出来的 {total} 个机会里真救回 {true_rescues}、假救回 {false_rescues}、"
            f"未救回 {exposed}——**没有证据支持「⑤ 会提高覆盖率」**；"
            + (
                "但机会数是如此的少（见 #168 的机会数构成），所以这句只能写成「未获证据」，"
                "**不能写成「⑤ 从不救回」**。"
                if tiny
                else "因此不要把它写成双向能力。"
            )
        )
    else:
        tiny_bidirectional = total < 10
        lines.append(
            f"- **判断：⑤ 是双向的**（暴露 + 挽回）：真救回 {true_rescues} 条、假救回 "
            f"{false_rescues} 条、未救回 {exposed} 条。两个方向必须**分开报**，不许合成一个"
            "「改善率」：真救回让答案变多且更准，未救回让答案更保守，而假救回是**负向**的。"
            + (
                f"但合计只有 **{total}** 个机会要点（真救回 {true_rescues} 条），一次翻转就是 "
                f"{100 / total:.0f} 个百分点：这是**方向性读数**，不是发生率；而且那 {true_rescues} "
                "条真救回**只在追加条件**（更小的 `top_k`）下出现——票面那一档（`top_k=2`）里"
                "一次都没有。"
                if tiny_bidirectional
                else ""
            )
        )
        lines.append(
            "- **建议措辞（ADR-0029 §2.2）**："
            "\n  > ⑤ 的作用是**双向但不对称**的：它主要让隐藏缺口现形（`unknown → none`），"
            f"同时确实能把一部分「材料到了但没人判」的要点救回成有支撑的结论"
            f"（真救回 {true_rescues}/{total}）；"
            + (
                f"但它也会**变乐观**（假救回 {false_rescues}/{total}），所以下游读 `coverage` 时"
                "不能把 `supported` 当成「一定对」。"
                if false_rescues
                else "本轮**一次假救回也没观测到**（假救回 0），也就是复查没有用别的节硬撑过"
                f"一个要点——但分母只有 {total}，这句话的证据强度也仅此而已。"
            )
        )
    if false_rescues:
        lines.append(
            f"- **假救回 {false_rescues} 条是负向读数，必须单列**：那是「复查判了 supported，"
            "但撑住它的是**别的节**的材料」——模型把「相关」当成了「支撑」。它与真救回方向相反，"
            "合并成一个「改判率」会把两件事抵消掉。"
        )
    if control["unknown_points"]:
        lines.append(
            f"- 顺带把「暴露」那一面在同一批题上重测了一遍：⑤ 关掉时有 "
            f"{control['unknown_points']} 个要点停在 `unknown`（既不进 `gaps` 也没有支撑），"
            "⑤ 开着之后它们落地成 `none` / `supported`——这一面与 #167 的读数方向一致。"
        )
    else:
        lines.append(
            "- ⑤ 关掉时**一个 `unknown` 也没有**：本票的题集在 `top_k=2` 下没造出「材料到了但"
            "没人判」的状态，所以「暴露」这一面这次**没有被重测**，它上面的数全部来自 #167。"
        )
    return lines


def _opportunity_verdict(
    b_block: Mapping[str, Any],
    control: Mapping[str, Any],
    tight: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[str]:
    """第三问：真救回率低，是「⑤ 很少救回」还是「机会造得太少」。"""

    lines: list[str] = []
    upper = b_block["cases"] * FOLLOWUP_QUERY_CAP
    b_on = [
        record
        for record in records
        if record.get("group") == GROUP_B and _in_condition(record, CONDITION_FIVEFOLD)
    ]
    runtime_miss = [record for record in b_on if record.get("round1_expected_missing")]
    lines.append(
        f"- **机会数的构成先摆出来**：B 组 {b_block['cases']} 题里 **{b_block['opportunity_cases']}** "
        f"题造出了机会、共 **{b_block['opportunity_points']}** 个要点；"
        f"**{b_block['no_chance_cases']} 题无机会**。落 `unknown` 的要点占全部要点的 "
        f"**{b_block['unknown_points']}/{b_block['points_total']}**。"
    )
    lines.append(
        f"- **机会数有一个结构性上界：每题 ≤ {FOLLOWUP_QUERY_CAP} 个**。补证最多跑 "
        f"{FOLLOWUP_QUERY_CAP} 条查询（`MAX_FOLLOWUP_QUERIES`），而只有「绑定在某个缺口要点上的"
        "那条补证查询**真的**带回了新材料」时，那个要点才会从 `none` 改成 `unknown`。于是本票的"
        f"机会数上界是 {b_block['cases']} × {FOLLOWUP_QUERY_CAP} = {upper}，实测 "
        f"{b_block['opportunity_points']}"
        + (f"，上界只用到 {b_block['opportunity_points'] / upper * 100:.0f}%。" if upper else "。")
    )
    lines.append(
        f"- **人造的漏召回在真跑首轮里只剩 {len(runtime_miss)}/{len(b_on)} 题**：选题探针用"
        "**原问题**问一次，真跑首轮是规划器拆出来的**按要点多条**查询——后者更容易把标注节捞"
        f"回来。真跑首轮**真的缺**了标注节的是 "
        + (
            "、".join(f"{record['case']}（{record['origin']}）" for record in runtime_miss)
            if runtime_miss
            else "（没有）"
        )
        + "；其余题的首轮已经把标注节取回来了，所以它们**不是「造了机会但没救回」，而是根本没造出漏召回**。"
    )
    lines.append(
        "- **「标注节缺席」本来也不等于「答案缺席」**：这些语料里同一件事常常在两三节里都写着"
        "（例如优惠券退款、积分有效期），标注节没进 top-2 时，判定可以用**别的节**把要点判成 "
        "`supported`——这时补证与复查都不会被叫到。本票不评价这种判定对不对（没有逐要点的 "
        "ground truth），但它足以让「机会」消失。"
    )
    if control["unknown_cases"]:
        lines.append(
            f"- **补证那一侧也在收窄机会**：⑤ 关掉时有 {control['unknown_cases']}/"
            f"{control['cases']} 题带 `unknown`，说明「补证真的带回材料」在这批题上并非必然——"
            "「检索永远返回 top-k」这个直觉在**节去重之后**会打折：补证捞回来的往往是首轮已经在"
            "池子里的那几节（B01 那两条补证查询各新增 0 条就是这一种）。"
        )
    if tight.get("cases"):
        lines.append(
            f"- **「深度不够小」这个解释被追加条件检验过了**：同一批题压到 "
            f"`top_k={TIGHT_TOP_K}` 之后，机会数 **{tight['opportunity_points']}**"
            f"（`top_k={B_TOP_K}` 时是 {b_block['opportunity_points']}）、"
            f"真 {tight['counts'][RESCUE_TRUE]} / 假 {tight['counts'][RESCUE_FALSE]} / 未 "
            f"{tight['counts'][RESCUE_NOT]}。"
            + (
                "**没有变多**，所以「机会数小」不是「`top_k=2` 压得还不够狠」——把池子压到每查询"
                " 1 节也没能造出更多「材料到了但没人判」，卡点在别处（判定在上游就判了 "
                "`supported`、或补证捞回来的都是已有的节）。"
                if tight["opportunity_points"] <= b_block["opportunity_points"]
                else "**变多了**，说明机会数与检索深度强相关：`top_k=2` 这一档压得不够狠，"
                "本票的主读数因此更像「深度 × 判定」共同压出来的下界，而不是「⑤ 不救回」。"
            )
        )
    lines.append(
        "- **判断：就本票而言，是「机会造得太少」，不是「⑤ 很少救回」**。三条同时成立："
        "(1) 选题探针比真跑保守，10 题里只有少数几题在真跑首轮真的漏了标注节；"
        "(2) 标注节缺席不等于答案缺席，判定可以用别的节判 supported，于是补证/复查不被触发；"
        f"(3) 补证即使跑了也可能一条新材料都捞不到（每题最多 {FOLLOWUP_QUERY_CAP} 条查询 +"
        "按节去重）。**⑤ 在本票被叫到的次数屈指可数**：`top_k=2` 那一档真救回 0、"
        f"`top_k={TIGHT_TOP_K}` 那一档真救回 {tight.get('counts', {}).get(RESCUE_TRUE, 0)}"
        "——所以「真救回 0」不能读成「⑤ 不救回」，它更像「深度不够狠」。"
        "要量那一件事，得造出更多「材料到了但没人判」，而不是把 0 写成一个率。"
    )
    lines.append(
        "- 想看「机会造得多不多」，最直接的读数是第二节配对表里的 `unknown` 列"
        "（⑤ 关掉时的 `unknown` 就是可以复查的对象）：它说明在**同一批题、同一个 `top_k`** 下，"
        "「材料到了但没人判」到底出现几次。"
    )
    return lines


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
    if args.command == "recompute":
        return _recompute(out_dir)
    asyncio.run(run_all(groups=tuple(args.groups), out_dir=out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

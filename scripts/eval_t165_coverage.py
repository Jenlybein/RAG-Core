"""覆盖判定准确率评测：假缺口率 / 检出率 / 抗干扰 / ``unknown`` 率（票 #165）。

**它是测量，不是功能。** 第 11 步收窄之后，Research 的核心是**逐要点覆盖判定**
（每个要点 ``supported`` / ``none`` / ``unknown``）——它是缺口闸的上游，而在本票之前
**一个读数都没有**。本脚本只回答「给定一个池子，判定说不说得准」，不改任何生产行为。

**为什么不测召回。** 「检索能不能捞到」会被语料饱和度毁掉：上一轮在 3 篇 15 节的语料上测
图谱，注入 0 条，测出来的「没用」其实是语料的性质（ADR-0029 决策一理由 2）。而「给定一个
池子，判定判得对不对」这件事**可以由构造给出标准答案**，不需要人工读材料。

三个条件，每个条件一次判定调用：

===========  ==================================  ==========================  ==============
条件          怎么造                               期望                        测什么
===========  ==================================  ==========================  ==============
**A 满池**    ``expected`` 的节全给               每个要点都 ``supported``    **假缺口率**
**B 缺一块**  删掉某个要点对应的节               该要点 ``none``，其余照旧   **检出率**
**C 加干扰**  A 的池子 + 3 条无关节               仍然全 ``supported``        **抗干扰**
===========  ==================================  ==========================  ==============

**B 只用多要点题**（单要点题删掉唯一相关节会退化成「全缺」，测不出逐要点）——而且本脚本
再收紧一步：删完之后**必须还剩至少一个被支撑的要点**，否则这一条同「全缺」，不进 B
（见 :data:`_REFS_LABELS` 里 ``drop=None`` 的两题与报告里的排除说明）。

**标注独立于材料（ADR-0029 决策三，本票的硬要求）。** 要点只有两个来源：

* ``eval/refs-questions.json`` 的 ``answer_points``（写在检索之前、与材料无关）；
* ``eval/questions.json``（137 题库）的 ``relevant`` 标注 + 题面 ``note``。

脚本**不在运行时从材料里归纳任何要点**：要点文本按索引从标注文件里取，节归属按标注里的
节号 / ``heading_path`` 解析。上一轮在这上面栽过——3 道题的点是照着材料写的，覆盖率必然
全中，结论作废。

**判定复用生产路径，不另写一套提示词。** 判定走的正是 :func:`app.services.research._Run._judge`
（``_JUDGE_SYSTEM`` + :func:`app.services.research.build_judge_prompt` +
:func:`app.services.research._coerce_coverage``）。为了让「判定作废」这一栏能归因到五条收敛
规则里的哪一条，脚本把 ``_coerce_coverage`` 临时换成会**记录异常**的包装（``mock.patch``，
不改 ``app/`` 一个字节）；判定本身仍由生产函数做。

**缺口闸那一组跑的是完整五步状态机**（:meth:`_Run.execute`：规划 → 检索 → 判定 → 补证 →
生成），只把**检索内核**换成「一个由构造决定的池子」——那是 ADR-0028 §4 明文留的注入缝。
断言只有一条：有缺口时 ``answer is None`` **或**答案里出现「材料不足」。

**预算：≤60 次模型调用**（:data:`MODEL_CALL_CAP`）。开跑前打印预估，跑完打印实际次数，两个
数都进产物与报告。预估口径见 :func:`predict_model_calls`。

用法::

    # 只打印预估与素材自检（0 次模型调用，不连库）
    uv run python scripts/eval_t165_coverage.py plan

    # 全量：A/B/C + 缺口闸 + 补证恢复（写 eval/reports/t165/）
    uv run python scripts/eval_t165_coverage.py run

    # 只跑其中几组
    uv run python scripts/eval_t165_coverage.py run --groups A,B,C

产物：``eval/reports/t165/results.json``（机器可读）与 ``report.md``（人读）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    # 以 ``python scripts/eval_t165_coverage.py`` 运行时 ``sys.path[0]`` 是 ``scripts/``；
    # ``scripts`` 是没有 ``__init__.py`` 的命名空间包，靠这行才能 ``from scripts...``。
    sys.path.insert(0, str(REPO_ROOT))

EVAL_DIR = REPO_ROOT / "eval"
REFS_CORPUS_DIR = EVAL_DIR / "corpus-refs"
BASE_CORPUS_DIR = EVAL_DIR / "corpus"
REFS_QUESTIONS = EVAL_DIR / "refs-questions.json"
BASE_QUESTIONS = EVAL_DIR / "questions.json"
REPORT_DIR = EVAL_DIR / "reports" / "t165"

#: 模型调用的硬顶（票面：**≤60 次**）。预估与执行都对着它判，超了就不跑下一组。
MODEL_CALL_CAP: Final[int] = 60

#: 一次 Research 的最坏模型调用次数（规划 + 判定 + 生成 + 兜底一格），从生产常量推。
FULL_RESEARCH_CALLS: Final[int] = 4

#: 归属索引用的租户：评测自带一个，不与任何真实租户相交（只进日志指纹）。
TENANT_ID: Final[str] = "tenant_t165"

#: 缺口闸要求答案里出现的那句话（生产常量 ``app.services.citation_blocks.GAP_DECLARATION``）。
#: 这里抄一份而不是 import：``plan`` 模式要在没有配置/没有库的机器上也能跑
#: （app 的 import 会读 settings）。``tests/test_eval_t165_coverage.py`` 钉住两者相等。
GAP_DECLARATION: Final[str] = "材料不足"

#: C 条件加几条无关节（票面：2–3 条；取满 3 条）。
DISTRACTOR_COUNT: Final[int] = 3

#: 缺口闸那一组的题（B 条件，多要点题）。三条是票面「3–5 题」的下沿。
GAP_GATE_CASES: Final[tuple[str, ...]] = ("r05", "r06", "r08")

#: 「补证后恢复」那一组的题：用来量 ``unknown`` 的产生条件（ADR-0029 实施备注第 2 条）。
RECOVERY_CASES: Final[tuple[str, ...]] = ("r01", "r04", "r07")

#: 假缺口率的容忍线：超过它说明缺口闸在误杀好答案（这是最危险的错）。
FALSE_GAP_TOLERANCE: Final[float] = 0.10

#: ``unknown`` 的判据线（ADR-0029 实施备注第 2 条）：补证发生过、且至少一个要点落在
#: ``unknown`` 的题占比超过它，就该考虑加 ⑤ 复查。
UNKNOWN_TOLERANCE: Final[float] = 0.20

_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+?)\s*$")
_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<number>\d+(?:\.\d+)*)")


# ─────────────────────────────────────────────────────────── 语料：节与交付单元


@dataclass(frozen=True)
class Section:
    """语料里的一节：**从 h1 到它自己的完整标题路径** + 原文（含标题行）。

    正文止于下一个**层级不高于它**的标题——所以 ``## 2.`` 的正文含它自己的 ``### 2.1``，
    而 ``### 2.1`` 的正文只是它自己那一段。这条边界与生产切块器的节边界一致（同一份
    markdown、同一条「按标题层级切」的规矩）。
    """

    path: tuple[str, ...]
    level: int
    text: str

    @property
    def title(self) -> str:
        return self.path[-1]

    @property
    def number(self) -> str | None:
        """标题自身的编号前缀（``2. 抵扣`` → ``2``、``2.2 上限`` → ``2.2``）；没有则 ``None``。"""

        match = _NUMBER_RE.match(self.title)
        return match.group("number") if match else None


@dataclass(frozen=True)
class Unit:
    """一个**交付单元**：生产把标题路径前两段折叠成一条材料（ADR-0013 / 折叠口径）。

    ``key`` 是它在本题里的身份：``<document>::<heading_path>``。``document`` 取文件名主干
    （与题集里的 document 字段同一套写法）。
    """

    key: str
    document: str
    title: str
    heading_path: str
    content: str
    order: int


def parse_sections(markdown: str) -> list[Section]:
    """把一篇 markdown 拆成「标题路径 → 节」，按出现顺序。

    路径用**标题栈**算（比自己拼字符串可靠）：弹掉所有层级 ≥ 当前的标题，剩下的就是祖先。
    """

    lines = markdown.splitlines()
    heads: list[tuple[int, int, str]] = []  # (行号, 层级, 标题)
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match is not None:
            heads.append((index, len(match.group("hashes")), match.group("title")))

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    for position, (index, level, title) in enumerate(heads):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        end = len(lines)
        for next_index, next_level, _next_title in heads[position + 1 :]:
            if next_level <= level:
                end = next_index
                break
        sections.append(
            Section(
                path=tuple(name for _level, name in stack),
                level=level,
                text="\n".join(lines[index:end]).strip(),
            )
        )
    return sections


def unit_of(section: Section) -> str:
    """一条材料的折叠口径：标题路径取**前两段**（文档标题 + 一级节）。"""

    return " > ".join(section.path[:2])


def load_units(directory: Path) -> tuple[dict[str, Unit], list[Unit]]:
    """把整个语料目录读成「单元索引 + 有序单元表」。

    一个单元 = 前两段路径相同的那一组节；正文取**层级最浅的那一节**（它按上面的边界已经
    含了自己的子节），这与生产折叠交回的那一条材料是同一段文字。
    """

    index: dict[str, Unit] = {}
    ordered: list[Unit] = []
    for path in sorted(directory.glob("*.md")):
        document = path.stem
        sections = parse_sections(path.read_text(encoding="utf-8"))
        if not sections:
            continue
        doc_title = sections[0].path[0]
        for section in sections:
            if len(section.path) < 2:
                continue
            heading_path = unit_of(section)
            key = f"{document}::{heading_path}"
            if key in index:
                continue
            unit = Unit(
                key=key,
                document=document,
                title=doc_title,
                heading_path=heading_path,
                content=section.text,
                order=len(ordered),
            )
            index[key] = unit
            ordered.append(unit)
    return index, ordered


def refs_unit_key(units: Mapping[str, Unit], *, document: str, section: str) -> str:
    """把 refs 标注里的「文档 + 节号」解析成交付单元的 key。

    标注写的是**节号**（``2.2``），而交付单元是**一级节**（``2.``）——所以先取节号的第一段，
    再在本文档的一级节里按标题编号找。找不到就是标注/语料对不上，当场失败（不猜）。
    """

    wanted = section.split(".")[0]
    for unit in units.values():
        if unit.document != document:
            continue
        tail = unit.heading_path.split(" > ")[-1]
        match = _NUMBER_RE.match(tail)
        if match is not None and match.group("number") == wanted:
            return unit.key
    raise SystemExit(f"标注解析失败：{document} 第 {section} 节找不到对应的交付单元")


def base_unit_key(*, document: str, heading_path: str) -> str:
    """137 题库的标注直接给 ``heading_path``（完整路径），折叠口径取前两段。"""

    return f"{document}::{unit_of(Section(path=tuple(heading_path.split(' > ')), level=2, text=''))}"


# ─────────────────────────────────────────────────────────── 标注（冻结在问题侧）


@dataclass(frozen=True)
class LabelPoint:
    """一个要点的一条标注：文本（问题侧）+ 它需要的交付单元 + 人类可读的出处。"""

    text: str
    unit: str
    source: str


@dataclass(frozen=True)
class Case:
    """一道题的一次构造：题面 + 要点标注 + B 条件删哪个单元。"""

    case_id: str
    corpus: str  # "refs" | "base"
    query: str
    points: tuple[LabelPoint, ...]
    units: tuple[str, ...]
    drop: str | None
    note: str = ""

    def dropped_point_indexes(self) -> tuple[int, ...]:
        """B 条件里**期望判 none** 的要点下标（其余期望 ``supported``）。"""

        if self.drop is None:
            return ()
        return tuple(i for i, point in enumerate(self.points) if point.unit == self.drop)


#: refs 8 题的**节归属表**：``points`` 把 ``answer_points`` 的每一条映到 ``units`` 的下标，
#: ``drop`` 指 B 条件删哪个单元（``None`` = 该题不进 B）。这张表是从
#: ``eval/refs-questions.json`` 的 ``answer_points`` 与 ``expected[].why`` 抄下来的**归属**，
#: 不看材料正文：``why`` 说的就是「这一节承担哪几条」。
#:
#: ``r02`` / ``r03`` 的 ``drop=None``：这两题的全部要点都压在**同一个**核心节上，删掉它就
#: 是「全缺」，与单要点题退化成同一件事（测不出逐要点），所以按票面的规矩不进 B。
_REFS_LABELS: Final[dict[str, dict[str, Any]]] = {
    "r01": {
        "units": (("refund_policy", "2"), ("refund_policy", "3")),
        "points": (0, 0, 1, 1),
        "drop": 0,
    },
    "r02": {
        "units": (("points_policy", "3"), ("points_policy", "1")),
        "points": (0, 0, 0),
        "drop": None,
        "note": "全部要点压在同一核心节，删掉即全缺 → 不进 B（见模块文档）",
    },
    "r03": {
        "units": (("promo_rules", "3"), ("refund_policy", "2")),
        "points": (0, 0, 0, 0),
        "drop": None,
        "note": "全部要点压在同一核心节，删掉即全缺 → 不进 B（见模块文档）",
    },
    "r04": {
        "units": (("points_policy", "2.2"), ("promo_rules", "1")),
        "points": (0, 0, 1),
        "drop": 1,
    },
    "r05": {
        "units": (("promo_rules", "3"), ("points_policy", "3"), ("refund_policy", "1")),
        "points": (0, 1, 2, 2),
        "drop": 1,
    },
    "r06": {
        "units": (("promo_rules", "1"), ("promo_rules", "2"), ("promo_rules", "3")),
        "points": (0, 0, 1, 2),
        "drop": 1,
    },
    "r07": {
        "units": (("refund_policy", "2"), ("promo_rules", "3"), ("points_policy", "3")),
        "points": (1, 1, 2),
        "drop": 2,
    },
    "r08": {
        "units": (
            ("points_policy", "4"),
            ("refund_policy", "4"),
            ("points_policy", "2.2"),
        ),
        "points": (0, 1, 2, 0),
        "drop": 1,
    },
}

#: 137 题库的 4 道多要点题（``cross_section``，两节各承担一半答案）。
#:
#: 要点文本按**题面 + 题集自带的 ``note``** 写（``note`` 是标注者写的「哪一节给什么」），
#: 单元一律取 ``relevant`` 里原样给出的 ``heading_path``——本表一个字的材料正文都没读。
_BASE_LABELS: Final[dict[str, dict[str, Any]]] = {
    "q16": {
        "points": (
            ("证书过期这类告警的现象与处置入口", "support_errors", "售后故障与错误码速查 > 连接与证书类错误（一）"),
            ("证书轮换的四步流程，以及过程中不重启平台入口", "ops_runbook", "值班处置手册 > 证书轮换操作步骤"),
        ),
        "drop": 1,
    },
    "q18": {
        "points": (
            ("P1 故障的响应时限", "ops_runbook", "值班处置手册 > 告警分级与响应时限"),
            ("P1 超时之后的升级对象与通报动作", "ops_runbook", "值班处置手册 > 升级与通报路径"),
        ),
        "drop": 1,
    },
    "q60": {
        "points": (
            ("变更窗口在什么时间", "ops_change", "发布与变更管理规范 > 变更窗口"),
            ("窗口结束前的变更后验证三层", "ops_change", "发布与变更管理规范 > 变更后验证"),
        ),
        "drop": 1,
    },
    "q77": {
        "points": (
            ("账单金额算错时的申诉方式与提交时限", "billing_policy", "计费与账务政策 > 计费争议"),
            ("账单的出账时间（账期与出账日）", "billing_policy", "计费与账务政策 > 账单出账时间"),
        ),
        "drop": 0,
    },
}


def load_refs_cases(units: Mapping[str, Unit]) -> list[Case]:
    """refs 8 题：要点文本**逐字**取自 ``answer_points``（不重写、不改写）。"""

    payload = json.loads(REFS_QUESTIONS.read_text(encoding="utf-8"))
    cases: list[Case] = []
    for question in payload["questions"]:
        spec = _REFS_LABELS[question["id"]]
        unit_keys = tuple(
            refs_unit_key(units, document=document, section=section)
            for document, section in spec["units"]
        )
        answers = list(question["answer_points"])
        points = tuple(
            LabelPoint(
                text=answers[index],
                unit=unit_keys[unit_index],
                source=f"answer_points[{index}] → {spec['units'][unit_index][0]}:"
                f"{spec['units'][unit_index][1]}",
            )
            for index, unit_index in enumerate(spec["points"])
        )
        cases.append(
            Case(
                case_id=question["id"],
                corpus="refs",
                query=str(question["query"]),
                points=points,
                units=unit_keys,
                drop=None if spec["drop"] is None else unit_keys[spec["drop"]],
                note=str(spec.get("note") or ""),
            )
        )
    return cases


def load_base_cases() -> list[Case]:
    """137 题库的 4 道多要点题：要点文本来自 ``note`` 的「哪一节给什么」。"""

    payload = json.loads(BASE_QUESTIONS.read_text(encoding="utf-8"))
    by_id = {str(item["id"]): item for item in payload["questions"]}
    cases: list[Case] = []
    for case_id, spec in _BASE_LABELS.items():
        question = by_id[case_id]
        points = tuple(
            LabelPoint(text=text, unit=base_unit_key(document=document, heading_path=path), source=path)
            for text, document, path in spec["points"]
        )
        unit_keys = tuple(dict.fromkeys(point.unit for point in points))
        drop_point = spec["drop"]
        cases.append(
            Case(
                case_id=case_id,
                corpus="base",
                query=str(question["query"]),
                points=points,
                units=unit_keys,
                drop=points[drop_point].unit,
                note=str(question.get("note") or ""),
            )
        )
    return cases

# ─────────────────────────────────────────────────────────── 三个条件的池子

#: 逐要点判定的三个词，直接取生产常量——评测与生产不许有两个词表。
SUPPORTED: Final[str] = "supported"
NONE: Final[str] = "none"
UNKNOWN: Final[str] = "unknown"

CONDITIONS: Final[tuple[str, ...]] = ("A", "B", "C")


@dataclass(frozen=True)
class PointExpectation:
    """一个要点在某个条件下的**标准答案**：由构造给出，不看材料正文。"""

    index: int
    text: str
    expected: str
    unit: str


def expectations(case: Case, *, condition: str) -> list[PointExpectation]:
    """某个条件下每个要点该被判成什么。

    A / C：全部 ``supported``（池子给全了，判不出支撑就是**假缺口** / 抗干扰失败）；
    B：被删单元扛的那几条期望 ``none``，其余照旧 ``supported``。
    """

    if condition not in CONDITIONS:
        raise ValueError(f"未知条件：{condition}")
    dropped = set(case.dropped_point_indexes()) if condition == "B" else set()
    return [
        PointExpectation(
            index=index,
            text=point.text,
            expected=NONE if index in dropped else SUPPORTED,
            unit=point.unit,
        )
        for index, point in enumerate(case.points)
    ]


def select_distractors(
    catalogue: Sequence[Unit], *, keep: Sequence[str], count: int = DISTRACTOR_COUNT
) -> list[str]:
    """C 条件的无关节：**同文档里紧挨着期望节的节优先**，再补同语料其他文档。

    为什么先要「邻居」：最容易被判错的干扰从来不是随机噪声，而是同一篇里讲隔壁事的节
    （错误码 (一) 与 (二) 正文九成相同）。选它才是这条对照有分辨力的地方。
    顺序全部确定（按语料出现顺序），所以重跑选出来的干扰一模一样。
    """

    keep_set = set(keep)
    by_document: dict[str, list[Unit]] = {}
    for unit in catalogue:
        by_document.setdefault(unit.document, []).append(unit)

    ranked: list[str] = []
    for key in keep:
        target = next((unit for unit in catalogue if unit.key == key), None)
        if target is None:
            continue
        siblings = by_document.get(target.document, [])
        position = next(
            (index for index, unit in enumerate(siblings) if unit.key == key), None
        )
        if position is None:
            continue
        for offset in range(1, len(siblings)):
            for candidate in (position + offset, position - offset):
                if 0 <= candidate < len(siblings):
                    ranked.append(siblings[candidate].key)
    for unit in catalogue:
        ranked.append(unit.key)

    picked: list[str] = []
    for key in ranked:
        if len(picked) >= count:
            break
        if key in keep_set or key in picked:
            continue
        picked.append(key)
    return picked


def build_pool(
    units: Mapping[str, Unit],
    case: Case,
    *,
    condition: str,
    catalogue: Sequence[Unit] | None = None,
) -> list[Unit]:
    """按条件交出一个材料池（顺序确定：期望节在前，无关节在后）。

    A：``expected`` 的节全给（交付单元口径）；B：A 再删掉 ``case.drop`` 那一条；
    C：A 再加 :func:`select_distractors` 挑的 3 条无关节。
    """

    keys = [key for key in case.units if not (condition == "B" and key == case.drop)]
    if condition == "C":
        if catalogue is None:
            raise ValueError("C 条件需要整份语料（用来挑无关节）")
        keys += select_distractors(catalogue, keep=case.units)
    pool: list[Unit] = []
    for key in keys:
        unit = units.get(key)
        if unit is None:
            raise SystemExit(f"{case.case_id} 的单元不在语料里：{key}")
        pool.append(unit)
    return pool


def material_rows(pool: Sequence[Unit]) -> list[dict[str, Any]]:
    """交付单元 → 判定阶段看到的材料行（与生产同形：标题 / 章节路径 / 正文）。"""

    return [
        {
            "chunk_id": f"t165-{index:02d}",
            "section_id": f"t165-{unit.key}",
            "document_id": unit.document,
            "kb_id": "kb-t165",
            "title": unit.title,
            "heading_path": unit.heading_path,
            "content": unit.content,
            "score": 1.0,
        }
        for index, unit in enumerate(pool, start=1)
    ]


def describe_pool(case: Case, pool: Sequence[Unit], *, condition: str) -> dict[str, Any]:
    """材料池的可复算摘要：进了哪几条、删了哪一条（B 才有）。"""

    return {
        "condition": condition,
        "units": [unit.key for unit in pool],
        "dropped": case.drop if condition == "B" else None,
        "materials": len(pool),
        "chars": sum(len(unit.content) for unit in pool),
    }


# ─────────────────────────────────────────────────────────── 指标


def score_verdicts(
    expected: Sequence[PointExpectation],
    statuses: Sequence[str] | None,
    *,
    invalid_reason: str | None = None,
) -> dict[str, Any]:
    """把一次判定的结果折成计数。``statuses is None`` = 整份判定作废。

    判定作废与 ``unknown`` **分开记**：前者是「模型答了但不合法、按五条收敛规则整份丢掉」，
    后者是「没人看过」。混成一个数就再也分不清「模型不听话」与「流程没走到」。
    """

    total = len(expected)
    counts = {
        "verdicts": total,
        "invalid": 1 if statuses is None else 0,
        "expected_supported": sum(1 for item in expected if item.expected == SUPPORTED),
        "expected_none": sum(1 for item in expected if item.expected == NONE),
        "judged_supported": 0,
        "judged_none": 0,
        "judged_unknown": 0,
        "false_gap": 0,
        "false_supported": 0,
        "none_detected": 0,
        "invalid_reason": invalid_reason,
    }
    if statuses is None:
        return counts

    for item, status in zip(expected, statuses, strict=True):
        if status == SUPPORTED:
            counts["judged_supported"] += 1
        elif status == NONE:
            counts["judged_none"] += 1
        else:
            counts["judged_unknown"] += 1

        if item.expected == SUPPORTED and status == NONE:
            counts["false_gap"] += 1
        if item.expected == NONE and status == SUPPORTED:
            counts["false_supported"] += 1
        if item.expected == NONE and status == NONE:
            counts["none_detected"] += 1
    return counts


def _sum_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    keys = (
        "verdicts",
        "invalid",
        "cases",
        "cases_invalidated",
        "expected_supported",
        "expected_none",
        "judged_supported",
        "judged_none",
        "judged_unknown",
        "false_gap",
        "false_supported",
        "none_detected",
        "evidence_on_expected_unit",
        "evidence_checked",
    )
    return {key: sum(int(row.get(key) or 0) for row in rows) for key in keys}


def _rate(numerator: int | float, denominator: int | float) -> float | None:
    if not denominator:
        return None
    return round(float(numerator) / float(denominator), 4)


def aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """一组用例的总账（每个率都给出分子与分母，避免「0/0 = 1」那种假结论）。"""

    totals = _sum_counts(rows)
    supported_all = totals["expected_supported"] + totals["expected_none"]
    return {
        "cases": totals["cases"],
        "point_verdicts": totals["verdicts"],
        "假缺口率": _rate(totals["false_gap"], totals["expected_supported"]),
        "假缺口_分子": totals["false_gap"],
        "假缺口_分母": totals["expected_supported"],
        "检出率": _rate(totals["none_detected"], totals["expected_none"]),
        "检出_分子": totals["none_detected"],
        "检出_分母": totals["expected_none"],
        "抗干扰率": _rate(totals["judged_supported"], totals["expected_supported"]),
        "unknown率": _rate(totals["judged_unknown"], supported_all),
        "unknown_分子": totals["judged_unknown"],
        "unknown_分母": supported_all,
        "误报support率": _rate(totals["false_supported"], totals["expected_none"]),
        "判定作废率": _rate(totals["cases_invalidated"], totals["cases"]),
        "判定作废_分子": totals["cases_invalidated"],
        "判定作废_分母": totals["cases"],
        "证据指向准确率": _rate(totals["evidence_on_expected_unit"], totals["evidence_checked"]),
        "证据指向_分子": totals["evidence_on_expected_unit"],
        "证据指向_分母": totals["evidence_checked"],
    }


# ─────────────────────────────────────────────────────────── 预算


def _cases_for(condition: str) -> list[tuple[str, str]]:
    """``(corpus, case_id)`` 清单：A / C 用全部 12 题，B 只用有 ``drop`` 的题。"""

    refs = json.loads(REFS_QUESTIONS.read_text(encoding="utf-8"))["questions"]
    refs_ids = [str(item["id"]) for item in refs]
    droppable = {case_id for case_id, spec in _REFS_LABELS.items() if spec["drop"] is not None}
    out: list[tuple[str, str]] = []
    for question_id in refs_ids:
        if condition == "B" and question_id not in droppable:
            continue
        out.append(("refs", question_id))
    for question_id in _BASE_LABELS:
        out.append(("base", question_id))
    return out


def predict_model_calls() -> dict[str, Any]:
    """开跑前的预估：判定每个条件各 1 次/题，完整 Research 每題 3 次（兜底另算）。

    预估写死成「按题数算」，不按上限算：上限（:data:`FULL_RESEARCH_CALLS`）只用来给
    **最坏情况**兜底，两个数都打印、都进产物——一个数说不清「这次跑了多少」与「最多能花多少」。
    """

    groups = {
        "A": len(_cases_for("A")),
        "B": len(_cases_for("B")),
        "C": len(_cases_for("C")),
        "gap_gate": len(GAP_GATE_CASES) * 3,
        "recovery": len(RECOVERY_CASES) * 3,
    }
    nominal = sum(groups.values())
    worst = groups["A"] + groups["B"] + groups["C"]
    worst += (len(GAP_GATE_CASES) + len(RECOVERY_CASES)) * FULL_RESEARCH_CALLS
    return {
        "budget": MODEL_CALL_CAP,
        "nominal": nominal,
        "worst": worst,
        "groups": groups,
        "actual": 0,
    }


@dataclass
class Budget:
    """模型调用预算：**先预留再发**。超了就不再开下一组，而不是跑完再算账。"""

    cap: int = MODEL_CALL_CAP
    spent: int = 0
    skipped: list[str] = field(default_factory=list)

    def can(self, calls: int) -> bool:
        return self.spent + calls <= self.cap

    def charge(self, calls: int) -> None:
        self.spent += max(0, calls)

    def skip(self, label: str, calls: int) -> None:
        self.skipped.append(f"{label}（还要 {calls} 次，已用 {self.spent}/{self.cap}）")


def build_plan() -> dict[str, Any]:
    """素材自检 + 预估（0 次模型调用、不连库、不读模型配置）。

    它把「标注能不能解析」「三个条件的池子造不造得出来」两件事变成**开跑前**的硬失败：
    跑到一半才发现某题的节号对不上，会白花掉前面那些模型调用。
    """

    refs_index, refs_all = load_units(REFS_CORPUS_DIR)
    base_index, base_all = load_units(BASE_CORPUS_DIR)
    refs_cases = load_refs_cases(refs_index)
    base_cases = load_base_cases()

    sample: list[dict[str, Any]] = []
    pools_ok = True
    for corpus_cases, index, catalogue in (
        (refs_cases, refs_index, refs_all),
        (base_cases, base_index, base_all),
    ):
        for case in corpus_cases:
            row: dict[str, Any] = {
                "case": case.case_id,
                "corpus": case.corpus,
                "query": case.query,
                "points": [point.text for point in case.points],
                "units": list(case.units),
                "drop": case.drop,
                "note": case.note,
            }
            for condition in CONDITIONS:
                if condition == "B" and case.drop is None:
                    row["B"] = "skipped: 删掉唯一相关节会退化成全缺"
                    continue
                pool = build_pool(index, case, condition=condition, catalogue=catalogue)
                row[condition] = describe_pool(case, pool, condition=condition)
                if condition == "B" and len(pool) != len(case.units) - 1:
                    pools_ok = False
            sample.append(row)

    labels_ok = all(
        all(point.unit in index for point in case.points)
        for corpus_cases, index in ((refs_cases, refs_index), (base_cases, base_index))
        for case in corpus_cases
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "issue": "#165 覆盖判定准确率评测",
        "model_calls": predict_model_calls(),
        "checks": {
            "labels_ok": labels_ok,
            "pools_ok": pools_ok,
            "refs_cases": len(refs_cases),
            "base_cases": len(base_cases),
            "refs_units": len(refs_all),
            "base_units": len(base_all),
        },
        "sample": sample,
    }


def render_plan(plan: Mapping[str, Any]) -> str:
    """人读的预估：样本清单 + 预算 + 素材自检结果（``plan`` 模式的全部输出）。"""

    calls = plan["model_calls"]
    checks = plan["checks"]
    lines = [
        "# #165 覆盖判定准确率评测 · 开跑前预估",
        "",
        f"- 生成时间：`{plan['generated_at']}`",
        f"- 语料：refs {checks['refs_units']} 条交付单元 / 137 题库 {checks['base_units']} 条",
        f"- 标注自检：labels_ok={checks['labels_ok']} pools_ok={checks['pools_ok']}",
        f"- 题量：refs {checks['refs_cases']} 题 + 137 题库 {checks['base_cases']} 题",
        "",
        "## 模型调用预估（硬顶 {} 次）".format(calls["budget"]),
        "",
        "| 组 | 次数 |",
        "| --- | --- |",
    ]
    for name, count in calls["groups"].items():
        lines.append(f"| {name} | {count} |")
    lines += [
        f"| **合计（名义）** | **{calls['nominal']}** |",
        f"| **合计（最坏，含兜底格）** | **{calls['worst']}** |",
        "",
        "## 样本清单",
        "",
        "| 题 | 语料 | 要点 | A | B | C |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in plan["sample"]:
        a = row.get("A") or {}
        b = row.get("B")
        c = row.get("C") or {}
        b_cell = "跳过" if isinstance(b, str) else f"{b.get('materials', 0)} 条（删 {b.get('dropped')}）"
        lines.append(
            f"| {row['case']} | {row['corpus']} | {len(row['points'])} | "
            f"{a.get('materials', 0)} 条 | {b_cell} | {c.get('materials', 0)} 条 |"
        )
    return "\n".join(lines) + "\n"

# ─────────────────────────────────────────────────────────── 判定：走生产那条路


@dataclass(frozen=True)
class Judgement:
    """一次判定的读数。``statuses is None`` = 整份判定作废（五条收敛规则之一）。"""

    statuses: tuple[str, ...] | None
    evidence_units: tuple[tuple[str, ...], ...]
    invalid_reason: str | None
    model_calls: int


async def _no_search(_sub_query: str) -> list[dict[str, Any]]:
    """判定单独跑时不需要检索：材料池是构造出来的，这里只满足 ``_Run`` 的入参形状。"""

    return []


def _evidence_units(verdicts: Sequence[Any], pool: Sequence[Unit]) -> tuple[tuple[str, ...], ...]:
    """把判定给的 ``E1`` / ``E2`` 反查回「它点的是哪一条交付单元」。

    判定只会写材料序号（ADR-0026 §4：模型没有可装正文的通道），所以这一层是纯粹的
    序号回查——它让我们能问「判 supported 时点到的那一条，是不是期望节」，而不用相信模型
    自己说的「有支撑」。
    """

    resolved: list[tuple[str, ...]] = []
    for verdict in verdicts:
        keys: list[str] = []
        for evidence in verdict.evidence_ids:
            text = str(evidence)
            order = int(text[1:]) if text[1:].isdigit() else 0
            if 1 <= order <= len(pool):
                keys.append(pool[order - 1].key)
        resolved.append(tuple(keys))
    return tuple(resolved)


async def judge_pool(
    *,
    query: str,
    points: Sequence[str],
    pool: Sequence[Unit],
    llm_factory: Any,
) -> Judgement:
    """**直接调生产判定**：``_Run._judge``（提示词与收敛都是生产里那两份）。

    为什么用私有类：判定只有经状态机才到得了（``_judge`` 是 ``_Run`` 的方法），而
    「另写一套提示词」会让测的不是生产里那个东西。于是这里只做两件不改变行为的事：
    把要点与材料池**构造成既定输入**，并在 ``_coerce_coverage`` 外面套一层**记录异常**的
    包装（结果照旧抛回去）。``app/`` 的签名一个字没改。

    ``_judge`` 的失败出口是唯一的：任何一条收敛规则不成立就返回 ``None``、``coverage``
    全部 ``unknown``——这正是「判定作废率」要数的东西。
    """

    from app.services import research as research_service

    run = research_service._Run(
        query=query,
        tenant_id=TENANT_ID,
        search=_no_search,
        llm_factory=llm_factory,
        strong_llm_factory=None,
        sub_search_degraded=(),
        clock=time.monotonic,
    )
    run.points = [
        research_service.PlanPoint(f"A{index}", text) for index, text in enumerate(points, start=1)
    ]
    run.materials = material_rows(pool)

    invalid: dict[str, str] = {}
    original = research_service._coerce_coverage

    def spy(raw: Any, *, points: Sequence[Any], material_count: int) -> Any:
        try:
            return original(raw, points=points, material_count=material_count)
        except Exception as exc:  # noqa: BLE001 - 记录后原样抛回，判定行为不变
            invalid.setdefault("reason", str(exc) or type(exc).__name__)
            raise

    with mock.patch.object(research_service, "_coerce_coverage", spy):
        judged = await run._judge()

    calls = run.model_calls
    if judged is None:
        return Judgement(
            statuses=None,
            evidence_units=(),
            invalid_reason=invalid.get("reason") or "judgement failed",
            model_calls=calls,
        )
    verdicts, _queries = judged
    return Judgement(
        statuses=tuple(verdict.status for verdict in verdicts),
        evidence_units=_evidence_units(verdicts, pool),
        invalid_reason=None,
        model_calls=calls,
    )


def case_record(
    case: Case,
    *,
    condition: str,
    pool: Sequence[Unit],
    judgement: Judgement,
) -> dict[str, Any]:
    """一道题在某个条件下的完整读数（报告与 results.json 共用这一份）。"""

    expected = expectations(case, condition=condition)
    counts = score_verdicts(expected, judgement.statuses, invalid_reason=judgement.invalid_reason)
    counts["cases"] = 1
    counts["cases_invalidated"] = counts["invalid"]

    evidence_ok = 0
    evidence_seen = 0
    point_rows: list[dict[str, Any]] = []
    for item in expected:
        status = None if judgement.statuses is None else judgement.statuses[item.index]
        units = () if judgement.statuses is None else judgement.evidence_units[item.index]
        on_expected = bool(units) and item.unit in units
        if status == SUPPORTED:
            evidence_seen += 1
            evidence_ok += 1 if on_expected else 0
        point_rows.append(
            {
                "index": item.index,
                "point": item.text,
                "unit": item.unit,
                "expected": item.expected,
                "status": status,
                "evidence_units": list(units),
                "on_expected_unit": on_expected,
            }
        )
    counts["evidence_on_expected_unit"] = evidence_ok
    counts["evidence_checked"] = evidence_seen

    record = dict(counts)
    record.update(
        {
            "case": case.case_id,
            "corpus": case.corpus,
            "condition": condition,
            "query": case.query,
            "pool": describe_pool(case, pool, condition=condition),
            "points": point_rows,
            "model_calls": judgement.model_calls,
            "note": case.note,
        }
    )
    return record


async def run_conditions(
    *,
    conditions: Sequence[str],
    llm_factory: Any,
    budget: Budget,
    plan_only: bool = False,
) -> dict[str, Any]:
    """A / B / C 三组：每题一次判定调用。"""

    refs_index, refs_all = load_units(REFS_CORPUS_DIR)
    base_index, base_all = load_units(BASE_CORPUS_DIR)
    suites = {
        "refs": (load_refs_cases(refs_index), refs_index, refs_all),
        "base": (load_base_cases(), base_index, base_all),
    }

    results: dict[str, Any] = {}
    for condition in conditions:
        records: list[dict[str, Any]] = []
        for corpus, (cases, index, catalogue) in suites.items():
            for case in cases:
                if condition == "B" and case.drop is None:
                    continue
                label = f"{condition}:{case.case_id}"
                if plan_only:
                    continue
                if not budget.can(1):
                    budget.skip(label, 1)
                    continue
                pool = build_pool(index, case, condition=condition, catalogue=catalogue)
                judgement = await judge_pool(
                    query=case.query,
                    points=[point.text for point in case.points],
                    pool=pool,
                    llm_factory=llm_factory,
                )
                budget.charge(judgement.model_calls)
                records.append(case_record(case, condition=condition, pool=pool, judgement=judgement))
                print(
                    f"  {label}: {judgement.statuses or f'作废（{judgement.invalid_reason}）'}",
                    flush=True,
                )
        results[condition] = {
            "metrics": aggregate(records),
            "cases": records,
        }
    return results


# ─────────────────────────────────────────────────────────── 完整 Research（五步）


def _recovered_topic(case: Case) -> str:
    """B 条件里被删掉的那一节：补证组「捞回来」的就是它（来源可复算）。"""

    return case.drop or ""


async def run_full_research(
    *,
    case: Case,
    units: Mapping[str, Unit],
    recovered: bool,
    llm_factory: Any,
    strong_llm_factory: Any,
) -> dict[str, Any]:
    """跑**完整五步状态机**（规划 → 检索 → 判定 → 补证 → 生成），只换检索内核。

    ``recovered=False``（缺口闸那组）：内核交回的永远是 B 条件的池子——缺口补不上，
    于是最后一步必须面对「有缺口」这件事。
    ``recovered=True``（补证恢复那组）：判定跑完之后内核改交**被删的那一节**，于是缺口
    要点会走成 ``unknown``（ADR-0029 实施备注第 2 条要量的正是这个）。

    两组的检索结果都由构造决定，所以「有没有材料」这件事不靠运气——这是能断言缺口闸的
    唯一办法（真检索里同一题两次捞到的东西不一样）。
    """

    from app.services import research as research_service

    pool_b = build_pool(units, case, condition="B")
    if recovered:
        added = [units[case.drop]] if case.drop else []
    else:
        added = []
    first_rows = material_rows(pool_b)
    later_rows = material_rows(added)

    holder: dict[str, Any] = {}

    async def search(_sub_query: str) -> list[dict[str, Any]]:
        run = holder.get("run")
        # 判定跑完之后才进 ④ 补证，所以 ``_judged`` 就是「这是补证那一轮」的判据。
        if run is not None and getattr(run, "_judged", False):
            return [dict(row) for row in later_rows]
        return [dict(row) for row in first_rows]

    run = research_service._Run(
        query=case.query,
        tenant_id=TENANT_ID,
        search=search,
        llm_factory=llm_factory,
        strong_llm_factory=strong_llm_factory,
        sub_search_degraded=(),
        clock=time.monotonic,
    )
    holder["run"] = run
    outcome = await run.execute()

    answer = outcome.answer
    coverage = [
        {"point_id": verdict.point_id, "status": verdict.status, "evidence_ids": list(verdict.evidence_ids)}
        for verdict in outcome.coverage
    ]
    return {
        "case": case.case_id,
        "mode": "recovered" if recovered else "gap_gate",
        "query": case.query,
        "pool": describe_pool(case, pool_b, condition="B"),
        "recovered_unit": _recovered_topic(case) if recovered else None,
        "points": [point.text for point in outcome.points],
        "coverage": coverage,
        "gaps": list(outcome.gaps),
        "stop_reason": str(outcome.stop_reason.value),
        "evidence_state": outcome.evidence_state,
        "answered": answer is not None,
        "answer": answer,
        "declared_insufficient": bool(answer is not None and GAP_DECLARATION in answer),
        "answer_rejected_reason": outcome.answer_rejected_reason,
        "citations": len(outcome.citations),
        "strong_model_fallback": outcome.strong_model_fallback,
        "model_calls": outcome.model_calls,
        "duration_ms": outcome.duration_ms,
    }


def gate_verdict(record: Mapping[str, Any]) -> dict[str, Any]:
    """缺口闸那一条断言：有缺口时 ``answer is None`` **或**答案里出现「材料不足」。

    没有缺口时（规划没点到被删的那一块，或判定把它判成了 supported）闸门**没有被驱动**——
    这一栏如实写成 ``not_exercised``，绝不算通过：把「没发生」记成「通过了」正是这类报告
    最不该犯的错。
    """

    gaps = list(record.get("gaps") or [])
    if not gaps:
        return {"status": "not_exercised", "ok": None, "reason": "判定没有报出任何缺口"}
    if record.get("answer") is None:
        return {
            "status": "blocked",
            "ok": True,
            "reason": str(record.get("answer_rejected_reason") or "answer is None"),
        }
    if record.get("declared_insufficient"):
        return {"status": "declared", "ok": True, "reason": f"答案含「{GAP_DECLARATION}」"}
    return {"status": "leaked", "ok": False, "reason": "有缺口但答案既没作废也没声明不足"}


def unknown_reading(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """ADR-0029 实施备注第 2 条的判据：补证发生过、且至少一个要点落在 ``unknown`` 的题占比。"""

    exercised = [record for record in records if record.get("stop_reason") in ("max_rounds",)]
    with_unknown = [
        record
        for record in exercised
        if any(item["status"] == UNKNOWN for item in record.get("coverage") or [])
    ]
    points = [item for record in records for item in record.get("coverage") or []]
    return {
        "cases": len(records),
        "followup_exercised": len(exercised),
        "cases_with_unknown": len(with_unknown),
        "rate_with_unknown": _rate(len(with_unknown), len(exercised)),
        "unknown_points": sum(1 for item in points if item["status"] == UNKNOWN),
        "point_total": len(points),
        "point_unknown_rate": _rate(
            sum(1 for item in points if item["status"] == UNKNOWN), len(points)
        ),
        "cases_detail": [
            {
                "case": record.get("case"),
                "stop_reason": record.get("stop_reason"),
                "gaps": record.get("gaps"),
                "coverage": record.get("coverage"),
                "answered": record.get("answered"),
                "declared_insufficient": record.get("declared_insufficient"),
            }
            for record in records
        ],
    }

# ─────────────────────────────────────────────────────────── 报告


def _fmt_rate(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.1f}%"


def _metric_line(label: str, value: Any, numerator: Any, denominator: Any) -> str:
    return f"{label} | {_fmt_rate(value)} | {numerator}/{denominator}"


def recommendation(payload: Mapping[str, Any]) -> list[str]:
    """把「要不要加 ⑤ 复查」写成一条**有判据**的结论，而不是一句感觉。

    判据是 ADR-0029 实施备注第 2 条给的那个数：补证发生过、且至少一个要点落在 ``unknown``
    的题占比。它高的意思是「材料到了，但没人再回答过它支不支持」——那正是 ⑤ 存在的理由。
    """

    unknown = payload.get("unknown") or {}
    rate = unknown.get("rate_with_unknown")
    exercised = unknown.get("followup_exercised") or 0
    with_unknown = unknown.get("cases_with_unknown") or 0
    metrics_a = ((payload.get("conditions") or {}).get("A") or {}).get("metrics") or {}
    false_gap = metrics_a.get("假缺口率")

    lines: list[str] = []
    if not exercised:
        lines.append(
            "- **补证一轮都没发生**，ADR 那条判据量不出来：这一轮不能回答「要不要加 ⑤」，"
            "只能说这次的材料池没有触发补证。"
        )
        return lines
    lines.append(
        f"- 补证发生过 **{exercised}** 题，其中 **{with_unknown}** 题至少一个要点落在 "
        f"`unknown`（{_fmt_rate(rate)}；判据线 {_fmt_rate(UNKNOWN_TOLERANCE)}）。"
    )
    if rate is not None and rate >= UNKNOWN_TOLERANCE:
        lines.append(
            "- **结论：该加 ⑤（补证后再判一次）**。理由是 `unknown` 不是罕见的边缘态："
            "补证一旦捞到材料就有这么多题落在「材料到了、但没人再审过」上，而 `unknown` "
            "既进不了 `gaps`（缺口闸不会声明不足），也不代表有支撑——它只是没被回答。"
            "这类要点在答案里两种结果都是坏的：算进缺口会让缺口闸误杀，算成支撑是乐观。"
            f"代价是 +1 次模型调用，于是 ADR-0026 §5 的硬顶要跟着改（{3} → {4}）。"
        )
    else:
        lines.append(
            "- **结论：先不加 ⑤**。补证恢复之后落 `unknown` 的题少于判据线，"
            "多一次调用换来的只是「一个更漂亮的字段」，而不是更准的答案。"
        )
    if false_gap is not None and float(false_gap) > FALSE_GAP_TOLERANCE:
        lines.append(
            f"- ⚠️ 但 A 条件（满池）的**假缺口率 {_fmt_rate(false_gap)}** "
            f"高于容忍线 {_fmt_rate(FALSE_GAP_TOLERANCE)}：缺口闸在这批题上会误杀"
            "「其实有材料」的答案。这一项**不是加 ⑤ 能修的**（⑤ 只把 `unknown` 变成确定结论，"
            "降不了满池上的误判），它指向判定提示词的「支撑」口径或材料形态。"
        )
    return lines


def render_report(payload: Mapping[str, Any]) -> str:
    """人读报告。所有数字都从 ``payload`` 取，重跑即重算。"""

    conditions = payload.get("conditions") or {}
    calls = payload.get("model_calls") or {}
    lines: list[str] = [
        "# #165 覆盖判定准确率评测",
        "",
        f"- 生成时间：`{payload.get('generated_at')}`",
        f"- 模型链路：`{(payload.get('llm_chain') or {}).get('llm_model')}`"
        f"（来源 {(payload.get('llm_chain') or {}).get('llm_endpoint_source')}）",
        f"- 模型调用：名义预估 **{calls.get('nominal')}** 次 / 实际 **{calls.get('actual')}** 次"
        f"（硬顶 {calls.get('budget')}）",
        "",
        "## 一、五个数",
        "",
        "| 指标 | 值 | 分子/分母 |",
        "| --- | --- | --- |",
    ]
    metrics_a = (conditions.get("A") or {}).get("metrics")
    metrics_b = (conditions.get("B") or {}).get("metrics")
    metrics_c = (conditions.get("C") or {}).get("metrics")
    rows: list[tuple[str, Mapping[str, Any] | None, str, str, str]] = [
        ("假缺口率（A 满池）", metrics_a, "假缺口率", "假缺口_分子", "假缺口_分母"),
        ("检出率（B 缺一块）", metrics_b, "检出率", "检出_分子", "检出_分母"),
        ("误报 support 率（B）", metrics_b, "误报support率", "expected_none", "expected_none"),
        ("假缺口率（B，未被删的要点）", metrics_b, "假缺口率", "假缺口_分子", "假缺口_分母"),
        ("抗干扰率（C 加干扰）", metrics_c, "抗干扰率", "judged_supported", "expected_supported"),
        ("unknown 率（A）", metrics_a, "unknown率", "unknown_分子", "unknown_分母"),
        ("unknown 率（B）", metrics_b, "unknown率", "unknown_分子", "unknown_分母"),
        ("unknown 率（C）", metrics_c, "unknown率", "unknown_分子", "unknown_分母"),
        ("判定作废率（全组）", metrics_a, "判定作废率", "判定作废_分子", "判定作废_分母"),
        ("证据指向准确率（C）", metrics_c, "证据指向准确率", "证据指向_分子", "证据指向_分母"),
    ]
    for label, metrics, rate_key, numerator_key, denominator_key in rows:
        if not metrics:
            lines.append(f"| {label} | 未采集 | — | — |")
            continue
        numerator = metrics.get(numerator_key)
        if label.startswith("误报 support"):
            numerator = (metrics.get("expected_none") or 0) - (metrics.get("none_detected") or 0)
        lines.append(
            "| "
            + _metric_line(label, metrics.get(rate_key), numerator, metrics.get(denominator_key))
            + " |"
        )

    lines += ["", "## 二、结论", ""]
    lines += ["### 2.1 要不要加「补证后再判一次」（⑤）", ""]
    lines += recommendation(payload)

    lines += ["", "### 2.2 缺口闸现在实际拦到的是什么", ""]
    gate_rows = (payload.get("gap_gate") or {}).get("cases") or []
    if not gate_rows:
        lines.append("- 这一组没有采集。")
    for record in gate_rows:
        verdict = record.get("verdict") or {}
        lines.append(
            f"- **{record['case']}**：停止原因 `{record['stop_reason']}`、缺口 "
            f"{record.get('gaps')}、答案 `{'null' if record.get('answer') is None else '非空'}`、"
            f"声明不足 {record.get('declared_insufficient')} → `{verdict.get('status')}`"
            f"（{verdict.get('reason')}）"
        )

    lines += ["", "## 三、逐题明细（三个条件）", ""]
    for condition in ("A", "B", "C"):
        cases = (conditions.get(condition) or {}).get("cases") or []
        if not cases:
            continue
        lines += [f"### 条件 {condition}", "", "| 题 | 材料 | 要点判定 |", "| --- | --- | --- |"]
        for record in cases:
            cells = []
            for point in record["points"]:
                mark = "" if point["status"] == point["expected"] else "❗"
                cells.append(f"{point['point'][:18]}→{point['status']}{mark}")
            lines.append(
                f"| {record['case']} | {record['pool']['materials']} 条 | "
                f"{'；'.join(cells)} |"
            )
        invalid = [record for record in cases if record.get("invalid_reason")]
        if invalid:
            lines += [
                "",
                "作废的判定（`_coerce_coverage` 五条收敛规则之一）："
                + "、".join(f"{item['case']}（{item['invalid_reason']}）" for item in invalid),
            ]
        lines.append("")

    lines += ["", "## 四、``unknown`` 那一组（补证后恢复）", ""]
    unknown = payload.get("unknown") or {}
    if not unknown.get("cases"):
        lines.append("- 这一组没有采集。")
    else:
        lines += [
            f"- 补证发生：**{unknown['followup_exercised']}/{unknown['cases']}** 题；"
            f"其中落 `unknown` 的：**{unknown['cases_with_unknown']}** 题"
            f"（{_fmt_rate(unknown['rate_with_unknown'])}）",
            f"- 要点级 `unknown`：{unknown['unknown_points']}/{unknown['point_total']}"
            f"（{_fmt_rate(unknown['point_unknown_rate'])}）",
            "",
            "| 题 | 停止原因 | 缺口 | 覆盖状态 | 有答案 | 声明不足 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for record in unknown["cases_detail"]:
            statuses = "；".join(
                f"{item['point_id']}={item['status']}" for item in record["coverage"] or []
            )
            lines.append(
                f"| {record['case']} | `{record['stop_reason']}` | {record['gaps']} | "
                f"{statuses} | {record['answered']} | {record['declared_insufficient']} |"
            )

    lines += [
        "",
        "## 五、这份对照不能回答什么",
        "",
        "- **样本量小**：refs 8 题（B 条件 6 题）+ 137 题库 4 题，一共 12 题。"
        "任何一个率的分母都在 10–40 之间，一个要点翻转就能改掉好几个百分点；"
        "这里的数只够判断「有没有系统性偏差」，不够做版本间比较。",
        "- **refs 语料是合成的**：3 篇 13 节、篇幅与用词都是为定向题写的。"
        "合成语料上判定准，说明不了真实文档（长节、表格、跳跃指代）上也准。",
        "- **只测判定，不测召回**：材料池由构造给出，绕开了「检索能不能捞到」这件事。"
        "所以这里的「检出率」是「给定一个缺了一块的池子，判定报不报得出来」，"
        "**不是**端到端答案的正确率，也不是缺口闸在真实检索下的拦截率。",
        "- **B 条件的标准答案有构造风险**：删掉一节之后，如果那一节讲的事实恰好也在别的节里，"
        "判定判 `supported` 是**对的**，却会被记成漏检。看 `false_supported` 时请连同这个可能"
        "一起读（逐题明细里的 `evidence_units` 可以人工复核）。",
        "- **137 题库那 4 题只有两节**：两节各承担一半答案，比 refs 的三节题更粗；"
        "它们的要点文本来自题集的 `note`（标注者写的「哪一节给什么」），不是逐条参考要点。",
        "- **不测延迟与成本结构**：这一轮只数模型调用次数，没有比较档位、缓存或并发。",
        "",
        "## 六、怎么复现",
        "",
        "```powershell",
        "# 只打印预估与素材自检（0 次模型调用）",
        "uv run python scripts/eval_t165_coverage.py plan",
        "",
        "# 全量（约 {} 次模型调用）".format(calls.get("nominal")),
        "uv run python scripts/eval_t165_coverage.py run",
        "```",
        "",
        "判定调用走的是生产路径（`_Run._judge` + `_JUDGE_SYSTEM` + `_coerce_coverage`），"
        "完整 Research 那两组走 `_Run.execute()` 的五个阶段，只把检索内核换成构造池。",
    ]
    return "\n".join(lines) + "\n"

# ─────────────────────────────────────────────────────────── 装配：模型链与全量跑


async def _bind_model_chain() -> dict[str, Any]:
    """把模型接入接到**生产那条链**（``model_configs`` 的角色指针），返回用的是哪一条。

    与 ``scripts/eval_refs._bind_model_chain`` / ``scripts/eval_runner.bind_llm_role_resolver``
    是同一件事、同一组入口。不接会怎样：``.env`` 里的单供应商 key 与库里配的那一条不是同一个
    账号，走错那条时判定的每一题都会失败，读数看起来像「判定不行」——#153 在评测侧踩过同一脚。
    产物里记 ``llm_chain`` 就是为了事后能分开这两种情况。
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


def _print_budget(predicted: Mapping[str, Any]) -> None:
    print(f"预计模型调用：名义 {predicted['nominal']} 次 / 最坏 {predicted['worst']} 次"
          f"（硬顶 {predicted['budget']}）")
    for name, count in predicted["groups"].items():
        print(f"  - {name}: {count}")


async def _run_gap_gate(
    cases: Sequence[Case],
    units: Mapping[str, Unit],
    *,
    budget: Budget,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for case in cases:
        label = f"gap_gate:{case.case_id}"
        if not budget.can(1):
            budget.skip(label, 1)
            continue
        record = await run_full_research(
            case=case,
            units=units,
            recovered=False,
            llm_factory=_fast_provider,
            strong_llm_factory=_strong_provider,
        )
        budget.charge(int(record["model_calls"]))
        record["verdict"] = gate_verdict(record)
        records.append(record)
        print(f"  {label}: {record['verdict']['status']}（{record['verdict']['reason']}）", flush=True)
    return {"cases": records}


async def _run_recovery(
    cases: Sequence[Case],
    units: Mapping[str, Unit],
    *,
    budget: Budget,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for case in cases:
        label = f"recovery:{case.case_id}"
        if not budget.can(1):
            budget.skip(label, 1)
            continue
        record = await run_full_research(
            case=case,
            units=units,
            recovered=True,
            llm_factory=_fast_provider,
            strong_llm_factory=_strong_provider,
        )
        budget.charge(int(record["model_calls"]))
        records.append(record)
        print(f"  {label}: stop={record['stop_reason']} gaps={record['gaps']}", flush=True)
    return unknown_reading(records) | {"cases": records}


def actual_model_calls(payload: Mapping[str, Any]) -> int:
    """从产物本身数实际调用——**不另记一本账**。

    这样分组增量跑（一次只补一组）时，总数仍然只从记录里算，不会出现「实际次数」与「逐题
    记录」对不上的情况；重跑某一组也只会替换那一组自己那一段。
    """

    total = 0
    for section in (payload.get("conditions") or {}).values():
        total += sum(int(case.get("model_calls") or 0) for case in section.get("cases") or [])
    for key in ("gap_gate", "unknown"):
        total += sum(
            int(case.get("model_calls") or 0) for case in (payload.get(key) or {}).get("cases") or []
        )
    return total


async def run_all(
    *,
    groups: Sequence[str],
    out_dir: Path,
    mode: str = "run",
) -> dict[str, Any]:
    """跑指定的几组并落盘（``--mode plan`` 只打印预估，一次模型调用都不发）。

    产物是**可增量补**的：``results.json`` 已存在时只替换这次跑的那几组，其余原样保留——
    这样分组跑（先 A、再 B/C、再缺口闸）不必重花前面那些模型调用。
    """

    predicted = predict_model_calls()
    _print_budget(predicted)
    if mode == "plan":
        plan = build_plan()
        print(render_plan(plan))
        return plan

    chain = await _bind_model_chain()
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
            "issue": "#165 覆盖判定准确率评测",
            "llm_chain": chain,
            "model_calls": predicted,
            "conditions": dict(previous.get("conditions") or {}),
        }
    )

    condition_groups = [name for name in groups if name in CONDITIONS]
    if condition_groups:
        print(f"跑条件：{','.join(condition_groups)}", flush=True)
        fresh = await run_conditions(
            conditions=condition_groups,
            llm_factory=_fast_provider,
            budget=budget,
        )
        payload["conditions"] = dict(payload["conditions"]) | fresh
    if "gap_gate" in groups:
        refs_index, _ = load_units(REFS_CORPUS_DIR)
        cases = [case for case in load_refs_cases(refs_index) if case.case_id in GAP_GATE_CASES]
        print(f"跑缺口闸组：{[case.case_id for case in cases]}", flush=True)
        payload["gap_gate"] = await _run_gap_gate(cases, refs_index, budget=budget)
    if "recovery" in groups:
        refs_index, _ = load_units(REFS_CORPUS_DIR)
        cases = [case for case in load_refs_cases(refs_index) if case.case_id in RECOVERY_CASES]
        print(f"跑补证恢复组：{[case.case_id for case in cases]}", flush=True)
        payload["unknown"] = await _run_recovery(cases, refs_index, budget=budget)

    payload["model_calls"] = dict(predicted) | {
        "actual": actual_model_calls(payload),
        "skipped": list(budget.skipped),
    }
    (results_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "report.md").write_text(render_report(payload), encoding="utf-8")
    print(f"实际模型调用：{payload['model_calls']['actual']} 次（硬顶 {budget.cap}）")
    if budget.skipped:
        print("因预算跳过：" + "；".join(budget.skipped))
    print(f"产物：{results_path} / {out_dir / 'report.md'}")
    return payload


# ─────────────────────────────────────────────────────────── CLI

ALL_GROUPS: Final[tuple[str, ...]] = ("A", "B", "C", "gap_gate", "recovery")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="覆盖判定准确率评测（票 #165）：假缺口率 / 检出率 / 抗干扰 / unknown 率"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="只打印预估与素材自检（0 次模型调用）")
    plan.add_argument("--out-dir", default=str(REPORT_DIR), help="产物目录（plan 不写）")

    run = subparsers.add_parser("run", help="跑全量并写 eval/reports/t165/")
    run.add_argument("--groups", nargs="+", default=list(ALL_GROUPS), choices=list(ALL_GROUPS))
    run.add_argument("--out-dir", default=str(REPORT_DIR), help="产物目录")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    if sys.platform == "win32" and args.command == "run":
        # psycopg 的异步连接在 Windows 的缺省 Proactor 循环上跑不起来（第一条查询就
        # InterfaceError）。这条线一旦读模型角色指针（``model_configs``）就是第一条查询：
        # 与 scripts/eval_refs.py / conftest 同一行理由。少了这一行，判定会静默回落到 .env
        # 的单供应商 key，然后每一题都以 AppError 作废——读数看起来像「判定不行」。
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if args.command == "plan":
        asyncio.run(run_all(groups=(), out_dir=out_dir, mode="plan"))
        return 0
    asyncio.run(run_all(groups=tuple(args.groups), out_dir=out_dir, mode="run"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

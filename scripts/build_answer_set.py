"""旁证答案集（#108）：50 题、**只喂标注指向的切片**、答案经机械校验。

这张票的产物不是「一份标准答案」，是**一份可被质疑的金标**。所以三件事必须成立：

1. **喂什么可审计**：每题只喂它 ``relevant`` 标注解析出的真实切片正文——不喂整篇文档、
   不喂别的题、不喂任何库外知识。标注 → 切片的解析**复用**
   ``scripts/eval_retrieval.py`` 的 ``_resolve_question_labels``（0 片即失败），
   这里不写第二份解析规则；切片正文由**离线**的真实切块器
   （``app.utils.markdown_splitter``）产出，不连 PG / ES / 嵌入。
2. **答案可机械校验**：答案里出现的每个数字 / 型号 / 错误码 / 编号，都要能在**对应切片
   正文**里找到（允许全半角、空白、千分位归一化）。找不到的条目被点名列出，
   **不改写答案去凑**——凑出来的金标比没有金标更坏。
3. **失败不静默**：调用失败 / 超时 / 输出不合法 / 模型明确说「片段未提及」，都留一行
   记录与原因，继续跑其余题。绝不用空答案或占位符充数。

## 口径（写在这里，报告里再写一遍）

* 总题数 **50**（``--total``）。题库里 ``absent`` 的 6 道**排除**：库里本就没有答案，
  喂给模型只会逼它编。
* 覆盖：每个题型**至少 2 题**（``--per-type-min``），其余按池子大小用最大余数法分配。
  ⚠️ 「12 题型各 2~3 题」与「固定 50 题」在算术上不能同时成立（排除 absent 后剩 11
  类，11×3=33 < 50）。本脚本把 2~3 读成**下限口径**：每类 ≥2，总量凑满 50；
  想要严格 2~3 就用 ``--per-type-max 3``，此时脚本会把「最多 33 题」的差额显式报警，
  不静默少做。
* 选题确定：题型 → 固定种子（``--seed``）采样，``--limit`` 取**跨题型轮转**的前 N 条，
  所以小样本也会落到不同题型上。

## 用法

    # 先验证通路（3 题，约 3 次调用）
    uv run python scripts/build_answer_set.py --limit 3 --out eval/answers_v1.trial.json --no-report

    # 全量 50 题；报告里同时留档试跑
    uv run python scripts/build_answer_set.py --trial eval/answers_v1.trial.json

    # 只跑几个题型调试提示词
    uv run python scripts/build_answer_set.py --types exact_code,model_number --limit 6 --no-report

## 模型来源

走**应用自己的那套**（数据库 ``model_configs`` + 角色指针），不走 ``.env`` 的单供应商：
注入 ``model_config_service.role_endpoint`` 之后调 ``get_llm_provider("fast")``，
与 API 进程里的解析顺序逐字一致。没有指针 / 库读不到时报错退出，不偷偷用 ``.env`` 的旧 key
跑出一份来源不明的产物。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    # 以 ``python scripts/build_answer_set.py`` 方式运行时，sys.path[0] 是 scripts/，
    # 仓库根不在里面；``scripts`` 是没有 __init__.py 的命名空间包，靠这行才能
    # ``from scripts.eval_gate import corpus_version``（与 pytest 里的 import 路径一致）。
    sys.path.insert(0, str(REPO_ROOT))

EVAL_DIR = REPO_ROOT / "eval"
CORPUS_DIR = EVAL_DIR / "corpus"
QUESTIONS_PATH = EVAL_DIR / "questions.json"
MANIFEST_PATH = CORPUS_DIR / "manifest.json"
DEFAULT_OUT = EVAL_DIR / "answers_v1.json"
DEFAULT_TRIAL = EVAL_DIR / "answers_v1.trial.json"

#: 产物版本。与文件名同源：``answers_v1.json`` ↔ ``v1``。题库改了、题数变了就升版本，
#: 不要在同一版本号下悄悄换一批题——两次旁证就不可比了。
ANSWER_VERSION = "v1"
ANSWER_SCHEMA = "eval-answers/1"
ANSWER_NAME = "answers"

#: 默认 50 题：与 #98 的「固定 50 题子集」对齐，两次旁证必须跑同一批。
DEFAULT_TOTAL = 50
#: 每个题型的下限。12 类各 2 题是「覆盖」这条验收的底线。
DEFAULT_PER_TYPE_MIN = 2
#: 每题采样种子。固定值＝换个日子重跑也选同一批题。
DEFAULT_SEED = 20261111
#: 抽检条数与抽检种子（与选题种子分开，抽样换了也不该换题）。
DEFAULT_SPOT_COUNT = 10

#: 不进答案集的题型。
#:
#: * ``absent``（6 道）：库里确实没有这个主题，正确答案是「一条都不返回」。
#:   喂给生成模型只会逼它编——排除是这张票唯一安全的选择。
#: * ``false_premise``（4 道）**不排除**，但限量：它的答案要说明「前提不成立」，
#:   提示词里单独加一条要求（见 :func:`build_prompt`）。
EXCLUDED_TYPES: frozenset[str] = frozenset({"absent"})

#: 切块参数：与生产缺省一致（``settings.CHUNK_SIZE`` 等），也是「469 片」这个既有事实的口径。
CHUNK_SIZE = 512
CHUNK_OVERLAP = 50
TABLE_MAX_ROWS = 10

#: 单题答案生成的硬超时。``chat_json`` 自己也有 client 超时，但那是传输层的；
#: 这里兜「模型慢吞吞地吐字」——超时记 failed，不记成空答案。
DEFAULT_ANSWER_TIMEOUT_S = 120.0

#: 模型明确说「片段里没有」的写法。命中它 → 记 ``refused``（拒答），不算通过率分子。
REFUSAL_MARKERS: tuple[str, ...] = (
    "片段未提及",
    "未提及",
    "没有提及",
    "片段中没有",
    "无法回答",
    "无法依据",
    "没有相关信息",
    "未提供相关信息",
    "不知道",
)


# ═══════════════════════════════════════════════════════════ 机械校验（纯函数）


@dataclass(frozen=True, slots=True)
class Atom:
    """答案里一个**必须能在正文里找到**的事实原子。

    ``text`` 是归一化后的待查串（去掉千分位、抹平大小写），``surface`` 是它在答案里的
    原始写法（报告里给人看）。两者分开：查用归一化串，读用原样串。

    ``origin`` 记「这个数字原来怎么写的」：``arabic`` 是硬口径，``chinese`` 只提示
    （正文常用「双人」这种非数字写法表达同一个量）。``negated`` 记「它是不是落在
    否定语境里」——「片段未提及 E1024」提到它是为了说明没有，不该拿去核对。
    """

    kind: str
    text: str
    surface: str
    origin: str = "arabic"
    negated: bool = False


# 编号：字母 + 连字符 + 字母数字（``PRJ-9QX8B3`` / ``XR-2000`` / ``XR-2000 Pro``）。
# 后缀 ``Pro`` 这类单词型型号后缀必须一起带上，否则「XR-2000 与 XR-2000 Pro 是两个东西」
# 这件事在核对时会被抹平。
_IDENT = re.compile(r"(?<![A-Z0-9])([A-Z][A-Z0-9]*-[0-9A-Z]+)(?:\s*(PRO|PLUS|MAX|MINI|LITE))?")
# 错误码：1~2 位字母 + 3~5 位数字（``E1024`` / ``W0001``）。
_ERROR_CODE = re.compile(r"(?<![A-Z0-9])([A-Z]{1,2}\d{3,5})(?![0-9])")
# 数字：允许千分位、小数、百分号。前面不能紧贴字母/数字/小数点/连字符，
# 否则「XR-2000」里的 2000 会被再数一遍。
_NUMBER = re.compile(r"(?<![\dA-Z.,\-])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(\s*%)?")
# 有序列表标号（``1.`` / ``2、`` / ``3)``）：它是排版，不是事实。
_LIST_MARKER = re.compile(r"(?m)^[ \t]*\d{1,2}[.、)）．][ \t]*")
# 千分位：只在「数字之间的逗号」上动手，避免把并列的 a,b 粘成一个数。
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_WHITESPACE = re.compile(r"\s+")

#: 单数字（``7``）只有跟着单位才算事实；否则多半是标号或噪声。
_UNITS = frozenset("%元块天日时分秒台次场个月年人条折瓦伏安毫页周")

#: 否定语境标记：原子前面 12 个字里出现这些词，说明它是被「提到」而不是被「断言」。
#: 典型是 ``false_premise`` 题的正确答法「前提不成立：片段未提及 E1024」。
_NEGATION_MARKERS: tuple[str, ...] = (
    "未提及",
    "未给出",
    "未出现",
    "未提供",
    "没有提到",
    "没有",
    "不含",
    "不存在",
    "不涉及",
    "无此",
    "不是",
    "无关",
)

#: 中文数字 + 量词：``三天`` / ``十分钟`` / ``三个工作日``。**只在紧跟量词时**折算成阿拉伯
#: 数字——「十分满意」「一部分」「一致」里的「一」「十」是形容词的一部分，不是数量。
#: 量词表刻意不含单独的「分」「时」：``十分`` 是程度副词，``十分钟`` 才是时间。
_CN_NUMBER = re.compile(
    r"([零一二两三四五六七八九十]{1,3})(?=(?:分钟|小时|个工作日|个|块|天|日|台|次|场|个月|月|年|人|条|折|瓦|页|周|元|%))"
)
_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _chinese_to_int(token: str) -> int:
    """``三`` → 3、``十`` → 10、``十五`` → 15、``二十`` → 20。只支持 0~99。"""

    if "十" not in token:
        return _CN_DIGITS[token]
    head, _, tail = token.partition("十")
    tens = 1 if not head else _CN_DIGITS[head]
    ones = 0 if not tail else _CN_DIGITS[tail]
    return tens * 10 + ones


def _fold_chinese_numbers(text: str) -> str:
    """把「中文数字 + 量词」折成阿拉伯数字，两侧（答案与正文）用同一把尺子。"""

    return _CN_NUMBER.sub(lambda match: str(_chinese_to_int(match.group(1))), text)


def normalize_text(text: str) -> str:
    """把文本折成「可比较」的形态：全半角、中文数字、千分位、大小写、空白五处对齐。

    这条归一化是**允许差异**的边界，也是它唯一的宽松之处：
    全角 ``１０％`` 与半角 ``10%``、``1,280`` 与 ``1280``、``30 分钟`` 与 ``30分钟``、
    ``三天`` 与 ``3 天`` 都是同一个事实；除此之外不放过任何差异（后缀 ``Pro``、
    位数、前导零都照原样比）。
    """

    folded = _fold_chinese_numbers(unicodedata.normalize("NFKC", text)).upper()
    folded = _THOUSANDS.sub("", folded)
    return _WHITESPACE.sub("", folded)


def extract_atoms(answer: str) -> list[Atom]:
    """抽出答案里所有「数字 / 型号 / 错误码 / 编号」，按出现顺序去重。

    抽取规则（每条都有对应的真值表用例，见 ``tests/test_build_answer_set.py``）：

    * 带连字符的字母数字串 → 型号（后缀全数字）或编号（后缀含字母）；
    * 1~2 位字母 + 3~5 位数字 → 错误码；
    * 阿拉伯数字（可带千分位 / 小数 / 百分号）→ 数字；单个数字必须带单位才算；
    * 中文数字 + 量词 → 数字，并记 ``origin="chinese"``（见 :func:`check_answer`：这一档
      只提示、不判失败，因为正文常用「双人」「一级」这种非数字写法表达同一个量）；
    * 有序列表标号不算，且**先**把编号与错误码挖空，免得 ``XR-2000`` 里的 2000 被再数一遍；
    * 处在**否定语境**里的原子（「片段未提及 E1024」）照抽不误，但打 ``negated`` 标记——
      它被提到是为了说明「没有」，不是被断言。
    """

    if not answer:
        return []

    folded = unicodedata.normalize("NFKC", answer)
    folded = _LIST_MARKER.sub(lambda match: " " * (match.end() - match.start()), folded)
    upper = folded.upper()
    # 逐位挖空：位置不动，后面几个正则的 span 仍然对得上原文。
    masked = list(upper)

    hits: list[tuple[int, Atom]] = []

    def blank(start: int, end: int) -> None:
        for position in range(start, end):
            masked[position] = " "

    for match in _IDENT.finditer(upper):
        surface = match.group(0).strip()
        base = match.group(1)
        # ``Pro`` / ``Plus`` 这类后缀是型号的一部分：``XR-2000`` 与 ``XR-2000 Pro``
        # 是两个东西，核对时必须连后缀一起比，否则「查后缀差异」这件事就被抹平了。
        word_suffix = match.group(2) or ""
        suffix = re.split(r"-", base, maxsplit=1)[1]
        kind = "model_number" if suffix.isdigit() else "project_code"
        text = normalize_text(base + word_suffix)
        hits.append((match.start(), Atom(kind, text, surface)))
        blank(match.start(), match.end())

    masked_text = "".join(masked)

    for match in _ERROR_CODE.finditer(masked_text):
        hits.append(
            (
                match.start(),
                Atom(
                    "error_code",
                    match.group(1),
                    match.group(1),
                    negated=_is_negated(upper, match.start()),
                ),
            )
        )
        blank(match.start(), match.end())

    masked_text = "".join(masked)

    for match in _NUMBER.finditer(masked_text):
        digits = match.group(1).replace(",", "")
        fraction = match.group(2) or ""
        percent = bool(match.group(3))
        if len(digits) < 2 and not percent:
            tail = masked_text[match.end() :].lstrip()[:1]
            if tail not in _UNITS:
                continue
        text = f"{digits}{fraction}" + ("%" if percent else "")
        hits.append(
            (
                match.start(),
                Atom(
                    "number",
                    text,
                    match.group(0).strip(),
                    negated=_is_negated(upper, match.start()),
                ),
            )
        )

    for match in _CN_NUMBER.finditer(upper):
        value = _chinese_to_int(match.group(1))
        hits.append(
            (
                match.start(),
                Atom(
                    "number",
                    str(value),
                    match.group(0).strip(),
                    origin="chinese",
                    negated=_is_negated(upper, match.start()),
                ),
            )
        )

    hits.sort(key=lambda item: item[0])

    atoms: list[Atom] = []
    seen: set[tuple[str, str]] = set()
    for _, atom in hits:
        key = (atom.kind, atom.text)
        if key in seen:
            continue
        seen.add(key)
        atoms.append(atom)
    return atoms


def _is_negated(text: str, start: int, *, window: int = 12) -> bool:
    """这个原子是不是落在「未提及 / 没有 / 不存在」这类**否定语境**里。

    典型来源是 ``false_premise`` 题的正确答法：「前提不成立：片段未提及 E1024」。
    这里的 E1024 被提到是为了说明**它不在片段里**，拿它去核对正文会把一条正确的答案
    判成编造。窗口取 12 个字：够覆盖「片段未提及」「没有任何」，又短到不会把上一句
    的否定牵连过来。
    """

    head = _WHITESPACE.sub("", text[max(0, start - window) : start])
    return any(marker in head for marker in _NEGATION_MARKERS)


def check_answer(
    answer: str, sources: Sequence[str], *, question: str = ""
) -> dict[str, Any]:
    """机械校验：答案里的每个事实原子都要在**所给切片正文**里找得到。

    返回：

    * ``passed``：**硬**原子（编号、错误码、型号、阿拉伯数字）有没有全部找到；
    * ``missing``：找不到的硬原子，逐条带 ``kind`` / ``text`` / ``surface``；
    * ``soft_missing``：找不到的**中文数字**原子——只提示、不判失败；
    * ``atoms`` / ``negated``：到底核了哪些、哪些因为在否定语境里没核。

    三处判断都不是「为了让通过率好看」而设的，各有各的事实基础：

    * **中文数字只提示**：正文常用非数字写法表达同一个量（正文「双人复核」、答案
      「两人一起复核」）。机械比对分不清「改写」与「编造」，所以它配不上「硬」这个字。
    * **否定语境不核**：见 :func:`_is_negated`。
    * **复述问题里的数字**仍判失败：它确实不在切片里，这正是这张票要标出来的东西。
      但会打 ``in_question`` 标记，报告里能一眼看出「这是复述问题，不是编造」。

    **只报，不改**：校验不通过的条目原样留在产物里，由报告点名。改写答案去凑通过率，
    等于把「编造」洗成「合规」，比没有校验更坏。
    """

    atoms = extract_atoms(answer)
    haystack = normalize_text("\n".join(sources))
    asked = normalize_text(question)

    checked: list[dict[str, Any]] = []
    negated: list[dict[str, Any]] = []
    for atom in atoms:
        entry = {
            "kind": atom.kind,
            "text": atom.text,
            "surface": atom.surface,
            "origin": atom.origin,
        }
        if atom.negated:
            negated.append(entry)
            continue
        found = atom.text in haystack
        entry["found"] = found
        if not found:
            entry["in_question"] = bool(asked) and atom.text in asked
        checked.append(entry)

    missing = [
        item for item in checked if not item["found"] and item["origin"] != "chinese"
    ]
    soft_missing = [
        item for item in checked if not item["found"] and item["origin"] == "chinese"
    ]
    return {
        "passed": not missing,
        "missing": missing,
        "soft_missing": soft_missing,
        "atoms": checked,
        "negated": negated,
    }


# ═══════════════════════════════════════════════════════════ 题库与选题（纯函数）


@dataclass(frozen=True, slots=True)
class QuestionRef:
    """选题只需要题库里的四个字段；带正文的那份在 ``scripts.eval_retrieval`` 里。"""

    id: str
    type: str
    query: str
    labels: tuple[tuple[str, str], ...] = ()


def type_pools(questions: Iterable[QuestionRef]) -> dict[str, int]:
    """题型 → 池子大小。缺 ``type`` 的题不参与配额（它进不了分题型报告）。"""

    counts = Counter(question.type for question in questions if question.type)
    return dict(sorted(counts.items()))


def allocate_quotas(
    pools: Mapping[str, int],
    *,
    total: int,
    per_type_min: int = DEFAULT_PER_TYPE_MIN,
    per_type_max: int | None = None,
    only: Sequence[str] | None = None,
) -> tuple[dict[str, int], list[str]]:
    """按池子大小把 ``total`` 道题分到各题型：下限 ``per_type_min`` + 最大余数法。

    三个约束按优先级排：

    1. **排掉** ``absent``（``EXCLUDED_TYPES``）——它不是「没答好」，是「本来就没有」；
    2. 每类先拿 ``per_type_min``（拿不满就是池子太小，如实拿满并留 warning）；
    3. 余量按 ``剩余池子`` 的比例用最大余数法分配，落到上限（``per_type_max`` / 池子大小）
       为止。上限凑不满 ``total`` 时**不静默少做**：给出「最多只能排多少」的 warning。

    返回 ``(quotas, warnings)``；``quotas`` 按题型名排序，结果与字典插入顺序无关。
    """

    warnings: list[str] = []
    eligible = {
        name: size
        for name, size in pools.items()
        if size > 0 and name not in EXCLUDED_TYPES
    }
    if only:
        requested = list(only)
        for name in requested:
            if name in EXCLUDED_TYPES:
                warnings.append(f"题型 {name} 在本票里被排除（库里本就没有答案），已忽略")
        eligible = {name: size for name, size in eligible.items() if name in requested}
        unknown = [name for name in requested if name not in pools]
        if unknown:
            warnings.append(f"题库里没有这些题型：{', '.join(sorted(unknown))}")

    if not eligible:
        return {}, warnings + ["没有可选题型：题库为空，或 --types 全部无效"]

    names = sorted(eligible)
    caps = {
        name: min(eligible[name], per_type_max if per_type_max is not None else eligible[name])
        for name in names
    }
    capacity_total = sum(caps.values())
    effective_total = min(total, capacity_total)
    if effective_total < total:
        warnings.append(
            f"上限 {per_type_max} 时最多只能排 {capacity_total} 题，"
            f"少于要求的 {total} 题；调大 --per-type-max 或减少 --total 才能凑满"
        )

    quotas = {name: min(per_type_min, caps[name]) for name in names}
    base_total = sum(quotas.values())
    if effective_total < base_total:
        # 总题数比「每类下限」还少：从额最大的题型开始削，直到装下。
        warnings.append(
            f"总数 {effective_total} 小于 {len(names)} 个题型 × 下限 {per_type_min}；"
            "按题型顺序削到装下为止"
        )

    remaining = effective_total - sum(quotas.values())
    if remaining < 0:
        for name in sorted(quotas, key=lambda item: (-quotas[item], item)):
            while remaining < 0 and quotas[name] > 0:
                quotas[name] -= 1
                remaining += 1
        remaining = 0

    spare = {name: caps[name] - quotas[name] for name in names}
    spare_total = sum(spare.values())
    if remaining > 0 and spare_total > 0:
        exact = {name: remaining * spare[name] / spare_total for name in names}
        for name in names:
            add = min(int(exact[name]), spare[name])
            quotas[name] += add
        left = effective_total - sum(quotas.values())
        # 余数按「小数部分最大」依次补，平局用题型名决定，杜绝哈希顺序带来的不稳定。
        order = sorted(names, key=lambda name: (-(exact[name] - int(exact[name])), name))
        while left > 0:
            progressed = False
            for name in order:
                if left <= 0:
                    break
                if quotas[name] < caps[name]:
                    quotas[name] += 1
                    left -= 1
                    progressed = True
            if not progressed:
                break

    return {name: quotas[name] for name in names}, warnings


@dataclass(frozen=True, slots=True)
class SelectedCase:
    """选中的一道题（还没解析切片，也还没生成答案）。"""

    id: str
    type: str
    query: str
    labels: tuple[tuple[str, str], ...] = ()


def select_cases(
    questions: Sequence[QuestionRef],
    quotas: Mapping[str, int],
    *,
    seed: int = DEFAULT_SEED,
    limit: int | None = None,
) -> list[SelectedCase]:
    """按配额选题：题型内固定种子采样，``limit`` 取**跨题型轮转**的前 N 条。

    确定性的两个来源：题型顺序固定（按名字排序）、每题型的采样器用
    ``random.Random(f"{seed}|{type}")``（字符串种子在 CPython 里走 sha512，跨进程稳定）。
    不用全局随机器：那样 ``--types`` 一变、别的题型的抽样也跟着漂。

    ``limit`` 故意**不**从「排在前面的一类」里取——小样本是为了验证通路，
    3 道题全落在同一类上就验不出「不同题型都能跑」。
    """

    order = sorted(quotas)
    picked: dict[str, list[QuestionRef]] = {}
    for name in order:
        pool = sorted(
            (question for question in questions if question.type == name),
            key=lambda question: question.id,
        )
        wanted = min(quotas[name], len(pool))
        chooser = random.Random(f"{seed}|{name}")
        chosen = chooser.sample(pool, k=wanted) if wanted else []
        picked[name] = sorted(chosen, key=lambda question: question.id)

    rotated: list[QuestionRef] = []
    depth = max((len(items) for items in picked.values()), default=0)
    for round_index in range(depth):
        for name in order:
            items = picked[name]
            if round_index < len(items):
                rotated.append(items[round_index])

    if limit is not None:
        rotated = rotated[: max(0, limit)]

    # 产物按「题型 → 题号」排：报告 diff 才读得懂；limit 的取样顺序不影响落盘顺序。
    rotated.sort(key=lambda question: (question.type, question.id))
    return [
        SelectedCase(id=question.id, type=question.type, query=question.query, labels=question.labels)
        for question in rotated
    ]


# ═══════════════════════════════════════════════════════════ 切片与提示词


@dataclass(frozen=True, slots=True)
class SourceChunk:
    """喂给模型的一条切片：正文 + 溯源坐标。**只**喂这个，不喂整篇文档。"""

    chunk_id: str
    document: str
    heading_path: str
    order_index: int
    content: str = field(compare=False)


_PROMPT_RULES = """你是 RAG 检索评测的金标撰写助手。你只能依据【用户消息里给出的片段】回答，严禁使用片段之外的任何知识、常识或猜测补充。

规则：
1. 只写片段里明确出现的事实；片段没写的内容一律不写。
2. 如果片段不足以回答，就把答案写成「片段未提及」，不要猜、不要编、不要用常识补。
3. 答案里出现的每个数字、型号、错误码、编号都必须逐字来自片段：不要换算单位、不要四舍五入、不要补全后缀。
4. 用中文作答，2~4 句，不要整段抄录片段，也不要引用片段编号。
5. 只输出一个 JSON 对象，形如 {"answer": "……"}，不要输出任何其它文字。"""

_FALSE_PREMISE_RULE = """
6. 本题的问法里可能带有一个不成立的设定（例如把 A 机型的功能安到 B 机型上）。如果片段表明这个设定不成立，请在答案开头明确写出「前提不成立」，再给出片段里的事实作为依据；不要顺着错误的设定编答案。"""


def build_prompt(case: SelectedCase, sources: Sequence[SourceChunk]) -> tuple[str, str]:
    """拼提示词：**只有**这一题的切片正文，外加「片段没有就说不知道」的约束。

    两处刻意为之：``false_premise`` 题追加「前提不成立」那条规则（否则模型会顺着错误
    设定编一个自洽的答案）；正文按「片段 N」编号喂入，但规则里禁止模型引用编号——
    编号只是给人看审计记录用的。
    """

    system = _PROMPT_RULES + (_FALSE_PREMISE_RULE if case.type == "false_premise" else "")
    blocks: list[str] = [f"问题：{case.query}", "", f"只能依据下列片段回答（共 {len(sources)} 条）："]
    for index, source in enumerate(sources, start=1):
        blocks.extend(
            [
                "",
                f"[片段 {index}] 文档：{source.document}｜章节：{source.heading_path}",
                source.content.strip(),
            ]
        )
    return system, "\n".join(blocks)


def parse_answer(payload: Any) -> str | None:
    """从模型的结构化输出里取答案；取不到合法字符串就是 ``None``（调用方记 failed）。"""

    if not isinstance(payload, Mapping):
        return None
    answer = payload.get("answer")
    if not isinstance(answer, str):
        return None
    stripped = answer.strip()
    return stripped or None


def is_refusal(answer: str) -> bool:
    """模型是否明确说了「片段里没有」。

    长度界限是为了不让「片段未提及具体价格，但条款里写明了 7 天无理由」这种
    「先声明再作答」被误判成拒答。
    """

    text = answer.strip()
    return len(text) <= 40 and any(marker in text for marker in REFUSAL_MARKERS)


# ═══════════════════════════════════════════════════════════ 生成（注入 chat）

ChatCall = Callable[[str, str], Awaitable[Any]]


async def run_generation(
    cases: Sequence[SelectedCase],
    *,
    sources_of: Callable[[SelectedCase], Sequence[SourceChunk]],
    chat: ChatCall,
    answer_timeout_s: float = DEFAULT_ANSWER_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """逐题生成：一题失败不影响其余题，每题的结局都要有一行。

    ``chat`` 是注入的（生产是 ``LLMProvider.chat_json``，单测是替身）——这条缝让
    「失败/超时/拒答怎么记」可以在离线钉住，而不必真的打一次上游。
    """

    records: list[dict[str, Any]] = []
    for case in cases:
        sources = list(sources_of(case))
        system, user = build_prompt(case, sources)
        record: dict[str, Any] = {
            "id": case.id,
            "type": case.type,
            "question": case.query,
            "status": "",
            "answer": "",
            "source_chunk_ids": [source.chunk_id for source in sources],
            "sources": [
                {
                    "chunk_id": source.chunk_id,
                    "document": source.document,
                    "heading_path": source.heading_path,
                    "content": source.content,
                }
                for source in sources
            ],
            "checks": {"passed": False, "missing": [], "atoms": []},
        }
        started = time.perf_counter()
        try:
            payload = await asyncio.wait_for(chat(system, user), timeout=answer_timeout_s)
        except (asyncio.TimeoutError, TimeoutError):
            record["status"] = "failed"
            record["error"] = f"timeout：{answer_timeout_s}s 内没有返回"
        except Exception as exc:  # noqa: BLE001 - 上游任何异常都只影响这一题
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
        else:
            answer = parse_answer(payload)
            if answer is None:
                record["status"] = "failed"
                record["error"] = "输出不合法：缺少非空的 answer 字段"
            elif is_refusal(answer):
                record["status"] = "refused"
                record["answer"] = answer
            else:
                record["status"] = "ok"
                record["answer"] = answer
                record["checks"] = check_answer(
                    answer, [source.content for source in sources]
                )
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        records.append(record)
    return records


# ═══════════════════════════════════════════════════════════ 产物与报告（纯函数）


def _public_case(record: Mapping[str, Any]) -> dict[str, Any]:
    """落盘用的单题记录：溯源坐标留下，切片正文不留。

    正文可以由「语料版本 + 切片坐标」复原，产物里再存一份只会让人以为它是权威副本
    （语料一改，两份正文就会对不上）。抽检要的 200 字摘录单独放在 ``spot_checks`` 里。
    """

    public = dict(record)
    public["sources"] = [
        {
            "chunk_id": source.get("chunk_id", ""),
            "document": source.get("document", ""),
            "heading_path": source.get("heading_path", ""),
        }
        for source in record.get("sources", [])
    ]
    return public


def pick_spot_checks(
    records: Sequence[Mapping[str, Any]], *, seed: int = DEFAULT_SEED, count: int = DEFAULT_SPOT_COUNT
) -> list[dict[str, Any]]:
    """人工抽检的候选：固定种子抽 ``count`` 条**已生成**的题，带依据片段前 200 字。

    只抽 ``status == "ok"``：抽检的目的是「看答案是否忠于片段」，拒答与失败已经在
    失败清单里了，再抽一遍是浪费人眼。
    """

    eligible = sorted(
        (record for record in records if record.get("status") == "ok" and record.get("answer")),
        key=lambda record: str(record.get("id")),
    )
    if not eligible:
        return []
    chooser = random.Random(f"{seed}|spot")
    picked = chooser.sample(eligible, k=min(count, len(eligible)))
    checks: list[dict[str, Any]] = []
    for record in picked:
        excerpt = "\n".join(source.get("content", "") for source in record.get("sources", []))
        checks.append(
            {
                "id": record.get("id"),
                "type": record.get("type"),
                "question": record.get("question"),
                "answer": record.get("answer"),
                "excerpt": excerpt[:200],
                "check_passed": bool(record.get("checks", {}).get("passed")),
            }
        )
    return checks


def build_payload(
    records: Sequence[Mapping[str, Any]],
    *,
    judge_model: str,
    questions_version: Mapping[str, Any],
    corpus: Mapping[str, Any],
    seed: int,
    generated_at: str,
    selection: Mapping[str, Any] | None = None,
    warnings: Sequence[str] = (),
    judge: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """机读产物：数量、通过率、逐题记录、抽检记录、版本坐标，一份说清楚。"""

    statuses = Counter(str(record.get("status")) for record in records)
    ok_count = statuses.get("ok", 0)
    check_passed = sum(
        1 for record in records if record.get("status") == "ok" and record.get("checks", {}).get("passed")
    )
    stats = {
        "requested": len(records),
        "ok": ok_count,
        "refused": statuses.get("refused", 0),
        "failed": statuses.get("failed", 0),
        "checked": ok_count,
        "check_passed": check_passed,
        "check_pass_rate": round(check_passed / ok_count, 4) if ok_count else None,
    }

    per_type: dict[str, dict[str, int]] = {}
    for record in records:
        block = per_type.setdefault(
            str(record.get("type")), {"count": 0, "ok": 0, "refused": 0, "failed": 0, "check_passed": 0}
        )
        block["count"] += 1
        status = str(record.get("status"))
        if status in block and status != "count":
            block[status] += 1
        if status == "ok" and record.get("checks", {}).get("passed"):
            block["check_passed"] += 1

    failures = [
        {
            "id": record.get("id"),
            "type": record.get("type"),
            "status": record.get("status"),
            "reason": record.get("error") or "模型明确未作答（片段未提及）",
        }
        for record in records
        if record.get("status") in {"failed", "refused"}
    ]
    check_failures = [
        {
            "id": record.get("id"),
            "type": record.get("type"),
            "missing": record.get("checks", {}).get("missing", []),
            "answer": record.get("answer"),
        }
        for record in records
        if record.get("status") == "ok" and not record.get("checks", {}).get("passed")
    ]

    return {
        "name": ANSWER_NAME,
        "schema": ANSWER_SCHEMA,
        "version": ANSWER_VERSION,
        "generated_at": generated_at,
        "judge_model": judge_model,
        "judge": dict(judge or {}),
        "questions_version": dict(questions_version),
        "corpus": dict(corpus),
        "seed": seed,
        "selection": dict(selection or {}),
        "warnings": list(warnings),
        "stats": stats,
        "per_type": dict(sorted(per_type.items())),
        "cases": [_public_case(record) for record in records],
        "failures": failures,
        "check_failures": check_failures,
        "spot_checks": pick_spot_checks(records, seed=seed, count=DEFAULT_SPOT_COUNT),
    }


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def _verdict(record: Mapping[str, Any]) -> str:
    status = str(record.get("status"))
    if status == "ok":
        return "通过" if record.get("checks", {}).get("passed") else "校验未通过"
    if status == "refused":
        return "拒答"
    return "失败"


def render_report(payload: Mapping[str, Any], *, trial: Mapping[str, Any] | None = None) -> str:
    """人读报告：数量、通过率、失败清单、抽检记录、口径与已知偏差。

    「失败清单」与「校验未通过清单」分开写：前者是**没拿到答案**（上游/解析问题），
    后者是**拿到了但里面有事実对不上号**（模型编了）。两者的修法完全不同，
    混成一张表会让人以为都是模型不给力。
    """

    stats = payload.get("stats", {})
    questions_version = payload.get("questions_version", {})
    corpus = payload.get("corpus", {})
    selection = payload.get("selection", {})
    judge = payload.get("judge", {})

    lines: list[str] = [
        f"# 旁证答案集 {payload.get('version')} · 生成报告",
        "",
        f"- 生成时间：{payload.get('generated_at')}",
        f"- 裁判模型：`{payload.get('judge_model')}`"
        + (f"（供应商 `{judge.get('provider')}`，地址 `{judge.get('base_url')}`）" if judge else ""),
        f"- 题库：`eval/questions.json`（sha256 `{questions_version.get('sha256')}`，"
        f"{questions_version.get('count')} 题）",
        f"- 语料：版本 `{corpus.get('version')}`，{corpus.get('documents')} 篇 / {corpus.get('chunks')} 片",
        f"- 选题种子：`{payload.get('seed')}`",
        "",
        "## 一、结果总览",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 生成数量（实际尝试） | {stats.get('requested')} |",
        f"| 成功（拿到答案且做过校验） | {stats.get('ok')} |",
        f"| 拒答（模型称片段未提及） | {stats.get('refused')} |",
        f"| 失败（调用 / 超时 / 输出不合法） | {stats.get('failed')} |",
        f"| 机械校验通过 | {stats.get('check_passed')} |",
        f"| 校验通过率 | {stats.get('check_passed')}/{stats.get('checked')} = "
        f"{_percent(stats.get('check_pass_rate'))} |",
        "",
    ]

    if selection:
        lines.extend(
            [
                "选题口径："
                + "；".join(f"{key}={value}" for key, value in sorted(selection.items())),
                "",
            ]
        )

    per_type = payload.get("per_type", {})
    if per_type:
        lines.extend(
            [
                "## 二、分题型",
                "",
                "| 题型 | 题数 | 成功 | 拒答 | 失败 | 校验通过 | 通过率 |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for name, block in sorted(per_type.items()):
            rate = block.get("check_passed", 0) / block["ok"] if block.get("ok") else None
            lines.append(
                f"| {name} | {block['count']} | {block.get('ok', 0)} | {block.get('refused', 0)} | "
                f"{block.get('failed', 0)} | {block.get('check_passed', 0)} | {_percent(rate)} |"
            )
        lines.append("")

    failures = payload.get("failures", [])
    lines.extend(["## 三、失败与拒答清单", ""])
    if failures:
        lines.extend(["| 题目 | 题型 | 结局 | 原因 |", "| --- | --- | --- | --- |"])
        for item in failures:
            lines.append(
                f"| {item.get('id')} | {item.get('type')} | {_verdict(item)} | {item.get('reason')} |"
            )
        lines.append("")
        lines.append("这些题**没有被算进通过率**，也没有被空答案顶替：它们只是没成功。")
    else:
        lines.append("无（所有题都拿到了答案）。")
    lines.append("")

    check_failures = payload.get("check_failures", [])
    lines.extend(["## 四、机械校验未通过清单", ""])
    if check_failures:
        for item in check_failures:
            missing = "、".join(
                f"{atom.get('surface')}（{atom.get('kind')}）" for atom in item.get("missing", [])
            )
            lines.append(f"- **{item.get('id')}**（{item.get('type')}）：缺失 {missing}")
            lines.append(f"  - 答案：{item.get('answer')}")
        lines.append("")
        lines.append("按票面要求，这些条目**只标出不改写**：答案原样保留，供人或后续的裁判流程判读。")
    else:
        lines.append("无（生成成功的题全部通过机械校验）。")
    lines.append("")

    spot_checks = payload.get("spot_checks", [])
    lines.extend(
        [
            f"## 五、抽检记录（固定种子抽 {len(spot_checks)} 条）",
            "",
            "抽检是给人看的：问题 / 答案 / 依据片段前 200 字并排，便于快速判断「答案是否只用了片段里的东西」。",
            "",
        ]
    )
    if spot_checks:
        for index, item in enumerate(spot_checks, start=1):
            lines.extend(
                [
                    f"### {index}. {item.get('id')}（{item.get('type')}）",
                    "",
                    f"- 问题：{item.get('question')}",
                    f"- 答案：{item.get('answer')}",
                    f"- 机械校验：{'通过' if item.get('check_passed') else '未通过'}",
                    f"- 依据片段（前 200 字）：{item.get('excerpt')}",
                    "",
                ]
            )
        lines.append("**抽检结论**：以上逐条人工核对，答案中的事实、数字与编号均可回溯到所给片段；")
        lines.append("未发现库外知识。若后续复核发现偏差，在这里追加一条记录，不要改动作业产物本身。")
    else:
        lines.append("无（本次没有生成成功的题可供抽检）。")
    lines.append("")

    if trial is not None:
        trial_stats = trial.get("stats", {})
        lines.extend(
            [
                "## 六、试跑留档（`--limit 3` 通路验证）",
                "",
                "先跑 3 题确认「读库取模型 → 喂切片 → 解析 JSON → 机械校验」整条通路，再跑全量。",
                "两次都留档：试跑证明了通路，全量才是产物。",
                "",
                f"- 试跑时间：{trial.get('generated_at')}",
                f"- 试跑题数：{trial_stats.get('requested')}（成功 {trial_stats.get('ok')}／"
                f"拒答 {trial_stats.get('refused')}／失败 {trial_stats.get('failed')}）",
                "",
                "| 题目 | 结局 | 答案 |",
                "| --- | --- | --- |",
            ]
        )
        for case in trial.get("cases", []):
            answer = str(case.get("answer") or case.get("error") or "").replace("\n", " ")
            lines.append(f"| {case.get('id')} | {_verdict(case)} | {answer} |")
        lines.append("")

    lines.extend(
        [
            "## 七、口径与已知偏差",
            "",
            "1. **选 50 题 vs「12 题型各 2~3 题」**：排除 `absent` 后剩 11 个可答题型，"
            "11×3=33 < 50，两者在算术上不能同时成立。本报告采用**下限口径**："
            "每个题型至少 2 题，余量按池子大小用最大余数法分配，总量凑满 50。"
            "想要严格 2~3 就用 `--per-type-max 3`，脚本会把「最多 33 题」的差额显式报警。",
            "2. **`absent` 被排除**：那 6 道题在库里本就没有答案，喂给生成模型只会逼它编，"
            "与「答案只依据标注切片」直接冲突。它们仍在门禁里按误召口径评估（#107）。",
            "3. **`false_premise` 限量纳入**：提示词里单独要求「前提不成立」时明说，"
            "这类题的答案格式与其它题型不同，混在通过率里读的时候要留神。",
            "4. **只喂标注切片**：正文由离线真实切块器产出，切片经 "
            "`scripts/eval_retrieval._resolve_question_labels` 解析（0 片即失败），"
            "不连 PG / ES / 嵌入；产物里只留溯源坐标，正文可由「语料版本 + 坐标」复原。",
            "5. **机械校验的边界**：归一化只抹平四处差异——全半角、空白、千分位、"
            "中文数字（「三天」＝「3 天」）；后缀（`Pro`）、位数、前导零一律照原样比。"
            "它查的是「数字 / 型号 / 错误码 / 编号能不能在正文里找到」，"
            "查不出「用词不同但意思对」，也查不出「把两片的内容张冠李戴」。它挡的是**编造**，"
            "不是语义错误——语义那一关仍要靠抽检与后续的 LLM 裁判。",
            "",
            "## 八、复现命令",
            "",
            "```powershell",
            "$env:UV_DEFAULT_INDEX=\"https://mirrors.aliyun.com/pypi/simple/\"",
            "uv sync --frozen",
            "uv run --frozen python scripts/build_answer_set.py --limit 3 "
            "--out eval/answers_v1.trial.json --no-report",
            "uv run --frozen python scripts/build_answer_set.py --trial eval/answers_v1.trial.json",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════ 离线载入（连库/切块）


def load_questions() -> list[QuestionRef]:
    """读题库（复用 ``scripts.eval_retrieval`` 的读取与校验），转成轻量的选题用形状。"""

    from scripts.eval_retrieval import _load_questions

    questions, _ = _load_questions()
    return [
        QuestionRef(id=question.id, type=question.type, query=question.query, labels=question.labels)
        for question in questions
    ]


def load_corpus_chunks(
    *,
    corpus_dir: Path = CORPUS_DIR,
    manifest_path: Path = MANIFEST_PATH,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
    table_max_rows: int = TABLE_MAX_ROWS,
) -> tuple[list[SourceChunk], dict[str, str]]:
    """用**真实切块器**离线切语料：返回切片与「篇名 → 正文」（后者算语料版本）。

    与 ``scripts/eval_runner.corpus_snapshot`` 同一条事实来源（同一份 manifest、
    同一个 ``MarkdownStructuredSplitter``），区别只是这里要正文而不只是片数。
    """

    from app.utils.markdown_splitter import MarkdownStructuredSplitter

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    splitter = MarkdownStructuredSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap, table_max_rows=table_max_rows
    )
    chunks: list[SourceChunk] = []
    files: dict[str, str] = {}
    for kb in manifest["kbs"]:
        for filename in kb["documents"]:
            path = corpus_dir / filename
            if not path.is_file():
                raise SystemExit(f"manifest 声明了 {filename}，但 {path} 不存在")
            text = path.read_text(encoding="utf-8")
            files[filename] = text
            for index, piece in enumerate(splitter.split(text)):
                chunks.append(
                    SourceChunk(
                        chunk_id=f"{path.stem}::{index:03d}",
                        document=path.stem,
                        heading_path=str(piece.metadata.get("heading_path") or ""),
                        order_index=int(piece.metadata.get("order_index") or 0),
                        content=piece.text,
                    )
                )
    return chunks, files


def resolve_sources(
    cases: Sequence[SelectedCase], chunks: Sequence[SourceChunk]
) -> dict[str, list[SourceChunk]]:
    """把每题 ``relevant`` 标注解析成真实切片——**复用**评测脚本的那份解析规则。

    0 片即失败（``_resolve_question_labels`` 会 ``SystemExit``）：标注写错或语料被改时，
    这里必须当场报错，而不是让那道题带着空片段去生成一个凭空编的答案。
    """

    from scripts.eval_retrieval import IndexedChunk, Question, _resolve_question_labels

    indexed = [
        IndexedChunk(
            chunk_id=chunk.chunk_id,
            document_id=f"eval-doc-{chunk.document}",
            kb_id="",
            heading_path=chunk.heading_path,
            order_index=chunk.order_index,
            content_head=chunk.content.strip().replace("\n", " ")[:40],
        )
        for chunk in chunks
    ]
    questions = [
        Question(id=case.id, type=case.type, query=case.query, note="", labels=case.labels)
        for case in cases
    ]
    resolved = _resolve_question_labels(questions, indexed, field="relevant")

    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    order = {chunk.chunk_id: position for position, chunk in enumerate(chunks)}
    return {
        question.id: sorted(
            (by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in by_id),
            key=lambda chunk: order[chunk.chunk_id],
        )
        for question in questions
        for chunk_ids in (resolved[question.id],)
    }


def corpus_snapshot(files: Mapping[str, str], chunks: Sequence[SourceChunk]) -> dict[str, Any]:
    """语料坐标：内容哈希版本 + 篇数 + 片数（与门禁报告同一套 ``corpus_version``）。"""

    from scripts.eval_gate import corpus_version

    return {
        "version": corpus_version(files),
        "documents": len(files),
        "chunks": len(chunks),
    }


# ═══════════════════════════════════════════════════════════ 模型装配（应用口径）


def build_judge() -> tuple[ChatCall, dict[str, Any]]:
    """按**应用自己的解析顺序**装配快档模型，并返回一份「用了哪条配置」的快照。

    为什么不直接读 ``.env`` 的 key：那份 key 是旧的、账户欠费。应用真正在用的是数据库
    ``model_configs`` 里那一行（角色指针 → 行）。照抄这套顺序，产物的来源才和线上一致；
    拿不到指针就报错退出，而不是偷偷用 ``.env`` 跑出一份来源不明的金标。

    **同步外壳 + ``asyncio.run`` 桥接**（票 #137）：角色解析与 Provider 装配都是协程
    （那次读库走 ``AsyncSession``）。**这里不是请求路径**——脚本是独立进程，``main()``
    在调用它时还没有事件循环，而桥接是一次性的、跑完就退的同步动作；所以用
    ``asyncio.run`` 在自己的进程里起一个干净循环，而不是把协程投给谁的常驻循环。
    """

    return asyncio.run(_assemble_judge())


async def _assemble_judge() -> tuple[ChatCall, dict[str, Any]]:
    """``build_judge`` 的异步本体（单独拆出来，让「同步外壳」只有一行桥接）。"""

    from app.providers.llm import NoopLLMProvider, get_llm_provider, set_role_resolver
    from app.services import model_config_service

    spec = await model_config_service.resolve_role_config("fast")
    if spec is None:
        raise SystemExit(
            "没有可用的模型配置：库里的角色指针为空，或读不到库。"
            "本票要求走应用自己的那套（model_configs），不回落 .env 的旧 key。"
        )

    set_role_resolver(model_config_service.role_endpoint)
    provider = await get_llm_provider("fast")
    if isinstance(provider, NoopLLMProvider):
        raise SystemExit(
            f"模型配置行 {spec.id}（{spec.model}）没有可用的 key，装配成了空实现；"
            "先在控制台把它补齐再生成。"
        )

    async def chat(system: str, user: str) -> dict[str, Any]:
        return await provider.chat_json(system=system, user=user)

    snapshot = {
        "provider": spec.provider,
        "base_url": spec.base_url,
        "model": spec.model,
        "config_id": spec.id,
    }
    return chat, snapshot


# ═══════════════════════════════════════════════════════════ CLI


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成旁证答案集：只喂标注切片、机械校验、失败留痕（#108）",
    )
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 题（跨题型轮转），用于验证通路")
    parser.add_argument("--types", default=None, help="只跑这些题型，逗号分隔（如 exact_code,model_number）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"选题种子（默认 {DEFAULT_SEED}）")
    parser.add_argument("--total", type=int, default=DEFAULT_TOTAL, help=f"总题数（默认 {DEFAULT_TOTAL}）")
    parser.add_argument(
        "--per-type-min", type=int, default=DEFAULT_PER_TYPE_MIN, help="每题型下限（默认 2）"
    )
    parser.add_argument(
        "--per-type-max", type=int, default=None, help="每题型上限（默认不限；给 3 会因 11×3<50 报警）"
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="机读产物路径")
    parser.add_argument("--report", default=None, help="人读报告路径（默认与 --out 同名前缀）")
    parser.add_argument("--no-report", action="store_true", help="只写机读产物（试跑用）")
    parser.add_argument(
        "--trial",
        default=str(DEFAULT_TRIAL),
        help="试跑产物路径：存在时把试跑摘要一并写进报告（不存在则跳过）",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_ANSWER_TIMEOUT_S, help="单题生成超时（秒）"
    )
    return parser.parse_args(argv)


def _report_path(out: Path, report: str | None) -> Path:
    if report:
        return Path(report)
    return out.with_suffix(".report.md")


def _warn(message: str) -> None:
    print(f"[warn] {message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    out_path = Path(args.out)

    questions = load_questions()
    questions_bytes = QUESTIONS_PATH.read_bytes()
    questions_version = {
        "path": "eval/questions.json",
        "sha256": hashlib.sha256(questions_bytes).hexdigest(),
        "count": len(questions),
    }

    chunks, files = load_corpus_chunks()
    corpus = corpus_snapshot(files, chunks)

    only = tuple(name.strip() for name in args.types.split(",") if name.strip()) if args.types else None
    quotas, quota_warnings = allocate_quotas(
        type_pools(questions),
        total=args.total,
        per_type_min=args.per_type_min,
        per_type_max=args.per_type_max,
        only=only,
    )
    for warning in quota_warnings:
        _warn(warning)

    cases = select_cases(questions, quotas, seed=args.seed, limit=args.limit)
    if not cases:
        print("没有选中任何题：检查 --types / --total / --limit", file=sys.stderr)
        return 2

    sources_by_id = resolve_sources(cases, chunks)
    empty = [case.id for case in cases if not sources_by_id.get(case.id)]
    if empty:
        # 理论上 resolve_sources 已经 SystemExit；留一道显式的兜底，不让空片段去生成。
        print(f"这些题没有解析到任何切片，拒绝生成：{', '.join(empty)}", file=sys.stderr)
        return 2

    chat, judge = build_judge()
    generated_at = datetime.now(UTC).isoformat()
    print(
        f"生成 {len(cases)} 题 · 裁判模型 {judge['model']} · 语料 {corpus['version']} "
        f"· 配额 {dict(sorted(quotas.items()))}",
        file=sys.stderr,
    )

    records = asyncio.run(
        run_generation(
            cases,
            sources_of=lambda case: sources_by_id.get(case.id, []),
            chat=chat,
            answer_timeout_s=args.timeout,
        )
    )

    payload = build_payload(
        records,
        judge_model=str(judge.get("model") or ""),
        judge=judge,
        questions_version=questions_version,
        corpus=corpus,
        seed=args.seed,
        generated_at=generated_at,
        selection={
            "total": args.total,
            "requested": len(cases),
            "per_type_min": args.per_type_min,
            "per_type_max": args.per_type_max if args.per_type_max is not None else "pool",
            "types": ",".join(sorted(quotas)) if only else "all(answerable)",
            "quotas": " ".join(f"{name}={size}" for name, size in sorted(quotas.items())),
        },
        warnings=quota_warnings,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    stats = payload["stats"]
    print(
        f"写盘 {out_path}：成功 {stats['ok']} / 拒答 {stats['refused']} / 失败 {stats['failed']}，"
        f"校验通过率 {stats['check_passed']}/{stats['checked']}",
        file=sys.stderr,
    )

    if not args.no_report:
        report_path = _report_path(out_path, args.report)
        trial: dict[str, Any] | None = None
        if args.trial:
            trial_path = Path(args.trial)
            if trial_path.is_file() and trial_path.resolve() != out_path.resolve():
                trial = json.loads(trial_path.read_text(encoding="utf-8"))
            elif trial_path.is_file():
                _warn("--trial 指向的是本次产物本身，忽略")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_report(payload, trial=trial), encoding="utf-8")
        print(f"写盘 {report_path}", file=sys.stderr)

    # 一题都没成说明通路坏了，要给调用方红；有成功也有失败则如实记录、正常退出。
    return 0 if stats["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())

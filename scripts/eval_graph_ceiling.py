"""可定位上限探针（#158）：漏掉的 `explicit` 里，有多少靠**确定性匹配**就能定位？

**它回答的问题**：标注集测出 recall 只有 0.081，是该上模型，还是该补规则？

对每一条 `explicit` 标注，**不经过抽取器**，直接问：给定 evidence 原文与目标节标题，有没有
一种确定性匹配能唯一定位到那个节？

* 能 → 缺的是「定位符的语法」没覆盖，补规则即可；
* 不能 → 才可能是「语义」问题，那时才谈得上引入模型。

**为什么必须补这一步**：票面只有一条阈值（recall ≥ 0.7 且 precision ≥ 0.95 才不上 LLM），
而阈值只回答「够不够好」，不回答「为什么不好」。把 0.081 直接读成「要上 LLM」会跳过
一个更便宜的问题：那 34 条漏项里，有多少其实是同一两条规则就能覆盖的？

**这个脚本的边界**：它算的是**上限**——只判「能不能唯一定位到」，不判「会不会顺手多建
别的边」。上限够高，才值得去实现并重测真实 precision。

用法：``uv run python scripts/eval_graph_ceiling.py``（纯离线，不连库、不调模型）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
LABELS: Final[Path] = ROOT / "eval" / "graph-labels" / "labels.json"

#: 全角数字 → 半角。真实政策文本里「第４节」这种写法很常见。
_FULLWIDTH: Final = str.maketrans("０１２３４５６７８９", "0123456789")

#: 中文序数 → 阿拉伯。只到「百」，够用；更复杂的（``一百零三``）留给需要时再补。
_CN_DIGITS: Final[dict[str, int]] = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

#: 引号括住的短语：``「后果」`` / ``『自检』``。这是「作者明确指了一个名字」的最强信号。
_QUOTED: Final = re.compile(r"[「『]([^」』\n]{2,40})[」』]")

#: 编号定位符：``决策五`` / ``第 2 条`` / ``Section 3``。冒号后的编号也认。
_NUMBERED: Final = re.compile(
    r"(?:决策|第)\s*([一二三四五六七八九十百]+|\d+)\s*(?:条|款|节|项)?|Section\s+(\d+)"
)

#: 章-节双层：``第三章第二节``。
_CHAPTER_SECTION: Final = re.compile(
    r"第([一二三四五六七八九十百]+|\d+)\s*章\s*第([一二三四五六七八九十百]+|\d+)\s*节"
)

#: 标题开头的编号：``3. 只认…`` → ``3``；``决策五：…`` → ``5``；``第 2 条 …`` → ``2``；
#: ``Section 2 Availability`` → ``2``（英文附件的编号惯例）。
_TITLE_ARABIC: Final = re.compile(r"^(\d+)(?![0-9])", re.UNICODE)
_TITLE_DECISION: Final = re.compile(r"^决策\s*([一二三四五六七八九十百]+|\d+)")
_TITLE_ORDINAL: Final = re.compile(r"^第\s*([一二三四五六七八九十百]+|\d+)")
_TITLE_GROUPED: Final = re.compile(r"^第\s*([一二三四五六七八九十百]+|\d+)\s*[章节条款项]")
_TITLE_ENGLISH: Final = re.compile(r"^section\s*(\d+)\b", re.IGNORECASE)


def normalize(text: str) -> str:
    """归一：全角→半角、去掉标点与空白、小写。比较两边都走它，才不会被排版差异骗。"""

    text = text.translate(_FULLWIDTH)
    text = re.sub(r"[^\w\u4e00-\u9fff.]", "", text)
    return text.lower()


def to_arabic(token: str) -> str | None:
    """``3`` / ``三`` / ``十二`` → 阿拉伯串；认不出回 ``None``。"""

    token = token.translate(_FULLWIDTH)
    if token.isdigit():
        return token
    if not token:
        return None
    if token in _CN_DIGITS:
        return str(_CN_DIGITS[token])
    if token == "十":
        return "10"
    if "十" in token:
        left, _, right = token.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return str(tens * 10 + ones)
    hundred = re.fullmatch(r"([一二三四五六七八九])百(?:零?([一二三四五六七八九])?)?", token)
    if hundred:
        head = _CN_DIGITS[hundred.group(1)] * 100
        tail = _CN_DIGITS.get(hundred.group(2), 0) if hundred.group(2) else 0
        return str(head + tail)
    return None


def heading_number(title: str) -> str | None:
    """节标题自带的编号。五种惯例都认：``3.`` / ``决策五：`` / ``第 2 条`` / ``第 2 章`` / ``Section 2``。"""

    plain = normalize(title)
    for pattern in (_TITLE_ARABIC,):
        match = pattern.match(plain)
        if match:
            return match.group(1)
    for pattern in (_TITLE_ENGLISH, _TITLE_DECISION, _TITLE_GROUPED, _TITLE_ORDINAL):
        match = pattern.match(title)
        if match:
            return to_arabic(match.group(1))
    return None


def locatable(evidence: str, target_title: str) -> str | None:
    """这条 evidence 能不能靠确定性规则定位到 ``target_title``？返回规则名或 ``None``。"""

    title_plain = normalize(target_title)
    target_number = heading_number(target_title)

    # R1-引号：引号里的短语是目标标题的一部分（「后果」「后续修订（第 7 步）」「备选方案」）
    for match in _QUOTED.finditer(evidence):
        phrase = normalize(match.group(1))
        if phrase and phrase in title_plain:
            return "R1 引号标题名"
        for token in re.findall(r"[一二三四五六七八九十百]+|\d+", phrase):
            number = to_arabic(token)
            if number is not None and target_number == number:
                return "R1 引号内编号"

    # R1-裸名：整句里出现目标标题名（≥4 字才算，`背景` / `自检` 这种短名撞上的概率是必然）
    bare = normalize(evidence)
    if len(title_plain) >= 4 and title_plain in bare:
        return "R1 裸标题名"

    # R2 编号：证据里的编号与标题编号一致（`决策 3` / `第 2 条` / `Section 4`）
    for match in _NUMBERED.finditer(evidence):
        token = match.group(1) or match.group(2)
        number = to_arabic(token) if token else None
        if number is not None and target_number == number:
            return "R2 编号定位"

    # R2 章-节：`第三章第二节` → 目标标题编号 3（章）下的第 2 节，本仓库语料里直接对 3
    for chapter, section in _CHAPTER_SECTION.findall(evidence):
        if target_number == to_arabic(chapter) and section:
            return "R2 章-节双层"

    return None


def main() -> int:
    payload = json.loads(LABELS.read_text(encoding="utf-8"))
    rows = [item for item in payload["labels"] if item["label"] == "explicit"]
    tiers = sorted(
        {item["tier"] for item in rows}, key=lambda t: ("engineering", "policy", "minutes").index(t)
    )

    print(f"标注集：{len(payload['labels'])} 条，其中 explicit {len(rows)} 条\n")
    header = f"{'档':<14}{'explicit':>9}{'可确定定位':>11}{'需语义':>7}{'上限 recall':>12}"
    print(header)
    print("-" * len(header))
    overall_hit = 0
    for tier in tiers:
        items = [item for item in rows if item["tier"] == tier]
        hits = [item for item in items if locatable(item["evidence"], item["target"]["section"])]
        overall_hit += len(hits)
        ceiling = f"{len(hits) / len(items):.3f}" if items else "—"
        print(f"{tier:<14}{len(items):>9}{len(hits):>11}{len(items) - len(hits):>7}{ceiling:>12}")

    total = len(rows)
    print("-" * len(header))
    print(
        f"{'总体':<14}{total:>9}{overall_hit:>11}{total - overall_hit:>7}{overall_hit / total:>12.3f}\n"
    )

    # 分解：哪条规则救回多少（这决定「先补哪条」）
    by_rule: dict[str, int] = {}
    for item in rows:
        rule = locatable(item["evidence"], item["target"]["section"])
        if rule:
            by_rule[rule] = by_rule.get(rule, 0) + 1
    print("按规则分解：")
    for rule, count in sorted(by_rule.items(), key=lambda kv: -kv[1]):
        print(f"  {rule}: {count}")

    misses = [item for item in rows if not locatable(item["evidence"], item["target"]["section"])]
    print(f"\n仍需语义（{len(misses)} 条）：")
    for item in misses:
        print(f"  {item['id']}  {item['evidence'][:50]!r} -> {item['target']['section'][:30]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

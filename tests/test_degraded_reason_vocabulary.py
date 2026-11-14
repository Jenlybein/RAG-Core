"""降级原因的词表守卫：源码里能长出来的固定串，只有文档写死的那四个。

**为什么要有这条**：`graph injection failed` 当初是第六个原因串，它落地时 `GLOSSARY.md` 与
ADR-0010 还都写着「只有五个」——两处说法不一致，而没有任何一条用例会发现这种不一致。
词表是**响应契约**的一部分（调用方按它写分支），所以它得像状态机那样有个结构守卫。

这个文件里的两次变动都来自**范围变更**（[ADR-0029](../docs/adr/0029-scope-down-to-controlled-research.md)），
不是新能力：

- `graph injection failed` 随轻量图谱撤销而消失：图谱不是一个"步骤"，撤销之后没有任何代码能发出它；
- `orchestration failed` 随独立编排块撤销而消失：一个不再存在的步骤不会失败，留着它的原因串只会让
  调用方写一条永远走不到的分支。

两次之后是**四个**。文档与源码逐字比对，多一个少一个都红。

做法：扫 `app/` 下所有字符串字面量，取形如 `"<小写单词> failed"` 的那些，与文档里的
四个逐字比对。多一个（有人偷偷加）或少一个（文档写了但代码没有）都红。
"""

from __future__ import annotations

import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
GLOSSARY = Path(__file__).resolve().parents[1] / "GLOSSARY.md"

#: 响应契约里允许出现的全部降级原因（ADR-0010 第十一次修订之后是四个）。
DOCUMENTED: frozenset[str] = frozenset(
    {
        "vector search failed",
        "keyword search failed",
        "rerank failed",
        "query rewrite failed",
    }
)

#: 只认「整串就是一个小写英文短语 + failed」的字面量：日志里的长句子不会误入。
_REASON_LITERAL = re.compile(r'"([a-z][a-z ]* failed)"')

#: 一行里出现这些名字，才算「这一行在定义/传递一个降级原因」。只按字面量形状扫全仓会把
#: 日志里的 `strong model failed`、`section context failed` 这类内部标记一起卷进来——
#: 它们不往外发，不属于响应契约。
_REASON_CONTEXT = re.compile(r"\b(degraded_reason|DEGRADED_REASON|REASON_[A-Z_]+|rewrite_reason)\b")


def _source_reasons() -> frozenset[str]:
    found: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if _REASON_CONTEXT.search(line):
                found.update(_REASON_LITERAL.findall(line))
    return frozenset(found)


def test_the_source_only_knows_the_documented_reasons() -> None:
    assert _source_reasons() == DOCUMENTED


def test_the_glossary_lists_every_reason_the_source_can_emit() -> None:
    text = GLOSSARY.read_text(encoding="utf-8")
    missing = sorted(reason for reason in DOCUMENTED if f"`{reason}`" not in text)

    assert missing == [], "GLOSSARY 的「降级原因」条目漏了源码里能发出的串"


def test_the_glossary_says_four_and_the_count_matches() -> None:
    text = GLOSSARY.read_text(encoding="utf-8")
    entry = text.split("**降级原因（degraded_reason）**", 1)[1].split("_Avoid_", 1)[0]

    assert "只有四个" in entry, "条目的数量词必须与下面的清单一致"
    assert entry.count("failed`") == len(DOCUMENTED)

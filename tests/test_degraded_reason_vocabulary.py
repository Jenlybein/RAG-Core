"""降级原因的词表守卫：源码里能长出来的固定串，只有文档写死的那四个。

**为什么要有这条**：词表是**响应契约**的一部分（调用方按它写分支），文档与源码
不一致时不会有任何功能用例变红，所以它得有个结构守卫。

第 13 步的两次变动都是**范围变更**，不是新能力：

- ``query rewrite failed`` 随「改写搬出 /rag/retrieve」退役：失败现在是整单 50003，
  不再有降级串（词条保留在 GLOSSARY 里解释历史归属索引行）；
- ``rerank quota exhausted`` 新增：重排是可选优化，额度用尽时跳过重排并如实标原因。

两次之后仍是**四个**。文档与源码逐字比对，多一个少一个都红。

做法：扫 `app/` 下所有 ``REASON_*`` 常量定义行，取赋值的字符串字面量——它比
「按 failed 结尾扫全仓」稳，因为新串不以 failed 结尾；同时不会把日志里的长句卷进来。
"""

from __future__ import annotations

import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
GLOSSARY = Path(__file__).resolve().parents[1] / "GLOSSARY.md"

#: 响应契约里允许出现的全部降级原因（第 13 步之后是四个）。
DOCUMENTED: frozenset[str] = frozenset(
    {
        "vector search failed",
        "keyword search failed",
        "rerank failed",
        "rerank quota exhausted",
    }
)

#: 只认「一行在定义一个 REASON_* 常量并赋值一个字符串字面量」。
_REASON_DEF = re.compile(r'^\s*REASON_[A-Z_]+\s*(?::[^=]+)?=\s*"([a-z][a-z ]*)"\s*$')

#: 降级原因只由检索链路这两个模块产出。观测侧也有一批 ``REASON_*`` 常量
#: （``not_configured`` / ``unreachable`` …），它们不往外发，不属于响应契约。
_REASON_FILES = (
    APP_ROOT / "services" / "retrieval" / "pipeline.py",
    APP_ROOT / "services" / "retrieval" / "rerank.py",
)


def _source_reasons() -> frozenset[str]:
    found: set[str] = set()
    for path in _REASON_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _REASON_DEF.match(line)
            if match:
                found.add(match.group(1))
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

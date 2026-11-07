"""同义词扩展：**纯函数**，输入（``effective_query`` + 词表）→ 输出 ``search_query``。

它在 GLOSSARY「查询优化」里占一半：召回之前对 query 做规范化的步骤之一。另一半是
改写（``app/services/query/rewrite.py``），两者只影响各自该影响的那一路——同义词补的是
**字面**匹配，对语义召回没有意义，所以它只喂关键词一路。

三条不许动的约定：

1. **纯**：不碰数据库、不碰网络、不读配置、不看时钟。词表由调用方从
   ``knowledge_bases.settings["synonyms"]`` 读出来传进来（检索链路里 ``ensure_visible``
   已经把那行拿在手上，不需要为它再查一次库）。同一个输入永远同一个输出，因此可以
   做成真值表。
2. **库级配置，不跨库不跨租户**：多库检索时只合并**本次涉及的那几个库**的词表，
   合并由 :func:`collect_synonym_groups` 做，它只接受调用方显式传进来的 settings。
3. **匹配口径照抄原文**：遍历每组，命中任一 term 即命中该组；中文 term 精确子串，
   英文 term 忽略大小写；命中组的 ``expand`` 追加到 query 末尾、空格分隔；多组命中时
   去重后追加；**未命中就是原句**（不是空串，也不是加个尾巴的句子）。

词表是租户填的 JSON，形状不可信：形状不对的组一律跳过。一次检索不该因为某库的
同义词表写歪了而整单 500。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# ``knowledge_bases.settings`` 里放同义词表的那个键。写在一个常量里而不是各处拼字符串：
# 它是**对外可见的配置契约**（租户在控制台里填的就是这个键）。
SYNONYMS_SETTING_KEY = "synonyms"


@dataclass(frozen=True)
class SynonymGroup:
    """一组同义词：``terms`` 是命中条件，``expand`` 是命中后要追加的词。

    规范化发生在 :func:`parse_synonym_groups`：空串、非字符串、重复项都在那里去掉，
    所以进到这里的两列永远是「干净且保序」的。``frozen`` 让它能安全地放进集合 / 去重。
    """

    terms: tuple[str, ...]
    expand: tuple[str, ...]


def _clean_terms(value: Any) -> tuple[str, ...]:
    """把 JSON 里的一列字符串收敛成「去空、去重、保序」的元组。

    去重是**保序去重**而不是转集合：expand 追加进 query 的顺序会影响可复现性，
    同样的请求两次必须给出同样的 ``search_query``。
    """

    if not isinstance(value, (list, tuple)):
        return ()

    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return tuple(cleaned)


def parse_synonym_groups(value: Any) -> tuple[SynonymGroup, ...]:
    """把 ``settings["synonyms"]`` 的原始 JSON 收敛成组；不合法的一律跳过。

    跳过而不是抛错：这是租户可写的配置，检索是读路径。一个写歪的组只该让「这一组
    同义词不生效」，不该让整个知识库查不了。
    """

    if not isinstance(value, (list, tuple)):
        return ()

    groups: list[SynonymGroup] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            continue
        terms = _clean_terms(entry.get("terms"))
        expand = _clean_terms(entry.get("expand"))
        # 没有 terms 的组永远命中不了，没有 expand 的组命中也没东西可加：两者都是废组。
        if terms and expand:
            groups.append(SynonymGroup(terms=terms, expand=expand))
    return tuple(groups)


def collect_synonym_groups(
    kb_settings: Iterable[Mapping[str, Any] | None],
) -> tuple[SynonymGroup, ...]:
    """把**本次涉及的库**的 settings 合并成一张词表。

    输入是调用方已经拿到的 ``KnowledgeBase.settings``（不是 ``kb_id``，更不是查询）：
    多库检索时这里只看得见本次真正参与的库，所以天然「不跨库、不跨租户」。完全相同的
    组只留一份——两个库配了同一组同义词时，不该把同一个词往 query 里说两遍。
    """

    groups: list[SynonymGroup] = []
    for settings_row in kb_settings:
        if not isinstance(settings_row, Mapping):
            continue
        for group in parse_synonym_groups(settings_row.get(SYNONYMS_SETTING_KEY)):
            if group not in groups:
                groups.append(group)
    return tuple(groups)


def _is_ascii_term(term: str) -> bool:
    """英文（纯 ASCII）term 才做大小写折叠；中文没有大小写可言。

    用「是不是纯 ASCII」而不是「有没有中文字符」来判：前者对 ``E1024`` 这类代号
    同样成立（它们本来就是 ASCII，折叠大小写不会误伤）。
    """

    return term.isascii()


def _hits(query: str, term: str) -> bool:
    """``query`` 是否包含这个词条。

    中文 term 精确子串匹配；英文 term 忽略大小写（两侧同时折叠，所以词表写
    ``Refund``、用户写 ``refund`` 都算命中）。空 term 不参与匹配——「包含空串」
    恒为真，那会让所有组都命中。
    """

    if not term:
        return False
    if _is_ascii_term(term):
        return term.lower() in query.lower()
    return term in query


def expand_synonyms(query: str, groups: Iterable[SynonymGroup]) -> str:
    """命中组把 ``expand`` 追加到 ``query`` 末尾；未命中返回原句。

    返回的 ``search_query`` 只喂**关键词一路**。它与 ``effective_query`` 不同是设计
    而不是 bug：同义词补的是字面匹配，改写补的是语义，两路各拿各的才不会互相稀释。
    """

    appended: list[str] = []
    for group in groups:
        if not any(_hits(query, term) for term in group.terms):
            continue
        for word in group.expand:
            if word not in appended:
                appended.append(word)

    if not appended:
        return query
    if not query:
        # 空 query 命中时装出来的句子不该带前导空格。
        return " ".join(appended)
    return f"{query} {' '.join(appended)}"

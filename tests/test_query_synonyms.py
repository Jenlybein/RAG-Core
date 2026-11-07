"""同义词扩展：纯函数真值表（不起应用、不碰数据库、不碰网络）。

它是父 spec「Testing Decisions」点名要**直接单测**的那一类：输入（``effective_query``
+ 词表）→ 输出 ``search_query``，手算能对得上，不必经过任何接缝。

本文件守的匹配口径（父 spec「查询优化」与原教程的原文口径）：

* 遍历每组 synonyms，``effective_query`` 包含任一 term 即命中该组；
* 中文 term 精确子串匹配，英文 term 忽略大小写；
* 命中组的 expand 词追加到 query 末尾、空格分隔；
* 多组同时命中 → expand 去重后追加（同一个词只说一遍）；
* 未命中任何 term → ``search_query`` **等于** ``effective_query``（不是空串，也不是加了尾巴）；
* 空词表 / 缺词表 / 词表形状不合法 → 同样等于原句。

词表是**库级配置**（``knowledge_bases.settings["synonyms"]``）：不跨库、不跨租户。
多库检索时把本次涉及的库的词表合并，所以这里也钉 ``collect_synonym_groups``。
"""

from __future__ import annotations

from app.services.query.synonyms import (
    SynonymGroup,
    collect_synonym_groups,
    expand_synonyms,
)


def _group(terms: list[str], expand: list[str]) -> dict[str, list[str]]:
    """库里存的那种形状：一组同义词 = ``terms``（命中条件）+ ``expand``（追加词）。"""

    return {"terms": terms, "expand": expand}


# ────────────────────────────────────────────────────────── 真值表（6 条）


def test_multiple_groups_hit_and_expands_are_deduplicated() -> None:
    """两条组同时命中 + 同一个 expand 词出现在两组里 → 只追加一次，且保持先后顺序。"""

    table = [
        _group(["退款", "退钱"], ["退款流程", "售后"]),
        _group(["发票"], ["售后", "开票"]),
    ]

    result = expand_synonyms("退钱怎么开发票", collect_synonym_groups([{"synonyms": table}]))

    assert result == "退钱怎么开发票 退款流程 售后 开票"


def test_single_group_hit_appends_its_expands_after_the_query() -> None:
    """只命中一组：该组的 expand 原序追加在 query 末尾，空格分隔。"""

    table = [_group(["背调"], ["背景调查", "背景审查"])]

    assert (
        expand_synonyms("背调要问啥", collect_synonym_groups([{"synonyms": table}]))
        == "背调要问啥 背景调查 背景审查"
    )


def test_english_terms_match_case_insensitively() -> None:
    """英文 term 忽略大小写：词表写 ``Refund`` 也要能命中 query 里的 ``refund``。"""

    table = [_group(["Refund", "REFUND"], ["退款流程"])]

    groups = collect_synonym_groups([{"synonyms": table}])
    assert expand_synonyms("how to refund an order", groups) == "how to refund an order 退款流程"
    assert expand_synonyms("HOW TO REFUND", groups) == "HOW TO REFUND 退款流程"


def test_no_hit_returns_the_query_unchanged() -> None:
    """一个 term 都没命中：``search_query`` 就是原句，一个字都不许多。"""

    table = [_group(["退款"], ["退款流程"])]

    assert expand_synonyms("发票怎么开", collect_synonym_groups([{"synonyms": table}])) == (
        "发票怎么开"
    )


def test_empty_synonym_table_leaves_the_query_untouched() -> None:
    """空词表（库没配同义词）：原句返回。"""

    assert expand_synonyms("退款怎么走", collect_synonym_groups([{"synonyms": []}])) == "退款怎么走"
    assert expand_synonyms("退款怎么走", collect_synonym_groups([{}])) == "退款怎么走"
    assert expand_synonyms("退款怎么走", collect_synonym_groups([None])) == "退款怎么走"


# ────────────────────────────────────────────────────────── 边界与合并


def test_english_query_does_not_hit_a_chinese_term_by_case_folding() -> None:
    """中文 term 走精确子串：不因为「不区分大小写」就命中不相干的句子。"""

    groups = (SynonymGroup(terms=("退款",), expand=("退款流程",)),)

    assert expand_synonyms("refund policy", groups) == "refund policy"


def test_malformed_entries_are_ignored_instead_of_exploding() -> None:
    """词表是租户填的 JSON：形状不对的组跳过，绝不让一次检索整单 500。"""

    groups = collect_synonym_groups(
        [
            {
                "synonyms": [
                    "不是字典",
                    {"terms": "不是列表", "expand": ["x"]},
                    {"terms": ["退款"], "expand": "不是列表"},
                    {"terms": ["退款"], "expand": ["", 3, "退款流程"]},
                ]
            }
        ]
    )

    assert groups == (SynonymGroup(terms=("退款",), expand=("退款流程",)),)
    assert expand_synonyms("退款怎么走", groups) == "退款怎么走 退款流程"


def test_groups_from_multiple_knowledge_bases_are_merged_and_deduplicated() -> None:
    """多库检索：本次涉及的库的词表合并；完全相同的组只留一份。"""

    same = _group(["退款"], ["退款流程"])

    groups = collect_synonym_groups(
        [
            {"synonyms": [same]},
            {"synonyms": [same, _group(["发票"], ["开票"])]},
        ]
    )

    assert groups == (
        SynonymGroup(terms=("退款",), expand=("退款流程",)),
        SynonymGroup(terms=("发票",), expand=("开票",)),
    )
    # 合并之后仍然只追加一次：两库配了同一组，不该把词说两遍。
    assert expand_synonyms("退款发票", groups) == "退款发票 退款流程 开票"


def test_empty_query_only_returns_the_expands() -> None:
    """空 query 不该拼出一个前导空格。"""

    groups = (SynonymGroup(terms=("",), expand=("退款流程",)),)

    # 空 term 不参与匹配（否则「包含空串」永远成立）。
    assert expand_synonyms("", groups) == ""

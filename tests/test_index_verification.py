"""#143 · 核对：四项计数 + 节的身份证自洽（含「悬空父 id」两档口径）。

两类断言，都打在纯函数上（不连库、不连 ES）：

* :func:`app.services.index_verification.judge_counts` —— 相等**且非 0** 才算过；
  没有关键词引擎时那一维不参与判定（能力缺失不是数据不一致）。
* :func:`app.services.index_verification.verify_identity` —— 每行的 ``section_id`` 与
  父 id 都必须能由 ``(文档 id, 标题路径)`` 重算出来；父节只有标题（不产切片）是**正常**
  的一档，单独计量而不算错。

原先还有一段验 ``scripts/verify_index_counts.py`` 的退出码（不等 / 为 0 / 身份对不上 →
非零；全对 → 0）；那个脚本已随库级四项计数对账一起删除（ADR-0025 第二次修订），
那几条用例也一并删掉了。这里只留**服务层**的判定与身份核对。
"""

from __future__ import annotations

from typing import Any

from app.services.index_build_service import BuildCounts
from app.services.index_verification import (
    IdentityRow,
    describe_counts,
    judge_counts,
    verify_identity,
)
from app.utils.section_identity import parent_section_id_of, section_id_of

DOC_A = "doc-verify-aaaaaaaa-0000-4000-8000-000000000001"
DOC_B = "doc-verify-bbbbbbbb-0000-4000-8000-000000000002"


def _row(document_id: str, heading_path: str, **overrides: Any) -> IdentityRow:
    """一行「节的身份」读数；缺省值按算法算出来（要造错时用 ``overrides`` 覆盖）。"""

    return IdentityRow(
        document_id=document_id,
        heading_path=heading_path,
        section_id=overrides.get(
            "section_id", section_id_of(document_id=document_id, heading_path=heading_path)
        ),
        parent_section_id=overrides.get(
            "parent_section_id",
            parent_section_id_of(document_id=document_id, heading_path=heading_path),
        ),
    )


# ──────────────────────────────────────────────────────────────── 四项计数


def _counts(**overrides: Any) -> BuildCounts:
    base = {
        "knowledge_bases": 1,
        "chunks": 12,
        "vectors": 12,
        "keyword_documents": 12,
    }
    base.update(overrides)
    return BuildCounts(**base)


def test_four_counts_that_are_equal_and_non_zero_pass() -> None:
    verdict = judge_counts(_counts())

    assert verdict.ok, verdict.problems


def test_a_keyword_capability_that_does_not_exist_is_not_a_failure() -> None:
    """没部署 ES 时这一维是 ``None``（能力缺失），不是 0（数据不一致）。"""

    verdict = judge_counts(_counts(keyword_documents=None))

    assert verdict.ok, verdict.problems


def test_a_version_without_any_chunk_fails_even_when_the_numbers_agree() -> None:
    """全 0 时三项「彼此相等」——形式上自洽，但那个版本什么都查不到。"""

    verdict = judge_counts(_counts(chunks=0, vectors=0, keyword_documents=0))

    assert not verdict.ok
    assert any("切片计数为 0" in problem for problem in verdict.problems)
    assert any("向量计数为 0" in problem for problem in verdict.problems)
    assert any("关键词文档计数为 0" in problem for problem in verdict.problems)


def test_unequal_counts_fail() -> None:
    verdict = judge_counts(_counts(vectors=11, keyword_documents=13))

    assert not verdict.ok
    assert any("不相等" in problem for problem in verdict.problems)
    assert len(verdict.problems) == 2


def test_counts_are_printed_in_a_fixed_order_with_na_for_absent_keyword() -> None:
    labelled = describe_counts(_counts(keyword_documents=None))

    assert [label for label, _value in labelled] == [
        "knowledge_bases",
        "chunks",
        "vectors",
        "keyword_documents",
    ]
    assert dict(labelled)["keyword_documents"] == "n/a"


# ──────────────────────────────────────────────────────────── 节的身份与父 id


def test_identity_of_a_healthy_version_passes() -> None:
    rows = [
        _row(DOC_A, "售后手册"),
        _row(DOC_A, "售后手册 > 退款"),
        _row(DOC_A, "售后手册 > 退款 > 流程"),
        _row(DOC_B, "售后手册 > 退款"),
    ]

    report = verify_identity(rows)

    assert report.ok, report.render_problems()
    assert report.sections == 4, "同一个标题路径在两份文档下是两节"
    assert report.parented == 3
    assert report.parent_with_body == 2, "A 的两节父节都在本版本里有切片"
    assert report.parent_without_body == 1, "B 的「售后手册」只有标题，不产切片"


def test_a_parent_section_without_its_own_chunk_is_counted_not_blamed() -> None:
    """父节只有标题、正文全在子节里 → 父 id 在本版本里没有切片，这是**正常**的。

    这条用例是「悬空父 id」这一条的字面读法与切块器行为的冲突点：不能把它判成错，
    否则每个「h2 只有标题」的文档都会报失败。它单独计量，只打印不判错。
    """

    rows = [
        _row(DOC_A, "售后手册 > 退款"),
        _row(DOC_A, "售后手册 > 退款 > 流程"),
    ]

    report = verify_identity(rows)

    assert report.ok, report.render_problems()
    assert report.parent_with_body == 1, "「售后手册 > 退款」自己有切片"
    assert report.parent_without_body == 1, "「售后手册」只有标题，没有自己的切片"


def test_an_old_style_section_id_is_reported() -> None:
    """改造前写下的行（只哈希标题路径）在这个版本里必须被指出来。"""

    import hashlib

    stale = hashlib.sha256("售后手册 > 退款".encode("utf-8")).hexdigest()
    rows = [_row(DOC_A, "售后手册 > 退款", section_id=stale)]

    report = verify_identity(rows)

    assert not report.ok
    assert [problem.kind for problem in report.problems] == ["section_id_mismatch"]
    assert DOC_A in report.problems[0].examples[0]


def test_an_old_style_parent_id_is_reported() -> None:
    import hashlib

    stale_parent = hashlib.sha256("售后手册".encode("utf-8")).hexdigest()
    rows = [
        _row(DOC_A, "售后手册"),
        _row(DOC_A, "售后手册 > 退款", parent_section_id=stale_parent),
    ]

    report = verify_identity(rows)

    assert not report.ok
    assert [problem.kind for problem in report.problems] == ["parent_id_mismatch"]


def test_a_missing_parent_id_is_reported() -> None:
    rows = [_row(DOC_A, "售后手册 > 退款", parent_section_id=None)]

    report = verify_identity(rows)

    assert not report.ok
    assert [problem.kind for problem in report.problems] == ["parent_id_missing"]


def test_one_section_id_under_two_documents_is_reported() -> None:
    """不带文档 id 的旧 id 会跨文档撞：这类行必须能被抓出来。"""

    shared = section_id_of(document_id="", heading_path="售后手册 > 退款")
    rows = [
        IdentityRow(DOC_A, "售后手册 > 退款", shared, None),
        IdentityRow(DOC_B, "售后手册 > 退款", shared, None),
    ]

    report = verify_identity(rows)

    assert not report.ok
    kinds = {problem.kind for problem in report.problems}
    assert "section_shared_across_documents" in kinds
    assert "section_id_mismatch" in kinds, "它们同时也与算法对不上（两份都会报）"

"""#143 · 核对：四项计数 + 节的身份证自洽（含「悬空父 id」两档口径）。

两类断言，都打在纯函数上（不连库、不连 ES）：

* :func:`app.services.index_verification.judge_counts` —— 相等**且非 0** 才算过；
  没有关键词引擎时那一维不参与判定（能力缺失不是数据不一致）。
* :func:`app.services.index_verification.verify_identity` —— 每行的 ``section_id`` 与
  父 id 都必须能由 ``(文档 id, 标题路径)`` 重算出来；父节只有标题（不产切片）是**正常**
  的一档，单独计量而不算错。

最后一段验脚本的退出码：不等 / 为 0 / 身份对不上 → 非零；全对 → 0。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

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

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


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


# ────────────────────────────────────────────────────────────── 脚本的退出码


def _load_script(module_name: str) -> ModuleType:
    path = SCRIPTS_DIR / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"t143_verify_{module_name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _patch_verifier(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    counts: BuildCounts,
    rows: list[IdentityRow],
) -> None:
    """把「读四项计数」与「读身份行」两个外部协作者换成替身。

    仓储层的那条读返回的是**元组**（脚本自己拼成 ``IdentityRow``），所以替身也照那个
    形状回，脚本里那段映射才真的被执行一遍。
    """

    class _RecordingService:
        async def collect_counts(self, _db: Any, *, kb: Any, version: str) -> BuildCounts:
            return counts

    async def _identity_rows(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        return [
            (row.document_id, row.heading_path, row.section_id, row.parent_section_id)
            for row in rows
        ]

    monkeypatch.setattr(module, "IndexBuildService", _RecordingService)
    monkeypatch.setattr(module.chunk_repository, "list_section_identity_rows", _identity_rows)


def _run_script(module: ModuleType, monkeypatch: pytest.MonkeyPatch, *, kb_id: str = "kb-1") -> int:
    """跑一次 ``verify_knowledge_base``（直接用替身会话与之下的读替身）。"""

    session = SimpleNamespace(close=_noop_close)
    kb = SimpleNamespace(id=kb_id, tenant_id="tenant-1", active_index_version="v2")
    return asyncio.run(
        module.verify_knowledge_base(session, kb=kb, tenant_id="tenant-1", index_version=None)
    )


async def _noop_close() -> None:
    return None


def test_the_verification_script_exits_non_zero_when_counts_disagree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("verify_index_counts")
    _patch_verifier(
        module,
        monkeypatch,
        counts=_counts(vectors=11),
        rows=[_row(DOC_A, "售后手册")],
    )

    assert _run_script(module, monkeypatch) == 1


def test_the_verification_script_exits_non_zero_on_a_dangling_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("verify_index_counts")
    _patch_verifier(
        module,
        monkeypatch,
        counts=_counts(),
        rows=[_row(DOC_A, "售后手册 > 退款", parent_section_id="0" * 64)],
    )

    assert _run_script(module, monkeypatch) == 1


def test_the_verification_script_exits_zero_when_everything_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("verify_index_counts")
    _patch_verifier(
        module,
        monkeypatch,
        counts=_counts(),
        rows=[
            _row(DOC_A, "售后手册"),
            _row(DOC_A, "售后手册 > 退款"),
        ],
    )

    assert _run_script(module, monkeypatch) == 0


def test_the_verification_script_defaults_to_the_live_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不给 ``--index-version`` 时核对的是这个库的**生效版本**（不是写死的 v1）。"""

    module = _load_script("verify_index_counts")
    _patch_verifier(module, monkeypatch, counts=_counts(), rows=[_row(DOC_A, "售后手册")])

    reported: list[str] = []

    monkeypatch.setattr(
        module, "print", lambda *args, **kwargs: reported.append(str(args[0])), raising=False
    )

    assert _run_script(module, monkeypatch) == 0
    assert any("v2" in line for line in reported), reported

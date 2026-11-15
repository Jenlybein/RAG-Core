"""索引版本核对：四项计数 + 节的身份证自洽（#143）。

两件事，同一个判断口径：

1. **四项计数**（知识库 / 切片 / 向量 / 关键词文档）——`ready` 前的闸门是 #142 的
   :class:`~app.services.index_build_service.BuildCounts`；核对脚本在它之外**多挡一条**：
   任一数据计数为 0 也算不过。理由是两者的问题不同：``BuildCounts.matched`` 回答「这一次
   构建能不能标 ready」（全 0 时三项彼此相等，形式上成立），核对脚本回答「这个版本现在
   还能不能查到东西」——一个 0 片的版本对它来说就是空的，哪怕它「自洽」。
2. **节的身份证自洽**——每行的 ``section_id`` 必须能由 ``(文档 id, 标题路径)`` 重算出来，
   父 id 同法。这挡住的是最难查的一类故障：**混了两种算法的版本**（改造前写下的行没被
   清掉、或一次重建只跑了一半）。那种版本的每一项计数都可能「相等且非 0」，检索也会
   正常返回结果，只是节的身份已经对不上父子关系。

## 悬空父 id 分两档，只有一档算错

| 档 | 含义 | 计法 |
| --- | --- | --- |
| ``parent_with_body`` | 父节在本版本里有自己的切片 | 正常 |
| ``parent_without_body`` | 父节只有标题、正文全在子节里，因此**不产切片** | **正常**，单独统计 |
| 其余 | 父 id 与算法算出来的不一致 / 本应为空却非空 / 应非空却为空 | 算错 |

第二档不是缺陷，而是切块器的既有行为：``_is_heading_only`` 会丢掉「只有标题」的行，
于是一个作者写成「h2 只有标题、正文都在 h3 里」的文档，其 h2 节的 id 在本版本里没有任何
切片对应。要求「每个非空父 id 都能在本版本找到」的字面版本会让每个这样的文档都报错——
那是把切块器的设计当成数据错误。核对脚本因此**打印**这一档的条数（运维要知道有多少父节
是「只有标题」的），只对真正对不上的那一档返回非零。

## 这一层是纯函数

它只吃「读数」与「行」，不碰会话、不碰 ES：核对脚本与将来的控制面入口共用同一份判断，
而用例可以对着几十行字典把每条错误各试一遍。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.services.index_build_service import BuildCounts
from app.utils.section_identity import parent_section_id_of, section_id_of

#: 每个问题最多带几条具体例子（排障用；再多只会淹没结论）。
MAX_EXAMPLES: int = 3


@dataclass(frozen=True)
class IdentityRow:
    """一行「节的身份」读数：``chunks`` 里 distinct 出来的一个节。"""

    document_id: str
    heading_path: str
    section_id: str
    parent_section_id: str | None


@dataclass(frozen=True)
class IdentityProblem:
    """一类身份问题：固定种类码 + 固定文案 + 条数 + 最多 ``MAX_EXAMPLES`` 条例子。"""

    kind: str
    message: str
    count: int
    examples: tuple[str, ...] = ()

    def render(self) -> str:
        """一行可读的报错（脚本直接打印它）。"""

        detail = f"；例如：{' / '.join(self.examples)}" if self.examples else ""
        return f"{self.message}（{self.count} 处）{detail}"


@dataclass(frozen=True)
class IdentityReport:
    """节身份核对的结果。

    ``sections`` 是本版本的节数（distinct 后的行数），``parented`` 是其中带父 id 的节数。
    ``parent_without_body`` 见模块文档：它不是错误，只是「父节只有标题」的计量。
    """

    sections: int = 0
    parented: int = 0
    parent_with_body: int = 0
    parent_without_body: int = 0
    problems: tuple[IdentityProblem, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def render_problems(self) -> list[str]:
        return [problem.render() for problem in self.problems]


@dataclass(frozen=True)
class CountsVerdict:
    """四项计数的判定结果（口径见模块文档第 1 段）。"""

    counts: BuildCounts
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class VerificationReport:
    """一次核对的完整结论：四项计数 + 节身份。"""

    kb_id: str
    tenant_id: str
    index_version: str
    counts: CountsVerdict
    identity: IdentityReport

    @property
    def ok(self) -> bool:
        return self.counts.ok and self.identity.ok


def judge_counts(counts: BuildCounts) -> CountsVerdict:
    """判定四项计数：**相等且非 0** 才算过（与 ``ready`` 的闸门差一条「非 0」）。

    ``keyword_documents is None`` 表示关键词能力不存在（没部署 ES）：这一维不参与判定。
    把「没有这项能力」当成「这一维是 0」，会让没有 ES 的部署永远核对失败。
    """

    problems: list[str] = []
    if counts.knowledge_bases != 1:
        problems.append(
            f"知识库计数应为 1（本次核对的库），实际 {counts.knowledge_bases}"
        )
    if counts.chunks == 0:
        problems.append("切片计数为 0：这个版本没有任何切片")
    if counts.vectors == 0:
        problems.append("向量计数为 0：这个版本没有任何向量")
    if counts.chunks != counts.vectors:
        problems.append(f"切片数 {counts.chunks} 与向量数 {counts.vectors} 不相等")
    # 关键词这一维：None = 能力不存在（不判），有值就按「非 0 且与切片数相等」判。
    keyword = counts.keyword_documents
    if keyword is not None:
        if keyword == 0:
            problems.append("关键词文档计数为 0：这个版本在关键词引擎里没有文档")
        if keyword != counts.chunks:
            problems.append(f"切片数 {counts.chunks} 与关键词文档数 {keyword} 不相等")
    return CountsVerdict(counts=counts, problems=tuple(problems))


def verify_identity(rows: Iterable[IdentityRow]) -> IdentityReport:
    """逐行核对节的身份证：``section_id`` 与父 id 都必须能由算法重算出来。

    另外查一条「跨文档不撞」：一个 ``section_id`` 出现在两个文档下，说明这个版本里混着
    **不带文档 id 的旧 id**（改造前的算法）——那正是本次改造要消灭的形状。
    """

    rows = list(rows)
    section_ids = {row.section_id for row in rows if row.section_id}
    documents_by_section: dict[str, set[str]] = {}
    for row in rows:
        if row.section_id:
            documents_by_section.setdefault(row.section_id, set()).add(row.document_id)

    mismatched_sections: list[str] = []
    mismatched_parents: list[str] = []
    missing_parents: list[str] = []
    parented = 0
    with_body = 0
    without_body = 0

    for row in rows:
        expected = section_id_of(
            document_id=row.document_id, heading_path=row.heading_path
        )
        if row.section_id != expected:
            mismatched_sections.append(
                f"{row.document_id}/{row.heading_path or '(空路径)'}"
            )

        expected_parent = parent_section_id_of(
            document_id=row.document_id, heading_path=row.heading_path
        )
        stored_parent = row.parent_section_id or None
        if stored_parent is None:
            if expected_parent is not None:
                missing_parents.append(
                    f"{row.document_id}/{row.heading_path or '(空路径)'}"
                )
            continue
        parented += 1
        if stored_parent != expected_parent:
            mismatched_parents.append(
                f"{row.document_id}/{row.heading_path or '(空路径)'} → {stored_parent}"
            )
            continue
        if stored_parent in section_ids:
            with_body += 1
        else:
            # 父节只有标题（正文全在子节里），因此不产切片：正常，单独计量。
            without_body += 1

    problems: list[IdentityProblem] = []

    def _add(kind: str, message: str, values: list[str]) -> None:
        if values:
            problems.append(
                IdentityProblem(
                    kind=kind,
                    message=message,
                    count=len(values),
                    examples=tuple(values[:MAX_EXAMPLES]),
                )
            )

    _add(
        "section_id_mismatch",
        "section_id 与 stable_hash(文档 id : 标题路径) 对不上（这个版本混了别的算法）",
        mismatched_sections,
    )
    _add(
        "parent_id_mismatch",
        "parent_section_id 与「上一级路径 + 同一份文档 id」算出来的 id 对不上",
        mismatched_parents,
    )
    _add(
        "parent_id_missing",
        "parent_section_id 为空，但这个标题路径本该有父节",
        missing_parents,
    )
    shared = {
        section_id: docs
        for section_id, docs in documents_by_section.items()
        if len(docs) > 1
    }
    _add(
        "section_shared_across_documents",
        "同一个 section_id 出现在两个文档下（不带文档 id 的旧 id 混进来了）",
        [f"{section_id} → {', '.join(sorted(docs))}" for section_id, docs in shared.items()],
    )

    return IdentityReport(
        sections=len(section_ids),
        parented=parented,
        parent_with_body=with_body,
        parent_without_body=without_body,
        problems=tuple(problems),
    )


def describe_counts(counts: BuildCounts) -> Sequence[tuple[str, str]]:
    """把四项计数摆成 ``(标签, 值)``：脚本按这个顺序打印，值可能是 ``n/a``。"""

    return (
        ("knowledge_bases", str(counts.knowledge_bases)),
        ("chunks", str(counts.chunks)),
        ("vectors", str(counts.vectors)),
        (
            "keyword_documents",
            "n/a" if counts.keyword_documents is None else str(counts.keyword_documents),
        ),
    )


__all__ = [
    "MAX_EXAMPLES",
    "CountsVerdict",
    "IdentityProblem",
    "IdentityReport",
    "IdentityRow",
    "VerificationReport",
    "describe_counts",
    "judge_counts",
    "verify_identity",
]

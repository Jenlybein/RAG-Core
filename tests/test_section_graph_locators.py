"""#158 · 定位符规则：R1 标题名引用 / R2 编号定位（ADR-0027 决策 8）。

决策 8 的两条规则都只比对「作者写下的定位符」，不引入相似度。两类用例：

1. **认得出、落得准**：标注集里真实漏掉的那种写法（文档编号 + 引号标题名、`见翻案条件`、
   `决策 N`、`第 N 条`、`第 M 章第 N 节`、`Section N`）要建出边，且落到**那一节**；
2. **认不出就不建**：唯一性、短名保护、无文档上下文的引号短语、范围引用、否定语境——
   每一条都是精度守卫，少一条精度就塌（ADR-0027 决策 8「三条守卫」）。

用例的形状取自 ``eval/graph-labels/labels.json`` 里的真实漏项（``eng-*`` / ``pol-*`` /
``min-*``），但**不用那份语料**：这里只造最小目录，钉住规则本身。
"""

from __future__ import annotations

from app.models.section_edge import RELATION_REFERENCE
from app.services.section_graph import (
    EdgeScope,
    SectionCatalog,
    SectionRecord,
    SectionText,
    extract_section_edges,
)
from app.utils.section_identity import section_id_of

TENANT = "tenant-a"
KB = "kb-1"
VERSION = "v1"
SCOPE = EdgeScope(tenant_id=TENANT, kb_id=KB, index_version=VERSION)


# ─────────────────────────────────────────────────────────────── 构造器


def _record(
    *,
    document_id: str = "doc-a",
    title: str = "售后手册",
    heading: str = "2.1 退款",
    order: int = 0,
    source_filename: str = "",
) -> SectionRecord:
    return SectionRecord(
        tenant_id=TENANT,
        kb_id=KB,
        index_version=VERSION,
        document_id=document_id,
        document_title=title,
        section_id=section_id_of(document_id=document_id, heading_path=heading),
        heading_path=heading,
        order=order,
        source_filename=source_filename,
    )


def _catalog(*records: SectionRecord) -> SectionCatalog:
    return SectionCatalog.of(records)


def _source(heading: str = "2.1 退款", *, text: str, title: str = "售后手册") -> SectionText:
    return SectionText(record=_record(heading=heading, title=title), text=text)


def _edges(*, sections: tuple[SectionText, ...], catalog: SectionCatalog, scope=SCOPE):
    return extract_section_edges(
        scope=scope,
        document_id="doc-a",
        document_title="售后手册",
        sections=sections,
        catalog=catalog,
    )


def _reference_targets(edges, source_heading: str) -> set[str]:
    return {
        edge.target_section_id
        for edge in edges
        if edge.relation_type == RELATION_REFERENCE
        and edge.metadata["source_heading_path"] == source_heading
    }


# ───────────────────────────────────────────── R1：引号标题名


def test_a_quoted_heading_name_after_an_end_of_document_marker_resolves() -> None:
    """「见文末『后续修订（第 7 步）』」：引号里的名字是目标标题的前缀（eng-0013-01）。"""

    heading = "后续修订（第 7 步，#73，2026-11-10）：折叠开关改由密钥配置提供"
    target = _record(heading=heading, order=1)
    text = "两个折叠开关的归属已由第 7 步修订改为「密钥配置」，见文末「后续修订（第 7 步）」。"
    edges = _edges(
        sections=(_source(heading="0013. 一条检索结果代表一节", text=text),),
        catalog=_catalog(target),
    )

    assert _reference_targets(edges, "0013. 一条检索结果代表一节") == {target.section_id}


def test_an_adr_code_plus_a_quoted_heading_scopes_the_name_to_that_document() -> None:
    """「ADR-0013『备选方案』」：文档编号给出目标文档，引号里的标题名给出节（eng-0028-05）。"""

    doc_title = "0013. 一条检索结果代表一节，而不是一个切片"
    target = _record(
        document_id="doc-0013", title=doc_title, heading=f"{doc_title} > 备选方案", order=1
    )
    text = "实测复刻生产逻辑、复刻错了，三次数字全错（ADR-0013「备选方案」最后一条）。"
    edges = _edges(
        sections=(_source(heading="被否掉的方案", text=text),),
        catalog=_catalog(target),
    )

    assert _reference_targets(edges, "被否掉的方案") == {target.section_id}


def test_a_bare_heading_name_after_a_citation_verb_resolves() -> None:
    """「（见翻案条件）」：紧跟引用动词的裸标题名（eng-0027-11）。"""

    target = _record(heading="翻案条件", order=1)
    text = "等真需要多跳再说（见翻案条件），而不是先上一套图数据库。"
    edges = _edges(
        sections=(
            _source(heading="被否掉的方案", text=text),
            SectionText(
                record=_record(heading="翻案条件", order=1), text="新语料里仍抽不出足够的边。"
            ),
        ),
        catalog=_catalog(target),
    )

    assert _reference_targets(edges, "被否掉的方案") == {target.section_id}


def test_a_quoted_heading_name_that_fits_two_sections_is_ambiguous() -> None:
    """「本文『后果』一节」：本文里有两节都叫「后果」→ 唯一性守卫弃权（eng-0013-06）。"""

    root = _record(heading="0013. 一条检索结果代表一节", order=0)
    top = _record(heading="0013. 一条检索结果代表一节 > 后果", order=1)
    nested = _record(heading="0013. 一条检索结果代表一节 > 后续修订 > 后果", order=2)
    text = "与本文「后果」一节那两句话冲突之处以此节为准。"
    edges = _edges(
        sections=(_source(heading="后续修订", text=text),),
        catalog=_catalog(root, top, nested),
    )

    assert _reference_targets(edges, "后续修订") == set()


def test_a_two_character_bare_heading_name_is_not_even_tried() -> None:
    """「见背景四条」：两字标题在正文里到处都是，短名保护直接不试（eng-0028-03）。"""

    background = _record(heading="背景", order=1)
    text = "见背景四条：每条子查询再改写一次。"
    edges = _edges(
        sections=(
            _source(heading="被否掉的方案", text=text),
            SectionText(
                record=_record(heading="背景", order=1), text="Research 门控要一次调用一份账。"
            ),
        ),
        catalog=_catalog(background),
    )

    assert _reference_targets(edges, "被否掉的方案") == set()


def test_a_quoted_heading_name_without_a_document_context_creates_no_edge() -> None:
    """引号里的短语没有「本文 / 文末 / 文档编号 / 链接」给出上下文：那只是在引用原文。"""

    top = _record(heading="后果", order=1)
    text = "这里讲的「后果」不是检索质量那件事。"
    edges = _edges(
        sections=(_source(heading="背景", text=text),),
        catalog=_catalog(top),
    )

    assert _reference_targets(edges, "背景") == set()


def test_a_link_plus_a_quoted_heading_lands_on_that_section_not_the_root() -> None:
    """「[ADR-0010](…md)『第三次修订』」：链接只指到文件，节靠链接外面的引号标题（eng-0013-08）。"""

    doc_title = "0010. 检索契约：RRF 融合、三种结局与索引范围"
    root = _record(document_id="doc-0010", title=doc_title, heading=doc_title, order=0)
    revision = _record(
        document_id="doc-0010",
        title=doc_title,
        heading=f"{doc_title} > 第三次修订（第 7 步，#73，2026-11-10）：请求只剩两个字段",
        order=2,
        source_filename="0010-retrieval-contract.md",
    )
    text = "请求体只留两个字段（见 [ADR-0010](0010-retrieval-contract.md)「第三次修订」）。"
    edges = _edges(
        sections=(_source(heading="后续修订（第 7 步）", text=text),),
        catalog=_catalog(root, revision),
    )

    assert _reference_targets(edges, "后续修订（第 7 步）") == {revision.section_id}

"""#152 · 三种边各自的识别规则与**不建边**的边界（纯函数，不连库、不发网络）。

这一层是 ADR-0027 的核心：**只认文档里写明的表达**，目标有歧义就不建，绝不按语义相似度
猜边。所以用例按「哪种表达 → 建哪条边」与「哪种情形 → 一条都不建」两类组织，且后者比前者
多——图谱最容易长成的坏样子是「抽错边」（假引用），不是「抽少了」。

六个正交的判断点：

1. **节点是节**：端点用 ``section_identity.section_id_of``，带文档 id，不是切片 id；
2. **引用只认显式表达**：``参见《X》第 N 节`` / ``本文件第 N 节`` / Markdown 链接 / 同文档锚点；
3. **歧义不建**：文档名对上两篇、节号对上两节、节号解析不出、目标节在本版本里不存在——
   全都不建（悬空边＝一个不会报错的错答案）；
4. **兄弟按标题路径判**：同一父标题下的**相邻**两节（ADR-0027「2.3 与 2.4」），不是相邻切片；
5. **父节按标题层级**：父子两侧用同一份算法算 id，父节只在本版本里有节时才建；
6. **作用域不可混**：边的两端必须同租户、同库、同版本，跨出去的候选一律丢。
"""

from __future__ import annotations

from app.models.section_edge import RELATION_PARENT, RELATION_REFERENCE, RELATION_SIBLING
from app.services.section_graph import (
    CHUNK_TYPE_REFERENCE_POINTER,
    EdgeScope,
    SectionCatalog,
    SectionRecord,
    SectionText,
    extract_section_edges,
    is_reference_pointer,
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
    tenant_id: str = TENANT,
    kb_id: str = KB,
    index_version: str = VERSION,
) -> SectionRecord:
    """一节的身份行；``section_id`` 由**生产那份算法**算（ADR-0025 决策五）。"""

    return SectionRecord(
        tenant_id=tenant_id,
        kb_id=kb_id,
        index_version=index_version,
        document_id=document_id,
        document_title=title,
        section_id=section_id_of(document_id=document_id, heading_path=heading),
        heading_path=heading,
        order=order,
    )


def _catalog(*records: SectionRecord) -> SectionCatalog:
    return SectionCatalog.of(records)


def _source(heading: str = "2.1 退款", *, text: str, title: str = "售后手册") -> SectionText:
    return SectionText(
        record=_record(heading=heading, title=title),
        text=text,
    )


def _edges(*, sections: tuple[SectionText, ...], catalog: SectionCatalog, scope=SCOPE):
    return extract_section_edges(
        scope=scope,
        document_id="doc-a",
        document_title="售后手册",
        sections=sections,
        catalog=catalog,
    )


def _pairs(edges) -> set[tuple[str, str]]:
    """把边压成 ``(源标题路径, 目标标题路径)`` 的集合，断言只关心「连了谁」。"""

    return {(edge.source_heading_path, edge.target_heading_path) for edge in edges}


def _of_type(edges, relation_type: str):
    return [edge for edge in edges if edge.relation_type == relation_type]


# ─────────────────────────────────────────── 引用：跨文档的显式表达才建边


def test_a_titled_cross_document_reference_creates_an_edge() -> None:
    """「详见《促销规则》2.4 节」→ 一条 ``reference`` 边，指向那一节的 id。"""

    target = _record(document_id="doc-b", title="促销规则", heading="2.4 到账时限", order=1)
    edges = _edges(
        sections=(_source(text="退款超期需人工审核，详见《促销规则》2.4 节。"),),
        catalog=_catalog(target),
    )

    reference = _of_type(edges, RELATION_REFERENCE)
    assert len(reference) == 1
    edge = reference[0]
    source = _source(text="退款超期需人工审核，详见《促销规则》2.4 节。").record
    assert (edge.source_document_id, edge.source_section_id) == ("doc-a", source.section_id)
    assert (edge.target_document_id, edge.target_section_id) == ("doc-b", target.section_id)
    # 证据留在 metadata 里：排障时先看命中的那一句。
    assert edge.metadata["expression"] == "《促销规则》2.4 节"
    assert "详见" in edge.metadata["sentence"]


def test_a_same_document_section_reference_resolves_inside_the_document() -> None:
    """「本文件第 3 节」按**文档内顺序**解析（第 3 个节），不跨文档。"""

    first = _record(heading="1 总则", order=0)
    second = _record(heading="2 范围", order=1)
    third = _record(heading="3 时效", order=2)
    edges = _edges(
        sections=(
            _source(heading="1 总则", text="索赔时效见本文件第 3 节。"),
            SectionText(record=second, text="本节讲范围。"),
            SectionText(record=third, text="时效为 7 天。"),
        ),
        catalog=_catalog(first, second, third),
    )

    reference = _of_type(edges, RELATION_REFERENCE)
    assert [(edge.source_section_id, edge.target_section_id) for edge in reference] == [
        (first.section_id, third.section_id)
    ]


def test_a_markdown_anchor_resolves_within_the_same_document() -> None:
    """同文档锚点：``[参见](#2-4-到账时限)`` 按标题文本归一化后匹配。"""

    anchor_target = _record(heading="2.4 到账时限", order=1)
    edges = _edges(
        sections=(_source(text="到账时限见[这里](#2-4-到账时限)。"),),
        catalog=_catalog(anchor_target),
    )

    reference = _of_type(edges, RELATION_REFERENCE)
    assert [edge.target_section_id for edge in reference] == [anchor_target.section_id]


def test_a_markdown_link_to_another_document_resolves_by_title() -> None:
    """跨文档 Markdown 链接：按链接文件名（去扩展名）匹配文档标题。

    目标文档只有一个顶层节时那条边指向它；多个顶层节时「整篇」有歧义，不建边
    （节点是节，不是文档——ADR-0027 决策 1）。
    """

    only_root = _record(document_id="doc-b", title="促销规则", heading="促销规则", order=0)
    edges = _edges(
        sections=(_source(text="折扣叠加规则见[促销规则](促销规则.md)。"),),
        catalog=_catalog(only_root),
    )

    assert [edge.target_section_id for edge in _of_type(edges, RELATION_REFERENCE)] == [
        only_root.section_id
    ]


def test_a_markdown_link_into_a_document_with_several_top_level_sections_is_ambiguous() -> None:
    """整篇有多个顶层节时「指向哪一节」无从判断：宁可不建。"""

    one = _record(document_id="doc-b", title="促销规则", heading="一 满减", order=0)
    two = _record(document_id="doc-b", title="促销规则", heading="二 优惠券", order=1)
    edges = _edges(
        sections=(_source(text="规则见[促销规则](促销规则.md)。"),),
        catalog=_catalog(one, two),
    )

    assert _of_type(edges, RELATION_REFERENCE) == []


# ───────────────────────────────────────────────── 不建边：歧义、缺失、猜边


def test_two_documents_sharing_a_title_make_the_reference_ambiguous() -> None:
    """《促销规则》对上两篇文档：不建边（ADR-0027「宁可少一条，不可错一条」）。"""

    same_title_one = _record(document_id="doc-b", title="促销规则", heading="2.4 到账时限", order=0)
    same_title_two = _record(document_id="doc-c", title="促销规则", heading="2.4 到账时限", order=0)

    edges = _edges(
        sections=(_source(text="详见《促销规则》2.4 节。"),),
        catalog=_catalog(same_title_one, same_title_two),
    )

    assert _of_type(edges, RELATION_REFERENCE) == []


def test_two_sections_with_the_same_number_in_the_target_make_it_ambiguous() -> None:
    """目标文档里有两个都以 «2.4» 开头的节：节号解析有歧义，不建边。"""

    one = _record(document_id="doc-b", title="促销规则", heading="2.4 到账时限", order=0)
    two = _record(document_id="doc-b", title="促销规则", heading="2.4 退款路径", order=1)

    edges = _edges(
        sections=(_source(text="详见《促销规则》2.4 节。"),),
        catalog=_catalog(one, two),
    )

    assert _of_type(edges, RELATION_REFERENCE) == []


def test_a_reference_to_a_section_that_does_not_exist_creates_no_edge() -> None:
    """目标版本里没有 9.9 节：悬空边一律不建。"""

    target = _record(document_id="doc-b", title="促销规则", heading="2.4 到账时限", order=0)

    edges = _edges(
        sections=(_source(text="详见《促销规则》9.9 节。"),),
        catalog=_catalog(target),
    )

    assert _of_type(edges, RELATION_REFERENCE) == []


def test_a_bare_section_number_without_a_qualifier_creates_no_edge() -> None:
    """「第 3 节」没说是哪个文件的：两种解读都成立，歧义 → 不建边。"""

    third = _record(heading="3 时效", order=2)
    other = _record(document_id="doc-b", title="促销规则", heading="3 时效", order=2)

    edges = _edges(
        sections=(_source(text="详见第 3 节。"),),
        catalog=_catalog(third, other),
    )

    assert _of_type(edges, RELATION_REFERENCE) == []


def test_semantically_similar_sections_without_an_expression_have_no_edge() -> None:
    """防猜测的回归用例：两节讲同一件事，但正文里没有任何显式表达 → 一条引用边都没有。"""

    sibling_topic = _record(heading="2.2 退款审核", order=1)
    other_doc = _record(document_id="doc-b", title="促销规则", heading="2.4 退款到账", order=0)
    edges = _edges(
        sections=(
            _source(heading="2.1 退款", text="退款申请需要人工审核，超期未处理会升级。"),
            SectionText(record=sibling_topic, text="退款审核由风控团队执行，三个工作日内完成。"),
        ),
        catalog=_catalog(sibling_topic, other_doc),
    )

    assert _of_type(edges, RELATION_REFERENCE) == []


def test_a_reference_to_a_document_without_any_section_number_creates_no_edge() -> None:
    """「参见《促销规则》」没有节号：指向整篇，而节点是节 → 不建边。"""

    target = _record(document_id="doc-b", title="促销规则", heading="2.4 到账时限", order=0)

    edges = _edges(sections=(_source(text="参见《促销规则》。"),), catalog=_catalog(target))

    assert _of_type(edges, RELATION_REFERENCE) == []


# ───────────────────────────────────────────────── 兄弟节：同一父标题下相邻


def test_sibling_edges_join_consecutive_sections_under_the_same_parent() -> None:
    """2.1 / 2.2 / 2.3 同属「2 退款」：相邻两对各一条边，两头都连（有向边存两行）。"""

    one = _record(heading="2 退款 > 2.1 申请", order=0)
    two = _record(heading="2 退款 > 2.2 审核", order=1)
    three = _record(heading="2 退款 > 2.3 到账", order=2)
    edges = _edges(
        sections=(
            SectionText(record=one, text="提交申请。"),
            SectionText(record=two, text="人工审核。"),
            SectionText(record=three, text="三个工作日到账。"),
        ),
        catalog=_catalog(one, two, three),
    )

    siblings = _of_type(edges, RELATION_SIBLING)
    assert {(edge.source_section_id, edge.target_section_id) for edge in siblings} == {
        (one.section_id, two.section_id),
        (two.section_id, one.section_id),
        (two.section_id, three.section_id),
        (three.section_id, two.section_id),
    }
    assert (one.section_id, three.section_id) not in {
        (edge.source_section_id, edge.target_section_id) for edge in siblings
    }, "2.1 与 2.3 不相邻：兄弟边只连相邻两节（ADR-0027 的「2.3 与 2.4」）"


def test_sections_under_different_parents_are_not_siblings() -> None:
    """标题路径不同父：即使文档顺序相邻，也不是兄弟。"""

    refund = _record(heading="2 退款 > 2.1 申请", order=0)
    invoice = _record(heading="3 发票 > 3.1 开具", order=1)
    edges = _edges(
        sections=(
            SectionText(record=refund, text="提交申请。"),
            SectionText(record=invoice, text="开具发票。"),
        ),
        catalog=_catalog(refund, invoice),
    )

    assert _of_type(edges, RELATION_SIBLING) == []


# ───────────────────────────────────────────────────── 父节：标题层级


def test_parent_edges_follow_the_heading_hierarchy() -> None:
    """子节 → 父节一条边；父节自己有正文时才建（父节只有标题时它在本版本里没有节）。"""

    parent = _record(heading="2 退款", order=0)
    child = _record(heading="2 退款 > 2.1 申请", order=1)
    edges = _edges(
        sections=(
            SectionText(record=parent, text="本章讲退款。"),
            SectionText(record=child, text="提交申请。"),
        ),
        catalog=_catalog(parent, child),
    )

    parents = _of_type(edges, RELATION_PARENT)
    assert [(edge.source_section_id, edge.target_section_id) for edge in parents] == [
        (child.section_id, parent.section_id)
    ]


def test_a_parent_without_a_body_gets_no_parent_edge() -> None:
    """父节只有标题、正文全在子节里 → 父节不产切片，本版本里没有它，边不建。"""

    child = _record(heading="2 退款 > 2.1 申请", order=0)
    edges = _edges(
        sections=(SectionText(record=child, text="提交申请。"),),
        catalog=_catalog(child),
    )

    assert _of_type(edges, RELATION_PARENT) == []


def test_a_top_level_section_has_no_parent() -> None:
    """路径只有一段时没有父节（``parent_section_id_of`` 回 ``None``）。"""

    only = _record(heading="退款", order=0)
    edges = _edges(sections=(SectionText(record=only, text="提交申请。"),), catalog=_catalog(only))

    assert _of_type(edges, RELATION_PARENT) == []


# ────────────────────────────────────────────── 作用域：两端必须同租同库同版本


def test_an_edge_across_tenants_is_rejected() -> None:
    """目标节在别的租户里：不建边。关系本身不能成为越权通道（ADR-0027 决策 5）。"""

    foreign = _record(
        document_id="doc-x", title="促销规则", heading="2.4 到账时限", tenant_id="tenant-b"
    )
    edges = _edges(
        sections=(_source(text="详见《促销规则》2.4 节。"),),
        catalog=_catalog(foreign),
    )

    assert _of_type(edges, RELATION_REFERENCE) == []


def test_an_edge_across_index_versions_is_rejected() -> None:
    """目标节在另一个版本里：不建边（旧边指向新版本同 id 的节就是一个不会报错的错答案）。"""

    foreign = _record(
        document_id="doc-b", title="促销规则", heading="2.4 到账时限", index_version="v2"
    )
    edges = _edges(
        sections=(_source(text="详见《促销规则》2.4 节。"),),
        catalog=_catalog(foreign),
    )

    assert _of_type(edges, RELATION_REFERENCE) == []


def test_an_edge_across_knowledge_bases_is_rejected() -> None:
    """跨库同样不建：本票的建边只在同一库的同一份构建产物里发生。

    （跨库引用在 ADR-0027 里是允许的，但那要等 A2 按请求的库范围做注入过滤时才成立；
    本票的目录是按「租户 + 库 + 版本」读的，跨库节根本不在目录里。）
    """

    foreign = _record(document_id="doc-b", title="促销规则", heading="2.4 到账时限", kb_id="kb-2")
    edges = _edges(
        sections=(_source(text="详见《促销规则》2.4 节。"),),
        catalog=_catalog(foreign),
    )

    assert _of_type(edges, RELATION_REFERENCE) == []


def test_every_edge_carries_the_scope_it_was_extracted_in() -> None:
    """作用域只有一个来源：抽取时给的那个 ``EdgeScope``。混着写就会串版本。"""

    target = _record(document_id="doc-b", title="促销规则", heading="2.4 到账时限", order=1)
    edges = _edges(
        sections=(_source(text="详见《促销规则》2.4 节。"),),
        catalog=_catalog(target),
    )

    assert edges, "这条用例得先有边"
    assert {(edge.tenant_id, edge.kb_id, edge.index_version) for edge in edges} == {
        (TENANT, KB, VERSION)
    }


def test_the_same_edge_is_collapsed_to_one() -> None:
    """同一节里两片都写了同一条引用：只留一条边（唯一约束的输入侧去重）。"""

    target = _record(document_id="doc-b", title="促销规则", heading="2.4 到账时限", order=1)
    section = _source(text="详见《促销规则》2.4 节。")
    edges = _edges(sections=(section, section), catalog=_catalog(target))

    reference = _of_type(edges, RELATION_REFERENCE)
    assert len(reference) == 1
    assert len({edge.unique_key for edge in edges}) == len(edges)


# ────────────────────────────────────────────── 路标切片：判定只看「除引用外有没有内容」


def test_a_pure_pointer_sentence_is_marked_reference_pointer() -> None:
    assert is_reference_pointer("详见《促销活动与优惠券使用规则》2.4 节。") is True
    assert is_reference_pointer("参见《售后手册》第 3 节") is True
    assert is_reference_pointer("到账时限见[这里](#2-4-到账时限)。") is True


def test_a_sentence_with_substance_is_not_a_pointer() -> None:
    """带实质内容的句子即使含引用，也仍是正常切片：它自己回答得了问题。"""

    assert (
        is_reference_pointer(
            "退款超期需人工审核，审核通过后三个工作日内到账，详见《促销规则》2.4 节。"
        )
        is False
    )
    assert is_reference_pointer("提交工单后 3 个工作日内到账。") is False
    assert is_reference_pointer("") is False


def test_the_pointer_type_name_is_the_one_recall_filters_on() -> None:
    """召回侧过滤用的字符串与这里标进 ``meta.chunk_type`` 的必须是同一个常量。"""

    assert CHUNK_TYPE_REFERENCE_POINTER == "reference_pointer"


# ────────────────────────────────────────────────────── 目录自身的口径


def test_the_catalog_dedupes_sections_and_keeps_the_first_order() -> None:
    """同一节的多片共享节身份：目录里每节只有一行，顺序取最先出现的那片。"""

    first = _record(heading="2.1 退款", order=3)
    catalog = _catalog(
        first,
        SectionRecord(
            tenant_id=first.tenant_id,
            kb_id=first.kb_id,
            index_version=first.index_version,
            document_id=first.document_id,
            document_title=first.document_title,
            section_id=first.section_id,
            heading_path=first.heading_path,
            order=7,
        ),
    )

    sections = catalog.of_document("doc-a")
    assert len(sections) == 1
    assert sections[0].order == 3


def test_the_catalog_keeps_documents_apart() -> None:
    """两份文档同名章节：目录里是两个节，不是一节（这正是节 id 带文档 id 的理由）。"""

    a = _record(document_id="doc-a", heading="退款 > 流程", order=0)
    b = _record(document_id="doc-b", title="另一份", heading="退款 > 流程", order=0)
    catalog = _catalog(a, b)

    assert len(catalog.of_document("doc-a")) == 1
    assert len(catalog.of_document("doc-b")) == 1
    assert a.section_id != b.section_id


def test_the_catalog_is_scoped_by_tenant_kb_and_version() -> None:
    """目录按作用域收窄：别的租户 / 库 / 版本的节读不出来，边自然指不过去。"""

    in_scope = _record()
    other_tenant = _record(tenant_id="tenant-b")
    other_version = _record(index_version="v2")
    other_kb = _record(kb_id="kb-2")
    catalog = SectionCatalog.of([in_scope, other_tenant, other_version, other_kb], scope=SCOPE)

    assert catalog.of_document("doc-a"), "本作用域里的节要读得出来"
    assert all(
        (section.tenant_id, section.kb_id, section.index_version) == (TENANT, KB, VERSION)
        for section in catalog.sections
    )

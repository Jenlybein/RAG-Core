"""票 #149：引用块——块结构、渲染串、四条硬校验与三态完整度。

这一层是**纯函数**（模型输出 + 最终材料池 → 引用块 / 渲染串），所以每条判定都可以被
手写的一份「模型输出」直接钉住，不必发一次模型调用：

* 块 = 文本 + 状态 + 两类下标（核心片 / 辅助片）。模型**只点下标**，正文逐字来自材料池；
* 顶层 ``answer`` 只是块的渲染串——它不是第二种产物，因此没有「模型自己写的那一遍」；
* 四条硬校验任一不过 → 抛 :class:`AnswerRejected`（调用方把 ``answer`` 落成 ``null``，
  材料与计划照给）；
* 三态完整度由**核心片数**算出来，与切片编排同一口径（``complete`` / ``partial`` /
  ``missing``），不是模型自评。

四条校验各自守的边界：

1. 有结论的块至少要一条核心片——只引辅助片 / 一条都不引，都不成立；
2. 材料不足的块不许挂下标——「没材料就说没有」是可检验的结构，不是一句提醒；
3. 答案里不许出现未解析的候选编号或内部切片标识——``E9``、``chunk-a``、``候选 3``
   都必须被拦在对外文本之外；
4. 文档冲突时分别保留来源、不替用户裁决——把两种说法并进一块（``conflict``）或在
   多来源块里写「以…为准」，整份答案作废。
"""

from __future__ import annotations

import pytest

from app.services.citation_blocks import (
    REJECTED_ASSERTION_WITHOUT_CORE,
    REJECTED_CITATION_ON_INSUFFICIENT,
    REJECTED_CONFLICT_ADJUDICATED,
    REJECTED_EMPTY_BLOCK,
    REJECTED_UNRESOLVED_REFERENCE,
    STATUS_INSUFFICIENT,
    STATUS_SUPPORTED,
    AnswerRejected,
    FinalAnswer,
    evidence_state_of,
    finalize_answer,
    render_answer,
)

COMPLETE = "complete"
PARTIAL = "partial"
MISSING = "missing"


# ─────────────────────────────────────────────────────────────── 夹具


def _material(
    key: str,
    *,
    content: str | None = None,
    document: str | None = None,
    title: str = "退款政策",
) -> dict:
    """一条最终材料（交付单元的形状：一节一条，带代表片与章节路径）。"""

    return {
        "document_id": document or f"doc-{key}",
        "chunk_id": f"chunk-{key}",
        "kb_id": "kb-a",
        "title": title,
        "content": content if content is not None else f"{key} 的正文。",
        "section_id": f"section-{key}",
        "heading_path": f"退款 > {key}",
    }


def _block(
    text: str,
    *,
    kind: str = STATUS_SUPPORTED,
    primary: tuple[int, ...] = (),
    supporting: tuple[int, ...] = (),
    conflict: bool = False,
) -> dict:
    return {
        "text": text,
        "kind": kind,
        "primary": list(primary),
        "supporting": list(supporting),
        "conflict": conflict,
    }


def _output(*blocks: dict) -> dict:
    return {"blocks": list(blocks)}


# ───────────────────────────────────────────────────────── 块结构：模型只能点下标


def test_a_block_is_text_status_and_resolved_evidence_ids() -> None:
    """一块 = 文本 + 状态 + 已解析的证据下标；下标按材料池顺序变成稳定的 ``E`` 编号。"""

    materials = [_material("a"), _material("b"), _material("c")]
    out = finalize_answer(
        _output(_block("超期退款需人工审核。", primary=(1,), supporting=(3,))),
        materials=materials,
    )

    assert isinstance(out, FinalAnswer)
    assert len(out.citations) == 1
    citation = out.citations[0]
    assert citation.status == STATUS_SUPPORTED
    assert citation.text == "超期退款需人工审核。"
    # ``E1`` = 材料池第 1 条：编号按最终材料顺序分配，同一块内去重、升序。
    assert citation.evidence_ids == ("E1", "E3")


def test_the_answer_is_only_the_rendered_blocks() -> None:
    """顶层 ``answer`` 是块的渲染串：有结论的块带证据编号，材料不足的块明说不足。"""

    materials = [_material("a"), _material("b")]
    out = finalize_answer(
        _output(
            _block("3 个工作日内到账。", primary=(2,)),
            _block(
                "未找到礼券叠加规则的说明。",
                kind=STATUS_INSUFFICIENT,
            ),
        ),
        materials=materials,
    )

    assert out.answer == "3 个工作日内到账。（E2）\n未找到礼券叠加规则的说明。（材料不足）"
    # 渲染串逐字由块拼出来：没有任何一段是块以外的东西。
    assert out.answer == render_answer(out.citations)


def test_the_evidence_numbering_follows_the_material_order_not_the_citation() -> None:
    """先被引用的不一定就是 ``E1``：编号属于材料池那一格，不属于块的出现顺序。"""

    materials = [_material("a"), _material("b")]
    out = finalize_answer(
        _output(
            _block("先引第二条。", primary=(2,)),
            _block("后引第一条。", primary=(1,)),
        ),
        materials=materials,
    )

    assert [c.evidence_ids for c in out.citations] == [("E2",), ("E1",)]
    assert out.answer == "先引第二条。（E2）\n后引第一条。（E1）"


# ─────────────────────────────────────────── 校验一：有结论的块必须有核心片


def test_a_supported_block_without_any_citation_is_rejected() -> None:
    """无引用断言 = 不过：说「是什么」却一条材料都不给，是凭空生成。"""

    with pytest.raises(AnswerRejected) as caught:
        finalize_answer(_output(_block("退款 3 个工作日到账。")), materials=[_material("a")])

    assert caught.value.reason == REJECTED_ASSERTION_WITHOUT_CORE


def test_a_supported_block_with_only_supporting_citation_is_rejected() -> None:
    """只引辅助片就下结论 = 不过：辅助片只能补充背景，撑不起一个结论。"""

    with pytest.raises(AnswerRejected) as caught:
        finalize_answer(
            _output(_block("退款 3 个工作日到账。", supporting=(1,))),
            materials=[_material("a")],
        )

    assert caught.value.reason == REJECTED_ASSERTION_WITHOUT_CORE


# ─────────────────────────────────────── 校验二：材料不足的块不许伪造引用


def test_an_insufficient_block_with_a_citation_is_rejected() -> None:
    """「材料不足」却挂着一条证据：要么它有材料（该 supported），要么它没有（该空着）。"""

    with pytest.raises(AnswerRejected) as caught:
        finalize_answer(
            _output(_block("未找到相关说明。", kind=STATUS_INSUFFICIENT, primary=(1,))),
            materials=[_material("a")],
        )

    assert caught.value.reason == REJECTED_CITATION_ON_INSUFFICIENT


def test_an_insufficient_block_keeps_the_answer_but_marks_the_gap() -> None:
    """有材料缺口时答案照样产出：``insufficient`` 块是**承认不足**，不是失败。"""

    materials = [_material("a")]
    out = finalize_answer(
        _output(
            _block("超期退款需人工审核。", primary=(1,)),
            _block("未找到到账时限的说明。", kind=STATUS_INSUFFICIENT),
        ),
        materials=materials,
    )

    assert [c.status for c in out.citations] == [STATUS_SUPPORTED, STATUS_INSUFFICIENT]
    assert "未找到到账时限的说明。（材料不足）" in out.answer


# ──────────────────────── 校验三：不许出现未解析的候选编号或内部切片标识


def test_an_out_of_range_evidence_index_is_rejected() -> None:
    """模型点了一个不存在的下标（越界）→ 整份答案作废，不猜、不截。"""

    with pytest.raises(AnswerRejected) as caught:
        finalize_answer(
            _output(_block("退款 3 个工作日到账。", primary=(7,))),
            materials=[_material("a"), _material("b")],
        )

    assert caught.value.reason == REJECTED_UNRESOLVED_REFERENCE


def test_an_internal_chunk_identifier_in_the_answer_is_rejected() -> None:
    """正文里漏出切片 id / 节 id / 文档 id —— 内部结构不许进对外文本。"""

    materials = [_material("a", content="超期退款需人工审核。")]
    for leaked in ("chunk-a", "section-a", "doc-a"):
        with pytest.raises(AnswerRejected) as caught:
            finalize_answer(
                _output(_block(f"按 {leaked} 的说法，超期退款需人工审核。", primary=(1,))),
                materials=materials,
            )
        assert caught.value.reason == REJECTED_UNRESOLVED_REFERENCE


def test_an_unresolved_evidence_number_in_the_answer_is_rejected() -> None:
    """模型自己写了一个没被解析的编号（``E9`` / ``候选 3``）→ 不生成答案。"""

    materials = [_material("a")]
    for leaked in ("见 E9 所述。", "见候选 3。"):
        with pytest.raises(AnswerRejected) as caught:
            finalize_answer(
                _output(_block(leaked, primary=(1,))),
                materials=materials,
            )
        assert caught.value.reason == REJECTED_UNRESOLVED_REFERENCE


def test_a_cited_number_inside_the_text_is_allowed() -> None:
    """被这一块真正解析到的编号可以出现在正文里（编号稳定就是为了能被引用）。"""

    materials = [_material("a")]
    out = finalize_answer(
        _output(_block("见 E1：超期退款需人工审核。", primary=(1,))), materials=materials
    )

    assert out.answer == "见 E1：超期退款需人工审核。（E1）"


# ─────────────────────── 校验四：文档冲突时分别保留来源、不替用户裁决


def test_two_sources_of_one_conflict_stay_two_blocks() -> None:
    """两份文档说法不同 → 两块并列、各自带来源；渲染串里两条来源都在，没有任何裁决。"""

    materials = [
        _material("a", content="超期退款需人工审核。", document="doc-a", title="退款政策"),
        _material("b", content="超期退款自动通过。", document="doc-b", title="运营手册"),
    ]
    out = finalize_answer(
        _output(
            _block("超期退款需人工审核。", primary=(1,)),
            _block("超期退款自动通过。", primary=(2,)),
        ),
        materials=materials,
    )

    assert [c.evidence_ids for c in out.citations] == [("E1",), ("E2",)]
    assert "（E1）" in out.answer and "（E2）" in out.answer


def test_a_conflict_merged_into_one_block_is_rejected() -> None:
    """把两种说法并进一块（``conflict``）→ 作废：那正是「替用户裁决」的形状。"""

    materials = [
        _material("a", document="doc-a"),
        _material("b", document="doc-b"),
    ]
    with pytest.raises(AnswerRejected) as caught:
        finalize_answer(
            _output(_block("两份文档说法不同。", primary=(1, 2), conflict=True)),
            materials=materials,
        )

    assert caught.value.reason == REJECTED_CONFLICT_ADJUDICATED


def test_a_conflict_verdict_across_sources_is_rejected() -> None:
    """多来源块里写「以…为准」/「更权威」→ 作废：谁更权威由用户决定，不由这里决定。"""

    materials = [_material("a", document="doc-a"), _material("b", document="doc-b")]
    for verdict in ("以《退款政策》为准。", "《退款政策》更权威。", "建议采信运营手册。"):
        with pytest.raises(AnswerRejected) as caught:
            finalize_answer(
                _output(_block(verdict, primary=(1,), supporting=(2,))),
                materials=materials,
            )
        assert caught.value.reason == REJECTED_CONFLICT_ADJUDICATED


def test_a_single_source_may_use_verdict_wording() -> None:
    """只有一个来源时「以…为准」不是裁决，而是原文的措辞——不误伤正常材料。"""

    materials = [_material("a", content="以银行实际到账时间为准。")]
    out = finalize_answer(
        _output(_block("以银行实际到账时间为准。", primary=(1,))), materials=materials
    )

    assert out.citations[0].evidence_ids == ("E1",)


# ───────────────────────────────────────────── 空的 / 坏的模型输出


def test_an_empty_block_list_is_rejected() -> None:
    """模型一块都没给 → 没有可校验的答案（``answer`` 因此是 ``null``）。"""

    with pytest.raises(AnswerRejected) as caught:
        finalize_answer(_output(), materials=[_material("a")])

    assert caught.value.reason == REJECTED_EMPTY_BLOCK


def test_a_block_without_text_is_rejected() -> None:
    """空文本不是一块（它无法被引用，也无法被读）。"""

    with pytest.raises(AnswerRejected) as caught:
        finalize_answer(_output(_block("   ", primary=(1,))), materials=[_material("a")])

    assert caught.value.reason == REJECTED_EMPTY_BLOCK


# ───────────────────────────────────────── 三态完整度：由核心片数算出来


def test_the_evidence_state_is_complete_when_every_block_has_a_core_piece() -> None:
    """所有块都有核心片 → ``complete``（与切片编排同一口径）。"""

    materials = [_material("a"), _material("b")]
    out = finalize_answer(
        _output(
            _block("超期退款需人工审核。", primary=(1,)),
            _block("3 个工作日内到账。", primary=(2,)),
        ),
        materials=materials,
    )

    assert out.evidence_state == COMPLETE


def test_the_evidence_state_is_partial_when_some_blocks_lack_a_core_piece() -> None:
    """部分块有核心片 → ``partial``。"""

    materials = [_material("a")]
    out = finalize_answer(
        _output(
            _block("超期退款需人工审核。", primary=(1,)),
            _block("未找到到账时限的说明。", kind=STATUS_INSUFFICIENT),
        ),
        materials=materials,
    )

    assert out.evidence_state == PARTIAL


def test_the_evidence_state_is_missing_without_any_core_piece() -> None:
    """一条核心片都没有（材料池空 / 全是 ``insufficient``）→ ``missing``。"""

    assert evidence_state_of(citations=(), materials=()) == MISSING

    materials = [_material("a")]
    out = finalize_answer(
        _output(_block("未找到相关说明。", kind=STATUS_INSUFFICIENT)),
        materials=materials,
    )
    assert out.evidence_state == MISSING

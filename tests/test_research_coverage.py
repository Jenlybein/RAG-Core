"""票 #164 / ADR-0029 决策三：Research 收窄——分解独立于材料 + 逐要点覆盖判定 + 定向补证 + 缺口闸。

本票没有重写状态机：固定五步、六条硬上限、七种停止原因、四条引用硬校验全部照旧。改的只有
**基准从哪里来**：

```text
改前：要点从材料里归纳 → 「材料够不够」也从材料侧数（完不成 complete）
改后：要点只从问题里来 → 逐个要点问「材料里有没有直接支撑它的」 → none 可表达
      → 只给 none 的要点补检索 → 剩下的 none 进 coverage.gaps → 答案必须声明不足
```

本文件守四件事（其余性质留在 ``test_research_state_machine.py`` / ``test_research_finalizer.py``）：

1. **分解的输入里没有任何材料**（决策三的全部意义）：规划提示词与问题**逐字相等**，且
   :func:`app.services.research.build_planner_prompt` 没有可装材料的参数；
2. **覆盖判定是逐要点的**，``none`` 是可表达的（不是「有一条查询」的副作用），材料侧数出来的
   字典（``evidence_state``）不许替它说话；
3. **补证只针对没有支撑的要点**：绑到已支撑要点上的查询一律不执行；
4. **缺口闸**：有 gap 时答案必须声明「材料不足」，否则整份答案作废（``answer = null``）；
   没有 gap 时不许平白冒出这句。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping, Sequence
from typing import Any

from app.providers.llm.base import LLMProvider
from app.services import research
from app.services.citation_blocks import REJECTED_GAP_NOT_DECLARED
from app.services.research_limits import (
    MAX_FIRST_ROUND_QUERIES,
    StopReason,
)

TENANT = "tenant_demo"
QUERY = "退款多久到账，需要哪些材料"


# ─────────────────────────────────────────────────────────────── 离线替身


def _row(key: str, *, content: str | None = None) -> dict[str, Any]:
    """一条交付单元（内核折叠之后的形状：一节一条，带代表片 id 与章节路径）。"""

    return {
        "document_id": f"doc-{key}",
        "chunk_id": f"chunk-{key}",
        "kb_id": "kb-a",
        "title": "退款政策",
        "content": content if content is not None else f"{key} 的正文。",
        "score": 0.9,
        "section_id": f"section-{key}",
        "heading_path": f"退款 > {key}",
    }


class _Search:
    """检索内核替身：按子查询返回预置行（``default`` 兜底），并记下每一次的查询串。"""

    def __init__(
        self,
        rows_for: Mapping[str, Sequence[dict[str, Any]]] | None = None,
        *,
        default: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.rows_for = dict(rows_for or {})
        self.default = list(default)
        self.calls: list[str] = []

    async def __call__(self, sub_query: str) -> list[dict[str, Any]]:
        self.calls.append(sub_query)
        return [dict(row) for row in self.rows_for.get(sub_query, self.default)]


class _Scripted(LLMProvider):
    """脚本替身：按顺序交出预置输出；预置项是异常就当场抛出。"""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []
        self.prompts: list[str] = []

    async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
        self.calls.append(schema.__name__)
        self.prompts.append(user)
        if not self._responses:
            raise AssertionError(f"预置输出用完了：还有一次 {schema.__name__} 没有应答")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def chat_json(self, *, system: str, user: str) -> dict:
        raise AssertionError("Research 只走结构化输出")


def _factory(llm: LLMProvider):
    async def _get() -> LLMProvider:
        return llm

    return _get


def _plan(
    points: list[str] | None = None, queries: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """规划器的预置输出：要点 + 首轮查询（每个要点一条）。"""

    points = points if points is not None else ["退款时限", "所需材料"]
    return {
        "points": points,
        "queries": queries
        if queries is not None
        else [{"point": index, "query": f"查询{index}"} for index in range(1, len(points) + 1)],
    }


def _judge(
    *verdicts: dict[str, Any], queries: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """覆盖判定的预置输出：逐要点判定（supported / none）+ 只针对 none 的补证查询。"""

    return {"points": list(verdicts), "queries": queries or []}


def _supported(point: int, *materials: int) -> dict[str, Any]:
    return {"point": point, "status": "supported", "materials": list(materials)}


def _none(point: int) -> dict[str, Any]:
    return {"point": point, "status": "none", "materials": []}


def _blocks(*items: dict[str, Any]) -> dict[str, Any]:
    return {"blocks": list(items)}


def _block(
    text: str,
    *,
    kind: str = "supported",
    primary: tuple[int, ...] = (),
    supporting: tuple[int, ...] = (),
) -> dict[str, Any]:
    return {
        "text": text,
        "kind": kind,
        "primary": list(primary),
        "supporting": list(supporting),
    }


def _run(
    *responses: object,
    search: _Search | None = None,
    strong: LLMProvider | None = None,
) -> tuple[research.ResearchOutcome, _Scripted, _Search]:
    """跑一次完整五步：``responses`` 是规划 + 覆盖判定的预置输出，生成另有一份脚本替身。"""

    probe = search if search is not None else _Search()
    llm = _Scripted(*responses)
    generator = (
        strong
        if strong is not None
        else _Scripted(_blocks(_block("3 个工作日内到账。", primary=(1,))))
    )
    outcome = asyncio.run(
        research.run_research(
            query=QUERY,
            tenant_id=TENANT,
            search=probe,
            llm_factory=_factory(llm),
            strong_llm_factory=_factory(generator),
        )
    )
    return outcome, llm, probe


# ───────────────────────────────────────── ① 分解的输入里没有任何材料（决策三）


def test_the_planner_input_is_the_question_and_nothing_else() -> None:
    """规划提示词与问题**逐字相等**——材料一个字都进不去。

    断言整串相等而不是 ``not in``：往规划提示词里加任何东西（材料摘录、材料目录、上一轮
    的计划）都会让这条红。这正是决策三要守的东西——一旦模型看见了材料，它就会按「材料支持
    什么」来写要点，于是「材料里没有的东西」永远不会进要点，缺口永远报不出来。
    """

    search = _Search(
        {
            "查询1": [_row("a", content="超期退款需人工审核后才到账。")],
            "查询2": [_row("b", content="需要提供订单号与银行流水。")],
        }
    )

    _, llm, _ = _run(_plan(), _judge(_supported(1, 1), _supported(2, 2)), search=search)

    assert llm.prompts[0] == f"问题：{QUERY}"


def test_the_planner_builder_has_no_way_to_carry_material() -> None:
    """规划提示词的构造函数**没有第二个参数**：不是「这次恰好没传材料」。

    与上面那条一起构成结构性防护：一条证明这次没喂，一条证明喂不进去。ADR-0029 把这条写成
    「喂材料进去必须红」，两条断言合起来才是那个意思。
    """

    signature = inspect.signature(research.build_planner_prompt)

    assert list(signature.parameters) == ["query"]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )


# ───────────────────────────────────────── ① 规划：每个要点配一条检索查询


def test_a_point_without_a_retrieval_query_is_searched_with_its_own_text() -> None:
    """有要点却没配查询 → **后端用要点正文补一条真查询**，计划照常成立。

    这条改过语义（原来是「计划不成立、退回原问题」），理由是三种取法里的取舍：

    * 补**空**查询会造**假缺口**（没查过被判成材料里没有）；
    * 删掉这个要点会造**假完备**（缺口根本不出现，正是 ADR-0029 决策三的盲区）；
    * 整份计划作废会把一次多轮 Research 降级成单轮检索，为一条查询赔掉整个能力。

    要点正文是一句为这个问题写的话，拿它去检索是**真查询**——与补证阶段对缺口要点的取法
    同一条规则。所以这里断言：两条查询都跑了，第二条用的就是要点正文。
    """

    plan = _plan(points=["退款时限", "所需材料"], queries=[{"point": 1, "query": "查询1"}])
    search = _Search(default=[_row("a")])

    outcome, _, probe = _run(plan, _judge(_supported(1, 1), _supported(2, 1)), search=search)

    assert probe.calls == ["查询1", "所需材料"], "缺的那条要用要点正文补，且不能是空串"
    assert outcome.stop_reason is not StopReason.DEGRADED
    assert [point.point_id for point in outcome.points] == ["A1", "A2"], "要点一个都不许丢"


def test_every_point_gets_its_own_query_even_when_the_round_cap_runs_fewer() -> None:
    """5 个要点各配一条查询 → 计划成立；首轮成本上限只截断**执行**，不截断计划清单本身。"""

    points = ["要一", "要二", "要三", "要四", "要五"]
    plan = _plan(points=points, queries=[{"point": i, "query": f"查询{i}"} for i in range(1, 6)])
    search = _Search(default=[_row("a")])

    outcome, _, probe = _run(plan, _judge(*(_supported(i, 1) for i in range(1, 6))), search=search)

    assert probe.calls == [f"查询{i}" for i in range(1, MAX_FIRST_ROUND_QUERIES + 1)]
    assert [point.text for point in outcome.points] == points
    assert outcome.searches == MAX_FIRST_ROUND_QUERIES, "首轮成本上限照旧管住检索次数"
    assert outcome.stop_reason is not StopReason.DEGRADED


# ───────────────────────────────────────── ② 逐要点覆盖判定：``none`` 是可表达的输出


def test_the_judgement_answers_each_point_with_support_or_none() -> None:
    """判定是**逐个要点**给的：A1 有支撑（带材料序号），A2 没有。

    这是整票的核心：判定的基准是**问题侧**的要点，不是「材料里有几条核心片」。所以
    「A2 没有支撑」必须是模型能写出来的一个结论，而不是从材料数里推出来的副产品。
    """

    search = _Search({"查询1": [_row("a")], "查询2": [_row("b")]})

    outcome, _, _ = _run(_plan(), _judge(_supported(1, 1), _none(2)), search=search)

    assert [(item.point_id, item.status) for item in outcome.coverage] == [
        ("A1", "supported"),
        ("A2", "none"),
    ]
    # 有支撑的要点带着它引到的证据编号（与答案里的 E 编号同一套）。
    assert outcome.coverage[0].evidence_ids == ("E1",)
    assert outcome.gaps == ("A2",)


def test_a_verdict_that_skips_a_point_is_not_accepted() -> None:
    """少答一个要点 = 这份判定不能用（退回「不知道」，而不是被当成「有材料」）。

    漏答不能默认成 supported（那是乐观），也不能默认成 none（那是凭空造缺口）——
    两种猜测都在替模型回答它没回答的问题，所以整份判定作废，``coverage`` 保持 unknown。
    """

    search = _Search(default=[_row("a")])

    outcome, _, _ = _run(_plan(), _judge(_supported(1, 1)), search=search)

    assert [item.status for item in outcome.coverage] == ["unknown", "unknown"]
    assert outcome.gaps == ()
    assert outcome.stop_reason is StopReason.DEGRADED


def test_a_supported_verdict_must_point_at_materials() -> None:
    """``supported`` 却不给材料序号 = 自相矛盾（「有支撑」必须有指向）。"""

    outcome, _, _ = _run(
        _plan(),
        _judge(_supported(1), _supported(2, 1)),
        search=_Search(default=[_row("a")]),
    )

    assert outcome.stop_reason is StopReason.DEGRADED
    assert [item.status for item in outcome.coverage] == ["unknown", "unknown"]


def test_a_none_verdict_must_not_point_at_materials() -> None:
    """``none`` 却挂着材料序号 = 自相矛盾（要么给序号，要么说没有）。"""

    contradiction = {"point": 2, "status": "none", "materials": [1]}

    outcome, _, _ = _run(
        _plan(),
        _judge(_supported(1, 1), contradiction),
        search=_Search(default=[_row("a")]),
    )

    assert outcome.stop_reason is StopReason.DEGRADED
    assert outcome.gaps == ()


def test_a_verdict_may_not_point_beyond_the_material_pool() -> None:
    """要点指向不存在的材料 = 越界（模型只能点它真的看见的那几条）。"""

    outcome, _, _ = _run(
        _plan(), _judge(_supported(1, 9), _none(2)), search=_Search(default=[_row("a")])
    )

    assert outcome.stop_reason is StopReason.DEGRADED
    assert outcome.gaps == ()


def test_a_verdict_on_a_point_that_does_not_exist_is_rejected() -> None:
    """判定里出现计划中没有的要点序号 = 越界。"""

    outcome, _, _ = _run(
        _plan(),
        _judge(_supported(1, 1), _supported(2, 1), _none(9)),
        search=_Search(default=[_row("a")]),
    )

    assert outcome.stop_reason is StopReason.DEGRADED


def test_the_judgement_prompt_defines_support_as_readable_not_related() -> None:
    """定义闸：「支撑」= 材料里能**直接读出**该事实，不是「相关」。

    定义必须同时出现在提示词与模块文档里（ADR-0029 四种闸门之一）：只写在注释里，模型读不到
    「相关」不算；只写在提示词里，下一个人改提示词时不知道这是一条闸门。
    """

    assert "直接读出" in research._JUDGE_SYSTEM
    assert "直接读出" in (research.__doc__ or "")


# ───────────────────────────────────────── ③ 定向补证 + coverage 进响应


def test_the_followup_only_retargets_the_uncovered_points() -> None:
    """补证**只补没有支撑的那一块**：绑在已支撑要点上的查询一条都不执行。

    判定是唯一事实来源——模型把一条查询绑到 A1（已经判为 supported）上时，那条查询丢掉：
    重跑一个已经有材料的要点只会白花一次检索，而且会让「定向补证」这四个字在行为上不成立。
    """

    search = _Search({"查询1": [_row("a")], "查询2": [], "再查一次时限": [_row("z")]})

    outcome, _, probe = _run(
        _plan(),
        _judge(
            _supported(1, 1),
            _none(2),
            queries=[
                {"point": 1, "query": "再查一次时限"},
                {"point": 2, "query": "超期退款需要哪些材料"},
            ],
        ),
        search=search,
    )

    assert "再查一次时限" not in probe.calls, "已覆盖的要点不许再查一遍"
    assert probe.calls == ["查询1", "查询2", "超期退款需要哪些材料"]
    # 补证查询在计划里带 round=2，且只那一条。
    assert [(item.query, item.round) for item in outcome.queries] == [
        ("查询1", 1),
        ("查询2", 1),
        ("超期退款需要哪些材料", 2),
    ]


def test_a_gap_without_a_proposed_query_is_searched_with_the_point_text() -> None:
    """判定说「没有支撑」却没给出更好的说法 → 就拿要点本身当查询补一次。

    这是「定向补证」可以不花模型调用就总是成立的原因：要点就是一句为这个问题写的话。
    补一次没捞到新材料，缺口才真的落定（``no_new_evidence``，不是「判断失败」）。
    """

    outcome, _, probe = _run(
        _plan(), _judge(_supported(1, 1), _none(2)), search=_Search({"查询1": [_row("a")]})
    )

    assert probe.calls == ["查询1", "查询2", "所需材料"], "缺口要点用要点文本补了一次"
    assert outcome.followup_rounds == 1
    assert outcome.stop_reason is StopReason.NO_NEW_EVIDENCE
    assert outcome.gaps == ("A2",)


def test_a_point_that_the_followup_recovered_is_unknown_not_supported() -> None:
    """补证给缺口要点带回了新材料 → 它是 ``unknown``：材料到了，但支撑**没有再审一次**。

    这是本票最容易做错的一格。状态机固定五步、不回头复判，所以「补回来一条材料」不等于
    「这个要点被支撑了」——把它记成 supported 是乐观（正是 ADR-0029 反对的那种），记成
    none 则会让缺口闸把一份其实有材料的答案判死。
    """

    search = _Search({"查询1": [_row("a")], "超期退款需要哪些材料": [_row("c")]})

    outcome, _, _ = _run(
        _plan(),
        _judge(_none(2), _supported(1, 1), queries=[{"point": 2, "query": "超期退款需要哪些材料"}]),
        search=search,
    )

    assert [(item.point_id, item.status) for item in outcome.coverage] == [
        ("A1", "supported"),
        ("A2", "unknown"),
    ]
    assert outcome.gaps == (), "材料已经到了，这不再是「知道没有」"
    assert outcome.stop_reason is StopReason.MAX_ROUNDS


def test_the_coverage_block_reaches_the_response() -> None:
    """``coverage`` 是 Research 的一等输出（不做独立接口）：逐要点状态 + ``gaps``。

    装填走**生产代码**（``payload_of`` → ``ResearchData``），所以这条同时钉住「响应里的字
    段名与词表」这件事：``gaps`` 就是状态为 ``none`` 的那些要点编号，别处不再算第二遍。
    """

    from app.schemas.research import ResearchData, payload_of
    from app.services.research_limits import PLAN_NAME

    search = _Search({"查询1": [_row("a")], "查询2": []})

    outcome, _, _ = _run(_plan(), _judge(_supported(1, 1), _none(2)), search=search)

    data = ResearchData.model_validate(
        payload_of(
            query=QUERY, kb_ids=["kb-a"], outcome=outcome, plan_name=PLAN_NAME, trace_id="t" * 32
        )
    )

    assert [(item.point_id, item.status) for item in data.coverage.points] == [
        ("A1", "supported"),
        ("A2", "none"),
    ]
    assert data.coverage.points[0].evidence_ids == ["E1"]
    assert data.coverage.gaps == ["A2"]


# ───────────────────────────────────────── ④ 缺口闸：有 gap 就必须声明「材料不足」


def _gap_run(*blocks: dict[str, Any]) -> tuple[research.ResearchOutcome, _Scripted, _Search]:
    """跑一次**带真缺口**的 Research：A2 在材料里没有支撑，补证也没捞回来。"""

    search = _Search({"查询1": [_row("a")], "查询2": []})
    strong = _Scripted(_blocks(*blocks))
    outcome, _, probe = _run(
        _plan(), _judge(_supported(1, 1), _none(2)), search=search, strong=strong
    )
    return outcome, strong, probe


def test_the_answer_must_declare_a_gap_and_never_fill_it_from_its_own_knowledge() -> None:
    """有 gap 而答案全是 ``supported``（自己把缺口补上了）→ 整份答案作废。

    这是 ADR-0029 的缺口闸：上下文不足时模型仍有相当概率「答对」，反面就是相当概率
    「看起来答对了其实在编」。没有这道闸，覆盖判定等于白做——它会产出一份只有「有支撑」
    没有「材料不足」的答案，而调用方无从分辨。
    """

    outcome, _, _ = _gap_run(_block("3 个工作日内到账。", primary=(1,)))

    assert outcome.answer is None
    assert outcome.citations == ()
    assert outcome.answer_rejected_reason == REJECTED_GAP_NOT_DECLARED
    # 缺口本身照样报出来，材料、计划、判定一样都不少（这不是一次失败）。
    assert outcome.gaps == ("A2",)
    assert [row["chunk_id"] for row in outcome.materials] == ["chunk-a"]
    assert [item.status for item in outcome.coverage] == ["supported", "none"]


def test_an_answer_that_declares_the_gap_is_accepted() -> None:
    """同一份缺口，答案里给了 ``insufficient`` 块（渲染串里就有「材料不足」）→ 照常出答案。"""

    outcome, _, _ = _gap_run(
        _block("3 个工作日内到账。", primary=(1,)),
        _block("未找到所需材料的说明。", kind="insufficient"),
    )

    assert outcome.answer is not None
    assert "材料不足" in outcome.answer
    assert outcome.answer_rejected_reason is None


def test_a_run_without_gaps_does_not_declare_insufficiency() -> None:
    """没有 gap 时答案里不许平白出现「材料不足」——那句话是缺口的声明，不是口头禅。"""

    outcome, _, _ = _run(
        _plan(),
        _judge(_supported(1, 1), _supported(2, 1)),
        search=_Search({"查询1": [_row("a")], "查询2": [_row("b")]}),
    )

    assert outcome.gaps == ()
    assert outcome.answer is not None
    assert "材料不足" not in outcome.answer


def test_the_finalizer_is_told_which_points_are_uncovered() -> None:
    """缺口闸不是「模型自己想起来要声明」：生成提示词里点名了哪些要点没有支撑。

    提示词与校验是一对：提示词说「这几块材料不足，必须明说」，校验在它没照做时把答案作废。
    只有校验没有提示词，等于让人猜一条不写在纸上的规则。
    """

    outcome, strong, _ = _gap_run(
        _block("3 个工作日内到账。", primary=(1,)),
        _block("未找到所需材料的说明。", kind="insufficient"),
    )

    prompt = strong.prompts[0]
    assert "所需材料" in prompt, "缺口要点的正文必须点名"
    assert "材料不足" in prompt
    assert outcome.answer is not None


def test_a_run_without_any_material_declares_the_gap_by_not_answering() -> None:
    """一条材料都没有 → 不生成答案（问了也只能编），缺口照样逐要点交回。

    「没有答案」本身就是最彻底的声明：比在 ``answer`` 里追加一句凭空的话诚实——那句话会
    成为渲染串里唯一一处不由引用块支撑的内容。
    """

    strong = _Scripted(_blocks(_block("3 个工作日内到账。", primary=(1,))))

    outcome, _, probe = _run(_plan(), _judge(_none(1), _none(2)), search=_Search(), strong=strong)

    # 两个缺口要点各自拿要点正文补了一次（判定没有给查询），都没捞到材料。
    assert probe.calls == ["查询1", "查询2", "退款时限", "所需材料"]
    assert strong.calls == [], "没有材料就不该问生成"
    assert outcome.answer is None
    assert outcome.citations == ()
    assert outcome.gaps == ("A1", "A2")
    assert [item.status for item in outcome.coverage] == ["none", "none"]

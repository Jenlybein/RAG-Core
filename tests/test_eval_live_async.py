"""评测 live 路径的异步改造（#157 §①）：六处会话与两个命令行入口。

## 这张票在修什么

``scripts/eval_runner.py`` / ``scripts/eval_retrieval.py`` 的 live 路径仍拿**同步** ``Session``
去喂异步检索链路（#130 / #132 之后 ``rag_service.retrieve`` 是协程），第一条查询上就是
``TypeError: object ChunkedIteratorResult can't be used in 'await' expression``。要动的六处是
清库、建库、建文档、索引、标签解析、打分——它们都是「评测脚本与数据库之间的那一层」，
所以**接缝就是这六个函数本身**：调用方（``_run``）只认它们是协程。

第二件同等重要、但更容易漏的事是**事件循环策略**。psycopg 的异步连接在 Windows 的缺省
``ProactorEventLoop`` 上直接拒绝工作（``Psycopg cannot use the 'ProactorEventLoop'``），
所以两个入口都必须先钉 ``WindowsSelectorEventLoopPolicy``——其余运维脚本（``eval_c6_guard``
/ ``migrate_keyword_alias``）早就是这么做的，评测脚本是漏掉的那两个。

## 为什么还要一条「别再写回同步」的结构守卫

这六处搬家之后，最可能的回归不是写错 await，而是**下一个人顺手再开一个同步会话**：那不会
报错，只会在真库上炸成上面那个 ``TypeError``，而离线套件一直是绿的。所以用例直接扫源码：
评测脚本里不许出现 ``SessionLocal``、两个入口必须 ``asyncio.run`` + 选择器策略。
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from scripts import eval_retrieval, eval_runner
from tests.fake_db import AsyncRecordingSession

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EVAL_SCRIPTS = (
    REPO_ROOT / "scripts" / "eval_retrieval.py",
    REPO_ROOT / "scripts" / "eval_runner.py",
)


def _source(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


# ────────────────────────────────────────────────── 六处：形状（必须是协程）

#: 清库 / 建库 / 建文档 / 索引 / 标签解析 / 打分 —— 六处逐一对上一个可调用对象。
#: 「打分」对应 ``eval_runner._score_questions``（它把 ``db`` 传给 ``_run_question``，
#: 同一条会话一路走到 ``retrieve``）；``_cleanup`` 是清库的入口形态，一起钉住。
LIVE_STEPS = (
    ("清库", eval_retrieval, "_reset_postgres"),
    ("建库", eval_retrieval, "_ensure_knowledge_base"),
    ("建文档", eval_retrieval, "_ensure_document"),
    ("索引", eval_retrieval, "_index_documents"),
    ("标签解析", eval_retrieval, "_load_indexed_chunks"),
    ("清库入口", eval_retrieval, "_cleanup"),
    ("打分", eval_runner, "_score_questions"),
)


@pytest.mark.parametrize(("label", "module", "name"), LIVE_STEPS)
def test_every_live_step_is_a_coroutine(label: str, module: object, name: str) -> None:
    """六处都必须是 ``async def``：同步函数接不住 ``await retrieve(...)``。

    断言的是**函数形状**而不是某一行实现——搬家可以改名字以外的任何东西，但
    「这一层是协程」是调用方 ``_run`` 与 ``asyncio.run`` 之间的契约。
    """

    function = getattr(module, name, None)
    assert function is not None, f"{label}：{module.__name__}.{name} 不存在（被改名了？）"
    assert inspect.iscoroutinefunction(function), (
        f"{label}：{module.__name__}.{name} 还是同步函数，接异步检索链路会炸 ChunkedIteratorResult"
    )


# ────────────────────────────────────────────────── 六处：行为（经异步会话替身）


@pytest.mark.asyncio
async def test_reset_postgres_clears_the_three_tables_in_one_transaction() -> None:
    """清库：三条 DELETE（切片 → 文档 → 库，外键顺序）+ 恰好一次提交。

    断言 SQL 的**顺序**：先删切片再删文档最后删库。反过来会撞外键（真库上直接失败），
    而替身不会替我们拦住它，所以顺序在用例里钉死。
    """

    db = AsyncRecordingSession()

    counts = await eval_retrieval._reset_postgres(db)

    assert set(counts) == {"chunks", "documents", "knowledge_bases"}
    verbs = [statement.lstrip().split(maxsplit=1)[0].upper() for statement in db.statements]
    assert verbs == ["DELETE", "DELETE", "DELETE"]
    tables = [statement.split()[2] for statement in db.statements]
    assert tables == ["chunks", "documents", "knowledge_bases"]
    assert db.commits == 1, "清库必须在一个事务里收口（三条 DELETE 各提交一次会留下半清的库）"


@pytest.mark.asyncio
async def test_ensure_knowledge_base_inserts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """建库：库里没有这个 kb 时插入一行，并且**只**插入一次。"""

    db = AsyncRecordingSession()

    await eval_retrieval._ensure_knowledge_base(
        db,
        kb_id="kb-eval",
        name="评测语料库·客户支持",
        description="",
        split_strategy="structure",
    )

    assert len(db.added) == 1
    created = db.added[0]
    assert created.id == "kb-eval"
    assert created.split_strategy == "structure"
    assert created.active_index_version == "v1", (
        "建库必须写生效索引版本（ADR-0025 决策四）：不写的话新库每个请求都被守卫拦下，"
        "全量重建路径（非 --reuse）根本跑不起来"
    )
    # 白名单按策略推导（不在脚本里另写一份）：structure → 只收 markdown。
    assert created.allowed_formats == ["markdown"]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_ensure_knowledge_base_refuses_a_strategy_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已存在但策略不同 → 当场拒绝，不就地改（改配置补不回旧的切法）。"""

    from app.models.knowledge_base import KnowledgeBase

    existing = KnowledgeBase(
        id="kb-eval",
        tenant_id="tenant_demo",
        name="评测语料库·客户支持",
        description="",
        split_strategy="size",
        allowed_formats=[],
        settings={},
    )
    db = AsyncRecordingSession(rows={KnowledgeBase: [existing]})

    with pytest.raises(SystemExit, match="策略"):
        await eval_retrieval._ensure_knowledge_base(
            db,
            kb_id="kb-eval",
            name="评测语料库·客户支持",
            description="",
            split_strategy="structure",
        )

    assert db.added == []
    assert db.commits == 0, "拒绝时一个字节都不该写"


@pytest.mark.asyncio
async def test_ensure_document_requeues_instead_of_duplicating() -> None:
    """建文档：已有行就原地重置成「已排队、未开工」，不再 add 一条（主键会撞）。"""

    from app.models.document import Document

    existing = Document(
        id="eval-doc-support_errors",
        tenant_id="tenant_demo",
        kb_id="kb-eval",
        title="旧标题",
        content="旧正文",
        source_type="markdown",
    )
    db = AsyncRecordingSession(rows={Document: [existing]})
    doc = eval_retrieval.CorpusDoc(
        stem="support_errors",
        path=REPO_ROOT / "eval" / "corpus" / "support_errors.md",
        kb_id="kb-eval",
        title="售后故障与错误码速查",
        text="正文",
    )

    await eval_retrieval._ensure_document(db, doc)

    assert db.added == []
    assert existing.title == "售后故障与错误码速查"
    assert existing.content == "正文"
    assert existing.index_claimed_at is None
    assert db.commits == 1


@pytest.mark.asyncio
async def test_ensure_document_inserts_a_claimable_row() -> None:
    """建文档：没有行时插入一行，且状态是 Worker 可抢占的那一个。"""

    db = AsyncRecordingSession()
    doc = eval_retrieval.CorpusDoc(
        stem="support_errors",
        path=REPO_ROOT / "eval" / "corpus" / "support_errors.md",
        kb_id="kb-eval",
        title="售后故障与错误码速查",
        text="正文",
    )

    await eval_retrieval._ensure_document(db, doc)

    assert len(db.added) == 1
    row = db.added[0]
    assert row.index_claimed_at is None, "占着租约的行会让 Worker 静默跳过，重跑就不再索引"
    from app.utils.status import DocumentStatus

    assert row.status == DocumentStatus.PROCESSING


# ────────────────────────────────────────────────── 暖机：冷启动不许记到首题头上


@pytest.mark.asyncio
async def test_warm_up_pays_the_cold_start_outside_the_scored_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """暖机走**生产同一条** hybrid 检索，且用的题号不在题库里。

    为什么必须暖机（#157 在真库上量出来的）：进程内第一次检索会顺手建可观测性的
    Langfuse 客户端，那一步 import ``openai``（几百个 pydantic 模型）把事件循环堵住
    ~2.4s；同一题里关键词一路的预算只有 2.0s，于是**只有第一题**降级成
    ``keyword search failed``。基线 127 题里 0 题降级，所以这不是检索退化，是冷启动
    记到了首题头上。暖机把这一次付在计分之外。
    """

    seen: list[tuple[str, str, tuple[str, ...]]] = []

    async def fake_run_question(db: object, question: object, **kwargs: object) -> object:
        seen.append(
            (
                question.id,  # type: ignore[attr-defined]
                kwargs["mode"],  # type: ignore[arg-type]
                tuple(kwargs["kb_ids"]),  # type: ignore[arg-type]
            )
        )
        return eval_retrieval.QuestionOutcome(
            id=question.id,  # type: ignore[attr-defined]
            query=question.query,  # type: ignore[attr-defined]
            mode=kwargs["mode"],  # type: ignore[arg-type]
            duration_ms=2412.0,
        )

    monkeypatch.setattr(eval_retrieval, "_run_question", fake_run_question)

    report = await eval_retrieval._warm_up(object(), kb_ids=["kb-eval"], top_k=5)

    assert seen == [
        (
            eval_retrieval.WARMUP_QUESTION_ID,
            eval_retrieval.MODE_HYBRID,
            ("kb-eval",),
        )
    ], "暖机必须走 hybrid（两路都热起来），且用专门的题号"
    assert report["model_calls"] == 1, "暖机是一次真实查询嵌入，要计入模型调用预算"
    assert report["duration_ms"] == 2412.0
    assert report["question_id"] == eval_retrieval.WARMUP_QUESTION_ID

    questions, _k = eval_retrieval._load_questions()
    assert all(question.id != eval_retrieval.WARMUP_QUESTION_ID for question in questions), (
        "暖机题号绝不能出现在题库里：它是被判分的题的邻居，混进去就多算一题"
    )


@pytest.mark.asyncio
async def test_warm_up_reports_a_degraded_cold_start_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """暖机自己降级了也要如实写进读数——那是「冷启动有多贵」的证据，不是要藏掉的丑事。"""

    async def fake_run_question(db: object, question: object, **kwargs: object) -> object:
        outcome = eval_retrieval.QuestionOutcome(
            id=question.id,  # type: ignore[attr-defined]
            query=question.query,  # type: ignore[attr-defined]
            mode="hybrid",
        )
        outcome.degraded = True
        outcome.degraded_reason = "keyword search failed"
        return outcome

    monkeypatch.setattr(eval_retrieval, "_run_question", fake_run_question)

    report = await eval_retrieval._warm_up(object(), kb_ids=["kb-eval"], top_k=5)

    assert report["degraded"] is True
    assert report["degraded_reason"] == "keyword search failed"


# ────────────────────────────────────────────────── 预算：开跑前先算账


def test_the_model_call_budget_is_printed_before_the_run() -> None:
    """137 题一轮的预算：暖机 1 次 + 每题 1 次查询嵌入 = 138 次，上限 450。

    这条用例钉住的是**算式**而不是某个数字：总调用 = 暖机 + 题数。预算的意义在于
    「开跑前能算出来」，所以它必须是一个纯函数、能离线断言。
    """

    within = eval_runner.model_call_budget(questions=137)

    assert within["warmup"] == 1
    assert within["questions"] == 137
    assert within["total"] == 138
    assert within["budget"] == eval_runner.MODEL_CALL_BUDGET == 450
    assert within["within_budget"] is True


def test_an_over_budget_plan_is_refused() -> None:
    """超预算的方案必须判 False（调用点据此 SystemExit 停下报告，不硬跑）。"""

    over = eval_runner.model_call_budget(questions=450)

    assert over["total"] == 451
    assert over["within_budget"] is False


# ────────────────────────────────────────────────── 关键词那一路：必须真的命中过


def test_the_literal_question_only_matches_by_literal_text() -> None:
    """选来验关键词的那道题，得真的是「只能靠字面命中」。

    题面里带编号 ``W0001``，而它的标注章节标题里**没有**这个编号——编号只出现在正文，
    且同文档里紧挨着 ``W0002``～``W0010`` 九节、措辞 90% 相同。所以：向量这一路分不开，
    字面这一路分得开。换题而没换成这种题，下面的命中断言就退化成「随便一道题有返回」。

    选 ``q05`` 还有一条实测依据：C6 守分抽样里它正是被「关键词超时降级」记成 0 分的那题
    （``eval/reports/c6/c6-acceptance.md``），于是这条断言与那次事故盯着同一个探针。
    """

    questions, _k = eval_retrieval._load_questions()
    literal = next(
        question
        for question in questions
        if question.id == eval_retrieval.KEYWORD_LITERAL_QUESTION_ID
    )

    assert literal.type == "exact_code"
    assert "W0001" in literal.query
    assert all("W0001" not in heading for _stem, heading in literal.labels)


def test_a_bm25_hit_on_the_literal_question_counts_as_the_keyword_leg_working() -> None:
    """`bm25` 那一组只走关键词（不向量化、不查向量库），它命中相关片就是这一路在工作。"""

    group = {
        "per_question": [
            {
                "id": eval_retrieval.KEYWORD_LITERAL_QUESTION_ID,
                "status": "ok",
                "empty_reason": None,
                "returned_total": 2,
                "returned": [
                    {"chunk_id": "chunk-relevant", "is_relevant": True},
                    {"chunk_id": "chunk-twin", "is_relevant": False},
                ],
            }
        ]
    }

    report = eval_retrieval.keyword_hit_report(
        group=group, labels={eval_retrieval.KEYWORD_LITERAL_QUESTION_ID: ["chunk-relevant"]}
    )

    assert report["passed"] is True
    assert report["hit_count"] == 1
    assert report["hit_rank"] == 1
    assert report["returned_count"] == 2


def test_an_empty_bm25_leg_fails_the_assertion() -> None:
    """索引空跑的样子：一组零返回。这正是「关键词一路等于没跑」要被抓出来的形态。"""

    group = {
        "per_question": [
            {
                "id": eval_retrieval.KEYWORD_LITERAL_QUESTION_ID,
                "status": "empty",
                "empty_reason": "no_chunks_matched",
                "returned_total": 0,
                "returned": [],
            }
        ]
    }

    report = eval_retrieval.keyword_hit_report(
        group=group, labels={eval_retrieval.KEYWORD_LITERAL_QUESTION_ID: ["chunk-relevant"]}
    )

    assert report["passed"] is False
    assert report["returned_count"] == 0
    assert report["empty_reason"] == "no_chunks_matched"


def test_returning_only_irrelevant_chunks_fails_the_assertion() -> None:
    """有返回、但一条都不相关——也不算「命中过」。别拿总数冒充命中。"""

    group = {
        "per_question": [
            {
                "id": eval_retrieval.KEYWORD_LITERAL_QUESTION_ID,
                "status": "ok",
                "empty_reason": None,
                "returned_total": 1,
                "returned": [{"chunk_id": "chunk-twin", "is_relevant": False}],
            }
        ]
    }

    report = eval_retrieval.keyword_hit_report(
        group=group, labels={eval_retrieval.KEYWORD_LITERAL_QUESTION_ID: ["chunk-relevant"]}
    )

    assert report["passed"] is False
    assert report["returned_count"] == 1
    assert report["hit_count"] == 0


@pytest.mark.asyncio
async def test_keyword_probe_runs_bm25_only_and_reports_the_hit(monkeypatch) -> None:
    """门禁产物里的关键词断言：只跑 ``mode=bm25``，0 次模型调用，读数来自同一份解析结果。"""

    seen: dict[str, object] = {}

    async def fake_run_question(db: object, question: object, **kwargs: object) -> object:
        seen["question_id"] = question.id  # type: ignore[attr-defined]
        seen["mode"] = kwargs["mode"]
        outcome = eval_retrieval.QuestionOutcome(
            id=question.id,  # type: ignore[attr-defined]
            query=question.query,  # type: ignore[attr-defined]
            mode=str(kwargs["mode"]),
        )
        outcome.returned = [{"chunk_id": "chunk-literal"}]
        return outcome

    monkeypatch.setattr(eval_retrieval, "_run_question", fake_run_question)
    questions, _k = eval_retrieval._load_questions()
    literal = next(
        question
        for question in questions
        if question.id == eval_retrieval.KEYWORD_LITERAL_QUESTION_ID
    )

    report = await eval_retrieval.keyword_probe(
        object(),
        question=literal,
        labels={literal.id: ["chunk-literal"]},
        kb_ids=["kb-eval"],
        top_k=5,
    )

    assert seen == {"question_id": literal.id, "mode": eval_retrieval.MODE_BM25}, (
        "必须只走关键词单路：hybrid 的分数会被向量那一路撑住，看不出关键词空跑"
    )
    assert report["passed"] is True
    assert report["hit_rank"] == 1
    assert report["model_calls"] == 0, "bm25 不向量化，这一次探针不该计入模型调用预算"


@pytest.mark.asyncio
async def test_a_dead_keyword_leg_fails_the_probe_and_surfaces_the_degraded_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关键词一路降级 / 空返回时，探针必须判 False 并把原因带出来（门禁据此判红）。"""

    async def fake_run_question(db: object, question: object, **kwargs: object) -> object:
        outcome = eval_retrieval.QuestionOutcome(
            id=question.id,  # type: ignore[attr-defined]
            query=question.query,  # type: ignore[attr-defined]
            mode="bm25",
        )
        outcome.degraded = True
        outcome.degraded_reason = "keyword search failed"
        outcome.empty_reason = "no_chunks_matched"
        return outcome

    monkeypatch.setattr(eval_retrieval, "_run_question", fake_run_question)

    report = await eval_retrieval.keyword_probe(
        object(),
        question=eval_retrieval.Question(
            id="q05", type="exact_code", query="W0001", note="", labels=()
        ),
        labels={"q05": ["chunk-literal"]},
        kb_ids=["kb-eval"],
        top_k=5,
    )

    assert report["passed"] is False
    assert report["hit_count"] == 0
    assert report["empty_reason"] == "no_chunks_matched"
    assert report["degraded"] is True
    assert report["degraded_reason"] == "keyword search failed"


# ──────────────────────────────── 版本化写入：索引完要把别名切到刚写出的版本


class _ScalarRows:
    """``execute(...).scalars().all()`` 的最小替身（只回一组版本名）。"""

    def __init__(self, values: list[str]) -> None:
        self._values = list(values)

    def scalars(self) -> _ScalarRows:
        return self

    def all(self) -> list[str]:
        return list(self._values)


class _VersionDb:
    """只回答 ``select(Chunk.index_version)`` 的假会话。"""

    def __init__(self, versions: list[str]) -> None:
        self._versions = versions
        self.statements: list[str] = []

    async def execute(self, statement: object) -> _ScalarRows:
        self.statements.append(str(statement))
        return _ScalarRows(self._versions)


class _VersionProvider:
    """关键词 Provider 的替身：只记「建了哪些版本、切了哪些版本」。"""

    def __init__(self) -> None:
        self.ensured: list[str] = []
        self.activated: list[str] = []

    async def ensure_version_index(self, version: str) -> str:
        self.ensured.append(version)
        return f"physical__{version}"

    async def activate_version(self, version: str) -> None:
        self.activated.append(version)


def _patch_keyword_provider(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    monkeypatch.setattr(
        "app.providers.keyword_search.get_keyword_search_provider", lambda: provider
    )


@pytest.mark.asyncio
async def test_activate_keyword_versions_switches_the_alias_per_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """索引完必须把别名切过去（#157 §②）：``add_chunks`` 只写物理索引，不动别名。

    漏了这一步的症状是「关键词一路搜不到」——不报错。版本号从 ``chunks`` 列读，
    多个版本时按序各切一次、最后一个生效（库里是事实，内存猜会与物理索引名错位）。
    """

    provider = _VersionProvider()
    _patch_keyword_provider(monkeypatch, provider)

    versions = await eval_retrieval._activate_keyword_versions(_VersionDb(["v2", "v1", "v1"]))

    assert provider.ensured == ["v1", "v2"], "去重后按版本名排序；先建索引再切别名"
    assert provider.activated == ["v1", "v2"], "多个版本各切一次，最后切的是最大版本"
    assert versions["activated"] == [
        {"version": "v1", "physical": "physical__v1"},
        {"version": "v2", "physical": "physical__v2"},
    ]


@pytest.mark.asyncio
async def test_activate_keyword_versions_is_a_noop_when_no_chunks_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库里一个切片都没有时：不建空索引、不切别名（全新环境不该被这一步弄出脏索引）。"""

    provider = _VersionProvider()
    _patch_keyword_provider(monkeypatch, provider)

    result = await eval_retrieval._activate_keyword_versions(_VersionDb([]))

    assert provider.ensured == []
    assert provider.activated == []
    assert result["activated"] == []


# ────────────────────────────────────────────────── 清索引：版本化形状也要清得干净


class _EsResponse:
    """``httpx`` 响应的最小替身（只要 ``status_code`` / ``json`` / ``text``）。"""

    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        """与 ``httpx.Response`` 同口径：>=400 才抛（用例只构 200，延迟到需要时再模拟）。"""

        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


def test_reset_deletes_every_physical_version_behind_the_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清索引必须删**具体物理索引**，而不是那个别名（别名发 DELETE 会被 ES 拒）。

    版本化之后 ``rag_chunks_eval`` 是别名，真正要删的是 ``rag_chunks_eval__v1`` 之类；
    只删别名指向的那一张会漏掉旧版本，下一轮构建再写一版，两张就同时在集群里。
    """

    seen: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> _EsResponse:
        seen["get_url"] = url
        seen["params"] = kwargs.get("params")
        return _EsResponse(
            200, [{"index": "rag_chunks_eval__v1"}, {"index": "rag_chunks_eval__v2"}]
        )

    def fake_delete(url: str, **kwargs: object) -> _EsResponse:
        seen["delete_url"] = url
        return _EsResponse(200, {"acknowledged": True})

    monkeypatch.setattr(eval_retrieval.httpx, "get", fake_get)
    monkeypatch.setattr(eval_retrieval.httpx, "delete", fake_delete)

    state = eval_retrieval._reset_elasticsearch("rag_chunks_eval")

    assert str(seen["get_url"]).endswith("/_cat/indices/rag_chunks_eval,rag_chunks_eval__*"), (
        "要靠 _cat 把别名解析成具体物理索引，且版本前缀一起覆盖"
    )
    assert str(seen["delete_url"]).endswith("/rag_chunks_eval__v1,rag_chunks_eval__v2")
    assert state.startswith("deleted(")


def test_reset_reports_absent_when_nothing_is_there(monkeypatch: pytest.MonkeyPatch) -> None:
    """什么都不存在 → ``absent``（全新环境上不该报错，也不该发一条 DELETE）。"""

    deleted: list[str] = []
    monkeypatch.setattr(eval_retrieval.httpx, "get", lambda url, **kwargs: _EsResponse(200, []))
    monkeypatch.setattr(
        eval_retrieval.httpx,
        "delete",
        lambda url, **kwargs: deleted.append(url) or _EsResponse(200, {}),
    )

    assert eval_retrieval._reset_elasticsearch("rag_chunks_eval") == "absent"
    assert deleted == []


def test_reset_refuses_a_name_that_is_not_the_eval_index() -> None:
    """防手滑：名字里没有 eval 就一个请求都不发（这条守卫比任何注释都管用）。"""

    with pytest.raises(SystemExit, match="eval"):
        eval_retrieval._reset_elasticsearch("rag_chunks")


# ──────────────────────────────── --reuse 守卫：旧索引会静默空跑，必须当场停下


def _patch_es_counts(
    monkeypatch: pytest.MonkeyPatch, *, total: int, versioned: int
) -> list[dict[str, object]]:
    """把 ``GET .../_count`` 与 ``POST .../_count``（exists 过滤）换成固定读数。"""

    posts: list[dict[str, object]] = []

    def fake_get(url: str, **kwargs: object) -> _EsResponse:
        return _EsResponse(200, {"count": total})

    def fake_post(url: str, **kwargs: object) -> _EsResponse:
        posts.append(dict(kwargs.get("json") or {}))
        return _EsResponse(200, {"count": versioned})

    monkeypatch.setattr(eval_retrieval.httpx, "get", fake_get)
    monkeypatch.setattr(eval_retrieval.httpx, "post", fake_post)
    return posts


def test_reuse_guard_refuses_an_unversioned_keyword_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """索引里有 469 条、却一条都没 ``index_version`` → 当场停下，别等花了 128 次调用才发现。

    这正是本票之前的现场：查询按 ``(库, 版本)`` 过滤，而旧索引的文档没有这一列，于是
    关键词这一路**静默空跑**（ADR-0025 决策四）——不报错，只是 `q05` 这种字面题归零。
    ``--reuse`` 的守卫要在这里响，指向迁移脚本，而不是让整轮跑完再归因。
    """

    posts = _patch_es_counts(monkeypatch, total=469, versioned=0)

    with pytest.raises(SystemExit, match="index_version"):
        eval_retrieval._assert_keyword_index_is_versioned("rag_chunks_eval")

    assert posts and posts[0]["query"]["exists"]["field"] == "index_version"  # type: ignore[index]


def test_reuse_guard_accepts_a_versioned_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """全部文档都带版本 → 放行（这就是本票迁移之后的状态：469/469）。"""

    _patch_es_counts(monkeypatch, total=469, versioned=469)

    assert eval_retrieval._assert_keyword_index_is_versioned("rag_chunks_eval") == 469


def test_reuse_guard_allows_an_empty_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """索引还不存在 / 一条都没有：那是「全新环境还没建」，不是迁移漏做，放行且不发第二条请求。"""

    posts = _patch_es_counts(monkeypatch, total=0, versioned=0)

    assert eval_retrieval._assert_keyword_index_is_versioned("rag_chunks_eval") == 0
    assert posts == []


def test_both_reuse_paths_call_the_keyword_version_guard() -> None:
    """守卫必须接在**两个** ``--reuse`` 入口上：只写函数不接线，等于没做。

    接线位置也要在「花查询嵌入之前」——接线挪到跑完再查，就退化成事后归因，
    本票要消的正是那笔钱。所以这里扫源码：两个脚本的 reuse 分支都要出现调用。
    """

    retrieval = _source(REPO_ROOT / "scripts" / "eval_retrieval.py")
    runner = _source(REPO_ROOT / "scripts" / "eval_runner.py")

    assert retrieval.count("_assert_keyword_index_is_versioned") >= 2, (
        "eval_retrieval 里要有定义 + reuse 分支的调用"
    )
    assert "_assert_keyword_index_is_versioned(live.elasticsearch_index)" in runner, (
        "eval_runner 的 --reuse 路径也要接同一个守卫"
    )
    # 两个入口的 reuse 分支都还保留 mapping 守卫（索引代际不同也不能复用）。
    assert "_assert_query_analyzer(index_name)" in retrieval
    assert "_assert_query_analyzer(live.elasticsearch_index)" in runner


# ────────────────────────────────────────────────── 结构守卫：别写回同步


@pytest.mark.parametrize("path", EVAL_SCRIPTS, ids=lambda path: path.name)
def test_eval_scripts_never_open_a_synchronous_session(path: pathlib.Path) -> None:
    """评测脚本里不许再出现同步会话：那正是本票要根治的那条路。

    不查「有没有 await」（写法太多），只查「有没有那个会炸的对象」——``SessionLocal``
    与 ``app.db.session`` 的一次出现就足够把整条 live 路径打回同步栈。
    """

    source = _source(path)
    assert not re.search(r"(?<![A-Za-z_])SessionLocal\s*\(", source), f"{path.name} 又开了同步会话"
    assert "from app.db.session import" not in source, f"{path.name} 又把同步会话引进来了"


@pytest.mark.parametrize("path", EVAL_SCRIPTS, ids=lambda path: path.name)
def test_eval_scripts_pin_the_selector_event_loop(path: pathlib.Path) -> None:
    """入口必须钉 ``WindowsSelectorEventLoopPolicy``。

    psycopg 的异步连接在 Windows 缺省的 Proactor 循环上直接拒绝工作，而这只在**真库**
    上暴露：离线套件全绿，第一次连库就炸。其余运维脚本早就是这套，评测脚本不能例外。
    """

    source = _source(path)
    assert "asyncio.run(" in source, f"{path.name} 的入口没有走 asyncio.run"
    assert "WindowsSelectorEventLoopPolicy" in source, (
        f"{path.name} 的入口没钉选择器事件循环（psycopg 异步在 Proactor 上直接拒绝）"
    )

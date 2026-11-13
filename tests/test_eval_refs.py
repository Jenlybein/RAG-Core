"""引用语料 / 定向题集 / 跑批脚本的离线守卫（票 A3 / #145）。

三组断言，每组守的是一件**不会自己报错**的事：

1. **语料与题集自洽**：每条「详见《X》第 N 节」都能解析到确实存在的节，而且原文里真有
   那句话。这类错误不会让任何用例变红——改个措辞、拆成两句，图上少一条边，读数却照旧出。
   所以清单里钉住了 evidence，这里拿抽取器（``scripts.eval_refs`` 的解析函数）逐条对账。
2. **冻结资产没被动过**：``eval/corpus`` 与 ``eval/baseline`` 是本票明确不动的东西
   （#138 验收第 1 条：往基线语料里加文档等于基线当场失效）。哈希清单把「没动」变成会红
   的断言；多出一个文件也算失败，因为这正是最危险的那一类变更。
3. **跑批脚本的诚实边界**：默认零模型调用、``--source live`` 不显式加参数就拒绝执行、
   未落地的能力印 ``capability-not-implemented`` 而不是伪造一组相同的数字。

全程纯函数 + 临时目录：不连库、不发网络、不碰 ``eval/refs-runs/``。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from scripts import eval_refs

REPO_ROOT = Path(__file__).resolve().parents[1]
REFS_DIR = REPO_ROOT / "eval" / "corpus-refs"
MANIFEST_PATH = REFS_DIR / "manifest.json"
QUESTIONS_PATH = REPO_ROOT / "eval" / "refs-questions.json"

#: 三种必须覆盖的显式引用形态（#138 §A 抽取规则：只认文档里写明的表达）。
REQUIRED_FORMS = ("explicit_cross_doc", "same_doc", "markdown_link")

#: 四类机制，各 2 道（票里的硬要求）。
REQUIRED_MECHANISMS = (
    "cross_section",
    "cross_document",
    "parallel_points",
    "dependent_hop",
)

ALLOWED_ROLES = frozenset({"core", "support", "pointer", "hop1", "hop2"})


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _questions() -> dict:
    return json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))


def _section_text(document: str, number: str) -> str:
    meta = _manifest()["documents"][document]
    sections = eval_refs.split_sections((REFS_DIR / meta["file"]).read_text(encoding="utf-8"))
    return sections[number].text


# ─────────────────────────────────────────────── 一、语料自身的结构


def test_corpus_refs_is_three_documents_in_one_kb() -> None:
    """3 篇、一个库、文件都在，且每篇的节号与清单逐条对得上。

    节号对不上等于引用边指向了不存在的节——而「指向不存在的节」在抽取器眼里只是
    「歧义，不建边」，不报错、只是少一条边。
    """

    layout = _manifest()
    assert len(layout["kbs"]) == 1
    kb = layout["kbs"][0]
    assert kb["kb_id"] == "kb-refs"
    assert len(kb["documents"]) == 3

    for filename in kb["documents"]:
        path = REFS_DIR / filename
        assert path.is_file(), f"清单声明的语料不存在：{filename}"
        assert path.read_text(encoding="utf-8").startswith("# "), f"{filename} 缺一级标题"

    for stem, meta in layout["documents"].items():
        assert meta["file"] in kb["documents"], f"{stem} 不在 kbs[0].documents 里"
        text = (REFS_DIR / meta["file"]).read_text(encoding="utf-8")
        sections = eval_refs.split_sections(text)
        assert list(sections) == list(meta["sections"]), (
            f"{stem}: 标题里的节号 {list(sections)} 与清单 {list(meta['sections'])} 不一致"
        )
        for number, title in meta["sections"].items():
            assert sections[number].title == title, f"{stem} 第 {number} 节标题与清单不一致"


def test_every_reference_edge_resolves_to_an_existing_section() -> None:
    """9 条边的两端都必须落在语料里，而且 evidence 就是原文里那句话。

    三件事一起断言，缺一件都会让「引用边」变成一厢情愿：源节存在、目标节存在、
    那句路标真的写在源节里（片段能在源节原文里找到）。
    """

    layout = _manifest()
    edges = layout["reference_inventory"]
    assert edges, "引用语料的价值全在这些边上，一条都没有等于白建"

    for edge in edges:
        source = edge["from"]
        target = edge["to"]
        assert source["document"] in layout["documents"], source
        assert target["document"] in layout["documents"], target
        assert source["section"] in layout["documents"][source["document"]]["sections"], source
        assert target["section"] in layout["documents"][target["document"]]["sections"], target
        assert edge["form"] in REQUIRED_FORMS, edge["form"]

        result = eval_refs.resolve_citation(
            document=source["document"],
            section=source["section"],
            snippet=edge["evidence"],
            corpus_dir=REFS_DIR,
            manifest=layout,
        )
        assert result.ok, (
            f"{source['document']}:{source['section']} 里找不到这条证据（{result.reason}）："
            f"{edge['evidence']}"
        )


def test_each_form_uses_its_own_wording() -> None:
    """三种形态各按各自的写法出现——否则「抽取三种显式表达」只验证了一种。

    - ``explicit_cross_doc``：证据里有《目标文档标题》与目标节号；
    - ``same_doc``：证据里有「本文第 N 节」，且目标就是同一个文档；
    - ``markdown_link``：证据里有指向目标文件的 Markdown 链接。
    """

    layout = _manifest()
    targets = {edge["form"] for edge in layout["reference_inventory"]}
    assert set(REQUIRED_FORMS) <= targets, f"缺形态：{set(REQUIRED_FORMS) - targets}"

    for edge in layout["reference_inventory"]:
        source, target, evidence = edge["from"], edge["to"], edge["evidence"]
        if edge["form"] == "explicit_cross_doc":
            title = layout["documents"][target["document"]]["title"]
            assert f"《{title}》" in evidence, evidence
            assert f"第 {target['section']} 节" in evidence, evidence
            assert source["document"] != target["document"], "跨文档形态却指向同一篇"
        elif edge["form"] == "same_doc":
            assert f"本文第 {target['section']} 节" in evidence, evidence
            assert source["document"] == target["document"], "同文档形态却指向另一篇"
        else:
            filename = layout["documents"][target["document"]]["file"]
            assert f"](./{filename})" in evidence, evidence


def test_reference_graph_is_connected_enough_for_multihop() -> None:
    """至少一条「三跳链」：退款 → 促销 → 积分。

    依赖型多跳题需要一个「第二轮才出现」的目标，而它只能靠第一轮材料里的路标到达。
    链短到一跳时，多跳题就退化成跨文档题。
    """

    edges = _manifest()["reference_inventory"]
    forward = {(edge["from"]["document"], edge["to"]["document"]) for edge in edges}
    assert ("refund_policy", "promo_rules") in forward
    assert ("promo_rules", "points_policy") in forward


# ─────────────────────────────────────────────── 二、题集


def test_refs_questions_are_eight_across_four_mechanisms() -> None:
    payload = _questions()
    questions = payload["questions"]
    assert len(questions) == 8
    counts = Counter(question["mechanism"] for question in questions)
    assert dict(counts) == {mechanism: 2 for mechanism in REQUIRED_MECHANISMS}
    assert set(payload["mechanisms"]) == set(REQUIRED_MECHANISMS)
    assert payload["kb_id"] == _manifest()["kbs"][0]["kb_id"]


def test_every_question_says_what_mechanism_it_verifies() -> None:
    """每道题都必须能回答「为什么留着它」（#138 验收第 5 条）。

    ``mechanism`` 只是标签，``mechanism_note`` 才是那个理由：它要说明这一类为什么成立、
    哪一侧能力关掉之后这道题会退化。缺了它，日后没人知道删掉一道题会失去什么。
    """

    for question in _questions()["questions"]:
        assert question["id"].startswith("r")
        assert len(question["query"]) >= 10, question["id"]
        assert len(question["mechanism_note"]) >= 40, f"{question['id']} 缺少机制说明"
        assert len(question["answer_points"]) >= 3, f"{question['id']} 参考答案要点太少"
        assert len(question["expected"]) >= 2, f"{question['id']} 只标注了一节"


def test_question_expectations_point_at_real_sections() -> None:
    """期望命中的文档与节必须存在，role 必须来自白名单。"""

    layout = _manifest()
    ids = [question["id"] for question in _questions()["questions"]]
    assert len(ids) == len(set(ids)), "题号重复"

    for question in _questions()["questions"]:
        for item in question["expected"]:
            meta = layout["documents"].get(item["document"])
            assert meta is not None, f"{question['id']}: 未知文档 {item['document']}"
            assert item["section"] in meta["sections"], (
                f"{question['id']}: {item['document']} 没有第 {item['section']} 节"
            )
            assert item["role"] in ALLOWED_ROLES, f"{question['id']}: 未知 role {item['role']}"
            assert item.get("why"), f"{question['id']}: 缺少「为什么期望这一节」"


def test_opening_edges_are_in_the_corpus_inventory() -> None:
    """题里说的「应该被走通的那条边」必须真的存在——否则题集在描述一张不存在的图。"""

    edges = {
        f"{edge['from']['document']}:{edge['from']['section']}"
        f"->{edge['to']['document']}:{edge['to']['section']}"
        for edge in _manifest()["reference_inventory"]
    }
    for question in _questions()["questions"]:
        for edge in question.get("opening_edges", []):
            key = f"{edge['from']}->{edge['to']}"
            assert key in edges, f"{question['id']} 引用了不存在的边 {key}"

    # 跨文档与多跳两类题必须都写明「该走哪条边」，否则 A2 / C6 没法按边判走没走通。
    for question in _questions()["questions"]:
        if question["mechanism"] in ("cross_document", "dependent_hop"):
            assert question.get("opening_edges"), f"{question['id']} 缺 opening_edges"


# ─────────────────────────────────────────────── 三、冻结资产守护


def test_eval_corpus_and_baseline_are_untouched() -> None:
    """本票最大的破坏面：动一下基线语料或基线读数，整条门禁的尺子就变了。

    ``changed`` 是「内容被改了」、``extra`` 是「往里加了文件」——后者最危险：加一篇文档
    会让 137 道题的 baseline 当场失效，而且**不会让任何功能用例变红**。
    """

    result = eval_refs.check_frozen(root=REPO_ROOT)
    assert result.ok, (
        f"eval/corpus 与 eval/baseline 被改动过：缺失 {result.missing}、"
        f"内容变更 {result.changed}、多出 {result.extra}。"
        "确需变更时先重跑 baseline 并归档，再 `uv run python scripts/eval_refs.py freeze --write`。"
    )
    assert result.checked > 0


def test_frozen_check_catches_added_changed_and_missing(tmp_path: Path) -> None:
    """守护本身也要被测：否则它可能一直返回「通过」而没人发现。"""

    corpus = tmp_path / "eval" / "corpus"
    baseline = tmp_path / "eval" / "baseline"
    corpus.mkdir(parents=True)
    baseline.mkdir(parents=True)
    (corpus / "a.md").write_text("# a\n正文\n", encoding="utf-8")
    (baseline / "report.md").write_text("# 基线\n", encoding="utf-8")

    manifest = {
        "_readme": ["test"],
        "files": eval_refs.collect_frozen(tmp_path),
    }
    manifest_path = tmp_path / "eval" / "frozen-assets.sha256.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert eval_refs.check_frozen(root=tmp_path).ok

    (corpus / "a.md").write_text("# a\n正文改了\n", encoding="utf-8")
    (corpus / "b.md").write_text("# b\n新加的一篇\n", encoding="utf-8")
    (baseline / "report.md").unlink()

    result = eval_refs.check_frozen(root=tmp_path)
    assert result.changed == ["eval/corpus/a.md"]
    assert result.extra == ["eval/corpus/b.md"]
    assert result.missing == ["eval/baseline/report.md"]
    assert not result.ok


def test_frozen_hash_ignores_line_ending_style(tmp_path: Path) -> None:
    """换行策略不该被误报成内容变更（Windows 上 ``core.autocrlf`` 会改写工作区字节）。"""

    path = tmp_path / "x.md"
    path.write_bytes(b"# t\r\n\r\nline\r\n")
    lf = eval_refs.normalized_sha256(path)
    path.write_bytes(b"# t\n\nline\n")
    assert eval_refs.normalized_sha256(path) == lf


# ─────────────────────────────────────────────── 四、跑批脚本的诚实边界


def _config(**overrides) -> eval_refs.RunConfig:
    base = {
        "source": eval_refs.SOURCE_DRY_RUN,
        "graph": "off",
        "research": "off",
        "allow_model_calls": False,
        "kb_ids": ("kb-refs",),
        "top_k": 5,
        "fixtures": None,
        "tenant_id": None,
    }
    base.update(overrides)
    return eval_refs.RunConfig(**base)


def test_dry_run_produces_all_four_metric_blocks_without_spending() -> None:
    """默认参数：零模型调用，四类指标都在，且「没有样本」不伪装成 0。"""

    payload = eval_refs.run_batch(_config())
    metrics = payload["metrics"]

    # 一、停止原因分布：research 关着，不能编出任何一种七种原因。
    assert metrics["stop_reason_distribution"]["status"] == "disabled"
    assert metrics["stop_reason_distribution"]["counts"] == {}
    # 今天链路的等价终止形态如实存在（空跑自检 = 检索器不返回东西）。
    assert metrics["retrieval_termination"] == {"empty:dry_run_no_retriever": 8}

    # 二、引用回溯率：没有命中样本 → None，不是 0.0。
    assert metrics["citation_traceability"]["hits"] == 0
    assert metrics["citation_traceability"]["rate"] is None

    # 三、延迟：没有真实耗时样本 → 全 None。
    assert metrics["latency_ms"] == {"p50": None, "p95": None, "max": None, "total": None}

    # 四、模型调用次数：0，且预计也是 0。
    assert metrics["model_calls"]["total"] == 0
    assert payload["predicted_calls"]["total"] == 0

    assert len(payload["per_question"]) == 8
    assert payload["premise"]["comparable_with_original_project"] is False


def test_both_switches_report_capability_not_implemented() -> None:
    """「开 / 关」两侧今天都不存在：报告必须写明，而不是给出两组相同的数字。"""

    payload = eval_refs.run_batch(_config(graph="on", research="on"))
    statuses = payload["capabilities"]
    assert statuses["graph"] == eval_refs.CAPABILITY_NOT_IMPLEMENTED
    assert statuses["research"] == eval_refs.CAPABILITY_NOT_IMPLEMENTED
    # research=on 时停止原因分布不伪造：状态写 capability-not-implemented、计数为空。
    distribution = payload["metrics"]["stop_reason_distribution"]
    assert distribution["status"] == eval_refs.CAPABILITY_NOT_IMPLEMENTED
    assert distribution["counts"] == {}


def test_report_prints_capability_not_implemented_and_not_comparable() -> None:
    config = _config(graph="on", research="on")
    report = eval_refs.render_report(eval_refs.run_batch(config), config)
    assert eval_refs.CAPABILITY_NOT_IMPLEMENTED in report
    assert "与原项目的数字不可比" in report
    assert "对照有效性" in report
    for heading in ("停止原因分布", "引用回溯率", "延迟", "模型调用次数"):
        assert heading in report


def test_live_source_refuses_without_explicit_allowance(capsys) -> None:
    """要花钱的一侧必须显式开门：``--source live`` 不带 ``--allow-model-calls`` 直接拒绝。"""

    exit_code = eval_refs.main(["run", "--source", "live"])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--allow-model-calls" in captured.err


def test_predicted_calls_are_printed_before_running(capsys, tmp_path: Path) -> None:
    """开跑前先打印预计调用次数（默认参数下应为 0），跑完落盘两份产物。"""

    exit_code = eval_refs.main(["run", "--out", str(tmp_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "预计模型调用次数" in captured.out
    assert "合计      : 0 次" in captured.out

    runs = list(tmp_path.iterdir())
    assert len(runs) == 1, runs
    assert (runs[0] / "report.md").is_file()
    assert (runs[0] / "results.json").is_file()
    payload = json.loads((runs[0] / "results.json").read_text(encoding="utf-8"))
    assert payload["source"] == eval_refs.SOURCE_DRY_RUN
    assert payload["metrics"]["model_calls"]["total"] == 0


def test_prediction_scales_with_the_question_count() -> None:
    """live 每题一次查询嵌入；dry-run 一次都不调。"""

    dry = eval_refs.predict_calls(
        source=eval_refs.SOURCE_DRY_RUN,
        question_count=8,
        research_enabled=False,
        research_status="disabled",
    )
    live = eval_refs.predict_calls(
        source=eval_refs.SOURCE_LIVE,
        question_count=8,
        research_enabled=False,
        research_status="disabled",
    )
    assert dry["total"] == 0
    assert live["embedding"] == 8 and live["total"] == 8
    # Research 未落地：即便打开也不预扣 3 次生成调用。
    research_on = eval_refs.predict_calls(
        source=eval_refs.SOURCE_DRY_RUN,
        question_count=8,
        research_enabled=True,
        research_status=eval_refs.CAPABILITY_NOT_IMPLEMENTED,
    )
    assert research_on["research_llm"] == 0


def test_fixture_replay_computes_a_real_traceability_rate(tmp_path: Path) -> None:
    """回放一份固定结果：命中取自语料原文 → 回溯率必须是 1.0，且节号能被解析出来。

    这条用例同时证明「回溯率是算出来的」而不是写死的：同一段代码在 dry-run 下给 None，
    在真命中下给 1.0。
    """

    text = _section_text("refund_policy", "3")
    snippet = text.split("\n", 1)[1][:80].strip()
    fixture = {
        "per_question": [
            {
                "id": "r01",
                "hits": [
                    {
                        "chunk_id": "c-1",
                        "document": "refund_policy",
                        "heading_path": "会员订单退款政策 > 3. 退款到账与凭证",
                        "content": snippet,
                        "score": 0.5,
                    }
                ],
                "latency_ms": 12.5,
                "model_calls": 0,
            }
        ]
    }
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")

    payload = eval_refs.run_batch(_config(source=eval_refs.SOURCE_FIXTURE, fixtures=path))
    trace = payload["metrics"]["citation_traceability"]
    assert trace["hits"] == 1
    assert trace["traceable"] == 1
    assert trace["rate"] == pytest.approx(1.0)
    assert payload["coverage"]["r01"]["found_keys"] == ["refund_policy:3"]
    # 有真实耗时样本时延迟不再是 n/a。
    assert payload["metrics"]["latency_ms"]["p50"] == pytest.approx(12.5)


def test_untraceable_hits_are_listed_with_a_reason(tmp_path: Path) -> None:
    """被改写的正文不会被算成可回溯，而且失败原因要能区分是哪一类错。"""

    fixture = {
        "per_question": [
            {
                "id": "r03",
                "hits": [
                    {
                        "chunk_id": "c-2",
                        "document": "promo_rules",
                        "heading_path": "促销活动与优惠券使用规则 > 3. 优惠券与退款的关系",
                        "content": "这句话不在语料里，是模型编的。",
                    },
                    {
                        "chunk_id": "c-3",
                        "document": "不存在的文档",
                        "heading_path": "x > 1. y",
                        "content": "随便什么",
                    },
                ],
            }
        ]
    }
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")

    payload = eval_refs.run_batch(_config(source=eval_refs.SOURCE_FIXTURE, fixtures=path))
    trace = payload["metrics"]["citation_traceability"]
    assert trace["hits"] == 2 and trace["traceable"] == 0
    assert trace["rate"] == pytest.approx(0.0)
    reasons = [item["reason"] for item in trace["untraceable"]]
    assert reasons == ["snippet-not-in-section", "unknown-document"]


# ─────────────────────────────────────────────── 五、引用回溯的机械复算


def test_verify_citation_resolves_every_inventory_edge() -> None:
    """清单里每条边的证据都能被 CLI 走通的那条路径还原——「可机械复算」的底线。"""

    for edge in _manifest()["reference_inventory"]:
        result = eval_refs.resolve_citation(
            document=edge["from"]["document"],
            section=edge["from"]["section"],
            snippet=edge["evidence"],
        )
        assert result.ok, (edge["evidence"], result.reason)


def test_verify_citation_accepts_heading_path_and_document_title() -> None:
    """节路径与文档标题都能当入参：报告里印的是路径，清单里写的是节号与 stem。"""

    result = eval_refs.resolve_citation(
        document="积分规则",
        section="积分规则 > 2. 积分抵扣与使用上限 > 2.2 抵扣上限",
        snippet="银卡及以上时，200 元的固定上限提高到 500 元",
    )
    assert result.ok
    assert result.document == "points_policy"
    assert result.section == "2.2"
    assert result.section_title == "抵扣上限"


def test_section_boundary_is_respected(capsys) -> None:
    """片段在别的节里就是不可回溯：空跑与复算都不能靠「全文里找得到」蒙过去。"""

    result = eval_refs.resolve_citation(
        document="refund_policy", section="2", snippet="银行卡一般 3 个工作日"
    )
    assert result.ok is False
    assert result.reason == "snippet-not-in-section"

    assert (
        eval_refs.main(
            [
                "verify-citation",
                "--document",
                "refund_policy",
                "--section",
                "2",
                "--snippet",
                "银行卡一般 3 个工作日",
            ]
        )
        == 1
    )
    assert "不可还原" in capsys.readouterr().err


def test_verify_citation_cli_exit_code_zero_on_success(capsys) -> None:
    code = eval_refs.main(
        [
            "verify-citation",
            "--document",
            "refund_policy",
            "--section",
            "3",
            "--snippet",
            "银行卡一般 3 个工作日",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert '"ok": true' in captured.out
    assert "可还原" in captured.out


def test_default_subcommand_is_run(capsys, tmp_path: Path) -> None:
    """``eval_refs.py --top-k 3`` 必须等价于 ``eval_refs.py run --top-k 3``（一条命令跑）。"""

    parser = eval_refs._build_parser()
    args = parser.parse_args(["run", "--top-k", "3"])
    assert args.command == "run" and args.top_k == 3
    assert args.source == eval_refs.SOURCE_DRY_RUN
    assert args.graph == "off" and args.research == "off"
    assert args.allow_model_calls is False

    # 不带子命令直接给参数：main 会补上 run，而不是报 invalid choice。
    assert eval_refs.main(["--top-k", "3", "--out", str(tmp_path)]) == 0
    runs = list(tmp_path.iterdir())
    payload = json.loads((runs[0] / "results.json").read_text(encoding="utf-8"))
    assert payload["questions"]["k"] == 5  # 题集自己的 k，不随 --top-k 漂移
    assert len(payload["per_question"]) == 8

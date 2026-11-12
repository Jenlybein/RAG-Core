"""统一评测运行器的子命令与归档接缝：临时目录 + 假数据，不连库、不发网络。

运行器的 ``run`` 子命令要真 PostgreSQL + 真 Elasticsearch + 真嵌入，跑一次几分钟，
不适合放进默认套件；能在离线钉住的是**不那么起眼但更容易错**的那部分：

* 四个子命令的退出码与它们说出来的话（校验失败必须是红的，不能是「提示一下」）；
* 归档目录的三份产物（人读 / 机读 / 环境快照）能被下一个人读回来再渲染一次；
* 基线的挑选顺序（随仓库的固定基线优先，其次最近一次**全量**归档——冒烟不能当基线）；
* HTTP 冒烟的字段清单与挑题顺序（它不进闸门，但缺口字段必须当场报出来）。

``scripts/eval_retrieval.py`` 里那些「必须连真库」的部分在 ``run`` 内被调用，
由真跑覆盖；这里不替它编造数字。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import eval_runner
from scripts.eval_gate import QuestionScore, build_results_payload

REPO = Path(__file__).resolve().parents[1]


# ───────────────────────────────────────────────────────────────── 小工具


def _score(
    qid: str, *, type_: str = "colloquial", cp: Any = 1.0, cr: Any = 1.0, reason: str | None = None
) -> QuestionScore:
    return QuestionScore(
        id=qid, type=type_, context_precision=cp, context_recall=cr, unscored_reason=reason
    )


def _payload(
    *,
    run_id: str,
    scores: list[QuestionScore],
    full: bool = True,
    status: str = "ok",
    corpus_version: str = "v1",
    durations: list[float] | None = None,
) -> dict[str, Any]:
    return build_results_payload(
        run_id=run_id,
        run_dir=f"eval/runs/{run_id}",
        generated_at="2026-11-11T00:00:00+00:00",
        status=status,
        experiment={"name": "t", "mode": "hybrid", "top_k": 5},
        corpus={"version": corpus_version, "documents": 1, "chunks": 2, "questions": len(scores)},
        environment={
            "retrieval": {
                "mode": "hybrid",
                "top_k": 5,
                "rerank_enabled": False,
                "query_rewrite_enabled": False,
                "synonym_expansion_enabled": False,
            },
            "embedding": {"model": "text-embedding-v4", "dimensions": 1024},
            "keyword": {"provider": "elasticsearch", "index": "rag_chunks_eval"},
        },
        scores=scores,
        gate={
            "verdict": "no_baseline",
            "passed": None,
            "reason": None,
            "overall": [],
            "by_type": [],
            "regressions": [],
            "summary": "",
        },
        per_question=[
            {
                "id": score.id,
                "type": score.type,
                "query": f"问句 {score.id}",
                "returned_count": 3,
                "reference_count": 2,
                "context_precision": score.context_precision,
                "context_recall": score.context_recall,
                "unscored_reason": score.unscored_reason,
                # 逐题耗时是检索那一步本来就记的（#114 的 P50 / P95 只读它）
                "duration_ms": (durations[index] if durations else None),
            }
            for index, score in enumerate(scores)
        ],
        scope_full=full,
        excluded_types={"absent": 1},
    )


# ───────────────────────────────────────────────── 重排开关（默认关 = 历史口径）


def _restore_settings(monkeypatch) -> None:
    """把本轮会被钉死的旋钮先登记好，用例结束后 monkeypatch 自动还原。"""

    from app.core.config import settings

    for name in (
        "RERANK_ENABLED",
        "QUERY_REWRITE_ENABLED",
        "SYNONYM_EXPANSION_ENABLED",
        "TOP_K",
        "ELASTICSEARCH_INDEX",
    ):
        monkeypatch.setattr(settings, name, getattr(settings, name))


def test_the_rerank_switch_is_off_by_default_and_on_only_when_asked(monkeypatch) -> None:
    """``--rerank`` 缺省关：两次冻结基线都是关重排跑的，历史口径要逐字节不变。"""

    _restore_settings(monkeypatch)
    from app.core.config import settings

    monkeypatch.setattr(settings, "RERANK_ENABLED", True)  # 机器上 .env 的偶然值

    assert eval_runner._apply_eval_switches(SimpleNamespace(), top_k=5) is False
    assert settings.RERANK_ENABLED is False  # .env 里的偶然值被钉死，不是「碰巧没开」
    assert settings.TOP_K == 5

    assert eval_runner._apply_eval_switches(SimpleNamespace(rerank=True), top_k=3) is True
    assert settings.RERANK_ENABLED is True
    assert settings.TOP_K == 3


def test_the_environment_snapshot_records_the_rerank_switch_as_it_ran(monkeypatch) -> None:
    """开着重排跑的归档，环境快照必须写明「重排：开」——否则读数无法核对。"""

    _restore_settings(monkeypatch)
    from app.core.config import settings

    monkeypatch.setattr(settings, "RERANK_ENABLED", True)

    snapshot = eval_runner.environment_snapshot(
        run_id="20261112T000000Z",
        generated_at="2026-11-12T00:00:00+00:00",
        experiment={"mode": "hybrid", "top_k": 5},
        corpus={"version": "v1"},
        dataset_version="v1",
        env_file=None,
        llm_endpoint_source=eval_runner.ENDPOINT_SOURCE_ENV,
        llm_model="deepseek-flash",
    )

    assert snapshot["retrieval"]["rerank_enabled"] is True


def test_the_environment_snapshot_records_the_rerank_budget_as_it_ran(monkeypatch) -> None:
    """重排预算三项（超时 / 候选封顶 / 单条截断）必须在快照里，且是**本轮实际生效**的值。

    为什么不是「可有可无的补充字段」：15 秒超时下重排会有一题超时降级，60 秒下同一份
    题集全部排过。两条读数的质量差异会因此归因到「谁没超时」而不是「重排模型好坏」。
    记的是 ``settings`` 上钉完之后的真值——这一轮进程里抬高的上限也要如实体现，
    否则「clone 后照这份快照能复现」就是句空话。
    """

    _restore_settings(monkeypatch)
    from app.core.config import settings

    for name in ("RERANK_TIMEOUT_SECONDS", "RERANK_MAX_CANDIDATES", "RERANK_CHUNK_MAX_CHARS"):
        monkeypatch.setattr(settings, name, getattr(settings, name))

    # 模拟「这一轮评测进程里把上限抬到 60 秒」：环境变量 + .env 都不动，只钉进程内的值。
    monkeypatch.setattr(settings, "RERANK_TIMEOUT_SECONDS", 60.0)
    monkeypatch.setattr(settings, "RERANK_MAX_CANDIDATES", 30)
    monkeypatch.setattr(settings, "RERANK_CHUNK_MAX_CHARS", 512)

    snapshot = eval_runner.environment_snapshot(
        run_id="20261112T000000Z",
        generated_at="2026-11-12T00:00:00+00:00",
        experiment={"mode": "hybrid", "top_k": 5},
        corpus={"version": "v1"},
        dataset_version="v1",
        env_file=None,
        llm_endpoint_source=eval_runner.ENDPOINT_SOURCE_ENV,
        llm_model="deepseek-flash",
    )

    retrieval = snapshot["retrieval"]
    assert retrieval["rerank_timeout_seconds"] == 60.0
    assert retrieval["rerank_max_candidates"] == 30
    assert retrieval["rerank_chunk_max_chars"] == 512


def test_the_run_parser_defaults_to_rerank_off_and_the_repo_baseline() -> None:
    parser = eval_runner.build_parser()

    args = parser.parse_args(["run"])
    assert args.rerank is False
    assert args.baseline_dir is None  # None → 解析成随仓库的 eval/baseline/

    args = parser.parse_args(["run", "--rerank", "--baseline-dir", "eval/baseline-rerank"])
    assert args.rerank is True
    assert args.baseline_dir == "eval/baseline-rerank"


# ───────────────────────────────────── 逐题明细记录重排事实（#115 的验收读数）


def test_the_archived_per_question_detail_keeps_the_rerank_facts_verbatim(monkeypatch) -> None:
    """冒烟与基线的验收都读逐题明细：重排发生与否、候选数、返回数、耗时必须在里面。

    #114 之前逐题明细只有 ``degraded``；「这一轮开了重排」在归档里无处可查，只能
    回头去看当次响应——而响应跑完就没了。这里钉住的是**原样**照抄
    ``metadata.retrieval.rerank``：不在明细里另算一份，否则两处口径迟早分叉。
    """

    import asyncio

    from scripts import eval_retrieval as ev

    rerank = {"applied": True, "candidates": 30, "returned": 5, "duration_ms": 812.34}

    async def fake_run_question(db, question, *, mode, top_k, kb_ids, rerank_factory=None):
        return ev.QuestionOutcome(
            id=question.id,
            query=question.query,
            mode=mode,
            returned=[{"chunk_id": f"{question.id}-c1"}],
            duration_ms=1234.5,
            # 第二道题模拟「响应里没有重排事实」（例如检索本身失败）：如实留 None，
            # 不编一个 applied=False 出来——「没记录」和「确实没开」是两件事。
            rerank=rerank if question.id == "q1" else None,
        )

    monkeypatch.setattr(ev, "_run_question", fake_run_question)

    questions = [
        SimpleNamespace(id="q1", type="colloquial", query="怎么装"),
        SimpleNamespace(id="q2", type="colloquial", query="坏了"),
    ]
    _scores, detail, failures = asyncio.run(
        eval_runner._score_questions(
            None,
            questions=questions,
            labels={"q1": ["q1-c1"]},
            mode="hybrid",
            top_k=1,
            kb_ids=["kb-eval"],
        )
    )

    assert failures == []
    assert detail[0]["rerank"] == rerank  # 原样照抄，不是折算过的摘要
    assert detail[0]["rerank"]["applied"] is True
    assert detail[0]["rerank"]["duration_ms"] == 812.34
    assert detail[1]["rerank"] is None
    # 既有键一个都没动（纯增量）：旧归档的读者按老键取值仍然成立
    assert set(detail[0]) >= {
        "id",
        "type",
        "query",
        "returned_count",
        "reference_count",
        "context_precision",
        "context_recall",
        "unscored_reason",
        "empty_reason",
        "degraded",
        "degraded_reason",
        "duration_ms",
        "returned_ids",
        "reference_ids",
    }


def test_the_retrieval_matrix_detail_carries_the_same_rerank_facts() -> None:
    """另一条逐题明细（``eval_retrieval`` 自己的矩阵/诊断）也读同一份事实。

    两条明细的可信度取决于它们说的是同一件事：都从 ``QuestionOutcome.rerank`` 取，
    不各算一份。
    """

    from scripts import eval_retrieval as ev

    rerank = {"applied": True, "candidates": 30, "returned": 5, "duration_ms": 812.34}
    outcome = ev.QuestionOutcome(
        id="q1", query="怎么装", mode="hybrid", duration_ms=1234.5, rerank=rerank
    )

    item = ev._score_question(
        outcome,
        question_type="colloquial",
        relevant=["c1"],
        trap=[],
        chunk_by_id={},
    )

    assert item["rerank"] == rerank
    assert item["duration_ms"] == 1234.5  # 检索耗时与重排耗时是两个数，别混


# ────────────────────── 评测与生产走同一条模型接入链（#115 的根因修复）


def _restore_role_resolver(monkeypatch) -> None:
    """``set_role_resolver`` 写的是**进程级全局**：用例结束后必须复位，否则污染邻居。

    先把当前值登记给 monkeypatch，它会在拆解时连同用例里的写入一起还原——与
    ``tests/test_llm_provider.py`` 手工 ``set_role_resolver(None)`` 是同一件事，
    只是不用指望每个用例的作者都记得写最后那一行。
    """

    from app.providers import llm as llm_module

    monkeypatch.setattr(llm_module, "_role_resolver", llm_module._role_resolver)


def _snapshot_with(**access: Any) -> dict[str, Any]:
    """只有「模型接入来自哪一条」在变的快照；其余条件固定，读数才可比。"""

    return eval_runner.environment_snapshot(
        run_id="20261112T000000Z",
        generated_at="2026-11-12T00:00:00+00:00",
        experiment={"mode": "hybrid", "top_k": 5},
        corpus={"version": "v1"},
        dataset_version="v1",
        env_file=None,
        **access,
    )


def test_no_role_pointer_keeps_the_eval_on_env_and_records_env(monkeypatch) -> None:
    """解析器说「没有指向」时：行为不变（回落 ``.env``），快照**如实**写 ``env``。

    钉住的是「不许把回落说成 model_configs」：两者用的 key 可能属于**不同账号**，
    混起来之后，下一次「生产好、评测全挂」又只能靠猜。
    """

    from app.core.config import settings
    from app.providers import llm as llm_module

    _restore_role_resolver(monkeypatch)
    monkeypatch.setattr(settings, "LLM_MODEL", "env-model-x")

    access = eval_runner.bind_llm_role_resolver(lambda role: None)

    assert access == {"llm_endpoint_source": "env", "llm_model": "env-model-x"}
    # 行为不变：解析器确实被注入了，只是它给出的答案是「没有指向」→ Provider 走 .env
    assert llm_module._resolved_endpoint("fast") is None

    snapshot = _snapshot_with(**access)
    assert snapshot["llm_endpoint_source"] == "env"
    assert snapshot["llm_model"] == "env-model-x"


def test_the_injected_resolver_points_the_eval_at_the_model_configs_row(monkeypatch) -> None:
    """注入之后：快照写 ``model_configs`` + 那一行的模型名，且 Provider 层真的会用它。

    只写标签不注入，是最坏的一种「修好了」：归档说走库里的配置，钱还在花 ``.env`` 的
    那把 key。所以这里同时断言「Provider 层拿到的就是这一条」。
    """

    from app.providers import llm as llm_module
    from app.providers.llm import RoleEndpoint

    _restore_role_resolver(monkeypatch)
    endpoint = RoleEndpoint(
        provider="openai",
        base_url="https://model-configs.invalid/v1",
        model="fast-model-y",
        api_key="test-key-not-a-real-secret",
        config_id="cfg-test",
    )

    access = eval_runner.bind_llm_role_resolver(lambda role: endpoint if role == "fast" else None)

    assert access == {"llm_endpoint_source": "model_configs", "llm_model": "fast-model-y"}
    assert llm_module._resolved_endpoint("fast") is endpoint  # 真的接上了，不是只记了个标签

    snapshot = _snapshot_with(**access)
    assert snapshot["llm_endpoint_source"] == "model_configs"
    assert snapshot["llm_model"] == "fast-model-y"


class _StubRerank:
    """重排端点的替身：只在用例里出现，不连网、不花钱。"""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        rows: list[dict[str, Any]] | None = None,
        available: bool = True,
    ) -> None:
        self.available = available
        self._error = error
        self._rows = rows
        self.calls: list[dict[str, Any]] = []

    async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]:
        self.calls.append({"query": query, "chunks": chunks, "top_n": top_n})
        if self._error is not None:
            raise self._error
        return list(self._rows if self._rows is not None else chunks[:top_n])


def test_a_failed_rerank_probe_stops_the_run_before_any_question_runs() -> None:
    """探测失败 = 当场中止，而不是把整轮跑成降级；原因只留异常**类型**。

    ``str(exc)`` 常夹带提示词（题面 + 候选内容）。把它抄进终端与归档，等于把评测数据
    写进仓库日志——所以这里连同「原文不该出现」一起钉住。
    """

    leaky = RuntimeError("401 Unauthorized：user=怎么装电脑 候选=第一条假文档")
    provider = _StubRerank(error=leaky)

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(eval_runner.probe_rerank(provider))

    message = str(excinfo.value)
    assert "RuntimeError" in message  # 类型名在：够区分 401 / 超时 / 结构不合法
    assert "401 Unauthorized" not in message  # 原文不在
    assert "怎么装电脑" not in message


def test_the_rerank_probe_refuses_a_noop_provider() -> None:
    """空实现（重排开关其实没打开）也必须在这里停：跑下去只会得到一轮「没排过」。"""

    class _Noop:
        available = False

        async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]:
            raise AssertionError("空实现不该被调用")

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(eval_runner.probe_rerank(_Noop()))

    assert "Noop" in str(excinfo.value)


def test_the_rerank_probe_sends_two_fake_documents_and_reports_its_own_timing() -> None:
    """探测只用两条**假**文档：够证明端点答得上来，又不烧掉一道真题的钱。"""

    provider = _StubRerank()

    elapsed_ms = asyncio.run(eval_runner.probe_rerank(provider))

    assert elapsed_ms >= 0
    (call,) = provider.calls
    assert call["top_n"] == 2
    assert [chunk["chunk_id"] for chunk in call["chunks"]] == ["probe-0", "probe-1"]
    assert all("假文档" in chunk["content"] for chunk in call["chunks"])


def test_the_offline_subcommands_never_bind_the_model_resolver(monkeypatch) -> None:
    """``validate-dataset`` / ``validate-config`` 必须保持离线：不许连库、不许注入解析器。

    注入一旦挪进 ``main`` 或模块导入，离线子命令就会去读 ``model_configs``；CI 没有库，
    于是「离线校验」变成「没有库就红」——那正是这两个子命令存在的理由。
    """

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("离线子命令不许连库 / 注入角色解析器")

    monkeypatch.setattr(eval_runner, "bind_llm_role_resolver", _forbidden)
    monkeypatch.setattr(eval_runner, "probe_rerank", _forbidden)

    assert eval_runner.main(["validate-dataset"]) == 0
    assert eval_runner.main(["validate-config"]) == 0


# ───────────────────────────────────────────────────────── 离线可导入（硬约束）


def test_the_runner_imports_without_an_env_file_or_the_app() -> None:
    """「无 .env、无网络」下必须能 import 本模块，否则 pytest 一收集就崩。

    做法是另起一个进程、只看它 import 之后 ``sys.modules`` 里有没有 ``app.*``：
    模块顶层一旦顺手 import 了 ``app.core.config``（那会读环境、定型 settings）
    或 ``scripts.eval_retrieval``（那会在 import 时解析 argv），这条就会红。
    """

    code = (
        "import sys;"
        "import scripts.eval_runner as runner;"
        "print(runner.RUN_SCHEMA);"
        "print(any(name == 'app' or name.startswith('app.') for name in sys.modules));"
        "print('scripts.eval_retrieval' in sys.modules)"
    )
    completed = subprocess.run(  # noqa: S603 - 固定命令，无 shell
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["eval-run/1", "False", "False"]


# ───────────────────────────────────────────────────────── 子命令：validation


def test_validate_dataset_subcommand_passes_on_the_repo_dataset(capsys) -> None:
    code = eval_runner.main(["validate-dataset"])
    output = capsys.readouterr().out

    assert code == 0
    assert "137 道" in output
    assert "进平均 127 道" in output
    assert "absent 6" in output and "false_premise 4" in output


def test_validate_dataset_subcommand_is_red_on_a_broken_label(tmp_path, capsys) -> None:
    """标注写了语料里不存在的节：必须退出码 1，并指出是哪一道题。"""

    broken = {
        "k": 5,
        "questions": [
            {
                "id": "q404",
                "type": "exact_code",
                "query": "不存在的节",
                "relevant": [{"document": "support_errors", "heading_path": "这一节不存在"}],
            }
        ],
    }
    questions_path = tmp_path / "questions.json"
    questions_path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")

    code = eval_runner.main(["validate-dataset", "--questions", str(questions_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert "q404" in captured.out
    assert "不通过" in captured.err


def test_validate_dataset_subcommand_missing_file_is_a_hard_failure(tmp_path, capsys) -> None:
    code = eval_runner.main(["validate-dataset", "--questions", str(tmp_path / "nope.json")])
    assert code == 2
    assert "题库不存在" in capsys.readouterr().err


def test_validate_config_subcommand_accepts_the_repo_config(capsys) -> None:
    code = eval_runner.main(["validate-config"])
    output = capsys.readouterr().out

    assert code == 0
    assert "mode='hybrid'" in output
    assert "整体 ≥ 基线 -1.0pp" in output


def test_validate_config_subcommand_is_red_on_an_unsupported_mode(tmp_path, capsys) -> None:
    config_path = tmp_path / "experiment.toml"
    config_path.write_text(
        "[experiment]\n"
        'name = "t"\n'
        'mode = "fancy"\n'
        "top_k = 5\n"
        'split_strategy = "structure"\n'
        "[gate]\n"
        "overall_pp = 1.0\n"
        "per_type_pp = 3.0\n"
        'baseline = ""\n'
        "[http]\n"
        "samples = 3\n",
        encoding="utf-8",
    )

    code = eval_runner.main(["validate-config", "--config", str(config_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert "mode" in captured.out


def test_validate_config_is_red_when_the_configured_baseline_cannot_be_read(
    tmp_path, capsys
) -> None:
    """配了基线却读不到，会让闸门悄悄退化成「无基线」——必须在配置校验就红。"""

    config_path = tmp_path / "experiment.toml"
    config_path.write_text(
        "[experiment]\n"
        'name = "t"\n'
        'mode = "hybrid"\n'
        "top_k = 5\n"
        'split_strategy = "structure"\n'
        "[gate]\n"
        "overall_pp = 1.0\n"
        "per_type_pp = 3.0\n"
        'baseline = "eval/runs/does-not-exist"\n'
        "[http]\n"
        "samples = 3\n",
        encoding="utf-8",
    )

    code = eval_runner.main(["validate-config", "--config", str(config_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert "基线" in captured.out


# ───────────────────────────────────────────────────── 归档：写 / 读 / 重出报告


def test_write_run_dir_writes_the_three_artifacts_and_reads_back(tmp_path) -> None:
    run_dir = tmp_path / "20261111T000000Z"
    payload = _payload(run_id="20261111T000000Z", scores=[_score("q1")])

    written = eval_runner.write_run_dir(run_dir, payload)

    assert {path.name for path in written} == {"results.json", "environment.json", "report.md"}
    assert (run_dir / "report.md").read_text(encoding="utf-8").startswith("# 评测门禁报告")

    # 归档是自足的：读回来能直接再渲染一次，不需要重跑检索
    reloaded = eval_runner.read_run(run_dir)
    assert reloaded["run_id"] == "20261111T000000Z"
    assert reloaded["metrics"]["overall"]["context_precision"] == pytest.approx(1.0)
    assert reloaded["environment"]["embedding"]["model"] == "text-embedding-v4"
    assert eval_runner.render_report(reloaded).startswith("# 评测门禁报告")


def test_write_run_dir_never_writes_nan(tmp_path) -> None:
    """``NaN`` 不是合法 JSON：写出去之后，下一次「与基线比」会读不回来。"""

    run_dir = tmp_path / "run"
    payload = _payload(run_id="run", scores=[_score("q1", cp=float("nan"), cr=None, reason="x")])

    eval_runner.write_run_dir(run_dir, payload)

    text = (run_dir / "results.json").read_text(encoding="utf-8")
    assert "NaN" not in text
    assert json.loads(text)["metrics"]["overall"]["context_precision"] is None


def test_report_subcommand_rerenders_and_can_print_one_number(tmp_path, capsys) -> None:
    run_dir = tmp_path / "20261111T000000Z"
    payload = _payload(run_id="20261111T000000Z", scores=[_score("q1", cp=0.6123, cr=0.75)])
    eval_runner.write_run_dir(run_dir, payload)

    assert eval_runner.main(["report", "--run", str(run_dir)]) == 0
    output = capsys.readouterr().out
    assert "报告" in output
    assert "复现命令" in (run_dir / "report.md").read_text(encoding="utf-8")

    code = eval_runner.main(
        ["report", "--run", str(run_dir), "--select", "metrics.overall.context_precision"]
    )
    assert code == 0
    assert capsys.readouterr().out.strip() == "0.6123"


def test_the_latency_numbers_can_be_printed_by_their_own_command(tmp_path, capsys) -> None:
    """报告里 P50 / P95 旁边的复现命令必须真能跑出那两个数。"""

    run_dir = tmp_path / "20260303T000000Z"
    payload = _payload(
        run_id="20260303T000000Z",
        scores=[_score("q1"), _score("q2")],
        durations=[120.0, 480.0],
    )
    eval_runner.write_run_dir(run_dir, payload)

    code = eval_runner.main(["report", "--run", str(run_dir), "--select", "metrics.latency"])

    assert code == 0
    latency = json.loads(capsys.readouterr().out)
    assert latency["p50_ms"] == 120.0
    assert latency["p95_ms"] == 480.0


def test_report_subcommand_fails_loudly_on_an_unknown_select_key(tmp_path, capsys) -> None:
    run_dir = tmp_path / "run"
    eval_runner.write_run_dir(run_dir, _payload(run_id="run", scores=[_score("q1")]))

    code = eval_runner.main(["report", "--run", str(run_dir), "--select", "metrics.nope"])

    assert code == 2
    assert "取不到" in capsys.readouterr().err


def test_report_subcommand_can_repin_the_baseline_without_rerunning(tmp_path, monkeypatch) -> None:
    """重钉基线的动作不该要求重跑一次检索：归档里已经有全部数字。

    副本里的复现命令必须指向副本自己——否则换一台机器 clone 下来，
    报告里每条 ``--select`` 都指向一个不存在的 ``eval/runs/<id>``。
    """

    baseline_dir = tmp_path / "baseline"
    monkeypatch.setattr(eval_runner, "BASELINE_DIR", baseline_dir)
    run_dir = tmp_path / "20261111T000000Z"
    eval_runner.write_run_dir(run_dir, _payload(run_id="20261111T000000Z", scores=[_score("q1")]))

    code = eval_runner.main(["report", "--run", str(run_dir), "--update-baseline"])

    assert code == 0
    promoted = json.loads((baseline_dir / "results.json").read_text(encoding="utf-8"))
    archived = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert promoted["promoted_from"] == archived["run_dir"]
    assert promoted["run_dir"] == str(baseline_dir)
    report = (baseline_dir / "report.md").read_text(encoding="utf-8")
    assert f"--run {baseline_dir} --select" in report
    assert "来源 run" in report


def test_repinning_a_partial_run_is_refused(tmp_path, monkeypatch) -> None:
    baseline_dir = tmp_path / "baseline"
    monkeypatch.setattr(eval_runner, "BASELINE_DIR", baseline_dir)
    run_dir = tmp_path / "run"
    eval_runner.write_run_dir(
        run_dir, _payload(run_id="run", scores=[_score("q1")], full=False, status="smoke")
    )

    code = eval_runner.main(["report", "--run", str(run_dir), "--update-baseline"])

    assert code == 2
    assert not (baseline_dir / "results.json").exists()


def test_report_subcommand_refuses_a_directory_without_results(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(SystemExit) as excinfo:
        eval_runner.main(["report", "--run", str(empty)])

    assert "results.json" in str(excinfo.value)


# ───────────────────────────────────────────── 基线目录：读与写走同一个参数


def test_the_baseline_dir_defaults_to_the_repo_baseline_and_resolves_relative_paths(
    tmp_path,
) -> None:
    assert eval_runner.resolve_baseline_dir(None) == eval_runner.BASELINE_DIR
    assert eval_runner.resolve_baseline_dir("   ") == eval_runner.BASELINE_DIR
    assert eval_runner.resolve_baseline_dir("eval/baseline-rerank") == (
        REPO / "eval" / "baseline-rerank"
    )
    assert eval_runner.resolve_baseline_dir(str(tmp_path)) == tmp_path


def test_a_chosen_baseline_dir_is_what_gets_read(tmp_path, monkeypatch) -> None:
    """``--baseline-dir`` 指哪儿就从哪儿读，不能悄悄退回 eval/runs 里最近一次。"""

    _install_runs(
        tmp_path,
        monkeypatch,
        [_payload(run_id="20260101T000000Z", scores=[_score("q1", cp=0.5, cr=0.5)])],
    )
    other = tmp_path / "baseline-rerank"
    eval_runner.write_run_dir(
        other, _payload(run_id="20260303T000000Z", scores=[_score("q1", cp=0.7, cr=0.7)])
    )

    baseline, source = eval_runner.resolve_baseline(reference=None, baseline_dir=other)

    assert baseline is not None
    assert baseline["run_id"] == "20260303T000000Z"
    assert source and "baseline-rerank" in source


def test_report_repins_into_the_chosen_dir_and_leaves_the_repo_baseline_alone(tmp_path) -> None:
    """另存第二份基线（重排开的那份）不能动到随仓库的冻结基线。"""

    frozen_path = REPO / "eval" / "baseline" / "results.json"
    frozen_before = frozen_path.read_bytes()

    target = tmp_path / "baseline-rerank"
    run_dir = tmp_path / "20260303T000000Z"
    eval_runner.write_run_dir(run_dir, _payload(run_id="20260303T000000Z", scores=[_score("q1")]))

    code = eval_runner.main(
        ["report", "--run", str(run_dir), "--update-baseline", "--baseline-dir", str(target)]
    )

    assert code == 0
    promoted = json.loads((target / "results.json").read_text(encoding="utf-8"))
    assert promoted["run_dir"] == str(target)
    assert (target / "report.md").is_file()
    # 冻结基线一个字节都没动
    assert frozen_path.read_bytes() == frozen_before


def test_report_reads_the_baseline_from_the_chosen_dir(tmp_path) -> None:
    """读也走同一个参数：--baseline-dir 里的基线决定这次判可不可比。"""

    target = tmp_path / "baseline"
    eval_runner.write_run_dir(
        target,
        _payload(run_id="base", scores=[_score("q1", cp=0.9, cr=0.9)]),  # 重排关
    )
    run_dir = tmp_path / "now"
    payload = _payload(run_id="now", scores=[_score("q1", cp=0.9, cr=0.9)])
    payload["environment"]["retrieval"]["rerank_enabled"] = True
    eval_runner.write_run_dir(run_dir, payload)

    code = eval_runner.main(["report", "--run", str(run_dir), "--baseline-dir", str(target)])

    assert code == 0
    gate = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))["gate"]
    assert gate["verdict"] == "incomparable"
    assert "重排开关" in gate["summary"]


# ───────────────────────────────────── 护栏：换了一根尺子的数字不许盖旧基线


def test_repinning_a_baseline_with_another_rerank_switch_is_refused(tmp_path, capsys) -> None:
    """``report --update-baseline`` 会重写归档：一次手滑就能毁掉冻结基线。"""

    target = tmp_path / "baseline"
    eval_runner.write_run_dir(
        target,
        _payload(run_id="base", scores=[_score("q1", cp=0.9, cr=0.9)]),  # 重排关
    )
    before = (target / "results.json").read_text(encoding="utf-8")

    run_dir = tmp_path / "now"
    payload = _payload(run_id="now", scores=[_score("q1", cp=0.9, cr=0.9)])
    payload["environment"]["retrieval"]["rerank_enabled"] = True
    eval_runner.write_run_dir(run_dir, payload)

    code = eval_runner.main(
        ["report", "--run", str(run_dir), "--update-baseline", "--baseline-dir", str(target)]
    )

    assert code == 2
    assert "重排" in capsys.readouterr().err
    assert (target / "results.json").read_text(encoding="utf-8") == before


def test_force_overwrites_a_baseline_with_another_rerank_switch(tmp_path) -> None:
    target = tmp_path / "baseline"
    eval_runner.write_run_dir(
        target,
        _payload(run_id="base", scores=[_score("q1", cp=0.9, cr=0.9)]),  # 重排关
    )
    run_dir = tmp_path / "now"
    payload = _payload(run_id="now", scores=[_score("q1", cp=0.9, cr=0.9)])
    payload["environment"]["retrieval"]["rerank_enabled"] = True
    eval_runner.write_run_dir(run_dir, payload)

    code = eval_runner.main(
        [
            "report",
            "--run",
            str(run_dir),
            "--update-baseline",
            "--baseline-dir",
            str(target),
            "--force",
        ]
    )

    assert code == 0
    promoted = json.loads((target / "results.json").read_text(encoding="utf-8"))
    assert promoted["environment"]["retrieval"]["rerank_enabled"] is True


def test_pinning_a_baseline_of_the_same_rerank_switch_needs_no_force(tmp_path) -> None:
    target = tmp_path / "baseline"
    eval_runner.write_run_dir(
        target,
        _payload(run_id="base", scores=[_score("q1", cp=0.5, cr=0.5)]),  # 重排关
    )
    run_dir = tmp_path / "now"
    eval_runner.write_run_dir(
        run_dir,
        _payload(run_id="now", scores=[_score("q1", cp=0.9, cr=0.9)]),  # 也是关
    )

    code = eval_runner.main(
        ["report", "--run", str(run_dir), "--update-baseline", "--baseline-dir", str(target)]
    )

    assert code == 0
    assert json.loads((target / "results.json").read_text(encoding="utf-8"))["run_id"] == "now"


def test_run_refuses_to_pin_a_rerank_on_run_over_a_rerank_off_baseline_before_running(
    tmp_path, capsys
) -> None:
    """护栏要在跑之前就说不：不然不但覆盖了基线，还白烧了一整轮嵌入与重排的钱。"""

    target = tmp_path / "baseline"
    eval_runner.write_run_dir(target, _payload(run_id="base", scores=[_score("q1")]))
    before = (target / "results.json").read_text(encoding="utf-8")

    code = eval_runner.main(["run", "--rerank", "--update-baseline", "--baseline-dir", str(target)])

    assert code == 2
    assert "重排" in capsys.readouterr().err
    assert (target / "results.json").read_text(encoding="utf-8") == before


# ───────────────────────────────────────────────────────────── 基线的挑选


def _install_runs(tmp_path, monkeypatch, payloads: list[dict[str, Any]]) -> Path:
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    for payload in payloads:
        eval_runner.write_run_dir(runs / payload["run_id"], payload)
    monkeypatch.setattr(eval_runner, "RUNS_DIR", runs)
    monkeypatch.setattr(eval_runner, "BASELINE_DIR", tmp_path / "baseline")
    return runs


def test_the_pinned_baseline_wins_over_the_archives(tmp_path, monkeypatch) -> None:
    _install_runs(
        tmp_path,
        monkeypatch,
        [_payload(run_id="20260101T000000Z", scores=[_score("q1", cp=0.5, cr=0.5)])],
    )
    pinned = _payload(run_id="20260202T000000Z", scores=[_score("q1", cp=0.9, cr=0.9)])
    eval_runner.write_run_dir(tmp_path / "baseline", pinned)

    baseline, source = eval_runner.resolve_baseline(reference=None)

    assert baseline is not None
    assert baseline["run_id"] == "20260202T000000Z"
    assert source is not None
    assert Path(source).parts[-2:] == ("baseline", "results.json")


def test_baseline_falls_back_to_the_newest_full_run_and_skips_smoke_runs(
    tmp_path, monkeypatch
) -> None:
    """``--limit`` 跑出来的归档不能当基线：5 道题的数不能代表 127 道题。"""

    _install_runs(
        tmp_path,
        monkeypatch,
        [
            _payload(run_id="20260101T000000Z", scores=[_score("q1", cp=0.9, cr=0.9)]),
            _payload(
                run_id="20260103T000000Z",
                scores=[_score("q1", cp=0.1, cr=0.1)],
                full=False,
                status="smoke",
            ),
        ],
    )

    baseline, source = eval_runner.resolve_baseline(reference=None)

    assert baseline is not None
    assert baseline["run_id"] == "20260101T000000Z"
    assert source and "20260101T000000Z" in source


def test_baseline_none_means_no_comparison(tmp_path, monkeypatch) -> None:
    _install_runs(
        tmp_path, monkeypatch, [_payload(run_id="20260101T000000Z", scores=[_score("q1")])]
    )
    assert eval_runner.resolve_baseline(reference="none") == (None, None)


def test_an_explicit_baseline_reference_is_honoured(tmp_path, monkeypatch) -> None:
    runs = _install_runs(
        tmp_path,
        monkeypatch,
        [
            _payload(run_id="20260101T000000Z", scores=[_score("q1", cp=0.4, cr=0.4)]),
            _payload(run_id="20260102T000000Z", scores=[_score("q1", cp=0.8, cr=0.8)]),
        ],
    )

    baseline, _source = eval_runner.resolve_baseline(reference="20260101T000000Z")

    assert baseline is not None
    assert baseline["run_id"] == "20260101T000000Z"
    assert (runs / "20260101T000000Z" / "results.json").is_file()


def test_an_unreadable_baseline_reference_is_a_loud_failure(tmp_path, monkeypatch) -> None:
    _install_runs(tmp_path, monkeypatch, [])

    with pytest.raises(SystemExit) as excinfo:
        eval_runner.resolve_baseline(reference="20260101T000000Z")

    assert "找不到 run" in str(excinfo.value)


# ───────────────────────────────────────────────────── HTTP 冒烟（不进闸门）


def test_missing_response_fields_is_empty_for_a_complete_body() -> None:
    body = {
        "code": 0,
        "msg": "success",
        "data": {
            "query": "q",
            "kb_ids": ["kb-eval"],
            "retrieved_chunks": [{"chunk_id": "c1", "kb_id": "kb-eval", "score": 0.9}],
            "metadata": {
                "mode": "hybrid",
                "top_k": 5,
                "vector_store": "pgvector",
                "trace_id": "0" * 32,
                "retrieval": {
                    "degraded": False,
                    "degraded_reason": None,
                    "empty_reason": None,
                    "rerank": None,
                },
            },
        },
    }
    assert eval_runner.missing_response_fields(body) == []


def test_missing_response_fields_names_every_missing_layer() -> None:
    body = {
        "code": 0,
        "data": {
            "query": "q",
            "kb_ids": [],
            "retrieved_chunks": [{"chunk_id": "c1"}],
            "metadata": {
                "mode": "hybrid",
                "top_k": 5,
                "vector_store": "pgvector",
                "retrieval": {"degraded": False},
            },
        },
    }
    missing = eval_runner.missing_response_fields(body)

    assert "metadata.trace_id" in missing
    assert "metadata.retrieval.degraded_reason" in missing
    assert "data.retrieved_chunks[0].kb_id" in missing


def test_missing_response_fields_reports_a_business_failure_and_a_non_json_body() -> None:
    assert any(
        "code=" in item
        for item in eval_runner.missing_response_fields({"code": 20008, "msg": "no", "data": None})
    )
    assert eval_runner.missing_response_fields("not json") == ["响应体不是 JSON 对象"]


def test_smoke_questions_cover_one_per_type_then_fill_in_file_order() -> None:
    questions = [
        SimpleNamespace(id="q1", type="exact_code"),
        SimpleNamespace(id="q2", type="exact_code"),
        SimpleNamespace(id="q3", type="colloquial"),
        SimpleNamespace(id="q4", type="synonym"),
    ]

    assert [q.id for q in eval_runner.select_smoke_questions(questions, 2)] == ["q1", "q3"]
    # 题型不够 3 种时按原顺序补齐，绝不因为「凑不够」少跑一道
    assert [q.id for q in eval_runner.select_smoke_questions(questions[:2], 3)] == ["q1", "q2"]


# ───────────────────────────────────────────────────────────── 纯小工具


def test_utc_stamp_is_sortable_and_always_utc() -> None:
    moment = datetime(2026, 11, 11, 14, 20, 0, tzinfo=UTC)
    assert eval_runner.utc_stamp(moment) == "20261111T142000Z"
    # 带偏移的输入也回到 UTC：报告会被跨时区的人读
    local = datetime(2026, 11, 11, 6, 20, 0, tzinfo=UTC).astimezone(
        datetime.now().astimezone().tzinfo
    )
    assert eval_runner.utc_stamp(local) == "20261111T062000Z"


def test_database_url_is_redacted_before_it_reaches_the_archive() -> None:
    redacted = eval_runner._redact_database_url(
        "postgresql+psycopg://rag:s3cr3t@localhost:5432/rag"
    )
    assert "s3cr3t" not in redacted
    assert redacted.startswith("postgresql+psycopg://rag:***@localhost:5432/rag")
    # 没有口令的连接串原样返回
    assert eval_runner._redact_database_url("sqlite:///./dev.db") == "sqlite:///./dev.db"


def test_json_safe_turns_non_finite_floats_into_none() -> None:
    cleaned = eval_runner.json_safe(
        {"a": float("nan"), "b": float("inf"), "c": [1.0, float("-inf")], "d": "NaN"}
    )
    assert cleaned == {"a": None, "b": None, "c": [1.0, None], "d": "NaN"}


# ────────────────────── 重排实现的选择与注入（票 #116）


def _pin_rerank_settings(
    monkeypatch, *, enabled: bool, base_url: str = "", api_key: str = ""
) -> None:
    """把「这一轮用的是哪种重排」钉死，不依赖本机 ``.env`` 里恰好有什么。"""

    from app.core.config import settings

    for name in ("RERANK_ENABLED", "RERANK_BASE_URL", "RERANK_API_KEY", "MODEL_API_KEY"):
        monkeypatch.setattr(settings, name, getattr(settings, name))
    monkeypatch.setattr(settings, "RERANK_ENABLED", enabled)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", base_url)
    monkeypatch.setattr(settings, "RERANK_API_KEY", api_key)
    monkeypatch.setattr(settings, "MODEL_API_KEY", "")


def test_the_run_parser_defaults_to_auto_and_accepts_llm_for_comparison() -> None:
    """``--rerank-provider`` 缺省 ``auto``（生产口径）；``llm`` 是显式的对照开关。"""

    parser = eval_runner.build_parser()

    assert parser.parse_args(["run"]).rerank_provider == eval_runner.RERANK_PROVIDER_AUTO
    assert (
        parser.parse_args(["run", "--rerank", "--rerank-provider", "llm"]).rerank_provider
        == eval_runner.RERANK_PROVIDER_LLM
    )


def test_an_unknown_rerank_provider_is_refused_before_running(monkeypatch) -> None:
    """拼错的名字当场拒绝：不能静默退化成生产实现，让对照读数变成第三样东西。"""

    with pytest.raises(SystemExit):
        eval_runner.resolve_rerank_provider(
            SimpleNamespace(rerank_provider="bogus"), rerank_enabled=True
        )


def test_rerank_provider_is_none_when_the_switch_is_off(monkeypatch) -> None:
    """重排关着时快照写 ``none``：那一步根本没跑，写 ``llm`` 会让人以为跑过对照。"""

    _pin_rerank_settings(monkeypatch, enabled=False, base_url="https://x.test/v1", api_key="k")

    assert (
        eval_runner.resolve_rerank_provider(
            SimpleNamespace(rerank_provider="llm"), rerank_enabled=False
        )
        == "none"
    )


def test_auto_reports_the_production_implementation(monkeypatch) -> None:
    """``auto`` 如实写生产工厂会装出的那个：端点配齐是 ``qwen3``，否则 ``none``。"""

    _pin_rerank_settings(
        monkeypatch, enabled=True, base_url="https://rerank.test/compatible-api/v1", api_key="k"
    )
    assert (
        eval_runner.resolve_rerank_provider(
            SimpleNamespace(rerank_provider="auto"), rerank_enabled=True
        )
        == "qwen3"
    )

    _pin_rerank_settings(monkeypatch, enabled=True, base_url="", api_key="k")
    assert (
        eval_runner.resolve_rerank_provider(
            SimpleNamespace(rerank_provider="auto"), rerank_enabled=True
        )
        == "none"
    )


def test_explicit_llm_reports_the_old_implementation(monkeypatch) -> None:
    """``--rerank-provider llm`` 与环境无关：它说的是「这一轮跑旧实现」。"""

    _pin_rerank_settings(monkeypatch, enabled=True, base_url="", api_key="")

    assert (
        eval_runner.resolve_rerank_provider(
            SimpleNamespace(rerank_provider="llm"), rerank_enabled=True
        )
        == "llm"
    )


def test_the_environment_snapshot_records_which_rerank_implementation_ran(monkeypatch) -> None:
    """快照必须能区分两种实现：``rerank_enabled`` 两边都是 true，光看它分不出来。"""

    snapshot = eval_runner.environment_snapshot(
        run_id="20261112T000000Z",
        generated_at="2026-11-12T00:00:00+00:00",
        experiment={"mode": "hybrid", "top_k": 5},
        corpus={"version": "v1"},
        dataset_version="v1",
        env_file=None,
        llm_endpoint_source=eval_runner.ENDPOINT_SOURCE_ENV,
        llm_model="deepseek-flash",
        rerank_provider="llm",
    )

    assert snapshot["rerank_provider"] == "llm"


def test_the_snapshot_derives_the_test_only_when_not_told(monkeypatch) -> None:
    """不给时按生产工厂推算——但不是随便编一个：端点没配就是 ``none``。"""

    _pin_rerank_settings(monkeypatch, enabled=True, base_url="", api_key="")

    snapshot = eval_runner.environment_snapshot(
        run_id="20261112T000000Z",
        generated_at="2026-11-12T00:00:00+00:00",
        experiment={"mode": "hybrid", "top_k": 5},
        corpus={"version": "v1"},
        dataset_version="v1",
        env_file=None,
        llm_endpoint_source=eval_runner.ENDPOINT_SOURCE_ENV,
        llm_model="deepseek-flash",
    )

    assert snapshot["rerank_provider"] == "none"


class _FakeProvider:
    """替身重排：只记下自己被装配过，不发网络。"""

    available = True
    provider_name = "fake"
    model_name = None

    async def rerank(self, *, query: str, chunks: list[dict], top_n: int) -> list[dict]:
        return chunks[:top_n]


def test_build_rerank_factory_is_none_for_the_production_path() -> None:
    """生产口径不需要注入：``None`` 让编排层走既有工厂出口。"""

    assert eval_runner.build_rerank_factory(provider_name="qwen3") is None
    assert eval_runner.build_rerank_factory(provider_name="none") is None


def test_build_rerank_factory_llm_really_builds_the_old_implementation(
    monkeypatch,
) -> None:
    """``llm`` 时注入的确实是旧实现（大模型重排），且装配走 fast 档。

    替身大模型接在既有出口上，所以这条用例**不发网络**：它证明的是「注入的东西是旧的
    那个类」，而不是「那个类今天答得好不好」。
    """

    from app.providers.llm.base import LLMProvider
    from app.providers.rerank.llm import LLMRerankProvider

    class _FakeLLM(LLMProvider):
        model_name = "fast-stand-in"

        async def chat_structured(self, schema, *, system: str, user: str) -> Any:
            raise AssertionError("这条用例只装配，不调用")

        async def chat_json(self, *, system: str, user: str) -> dict:  # pragma: no cover
            raise AssertionError("重排只走结构化输出")

    monkeypatch.setattr("app.providers.llm.get_llm_provider", lambda role="fast": _FakeLLM())

    factory = eval_runner.build_rerank_factory(provider_name="llm")
    assert factory is not None
    provider = factory()

    assert isinstance(provider, LLMRerankProvider)
    assert (provider.provider_name, provider.model_name) == ("llm", "fast-stand-in")


def test_score_questions_forwards_the_rerank_factory(monkeypatch) -> None:
    """注入缝一路到底：``_score_questions`` 必须把工厂交给 ``_run_question``。"""

    from scripts import eval_retrieval as ev

    captured: dict[str, Any] = {}

    async def fake_run_question(db, question, *, mode, top_k, kb_ids, rerank_factory=None):
        captured["rerank_factory"] = rerank_factory
        return ev.QuestionOutcome(
            id=question.id,
            query=question.query,
            mode=mode,
            returned=[{"chunk_id": "c1"}],
            rerank={"applied": True, "provider": "llm", "model": None},
        )

    monkeypatch.setattr(ev, "_run_question", fake_run_question)

    factory = lambda: _FakeProvider()  # noqa: E731 - 工厂就是一个可调用对象
    questions = [SimpleNamespace(id="q1", type="colloquial", query="怎么装")]

    asyncio.run(
        eval_runner._score_questions(
            None,
            questions=questions,
            labels={"q1": ["c1"]},
            mode="hybrid",
            top_k=1,
            kb_ids=["kb-eval"],
            rerank_factory=factory,
        )
    )

    assert captured["rerank_factory"] is factory


def test_run_question_hands_the_factory_to_the_retrieval_entry(monkeypatch) -> None:
    """``_run_question`` 是评测侧最后一环：工厂必须出现在 ``retrieve`` 的参数里。"""

    from scripts import eval_retrieval as ev

    captured: dict[str, Any] = {}

    async def fake_retrieve(db, **kwargs):
        captured.update(kwargs)
        return {
            "query": kwargs["query"],
            "kb_ids": list(kwargs["kb_ids"]),
            "retrieved_chunks": [],
            "metadata": {
                "retrieval": {
                    "degraded": False,
                    "degraded_reason": None,
                    "empty_reason": None,
                    "rerank": {"applied": False, "provider": None, "model": None},
                }
            },
        }

    monkeypatch.setattr(ev, "retrieve", fake_retrieve)

    from app.providers.rerank import build_llm_rerank_provider

    factory = eval_runner.build_rerank_factory(provider_name="llm")
    asyncio.run(
        ev._run_question(
            None,
            SimpleNamespace(id="q1", type="colloquial", query="怎么装"),
            mode="hybrid",
            top_k=1,
            kb_ids=["kb-eval"],
            rerank_factory=factory,
        )
    )

    assert captured["rerank_factory"] is build_llm_rerank_provider

"""旁证答案集（#108）的离线守卫：机械校验器、选题确定性与失败留痕。

这份套件只碰**纯函数**与**注入的替身**，不连库、不发网络、不读 ``.env``：

* 机械校验器是这张票真正的价值。它判错一次，要么把编造的数字放行（假绿），
  要么把忠实复述的答案判成脏（假红，逼着人去手改金标）。所以用真值表把
  「编号在正文里 / 不在正文里 / 全半角差异 / 千分位」四种情况逐条钉死，
  期望值全是手写的字面量，不由被测代码算出来。
* 选题必须**确定性**：同种子两跑选出同一批题、题型覆盖达标、``absent`` 被排除。
  否则「固定 50 题子集」这句可复现的承诺就是空话。
* 失败与拒答必须**有记录**：生成异常 / 超时 / 输出不合法 / 模型说「片段没提」，
  都要留下明确的一行，绝不能被静默跳过——那会让通过率凭空变高。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import build_answer_set as bas

REPO = Path(__file__).resolve().parents[1]


# ─────────────────────────────────────────────────────── 机械校验：抽取待查项


def _atoms(answer: str) -> list[tuple[str, str]]:
    return [(atom.kind, atom.text) for atom in bas.extract_atoms(answer)]


def test_extracts_error_code_model_number_and_project_code() -> None:
    """三种「编号」都要抽出来，并且各自认对种类。"""

    atoms = _atoms("E1024 先续期；XR-2000 Pro 用 24 瓦适配器；PRJ-9QX8B3 是项目代号。")

    assert ("error_code", "E1024") in atoms
    assert ("model_number", "XR-2000PRO") in atoms
    assert ("project_code", "PRJ-9QX8B3") in atoms


def test_list_markers_are_not_numbers() -> None:
    """有序列表的「1.」「2、」不是事实，不该被当成数字去核对。"""

    atoms = _atoms("1. 先续期\n2、再重试\nE1025 表示吊销")

    assert [text for kind, text in atoms if kind == "number"] == []
    assert ("error_code", "E1025") in atoms


def test_percent_and_unit_carrying_single_digit_survive() -> None:
    """「10%」「7 天」这种带单位的单个数字是**事实**，不能因为位数少被丢掉。"""

    atoms = _atoms("余量低于 10% 时上报，静默窗口 7 天，门槛 15 次。")

    assert ("number", "10%") in atoms
    assert ("number", "7") in atoms
    assert ("number", "15") in atoms


# ─────────────────────────────────────────────────────── 机械校验：真值表


def test_code_present_in_source_passes() -> None:
    """编号在正文里 → 通过，missing 为空。"""

    result = bas.check_answer(
        "E1024 表示证书链已过期，先触发在线续期再重试。",
        ["E1024 表示设备本地保存的证书链已经超过有效期。处理方式是触发一次在线续期。"],
    )

    assert result["passed"] is True
    assert result["missing"] == []


def test_code_absent_from_source_is_flagged() -> None:
    """编号不在正文里 → 判失败，并把**哪一个**编号缺了说清楚。"""

    result = bas.check_answer(
        "E1024 表示证书链已过期，处理方式见 E9999。",
        ["E1024 表示设备本地保存的证书链已经超过有效期。"],
    )

    assert result["passed"] is False
    assert [item["text"] for item in result["missing"]] == ["E9999"]


def test_fullwidth_and_halfwidth_match() -> None:
    """全角数字 / 全角百分号要和半角正文对得上（归一化后比较）。"""

    result = bas.check_answer("余量低于 １０％ 时上报，门槛 １５ 次。", ["余量低于 10% 时上报，门槛 15 次。"])

    assert result["passed"] is True


def test_thousand_separator_matches_without_it() -> None:
    """答案写千分位、正文没写（以及反过来）都算找到：1280 与 1,280 是同一个数。"""

    with_separator = bas.check_answer("指导价 1,280 元。", ["| XR-2000 | 1280 | 1 台 |"])
    without_separator = bas.check_answer("指导价 1280 元。", ["| XR-2000 | 1,280 | 1 台 |"])

    assert with_separator["passed"] is True
    assert without_separator["passed"] is True


def test_model_suffix_must_match_exactly() -> None:
    """XR-2100 不能因为正文里有 XR-2000 Pro 就算找到——后缀差异正是这类题的考点。"""

    result = bas.check_answer("XR-2100 是机架款。", ["XR-2000 Pro 是增强款，含 24 瓦适配器。"])

    assert result["passed"] is False
    assert [item["text"] for item in result["missing"]] == ["XR-2100"]


def test_whitespace_inside_number_is_ignored() -> None:
    """正文里的「30 分钟」与答案里的「30分钟」是同一个事实。"""

    result = bas.check_answer("静默窗口打开 30分钟。", ["把告警静默窗口打开 30 分钟，避免重复打扰。"])

    assert result["passed"] is True


def test_check_reports_every_atom_it_looked_for() -> None:
    """报告要能回答「到底核了哪些东西」，不只是缺了什么。"""

    result = bas.check_answer("E1032 与 E1040 都是证书类错误。", ["E1032 是双向认证问题。"])

    assert [item["text"] for item in result["atoms"]] == ["E1032", "E1040"]
    assert [item["text"] for item in result["missing"]] == ["E1040"]


def test_chinese_numerals_with_units_are_checked_too() -> None:
    """「三天」「十分钟」也是事实：正文写 3 天、答案写三天，是同一个数，不能漏检。

    **查出来但只提示，不判失败**：中文数字与阿拉伯数字的对应关系是「同一个量可以有
    多种写法」，机械比对分不清「改写」与「编造」——正文写「双人复核」、答案写「两人
    一起复核」是正确转述，判硬失败就是冤枉。所以它落在 ``soft_missing`` 里，由报告
    点名；硬失败只留给编号、错误码、型号与阿拉伯数字。
    """

    found = bas.check_answer("门店连续三天重复。", ["门店在凌晨集中离线，连续 3 天重复。"])
    missed = bas.check_answer("门店连续三天重复。", ["门店在凌晨集中离线，连续 7 天重复。"])
    ten = bas.check_answer("静默窗口十分钟。", ["把告警静默窗口打开 10 分钟。"])

    # 同一个量（3 = 三）：两侧折成同一把尺子之后，它不该出现在任何一张缺失清单里。
    assert found["passed"] is True
    assert found["soft_missing"] == []

    # 不一致（三 ≠ 7）：**查得出来**，但只进 soft_missing，不进 missing、不改 passed。
    assert [item["text"] for item in missed["soft_missing"]] == ["3"]
    assert missed["missing"] == []

    assert ten["passed"] is True


def test_ambiguous_chinese_phrases_are_not_numbers() -> None:
    """「十分满意」「一部分」不是数量：只认「中文数字 + 量词」，否则会把形容词当事实。"""

    assert bas.extract_atoms("客户十分满意，只有一部分设备受影响。") == []


# ─────────────────────────────────────────────────────── 选题：确定性与覆盖


def _question(qid: str, type_: str) -> bas.QuestionRef:
    return bas.QuestionRef(
        id=qid,
        type=type_,
        query=f"{qid} 的问题",
        labels=(("support_errors", "售后故障与错误码速查 > 连接与证书类错误（一）"),),
    )


def _pool(counts: dict[str, int]) -> list[bas.QuestionRef]:
    return [
        _question(f"{type_[:3]}{index:02d}", type_)
        for type_, total in counts.items()
        for index in range(1, total + 1)
    ]


_POOL = _pool(
    {
        "exact_code": 5,
        "model_number": 15,
        "colloquial": 11,
        "synonym": 12,
        "cross_section": 17,
        "table_lookup": 17,
        "noise_robust": 17,
        "negative_scope": 14,
        "counterfactual": 13,
        "rare_token": 6,
        "false_premise": 4,
        "absent": 6,
    }
)


def test_quotas_fill_the_requested_total_without_exceeding_pools() -> None:
    """配额之和必须正好等于总题数，且任一题型不超过它的池子大小。"""

    quotas, warnings = bas.allocate_quotas(
        bas.type_pools(_POOL), total=50, per_type_min=2, per_type_max=None
    )

    assert sum(quotas.values()) == 50
    assert warnings == []
    for type_, quota in quotas.items():
        assert quota <= bas.type_pools(_POOL)[type_]
        assert quota >= 2


def test_absent_is_never_selected() -> None:
    """``absent`` 题在库里本就没有答案，喂给模型只会逼它编——一律排除。"""

    quotas, _ = bas.allocate_quotas(
        bas.type_pools(_POOL), total=50, per_type_min=2, per_type_max=None
    )
    cases = bas.select_cases(_POOL, quotas, seed=7)

    assert quotas.get("absent", 0) == 0
    assert all(case.type != "absent" for case in cases)


def test_selection_is_deterministic_for_a_seed() -> None:
    """同一个种子两跑选出同一批题（顺序也一致），否则「固定 50 题」不可比。"""

    quotas, _ = bas.allocate_quotas(
        bas.type_pools(_POOL), total=50, per_type_min=2, per_type_max=None
    )

    first = [case.id for case in bas.select_cases(_POOL, quotas, seed=7)]
    second = [case.id for case in bas.select_cases(_POOL, quotas, seed=7)]
    other = [case.id for case in bas.select_cases(_POOL, quotas, seed=8)]

    assert first == second
    assert first != other


def test_limit_takes_a_prefix_that_still_covers_several_types() -> None:
    """``--limit 3`` 是通路验证用的：3 道题要落在 3 个不同题型上，而不是同一类。"""

    quotas, _ = bas.allocate_quotas(
        bas.type_pools(_POOL), total=50, per_type_min=2, per_type_max=None
    )
    cases = bas.select_cases(_POOL, quotas, seed=7, limit=3)

    assert len(cases) == 3
    assert len({case.type for case in cases}) == 3


def test_per_type_max_caps_the_plan_and_says_so() -> None:
    """把上限压到 3 时，50 题装不下——不许悄悄少做，必须给出「差多少」的告警。"""

    quotas, warnings = bas.allocate_quotas(
        bas.type_pools(_POOL), total=50, per_type_min=2, per_type_max=3
    )

    assert sum(quotas.values()) == 11 * 3
    assert any("50" in warning and "33" in warning for warning in warnings)


def test_types_filter_restricts_the_plan() -> None:
    """``--types`` 只跑指定题型（小样本调试用）。"""

    quotas, _ = bas.allocate_quotas(
        bas.type_pools(_POOL),
        total=6,
        per_type_min=2,
        per_type_max=None,
        only=("exact_code", "rare_token", "false_premise"),
    )

    assert set(quotas) == {"exact_code", "rare_token", "false_premise"}
    assert sum(quotas.values()) == 6


# ─────────────────────────────────────────────────────── 喂什么：只有标注切片


def _case(ref: bas.QuestionRef | None = None) -> bas.QuestionRef:
    return ref or _question("q01", "exact_code")


def test_prompt_feeds_only_the_given_chunks() -> None:
    """提示词里出现的就是那几条切片正文，且明确「片段没有就说不知道」。"""

    sources = [
        bas.SourceChunk(
            chunk_id="support_errors::000",
            document="support_errors",
            heading_path="售后故障与错误码速查 > 连接与证书类错误（一）",
            order_index=0,
            content="E1024 表示设备本地保存的证书链已经超过有效期。",
        )
    ]
    system, user = bas.build_prompt(_case(), sources)

    assert "E1024 表示设备本地保存的证书链已经超过有效期。" in user
    assert "只能依据" in system
    assert "片段未提及" in system
    assert "JSON" in system
    # 干扰项：切片之外的内容一个字都不许带进去。
    assert "support_models" not in user + system
    assert "XR-2000" not in user + system


def test_false_premise_prompt_asks_for_the_broken_premise() -> None:
    """``false_premise`` 题的答案要说明「前提不成立」，这条写进提示词而不是靠运气。"""

    system, _ = bas.build_prompt(_question("q100", "false_premise"), [])
    plain_system, _ = bas.build_prompt(_question("q01", "exact_code"), [])

    assert "前提" in system
    assert "前提" not in plain_system


# ─────────────────────────────────────────────────────── 失败 / 拒答要留痕


def _run(cases, chat, **kwargs):
    sources = kwargs.pop("sources", [])
    return asyncio.run(
        bas.run_generation(cases, sources_of=lambda case: sources, chat=chat, **kwargs)
    )


def test_failed_call_is_recorded_not_skipped() -> None:
    """模型调用抛错 → 该题记 failed 并附原因，其余题继续跑。"""

    async def chat(system: str, user: str) -> dict[str, Any]:
        if "q02" in user:
            raise RuntimeError("boom 上游 503")
        return {"answer": "E1024 表示证书链过期。"}

    records = _run(
        [_case(_question("q01", "exact_code")), _case(_question("q02", "exact_code"))],
        chat,
        sources=[
            bas.SourceChunk(
                chunk_id="support_errors::000",
                document="support_errors",
                heading_path="h",
                order_index=0,
                content="E1024 表示设备本地保存的证书链已经超过有效期。",
            )
        ],
    )

    by_id = {record["id"]: record for record in records}
    assert by_id["q01"]["status"] == "ok"
    assert by_id["q02"]["status"] == "failed"
    assert "503" in by_id["q02"]["error"]
    assert by_id["q02"]["answer"] == ""


def test_timeout_is_recorded_as_failure() -> None:
    """超时不能被当作「没有答案」掠过，必须留下 timeout 字样。"""

    async def chat(system: str, user: str) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {"answer": "迟到"}

    records = _run([_case()], chat, answer_timeout_s=0.01)

    assert records[0]["status"] == "failed"
    assert "timeout" in records[0]["error"].lower()


def test_invalid_payload_is_recorded_as_failure() -> None:
    """``{"answer": 123}`` / 空串都不是答案：记 failed，绝不写占位符冒充成功。"""

    async def chat(system: str, user: str) -> dict[str, Any]:
        return {"answer": "   "}

    records = _run([_case()], chat)

    assert records[0]["status"] == "failed"
    assert records[0]["answer"] == ""


def test_refusal_is_recorded_separately_from_success() -> None:
    """模型明确说「片段未提及」→ 记 refused，不算通过率的分子，也不假装成功。"""

    async def chat(system: str, user: str) -> dict[str, Any]:
        return {"answer": "片段未提及"}

    records = _run([_case()], chat)

    assert records[0]["status"] == "refused"


def test_answer_with_fabricated_code_fails_the_check_but_keeps_the_answer() -> None:
    """校验不通过时**不改写答案去凑**：答案原样留下，失败清单里点名。"""

    async def chat(system: str, user: str) -> dict[str, Any]:
        return {"answer": "E9999 表示证书链过期。"}

    records = _run(
        [_case()],
        chat,
        sources=[
            bas.SourceChunk(
                chunk_id="support_errors::000",
                document="support_errors",
                heading_path="h",
                order_index=0,
                content="E1024 表示设备本地保存的证书链已经超过有效期。",
            )
        ],
    )

    assert records[0]["status"] == "ok"
    assert records[0]["answer"] == "E9999 表示证书链过期。"
    assert records[0]["checks"]["passed"] is False
    assert records[0]["checks"]["missing"][0]["text"] == "E9999"


# ─────────────────────────────────────────────────────── 产物形状与抽检


def test_payload_carries_counts_and_pass_rate() -> None:
    """机读产物要能回答「生成了几条、通过率多少」，并与题库/语料同源版本化。"""

    records = [
        {
            "id": "q01",
            "type": "exact_code",
            "question": "q",
            "status": "ok",
            "answer": "E1024",
            "source_chunk_ids": ["c1"],
            "sources": [],
            "checks": {"passed": True, "missing": [], "atoms": []},
        },
        {
            "id": "q02",
            "type": "exact_code",
            "question": "q",
            "status": "failed",
            "answer": "",
            "error": "boom",
            "source_chunk_ids": ["c2"],
            "sources": [],
            "checks": {"passed": False, "missing": [], "atoms": []},
        },
        {
            "id": "q03",
            "type": "synonym",
            "question": "q",
            "status": "refused",
            "answer": "片段未提及",
            "source_chunk_ids": ["c3"],
            "sources": [],
            "checks": {"passed": False, "missing": [], "atoms": []},
        },
    ]
    payload = bas.build_payload(
        records,
        judge_model="deepseek-v4-1-flash-economy-261017",
        questions_version={"sha256": "abc", "count": 137},
        corpus={"version": "v4", "documents": 21, "chunks": 469},
        seed=7,
        generated_at="2026-11-11T00:00:00+00:00",
    )

    assert payload["version"] == "v1"
    assert payload["stats"] == {
        "requested": 3,
        "ok": 1,
        "refused": 1,
        "failed": 1,
        "checked": 1,
        "check_passed": 1,
        "check_pass_rate": 1.0,
    }
    assert payload["judge_model"] == "deepseek-v4-1-flash-economy-261017"
    assert payload["cases"][0]["checks"]["passed"] is True


def test_spot_checks_are_deterministic_and_carry_an_excerpt() -> None:
    """抽检记录要能被人快速核对：同种子抽出同一批，且带依据片段前 200 字。"""

    records = [
        {
            "id": f"q{index:02d}",
            "type": "exact_code",
            "question": f"问题 {index}",
            "status": "ok",
            "answer": f"答案 {index}",
            "source_chunk_ids": [f"c{index}"],
            "sources": [
                {
                    "chunk_id": f"c{index}",
                    "document": "support_errors",
                    "heading_path": "h",
                    "content": "正" * 300,
                }
            ],
            "checks": {"passed": True, "missing": [], "atoms": []},
        }
        for index in range(1, 13)
    ]

    first = bas.pick_spot_checks(records, seed=7, count=10)
    second = bas.pick_spot_checks(records, seed=7, count=10)

    assert [item["id"] for item in first] == [item["id"] for item in second]
    assert len(first) == 10
    assert all(len(item["excerpt"]) == 200 for item in first)


def test_report_states_counts_pass_rate_and_failure_reasons() -> None:
    """报告的三个硬信息：生成数量、校验通过率、失败清单（含原因）。"""

    records = [
        {
            "id": "q01",
            "type": "exact_code",
            "question": "E1024 是什么",
            "status": "ok",
            "answer": "E1024 表示证书链过期。",
            "source_chunk_ids": ["c1"],
            "sources": [{"chunk_id": "c1", "document": "d", "heading_path": "h", "content": "E1024 …"}],
            "checks": {"passed": True, "missing": [], "atoms": []},
        },
        {
            "id": "q02",
            "type": "synonym",
            "question": "退钱要多久",
            "status": "failed",
            "answer": "",
            "error": "AppError LLM_FAILED: 上游 503",
            "source_chunk_ids": ["c2"],
            "sources": [],
            "checks": {"passed": False, "missing": [], "atoms": []},
        },
        {
            "id": "q03",
            "type": "model_number",
            "question": "XR-2100 支持什么",
            "status": "ok",
            "answer": "XR-2100 是机架款，价格 9600。",
            "source_chunk_ids": ["c3"],
            "sources": [{"chunk_id": "c3", "document": "d", "heading_path": "h", "content": "9600"}],
            "checks": {
                "passed": False,
                "missing": [{"kind": "number", "text": "9600", "surface": "9600"}],
                "atoms": [{"kind": "number", "text": "9600", "surface": "9600"}],
            },
        },
    ]
    payload = bas.build_payload(
        records,
        judge_model="m",
        questions_version={"sha256": "abc", "count": 137},
        corpus={"version": "v4", "documents": 21, "chunks": 469},
        seed=7,
        generated_at="2026-11-11T00:00:00+00:00",
    )

    report = bas.render_report(payload, trial=None)

    assert "生成数量" in report
    assert "校验通过率" in report
    assert "AppError LLM_FAILED: 上游 503" in report
    assert "9600" in report
    assert "q03" in report


def test_report_can_carry_the_trial_run() -> None:
    """成本控制要求两次输出都留档：先 ``--limit 3`` 通路，再全量。"""

    payload = bas.build_payload(
        [],
        judge_model="m",
        questions_version={"sha256": "abc", "count": 137},
        corpus={"version": "v4", "documents": 21, "chunks": 469},
        seed=7,
        generated_at="2026-11-11T00:00:00+00:00",
    )
    trial = {
        "stats": {"requested": 3, "ok": 2, "refused": 0, "failed": 1, "check_passed": 2, "check_pass_rate": 1.0},
        "cases": [{"id": "q01", "status": "ok", "answer": "E1024 表示证书链过期。"}],
        "generated_at": "2026-11-11T00:00:00+00:00",
    }

    report = bas.render_report(payload, trial=trial)

    assert "试跑" in report
    assert "q01" in report


def test_module_import_does_not_touch_the_database() -> None:
    """脚本要能在无 ``.env`` 下被单测导入：模块顶层不连库、不建客户端。"""

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import scripts.build_answer_set as m; "
            "print('app.db.session' in sys.modules, 'app.providers.llm' in sys.modules)",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False False", completed.stdout


def test_cli_writes_payload_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 这条缝：选题 → 解析切片 → 生成（替身）→ 写机读产物 + 报告，不连库不发网络。

    只替换两个外部依赖（题库/语料与模型），其余全走真代码——包括真实切块器与
    ``_resolve_question_labels``，因为「标注解析不到就当场失败」正是这条路径要守的东西。
    """

    questions = [_question("q01", "exact_code")]
    chunk = bas.SourceChunk(
        chunk_id="support_errors::000",
        document="support_errors",
        heading_path="售后故障与错误码速查 > 连接与证书类错误（一）",
        order_index=0,
        content="E1024 表示设备本地保存的证书链已经超过有效期。",
    )
    monkeypatch.setattr(bas, "load_questions", lambda: questions)
    monkeypatch.setattr(bas, "load_corpus_chunks", lambda **_: ([chunk], {"support_errors.md": "E1024 …"}))

    async def fake_chat(system: str, user: str) -> dict[str, Any]:
        return {"answer": "E1024 表示证书链已经超过有效期。"}

    monkeypatch.setattr(bas, "build_judge", lambda: (fake_chat, {"provider": "p", "model": "m", "base_url": "b"}))

    out = tmp_path / "answers.json"
    report = tmp_path / "answers.report.md"
    code = bas.main(
        [
            "--types",
            "exact_code",
            "--total",
            "1",
            "--out",
            str(out),
            "--report",
            str(report),
            "--trial",
            str(tmp_path / "missing-trial.json"),
        ]
    )

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["stats"]["ok"] == 1
    assert payload["cases"][0]["id"] == "q01"
    assert payload["cases"][0]["checks"]["passed"] is True
    # 产物里不存切片正文：正文由「语料版本 + 坐标」复原。
    assert "content" not in payload["cases"][0]["sources"][0]
    assert "生成数量" in report.read_text(encoding="utf-8")

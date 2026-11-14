"""#153 · C6 评测环境准备脚本：幂等判定与「可提交快照里不许有机密」。

脚本本身要连真库、真 ES、真嵌入，所以它的**整段运行**不属于离线套件。这里守的是它最容易
出错的两类东西，都是纯逻辑：

1. **幂等判定**：``plan_changes`` / ``policy_changes`` 只报「还差哪些列」。报多了等于每次重跑
   都改一遍（读数里就分不清「这次改了什么」）；报少了等于配置没生效而脚本说「已达标」。
2. **快照不许泄露 Key 原文**：那份 ``eval/reports/c6/environment.json`` 是要提交进仓库的，
   而 Key 在本期是明文入库（ADR-0004）——把整份状态直接 dump 出去，一次提交就等于把凭据
   贴到历史上。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import prep_eval_c6 as prep  # noqa: E402

KB_ID = "kb-1"
SECRET = "rc_live_0123456789abcdef0123456789abcdef"


def _plan_row(**overrides) -> SimpleNamespace:
    values = {"code": prep.PRO_PLAN_CODE, **prep.PRO_PLAN_TARGETS}
    values.update(overrides)
    return SimpleNamespace(**values)


def _policy_row(**overrides) -> SimpleNamespace:
    values = {
        "allowed_kb_ids": [KB_ID],
        **prep.POLICY_TARGETS,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# ───────────────────────────────────────────────────────────── 幂等判定


def test_plan_patch_is_empty_when_the_plan_already_matches() -> None:
    """重跑时「零变更」必须能断言：这行的读数是报告里「环境没被再改一遍」的证据。"""

    patch = prep.plan_changes(_plan_row())

    assert patch.changed is False
    assert patch.as_dict()["changes"] == {}


def test_plan_patch_lists_only_the_columns_that_differ() -> None:
    """迁移回填的是 0：第一次跑必须把 Research 那两列与编排位列出来。"""

    patch = prep.plan_changes(
        _plan_row(orchestration_allowed=False, research_qps=0, research_daily=0)
    )

    assert patch.changed is True
    assert patch.changes == {
        "orchestration_allowed": True,
        "research_qps": 1,
        "research_daily": 100,
    }


def test_policy_patch_requires_exactly_one_knowledge_base() -> None:
    """``allowed_kb_ids`` 要**恰好**只圈这个库：多圈一个，越权检查就查不出问题。"""

    assert prep.policy_changes(_policy_row(), kb_id=KB_ID).changes == {}

    wrong = prep.policy_changes(_policy_row(allowed_kb_ids=[]), kb_id=KB_ID)
    assert wrong.changes == {"allowed_kb_ids": [KB_ID]}


def test_policy_patch_does_not_touch_the_limits_that_are_already_right() -> None:
    """只报缺的列：把整张表每次重写一遍，读的人就分不清「谁改了什么」。"""

    patch = prep.policy_changes(_policy_row(profile="speed", research_allowed=False), kb_id=KB_ID)

    assert patch.changes == {"profile": "quality", "research_allowed": True}


def test_reference_edge_count_ignores_sibling_and_parent_edges() -> None:
    """对账只数 ``reference``：兄弟 / 父节边也在同一张表里，数总行数看不出引用齐没齐。"""

    assert prep.reference_edge_count({"reference": 9, "sibling": 22, "parent": 2}) == 9
    assert prep.reference_edge_count({"sibling": 22}) == 0


# ───────────────────────────────────────────────────────── 可提交的快照


def _correlation() -> prep.Correlation:
    return prep.Correlation(
        tenant_id="tenant_c6",
        policy_id="policy-1",
        key_id="key-1",
        kb_id=KB_ID,
        documents=["points_policy.md", "promo_rules.md", "refund_policy.md"],
        versions=[
            {"version": "v1", "status": "archived", "document_count": 3, "chunk_count": 15},
            {"version": "v2", "status": "active", "document_count": 3, "chunk_count": 15},
        ],
        chunks_by_version={"v1": 15, "v2": 15},
        sections_by_version={"v1": 15, "v2": 15},
        edges_by_version={
            "v1": {"reference": 5, "sibling": 22, "parent": 2},
            "v2": {"reference": 9, "sibling": 22, "parent": 2},
        },
    )


def _snapshot() -> dict:
    return prep.environment_snapshot(
        correlation=_correlation(),
        plan_row={"code": "pro", "research_qps": 1, "research_daily": 100},
        preset_row={"profile": "quality", "rerank": True},
        corpus={"dir": "eval/corpus-refs", "documents": [], "reference_inventory": 9},
    )


def test_the_key_value_lands_only_in_the_gitignored_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两份产物分工不许串：可提交的快照没有 Key 原文，本机状态有（且那份进 .gitignore）。

    Key 在本期是**明文入库**（ADR-0004），所以「哪份文件能提交」只有一条判据：里面有
    没有那串原文。把它写在测试里，比写在脚本注释里可核验。
    """

    environment_file = tmp_path / "environment.json"
    local_file = tmp_path / "c6-state.json"
    monkeypatch.setattr(prep, "ENVIRONMENT_FILE", environment_file)
    monkeypatch.setattr(prep, "LOCAL_STATE_FILE", local_file)

    prep.write_state(correlation=_correlation(), snapshot=_snapshot(), key_value=SECRET)

    snapshot_text = environment_file.read_text(encoding="utf-8")
    local_state = json.loads(local_file.read_text(encoding="utf-8"))
    assert SECRET not in snapshot_text, "可提交的那份不许出现 Key 原文"
    assert local_state["key_value"] == SECRET, "本机那份要留着它（后续命令直接取用）"
    assert local_state["keyword_alias"] == prep.DEFAULT_KEYWORD_ALIAS


def test_the_snapshot_answers_which_version_served_and_how_many_reference_edges() -> None:
    """报告里的每个数字都要能拿它复核：生效版本、逐版本切片/节/引用边数。"""

    snapshot = _snapshot()
    versions = snapshot["index_versions"]

    assert versions["active"] == "v2"
    assert versions["counts"]["chunks"] == {"v1": 15, "v2": 15}
    assert versions["counts"]["sections"] == {"v1": 15, "v2": 15}
    assert versions["reference_edges"] == {"v1": 5, "v2": 9}
    assert snapshot["keyword_index"]["alias"] == prep.DEFAULT_KEYWORD_ALIAS


def test_the_snapshot_names_the_ingest_and_build_versions() -> None:
    """「v1 是摄入的产物、v2 是构建的产物」这条口径必须写在快照里，而不是只在人脑里。"""

    versions = _snapshot()["index_versions"]

    assert versions["ingest_version"] == prep.INGEST_VERSION == "v1"
    assert versions["build_version"] == prep.BUILD_VERSION == "v2"


@pytest.mark.parametrize("switch", ("orchestration_allowed", "research_qps", "research_daily"))
def test_the_pro_plan_targets_are_the_three_columns_the_spec_names(switch: str) -> None:
    """#153 §① 点名的就是这三列；它们不许悄悄变（变了两份报告的 on/off 都跑不起来）。"""

    assert switch in prep.PRO_PLAN_TARGETS

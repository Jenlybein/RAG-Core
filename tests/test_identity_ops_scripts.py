"""票 #74：三条运维命令（以及播种命令）的入参与退出码。

脚本的接缝就是 ``main(argv) -> int``：断言参数怎么被解释、打印了什么、退几。会话换成
``tests/fake_db.RecordingSession``，日志与数据库连接都换成替身，所以整套仍然离线。

退出码规则沿用共享实现：业务错误打印到 stderr 并退 1；argparse 挡下的非法入参退 2；
成功退 0。**非法入参绝不能退 0**——运维脚本静默成功比失败更难查。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.models import Tenant
from app.models.api_key import ApiKey
from app.models.key_policy import KeyPolicy
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of
from tests.fake_db import RecordingSession

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

DEMO_TENANT = "tenant_demo"
ACME_TENANT = "tenant_acme"
POLICY_ID = "11111111-1111-4111-8111-111111111111"

API_KEY_PATTERN = re.compile(r"rc_live_[0-9a-f]{32}")


def _load_script(module_name: str) -> ModuleType:
    """从文件加载脚本：``scripts*`` 不在安装包里，显式路径最可靠。

    加载本身也要成功——脚本顶层不得连接数据库、不得解析 argv（导入即安全）。
    """

    path = SCRIPTS_DIR / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"t74_script_{module_name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


class _SequenceSession(RecordingSession):
    """给刚插入的套餐行补自增主键：真实库里 id 由序列生成，假会话得自己演。"""

    def add(self, obj: object) -> None:
        if isinstance(obj, Plan) and obj.id is None:
            obj.id = len(self._rows.get(Plan, [])) + 1
        super().add(obj)


def _plan_rows() -> list[Plan]:
    rows: list[Plan] = []
    for index, code in enumerate(PLAN_CODES, start=1):
        row = plan_row_of(PLAN_PRESETS[code])
        row.id = index
        rows.append(row)
    return rows


def _tenant_row(tenant_id: str = ACME_TENANT, *, plan_id: int = 1) -> Tenant:
    return Tenant(id=tenant_id, name=tenant_id, plan_id=plan_id)


def _policy_row(tenant_id: str = ACME_TENANT) -> KeyPolicy:
    """一套最小权限的配置行：能力全关、两个形状开关取默认（开）、档位 speed。

    布尔列显式写出来，不靠列默认值：``KeyPolicy`` 的行对象在 flush 之前属性还是
    ``None``，而改配置的脚本会先按快照做自洽校验——一份「还没 flush 的 None」会让
    测试里校验出的结论与真实库里的行为不一致。
    """

    return KeyPolicy(
        id=POLICY_ID,
        tenant_id=tenant_id,
        name="default",
        allowed_kb_ids=[],
        research_allowed=False,
        hybrid_allowed=False,
        rerank_allowed=False,
        query_rewrite_allowed=False,
        retrieve_qps=3,
        retrieve_daily=500,
        research_qps=0,
        research_daily=0,
        profile="speed",
        include_parent=True,
        expand_table_rows=True,
        custom_options={},
    )


def _patch_infrastructure(
    monkeypatch: pytest.MonkeyPatch, script: ModuleType, session: RecordingSession
) -> None:
    """只换「基础设施」：会话工厂与日志开关。业务函数保持真的。"""

    monkeypatch.setattr(script, "SessionLocal", lambda: session)
    monkeypatch.setattr(script, "setup_logging", lambda: None)


@pytest.fixture
def seed_script() -> ModuleType:
    return _load_script("seed_plans")


@pytest.fixture
def create_script() -> ModuleType:
    return _load_script("create_tenant")


@pytest.fixture
def issue_script() -> ModuleType:
    return _load_script("issue_api_key")


@pytest.fixture
def change_script() -> ModuleType:
    return _load_script("change_tenant_plan")


@pytest.fixture
def update_policy_script() -> ModuleType:
    return _load_script("update_key_policy")


# ───────────────────────────────────────────────────────────────── 播种命令


def test_seed_script_seeds_the_plans_then_backfills_old_tenants(
    seed_script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _SequenceSession(tenant_ids=[DEMO_TENANT, ACME_TENANT])
    _patch_infrastructure(monkeypatch, seed_script, session)

    assert seed_script.main([]) == 0

    assert [row.code for row in session.added_of(Plan)] == list(PLAN_CODES)
    plan_ids = {row.id: row.code for row in session.added_of(Plan)}
    backfilled = {row.id: plan_ids[row.plan_id] for row in session.added_of(Tenant)}
    assert backfilled == {DEMO_TENANT: "pro", ACME_TENANT: "standard"}
    assert session.closed

    out = capsys.readouterr().out
    assert "inserted: 3" in out
    assert "created: 2" in out


def test_seed_script_reports_zero_inserted_when_the_table_is_already_seeded(
    seed_script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = RecordingSession(rows={Plan: _plan_rows()})
    _patch_infrastructure(monkeypatch, seed_script, session)

    assert seed_script.main([]) == 0

    assert session.added_of(Plan) == []
    assert "inserted: 0" in capsys.readouterr().out


# ───────────────────────────────────────────────────────────────── 开租户


def test_create_tenant_script_prints_the_tenant_and_its_default_policy(
    create_script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = RecordingSession(rows={Plan: _plan_rows()})
    _patch_infrastructure(monkeypatch, create_script, session)

    assert (
        create_script.main(
            ["--id", ACME_TENANT, "--name", "Acme 公司", "--plan", "standard", "--kb-id", "kb-1"]
        )
        == 0
    )

    tenant = session.added_of(Tenant)[0]
    policy = session.added_of(KeyPolicy)[0]
    assert (tenant.id, tenant.name, tenant.plan_id) == (ACME_TENANT, "Acme 公司", 2)
    assert policy.tenant_id == ACME_TENANT
    assert policy.allowed_kb_ids == ["kb-1"]
    assert session.closed

    out = capsys.readouterr().out
    assert f"tenant: {ACME_TENANT}" in out
    assert f"key policy: {policy.id}" in out


def test_create_tenant_script_defaults_to_the_lowest_plan(
    create_script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = RecordingSession(rows={Plan: _plan_rows()})
    _patch_infrastructure(monkeypatch, create_script, session)

    assert create_script.main(["--id", ACME_TENANT, "--name", "Acme"]) == 0

    assert session.added_of(Tenant)[0].plan_id == PLAN_CODES.index("free") + 1


@pytest.mark.parametrize(
    "argv",
    [
        ["--id", ACME_TENANT, "--name", "Acme", "--plan", "platinum"],
        ["--name", "Acme"],
        ["--id", ACME_TENANT],
    ],
)
def test_create_tenant_script_rejects_bad_arguments_with_a_non_zero_exit(
    create_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    session = RecordingSession(rows={Plan: _plan_rows()})
    _patch_infrastructure(monkeypatch, create_script, session)

    with pytest.raises(SystemExit) as exit_info:
        create_script.main(argv)

    assert exit_info.value.code == 2
    assert capsys.readouterr().err.strip(), "非法入参必须打印原因"


def test_create_tenant_script_reports_a_duplicate_tenant_on_stderr(
    create_script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = RecordingSession(rows={Plan: _plan_rows(), Tenant: [_tenant_row()]})
    _patch_infrastructure(monkeypatch, create_script, session)

    assert create_script.main(["--id", ACME_TENANT, "--name", "Acme"]) == 1

    assert "已存在" in capsys.readouterr().err
    assert session.added_of(Tenant) == []
    assert session.rollbacks == 1


def test_create_tenant_script_explains_that_the_plans_are_not_seeded_yet(
    create_script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = RecordingSession()
    _patch_infrastructure(monkeypatch, create_script, session)

    assert create_script.main(["--id", ACME_TENANT, "--name", "Acme"]) == 1

    assert "seed_plans" in capsys.readouterr().err


# ───────────────────────────────────────────────────────────────── 发 Key


def test_issue_api_key_script_prints_the_generated_plaintext_key(
    issue_script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = RecordingSession(rows={Tenant: [_tenant_row()], KeyPolicy: [_policy_row()]})
    _patch_infrastructure(monkeypatch, issue_script, session)

    assert (
        issue_script.main(
            ["--tenant-id", ACME_TENANT, "--policy-id", POLICY_ID, "--label", "订单机器人"]
        )
        == 0
    )

    issued = session.added_of(ApiKey)[0]
    assert issued.policy_id == POLICY_ID
    assert issued.label == "订单机器人"
    assert API_KEY_PATTERN.fullmatch(issued.key_value)
    assert session.closed

    out = capsys.readouterr().out
    assert issued.key_value in out


def test_issue_api_key_script_refuses_a_policy_from_another_tenant(
    issue_script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = RecordingSession(
        rows={Tenant: [_tenant_row()], KeyPolicy: [_policy_row(tenant_id="tenant_other")]}
    )
    _patch_infrastructure(monkeypatch, issue_script, session)

    assert issue_script.main(["--tenant-id", ACME_TENANT, "--policy-id", POLICY_ID]) == 1

    assert "不属于租户" in capsys.readouterr().err
    assert session.added_of(ApiKey) == []


@pytest.mark.parametrize(
    "argv",
    [
        ["--tenant-id", ACME_TENANT],
        ["--policy-id", POLICY_ID],
    ],
)
def test_issue_api_key_script_requires_both_ids(
    issue_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    session = RecordingSession(rows={Tenant: [_tenant_row()], KeyPolicy: [_policy_row()]})
    _patch_infrastructure(monkeypatch, issue_script, session)

    with pytest.raises(SystemExit) as exit_info:
        issue_script.main(argv)

    assert exit_info.value.code == 2
    assert capsys.readouterr().err.strip()


def test_issue_api_key_script_reports_a_missing_policy_on_stderr(
    issue_script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = RecordingSession(rows={Tenant: [_tenant_row()]})
    _patch_infrastructure(monkeypatch, issue_script, session)

    assert issue_script.main(["--tenant-id", ACME_TENANT, "--policy-id", POLICY_ID]) == 1

    assert "密钥配置不存在" in capsys.readouterr().err


# ───────────────────────────────────────────────────────────────── 改套餐


def test_change_plan_script_moves_the_plan(
    change_script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tenant = _tenant_row(plan_id=1)
    session = RecordingSession(rows={Plan: _plan_rows(), Tenant: [tenant]})
    _patch_infrastructure(monkeypatch, change_script, session)

    assert change_script.main(["--tenant-id", ACME_TENANT, "--plan", "pro"]) == 0

    assert tenant.plan_id == PLAN_CODES.index("pro") + 1
    assert "plan: free -> pro" in capsys.readouterr().out


@pytest.mark.parametrize("plan_code", ["platinum", "PRO", ""])
def test_change_plan_script_only_accepts_the_three_known_codes(
    change_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    plan_code: str,
) -> None:
    tenant = _tenant_row(plan_id=1)
    session = RecordingSession(rows={Plan: _plan_rows(), Tenant: [tenant]})
    _patch_infrastructure(monkeypatch, change_script, session)

    with pytest.raises(SystemExit) as exit_info:
        change_script.main(["--tenant-id", ACME_TENANT, "--plan", plan_code])

    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert tenant.plan_id == 1


def test_change_plan_script_reports_a_missing_tenant_on_stderr(
    change_script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = RecordingSession(rows={Plan: _plan_rows()})
    _patch_infrastructure(monkeypatch, change_script, session)

    assert change_script.main(["--tenant-id", "tenant_ghost", "--plan", "pro"]) == 1

    assert "租户不存在" in capsys.readouterr().err
    assert session.closed


# ───────────────────────────────────────────────────────────── 改密钥配置（#72 缺口 2）


def _update_session(*, plan_id: int = PLAN_CODES.index("standard") + 1) -> RecordingSession:
    """标准档租户 + 一套 speed 配置：balanced 可用、hybrid 允许，改得动的空间最大。"""

    return RecordingSession(
        rows={
            Plan: _plan_rows(),
            Tenant: [_tenant_row(plan_id=plan_id)],
            KeyPolicy: [_policy_row()],
        }
    )


def test_update_policy_script_writes_the_new_values(
    update_policy_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """一次改五组内容：档位、能力开关、两个形状开关、限额、数据范围。"""

    session = _update_session()
    _patch_infrastructure(monkeypatch, update_policy_script, session)

    assert (
        update_policy_script.main(
            [
                "--policy-id",
                POLICY_ID,
                "--name",
                "订单机器人",
                "--profile",
                "balanced",
                "--hybrid",
                "--no-include-parent",
                "--qps",
                "8",
                "--daily",
                "4000",
                "--kb-id",
                "kb-1",
                "--kb-id",
                "kb-2",
            ]
        )
        == 0
    )

    policy = session._rows[KeyPolicy][0]
    assert policy.name == "订单机器人"
    assert policy.profile == "balanced"
    assert policy.hybrid_allowed is True
    # 没动的字段保持原样：脚本是「改这几项」，不是「整行重写」。
    assert policy.rerank_allowed is False
    assert policy.research_allowed is False
    assert policy.include_parent is False
    assert policy.expand_table_rows is True
    assert (policy.retrieve_qps, policy.retrieve_daily) == (8, 4000)
    assert policy.allowed_kb_ids == ["kb-1", "kb-2"]
    assert session.commits == 1
    assert session.closed

    out = capsys.readouterr().out
    assert f"key policy: {POLICY_ID}" in out
    assert "profile: speed -> balanced" in out
    assert "hybrid_allowed: False -> True" in out


def test_update_policy_script_can_clear_the_kb_whitelist(
    update_policy_script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--clear-kb-ids`` 把白名单清空；空 = 一个库都不许查，不是「全部」。"""

    session = _update_session()
    policy = session._rows[KeyPolicy][0]
    policy.allowed_kb_ids = ["kb-1"]
    _patch_infrastructure(monkeypatch, update_policy_script, session)

    assert update_policy_script.main(["--policy-id", POLICY_ID, "--clear-kb-ids"]) == 0

    assert policy.allowed_kb_ids == []


@pytest.mark.parametrize(
    "argv",
    [
        # free 只允许 speed：换成 balanced 不自洽
        ["--profile", "balanced"],
        # free 的限额是 qps 3 / daily 500
        ["--qps", "4"],
        ["--daily", "501"],
        # free 没有混合检索能力
        ["--hybrid"],
    ],
)
def test_update_policy_script_refuses_an_inconsistent_configuration(
    update_policy_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """与开租户同一份自洽校验：不自洽的请求打一行原因、退 1、且**一行都不写**。"""

    session = _update_session(plan_id=PLAN_CODES.index("free") + 1)
    _patch_infrastructure(monkeypatch, update_policy_script, session)

    assert update_policy_script.main(["--policy-id", POLICY_ID, *argv]) == 1

    assert capsys.readouterr().err.strip(), "不自洽必须打印原因"
    policy = session._rows[KeyPolicy][0]
    assert policy.profile == "speed"
    assert (policy.retrieve_qps, policy.retrieve_daily) == (3, 500)
    assert policy.hybrid_allowed is False
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["--profile", "balanced"],  # 少了 --policy-id
        ["--policy-id", POLICY_ID],  # 一个字段都没给
        ["--policy-id", POLICY_ID, "--profile", "platinum"],
        ["--policy-id", POLICY_ID, "--kb-id", "kb-1", "--clear-kb-ids"],
    ],
)
def test_update_policy_script_rejects_bad_arguments_with_a_non_zero_exit(
    update_policy_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    session = _update_session()
    _patch_infrastructure(monkeypatch, update_policy_script, session)

    with pytest.raises(SystemExit) as exit_info:
        update_policy_script.main(argv)

    assert exit_info.value.code == 2
    assert capsys.readouterr().err.strip()
    assert session.commits == 0


def test_update_policy_script_reports_a_missing_policy_on_stderr(
    update_policy_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = RecordingSession(rows={Plan: _plan_rows(), Tenant: [_tenant_row()]})
    _patch_infrastructure(monkeypatch, update_policy_script, session)

    assert update_policy_script.main(["--policy-id", POLICY_ID, "--qps", "2"]) == 1

    assert "密钥配置不存在" in capsys.readouterr().err
    assert session.commits == 0
    assert session.rollbacks == 1


def test_update_policy_script_help_lists_the_editable_fields(
    update_policy_script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        update_policy_script.main(["--help"])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for flag in (
        "--policy-id",
        "--name",
        "--profile",
        "--hybrid",
        "--include-parent",
        "--qps",
        "--daily",
        "--kb-id",
        "--clear-kb-ids",
    ):
        assert flag in out, f"--help 里看不到 {flag}"

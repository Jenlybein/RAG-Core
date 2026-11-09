"""票 #80：引导第一个管理员的运维脚本。

脚本的接缝就是 ``main(argv) -> int``：只看参数怎么被解释、落了哪一行、退几。会话换成
``RecordingSession``，日志换成空实现，所以整套仍然离线。

要守三件事：口令**只以哈希入库**、管理员**不属于任何租户**、用户名被占用或口令为空时
**退非零**（运维脚本静默成功比失败更难查）。
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.core import passwords
from app.models.account import Account
from tests.fake_db import RecordingSession

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
PASSWORD = "first-admin-password"


def _load_script() -> ModuleType:
    """从文件加载脚本：``scripts*`` 不在安装包里，显式路径最可靠。

    加载本身也要成功——脚本顶层不得连接数据库、不得解析 argv。
    """

    path = SCRIPTS_DIR / "create_admin.py"
    spec = importlib.util.spec_from_file_location("t80_create_admin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = _load_script()
    session = RecordingSession()
    monkeypatch.setattr(module, "SessionLocal", lambda: session)
    monkeypatch.setattr(module, "setup_logging", lambda: None)
    module._test_session = session  # type: ignore[attr-defined]
    return module


def test_the_first_admin_is_created_with_a_hash_only_and_no_tenant(
    script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    status = script.main(["--username", "admin", "--password", PASSWORD])

    assert status == 0
    accounts = script._test_session.added_of(Account)  # type: ignore[attr-defined]
    assert len(accounts) == 1
    account = accounts[0]
    assert account.username == "admin"
    assert account.role == "admin"
    assert account.status == "active"
    assert account.tenant_id is None
    assert account.password_hash != PASSWORD
    assert PASSWORD not in account.password_hash
    assert passwords.verify_password(PASSWORD, account.password_hash) is True

    printed = capsys.readouterr().out
    assert "role=admin" in printed
    assert PASSWORD not in printed


def test_the_password_may_come_from_stdin(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script.sys, "stdin", io.StringIO(PASSWORD + "\n"))

    status = script.main(["--username", "admin", "--password-stdin"])

    assert status == 0
    account = script._test_session.added_of(Account)[0]  # type: ignore[attr-defined]
    assert passwords.verify_password(PASSWORD, account.password_hash) is True


def test_an_existing_username_is_rejected_without_writing_anything(
    script: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = Account(
        id="existing",
        tenant_id=None,
        username="admin",
        password_hash=passwords.hash_password("old"),
        role="admin",
        status="active",
    )
    script._test_session._rows = {Account: [existing]}  # type: ignore[attr-defined]

    status = script.main(["--username", "admin", "--password", PASSWORD])

    assert status == 1
    assert script._test_session.added == []  # type: ignore[attr-defined]
    assert "already exists" in capsys.readouterr().err


def test_an_empty_password_is_rejected(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(script.sys, "stdin", io.StringIO("\n"))

    status = script.main(["--username", "admin", "--password-stdin"])

    assert status == 1
    assert script._test_session.added == []  # type: ignore[attr-defined]
    assert "must not be empty" in capsys.readouterr().err


def test_an_empty_username_is_rejected(script: ModuleType) -> None:
    status = script.main(["--username", "   ", "--password", PASSWORD])

    assert status == 1
    assert script._test_session.added == []  # type: ignore[attr-defined]


def test_missing_required_arguments_exit_two(script: ModuleType) -> None:
    with pytest.raises(SystemExit) as exit_info:
        script.main([])

    assert exit_info.value.code == 2

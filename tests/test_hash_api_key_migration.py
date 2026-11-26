"""「API Key 只存哈希」那条迁移（``c7a1e9d3b5f4``）的验收。

它和前面那些纯建表迁移不是一类：**它会删一列，而且回填要逐行算 scrypt**。所以这里守四件事：

1. 没有 ``-x confirm_drop=1`` 就**一行都不许动**（拒绝执行，而不是"先加了列再说"）；
2. 有确认时才回填：``key_hash`` 能被 ``verify_password`` 验过、``key_tail`` 是末 5 位；
3. ``emit_keys`` 写出的新串粘得回去（形状是 ``rc_live_<id>.<secret>``），因为存量 Key 换串
   靠的就是这一份；
4. ``downgrade()`` 明确抛错——明文已经不在了，任何"反向迁移"都只能造出一列空值。

全部离线：``op`` 与 ``bind`` 都是记录器，不需要 PostgreSQL。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from app.core.passwords import verify_password

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

#: 三条存量行：两条正常的旧形状，一条是手修库造出来的坏数据。
GOOD_ROWS = [
    {
        "id": "11111111-1111-4111-8111-111111111111",
        "tenant_id": "tenant_alpha",
        "key_value": "rc_live_" + "a" * 32,
        "label": "alpha-bot",
    },
    {
        "id": "22222222-2222-4222-8222-222222222222",
        "tenant_id": "tenant_beta",
        "key_value": "rc_live_" + "0123456789abcdef0123456789ab9f2c",
        "label": None,
    },
]


class _FakeBind:
    """只认两种语句：``SELECT``（回存量行）与 ``UPDATE``（记录回填参数）。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(statement)
        if sql.strip().upper().startswith("SELECT"):
            return _Mappings(self._rows)
        assert params is not None, "UPDATE 必须带参数"
        self.updates.append((sql, dict(params)))
        return None


class _Mappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_Mappings":
        return self

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _FakeOp:
    """记录 DDL；``get_context().config.cmd_opts.x`` 模拟 ``alembic -x``。"""

    def __init__(self, *, bind: _FakeBind, x_args: list[str]) -> None:
        self._bind = bind
        self._x_args = x_args
        self.added: list[tuple[str, str]] = []
        self.dropped: list[str] = []
        self.alterations: list[tuple[str, bool]] = []

    def get_context(self) -> Any:
        return SimpleNamespace(config=SimpleNamespace(cmd_opts=SimpleNamespace(x=self._x_args)))

    @property
    def bind(self) -> _FakeBind:
        """用例读回填参数的入口（与 ``get_bind()`` 同一份记录器）。"""

        return self._bind

    def get_bind(self) -> _FakeBind:
        return self._bind

    def f(self, name: str) -> str:
        return name

    def add_column(self, table: str, column: sa.Column) -> None:
        self.added.append((table, column.name))

    def drop_index(self, index_name: str, table_name: str | None = None, **_kw: Any) -> None:
        self.dropped.append(f"index:{table_name}.{index_name}")

    def drop_column(self, table: str, column: str) -> None:
        self.dropped.append(f"column:{table}.{column}")

    def alter_column(self, table: str, column: str, **kwargs: Any) -> None:
        self.alterations.append((column, bool(kwargs.get("nullable", True))))


def _load() -> ModuleType:
    path = next(VERSIONS_DIR.glob("*_hash_api_key_secrets.py"))
    spec = importlib.util.spec_from_file_location("t_hash_api_key_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(x_args: list[str], rows: list[dict[str, Any]]) -> tuple[_FakeOp, ModuleType]:
    module = _load()
    recorder = _FakeOp(bind=_FakeBind(rows), x_args=x_args)
    module.op = recorder
    return recorder, module


def test_without_confirm_it_refuses_and_touches_nothing() -> None:
    """少一个参数就整体拒绝：**连加列都不做**，否则半截迁移会在库上留下两列空值。"""

    recorder, module = _run([], GOOD_ROWS)

    with pytest.raises(RuntimeError, match="confirm_drop"):
        module.upgrade()

    assert recorder.added == []
    assert recorder.dropped == []
    assert recorder.alterations == []


def test_it_backfills_a_verifiable_hash_and_the_last_five_characters() -> None:
    recorder, module = _run(["confirm_drop=1"], GOOD_ROWS)

    module.upgrade()

    assert recorder.added == [("api_keys", "key_hash"), ("api_keys", "key_tail")]
    # 先删索引、再删列，最后才收紧 NOT NULL：顺序反了会在存量行上直接失败。
    assert recorder.dropped == [
        "index:api_keys.ix_api_keys_key_value",
        "column:api_keys.key_value",
    ]
    assert recorder.alterations == [("key_hash", False), ("key_tail", False)]

    updates = {params["id"]: params for _sql, params in recorder.bind.updates}
    assert set(updates) == {row["id"] for row in GOOD_ROWS}

    for row in GOOD_ROWS:
        secret = row["key_value"][len("rc_live_") :]
        params = updates[row["id"]]
        # 哈希是**能被校验的那一份**：迁移里的算法参数不能与运行时漂移。
        assert params["key_hash"].startswith("scrypt$16384$8$1$")
        assert verify_password(secret, params["key_hash"])
        assert params["key_tail"] == secret[-5:]
        # 末 5 位单独存，是为了让面板能显示 rc_live_…xxxxx；它不该是整串。
        assert len(params["key_tail"]) == 5


def test_emit_keys_writes_new_strings_that_point_at_the_same_row(tmp_path: Path) -> None:
    """导出的新串必须能"粘回"同一行：``rc_live_<id>.<原 secret>``。"""

    emitter = tmp_path / "keys-once.json"
    _recorder, module = _run(["confirm_drop=1", f"emit_keys={emitter}"], GOOD_ROWS)

    module.upgrade()

    emitted = json.loads(emitter.read_text(encoding="utf-8"))
    assert [item["key_id"] for item in emitted] == [row["id"] for row in GOOD_ROWS]
    for item, row in zip(emitted, GOOD_ROWS, strict=True):
        secret = row["key_value"][len("rc_live_") :]
        assert item["key_value"] == f"rc_live_{row['id']}.{secret}"
        assert item["tenant_id"] == row["tenant_id"]


def test_it_refuses_to_guess_a_broken_legacy_value() -> None:
    """坏数据必须先有人看：猜一个"看起来能用"的 secret，等于让那把 Key 永久失效。"""

    broken = [
        {
            "id": "33333333-3333-4333-8333-333333333333",
            "tenant_id": "t",
            "key_value": "garbage",
            "label": None,
        }
    ]
    _recorder, module = _run(["confirm_drop=1"], broken)

    with pytest.raises(RuntimeError, match="33333333-3333-4333-8333-333333333333"):
        module.upgrade()


def test_downgrade_is_refused() -> None:
    """明文不在了：重建一列空值会让人以为回滚成功，而每一把 Key 都突然不可用。"""

    _recorder, module = _run([], GOOD_ROWS)

    with pytest.raises(NotImplementedError, match="不可回滚"):
        module.downgrade()

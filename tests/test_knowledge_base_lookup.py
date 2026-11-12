"""票 #76 隔离收口：业务路径上不再存在「按 id 取库、再比租户」的形状。

这条形状的危险不在它写错了，而在它**可选**：下一处调用点只做前半句就完事，读出来的
别家库看起来一切正常。所以本票把它从仓储层删掉，换成一个租户是强制参数的入口，并在这里
钉住三件事：

1. 不带租户的「取一行库」函数**不存在**（删掉的那个不会悄悄回来）；
2. 带租户的那个把租户过滤写在 **SQL 里**，不是拿回行来再比——比一次就多一次忘的机会；
3. 唯一的可见性守卫调用的是它，且跨租户 / 缺失同一个码。

接缝都是**纯函数**：一个假会话，不碰 PostgreSQL。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.exceptions import AppError, ErrorCode
from app.repositories import knowledge_base_repository as kb_repo
from app.services.knowledge_base_access import ensure_visible


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class _RecordingSession:
    """只记下 SQL 与预置返回行的假会话（不实现 ``get``：那条路已经不该存在）。"""

    def __init__(self, row: Any = None) -> None:
        self._row = row
        self.statements: list[str] = []

    def execute(self, statement: Any) -> _Result:
        compiled = statement.compile(compile_kwargs={"literal_binds": True})
        self.statements.append(str(compiled))
        return _Result(self._row)

    def get(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - 访问即失败
        raise AssertionError("这里不该再按主键裸取：取库一律带租户")


# ───────────────────────────────────────────── 不带租户的取库函数已经不存在


def test_the_repository_no_longer_exposes_a_tenant_less_lookup() -> None:
    """``get_by_id`` 被删掉，且没有换个名字复活成「返回库行」的函数。

    第 11 步票 #127 多了一个**异步面** ``get_by_id_for_tenant``（检索链路走它），
    票 #132 起上传登记也走同一个异步面。它同样把租户写在名字里、写在签名里——两个面
    都是租户强制，所以这条守卫的口径不变：**凡是按 id 取库行的函数，名字里必须带租户**。
    """

    assert not hasattr(kb_repo, "get_by_id"), "按 id 裸取库的函数必须保持删除状态"

    lookup_names = {name for name in dir(kb_repo) if "get_by_id" in name}
    assert lookup_names == {"get_by_id_for_tenant", "get_by_id_for_tenant"}, (
        "取库只有带租户的两个入口（同步 / 异步孪生：检索链路与上传登记走异步那个）"
    )


@pytest.mark.parametrize(
    "lookup", [kb_repo.get_by_id_for_tenant, kb_repo.get_by_id_for_tenant]
)
def test_the_tenant_is_a_required_keyword_argument(lookup: Any) -> None:
    parameters = inspect.signature(lookup).parameters

    tenant = parameters["tenant_id"]
    assert tenant.kind is inspect.Parameter.KEYWORD_ONLY, "关键字强制，读调用点就知道带了租户"
    assert tenant.default is inspect.Parameter.empty, "无默认值：忘传就报错，不会悄悄查全表"


def test_the_security_relevant_default_lands_on_the_restricted_side() -> None:
    """这一层只守一件事：缺省构造出来的配置是「受限」的，豁免必须显式写出来。"""

    from app.models.key_policy import KeyPolicySnapshot

    assert KeyPolicySnapshot.__dataclass_fields__["unrestricted_scope"].default is False


# ───────────────────────────────────────────── 租户过滤在 SQL 里


def test_the_lookup_filters_on_id_and_tenant_in_one_statement() -> None:
    """一次查询同时钉住 ``id`` 与 ``tenant_id``：拿回行再比就多一次会忘的机会。"""

    session = _RecordingSession(row=None)

    assert kb_repo.get_by_id_for_tenant(session, "kb-1", tenant_id="tenant-a") is None

    assert len(session.statements) == 1
    sql = session.statements[0]
    assert "knowledge_bases.id = 'kb-1'" in sql
    assert "knowledge_bases.tenant_id = 'tenant-a'" in sql


@pytest.mark.asyncio
async def test_the_async_lookup_emits_the_very_same_statement() -> None:
    """异步面与同步面**共用一条语句**（第 11 步票 #127）：租户过滤不会在两面之间漂移。"""

    from tests.fake_db import AsyncRecordingSession

    class _SameShapeSyncSession(_RecordingSession):
        """与异步替身记同一份东西：``str(statement)``（不带字面量绑定）。"""

        def execute(self, statement: Any) -> _Result:
            self.statements.append(str(statement))
            return _Result(self._row)

    sync_session = _SameShapeSyncSession(row=None)
    kb_repo.get_by_id_for_tenant(sync_session, "kb-1", tenant_id="tenant-a")

    async_session = AsyncRecordingSession()
    assert (
        await kb_repo.get_by_id_for_tenant(async_session, "kb-1", tenant_id="tenant-a")
        is None
    )

    assert len(async_session.statements) == 1
    assert async_session.statements[0] == sync_session.statements[0]


def test_a_visible_row_is_returned_as_is() -> None:
    row = object()

    assert kb_repo.get_by_id_for_tenant(_RecordingSession(row), "kb-1", tenant_id="t") is row


# ───────────────────────────────────────────── 唯一的可见性守卫走的就是它


def test_ensure_visible_asks_the_tenant_scoped_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def _lookup(_db: Any, kb_id: str, *, tenant_id: str) -> Any:
        calls.append((kb_id, tenant_id))
        return None

    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", _lookup)

    with pytest.raises(AppError) as excinfo:
        ensure_visible(object(), tenant_id="tenant-a", kb_id="kb-1")

    assert excinfo.value.code is ErrorCode.KNOWLEDGE_BASE_NOT_FOUND
    assert calls == [("kb-1", "tenant-a")], "守卫把租户一起递进去，而不是自己再比一次"


def test_ensure_visible_returns_the_visible_row(monkeypatch: pytest.MonkeyPatch) -> None:
    row = object()
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", lambda _db, _kb, *, tenant_id: row)

    assert ensure_visible(object(), tenant_id="tenant-a", kb_id="kb-1") is row


# ───────────────────────────────────────────── 运维脚本的归属查询：只回字符串


def test_the_ops_only_lookup_answers_ownership_and_nothing_else() -> None:
    """它是唯一不带租户的函数，因此刻意只回租户标识——拿不到行，也就借不去读别人的库。"""

    class _OwnerSession:
        def __init__(self, row: Any) -> None:
            self._row = row

        def get(self, _model: Any, _primary_key: Any) -> Any:
            return self._row

    assert kb_repo.get_owner_tenant_id(_OwnerSession(SimpleNamespace(tenant_id="t-a")), "kb") == "t-a"
    assert kb_repo.get_owner_tenant_id(_OwnerSession(None), "kb") is None

    doc = kb_repo.get_owner_tenant_id.__doc__ or ""
    assert "运维" in doc, "要写明它只给运维脚本用"
    assert "不得调用" in doc, "要写明数据面不许用它"

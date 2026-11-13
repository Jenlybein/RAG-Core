"""票 #124 / #130：**一个请求一笔事务**——审计与业务写入要么都有、要么都没有。

接缝只有一条：真实应用 + ``TestClient``，只看 HTTP 响应体与「库里到底留下了什么」。
存储换成 ``TransactionalSession``（``tests/fake_db.py``）：它有事务语义，提交才落库、
回滚撤销这一笔里的全部写入——没有它，「整笔回滚」在离线套件里根本无从断言。它被
``AsyncSessionAdapter`` 包一层交给 ``get_async_db``：票 #130 起控制面写路径全部是
``AsyncSession``，而断言仍然打在**同一个**替身上（``commits`` / ``rollbacks`` / ``_rows``）。

选三条写路径做验收，各自代表一类「业务行 + 账本行」：

* **上传**（``POST /console/knowledge-bases/{kb}/documents``）：它曾经是**唯一**一条把
  审计排除在业务事务之外的写路径——``document_service.upload`` 自己提交登记，审计只能
  等它之后另起一笔（#124 修掉的那条窗口）；
* **改套餐**（``PATCH /console/plans/{id}``）：改一行配置 + 一条带前后快照的账本；
* **签发 Key**（``POST /console/keys``）：一次请求写**三行**（配置、Key、账本），任何一行
  失败都必须是零行。

每组两个用例：成功时整条请求**只提交一次**、行都在；审计写不进去时业务行也不落库，
响应仍是标准的三段式 500/50000（错误码与形状不变）。
"""

from __future__ import annotations

from typing import Any

import asyncio
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import app.main as main
from app.api import console_deps
from app.core.console_auth import ConsoleIdentity
from app.db import async_session as async_session_module
from app.models.api_key import ApiKey
from app.models.audit_log import AuditLog
from app.models.document import Document
from app.models.key_policy import KeyPolicy
from app.models.knowledge_base import KnowledgeBase
from app.models.plan import PLAN_PRESETS, Plan, plan_row_of, plan_spec_of
from app.models.tenant import Tenant
from app.repositories import knowledge_base_repository
from app.services import console_kb_service, console_service
from tests.fake_db import AsyncSessionAdapter, MemoryPlanCache, TransactionalSession

TENANT_ID = "tenant_atomic"
ACCOUNT_ID = "00000000-0000-4000-8000-000000000124"
ADMIN_ID = "00000000-0000-4000-8000-000000000125"
KB_ID = "kb-atomic"
PLAN_ID = 2
#: 改套餐写进去的库数上限：比 ``pro`` 的出厂值大 3，于是「改前 ≠ 改后」一定成立。
NEW_MAX_KB = PLAN_PRESETS["pro"].max_kb + 3
UPLOAD_PATH = f"/console/knowledge-bases/{KB_ID}/documents"
WRITE_HEADERS = {"x-requested-with": "XMLHttpRequest"}


def _plan() -> Plan:
    row = plan_row_of(PLAN_PRESETS["pro"])
    row.id = PLAN_ID
    return row


def _kb() -> KnowledgeBase:
    return KnowledgeBase(
        id=KB_ID,
        tenant_id=TENANT_ID,
        name="atomic-kb",
        description=None,
        split_strategy="structure",
        allowed_formats=["markdown"],
        settings={"chunk_size": 512},
    )


def _admin() -> ConsoleIdentity:
    """管理员身份：套餐与 Key 这两条写路径走管理员门。"""

    return ConsoleIdentity(account_id=ADMIN_ID, username="admin", role="admin", tenant_id=None)


def _user() -> ConsoleIdentity:
    """租户账号：上传走租户门。"""

    return ConsoleIdentity(
        account_id=ACCOUNT_ID, username="atomic", role="user", tenant_id=TENANT_ID
    )


@pytest.fixture()
def console(monkeypatch: pytest.MonkeyPatch) -> Any:
    """真应用 + 有事务语义的假会话；只替换存储、套餐缓存与身份三件事。"""

    plan = _plan()
    kb = _kb()
    session = TransactionalSession(
        rows={
            Tenant: [Tenant(id=TENANT_ID, name="Atomic", plan_id=PLAN_ID)],
            Plan: [plan],
            KnowledgeBase: [kb],
            Document: [],
            AuditLog: [],
            ApiKey: [],
            KeyPolicy: [],
        }
    )
    cache = MemoryPlanCache()
    # 套餐缓存自票 #129 起是异步的：预置一个命中项也要 ``await``（同步调用只会丢掉协程）。
    asyncio.run(cache.set(PLAN_ID, plan_spec_of(plan)))

    async def _visible_kb_async(*_a: Any, **_k: Any) -> Any:
        return kb

    # 控制面上传复用的是数据面那个 ``document_service.upload``，它已经是异步栈（票 #132），
    # 所以库可见性读的是异步孪生。整笔事务仍然只有一笔（控制面路由上的异步事务依赖）。
    monkeypatch.setattr(knowledge_base_repository, "get_by_id_for_tenant", _visible_kb_async)
    # 身份：上传与签发 Key 走**租户**门（票 #130 起 Key 也挂在 ``require_console_tenant_write``
    # 上：管理员没有租户，进这两条路径一律 20011）；改套餐走管理员门。两条门互不重叠，
    # 按路径分派即可。
    identities = {UPLOAD_PATH: _user(), "/console/keys": _user()}

    async def _identity_for(request: Request) -> ConsoleIdentity:
        return identities.get(request.url.path, _admin())

    main.app.dependency_overrides[async_session_module.get_async_db] = lambda: AsyncSessionAdapter(
        session
    )
    main.app.dependency_overrides[console_deps.get_plan_cache] = lambda: cache
    main.app.dependency_overrides[console_deps.get_console_account] = _identity_for
    try:
        # ``raise_server_exceptions=False``：未预期异常要能在**响应**上被看见
        # （500 + 50000）。默认的 True 会把异常直接抛给测试，看到的是堆栈而不是响应壳。
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield client, session
    finally:
        main.app.dependency_overrides.clear()


def _upload(client: TestClient) -> Any:
    return client.post(
        UPLOAD_PATH,
        json={"title": "atomic", "content": "正文"},
        headers=WRITE_HEADERS,
    )


def _patch_plan(client: TestClient) -> Any:
    return client.patch(
        f"/console/plans/{PLAN_ID}?dry_run=false",
        json={"max_kb": NEW_MAX_KB},
        headers=WRITE_HEADERS,
    )


def _issue_key(client: TestClient) -> Any:
    return client.post(
        "/console/keys",
        json={
            "label": "atomic-key",
            "policy": {"profile": "speed", "retrieve_qps": 1, "retrieve_daily": 10},
        },
        headers=WRITE_HEADERS,
    )


def _failing_audit(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("audit store unavailable")


def test_document_and_its_audit_row_are_committed_by_one_transaction(console: Any) -> None:
    client, session = console

    response = _upload(client)

    assert response.status_code == 200
    assert response.json()["code"] == 0
    assert session.commits == 1, "一个请求一笔事务：登记与账本一起提交，不是各提交一次"
    assert len(session._rows.get(Document, [])) == 1
    assert len(session._rows.get(AuditLog, [])) == 1


def test_a_failing_audit_write_rolls_back_the_document_too(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审计写不进去时，文档行**也不许**留下：要么都有、要么都没有。"""

    client, session = console

    monkeypatch.setattr(console_kb_service, "append_audit", _failing_audit)

    response = _upload(client)

    # 响应形状与错误码不变：未预期异常仍是 HTTP 500 + 50000 + 三段式响应壳。
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == 50000
    assert body["msg"]
    assert body["data"] is None
    # 整笔回滚：文档行与账本行都不在，而且确实是回滚掉的（不是没写）。
    assert session._rows.get(Document, []) == []
    assert session._rows.get(AuditLog, []) == []
    assert session.rollbacks == 1


def test_a_plan_patch_and_its_audit_row_are_committed_by_one_transaction(console: Any) -> None:
    """改套餐：业务行（套餐）与账本行（带前后快照）在同一笔事务里落定。"""

    client, session = console

    response = _patch_plan(client)

    assert response.status_code == 200, response.text
    assert response.json()["code"] == 0
    assert session.commits == 1
    assert session._rows[Plan][0].max_kb == NEW_MAX_KB
    audits = session._rows.get(AuditLog, [])
    assert [row.action for row in audits] == ["plan.update"]
    assert audits[0].before_snapshot["max_kb"] != audits[0].after_snapshot["max_kb"]


def test_a_failing_audit_write_rolls_back_the_plan_patch_too(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """账本写不进去 → 整笔回滚（不是「提交了一半」，也不是「改完再补账本」）。

    断言打在 ``commits`` / ``rollbacks`` 上而不是套餐行的属性上：假会话回滚的是**行集合**
    与写入记录，对象本身早被服务层改过了（真库才会把那条 ``UPDATE`` 一并撤销）。「没有提交、
    回滚了一次」正是「库里什么都不会留下」在离线套件里的可观测形态。
    """

    client, session = console

    monkeypatch.setattr(console_service, "append_audit", _failing_audit)

    response = _patch_plan(client)

    assert response.status_code == 500
    assert response.json()["code"] == 50000
    assert session.commits == 0, "整笔回滚：这次改动一次都没有提交"
    assert session._rows.get(AuditLog, []) == []
    assert session.rollbacks == 1


def test_issuing_a_key_writes_three_rows_in_one_transaction(console: Any) -> None:
    """签发：配置 + Key + 账本三行一起提交（少一行就是「Key 发出去了、配置没建」）。"""

    client, session = console

    response = _issue_key(client)

    assert response.status_code == 200, response.text
    assert response.json()["code"] == 0
    assert session.commits == 1
    assert len(session._rows.get(KeyPolicy, [])) == 1
    assert len(session._rows.get(ApiKey, [])) == 1
    assert [row.action for row in session._rows.get(AuditLog, [])] == ["key.issue"]


def test_a_failing_audit_write_rolls_back_the_issued_key_and_policy_too(
    console: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, session = console

    monkeypatch.setattr(console_service, "append_audit", _failing_audit)

    response = _issue_key(client)

    assert response.status_code == 500
    assert response.json()["code"] == 50000
    assert session._rows.get(KeyPolicy, []) == []
    assert session._rows.get(ApiKey, []) == []
    assert session._rows.get(AuditLog, []) == []
    assert session.rollbacks == 1


def test_the_suite_never_reaches_a_real_broker() -> None:
    """**离线是构造上的属性**：投递缝被 conftest 的 autouse 夹具换成了记账版。

    这条用例是赔出来的：漏桩的用例在开发机上会静默连上本机 Redis、一路绿，到了没有
    Redis 的 CI 才变成 500 + `Connection refused`（票 #137 的 CI 就这么红的）。
    这里直接断言「两处投递缝现在都不是真的那个」——它抓的是**将来有人把夹具删掉**。
    """

    from app.services import console_kb_service, document_service, index_dispatch

    for module in (document_service, console_kb_service, index_dispatch):
        target = module.enqueue_document
        assert target.__name__ == "_record_dispatch", (
            f"{module.__name__}.enqueue_document 又变回真的了："
            "默认套件会在开发机上静默连 Broker，只在 CI 上红"
        )

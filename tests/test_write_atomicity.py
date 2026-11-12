"""票 #124：**一个请求一笔事务**——审计与业务写入要么都有、要么都没有。

接缝只有一条：真实应用 + ``TestClient``，只看 HTTP 响应体与「库里到底留下了什么」。
存储换成 ``TransactionalSession``（``tests/fake_db.py``）：它有事务语义，提交才落库、
回滚撤销这一笔里的全部写入——没有它，「整笔回滚」在离线套件里根本无从断言。

选上传这条路径做验收，是因为它是**唯一一条曾经把审计排除在业务事务之外**的写路径：
``document_service.upload`` 自己提交登记，审计只能等它之后另起一笔。本票把提交收口到
请求边界，那条窗口随之消失——这两个用例就是它的证据：

* 成功时：文档行与账本行同时落库，整条请求**只提交一次**；
* 审计写不进去时：文档行也不落库，响应仍是标准的三段式 500/50000（错误码与形状不变）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.api import console_deps
from app.core.console_auth import ConsoleIdentity
from app.db import session as session_module
from app.models.audit_log import AuditLog
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.models.plan import PLAN_PRESETS, Plan, plan_row_of, plan_spec_of
from app.models.tenant import Tenant
from app.repositories import knowledge_base_repository
from app.services import console_kb_service
from tests.fake_db import TransactionalSession

TENANT_ID = "tenant_atomic"
ACCOUNT_ID = "00000000-0000-4000-8000-000000000124"
KB_ID = "kb-atomic"
PLAN_ID = 2
UPLOAD_PATH = f"/console/knowledge-bases/{KB_ID}/documents"
WRITE_HEADERS = {"x-requested-with": "XMLHttpRequest"}


class _MemoryPlanCache:
    """套餐缓存的内存假实现：``get/set/delete`` 就是全部接缝。"""

    def __init__(self) -> None:
        self.items: dict[int, Any] = {}

    def get(self, plan_id: int) -> Any:
        return self.items.get(plan_id)

    def set(self, plan_id: int, plan: Any) -> None:
        self.items[plan_id] = plan

    def delete(self, plan_id: int) -> None:
        self.items.pop(plan_id, None)


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


@pytest.fixture()
def console(monkeypatch: pytest.MonkeyPatch) -> Any:
    """真应用 + 有事务语义的假会话；只替换存储与套餐缓存两个外部依赖。"""

    plan = _plan()
    kb = _kb()
    session = TransactionalSession(
        rows={
            Tenant: [Tenant(id=TENANT_ID, name="Atomic", plan_id=PLAN_ID)],
            Plan: [plan],
            KnowledgeBase: [kb],
            Document: [],
            AuditLog: [],
        }
    )
    cache = _MemoryPlanCache()
    cache.set(PLAN_ID, plan_spec_of(plan))

    monkeypatch.setattr(
        knowledge_base_repository, "get_by_id_for_tenant", lambda *_a, **_k: kb
    )

    async def _visible_kb_async(*_a: Any, **_k: Any) -> Any:
        return kb

    # 控制面上传复用的是数据面那个 ``document_service.upload``，它已经是异步栈（票 #132），
    # 所以库可见性读的是异步孪生。整笔事务仍然只有一笔（控制面路由上的同步事务依赖）。
    monkeypatch.setattr(
        knowledge_base_repository, "get_by_id_for_tenant_async", _visible_kb_async
    )
    main.app.dependency_overrides[session_module.get_db] = lambda: session
    main.app.dependency_overrides[console_deps.get_plan_cache] = lambda: cache
    main.app.dependency_overrides[console_deps.get_console_account] = lambda: ConsoleIdentity(
        account_id=ACCOUNT_ID, username="atomic", role="user", tenant_id=TENANT_ID
    )
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

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(console_kb_service, "append_audit", _explode)

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

"""票 #151：MCP 第三个工具 ``rag_research``——签名、与门禁的接线、三种错误的翻译。

这一层只验**工具本身**：它递什么、不判什么、失败怎么表达。判定序列与状态机分别在
``test_research_entry.py``（HTTP 门禁）与 ``test_research_state_machine.py`` 里逐条钉住；
本文件要证明的是「MCP 这一侧**没有第二份**」——同一份 ``gated_research``、同一份
``ResearchRequest`` 校验、同一套错误码。

三类失败在这里是三种不同的东西（票 #151 的 AC 把它们并列，恰恰因为它们容易被做成一类）：

| 类别 | 例子 | 表达 | 有没有业务码 |
| --- | --- | --- | --- |
| 业务失败 | 越权库 / 没有 Research 授权 / 限流 / 额度用尽 | 工具执行失败（``isError``） | 有（``20008`` / ``20013`` / ``20005`` / ``20014``） |
| 参数非法 | 缺 ``kb_ids``、多给 ``rounds`` | **工具参数校验**失败（``isError``，文本来自调用参数） | 没有——它根本没走到业务层 |
| 内部异常 | 门禁之外的崩溃 | 交给 MCP 运行时（不冒充业务码） | 没有 |
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp.utilities.lifespan import combine_lifespans

from app.core.auth import KeyContext
from app.core.exceptions import AppError, ErrorCode
from app.mcp_server import create_mcp_server
from tests.fake_db import AsyncSessionAdapter, EmptySession

MCP_PATH = "/mcp/"
JSONRPC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
#: 门禁替身交回的身份。它只用来证明「工具把判定结论原样递下去」，不代表任何业务规则。
KEY = KeyContext(tenant_id="tenant_mcp_research", key_id="key-under-test")
#: 业务码出现在工具执行失败的文本里（``<code>|<msg>``，与既有双入口测试同形）。
BUSINESS_CODE = re.compile(r"(\d{5})\|")


class _FakeSession(EmptySession):
    """只回答「关没关」的会话替身（工具在 ``finally`` 里必须关掉它）。"""

    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _tools() -> dict[str, Any]:
    async def _retrieve(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def _list_kbs(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    mcp = create_mcp_server(
        get_async_db=lambda: AsyncSessionAdapter(_FakeSession()),
        retrieve=_retrieve,
        list_kbs=_list_kbs,
    )
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def test_tools_list_grows_from_two_to_three() -> None:
    """``tools/list`` 从两个变三个，第三个与 HTTP 请求体同形（``kb_ids`` + ``query``）。"""

    tools = _tools()

    assert set(tools) == {"rag_retrieve", "rag_list_knowledge_bases", "rag_research"}
    schema = getattr(tools["rag_research"], "parameters", {}) or {}
    assert set(schema["properties"]) == {"kb_ids", "query"}
    assert schema.get("required") == ["kb_ids", "query"]


def test_the_description_states_the_cost_and_points_at_rag_retrieve() -> None:
    """描述与参数描述都要写明：约 30 秒、有额外模型成本、普通问题先用 ``rag_retrieve``。

    模型的工具选择完全读这两段文字（票 #138 用户故事 29）：描述里不写成本，它就会把
    一次 30 秒、3 次模型调用的调用当成普通检索来用。
    """

    tool = _tools()["rag_research"]
    description = tool.description or ""

    assert "30 秒" in description, "描述里必须写明耗时"
    assert "模型" in description and "成本" in description, "描述里必须写明额外模型成本"
    assert "rag_retrieve" in description, "描述里必须指回普通检索"

    query = tool.parameters["properties"]["query"]
    assert "rag_retrieve" in (query.get("description") or ""), "参数描述里再提一次"


# ─────────────────────────────────────────────── 一条真实的 MCP 入口（JSON-RPC over /mcp/）


class _McpEntry:
    """``/mcp/`` 的 ``tools/call`` 客户端 + 本段用例要看的东西。

    接缝是**真实的 MCP 传输**（initialize 握手 + JSON-RPC ``tools/call``），不是把工具函数
    从闭包里挖出来直接调——后者会绕过 fastmcp 对调用参数的那一层校验，而「参数非法算协议
    错误」这条 AC 恰恰只在那层存在。
    """

    def __init__(
        self,
        client: TestClient,
        headers: dict[str, str],
        identity_calls: list[Any],
        session: _FakeSession,
    ) -> None:
        self.client = client
        self.headers = headers
        #: ``resolve_identity`` 收到的会话（与门禁收到的那一个比对，证明「一个会话」）。
        self.identity_calls = identity_calls
        #: 工具自己开的那个会话（``finally`` 里该被关掉）。
        self.session = session

    def result(
        self, name: str, arguments: dict[str, Any], *, headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        response = self.client.post(
            MCP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers={**JSONRPC_HEADERS, **self.headers, **(headers or {})},
        )
        assert response.status_code == 200, "协议层永远 200（ADR-0007）"
        return dict(response.json()["result"])

    def payload(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """成功调用的载荷（工具回的是普通 JSON，与 HTTP 的 ``data`` 同一个形状）。"""

        result = self.result(name, arguments)
        assert result.get("isError") is not True, result
        return dict(json.loads(result["content"][0]["text"]))


def _initialize(client: TestClient) -> dict[str, str]:
    response = client.post(
        MCP_PATH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mcp-research", "version": "0"},
            },
        },
        headers=JSONRPC_HEADERS,
    )
    assert response.status_code == 200
    headers: dict[str, str] = {}
    session_id = response.headers.get("mcp-session-id")
    if session_id:
        headers["mcp-session-id"] = session_id
    client.post(
        MCP_PATH,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**JSONRPC_HEADERS, **headers},
    )
    return headers


@pytest.fixture
def mcp_entry(monkeypatch: pytest.MonkeyPatch) -> Iterator[_McpEntry]:
    """只挂 MCP 子应用的入口；门禁由各用例自己换掉（本段验的是「工具递了什么」）。"""

    mcp_server = importlib.import_module("app.mcp_server")
    session = _FakeSession()
    view = AsyncSessionAdapter(session)
    identity_calls: list[Any] = []

    async def _identity(_request: Any, db: Any) -> KeyContext:
        identity_calls.append(db)
        return KEY

    async def _retrieve(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("本段用不到检索工具")

    async def _list_kbs(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(mcp_server, "resolve_identity", _identity)
    mcp_app = mcp_server.create_mcp_http_app(
        get_async_db=lambda: view,
        retrieve=_retrieve,
        list_kbs=_list_kbs,
    )
    app = FastAPI(lifespan=combine_lifespans(mcp_app.lifespan))
    app.mount("/mcp", mcp_app)
    with TestClient(app) as client:
        yield _McpEntry(client, _initialize(client), identity_calls, session)


def _gate_returning(payload: dict[str, Any], seen: dict[str, Any]) -> Any:
    """门禁替身：记下被递进来的东西，交回一个可 ``model_dump`` 的结果。"""

    class _Result:
        def model_dump(self) -> dict[str, Any]:
            return dict(payload)

    async def _gate(db: Any, *, key: Any, kb_ids: list[str], query: str) -> Any:
        seen.update(db=db, key=key, kb_ids=list(kb_ids), query=query)
        return _Result()

    return _gate


def test_the_tool_hands_the_session_the_identity_and_the_parameters_to_the_gate(
    mcp_entry: _McpEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具只做三件事：取会话、取身份、递参数；返回值就是门禁交回的那一份。

    「同一个 ``AsyncSession``」在这里是可断言的：身份判定收到的会话与门禁收到的会话必须是
    **同一个对象**（与 HTTP 侧 FastAPI 依赖缓存同形，票 #130）。两段各开一个会话的写法在
    异步栈上不会报错，只会让判定读到一个与业务不同的快照——所以必须钉住。
    """

    mcp_server = importlib.import_module("app.mcp_server")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        mcp_server,
        "gated_research",
        _gate_returning({"answer": None, "marker": "gate-returned"}, seen),
    )

    payload = mcp_entry.payload("rag_research", {"kb_ids": ["kb-a"], "query": "退款多久到账"})

    assert payload == {"answer": None, "marker": "gate-returned"}

    assert seen["kb_ids"] == ["kb-a"]
    assert seen["query"] == "退款多久到账"
    assert seen["key"] is KEY, "身份来自判定内核，工具不自己拼一个"
    assert seen["db"] is mcp_entry.identity_calls[-1], "判定与门禁用的是同一个会话"
    assert mcp_entry.session.closed is True, "工具在 finally 里关掉自己那个会话"


# ─────────────────────────────────────────────── 业务失败：工具执行失败 + 错误码


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.KNOWLEDGE_BASE_NOT_FOUND,  # 20008 白名单
        ErrorCode.FEATURE_NOT_ALLOWED,  # 20013 没有 Research 授权
        ErrorCode.API_RATE_LIMIT,  # 20005 这一秒打得太快
        ErrorCode.QUOTA_EXCEEDED,  # 20014 今天的额度用完了
    ],
)
def test_a_business_failure_is_a_tool_error_carrying_the_gate_code(
    mcp_entry: _McpEntry, monkeypatch: pytest.MonkeyPatch, code: ErrorCode
) -> None:
    """四个码是四件不同的事（换套餐 / 退避重试 / 提额），工具一个都不许改写或合并。"""

    mcp_server = importlib.import_module("app.mcp_server")

    async def _gate(*_args: Any, **_kwargs: Any) -> Any:
        raise AppError(code, "refused by the gate")

    monkeypatch.setattr(mcp_server, "gated_research", _gate)

    result = mcp_entry.result("rag_research", {"kb_ids": ["kb-a"], "query": "退款多久到账"})

    assert result["isError"] is True
    text = result["content"][0]["text"]
    match = BUSINESS_CODE.search(text)
    assert match is not None, f"业务失败的文本里必须带错误码：{text!r}"
    assert int(match.group(1)) == code.value, "工具原样转发门禁的码，不翻译成别的"


# ─────────────────────────────────────────── 参数非法：协议层的事，连门禁都进不去


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"kb_ids": [], "query": "退款多久到账"}, id="empty-kb-ids"),
        pytest.param({"kb_ids": ["kb-a"], "query": ""}, id="empty-query"),
        pytest.param({"kb_ids": ["kb-a"], "query": "   "}, id="blank-query"),
    ],
)
def test_a_semantically_invalid_payload_gets_the_same_20002_as_the_http_entry(
    mcp_entry: _McpEntry, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]
) -> None:
    """类型合法、语义非法的值（空列表 / 空串 / 纯空白）走**同一个值对象**，因此同一个码。

    这三种形状是工具签名挡不住的那一类：``list[str]`` 与 ``str`` 都收得下它们。既有
    ``rag_retrieve`` 在同一个位置给出 ``20002``（``test_entry_validation_parity.py``
    钉住），第三个工具不许自己发明第二种答案。
    """

    mcp_server = importlib.import_module("app.mcp_server")

    async def _gate(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("语义非法的请求不该进业务层")

    monkeypatch.setattr(mcp_server, "gated_research", _gate)

    result = mcp_entry.result("rag_research", arguments)

    assert result["isError"] is True
    match = BUSINESS_CODE.search(result["content"][0]["text"])
    assert match is not None, result
    assert int(match.group(1)) == ErrorCode.REQUEST_VALIDATION_ERROR.value == 20002


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"query": "退款多久到账"}, id="missing-kb-ids"),
        pytest.param({"kb_ids": ["kb-a"]}, id="missing-query"),
        # ``rounds`` / ``top_k`` 是 HTTP 侧会被 ``extra="forbid"`` 拒掉的字段（ADR-0026 §1
        # 「不接受轮次、条数」）；它们在 MCP 的调用参数里同样不该被静默收下。
        pytest.param({"kb_ids": ["kb-a"], "query": "退款多久到账", "rounds": 2}, id="extra-rounds"),
        pytest.param({"kb_ids": ["kb-a"], "query": "退款多久到账", "top_k": 50}, id="extra-top-k"),
    ],
)
def test_a_malformed_call_is_rejected_before_the_business_layer(
    mcp_entry: _McpEntry, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any]
) -> None:
    """缺字段 / 多给字段是**调用参数**的事：协议层就挡住，文本里没有业务码。

    「文本里没有业务码」是这条用例的实质断言：一个 ``20002`` 会让调用方以为请求被业务层
    看过一眼（甚至可能去重试），而这里它连工具函数体都没进。
    """

    mcp_server = importlib.import_module("app.mcp_server")

    async def _gate(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("参数不合法的调用不该进业务层")

    monkeypatch.setattr(mcp_server, "gated_research", _gate)

    result = mcp_entry.result("rag_research", arguments)

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert BUSINESS_CODE.search(text) is None, f"协议错误不该带业务码：{text!r}"
    offending = next(iter(set(arguments) ^ {"kb_ids", "query"}))
    assert offending in text, f"文本里该点出是哪个参数：{text!r}"


# ─────────────────────────────────────────────── 内部异常：不冒充业务码


def test_an_internal_failure_is_not_dressed_up_as_a_business_code(
    mcp_entry: _McpEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """门禁之外的崩溃照常冒泡（工具执行失败），**不编一个业务码**，会话照样关掉。

    冒充业务码的代价是排障方向被带偏：``20014`` 会让运维去查额度，而真正的现场是一次
    状态机崩溃。这里只要求它不带码、不吞掉，并且 ``finally`` 仍然执行（会话不泄漏）。
    """

    mcp_server = importlib.import_module("app.mcp_server")

    async def _gate(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("state machine exploded")

    monkeypatch.setattr(mcp_server, "gated_research", _gate)

    result = mcp_entry.result("rag_research", {"kb_ids": ["kb-a"], "query": "退款多久到账"})

    assert result["isError"] is True
    assert BUSINESS_CODE.search(result["content"][0]["text"]) is None
    assert mcp_entry.session.closed is True, "崩溃路径也必须关掉会话"


# ─────────────────────────────── 与 HTTP 侧共用同一份实现（结构 + 行为两条证据）


def test_both_entries_call_the_very_same_gate_object() -> None:
    """结构证据：两个入口 import 的是**同一个函数对象**，不是同名同形的两份。

    这条断言比「两边都有个叫 ``gated_research`` 的东西」更严：``is`` 认的是对象身份，
    任何一侧改成 import 自己模块里的另一份实现，它立刻红。
    """

    from app.api.routes import rag as rag_route

    mcp_server = importlib.import_module("app.mcp_server")

    assert mcp_server.gated_research is rag_route.gated_research
    assert mcp_server.gated_research.__module__ == "app.services.research_gate"


def test_the_tool_function_holds_no_business_decision() -> None:
    """结构证据（AST）：工具函数体里**一次都不出现**判定函数，判定全在下层。

    只用 AST 看 ``Name`` / ``Attribute``，不看注释与文档串——描述里必须写「额外模型成本」
    与「独立配额」，那是给模型看的文字，不是判定。反过来，工具若在本地判一次白名单或
    套餐，那几个名字一定会出现在它的语法树里。
    """

    from pathlib import Path

    tree = ast.parse(
        (Path(__file__).resolve().parent.parent / "app" / "mcp_server.py").read_text(
            encoding="utf-8"
        )
    )
    tool = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "rag_research"
    )
    used: set[str] = set()
    for node in ast.walk(tool):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            used.add(node.func.id)

    # 它必须做的三件事：取身份、走同一个值对象、调同一个门禁。
    assert {"_key_context", "parse_research_request", "gated_research"} <= used
    # 判定与装配一个都不许在工具里（白名单 / 套餐 / 配额 / 档位 / 内核 / 状态机）。
    forbidden = {
        "ensure_kb_scope",
        "ensure_research_allowed",
        "research_quota",
        "resolve_retrieval_options",
        "ensure_visible",
        "resolve_index_versions",
        "build_kernel_search",
        "run_research",
    }
    assert not used & forbidden, f"工具里出现了业务判定：{used & forbidden}"


# ─────────────────────────────── 行为证据：同一份输入，两条入口交出同一份形状

RESEARCH_PATH = "/api/v1/rag/research"
KB = "kb-a"
KB_OUT_OF_SCOPE = "kb-not-mine"
QUERY = "退款多久到账，需要哪些材料"


def _outcome() -> Any:
    """状态机的替身产物：只固定「材料 / 计划 / 轮次 / 停止原因」，不固定装填形状。

    装填（``payload_of`` → ``ResearchData``）跑的是**生产代码**，所以两条入口交出的形状
    是同一个校验器给的——这正是本段要证明的东西。
    """

    from app.services.research import PlanPoint, PlanQuery, ResearchOutcome
    from app.services.research_limits import StopReason

    return ResearchOutcome(
        points=(PlanPoint(point_id="A1", text="退款时限"),),
        queries=(PlanQuery(point_id="A1", query="退款多久到账", round=1),),
        materials=(
            {
                "document_id": "doc-a",
                "chunk_id": "chunk-a",
                "title": "退款政策",
                "content": "3 个工作日内到账。",
                "score": 0.9,
                "kb_id": KB,
                "section_id": "section-a",
                "heading_path": "退款 > 时限",
            },
        ),
        searches=1,
        followup_rounds=0,
        model_calls=2,
        duration_ms=1234,
        stop_reason=StopReason.EVIDENCE_COMPLETE,
        answer="3 个工作日内到账。[E1]",
        citations=(
            {
                "text": "3 个工作日内到账。",
                "evidence_ids": ["E1"],
                "status": "supported",
            },
        ),
        evidence_state="complete",
    )


class _BothEntries:
    """HTTP 的 ``POST /api/v1/rag/research`` + MCP 的 ``rag_research``，挂在同一个应用上。

    真门禁（``gated_research``）在两边的执行是**同一段代码**：本夹具只换掉它的两个外部
    依赖（状态机与归属索引落点），其余 9 步（白名单、能力位、参数、限流、日配额、范围
    解析、身份、响应成形、记账）都由生产实现真跑一遍。
    """

    def __init__(self, client: TestClient, mcp_headers: dict[str, str], holder: dict[str, Any]):
        self.client = client
        self.mcp_headers = mcp_headers
        #: 两条入口共用的一份身份（HTTP 走依赖覆盖，MCP 走判定内核替身）。
        self.holder = holder

    def http(self, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(RESEARCH_PATH, json=arguments)
        assert response.status_code == 200, "业务失败也是 HTTP 200（ADR-0007）"
        return response.json()

    def mcp(self, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(
            MCP_PATH,
            json={
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": "rag_research", "arguments": arguments},
            },
            headers={**JSONRPC_HEADERS, **self.mcp_headers},
        )
        assert response.status_code == 200
        return dict(response.json()["result"])

    def mcp_payload(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.mcp(arguments)
        assert result.get("isError") is not True, result
        return dict(json.loads(result["content"][0]["text"]))


@pytest.fixture
def both_entries(monkeypatch: pytest.MonkeyPatch) -> Iterator[_BothEntries]:
    import dataclasses

    from app.api.deps import get_current_key
    from app.core.auth import DEV_KEY_POLICY
    from app.models.plan import PLAN_PRESETS, PLAN_PRO
    from tests import fake_db
    from tests.fake_db import AsyncRecordingSession, async_visible_kb

    router_mod = importlib.import_module("app.api.router")
    error_handlers = importlib.import_module("app.api.error_handlers")
    mcp_server = importlib.import_module("app.mcp_server")
    gate = importlib.import_module("app.services.research_gate")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")
    async_session_mod = importlib.import_module("app.db.async_session")
    factory_mod = importlib.import_module("app.db.session_factory")

    holder: dict[str, Any] = {
        "context": KeyContext(
            tenant_id="tenant_mcp_research",
            key_id="key-both-entries",
            policy=dataclasses.replace(DEV_KEY_POLICY, research_allowed=True),
            plan=PLAN_PRESETS[PLAN_PRO],
        )
    }

    async def _run_research(*_args: Any, **_kwargs: Any) -> Any:
        return _outcome()

    async def _identity(_request: Any, _db: Any) -> KeyContext:
        return holder["context"]

    monkeypatch.setattr(gate, "run_research", _run_research)
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", async_visible_kb)
    # 归属索引是一次独立短事务（自己开会话）：指到本用例的替身上才不会真去连库。
    trace_session = AsyncRecordingSession()
    factory_mod.set_async_session_factory(lambda: trace_session)
    #: 两条入口各自的归属索引行（一次 Research 一行，两边各写一行 = 两次调用都真的过了门禁）。
    holder["trace_session"] = trace_session

    monkeypatch.setattr(mcp_server, "resolve_identity", _identity)
    request_session = AsyncSessionAdapter(_FakeSession())
    mcp_app = mcp_server.create_mcp_http_app(
        get_async_db=lambda: request_session,
        retrieve=lambda *_a, **_k: None,
        list_kbs=lambda *_a, **_k: [],
    )
    app = FastAPI(lifespan=combine_lifespans(mcp_app.lifespan))
    app.include_router(router_mod.api_router)
    error_handlers.install_handlers(app)
    app.mount("/mcp", mcp_app)
    fake_db.override_all_db(app, _FakeSession())
    app.dependency_overrides[get_current_key] = lambda: holder["context"]
    app.dependency_overrides[async_session_mod.get_async_db] = lambda: request_session

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield _BothEntries(client, _initialize(client), holder)
    finally:
        app.dependency_overrides.clear()
        factory_mod.set_async_session_factory(None)


def test_the_same_input_yields_the_same_shape_on_both_entries(
    both_entries: _BothEntries,
) -> None:
    """同一份输入，MCP 与 HTTP 交出**同一份载荷**（只有每次调用的 ``trace_id`` 不同）。

    与 ``test_both_entries_answer_the_same_question_the_same_way``（检索那一条）同形：
    两次独立调用本来就不该共用一根线，所以把 ``trace_id`` 摘掉再比整份载荷。
    """

    arguments = {"kb_ids": [KB], "query": QUERY}

    http_data = both_entries.http(arguments)["data"]
    mcp_data = both_entries.mcp_payload(arguments)

    assert set(mcp_data) == set(http_data), "顶层字段逐个相同"
    http_trace_id = http_data["metadata"].pop("trace_id")
    mcp_trace_id = mcp_data["metadata"].pop("trace_id")
    assert http_trace_id != mcp_trace_id, "两次独立调用不该共用一根线"
    assert len(http_trace_id) == len(mcp_trace_id) == 32
    assert mcp_data == http_data, "整份载荷相等：计划、材料、轮次、引用块、元数据"

    # 两次调用都真的过了那唯一一段门禁：归属索引各写一行（子检索一条都不写）。
    rows = [row for row in both_entries.holder["trace_session"].added if hasattr(row, "chunk_ids")]
    assert len(rows) == 2, "两条入口各跑了一次同一段组合根"
    assert {row.trace_id for row in rows} == {http_trace_id, mcp_trace_id}


def test_both_entries_reject_an_out_of_scope_kb_with_the_same_code(
    both_entries: _BothEntries,
) -> None:
    """白名单这一条判定只有一个：两条入口给出同一个 ``20008``。"""

    both_entries.holder["context"] = _context(
        research_allowed=True, allowed_kb_ids=("kb-ok",), unrestricted=False
    )

    http_code = both_entries.http({"kb_ids": [KB_OUT_OF_SCOPE], "query": QUERY})["code"]
    mcp_result = both_entries.mcp({"kb_ids": [KB_OUT_OF_SCOPE], "query": QUERY})

    assert http_code == ErrorCode.KNOWLEDGE_BASE_NOT_FOUND.value
    assert _mcp_code(mcp_result) == http_code


def test_both_entries_reject_a_missing_research_capability_with_the_same_code(
    both_entries: _BothEntries,
) -> None:
    """能力位这一条判定也只有一个：两条入口给出同一个 ``20013``（不静默降级）。"""

    both_entries.holder["context"] = _context(research_allowed=False)

    http_code = both_entries.http({"kb_ids": [KB], "query": QUERY})["code"]
    mcp_result = both_entries.mcp({"kb_ids": [KB], "query": QUERY})

    assert http_code == ErrorCode.FEATURE_NOT_ALLOWED.value
    assert _mcp_code(mcp_result) == http_code


def _context(
    *,
    research_allowed: bool,
    allowed_kb_ids: tuple[str, ...] = (),
    unrestricted: bool = True,
) -> KeyContext:
    import dataclasses

    from app.core.auth import DEV_KEY_POLICY
    from app.models.plan import PLAN_PRESETS, PLAN_PRO

    return KeyContext(
        tenant_id="tenant_mcp_research",
        key_id="key-both-entries",
        policy=dataclasses.replace(
            DEV_KEY_POLICY,
            research_allowed=research_allowed,
            allowed_kb_ids=allowed_kb_ids,
            unrestricted_scope=unrestricted,
        ),
        plan=PLAN_PRESETS[PLAN_PRO],
    )


def _mcp_code(result: dict[str, Any]) -> int:
    """工具执行失败文本里的业务码；成功是 ``0``。"""

    if not result.get("isError"):
        return int(ErrorCode.SUCCESS)
    match = BUSINESS_CODE.search(result["content"][0]["text"])
    assert match is not None, f"MCP 失败结果里没有错误码：{result!r}"
    return int(match.group(1))

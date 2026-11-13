"""票 #94：多模型配置的验收（``model_configs`` 表、六条控制面端点、角色指向与装配顺序）。

接缝只有三个，其余全是真实实现：

1. **HTTP 边界**（``TestClient``）：六条 ``/console/model-configs*`` 端点只看响应体与库行；
2. **服务层**（``runtime_settings.patch_runtime_settings``）：角色指向的存在性校验与清空语义；
3. **装配点**（``app.providers.llm.get_llm_provider``）：这是「改完指向后下一次检索用新
   配置」唯一能被观察到的位置——挡在它前面的每一层（检索、重排、改写）都只拿它的产物。

数据库换成本仓既有的 ``RecordingSession``、缓存换成本文件里的内存假实现：**不连真库、
不连 Redis**（配置中台是平台级配置，没有租户维度，跨租户那几条对 ``model_configs`` 不适用）。

**回归红线**在最后一组：一行 ``model_configs`` 都没有、角色指针为空时，
``get_llm_provider`` 的产物必须与 `.env` 那条路径逐字段一致——离线与 CI 的行为不能因为
这张表的存在而改变。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from inspect import iscoroutinefunction
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from sqlalchemy import DateTime, ForeignKeyConstraint, inspect

import app.models as models_package
from app.core import passwords
from app.core.config import settings
from app.core.runtime_settings_cache import reset_settings_cache, set_settings_cache
from app.db.base import Base
from app.db.session_factory import set_async_session_factory
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.key_policy import KeyPolicy
from app.models.model_config import ModelConfig
from app.models.plan import PLAN_CODES, PLAN_PRESETS, Plan, plan_row_of
from app.models.runtime_setting import RuntimeSetting
from app.models.tenant import Tenant
from app.providers.llm import get_llm_provider, set_role_resolver
from app.providers.llm.noop import NoopLLMProvider
from app.services import model_config_service, runtime_settings
from app.utils.id_generator import mask_secret
from tests.fake_db import AsyncRecordingSession, MemoryPlanCache, RecordingSession, override_all_db

ADMIN_ID = "00000000-0000-4000-8000-000000000001"
ALICE_ID = "00000000-0000-4000-8000-000000000002"

ADMIN_PASSWORD = "admin-password"
USER_PASSWORD = "user-password"

TENANT_ALPHA = "tenant_alpha"

WRITE_HEADERS = {"x-requested-with": "XMLHttpRequest"}

SEEDED_AT = datetime(2026, 1, 1, 8, 0, 0)

#: 明文 Key 的哨兵。任何一条响应或审计里出现它，就是本票最严重的那类回归。
SECRET_SENTINEL = "sk-live-7d3f9b1c-SENTINEL-abcd"

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"


class _MemorySettingsCache:
    """运行时配置覆盖层缓存的内存假实现（与 #93 测试里那个同形）。"""

    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None
        self.deleted: int = 0

    def get(self) -> dict[str, Any] | None:
        return None if self.payload is None else dict(self.payload)

    def set(self, overrides: dict[str, Any]) -> None:
        self.payload = dict(overrides)

    def delete(self) -> None:
        self.deleted += 1
        self.payload = None

    async def adelete(self) -> None:
        """循环上的那条：与 ``delete`` 同一份状态（契约见 app.core.runtime_settings_cache）。"""

        self.delete()


def _plan_rows() -> list[Plan]:
    rows: list[Plan] = []
    for index, code in enumerate(PLAN_CODES, start=1):
        row = plan_row_of(PLAN_PRESETS[code])
        row.id = index
        rows.append(row)
    return rows


def _account(
    account_id: str,
    username: str,
    password: str,
    *,
    role: str = "user",
    tenant_id: str | None = None,
) -> Account:
    return Account(
        id=account_id,
        tenant_id=tenant_id,
        username=username,
        password_hash=passwords.hash_password(password),
        role=role,
        status="active",
    )


def _config(
    config_id: str,
    *,
    label: str = "DeepSeek 生产",
    provider: str = "deepseek",
    base_url: str = "",
    model: str = "deepseek-flash",
    api_key: str = SECRET_SENTINEL,
) -> ModelConfig:
    return ModelConfig(
        id=config_id,
        label=label,
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        created_at=SEEDED_AT,
        updated_at=SEEDED_AT,
    )


def _world() -> dict[type, list[Any]]:
    """最小世界：平台管理员、一个租户账号，外加两条模型配置（fast 指向第一条）。

    ``model_configs`` 是**平台级**资源：一次改动横跨所有租户，所以这组用例不需要第二个
    租户来做跨租户对照——「跨租户」这个概念对这张表不成立（#93 的同一段说理）。
    """

    return {
        Plan: _plan_rows(),
        Tenant: [Tenant(id=TENANT_ALPHA, name="Alpha Corp", plan_id=3, status="active")],
        Account: [
            _account(ADMIN_ID, "admin", ADMIN_PASSWORD, role="admin"),
            _account(ALICE_ID, "alice", USER_PASSWORD, tenant_id=TENANT_ALPHA),
        ],
        KeyPolicy: [],
        ModelConfig: [
            _config("config-deepseek"),
            _config(
                "config-ark",
                label="Ark 备用",
                provider="ark",
                base_url="https://ark.cn-beijing.volces.com/api/v3",
                model="doubao-pro",
                api_key="sk-ark-backup-abcd",
            ),
        ],
        #: 开局 fast 指向第一条；strong 不指向（回落 .env）。
        RuntimeSetting: [
            RuntimeSetting(
                key="model.role.fast",
                value="config-deepseek",
                updated_by=ADMIN_ID,
                updated_at=SEEDED_AT,
            )
        ],
    }


@dataclass(frozen=True)
class _Harness:
    client: TestClient
    db: RecordingSession
    cache: _MemorySettingsCache

    def login(self, username: str, password: str) -> Any:
        return self.client.post("/console/login", json={"username": username, "password": password})

    def data(self, response: Any) -> Any:
        body = response.json()
        assert body["code"] == 0, body
        return body["data"]

    def audit_entries(self) -> list[AuditLog]:
        return [row for row in self.db.added if isinstance(row, AuditLog)]

    def configs(self, response: Any) -> list[dict[str, Any]]:
        return self.data(response)["items"]

    def config_row(self, config_id: str) -> ModelConfig | None:
        return next((row for row in self.db._rows[ModelConfig] if row.id == config_id), None)

    def pointer_row(self, key: str) -> RuntimeSetting | None:
        return next((row for row in self.db._rows[RuntimeSetting] if row.key == key), None)

    def patch_setting(self, values: dict[str, Any], *, dry_run: bool = False) -> Any:
        query = "?dry_run=false" if not dry_run else ""
        return self.client.patch(
            f"/console/settings{query}", json={"values": values}, headers=WRITE_HEADERS
        )


@pytest.fixture(autouse=True)
def _isolate_env_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """把「回落 ``.env``」这条路固定成一套**与机器无关**的参数。

    本文件有几条用例专门验证「没有指针时装配产物与 ``.env`` 路径逐字一致」，它们只
    monkeypatch 了 ``DEEPSEEK_API_KEY``。可 ``LLM_PROVIDER`` 与另外五家的 Key 是**从本机
    ``.env`` 读进来的**：开发者 `.env` 里写着 ``LLM_PROVIDER=ark``（模板默认就是），这些
    用例就会去装配 ark 并拿到 ``ARK_API_KEY``，断言当场变红——而 CI 上没有 `.env`，同一套
    代码全绿。**「测试是绿的」不能取决于这台机器上有没有 `.env`**，所以这里无条件铺平：
    供应商钉 ``deepseek``、六家 Key 全部清空，需要哪家由用例自己 monkeypatch。

    与 ``tests/conftest.py`` 里强制 ``KEYWORD_SEARCH_PROVIDER=none`` 是同一条纪律：
    用**强制覆盖**而不是 ``setdefault``——变量已经存在时 ``setdefault`` 什么都不做。
    """

    monkeypatch.setattr(settings, "LLM_PROVIDER", "deepseek")
    for name in ("DEEPSEEK_API_KEY", "QWEN_API_KEY", "ARK_API_KEY"):
        monkeypatch.setattr(settings, name, "")
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "LLM_MODEL", "deepseek-flash")
    monkeypatch.setattr(settings, "LLM_STRONG_MODEL", "deepseek-v4-pro")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "LLM_REASONING_EFFORT", "off")


@pytest.fixture
def console() -> Any:
    """真实应用 + 假数据库 + 假缓存 + 假会话工厂；每个用例一套全新的世界与状态。"""

    import importlib

    main = importlib.import_module("app.main")
    console_deps = importlib.import_module("app.api.console_deps")

    saved = settings.model_dump()
    runtime_settings.reset_runtime_state()
    db = RecordingSession(rows=_world())
    cache = _MemorySettingsCache()
    plan_cache = MemoryPlanCache()
    set_settings_cache(cache)
    # 中间件与 Provider 装配都拿不到 Depends，只能自己开会话；注入同一个假会话，
    # 于是「改指针之后下一次装配按新行跑」这条在测试里也走真实路径（注入点是
    # 进程级共享的，见 app/db/session_factory.py）。
    runtime_settings.set_session_factory(lambda: db)
    # 角色解析也拿不到 ``Depends``，但它**走异步会话**（票 #137）：每次装配自己开一个
    # 会话读指针与配置行。这里每次开一个「看得见当前那份假库」的异步会话（行是同一批
    # 对象，所以上面改指针、删行都即时可见），关闭只关它自己，不动共享的 ``db``。
    set_async_session_factory(lambda: AsyncRecordingSession(rows=db._rows))
    # 角色解析器是**组合根注入**的（#82）：生产里 app.main 启动时注入一次；这里显式再注入
    # 一遍，让装配点用例不依赖「哪个测试文件先 import 了 app.main」这种顺序。
    set_role_resolver(model_config_service.role_endpoint)
    # 控制面的读与写都在异步栈上（票 #130）：两个异步会话入口一起换、喂同一个假库。
    override_all_db(main.app, db)
    main.app.dependency_overrides[console_deps.get_runtime_settings_cache] = lambda: cache
    main.app.dependency_overrides[console_deps.get_plan_cache] = lambda: plan_cache
    try:
        with TestClient(main.app) as client:
            yield _Harness(client=client, db=db, cache=cache)
    finally:
        main.app.dependency_overrides.clear()
        set_role_resolver(None)
        runtime_settings.reset_runtime_state()
        runtime_settings.set_session_factory(None)
        set_async_session_factory(None)
        reset_settings_cache()
        for key, value in saved.items():
            setattr(settings, key, value)


# ─────────────────────────────────────────────────────── 鉴权：平台级资源只给管理员


def test_a_model_configs_call_without_a_session_is_unauthorized(console: _Harness) -> None:
    """没有会话 → ``20010``（前端该跳登录）。六条端点一条都不能漏。"""

    assert console.client.get("/console/model-configs").json()["code"] == 20010
    assert (
        console.client.post(
            "/console/model-configs",
            json={"label": "x", "provider": "deepseek", "model": "m"},
            headers=WRITE_HEADERS,
        ).json()["code"]
        == 20010
    )
    assert (
        console.client.patch(
            "/console/model-configs/config-deepseek",
            json={"label": "x"},
            headers=WRITE_HEADERS,
        ).json()["code"]
        == 20010
    )
    assert (
        console.client.delete(
            "/console/model-configs/config-deepseek", headers=WRITE_HEADERS
        ).json()["code"]
        == 20010
    )
    assert (
        console.client.get("/console/model-configs/config-deepseek/secret").json()["code"] == 20010
    )


def test_a_tenant_account_cannot_read_or_write_model_configs(console: _Harness) -> None:
    """**跨租户越权边**：租户账号读/写一律 ``20011``，且不许留下任何库行或审计。

    ``model_configs`` 是平台级资源（一次改动改变所有租户下一次检索用的模型与凭证），
    对租户账号而言不是「权限粒度粗一点」，而是「能看到并改掉别人的模型接入」。
    """

    assert console.login("alice", USER_PASSWORD).json()["code"] == 0

    assert console.client.get("/console/model-configs").json()["code"] == 20011
    response = console.client.post(
        "/console/model-configs",
        json={"label": "租户来建", "provider": "deepseek", "model": "m"},
        headers=WRITE_HEADERS,
    )
    assert response.json()["code"] == 20011
    assert (
        console.client.patch(
            "/console/model-configs/config-deepseek",
            json={"label": "租户来改名"},
            headers=WRITE_HEADERS,
        ).json()["code"]
        == 20011
    )
    assert (
        console.client.delete(
            "/console/model-configs/config-deepseek", headers=WRITE_HEADERS
        ).json()["code"]
        == 20011
    )
    assert (
        console.client.get("/console/model-configs/config-deepseek/secret").json()["code"] == 20011
    )
    assert console.db.added_of(ModelConfig) == []
    assert console.audit_entries() == []


def test_a_write_without_the_anti_csrf_header_is_rejected(console: _Harness) -> None:
    """写操作必须带 ``x-requested-with``；缺了是 ``20011``，**不等于**掉登录。"""

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0
    commits_before = console.db.commits  # 登录本身会提交一条会话行

    assert (
        console.client.post(
            "/console/model-configs",
            json={"label": "新配置", "provider": "deepseek", "model": "m"},
        ).json()["code"]
        == 20011
    )
    assert (
        console.client.patch(
            "/console/model-configs/config-deepseek", json={"label": "改个名"}
        ).json()["code"]
        == 20011
    )
    assert console.client.delete("/console/model-configs/config-deepseek").json()["code"] == 20011
    assert console.db.added_of(ModelConfig) == []
    assert console.db.commits == commits_before


def test_revealing_the_secret_is_a_read_gate_and_needs_no_anti_csrf_header(
    console: _Harness,
) -> None:
    """「显示原文」是**读**接口（``get_console_admin``）：不查防跨站头，但每次留一条审计。

    #82 修复前这条 GET 挂的是 ``require_console_admin_write``（把「会留审计」当成写操作，
    属于越界）；spec 的边界是「写操作才要防跨站头，读走 ``get_console_admin``」。
    """

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0
    commits_before = console.db.commits

    response = console.client.get("/console/model-configs/config-deepseek/secret")

    body = response.json()
    assert body["code"] == 0, body
    assert body["data"] == {"id": "config-deepseek", "api_key": SECRET_SENTINEL}

    audit = console.audit_entries()[-1]
    assert (audit.action, audit.target) == ("model.reveal", "model:config-deepseek")
    assert SECRET_SENTINEL not in str(audit.after_snapshot)
    assert console.db.commits == commits_before + 1


def test_an_admin_can_read_the_model_config_list(console: _Harness) -> None:
    """管理员 ``tenant_id is None`` 正是平台级资源唯一合理的归属：能读、能写。"""

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    rows = console.configs(console.client.get("/console/model-configs"))

    assert [row["id"] for row in rows] == ["config-deepseek", "config-ark"]
    assert rows[0]["roles"] == ["fast"]
    assert rows[1]["roles"] == []
    # 列表里永远只有 masked 形态：明文 Key 连一个子串都不许出现。
    assert SECRET_SENTINEL not in console.client.get("/console/model-configs").text
    assert rows[0]["api_key"] == mask_secret(SECRET_SENTINEL)
    assert rows[0]["api_key"].endswith("abcd")


def test_a_config_pointed_at_by_both_roles_shows_both_and_cannot_be_deleted(
    console: _Harness,
) -> None:
    """同一个配置被 fast 与 strong 同时指向是常见配置：列表要两个角色都显示，
    删除时也要把**两个**角色都报出来（只报一个会让管理员改完一个还是删不掉）。
    """

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0
    assert console.patch_setting({"model.role.strong": "config-deepseek"}).json()["code"] == 0

    rows = console.configs(console.client.get("/console/model-configs"))
    assert rows[0]["roles"] == ["fast", "strong"]

    response = console.client.delete(
        "/console/model-configs/config-deepseek", headers=WRITE_HEADERS
    )
    body = response.json()
    assert body["code"] == 20015
    assert "fast" in body["msg"] and "strong" in body["msg"]
    assert console.config_row("config-deepseek") is not None


# ────────────────────────────────────────────────────────────────────── 新增与校验


def test_an_admin_can_add_a_model_config_with_a_preset_vendor(console: _Harness) -> None:
    """新增一条接入：供应商来自预设注册表，端点与模型名可自定义（本票的验收第一句）。"""

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.post(
        "/console/model-configs",
        json={
            "label": "  Qwen 自建端点 ",
            "provider": " Qwen ",
            "base_url": "https://dashscope.example.com/v1",
            "model": "qwen3-max",
            "api_key": "sk-qwen-secret-1234",
        },
        headers=WRITE_HEADERS,
    )

    created = console.data(response)
    assert created["provider"] == "qwen", "供应商名要规范化成小写，工厂按小写索引"
    assert created["label"] == "Qwen 自建端点"
    assert created["roles"] == []
    assert created["api_key"] == "sk-…1234"
    assert "sk-qwen-secret-1234" not in response.text

    row = console.config_row(created["id"])
    assert row.base_url == "https://dashscope.example.com/v1"
    assert row.model == "qwen3-max"
    assert row.api_key == "sk-qwen-secret-1234", "库里存明文（ADR-0004），只有响应 masked"

    audit = console.audit_entries()[-1]
    assert (audit.action, audit.target) == ("model.create", f"model:{created['id']}")
    assert audit.after_snapshot["api_key"] == "sk-…1234", "审计只许出现 masked 形态"


def test_a_provider_outside_the_registry_is_a_param_error(console: _Harness) -> None:
    """供应商必须是六家预设之一：拼错的名字不能「保存成功、第一次调用才炸」。"""

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    for provider in ("openai_compatible", "", "DeepSeek-V2"):
        response = console.client.post(
            "/console/model-configs",
            json={"label": "坏供应商", "provider": provider, "model": "m"},
            headers=WRITE_HEADERS,
        )
        assert response.json()["code"] == 20015, provider
    assert console.db.added_of(ModelConfig) == []


def test_empty_label_or_model_is_a_param_error(console: _Harness) -> None:
    """空备注名 / 空模型名没有意义，也不该在面板上显示成一行空白。"""

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    for field, value in (("label", ""), ("model", "   "), ("label", "x" * 129)):
        body = {"label": "合法名字", "provider": "deepseek", "model": "deepseek-flash"}
        body[field] = value
        response = console.client.post("/console/model-configs", json=body, headers=WRITE_HEADERS)
        assert response.json()["code"] == 20015, (field, value)
    assert console.db.added_of(ModelConfig) == []


def test_patching_a_config_updates_only_the_given_fields(console: _Harness) -> None:
    """部分更新：没发的字段不动；改了之后下一次调用就按新值（审计拿整行快照）。"""

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        "/console/model-configs/config-deepseek",
        json={"model": "deepseek-v4-flash", "api_key": "sk-rotated-9999"},
        headers=WRITE_HEADERS,
    )

    data = console.data(response)
    assert data["changed"] == ["api_key", "model"]
    row = console.config_row("config-deepseek")
    assert row.model == "deepseek-v4-flash"
    assert row.api_key == "sk-rotated-9999"
    assert row.label == "DeepSeek 生产", "没发的字段不许动"
    assert row.base_url == ""

    audit = console.audit_entries()[-1]
    assert audit.action == "model.patch"
    assert "sk-rotated-9999" not in str(audit.after_snapshot)
    assert audit.before_snapshot["api_key"] == "sk-…abcd"
    assert audit.after_snapshot["api_key"] == "sk-…9999"


def test_patching_a_missing_config_is_not_found(console: _Harness) -> None:
    """``id`` 不存在 → ``20016``（平台级资源没有「跨租户」这一分支）。"""

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.patch(
        "/console/model-configs/does-not-exist", json={"label": "x"}, headers=WRITE_HEADERS
    )

    assert response.json()["code"] == 20016


# ─────────────────────────────────────────────────────────── 删：被角色指向就不许删


def test_deleting_a_config_referenced_by_a_role_is_rejected(console: _Harness) -> None:
    """验收原话：删一条仍被角色引用的配置被拒（``20015``），提示先改指向。"""

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.delete(
        "/console/model-configs/config-deepseek", headers=WRITE_HEADERS
    )

    body = response.json()
    assert body["code"] == 20015
    assert "fast" in body["msg"] and "指向" in body["msg"]
    assert console.config_row("config-deepseek") is not None, "被拒的删除不许删掉任何行"
    assert console.db.added_of(AuditLog) == []


def test_a_config_becomes_deletable_after_the_pointer_is_cleared(console: _Harness) -> None:
    """改掉指向（写空串 = 回落到 .env）之后就能删了——「先改指向」必须是能做到的。"""

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    cleared = console.patch_setting({"model.role.fast": ""})
    assert cleared.json()["code"] == 0, cleared.json()

    response = console.client.delete(
        "/console/model-configs/config-deepseek", headers=WRITE_HEADERS
    )
    assert response.json()["code"] == 0
    assert console.data(response) == {"id": "config-deepseek", "deleted": True}
    assert console.db.deleted
    audit = console.audit_entries()[-1]
    assert (audit.action, audit.target) == ("model.delete", "model:config-deepseek")
    assert audit.after_snapshot is None


def test_deleting_an_unreferenced_config_succeeds(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.delete("/console/model-configs/config-ark", headers=WRITE_HEADERS)

    assert response.json()["code"] == 0
    assert console.config_row("config-ark") is None


def test_deleting_a_missing_config_is_not_found(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    assert (
        console.client.delete(
            "/console/model-configs/does-not-exist", headers=WRITE_HEADERS
        ).json()["code"]
        == 20016
    )


# ─────────────────────────────────────────────────────── 显示原文：一次 + 一条审计


def test_revealing_the_secret_returns_the_plaintext_once_and_audits_it(console: _Harness) -> None:
    """明文只有这一条出口，而且每次都被记账；**审计里不许出现明文**。"""

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.client.get(
        "/console/model-configs/config-deepseek/secret", headers=WRITE_HEADERS
    )

    assert response.json()["code"] == 0
    assert response.json()["data"] == {"id": "config-deepseek", "api_key": SECRET_SENTINEL}

    audit = console.audit_entries()[-1]
    assert (audit.action, audit.target, audit.actor_id) == (
        "model.reveal",
        "model:config-deepseek",
        ADMIN_ID,
    )
    assert SECRET_SENTINEL not in str(audit.before_snapshot)
    assert SECRET_SENTINEL not in str(audit.after_snapshot)
    assert audit.after_snapshot["api_key"] == "sk-…abcd"


def test_revealing_a_missing_config_is_not_found(console: _Harness) -> None:
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    assert (
        console.client.get(
            "/console/model-configs/does-not-exist/secret", headers=WRITE_HEADERS
        ).json()["code"]
        == 20016
    )
    assert console.db.added_of(AuditLog) == [], "找不到的行不该留下「看了明文」的账本"


def test_the_model_configs_table_is_registered_with_the_documented_columns() -> None:
    """表与迁移的形状：一表一迁移、接在 #93 的 head 之后、只建表不灌数据。"""

    assert "model_configs" in Base.metadata.tables
    assert "ModelConfig" in models_package.__all__

    table = ModelConfig.__table__
    assert [column.name for column in table.primary_key] == ["id"]
    assert table.c.id.type.length == 36
    assert table.c.label.type.length == 128
    assert not table.c.label.nullable
    assert table.c.provider.type.length == 32
    assert not table.c.provider.nullable
    assert table.c.base_url.type.length == 255
    assert not table.c.base_url.nullable
    assert table.c.model.type.length == 128
    assert not table.c.model.nullable
    assert table.c.api_key.type.length == 255
    assert not table.c.api_key.nullable
    assert isinstance(table.c.created_at.type, DateTime)
    assert isinstance(table.c.updated_at.type, DateTime)


def test_the_model_configs_table_carries_no_foreign_keys_and_no_relationships() -> None:
    """不加外键：角色指向存在 ``runtime_settings`` 里，引用完整性靠应用层校验。"""

    table = ModelConfig.__table__
    assert not table.foreign_keys
    assert not [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]
    assert len(inspect(ModelConfig).relationships) == 0


class _RecordingOp:
    """假 ``op``：只记录建了什么、删了什么；``execute`` 一律失败（迁移不许灌数据）。"""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.dropped: list[str] = []

    def f(self, name: str) -> str:
        return name

    def create_table(self, table_name: str, *_args: Any, **_kwargs: Any) -> None:
        self.created.append(f"table:{table_name}")

    def drop_table(self, table_name: str, **_kwargs: Any) -> None:
        self.dropped.append(f"table:{table_name}")

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"本票的迁移只建一张表，不该用到 op.{name}")


def _load_migration() -> tuple[ModuleType, _RecordingOp]:
    path = next(VERSIONS_DIR.glob("*_create_model_configs.py"))
    spec = importlib.util.spec_from_file_location("t94_model_configs_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    recorder = _RecordingOp()
    module.op = recorder
    return module, recorder


def test_the_migration_creates_exactly_the_model_configs_table_and_drops_it_again() -> None:
    module, recorder = _load_migration()

    module.upgrade()
    created = sorted(recorder.created)
    module.downgrade()

    assert created == ["table:model_configs"]
    assert sorted(recorder.dropped) == created


def test_the_migration_chains_onto_the_runtime_settings_head() -> None:
    path = next(VERSIONS_DIR.glob("*_create_model_configs.py"))
    text = path.read_text(encoding="utf-8")
    revision = re.search(r"""^revision(?::\s*str)?\s*=\s*['"]([^'"]+)['"]""", text, re.M)
    down = re.search(r"""^down_revision(?::\s*[^=]+)?\s*=\s*['"]([^'"]+)['"]""", text, re.M)

    assert revision is not None and re.fullmatch(r"[0-9a-f]{12}", revision.group(1))
    assert down is not None and down.group(1) == "e31c7a5d9f42"
    assert path.name == f"{revision.group(1)}_create_model_configs.py"


# ─────────────────────────────────────────────── 角色指向：存在性校验与清空语义


def test_writing_a_pointer_to_a_missing_config_is_rejected(console: _Harness) -> None:
    """写 ``model.role.fast = "不存在-id"`` → ``20015``，且指针行保持原值。

    校验点必须在**服务层**（``runtime_settings.patch_runtime_settings``）：指针将来还有
    CLI / 迁移脚本等入口，判定写进路由的话每个入口都得再写一遍。
    """

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    response = console.patch_setting({"model.role.fast": "no-such-config"})

    body = response.json()
    assert body["code"] == 20015
    assert "该模型配置不存在" in body["msg"]
    assert console.pointer_row("model.role.fast").value == "config-deepseek"
    assert console.audit_entries() == []


@pytest.mark.asyncio
async def test_writing_an_existing_config_id_moves_the_pointer_and_the_next_call_uses_it(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收原话：改 ``fast`` 的指向后，**下一次**装配就用新那条的 provider/base_url/model/api_key。

    「下一次检索 / 重排用新配置」在服务端唯一能被观察到的地方就是 ``get_llm_provider``：
    挡在它前面的每一层都只拿它的产物。这里把 ``init_chat_model`` 换成一个记录器，
    断言工厂真的按新行拼了参数（而不是仍按 `.env`）。
    """

    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "sk-env-deepseek")
    monkeypatch.setattr(settings, "LLM_MODEL", "env-fast-model")
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    moved = console.patch_setting({"model.role.fast": "config-ark"})
    assert moved.json()["code"] == 0, moved.json()
    assert console.pointer_row("model.role.fast").value == "config-ark"

    captured = _capture_chat_model(monkeypatch)
    provider = await get_llm_provider("fast")
    # 装配发生在**第一次要用模型**的时候（#117：Provider 缓存、模型每次现装），
    # 所以这里显式取一次模型——捕获到的就是这次检索真会用的那组参数。
    assert provider.chat_model is not None

    assert provider.model_name == "doubao-pro"
    assert captured == [
        (
            "doubao-pro",
            {
                "model_provider": "openai",
                "api_key": "sk-ark-backup-abcd",
                "timeout": settings.LLM_TIMEOUT_SECONDS,
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "temperature": 0,
            },
        )
    ], "fast 角色按新行的 provider / 地址 / 模型 / Key 装配"
    # 改指向以「hot」的方式写库：删缓存，下一次读就是新值（与 #93 一致）。
    assert console.cache.deleted >= 1


def test_writing_null_or_an_empty_pointer_means_fall_back_to_env(console: _Harness) -> None:
    """空串与 ``null`` 都是「不指向任何一条」：运行时回落到 ``.env`` 的单供应商。

    这是「改回默认」唯一做得到的动作，所以它必须是合法的写入；库里存成空串而不是
    JSON/SQL null——``value`` 列非空，清空一条指针不该变成一次 ``NOT NULL`` 冲突。
    """

    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0

    for value in ("", None):
        response = console.patch_setting({"model.role.fast": value})
        assert response.json()["code"] == 0, (value, response.json())
        assert console.pointer_row("model.role.fast").value == ""


@pytest.mark.asyncio
async def test_a_cleared_pointer_makes_the_next_provider_use_the_env_single_vendor(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "sk-env-deepseek")
    monkeypatch.setattr(settings, "LLM_MODEL", "env-fast-model")
    assert console.login("admin", ADMIN_PASSWORD).json()["code"] == 0
    assert console.patch_setting({"model.role.fast": ""}).json()["code"] == 0

    captured = _capture_chat_model(monkeypatch)
    provider = await get_llm_provider("fast")
    # 模型在第一次要用时才现装（#117），先把它取出来再断言「拼了什么参数」。
    assert provider.chat_model is not None

    assert provider.model_name == "env-fast-model"
    assert captured[0][1]["api_key"] == "sk-env-deepseek"
    assert captured[0][1].get("base_url") == "https://api.deepseek.com/v1"


# ───────────────────────────────────── 装配顺序与回归红线（离线 / CI 行为逐字不变）


@pytest.mark.asyncio
async def test_no_model_config_and_no_pointer_reproduces_the_env_path(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**回归红线**：一行配置、一条指针都没有时，装配产物与 ``.env`` 路径逐字一致。

    这条是本票写进验收里的红线：``model_configs`` 是新增的覆盖层，不是新的缺省。
    离线测试、CI、以及没部署过这张表的机器都必须落到与改造前完全相同的参数上。
    """

    console.db._rows[ModelConfig] = []
    console.db._rows[RuntimeSetting] = []
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "sk-env-deepseek")
    monkeypatch.setattr(settings, "LLM_MODEL", "deepseek-flash")
    monkeypatch.setattr(settings, "LLM_STRONG_MODEL", "deepseek-v4-pro")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "LLM_REASONING_EFFORT", "high")

    captured = _capture_chat_model(monkeypatch)

    # 模型在第一次要用时才现装（#117）：两档各取一次，捕获顺序才是 fast → strong。
    assert (await get_llm_provider("fast")).chat_model is not None
    assert (await get_llm_provider("strong")).chat_model is not None

    assert captured[0] == (
        "deepseek-flash",
        {
            "model_provider": "openai",
            "api_key": "sk-env-deepseek",
            "timeout": settings.LLM_TIMEOUT_SECONDS,
            "base_url": "https://api.deepseek.com/v1",
            "temperature": 0,
        },
    )
    assert captured[1] == (
        "deepseek-v4-pro",
        {
            "model_provider": "openai",
            "api_key": "sk-env-deepseek",
            "timeout": settings.LLM_TIMEOUT_SECONDS,
            "base_url": "https://api.deepseek.com/v1",
            "reasoning_effort": "high",
        },
    )


@pytest.mark.asyncio
async def test_no_key_anywhere_still_returns_the_noop_provider(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空表 + 空指针 + 没配 key：还是空实现（离线与 CI 的默认路径）。"""

    console.db._rows[ModelConfig] = []
    console.db._rows[RuntimeSetting] = []
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "")

    assert isinstance(await get_llm_provider("fast"), NoopLLMProvider)
    assert isinstance(await get_llm_provider("strong"), NoopLLMProvider)


@pytest.mark.asyncio
async def test_a_pointer_to_a_row_deleted_behind_the_api_falls_back_to_env_without_raising(
    console: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """指针指向的行在运行中被绕过接口删掉：回落 ``.env`` + 告警，**不在请求路径上炸**。

    手工 ``DELETE``、回滚失败的迁移都能造出这个状态；一个正在服务的进程不该因为它而
    让所有租户的检索 500（与 ``runtime_settings`` 对非法行「丢弃 + 保留上一份好值」同一
    条纪律）。
    """

    console.db._rows[ModelConfig] = []  # 库里那一行没了，但指针还在
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", "sk-env-deepseek")
    monkeypatch.setattr(settings, "LLM_MODEL", "env-fast-model")

    captured = _capture_chat_model(monkeypatch)
    provider = await get_llm_provider("fast")
    # 模型在第一次要用时才现装（#117），先把它取出来再断言「拼了什么参数」。
    assert provider.chat_model is not None

    assert provider.model_name == "env-fast-model"
    assert captured[0][1]["api_key"] == "sk-env-deepseek"


# ───────────────────────────── 装配期读库的形状（票 #137：搬出事件循环）


@pytest.mark.asyncio
async def test_role_resolution_reads_the_pointer_through_the_async_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """角色指针与配置行都走 ``AsyncSession``（票 #137）。

    这是「事件循环上不再出现同步会话读」的最小可观察事实：注入的异步工厂被真的打开一次
    （装配解析各读一次、共用同一个会话），而 ``model_config_service`` 里**没有**同步会话
    工厂这个名字可用——同步入口不是「这次没走」，是「这条路径上没有」。
    """

    opened: list[Any] = []

    def _open() -> Any:
        session = AsyncRecordingSession(rows=_world())
        opened.append(session)
        return session

    monkeypatch.setattr(model_config_service, "async_session_factory", lambda: _open)

    endpoint = await model_config_service.role_endpoint("fast")

    assert endpoint is not None
    assert endpoint.config_id == "config-deepseek"
    assert endpoint.model == "deepseek-flash"
    assert len(opened) == 1, "指针与配置行走同一个会话，不各开一个"
    assert not hasattr(model_config_service, "session_factory"), (
        "装配期不再有同步会话入口（ADR-0024 决策四第一行已搬走）"
    )
    # 形状本身也钉住：两个入口都是协程，事件循环上不可能有「同步调用悄悄少一次」
    assert iscoroutinefunction(model_config_service.resolve_role_config)
    assert iscoroutinefunction(model_config_service.role_endpoint)


@pytest.mark.asyncio
async def test_a_failing_role_read_still_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读库失败照旧吞掉并回落 ``.env``：只记 warning，不在请求路径上炸。

    「离线 / CI / 运维正在搬迁」与「库里有半条脏数据」是同一条纪律：检索是一条正在服务的
    路径，一次主键查询失败不该把它整段打断。
    """

    class _BrokenSession(AsyncRecordingSession):
        async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("database is down")

    monkeypatch.setattr(model_config_service, "async_session_factory", lambda: _BrokenSession)

    assert await model_config_service.role_endpoint("fast") is None
    assert await model_config_service.resolve_role_config("fast") is None


def test_the_rerank_follows_fast_and_has_no_separate_config(console: _Harness) -> None:
    """**重排跟随 fast**：没有「重排模型」这一条配置，也没有第三个指针键。

    一期 ``RerankProvider`` 包着 ``LLMProvider``（ADR-0002）；面板若出现「重排模型」下拉，
    用户会以为它可独立配置，而实际上改了也不会生效。这条用例把「白名单里只有两个指针键」
    钉住，避免后来人顺手加一个不存在的配置位。
    """

    pointer_keys = [
        key
        for key, spec in runtime_settings.SETTING_SPECS.items()
        if spec.target == runtime_settings.TARGET_POINTER
    ]
    assert pointer_keys == ["model.role.fast", "model.role.strong"]
    assert not any("rerank" in key for key in pointer_keys)


def _capture_chat_model(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict[str, Any]]]:
    """把 ``init_chat_model`` 换成记录器：装配出的参数就是「用哪条配置」的全部证据。

    返回一个**列表**（按调用顺序），每项是 ``(模型名, 关键字参数)``。真的构造一个
    ``ChatOpenAI`` 也能跑，但那样断言只能读到 LangChain 自己的字段名，而不是工厂喂给它的
    那组参数——测试要钉的是「工厂拼了什么」，不是框架怎么存。
    """

    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_init(model_name: str, **kwargs: Any) -> FakeListChatModel:
        kwargs = dict(kwargs)
        # 连接来自注册表（异步按 loop、同步进程级），它不是「用哪条配置」的证据，而且
        # 对象身份没法写进期望值里；连接这件事由 ``tests/test_provider_cache.py`` 钉。
        for name in ("http_client", "http_async_client"):
            kwargs.pop(name, None)
        calls.append((model_name, kwargs))
        return FakeListChatModel(responses=["unused"])

    monkeypatch.setattr("app.providers.llm.init_chat_model", _fake_init)
    return calls

"""票 #146：切片编排开关**三层同构**（套餐允许 / 密钥配置启用 / 档位想要）。

编排本身是 #147 的事。本票只把「开关」装好，所以这里断言的全是**判定路径**：

* 三层各否决一次，编排就不启用（套餐不给 / Key 不要 / 档位不带）；
* 三层都同意，编排生效，且回显与生效值**同源**（``tenant_policy.orchestration``）；
* 与重排 / 改写同一套反应：命名档位越权 → ``20013`` 拒绝（不静默降级）；
  ``custom`` 档越权 → 裁剪并回显 ``clipped``；
* 配置时自洽校验（ADR-0018 第 2 条）：档位想要编排而配置没启用 → ``20015``；
  配置启用而套餐不给 → ``20015``；
* 开关对检索结果**没有影响**（#147 之前它只影响回显）——这条是「先把开关装好」的
  可断言形态；
* 迁移只有一条新 revision、接在既有唯一 head 上、可升可降、既有行回填成 ``False``。

控制面那三条写入口（套餐 / Key 配置 / 档位预设）的契约与审计分别落在它们自己的文件里
（``test_console_api.py`` / ``test_console_keys.py`` / ``test_console_profiles.py``）：
那几处已经有各自的假库与登录壳，搬过来只会多一份重复的装配。
"""

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.auth import DEV_KEY_POLICY, DEV_KEY_PLAN, KeyContext
from app.core.profile_presets import (
    DEFAULT_PRESETS,
    ProfilePresetSpec,
    reset_profile_presets,
    set_profile_presets,
)
from app.core.exceptions import AppError, ErrorCode
from app.models.key_policy import (
    PROFILE_CUSTOM,
    PROFILE_QUALITY,
    PROFILE_SPEED,
    KeyPolicySnapshot,
)
from app.models.plan import PLAN_FREE, PLAN_PRESETS, PLAN_PRO, PLAN_STANDARD, PlanSpec
from app.services.profile_policy import ensure_policy_consistent, resolve_retrieval_options
from app.services.retrieval_options import ProfileChain, profile_chain_of, tenant_policy_echo
from tests.fake_db import AsyncSessionAdapter, EmptySession, async_visible_kb

TENANT_ID = "tenant_demo"
RETRIEVE_PATH = "/api/v1/rag/retrieve"
KB = "kb-a"


def _policy(
    *,
    profile: str = PROFILE_CUSTOM,
    custom_options: tuple[tuple[str, object], ...] = (),
    orchestration_allowed: bool = False,
    hybrid_allowed: bool = True,
    rerank_allowed: bool = True,
    query_rewrite_allowed: bool = True,
    research_allowed: bool = False,
) -> KeyPolicySnapshot:
    """一份只关心能力开关与档位的快照；其余字段取内置开发身份那份。"""

    return dataclasses.replace(
        DEV_KEY_POLICY,
        profile=profile,
        custom_options=custom_options,
        orchestration_allowed=orchestration_allowed,
        hybrid_allowed=hybrid_allowed,
        rerank_allowed=rerank_allowed,
        query_rewrite_allowed=query_rewrite_allowed,
        research_allowed=research_allowed,
    )


def _plan(code: str, **overrides: Any) -> PlanSpec:
    """三档种子之一，按需改几列（模拟管理员在控制台把某一档调低）。"""

    return dataclasses.replace(PLAN_PRESETS[code], **overrides)


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    """每个用例都从内置种子出发；改过的档位预设不许漏进下一个用例。"""

    reset_profile_presets()
    try:
        yield
    finally:
        reset_profile_presets()


def _want_orchestration(profile: str = PROFILE_QUALITY) -> None:
    """把某一档的「想要」打开（运营在面板上做的事，写进进程级注册表）。"""

    spec = DEFAULT_PRESETS[profile]
    set_profile_presets(
        {
            profile: ProfilePresetSpec(
                mode=spec.mode,
                top_k=spec.top_k,
                rerank=spec.rerank,
                rewrite=spec.rewrite,
                orchestration=True,
            )
        }
    )


# ───────────────────────────────────────────── 三层：各自否决一次


def test_orchestration_is_effective_only_when_all_three_layers_agree() -> None:
    """套餐允许 + Key 启用 + 档位想要 → 生效，且回显与生效值同源。

    ``tenant_policy`` 只放「本次真实生效、且改变了结果」的项：编排在 #147 之前没有消费者，
    所以回显当时被推迟（#146 的收口判断）；#147 把它接进链路之后，它和重排 / 改写一样
    出现在回显里，并且**从同一个 ``options`` 上读**（ADR-0010「第七次修订」）。
    """

    _want_orchestration(PROFILE_QUALITY)

    options = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_QUALITY, orchestration_allowed=True),
        plan=_plan(PLAN_PRO),
        kb_count=1,
    )

    assert options.orchestration is True
    assert tenant_policy_echo(options, kb_count=1)["orchestration"] is True


def test_the_tier_preset_keeps_orchestration_off_by_default() -> None:
    """档位不带：四个档位的种子一个都没打开编排，于是别的两层都同意也不生效。

    这就是「默认关闭」的可断言形态：要开得有人先在面板上打开这一档。
    """

    assert {spec.orchestration for spec in DEFAULT_PRESETS.values()} == {False}

    options = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_QUALITY, orchestration_allowed=True),
        plan=_plan(PLAN_PRO),
        kb_count=1,
    )

    assert options.orchestration is False


def test_the_key_config_switch_off_keeps_orchestration_off() -> None:
    """Key 不要：档位想要、套餐也允许，但这一套集成没启用 → 不生效，也不是错误。

    「这套集成要不要」是配置层的选择，与「这个租户买没买」是两件事：后者越权要
    拒绝（名字是契约），前者只是这一把 Key 不用它。
    """

    _want_orchestration(PROFILE_QUALITY)

    options = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_QUALITY, orchestration_allowed=False),
        plan=_plan(PLAN_PRO),
        kb_count=1,
    )

    assert options.orchestration is False


def test_a_named_tier_that_wants_it_without_the_plan_is_rejected_not_degraded() -> None:
    """套餐不给（命名档位）：拒绝。档位名是产品契约，悄悄少一步就是另一个档。"""

    _want_orchestration(PROFILE_QUALITY)

    with pytest.raises(AppError) as excinfo:
        resolve_retrieval_options(
            policy=_policy(profile=PROFILE_QUALITY, orchestration_allowed=True),
            plan=_plan(PLAN_PRO, orchestration_allowed=False),
            kb_count=1,
        )

    assert excinfo.value.code is ErrorCode.FEATURE_NOT_ALLOWED
    assert "orchestration" in excinfo.value.msg


def test_a_custom_tier_over_the_plan_is_clipped_and_echoed() -> None:
    """套餐不给（``custom`` 档）：裁剪 + 回显，与重排 / 改写同一个反应。

    裁剪而不回显等于欺骗：调用方以为编排跑了，实际没有，而响应里没有任何一处说明。
    """

    options = resolve_retrieval_options(
        policy=_policy(
            profile=PROFILE_CUSTOM,
            custom_options=(("mode", "hybrid"), ("orchestration", True)),
            orchestration_allowed=True,
        ),
        plan=_plan(PLAN_STANDARD),
        kb_count=1,
    )

    assert options.orchestration is False
    assert options.tenant_policy is not None
    assert options.tenant_policy.clipped == ("orchestration",), "裁剪必须回显被裁字段"
    echo = tenant_policy_echo(options, kb_count=1)
    assert echo["clipped"] == ["orchestration"]


def test_the_echo_reads_orchestration_from_the_effective_value() -> None:
    """回显从 ``options`` 上读：三层判完之后的值是什么，回显就是什么，不许各存一份。

    #146 时这条回显被刻意推迟（``tenant_policy`` 只放「真的改变了结果」的项，而编排当时
    没有消费者）。#147 接上消费者之后它随生效值一起进来，位置与语义都与 ``rerank`` /
    ``rewrite`` 一致——同源，所以「回显说开了编排」与「链路真的编排了」不可能分叉
    （ADR-0010「第七次修订」）。
    """

    _want_orchestration(PROFILE_QUALITY)
    options = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_QUALITY, orchestration_allowed=True),
        plan=_plan(PLAN_PRO),
        kb_count=1,
    )

    echo = tenant_policy_echo(options, kb_count=1)
    assert options.orchestration is True
    assert echo["orchestration"] is options.orchestration, "回显与生效值同源，不是另存一份"

    # 三层里任意一层否决：回显跟着变 False，而不是留在「想要」那一侧。
    off = resolve_retrieval_options(
        policy=_policy(profile=PROFILE_QUALITY, orchestration_allowed=False),
        plan=_plan(PLAN_PRO),
        kb_count=1,
    )
    assert tenant_policy_echo(off, kb_count=1)["orchestration"] is False


def test_the_custom_tier_reads_the_manual_switch_from_its_options() -> None:
    """``custom`` 档的「想要」写在手动参数里，缺键 = 不要（不擅自升级）。"""

    assert (
        profile_chain_of(
            _policy(
                profile=PROFILE_CUSTOM,
                custom_options=(("orchestration", True),),
            )
        ).orchestration
        is True
    )
    assert (
        profile_chain_of(_policy(profile=PROFILE_CUSTOM, custom_options=())).orchestration is False
    )


def test_a_named_tier_expands_its_orchestration_intent_from_the_preset() -> None:
    """命名档位的「想要」只来自档位预设，配置里的手动参数一个都不读（完整替换）。"""

    _want_orchestration(PROFILE_SPEED)

    chain = profile_chain_of(
        _policy(profile=PROFILE_SPEED, custom_options=(("orchestration", False),))
    )

    assert chain == ProfileChain(
        mode="vector", top_k=5, rerank=False, rewrite=False, orchestration=True
    )


# ───────────────────────────────────────────── 配置时自洽校验（ADR-0018 第 2 条）


def test_a_config_that_wants_orchestration_must_enable_the_switch() -> None:
    """第一段包含：档位想要编排 ⊆ 配置启用编排——否则这套配置落地即自相矛盾。"""

    _want_orchestration(PROFILE_QUALITY)

    with pytest.raises(AppError) as excinfo:
        ensure_policy_consistent(
            policy=_policy(profile=PROFILE_QUALITY, orchestration_allowed=False),
            plan=_plan(PLAN_PRO),
        )

    assert excinfo.value.code is ErrorCode.PARAM_ERROR
    assert "orchestration" in excinfo.value.msg


def test_a_policy_that_enables_orchestration_beyond_the_plan_is_rejected() -> None:
    """第二段包含：配置启用编排 ⊆ 套餐允许编排——配置不能凭空造能力。"""

    with pytest.raises(AppError) as excinfo:
        ensure_policy_consistent(
            policy=_policy(
                profile=PROFILE_SPEED,
                # 只留编排这一个越权项：其余能力关掉，报错才指向本票这一列。
                hybrid_allowed=False,
                rerank_allowed=False,
                query_rewrite_allowed=False,
                orchestration_allowed=True,
            ),
            plan=_plan(PLAN_STANDARD),
        )

    assert excinfo.value.code is ErrorCode.PARAM_ERROR
    assert "orchestration" in excinfo.value.msg


# ───────────────────────────────────────────── ASGI 边界：回显与既有行为不变


class _FakeEmbedding:
    async def embed_query(self, _query: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _FakeVectorStore:
    async def similarity_search_scope(self, _db, _vector, *, tenant_id, index_versions, top_k):
        return [
            {
                "document_id": f"doc-{kb_id}",
                "chunk_id": f"chunk-{kb_id}",
                "kb_id": kb_id,
                "title": "错误码手册",
                "content": "错误码 E1024 表示证书过期。",
                "score": 0.9,
            }
            for kb_id in index_versions
        ]


class _FakeKeyword:
    available = True

    async def keyword_search(self, query, *, tenant_id, index_versions, top_k):
        return [
            {
                "chunk_id": "chunk-hit",
                "document_id": "doc-hit",
                "kb_id": KB,
                "title": "错误码手册",
                "content": "错误码 E1024 表示证书过期。",
                "score": 12.5,
            }
        ]


class _Entry:
    """一条 HTTP 入口 + 一个可改的身份（改套餐 / 改配置就是改这里）。"""

    def __init__(self, client: TestClient, holder: dict[str, KeyContext]) -> None:
        self.client = client
        self._holder = holder

    def use(self, *, policy: KeyPolicySnapshot, plan: PlanSpec) -> None:
        self._holder["context"] = KeyContext(
            tenant_id=TENANT_ID, key_id="key-under-test", policy=policy, plan=plan
        )

    def post(self) -> dict:
        response = self.client.post(RETRIEVE_PATH, json={"kb_ids": [KB], "query": "E1024"})
        assert response.status_code == 200
        return response.json()


@pytest.fixture
def entry(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Entry]:
    """离线 ASGI 客户端；身份由测试直接指定（与既有的门禁用例同一套做法）。"""

    main = importlib.import_module("app.main")
    async_session_mod = importlib.import_module("app.db.async_session")
    service = importlib.import_module("app.services.rag_service")
    kb_repo = importlib.import_module("app.repositories.knowledge_base_repository")

    monkeypatch.setattr(service, "get_embedding_provider", _FakeEmbedding)
    monkeypatch.setattr(service, "get_vector_store", _FakeVectorStore)
    monkeypatch.setattr(service, "get_keyword_search_provider", _FakeKeyword)
    monkeypatch.setattr(kb_repo, "get_by_id_for_tenant", async_visible_kb)

    from app.api.deps import get_current_key

    holder: dict[str, KeyContext] = {
        "context": KeyContext(tenant_id=TENANT_ID, policy=DEV_KEY_POLICY, plan=DEV_KEY_PLAN)
    }
    main.app.dependency_overrides[async_session_mod.get_async_db] = lambda: AsyncSessionAdapter(
        EmptySession()
    )
    main.app.dependency_overrides[get_current_key] = lambda: holder["context"]
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            yield _Entry(client, holder)
    finally:
        main.app.dependency_overrides.clear()


def test_the_echo_follows_the_effective_value_and_the_material_stays_put(
    entry: _Entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关在两个方向上都可观察：回显跟着生效值走，检索材料一个字不变。

    #146 时这条钉的是「回显里还没有它」；#147 把消费者接上之后，回显与编排块一起出现
    ——正是当时写下的那句「回显随生效值真的改变结果一起进来」。开关只**增加**编排这一块，
    改不了交付单元本身：编排是组织材料，不是重新检索。
    """

    _want_orchestration(PROFILE_SPEED)

    class _Model:
        """两次结构化调用的替身；本用例只关心「开关带来了什么、没带走什么」。"""

        async def chat_structured(self, schema: Any, *, system: str, user: str) -> Any:
            if schema.__name__ == "PointList":
                return {"points": ["错误码含义"]}
            return {"points": [{"point": 1, "primary": [1]}]}

    async def _fake_llm(role: str = "fast") -> Any:
        return _Model()

    monkeypatch.setattr("app.providers.llm.get_llm_provider", _fake_llm)

    entry.use(
        policy=_policy(profile=PROFILE_SPEED, orchestration_allowed=False),
        plan=_plan(PLAN_PRO),
    )
    off = entry.post()["data"]

    entry.use(
        policy=_policy(profile=PROFILE_SPEED, orchestration_allowed=True),
        plan=_plan(PLAN_PRO),
    )
    on = entry.post()["data"]

    assert off["metadata"]["tenant_policy"]["orchestration"] is False
    assert on["metadata"]["tenant_policy"]["orchestration"] is True
    assert "orchestration" not in off, "关闭时连键都不出现"
    assert on["orchestration"]["completeness"] == "complete"
    assert on["retrieved_chunks"] == off["retrieved_chunks"], "开关不许改变检索材料"
    assert on["metadata"]["mode"] == off["metadata"]["mode"]
    assert on["metadata"]["retrieval"]["degraded"] is False


def test_a_plan_downgrade_turns_orchestration_off_on_the_next_call(entry: _Entry) -> None:
    """管理员事后把套餐的编排收回去 → 下一次调用就按新套餐判（不静默降级成不编排）。"""

    _want_orchestration(PROFILE_QUALITY)
    policy = _policy(profile=PROFILE_QUALITY, orchestration_allowed=True)

    entry.use(policy=policy, plan=_plan(PLAN_PRO))
    assert entry.post()["code"] == ErrorCode.SUCCESS

    entry.use(policy=policy, plan=_plan(PLAN_PRO, orchestration_allowed=False))

    body = entry.post()
    assert body["code"] == ErrorCode.FEATURE_NOT_ALLOWED
    assert body["data"] is None


# ───────────────────────────────────────────── 迁移：一条新 revision、可升可降

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"

#: 本票接在它后面（票 #132 的反馈表，本步此前的唯一 head）。
PREVIOUS_HEAD = "a91f3c7e2058"

#: 迁移要加的三个布尔列：``(表, 列)``。三层的名字刻意不同：套餐/配置是「允许 / 启用」，
#: 档位是「想要」，它们表达的不是同一件事。
EXPECTED_COLUMNS = (
    ("plans", "orchestration_allowed"),
    ("key_policies", "orchestration_allowed"),
    ("profile_presets", "orchestration"),
)

_REVISION_RE = re.compile(r"""^revision(?::\s*str)?\s*=\s*['"](?P<revision>[^'"]+)['"]""", re.M)
_DOWN_REVISION_RE = re.compile(
    r"""^down_revision(?::\s*[^=]+)?\s*=\s*(?P<down_revision>.+)$""", re.M
)


class _RecordingOp:
    """只记录加列 / 删列：本条迁移是「纯加列」，出现别的 op 就该先炸。"""

    def __init__(self) -> None:
        self.added: list[tuple[str, Any]] = []
        self.dropped: list[tuple[str, str]] = []

    def add_column(self, table_name: str, column: Any) -> None:
        self.added.append((table_name, column))

    def drop_column(self, table_name: str, column_name: str, **_kwargs: Any) -> None:
        self.dropped.append((table_name, column_name))

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"本票的迁移不该用到 op.{name}：它只加列、只删列")


def _migration_path(slug: str) -> Path:
    matches = sorted(VERSIONS_DIR.glob(f"*_{slug}.py"))
    assert len(matches) == 1, f"{slug}: 期望恰好一个迁移文件，实际 {matches}"
    return matches[0]


def _load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"t146_migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _down_revision_of(path: Path) -> str | None:
    match = _DOWN_REVISION_RE.search(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path.name}: 找不到 down_revision"
    return match.group("down_revision").strip().strip("'\"")


def _revision_of(path: Path) -> str:
    match = _REVISION_RE.search(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path.name}: 找不到 revision"
    return match.group("revision")


def test_the_orchestration_migration_sits_on_top_of_the_single_head() -> None:
    """整条链仍然只有一个 head；本票这条接在它之前的 head 上（``alembic heads`` 的文本版）。

    #141 的索引构建版本表与本票并行开发，合并时接在**本票之后**——于是链尾前移到
    ``add_index_build_versions``。按仓库惯例，加最后一条迁移的那个人改这一行，
    「链尾是哪个文件」要人看一眼确认。
    """

    revisions = {_revision_of(path): path for path in VERSIONS_DIR.glob("*.py")}
    pointed_at = {
        _down_revision_of(path)
        for path in VERSIONS_DIR.glob("*.py")
        if _down_revision_of(path) is not None
    }
    heads = sorted(set(revisions) - pointed_at)

    path = _migration_path("add_orchestration_switch")
    assert _down_revision_of(path) == PREVIOUS_HEAD, "本条迁移必须接在本步此前的唯一 head 上"
    assert len(heads) == 1, f"整条链应当只有一个 head，实际 {heads}"
    assert heads == [_revision_of(_migration_path("create_section_edges"))], (
        f"链尾应当是 #152 的关系边表（#141 的版本表与 #150 的配额列之后），实际 {heads}"
    )


def test_the_orchestration_migration_adds_three_columns_with_a_false_backfill() -> None:
    """加三列、``NOT NULL``、``server_default false``——存量行因此在升级时就拿到 ``False``。"""

    module = _load_migration(_migration_path("add_orchestration_switch"))
    recorder = _RecordingOp()

    module.op = recorder
    module.upgrade()

    assert [(table, column.name) for table, column in recorder.added] == list(EXPECTED_COLUMNS)
    for table, column in recorder.added:
        assert column.nullable is False, f"{table}.{column.name} 必须非空"
        assert "false" in str(column.server_default.arg).lower(), (
            f"{table}.{column.name} 必须回填 False"
        )
        assert column.type.python_type is bool


def test_the_orchestration_migration_downgrade_drops_exactly_what_it_added() -> None:
    """可升可降：回滚只删这三列，留下的是「改造前那张表」。"""

    module = _load_migration(_migration_path("add_orchestration_switch"))
    recorder = _RecordingOp()

    module.op = recorder
    module.downgrade()

    assert recorder.added == []
    assert recorder.dropped == list(EXPECTED_COLUMNS)


def test_the_three_layers_are_disabled_in_the_shipped_seeds() -> None:
    """种子值就是票面：套餐 free / standard 不给、pro 给；三个命名档位都不要。"""

    assert PLAN_PRESETS[PLAN_FREE].orchestration_allowed is False
    assert PLAN_PRESETS[PLAN_STANDARD].orchestration_allowed is False
    assert PLAN_PRESETS[PLAN_PRO].orchestration_allowed is True

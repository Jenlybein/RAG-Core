"""#143 · 关键词索引迁移：``ELASTICSEARCH_INDEX`` 从物理索引名变成别名。

既有部署里那个叫 ``rag_chunks`` 的物理索引必须改名成 ``rag_chunks__v1`` 并**挂成别名**，
否则激活新版本时两份数据各查一半、而旧文档缺 ``index_version`` 会让关键词这一路静默变空。

这里验四件事（都打在脚本的纯函数与「假客户端」上，不连 ES）：

* ``reindex_body`` 一定带**回填** ``index_version`` 的脚本（漏了就是静默变空）；
* 三种形态（已是别名 / 物理索引 / 不存在）各自走对路，且**幂等**；
* dry-run 一个写都不发；``--execute`` 才有动作，且顺序是「建 → 搬 → 核对 → 删 → 挂别名」；
* 条数对不上 / reindex 报错时**停在删除之前**（那一步之后没有回头路）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ALIAS = "rag_chunks"
VERSION = "v1"
DEST = "rag_chunks__v1"


def _load_script(module_name: str) -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "scripts" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"t143_migrate_{module_name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


@pytest.fixture()
def module() -> ModuleType:
    return _load_script("migrate_keyword_alias")


class _FakeIndices:
    """``client.indices`` 的替身：记下全部调用，按预置状态回答。"""

    def __init__(
        self,
        *,
        existing: set[str],
        aliased: bool,
        created_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._existing = set(existing)
        self._aliased = aliased
        self.created_kwargs: dict[str, Any] = {}

    async def exists_alias(self, *, name: str) -> bool:
        self.calls.append(("exists_alias", name))
        return self._aliased and name == ALIAS

    async def exists(self, *, index: str) -> bool:
        self.calls.append(("exists", index))
        return index in self._existing

    async def create(self, *, index: str, **kwargs: Any) -> None:
        self.calls.append(("create", index))
        self._existing.add(index)
        self.created_kwargs = kwargs

    async def delete(self, *, index: str) -> None:
        self.calls.append(("delete", index))
        self._existing.discard(index)

    async def update_aliases(self, *, actions: list[dict[str, Any]]) -> None:
        self.calls.append(("update_aliases", actions))
        self._aliased = True

    @property
    def mutations(self) -> list[tuple[Any, ...]]:
        """所有「改了集群」的调用（dry-run 之后必须为空）。"""

        return [call for call in self.calls if call[0] in {"create", "delete", "update_aliases"}]


class _FakeClient:
    """``AsyncElasticsearch`` 的替身：``count`` / ``reindex`` / ``indices``。"""

    def __init__(
        self,
        *,
        source_documents: int = 7,
        dest_documents: int | None = None,
        reindex_failures: list[dict[str, Any]] | None = None,
        aliased: bool = False,
        existing: set[str] | None = None,
    ) -> None:
        self.indices = _FakeIndices(
            existing={ALIAS} if existing is None else existing, aliased=aliased
        )
        #: ``dest_documents`` 模拟「搬完之后目标索引里的条数」：默认与源一致，给一个更小的
        #: 值就是「搬丢了」——那条路径必须在删旧索引之前停下。
        self._reindex_written = dest_documents
        self.counts = {ALIAS: source_documents, DEST: 0}
        self.reindex_bodies: list[dict[str, Any]] = []
        self._reindex_failures = reindex_failures or []

    async def count(self, *, index: str) -> dict[str, int]:
        return {"count": self.counts.get(index, 0)}

    async def reindex(self, body: dict[str, Any]) -> dict[str, Any]:
        self.reindex_bodies.append(body)
        dest = body["dest"]["index"]
        self.counts[dest] = (
            self.counts.get(body["source"]["index"], 0)
            if self._reindex_written is None
            else self._reindex_written
        )
        return {"total": self.counts[dest], "failures": self._reindex_failures}


# ───────────────────────────────────────────────────────── 纯函数：请求体与动作


def test_reindex_body_backfills_the_index_version(module: ModuleType) -> None:
    """回填 ``index_version`` 是这次迁移最不能漏的一步（漏了就是静默变空）。"""

    body = module.reindex_body(source=ALIAS, dest=DEST, version=VERSION)

    assert body["source"] == {"index": ALIAS}
    assert body["dest"] == {"index": DEST}
    script = body["script"]
    assert "index_version" in script["source"]
    assert "null" in script["source"], "只在缺失时回填：别的版本的数据不许被改写"
    assert script["params"] == {"version": VERSION}


def test_add_alias_actions_point_the_alias_at_the_physical_index(
    module: ModuleType,
) -> None:
    assert module.add_alias_actions(alias=ALIAS, index=DEST) == [
        {"add": {"index": DEST, "alias": ALIAS}}
    ]


# ───────────────────────────────────────────────────────── 三种形态与幂等


@pytest.mark.parametrize(
    "aliased, existing, expected",
    [
        (True, {ALIAS, DEST}, "alias"),
        (False, {ALIAS}, "physical"),
        (False, set(), "absent"),
    ],
)
def test_detect_state_tells_the_three_shapes_apart(
    module: ModuleType, aliased: bool, existing: set[str], expected: str
) -> None:
    client = _FakeClient(aliased=aliased, existing=existing)

    state = asyncio.run(module.detect_state(client, alias=ALIAS))

    assert state.value == expected


def test_an_already_migrated_alias_is_a_no_op(module: ModuleType) -> None:
    client = _FakeClient(aliased=True, existing={ALIAS, DEST})

    outcome = asyncio.run(
        module.migrate_keyword_alias(client, alias=ALIAS, version=VERSION, execute=True)
    )

    assert outcome.state.value == "alias"
    assert not outcome.needed
    assert not outcome.applied
    assert client.indices.mutations == [], "已经是别名时一个写都不许发"
    assert client.reindex_bodies == []


def test_a_fresh_deployment_has_nothing_to_migrate(module: ModuleType) -> None:
    client = _FakeClient(existing=set())

    outcome = asyncio.run(
        module.migrate_keyword_alias(client, alias=ALIAS, version=VERSION, execute=True)
    )

    assert outcome.state.value == "absent"
    assert not outcome.needed
    assert client.indices.mutations == []
    assert any("__v1" in message for message in outcome.messages)


# ───────────────────────────────────────────────────────── dry-run 与执行


def test_dry_run_reads_but_changes_nothing(module: ModuleType) -> None:
    client = _FakeClient(source_documents=7)

    outcome = asyncio.run(
        module.migrate_keyword_alias(client, alias=ALIAS, version=VERSION, execute=False)
    )

    assert outcome.needed
    assert not outcome.applied
    assert outcome.source_documents == 7
    assert client.indices.mutations == []
    assert client.reindex_bodies == []
    assert any("--execute" in message for message in outcome.messages)


def test_execute_creates_reindexes_verifies_then_swaps(
    module: ModuleType,
) -> None:
    client = _FakeClient(source_documents=7)

    outcome = asyncio.run(
        module.migrate_keyword_alias(client, alias=ALIAS, version=VERSION, execute=True)
    )

    assert outcome.applied
    assert outcome.dest_documents == 7
    assert [call[0] for call in client.indices.calls] == [
        "exists_alias",
        "exists",
        "exists",  # 目标索引还没有
        "create",
        "delete",
        "update_aliases",
    ]
    assert client.indices.created_kwargs, "建索引必须带 app 的 mapping"
    assert "ik_max_word" in str(client.indices.created_kwargs), "中文分词器不能丢"
    assert client.reindex_bodies[0]["dest"] == {"index": DEST}
    assert client.indices.calls[-1] == (
        "update_aliases",
        [{"add": {"index": DEST, "alias": ALIAS}}],
    )


def test_count_mismatch_stops_before_deleting_the_old_index(
    module: ModuleType,
) -> None:
    """条数对不上就停：删旧索引之后没有回头路。"""

    client = _FakeClient(source_documents=7, dest_documents=5)

    with pytest.raises(module.MigrationRefused):
        asyncio.run(
            module.migrate_keyword_alias(client, alias=ALIAS, version=VERSION, execute=True)
        )

    assert ("delete", ALIAS) not in client.indices.calls
    assert not any(call[0] == "update_aliases" for call in client.indices.calls)


def test_reindex_failures_stop_before_deleting_the_old_index(
    module: ModuleType,
) -> None:
    client = _FakeClient(reindex_failures=[{"reason": "mapper_parsing_exception"}])

    with pytest.raises(module.MigrationRefused):
        asyncio.run(
            module.migrate_keyword_alias(client, alias=ALIAS, version=VERSION, execute=True)
        )

    assert ("delete", ALIAS) not in client.indices.calls

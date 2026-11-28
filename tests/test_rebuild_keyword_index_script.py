"""`scripts/rebuild_keyword_index.py`：默认只读、以及「删别名 ≠ 删索引」这一处分叉。

## 为什么值得单独测

这个脚本是关键词索引去版本化之后**唯一**的批量重建入口，而上线动作的失败方式是安静的那种：

* 默认就该只打印计划——少一个 `if args.apply`，一条命令就能把线上索引删掉；
* 老部署的 `ELASTICSEARCH_INDEX` 可能是一个**别名**（指向 `<名字>__vN`）。这时只删别名会
  留下那张物理索引、新代码又不会重建它，于是关键词路**永久空**；反过来把别名当索引删，
  报错信息又完全看不出原因。这两个分支必须按「别名连物理索引一起删、物理索引直接删」
  钉住。

ES 客户端与数据库连接都在用例里换掉：这一层要证明的是**决定**，不是连通性。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from scripts import rebuild_keyword_index as script

pytest_plugins = ("pytest_asyncio",)


def _client(monkeypatch: pytest.MonkeyPatch, *, aliases: dict, exists: bool) -> dict[str, Any]:
    """把 `Elasticsearch` 换成记录器：返回它收到的删除调用。"""

    calls: dict[str, Any] = {"deleted": []}

    class _Fake:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def indices_get_alias(self, name: str) -> dict:
            return aliases

        def indices_exists(self, index: str) -> bool:
            return exists

        def indices_delete(self, index: str, ignore_unavailable: bool = True) -> None:
            calls["deleted"].append(index)

        # 真实客户端的方法名（脚本里用的是 client.indices.*，这里用同名转发）
        @property
        def indices(self) -> Any:  # pragma: no cover - 只在本替身的简单用法下走
            class _Indices:
                def get_alias(self, *, name: str, ignore_unavailable: bool = True) -> dict:
                    return aliases

                def exists(self, *, index: str) -> bool:
                    return exists

                def delete(self, *, index: str, ignore_unavailable: bool = True) -> None:
                    calls["deleted"].append(index)

            return _Indices()

        def close(self) -> None:
            calls["closed"] = True

        # 上面 `indices_*` 那组是脚本第一版写法留下的；保留它们是为了让替身对
        # 「脚本改回直接调 client.xxx」也成立——两条路径都指向同一份记录器。
        def __getattr__(self, item: str) -> Any:  # pragma: no cover
            raise AssertionError(f"脚本用了替身没准备的方法：{item}")

    monkeypatch.setattr("elasticsearch.Elasticsearch", _Fake)
    return calls


def test_an_alias_is_deleted_together_with_its_physical_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ELASTICSEARCH_INDEX` 还是别名（老部署）：只删别名会让关键词路永久空。"""

    calls = _client(monkeypatch, aliases={"rag_chunks__v4": ["rag_chunks"]}, exists=True)

    script._clear_index()

    assert calls["deleted"] == ["rag_chunks__v4"], (
        "别名下面挂着的物理索引必须一起删；只删别名 = 关键词路永远查不到"
    )
    assert calls.get("closed") is True, "客户端要关掉，否则脚本跑完进程挂着连接"


def test_a_plain_physical_index_is_deleted_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _client(monkeypatch, aliases={}, exists=True)

    script._clear_index()

    assert calls["deleted"] == ["rag_chunks"]


def test_a_missing_index_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """索引本来就不存在（全新部署）：不许抛，也不许「删一个不存在的东西」。"""

    calls = _client(monkeypatch, aliases={}, exists=False)

    script._clear_index()

    assert calls["deleted"] == []


def _plan(monkeypatch: pytest.MonkeyPatch, rows: list[tuple[str, str, str]]) -> None:
    async def _fake_plan() -> tuple[list[tuple[str, str, str]], int]:
        return rows, len(rows)

    monkeypatch.setattr(script, "_plan", _fake_plan)


def test_the_default_run_deletes_nothing_and_dispatches_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """默认只读：没有 `--apply` 就不许删索引、不许投任务。"""

    _plan(monkeypatch, [("doc-1", "tenant-a", "kb-1")])
    cleared: list[int] = []
    monkeypatch.setattr(script, "_clear_index", lambda: cleared.append(1))

    async def _apply(_rows: object) -> int:  # pragma: no cover - 不该被调到
        raise AssertionError("dry-run 不许执行 apply")

    monkeypatch.setattr(script, "_apply", _apply)

    assert script.main([]) == 0
    assert cleared == [], "dry-run 不许删索引"
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "待重建 1 篇" in out


def test_apply_dispatches_and_reports_a_truthful_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--apply` 才真的做；回执是「投了几篇 / 计划几篇」（抢不到的篇不算进去）。"""

    _plan(monkeypatch, [("doc-1", "tenant-a", "kb-1"), ("doc-2", "tenant-a", "kb-1")])
    applied: list[object] = []

    async def _apply(rows: object) -> int:
        applied.append(rows)
        return 2

    monkeypatch.setattr(script, "_apply", _apply)

    assert script.main(["--apply"]) == 0
    assert len(applied) == 1, "--apply 才走真正的执行路径"
    out = capsys.readouterr().out
    assert "已投递 2 / 2 篇" in out


def test_limit_caps_one_round(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--limit` 支持分批：一轮只投这么多，剩下的下一轮再跑。"""

    _plan(monkeypatch, [(f"doc-{index}", "tenant-a", "kb-1") for index in range(5)])
    seen: list[list[tuple[str, str, str]]] = []

    async def _apply(rows: object) -> int:
        seen.append(list(rows))  # type: ignore[arg-type]
        return len(seen[0])

    monkeypatch.setattr(script, "_apply", _apply)

    assert script.main(["--apply", "--limit", "2"]) == 0
    assert len(seen[0]) == 2
    assert "这一轮只投前 2 篇" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_apply_clears_the_index_before_it_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_apply` 自己负责清索引：先清、再逐篇置排队中并投递（顺序反了就是往旧索引里写）。"""

    order: list[str] = []
    dispatched: list[str] = []

    monkeypatch.setattr(script, "_clear_index", lambda: order.append("clear"))

    class _FakeSession:
        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _FakeFactory:
        def __call__(self) -> _FakeSession:
            return _FakeSession()

    monkeypatch.setattr(script, "async_session_factory", lambda: _FakeFactory())

    class _Tx:
        def __init__(self, _db: object) -> None:
            pass

        async def __aenter__(self) -> "_Tx":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(script, "async_transaction", _Tx)

    async def _prepare(
        _db: object, document_id: str, *, tenant_id: str, allow_success: bool
    ) -> bool:
        return True

    monkeypatch.setattr(script.document_repository, "prepare_for_indexing_async", _prepare)
    monkeypatch.setattr(
        script, "enqueue_document", lambda **kwargs: dispatched.append(kwargs["document_id"])
    )

    count = await script._apply([("doc-1", "tenant-a", "kb-1"), ("doc-2", "tenant-a", "kb-1")])

    assert order == ["clear"], "先清索引，再投递"
    assert count == 2
    assert dispatched == ["doc-1", "doc-2"]


@pytest.fixture(autouse=True)
def _windows_loop_policy() -> Iterator[None]:
    """脚本入口在 Windows 上切事件循环策略；用例里只跑函数体，不需要真的切。"""

    yield

"""票 #130：控制面的**写入**换到新栈，迁移期的两处脚手架随之消失。

这份文件只钉「形状」与「脚手架不在场」两件事——行为（审计内容、行数、错误码、自洽校验）
由既有的 ``test_console_*.py`` 继续断言，本票不改那些断言，只换它们脚下的会话。

三条守卫，各自防一种「改了一半也全绿」：

1. **每一条控制面入口都从异步栈取会话**（依赖树里是 ``get_async_db``，不是 ``get_db``）。
   默认套件离线、没有真库，「这条路径走的是哪套会话」只能从依赖树上读出来；靠人肉
   grep 的话，下一个加控制面接口的人一定会漏。这条守卫覆盖 ``/console`` 下的**全部**
   路由（从 ``app.routes`` 现取，不是手写清单），所以新接口自动被它管住。
2. **写入口声明异步事务**：写路径的提交点是 ``Depends(get_async_transaction)``，
   一个请求恰好一笔。少声明一条就是「这次改动没有事务边界」——审计与业务行会各落各的。
3. **过渡脚手架已经删除**：``app/db/dual_session.py`` 与 ``app/core/sync_bridge.py``
   连同它们的调用点一起消失。它们的删除条件写在各自的模块文档里，本票就是那个条件
   被满足的时刻；留着它们，下一个改控制面的人会以为「两种会话还能混着喂」。
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path
from typing import Any

import pytest

#: 控制面里**会写库**的入口（方法, 路径）。``GET .../secret`` 也在其中：它是读门，
#: 但每次调用都写一条 ``model.reveal`` 审计，所以它必须有事务边界。
CONSOLE_WRITE_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/console/login"),
    ("POST", "/console/logout"),
    ("PATCH", "/console/plans/{plan_id}"),
    ("PATCH", "/console/accounts/{account_id}"),
    ("POST", "/console/knowledge-bases"),
    ("DELETE", "/console/knowledge-bases/{kb_id}"),
    ("POST", "/console/knowledge-bases/{kb_id}/documents"),
    ("POST", "/console/documents/{document_id}/reindex"),
    ("DELETE", "/console/documents/{document_id}"),
    ("POST", "/console/keys"),
    ("DELETE", "/console/keys/{key_id}"),
    ("PATCH", "/console/key-policies/{policy_id}"),
    ("PATCH", "/console/settings"),
    ("PATCH", "/console/profile-presets/{profile}"),
    ("POST", "/console/model-configs"),
    ("PATCH", "/console/model-configs/{config_id}"),
    ("DELETE", "/console/model-configs/{config_id}"),
    ("GET", "/console/model-configs/{config_id}/secret"),
)

#: 仓库根（``tests/`` 的上一级）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 用 POST 但不写库的控制面路由：``POST /console/eval/side-evidence`` 只投递一次 Celery
#: 跑批，没有任何数据库写入——它不该被要求声明事务依赖，也不该被那张清单漏掉后无声通过。
NON_DB_WRITE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {("POST", "/console/eval/side-evidence")}
)


def _dependant_calls(dependant: Any, seen: list[Any] | None = None) -> list[Any]:
    """把一条路由的依赖树摊平成「可调用对象」列表（含路由函数自己）。"""

    found = [] if seen is None else seen
    found.append(dependant.call)
    for child in dependant.dependencies:
        _dependant_calls(child, found)
    return found


def _routes() -> dict[tuple[str, str], Any]:
    """``(方法, 路径)`` → 路由对象；路径含占位符时按声明原样比对。"""

    app = importlib.import_module("app.main").app
    routes: dict[tuple[str, str], Any] = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", set()) or set():
            routes[(method, path)] = route
    return routes


def _console_route_keys() -> list[tuple[str, str]]:
    """全部 ``/console`` 路由（含 ``/console/admin``），按路径排序后返回，便于参数化。"""

    return sorted(
        key for key in _routes() if key[1].startswith("/console")
    )


@pytest.mark.parametrize(("method", "path"), _console_route_keys())
def test_every_console_route_takes_its_session_from_the_async_stack(
    method: str, path: str
) -> None:
    """控制面不再有第二条会话来源：依赖树里只能出现 ``get_async_db``。"""

    from app.db.async_session import get_async_db
    from app.db.session import get_db

    calls = _dependant_calls(_routes()[(method, path)].dependant)

    assert get_db not in calls, f"{method} {path} 仍在同步栈上取会话"
    # 不带数据库的控制面入口（登出之外的少数几条）只要求「没有同步会话」，
    # 其余必须显式从异步栈取——两条合起来才是「控制面全在异步栈上」。
    if get_async_db not in calls:
        pytest.fail(f"{method} {path} 没有从异步栈取会话")


@pytest.mark.parametrize(("method", "path"), CONSOLE_WRITE_ROUTES)
def test_console_writes_declare_the_async_transaction(method: str, path: str) -> None:
    """写入口的提交点只有异步事务依赖；同步事务依赖一条都不许留。"""

    from app.db.async_session import get_async_transaction
    from app.db.session import get_transaction

    calls = _dependant_calls(_routes()[(method, path)].dependant)

    assert get_async_transaction in calls, f"{method} {path} 没有声明异步事务依赖"
    assert get_transaction not in calls, f"{method} {path} 仍在用同步事务依赖"


def test_the_write_route_list_covers_every_mutating_console_endpoint() -> None:
    """清单守卫：``/console`` 下除 GET 之外的每条路由都必须在上面那张表里。

    少一条 = 那条写入口没被「必须声明异步事务」这条守卫覆盖，而它照样能全绿。
    """

    listed = set(CONSOLE_WRITE_ROUTES) | set(NON_DB_WRITE_ROUTES)
    missing = sorted(
        key
        for key in _console_route_keys()
        if key[0] != "GET" and key not in listed
    )
    assert missing == [], f"这些写入口没有登记在 CONSOLE_WRITE_ROUTES 里：{missing}"


def test_the_secret_endpoint_is_counted_as_a_write() -> None:
    """``GET .../secret`` 是读门但**写审计**：它必须在写入口清单里。"""

    assert ("GET", "/console/model-configs/{config_id}/secret") in CONSOLE_WRITE_ROUTES


# ─────────────────────────────────────────────────────────── 过渡脚手架已删除


def test_the_dual_session_scaffold_is_gone() -> None:
    """``app/db/dual_session.py`` 的删除条件已经满足：控制面也拿 ``AsyncSession``。"""

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.db.dual_session")


def test_the_sync_bridge_scaffold_is_gone() -> None:
    """``app/core/sync_bridge.py`` 的删除条件已经满足：没有同步调用点要跨回事件循环。"""

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.core.sync_bridge")


def _python_sources() -> list[Path]:
    return sorted((REPO_ROOT / "app").rglob("*.py"))


@pytest.mark.parametrize(
    "needle",
    [
        # 调用 / 取属性 / 导入的写法；文档里提一句历史不算违规（删掉的东西值得留个名字），
        # 但**代码里再出现一次**就是回退。
        "dual_session.",
        "import dual_session",
        "sync_bridge.",
        "import sync_bridge",
        "run_blocking(",
        "resolve_plan_blocking(",
        "invalidate_plan_blocking(",
    ],
)
def test_no_module_still_calls_the_removed_scaffolds(needle: str) -> None:
    """这些名字只允许出现在「它已经删了」的说明里，不许出现在可执行代码里。"""

    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _python_sources()
        if needle in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], f"这些文件还在用 {needle}：{offenders}"


def test_the_plan_service_no_longer_exposes_blocking_twins() -> None:
    """套餐解析只剩 ``await`` 的那一份：同步孪生随脚手架一起删掉。"""

    from app.services import plan_service

    assert not hasattr(plan_service, "resolve_plan_blocking")
    assert not hasattr(plan_service, "invalidate_plan_blocking")


def test_the_identity_kernels_are_coroutines() -> None:
    """判定内核（数据面与控制面）都已经是 ``async``：它们背后是异步会话与异步缓存。"""

    from app.api.console_deps import get_console_account
    from app.api.deps import get_current_key
    from app.core.auth import resolve_identity
    from app.core.console_auth import resolve_console_identity

    for function in (
        resolve_identity,
        get_current_key,
        resolve_console_identity,
        get_console_account,
    ):
        assert inspect.iscoroutinefunction(function), f"{function.__name__} 还不是 async"


def test_anyio_is_no_longer_a_direct_dependency() -> None:
    """``anyio`` 当初只为那个过渡桥而直接钉住；桥没了，直接依赖也该消失。

    ``fastapi`` / ``starlette`` 仍然传递依赖它，所以这不影响任何运行期能力——只是别让
    依赖表继续为一段已经删掉的代码背书。
    """

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^\s*"anyio[=<>~!]', pyproject, re.MULTILINE) is None

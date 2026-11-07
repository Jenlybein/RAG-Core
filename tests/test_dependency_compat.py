"""依赖约束自洽性守卫：不碰 broker 也能发现「装得上、跑不通」的组合。

背景：`celery[redis]` 的版本链条是三方约束——celery 限定 kombu 的窗口，kombu 再限定
redis-py 的窗口。三者可以各自满足对方而**整体不相容**，症状是第一次投递/建 channel 时
才抛 `kombu.exceptions.VersionMismatch`。离线测试碰不到 broker，CI 因此全绿，而真实上传
100% 失败（2026-11-06 实际踩过：celery 5.4.0 + kombu 5.7.0 + redis 5.2.0）。

本文件只读 `importlib.metadata` 的 Requires-Dist 元数据，把「上游声明的可选依赖窗口」
与「实际装到的版本」对齐，不发任何网络请求。
"""

from __future__ import annotations

import importlib.metadata as metadata

from packaging.requirements import Requirement
from packaging.version import Version


def _declared_requirements(distribution: str) -> list[Requirement]:
    """读一个已安装分发的 Requires-Dist；没有元数据时返回空列表。"""

    return [Requirement(raw) for raw in (metadata.requires(distribution) or [])]


def _requirements_for(
    distribution: str, dependency: str, *, extra: str | None
) -> list[Requirement]:
    """挑出某分发的某条依赖声明；`extra=None` 只取无条件声明。"""

    selected: list[Requirement] = []
    for requirement in _declared_requirements(distribution):
        if requirement.name.lower() != dependency.lower():
            continue
        if extra is None:
            if requirement.marker is None:
                selected.append(requirement)
            continue
        if requirement.marker is not None and requirement.marker.evaluate({"extra": extra}):
            selected.append(requirement)
    return selected


def _assert_satisfied(installed: str, requirements: list[Requirement], *, context: str) -> None:
    version = Version(metadata.version(installed))
    for requirement in requirements:
        assert version in requirement.specifier, (
            f"{context}: 已安装 {installed}=={version} 不满足约束 "
            f"{requirement}（这条在真实投递时才会炸，离线 CI 看不出来）"
        )


def test_installed_redis_satisfies_kombu_redis_extra() -> None:
    """kombu 的 redis 传输窗口必须覆盖实际装到的 redis-py。"""

    requirements = _requirements_for("kombu", "redis", extra="redis")
    assert requirements, "kombu 没有声明 redis extra，说明元数据异常"
    _assert_satisfied("redis", requirements, context="kombu[redis] -> redis-py")


def test_installed_kombu_satisfies_celery_requirement() -> None:
    """celery 对 kombu 的窗口必须覆盖实际装到的 kombu。"""

    requirements = _requirements_for("celery", "kombu", extra=None)
    assert requirements, "celery 没有声明 kombu 依赖，说明元数据异常"
    _assert_satisfied("kombu", requirements, context="celery -> kombu")


def test_celery_redis_extra_does_not_cap_redis_below_kombu_floor() -> None:
    """celery 自己若对 redis-py 另设上限，该上限不能与 kombu 的下限冲突。

    celery 5.4.0 的 redis extra 写死 `redis<6.0.0`，而它允许的 kombu 5.7.0 要求
    `redis>=6.4.0`——正是这个组合让整条链路不可达。这里允许「celery 不声明 redis 上限」
    （5.5.x 起改为转发 kombu[redis]），只禁止声明了却与 kombu 下限矛盾的情况。
    """

    installed_redis = Version(metadata.version("redis"))

    celery_redis_caps = _requirements_for("celery", "redis", extra="redis")
    for requirement in celery_redis_caps:
        # 只关心「上限」类约束；`<` 或 `<=` 出现在 specifier 里才算。
        upper_bounds = [spec for spec in requirement.specifier if spec.operator in ("<", "<=")]
        if not upper_bounds:
            continue
        assert installed_redis in requirement.specifier, (
            f"celery[redis] 对 redis-py 的上限 {requirement} 与实际安装的 {installed_redis} 冲突"
        )


# ── 第 4 步票 1：模型接入层带来的新链条 ──
#
# 与上面三条同源：这些约束在离线测试里一点症状都没有，却会在第一次真实调用时炸。
# langgraph 只在 langchain 1.x 的运行时被间接拉起，评测库 ragas 则把 langchain 与
# openai 同时钉在各自的窗口里——三条链各自自洽、合起来不一定能跑。


def test_installed_langchain_core_satisfies_langgraph_requirement() -> None:
    """langgraph 由 langchain 1.x 自带；它声明的 langchain-core 窗口必须覆盖实际装到的版本。"""

    requirements = _requirements_for("langgraph", "langchain-core", extra=None)
    assert requirements, "langgraph 没有声明 langchain-core 依赖，说明元数据异常"
    _assert_satisfied("langchain-core", requirements, context="langgraph -> langchain-core")


def test_installed_langchain_satisfies_langchain_openai_requirement() -> None:
    """三家适配器（openai / anthropic / google-genai）都要求各自的 langchain-core 窗口。"""

    for distribution in ("langchain-openai", "langchain-anthropic", "langchain-community"):
        requirements = _requirements_for(distribution, "langchain-core", extra=None)
        assert requirements, f"{distribution} 没有声明 langchain-core 依赖，说明元数据异常"
        _assert_satisfied(
            "langchain-core", requirements, context=f"{distribution} -> langchain-core"
        )


def test_installed_openai_satisfies_ragas_and_langchain_openai_requirements() -> None:
    """评测库与适配器对 openai 的窗口必须同时覆盖实际装到的 openai。"""

    for distribution in ("ragas", "langchain-openai"):
        requirements = _requirements_for(distribution, "openai", extra=None)
        assert requirements, f"{distribution} 没有声明 openai 依赖，说明元数据异常"
        _assert_satisfied("openai", requirements, context=f"{distribution} -> openai")


def test_langchain_community_keeps_ragas_importable() -> None:
    """ragas 0.4.3 在模块顶层 import `langchain_community.chat_models.vertexai`。

    该模块在 langchain-community 0.4.2 被删掉，装最新版会让 `import ragas` 直接
    ModuleNotFoundError——而评测代码要到第 7 步才会 import 它，中间这段时间没有任何
    离线测试会碰到。所以这里直接守「模块还在」这一事实，而不是守版本号。
    """

    from importlib.util import find_spec

    assert find_spec("langchain_community.chat_models.vertexai") is not None, (
        "langchain-community 太新：ragas 依赖的 chat_models.vertexai 已被删除，"
        "评测库会 import 失败（pyproject 里钉住 0.4.1 就是为这条）"
    )

"""档位预设的进程级注册表（注入点）。

**为什么要一个注册表而不是让检索链路直接读库**：`profile_chain_of` 是纯函数，被每次
检索调用，签名里没有（也不该有）数据库会话——它要做的事只是「把配置里的档位展开成
一条链路」。所以真相放在这里：组合根启动时从 ``profile_presets`` 表装载一次，管理端
写完库再装载一次，检索侧只读这份快照。

**这层注入与 ``app.providers.llm`` 的 ``RoleResolver`` 是同一套做法**，理由也一样：
让「检索参数怎么算」保持可测（测试里塞一份字典就能验），不必为了读档位去搭一个真库。

**落地范围要说清**：这是**进程内**快照。本仓库的部署是单 uvicorn + 单 worker，检索只
发生在 API 进程，所以「后台改完立刻生效」成立。真要多副本部署，这个模块就是接 Redis
广播的位置——把 :func:`set_profile_presets` 的入参同时写进一个带 TTL 的键，各进程在
读之前对一下版本即可；现在不提前做，因为单副本下它只是没有收益的复杂度。

**缺省值是内置种子**，与迁移里播种的四行逐字一致。空注册表时回落到种子而不是报错：
一个还没跑过迁移的库不该让整个检索路径失效，而种子恰好就是改造前的那份硬编码行为。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Mapping

import structlog

from app.models.key_policy import (
    PROFILE_BALANCED,
    PROFILE_CUSTOM,
    PROFILE_QUALITY,
    PROFILE_SPEED,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ProfilePresetSpec:
    """一个档位的参数。字段与 ``profile_presets`` 的四个业务列一一对应。"""

    mode: str
    top_k: int
    rerank: bool
    rewrite: bool


#: 内置种子。必须与 ``migrations/versions/a7d2e8f4c615_create_profile_presets.py``
#: 的播种行、以及改造前 ``retrieval_options.PROFILE_CHAINS`` 的值保持一致——三处一致
#: 才能保证「不灌库」与「灌了库」两种世界跑出同样的结果。有测试钉住这一点。
DEFAULT_PRESETS: Final[dict[str, ProfilePresetSpec]] = {
    PROFILE_SPEED: ProfilePresetSpec(mode="vector", top_k=5, rerank=False, rewrite=False),
    PROFILE_BALANCED: ProfilePresetSpec(mode="hybrid", top_k=5, rerank=False, rewrite=False),
    PROFILE_QUALITY: ProfilePresetSpec(mode="hybrid", top_k=5, rerank=True, rewrite=True),
    # custom 这一行只用来给编辑器一个初值；检索时 custom 仍逐键回落部署配置
    # （见 `app/models/profile_preset.py` 的模块文档）。
    PROFILE_CUSTOM: ProfilePresetSpec(mode="vector", top_k=5, rerank=False, rewrite=False),
}

_presets: dict[str, ProfilePresetSpec] = dict(DEFAULT_PRESETS)


def get_profile_presets() -> Mapping[str, ProfilePresetSpec]:
    """当前生效的档位参数快照（只读视图，调用方不要改它）。"""

    return _presets


def profile_preset_of(profile: str) -> ProfilePresetSpec | None:
    """按档位名取参数；未知档位返回 ``None``，由调用方决定怎么兜底。"""

    return _presets.get(profile)


def set_profile_presets(presets: Mapping[str, ProfilePresetSpec]) -> None:
    """整体替换。**不是合并**：档位是四行固定的小表，漏传一档应当是可见的错误，而不是
    悄悄留着上一版的值。缺档时补种子，避免检索时该档位凭空消失。
    """

    merged = dict(DEFAULT_PRESETS)
    merged.update(presets)
    _presets.clear()
    _presets.update(merged)


def reset_profile_presets() -> None:
    """回到内置种子。测试与本地排查用；生产路径不会调它。"""

    _presets.clear()
    _presets.update(DEFAULT_PRESETS)


#: 装载器的注入点。**为什么要有它**：装载要读库，而组合根（`app.main`）是唯一知道
#: ``SessionLocal`` 的地方；把连接串塞进这个模块会让它变成第二个组合根。更重要的是
#: 测试：离线用例不连库，它们把装载器换成空操作，于是 ``TestClient`` 的 lifespan
#: 不会去碰 PostgreSQL——这与 ``set_role_resolver`` 是同一套做法。
_loader: Callable[[], None] | None = None


def set_profile_preset_loader(loader: Callable[[], None] | None) -> None:
    global _loader
    _loader = loader


def get_profile_preset_loader() -> Callable[[], None] | None:
    """当前装载器。测试要在用例结束时原样还原，所以必须能读回来。"""

    return _loader


def load_profile_presets() -> None:
    """跑一次装载（启动时用）。**失败不抛出**：注册表空着会回落到内置种子，那份种子
    就是改造前那份硬编码行为，于是「迁移没跑」与「跑过了」两种世界结果一致；若这里抛出，
    一个没跑迁移的库会连登录页都打不开，排查方向会被误导到整个应用。"""

    if _loader is None:
        return
    try:
        _loader()
    except Exception as exc:  # noqa: BLE001 - 迁移未跑的库要能启动，见上
        logger.warning("profile_presets_load_failed", error=str(exc))

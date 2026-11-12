"""档位预设的读写编排：列、改、装载。

路由只做「取参数 → 调这里 → 装壳」，判定全部收在这一层。三条刻意的取舍：

- **只能改参数，不能增删档位**。档位名同时是套餐白名单（``plan.allowed_profiles``）、
  前端卡片、门禁判据的词表，插一行「turbo」不会让它可用——那是一次跨三层的功能，不是
  一次配置写入。
- **写入即装载**。改完库不刷新注册表，检索侧要等下次重启才生效，面板上却显示「已保存」，
  那是最糟的一种不一致。所以写完立刻 ``set_profile_presets``。
- **审计记参数、不记整表**。一条账本回答「谁把哪一档从什么改成什么」，把四行一起塞进去
  会让 diff 变噪声。

与配置中台一致的纪律：**跨租户与不存在都按「不存在」**——但这里根本没有租户维度，
档位是平台级配置，所以只有管理员能读写（见路由的 ``require_console_admin``）。
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy.orm import Session

from app.core.config import SUPPORTED_RETRIEVAL_MODES
from app.core.console_auth import ConsoleIdentity
from app.core.exceptions import AppError, ErrorCode
from app.db.transaction import after_commit
from app.core.profile_presets import (
    DEFAULT_PRESETS,
    ProfilePresetSpec,
    set_profile_presets,
)
from app.models.key_policy import PROFILES
from app.models.profile_preset import ProfilePreset
from app.services.console_service import append_audit
from app.services.retrieval_options import MAX_TOP_K, MIN_TOP_K

#: 账本动作名。与 ``plan.update`` / ``account.update`` 同一个命名法。
ACTION_PROFILE_UPDATE: Final[str] = "profile.update"


def _spec_of(row: ProfilePreset) -> ProfilePresetSpec:
    return ProfilePresetSpec(
        mode=row.mode, top_k=row.top_k, rerank=bool(row.rerank), rewrite=bool(row.rewrite)
    )


def list_presets(db: Session) -> dict[str, ProfilePresetSpec]:
    """四个档位的当前参数。表空时回落到内置种子，与检索侧的缺省同一条口径。"""

    rows = db.query(ProfilePreset).all()
    if not rows:
        return dict(DEFAULT_PRESETS)
    found = {row.profile: _spec_of(row) for row in rows}
    # 缺档补种子：局部迁移或手工删行之后，少一档不该让检索在那一档上静默退化。
    for profile, spec in DEFAULT_PRESETS.items():
        found.setdefault(profile, spec)
    return found


def load_into_registry(db: Session) -> dict[str, ProfilePresetSpec]:
    """装载进进程级注册表（组合根启动时与每次写入后用）。"""

    presets = list_presets(db)
    set_profile_presets(presets)
    return presets


def _validate(*, profile: str, mode: str | None, top_k: int | None) -> None:
    if profile not in PROFILES:
        # 未知档位 = 参数错误（20015），与密钥配置里「档位不在词表」同一个码：
        # 这是调用方给错了值，不是资源不存在。
        raise AppError(ErrorCode.PARAM_ERROR, f"未知档位：{profile}")
    if mode is not None and mode not in SUPPORTED_RETRIEVAL_MODES:
        raise AppError(
            ErrorCode.PARAM_ERROR,
            f"检索方式只能是 {' / '.join(SUPPORTED_RETRIEVAL_MODES)}",
        )
    if top_k is not None and not (MIN_TOP_K <= top_k <= MAX_TOP_K):
        raise AppError(
            ErrorCode.PARAM_ERROR, f"返回条数必须在 {MIN_TOP_K}–{MAX_TOP_K} 之间"
        )


def patch_preset(
    db: Session,
    *,
    profile: str,
    mode: str | None = None,
    top_k: int | None = None,
    rerank: bool | None = None,
    rewrite: bool | None = None,
    actor: ConsoleIdentity | None,
) -> dict[str, Any]:
    """改一档的参数：**部分更新**（没发的字段不动）。

    返回 ``before`` / ``after`` / ``changed``，与密钥配置的 PATCH 同一种形状——面板要
    能说清「这一行到底改了什么」，而"已保存"三个字说明不了这件事。
    """

    _validate(profile=profile, mode=mode, top_k=top_k)

    # 用 query+filter 而不是 ``db.get``：这张表的主键是档位名（不是 ``id``），而仓储层
    # 全仓统一走 ``query(...).filter(...)`` 的写法；离线测试的假会话也只认这一种。
    row = db.query(ProfilePreset).filter(ProfilePreset.profile == profile).first()
    if row is None:
        # 表里没有这一档（种子没灌到、或这档还没被播种）：以种子为基准新建一行，
        # 而不是拒绝——拒绝会让「把 speed 的条数从 5 改成 8」这种最普通的操作失败，
        # 而原因只是迁移漏了播种。
        seed = DEFAULT_PRESETS[profile]
        row = ProfilePreset(
            profile=profile, mode=seed.mode, top_k=seed.top_k, rerank=seed.rerank, rewrite=seed.rewrite
        )
        db.add(row)

    before = {
        "profile": profile,
        "mode": row.mode,
        "top_k": row.top_k,
        "rerank": bool(row.rerank),
        "rewrite": bool(row.rewrite),
    }

    # 逐字段只写发来的：`None` 表示「这次不动它」，与 KeyPolicyPatchRequest 同一条约定。
    if mode is not None:
        row.mode = mode
    if top_k is not None:
        row.top_k = top_k
    if rerank is not None:
        row.rerank = rerank
    if rewrite is not None:
        row.rewrite = rewrite

    after = {
        "profile": profile,
        "mode": row.mode,
        "top_k": row.top_k,
        "rerank": bool(row.rerank),
        "rewrite": bool(row.rewrite),
    }
    changed = sorted(key for key in before if before[key] != after[key])

    append_audit(
        db,
        actor=actor,
        action=ACTION_PROFILE_UPDATE,
        target=f"profile:{profile}",
        before=before,
        after=after,
    )
    # 装载必须等库先落地：注册表是本进程的快照，提交前装载会把旧值读回去。
    after_commit(db, lambda: load_into_registry(db))

    return {"before": before, "after": after, "changed": changed}

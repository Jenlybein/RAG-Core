"""模型配置的业务层（票 #94）：角色指向的解析、被引用判定、增删改与明文查看。

这个模块回答三个问题，且每个**只回答一处**：

1. **这条配置被哪个角色引用**（:func:`roles_referencing`）。删配置时的拒绝与写入角色
   指向时的目标校验都从这一份判定出发——「有没有人指着它」只该有一个答案；
2. **现在该用哪一条**（:func:`resolve_role_config`）。装配 Provider 的**唯一**入口，
   顺序是「角色指针 → 那一行」，指针为空或指向的行已经消失就返回 ``None``，让调用方
   回落 ``.env`` 的单供应商（见 :mod:`app.providers.llm`）。Provider 不 import 本模块
   （层向），所以组合根注入的是它的适配器 :func:`role_endpoint`（#82 修复）；
3. **密钥怎么打码**（:func:`app.utils.id_generator.mask_secret`）。列表、审计、日志都从
   那一份派生，本模块不再自己写规则；明文只有 ``reveal_secret`` 一条出口。

## 平台级资源：写入口只给管理员

``model_configs`` 不属于任何租户：一次改动会改变**所有租户**下一次检索所用的模型与
凭证。所以这组接口只挂在管理员依赖上（``require_console_admin_write``），与配置中台
（#93）同一条理由——跨租户越权比权限粒度问题严重得多，详细论证见
``app/api/routes/console_settings.py`` 的模块文档（这里不再重写一遍）。

## 为什么解析顺序写成「指针 → 行 → 回落 .env」

指针指向的行可能在运行中被绕过接口删掉（手工 ``DELETE``、回滚失败的迁移）。这时**不能
在请求路径上炸**：检索是一个正在服务的过程，库里有半条脏数据不该让所有租户的检索 500。
所以「指向的行不见了」按「没有指向」处理，记一条 warning 并回落 ``.env``——这与
``runtime_settings`` 对非法行的处理同一条纪律（丢弃那一行、保留上一份好值、照常服务）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import structlog
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ErrorCode
from app.core.llm_vendors import LLM_VENDORS
from app.core.model_roles import (
    MODEL_ROLES,
    role_of_pointer_key,
    role_pointer_target,
)
from app.db.base import now
from app.db.session_factory import session_factory
from app.models.model_config import ModelConfig, ModelConfigSpec, spec_of
from app.repositories import model_config_repository, runtime_setting_repository
from app.services.console_service import append_audit
from app.utils.id_generator import mask_secret, new_id

logger = structlog.get_logger(__name__)

if TYPE_CHECKING:  # pragma: no cover - 只给类型检查看，运行期不 import Provider
    from app.providers.llm import RoleEndpoint

#: 审计里的动作名。``model.`` 前缀让「动的是模型接入」一眼可分；``model.reveal`` 是
#: 唯一一条「看了明文」的账本，事后追「谁把 Key 看过」就靠它。
ACTION_CREATE: Final[str] = "model.create"
ACTION_PATCH: Final[str] = "model.patch"
ACTION_DELETE: Final[str] = "model.delete"
ACTION_REVEAL: Final[str] = "model.reveal"

#: 审计目标前缀：``model:<id>``。与套餐/账号的 ``plan:`` / ``account:`` 同一形态。
AUDIT_TARGET_PREFIX: Final[str] = "model:"

#: 删除被引用时的拒绝措辞。**必须给出下一步动作**（先改指向），否则管理员只知道「删不掉」。
MSG_REFERENCED: Final[str] = (
    "这条模型配置仍被角色 {roles} 引用，先把 fast/strong 的指向改掉"
    "（PATCH /console/settings 把 model.role.<角色> 写空串即可清空）"
)

#: 角色指向写了一个不存在的 id。整句里带上键名与那个 id，管理员才找得到是哪一项。
MSG_UNKNOWN_TARGET: Final[str] = "该模型配置不存在：{value}（{key} 只能指向 model_configs 里现存的 id，写空串表示回落 .env）"

#: 供应商必须是注册表里的六家之一。出厂预设只是**可选项的清单**，不是自由字符串：
#: 装配知识（框架 provider 名 / 默认地址 / key 落点）在 ``app/providers/llm`` 里按名字
#: 索引，拼错一个字母的后果是「保存成功、第一次调用才 KeyError」。
MSG_UNKNOWN_VENDOR: Final[str] = "未知的模型供应商：{value}（只支持 {vendors}）"


def _session_of(db: Session | None) -> Session:
    """拿一个会话：调用方给了就用它，否则照进程级注入点开一个（用完要关）。"""

    return db if db is not None else session_factory()()


def _close_if_temporary(session: Session, db: Session | None) -> None:
    if db is None:
        session.close()


# ───────────────────────────────────────────────────────────── 打码与快照


def _masked_snapshot(spec: ModelConfigSpec) -> dict[str, Any]:
    """审计快照里的那一行：``api_key`` 打码，其余原样。

    审计列表是管理员都能读的一页；把明文再抄进 ``audit_logs`` 只是把泄露面从一个表扩大到
    两个表——与 #93 对 ``MODEL_API_KEY`` 的处理同一条。
    """

    return {
        "id": spec.id,
        "label": spec.label,
        "provider": spec.provider,
        "base_url": spec.base_url,
        "model": spec.model,
        "api_key": mask_secret(spec.api_key),
    }


# ───────────────────────────────────────────────────────── 角色指针与被引用判定


def pointer_map(db: Session) -> dict[str, str]:
    """角色 → 它现在指向的配置 id；**空值 / 非法值不出现**（等于「不指向」）。

    这是读指针的**唯一**位置：``runtime_settings`` 把指针存在自己的表里，但两张表之间的
    关系（「谁指着谁」）只能在这里被解释一次，否则删除校验、装配解析、面板展示会各写
    一遍「空串算不算指向」。
    """

    pointers: dict[str, str] = {}
    for row in runtime_setting_repository.list_all(db):
        role = role_of_pointer_key(row.key)
        if role is None:
            continue
        target = role_pointer_target(row.value)
        if target is not None:
            pointers[role] = target
    return pointers


def roles_referencing(db: Session, config_id: str) -> tuple[str, ...]:
    """指向这条配置的角色名（按 ``MODEL_ROLES`` 的顺序）。**「被引用」的唯一判定。**

    删除一条仍被角色引用的配置会让运行时在下一次装配时静默回落到 ``.env``——那是一次
    「看起来删掉了、其实整条链路换了模型」的改动，所以必须先拒绝（``20015``）并要求
    管理员先把指向改掉。写入角色指针时也用它：目标必须是一条**有人可以用**的行。
    """

    pointers = pointer_map(db)
    return tuple(role for role in MODEL_ROLES if pointers.get(role) == config_id)


def ensure_pointer_target(db: Session, key: str, value: Any) -> None:
    """写入角色指针前的目标校验：空值允许（清空 → 回落 .env），非空必须是现存的行。

    校验点放在服务层（由 :mod:`app.services.runtime_settings` 的写入收口调用）而不是
    路由里：指针还有别的写入口（未来的 CLI / 迁移脚本），而「指向一个不存在的 id」在
    任何入口上都是同一个错误。失败是 ``20015``（参数非法），不是「不存在」——写的是
    一个不合法的配置值，被指向的东西存不存在只是原因之一。
    """

    role = role_of_pointer_key(key)
    if role is None:
        return
    target = role_pointer_target(value)
    if target is None:
        return
    if model_config_repository.get_by_id(db, target) is not None:
        return
    raise AppError(ErrorCode.PARAM_ERROR, MSG_UNKNOWN_TARGET.format(value=target, key=key))


@dataclass(frozen=True)
class ConfigRowView:
    """列表里的一行：配置快照 + 它现在被哪些角色引用（面板据此显示「fast 指这条」）。"""

    spec: ModelConfigSpec
    roles: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ConfigPatchResult:
    """一次改配置的产物：改前 / 改后同一形状（``api_key`` 已打码）与真变了的字段。"""

    config_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    changed: tuple[str, ...]


def list_configs(db: Session) -> list[ConfigRowView]:
    """全部配置行 + 各自被哪个角色引用。

    指针**只读一次**（``pointer_map``）而不是每行各读一次：面板要的是「这张表现在长
    什么样」，N 行各读一次两张指针键纯属多余，而且行与行之间可能读到同一次改动的前后
    两个状态。
    """

    pointers = pointer_map(db)
    # 一个配置可以被**两个**角色同时指向（fast 与 strong 用同一家、同一个模型是常见
    # 配置）。所以这里按键收集而不是 `{config_id: role}` 覆盖写——后者会让面板只显示
    # 最后读到的那个角色，而「这条同时被 fast 和 strong 引用」恰好是删除时最该看到的。
    referenced_by: dict[str, list[str]] = {}
    for role in MODEL_ROLES:
        target = pointers.get(role)
        if target is not None:
            referenced_by.setdefault(target, []).append(role)
    views: list[ConfigRowView] = []
    for row in model_config_repository.list_all(db):
        views.append(
            ConfigRowView(
                spec=spec_of(row),
                roles=tuple(referenced_by.get(row.id, ())),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    return views


# ───────────────────────────────────────────────────────────── 字段校验（写入唯一入口）


#: 字段名 → 最大长度。与迁移里的列宽逐个相等：先在这里判，才不会有「保存成功」却撞
#: 数据库 ``DataError`` 的 500。长度超限是**参数非法**（``20015``），不是 422——它是
#: 人填错的一个值，不是请求体结构错。
_FIELD_MAX_LENGTH: Final[dict[str, int]] = {
    "label": 128,
    "provider": 32,
    "base_url": 255,
    "model": 128,
    "api_key": 255,
}

#: 不允许为空串的字段：供应商、模型名、备注名。``base_url`` 为空是合法缺省（用供应商
#: 默认地址），``api_key`` 为空也是合法状态（这条接入还没填 Key，装配时退回空实现）。
_REQUIRED_TEXT: Final[tuple[str, ...]] = ("label", "provider", "model")


def _normalize_provider(value: str) -> str:
    """供应商名规范化 + 注册表校验；返回小写后的名字（存库的是它，不是原始输入）。

    大小写与空白不算拼错（``DeepSeek `` 与 ``deepseek`` 是同一家）——但**不属于**
    ``LLM_VENDORS`` 的值一律拒绝：出厂预设只是可选清单，装配知识按名字索引（见
    ``app/core/llm_vendors.py``），拼错的后果是保存成功、第一次调用才 KeyError（与
    ``LLM_PROVIDER`` 的启动期校验同一条理由，只是这里改不动进程，只能拒在写入那一刻）。
    """

    normalized = value.strip().lower()
    if normalized not in LLM_VENDORS:
        raise AppError(
            ErrorCode.PARAM_ERROR,
            MSG_UNKNOWN_VENDOR.format(value=value, vendors=LLM_VENDORS),
        )
    return normalized


def _validated_fields(values: dict[str, Any]) -> dict[str, Any]:
    """逐字段校验这次要写的值；**任意一个不合格就整批不写**（``20015``）。

    返回规范化后的值：``provider`` 转小写，其余去掉首尾空白。校验与规范化必须一起做——
    只校验不规范化，库里会留下 ``"DeepSeek"`` 与 ``"deepseek"`` 两种写法，而工厂按小写
    索引，前者等于一条坏配置。
    """

    normalized: dict[str, Any] = {}
    for key, value in values.items():
        if key not in _FIELD_MAX_LENGTH:
            raise AppError(ErrorCode.PARAM_ERROR, f"未知字段：{key}")
        if not isinstance(value, str):
            raise AppError(ErrorCode.PARAM_ERROR, f"{key} 必须是字符串")
        cleaned = value.strip()
        if key in _REQUIRED_TEXT and not cleaned:
            raise AppError(ErrorCode.PARAM_ERROR, f"{key} 不能为空")
        if len(cleaned) > _FIELD_MAX_LENGTH[key]:
            raise AppError(
                ErrorCode.PARAM_ERROR,
                f"{key} 最长 {_FIELD_MAX_LENGTH[key]} 个字符（当前 {len(cleaned)}）",
            )
        normalized[key] = _normalize_provider(cleaned) if key == "provider" else cleaned
    return normalized


def _config_of(db: Session, config_id: str) -> ModelConfig:
    """按 id 找一行；不存在 → ``20016``。

    **没有「跨租户」这一分支**：这张表不属于任何租户，所以「不存在」是唯一一种找不到
    的理由（与账户/套餐之外的平台级资源同一个口径）。
    """

    row = model_config_repository.get_by_id(db, config_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"model config not found: {config_id}")
    return row


# ───────────────────────────────────────────────────────────────────────── 写入


def create_config(
    db: Session,
    *,
    actor: Any,
    label: str,
    provider: str,
    base_url: str,
    model: str,
    api_key: str,
) -> ConfigRowView:
    """新增一条模型接入：校验 → 落库 → 审计（同一事务）。

    三条必须字段（label / provider / model）非空且 provider 属于注册表；``base_url``
    与 ``api_key`` 允许空（前者表示用默认地址，后者表示还没填 Key）。
    """

    values = _validated_fields(
        {
            "label": label,
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
        }
    )
    stamp = now()
    row = ModelConfig(
        id=new_id(),
        label=values["label"],
        provider=values["provider"],
        base_url=values["base_url"],
        model=values["model"],
        api_key=values["api_key"],
        created_at=stamp,
        updated_at=stamp,
    )
    model_config_repository.create(db, row)
    spec = spec_of(row)
    append_audit(
        db,
        actor=actor,
        action=ACTION_CREATE,
        target=f"{AUDIT_TARGET_PREFIX}{row.id}",
        before=None,
        after=_masked_snapshot(spec),
    )
    db.commit()
    return ConfigRowView(
        spec=spec, roles=(), created_at=row.created_at, updated_at=row.updated_at
    )


def patch_config(
    db: Session, *, actor: Any, config_id: str, changes: dict[str, Any]
) -> ConfigPatchResult:
    """部分更新一条配置：只改给出的字段，其余保持原值（``api_key`` 也走这条）。

    审计的前后快照是**整行**（打码后）而不是只给 changed：管理员看账本时要能回答「这次
    之后这条配置长什么样」，而两份「只有被改字段」的 diff 拼不出那一行。
    """

    row = _config_of(db, config_id)
    if not changes:
        raise AppError(ErrorCode.PARAM_ERROR, "no fields to change")
    values = _validated_fields(changes)
    before = _masked_snapshot(spec_of(row))
    changed: list[str] = []
    for key, value in values.items():
        if getattr(row, key) != value:
            changed.append(key)
        setattr(row, key, value)
    if changed:
        row.updated_at = now()
    after = _masked_snapshot(spec_of(row))
    append_audit(
        db,
        actor=actor,
        action=ACTION_PATCH,
        target=f"{AUDIT_TARGET_PREFIX}{config_id}",
        before=before if changed else None,
        after=after if changed else None,
    )
    db.commit()
    return ConfigPatchResult(
        config_id=config_id, before=before, after=after, changed=tuple(sorted(changed))
    )


def delete_config(db: Session, *, actor: Any, config_id: str) -> str:
    """删一条配置。**被任一角色指向时拒绝**（``20015``），先改指向。

    为什么不做成「删掉配置、顺手把指向清空」：那会在一次点击里同时改两处平台级配置，
    而管理员看到的只是「删掉了一条」。先拒绝并给出下一步，让「换指向」成为一次显式操作。
    """

    row = _config_of(db, config_id)
    roles = roles_referencing(db, config_id)
    if roles:
        raise AppError(ErrorCode.PARAM_ERROR, MSG_REFERENCED.format(roles="/".join(roles)))
    before = _masked_snapshot(spec_of(row))
    db.delete(row)
    append_audit(
        db,
        actor=actor,
        action=ACTION_DELETE,
        target=f"{AUDIT_TARGET_PREFIX}{config_id}",
        before=before,
        after=None,
    )
    db.commit()
    return config_id


def reveal_secret(db: Session, *, actor: Any, config_id: str) -> str:
    """显示一次明文（``api_key``），**每次都写一条审计**。

    明文只在响应体里出现这一次，不回写任何表；审计快照里仍然打码——它是「谁、什么时候
    看了哪一条的明文」，不是明文本身（审计列表是管理员都能读的一页）。
    """

    row = _config_of(db, config_id)
    append_audit(
        db,
        actor=actor,
        action=ACTION_REVEAL,
        target=f"{AUDIT_TARGET_PREFIX}{config_id}",
        before=None,
        after={
            "id": config_id,
            "label": row.label,
            "api_key": mask_secret(row.api_key or ""),
        },
    )
    db.commit()
    return row.api_key or ""


# ───────────────────────────────────────────────────────── 运行时解析（装配点入口）


def resolve_role_config(role: str, db: Session | None = None) -> ModelConfigSpec | None:
    """角色 → 它现在该用的那一条配置；``None`` 表示「没有指向，回落 ``.env``」。

    三步都在这里：读指针（一次主键查询）、按 id 找行、行不见了就记 warning 并返回
    ``None``。**指针为空不是错误**，它是本票最重要的一种状态（离线 / CI / 全新部署都
    落到这里）。

    ``db`` 为空时自己开一个会话（装配点拿不到 ``Depends``）；读取必然是**当场读库**，
    不走缓存：指向的语义是「下一次检索用新配置」，而缓存最多会把这句话推迟 30 秒。
    一次主键查询换一条准确的承诺，是值得的。
    """

    session = _session_of(db)
    try:
        try:
            pointer = pointer_map(session).get(role)
            if pointer is None:
                return None
            row = model_config_repository.get_by_id(session, pointer)
        except Exception as exc:  # noqa: BLE001 —— 读不到就回落 .env，绝不在请求路径上炸
            # 库不可用（离线测试、CI、运维正在搬迁）时，唯一正确的行为是「当作没有指向」：
            # 检索是一条正在服务的路径，一次主键查询失败不该把它整段打断。告警里带上角色
            # 与原因，排障仍然有线索（与 runtime_settings 对非法行「丢弃 + 保留好值」同一条）。
            logger.warning(
                "model role pointer read failed; falling back to .env",
                role=role,
                error=str(exc),
            )
            return None
        if row is None:
            logger.warning(
                "model role points at a missing model config; falling back to .env",
                role=role,
                model_config_id=pointer,
            )
            return None
        return spec_of(row)
    finally:
        _close_if_temporary(session, db)


def role_endpoint(role: str) -> RoleEndpoint | None:
    """``resolve_role_config`` 的适配器：服务层形状 → Provider 层的 ``RoleEndpoint``。

    这是**唯一**的翻译点，也是 ``app/main.py`` 与 Celery Worker 注入给
    :func:`app.providers.llm.set_role_resolver` 的那个函数。放在服务层而不是 Provider 里：
    Provider 不认识 ``ModelConfigSpec``（``app/providers/**`` 不许 import ``app.services``，
    #82 修复的层向倒置），而服务层认识 Provider 的类型是正当的方向。

    适配是纯字段搬运（provider / base_url / model / api_key + 日志用的 id），没有任何判定：
    指针为空、行已消失、库读不到都由 :func:`resolve_role_config` 统一折成 ``None``。
    """

    from app.providers.llm import RoleEndpoint

    spec = resolve_role_config(role)
    if spec is None:
        return None
    return RoleEndpoint(
        provider=spec.provider,
        base_url=spec.base_url,
        model=spec.model,
        api_key=spec.api_key,
        config_id=spec.id,
    )

"""模型角色的词汇表，以及它在 ``runtime_settings`` 里的指针键名。

**第 13 步起平台只配一套模型接入**：改写、分解、受控 Research 的规划 / 判定 / 复查 /
生成都走同一个角色 ``default``。原先 ``fast`` / ``strong`` 两个角色（先快后准）已经撤销——
分成两个名字的代价是控制台两个下拉、两套指针键、两套装配参数，以及 Research 里一段
「强模型没装就换快模型兜底」的降级逻辑。既然那条降级按新规整单失败，角色也就没有再分的理由。

键名一旦复制两份，「加一个角色 / 改一次键名」就会有一处静默不同步，而症状是
「面板上改了指向、运行时仍按旧的跑」。所以键名只在这里推导，角色名也只在
:data:`MODEL_ROLES` 一处声明。
"""

from __future__ import annotations

from typing import Final

from app.core.exceptions import AppError, ErrorCode

MODEL_DEFAULT: Final[str] = "default"

#: 唯一一个模型角色。写成 tuple 是为了让「有几个角色」只有一处答案。
MODEL_ROLES: Final[tuple[str, ...]] = (MODEL_DEFAULT,)

POINTER_KEY_PREFIX: Final[str] = "model.role."


def role_pointer_key(role: str) -> str:
    """角色 → 它在 ``runtime_settings`` 里的指针键名（``model.role.default``）。"""

    if role not in MODEL_ROLES:
        raise ValueError(f"未知模型角色 {role!r}，只支持 {MODEL_ROLES}")
    return f"{POINTER_KEY_PREFIX}{role}"


def role_of_pointer_key(key: str) -> str | None:
    """指针键名 → 角色；不是指针键时返回 ``None``。"""

    if not key.startswith(POINTER_KEY_PREFIX):
        return None
    role = key[len(POINTER_KEY_PREFIX) :]
    return role if role in MODEL_ROLES else None


def role_pointer_target(value: object) -> str | None:
    """指针值 → 目标 id；空字符串 / ``None`` / 非字符串都表示「不指向任何一条」。"""

    if not isinstance(value, str):
        return None
    return value.strip() or None


def ensure_role(role: str) -> str:
    """校验角色名；不认识就立刻失败。"""

    if role not in MODEL_ROLES:
        raise AppError(ErrorCode.PARAM_ERROR, f"未知模型角色：{role!r}，只支持 {MODEL_ROLES}")
    return role


__all__ = [
    "MODEL_DEFAULT",
    "MODEL_ROLES",
    "POINTER_KEY_PREFIX",
    "ensure_role",
    "role_of_pointer_key",
    "role_pointer_key",
    "role_pointer_target",
]

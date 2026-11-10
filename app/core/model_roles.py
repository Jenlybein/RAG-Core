"""模型角色（fast / strong）的词汇表，以及它们在 ``runtime_settings`` 里的指针键名。

**为什么单独一个中立模块**（而不是把键名写在两个调用方里）：

角色指向有四个使用方，依赖方向彼此交叉：

* 白名单 ``app/services/runtime_settings.py`` 要登记这两个指针键（档位、理由、可写性）；
* 模型配置服务要读它们判「这条配置还被谁引用」，并在写入时校验目标存在；
* Provider 工厂要在装配时按角色解析出该用哪一行；
* 面板（#96）要按同一对名字展示「fast 指向谁、strong 指向谁」。

键名一旦复制两份，「加一个角色 / 改一次键名」就会有一处静默不同步，而症状是
「面板上改了指向、运行时仍按旧的跑」——最难查的一类。所以键名只在这里推导，
角色名也只在 :data:`MODEL_ROLES` 一处声明（``app/providers/llm/__init__.py`` 的
``_MODEL_ROLES`` 已经 import 它，不再各写一份）。
"""

from __future__ import annotations

from typing import Final

from app.core.exceptions import AppError, ErrorCode

MODEL_FAST: Final[str] = "fast"
MODEL_STRONG: Final[str] = "strong"

#: 两个模型角色。顺序即报错文案里的顺序（与 ``LLM_MODEL`` / ``LLM_STRONG_MODEL`` 的
#: 语义顺序一致：先快后准）。
MODEL_ROLES: Final[tuple[str, ...]] = (MODEL_FAST, MODEL_STRONG)

#: 指针键前缀。``model.role.<role>`` 是 ``runtime_settings`` 里的键名，
#: 值是 ``model_configs.id``。
POINTER_KEY_PREFIX: Final[str] = "model.role."


def role_pointer_key(role: str) -> str:
    """角色 → 它在 ``runtime_settings`` 里的指针键名（``model.role.fast``）。"""

    if role not in MODEL_ROLES:
        raise ValueError(f"未知模型角色 {role!r}，只支持 {MODEL_ROLES}")
    return f"{POINTER_KEY_PREFIX}{role}"


def role_of_pointer_key(key: str) -> str | None:
    """指针键名 → 角色；不是指针键时返回 ``None``（读库时遇到别的键要跳过）。"""

    if not key.startswith(POINTER_KEY_PREFIX):
        return None
    role = key[len(POINTER_KEY_PREFIX) :]
    return role if role in MODEL_ROLES else None


def role_pointer_target(value: object) -> str | None:
    """指针值 → 目标 id；空字符串 / ``None`` / 非字符串都表示「不指向任何一条」。

    「不指向」是**一个明确的状态**，不是坏值：它让运行时回落到 ``.env`` 的单供应商，
    也正是管理员清空指向的方式。把空值当成非法会让「改回默认」变成一个做不到的动作。
    """

    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def ensure_known_role(role: str) -> str:
    """角色名校验的唯一实现；写错要立刻炸，不能悄悄按 fast 跑。"""

    if role not in MODEL_ROLES:
        raise AppError(ErrorCode.PARAM_ERROR, f"未知模型角色：{role!r}，只支持 {MODEL_ROLES}")
    return role


__all__ = [
    "MODEL_FAST",
    "MODEL_ROLES",
    "MODEL_STRONG",
    "POINTER_KEY_PREFIX",
    "ensure_known_role",
    "role_of_pointer_key",
    "role_pointer_key",
    "role_pointer_target",
]

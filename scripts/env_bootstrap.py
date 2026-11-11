"""``--env-file`` 的引导：**必须**早于任何 ``app.core.config`` 的导入。

三个命令行工具（``eval_retrieval`` / ``eval_side_evidence`` / ``export_eval_cases``）原先各抄
了一份同样的 ``_preload_env_file``。三份一模一样的东西放在三个文件里，改一处忘两处时不会
报错——只会让「这次跑的是哪套环境」变成一个查不出来的问题。所以收到这里。

**为什么它不能住在 ``app/`` 里**：``settings`` 是模块级单例（``app/core/config.py`` 末尾
``settings = Settings()``），导入即定型。这个函数存在的全部意义就是「在导入 settings 之前
把键塞进 ``os.environ``」；一旦它自己 import 了 ``app.*``，就自相矛盾了。所以它只能留在
``scripts/`` 这一侧，只依赖标准库与 ``python-dotenv``。

**为什么用 ``setdefault`` 而不是覆盖**：显式传进来的环境变量优先于文件，与
``pydantic-settings`` 的优先级一致。运维用 ``$env:MODEL_API_KEY=...`` 临时覆盖文件时，
不会被文件反咬。

**为什么用 ``python-dotenv`` 而不是自己切 ``KEY=VALUE``**：它是 ``pydantic-settings`` 的硬
依赖（后者本来就靠它读 ``.env``），而 ``pydantic-settings`` 已在 ``pyproject.toml`` 里硬钉
版本。自己写半吊子解析器，会在引号、行内注释、``export`` 前缀这些地方悄悄读错一个 key。
"""

from __future__ import annotations

import argparse
import os


def preload_env_file() -> str | None:
    """从 ``--env-file`` 预读键值进 ``os.environ``；返回用到的文件路径，没传就 ``None``。

    只做「预读」这一件事：真正的配置定型仍由 ``app.core.config`` 负责，这里不构造任何
    配置对象，也不校验键名。
    """

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", default=None)
    known, _ = parser.parse_known_args()
    if not known.env_file:
        return None

    from dotenv import dotenv_values

    for key, value in dotenv_values(known.env_file).items():
        if value is not None:
            os.environ.setdefault(key, value)
    return known.env_file


#: 旧名保留：三个脚本原先各自定义 ``_preload_env_file``，调用点按旧名写。
_preload_env_file = preload_env_file

__all__ = ["preload_env_file"]

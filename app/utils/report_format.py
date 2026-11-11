"""报告里的数字怎么显示成文本（评测 / 旁证共用一份）。

**为什么单独一个模块**：``"n/a" if value is None else f"{value:.4f}"`` 这一句此前在
``scripts/eval_retrieval.py``、``scripts/eval_runner.py`` 与 ``app/services/side_evidence.py``
各写了一遍——三份一模一样的格式约定，任何一份改了（比如统一到三位小数）都会让两份报告
在同一份结论里显示成不同的精度。本轮 code-review 把它收到一处。

**``None`` 显示成 ``n/a`` 而不是 0**：门禁与旁证里 ``None`` 的含义是「未评分」，
与「评价为 0 分」是两件事（见 GLOSSARY「旁证指标」与 ``docs/step9-acceptance.md`` 第七节）。
"""

from __future__ import annotations


def as_metric(value: object, *, digits: int = 4) -> str:
    """把分数渲染成固定小数位；``None`` 渲染成 ``n/a``。

    只接受实数：``bool`` 虽然也是 ``int`` 的子类，但把 ``True`` 印成 ``1.0000`` 会让人
    以为那是一个得分，所以显式排除。
    """

    if value is None or isinstance(value, bool):
        return "n/a"
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{float(value):.{digits}f}"

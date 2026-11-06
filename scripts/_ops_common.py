"""两个运维脚本共享的报告小工具：统计打印与退出码规则。

导入方式：``scripts*`` 不在安装包里（见 pyproject 的 packages.find），所以脚本
不能假设仓库根在 ``sys.path`` 上。两个脚本在顶部把**自己所在目录**插入
``sys.path``，再用普通 ``from _ops_common import ...`` 导入；这样既保持
``spec_from_file_location`` 的文件加载可用，也不需要把 ``scripts`` 变成包。
"""

from collections.abc import Sequence
from typing import Any, Protocol


class OpsStats(Protocol):
    """脚本统计对象的最小形状：``delivered`` 是「已投递 / 已重新投递」数。"""

    failed: int
    delivered: int


def exit_code_for(stats: OpsStats) -> int:
    """有失败且一份都没投递成功时为 1，否则 0（部分失败仍算跑过）。"""

    return 1 if stats.failed and not stats.delivered else 0


def print_summary(
    header: str, *, stats: Any, counters: Sequence[tuple[str, str]], note: str
) -> None:
    """打印一行标题、按 ``(标签, 字段名)`` 顺序打印统计、最后打印提示。"""

    print(header)
    for label, attribute in counters:
        print(f"  {label}: {getattr(stats, attribute)}")
    print(note)

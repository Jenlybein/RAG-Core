"""七个运维脚本共享的报告小工具：统计打印与退出码规则。

导入方式：``scripts*`` 不在安装包里（见 pyproject 的 packages.find），所以脚本不能假设
仓库根或本目录在 ``sys.path`` 上。每个脚本在顶部都有**同一行**「把自身目录放进
``sys.path``」的引导（``sys.path.insert(0, str(Path(__file__).resolve().parent))``），
再用普通 ``from _ops_common import ...`` 导入；这样既保持 ``spec_from_file_location``
的文件加载可用，也不需要把 ``scripts`` 变成包。

这一行必须留在每个脚本里、不能搬进本模块：**要先能导入本模块，才谈得上调用它**——把引导
写在这里就等于让每个脚本先导入一个「必须先导入才能拿到路径」的模块。共享的是下面这些
**导入之后**才成立的东西（打印格式、退出码规则与 ``failed`` 的口径），不是那一行引导本身。

``exit_code_for`` 只给**会在一批里部分失败**的脚本用（reindex / requeue：某一篇投递失败，
其余的继续跑）。一次只动一行、失败即整体返回的脚本（开租户、发 Key、改套餐、播种、引导
管理员）没有 ``failed`` 字段，成功路径直接返回 0——留一个永远为 0 的 ``failed`` 会让汇总
行打印「failed: 0」，看起来像「跑过并确认零失败」。
"""

from collections.abc import Sequence
from typing import Any, Protocol


class OpsStats(Protocol):
    """批量脚本的统计对象：``failed`` 是这一批里失败的条数，``delivered`` 是已投递数。"""

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

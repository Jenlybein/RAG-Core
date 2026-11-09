"""改套餐：把某个租户换到另一个已播种的档位。

只认三个已知档位码（``free`` / ``standard`` / ``pro``），别的值在解析参数时就被拦下：

- 不做大小写兜底：``Pro`` 拼错被静默降到免费档，客户会先掉额度再投诉；
- 不猜最接近的一档：档位是产品契约，改错方向比拒绝更贵；
- 也不接受「还没播种的码」——库里的三档被删了一档时，脚本失败比改出一个空档位好。

改完立刻生效：改的是租户的 ``plan_id`` 指针，不是套餐行本身；套餐缓存按 ``plan_id`` 记，
所以这里没有需要清的条目（改套餐行本身才删缓存，那是第 7 步控制面的写入口）。

用法：``uv run python scripts/change_tenant_plan.py --tenant-id tenant_acme --plan pro``。
退出码：改成功为 0；档位名非法或未指定由 argparse 拦下（2）；租户不存在、档位没播种
为 1 并打印原因。模块顶层不解析 argv、不连接数据库，导入即安全。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import setup_logging
from app.db.session import SessionLocal
from app.models.plan import PLAN_CODES
from app.services.tenant_service import change_tenant_plan

# 共享报告工具与脚本同目录；scripts* 不在安装包里，显式把自己所在目录放进
# sys.path 后再用普通导入，spec_from_file_location 的文件加载才能找到它。
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _ops_common import exit_code_for, print_summary  # noqa: E402

CHANGED_NOTE = (
    "NOTE: the new tier takes effect on the next call; limits are read from the plan row "
    "every time, and API keys keep their own key policy untouched."
)


@dataclass(frozen=True)
class PlanChangeStats:
    """一次改套餐的统计：成功就动一行。"""

    changed: int = 0
    failed: int = 0

    @property
    def delivered(self) -> int:
        """共享退出码规则里的「已投递」口径。"""

        return self.changed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Move a tenant to another plan tier.")
    parser.add_argument("--tenant-id", required=True, help="tenant to change")
    parser.add_argument(
        "--plan", required=True, choices=PLAN_CODES, help="target plan code"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己的 Session → 改套餐。"""

    args = _build_parser().parse_args(argv)
    setup_logging()

    db = SessionLocal()
    try:
        change = change_tenant_plan(db, tenant_id=args.tenant_id, plan_code=args.plan)
    except (LookupError, ValueError) as exc:
        db.rollback()
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()

    print(f"plan: {change.from_code or '?'} -> {change.to_code}")
    print_summary(
        f"change_tenant_plan: {change.tenant_id}",
        stats=PlanChangeStats(changed=1),
        counters=(("changed", "changed"), ("failed", "failed")),
        note=CHANGED_NOTE,
    )
    return exit_code_for(PlanChangeStats(changed=1))


if __name__ == "__main__":
    raise SystemExit(main())

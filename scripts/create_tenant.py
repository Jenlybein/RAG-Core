"""开租户：建一行 ``tenants``，并顺手建一套最小权限的密钥配置。

为什么要顺手建配置：没有配置就发不出 Key（Key 必须绑定一套配置），而「开完租户却发
不了 Key」会让运维以为是权限问题跑去看鉴权代码。这套配置能力全关、上限照抄套餐、
档位取套餐白名单的第一档——能力放开是显式动作，不是默认。

``--kb-id`` 可以重复给，用来圈这套配置的数据范围；不给就是空白名单（「还没圈定」，
不是「全部」）。

用法：``uv run python scripts/create_tenant.py --id tenant_acme --name "Acme 公司"
[--plan free|standard|pro] [--kb-id kb-1 ...]``。
退出码：开成功为 0；未指定必填参数、或档位名不在三档之内由 argparse 拦下（2）；
租户已存在、档位还没播种等业务错误为 1 并打印原因。模块顶层不解析 argv、不连接
数据库，导入即安全。
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
from app.models.tenant import DEFAULT_PLAN_CODE
from app.services.tenant_service import create_tenant

# 共享报告工具与脚本同目录；scripts* 不在安装包里，显式把自己所在目录放进
# sys.path 后再用普通导入，spec_from_file_location 的文件加载才能找到它。
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _ops_common import exit_code_for, print_summary  # noqa: E402

CREATED_NOTE = (
    'NOTE: the key policy above is the one to pass to issue_api_key.py. It grants no extra '
    'capabilities; limits come from the tenant plan at creation time.'
)


@dataclass(frozen=True)
class CreateTenantStats:
    """一次开租户的统计：成功就落一行租户 + 一套配置。"""

    created: int = 0
    failed: int = 0

    @property
    def delivered(self) -> int:
        """共享退出码规则里的「已投递」口径。"""

        return self.created


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a tenant with a plan and a default key policy."
    )
    parser.add_argument("--id", dest="tenant_id", required=True, help="tenant id, e.g. tenant_acme")
    parser.add_argument("--name", required=True, help="display name")
    parser.add_argument(
        "--plan",
        default=None,
        choices=PLAN_CODES,
        help=f"plan code (default: the lowest tier, {DEFAULT_PLAN_CODE})",
    )
    parser.add_argument(
        "--kb-id",
        dest="kb_ids",
        action="append",
        default=[],
        help="knowledge base allowed by the default policy (repeatable)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己的 Session → 开租户。"""

    args = _build_parser().parse_args(argv)
    setup_logging()

    db = SessionLocal()
    try:
        created = create_tenant(
            db,
            tenant_id=args.tenant_id,
            name=args.name,
            plan_code=args.plan,
            kb_ids=args.kb_ids,
        )
    except (LookupError, ValueError) as exc:
        db.rollback()
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()

    print(
        f"tenant: {created.tenant.id} plan_id={created.tenant.plan_id} "
        f"name={created.tenant.name}"
    )
    print(
        f"key policy: {created.policy.id} profile={created.policy.profile} "
        f"qps={created.policy.retrieve_qps} daily={created.policy.retrieve_daily} "
        f"kb_ids={created.policy.allowed_kb_ids}"
    )
    print_summary(
        f"create_tenant: {created.tenant.id}",
        stats=CreateTenantStats(created=1),
        counters=(("created", "created"), ("failed", "failed")),
        note=CREATED_NOTE,
    )
    return exit_code_for(CreateTenantStats(created=1))


if __name__ == "__main__":
    raise SystemExit(main())

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

from app.core.exceptions import AppError
from app.core.logging import setup_logging
from app.db.session import SessionLocal
from app.db.transaction import transaction
from app.models.plan import PLAN_CODES
from app.models.tenant import DEFAULT_PLAN_CODE
from app.services.tenant_service import create_tenant

# scripts* 不在安装包里，把自身目录放进 sys.path 才能导入 _ops_common（见该模块文档）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ops_common import print_summary  # noqa: E402

CREATED_NOTE = (
    'NOTE: the key policy above is the one to pass to issue_api_key.py. It grants no extra '
    'capabilities; limits come from the tenant plan at creation time.'
)


@dataclass(frozen=True)
class CreateTenantStats:
    """一次开租户的统计：成功就落一行租户 + 一套配置。

    没有 ``failed``：失败路径在打印之前就带非零退出码返回了，这个字段不会被写、也不会
    被读——留着它只会让汇总行永远打印「failed: 0」。
    """

    created: int = 0


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
        help=f"plan code (default: the lowest plan, {DEFAULT_PLAN_CODE})",
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
        with transaction(db):
            created = create_tenant(
                db,
                tenant_id=args.tenant_id,
                name=args.name,
                plan_code=args.plan,
                kb_ids=args.kb_ids,
            )
        # **在会话还活着时**把要打印的字段抄成普通值。
        # 这里曾经直接 print(created.tenant.id)：``create_tenant`` 内部 commit 过，行在
        # commit 时被 expire，紧接着 ``db.close()`` 又把它 detach，于是最后这几行必然抛
        # ``DetachedInstanceError``——数据其实已经落库，脚本却以非零码退出，且把「开租户
        # 成功」报成一次失败。新建的库里一定会遇到，所以抄值这一步不能省。
        report = (
            created.tenant.id,
            created.tenant.plan_id,
            created.tenant.name,
            created.policy.id,
            created.policy.profile,
            created.policy.retrieve_qps,
            created.policy.retrieve_daily,
            list(created.policy.allowed_kb_ids),
        )
    except (LookupError, ValueError, AppError) as exc:
        # ``AppError`` 是「配置不自洽」（20015）：档位要的能力配置没开、配置开的比套餐
        # 允许的多，或数值超过套餐上限。它与入参错误一样是**人改一下就好**的失败，
        # 不是脚本故障，所以同样打一行消息、返回非零，不吐 traceback。
        # 回滚已由上面的 with transaction(db) 做过，这里不再重复。
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()

    (
        tenant_id,
        plan_id,
        tenant_name,
        policy_id,
        profile,
        qps,
        daily,
        kb_ids,
    ) = report
    print(f"tenant: {tenant_id} plan_id={plan_id} name={tenant_name}")
    print(
        f"key policy: {policy_id} profile={profile} "
        f"qps={qps} daily={daily} kb_ids={kb_ids}"
    )
    print_summary(
        f"create_tenant: {tenant_id}",
        stats=CreateTenantStats(created=1),
        counters=(("created", "created"),),
        note=CREATED_NOTE,
    )
    # 失败在打印之前就带着非零退出码返回了；走到这里就是一次成功，退出码固定为 0。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

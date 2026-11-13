"""改一套密钥配置：档位、能力开关、两个结果形状开关、限额与数据范围。

为什么是脚本而不是控制台接口：租户侧面板的 Key / 配置增删改查是**第 8 步**的工作
（spec #72「Out of Scope」）。本步只给运维一条能改配置的命令，取代「手写 SQL 更新
``key_policies``」；第 8 步做同一件事时会调同一个服务函数
:func:`app.services.key_policy_service.update_key_policy`。

三条硬约束（都属于「改错了很难查」的那一类）：

- **写前自洽校验**：与 ``create_tenant.py`` 用的是同一个 ``ensure_policy_consistent``。
  换成套餐不允许的档位、把限额调到超过套餐、开着套餐没有的能力——脚本直接失败、
  一行都不写。判在写之前：不自洽的配置一旦进库，第一次调用就会撞门禁，而那时改的是
  客户的生产配置；
- **只改给出的字段**：没给的保持原样。两个布尔开关用 ``--x`` / ``--no-x``，两个都不给
  就是「这次不动它」，所以不存在「手滑把某个开关写成 false」；
- **不需要清缓存**：判定内核每个请求现读配置行，改完下一次调用即生效，Key 也不用重签
  （理由写在服务模块文档里）。

用法：``uv run python scripts/update_key_policy.py --policy-id <id>
[--name <名字>] [--profile speed|balanced|quality|custom]
[--research|--no-research] [--hybrid|--no-hybrid] [--rerank|--no-rerank]
[--rewrite|--no-rewrite] [--orchestration|--no-orchestration]
[--include-parent|--no-include-parent]
[--expand-table-rows|--no-expand-table-rows] [--qps N] [--daily N]
[--kb-id kb-1 ... | --clear-kb-ids]``。
退出码：成功为 0；未给 ``--policy-id``、一个待改字段都没给、或 ``--kb-id`` /
``--clear-kb-ids`` 同时出现由 argparse 拦下（2）；配置 / 租户 / 套餐不存在、字段非法为
1；改完不自洽（``AppError`` 20015）为 1。模块顶层不解析 argv、不连接数据库，导入即安全。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.exceptions import AppError
from app.core.logging import setup_logging
from app.db.session import SessionLocal
from app.db.transaction import transaction
from app.models.key_policy import PROFILES
from app.services.key_policy_service import update_key_policy

# scripts* 不在安装包里，把自身目录放进 sys.path 才能导入 _ops_common（见该模块文档）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ops_common import print_summary  # noqa: E402

EFFECT_NOTE = (
    "NOTE: the new values take effect on the next call. The identity re-reads the policy "
    "row on every request, so no cache needs flushing and the key does not have to be "
    "re-issued."
)

#: ``(命令行选项, argparse dest, 落在哪一列)``。能力开关与形状开关共用这一份对应
#: 关系：三处各写一遍时，拼错一处就会「改了一个不存在的键」，而服务层会报未知字段。
SWITCHES: tuple[tuple[str, str, str], ...] = (
    ("research", "research", "research_allowed"),
    ("hybrid", "hybrid", "hybrid_allowed"),
    ("rerank", "rerank", "rerank_allowed"),
    ("rewrite", "rewrite", "query_rewrite_allowed"),
    ("orchestration", "orchestration", "orchestration_allowed"),
    ("include-parent", "include_parent", "include_parent"),
    ("expand-table-rows", "expand_table_rows", "expand_table_rows"),
)


@dataclass(frozen=True)
class PolicyUpdateStats:
    """一次改配置的统计：``changed`` 是**真的**变了的字段数（全等时可以是 0）。

    没有 ``failed``：失败路径在打印之前就带非零退出码返回了，这个字段不会被写、也不会
    被读——留着它只会让汇总行永远打印「failed: 0」。
    """

    changed: int = 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update one key policy (profile, capability / shape switches, limits, scope).",
        epilog=EFFECT_NOTE,
    )
    parser.add_argument("--policy-id", required=True, help="key policy id (from create_tenant.py)")
    parser.add_argument("--name", default=None, help="display name of this key policy")
    parser.add_argument(
        "--profile",
        choices=PROFILES,
        default=None,
        help="default retrieval profile; a named profile is a full replacement",
    )
    for option, _dest, column in SWITCHES:
        parser.add_argument(
            f"--{option}",
            action=argparse.BooleanOptionalAction,
            default=None,
            help=f"turn {column} on / off (omit to leave it untouched)",
        )
    parser.add_argument("--qps", type=int, default=None, help="key-level QPS limit (>= 1)")
    parser.add_argument("--daily", type=int, default=None, help="key-level daily quota (>= 1)")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--kb-id",
        dest="kb_ids",
        action="append",
        default=None,
        metavar="KB_ID",
        help="allowed knowledge base id; repeatable, replaces the whole whitelist",
    )
    scope.add_argument(
        "--clear-kb-ids",
        action="store_true",
        help="empty the whitelist (empty means 'nothing allowed', not 'everything')",
    )
    return parser


def _changes_of(args: argparse.Namespace) -> dict[str, Any]:
    """这次真的要改的字段：没给的、``None`` 的都不算（缺省 = 不动）。"""

    changes: dict[str, Any] = {}
    if args.name is not None:
        changes["name"] = args.name
    if args.profile is not None:
        changes["profile"] = args.profile
    for _option, dest, column in SWITCHES:
        value = getattr(args, dest)
        if value is not None:
            changes[column] = value
    if args.qps is not None:
        changes["retrieve_qps"] = args.qps
    if args.daily is not None:
        changes["retrieve_daily"] = args.daily
    if args.kb_ids is not None:
        changes["allowed_kb_ids"] = list(args.kb_ids)
    elif args.clear_kb_ids:
        changes["allowed_kb_ids"] = []
    return changes


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己的 Session → 改配置。"""

    parser = _build_parser()
    args = parser.parse_args(argv)
    changes = _changes_of(args)
    if not changes:
        # 这属于「命令行用错了」，不是业务失败：按 argparse 的规矩退 2，与兄弟脚本一致。
        parser.error("nothing to change: pass at least one policy field")

    setup_logging()

    db = SessionLocal()
    try:
        with transaction(db):
            update = update_key_policy(db, policy_id=args.policy_id, changes=changes)
    except (LookupError, ValueError, AppError) as exc:
        # ``AppError`` 是「配置不自洽」（20015）：档位要的能力没开、配置开的比套餐允许的
        # 多、限额超过套餐上限。它与入参错误一样是**人改一下就好**的失败，不是脚本故障，
        # 所以同样打一行消息、返回非零，不吐 traceback。回滚已经由上面的
        # ``with transaction(db)`` 做过，这里不再重复回滚。
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()

    print(f"key policy: {update.policy_id} tenant_id={update.tenant_id}")
    for column in update.changed:
        print(f"  {column}: {update.before[column]} -> {update.after[column]}")
    print_summary(
        f"update_key_policy: {update.policy_id}",
        stats=PolicyUpdateStats(changed=len(update.changed)),
        counters=(("changed", "changed"),),
        note=EFFECT_NOTE,
    )
    # 失败在打印之前就带着非零退出码返回了；走到这里就是一次成功，退出码固定为 0。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

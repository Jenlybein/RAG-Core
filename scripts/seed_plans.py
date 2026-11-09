"""播种套餐三档，并给升级前就已存在的租户建档。

为什么播种要单独一条命令：迁移只建表、不写数据（数据回填混进迁移就没法单独重放，也
没法在回滚时区分「结构」与「内容」）。而套餐三档是**首次启动的种子**——表里已经有行
就整表跳过，运营在面板上改过的额度不会被一次重启改回常量值。

回填读的是既有四张业务表上出现过的 ``tenant_id``（只读，不改它们的数据）：升级前
没有租户表，租户就是一个写在业务行上的字符串。它们此前一直能用全套能力，所以按标准
档建档；本地演示租户按最高档。此后新建租户默认最低档，那条口径在 ``app.models.tenant``。

用法：``uv run python scripts/seed_plans.py [--demo-tenant-id tenant_demo]``。
退出码：按共享规则——播种与回填都没能落下任何东西时为 1，否则 0。模块顶层不解析
argv、不连接数据库，导入即安全，测试可以直接 import 函数。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from app.core.logging import setup_logging
from app.db.session import SessionLocal
from app.services.identity_seeding import backfill_tenants, seed_plans

# scripts* 不在安装包里，把自身目录放进 sys.path 才能导入 _ops_common（见该模块文档）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ops_common import print_summary  # noqa: E402

PLANS_NOTE = (
    'NOTE: plans are seeded only while the plans table is empty; an operator-edited '
    'table is left untouched, so "inserted: 0" is the normal result on a live install.'
)
TENANTS_NOTE = (
    'NOTE: tenants that existed before the upgrade get the standard plan, the demo tenant '
    'the top plan. Tenants created later start on the lowest plan; the database is the truth '
    'from then on.'
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seed the three plans (only when the table is empty) and create "
            "tenant rows for tenants that existed before the upgrade."
        )
    )
    parser.add_argument(
        "--demo-tenant-id",
        default=None,
        help="tenant id that gets the top plan (default: DEV_TENANT_ID from settings)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己的 Session → 播种与回填。"""

    args = _build_parser().parse_args(argv)
    setup_logging()

    db = SessionLocal()
    try:
        plan_stats = seed_plans(db)
        tenant_stats = backfill_tenants(db, demo_tenant_id=args.demo_tenant_id)
    except LookupError as exc:
        db.rollback()
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()

    print_summary(
        "seed_plans (plans)",
        stats=plan_stats,
        counters=(("inserted", "inserted"), ("skipped", "skipped")),
        note=PLANS_NOTE,
    )
    print_summary(
        "seed_plans (tenants)",
        stats=tenant_stats,
        counters=(
            ("considered", "considered"),
            ("created", "created"),
            ("kept", "kept"),
        ),
        note=TENANTS_NOTE,
    )
    # 两条统计都没有 ``failed``：播种 / 回填的失败会在打印之前带着非零退出码返回
    # （``LookupError``）。走到这里就是成功，退出码固定为 0。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

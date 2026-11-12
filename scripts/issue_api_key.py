"""发 Key：给某租户签一把绑定到指定密钥配置的 API Key。

Key 是数据面唯一的凭证，所以脚本要打印原文——本期明文入库（ADR-0004：调试台要拿原文
打真实接口），运维抄走的这一串就是唯一能用的那一串。生产改成「哈希 + 密文」之后，
这里仍然会打印一次原文，但库里就再也取不出来了。

为什么要显式给 ``--policy-id``：一把 Key 的行为（数据范围、能力开关、额度、档位）全都
写在那套配置里。让脚本自己猜一套配置，等于让「这把 Key 能查什么」取决于脚本参数以外的
东西；配置从 ``create_tenant.py`` 的输出里抄。

用法：``uv run python scripts/issue_api_key.py --tenant-id tenant_acme
--policy-id <uuid> [--label "订单机器人"]``。
退出码：签发成功为 0；缺必填参数由 argparse 拦下（2）；租户或配置不存在、配置属于
别的租户为 1 并打印原因。模块顶层不解析 argv、不连接数据库，导入即安全。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import setup_logging
from app.db.session import SessionLocal
from app.db.transaction import transaction
from app.services.api_key_service import issue_api_key

# scripts* 不在安装包里，把自身目录放进 sys.path 才能导入 _ops_common（见该模块文档）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ops_common import print_summary  # noqa: E402

PLAINTEXT_NOTE = (
    "NOTE: the key value is stored in plaintext in this phase (ADR-0004), so it stays "
    "readable from the database until the hash + ciphertext migration lands. Hand it to the "
    "integrator over a private channel; it authenticates both the HTTP and the MCP entry."
)


@dataclass(frozen=True)
class IssueKeyStats:
    """一次发 Key 的统计：成功就落一行。

    没有 ``failed``：失败路径在打印之前就带非零退出码返回了，这个字段不会被写、也不会
    被读——留着它只会让汇总行永远打印「failed: 0」。
    """

    issued: int = 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue an API key bound to one key policy (prints the plaintext value)."
    )
    parser.add_argument("--tenant-id", required=True, help="tenant that owns the key")
    parser.add_argument("--policy-id", required=True, help="key policy the key is bound to")
    parser.add_argument(
        "--label", default=None, help="human-readable note, e.g. which integration uses it"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己的 Session → 发 Key。"""

    args = _build_parser().parse_args(argv)
    setup_logging()

    db = SessionLocal()
    try:
        with transaction(db):
            api_key = issue_api_key(
                db, tenant_id=args.tenant_id, policy_id=args.policy_id, label=args.label
            )
    except (LookupError, ValueError) as exc:
        # 回滚已由上面的 with transaction(db) 做过，这里不再重复。
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()

    print(f"api key: {api_key.key_value}")
    print(f"key id: {api_key.id} tenant_id={api_key.tenant_id} policy_id={api_key.policy_id}")
    print_summary(
        f"issue_api_key: {args.tenant_id}",
        stats=IssueKeyStats(issued=1),
        counters=(("issued", "issued"),),
        note=PLAINTEXT_NOTE,
    )
    # 失败在打印之前就带着非零退出码返回了；走到这里就是一次成功，退出码固定为 0。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

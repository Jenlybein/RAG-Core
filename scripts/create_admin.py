"""引导第一个平台管理员账号。

为什么必须有一条命令：账号表为空时，没有任何人能登录控制台去「创建一个账号」——这是一个
先有鸡还是先有蛋的问题。运维在部署后先跑这一条，之后所有账号都可以由这个管理员在面板里
管理（面板的账号增删属于第 8 步；本步只要求「第一把钥匙」）。

管理员**不属于任何租户**（``tenant_id`` 为空）：它跨租户管账号、套餐与审计，若挂到某个
「平台租户」名下，那个假租户就凭空拥有了一整套数据面身份。

口令只存 ``hashlib.scrypt`` 哈希（见 :mod:`app.core.passwords`），脚本**从不打印明文**。
``--password`` 与 ``--password-stdin`` 二选一；都不给时交互式提示输入（``getpass``），
这样默认不会把口令留在 shell history 或进程列表里。

用法：``uv run python scripts/create_admin.py --username admin [--password <pwd> |
--password-stdin]``。
退出码：创建成功为 0；用户名已被占用等业务错误为 1 并打印原因；缺参数或两个口令来源
同时给由 argparse 拦下（2）。模块顶层不解析 argv、不连接数据库，导入即安全。
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.console_auth import ROLE_ADMIN, STATUS_ACTIVE
from app.core.exceptions import AppError
from app.core.logging import setup_logging
from app.core.passwords import hash_password
from app.db.session import SessionLocal
from app.models.account import Account
from app.repositories import account_repository
from app.utils.id_generator import new_id

# scripts* 不在安装包里，把自身目录放进 sys.path 才能导入 _ops_common（见该模块文档）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ops_common import print_summary  # noqa: E402

CREATED_NOTE = (
    "NOTE: this account belongs to no tenant and can administer every tenant. "
    "Treat its password like a root credential."
)


@dataclass(frozen=True)
class CreateAdminStats:
    """一次引导的结果：成功就落一行管理员账号。

    没有 ``failed``：失败路径在打印之前就带非零退出码返回了，这个字段不会被写、也不会
    被读——留着它只会让汇总行永远打印「failed: 0」。
    """

    created: int = 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the first platform admin account for the console."
    )
    parser.add_argument("--username", required=True, help="login name, unique across accounts")
    password_source = parser.add_mutually_exclusive_group()
    password_source.add_argument(
        "--password",
        default=None,
        help="password (visible in shell history; prefer --password-stdin or the prompt)",
    )
    password_source.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from standard input",
    )
    return parser


def _read_password(args: argparse.Namespace) -> str:
    """按来源取口令：显式参数 → stdin → 交互提示；**不做 strip**（空格可能是口令的一部分）。"""

    if args.password is not None:
        return str(args.password)
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    return getpass.getpass("Password: ")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数 → 一次 ``setup_logging()`` → 自己的 Session → 落一行管理员。"""

    args = _build_parser().parse_args(argv)
    username = str(args.username).strip()
    password = _read_password(args)
    if not username:
        print("username must not be empty", file=sys.stderr)
        return 1
    if not password:
        print("password must not be empty", file=sys.stderr)
        return 1

    setup_logging()
    db = SessionLocal()
    try:
        if account_repository.get_by_username(db, username) is not None:
            print(f"username already exists: {username}", file=sys.stderr)
            return 1
        account = Account(
            id=new_id(),
            tenant_id=None,
            username=username,
            password_hash=hash_password(password),
            role=ROLE_ADMIN,
            status=STATUS_ACTIVE,
        )
        account_repository.create(db, account)
        db.commit()
    except (AppError, ValueError) as exc:
        db.rollback()
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()

    print(
        f"account: {account.id} username={account.username} role={account.role} "
        f"tenant_id={account.tenant_id}"
    )
    print_summary(
        f"create_admin: {account.username}",
        stats=CreateAdminStats(created=1),
        counters=(("created", "created"),),
        note=CREATED_NOTE,
    )
    # 失败在打印之前就带着非零退出码返回了；走到这里就是一次成功，退出码固定为 0。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

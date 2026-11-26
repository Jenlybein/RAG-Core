"""hash api key secrets（数据面 API Key 改「只存哈希」）

``api_keys`` 从「一列明文 ``key_value``」改成「两列」：

* ``key_hash`` —— 密钥本体的 ``scrypt$<n>$<r>$<p>$<salt>$<hash>``（与
  ``accounts.password_hash`` 同一套规则，见 ``app/core/passwords.py``）；
* ``key_tail`` —— 密钥本体末 5 位，只服务面板展示 ``rc_live_…4f3a2``。哈希算不出头尾，
  要显示就必须额外存一截。

Key 原文的形状随之变成 ``rc_live_<id>.<32 位十六进制>``：行身份证进了串里，鉴权才能靠
主键一次命中——哈希不能当索引，而「按原文一击命中」再也不可能了。

**这一步不可逆**：``DROP COLUMN key_value`` 之后原文永久消失（scrypt 不可逆）。
所以迁移要求一个显式参数，并且可以把新串一次性导出：

    alembic -x confirm_drop=1 upgrade head                          # 只回填，不导出
    alembic -x confirm_drop=1 -x emit_keys=keys.json upgrade head   # 回填 + 导出新串

没给 ``confirm_drop=1`` 就直接拒绝执行——漏了这一步时，发现它的地方应该是那份明确写下
「先备份、再确认」的运维口令，而不是一句「刚才那条命令少了参数」。``emit_keys`` 指向的
文件里是**明文新 Key**：用完必须删除，不许进仓库。

回填只能逐行在 Python 里算 scrypt（标准库实现，SQL 里没有）。行数就是「签出过多少把 Key」，
量级在个位到百位，一次迁移算得完。

Revision ID: c7a1e9d3b5f4
Revises: a4c81f60d9b2（merge_model_role_pointers）
Create Date: 2026-11-23
"""

import json
import os
from pathlib import Path
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7a1e9d3b5f4"
down_revision: Union[str, None] = "a4c81f60d9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 旧 Key 的形状：``rc_live_`` + 32 位十六进制，**没有行身份证**。回填只认这一种。
LEGACY_PREFIX: str = "rc_live_"

#: 必须显式给的那个参数。名字写成 ``confirm_drop`` 而不是 ``yes``：它说的正是「我知道
#: 这条命令会删掉什么」。
CONFIRM_KEY: str = "confirm_drop"
EMIT_KEY: str = "emit_keys"


def upgrade() -> None:
    arguments = _x_arguments()
    if arguments.get(CONFIRM_KEY) != "1":
        raise RuntimeError(
            "这条迁移会删除 api_keys.key_value（不可逆）：先备份数据库，再显式传 "
            f"-x {CONFIRM_KEY}=1 重跑；要把新 Key 交回给使用者就同时传 "
            f"-x {EMIT_KEY}=<文件路径>。（本次读到的参数：{sorted(arguments)}）"
        )

    op.add_column("api_keys", sa.Column("key_hash", sa.String(length=255), nullable=True))
    op.add_column("api_keys", sa.Column("key_tail", sa.String(length=16), nullable=True))

    bind = op.get_bind()
    rows = (
        bind.execute(sa.text("SELECT id, tenant_id, key_value, label FROM api_keys"))
        .mappings()
        .all()
    )
    emitted = [
        _backfill(bind, row) for row in rows
    ]
    _write_emission(arguments.get(EMIT_KEY), emitted)

    op.drop_index(op.f("ix_api_keys_key_value"), table_name="api_keys")
    op.drop_column("api_keys", "key_value")
    # 回填完才收紧：先 NOT NULL 再回填会让这条迁移在存量行上直接失败。
    op.alter_column("api_keys", "key_hash", nullable=False)
    op.alter_column("api_keys", "key_tail", nullable=False)


def downgrade() -> None:
    """**原则上不可逆**：明文已经不在了，任何「反向迁移」都只能造出一列空值。

    重建一列空的 ``key_value`` 比报错更坑——它会让人以为回滚成功了，而每一把 Key 都
    突然不可用。要回到明文只能从备份恢复。
    """

    raise NotImplementedError(
        "api_keys 的明文已被删除，这条迁移不可回滚：需要回到明文请从备份恢复数据库。"
    )


def _backfill(bind: Any, row: Any) -> dict[str, Any]:
    """把一行的旧明文换成「哈希 + 末 5 位」，并返回它的新串（供 ``emit_keys`` 使用）。"""

    from app.core.passwords import hash_password
    from app.utils.id_generator import (
        API_KEY_MASK_TAIL,
        API_KEY_PREFIX,
        API_KEY_SEPARATOR,
    )

    secret = _legacy_secret(row["key_value"], row["id"])
    bind.execute(
        sa.text(
            "UPDATE api_keys SET key_hash = :key_hash, key_tail = :key_tail WHERE id = :id"
        ),
        {
            "key_hash": hash_password(secret),
            "key_tail": secret[-API_KEY_MASK_TAIL:],
            "id": row["id"],
        },
    )
    return {
        "key_id": row["id"],
        "tenant_id": row["tenant_id"],
        "label": row["label"],
        "key_value": f"{API_KEY_PREFIX}{row['id']}{API_KEY_SEPARATOR}{secret}",
    }


def _legacy_secret(value: str, key_id: str) -> str:
    """旧明文 → 密钥本体；不是旧形状就**失败**而不是猜。

    猜错的代价不对称：猜出一个「看起来能用」的 secret 意味着那把 Key 在迁移完成后永久
    失效，而迁移看起来是成功的。坏数据必须先有人看。
    """

    if value.startswith(LEGACY_PREFIX) and len(value) > len(LEGACY_PREFIX):
        return value[len(LEGACY_PREFIX) :]
    raise RuntimeError(
        f"api_keys 行 {key_id} 的 key_value 不是 {LEGACY_PREFIX!r} + 十六进制的旧形状，"
        "无法回填：先人工核对这一行，再重跑迁移。"
    )


def _write_emission(path: str | None, emitted: list[dict[str, Any]]) -> None:
    """把新串写进 ``emit_keys`` 指定的文件（明文！用完删）。没给路径就什么都不写。"""

    if path is None:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(emitted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _x_arguments() -> dict[str, str]:
    """``alembic -x k=v`` 传进来的参数。

    直接从 ``config.cmd_opts.x`` 自己拆，而不是调 ``context.get_x_argument()``：后者在
    某些调用方式下不可用，而**这里必须准确**——拿不到 ``confirm_drop`` 就该拒绝执行，
    「读不到参数」与「没给参数」必须是同一个结果，绝不能因为读参数本身出错而放行。

    也认环境变量（``ALEMBIC_CONFIRM_DROP=1``）：CI 里拼长命令行时比 ``-x`` 好写。
    """

    parsed: dict[str, str] = {}
    cmd_opts = getattr(op.get_context().config, "cmd_opts", None)
    for item in getattr(cmd_opts, "x", None) or []:
        key, separator, value = str(item).partition("=")
        if separator:
            parsed[key.strip()] = value.strip()
    if os.environ.get("ALEMBIC_CONFIRM_DROP") == "1":
        parsed.setdefault(CONFIRM_KEY, "1")
    if os.environ.get("ALEMBIC_EMIT_KEYS"):
        parsed.setdefault(EMIT_KEY, os.environ["ALEMBIC_EMIT_KEYS"])
    return parsed

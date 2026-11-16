"""把已撤销的模型角色指针并入 model.role.default

Revision ID: a4c81f60d9b2
Revises: d7e3b8a1f642
Create Date: 2026-11-17 04:20:00.000000

第 13 步把模型角色从 ``fast`` / ``strong`` 并成一个 ``default``（见
``app/core/model_roles.py``），但**没有任何迁移去搬那两条指针**。后果是升级后的部署：

* ``model.role.default`` 不存在 → 运行时按解析顺序回落到 ``.env`` 的单供应商；
* ``model.role.fast`` / ``strong`` 留在库里 → 每次读整张 ``runtime_settings`` 都被判
  「不在白名单内」并刷一条 warning。

回落本身不报错，所以症状是「面板上配好的那条接入好像没生效」——排查时最容易被漏掉的
那类。真实踩到：升级后 LLM 一直用 ``.env`` 里那把旧 key 打火山方舟，403
``AccountOverdueError``，而库里那条配置的 key 是好的。

两条动作，都在 ``runtime_settings`` 上：

1. ``model.role.default`` 缺失时，从 ``fast`` / ``strong`` 里**挑一条**（优先 ``fast``）的
   值填进去。已经有 ``default`` 就**不覆盖**：升级不该把管理员后来改过的指向改回去。
2. 删掉 ``model.role.fast`` / ``model.role.strong`` 两行：角色的唯一出处在代码里，
   库里留着它们只会制造「配了但没用」的错觉。

``updated_by`` 写 ``migration``：这一行不是谁在面板上点的，审计里要看得出来。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "a4c81f60d9b2"
down_revision: Union[str, None] = "d7e3b8a1f642"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_KEY = "model.role.default"
_LEGACY_KEYS = ("model.role.fast", "model.role.strong")


def upgrade() -> None:
    # 1. 有旧指针、没有新指针时，把其中一条的值搬过去。
    #    `LIMIT 1` + `ORDER BY key` 让结果确定（fast 排在 strong 前面），
    #    不依赖数据库返回行的顺序。
    op.execute(
        f"""
        INSERT INTO runtime_settings (key, value, updated_at, updated_by)
        SELECT '{_DEFAULT_KEY}', legacy.value, now(), 'migration'
        FROM runtime_settings AS legacy
        WHERE legacy.key IN ('{_LEGACY_KEYS[0]}', '{_LEGACY_KEYS[1]}')
          AND legacy.value <> '""'::jsonb
          AND NOT EXISTS (
              SELECT 1 FROM runtime_settings WHERE key = '{_DEFAULT_KEY}'
          )
        ORDER BY legacy.key
        LIMIT 1
        """
    )

    # 2. 撤销的键不再留在库里。
    op.execute(
        f"""
        DELETE FROM runtime_settings
        WHERE key IN ('{_LEGACY_KEYS[0]}', '{_LEGACY_KEYS[1]}')
        """
    )


def downgrade() -> None:
    """不可逆，且**故意**不逆。

    ``default`` 的值可能来自升级后管理员的修改，把它复制回 ``fast`` / ``strong`` 等于
    凭空造出两个角色——而这两个角色在代码里已经不存在，回滚数据库到第 12 步的同时也该
    回滚代码。真需要回退时，正确做法是整体回滚部署，而不是让这次迁移反向猜一遍。
    """

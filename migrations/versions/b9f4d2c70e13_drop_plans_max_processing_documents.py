"""第 17 步（#218）：删除 ``plans.max_processing_documents``

## 这条迁移改了什么

删掉套餐里「同时处理中篇数」这一列。它支撑的那道门禁（按「索引中且租约有效」的行数判定）
本票一并拆除，上传、两条重建入口、建版本、回滚都不再检查它。

## 为什么删（理由与代价都在 ADR-0034 决策八）

1. **它量的东西本身是假的**：判据是 ``status=3 且租约有效``，而 ``3`` 的可信度完全依赖
   「状态回写是否真的写了库」。回写静默失效的那段时间，它把「其实没人跑」读成「有一堆人
   在跑」，然后把用户挡在门外。一条以状态为判据的门禁，在状态被修好之前只会放大缺陷。
2. **它防的不是容量**：它想防「一个租户上传一百篇把工人占满」，而入口拒绝从来不是对
   「别人排队变长」的处置——那件事的处置是队列与调度，不是拒绝这个用户。
3. **代价已明确承认**：删掉之后**没有按租户的公平调度**，大客户批量上传会占满常规
   Worker。本改造唯一的缓解是「构建走专用队列与专用 worker」。

## 四档原值留档（删除前）

| 档 | free | plus | pro | max |
| --- | --- | --- | --- | --- |
| ``max_processing_documents``（已删除） | 1 | 2 | 10 | 10 |

ADR-0034 决策八有同一张表；这里再写一遍是为了让 ``downgrade`` 有一个不依赖它的来源——
历史迁移不许 import 应用代码（``PLAN_PRESETS`` 早就没有这一列了）。

## downgrade 是「恢复列 + 按四档原值回填」

删列是不可逆的（值已经不在库里），所以降级只能回到**形状 + 四档原值**：运营在这四档上
手工改过的数字回不来——这正是「删列前先把取值留档」的原因。加列时带一个临时的
``server_default='1'`` 让存量行走过 ``NOT NULL`` 校验（``free`` 档恰好就是 1，其余三档
随后被 UPDATE 覆盖），回填完再把默认值去掉，让列的最终形状与它被创建时（
``8a2d3b0e5c71_create_plans.py``：``nullable=False``、无 ``server_default``）一致。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9f4d2c70e13"
down_revision: str | None = "b2c7d4e91a35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 删除前的四档取值（ADR-0034 决策八）。降级按它回填。
LEGACY_MAX_PROCESSING_DOCUMENTS: dict[str, int] = {
    "free": 1,
    "plus": 2,
    "pro": 10,
    "max": 10,
}


def upgrade() -> None:
    op.drop_column("plans", "max_processing_documents")


def downgrade() -> None:
    op.add_column(
        "plans",
        sa.Column(
            "max_processing_documents",
            sa.Integer(),
            nullable=False,
            # 只是为了让存量行能过 NOT NULL；回填完就摘掉（见模块文档）。
            server_default="1",
        ),
    )
    for code, value in LEGACY_MAX_PROCESSING_DOCUMENTS.items():
        op.execute(f"UPDATE plans SET max_processing_documents={int(value)} WHERE code='{code}'")
    op.alter_column("plans", "max_processing_documents", server_default=None)

"""#215 补丁 · ``index_build_versions.auto_activate``：这一版收敛成 ready 后要不要立刻激活

## 为什么需要这一列（它是 #215 引入的一处功能回归的修法）

「管理员改切块设置 → 保存就生效」这条路径的语义是**改完就该生效**（第 16 步 #207），所以
它在起构建时带 ``activate=True``。改造前那个布尔量由 ``build_index_version_task`` 收下，
构建跑完就地 ``activate``。

#215 把构建改成「派活 + 按篇投递」之后，这个布尔量走了这样一条路：

```
console_kb_service(activate=True) -> enqueue_index_build -> dispatch_build_task（载荷带着）
    -> _run_dispatch 收下 -> dispatch() 不收 -> enqueue_build_document 没有这个参数 -> 丢了
```

于是**构建跑完停在 ``ready`` 不激活**：库里 ``active_index_version`` 还是旧的，前端那条
「当前切块设置与生效索引版本不一致」的提示一直挂着，而管理员明明已经保存过。它不报错，
只是「保存了但没生效」——正是这一票最不愿意留下的形态。

## 为什么存在构建行上，而不是塞进每一篇子任务的载荷

三选一，前两个都不成立：

* **塞进每篇子任务载荷**：只有「真的把这一版迁到 ready」的那一次该激活，而子任务自己不知道
  它是不是最后一个（判据看真值不看计数）。让每篇都带、都尝试一次，靠 `ready -> active`
  的真值表守门确实只会成功一次，但这是「用并发去赌唯一性」，而这件事本来有一个天然的单一
  载体：**构建行**。
* **只让派活任务在结尾激活**：派活任务投完消息就结束了，它不知道那些篇什么时候跑完。
* **存在构建行上**：它本来就是「这一次构建的属性」，与 ``documents_planned`` /
  ``failed_documents`` 同族。收敛无论由谁触发（最后一篇 / 清扫器 / 运维脚本），读到同一格，
  行为就一致——这正是 ADR-0034 决策四「收敛可以被任意一方触发」想要的形状。

## 激活失败不抹掉 ready

``auto_activate`` 为真、收敛成 ``ready``、然后激活失败（例如关键词别名切不动）：**留在
``ready``** 并记一条 warning。版本本身是好的，失败的是「切换」那一步；把刚建好的版本标成
失败，运维重建一次也是白建（与 ``build_index_version_task`` 的既有处置逐字一致）。

## downgrade 是完整的

一列纯声明（``false`` = 不自动激活，与改造前的默认行为一致），删掉即回到上一版：
已经 ``ready`` 的行不受影响，只是「下次自动激活」这件事不存在了。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a4b8d6c392"
down_revision: str | None = "e6a1d8c5f247"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "index_build_versions",
        sa.Column("auto_activate", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("index_build_versions", "auto_activate")

"""#215 · ``index_build_versions`` 加两列：失败明细与重投次数

## 为什么是这两列

整库构建从「一个任务跑完整个库」改成「派活 + 每篇一个任务」之后，收尾判据多了一个输入：
**有哪几篇没进来**。真值（该版本该稿下已有切片的文档数）只能回答「够不够」，回答不了
「为什么不够」——而用户要看的恰恰是后者（「3 篇未入：某某.pdf（解析失败）」）。
[ADR-0034](../../docs/adr/0034-queue-state-and-build-convergence.md) 决策九明确了它放在
**构建行**而不是文档行：

* 它是这一版的属性（同一篇可能没进 v2、进了 v3），挂文档行就得每版改写一次；
* 它天然是续建的清单——清扫器读这一格就知道缺哪几篇；
* 版本列表页能直接显示「497 / 500 篇，3 篇未入」。

| 列 | 作用 | 回调为什么是它 |
| --- | --- | --- |
| ``failed_documents`` | ``[{"document_id","title","reason"}]``；非空即收敛判据里的 ``failed > 0`` | ``NULL``：迁移前的老行没有明细，NULL 表示「没记过」而不是「零篇失败」——后者会与「真值不够」混为一谈 |
| ``redispatch_count`` | 清扫器重投过几次 | ``0``：还没重投过 |

## 为什么需要重投次数上限

清扫器的兜底是「按真值 diff 出缺失篇目再投一次」。没有次数上限时，一个**永远救不回来**的
构建（例如某篇文档的源文件坏了）会被无限重投：清扫器每轮都数出同一个缺口、投同一批任务，
队列里因此常年躺着一批必然失败的消息。上限（:data:`MAX_REDISPATCH`，见构建服务）到顶就收成
``failed``——「救不回来」也是一个必须兑现的结论，不是可以一直拖着的事。

## downgrade 是完整的

两列都只承载「这一次」的过程数据（明细可从 ``chunks`` 与文档表反推，重投次数是纯计数），
删掉即回到上一版；已经有结论的行不受影响。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4e8f1b6a2d7"
down_revision: str | None = "b9f4d2c70e13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "index_build_versions",
        sa.Column("failed_documents", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "index_build_versions",
        sa.Column("redispatch_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("index_build_versions", "redispatch_count")
    op.drop_column("index_build_versions", "failed_documents")

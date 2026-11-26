"""#214 · ``index_build_versions`` 加四列：参与篇数、稿号、进度、收敛期限

## 为什么要这四列

「这次构建该不该收尾」原来靠一个**假计数**：构建循环丢掉了每篇重建函数的返回值，抢不到
也算一篇「已重建」，而唯一的「一篇都没重建」守卫数的正是这个假计数；四项计数对账在
「切片 0 / 向量 0 / 关键词 0」时三方相等，于是**一篇都没真正重建的构建也能 ready 并激活**
——线上静默变成「版本正常、检索全空」（[ADR-0034](../../docs/adr/0034-queue-state-and-build-convergence.md)
决策三、决策四）。

改成读真值之后，判据需要两样东西才成立，于是各有一列：

| 列 | 作用 | 默认值为什么是它 |
| --- | --- | --- |
| ``documents_planned`` | 参与篇数快照 = 收尾时比较的**分母** | ``0``：「这个库起构建时没有可参与的文档」是一个合法事实（空库也能建版本），不该用 NULL 把它与「没记过」混在一起 |
| ``doc_version`` | 稿号快照 = 判据的**另一半**（``该版本 + 该稿``） | ``1``：存量库里当前稿指针几乎都是 1（回滚才会前移），这是唯一有依据的缺省 |
| ``documents_done`` | 进度 | ``0``：一开头就是零篇 |
| ``converge_deadline`` | 构建行自己的租约（最晚给结论的时间点） | ``now()``：**没写期限的最保守解释就是「已经该收尾了」**，宁可让清扫器早看一眼，也不留一个永远等不到结论的 ``building`` 行 |

``documents_done`` **不参与任何判定**（真值来自 ``chunks`` 表的 ``COUNT(DISTINCT document_id)``）；
它存在只是为了界面能显示「重建中 37 / 500」。两者的分工写死在
``app/services/index_build_service.py::decide_convergence`` 的注释里，别处不许拿它冒充判据。

## 为什么回填存量 ``building`` 行

迁移之前起的那几次构建还停在 ``building``，它们**没有快照**。若不管，它们会带着默认的
``documents_planned = 0`` 进判据——分母为零意味着「真值 0 也够」，恰好把这次要堵的那条路
（空版本上线）又留了一条缝。所以这里按**此刻**库里的可参与篇数与稿号把它们补上：推迟的
那点时间不影响正确性（判据要的是「够不够」，不是「是不是同一刻数的」），而它堵住了缝。

停在其他状态（``ready`` / ``active`` / ``archived`` / ``failed``）的行不回填：它们不会再被
收敛，回填只会制造「看起来像真的」的数字。

## downgrade 是完整的

四列都是本次新增的、不承载不可再生数据（快照可以从库里重算），删掉即回到上一版；已经有
结论的行不受影响，只是回到「判据不存在」的旧行为。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c7d4e91a35"
down_revision: str | None = "f8b3c2d91a70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 与 ``app/utils/status.py::DocumentStatus.SUCCESS`` 同值（迁移不 import 应用代码：
#: 迁移是一次性脚本，让它跟着应用代码改会被下次重跑的历史悄悄改掉）。
_SUCCESS = 1

#: 回填：可参与篇数 = 该库 ``status=1`` 的行数；稿号 = 这批文档当前稿指针的最大值。
#: 两条子查询都带租户，范围与建构建时的那次读数逐字相同（``kb_id`` + ``tenant_id``）。
_BACKFILL_BUILDING = sa.text(
    "UPDATE index_build_versions AS b SET "
    "documents_planned = ("
    "  SELECT count(*) FROM documents d "
    "  WHERE d.kb_id = b.kb_id AND d.tenant_id = b.tenant_id AND d.status = "
    f"{_SUCCESS}"
    "), "
    "doc_version = COALESCE(("
    "  SELECT max(d.current_version) FROM documents d "
    "  WHERE d.kb_id = b.kb_id AND d.tenant_id = b.tenant_id AND d.status = "
    f"{_SUCCESS}"
    "), 1), "
    # 期限按「开始那一刻 + 一小时」补：老行没有篇数可估，一小时沿用这个仓库里「一次构建
    # 到底跑没跑」的既有口径（见 ``index_build_service.BUILD_STALL_SECONDS``）。
    "converge_deadline = b.created_at + interval '1 hour' "
    "WHERE b.status = 'building'"
)


def upgrade() -> None:
    op.add_column(
        "index_build_versions",
        sa.Column("documents_planned", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "index_build_versions",
        sa.Column("doc_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "index_build_versions",
        sa.Column("documents_done", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "index_build_versions",
        sa.Column(
            "converge_deadline",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(_BACKFILL_BUILDING)


def downgrade() -> None:
    op.drop_column("index_build_versions", "converge_deadline")
    op.drop_column("index_build_versions", "documents_done")
    op.drop_column("index_build_versions", "doc_version")
    op.drop_column("index_build_versions", "documents_planned")

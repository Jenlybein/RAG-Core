"""第 17 步现场九 · 第一步：给「索引代」加列并回填（**不删任何东西**）。

## 这一条迁移的任务边界

只做加法：

1. ``documents.current_index_generation`` —— 这篇文档现在用的是第几代产物（``NULL`` = 还没有）；
2. ``documents.settings_hash`` —— 这一代产物的切块参数指纹（回答「要不要更新」）；
3. ``chunks.generation`` —— 这一片属于该文档的第几代 + 配套索引。

**一条 DROP 都没有**。删旧列与旧表是紧接的下一条迁移，中间留一个**停点**：回填对不对，
在这一步就能逐篇验出来；不对就停在这里，旧列旧表一个都还在，回滚只回滚一步。

## 回填口径

* ``chunks.generation``：切片所属的库级版本名 **等于该库当前生效版本名** → 第 1 代；
  否则 → 第 0 代（历史产物。改造前从来没有回收过它们，这一列正好把它们标出来）；
* ``documents.current_index_generation``：该篇在生效版本里有切片 → 第 1 代；
  一片都没有 → **``NULL``**。``NULL`` 是「还没有产物」，不是「第 1 代但恰好是空」——
  两者在检索侧的处理完全不同（前者跳过并计数，后者是一次正常的空结果），
  用一个值表示两件事，第三根轴还没上线就已经说不清了；
* ``documents.settings_hash``：取**该库生效那一版**的参数快照算指纹。用「生效版本的参数」
  而不是「库当前设置」，是因为两者可以不一致（改了设置但没重建）：拿库当前设置回填，会把
  一批**实际上用旧参数算出来**的切片标成「已是最新」，于是界面永远不提示更新——那不是少
  提示一次，是**永远不再提示**。

## 为什么停点检查放在这条迁移里，而不是写成脚本

这一轮反而要删掉三个脚本。检查写在这里，它就不可能被忘记跑：迁移失败 = 部署停 = 旧列旧表
一个都没删。检查是**两条独立的查询**，不能并成一条：

* **一致性**：逐篇比「第 1 代切片数」与「生效版本里的切片数」。它抓的是「回填漏了」——
  典型是切片所属的库行已经不在（孤儿切片会保住列缺省值 1）；
* **可用性**：``SUCCESS`` 但第 1 代零片的篇数。它抓的是「切过去之后这篇查不到」，正是本仓
  最怕的那类静默故障（「套件全绿、线上查空」）。这一条**必须单独计数**：并进上一条的
  ``WHERE backfilled <> expected`` 里会被过滤掉，因为零片那几篇恰好 ``0 = 0``、不满足不等式
  ——写成一条 SQL 时这个过滤器会让它永远返回 0，等于把守卫写成了一句安慰。

## 为什么指纹在 Python 里逐行算

判「这份产物用的是哪份设置」只有一份口径（:func:`app.services.index_generation.settings_fingerprint`），
迁移与运行时必须共用它。在 SQL 里再写一遍（``md5(params::text)`` 之类）就有了两份口径，
它们迟早分叉，而分叉的症状是「设置没改，界面却说全库待更新」。
代价照 ADR-0031 决策五记过的同一条：**这条迁移不能 ``--sql`` 离线渲染**，所以第一行就
显式拒绝（``op.get_context().as_sql``），而不是写了一半 DDL 再崩一个看不懂的错。

## 与下一条迁移之间的过渡态（说清楚，不假装没有）

本步不改写路径：这期间新索引出来的切片会拿到列的缺省值（第 1 代），与回填出来的第 1 代
**混在一起**。这是有意的——两条迁移之间的窗口里，线上还在用库级版本那套机制，产物确实
「只有一代」。第二步把写路径改成「每次索引新开一代」之后，这个混同自然结束；下一条迁移的
停点会按那时的口径重新对账，不会沿用这一条的数字。

Revision ID: c8e3f1a5d704
Revises: e4b1d7a9c2f5（现场五的过渡密钥四列）
Create Date: 2026-11-28 05:20:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.models.chunk import DEFAULT_GENERATION
from app.services.index_generation import SUPERSEDED_GENERATION, settings_fingerprint

# revision identifiers, used by Alembic.
revision: str = "c8e3f1a5d704"
down_revision: Union[str, None] = "e4b1d7a9c2f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 三列的名字（与 ``app/models/document.py`` / ``app/models/chunk.py`` 同一份）。
_DOCUMENT_GENERATION_COLUMN = "current_index_generation"
_DOCUMENT_SETTINGS_HASH_COLUMN = "settings_hash"
_CHUNK_GENERATION_COLUMN = "generation"

#: 切片上「按库收代」的索引。**读路径**按 ``(租户, 库, 代)`` 收窄走它；旧代回收的候选查询
#: 不带租户与库，走的是后加的 ``ix_chunks_document_id_generation``（见 ``d91f4b7c3e28``）。
_CHUNK_GENERATION_INDEX = "ix_chunks_tenant_id_kb_id_generation"

#: 离线渲染的拒绝文案：逐行算指纹需要真库，异地渲染 SQL 再执行的玩法对它不适用。
_OFFLINE_REFUSAL = (
    "这条迁移要在 Python 里逐篇算切块参数指纹，需要真库（不能 --sql 离线渲染）。"
    "请在能连库的环境里执行 alembic upgrade head。"
)

#: 回填切片代数的 SQL。判据是「版本名是否等于该库生效版本名」，库里求值最准，也不要把
#: 两千多行切片拉回 Python。
_BACKFILL_CHUNK_GENERATION = f"""
UPDATE chunks AS c
SET {_CHUNK_GENERATION_COLUMN} = CASE
        WHEN kb.active_index_version IS NOT NULL
         AND c.index_version = kb.active_index_version THEN {DEFAULT_GENERATION}
        ELSE {SUPERSEDED_GENERATION}
    END
FROM knowledge_bases AS kb
WHERE kb.id = c.kb_id
"""

#: 文档代数：有产物 → 第 1 代；没有 → ``NULL``（「还没有产物」，与「第 1 代但为空」分开）。
_BACKFILL_DOCUMENT_GENERATION = f"""
UPDATE documents AS d
SET {_DOCUMENT_GENERATION_COLUMN} = CASE
        WHEN EXISTS (
            SELECT 1 FROM chunks c
             WHERE c.document_id = d.id
               AND c.{_CHUNK_GENERATION_COLUMN} = {DEFAULT_GENERATION}
        ) THEN {DEFAULT_GENERATION}
        ELSE NULL
    END
"""

#: 停点①：逐篇「回填的第 1 代片数」与「生效版本里的片数」是否一致。
_STOP_POINT_CONSISTENCY = f"""
SELECT count(*)
FROM (
    SELECT d.id AS document_id,
           (SELECT count(*) FROM chunks c
             WHERE c.document_id = d.id
               AND c.{_CHUNK_GENERATION_COLUMN} = {DEFAULT_GENERATION}) AS backfilled,
           (SELECT count(*) FROM chunks c
              JOIN knowledge_bases kb ON kb.id = c.kb_id
             WHERE c.document_id = d.id
               AND c.index_version = kb.active_index_version) AS expected
    FROM documents d
) AS per_document
WHERE backfilled <> expected
"""

#: 停点②：``SUCCESS`` 却没有第 1 代切片的篇数（切过去之后会查不到）。
#:
#: 单独一条查询，**不能**并进上面那条的 ``WHERE``：零片那几篇满足 ``0 = 0``、被不等式过滤掉，
#: 于是这个读数会永远是 0。
_STOP_POINT_EMPTY_PRODUCTS = f"""
SELECT count(*)
FROM documents d
WHERE d.status = 1
  AND NOT EXISTS (
      SELECT 1 FROM chunks c
       WHERE c.document_id = d.id
         AND c.{_CHUNK_GENERATION_COLUMN} = {DEFAULT_GENERATION}
  )
"""


def upgrade() -> None:
    _reject_offline_render()

    # ① 三列。``current_index_generation`` 允许为空：``NULL`` = 还没有任何一代产物。
    op.add_column(
        "documents",
        sa.Column(_DOCUMENT_GENERATION_COLUMN, sa.Integer(), nullable=True),
    )
    # ② 指纹允许为空：空 = 「这批产物的参数来路不明」（例如生效版本没有 params 快照）。
    op.add_column(
        "documents",
        sa.Column(_DOCUMENT_SETTINGS_HASH_COLUMN, sa.String(length=64), nullable=True),
    )
    op.add_column(
        "chunks",
        sa.Column(
            _CHUNK_GENERATION_COLUMN,
            sa.Integer(),
            nullable=False,
            server_default=str(DEFAULT_GENERATION),
        ),
    )
    op.create_index(
        _CHUNK_GENERATION_INDEX,
        "chunks",
        ["tenant_id", "kb_id", _CHUNK_GENERATION_COLUMN],
    )

    # ③ 切片分代 → ④ 文档代数 → ⑤ 参数指纹（逐行 Python，理由见模块文档）。
    op.execute(sa.text(_BACKFILL_CHUNK_GENERATION))
    op.execute(sa.text(_BACKFILL_DOCUMENT_GENERATION))
    _backfill_settings_hash()

    # ⑥ 停点：任一条不过就让部署停在这里（旧列旧表一个都没删）。
    _assert_stop_point()


def _reject_offline_render() -> None:
    """``--sql`` 离线渲染直接拒绝（第一行就拒，不留半份 DDL 脚本）。"""

    if getattr(op.get_context(), "as_sql", False):
        raise RuntimeError(_OFFLINE_REFUSAL)


def _backfill_settings_hash() -> None:
    """按库把「生效版本的参数指纹」写给这个库的每一篇文档。"""

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT kb.id AS kb_id, iv.params AS params
            FROM knowledge_bases AS kb
            JOIN index_build_versions AS iv
              ON iv.kb_id = kb.id AND iv.version = kb.active_index_version
            WHERE iv.params IS NOT NULL
            """
        )
    ).fetchall()
    for row in rows:
        fingerprint = settings_fingerprint(row.params)
        if fingerprint is None:
            # 快照缺规范键（更老的版本行）：留空 = 来路不明，界面按落后提示。
            continue
        bind.execute(
            sa.text(
                f"UPDATE documents SET {_DOCUMENT_SETTINGS_HASH_COLUMN} = :fingerprint "
                "WHERE kb_id = :kb_id"
            ),
            {"fingerprint": fingerprint, "kb_id": row.kb_id},
        )


def _assert_stop_point() -> None:
    """两条独立的对账；任一不过就中止迁移。"""

    bind = op.get_bind()
    mismatched = int(bind.execute(sa.text(_STOP_POINT_CONSISTENCY)).scalar() or 0)
    empty_products = int(bind.execute(sa.text(_STOP_POINT_EMPTY_PRODUCTS)).scalar() or 0)
    if mismatched or empty_products:
        raise RuntimeError(
            "索引代回填的停点检查没通过，迁移中止（旧列与旧表一个都没删）。"
            f"逐篇对不上的文档数={mismatched}，其中「切过去会查不到」的篇数={empty_products}。"
            "先修好那几篇（在旧代码上重建一次即可），再重新执行迁移。"
        )


def downgrade() -> None:
    """对称地拆掉这三列与那个索引（回填不写任何既有列，所以不需要撤数据）。"""

    op.drop_index(_CHUNK_GENERATION_INDEX, table_name="chunks")
    op.drop_column("chunks", _CHUNK_GENERATION_COLUMN)
    op.drop_column("documents", _DOCUMENT_SETTINGS_HASH_COLUMN)
    op.drop_column("documents", _DOCUMENT_GENERATION_COLUMN)

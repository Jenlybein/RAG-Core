"""第 17 步现场九 · 第一步：给「索引代」加列并回填（**不删任何东西**）。

## 这一条迁移的任务边界

只做加法：

1. ``documents.current_index_generation`` —— 这篇文档现在用的是第几代产物；
2. ``documents.settings_hash`` —— 这篇产物的切块参数指纹（回答「要不要更新」）；
3. ``chunks.generation`` —— 这一片属于该文档的第几代 + 配套索引。

**一条 DROP 都没有**。删旧列与旧表是紧接的下一条迁移，中间留一个**停点**：回填对不对，
在这一步就能逐篇验出来；不对就停在这里，旧列旧表一个都还在，回滚只回滚一步。

## 回填口径（两句话）

* ``chunks.generation``：切片所属的库级版本名 **等于该库当前生效版本名** → 第 1 代；
  否则 → 第 0 代（历史产物，改造前从来没有回收过它们，这一列正好把它们标出来）；
* ``documents.settings_hash``：取**该库当前生效那一版**的参数快照算指纹。这一版就是线上
  正在服务的那份产物，它用的参数才是「这批切片是用哪份设置算的」唯一有据可查的答案。

用「生效版本的参数」而不是「库当前设置」，是因为两者可以不一致（改了设置但没重建）：
拿库当前设置回填，会把一批**实际上用旧参数算出来的**切片标成「已是最新」，于是界面永远
不提示更新——那不是少提示一次，是**永远不再提示**。

## 为什么停点检查放在这条迁移里，而不是写成脚本

运维脚本已经不缺了（这一轮反而要删掉三个）。检查写在这里，它就不可能被忘记跑：
迁移失败 = 部署停 = 旧列旧表一个都没删。检查的内容是**逐篇**的：

* 每一篇 ``SUCCESS`` 的文档，第 1 代的切片数必须等于「生效版本里的切片数」（回填的正确性）；
* 且不能为零——零意味着切过去之后这篇**查不到**，正是本仓最怕的那类静默故障
  （「套件全绿、线上查空」）。它在这里暴露成一次失败的部署，而不是上线后的一条投诉。

## 为什么指纹在 Python 里逐行算

判「这份产物用的是哪份设置」只有一份口径（:func:`app.services.index_generation.settings_fingerprint`），
迁移与运行时必须共用它。在 SQL 里再写一遍（``md5(params::text)`` 之类）就有了两份口径，
它们迟早分叉，而分叉的症状是「设置没改，界面却说全库待更新」。
代价照 ADR-0031 决策五记过的同一条：**这条迁移不能 ``--sql`` 离线渲染**，检查到离线就
显式拒绝，而不是在 ``op.get_bind()`` 上崩一个看不懂的错。

Revision ID: c8e3f1a5d704
Revises: e4b1d7a9c2f5（现场五的过渡密钥四列）
Create Date: 2026-11-28 05:20:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.services.index_generation import settings_fingerprint

# revision identifiers, used by Alembic.
revision: str = "c8e3f1a5d704"
down_revision: Union[str, None] = "e4b1d7a9c2f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 三列的名字（与 ``app/models/document.py`` / ``app/models/chunk.py`` 同一份）。
_DOCUMENT_GENERATION_COLUMN = "current_index_generation"
_DOCUMENT_SETTINGS_HASH_COLUMN = "settings_hash"
_CHUNK_GENERATION_COLUMN = "generation"

#: 切片上「按库收代」的索引。检索与回收都按 ``(租户, 库, 代)`` 收窄。
_CHUNK_GENERATION_INDEX = "ix_chunks_tenant_id_kb_id_generation"

#: 离线渲染的拒绝文案：逐行算指纹需要真库，异地渲染 SQL 再执行的玩法对它不适用。
_OFFLINE_REFUSAL = (
    "这条迁移要在 Python 里逐篇算切块参数指纹，需要真库（不能 --sql 离线渲染）。"
    "请在能连库的环境里执行 alembic upgrade head。"
)

#: 回填第 0 代 / 第 1 代的那条 SQL。写成一句 SQL 而不是逐行循环：判据是「版本名是否等于
#: 该库生效版本名」，库里求值最准，也不需要把 2000 多行切片拉回 Python。
_BACKFILL_CHUNK_GENERATION = f"""
UPDATE chunks AS c
SET {_CHUNK_GENERATION_COLUMN} = CASE
        WHEN kb.active_index_version IS NOT NULL
         AND c.index_version = kb.active_index_version THEN 1
        ELSE 0
    END
FROM knowledge_bases AS kb
WHERE kb.id = c.kb_id
"""

#: 停点检查：逐篇比「第 1 代的切片数」与「生效版本里的切片数」，并挑出零片的那几篇。
_STOP_POINT_QUERY = f"""
WITH per_document AS (
    SELECT d.id AS document_id,
           d.status AS status,
           (SELECT count(*) FROM chunks c
             WHERE c.document_id = d.id AND c.{_CHUNK_GENERATION_COLUMN} = 1) AS backfilled,
           (SELECT count(*) FROM chunks c
              JOIN knowledge_bases kb ON kb.id = c.kb_id
             WHERE c.document_id = d.id AND c.index_version = kb.active_index_version) AS expected
    FROM documents d
)
SELECT count(*) AS mismatched,
       count(*) FILTER (WHERE status = 1 AND backfilled = 0) AS empty_products
FROM per_document
WHERE backfilled <> expected
"""


def upgrade() -> None:
    # ① 三列都带 ``server_default``：老库里已有的行不必先写一遍再改约束，加列瞬间完成。
    op.add_column(
        "documents",
        sa.Column(_DOCUMENT_GENERATION_COLUMN, sa.Integer(), nullable=False, server_default="1"),
    )
    # ② 指纹允许为空：空 = 「这批产物的参数来路不明」（例如生效版本没有 params 快照）。
    #    空一律按「落后」处理，于是界面会提示更新——宁可多提示一次。
    op.add_column(
        "documents",
        sa.Column(_DOCUMENT_SETTINGS_HASH_COLUMN, sa.String(length=64), nullable=True),
    )
    op.add_column(
        "chunks",
        sa.Column(_CHUNK_GENERATION_COLUMN, sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index(
        _CHUNK_GENERATION_INDEX,
        "chunks",
        ["tenant_id", "kb_id", _CHUNK_GENERATION_COLUMN],
    )

    # ③ 切片分代。
    op.execute(sa.text(_BACKFILL_CHUNK_GENERATION))

    # ④ 文档的参数指纹：从「该库生效那一版」的 params 算。逐行 Python 算，理由见模块文档。
    _backfill_settings_hash()

    # ⑤ 停点：对不上就让部署停在这里（旧列旧表一个都没删）。
    _assert_stop_point()


def _backfill_settings_hash() -> None:
    """按库把「生效版本的参数指纹」写给这个库的每一篇文档。"""

    bind = op.get_bind()
    if bind is None:  # pragma: no cover - 离线渲染分支（上面已拒）
        raise RuntimeError(_OFFLINE_REFUSAL)

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
        if fingerprint is None:  # pragma: no cover - 上面 WHERE 已排掉 NULL
            continue
        bind.execute(
            sa.text(
                f"UPDATE documents SET {_DOCUMENT_SETTINGS_HASH_COLUMN} = :fingerprint "
                "WHERE kb_id = :kb_id"
            ),
            {"fingerprint": fingerprint, "kb_id": row.kb_id},
        )


def _assert_stop_point() -> None:
    """逐篇对账；有任何一篇对不上（或切过去之后会查不到）就中止迁移。"""

    bind = op.get_bind()
    if bind is None:  # pragma: no cover
        raise RuntimeError(_OFFLINE_REFUSAL)
    row = bind.execute(sa.text(_STOP_POINT_QUERY)).fetchone()
    if row is None:  # pragma: no cover - 聚合查询恒有一行
        return
    mismatched = int(row[0] or 0)
    empty_products = int(row[1] or 0)
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

"""第 17 步现场九 · 第六步：给旧代回收的候选查询加索引（**只加索引，不删不填**）。

## 为什么单独一条迁移

``c8e3f1a5d704`` 建的是 ``(tenant_id, kb_id, generation)``——它服务**读路径**（库 + 代 +
当前稿）。而回收的候选查询长这样：

```sql
SELECT document_id FROM chunks GROUP BY document_id
HAVING count(DISTINCT generation) > 2
ORDER BY document_id LIMIT :batch
```

它**不带**租户、也不带库（判据只看「这一篇存在几代」），于是那条复合索引的头两列用不上，
PG 只能全表扫 ``chunks``——全库最大的表，而且每晚都扫一次（稳态下也扫，因为要先知道有没有
超窗的篇）。加上 ``(document_id, generation)`` 之后候选查询退化成 index-only scan，稳态几乎
不返回行。

## 为什么放在这条迁移里，而不是并进前一条

前一条已经发到 ``main`` 并且本地库已经在 ``c8e3f1a5d704`` 这个 head 上跑过——改一条**已经
执行过的**迁移，等于让「库里的结构」与「迁移说的是什么」脱钩。加索引就单开一条，可重复执行
（``CREATE INDEX`` 带 ``IF NOT EXISTS`` 语义由 alembic 的 ``create_index`` 不保证，所以这里
在 downgrade 里对称删掉）。

## 只有 DDL，可以离线渲染

这条不读写数据，``--sql`` 渲染是安全的（与前一条不同：那一条要在 Python 里逐篇算指纹）。
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d91f4b7c3e28"
down_revision: Union[str, None] = "c8e3f1a5d704"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: 回收候选查询用的索引：``PARTITION BY document_id ORDER BY generation`` 与
#: ``GROUP BY document_id HAVING count(DISTINCT generation)`` 两条都靠它。
_RETENTION_INDEX = "ix_chunks_document_id_generation"


def upgrade() -> None:
    op.create_index(_RETENTION_INDEX, "chunks", ["document_id", "generation"])


def downgrade() -> None:
    op.drop_index(_RETENTION_INDEX, table_name="chunks")

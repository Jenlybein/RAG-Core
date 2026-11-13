"""表 4：retrieval_traces —— 归属索引（#101 改名并只留结构）。

**这张表回答「那一次调用发生过什么」，不回答「那一次问了什么」**（ADR-0021 决策 2）。
问句原文、改写句、检索用句、切片正文、任何分数明细都不在这里：它跟业务库同生共死，
备份、只读副本、``pg_dump`` 都会把它带走，所以它上面的每一个字都是长期承诺。原文只允许
进内网观测栈（按 ``trace_id`` 查回来）。

留下的十列全部可枚举：

====================  ==========================================================
``id``                行 id，**对外即 ``log_id``**；由中台用 UUID v4 生成
``tenant_id``         谁调用的
``trace_id``          ``log_id`` 的确定性派生（32 位十六进制），NOT NULL
``kb_ids``            这次查了哪些库（一次检索可以多库）
``chunk_ids``         交付单元的代表片 + 溯源片 id，**只存 id**
``top_k``             这次要了几条
``latency_ms``        这次多慢
``degraded_reason``   降级原因，没有就是 NULL
``empty_reason``      空结果原因，没有就是 NULL
``created_at``        什么时候
====================  ==========================================================

三条不能改的性质：

* ``trace_id`` **由派生得到，不由外部服务分配**：``log_id`` 先生成、``trace_id`` 立刻派生，
  然后才尝试写库。写入失败只留 warning，响应里的 ``trace_id`` 仍然有值——反过来
  「写成功才拿到 id」等于把契约挂在旁路的健康度上（ADR-0010 第五次修订）。
* ``trace_id`` 唯一：一次检索一行，与响应里那根线一一对应，于是「按 trace_id 回查」不需要
  第二张对照表。
* 写入必须走**独立短事务**（``app/services/retrieval_trace.py``）：请求会话 ``get_async_db`` 从不
  ``commit``，在它上面顺手 INSERT 会被 ``db.close()`` 丢掉。

表名的来历写在导入说明里：这张表在 #101 改了名，旧名只允许留在两条迁移里（一条创建、
一条改名）——代码里不再出现它，见 ``tests/test_trace_identity.py`` 的全树扫描用例。
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now


class RetrievalTrace(Base):
    __tablename__ = "retrieval_traces"

    #: 行 id——**对外身份就是它**（``log_id``）。响应里不出现，但报障时运维可以用它定位。
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    #: 32 位十六进制。唯一索引不是性能优化，而是把「一行一次检索」钉进库里。
    trace_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    #: 一次检索可以查多个库，所以是列表而不是单值（旧表的 ``kb_id`` 单列已在 #101 删除）。
    kb_ids: Mapped[list] = mapped_column(JSONB, default=list)
    #: 只存 id：正文在切片表里有一份，分数明细属于评测与 trace。重复存只会多一份会过期的副本。
    chunk_ids: Mapped[list] = mapped_column(JSONB, default=list)
    top_k: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    #: 可空：没有降级 / 不是空结果时它就是 NULL。它不是常驻字段，调用方别拿它当枚举兜底。
    degraded_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    empty_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

"""表 4：usage_events —— 一次请求一行的「账 + 归属索引」（第 14 步改名）。

**这张表回答「那一次调用发生了什么」，不回答「那一次问了什么」**（ADR-0021 决策 2）。
问句原文、改写句、检索用句、切片正文、任何分数明细都不在这里：它跟业务库同生共死，
备份、只读副本、``pg_dump`` 都会把它带走，所以它上面的每一个字都是长期承诺。原文只允许
进内网观测栈（按 ``trace_id`` 查回来）。

## 它为什么有两个身份

原来它只装「走到响应成形」的检索，是一份**归属索引**：拿 ``trace_id`` 回查那一次发生了什么。
第 14 步起它同时是**计量账**：进了门的请求就落一行，被挡下的（``20013`` / ``20014`` / ``20033``）
也落，并带上业务码与三本账的实际计数。只有这样「成功率」才有分母——不落行的那一版，
分母永远是空的。

``20010``（鉴权失败）**不落行**：没有身份就没有租户与 Key，一行账无从归属；而且它不需要过
鉴权，落行等于给未认证流量开写库通道（见 ``usage_meter`` 与 ADR-0021 修订）。

两个身份共用一行的代价写在 ADR-0021 的修订里：观测页看「全部」时总量会包含被挡下的请求，
所以那个页面拆成「全部 / 只看成功 / 只看失败」三档，而不是偷偷换掉旧语义。

## 十八列全部可枚举

====================  ==========================================================
``id``                行 id，**对外即 ``log_id``**；由中台用 UUID v4 生成
``tenant_id``         谁调用的
``trace_id``          ``log_id`` 的确定性派生（32 位十六进制），NOT NULL
``key_id``            哪把 Key（第 14 步新增；老行为 NULL，归「历史汇总」）
``endpoint``          哪条端点：``retrieve`` / ``rewrite`` / ``decompose`` / ``research``
``code``              业务码：0 = 成功；``20010`` / ``20013`` / ``20014`` / ``5xxxx``
``meter_r``           检索账这次计几次（decompose 是 N 次）
``meter_rr``          重排账这次计几次
``meter_llm``         大模型账这次计几次（research 是 M 次）
``kb_ids``            这次查了哪些库（一次检索可以多库）
``index_versions``    这次查的是**哪一次构建**：``{kb_id: version}`` 映射
``doc_versions``      这次命中的是**第几稿**：``{document_id: doc_version}`` 映射
``chunk_ids``         交付单元的代表片 + 溯源片 id，**只存 id**
``top_k``             这次要了几条
``latency_ms``        这次多慢
``degraded_reason``   降级原因，没有就是 NULL
``empty_reason``      空结果原因，没有就是 NULL
``created_at``        什么时候
====================  ==========================================================

``index_versions`` 是**契约变更**（ADR-0025 决策六，本表因此从十列变十一列）：版本一旦
会变，同一个 ``kb_ids + query`` 两次调用的差异就无从归因——切法变了？文档变了？还是召回
抖了？它存的是**映射**而不是两个并列数组（``kb_ids`` 一个、``versions`` 一个）：那正是
ADR-0010 §5 明令禁止的「两个独立集合」，``['kb-a','kb-b']`` 配 ``['v1','v2']`` 拼得出
「A 库配 B 库版本」这种不存在的组合，而且不报错。版本名是结构（``v1``），不是原文，
所以 ADR-0021「只存结构」的红线不受影响。

``doc_versions`` 是**又一次契约变更**（#179，本表因此从十一列变十二列），形状与
``index_versions`` **完全同形**，理由也同一条：**文档 → 稿号**的配对关系只能从映射里
读出来。为什么要它（用户故事第 3 条）：``chunk_ids`` 只答得出「命中过哪些片」，而片会被
保留策略物理删掉（#178），删掉之后那一串 id 就再也答不出「那次查的是第几稿」——
稿号必须自己留在这一行上。稿号是整数、文档 id 是结构，两者都不是原文。

## 三条不能改的性质

* ``trace_id`` **由派生得到，不由外部服务分配**：``log_id`` 先生成、``trace_id`` 立刻派生，
  然后才尝试写库。写入失败只留 warning，响应里的 ``trace_id`` 仍然有值——反过来
  「写成功才拿到 id」等于把契约挂在旁路的健康度上（ADR-0010 第五次修订）。
* ``trace_id`` 唯一：一次检索一行，与响应里那根线一一对应，于是「按 trace_id 回查」不需要
  第二张对照表。
* 写入必须走**独立短事务**（``app/services/retrieval_trace.py``）：请求会话 ``get_async_db`` 从不
  ``commit``，在它上面顺手 INSERT 会被 ``db.close()`` 丢掉。

## 两条索引各管一个读法

``(tenant_id, created_at)`` 给租户看自己的窗口（先按租户过滤，再按时间切片），
``(key_id, created_at)`` 给 per-Key 视图。「只看时间」这种跨租户查询只出现在管理员那条路，
它靠这两条索引里 ``created_at`` 的第二位做 range scan 已经够用——不要为此再加第三条
只有 ``created_at`` 的索引。

表名的来历：它在 #101 之前叫过一个更窄的名字（初始迁移里保留），#101 之后叫「检索轨迹」时代的名字，
第 14 步改名 ``usage_events``（它装的不再只是「检索的轨迹」）。旧名只允许留在迁移里——代码里不再
出现它们，见 ``tests/test_trace_identity.py`` 的全树扫描用例（那个用例也把本文件扫进去，
所以这段说明只能用「更窄的名字」这种说法，不能写出旧名本身）。
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        # 租户视角：先过滤租户、再切时间窗（两个查询都长这样）
        Index("ix_usage_events_tenant_created", "tenant_id", "created_at"),
        # per-Key 视角：同上，只是第一列换成 key_id
        Index("ix_usage_events_key_created", "key_id", "created_at"),
    )

    #: 行 id——**对外身份就是它**（``log_id``）。响应里不出现，但报障时运维可以用它定位。
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    #: 32 位十六进制。唯一索引不是性能优化，而是把「一行一次检索」钉进库里。
    trace_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    #: 哪把 Key。**可空**：第 14 步之前的行没有这个信息，回填只能靠猜——猜错比没有更糟，
    #: 所以宁可留 NULL，读侧把它归进「历史汇总」那一格。
    key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    #: 哪条端点。同样可空：老行分不出是 retrieve 还是 research（那时没有这一列的语义），
    #: 而「都算 retrieve」是个会被当真的谎。
    endpoint: Mapped[str | None] = mapped_column(String(24), nullable=True)
    #: 业务码。0 = 成功；非 0 与 ``ErrorCode`` 对齐。**这是成功率的分母依据**。
    #: 老行为 NULL（那时只记成功，没有码可记）。NULL **不是 0**：它既不算成功也不算失败，
    #: 读侧把它归进「未统计」，并且**不进成功率的分母**（ADR-0033 §3）。
    code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 三本账各计几次。默认 0 而不是 NULL：新行必须显式说出「这本账这次计了没有」，
    #: 而 NULL 会让「0 次」与「不知道」混在一起。
    meter_r: Mapped[int] = mapped_column(Integer, default=0)
    meter_rr: Mapped[int] = mapped_column(Integer, default=0)
    meter_llm: Mapped[int] = mapped_column(Integer, default=0)
    #: 一次检索可以查多个库，所以是列表而不是单值（旧表的 ``kb_id`` 单列已在 #101 删除）。
    kb_ids: Mapped[list] = mapped_column(JSONB, default=list)
    #: 「这次查的是哪次构建」——``{kb_id: version}``，与检索范围**同一个形状**。
    #: 可空：迁移之前写下的行没有版本可记（那时版本恒为 v1，没人需要在日志里记它）。
    index_versions: Mapped[dict | None] = mapped_column(JSONB, default=dict, nullable=True)
    #: 「这次命中的是哪几稿」——``{document_id: doc_version}``，与 ``index_versions``
    #: **同一个形状**（#179）。可空：迁移之前写下的行没有稿号可记（那时稿号恒为 1）。
    #: 值来自交付单元上那个真实列（``chunks.doc_version``，#174），不是另算一遍。
    doc_versions: Mapped[dict | None] = mapped_column(JSONB, default=dict, nullable=True)
    #: 只存 id：正文在切片表里有一份，分数明细属于评测与 trace。重复存只会多一份会过期的副本。
    chunk_ids: Mapped[list] = mapped_column(JSONB, default=list)
    top_k: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    #: 可空：没有降级 / 不是空结果时它就是 NULL。它不是常驻字段，调用方别拿它当枚举兜底。
    degraded_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    empty_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

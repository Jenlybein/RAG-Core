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

## 二十列全部可枚举

===============================  ===============================================
``id``                           行 id，**对外即 ``log_id``**；由中台用 UUID v4 生成
``tenant_id``                    谁调用的
``trace_id``                     ``log_id`` 的确定性派生（32 位十六进制），NOT NULL
``key_id``                       哪把 Key（第 14 步新增；老行为 NULL，归「历史汇总」）
``endpoint``                     哪条端点：``retrieve`` / ``rewrite`` / ``decompose`` / ``research``
``code``                         业务码：0 = 成功；``20010`` / ``20013`` / ``20014`` / ``5xxxx``
``meter_r``                      检索账这次计几次（decompose 是 N 次）
``meter_rr``                     重排账这次计几次
``meter_chunk``                  第四本账：这次消耗多少检索文本 token
``meter_llm``                    大模型账这次计几次（research 是 M 次）
``kb_ids``                       这次查了哪些库（一次检索可以多库）
``index_generations``            这次**每篇**落在第几代：``{document_id: 代数}`` 映射
``doc_versions``                 这次命中的是**第几稿**：``{document_id: doc_version}`` 映射
``chunk_ids``                    交付单元的代表片 + 溯源片 id，**只存 id**
``missing_generation_documents`` 本次范围内有多少篇**还没有产物**（``current_index_generation`` 为 NULL，被跳过）
``top_k``                        这次要了几条
``latency_ms``                   这次多慢
``degraded_reason``              降级原因，没有就是 NULL
``empty_reason``                 空结果原因，没有就是 NULL
``created_at``                   什么时候
===============================  ===============================================

## ``index_versions`` 为什么改成了 ``index_generations``

旧列是 ``{kb_id: version}``（ADR-0025 决策六），回答「这次查的是**哪个库的哪一版构建**」。
那个问题在新模型里**不存在了**：可见性判据已经从「库级索引版本」下放到「篇级当前代 ×
当前稿」（``chunks.generation = documents.current_index_generation`` + 稿号对齐，见
ADR-0025 第 2 次修订），库级版本轴正在被第二支迁移删掉。留着旧列会让这本账**自相矛盾**：
账上写着「这次查的是 v4」，而检索实际按每篇各自的当前代取数——两个说法各指一件事，事后
拿它归因必然归错。所以列名与含义一起换成了 ``{document_id: 代数}``：只记**交付过的篇**
各自落在第几代（与 ``doc_versions`` / ``chunk_ids`` 同一批交付物，三列严格同源）。

**代价写在这里，也是迁移里的取舍**：历史行「哪一次构建」的归因能力随这次迁移**消失**了
——旧值没有可映射的篇级对应物，迁移不做数据搬运，所以升级后的老行拿到 ``{}``。换来的是
「每篇在第几代交付」，那是新模型里唯一能回答这个问题的形状；承认旧能力退场，比留一列
没人能正确读懂的字段要好。

``doc_versions`` 是**又一次契约变更**（#179），形状与 ``index_generations`` **完全同形**
（两者都是「某类 id → 一个整数版本号」）：「文档 → 稿号」的配对关系只能从映射里读出来
（ADR-0010 §5：两个并列数组会拼出不存在的组合，而且不报错）。为什么要它（用户故事
第 3 条）：``chunk_ids`` 只答得出「命中过哪些片」，而片会被保留策略物理删掉（#178），
删掉之后那一串 id 就再也答不出「那次查的是第几稿」——稿号必须自己留在这一行上。

``missing_generation_documents`` 回答的是**另一个问题**：这次调用范围内有多少篇
``current_index_generation IS NULL``（= 还没有任何产物，检索时整篇被跳过）。它不是
「空结果原因」的替代品，而是它的**分母补充**：结果不满额时，调用方/运维要能一眼看出
「是库里真的没内容，还是有几篇还没产物所以凑不满」。所以它**只在结果不足时**才去数
（见 ``app/services/retrieval_trace.missing_generation_count``），满额时恒为 0 且**不发
查询**：满额说明缺代没有伤害这次调用，为每个请求多打一次库是没换来东西的固定成本。

缺代计数**不按 ``status`` 过滤**：``SUCCESS`` 但代数 NULL = 「建过、却没有产物」，
``QUEUED`` = 「还在排队」，``PROCESSING`` / ``FAILED`` 同理——它们都能解释「为什么少了几
篇」，而这一列记的是「被跳过的篇数」，不是状态分布。按状态过滤会把一部分原因从账上抹掉。

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

from sqlalchemy import DateTime, Index, Integer, String, text
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
    #: 第四本账（切块）：这一行消耗多少**检索文本 token**（0 = 这次没有索引消耗）。
    meter_chunk: Mapped[int] = mapped_column(Integer, default=0)
    meter_llm: Mapped[int] = mapped_column(Integer, default=0)
    #: 一次检索可以查多个库，所以是列表而不是单值（旧表的 ``kb_id`` 单列已在 #101 删除）。
    kb_ids: Mapped[list] = mapped_column(JSONB, default=list)
    #: 「这次每篇落在第几代」——``{document_id: 代数}``（ADR-0025 第 2 次修订第 5 条）。
    #: 只记**交付过的篇**，与 ``doc_versions`` / ``chunk_ids`` 同一批交付物（三列严格同源）。
    #: 形状是映射而不是两个并列数组（ADR-0010 §5）；文档 id 与整数代数是结构，不是原文。
    #:
    #: ``server_default`` 是空对象：旧列 ``index_versions`` 的值没有篇级对应物，迁移不做
    #: 数据搬运（见模块文档「``index_versions`` 为什么改成了 ``index_generations``」），
    #: 于是升级后的老行落在 ``{}``——「这一行没有
    #: 篇级代可记」，这正是它当时的真实处境。可空是给迁移之前的行留的（它们连库级版本都
    #: 没记），应用侧写入永远给 ``dict``。
    index_generations: Mapped[dict | None] = mapped_column(
        JSONB, default=dict, nullable=True, server_default=text("'{}'::jsonb")
    )
    #: 「这次命中的是哪几稿」——``{document_id: doc_version}``，与 ``index_generations``
    #: **同一个形状**（#179）。可空：迁移之前写下的行没有稿号可记（那时稿号恒为 1）。
    #: 值来自交付单元上那个真实列（``chunks.doc_version``，#174），不是另算一遍。
    doc_versions: Mapped[dict | None] = mapped_column(JSONB, default=dict, nullable=True)
    #: 只存 id：正文在切片表里有一份，分数明细属于评测与 trace。重复存只会多一份会过期的副本。
    chunk_ids: Mapped[list] = mapped_column(JSONB, default=list)
    #: 本次范围内「还没有任何产物」的篇数（``documents.current_index_generation IS NULL``）。
    #:
    #: 只在结果不满额时才算（推理与代价见 ``app/services/retrieval_trace.py`` 的同名辅助）：
    #: 满额时恒 0 且不发那条查询。默认 0 而不是 NULL：新行必须显式说出「这次数过没有」，
    #: 而 NULL 会让「0 篇缺代」与「没有数」混成一个读数；被拒 / 未走到召回的请求就是 0。
    missing_generation_documents: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    top_k: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    #: 可空：没有降级 / 不是空结果时它就是 NULL。它不是常驻字段，调用方别拿它当枚举兜底。
    degraded_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    empty_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

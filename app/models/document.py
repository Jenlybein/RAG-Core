"""表 2：documents —— 知识库中的一份原始资料。

status 用整数（见 app/utils/status.py）。source_* 与 error_message
是阶段 3 才用到的列，按最终形态一次给全，避免来回改表。
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now
from app.utils.status import DocumentStatus


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    kb_id: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text, default="")
    source_type: Mapped[str] = mapped_column(String(32), default="text")
    source_file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 默认「排队中」（4）：刚出现的文档行只可能是被收下、还没人接（ADR-0034 决策一）。
    # 三态时代这里默认 3，于是「没有租约的 3」成了新行的常态，把「在排队」与「在跑」
    # 混成了同一个字面值。真正写状态的是入队侧与 Worker（它们都显式给值），这只是兜底。
    status: Mapped[int] = mapped_column(Integer, default=DocumentStatus.QUEUED)
    # 当前稿指针（指向第几稿）。与 index_version（第几次重建）正交，见 ADR-0005。
    # 名字与「稿号」分开：切片的 ``chunks.doc_version`` 才是「这一片属于第几稿」，
    # 这里只说文档行现在指着哪一稿。自 #174 起它是**检索的范围条件**：向量 / 折叠 /
    # 计数 / 溯源四条 SQL 都用 :func:`app.models.chunk.current_draft_filter` 与
    # ``chunks.doc_version`` 做列比较，指针因此只有一个真值来源（这一行），
    # 应用层不缓存它——缓存里的指针过期只在切稿那一瞬间成立，而那种 bug 只表现为
    # 「多召回一份旧稿」。
    current_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # 这一篇现在生效的是第几代**索引产物**（第 17 步现场九）。
    #
    # 与上面的 ``current_version`` 是两根正交的轴：那个管「正文改过几次」，这个管「索引
    # 算过几次」。改造之前产物是库级的（``knowledge_bases.active_index_version``），于是
    # 起一次构建要取全库稿号最大值当「统一稿号」套到每一篇头上——库级判据引用了篇级数据，
    # 稿次落后那几篇必然取不到正文（20030）。下放到篇级之后两根轴各自闭环。
    # 允许为空：``NULL`` = 「这篇还没有任何一代产物」。与「第 1 代但恰好是空结果」分开——
    # 两者在检索侧的处理不同（前者跳过并计数，后者是一次正常的空结果），用一个值表示
    # 两件事，第三根轴还没上线就已经说不清了。
    current_index_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 这一代产物是用**哪份切块参数**算出来的（:func:`app.services.index_generation.settings_fingerprint`）。
    #
    # 允许为空，空 = 「来路不明」（例如那条生效版本没有参数快照）。空一律按落后处理：
    # 界面会提示更新，而不是悄悄认为它是最新的——「少提示一次」与「永远不再提示」不是一回事。
    settings_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 索引租约开始时间（Worker 抢占时写）。它区分「已入队但 Worker 还没开工」
    # （NULL）与「已经被抢占、正在跑」（有时间戳）；没有这一列，仅凭 status 与
    # 刚写过的 updated_at 无法区分两者，CAS 会永远抢不到或永远抢到，任务去重
    # 也就无从谈起。
    index_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 正文 token 数（#220 / ADR-0035 决策一）：单库容量判据的**唯一计量**。
    #:
    #: 口径是**正文**（与版本行正文同源），不是「发给嵌入模型的那份检索文本」——后者多出
    #: 标题、节标题路径与节说明前缀（差 10–20%），那是**每日切块额度**的口径。两个数字
    #: 各管一件事，混用会让「容量没满但额度超了」这种解释不清的场面出现。
    #:
    #: 0 = 还没算过（存量行由回填脚本补；迁移里只填了一个按字符数的**下界**）。
    #: 最近一次心跳（#225，迁移 ``c3d6e9f2b5a8``）：任务**跑着的时候**周期性刷新。
    #:
    #: 它与 ``index_claimed_at`` 分工：那是 fencing 令牌（永不变，谁持有谁就是主人），这是
    #: 「还活着吗」的证据（会变）。合成一列只有两个坏结果：清扫器抢走正在跑的长文档（白跑
    #: 一轮 embedding），或者长文档永远不被救（Worker 挂了就一直停在「索引中」）。
    index_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 清扫器救过这一篇几次（#225，迁移 ``a2b5c8d1e4f9``）。
    #:
    #: 为什么存在文档行而不是 Redis：它要表达的是「这篇文档已经试过 N 次」——那是文档的事实，
    #: 该跟着文档行一起活着、一起被删。放在 Redis 里会随重启/过期归零，于是那篇文档又开始
    #: 无限重投，而用户看到的是永远转圈。
    #:
    #: **成功索引时清零**（与状态同一笔事务）：不清零的话，一篇曾经被救回 3 次的好文档，
    #: 第 4 次万一再卡住就直接被判失败。
    redispatch_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

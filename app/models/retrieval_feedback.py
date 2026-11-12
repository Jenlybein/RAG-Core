"""表 5：retrieval_feedback —— 反馈（#105）。

**「调用方给这次检索打了几分」是业务数据，不是观测数据**（ADR-0021 决策 3）：它要统计、
要进评测、要给排序实验看，所以权威留在业务库，而不是观测栈。观测栈里那条 score 是
**旁路推送**——推失败只留 warning，这里的那一行照样成立。

这张表刻意与归属索引**分家**，不往 ``retrieval_traces`` 上加列：归属索引是**只写不改的
账本**（一次检索一行，写完就不再变），而反馈天生要被覆盖（同一 ``log_id`` 重打一次分，
后写覆盖，见 ``app/services/retrieval_feedback.py``）。把可更新的列塞进不改的账本里，
那张表「写下的就是历史」这句话立刻不成立。

====================  ==========================================================
``id``                行 id（UUID v4），反馈自己的身份
``log_id``            归属索引那一行的 id，**一行一个**（唯一索引）
``tenant_id``         谁打的（与归属索引校验过是同一个租户）
``score``             1~5；范围校验在服务层，越界回 ``20022``
``comment``           调用方自己写的一句话，可空
``created_at``        第一次打分的时间
``updated_at``        最后一次覆盖的时间
====================  ==========================================================

三条性质：

* **``log_id`` 不设外键**。归属索引是旁路账本，删行不该连带删掉反馈（审计要能在被引用的
  行消失之后仍然读得懂）。引用完整性由应用层校验：写入前必须确认那一行存在、``trace_id``
  对得上、且属于当前租户——三种失败对外的码与文案**完全一样**（``20021``），不泄漏
  「存在但不属于你」。
* **``comment`` 不是原文通道**。它是调用方自己写的一句话，我们既不填、也不从检索链路
  取任何文字塞进来；这张表里的每一个字都来自调用方的这次提交。
* **一 ``log_id`` 一行**：唯一索引把「后写覆盖」这件事钉在库里。并发首提交撞上唯一索引
  会失败成 ``20020``（本地写入失败），调用方重试即覆盖——而不是留下两条互相矛盾的行。
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now


class RetrievalFeedback(Base):
    __tablename__ = "retrieval_feedback"

    #: 反馈自己的行 id。与 ``log_id`` 分开：一个 ``log_id`` 上可能有多轮覆盖，行的身份不变。
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: 哪次检索——对外就是响应里那个 ``log_id``。唯一索引 = 「一次检索只有一份反馈」。
    log_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    #: 1~5。范围由服务层判定（越界要回 ``20022``，而不是参数校验失败的 ``20002``）。
    score: Mapped[int] = mapped_column(Integer)
    #: 可空：调用方只想打分不想解释时它就是 NULL，不是空串。
    comment: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    #: 覆盖时由仓储层显式写（见 ``retrieval_feedback_repository.upsert``）。
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

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
    status: Mapped[int] = mapped_column(Integer, default=DocumentStatus.PROCESSING)
    # 稿号（第几稿）。与 index_version（第几次重建）正交，见 ADR-0005。
    # 本步只随切片落库，**不参与过滤**：doc_versions 表与「默认只查当前稿」的
    # 约束留给第 10 步；在那之前按稿号过滤会拿到没有约束保护的结果。
    doc_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # 索引租约开始时间（Worker 抢占时写）。它区分「已入队但 Worker 还没开工」
    # （NULL）与「已经被抢占、正在跑」（有时间戳）；没有这一列，仅凭 status 与
    # 刚写过的 updated_at 无法区分两者，CAS 会永远抢不到或永远抢到，任务去重
    # 也就无从谈起。
    index_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

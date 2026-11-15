"""表 5：document_versions —— 「哪一稿」的真实存在（#172，ADR-0005 / D2 D3）。

一稿一行：正文、``sha256`` 校验和、状态（当前 / 归档）、创建时间。三条名字各说各的事：

* **当前稿指针** ``documents.current_version``：文档行现在指着第几稿（#171 改名）；
* **稿号本体** ``document_versions.version``：这一行是第几稿；
* **切片的稿号** ``chunks.doc_version``：这一片属于第几稿。

## 为什么 ``document_id`` 是裸字符串 id，没有 ``ForeignKey``

仓库里一张表都没有外键（``git grep ForeignKey -- app migrations`` 为空），引用关系一律
靠应用层校验。版本表跟着这条走，于是「文档删掉时版本行跟着走」**不能**指望库级
``ON DELETE CASCADE``：它由删文档那条路径显式删除
（:func:`app.repositories.document_version_repository.delete_for_document`），与清除切片
落在同一笔事务里。两套引用风格并存的代价是「哪张表走库、哪张表走代码」说不清，而
说不清的那一处迟早漏删——所以宁可把级联写进代码，也不开这口子。

## 为什么 ``version`` 没有默认值

稿号由写入侧算（``max(version) + 1``，靠行锁 + 唯一约束串行化，#173）。给一个默认值 1
会让「忘了传稿号」变成一次静默的 ``(document_id, version)`` 唯一约束冲突，错误现场离
出错点很远。列上没有默认值，这种遗漏当场就是 ``IntegrityError``。

## 状态的两个取值

``current`` / ``archived``（GLOSSARY「文档版本」）。**回到旧稿是把既有那一行的状态改回
``current``**（:func:`app.repositories.document_version_repository.mark_current`），
**不新增一行**：稿号是「这一稿存在过」的身份，回滚是「重新选中它」，不是「重新写一遍」。
**时间轴因此仍然单调**——稿号不变、只改一行的状态（指针在文档行上一起切回去），历史行
一条都不删，中间那几次变更也不会被抹掉（#176 的 rollback / D 系列用户故事第 10 条）。
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now
from app.utils.checksum import CHECKSUM_HEX_LENGTH

#: 状态：这一稿是文档的当前稿。
DOCUMENT_VERSION_CURRENT = "current"
#: 状态：这一稿已经被新稿取代（保留窗口内仍可回滚 / 只读直查）。
DOCUMENT_VERSION_ARCHIVED = "archived"


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "version",
            name="uq_document_versions_document_id_version",
        ),
        CheckConstraint(
            "status IN ('current', 'archived')",
            name="ck_document_versions_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 裸字符串 id + 索引（零外键，见模块文档）：文档删掉时由删文档路径显式删这些行。
    document_id: Mapped[str] = mapped_column(String(36), index=True)
    # 稿号本体：一稿一行，(document_id, version) 唯一。
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 这一稿的正文（重建 / 重解析 / 回滚一律从这里读，D6）。空正文是合法的
    # 没有切片的失败文档允许它为空。
    content: Mapped[str] = mapped_column(Text, default="")
    # 重解析用的源文件；JSON 上传的文档没有源文件，因此可空。
    source_file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 规范化后的正文的 sha256（唯一入口 app.utils.checksum）。判重、回滚与对账都读它。
    # 列长直接取那边的常量（``CHECKSUM_HEX_LENGTH``）：两个 64 各写一份的那一年，改口径时
    # 只会改到一边，而症状是「写入时太长被截断 / 迁移里宽度不一致」。
    checksum: Mapped[str] = mapped_column(String(CHECKSUM_HEX_LENGTH), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default=DOCUMENT_VERSION_CURRENT,
        server_default=DOCUMENT_VERSION_CURRENT,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

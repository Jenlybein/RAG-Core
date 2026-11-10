"""表 11：runtime_settings —— 运行时配置的**覆盖层**（``.env`` 是缺省，库是覆盖）。

为什么它是「一层」而不是「配置的搬家」：`.env` 已经是这套系统的缺省来源，且「配错就
启动即炸」那条纪律靠的就是它。把缺省整体搬进库会立刻丢掉两件事——离线 / CI 不能再靠
一份 .env 就起来，以及「这台机器装了什么」与「运营想要什么」混成一列，没人分得清哪个
值才是当前生效的。所以库里**只存被显式覆盖的那几行**：一行都没有时，行为与改造前逐字
一致（迁移里刻意不灌任何数据）。

**为什么 value 是 JSONB 而不是 String**：白名单里的值横跨 ``bool`` / ``int`` / ``float`` /
``str``（将来还可能有列表）。整列存 JSON 才不用在读写两处各写一遍「这一项该按什么类型
解析」，而「解析错类型」正是这类配置面板最典型的静默错误（``"false"`` 被当成 ``True``）。

**为什么 target 不在表里**：这张表只回答「哪个键被覆盖成了什么」。这个键属于哪一档、
能不能热更、合并到 ``Settings`` 还是只被业务代码读，全部写在
``app/services/runtime_settings.py`` 的白名单常量表里——那是代码里的决定，不是数据。
把它复制一列到库里，两张真相迟早分叉，而分叉的方向是「库里的 tier 说可热更、代码说
不可迁」这种最坏的一种。

**为什么更新人是一个裸字符串、且不加外键**：与仓库既有四张业务表同一套风格——id 是
裸字符串、引用完整性靠应用层校验。``updated_by`` 存的是账号 id 而不是用户名，因为
用户名可以被改名甚至被复用，而「谁改的」必须事后仍然指得回那一行。审计表里另有更完整
的前后快照，这里只留一个最基本的「最后碰过它的人」，两者用途不同：审计回答「历史」，
本表回答「当前这一行是谁写的」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"

    #: 键名就是 ``Settings`` 上的字段名（或 ``model.role.*`` 这类指针键），主键直接用它：
    #  「一个键最多一行」是这张表的全部约束，再有一个代理主键只会多一层没用的映射。
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    #: 标量值：bool / int / float / str / list。理由见模块文档。
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    #: 这一行自己写的是「什么时候被写成现在这个值」，不是「什么时候被读过」。
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    #: 账号 id（不是用户名）；系统脚本写入时由脚本自己填，不编一个假账号。
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)

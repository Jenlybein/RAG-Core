"""表 9：sessions —— 控制台的服务端会话（一张会话行 = 一次登录）。

**为什么是库里的一行，而不是一个签名的 token**：停用一个账号要**下一次请求就失效**。
签名 token 在签发那一刻就把角色与状态固化了，管理员点完「停用」之后旧 token 还能用满
它的有效期——止血动作要等到过期才生效，那就不叫止血。会话行放在库里，于是：

- 登出可以**两边都做**：删行让人拿不到身份，删 cookie 让浏览器不再发它。缺任何一半，
  都留下一个还能用的会话；
- 角色与状态**不写进会话行**，每次请求回账号表现读一遍——降级管理员、停用账号同样
  在下一个请求生效，而不必等过期。

``id`` 是随机的不可猜标识（``secrets.token_urlsafe``），**主键即凭证**；行里不存口令、
不存 token 副本、不存任何能推导出凭证的东西：多存一份就多一处泄露面。
``expires_at`` 是硬过期时间；不靠 cookie 的有效期来兜停用（cookie 在客户端，改不动）。

不带外键、不建关系——与仓库既有的四张业务表同一风格，引用完整性由应用层校验。
"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now


class ConsoleSession(Base):
    __tablename__ = "sessions"

    # 随机会话标识，主键即凭证。列宽 64 是给 token_urlsafe(32) 留的余量（实际 43 字符）。
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 会话属于哪个账号。索引是因为「这个账号还有哪些会话」是登出与排障的常用读。
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

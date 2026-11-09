"""表 7：accounts —— 控制台账号（租户管理员 / 平台管理员）。

与租户**分离**，而不是「租户里的一列」：

- 管理员跨租户管账号、套餐与审计，它不属于任何租户，所以 ``tenant_id`` 可空。若把
  管理员挂到某个「平台租户」上，那个假租户就意外拥有了一整套数据面身份；
- 一个租户可以有多个账号，将来还会有停用与改密，这些都只影响账号行。

``username`` 唯一：登录时靠它定位账号，唯一性放在库里比放在校验代码里可靠。
``password_hash`` 存哈希，列宽 255 是给 bcrypt/argon2 这类方案留的余量——明文密码
一旦入库，泄露就是全量泄露，没有任何补救手段。
"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 可空：平台管理员不属于任何租户。索引是因为「某租户的账号列表」是控制台的常用读。
    tenant_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="user")
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

"""表 9：api_keys —— 数据面唯一的凭证。

一个外部应用拿一把 Key 调 HTTP 与 MCP 两条入口，两条路认的都是这一行。``label`` 只是给人
看的备注名：面板与日志里要能分清「这是哪套集成」，但鉴权永远不读它。

**库里没有可用的 Key**：``key_hash`` 是 ``scrypt$<n>$<r>$<p>$<salt>$<hash>``（与
``accounts.password_hash`` 同一套规则，见 :mod:`app.core.passwords`），原文只在签发那一刻
存在过一次。于是「按原文一击命中」这件事不可能再做，行定位改由 Key 自己携带：
原文形如 ``rc_live_<id>.<32 位十六进制>``，``<id>`` 就是本表主键，鉴权拿它一次主键命中。
``key_hash`` 因此**不需要唯一索引**——它压根不参与查找。

``key_tail`` 是密钥本体末 5 位，唯一用途是让面板能把它显示成 ``rc_live_…4f3a2``：
哈希算不出头尾，要显示就必须额外存一截，而这一截（20 bit）连同「前缀 + 行身份证」也不足以
重建密钥。

配置不摊进这张表，而是 ``policy_id`` 指向 :class:`app.models.key_policy.KeyPolicy`：

- 一套配置能被同一个租户的多把 Key 复用（本期按一对一用），改档位就是改配置，
  改完这些 Key 立刻生效，不必重新签发；
- 删掉配置时，引用它的 Key 字符串级失效——「指向一个不存在的配置」这件事必须有
  一个明确的收场，而不是让 Key 带着半套默认值继续跑。
"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    policy_id: Mapped[str] = mapped_column(String(36), index=True)
    #: 密钥本体的 scrypt 哈希串（约 84 字符；255 是给更强的参数与将来的算法换血留的余量）。
    key_hash: Mapped[str] = mapped_column(String(255))
    #: 密钥本体的末 5 位，只服务面板展示。列宽留到 16：``API_KEY_MASK_TAIL`` 是可调参数，
    #: 调大时不该先来改表。
    key_tail: Mapped[str] = mapped_column(String(16))
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    # 只给运营看「这把 Key 最近有没有在用」；每次调用都写它会让热路径多一次写，
    # 所以它可以为 NULL、也允许滞后。
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

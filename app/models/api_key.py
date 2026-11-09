"""表 9：api_keys —— 数据面唯一的凭证。

一个外部应用拿一把 Key 调 HTTP 与 MCP 两条入口，两条路认的都是这一行。所以
``key_value`` 是**查找路径**（唯一索引，一次命中），``label`` 只是给人看的备注名：
面板与日志里要能分清「这是哪套集成」，但鉴权永远不读它。

配置不摊进这张表，而是 ``policy_id`` 指向 :class:`app.models.key_policy.KeyPolicy`：

- 一套配置能被同一个租户的多把 Key 复用（本期按一对一用），改档位就是改配置，
  改完这些 Key 立刻生效，不必重新签发；
- 删掉配置时，引用它的 Key 字符串级失效——「指向一个不存在的配置」这件事必须有
  一个明确的收场，而不是让 Key 带着半套默认值继续跑。

``key_value`` 本期**明文**（ADR-0004 的明确取舍：调试台要拿原文打真实接口）。这是一条
写进风险清单的债：任何能读库的人都能拿到可用 Key，生产前必须改成「哈希 + 密文」双列。
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
    # rc_live_ + 32 位十六进制（见 app.utils.id_generator.new_api_key_value）。
    # 长度 64 留了余量：将来改成「哈希 + 密文」时密文列会明显更长。
    key_value: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    # 只给运营看「这把 Key 最近有没有在用」；每次调用都写它会让热路径多一次写，
    # 所以它可以为 NULL、也允许滞后。
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

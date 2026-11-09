"""表 5：tenants —— 一个客户。

主键沿用现成的字符串标识（``tenant_demo`` 这类）：四张既有业务表上的 ``tenant_id``
列存的就是它，所以建这张表**一行数据都不用迁**。若为了「更规范」换成自增数字主键，
四张表都得跟着迁一遍，而收益只是省几个字节。

``plan_id`` 只存整数、不加外键（与既有风格一致），**且非空**：一个「套餐未知」的租户
意味着它的每一项上限都没有来源，这不是一种合法状态，宁可插入时失败。

## 租户拿到哪一档（三条口径，写在这一处）

- 新建租户 → :data:`DEFAULT_PLAN_CODE`（最低档）；
- 升级前就已存在的租户 → :data:`EXISTING_TENANT_PLAN_CODE`（标准档），它们此前一直是
  「全套能力都能用」，按最低档建档等于升级当天全线降级；
- 本地演示租户 → :data:`DEMO_TENANT_PLAN_CODE`（最高档），演示要看的是完整能力。

三条口径分散到脚本、迁移、服务里各写一遍，迟早出现「脚本开的租户和升级回来的租户
不是同一档」这种只在某条路径上才复现的怪事。
"""

from datetime import datetime
from typing import Final

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now
from app.models.plan import PLAN_FREE, PLAN_PRO, PLAN_STANDARD, check_plan_code

#: 新建租户的套餐：没说就是最低档。
DEFAULT_PLAN_CODE: Final[str] = PLAN_FREE
#: 升级前就已存在的租户：按标准档建档，不按最低档。
EXISTING_TENANT_PLAN_CODE: Final[str] = PLAN_STANDARD
#: 本地演示租户：按最高档，演示要看到完整能力。
DEMO_TENANT_PLAN_CODE: Final[str] = PLAN_PRO


def resolve_plan_code(plan_code: str | None) -> str:
    """新建租户时把「没指定」补成最低档，并拒绝不认识的档位名。

    未知档位名直接抛错而不是回落默认值：把 ``pro`` 拼错成 ``Pro`` 却静默建出一个免费
    租户，客户会先掉额度再投诉，而日志里一切正常。
    """

    if plan_code is None:
        return DEFAULT_PLAN_CODE
    return check_plan_code(plan_code)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    plan_id: Mapped[int] = mapped_column(Integer)
    # 停用租户要让它的所有调用立刻失败（出事时先止血），所以状态是租户级的，
    # 不靠删 Key 来表达——删 Key 得一把把删，还会丢审计线索。
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

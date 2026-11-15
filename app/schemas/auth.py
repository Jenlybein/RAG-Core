"""数据面「我是谁」的响应模型（``GET /api/v1/auth/me``，票 #72 收尾）。

与 ``/console/me`` **返回同一份套餐与用量**，但两个接口不合并：数据面只认 API Key，
控制面只认会话 cookie（spec #72「两个 /me 分工」）。形状因此复用
``app/schemas/console.py`` 里的 :class:`PlanSnapshotData` 与 :class:`UsageData`——
复制出第二份形状，迟早与第一份漂移，而这两栏正是调用方拿来判断「还能不能继续跑」的。

``tenant`` 只有 id 与显示名：两者都来自判定内核已经解出的 :class:`app.core.auth.KeyContext`。
不为了多显示一个状态再查一次库，也就不会把任何别家的行带进响应。
"""

from __future__ import annotations

from pydantic import BaseModel

from app.schemas.console import PlanSnapshotData, UsageData


class AuthTenantData(BaseModel):
    """调用方所属的租户身份：判定结论里已有的那两项。"""

    tenant_id: str
    name: str


class AuthMeData(BaseModel):
    """数据面 ``/me`` 的四栏：租户身份、套餐快照、今日检索用量、今日 Research 用量。

    ``usage`` **不可缺省**：数据面调用方一定属于某个租户（Key 挂在租户上），所以它
    总要得到一份用量——开关关着时 ``used`` 为 ``None`` + ``source="not_tracked"``，
    那是一个明确的状态，不是缺值。

    ``research_usage``（票 #150）与 ``usage`` **同形**，读的是 Research 那一本账：两条
    链路的额度互相独立，合成一个数字就答不了「我还能跑几次 Research」。
    """

    tenant: AuthTenantData
    plan: PlanSnapshotData
    usage: UsageData
    research_usage: UsageData

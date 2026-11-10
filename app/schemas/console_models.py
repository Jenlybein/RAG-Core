"""多模型配置（``/console/model-configs``）的请求与响应模型（票 #94）。

两处刻意的写法：

- **列表里的 ``api_key`` 永远是 masked**（``sk-…abcd``）。明文只有 ``GET /{id}/secret``
  一条出口（响应模型 :class:`ModelConfigSecretData`），所以「列表」与「显示原文」在类型
  上是两个不同的东西，而不是同一个字段靠参数开关——后者一次「顺手加个字段」就泄露了；
- **``roles`` 在列表响应里**：面板（#96）要显示「fast / strong 指向哪一条」。让页面再
  去读一次 ``GET /console/settings`` 拼这个关系，等于让前端复述一遍「指针值 = 配置 id」
  这条知识；服务端已经算过一遍（:func:`app.services.model_config_service.list_configs`）。

请求体里的空值语义与密钥配置那一票不同：这里的 ``base_url`` / ``api_key`` **空串是
合法状态**（用默认地址 / 还没填 Key），所以不能用 ``exclude_none`` 把空串一起吃掉——
PATCH 只按「这个字段有没有出现」判，见 :meth:`ModelConfigPatchRequest.provided`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelConfigData(BaseModel):
    """列表里的一行（也是新增 / 改配置后的那一行）。"""

    id: str
    #: 给人看的备注名（面板与审计用它分清「生产还是备用」），不参与任何判定。
    label: str
    #: 六个预设供应商之一。写入时由服务层对着 ``LLM_VENDORS`` 校验，这里只是透传。
    provider: str
    base_url: str
    model: str
    #: **永远 masked**。明文只从 ``/{id}/secret`` 出一次。
    api_key: str
    #: 现在指向这条配置的角色（``fast`` / ``strong``）；没有角色指向时为空列表。
    roles: list[str]
    created_at: datetime
    updated_at: datetime


class ModelConfigListData(BaseModel):
    items: list[ModelConfigData]


class ModelConfigCreateRequest(BaseModel):
    """新增一条接入：``label`` / ``provider`` / ``model`` 必填，其余有默认值。

    这里**不写** ``min_length``：空串在服务层才被判成 ``20015``（参数非法），而不是先被
    pydantic 判成 ``20002``。两处都判会让面板拿到两种码，而「空着没填」是最常见的那种
    人填错，它应该落在一个统一的码上。
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    provider: str
    base_url: str = ""
    model: str
    api_key: str = ""


class ModelConfigPatchRequest(BaseModel):
    """部分更新：**没发的字段不动**（``null`` 也按「没发」处理）。

    ``api_key`` 允许显式发空串（清掉这条接入的 Key），所以不能用 ``exclude_none`` 之外
    的「全部字段可选」写法把空串也丢掉——见 ``provided()``。
    """

    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None

    def provided(self) -> dict[str, Any]:
        """这次真的要写的字段：没发的、以及发了 ``null`` 的都不算，**空串算**。"""

        return self.model_dump(exclude_unset=True, exclude_none=True)


class ModelConfigPatchData(BaseModel):
    id: str
    #: 改前 / 改后：整行快照（``api_key`` 已打码）。
    before: dict[str, Any]
    after: dict[str, Any]
    #: 这次**真的**变了的字段，按字段名排序；空列表表示给出的值都与原值相同。
    changed: list[str]


class ModelConfigDeleteData(BaseModel):
    id: str
    deleted: bool


class ModelConfigSecretData(BaseModel):
    """一次明文：只有这一条响应模型带未打码的 ``api_key``，且每次调用都留一条审计。"""

    id: str
    api_key: str = Field(description="明文 Key；只在本响应里出现一次，不回写任何表")

"""档位预设的请求 / 响应模型。

``extra="forbid"`` 与其它控制面 schema 同一条约定：多写一个字段要撞 ``20002``，而不是
被静默忽略——「界面上改得动、后端读不到」比「界面上没有这个字段」更糟。

``PATCH`` 全部字段可缺省（``None`` = 这次不动它），与 ``KeyPolicyPatchRequest`` 同一种
形状：只改一个条数不该被迫把另外三个也发一遍，那样的请求体一旦有人漏字段，行为就从
「改一个」变成「清空其它」。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProfilePresetData(BaseModel):
    """一行档位参数。"""

    model_config = ConfigDict(extra="forbid")

    profile: str = Field(description="档位名")
    mode: str = Field(description="检索方式：vector / hybrid")
    top_k: int = Field(description="返回条数")
    rerank: bool = Field(description="该档位是否想要重排（生效值还要与部署开关取合取）")
    rewrite: bool = Field(description="该档位是否想要查询改写（同上）")
    orchestration: bool = Field(
        description="该档位是否想要切片编排（生效值还要与本套集成的启用开关取合取）"
    )


class ProfilePresetListData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ProfilePresetData]


class ProfilePresetPatchRequest(BaseModel):
    """部分更新。五个字段都可缺省；``None`` 表示「这一项不改」。"""

    model_config = ConfigDict(extra="forbid")

    mode: str | None = None
    top_k: int | None = None
    rerank: bool | None = None
    rewrite: bool | None = None
    orchestration: bool | None = None


class ProfilePresetPatchData(BaseModel):
    """回显这次改动的前后值与变化字段。

    「已保存」三个字说明不了改了什么；面板要把 ``changed`` 说出来，用户才能确认
    自己动的是预期的那一项。
    """

    model_config = ConfigDict(extra="forbid")

    before: dict[str, Any]
    after: dict[str, Any]
    changed: list[str]

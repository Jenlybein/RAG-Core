"""配置中台（``/console/settings``）的请求与响应模型。

两处刻意的写法：

- **``value`` 是 ``Any``**：白名单里的值横跨 ``bool`` / ``int`` / ``float`` / ``str``，
  响应模型不该把「这一项是布尔」再写一遍——那是白名单（``SETTING_SPECS``）的职责，
  两处写两遍就会有一个先漂移；
- **``dry_run`` 不在请求体里**，它是查询参数（默认 ``true``）：忘记传时最坏结果是
  「只试算、没保存」，与改套餐同一条纪律。

``source`` / ``invalid`` 是给页面（#96）看的两个**事实**：前者区分「这一行来自 .env
还是库里的覆盖」，后者是「库里那一行被读时校验丢弃了、现在回退到缺省值」。它们不是
内部字段的转义，而是本票验收里「面板标红」的唯一依据。
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.console import ImpactGroupData

#: 值的来源：`.env` 是缺省、库是覆盖。用 ``Literal`` 让 OpenAPI 里就是一个枚举下拉。
SettingSource = Literal["env", "db"]

#: 三档的名字在服务层只写一次（:mod:`app.services.runtime_settings`），这一层只做
#: 响应形状的透传；这里不重复声明 ``tier`` 的取值范围，否则加一档要改两处。
SOURCE_ENV: Final[SettingSource] = "env"
SOURCE_DB: Final[SettingSource] = "db"


class SettingRowData(BaseModel):
    """面板上的一行。``editable`` 与 ``tier`` 同时返回是刻意的冗余：页面要按 ``tier``
    分组、按 ``editable`` 禁用输入框，多一个派生字段就少一次前端判断。
    """

    key: str
    tier: str
    editable: bool
    #: 有效值：有可采纳的覆盖就是库里的值，否则是 ``.env`` 的缺省值。
    value: Any
    source: SettingSource
    #: 库里那一行读时校验失败（已回退成缺省值）。页面据此标红。
    invalid: bool
    #: 「为什么在这一档」的人话，直接展示（#96 不再自己编理由）。
    why: str
    #: 标红的行这里会有一句人话原因；正常行是 ``null``。
    invalid_reason: str | None = None


class SettingTierGroupData(BaseModel):
    """一档的全部键。``title`` 是给页面用的中文小标题（服务端已经写好，别在前端再编）。"""

    tier: str
    title: str
    editable: bool
    settings: list[SettingRowData]


class SettingsListData(BaseModel):
    #: 按「只读 → 需重启 → 热生效」固定顺序分三组。
    groups: list[SettingTierGroupData]
    #: 缓存有效期（秒）。页面必须用它而不是写死 30：这是「各进程 30s 内生效」的依据。
    cache_ttl_seconds: int


class SettingsPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 要改的键 → 值。**只允许白名单内的键**，且档位为 LOCKED 的键一律 ``20015``；
    #: 任意一个键不合格时整批不写（见 ``runtime_settings.patch_runtime_settings``）。
    values: dict[str, Any] = Field(min_length=1)


class SettingsImpactData(BaseModel):
    """一次能力开关改动的两个影响面（与套餐影响预览同一套 shape）。"""

    quality_degraded: ImpactGroupData
    keyword_channel_lost: ImpactGroupData


class SettingsPatchData(BaseModel):
    dry_run: bool
    #: 真正写进库的键（``dry_run=true`` 时为空）。
    updated: list[str]
    #: 这次改动里热生效 / 需重启的键。提示语文案由服务端给，页面不用自己拼。
    hot: list[str]
    restart: list[str]
    #: 改前/改后：键 → 覆盖层里的值（``null`` 表示此前没有覆盖行）。密钥类字段打码。
    before: dict[str, Any]
    after: dict[str, Any]
    #: 只有 ``dry_run=true`` 才有影响面；真写时为 ``null``（与改套餐同一条形状）。
    impact: SettingsImpactData | None
    note: str

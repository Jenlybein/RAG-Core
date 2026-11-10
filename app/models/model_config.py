"""表 12：model_configs —— 一行 = 一套可用的模型接入（票 #94）。

**为什么它是表而不是一组环境变量**：``.env`` 里的 ``LLM_PROVIDER`` / ``LLM_MODEL`` 只能
表达「这台机器上恰好配了一家」。调试台要让管理员**增删多条**接入（生产一家、备用一家、
本地一家），再由两个**模型角色**各指向其中一条——「有哪几条可用」是可增删的数据，
「现在用哪一条」才是指向（存在 ``runtime_settings`` 的 ``model.role.fast`` /
``model.role.strong`` 里，见 #93 的模块文档与 GLOSSARY「模型配置」）。

**角色指向为什么不做成这张表的列**：指向是「这台机器当前选哪一个」，不是「这条配置的
属性」；放在配置表里，换指向就变成一次需要唯一约束的列更新，还会绕过配置中台那条
「写库 + 审计 + 删缓存」的统一路径。放在 ``runtime_settings`` 就只是一次普通配置写入。

**``api_key`` 明文入库**：与 ``api_keys.key_value`` 同一取舍（ADR-0004）——调试台要拿
原文打真实接口。因此**任何列表 / 审计 / 日志都只出现 masked 形态**，明文只从
``GET /console/model-configs/{id}/secret`` 出一次（且每次调用都留审计）。

**不加外键**：与既有业务表同一套风格（裸字符串 id、引用完整性靠应用层校验）。角色指向
指向的是一条配置，但「被引用就不许删」这条判定必须在应用层做——它要先把两个指针键读
出来比对，外键表达不了「两个键里的任意一个」。

**重排没有独立的模型配置**：一期 ``RerankProvider`` 包着 ``LLMProvider``（ADR-0002），
它跟随 ``fast`` 角色。这张表里**不要**再加一个 ``rerank`` 用途位或第三个指针键——加了
也没有调用点会读它，而面板上会多出一个「改了也不生效」的配置项。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now


class ModelConfig(Base):
    __tablename__ = "model_configs"

    # 裸字符串主键：与仓库既有表一致（tenant_id / kb_id 都是裸字符串）。
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 给人看的备注名（面板与审计用它分清「这是生产还是备用」），不参与任何判定。
    label: Mapped[str] = mapped_column(String(128))
    # 六个预设供应商之一（``app.core.llm_vendors.LLM_VENDORS``）。装配知识在 Provider
    # 工厂里，这里只存名字；写入时由服务层校验它属于注册表，不是自由字符串。
    provider: Mapped[str] = mapped_column(String(32))
    # 自定义端点；空串表示用该供应商的默认地址（千问例外：必须自己填，见工厂的注释）。
    base_url: Mapped[str] = mapped_column(String(255), default="")
    model: Mapped[str] = mapped_column(String(128))
    # 明文凭证（ADR-0004 同一取舍）；masked 形态由应用层派生，不落库。
    api_key: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


@dataclass(frozen=True)
class ModelConfigSpec:
    """一条模型接入的**只读快照**：装配 Provider 只用这六个字段。

    为什么不直接把 ORM 行交出去：行绑在会话生命周期上，而装配发生在服务层读完它、
    会话已经关掉之后；快照让「会话关了还能不能用」这个问题不存在。``created_at`` /
    ``updated_at`` 不进快照：它们是面板与审计的信息，不是装配的输入。

    字段**一一对应、没有默认值**：映射漏一列就是 ``TypeError``，而不是悄悄装配出一家
    没填地址（或没填 key）的 Provider。
    """

    id: str
    label: str
    provider: str
    base_url: str
    model: str
    api_key: str


def spec_of(row: ModelConfig) -> ModelConfigSpec:
    """行 → 快照；逐字段搬运，不做任何兜底（理由见快照的文档）。"""

    return ModelConfigSpec(
        id=row.id,
        label=row.label,
        provider=row.provider,
        base_url=row.base_url or "",
        model=row.model,
        api_key=row.api_key or "",
    )

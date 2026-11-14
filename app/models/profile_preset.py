"""表 10：profile_presets —— 四个质量档位的参数（档位定义第一次落库）。

**为什么这张表存在**：档位原先硬编码在 ``app/services/retrieval_options.py`` 的
``PROFILE_CHAINS`` 里，意味着「quality 到底开不开重排」这种产品参数要改代码发版。
档位是产品契约，不是实现细节——它决定了调用方拿到什么质量的结果，运营调它必须能被
预览、审计、留痕。

**四行，一档一行**，``profile`` 做主键（裸字符串，不加外键，与其它业务表同风格）：

* ``speed`` / ``balanced`` / ``quality``：命名档位的**完整链路**（模式 / 条数 / 重排 /
  改写），选它就等于选了这一整条；
* ``custom``：这一行的语义与前三行**不同**——它是「新建 Key 时 ``custom_options``
  的初值」，不是检索时的回落。检索时 ``custom`` 仍然逐键回落部署配置
  （``RETRIEVAL_MODE`` / ``TOP_K`` / ``RERANK_ENABLED`` / ``QUERY_REWRITE_ENABLED``），
  那条口径没变：custom 的意思就是「按我填的，没填的跟这台机器走」。若把这一行也当成
  检索回落，改档位预设就会悄悄改变 custom 的行为，而用户从界面上看不出来。

因此本表**只覆盖三个命名档位**，``custom`` 行只被编辑器读。这一点必须留在表上，
不能只写在文档里——见 :data:`NAMED_PROFILES`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, now
from app.models.key_policy import PROFILE_BALANCED, PROFILE_QUALITY, PROFILE_SPEED

#: 会被检索链路读取的档位（命名档位）。`custom` 不在其中：见模块文档。
NAMED_PROFILES: Final[tuple[str, ...]] = (PROFILE_SPEED, PROFILE_BALANCED, PROFILE_QUALITY)


class ProfilePreset(Base):
    __tablename__ = "profile_presets"

    #: 档位名。主键就是它——四行固定，不存在「加一档」的运行时操作（加档位要动
    #: 套餐白名单、前端卡片与门禁校验，不是插一行能了的事）。
    profile: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: 检索方式：`vector` / `hybrid`（词表来自 `app.core.config.SUPPORTED_RETRIEVAL_MODES`）。
    mode: Mapped[str] = mapped_column(String(16))
    #: 返回条数。档位之间差的是**链路**（路数、重排、改写），不是返回多少条；但既然
    #: 要可配，条数就一并暴露——否则运营想「quality 多给两条」还得改代码。
    top_k: Mapped[int] = mapped_column(Integer)
    #: 这两个是**意图**（这个档位想要什么），不是「这台机器上会不会跑」：
    #: 生效值仍是它与部署开关的合取（见 `app.services.profile_policy`）。
    rerank: Mapped[bool] = mapped_column(Boolean)
    rewrite: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

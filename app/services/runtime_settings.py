"""运行时配置中台：三档白名单、热更落实、非法行回退与影响预览（票 #93）。

这个模块是「改配置」这件事的**唯一**判定处：白名单、档位、逐键校验、合并、就地写单例、
影响预览、写库与审计的顺序全在这里。路由只取参数与装壳（``app/api/routes/console_settings.py``），
管理员页面（#96）只按 ``tier`` / ``editable`` / ``source`` / ``invalid`` 四个字段渲染。

## 为什么需要「覆盖层」而不是直接改 ``settings``

``Settings`` 没开 ``validate_assignment``：写 ``settings.RERANK_TIMEOUT_SECONDS = -1`` 会
**静默成功并绕过所有 validator**，等于把「配错就启动即炸」那条纪律悄悄拆掉。所以运行期
永远不许直接赋值，必须经过这里：先用**同一批 validator** 验一遍，验过了才就地写单例。

## 平台级配置：只由管理员改，因为一次修改横跨所有租户

``runtime_settings`` 这几行**不属于任何租户**。``RERANK_ENABLED`` 一关，所有租户的
``quality`` 档位一起从「重排」退化为「降级」；``LLM_MODEL`` 一改，所有租户的下一次调用
都换模型。因此写入口只挂在管理员依赖上（``require_console_admin_write``），**不挂**
「有租户身份即可写」那种门禁——那等于给每个租户一把改别人服务质量的钥匙，是跨租户
越权，而不是权限粒度问题。管理员 ``tenant_id is None`` 是这份配置唯一合理的归属。

## 合并四步（顺序是判定的一部分）

1. 读缓存（TTL 30 秒），未命中才读库；
2. 逐键校验：每个键单独过一遍 ``Settings.model_validate`` + 白名单声明的类型/下界。
   失败的那**一行**丢弃（不是整批丢弃），记 warning，并让 ``GET`` 把那行标 ``invalid``;
3. 合并出最终 dict 再整体 ``Settings.model_validate`` 一次：不通过说明是键间冲突，
   整体放弃并保留上一份好值；
4. **就地** ``setattr`` 到 :data:`app.core.config.settings` 单例——不许替换模块属性：
   全仓一百多处 ``settings.X`` 都是 import 期拿到的对象引用，换掉属性它们看不到。

失败时保留上一份好值并告警，**绝不抛到请求路径上**：一个正在服务的进程不该因为库里
有一行脏数据就整段拒绝工作（那是「配错就启动即炸」在启动期的事，不是运行期的事）。

## 三档（判据是「改了之后哪里的数据会失效」，不是字段重不重要）

- ``LOCKED``：不可迁移，只读展示。含**全部安全开关**（能关掉鉴权的面板本身就是漏洞）；
- ``RESTART``：可迁但需重启 + 重建。写库 + 审计，**不参与热更**——这一档的值在**本进程
  第一次合并**时读入，之后改库要等下一次重启才生效；
- ``HOT``：下一轮刷新就生效（各进程 30s 内收敛）。

## 为什么 ``RESTART`` 不热更

``EMBEDDING_DIMENSIONS`` / ``CHUNK_SIZE`` 这类值一旦换了，**已落库的数据**是按旧值算出来
的：热更会让新请求按新维度去查旧维度的向量，或者让新切片与旧切片按两套窗口拼在一起。
所以这一档的写入是「把意图记下来」：面板如实显示「待重启」，进程重启后由第一次合并读入。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Final, Literal, Mapping

import structlog
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.core.exceptions import AppError, ErrorCode
from app.core.model_roles import MODEL_FAST, MODEL_STRONG, role_pointer_key
from app.core.runtime_settings_cache import (
    CACHE_TTL_SECONDS,
    RuntimeSettingsCache,
    get_settings_cache,
)
from app.db.base import now
from app.db.transaction import after_commit
from app.db.session_factory import session_factory, set_session_factory as _set_session_factory
from app.models.key_policy import KeyPolicy, policy_snapshot_of
from app.models.plan import Plan
from app.repositories import (
    key_policy_repository,
    plan_repository,
    runtime_setting_repository,
    tenant_repository,
)
from app.services.plan_admin import IMPACT_SAMPLE_LIMIT, ImpactGroup
from app.services.console_service import append_audit
from app.services import model_config_service
from app.services.retrieval_options import MODE_HYBRID, profile_chain_of
from app.utils.id_generator import MASK_HIDDEN, mask_secret

logger = structlog.get_logger(__name__)

# ───────────────────────────────────────────────────────────────────────────────
# 三档与白名单
# ───────────────────────────────────────────────────────────────────────────────

TIER_LOCKED: Final[str] = "locked"
TIER_RESTART: Final[str] = "restart"
TIER_HOT: Final[str] = "hot"

#: 面板分组的顺序：从「完全只读」到「可以热改」。顺序固定，页面因此可逐字节断言。
TIERS: Final[tuple[str, ...]] = (TIER_LOCKED, TIER_RESTART, TIER_HOT)

#: 每一档给管理员看的一句话。**说清楚「改完会发生什么」而不是档位名本身**，
#: 否则面板上出现的就是三个没人看得懂的英文单词。
TIER_TITLES: Final[dict[str, str]] = {
    TIER_LOCKED: "不可迁移 · 只读展示",
    TIER_RESTART: "可迁但需重启 + 重建",
    TIER_HOT: "可迁移 · 热生效",
}

#: 一个键的取值目标：``settings`` 参与 ``Settings`` 合并，``pointer`` 只被业务代码读
#: （多模型配置的角色指向，#94）。分成两种目标是因为指针键在 ``Settings`` 上根本没有
#: 对应字段，拿它去合并只会多一条「没有这个字段」的告警。
TARGET_SETTINGS: Final[str] = "settings"
TARGET_POINTER: Final[str] = "pointer"

#: 值来自哪里：`.env` 是缺省，库是覆盖。它是界面上看得见的一条规则。
#: 用 ``Literal`` 而不是裸 ``str``：响应模型（``app/schemas/console_settings.py``）里这
#: 一项是枚举下拉，服务层的视图类型必须是同一个窄类型，否则 mypy 会在路由那一层报
#: 「str 赋给 Literal」——那正是 #93 合并后留下的一条真实类型错。
SettingSource = Literal["env", "db"]
SOURCE_ENV: Final[SettingSource] = "env"
SOURCE_DB: Final[SettingSource] = "db"


@dataclass(frozen=True)
class SettingSpec:
    """白名单里的一项：它属于哪一档、合并到哪里、为什么在这一档。

    ``minimum`` 是**白名单自己的**数值下界（含）。为什么不指望 ``Settings`` 的 validator：
    那些 validator 只守两个枚举词（``RETRIEVAL_MODE`` / ``LLM_PROVIDER``），像
    ``RERANK_TIMEOUT_SECONDS=-1`` 这种值在 pydantic 眼里完全合法——但一个负的超时会让
    重排每次都「超时失败」，也就是静默地把这项能力关掉。所以下界必须在白名单里声明。

    ``secret`` 标记「值不能原样出现在面板与审计里」。本期只有 ``MODEL_API_KEY`` 一项
    （供应商 Key 的主体在 #94 的 ``model_configs`` 里）。
    """

    tier: str
    target: str
    why: str
    minimum: float | None = None
    secret: bool = False


def _spec(
    tier: str,
    target: str,
    why: str,
    *,
    minimum: float | None = None,
    secret: bool = False,
) -> SettingSpec:
    """白名单条目的简写构造器：让上面那张表读起来是「一行一个键」，不是一屏 dataclass。"""

    return SettingSpec(tier=tier, target=target, why=why, minimum=minimum, secret=secret)


#: 完整白名单，照 #82「配置中台」那张三档表。**这是唯一的写入边界**：不在这里的键，
#: 无论 PATCH 还是直接写库，都不会被采纳（前者 20015，后者被忽略并告警）。
SETTING_SPECS: Final[dict[str, SettingSpec]] = {
    # ── 第一档：不可迁，只读展示 ──
    "APP_ENV": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "「是不是生产」决定 cookie 要不要加 Secure 等启动期行为，改了要按新环境重起",
    ),
    "DATABASE_URL": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "库地址在进程启动时就被引擎绑死；换库等于换整个数据落点，不是一次配置写入",
    ),
    "CELERY_BROKER_URL": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "投递端与消费端各自在启动时构造连接，运行期改只会让两边同时看不到对方",
    ),
    "CELERY_RESULT_BACKEND": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "任务结果后端与 broker 同一类：连接在启动时建立，改完必须重起两端",
    ),
    "REDIS_URL": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "限流、套餐缓存与配额共用它；运行期换库会让同一份计数瞬间分叉",
    ),
    "VECTOR_STORE": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "向量库类型决定索引结构与迁移，不是运行期可以切换的实现",
    ),
    "DOCUMENT_STORAGE_PATH": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "已落盘的原始文件在这个绝对路径下；改了之后旧文件就地找不到",
    ),
    "OCR_MODE": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "解析链路在启动时按它装配，改完要让 Worker 重起才会走新分支",
    ),
    "OCR_OFFLINE": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "离线模式决定要不要下载模型资产，属于部署事实，改完要重起",
    ),
    "OCR_MODEL_DIRECTORY": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "模型目录在启动时被 OCR 运行时扫描并缓存，运行期改路径不会被重新发现",
    ),
    "PDFIUM_LIB_PATH": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "动态库在进程启动时加载；运行期改路径只会在下一次解析时炸",
    ),
    "ORT_DYLIB_PATH": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "同上：ONNX Runtime 的动态库也是在启动时加载的",
    ),
    "QWEN_TOKENIZER_PATH": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "切块口径依赖的词表，启动时校验；换词表等于换一套切块结果，旧切片全部作废",
    ),
    "LOG_LEVEL": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "日志在启动时按它装配处理器；运行期改只是「面板上说改了」，进程里什么都不会变",
    ),
    "LOG_DIR": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "同上：滚动文件的目录在启动时就被日志器打开",
    ),
    "ENABLE_MCP": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "MCP 子应用在 import 期挂载；运行期改开关不会卸载已经挂上的 /mcp/",
    ),
    # 安全开关：必须在白名单里（面板要能展示并写明理由），但写入一律 20015。
    "AUTH_ENABLED": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "安全开关：一个能关掉鉴权的面板本身就是漏洞，它只能由部署方改 .env",
    ),
    "RATE_LIMIT_ENABLED": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "安全开关：关掉它等于给所有租户无限额度，面板不该有这个按钮",
    ),
    "DEV_API_KEY": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "安全开关：开发身份的固定口令，它只该存在于 .env，不该被面板改",
    ),
    "DEV_TENANT_ID": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "安全开关：开发身份落进哪个租户；与 DEV_API_KEY 是一对，同样只由部署方定",
    ),
    # ── 第二档：可迁但需重启 + 重建 ──
    "EMBEDDING_MODEL": _spec(
        TIER_RESTART,
        TARGET_SETTINGS,
        "换嵌入模型等于换向量语义：旧切片必须全量重建，所以它只在重启后读入",
    ),
    "EMBEDDING_DIMENSIONS": _spec(
        TIER_RESTART,
        TARGET_SETTINGS,
        "chunks.embedding 的维度写死在迁移里（VECTOR(1536)）；改它要一起改表并重建",
        minimum=1,
    ),
    "EMBEDDING_PROVIDER": _spec(
        TIER_RESTART,
        TARGET_SETTINGS,
        "嵌入实现在启动时装配；换实现要重起，否则旧实现仍在跑",
    ),
    "EMBEDDING_BATCH_SIZE": _spec(
        TIER_RESTART,
        TARGET_SETTINGS,
        "批次大小在装配嵌入实现时决定；改它不影响已写入的数据，但要重启才用新值",
        minimum=1,
    ),
    "MODEL_BASE_URL": _spec(
        TIER_RESTART,
        TARGET_SETTINGS,
        "单供应商接入地址在装配时读；多模型配置之后由 #94 的 model_configs 承接",
    ),
    "MODEL_API_KEY": _spec(
        TIER_RESTART,
        TARGET_SETTINGS,
        "模型 Key 在装配时读；重启前改了不会被任何 Provider 看到（面板与审计里打码）",
        secret=True,
    ),
    "KEYWORD_SEARCH_PROVIDER": _spec(
        TIER_RESTART,
        TARGET_SETTINGS,
        "关键词通道在启动时装配；打开或关闭都要让进程重新建立引擎连接",
    ),
    "ELASTICSEARCH_URL": _spec(
        TIER_RESTART,
        TARGET_SETTINGS,
        "ES 连接在装配时建立，换地址要重起（运行期改只会留下一条连不上的旧客户端）",
    ),
    "ELASTICSEARCH_INDEX": _spec(
        TIER_RESTART,
        TARGET_SETTINGS,
        "索引名决定查哪份数据；换名与重建索引是同一件事，不能只改名字",
    ),
    "CHUNK_SIZE": _spec(
        TIER_RESTART,
        TARGET_SETTINGS,
        "切块窗口变了，旧切片是按旧窗口切成；不重建就会两套窗口混在一个库里",
        minimum=1,
    ),
    "CHUNK_OVERLAP": _spec(
        TIER_RESTART,
        TARGET_SETTINGS,
        "与 CHUNK_SIZE 同一条：重叠量属于已落库切片的一部分，改了要重建",
        minimum=0,
    ),
    "TABLE_MAX_ROWS_PER_CHUNK": _spec(
        TIER_RESTART,
        TARGET_SETTINGS,
        "表格切片上限进了切片内容，改了之后旧表格切片与新的是两种形状",
        minimum=1,
    ),
    # ── 第三档：可迁移且热生效 ──
    "LLM_PROVIDER": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "Provider 每次装配都现读配置；改完下一次调用就换家，不动业务代码",
    ),
    "LLM_BASE_URL": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "接入地址在每次装配 Provider 时读；空串表示用该供应商的默认地址",
    ),
    "LLM_MODEL": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "fast 模型角色（查询改写）在下次装配时读它；重排已改用专用端点，不再跟随 fast",
    ),
    "LLM_STRONG_MODEL": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "strong 模型角色在下次装配时读它；思考强度只作用于这个角色",
    ),
    "LLM_TIMEOUT_SECONDS": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "调用超时是每次请求的参数，改了下一轮刷新就按新值算",
        minimum=1,
    ),
    "LLM_REASONING_EFFORT": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "思考强度是每次调用的参数；改完下一次生成就按新值",
    ),
    "RERANK_ENABLED": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "重排开关是部署事实：关掉时工厂立即返回空实现，下一次检索就不再调模型",
    ),
    "RERANK_BASE_URL": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "重排端点在每次装配 Provider 时读；空串表示这台机器明确没有专用重排",
    ),
    "RERANK_MODEL": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "专用重排模型名在下次装配时读；换模型只影响之后的调用，不动已落库数据",
    ),
    # 密钥类**只读**：值只展示打码后的样子，写入口一律 20015（与 AUTH_ENABLED 同一条纪律）。
    # 它没有进热更档的原因要连起来读：热更意味着值会被反复读进内存、且面板上有一个可写的
    # 输入框；而对一把外部端点的凭证，改它本来就是「换一条接入」，该由部署方决定并重起，
    # 不该是一次热改。留空即回落 MODEL_API_KEY（同一把百炼 key 通常两处都通）。
    "RERANK_API_KEY": _spec(
        TIER_LOCKED,
        TARGET_SETTINGS,
        "密钥类只读不回显：面板只展示打码值；留空回落 MODEL_API_KEY，改它由部署方改 .env",
        secret=True,
    ),
    "RERANK_MAX_CANDIDATES": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "送进重排的候选上限只影响下一次调用（池宽是服务端选择，不进请求体）",
        minimum=1,
    ),
    "RERANK_CHUNK_MAX_CHARS": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "单条切片的截断长度只影响下一次调用；它直接等于提示词成本",
        minimum=1,
    ),
    "RERANK_TIMEOUT_SECONDS": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "超时由重排 Provider 每次调用时兜住；慢调用按失败处理并走降级",
        minimum=1,
    ),
    "QUERY_REWRITE_ENABLED": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "改写开关是部署事实：关掉时这一步是恒等变换，下一次检索就不调模型",
    ),
    "QUERY_REWRITE_TIMEOUT_MS": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "改写超时按每次调用生效；它必须小于客户端超时，否则降级也救不回来",
        minimum=1,
    ),
    "SYNONYM_EXPANSION_ENABLED": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "同义词扩展是纯函数，打开后下一次检索就多一份查询词，不需要重建",
    ),
    "SECTION_CONTEXT_ENABLED": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "节说明只影响新索引；开关本身下一次调用就生效，旧切片不用重建",
    ),
    "SECTION_CONTEXT_TIMEOUT_MS": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "节说明的超时按每次生成生效；它只作用于索引链路",
        minimum=1,
    ),
    "SECTION_CONTEXT_MAX_INPUT_CHARS": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "送进模型的节正文长度上限只影响下一次生成，不改变已落库的切片",
        minimum=1,
    ),
    "RETRIEVAL_MODE": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        # 这段 why 会**原样**渲染到面板表格里，所以不带 Markdown 标记：星号会以字面
        # 「**缺省值**」出现，用户看到的是排版错误而不是强调。
        "它只是「缺省值」（请求体没有 mode 字段）；改了下一次调用就按新缺省",
    ),
    "TOP_K": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "同样是缺省值：主要给 custom 档回落用，改了不影响已写入的索引",
        minimum=1,
    ),
    "HYBRID_RECALL_TOP_K": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "每路召回深度只影响下一次检索的候选池，不涉及任何已落库数据",
        minimum=1,
    ),
    # 第 11 步票 #127：``RETRIEVAL_POOL_SIZE`` / ``RETRIEVAL_QUEUE_BUDGET_MS`` 随执行池一起
    # 删除（见 ADR-0022「第五次修订」）。面板上不再有「改了也不生效」的两个键。
    "DOCUMENT_MAX_SIZE_MB": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "上传大小上限在每次上传时读，改完下一个请求就按新上限判",
        minimum=1,
    ),
    "PLAN_CACHE_TTL_SECONDS": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "缓存 TTL 在构造缓存时读；改完只影响新条目，最坏结果是旧值多活一会儿",
        minimum=1,
    ),
    "CONSOLE_SESSION_TTL_SECONDS": _spec(
        TIER_HOT,
        TARGET_SETTINGS,
        "会话有效期在登录时写进会话行与 cookie；改完只影响之后登录的人",
        minimum=60,
    ),
    # ── 角色指向（#94 写、这里只登记档位与理由，不参与 Settings 合并）──
    # 键名来自 app.core.model_roles 的推导（写死字面量等于第二份真相：
    # 工厂、删除判定与面板都按同一对名字读，改名时只允许改一处）。
    role_pointer_key(MODEL_FAST): _spec(
        TIER_HOT,
        TARGET_POINTER,
        "指向哪条模型配置；fast 用于查询改写（重排已换成专用端点，不再跟随这个角色）",
    ),
    role_pointer_key(MODEL_STRONG): _spec(
        TIER_HOT,
        TARGET_POINTER,
        "指向哪条模型配置；strong 用于需要思考的生成，思考强度只作用于它",
    ),
}

#: 首轮合并时把哪几档读进单例。``RESTART`` 只在本进程第一次合并时读入（见模块文档）。
_TIERS_MERGED_ON_REFRESH: Final[tuple[str, ...]] = (TIER_HOT,)

#: 刷新间隔（秒）。与缓存 TTL 同一个值：面板上「各进程 30s 内生效」这句话的唯一依据。
REFRESH_INTERVAL_SECONDS: Final[int] = CACHE_TTL_SECONDS

#: 审计里的动作名。列表与筛选都按它读，所以写在一处。
ACTION_SETTINGS_PATCH: Final[str] = "settings.patch"
#: 审计目标：一次 PATCH 可能改了好几个键，目标写成这一类对象而不是某一行的 id。
AUDIT_TARGET_SETTINGS: Final[str] = "settings"

#: 面板上的提示语。**不写「立即生效」**：写库之后别的进程最多要 30 秒才收敛，
#: 说「立即」会让管理员在另一个进程上看到一个还没变的值，然后以为保存失败。
NOTE_DRY_RUN: Final[str] = "试算未保存（dry_run=true）"
NOTE_HOT: Final[str] = "已推送（各进程 30s 内生效）"
NOTE_RESTART: Final[str] = "已保存，待重启与本步所需的数据重建后生效"

# ───────────────────────────────────────────────────────────────────────────────
# 类型与取值校验（不靠 Settings 的 validate_assignment —— 它没开）
# ─────────────────────────────────────────────────────────────────────────────────

#: ``Settings`` 上每个字段的声明类型。类型校验必须自己看一遍：pydantic 的宽松模式
#: 会把 ``True`` 收成 ``1``、把 ``"1"`` 收成 ``1``，那正是「写错了却看起来保存成功」。
_FIELD_TYPES: Final[dict[str, Any]] = {
    name: field.annotation for name, field in Settings.model_fields.items()
}

#: `.env` 层的缺省值：进程启动那一刻的快照。覆盖层永远只叠加在它上面。
#:
#: 为什么不每次现构造一个 ``Settings()``：那会把「运行期已经合并进来的覆盖值」与
#: 「环境变量」搅在一起——一旦某个覆盖被删掉，单例里就不会回到缺省值，而面板仍报
#: 「source=env」。一份启动快照才是「`.env` 是缺省」这句话的字面含义。
ENV_BASELINE: Final[dict[str, Any]] = settings.model_dump()


def spec_of(key: str) -> SettingSpec | None:
    """按键查白名单；不在表里返回 ``None``（**不抛异常**：读库时遇到野鍵要跳过）。"""

    return SETTING_SPECS.get(key)


def editable_specs(tier: str) -> dict[str, SettingSpec]:
    """某一档的全部键。面板按这个顺序渲染，键的顺序就是白名单里的声明顺序。"""

    return {key: spec for key, spec in SETTING_SPECS.items() if spec.tier == tier}


def _type_reason(key: str, value: Any) -> str | None:
    """JSON 值的类型是否与 ``Settings`` 上的声明一致；``None`` 表示一致。

    ``bool`` 与 ``int`` 分开判：``True`` 是 ``int`` 的子类，不排除的话
    ``TOP_K=True`` 会静默变成「返回 1 条」。
    """

    if SETTING_SPECS[key].target == TARGET_POINTER:
        # 指针值的语义是「一条 model_configs 的 id，或空值（= 不指向，回落 .env）」。
        # `None` 与空串都被接受：它们正是「改回默认」这个动作的两种写法（null / ""）。
        # 非空值只要求是字符串；**它指向的行是否存在由写入收口再判一次**（`_classify`
        # 还负责读库，读到一个已被删掉的 id 不该把这一行标红——那样面板会把一次
        # 绕过接口的删除显示成「配置非法」，而运行时的正确行为是回落到 .env）。
        if value is None:
            return None
        return None if isinstance(value, str) else "指针值必须是字符串或空值"

    annotation = _FIELD_TYPES.get(key)
    if annotation is None:
        # 白名单里有、Settings 上没有：说明白名单写错了。按「不可合并」处理，
        # 而不是让 model_validate 抛一个 KeyError 到请求路径上。
        return "Settings 上没有这个字段（白名单与配置表已经分叉）"
    if annotation is bool:
        return None if isinstance(value, bool) else "必须是布尔值"
    if annotation is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return None
        return "必须是整数"
    if annotation is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return None
        return "必须是数字"
    if annotation is str:
        return None if isinstance(value, str) else "必须是字符串"
    # 将来白名单里出现列表等复合类型时，交给 pydantic 自己判（这里不写死一份类型表）。
    return None


def _check_value(key: str, value: Any) -> tuple[Any, str | None]:
    """单个键的完整校验：白名单档位 → 类型 → 下界 → ``Settings`` 的 validator。

    返回 ``(校验后的值, 失败原因)``。校验后的值可能被 validator 规范化过
    （``RETRIEVAL_MODE`` 会被转小写），合并必须用它，不能用原始输入。

    **每个键单独过一遍**是刻意的：一个坏值只能丢它自己那一行，不能让整张表的覆盖一起
    失效——否则「运维手滑把超时写成 -1」会连带把另一个正确的模型配置也停掉。
    """

    spec = spec_of(key)
    if spec is None:
        return None, "不在白名单内"
    if spec.tier == TIER_LOCKED:
        return None, "该键不可迁移（安全开关 / 部署事实），只能由部署方改 .env"

    reason = _type_reason(key, value)
    if reason is not None:
        return None, reason
    if spec.minimum is not None and value < spec.minimum:
        return None, f"必须 ≥ {spec.minimum}（当前 {value}）"
    if spec.target == TARGET_POINTER:
        # 规范化：空值一律落成空串。库里 ``value`` 列非空，把「清空指向」写成 SQL NULL
        # 会撞 NOT NULL（JSON null 与 SQL NULL 在 SQLAlchemy 的绑定里是两件事）；
        # 空串既满足列约束，又让「空串 = 不指向」这条语义只有一个表示。
        return ("" if value is None else value.strip()), None

    # 跑的是**同一批 validator**：把当前值换掉这一项再构造一次 Settings。基准用启动期
    # 的 `.env` 快照（见 ENV_BASELINE），这样每个键都是拿缺省判断自己，不会因为上一次
    # 合并留下的值而「自我证明」。
    try:
        validated = Settings.model_validate({**ENV_BASELINE, key: value})
    except ValidationError as exc:
        return None, _first_error(exc)
    return getattr(validated, key), None


def _first_error(exc: ValidationError) -> str:
    """把 pydantic 的报错压成一句人话（面板上要能直接显示）。"""

    errors = exc.errors()
    if not errors:
        return "validator 拒绝了这个值"
    first = errors[0]
    message = first.get("msg", "validator 拒绝了这个值")
    return f"validator 拒绝：{message}"


def _visible_value(spec: SettingSpec, value: Any) -> Any:
    """面板与审计里能看到的那个值：密钥类字段走**唯一**的打码规则，其余原样。

    ``mask_secret`` 是 API Key、模型 ``api_key`` 与本模块共同的那一份规则（#82 修复：
    以前这里只露末 4 位、模型配置露前 3 + 末 4，两处都叫 masked 却不是同一个泄露面）。
    空值与非字符串保留原来的「••••」：那是「这一项没配」，不是一段可截断的凭证。
    """

    if not spec.secret:
        return value
    if not isinstance(value, str) or not value:
        return MASK_HIDDEN
    return mask_secret(value)


# ───────────────────────────────────────────────────────────────────────────────
# 覆盖层的读取与分类
# ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OverrideReport:
    """一次「读覆盖层」的结果：原始行、被采纳的行、被丢弃的行与原因。

    ``invalid`` 是面板标红的唯一来源（读时校验失败 → 回退 env 值 + 告警 + 标红），
    不是「这一行读不到」——读不到是另一个错误（走日志，不上面板）。
    """

    raw: Mapping[str, Any] = field(default_factory=dict)
    valid: Mapping[str, Any] = field(default_factory=dict)
    invalid: Mapping[str, str] = field(default_factory=dict)

    def merge_values(self, *, include_restart: bool) -> dict[str, Any]:
        """可以合并进 ``Settings`` 的那部分。

        只取 ``target="settings"`` 的键；``include_restart=False``（热刷新）时再滤掉第二档。
        """

        tiers = (TIER_HOT, TIER_RESTART) if include_restart else _TIERS_MERGED_ON_REFRESH
        return {
            key: value
            for key, value in self.valid.items()
            if SETTING_SPECS[key].target == TARGET_SETTINGS
            and SETTING_SPECS[key].tier in tiers
        }


def _classify(raw: Mapping[str, Any]) -> OverrideReport:
    """逐键校验一份原始覆盖行。坏的那一行只丢自己，其余照常采纳。"""

    valid: dict[str, Any] = {}
    invalid: dict[str, str] = {}
    for key, value in raw.items():
        normalized, reason = _check_value(key, value)
        if reason is None:
            valid[key] = normalized
        else:
            invalid[key] = reason
            logger.warning(
                "runtime setting rejected", setting=key, reason=reason, value=repr(value)
            )
    return OverrideReport(raw=dict(raw), valid=valid, invalid=invalid)


def _cache_or_default(cache: RuntimeSettingsCache | None) -> RuntimeSettingsCache:
    return get_settings_cache() if cache is None else cache


def _resolve_session_factory() -> Callable[[], Session]:
    """默认会话工厂：中间件与 Worker 拿不到 ``Depends``，只能自己开一个会话。

    实际实现收在 :mod:`app.db.session_factory`（与 #94 的 Provider 装配共用一个注入点：
    两处各开一个口子的话，测试只注入其中一个时，另一条路径就会悄悄连真 Postgres）。
    """

    return session_factory()


def set_session_factory(factory: Callable[[], Session] | None) -> None:
    """注入会话工厂（测试用假会话；生产复位成 ``SessionLocal``）。

    转发到进程级注入点（见 ``app/db/session_factory.py``）：这是 #93 的**公开**接口，
    既有测试与中间件都按这个名字注入，内部实现改成共享之后它们一行都不用改。

    这是一个**显式的注入点**而不是 monkeypatch 私有名字：中间件在请求路径上自己开会话，
    测试若不能替换它，就只能靠「本地恰好没有 Postgres」让套件变绿——那测到的是机器。
    """

    _set_session_factory(factory)


def _read_db_rows(db: Session | None) -> dict[str, Any]:
    """从库里读覆盖行。``db`` 为空时自己开一个会话（中间件 / Worker 的路径）。"""

    if db is not None:
        return {row.key: row.value for row in runtime_setting_repository.list_all(db)}
    session = _resolve_session_factory()()
    try:
        return {row.key: row.value for row in runtime_setting_repository.list_all(session)}
    finally:
        session.close()


def collect_overrides(
    db: Session | None = None, *, cache: RuntimeSettingsCache | None = None
) -> OverrideReport:
    """读覆盖层：先读缓存，未命中再读库，最后逐键校验。

    任何一步失败都**不抛给调用方**：缓存挂了就当没有缓存（慢一点、但还能跑），库读不到
    就当「没有覆盖」（于是行为回落到 `.env` 缺省）。这正是「不拒绝启动、也不静默吞掉」
    那条纪律：日志里有 warning 与原因，请求照常完成。
    """

    cache_obj = _cache_or_default(cache)
    raw: dict[str, Any] | None = None
    try:
        raw = cache_obj.get()
    except Exception as exc:  # noqa: BLE001 —— 缓存是加速层，它坏了只该变慢
        logger.warning("runtime settings cache read failed", error=str(exc))
    if raw is None:
        try:
            raw = _read_db_rows(db)
        except Exception as exc:  # noqa: BLE001 —— 读不到库时回落 .env 缺省
            logger.warning("runtime settings read failed", error=str(exc))
            return OverrideReport()
        try:
            cache_obj.set(raw)
        except Exception as exc:  # noqa: BLE001 —— 回填失败只影响下一次读
            logger.warning("runtime settings cache write failed", error=str(exc))
    return _classify(raw)


# ───────────────────────────────────────────────────────────────────────────────
# 合并与热更落实
# ───────────────────────────────────────────────────────────────────────────────

#: 进程内「上一次刷新」的状态。四个变量分别是：检查时刻、上一次 setattr 过的键、
#: 是否还处在「本进程第一次合并之前」、以及「已经有一个请求正在刷新」的并发闸门。
_state_lock = threading.Lock()
_last_checked_at: float | None = None
_last_applied_keys: frozenset[str] = frozenset()
_cold_start = True
_refreshing = False


def reset_runtime_state() -> None:
    """把进程内状态复位到「刚启动」：测试与「运行期换了一份配置源」都从这里开始。

    这是**公开接口**而不是 monkeypatch 私有变量：没有它，每个测试都要靠「上一条用例
    恰好没把单例改成别的值」才能通过，而那种依赖顺序的套件最难信任。
    """

    global _last_checked_at, _last_applied_keys, _cold_start, _refreshing
    with _state_lock:
        _last_checked_at = None
        _last_applied_keys = frozenset()
        _cold_start = True
        _refreshing = False


def _apply_report(report: OverrideReport, *, include_restart: bool) -> bool:
    """第 3、4 步：整体合并再校验，通过了才就地写单例。返回是否真的采纳。

    「整体校验」是比逐键校验更靠后的一道：逐键都合法但**键间**冲突时（比如两个互相矛盾
    的开关），整批放弃并保留上一份好值——半批生效会留下一个谁也没设计过的组合。
    """

    global _last_applied_keys
    candidates = report.merge_values(include_restart=include_restart)
    merged = dict(ENV_BASELINE)
    merged.update(candidates)
    try:
        validated = Settings.model_validate(merged)
    except ValidationError as exc:
        logger.warning(
            "runtime settings merge rejected; keeping the last good values",
            reason=_first_error(exc),
        )
        return False

    values = validated.model_dump()
    previous = set(_last_applied_keys)
    # 回收两件事：
    # * 上一次覆盖过、这次库里已经没有的**热更键** → 写回 `.env` 缺省值；
    # * 上一次覆盖过的**第二档键** → **不回收**：它的值只能由下一次重启重新读入，
    #   运行期把它改回缺省同样是一次「不重启就换了 chunk_size」的静默生效。
    restored = {
        key for key in previous - set(candidates) if SETTING_SPECS[key].tier == TIER_HOT
    }
    kept_restart = {key for key in previous if SETTING_SPECS[key].tier == TIER_RESTART}
    to_apply = set(candidates) | restored
    # 只写「这次被覆盖的键」「第二档已经生效过的键（值不变）」与「要回收的热更键」：
    # **别的键一根手指都不碰**——库里一行都没有时，进程行为与改造前逐字一致（回归红线）。
    for key in to_apply:
        setattr(settings, key, values[key])
    _last_applied_keys = frozenset(set(candidates) | kept_restart)
    logger.info(
        "runtime settings applied",
        settings=sorted(candidates),
        restored=sorted(restored),
    )
    return True


def apply_runtime_overrides(
    db: Session | None = None, *, cache: RuntimeSettingsCache | None = None
) -> OverrideReport:
    """读一次覆盖层并按四步合并进 ``app.core.config.settings`` 单例。

    ``RESTART`` 档只在本进程**第一次**合并时读入（见模块文档「为什么 RESTART 不热更」）：
    一次「保存」之后它不会自己生效，必须重启；面板据此显示「待重启」。
    """

    global _cold_start, _last_checked_at
    report = collect_overrides(db, cache=cache)
    with _state_lock:
        include_restart = _cold_start
    if _apply_report(report, include_restart=include_restart):
        with _state_lock:
            _cold_start = False
            _last_checked_at = time.monotonic()
    return report


def ensure_runtime_overrides(
    db: Session | None = None, *, cache: RuntimeSettingsCache | None = None
) -> None:
    """热更新入口：**每个请求**（app 中间件）与**每个任务**（Celery 入口）都调它。

    缓存命中的情况下这里只做一次时间比较（几十纳秒），不读缓存、不读库：把「每请求
    一次刷新」做成「每请求一次数据库往返」，会把检索的 P95 直接拖垮。

    并发进入时只有一个请求真正去刷新，其余直接放行：刷新的代价是「一次读库 + 一次合并」，
    让十个并发请求各做一遍毫无意义，而它们看到的会是同一份结果。
    """

    global _refreshing
    current = time.monotonic()
    with _state_lock:
        if (
            _last_checked_at is not None
            and current - _last_checked_at < REFRESH_INTERVAL_SECONDS
        ):
            return
        if _refreshing:
            return
        _refreshing = True
    try:
        apply_runtime_overrides(db, cache=cache)
    except Exception:  # noqa: BLE001 —— 刷新绝不允许把异常抛到请求路径上
        logger.exception("runtime settings refresh failed; keeping the last good values")
    finally:
        with _state_lock:
            _refreshing = False


def invalidate_runtime_settings(cache: RuntimeSettingsCache | None = None) -> None:
    """写完之后调它：删 Redis 缓存 + 清本进程的「刚检查过」标记。

    **删缓存，不写缓存**（ADR-0017）：写要每处修改都记得同步，漏一处就留下一个
    「看起来更新过、实际是旧值」的条目；删让下一次读自然拿到新值。
    """

    global _last_checked_at
    cache_obj = _cache_or_default(cache)
    try:
        cache_obj.delete()
    except Exception as exc:  # noqa: BLE001 —— 删不掉最多多等一个 TTL
        logger.warning("runtime settings cache delete failed", error=str(exc))
    with _state_lock:
        _last_checked_at = None
    # 不清 ``_last_applied_keys`` / ``_cold_start``：下一次合并会用新值覆盖已经生效的键，
    # 而 RESTART 档不该因为一次写就偷偷变成热更（那要重启）。


# ───────────────────────────────────────────────────────────────────────────────
# 面板视图（GET /console/settings）
# ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SettingRowView:
    """面板上的一行：键、档位、能不能改、**有效值**、来源、是否非法、为什么在这一档。"""

    key: str
    tier: str
    editable: bool
    value: Any
    source: SettingSource
    invalid: bool
    why: str
    invalid_reason: str | None = None


def describe_settings(db: Session) -> list[SettingRowView]:
    """按三档列出白名单的每一行（面板读的是**库里的真相**，不经过缓存）。

    为什么要绕过缓存：这一页要回答的恰恰是「库里现在是什么」，而缓存最多比库晚 30 秒。
    刚保存完就刷新面板却看到旧来源标记，会让人以为保存没成功——页面是给人看的，
    宁可多读一次库。
    """

    raw = _read_db_rows(db)
    report = _classify(raw)
    rows: list[SettingRowView] = []
    for key, spec in SETTING_SPECS.items():
        editable = spec.tier != TIER_LOCKED
        if key in report.valid:
            value: Any = _visible_value(spec, report.valid[key])
            source = SOURCE_DB
            invalid = False
            reason = None
        else:
            # 非法行与「没有覆盖行」都回退缺省值：前者是「库里那一行被忽略了」，
            # 后者是「这一项本来就走 .env」。两者靠 invalid 区分，值本身都是 env。
            value = (
                None
                if spec.target == TARGET_POINTER
                else _visible_value(spec, ENV_BASELINE.get(key))
            )
            source = SOURCE_ENV
            reason = report.invalid.get(key)
            invalid = reason is not None
        rows.append(
            SettingRowView(
                key=key,
                tier=spec.tier,
                editable=editable,
                value=value,
                source=source,
                invalid=invalid,
                why=spec.why,
                invalid_reason=reason,
            )
        )
    return rows


# ───────────────────────────────────────────────────────────────────────────────
# 影响预览（dry_run 的产出）
# ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SettingsImpact:
    """一次能力开关改动的影响面：两个形状相同的分组（与套餐影响预览同一套）。"""

    quality_degraded: ImpactGroup
    keyword_channel_lost: ImpactGroup


_ZERO_IMPACT: Final[ImpactGroup] = ImpactGroup(count=0, sample=())


def _turning_off_bool(key: str, values: Mapping[str, Any]) -> bool:
    """这次是不是「把一项现在开着的布尔开关关掉」。

    只有「开 → 关」才产生退化：本来就是关的，再关一次不改变任何租户的行为。
    当前值读的是**生效值**（单例），不是库里的缺省：管理员看到的是「现在真的在跑什么」。
    """

    requested = values.get(key)
    if not isinstance(requested, bool):
        return False
    return requested is False and bool(getattr(settings, key, False))


def _turning_off_keyword(values: Mapping[str, Any]) -> bool:
    """这次是不是把关键词通道关成 ``none``（同样只在「现在不是 none」时才算）。"""

    requested = values.get("KEYWORD_SEARCH_PROVIDER")
    if not isinstance(requested, str):
        return False
    if requested.strip().lower() != "none":
        return False
    current = str(getattr(settings, "KEYWORD_SEARCH_PROVIDER", "none")).strip().lower()
    return current != "none"


def _plan_of(db: Session, policy: KeyPolicy) -> Plan | None:
    """一条密钥配置所属租户的套餐；租户或套餐缺失时返回 ``None``（预演跳过它）。"""

    tenant = tenant_repository.get_by_id(db, policy.tenant_id)
    if tenant is None:
        return None
    return plan_repository.get_by_id(db, tenant.plan_id)


def preview_impact(db: Session, values: Mapping[str, Any]) -> SettingsImpact:
    """只读试算：这一批改动会让哪些**租户**的质量档位退化、哪些会丢掉关键词通道。

    判定口径（与 #82 的「能力开关影响预览」一节同一句）：

    * ``quality`` 档的链路是「混合 + 重排 + 改写」——关掉 ``RERANK_ENABLED`` 或
      ``QUERY_REWRITE_ENABLED`` 都让它的承诺少一步；
    * ``balanced`` / 任意 ``custom`` 只要链路是 ``hybrid``，就依赖关键词通道——
      把 ``KEYWORD_SEARCH_PROVIDER`` 关成 ``none`` 时它们只剩向量一路。

    两个额外的过滤条件，都是为了「只报真正会变的」：

    * 该租户的套餐必须**给过**这项能力：套餐没给过时这条配置本来就没在用它，
      关部署开关不会改变任何结果；
    * 当前生效值必须是「开着」：本来就是关的，再关一次什么也没变。

    计数按**租户**去重（一个租户可能有多套配置），``count`` 永远是全量、``sample`` 只放
    前 5 个租户名——与套餐影响预览逐字段同形，前端弹窗必须用 ``count``。
    """

    turning_off_rerank = _turning_off_bool("RERANK_ENABLED", values)
    turning_off_rewrite = _turning_off_bool("QUERY_REWRITE_ENABLED", values)
    losing_keyword = _turning_off_keyword(values)
    if not (turning_off_rerank or turning_off_rewrite or losing_keyword):
        return SettingsImpact(quality_degraded=_ZERO_IMPACT, keyword_channel_lost=_ZERO_IMPACT)

    degraded: list[str] = []
    lost: list[str] = []
    for policy in key_policy_repository.list_all(db):
        plan = _plan_of(db, policy)
        if plan is None:
            continue
        chain = profile_chain_of(policy_snapshot_of(policy))
        name = _tenant_name(db, policy.tenant_id)

        quality_loss = (
            (turning_off_rerank and chain.rerank and plan.rerank_allowed)
            or (turning_off_rewrite and chain.rewrite and plan.query_rewrite_allowed)
        )
        if quality_loss and name not in degraded:
            degraded.append(name)

        keyword_loss = losing_keyword and chain.mode == MODE_HYBRID and plan.hybrid_allowed
        if keyword_loss and name not in lost:
            lost.append(name)

    return SettingsImpact(
        quality_degraded=ImpactGroup(
            count=len(degraded), sample=tuple(degraded[:IMPACT_SAMPLE_LIMIT])
        ),
        keyword_channel_lost=ImpactGroup(count=len(lost), sample=tuple(lost[:IMPACT_SAMPLE_LIMIT])),
    )


def _tenant_name(db: Session, tenant_id: str) -> str:
    """租户的显示名；读不到行时用 id。影响面是给人看的，id 至少还能拿去查。"""

    tenant = tenant_repository.get_by_id(db, tenant_id)
    if tenant is None:
        return tenant_id
    return tenant.name or tenant.id


# ───────────────────────────────────────────────────────────────────────────────
# 写入（PATCH /console/settings）
# ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SettingsPatchResult:
    """一次配置保存的结果：试算标记、写进去的键（按档位分组）、前后快照、影响面与提示。"""

    dry_run: bool
    updated: tuple[str, ...]
    hot: tuple[str, ...]
    restart: tuple[str, ...]
    before: dict[str, Any]
    after: dict[str, Any]
    impact: SettingsImpact | None
    note: str


def _assert_writable(values: Mapping[str, Any]) -> None:
    """白名单与档位：**任意一个键不合格就整批不写**（20015）。

    整批拒绝而不是「跳过坏的写好的」：一次 PATCH 是管理员的一次意图，半批生效会让库里的
    状态既不是旧的也不是他以为的那个，而提示里还得解释「哪几项没写」——那正是最难解释的
    中间态。失败时一项都不写，管理员改完再提交一次。
    """

    if not values:
        raise AppError(ErrorCode.PARAM_ERROR, "no values to change")
    for key in values:
        spec = spec_of(key)
        if spec is None:
            raise AppError(ErrorCode.PARAM_ERROR, f"unknown setting: {key}（不在白名单内）")
        if spec.tier == TIER_LOCKED:
            raise AppError(
                ErrorCode.PARAM_ERROR,
                f"{key} is locked: {spec.why}",
            )


def _assert_values_valid(values: Mapping[str, Any]) -> dict[str, Any]:
    """逐键校验这次要写的值；任意一项不合格就是 ``20015``，且一项都还没写。

    返回规范化之后的值（validator 可能改过大小写）。
    """

    normalized: dict[str, Any] = {}
    for key, value in values.items():
        checked, reason = _check_value(key, value)
        if reason is not None:
            raise AppError(ErrorCode.PARAM_ERROR, f"{key}: {reason}")
        normalized[key] = checked
    return normalized


def _assert_pointer_targets(db: Session, values: Mapping[str, Any]) -> None:
    """角色指向的目标必须是一条**现存**的 ``model_configs`` 行（票 #94）。

    放在服务层而不是路由里：指针还有别的写入口（未来的 CLI / 迁移脚本），而「指向一个
    不存在的 id」在任何入口上都是同一个错误。空值不在这里判——它表示「不指向」，
    是「改回 .env」这个动作本身，合法且必须可用。

    判定本体在 :func:`app.services.model_config_service.ensure_pointer_target`，与删除
    被引用配置时的拒绝逻辑共用同一份「谁是角色、谁指着谁」的知识，这里只负责按白名单
    挑出指针键。
    """

    for key, value in values.items():
        if SETTING_SPECS[key].target == TARGET_POINTER:
            model_config_service.ensure_pointer_target(db, key, value)


def _assert_merge_valid(db: Session, values: Mapping[str, Any]) -> None:
    """键间冲突检查：把「库里的合法覆盖 + 这次的新值」整体过一遍 validator。

    这一步与逐键校验是两件事：逐键只看自己，整体能发现「A 改成 1、B 改成 2 之后互相
    矛盾」这种只有一起看才成立的错误。
    """

    report = _classify(_read_db_rows(db))
    merged = dict(ENV_BASELINE)
    merged.update(
        {
            key: value
            for key, value in report.valid.items()
            if SETTING_SPECS[key].target == TARGET_SETTINGS
        }
    )
    merged.update(values)
    try:
        Settings.model_validate(merged)
    except ValidationError as exc:
        raise AppError(
            ErrorCode.PARAM_ERROR, f"settings do not validate together: {_first_error(exc)}"
        ) from exc


def _override_snapshot(db: Session, key: str) -> Any:
    """改前这一键**在覆盖层里的样子**：有行就是那一行的值，没有行就是 ``None``。

    审计要回答「这次是新增一条覆盖，还是改掉一条已有覆盖」，所以这里报的是覆盖层的值
    而不是生效值——生效值在 before 里看不出来「之前根本没配过」。
    """

    row = runtime_setting_repository.get_by_key(db, key)
    return None if row is None else row.value


def patch_runtime_settings(
    db: Session,
    *,
    values: Mapping[str, Any],
    dry_run: bool,
    actor: Any,
    cache: RuntimeSettingsCache | None = None,
    at: datetime | None = None,
) -> SettingsPatchResult:
    """改配置的**唯一**入口：校验 →（可选）试算 → 落库 + 审计（同一事务）→ 删缓存。

    ``dry_run`` 由路由给（**默认 True**）：忘了这个参数时最坏结果是「只试算、没保存」，
    而不是「改了生产配置却没看到影响面」。``dry_run`` 不落库、不删缓存、不写审计。

    真写时按档位分开处理（提示语因此可以对不同项说不同的话）：

    * ``HOT``：写库 + 删缓存 + 审计，提示「各进程 30s 内生效」；
    * ``RESTART``：写库 + 审计，**不删缓存**（它不参与热更，删了也不会有人读）。
    """

    _assert_writable(values)
    normalized = _assert_values_valid(values)
    # 指针的存在性校验排在逐键校验之后、键间校验之前：值本身先要合法（字符串 / 空值），
    # 才谈得上「它指向的那一行在不在」。两项都失败时先报前一项，错误信息才是第一现场。
    _assert_pointer_targets(db, normalized)
    _assert_merge_valid(db, normalized)

    specs = {key: SETTING_SPECS[key] for key in normalized}
    before = {
        key: _visible_value(specs[key], _override_snapshot(db, key)) for key in normalized
    }
    after = {key: _visible_value(specs[key], value) for key, value in normalized.items()}
    hot = tuple(key for key, spec in specs.items() if spec.tier == TIER_HOT)
    restart = tuple(key for key, spec in specs.items() if spec.tier == TIER_RESTART)

    if dry_run:
        return SettingsPatchResult(
            dry_run=True,
            updated=(),
            hot=hot,
            restart=restart,
            before=before,
            after=after,
            impact=preview_impact(db, normalized),
            note=NOTE_DRY_RUN,
        )

    stamp = at if at is not None else now()
    actor_id = str(getattr(actor, "account_id", actor))
    for key, value in normalized.items():
        runtime_setting_repository.upsert(
            db, key=key, value=value, updated_by=actor_id, updated_at=stamp
        )
    # 审计与改动同一个事务：不允许出现「改了但没有审计」的窗口。审计落库失败就让整次
    # 改动一起回滚——没有账本的改动本来就不该生效（与改套餐同一条纪律）。
    append_audit(
        db,
        actor=actor,
        action=ACTION_SETTINGS_PATCH,
        target=AUDIT_TARGET_SETTINGS,
        # 前后快照里的密钥类字段已经在上面打过码：审计列表是管理员都能读的一页，
        # 把明文 Key 再抄一份进去只是把泄露面从一个表扩大到两个表。
        before=before,
        after=after,
    )
    # 删缓存必须等库先落地：先删再提交时，另一个请求可能刚好读回旧行并把它写回缓存。
    # 只有热生效档需要——第二档要重启才读入，删缓存不会让任何人看到新值。
    if hot:
        after_commit(db, lambda: invalidate_runtime_settings(cache))

    return SettingsPatchResult(
        dry_run=False,
        updated=tuple(normalized),
        hot=hot,
        restart=restart,
        before=before,
        after=after,
        impact=None,
        note=_written_note(hot=hot, restart=restart),
    )


def _written_note(*, hot: tuple[str, ...], restart: tuple[str, ...]) -> str:
    """一次保存的对外提示。**不写「立即生效」**，理由见 NOTE_HOT。"""

    if hot and restart:
        return f"{NOTE_HOT}；{len(restart)} 项需重启后生效"
    if hot:
        return NOTE_HOT
    return NOTE_RESTART

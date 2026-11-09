"""本次检索的**已解析参数**：模式、返回条数、是否重排、是否改写、结果形状，以及回显。

这是一次调用「用多好」的全部答案，而且**只由密钥配置决定**——请求体里只有
``kb_ids`` + ``query``（ADR-0010「第三次修订」、ADR-0018）。做成一个不可变的值对象
而不是一堆散落的关键字参数，理由有两条：

1. **来源只有一个**：组合根（``app.services.rag_service.retrieve``）拿到的是一个已经
   解析完的对象，调用链上不再出现「这里没传、所以去读服务端配置 / 读请求体」的分支。
   质量参数的读取点因此只剩本模块与 ``app.services.profile_policy`` 两处（前者是展开，
   后者是门禁）。
2. **加字段不必改一圈调用点**：后来的票往这个对象上加字段即可，构造点仍是这里与两个
   入口。

**这个模块只负责「展开」，不负责「准许」。** 档位展开成一个 :class:`ProfileChain`
（命名档位 = 完整替换的预设；``custom`` = 配置里那批手动参数，逐键回落部署缺省），
再由 ``app.services.profile_policy`` 依次过五道门、与部署能力做合取、生成回显。把两件事
分开的理由是：展开是**纯数据**（哪个档位承诺哪条链路），门禁是**判定**（这个租户买没买
得起）——混在一起之后，改一个预设值要连带读一遍套餐规则。

**三档的预设值写死，不读 ``settings``**：档位名承诺的是一段固定的产品行为。若
``balanced`` 的返回条数跟着 ``settings.TOP_K`` 漂，同一个档位名在两台机器上就是两条
不同的链路，而调用方看不出来。``custom`` 档才跟部署缺省走。

**链路常数不进来**：召回深度与融合池（``HYBRID_RECALL_TOP_K`` /
``RERANK_MAX_CANDIDATES``）决定「池子里有多少料」，不是「这次用多好」，仍由服务端
配置决定（ADR-0010「第三次修订」第 1 条的那个例外）。它们不在任何一档的预设里，
**也不许**以后的票顺手塞进来——同一个档位名在池宽不同的两台机器上会给出不同结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from app.core.config import SUPPORTED_RETRIEVAL_MODES, settings
from app.models.key_policy import (
    PROFILE_BALANCED,
    PROFILE_CUSTOM,
    PROFILE_QUALITY,
    PROFILE_SPEED,
    KeyPolicySnapshot,
)

# 这份 ``Literal`` 必须与 ``app.core.config.SUPPORTED_RETRIEVAL_MODES`` 保持一致；
# ``Literal`` 无法从元组展开，两处写两遍是语言限制，由一致性测试钉住。
RetrievalMode = Literal["vector", "bm25", "hybrid"]

# ``top_k`` 的硬上下界：下界保证「至少召回一条」，上界防止一次调用把整个库拖回来。
# 它是**接口契约里的常量**，不进配置：改了它，调用方能拿到的条数上限就变了。
# ``MAX_TOP_K`` 同时是裁剪时的「平台硬上限」——套餐里没有、也不该有「返回条数」这一列，
# 因为条数不是成本旋钮（召回深度才是），它只是响应形状的一部分。
MIN_TOP_K: Final[int] = 1
MAX_TOP_K: Final[int] = 50

# ``custom_options`` 里的键名。只在 ``profile='custom'`` 时有意义。
CUSTOM_OPTION_MODE: Final[str] = "mode"
CUSTOM_OPTION_TOP_K: Final[str] = "top_k"
CUSTOM_OPTION_RERANK: Final[str] = "rerank"
CUSTOM_OPTION_REWRITE: Final[str] = "rewrite"

#: 响应里回显「本次实际生效了什么」的那一栏（GLOSSARY ``tenant_policy``）。
TENANT_POLICY_KEY: Final[str] = "tenant_policy"

MODE_VECTOR: Final[str] = "vector"
MODE_HYBRID: Final[str] = "hybrid"


@dataclass(frozen=True)
class ProfileChain:
    """一个档位承诺的**一整条链路**：模式、返回条数、是否重排、是否查询改写。

    四项一起出现、一起被替换，这是「命名档位是完整替换」最直接的形式：没有「预设打底、
    配置覆盖」的第二条路径，因为一个 :class:`ProfileChain` 里没有「哪些项来自配置」这种
    信息可用——它本来就不该有。

    重排与改写在这里表达的是**意图**（这个档位想要什么），不是「这台机器上会不会跑」：
    部署装没装由 ``RERANK_ENABLED`` / ``QUERY_REWRITE_ENABLED`` 表达，生效值是两者的
    合取，见 ``app.services.profile_policy``。
    """

    mode: str
    top_k: int
    rerank: bool
    rewrite: bool


#: 三个命名档位的全部内容。**完整替换**：选它就等于选了这一整条链路。
#:
#: * ``speed``：只走向量一路，5 条，不重排、不改写——最便宜、最可预测；
#: * ``balanced``：混合检索（两路融合），5 条，不重排、不改写；
#: * ``quality``：混合 + 重排 + 改写，5 条——链路最长、延迟最高，也最能回答「文档里说的
#:   和我问的不是一个说法」这类问题。
#:
#: 条数统一是 5：档位之间差的是**链路**（路数、重排、改写），不是返回多少个字。
#: 想让调用方多拿几条是「结果形状组」的事，不是档位的事。
PROFILE_CHAINS: Final[dict[str, ProfileChain]] = {
    PROFILE_SPEED: ProfileChain(mode=MODE_VECTOR, top_k=5, rerank=False, rewrite=False),
    PROFILE_BALANCED: ProfileChain(mode=MODE_HYBRID, top_k=5, rerank=False, rewrite=False),
    PROFILE_QUALITY: ProfileChain(mode=MODE_HYBRID, top_k=5, rerank=True, rewrite=True),
}


@dataclass(frozen=True)
class TenantPolicy:
    """回显里**值对象本身表达不了**的那部分：套餐名、要求的档位、被裁掉的字段。

    模式 / 条数 / 重排 / 改写**不**存在这里：它们就是 :class:`RetrievalOptions` 上那几个
    字段。回显与生效值不能各存一份——两份就会分叉，而分叉的那一刻，调用方看到的就是
    「响应说开了重排、实际没开」这类无法自证的错误。:func:`tenant_policy_echo` 直接读
    值对象，正是为了在**类型层面**没有第二个真相。

    ``requested_profile`` 是配置里要求的档位（命名档位名或 ``custom``），``plan`` 是这个租户
    当前套餐的 ``code``。``clipped`` 只记**套餐上限**这一层裁掉的字段；本机没装重排 /
    改写不算裁剪，那是部署事实，已经由 ``rerank`` / ``rewrite`` 两个字段如实呈现。
    """

    plan: str
    requested_profile: str
    clipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalOptions:
    """一次检索的已解析参数；不可变，构造完就只读。

    ``mode`` 是字符串而不是 ``RetrievalMode``：值对象由 ``app.services.profile_policy``
    产出时已经验过词表，但 MCP / 内部调用仍可能自己造一个——那一道校验留在组合根
    （``rag_service.retrieve``），不因为多了这个对象就省掉（ADR-0010 §2「拒绝先于
    召回」：未知模式要先撞参数错误，而不是先查一次库）。

    ``fold_to_sections`` 刻意**不叫** ``include_parent``：后者是已发布的旧请求字段名，
    语义早已翻转（它实际控制的是「折不折叠」，ADR-0013）。请求契约收缩之后那一层
    不必再沿用这个与语义相反的名字（ADR-0010「第三次修订」第 4 条）；数据库列与
    密钥配置快照里的旧名为了不动已落地表结构仍然保留，两者在这里正好解耦。

    ``rerank`` / ``rewrite`` 是**生效值**，不是意愿值：它们已经是「档位想要」与「本机
    装了」的合取。链路因此只读这一个字段，不必再各自去问一次 ``settings``——
    两处判断迟早会分叉，而且分叉时谁也不知道哪一处才是「这次真的跑了吗」的答案。

    ``tenant_policy`` 是回显里值对象表达不了的那部分（见 :class:`TenantPolicy`）。
    缺省 ``None`` 的意思是「这份参数不是经由套餐门禁解析出来的」——评测脚本与内部调用
    会自己造 options，它们没有套餐可回显；与其编一个看起来合理的套餐名，不如承认这一点。
    """

    mode: str
    top_k: int
    fold_to_sections: bool
    expand_table_rows: bool
    rerank: bool
    rewrite: bool
    tenant_policy: TenantPolicy | None = None


def profile_chain_of(policy: KeyPolicySnapshot) -> ProfileChain:
    """把配置里的档位**展开**成一整条链路（五道门的第 2 道，纯数据那一步）。

    * 命名档位：查 :data:`PROFILE_CHAINS`，**配置里的 ``custom_options`` 一个都不读**
      ——完整替换，不是「预设打底、配置覆盖」；
    * ``custom``：读那批手动参数，**逐键**回落部署缺省（``RETRIEVAL_MODE`` /
      ``TOP_K`` / ``RERANK_ENABLED`` / ``QUERY_REWRITE_ENABLED``）；
    * 未知档位名：回落到部署缺省。配置写入时已经验过词表，这里只兜底，不把调用当场
      打死——与 ``custom`` 档「值不认识按缺键处理」同一条口径。
    """

    if policy.profile == PROFILE_CUSTOM:
        manual = dict(policy.custom_options)
        return ProfileChain(
            mode=_manual_mode(manual),
            top_k=_manual_top_k(manual),
            # 缺键 = 「跟部署缺省走」：部署开关就是「这台机器有没有这项能力」的缺省意图。
            rerank=_manual_flag(manual, CUSTOM_OPTION_RERANK, fallback=settings.RERANK_ENABLED),
            rewrite=_manual_flag(
                manual, CUSTOM_OPTION_REWRITE, fallback=settings.QUERY_REWRITE_ENABLED
            ),
        )

    preset = PROFILE_CHAINS.get(policy.profile)
    if preset is None:
        return server_default_chain()
    return preset


def server_default_chain() -> ProfileChain:
    """部署缺省的那条链路：四个值全部来自服务端配置。

    它只做三件事：``custom`` 档的逐键回落、未知档位的兜底、以及给「缺省行为」一个
    可引用的名字。**没有哪一档的预设读它**——档位是产品契约，不跟着部署配置漂。
    """

    return ProfileChain(
        mode=_server_default_mode(),
        top_k=settings.TOP_K,
        rerank=settings.RERANK_ENABLED,
        rewrite=settings.QUERY_REWRITE_ENABLED,
    )


def options_from_chain(
    *,
    policy: KeyPolicySnapshot,
    chain: ProfileChain,
    tenant_policy: TenantPolicy | None = None,
    rerank_capable: bool | None = None,
    rewrite_capable: bool | None = None,
) -> RetrievalOptions:
    """把「档位意图」与「本机能力」合成这次调用的最终参数。

    **生效值 = 意图 ∧ 能力**，这一条只有本函数一处实现。档位绝不能凭空变出一个部署
    没配置的 Provider：声称「高质量」却在没装重排的机器上默默跳过一次重排，比直接
    报错更难查——回显里写着 ``rerank: true``，实际一次模型都没调。

    ``rerank_capable`` / ``rewrite_capable`` 缺省读服务端配置；做成入参是为了让「装了 /
    没装」两种行为都能被纯函数直接测到（与 ``resolve_identity`` 的 ``auth_enabled``
    同一套做法：不必 monkeypatch 全局配置，也就不会在用例之间留下污染）。

    两个结果形状开关**每一档都读**配置自己那两列：它们是「结果形状组」，不是
    ``custom`` 档的专属手动参数（父 spec「密钥配置的内容是五组」）。
    """

    rerank_on = settings.RERANK_ENABLED if rerank_capable is None else rerank_capable
    rewrite_on = settings.QUERY_REWRITE_ENABLED if rewrite_capable is None else rewrite_capable

    return RetrievalOptions(
        mode=chain.mode,
        top_k=chain.top_k,
        fold_to_sections=policy.include_parent,
        expand_table_rows=policy.expand_table_rows,
        rerank=chain.rerank and rerank_on,
        rewrite=chain.rewrite and rewrite_on,
        tenant_policy=tenant_policy,
    )


def tenant_policy_echo(options: RetrievalOptions, *, kb_count: int) -> dict[str, object] | None:
    """响应 ``metadata.tenant_policy``：这次调用**实际生效**了什么。

    模式 / 条数 / 重排 / 改写一律从 ``options`` 上读（理由见 :class:`TenantPolicy`）；
    真正「这次用到了几个库」只有调用点知道，所以 ``kb_count`` 是入参而不是从别处算。

    返回 ``None`` 表示这份参数不经套餐门禁解析（见 ``RetrievalOptions.tenant_policy``）。
    """

    policy = options.tenant_policy
    if policy is None:
        return None
    return {
        "plan": policy.plan,
        "requested_profile": policy.requested_profile,
        "mode": options.mode,
        "top_k": options.top_k,
        "rerank": options.rerank,
        "rewrite": options.rewrite,
        "kb_count": kb_count,
        "clipped": list(policy.clipped),
    }


def _server_default_mode() -> str:
    # 配置里的值由 Settings 的校验器在**启动时**验过（配错就起不来），这里不必再抛
    # 一次；这个 cast 只对类型检查器负责，不是运行时守卫。
    return cast(RetrievalMode, settings.RETRIEVAL_MODE)


def _manual_mode(manual: dict[str, Any]) -> str:
    """``custom`` 档的检索方式；缺键或值不认识时回落到服务端缺省。

    「值不认识」也按缺键处理，而不是报 ``20002``：后者是**请求参数**校验失败的意思，
    配置里的一个错字不该冒充它；配置写入时已做自洽校验，运行时这一层只兜底。
    """

    value = manual.get(CUSTOM_OPTION_MODE)
    if isinstance(value, str) and value in SUPPORTED_RETRIEVAL_MODES:
        return cast(RetrievalMode, value)
    return _server_default_mode()


def _manual_top_k(manual: dict[str, Any]) -> int:
    """``custom`` 档的返回条数；缺键、低于下界或不是整数时回落到服务端缺省。

    **上界不在这里夹。** 超过 ``MAX_TOP_K`` 的条数交给平台层的裁剪步骤夹到硬上限，
    并在响应里**回显**「``top_k`` 被裁了」（#78）。在这里静默回落到缺省值看着更整齐，
    却正是那条「裁剪却不回显」的路：调用方要 70 条、拿到 5 条，而响应里没有任何一处
    说明发生了什么。

    ``bool`` 单独排除：``True`` 是 ``int`` 的子类，否则 ``top_k=True`` 会静默变成
    「返回 1 条」。
    """

    value = manual.get(CUSTOM_OPTION_TOP_K)
    if isinstance(value, int) and not isinstance(value, bool) and value >= MIN_TOP_K:
        return value
    return settings.TOP_K


def _manual_flag(manual: dict[str, Any], key: str, *, fallback: bool) -> bool:
    """``custom`` 档的布尔开关；缺键或不是布尔时回落到 ``fallback``（部署缺省）。

    不写 ``bool(value)``：``"false"`` 这个字符串会被判成 ``True``，而配置里最常见的
    手写错误恰好就是把它写成字符串。
    """

    value = manual.get(key)
    if isinstance(value, bool):
        return value
    return fallback

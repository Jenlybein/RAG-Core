"""本次检索的**已解析参数**：模式、返回条数、是否重排、结果形状，以及回显。

这是一次调用「用多好」的全部答案，而且**只由密钥配置决定**——请求体里只有
``kb_ids`` + ``query``（ADR-0010「第三次修订」、ADR-0018）。做成一个不可变的值对象
而不是一堆散落的关键字参数，理由有两条：

1. **来源只有一个**：组合根（``app.services.rag_service.retrieve``）拿到的是一个已经
   解析完的对象，调用链上不再出现「这里没传、所以去读服务端配置 / 读请求体」的分支。
   质量参数的读取点因此只剩本模块与 ``app.services.profile_policy`` 两处（前者是读值，
   后者是门禁）。
2. **加字段不必改一圈调用点**：后来的票往这个对象上加字段即可，构造点仍是这里与两个
   入口。

**第 13 步起后端没有「档位」**：一套配置用多好由三项直参数
``retrieval_mode / retrieval_top_k / retrieval_rerank`` 表达（ADR-0032）。本模块因此
只做一件事——**把三项参数从快照上读出来**，没有「预设打底、配置覆盖」的第二条路径，
也没有 ``custom`` 这个特例（``custom_options`` 列已随档位一起退役）。

**这个模块只负责「读值」，不负责「准许」。** 与部署能力做合取、按套餐裁剪 ``top_k``、
按授权位判端点是 ``app.services.profile_policy`` 的事。展开是**纯数据**，门禁是**判定**
——混在一起之后，改一个参数要连带读一遍套餐规则。

**链路常数不进来**：召回深度与融合池（``HYBRID_RECALL_TOP_K`` /
``RERANK_MAX_CANDIDATES``）决定「池子里有多少料」，不是「这次用多好」，仍由服务端
配置决定（ADR-0010「第三次修订」第 1 条的那个例外）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, cast

from app.core.config import settings
from app.providers.rerank import rerank_capability
from app.models.key_policy import KeyPolicySnapshot

# 这份 ``Literal`` 必须与 ``app.core.config.SUPPORTED_RETRIEVAL_MODES`` 保持一致；
# ``Literal`` 无法从元组展开，两处写两遍是语言限制，由一致性测试钉住。
RetrievalMode = Literal["vector", "bm25", "hybrid"]

# ``top_k`` 的硬上下界：下界保证「至少召回一条」，上界防止一次调用把整个库拖回来。
# 它是**接口契约里的常量**，不进配置：改了它，调用方能拿到的条数上限就变了。
# ``MAX_TOP_K`` 同时是裁剪时的「平台硬上限」——套餐里的 ``max_top_k`` 只能更小，
# 不能更大，因为条数首先是响应形状的一部分。
MIN_TOP_K: Final[int] = 1
MAX_TOP_K: Final[int] = 50

#: 响应里回显「本次实际生效了什么」的那一栏（GLOSSARY ``tenant_policy``）。
TENANT_POLICY_KEY: Final[str] = "tenant_policy"

MODE_VECTOR: Final[str] = "vector"
MODE_HYBRID: Final[str] = "hybrid"
#: 关键词单路。与上面两个同一套词表（``RetrievalMode`` 也有它）：三种模式的**分支**
#: 在内核（``app/services/retrieval/kernel.py``），词表只该有一份。
MODE_BM25: Final[str] = "bm25"


@dataclass(frozen=True)
class RetrievalParams:
    """一套配置直存的三项参数：检索方式、返回条数、是否重排。

    它是 frozen 的值对象：一次调用的参数在构造完就只读，链路里不存在「谁顺手改一下」
    的空子。``rerank`` 在这里是**意图**（这份配置想要重排），不是「这次会不会跑」：
    生效值还要与授权位、部署能力、重排额度三件事做合取，见
    ``app.services.profile_policy`` 与 ``app.services.retrieval_gate``。
    """

    mode: str
    top_k: int
    rerank: bool


@dataclass(frozen=True)
class TenantPolicy:
    """回显里**值对象本身表达不了**的那部分：套餐名与被裁掉的字段。

    模式 / 条数 / 重排**不**存在这里：它们就是 :class:`RetrievalOptions` 上那几个
    字段。回显与生效值不能各存一份——两份就会分叉，而分叉的那一刻，调用方看到的就是
    「响应说开了重排、实际没开」这类无法自证的错误。:func:`tenant_policy_echo` 直接读
    值对象，正是为了在**类型层面**没有第二个真相。

    ``clipped`` 只记**套餐 ``max_top_k`` / 平台硬上限**这一层裁掉的字段名。第 13 步起
    它是**唯一**的裁剪来源：模式与重排不再是「被套餐裁掉」的东西，而是授权位（位为假
    就是 ``20013``，不是悄悄降级）。
    """

    plan: str
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

    ``rerank`` 是**生效值**，不是意愿值：它已经是「配置想要」∧「有授权位」∧「本机装
    了」的合取。链路因此只读这一个字段，不必再各自去问一次 ``settings``——
    两处判断迟早会分叉，而且分叉时谁也不知道哪一处才是「这次真的跑了吗」的答案。

    ``tenant_policy`` 是回显里值对象表达不了的那部分（见 :class:`TenantPolicy`）。
    缺省 ``None`` 的意思是「这份参数不是经由套餐门禁解析出来的」——评测脚本与内部调用
    会自己造 options，它们没有套餐可回显；与其编一个看起来合理的套餐名，不如承认这一点。
    """

    mode: str
    top_k: int
    fold_to_sections: bool
    expand_table_rows: bool
    #: 一条结果 ``content`` 的硬上限（字符数，第 16 步 #207）。来自这把 Key 的配置，
    #: 与 ``top_k`` 一样是「这次调用要多少东西」的旋钮。
    max_unit_chars: int
    rerank: bool
    #: 重排**本该跑却没跑**的原因（第 13 步）：目前只有「额度用尽」一种，值是固定串
    #: ``rerank quota exhausted``。``rerank=False`` 时它才有意义，链路据它把降级原因
    #: 写进 ``metadata``——「跳过」与「没装」必须能分开，否则调用方看不出这次少了什么。
    rerank_skipped_reason: str | None = None
    tenant_policy: TenantPolicy | None = None


def retrieval_params_of(policy: KeyPolicySnapshot) -> RetrievalParams:
    """把配置里的三项直参数读成一个值对象（唯一读取点）。

    没有兜底、没有分支：快照上的三个字段就是库里的三列，``policy_snapshot_of`` 已经
    逐字段搬过一遍。缺值只可能是「映射漏了一列」，那在快照构造时就该炸，不该在这里
    被一个「看起来合理」的缺省盖住。
    """

    return RetrievalParams(
        mode=policy.retrieval_mode,
        top_k=policy.retrieval_top_k,
        rerank=policy.retrieval_rerank,
    )


def server_default_params() -> RetrievalParams:
    """部署缺省的三项参数：三个开关各读一处服务端配置。

    它只在两类地方被引用：没有套餐可解析的内部调用，以及「管理员把某一列清空后回落」
    的说明性缺省。**它不作为任何一档预设的底稿**——参数是存下来的事实，不跟着部署配置漂。
    """

    return RetrievalParams(
        mode=_server_default_mode(),
        top_k=settings.TOP_K,
        rerank=rerank_capability(),
    )


def options_from_params(
    *,
    policy: KeyPolicySnapshot,
    params: RetrievalParams,
    tenant_policy: TenantPolicy | None = None,
    rerank_capable: bool | None = None,
) -> RetrievalOptions:
    """把「配置想要的参数」与「本机能力」合成这次调用的最终参数。

    **生效值 = 意图 ∧ 能力**，这一条只有本函数一处实现。配置绝不能凭空变出一个部署
    没配置的 Provider：声称「开了重排」却在没装重排的机器上默默跳过，比直接报错更难查
    ——回显里写着 ``rerank: true``，实际一次模型都没调。

    ``rerank_capable`` 缺省读服务端配置；做成入参是为了让「装了 / 没装」两种行为都能被
    纯函数直接测到（不必 monkeypatch 全局配置，也就不会在用例之间留下污染）。调用方
    （``profile_policy``）负责把「授权位 ∧ 本机能力」一起折进这个入参。

    两个结果形状开关**每次调用都读**配置自己那两列：它们是「结果形状组」，与参数无关。
    """

    rerank_on = rerank_capability() if rerank_capable is None else rerank_capable

    return RetrievalOptions(
        mode=params.mode,
        top_k=params.top_k,
        fold_to_sections=policy.include_parent,
        expand_table_rows=policy.expand_table_rows,
        max_unit_chars=policy.max_unit_chars,
        rerank=params.rerank and rerank_on,
        tenant_policy=tenant_policy,
    )


def tenant_policy_echo(options: RetrievalOptions, *, kb_count: int) -> dict[str, object] | None:
    """响应 ``metadata.tenant_policy``：这次调用**实际生效**了什么。

    第 13 步起后端不再有档位概念、改写也由端点而非配置决定，所以回显里不出现
    ``requested_profile`` 与 ``rewrite``——它只报**参数本身**（模式 / 条数 / 重排）、
    套餐名、库数与「被裁了什么」。这些值一律从 ``options`` 上读（理由见
    :class:`TenantPolicy`）；``kb_count`` 只有调用点知道，所以是入参。

    返回 ``None`` 表示这份参数不经套餐门禁解析（见 ``RetrievalOptions.tenant_policy``）。
    """

    policy = options.tenant_policy
    if policy is None:
        return None
    return {
        "plan": policy.plan,
        "mode": options.mode,
        "top_k": options.top_k,
        "rerank": options.rerank,
        "kb_count": kb_count,
        "clipped": list(policy.clipped),
    }


def _server_default_mode() -> str:
    # 配置里的值由 Settings 的校验器在**启动时**验过（配错就起不来），这里不必再抛
    # 一次；这个 cast 只对类型检查器负责，不是运行时守卫。
    return cast(RetrievalMode, settings.RETRIEVAL_MODE)

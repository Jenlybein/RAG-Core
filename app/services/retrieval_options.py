"""本次检索的**已解析参数**：模式、返回条数、是否折叠成整节、表格行是否折回整表。

这是一次调用「用多好」的全部答案，而且**只由密钥配置决定**——请求体里只有
``kb_ids`` + ``query``（ADR-0010「第三次修订」、ADR-0018）。做成一个不可变的值对象
而不是四个散落的关键字参数，理由有两条：

1. **来源只有一个**：组合根（``app.services.rag_service.retrieve``）拿到的是一个已经
   解析完的对象，调用链上不再出现「这里没传、所以去读服务端配置 / 读请求体」的分支。
   质量参数的读取点因此只剩本模块一处。
2. **加字段不必改一圈调用点**：后续票（#78 的命名档位预设、重排与改写开关）往这个
   对象上加字段即可，构造点仍是这里与两个入口。

**链路常数不进来**：召回深度与融合池（``HYBRID_RECALL_TOP_K`` /
``RERANK_MAX_CANDIDATES``）决定「池子里有多少料」，不是「这次用多好」，仍由服务端
配置决定（ADR-0010「第三次修订」第 1 条的那个例外）。

**解析规则（本票口径）**：

* 档位是 ``custom``：手动参数（检索方式、返回条数）读 ``custom_options``，**逐键**
  回落到服务端缺省；两个形状开关读配置自己那两列（它们是「结果形状偏好」，不是
  ``custom`` 档的专属字段）。
* 档位是命名档位（``speed`` / ``balanced`` / ``quality``）：**临时**解析成服务端
  缺省，见 :func:`_named_tier_placeholder`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from app.core.config import SUPPORTED_RETRIEVAL_MODES, settings
from app.models.key_policy import PROFILE_CUSTOM, KeyPolicySnapshot

# 这份 ``Literal`` 必须与 ``app.core.config.SUPPORTED_RETRIEVAL_MODES`` 保持一致；
# ``Literal`` 无法从元组展开，两处写两遍是语言限制，由一致性测试钉住。
RetrievalMode = Literal["vector", "bm25", "hybrid"]

# ``top_k`` 的硬上下界：下界保证「至少召回一条」，上界防止一次调用把整个库拖回来。
# 它是**接口契约里的常量**，不进配置：改了它，调用方能拿到的条数上限就变了。
MIN_TOP_K: Final[int] = 1
MAX_TOP_K: Final[int] = 50

# ``custom_options`` 里的键名。只在 ``profile='custom'`` 时有意义。
CUSTOM_OPTION_MODE: Final[str] = "mode"
CUSTOM_OPTION_TOP_K: Final[str] = "top_k"

#: 服务端缺省的形状偏好。**不是**「配置读不到时的兜底」——真实配置永远有自己的两列；
#: 它只用于命名档位的临时兜底，以及给「缺省行为」一个可引用的名字。
SERVER_DEFAULT_FOLD_TO_SECTIONS: Final[bool] = True
SERVER_DEFAULT_EXPAND_TABLE_ROWS: Final[bool] = True


@dataclass(frozen=True)
class RetrievalOptions:
    """一次检索的已解析参数；不可变，构造完就只读。

    ``mode`` 是字符串而不是 ``RetrievalMode``：值对象由 :func:`options_from_policy`
    产出时已经验过词表，但 MCP / 内部调用仍可能自己造一个——那一道校验留在组合根
    （``rag_service.retrieve``），不因为多了这个对象就省掉（ADR-0010 §2「拒绝先于
    召回」：未知模式要先撞参数错误，而不是先查一次库）。

    ``fold_to_sections`` 刻意**不叫** ``include_parent``：后者是已发布的旧请求字段名，
    语义早已翻转（它实际控制的是「折不折叠」，ADR-0013）。请求契约收缩之后那一层
    不必再沿用这个与语义相反的名字（ADR-0010「第三次修订」第 4 条）；数据库列与
    密钥配置快照里的旧名为了不动已落地表结构仍然保留，两者在这里正好解耦。

    **本票只有这四个字段**：重排与查询改写仍是服务端全局开关
    （``RERANK_ENABLED`` / ``QUERY_REWRITE_ENABLED``），由 #78 与后续票把它们搬进
    这个对象（命名档位承诺的是一整条链路）。现在加它们会是「顺手改行为」。
    """

    mode: str
    top_k: int
    fold_to_sections: bool
    expand_table_rows: bool


def options_from_policy(policy: KeyPolicySnapshot) -> RetrievalOptions:
    """把一份密钥配置快照解析成这次调用要用的参数。

    参数来自**配置**，不来自请求：请求里连字段都没有（ADR-0010「第三次修订」）。
    """

    if policy.profile == PROFILE_CUSTOM:
        manual = dict(policy.custom_options)
        return RetrievalOptions(
            mode=_manual_mode(manual),
            top_k=_manual_top_k(manual),
            # 两个形状开关读配置自己那两列：它们是「结果形状偏好」，不属于 ``custom``
            # 档的专属手动参数（父 spec「密钥配置的内容是五组」）。
            fold_to_sections=policy.include_parent,
            expand_table_rows=policy.expand_table_rows,
        )

    return _named_tier_placeholder()


def _named_tier_placeholder() -> RetrievalOptions:
    """**临时兜底（票 #77）**：命名档位此刻解析成服务端缺省值。

    ``speed`` / ``balanced`` / ``quality`` 的真实预设（各自的模式、返回条数，以及
    重排与改写开关）由 **#78** 引入。本票只收缩请求契约，不许顺手改变既有调用方的
    检索行为：三个命名档位现在没有任何可用的预设值，任何「按档位猜一份」的写法都会
    让一批既有测试的结果发生变化，773 条的基线也就证明不了「契约收缩没有改变行为」。

    所以这里**连配置里的 ``custom_options`` 与两个形状列都不读**，一律返回服务端
    缺省：默认检索模式、默认返回条数、两个形状开关都开——与收缩之前「请求不传这四个
    字段」时的行为逐字节相同。

    **#78 落地真实预设时删掉本函数**，把 :func:`options_from_policy` 的命名档位分支
    指向预设表。当前行为由 ``tests/test_retrieval_options.py`` 里
    ``test_named_tier_options_are_the_server_defaults_for_now`` 钉住：那条用例在 #78
    里**应当变红**，提醒改动的人这是有意为之，而不是被顺手改坏的。
    """

    return RetrievalOptions(
        mode=_server_default_mode(),
        top_k=settings.TOP_K,
        fold_to_sections=SERVER_DEFAULT_FOLD_TO_SECTIONS,
        expand_table_rows=SERVER_DEFAULT_EXPAND_TABLE_ROWS,
    )


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
    """``custom`` 档的返回条数；缺键、越界或不是整数时回落到服务端缺省。

    ``bool`` 单独排除：``True`` 是 ``int`` 的子类，否则 ``top_k=True`` 会静默变成
    「返回 1 条」。
    """

    value = manual.get(CUSTOM_OPTION_TOP_K)
    if isinstance(value, int) and not isinstance(value, bool) and MIN_TOP_K <= value <= MAX_TOP_K:
        return value
    return settings.TOP_K

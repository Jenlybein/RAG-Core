"""与协议无关的错误词汇：分段错误码 + 业务异常载体。

权威全表来自 docs/设计决策.md「错误码全表」：全表一次性落地，3xxxx 仅预留。
分段按责任方划分：0 成功；2xxxx 是调用方能处理的业务失败；3xxxx 是存储段，
当前全表里没有成员，只以注释与本文档说明预留，不编造没有调用点的码；
4xxxx 是外部模型 / 服务请求失败；5xxxx 是服务异常与系统级失败。
后续章节只追加编号，不改既有值。

本模块不放任何 HTTP 或 JSON-RPC 知识。HTTP 入口的翻译在 app/api/error_handlers.py；
MCP 入口（第 1 步接通、第 7 步接上真实鉴权）会按自己的协议翻译同一个词汇——共享的是错误词汇，不是响应壳。
"""

from enum import IntEnum
from typing import Any

__all__ = ["AppError", "ErrorCode", "default_message"]


class ErrorCode(IntEnum):
    """错误码全表：全表一次性落地，3xxxx 仅预留。"""

    SUCCESS = 0

    # 2xxxx：业务失败（可预期、调用方能处理）
    REQUEST_VALIDATION_ERROR = 20002
    API_RATE_LIMIT = 20005
    KNOWLEDGE_BASE_NOT_FOUND = 20008
    UNAUTHORIZED = 20010
    FORBIDDEN = 20011
    FEATURE_NOT_ALLOWED = 20013
    QUOTA_EXCEEDED = 20014
    PARAM_ERROR = 20015
    NOT_FOUND = 20016
    PLAN_NOT_FOUND = 20017
    PLAN_INVALID = 20018
    PLAN_IN_USE = 20019
    FEEDBACK_FAILED = 20020
    FEEDBACK_LOG_MISMATCH = 20021
    FEEDBACK_INVALID_SCORE = 20022
    DOCUMENT_PARSE_FAILED = 20023
    DOCUMENT_CONTENT_EMPTY = 20024
    DOCUMENT_IS_PROCESSING = 20025
    FILE_TOO_LARGE = 20026
    REINDEX_REQUIRES_FORCE = 20027
    UNSUPPORTED_DOCUMENT_FORMAT = 20028
    # ── 文档版本（第 12 步 · #175）────────────────────────────────────────────
    # 稿号是**写入侧算出来的**（max(version) + 1），两个写入方同时算出同一个号时由
    # (document_id, version) 唯一约束顶住：那时不是 500，而是「换个号再来一次」。
    DOCUMENT_VERSION_CONFLICT = 20029
    DOCUMENT_VERSION_NOT_FOUND = 20030
    DOCUMENT_VERSION_UNCHANGED = 20031
    # 「这一稿已经是当前稿」属于 #176 的回滚语义；本票用不到它，但词汇位先占好——
    # 之后再补这个码时，「20032 是什么」不应该有第二种答案。
    DOCUMENT_VERSION_ALREADY_ACTIVE = 20032
    # 问题里没有可检索的内容（只有标点 / 符号 / 感叹词）。三条模型端点（改写、分解、
    # Research）在**任何模型调用之前**拒掉它：那种输入不会失败，模型会编出一堆「要点」
    # 再拿它们去召回，调用方拿到 code=0 与一串无关切片，看不出问题不在自己。判定口径见
    # ``app.services.query_content``；``/retrieve`` 不加这道闸（它不读语义、只做召回）。
    QUERY_HAS_NO_CONTENT = 20033
    # ── 容量与切块额度（第 17 步 · #209）──────────────────────────────────────
    # 与 20014 分开的理由是**调用方的处置不同**：库数 / 文档数 / 单库 token 满了都是「删点
    # 东西就能继续」，日切块额度用尽只能「等重置或提额」。混在 20014 里时前端只能给一句
    # 对三种情形都不对的文案（历史上正是如此：让人去密钥配置里改一个在套餐里的数字）。
    #
    # 四个码一次给全、按编号顺序排列，因为错误码全表是与本枚举逐项对账的（见
    # ``tests/test_business_errors.py``）：表里先落、枚举后落会让对账当场红。
    KB_COUNT_QUOTA_EXCEEDED = 20034
    KB_DOCUMENT_QUOTA_EXCEEDED = 20035
    KB_TOKEN_QUOTA_EXCEEDED = 20036
    CHUNK_DAILY_QUOTA_EXCEEDED = 20037
    #: 库里**没有一篇产物落后**：整库重建 / 改设置触发的按篇投递没有可投的篇。
    #:
    #: 单独一个码而不是回 ``200 + {dispatched: 0, total: 0}``：后者在界面上只会弹一句
    #: 「这次投递 0 / 0 篇」，没有原因也没有下一步。而且 20038 的语义随库级构建整层删除变过
    #: 一次——旧文案说的「全是失败 / 排队中 / 正在索引」不再适用（那些篇根本不进重建集合，
    #: 判据只取 ``SUCCESS`` 里产物落后的那批）；现在它只表示「判据说没有该重切的篇」。
    #:
    #: 为什么不复用 20015：20015 在前端有一句**固定的**通用文案（「参数非法，或几项配置之间
    #: 互相矛盾」），它会把后端写的那句「这个库里没有需要重建的文档」整个盖掉，而这句话正是
    #: 用户要知道的下一步。
    KB_NOTHING_TO_REBUILD = 20038

    # 3xxxx：存储段，仅预留，不定义成员（全表里没有调用点）。

    # 4xxxx：外部模型 / 服务请求失败
    API_TIMEOUT = 40001
    API_REQUEST_ERROR = 40002
    EMBEDDING_FAILED = 40003
    RERANK_FAILED = 40004
    LLM_FAILED = 40005

    # 5xxxx：服务异常与系统级失败
    SERVER_ERROR = 50000
    SYSTEM_BUSY = 50001
    RETRIEVAL_FAILED = 50002
    # 第 13 步：改写 / 分解 / Research 四阶段的大模型调用失败统一为**整单失败**，
    # 不再有「失败即降级」的静默路径。可重试语义写在响应契约里，调用方按码重试。
    MODEL_CALL_FAILED = 50003


def default_message(code: ErrorCode) -> str:
    """缺省提示：枚举名小写、下划线转空格，供 AppError 与响应构造器共用。"""

    return code.name.lower().replace("_", " ")


class AppError(Exception):
    """业务失败（可预期、调用方能处理）的载体，属于领域概念，不是 HTTP 异常。

    业务层只写 ``raise AppError(ErrorCode.X)``；「翻译成响应」由各入口适配器负责。
    """

    def __init__(
        self,
        code: ErrorCode,
        msg: str | None = None,
        *,
        data: Any = None,
    ) -> None:
        self.code = code
        self.msg = msg if msg is not None else default_message(code)
        self.data = data
        super().__init__(self.msg)

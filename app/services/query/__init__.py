"""查询优化（GLOSSARY「查询优化」）：召回之前对 query 做规范化的两步。

``改写 → 同义词扩展``，两个开关都默认关闭，关掉时 ``effective_query`` 与
``search_query`` 都等于原句，检索行为与票 4 逐字段一致。

* ``rewrite``：**口语 → 文档表述**，调大模型（fast 模型角色），失败即降级；
* ``synonyms``：**纯函数**，按知识库级词表把 ``expand`` 追加到 query 末尾。

它**不是第七类 Provider**：两者都没有自己的外部依赖（改写借 ``LLMProvider`` 出网），
归属业务编排层（ADR-0002）。
"""

from app.services.query.optimizer import QueryProcessResult, optimize_query
from app.services.query.rewrite import (
    REASON_REWRITE_FAILED,
    RewriteOutcome,
    rewrite_query,
)
from app.services.query.synonyms import (
    SYNONYMS_SETTING_KEY,
    SynonymGroup,
    collect_synonym_groups,
    expand_synonyms,
    parse_synonym_groups,
)

__all__ = [
    "REASON_REWRITE_FAILED",
    "SYNONYMS_SETTING_KEY",
    "QueryProcessResult",
    "RewriteOutcome",
    "SynonymGroup",
    "collect_synonym_groups",
    "expand_synonyms",
    "optimize_query",
    "parse_synonym_groups",
    "rewrite_query",
]

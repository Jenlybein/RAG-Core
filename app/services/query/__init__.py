"""查询加工：召回之前对 query 做的两步（第 13 步起由**端点**决定做不做）。

* ``/rag/retrieve``：``direct_query``，恒等变换，零模型调用；
* ``/rag/rewrite``：``optimize_for_rewrite``，先改写（单一 ``default`` 模型角色），
  再按知识库级词表做同义词扩展。

它**不是第七类 Provider**：两者都没有自己的外部依赖（改写借 ``LLMProvider`` 出网），
归属业务编排层（ADR-0002）。
"""

from app.services.query.optimizer import (
    QueryProcessResult,
    direct_query,
    optimize_for_rewrite,
)
from app.services.query.rewrite import (
    QueryRewrite,
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
    "SYNONYMS_SETTING_KEY",
    "QueryProcessResult",
    "QueryRewrite",
    "RewriteOutcome",
    "SynonymGroup",
    "collect_synonym_groups",
    "direct_query",
    "expand_synonyms",
    "optimize_for_rewrite",
    "parse_synonym_groups",
    "rewrite_query",
]

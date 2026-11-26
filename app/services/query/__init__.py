"""查询加工：召回之前对 query 做的那一步（第 13 步起由**端点**决定做不做）。

* ``/rag/retrieve``：``direct_query``，恒等变换，零模型调用；
* ``/rag/rewrite``：``optimize_for_rewrite``，改写一次（单一 ``default`` 模型角色），
  产出「原句 + 改写句」这一组召回用句。

第 15 步起**同义词扩展已下线**：它的词表是库级人工配置，覆盖面与维护成本都不划算，
而字面那一路现在吃的是「原句 + 改写句」这两句真实存在的话。

它**不是第七类 Provider**：改写借 ``LLMProvider`` 出网，没有自己的外部依赖，
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

__all__ = [
    "QueryProcessResult",
    "QueryRewrite",
    "RewriteOutcome",
    "direct_query",
    "optimize_for_rewrite",
    "rewrite_query",
]

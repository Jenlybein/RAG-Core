"""响应契约里的「分列」字段：五个分数 / 名次的唯一填充点。

向量通道、关键词通道、RRF 融合、重排 Provider 都会产出切片字典。这五个字段
（``vector_score`` / ``bm25_score`` / ``vector_rank`` / ``bm25_rank`` / ``rrf_score``）
是**契约字段**：少一个是契约破坏，多一个是误导，抄四份迟早漂移。

**为什么住在 ``app/utils/`` 而不是 ``app/services/retrieval/``**：三个调用点分属
服务层与 Provider 层。放进服务层，``app/providers/rerank`` 就得反过来 import
``app.services``——那会拆掉 ADR-0002 / ADR-0011 建的那条边界（Provider 只认识自己的
ABC，不知道业务）。``app/utils/`` 下的模块不依赖 ``app`` 内任何其它层
（``text_splitter.py`` / ``retrieval_text.py`` 就在这个位置），谁都能 import，谁都
不会被它拖进业务依赖。
"""

from __future__ import annotations

from typing import Any

# 名字与顺序**只在这里定义一次**。
SCORE_COLUMNS = ("vector_score", "bm25_score", "vector_rank", "bm25_rank", "rrf_score")


def with_score_columns(chunk: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """在 ``chunk`` 上补齐五个分列字段，返回新字典（不改入参）。

    ``overrides`` 里给了哪个字段就用哪个值；没给的沿用 ``chunk`` 自己已有的值
    （重排就是这么「只搬运、不重算」的）；两者都没有就补 ``None`` ——未命中的那一路
    是 ``None`` 而不是 0，这是契约（0 会被读成「命中了但很不相关」）。

    四个调用点（向量通道、关键词通道、RRF 融合、重排 Provider）都走这里，
    字段集合与默认值因此只有一个来源。
    """

    result = {**chunk}
    for name in SCORE_COLUMNS:
        if name in overrides:
            result[name] = overrides[name]
        elif name not in result:
            result[name] = None
    return result

"""检索编排的零件（票 4 起）与检索内核（第 11 步 #140）。

ADR-0011 的边界图里，这一层是**我们自写**的：

* ``fusion``：RRF 融合，纯函数，可手算验证、可离线单测；
* ``pipeline``：两路并行召回、降级判定、融合调用——它负责「知道发生了什么」，
  但不负责把它翻译成 HTTP 响应（那是 ``rag_service`` 的事）；
* ``rerank``：融合之后、组装之前的那一步，失败即降级并留下原因；
* ``vector_recall``：向量一路的「多查询并集」，两条调用方共用同一份去重口径；
* ``collapse``：交付口径本身——一条结果代表一节（ADR-0013）；
* ``kernel``：把上面这些串成**一次检索**（召回 → 融合 →（注入位）→ 重排 → TopK →
  折叠）。它是 ADR-0028 定下的边界：``/rag/retrieve`` 与受控 Research 共用这一份实现。
"""

from app.services.retrieval.fusion import fuse_by_rrf
from app.services.retrieval.kernel import KernelOutcome, retrieve_kernel

__all__ = ["KernelOutcome", "fuse_by_rrf", "retrieve_kernel"]

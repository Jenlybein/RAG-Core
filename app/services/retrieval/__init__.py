"""检索编排的零件（票 4 起）。

ADR-0011 的边界图里，这一层是**我们自写**的：

* ``fusion``：RRF 融合，纯函数，可手算验证、可离线单测；
* ``pipeline``：两路并行召回、降级判定、融合调用——它负责「知道发生了什么」，
  但不负责把它翻译成 HTTP 响应（那是 ``rag_service`` 的事）。
"""

from app.services.retrieval.fusion import fuse_by_rrf

__all__ = ["fuse_by_rrf"]

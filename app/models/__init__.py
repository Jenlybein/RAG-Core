"""ORM 模型统一导出。

alembic 的 env.py 要 import 这个模块，确保四张表都注册到 Base.metadata，
否则 --autogenerate 看不到它们。
"""

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.models.retrieval_log import RetrievalLog

__all__ = ["Chunk", "Document", "KnowledgeBase", "RetrievalLog"]

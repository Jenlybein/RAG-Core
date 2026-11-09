"""ORM 模型统一导出。

alembic 的 env.py 要 import 这个模块，确保每张表都注册到 Base.metadata，
否则 --autogenerate 看不到它们。

前四张是业务表（知识库 / 文档 / 切片 / 检索日志）；第 5 步的身份五张表
（租户 / 套餐 / 账号 / 密钥配置 / API Key）同样在这里登记，一张都不能漏。
"""

from app.models.account import Account
from app.models.api_key import ApiKey
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.key_policy import KeyPolicy
from app.models.knowledge_base import KnowledgeBase
from app.models.plan import Plan
from app.models.retrieval_log import RetrievalLog
from app.models.tenant import Tenant

__all__ = [
    "Account",
    "ApiKey",
    "Chunk",
    "Document",
    "KeyPolicy",
    "KnowledgeBase",
    "Plan",
    "RetrievalLog",
    "Tenant",
]
